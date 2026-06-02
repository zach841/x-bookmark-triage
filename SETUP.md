# Setup

Two prerequisites no matter how you run it:

1. **An X developer app** (free) for OAuth.
2. **An Anthropic API key** for the analysis — <https://console.anthropic.com>.

Plus a few dollars of X API credit (~$1–3/month at light use).

---

## 1. Create your X developer app

1. Go to <https://developer.x.com> and create a project + app (the free tier is
   fine for owned-bookmark reads).
2. In the app's **User authentication settings**:
   - **App permissions:** Read
   - **Type of App:** *Web App, Automated App or Bot* (a **confidential client**)
   - **Callback / Redirect URI:**
     - Local: `http://127.0.0.1:8080/oauth/callback`
     - Cloud Run: `https://<your-service-url>/oauth/callback` (you get this after
       the first deploy — set it then)
   - **Website URL:** anything (your GitHub repo is fine)
3. Copy the **OAuth 2.0 Client ID** and **Client Secret**.
4. Add a few dollars of credit to the X API account so reads succeed.

The app requests these scopes: `tweet.read users.read bookmark.read offline.access`.

---

## 2. Run it locally (zero extra accounts)

```bash
cp .env.example .env
#   set X_CLIENT_ID, X_CLIENT_SECRET, ANTHROPIC_API_KEY
#   SINK defaults to markdown, STATE_BACKEND defaults to file — nothing else needed
cp persona.example.txt persona.txt    # edit: who you are, what's relevant

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# one-time authorize — runs the web app just long enough to click "Authorize":
set -a; source .env; set +a
SETUP_SECRET=dev REDIRECT_URI=http://127.0.0.1:8080/oauth/callback python app.py
#   open http://127.0.0.1:8080/oauth/start?setup=dev  → authorize → "Baseline set"
#   (Ctrl-C the server afterward)
```

That writes your rotating refresh token into `./state.json`. From then on:

```bash
set -a; source .env; set +a
python cli.py run        # one triage pass → appends to ./bookmarks.md
python cli.py status     # show auth/baseline state
```

Schedule `python cli.py run` nightly with cron (`crontab -e`):

```cron
0 2 * * *  cd /path/to/x-bookmark-triage && ./.venv/bin/python cli.py run >> run.log 2>&1
```

> Note: the file backend (`state.json`, `bookmarks.md`) only persists on a real
> disk. On serverless/ephemeral hosts use `STATE_BACKEND=firestore` (below).

---

## 3. Output options (where bookmarks land)

Set `SINK` in `.env`.

### `markdown` (default, no account)
Appends a formatted entry to `MARKDOWN_PATH` (default `./bookmarks.md`). Open it
in any editor, sync it via Obsidian/iCloud/Dropbox, or commit it to a repo.

### `webhook` (no account)
POSTs each result as JSON to `WEBHOOK_URL` (optionally with `WEBHOOK_AUTH_HEADER`).
Wire it into anything: **n8n**, **Zapier**, **Make**, a Discord/Slack webhook, or
your own endpoint. Payload shape:

```json
{
  "title": "...", "category": "...", "priority": "High|Medium|Low",
  "relevance": "work|personal|both|none", "actionable": true,
  "recommendation": "...",
  "bookmark": {"id","url","author","created_at","text","links": []},
  "captured_at": "ISO8601"
}
```

### `airtable` — and free / open-source alternatives
Set `SINK=airtable`, `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID`.
By default it writes columns **by name**: `Task`, `Status`, `Priority`,
`Category` (multi-select), `Notes`, `Created At`. Create a table with those
columns and you're set. (To target columns by id instead — robust to renames —
set `AIRTABLE_FIELD_MAP` to a JSON map; see `.env.example`.)

**Don't use Airtable?** These give you the same "rows in a table" without the
Airtable bill or lock-in:

| Tool | Free? | How to connect |
|---|---|---|
| **NocoDB** | ✅ open-source, self-host (one `docker run`) | Exposes an **Airtable-compatible REST API v2**. Point the airtable sink's base URL at your NocoDB instance, or use the webhook sink. |
| **Baserow** | ✅ open-source + free hosted tier | REST API per table. Easiest via the **webhook** sink → a Baserow "create row" automation, or a tiny adapter. |
| **Teable** | ✅ open-source | Airtable-like; REST API. Use the webhook sink. |
| **Grist** | ✅ open-source | Spreadsheet-DB with a REST API; webhook sink. |
| **Notion** | Free tier, official API | Use the **webhook** sink → an n8n/Make "Notion: create database item" step (no code). |
| **Google Sheets** | ✅ free | Webhook sink → Apps Script web app, or an n8n/Zapier "append row" step. |
| **Plain Markdown** | ✅ none | Just use `SINK=markdown`. |

For NocoDB specifically (closest drop-in): create a table with the six columns
above, generate an API token, and either (a) use `SINK=webhook` with a small
flow, or (b) keep `SINK=airtable` and override the Airtable API base to your
NocoDB v2 endpoint. The webhook path is the least fiddly for everything except
Airtable itself.

---

## 4. Deploy to Cloud Run (optional, serverless)

Runs unattended with no machine of your own. State goes in Firestore.

```bash
cp deploy.env.example deploy.env     # set GCP_PROJECT, REGION, SINK, secrets, etc.
./bootstrap.sh                       # SA + IAM + secrets (placeholder X creds + auto cron secret)
./deploy.sh                          # build + deploy → prints the service URL
```

Then:

1. Set your X app's redirect URI to `<SERVICE_URL>/oauth/callback`.
2. Put the real X creds into Secret Manager (the `bootstrap.sh` output prints the
   exact commands), and `./deploy.sh` again.
3. Set a `SETUP_SECRET` (in `deploy.env`) and visit
   `<SERVICE_URL>/oauth/start?setup=<SETUP_SECRET>` to authorize.
4. Create the nightly schedule: `./bootstrap.sh --scheduler "<SERVICE_URL>"`.

`STATE_BACKEND=firestore` is required on Cloud Run (the container disk is
ephemeral). Provide Firestore access via `GOOGLE_SERVICE_ACCOUNT_KEY` (a SA JSON
in Secret Manager) or the runtime SA's own ADC + `roles/datastore.user`.

### Security note for public deployments
Cloud Run services here are `--allow-unauthenticated` so X can reach the OAuth
callback. The app is **single-tenant** (one set of credentials). Always set
`SETUP_SECRET` on a public deployment — otherwise anyone who discovers the URL
could hit `/oauth/start` and re-authorize the pipeline against their own account.
`/run` and `/diag/*` are already protected by `CRON_SHARED_SECRET`.

---

## Notes

- **Rotating refresh tokens:** X invalidates the refresh token on every use; the
  new one is persisted immediately on each run. If a run fails mid-refresh,
  re-authorize at `/oauth/start`.
- **`MAX_PER_RUN`** (default 100) caps how many bookmarks are processed per run.
- **Cost control:** analysis uses `ANALYSIS_MODEL` (default `claude-sonnet-4-6`);
  switch to a smaller model to cut cost.
- **Manual run (deployed):**
  `curl -XPOST <SERVICE_URL>/run -H "x-cron-secret: <CRON_SHARED_SECRET>"`
