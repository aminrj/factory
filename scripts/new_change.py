#!/usr/bin/env python3
"""Open a change (a new work issue) on an EXISTING app and post Checkpoint 1 for the owner.

Usage: new_change.py <app> <title> --seed <dir> [--body <file>]

The seed folder holds the Gate 1 draft: thoughts/1/spec.md (with `Status: draft`), tests/holdout/ and
.gitea/workflows/gates.yml. The draft is committed on agent/issue-<n> as factory-orchestrator, never as the owner.
Gate 1 is the owner's: they review the spec and the holdout tests, set `Status: approved` in a commit of their own and
reply `/go minutes=N`. new_app.py does the same for a brand new app.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import new_app  # noqa: E402
import orchestrate  # noqa: E402
from gitea import Gitea  # noqa: E402


def ensure_wip_pr(g: Gitea, repo: str, branch: str, n: int, title: str) -> dict:
    """The change gets its PR at Gate 1, so CI runs on the holdout tests and the owner has one place to look.
    dispatch.py reuses it and the orchestrator removes `WIP:` at the review checkpoint."""
    pr = next((p for p in g.get(f"/repos/{repo}/pulls?state=open") if p["head"]["ref"] == branch), None)
    return pr or g.call("POST", f"/repos/{repo}/pulls", {"head": branch, "base": "main", "title": f"WIP: {title}",
                                                          "body": f"Refs #{n}\n\nGate 1 draft. Nothing to merge yet."})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("title")
    ap.add_argument("--seed", type=Path, required=True)
    ap.add_argument("--body", type=Path)
    args = ap.parse_args()
    os.environ.update(orchestrate.ORCH)  # the draft is the factory's, not the owner's
    g = Gitea()
    repo = f"{g.user}/{args.app}"
    if not g.exists(f"/repos/{repo}"):
        raise SystemExit(f"{repo} does not exist; use new_app.py for a new app.")
    body = args.body.read_text() if args.body else ""
    n, branch = new_app.create_app(g, args.app, "", args.seed, args.title, body)
    pr = ensure_wip_pr(g, repo, branch, n, args.title)
    ctx = orchestrate.Ctx(g, repo, n, None, 0, {})
    tests = sorted((args.seed / "tests" / "holdout").glob("*.py"))
    links = "\n".join(f"- [{t.name}]({ctx.url(f'/src/branch/{branch}/tests/holdout/{t.name}')})" for t in tests)
    orchestrate.checkpoint(
        ctx, "1", "spec and holdout tests need your review",
        f"I drafted Gate 1 on `{branch}` as the factory, not as you. Pull request (CI runs the holdout tests here): {pr['html_url']}\n\n- [spec.md]({ctx.file_link('spec.md')})\n{links}\n\n"
        "Read the spec and the tests. Change what you disagree with. Then change `Status: draft` to `Status: approved` in a "
        "commit of your own (the pencil icon in Gitea works), and reply `/go minutes=N`. Approving makes the spec and the "
        "tests yours.", "after_gate1")
    print(f"issue {n}: {ctx.url('/issues/' + str(n))}")


if __name__ == "__main__":
    main()
