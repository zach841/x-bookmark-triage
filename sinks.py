"""Pluggable output sinks: where an analyzed bookmark gets written.

Pick one with the ``SINK`` env var:

  markdown  (default)  append a formatted entry to a local Markdown file
  webhook              POST the bookmark + analysis as JSON to a URL
  airtable             create a row in an Airtable table

The Markdown and webhook sinks need no account and no cloud infra, so the app
runs instantly out of the box. Airtable (and other table backends reached via
webhook/n8n) are opt-in. See SETUP.md for free/open-source alternatives.

Each sink exposes ``write(bookmark, analysis) -> str`` returning a short
reference string (a row id, a file path, or an HTTP status) for logging.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

import requests


# --- shared formatting ------------------------------------------------------

def _excerpt(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _markdown_entry(bookmark: dict[str, Any], a: dict[str, Any]) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"## 📌 {a['title']}",
        "",
        f"- **Category:** {a['category']} · **Priority:** {a['priority']} · "
        f"**Relevance:** {a['relevance']} · **Actionable:** {a['actionable']}",
        f"- **Recommendation:** {a['recommendation']}",
        f"- **Link:** {bookmark['url']}",
    ]
    if bookmark.get("links"):
        lines.append("- **Referenced:** " + ", ".join(bookmark["links"]))
    lines += [
        "",
        "> " + _excerpt(bookmark.get("text", "")).replace("\n", "\n> "),
        f"> — {bookmark.get('author', '')}",
        "",
        f"<sub>captured {when} · bookmark {bookmark['id']}</sub>",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


# --- markdown sink (default, zero-account) ----------------------------------

MARKDOWN_PATH = os.environ.get("MARKDOWN_PATH", "./bookmarks.md")
_MD_HEADER = "# Bookmark triage\n\nNewest entries are appended below.\n\n---\n\n"


def _write_markdown(bookmark: dict[str, Any], a: dict[str, Any]) -> str:
    path = MARKDOWN_PATH
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8") as fh:
        if new_file:
            fh.write(_MD_HEADER)
        fh.write(_markdown_entry(bookmark, a))
    return path


# --- webhook sink (zero-account) --------------------------------------------

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_AUTH_HEADER = os.environ.get("WEBHOOK_AUTH_HEADER", "")  # e.g. "Bearer xyz"


def _write_webhook(bookmark: dict[str, Any], a: dict[str, Any]) -> str:
    if not WEBHOOK_URL:
        raise RuntimeError("SINK=webhook but WEBHOOK_URL is not set")
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_AUTH_HEADER:
        headers["Authorization"] = WEBHOOK_AUTH_HEADER
    payload = {
        "title": a["title"],
        "category": a["category"],
        "priority": a["priority"],
        "relevance": a["relevance"],
        "actionable": a["actionable"],
        "recommendation": a["recommendation"],
        "bookmark": {
            "id": bookmark["id"],
            "url": bookmark["url"],
            "author": bookmark.get("author", ""),
            "created_at": bookmark.get("created_at", ""),
            "text": bookmark.get("text", ""),
            "links": bookmark.get("links", []),
        },
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(WEBHOOK_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return f"webhook {resp.status_code}"


# --- airtable sink (opt-in) -------------------------------------------------
# By default fields are written by NAME (Task, Status, Priority, Category,
# Notes, Created At) so any Airtable/NocoDB/Baserow-via-API table with those
# columns just works. To target a table by field IDs instead (robust to column
# renames), set AIRTABLE_FIELD_MAP to a JSON object mapping the logical keys
# below to field ids, e.g. {"task":"fld...","status":"fld...",...}.

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", os.environ.get("TODOS_BASE_ID", ""))
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_TABLE_ID", os.environ.get("TODOS_TABLE_ID", ""))
AIRTABLE_STATUS_VALUE = os.environ.get("AIRTABLE_STATUS_VALUE", "Open")

_DEFAULT_FIELD_MAP = {
    "task": "Task",
    "status": "Status",
    "priority": "Priority",
    "category": "Category",
    "notes": "Notes",
    "created_at": "Created At",
}


def _airtable_field_map() -> dict[str, str]:
    raw = os.environ.get("AIRTABLE_FIELD_MAP", "")
    if raw.strip():
        return json.loads(raw)
    return dict(_DEFAULT_FIELD_MAP)


def _airtable_notes(bookmark: dict[str, Any], a: dict[str, Any]) -> str:
    return (
        f"{a['recommendation']}\n\n"
        f"— Relevance: {a['relevance']} | actionable: {a['actionable']}\n"
        f"{bookmark['url']}\n\n"
        f"“{_excerpt(bookmark.get('text', ''))}”\n"
        f"— {bookmark.get('author', '')}\n\n"
        f"[captured via x-bookmark-triage]"
    )


def _write_airtable(bookmark: dict[str, Any], a: dict[str, Any]) -> str:
    if not AIRTABLE_API_KEY:
        raise RuntimeError("SINK=airtable but AIRTABLE_API_KEY is not set")
    if not (AIRTABLE_BASE_ID and AIRTABLE_TABLE_ID):
        raise RuntimeError("SINK=airtable requires AIRTABLE_BASE_ID and AIRTABLE_TABLE_ID")
    fm = _airtable_field_map()
    fields: dict[str, Any] = {
        fm["task"]: ("📌 " + a["title"])[:240],
        fm["status"]: AIRTABLE_STATUS_VALUE,
        fm["priority"]: a["priority"],
        fm["category"]: [a["category"]],  # multi-select expects a list
        fm["notes"]: _airtable_notes(bookmark, a),
        fm["created_at"]: datetime.now(timezone.utc).isoformat(),
    }
    # Optional extra columns, only set when the field map names them (e.g. a
    # "recurring" / "reminder_sent" pair some todo schemas expect).
    if "recurring" in fm:
        fields[fm["recurring"]] = os.environ.get("AIRTABLE_RECURRING_VALUE", "None")
    if "reminder_sent" in fm:
        fields[fm["reminder_sent"]] = False

    resp = requests.post(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}",
        headers={
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"records": [{"fields": fields}]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["records"][0]["id"]


# --- dispatch ---------------------------------------------------------------

_SINKS: dict[str, Callable[[dict, dict], str]] = {
    "markdown": _write_markdown,
    "webhook": _write_webhook,
    "airtable": _write_airtable,
}


def sink_name() -> str:
    return os.environ.get("SINK", "markdown").strip().lower()


def write_item(bookmark: dict[str, Any], analysis: dict[str, Any]) -> str:
    name = sink_name()
    fn = _SINKS.get(name)
    if not fn:
        raise RuntimeError(f"unknown SINK '{name}' (choose: {', '.join(_SINKS)})")
    return fn(bookmark, analysis)
