#!/usr/bin/env python3
"""Weekly vulnerability watch: find fixable HIGH/CRITICAL findings in an app's image before a release does.

brevkoll#36 (2026-11-23): the first time anyone learned that four Debian libraries had a security fix was the release
gate, with the owner waiting for a deployment. This job runs the same fresh build and the same scan on a schedule:

  1. build `main` of every `factory-app` repo from scratch (release.build_command) and scan it (release.scan)
  2. nothing fixable: record it, done
  3. fixable: try the two remedies code can apply without an agent, verify each by building and scanning again, and
     open a PR only when the scan got better:
       - `apt-get upgrade` is missing from the runtime stage: add it
       - the base image digest is behind its tag: pin the newest digest
  4. no remedy helped: open one issue labelled `bug` and `security` naming the packages (deduplicated), which the
     autopilot may take as a small fix

Merging is always the owner's. Every run appends to data/image_watch.jsonl.

Usage: image_watch.py [--repo NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from gitea import Gitea  # noqa: E402
from quality_lib import ORCHESTRATOR, app_repos, append_jsonl, now_iso, read_only_clone  # noqa: E402

BRANCH_PREFIX = "chore/image-security"
MARKER = "<!--factory:image-watch="
FROM_RE = re.compile(r"(?m)^(FROM\s+)(\S+?)@sha256:([0-9a-f]{64})(.*)$")


# ---------------------------------------------------------------- remedies on the Dockerfile text (pure)

def stages(text: str) -> list[str]:
    """The Dockerfile cut at every FROM line; the last piece is the runtime stage."""
    parts = re.split(r"(?m)^(?=FROM\s)", text)
    return [p for p in parts if p.strip()]


def add_apt_upgrade(text: str) -> str | None:
    """Put `apt-get upgrade` after `apt-get update` in the runtime stage. None when there is no apt there, or when it
    already upgrades (then the pinned base is what lags, which is the other remedy)."""
    parts = stages(text)
    final = parts[-1]
    if "apt-get install" not in final or "apt-get upgrade" in final:
        return None
    upgrade = "apt-get upgrade -y --no-install-recommends"
    multi = re.sub(r"(apt-get update[ \t]*\\\n)([ \t]*)&& (apt-get install)", rf"\1\2&& {upgrade} \\\n\2&& \3", final, count=1)
    if multi != final:
        patched = multi
    else:
        patched = re.sub(r"apt-get update(\s*&&\s*)apt-get install", rf"apt-get update\1{upgrade}\1apt-get install", final,
                         count=1)
    return "".join([*parts[:-1], patched]) if patched != final else None


def bump_base(text: str, resolve: Callable[[str], str | None]) -> tuple[str, list[str]] | None:
    """Pin every `FROM image:tag@sha256:...` to the newest digest of its tag (`resolve("image:tag")` -> digest, or None
    when it cannot be found). Returns the new text and one line per change, or None when nothing moved."""
    changes: list[str] = []
    done: dict[str, str] = {}

    def swap(m: re.Match[str]) -> str:
        ref, old = m.group(2), m.group(3)
        new = done.setdefault(f"{ref}@{old}", resolve(ref) or old)
        if new != old:
            if f"{ref}: {old[:12]} -> {new[:12]}" not in changes:
                changes.append(f"{ref}: {old[:12]} -> {new[:12]}")
        return f"{m.group(1)}{ref}@sha256:{new}{m.group(4)}"

    patched = FROM_RE.sub(swap, text)
    return (patched, changes) if changes else None


def resolve_digest(ref: str) -> str | None:
    """The digest the registry serves for `ref` today (docker pull, then read the repo digest)."""
    pulled = subprocess.run(["docker", "pull", "-q", ref], capture_output=True, text=True, check=False)
    if pulled.returncode != 0:
        return None
    out = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref],
                         capture_output=True, text=True, check=False).stdout
    repo = ref.split(":", 1)[0].split("/")[-1]
    for d in json.loads(out or "[]"):
        if d.split("@", 1)[0].split("/")[-1] == repo and "@sha256:" in d:
            return d.split("@sha256:", 1)[1]
    return None


# ---------------------------------------------------------------- build and scan

def build_and_scan(root: Path, tag: str) -> dict:
    """A fresh image of the tree at `root` and its scan: {'fixed', 'unfixed', 'critical', 'packages'}."""
    archive = subprocess.run(["tar", "-c", "--exclude=.git", "."], cwd=root, capture_output=True, check=True).stdout
    release.sh(*release.build_command([tag]), stdin=archive)
    try:
        return release.scan(tag, root / ".image-watch.cdx.json")
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, check=False)


def signature(packages: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(packages)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- Gitea (PR and issue)

def pr_exists(g: Gitea, full: str) -> bool:
    return any(p["head"]["ref"].startswith(BRANCH_PREFIX) for p in g.get(f"/repos/{full}/pulls?state=open"))


def issue_exists(g: Gitea, full: str, sig: str) -> bool:
    return any(MARKER + sig in (i.get("body") or "") for i in g.paged(f"/repos/{full}/issues?state=open&type=issues"))


def open_fix_pr(g: Gitea, repo: str, text: str, lines: list[str], before: dict, after: dict) -> str:
    full = f"{g.user}/{repo}"
    current = g.get(f"/repos/{full}/contents/Dockerfile?ref=main")
    branch = f"{BRANCH_PREFIX}-{time.strftime('%Y%m%d')}"
    g.call("POST", f"/repos/{full}/branches", {"new_branch_name": branch, "old_branch_name": "main"})
    g.call("PUT", f"/repos/{full}/contents/Dockerfile", {
        "content": base64.b64encode(text.encode()).decode(), "sha": current["sha"], "branch": branch,
        "message": "chore: take the security fixes the image is missing\n\n" + "\n".join(lines),
        "author": ORCHESTRATOR, "committer": ORCHESTRATOR})
    body = (f"The weekly image watch built `main` from scratch and found **{before['fixed']}** HIGH/CRITICAL findings with a "
            f"fix available: " + "; ".join(before["packages"]) + ".\n\nThis change:\n" + "\n".join(f"- {x}" for x in lines)
            + f"\n\nI built the changed Dockerfile from scratch and scanned it again: **{after['fixed']}** fixable findings "
              "left. Nothing else changed. Merging is yours; the next release then builds on it.")
    pr = g.call("POST", f"/repos/{full}/pulls", {"head": branch, "base": "main",
                                                  "title": "chore: take the security fixes the image is missing", "body": body})
    return f"pr_opened:{pr['number']}"


def open_issue(g: Gitea, repo: str, before: dict) -> str:
    full = f"{g.user}/{repo}"
    sig = signature(before["packages"])
    if issue_exists(g, full, sig):
        return "issue_already_open"
    g.ensure_label(full, "bug", "EE0701", "A bug fix: the autopilot may run it if the plan is small")
    g.ensure_label(full, "security", "B60205", "A security finding")
    labels = [g.label_id(full, name) for name in ("bug", "security")]
    body = (f"The weekly image watch built `main` from scratch: **{before['fixed']}** HIGH/CRITICAL findings have a fix "
            "available.\n\n" + "\n".join(f"- `{p}`" for p in before["packages"]) + "\n\nNeither `apt-get upgrade` nor a newer "
            "base image digest cleared them, so this needs a look at the Dockerfile or the dependency.\n\n"
            f"{MARKER}{sig}-->")
    issue = g.call("POST", f"/repos/{full}/issues", {"title": f"Security: {before['fixed']} fixable findings in the image",
                                                       "body": body, "labels": [x for x in labels if x]})
    return f"issue_opened:{issue['number']}"


def notify(title: str, body: str) -> None:
    try:
        import orchestrate  # noqa: PLC0415 - heavy, and only needed when something was opened
        orchestrate.notify(title, body, priority=3)
    except Exception:  # noqa: BLE001 - a missing notification must not hide the PR
        traceback.print_exc()


# ---------------------------------------------------------------- one app

def watch(g: Gitea, repo: str, dry: bool, scan: Callable[[Path, str], dict] = build_and_scan,
          resolve: Callable[[str], str | None] = resolve_digest) -> dict:
    record: dict = {"ts": now_iso(), "repo": repo, "status": "ok", "fixed": 0, "unfixed": 0, "packages": [], "action": "none"}
    with read_only_clone(g, repo) as root:
        dockerfile = root / "Dockerfile"
        if not dockerfile.exists():
            return {**record, "status": "no_dockerfile"}
        before = scan(root, f"image-watch/{repo}:before")
        record.update(fixed=before["fixed"], unfixed=before["unfixed"], packages=before["packages"])
        if not before["fixed"]:
            return record
        original = dockerfile.read_text()
        attempts = [("apt-get upgrade added to the runtime stage", add_apt_upgrade(original))]
        bumped = bump_base(original, resolve)
        attempts.append(("; ".join(bumped[1]) if bumped else "", bumped[0] if bumped else None))
        for label, text in attempts:
            if text is None:
                continue
            dockerfile.write_text(text)
            after = scan(root, f"image-watch/{repo}:after")
            if after["fixed"] < before["fixed"]:
                record["action"] = "dry_run" if dry else (
                    "pr_already_open" if pr_exists(g, f"{g.user}/{repo}") else open_fix_pr(g, repo, text, [label], before, after))
                record["after_fixed"] = after["fixed"]
                break
            dockerfile.write_text(original)
        else:
            record["action"] = "dry_run" if dry else open_issue(g, repo, before)
    if record["action"].startswith(("pr_opened", "issue_opened")):
        notify(f"{repo}: image security", f"{repo}: {before['fixed']} fixable findings. {record['action']}")
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    g = Gitea()
    failures = 0
    for repo in app_repos(g, a.repo):
        try:
            rec = watch(g, repo, a.dry_run)
        except Exception as e:  # noqa: BLE001 - one app's failure must not stop the others
            traceback.print_exc()
            rec = {"ts": now_iso(), "repo": repo, "status": f"error:{type(e).__name__}", "fixed": -1}
            failures += 1
        append_jsonl("image_watch.jsonl", rec)
        print(json.dumps({k: v for k, v in rec.items() if k != "packages"}))
    sys.exit(1 if failures and failures == len(list(app_repos(g, a.repo))) else 0)


if __name__ == "__main__":
    main()
