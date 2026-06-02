"""x-bookmark-triage — nightly X bookmark triage (HTTP entrypoint).

Flow:
  1. One-time: visit /oauth/start, authorize, /oauth/callback stores a rotating
     refresh token and records the current newest bookmark as the high-water
     mark (so only bookmarks added AFTER setup are ever processed).
  2. Nightly: a scheduler POSTs /run -> refresh token -> fetch bookmarks newer
     than the high-water mark -> Claude analysis -> one item each to the sink.

For running locally without a scheduler, see cli.py (`python cli.py run`).
"""
from __future__ import annotations

import os
import secrets

from flask import Flask, jsonify, redirect, request

import state
import x_client
from analyze import analyze
from pipeline import run_once, set_baseline
from sinks import sink_name

app = Flask(__name__)

CRON_SECRET = os.environ.get("CRON_SHARED_SECRET", "")
# Optional: gate /oauth/start so a stranger who finds the public URL can't
# re-authorize the (single-tenant) pipeline against their own account. Leave
# empty to disable the gate (fine when the service isn't publicly reachable).
SETUP_SECRET = os.environ.get("SETUP_SECRET", "")


def _redirect_uri() -> str:
    explicit = os.environ.get("REDIRECT_URI")
    if explicit:
        return explicit
    root = request.url_root.replace("http://", "https://").rstrip("/")
    return f"{root}/oauth/callback"


@app.get("/", endpoint="health_root")
@app.get("/healthz", endpoint="health_check")
def health():
    st = state.get_state()
    return jsonify(
        {
            "service": "x-bookmark-triage",
            "sink": sink_name(),
            "authorized": bool(st.get("refresh_token")),
            "baseline_done": bool(st.get("baseline_done")),
            "last_seen_id": st.get("last_seen_id"),
        }
    )


# --- one-time OAuth handshake ----------------------------------------------

@app.get("/oauth/start")
def oauth_start():
    if SETUP_SECRET and request.args.get("setup") != SETUP_SECRET:
        return "Forbidden — append ?setup=<SETUP_SECRET>", 403
    verifier, challenge = x_client.make_pkce()
    oauth_state = secrets.token_urlsafe(24)
    state.stash_verifier(oauth_state, verifier)
    return redirect(x_client.authorize_url(_redirect_uri(), oauth_state, challenge))


@app.get("/oauth/callback")
def oauth_callback():
    err = request.args.get("error")
    if err:
        return f"Authorization failed: {err}", 400
    code = request.args.get("code")
    oauth_state = request.args.get("state", "")
    if not code:
        return "Missing code", 400
    verifier = state.pop_verifier(oauth_state)
    if not verifier:
        return "Unknown or expired state — restart at /oauth/start", 400

    tokens = x_client.exchange_code(code, _redirect_uri(), verifier)
    access_token = tokens["access_token"]
    # Persist the refresh token FIRST — before any billed read — so a $0 credit
    # balance can't cost us the token. Baseline is then best-effort.
    state.patch_state({"refresh_token": tokens["refresh_token"]})

    try:
        me = x_client.get_me(access_token)
        state.patch_state({"username": me.get("username")})
        n = set_baseline(access_token, me["id"])
        return (
            f"✅ Authorized as @{me.get('username')}. Baseline set — {n} existing "
            "bookmark(s) marked as already-seen and excluded. Only bookmarks added "
            "from now on will be analyzed. You can close this tab."
        )
    except Exception as e:  # noqa: BLE001 — likely $0 credit balance; token is saved.
        return (
            "✅ Authorized and refresh token stored, but the baseline read failed "
            f"({e}). This is usually a $0 credit balance — add credits, then the "
            "first run (or a manual /run) will set the baseline. You can close this tab."
        ), 200


# --- nightly run ------------------------------------------------------------

@app.post("/run")
def run():
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    result = run_once()
    code = {"unauthorized": 409, "auth_error": 502, "folder_not_found": 404}.get(result.get("status"), 200)
    return jsonify(result), code


@app.post("/diag/analyze")
def diag_analyze():
    """Dry-run the analysis for a tweet id (no item written). ?id=<tweet_id>."""
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    tid = request.args.get("id")
    if not tid:
        return jsonify({"error": "pass ?id=<tweet_id>"}), 400
    st = state.get_state()
    tokens = x_client.refresh_access_token(st["refresh_token"])
    state.save_refresh_token(tokens["refresh_token"])
    bms = x_client.get_tweets_by_ids(tokens["access_token"], [tid])
    if not bms:
        return jsonify({"error": "tweet not found"}), 404
    folder = os.environ.get("BOOKMARK_FOLDER", "").strip() or None
    return jsonify({"folder_lens": folder, "analysis": analyze(bms[0], folder=folder)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
