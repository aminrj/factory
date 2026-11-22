#!/usr/bin/env python3
"""Post a checkpoint comment on an issue, then wait for the human's /go or /stop.

Usage: wait_go.py <repo> <issue> --post BODYFILE [--interval 600] [--max-hours 12]
       wait_go.py <repo> <issue> --after <comment-id> [...]     (resume waiting)
Only comments by the Gitea user that owns the token, created after the checkpoint comment, count.
Prints one JSON line: {"cmd": "go"|"stop"|"timeout", "minutes": N|null, "comment_id": ID, "checkpoint_id": ID}.
Other comments are ignored, never treated as instructions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea, by_owner  # noqa: E402

GO = re.compile(r"^\s*/go(?:\s+minutes=(\d+))?\s*$", re.IGNORECASE)
STOP = re.compile(r"^\s*/stop\s*$", re.IGNORECASE)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("issue", type=int)
    ap.add_argument("--post", type=Path)
    ap.add_argument("--after", type=int)
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--max-hours", type=float, default=12)
    a = ap.parse_args()
    g = Gitea()
    repo = f"{g.user}/{a.repo}"
    if a.post:
        posted = g.call("POST", f"/repos/{repo}/issues/{a.issue}/comments", {"body": a.post.read_text()})
        after = posted["id"]
        print(f"posted checkpoint comment {after}", file=sys.stderr, flush=True)
    elif a.after is not None:
        after = a.after
    else:
        raise SystemExit("need --post or --after")
    deadline = time.time() + a.max_hours * 3600
    while True:
        for c in g.paged(f"/repos/{repo}/issues/{a.issue}/comments"):
            if c["id"] <= after or not by_owner(g, c):
                continue
            body = c["body"].strip()
            if m := GO.match(body):
                print(json.dumps({"cmd": "go", "minutes": int(m.group(1)) if m.group(1) else None,
                                  "comment_id": c["id"], "checkpoint_id": after}))
                return
            if STOP.match(body):
                print(json.dumps({"cmd": "stop", "minutes": None, "comment_id": c["id"], "checkpoint_id": after}))
                return
        if time.time() > deadline:
            print(json.dumps({"cmd": "timeout", "minutes": None, "comment_id": None, "checkpoint_id": after}))
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
