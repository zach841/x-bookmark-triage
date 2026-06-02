#!/usr/bin/env bash
# Idempotent one-time Cloud Run infra (only if you deploy to GCP):
#   - runtime service account + IAM (Secret Manager; Firestore if used)
#   - secrets (X client id/secret you fill in; cron secret auto-generated)
#   - nightly Cloud Scheduler job
#
# Config comes from ./deploy.env (copy deploy.env.example). Local-only users can
# skip this entirely — see SETUP.md.
set -euo pipefail

cd "$(dirname "$0")"
[[ -f deploy.env ]] && { set -a; source deploy.env; set +a; }

PROJECT="${GCP_PROJECT:?set GCP_PROJECT in deploy.env}"
SERVICE="${SERVICE:?set SERVICE in deploy.env}"
REGION="${REGION:-us-central1}"
SA="${RUNTIME_SA:-${SERVICE}-runtime@${PROJECT}.iam.gserviceaccount.com}"
SA_NAME="${SA%%@*}"

# Secret names this instance expects (match the SECRETS line in deploy.env).
X_ID_SECRET="${X_ID_SECRET:-X_CLIENT_ID}"
X_SECRET_SECRET="${X_SECRET_SECRET:-X_CLIENT_SECRET}"
CRON_SECRET_NAME="${CRON_SECRET_NAME:-CRON_SHARED_SECRET}"

echo "== runtime service account =="
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT" --display-name "${SERVICE} runtime"

echo "== secret accessor IAM =="
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA}" \
  --role "roles/secretmanager.secretAccessor" --condition=None >/dev/null

if [[ "${STATE_BACKEND:-}" == "firestore" && -n "${FIREBASE_PROJECT_ID:-}" ]]; then
  echo "== Firestore IAM (project ${FIREBASE_PROJECT_ID}) =="
  gcloud projects add-iam-policy-binding "$FIREBASE_PROJECT_ID" \
    --member "serviceAccount:${SA}" \
    --role "roles/datastore.user" --condition=None >/dev/null || \
    echo "  (skip — the GOOGLE_SERVICE_ACCOUNT_KEY identity already has Firestore access)"
fi

echo "== secrets =="
ensure_secret() {  # name [value]
  local name="$1"; local val="${2:-}"
  gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$name" --project "$PROJECT" --replication-policy=automatic
  if [[ -n "$val" ]] && ! gcloud secrets versions list "$name" --project "$PROJECT" --format='value(name)' | grep -q .; then
    printf '%s' "$val" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=-
  fi
}
ensure_secret "$X_ID_SECRET" "PENDING"
ensure_secret "$X_SECRET_SECRET" "PENDING"
ensure_secret "$CRON_SECRET_NAME" "$(openssl rand -hex 24)"

cat <<EOF

Next:
  1. Create your X developer app (developer.x.com), OAuth2 "Web App", scopes
     read; set redirect URI to <SERVICE_URL>/oauth/callback (URL after deploy).
     Put the OAuth2 Client ID + Secret into the secrets:
        printf '%s' '<client-id>'     | gcloud secrets versions add $X_ID_SECRET --project $PROJECT --data-file=-
        printf '%s' '<client-secret>' | gcloud secrets versions add $X_SECRET_SECRET --project $PROJECT --data-file=-
  2. ./deploy.sh
  3. Visit <SERVICE_URL>/oauth/start (append ?setup=<SETUP_SECRET> if you set one) and authorize.
  4. Create the nightly scheduler:  ./bootstrap.sh --scheduler "<SERVICE_URL>"
EOF

create_scheduler() {
  local url="$1"
  local cron_secret
  cron_secret=$(gcloud secrets versions access latest --secret="$CRON_SECRET_NAME" --project "$PROJECT")
  gcloud scheduler jobs describe "${SERVICE}-nightly" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 && \
    gcloud scheduler jobs delete "${SERVICE}-nightly" --project "$PROJECT" --location "$REGION" --quiet || true
  gcloud scheduler jobs create http "${SERVICE}-nightly" \
    --project "$PROJECT" --location "$REGION" \
    --schedule "${SCHEDULE:-0 2 * * *}" --time-zone "${TIME_ZONE:-America/Denver}" \
    --uri "${url}/run" --http-method POST \
    --headers "x-cron-secret=${cron_secret}" \
    --attempt-deadline 600s
}

if [[ "${1:-}" == "--scheduler" && -n "${2:-}" ]]; then
  create_scheduler "$2"
  echo "Scheduler ${SERVICE}-nightly created -> ${2}/run."
fi
