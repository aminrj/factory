#!/usr/bin/env python3
"""Nightly code-health snapshot of every `factory-app` repo, and ratchet tightening.

For each app repo: shallow read-only clone of `main` into a temp dir, run its `scripts/health.py` (the
template's copy if the app has not adopted the guards yet), append one line to data/health.jsonl.
If a metric improved against `quality/health.json`, open a PR in the app repo that tightens the baseline,
authored as factory-orchestrator (through the contents API, so no clone is ever pushed from).
The baseline only moves down, and only through this PR, which a human merges.

Usage: nightly_health.py [--repo NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea, GiteaError  # noqa: E402
from quality_lib import ORCHESTRATOR, TEMPLATE_REPO, app_repos, append_jsonl, now_iso, read_only_clone  # noqa: E402

TOOLS = ["--with", "radon==6.0.1", "--with", "coverage==7.6.10"]
BRANCH_PREFIX = "chore/tighten-quality-baseline"


def sh(cmd: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fetch_file(g: Gitea, repo: str, path: str, ref: str = "main") -> str | None:
    try:
        data = g.get(f"/repos/{g.user}/{repo}/contents/{path}?ref={ref}")
    except GiteaError:
        return None
    return base64.b64decode(data["content"]).decode()


def measure(g: Gitea, clone: Path) -> tuple[dict | None, str, str]:
    """Returns (health json, source of the script, error)."""
    own = clone / "scripts" / "health.py"
    if own.exists():
        script, source = own, "app"
    else:
        text = fetch_file(g, TEMPLATE_REPO, "scripts/health.py", os.environ.get("QUALITY_TEMPLATE_REF", "main"))
        if text is None:
            return None, "none", "no scripts/health.py in the app or the template"
        script = clone.parent / "template_health.py"
        script.write_text(text)
        source = "template"
    sync = sh(["uv", "sync", "--frozen"], clone)
    if sync.returncode != 0:
        sync = sh(["uv", "sync"], clone)
    if sync.returncode != 0:
        return None, source, f"uv sync failed: {sync.stderr[-300:]}"
    out = clone.parent / "health.json"
    res = sh(["uv", "run", *TOOLS, "python", str(script), "--root", str(clone), "--out", str(out)], clone)
    if res.returncode != 0:
        return None, source, f"health.py failed: {(res.stderr or res.stdout)[-300:]}"
    return json.loads(out.read_text()), source, ""


def improvements(current: dict, baseline: dict, policy_text: str, clone: Path) -> tuple[dict, list[str]]:
    """Use the app's own ratchet.py `tighten` on a scratch copy of the baseline."""
    work = clone.parent / "tighten"
    work.mkdir(exist_ok=True)
    (work / "current.json").write_text(json.dumps(current))
    (work / "baseline.json").write_text(json.dumps(baseline))
    (work / "policy.toml").write_text(policy_text)
    ratchet = clone / "scripts" / "ratchet.py"
    res = sh([sys.executable, str(ratchet), "tighten", "--current", str(work / "current.json"),
              "--baseline", str(work / "baseline.json"), "--policy", str(work / "policy.toml")], clone)
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-300:])
    changed = [line for line in res.stdout.splitlines() if "->" in line]
    return json.loads((work / "baseline.json").read_text()), changed


def open_tighten_pr(g: Gitea, repo: str, new_baseline: dict, changed: list[str]) -> str:
    full = f"{g.user}/{repo}"
    open_prs = g.get(f"/repos/{full}/pulls?state=open")
    if any(p["head"]["ref"].startswith(BRANCH_PREFIX) for p in open_prs):
        return "pr_already_open"
    current = g.get(f"/repos/{full}/contents/quality/health.json?ref=main")
    branch = f"{BRANCH_PREFIX}-{time.strftime('%Y%m%d')}"
    g.call("POST", f"/repos/{full}/branches", {"new_branch_name": branch, "old_branch_name": "main"})
    content = json.dumps(new_baseline, indent=2, sort_keys=True) + "\n"
    g.call("PUT", f"/repos/{full}/contents/quality/health.json", {
        "content": base64.b64encode(content.encode()).decode(), "sha": current["sha"], "branch": branch,
        "message": "chore: tighten quality baseline\n\n" + "\n".join(changed),
        "author": ORCHESTRATOR, "committer": ORCHESTRATOR})
    body = ("The nightly health job measured `main` and found these metrics better than the baseline:\n\n"
            + "\n".join(f"- {c}" for c in changed)
            + "\n\nMerging this makes the improvement the new floor. Nothing else changed. "
              "Agents cannot edit `quality/`; this PR was opened by the nightly job.")
    pr = g.call("POST", f"/repos/{full}/pulls", {"head": branch, "base": "main",
                                                  "title": "chore: tighten quality baseline", "body": body})
    return f"pr_opened:{pr['number']}"


def process(g: Gitea, repo: str, dry: bool) -> dict:
    t0 = time.time()
    record = {"ts": now_iso(), "repo": repo, "status": "ok", "source": "", "metrics": {}, "worst_functions": [],
              "improved": [], "tighten": "none"}
    try:
        with read_only_clone(g, repo) as clone:
            health, record["source"], err = measure(g, clone)
            if health is None:
                record.update(status="error", error=err)
                return record
            record["metrics"], record["worst_functions"] = health["metrics"], health["worst_functions"]
            baseline_text = (clone / "quality" / "health.json")
            policy_text = (clone / "quality" / "policy.toml")
            if not (baseline_text.exists() and policy_text.exists() and (clone / "scripts" / "ratchet.py").exists()):
                record["tighten"] = "not_adopted"
                return record
            new_baseline, changed = improvements(health, json.loads(baseline_text.read_text()),
                                                 policy_text.read_text(), clone)
            record["improved"] = changed
            if changed and not dry:
                record["tighten"] = open_tighten_pr(g, repo, new_baseline, changed)
            elif changed:
                record["tighten"] = "dry_run"
    except Exception as exc:  # one broken app must not stop the others
        record.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
        traceback.print_exc()
    finally:
        record["duration_s"] = round(time.time() - t0, 1)
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--dry-run", action="store_true", help="measure and print; write no data, open no PR")
    a = ap.parse_args()
    g = Gitea()
    for repo in app_repos(g, a.repo):
        rec = process(g, repo, a.dry_run)
        print(json.dumps({k: rec[k] for k in ("repo", "status", "source", "improved", "tighten")}))
        if not a.dry_run:
            append_jsonl("health.jsonl", rec)


if __name__ == "__main__":
    main()
