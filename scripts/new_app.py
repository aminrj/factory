#!/usr/bin/env python3
"""Create a factory app on Gitea from the template, with labels, topic, work issue and Gate 1 seed.

Usage: new_app.py <app> "<one-line description>" [--seed DIR] [--idea-title T --idea-body F]
Idempotent: existing repos, labels, issues and branches are left alone.
The Gate 1 seed commit is made under the human's git identity (it is the only such commit).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import STAGES, Gitea  # noqa: E402

WORK = Path.home() / "git" / "software-factory"

APP_LABELS = [
    ("agent:threat-model", "1D76DB", "Dispatch: draft thoughts/<n>/threat-model.md"),
    ("agent:research", "1D76DB", "Dispatch: write thoughts/<n>/research.md"),
    ("agent:plan", "1D76DB", "Dispatch: write thoughts/<n>/plan.md"),
    ("agent:implement", "0052CC", "Dispatch: implement the next phase of an approved plan"),
    ("agent:validate", "1D76DB", "Dispatch: write thoughts/<n>/validation.md"),
    ("rework", "D93F0B", "PR sent back to the agent after review"),
    ("escaped-bug", "B60205", "Bug found after merge"),
    ("human-approved", "5319E7", "Human-authored change to guarded paths"),
    ("friction", "FBCA04", "Factory friction worth fixing"),
    ("feedback", "C2E0C6", "Feedback from a real user"),
]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def clone(g: Gitea, repo: str, dest: Path) -> None:
    if not dest.exists():
        subprocess.run(["git", "clone", "-q", g.repo_url(repo), str(dest)], check=True)


def create_app(g: Gitea, app: str, desc: str, seed: Path | None, work_title: str, work_body: str) -> tuple[int, str]:
    user = g.user
    repo = f"{user}/{app}"
    if not g.exists(f"/repos/{repo}"):
        g.call("POST", f"/repos/{user}/factory-template/generate", {
            "owner": user, "name": app, "private": True, "description": desc,
            "git_content": True, "topics": True, "labels": False, "git_hooks": False, "webhooks": False,
        })
        for _ in range(30):  # generation is asynchronous
            time.sleep(2)
            if g.exists(f"/repos/{repo}/branches/main"):
                break
    g.call("PUT", f"/repos/{repo}/topics/factory-app", ok=(204,))
    g.call("PUT", f"/repos/{repo}/topics/secure-ai", ok=(204,))
    g.call("PATCH", f"/repos/{repo}", {"has_actions": True, "has_issues": True, "has_pull_requests": True})
    for name, color, text in APP_LABELS:
        g.ensure_label(repo, name, color, text)
    for stage, (color, text) in STAGES.items():
        g.ensure_label(repo, f"stage/{stage}", color, text)

    issues = g.paged(f"/repos/{repo}/issues?state=all&type=issues")
    work = next((i for i in issues if i["title"] == work_title), None)
    if work is None:
        work = g.call("POST", f"/repos/{repo}/issues", {
            "title": work_title, "body": work_body,
            "labels": [g.label_id(repo, "stage/idea")],
        })
    n = work["number"]

    dest = WORK / app
    clone(g, repo, dest)
    git("fetch", "-q", "origin", cwd=dest)
    branch = f"agent/issue-{n}"
    if seed and not git("ls-remote", "--heads", "origin", branch, cwd=dest):
        git("switch", "-q", "-c", branch, "origin/main", cwd=dest)
        (dest / "thoughts" / str(n)).mkdir(parents=True, exist_ok=True)
        spec = (seed / "thoughts/1/spec.md").read_text()
        spec = re.sub(r"# Spec: #1 ", f"# Spec: #{n} ", spec)
        (dest / "thoughts" / str(n) / "spec.md").write_text(spec)
        shutil.copytree(seed / "tests/holdout", dest / "tests/holdout", dirs_exist_ok=True)
        (dest / ".gitea/workflows").mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed / ".gitea/workflows/gates.yml", dest / ".gitea/workflows/gates.yml")
        git("add", "-A", cwd=dest)
        git("commit", "-q", "-m", f"#{n} gate 1: draft spec, holdout acceptance tests, OCR in CI", cwd=dest)
        git("push", "-q", "-u", "origin", branch, cwd=dest)
        git("switch", "-q", "main", cwd=dest)  # worktrees need the branch free
        print(f"Pushed {branch} (committed as {git('config', 'user.name', cwd=dest)})")
    return n, branch


def ensure_wip_pr(g: Gitea, repo: str, branch: str, n: int, title: str) -> dict:
    """The work item gets its PR at Gate 1, so CI runs on the holdout tests and the owner has one place to look.
    dispatch.py reuses it and the foreman removes `WIP:` at the review checkpoint."""
    pr = next((p for p in g.get(f"/repos/{repo}/pulls?state=open") if p["head"]["ref"] == branch), None)
    return pr or g.call("POST", f"/repos/{repo}/pulls", {"head": branch, "base": "main", "title": f"WIP: {title}",
                                                          "body": f"Refs #{n}\n\nGate 1 draft. Nothing to merge yet."})


def open_work_item(g: Gitea, app: str, title: str, body: str, seed: Path | None, desc: str = "",
                   existing_only: bool = False, as_factory: bool = False) -> tuple[int, str]:
    """The one way to open work: issue, branch with the Gate 1 draft, WIP pull request, Checkpoint 1. Used for a brand new
    app (new_app.py) and for a change on an existing app (new_change.py), so a fix to the opening lands in both.
    as_factory: commit the draft as factory-orchestrator (a change the factory drafted for the owner) instead of the
    owner's own git identity (a seed the owner wrote)."""
    import orchestrate  # noqa: PLC0415 - orchestrate is heavy and only needed here

    repo = f"{g.user}/{app}"
    if existing_only and not g.exists(f"/repos/{repo}"):
        raise SystemExit(f"{repo} does not exist; use new_app.py for a new app.")
    if as_factory:
        os.environ.update(orchestrate.ORCH)
    n, branch = create_app(g, app, desc, seed, title, body)
    pr = ensure_wip_pr(g, repo, branch, n, title)
    ctx = orchestrate.Ctx(g, repo, n, None, 0, {})
    tests = sorted((seed / "tests" / "holdout").glob("*.py")) if seed else []
    links = "\n".join(f"- [{t.name}]({ctx.url(f'/src/branch/{branch}/tests/holdout/{t.name}')})" for t in tests)
    who = "as the factory, not as you" if as_factory else "from the seed"
    orchestrate.checkpoint(
        ctx, "1", "spec and holdout tests need your review",
        f"Gate 1 is on `{branch}`, drafted {who}. Pull request (CI runs the holdout tests here): {pr['html_url']}\n\n"
        f"- [spec.md]({ctx.file_link('spec.md')})\n{links}\n\nRead the spec and the tests and change what you disagree with. "
        "If the spec has a `Status:` line, change `Status: draft` to `Status: approved` in a commit of your own (the pencil icon "
        "in Gitea works). Then reply `/go minutes=N`. Approving makes the spec and the tests yours.", "after_gate1")
    return n, branch


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("description")
    ap.add_argument("--seed", type=Path)
    ap.add_argument("--work-title", default="v1")
    ap.add_argument("--work-body", default="")
    args = ap.parse_args()
    n, branch = open_work_item(Gitea(), args.app, args.work_title, args.work_body, args.seed, args.description)
    print(f"work issue #{n}, branch {branch}")


if __name__ == "__main__":
    main()
