"""Claude analysis of a single X bookmark.

Given the bookmark content, decide whether and how it applies to you, then write
a concrete, recommend-only suggestion. Output is forced to JSON via tool use.

Two things are configurable so this isn't hardwired to one person:
  - the persona / context: ``PERSONA_FILE`` (a text file describing who you are
    and what's relevant), or the ``PERSONA`` env var. Falls back to a generic
    "triage my bookmarks" persona if neither is set. See persona.example.txt.
  - the categories: ``CATEGORIES`` env (comma-separated). Defaults to a generic
    set.
"""
from __future__ import annotations

import os
from typing import Any

from anthropic import Anthropic

MODEL = os.environ.get("ANALYSIS_MODEL", "claude-sonnet-4-6")

_GENERIC_PERSONA = """\
You are triaging X (Twitter) bookmarks for their owner. For each bookmark, decide
if and how it should be acted on in their work or personal life, then write a
concrete, actionable recommendation. Be honest: many bookmarks are just
interesting and warrant no action — say so rather than inventing busywork.

If the owner provided more context about themselves it appears below; weigh it
heavily when judging relevance.
"""

_DEFAULT_CATEGORIES = ["Work", "Personal", "Learning", "Finance", "Health", "Errand", "Reference"]


def _persona() -> str:
    inline = os.environ.get("PERSONA", "").strip()
    if inline:
        return inline
    path = os.environ.get("PERSONA_FILE", "./persona.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
            if text:
                return text
    except FileNotFoundError:
        pass
    return _GENERIC_PERSONA


def _categories() -> list[str]:
    raw = os.environ.get("CATEGORIES", "").strip()
    if raw:
        cats = [c.strip() for c in raw.split(",") if c.strip()]
        if cats:
            return cats
    return list(_DEFAULT_CATEGORIES)


def _tool(categories: list[str]) -> dict[str, Any]:
    return {
        "name": "record_analysis",
        "description": "Record the structured analysis of this bookmark.",
        "input_schema": {
            "type": "object",
            "properties": {
                "relevance": {
                    "type": "string",
                    "enum": ["work", "personal", "both", "none"],
                    "description": "Who/what this applies to. 'none' = interesting but no clear application.",
                },
                "actionable": {
                    "type": "boolean",
                    "description": "True only if there is a concrete thing worth doing. False for pure FYI/inspiration.",
                },
                "category": {"type": "string", "enum": categories},
                "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                "title": {
                    "type": "string",
                    "description": "A short, imperative todo title (<= 90 chars). For non-actionable items, phrase as 'Review:' / 'FYI:'.",
                },
                "recommendation": {
                    "type": "string",
                    "description": "2-5 sentences: what the post is about and specifically if/how to act on it (or why not). Concrete and tactical.",
                },
            },
            "required": ["relevance", "actionable", "category", "priority", "title", "recommendation"],
        },
    }


def _system(categories: list[str]) -> str:
    return (
        f"{_persona()}\n\n"
        f"CATEGORIES (pick the single best fit): {' | '.join(categories)}\n"
    )


def analyze(bookmark: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    categories = _categories()

    parts = [f"Author: {bookmark['author']}", f"Posted: {bookmark.get('created_at', '')}", "", bookmark["text"]]
    if bookmark.get("quoted_text"):
        parts += ["", f"[Quoted/parent post]: {bookmark['quoted_text']}"]
    if bookmark.get("links"):
        parts += ["", "Links: " + ", ".join(bookmark["links"])]
    content = "\n".join(parts)

    lens = ""
    folder = (folder or "").strip()
    if folder:
        lens = (
            f"\n\nIMPORTANT — folder intent: you deliberately filed this into your "
            f"\"{folder}\" bookmark folder, which you reserve for {folder}-relevant "
            f"material. Treat it as intentional and frame the recommendation around "
            f"how it applies to that area, even if the underlying topic is generic. "
            f"Only deviate if the post is unambiguously about something else."
        )

    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": _system(categories), "cache_control": {"type": "ephemeral"}}],
        tools=[_tool(categories)],
        tool_choice={"type": "tool", "name": "record_analysis"},
        messages=[{"role": "user", "content": f"Analyze this bookmark:{lens}\n\n{content}"}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_analysis":
            return block.input
    # Defensive fallback — should not happen with forced tool_choice.
    return {
        "relevance": "none",
        "actionable": False,
        "category": categories[0],
        "priority": "Low",
        "title": f"Review bookmark from {bookmark['author']}",
        "recommendation": "Analysis unavailable; review manually.",
    }
