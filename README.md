# x-bookmark-triage

Your X (Twitter) bookmarks pile up and you never look at them again. This reads
**new** bookmarks (only ones added after you set it up), has Claude judge if and
how each one actually applies to your work or life, and writes a short,
actionable note for each — to a Markdown file, a webhook, or a table (Airtable
and open-source equivalents). Run it nightly and your bookmarks turn into a
triaged todo list instead of a graveyard.

One note per bookmark, recommend-only. Many bookmarks are just interesting —
it'll say so rather than inventing busywork.

## Why the X API (not Premium / Grok / a browser bot)

- X **Premium / Premium+ / SuperGrok / the Grok API do _not_ give you your
  bookmarks.** The Grok API is an LLM product, unrelated to reading your data.
- The **X API** is pay-per-use (since Feb 2026): no monthly minimum, ~$0.005 per
  post read. At a handful of bookmarks a day that's roughly **$1–3/month**. No
  fragile logged-in-browser scraping.

You'll need your own free X developer app and an Anthropic API key.

## Two ways to run it

**A. Locally, zero accounts beyond X + Anthropic** (Markdown file, state in a
local JSON file). Good for a laptop/VM with cron. → see [SETUP.md](SETUP.md).

```bash
cp .env.example .env            # fill in X + Anthropic keys
cp persona.example.txt persona.txt   # tell it who you are
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# authorize once (opens the X consent screen):
SETUP_SECRET=dev REDIRECT_URI=http://127.0.0.1:8080/oauth/callback python app.py
#   → visit http://127.0.0.1:8080/oauth/start?setup=dev
# then, nightly (cron/launchd):
python cli.py run
```

**B. Serverless** on Google Cloud Run (state in Firestore, triggered by Cloud
Scheduler). → see [SETUP.md](SETUP.md) §Deploy.

## Where your bookmarks go (`SINK`)

| `SINK` | Account needed | Notes |
|---|---|---|
| `markdown` *(default)* | none | appends to `bookmarks.md` |
| `webhook` | none | POSTs JSON to any URL (n8n, Zapier, Make, your own) |
| `airtable` | Airtable / NocoDB / Baserow | a row per bookmark |

Free and open-source Airtable alternatives (NocoDB, Baserow, Teable, Notion,
Google Sheets) are covered in [SETUP.md](SETUP.md) §Output options — most expose
an Airtable-style REST API or work via the webhook sink.

## How it decides what's "new"

- **All-bookmarks mode** (default): on first authorize it records your current
  bookmarks as a baseline and processes **none** of them. Only bookmarks added
  afterward are analyzed. The boundary is a *set* of recent ids, so un-bookmarking
  one post can't break it.
- **Folder mode** (`BOOKMARK_FOLDER=Reading`): only triages one X bookmark
  folder. Here the folder *is* the signal — anything you file into it gets
  analyzed (even old bookmarks), once. (X caps the folder endpoint at ~20 items
  with no pagination, so add fewer than ~20/day to a watched folder.)

## Config

Everything is environment variables — see [`.env.example`](.env.example) for the
full annotated list. The essentials: `X_CLIENT_ID`, `X_CLIENT_SECRET`,
`ANTHROPIC_API_KEY`, `SINK`, and a `persona.txt` describing you.

## Endpoints (deployed mode)

| Route | Auth | Purpose |
|---|---|---|
| `GET /` | none | status (authorized? baseline? sink?) |
| `GET /oauth/start` | `?setup=<SETUP_SECRET>` | one-time OAuth2 PKCE authorize |
| `GET /oauth/callback` | PKCE state | stores rotating refresh token + sets baseline |
| `POST /run` | `x-cron-secret` header | one triage pass |
| `POST /diag/analyze?id=<tweet_id>` | `x-cron-secret` | dry-run analysis, writes nothing |

## License

MIT — see [LICENSE](LICENSE).
