#!/usr/bin/env python3
"""Open a pull request on Gitea and tell the owner it is waiting for them. The factory never merges.

Usage: open_pr.py <repo> <branch> <title> --why "<one or two sentences: why this change>" [--body <file>] [--base main]

The push notification carries the title, the reason and the link. The pull request body starts with the reason, so
the notification that follows the merge can quote it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestrate  # noqa: E402
from gitea import Gitea  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("branch")
    ap.add_argument("title")
    ap.add_argument("--why", required=True)
    ap.add_argument("--body", type=Path)
    ap.add_argument("--base", default="main")
    a = ap.parse_args()
    g = Gitea()
    repo = a.repo if "/" in a.repo else f"{g.user}/{a.repo}"
    body = a.why.strip() + ("\n\n" + a.body.read_text() if a.body else "")
    pr = g.call("POST", f"/repos/{repo}/pulls", {"head": a.branch, "base": a.base, "title": a.title, "body": body})
    orchestrate.notify(f"Ready to merge: {repo.split('/')[1]}#{pr['number']}", f"{a.title}\n{a.why.strip()[:200]}",
                       click=pr["html_url"], priority=3)
    print(pr["html_url"])


if __name__ == "__main__":
    main()
