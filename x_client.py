"""Thin X (Twitter) API v2 client: OAuth2 PKCE + bookmarks reads.

Auth model: OAuth 2.0 Authorization Code with PKCE, confidential client
(HTTP Basic with client_id:client_secret). Scopes:
``tweet.read users.read bookmark.read offline.access``.

The ``offline.access`` scope yields a refresh token so the nightly job runs
unattended. Refresh tokens rotate on use — callers MUST persist the returned
refresh_token (see state.save_refresh_token) right away.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any, Optional

import requests

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API_BASE = "https://api.x.com/2"
SCOPES = "tweet.read users.read bookmark.read offline.access"

CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")


def _basic_auth() -> dict[str, str]:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


# --- PKCE helpers -----------------------------------------------------------

def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for an S256 PKCE flow."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --- token exchange / refresh ----------------------------------------------

def exchange_code(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# --- reads ------------------------------------------------------------------

def get_me(access_token: str) -> dict[str, Any]:
    resp = requests.get(
        f"{API_BASE}/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _bookmarks_page(
    access_token: str, user_id: str, pagination_token: Optional[str]
) -> dict[str, Any]:
    params = {
        "max_results": 100,
        "tweet.fields": "created_at,note_tweet,entities,referenced_tweets,public_metrics,lang",
        "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys",
        "user.fields": "username,name",
        "media.fields": "alt_text,type,url",
    }
    if pagination_token:
        params["pagination_token"] = pagination_token
    resp = requests.get(
        f"{API_BASE}/users/{user_id}/bookmarks",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def iter_new_bookmarks(
    access_token: str, user_id: str, known_ids, max_pages: int = 10
):
    """Yield normalized bookmarks newest-first, stopping as soon as a bookmark
    whose id is in ``known_ids`` is reached (the barrier set), or when
    ``max_pages`` is exhausted. Bookmarks are returned most-recently-bookmarked
    first, so the first id we recognize marks the boundary of already-seen
    territory — a SET (not a single anchor) so removing one bookmark can't
    erase the boundary.
    """
    known = set(known_ids or ())
    token: Optional[str] = None
    pages = 0
    while pages < max_pages:
        page = _bookmarks_page(access_token, user_id, token)
        tweets = page.get("data", [])
        if not tweets:
            break
        users = {u["id"]: u for u in page.get("includes", {}).get("users", [])}
        media = {m["media_key"]: m for m in page.get("includes", {}).get("media", [])}
        ref_tweets = {t["id"]: t for t in page.get("includes", {}).get("tweets", [])}
        for t in tweets:
            if t["id"] in known:
                return
            yield _normalize(t, users, media, ref_tweets)
        token = page.get("meta", {}).get("next_token")
        pages += 1
        if not token:
            break


def list_folders(access_token: str, user_id: str) -> list[dict]:
    """List the user's bookmark folders (id + name). Note: X hard-caps this at
    20 folders and ignores max_results."""
    resp = requests.get(
        f"{API_BASE}/users/{user_id}/bookmarks/folders",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", []) or []


def folder_bookmark_ids(access_token: str, user_id: str, folder_id: str) -> list[str]:
    """Bookmark ids inside a folder. X hard-caps this at 20 with no pagination."""
    resp = requests.get(
        f"{API_BASE}/users/{user_id}/bookmarks/folders/{folder_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return [t["id"] for t in (resp.json().get("data", []) or [])]


def resolve_folder_id(access_token: str, user_id: str, name: str) -> Optional[str]:
    """Find a bookmark folder id by (case-insensitive) name."""
    for f in list_folders(access_token, user_id):
        if (f.get("name") or "").strip().lower() == name.strip().lower():
            return f.get("id")
    return None


def get_tweets_by_ids(access_token: str, ids: list[str]) -> list[dict]:
    """Hydrate full normalized content for specific tweet ids (max 100). Used to
    enrich folder bookmark ids, since the folder endpoint rejects expansions."""
    if not ids:
        return []
    params = {
        "ids": ",".join(ids[:100]),
        "tweet.fields": "created_at,note_tweet,entities,referenced_tweets,public_metrics,lang",
        "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys",
        "user.fields": "username,name",
        "media.fields": "alt_text,type,url",
    }
    resp = requests.get(
        f"{API_BASE}/tweets",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    page = resp.json()
    tweets = page.get("data", []) or []
    users = {u["id"]: u for u in page.get("includes", {}).get("users", [])}
    media = {m["media_key"]: m for m in page.get("includes", {}).get("media", [])}
    ref_tweets = {t["id"]: t for t in page.get("includes", {}).get("tweets", [])}
    return [_normalize(t, users, media, ref_tweets) for t in tweets]


def recent_ids(access_token: str, user_id: str, max_pages: int = 1) -> list[str]:
    """Return the ids of the most-recently-bookmarked posts (newest-first),
    used to seed the baseline barrier set without processing anything."""
    ids: list[str] = []
    token: Optional[str] = None
    pages = 0
    while pages < max_pages:
        page = _bookmarks_page(access_token, user_id, token)
        ids.extend(t["id"] for t in page.get("data", []))
        token = page.get("meta", {}).get("next_token")
        pages += 1
        if not token:
            break
    return ids


def _normalize(t, users, media, ref_tweets) -> dict[str, Any]:
    author = users.get(t.get("author_id"), {})
    # note_tweet holds the full text for tweets longer than 280 chars.
    text = (t.get("note_tweet") or {}).get("text") or t.get("text") or ""
    urls = [
        u.get("expanded_url")
        for u in (t.get("entities", {}) or {}).get("urls", [])
        if u.get("expanded_url") and "/status/" not in u.get("expanded_url", "")
    ]
    quoted_text = ""
    for ref in t.get("referenced_tweets", []) or []:
        rt = ref_tweets.get(ref.get("id"))
        if rt and ref.get("type") in ("quoted", "replied_to"):
            quoted_text = rt.get("text", "")
            break
    username = author.get("username", "i")
    return {
        "id": t["id"],
        "url": f"https://x.com/{username}/status/{t['id']}",
        "author": f"{author.get('name', '')} (@{username})".strip(),
        "created_at": t.get("created_at", ""),
        "text": text,
        "quoted_text": quoted_text,
        "links": list(dict.fromkeys([u for u in urls if u])),
    }
