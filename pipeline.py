"""The core nightly job, independent of how it's triggered (HTTP or CLI).

``run_once`` refreshes the X token, fetches bookmarks newer than the high-water
mark (or the items in the configured folder), runs Claude analysis on each, and
writes one item per bookmark to the configured sink.
"""
from __future__ import annotations

import os
import traceback
from typing import Any

import state
import x_client
from analyze import analyze
from sinks import write_item

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "100"))
# When set, only bookmarks in this folder are considered. Empty = all bookmarks.
BOOKMARK_FOLDER = os.environ.get("BOOKMARK_FOLDER", "").strip()


def _process(new: list[dict], folder: str | None) -> tuple[list[dict], list[dict]]:
    results, errors = [], []
    for bm in reversed(new):  # oldest-first so output order matches bookmark order
        try:
            a = analyze(bm, folder=folder)
            ref = write_item(bm, a)
            results.append({"bookmark": bm["id"], "ref": ref, "title": a["title"], "category": a["category"]})
        except Exception as e:  # noqa: BLE001 — one bad bookmark shouldn't kill the run
            errors.append({"bookmark": bm["id"], "error": str(e)})
            traceback.print_exc()
    return results, errors


def run_once() -> dict[str, Any]:
    """Run one triage pass. Returns a JSON-able summary dict."""
    st = state.get_state()
    refresh_token = st.get("refresh_token")
    if not refresh_token:
        return {"error": "not authorized — visit /oauth/start", "status": "unauthorized"}

    try:
        tokens = x_client.refresh_access_token(refresh_token)
    except Exception as e:  # noqa: BLE001
        return {"error": f"token refresh failed: {e}", "hint": "re-authorize at /oauth/start", "status": "auth_error"}
    # Persist the rotated refresh token IMMEDIATELY (single-use).
    state.save_refresh_token(tokens["refresh_token"])
    access_token = tokens["access_token"]

    user_id = st.get("user_id")
    if not user_id:
        user_id = x_client.get_me(access_token)["id"]
        state.patch_state({"user_id": user_id})

    # --- folder mode -------------------------------------------------------
    if BOOKMARK_FOLDER:
        folder_id = x_client.resolve_folder_id(access_token, user_id, BOOKMARK_FOLDER) or st.get("folder_id")
        if not folder_id:
            return {"error": f"bookmark folder '{BOOKMARK_FOLDER}' not found", "status": "folder_not_found"}
        if folder_id != st.get("folder_id"):
            state.patch_state({"folder_id": folder_id})

        processed = set(state.get_folder_processed())
        new_ids = [i for i in x_client.folder_bookmark_ids(access_token, user_id, folder_id) if i not in processed]
        if not new_ids:
            return {"status": "ok", "new": 0, "folder": BOOKMARK_FOLDER}

        hydrated = x_client.get_tweets_by_ids(access_token, new_ids)
        hydrated_ids = {bm["id"] for bm in hydrated}
        batch = hydrated[:MAX_PER_RUN]
        results, errors = _process(batch, folder=BOOKMARK_FOLDER)
        # Mark everything we processed PLUS ids X couldn't hydrate (deleted /
        # protected / suspended — absent from the /2/tweets response). Without
        # the latter, an unhydratable folder id never lands in the processed set
        # and re-churns the folder endpoint on every run forever. Any truncated-
        # but-hydrated tail (only reachable if MAX_PER_RUN < folder size) is left
        # for the next run.
        done = [bm["id"] for bm in batch] + [i for i in new_ids if i not in hydrated_ids]
        state.add_folder_processed(done)
        return {"status": "ok", "new": len(batch), "created": len(results), "errors": errors, "folder": BOOKMARK_FOLDER}

    # --- all-bookmarks mode ------------------------------------------------
    if not st.get("baseline_done"):
        # Seed the barrier with the current bookmarks and process NOTHING. The
        # entire existing backlog falls behind this barrier; only bookmarks added
        # afterward are analyzed.
        ids = x_client.recent_ids(access_token, user_id, max_pages=1)
        state.set_seen_ids(ids)
        state.patch_state({"baseline_done": True})
        return {"status": "baseline_set", "barrier_size": len(ids), "folder": None}

    seen = set(state.get_seen_ids())
    new = list(x_client.iter_new_bookmarks(access_token, user_id, known_ids=seen))
    if not new:
        return {"status": "ok", "new": 0}

    # iter_new_bookmarks yields newest-first and the barrier stops at the FIRST
    # already-seen id, so the seen set has to stay one contiguous block. When
    # more than MAX_PER_RUN have piled up, drain the OLDEST chunk first (``new``
    # is newest-first, so its tail is oldest) and walk the barrier upward from
    # there. Taking the newest chunk instead (new[:MAX_PER_RUN]) would seal the
    # barrier above the older remainder, stranding it behind the stop-at-first-
    # seen boundary permanently. The remainder is picked up on subsequent runs.
    batch = new[-MAX_PER_RUN:]
    results, errors = _process(batch, folder=None)
    # Extend the barrier with every id we just saw (processed or errored).
    state.add_seen_ids([bm["id"] for bm in batch])
    out = {"status": "ok", "new": len(batch), "created": len(results), "errors": errors}
    backlog = len(new) - len(batch)
    if backlog:
        out["backlog_remaining"] = backlog  # more runs needed to drain
    return out


def set_baseline(access_token: str, user_id: str) -> int:
    """Seed the all-bookmarks barrier from the current bookmarks; process nothing.
    Returns the barrier size."""
    ids = x_client.recent_ids(access_token, user_id, max_pages=1)
    state.set_seen_ids(ids)
    state.patch_state({"user_id": user_id, "baseline_done": True})
    return len(ids)
