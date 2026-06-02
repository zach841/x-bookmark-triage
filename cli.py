#!/usr/bin/env python3
"""Local entrypoint — run a triage pass without Cloud Run or a scheduler.

  python cli.py run        # one triage pass (use from cron / launchd / Task Scheduler)
  python cli.py status     # print current state

Authorize once first by running the web app locally:

  SETUP_SECRET=$(openssl rand -hex 8) REDIRECT_URI=http://127.0.0.1:8080/oauth/callback python app.py
  # then open http://127.0.0.1:8080/oauth/start?setup=<that secret>

With STATE_BACKEND=file (the default) the refresh token lands in ./state.json,
so subsequent `python cli.py run` calls work unattended.
"""
from __future__ import annotations

import json
import sys

import state
from pipeline import run_once


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        print(json.dumps(run_once(), indent=2))
        return 0
    if cmd == "status":
        st = state.get_state()
        print(
            json.dumps(
                {
                    "authorized": bool(st.get("refresh_token")),
                    "username": st.get("username"),
                    "baseline_done": bool(st.get("baseline_done")),
                    "last_seen_id": st.get("last_seen_id"),
                    "seen_count": len(st.get("seen_ids", [])),
                    "folder_processed_count": len(st.get("folder_processed_ids", [])),
                },
                indent=2,
            )
        )
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
