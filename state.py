"""Persistent state for the bookmark triage job.

Two backends, chosen with ``STATE_BACKEND``:

  file       (default)  a single local JSON file at ``STATE_PATH``. Zero infra —
                        good for running locally or on any VM with a disk.
  firestore             a Firestore document. Good for serverless (Cloud Run),
                        where the local disk is ephemeral.

State holds:
  - rotating ``refresh_token`` (X invalidates the old one on every refresh, so we
    persist the new one immediately — before any other work — to avoid lockout),
  - ``user_id`` / ``username``,
  - ``baseline_done`` + a capped ``seen_ids`` barrier set (all-bookmarks mode),
  - ``folder_processed_ids`` (folder mode),
  - transient PKCE verifiers keyed by the OAuth ``state`` value during the
    one-time authorize handshake.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional

SEEN_CAP = 600  # how many recent bookmark ids to keep as the barrier set
STATE_BACKEND = os.environ.get("STATE_BACKEND", "file").strip().lower()


# ===========================================================================
# file backend
# ===========================================================================

STATE_PATH = os.environ.get("STATE_PATH", "./state.json")
_FILE_LOCK = threading.Lock()


def _file_load() -> dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _file_save(doc: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(STATE_PATH))
    os.makedirs(parent, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)  # atomic


# ===========================================================================
# firestore backend (lazy — only imported when selected)
# ===========================================================================

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "x_bookmarks")
STATE_DOC = "state"
OAUTH_DOC = "oauth"

_fs_client = None


def _fs_db():
    global _fs_client
    if _fs_client is not None:
        return _fs_client
    from google.cloud import firestore  # noqa: PLC0415 — lazy
    from google.oauth2 import service_account  # noqa: PLC0415

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if raw:
        creds = service_account.Credentials.from_service_account_info(json.loads(raw))
        _fs_client = firestore.Client(project=PROJECT_ID or None, credentials=creds)
    else:
        # ADC fallback (e.g. GOOGLE_APPLICATION_CREDENTIALS / workload identity).
        _fs_client = firestore.Client(project=PROJECT_ID or None)
    return _fs_client


def _fs_state_ref():
    return _fs_db().collection(COLLECTION).document(STATE_DOC)


# ===========================================================================
# unified state api
# ===========================================================================

def get_state() -> dict[str, Any]:
    if STATE_BACKEND == "firestore":
        snap = _fs_state_ref().get()
        return (snap.to_dict() or {}) if snap.exists else {}
    doc = _file_load()
    return {k: v for k, v in doc.items() if k != "_oauth"}


def patch_state(patch: dict[str, Any]) -> None:
    if STATE_BACKEND == "firestore":
        from google.cloud import firestore  # noqa: PLC0415

        merged = {**patch, "updated_at": firestore.SERVER_TIMESTAMP}
        _fs_state_ref().set(merged, merge=True)
        return
    with _FILE_LOCK:
        doc = _file_load()
        doc.update(patch)
        _file_save(doc)


def save_refresh_token(token: str) -> None:
    """Persist a rotated refresh token immediately (single-use tokens)."""
    patch_state({"refresh_token": token})


def get_refresh_token() -> Optional[str]:
    return get_state().get("refresh_token")


# --- all-bookmarks barrier set ---------------------------------------------

def get_seen_ids() -> list[str]:
    return get_state().get("seen_ids", [])


def set_seen_ids(ids: list[str]) -> None:
    """Store the barrier set (newest-first), capped. ``last_seen_id`` mirrors the
    newest id for at-a-glance status."""
    ids = ids[:SEEN_CAP]
    patch_state({"seen_ids": ids, "last_seen_id": ids[0] if ids else None})


def add_seen_ids(new_ids: list[str]) -> None:
    """Prepend freshly-processed ids to the barrier set (newest-first), capped."""
    set_seen_ids(new_ids + [i for i in get_seen_ids() if i not in set(new_ids)])


# --- folder mode: a SEPARATE processed-set ---------------------------------
# In folder mode the folder itself is the curation signal — dropping a tweet
# into the named folder means "analyze this." So folder mode does NOT inherit
# the all-bookmarks backlog barrier (``seen_ids``); it tracks only the folder
# items it has already written out, here. Keeps the two modes from poisoning
# each other.

def get_folder_processed() -> list[str]:
    return get_state().get("folder_processed_ids", [])


def add_folder_processed(ids: list[str]) -> None:
    """Record folder ids we've already processed (newest-first), capped."""
    existing = get_folder_processed()
    merged = list(ids) + [i for i in existing if i not in set(ids)]
    patch_state({"folder_processed_ids": merged[:SEEN_CAP]})


# --- transient OAuth PKCE handshake storage --------------------------------
# Firestore: one doc per handshake (doc id = "oauth-<state>"); doc ids tolerate
# the hyphens/underscores token_urlsafe produces, but map-field paths do not.
# File: stored under an "_oauth" map inside the single state file.

def stash_verifier(oauth_state: str, code_verifier: str) -> None:
    if STATE_BACKEND == "firestore":
        _fs_db().collection(COLLECTION).document(f"{OAUTH_DOC}-{oauth_state}").set(
            {"code_verifier": code_verifier}
        )
        return
    with _FILE_LOCK:
        doc = _file_load()
        doc.setdefault("_oauth", {})[oauth_state] = code_verifier
        _file_save(doc)


def pop_verifier(oauth_state: str) -> Optional[str]:
    if STATE_BACKEND == "firestore":
        ref = _fs_db().collection(COLLECTION).document(f"{OAUTH_DOC}-{oauth_state}")
        snap = ref.get()
        if not snap.exists:
            return None
        verifier = (snap.to_dict() or {}).get("code_verifier")
        ref.delete()
        return verifier
    with _FILE_LOCK:
        doc = _file_load()
        verifier = doc.get("_oauth", {}).pop(oauth_state, None)
        if verifier is not None:
            _file_save(doc)
        return verifier
