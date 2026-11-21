#!/usr/bin/env python3
"""Weekly digest of merged pull requests labelled `rework`, with their "Friction" sections.

Appends one line per PR (once) to data/rework.jsonl. The foreman and I read it when writing the weekly retro:
for every rework PR, which rule or check would have prevented it, and where that now lives.

Usage: rework_digest.py [--repo NAME] [--since-days 14] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea  # noqa: E402
from quality_lib import app_repos, append_jsonl, now_iso, parse_ts, read_jsonl, sanitize  # noqa: E402

FRICTION = re.compile(r"^#{1,4}\s*Friction[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)", re.S | re.M | re.I)


def friction_section(body: str) -> str:
    m = FRICTION.search(body or "")
    return sanitize(m.group(1).strip(), 2000) if m else ""


def iso_week(ts: datetime) -> str:
    y, w, _ = ts.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--since-days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    g = Gitea()
    seen = {(r["repo"], r["pr"]) for r in read_jsonl("rework.jsonl")}
    cutoff = datetime.now(UTC) - timedelta(days=a.since_days)
    new = 0
    for repo in app_repos(g, a.repo):
        full = f"{g.user}/{repo}"
        for pr in g.paged(f"/repos/{full}/issues?state=closed&type=pulls&labels=rework"):
            detail = g.get(f"/repos/{full}/pulls/{pr['number']}")
            if not detail.get("merged") or (repo, pr["number"]) in seen:
                continue
            merged = parse_ts(detail["merged_at"])
            if merged < cutoff:
                continue
            rec = {"ts": now_iso(), "week": iso_week(merged), "repo": repo, "pr": pr["number"],
                   "title": sanitize(pr["title"], 200), "merged_at": detail["merged_at"],
                   "issue": next(iter(re.findall(r"#(\d+)", detail.get("body") or "")), None),
                   "friction": friction_section(detail.get("body") or ""), "prevented_by": None}
            print(json.dumps(rec))
            if not a.dry_run:
                append_jsonl("rework.jsonl", rec)
            new += 1
    print(f"{new} new rework PR(s)")


if __name__ == "__main__":
    main()
