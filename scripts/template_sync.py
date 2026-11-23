#!/usr/bin/env python3
"""Keep every app's copy of the template's own files current, by pull request.

An app is generated from `factory-template` once (Gitea's "generate"). Nothing carried the template's later fixes to apps
that already existed: brevkoll's validate.md never told the agent to write acceptance.md for days, and every staged build
showed the generic phone checklist (2026-11-22). The template now names the paths it owns in `.factory/template.json`; this
job compares them with each app's `main` and opens one PR per app with the files that differ. An app that differs on
purpose lists the path in its own `.factory/template-exceptions` (one path or folder prefix per line).

Only files that exist in the template are synced; files an app added are never touched. Merging is the owner's.

Usage: template_sync.py [--repo NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea, GiteaError  # noqa: E402
from quality_lib import ORCHESTRATOR, TEMPLATE_REPO, app_repos, append_jsonl, now_iso, read_only_clone  # noqa: E402

BRANCH_PREFIX = "chore/template-sync"
SKIP = ("__pycache__/", ".pyc", ".pytest_cache/", ".ruff_cache/")


def owned_paths(template: Path) -> list[str]:
    manifest = template / ".factory" / "template.json"
    return list(json.loads(manifest.read_text()).get("owned", [])) if manifest.exists() else []


def exceptions(app: Path) -> list[str]:
    f = app / ".factory" / "template-exceptions"
    return [x.strip() for x in f.read_text().splitlines() if x.strip() and not x.startswith("#")] if f.exists() else []


def excepted(path: str, skip: list[str]) -> bool:
    return any(path == s or path.startswith(s if s.endswith("/") else s + "/") for s in skip)


def differences(template: Path, app: Path, owned: list[str], skip: list[str]) -> list[str]:
    """Template files (relative paths) that an app lacks or holds an older copy of."""
    out: list[str] = []
    for entry in owned:
        base = template / entry
        files = sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else ([base] if base.is_file() else [])
        for f in files:
            rel = f.relative_to(template).as_posix()
            if any(s in rel for s in SKIP) or excepted(rel, skip):
                continue
            mine = app / rel
            if not mine.is_file() or mine.read_bytes() != f.read_bytes():
                out.append(rel)
    return out


def open_sync_pr(g: Gitea, repo: str, template: Path, paths: list[str]) -> str:
    full = f"{g.user}/{repo}"
    if any(p["head"]["ref"].startswith(BRANCH_PREFIX) for p in g.get(f"/repos/{full}/pulls?state=open")):
        return "pr_already_open"
    branch = f"{BRANCH_PREFIX}-{time.strftime('%Y%m%d')}"
    g.call("POST", f"/repos/{full}/branches", {"new_branch_name": branch, "old_branch_name": "main"})
    for rel in paths:
        body = {"content": base64.b64encode((template / rel).read_bytes()).decode(), "branch": branch,
                "message": f"chore: take {rel} from the template", "author": ORCHESTRATOR, "committer": ORCHESTRATOR}
        try:
            body["sha"] = g.get(f"/repos/{full}/contents/{rel}?ref=main")["sha"]
        except GiteaError:
            pass  # a new file
        g.call("PUT", f"/repos/{full}/contents/{rel}", body)
    listing = "\n".join(f"- `{p}`" for p in paths)
    pr = g.call("POST", f"/repos/{full}/pulls", {
        "head": branch, "base": "main", "title": "chore: take the template's changes to its own files",
        "body": f"These files are owned by `factory-template` (see its `.factory/template.json`) and differ here:\n\n{listing}\n\n"
                "Merge to take them. If this app should keep one of them, list its path in `.factory/template-exceptions` "
                "and close this PR. Opened by the weekly template sync; files an app added are never touched."})
    return f"pr_opened:{pr['number']}"


def sync(g: Gitea, repo: str, dry: bool) -> dict:
    record = {"ts": now_iso(), "repo": repo, "status": "ok", "differs": [], "action": "none"}
    with read_only_clone(g, TEMPLATE_REPO) as template, read_only_clone(g, repo) as app:
        owned = owned_paths(template)
        if not owned:
            return {**record, "status": "template_has_no_manifest"}
        record["differs"] = differences(template, app, owned, exceptions(app))
        if record["differs"]:
            record["action"] = "dry_run" if dry else open_sync_pr(g, repo, template, record["differs"])
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    g = Gitea()
    for repo in app_repos(g, a.repo):
        try:
            rec = sync(g, repo, a.dry_run)
        except Exception as e:  # noqa: BLE001 - one app's failure must not stop the others
            traceback.print_exc()
            rec = {"ts": now_iso(), "repo": repo, "status": f"error:{type(e).__name__}", "differs": [], "action": "none"}
        append_jsonl("template_sync.jsonl", rec)
        print(json.dumps(rec))


if __name__ == "__main__":
    main()
