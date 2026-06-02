#!/usr/bin/env bash
# Deploy x-bookmark-triage to Cloud Run. Config comes from ./deploy.env
# (copy deploy.env.example). For local-only use you don't need this — see SETUP.md.
set -euo pipefail

cd "$(dirname "$0")"
if [[ ! -f deploy.env ]]; then
  echo "No deploy.env found. Copy deploy.env.example -> deploy.env and edit it." >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source deploy.env; set +a

: "${GCP_PROJECT:?set GCP_PROJECT in deploy.env}"
: "${REGION:?set REGION in deploy.env}"
: "${SERVICE:?set SERVICE in deploy.env}"
: "${SECRETS:?set SECRETS in deploy.env}"

# Assemble non-secret env. Use a '@' delimiter (^@^) so commas inside JSON
# (AIRTABLE_FIELD_MAP) and CATEGORIES don't get split by gcloud.
ENV_PAIRS=()
add() { if [[ -n "${2:-}" ]]; then ENV_PAIRS+=("$1=$2"); fi; }  # always returns 0 (set -e safe)
add SINK "${SINK:-}"
add STATE_BACKEND "${STATE_BACKEND:-}"
add FIREBASE_PROJECT_ID "${FIREBASE_PROJECT_ID:-}"
add ANALYSIS_MODEL "${ANALYSIS_MODEL:-}"
add BOOKMARK_FOLDER "${BOOKMARK_FOLDER:-}"
add MAX_PER_RUN "${MAX_PER_RUN:-}"
add CATEGORIES "${CATEGORIES:-}"
add AIRTABLE_BASE_ID "${AIRTABLE_BASE_ID:-}"
add AIRTABLE_TABLE_ID "${AIRTABLE_TABLE_ID:-}"
add AIRTABLE_STATUS_VALUE "${AIRTABLE_STATUS_VALUE:-}"
add AIRTABLE_FIELD_MAP "${AIRTABLE_FIELD_MAP:-}"
add WEBHOOK_URL "${WEBHOOK_URL:-}"
add SETUP_SECRET "${SETUP_SECRET:-}"
ENV_ARG="^@^$(IFS=@; echo "${ENV_PAIRS[*]}")"

SA_FLAG=()
[[ -n "${RUNTIME_SA:-}" ]] && SA_FLAG=(--service-account "$RUNTIME_SA")

echo "Deploying $SERVICE to $GCP_PROJECT/$REGION (sink=${SINK:-markdown})..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --project "$GCP_PROJECT" \
  "${SA_FLAG[@]}" \
  --labels "app=${SERVICE}" \
  --allow-unauthenticated \
  --memory 512Mi \
  --max-instances 2 \
  --timeout 600 \
  --set-secrets "$SECRETS" \
  --set-env-vars "$ENV_ARG"

gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" --region "$REGION" --format='value(status.url)'
