#!/usr/bin/env python3
"""Factory regression bench. See bench/README.md. Standard library only (plus git, uv, claude on PATH)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from gitea import Gitea, GiteaError  # noqa: E402
from quality_lib import append_jsonl, now_iso, read_jsonl  # noqa: E402

TASK = "add a /version endpoint with a test"
HUMAN = ("bench-human", "bench-human@users.noreply.local")
CI_WAIT_S = 900


def sh(*cmd: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> str:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(out.stderr or out.stdout)[-400:]}")
    return out.stdout.strip()


def human_env() -> dict[str, str]:
    return dict(os.environ, GIT_AUTHOR_NAME=HUMAN[0], GIT_AUTHOR_EMAIL=HUMAN[1],
                GIT_COMMITTER_NAME=HUMAN[0], GIT_COMMITTER_EMAIL=HUMAN[1])


def wait_ci(g: Gitea, repo: str, branch: str) -> tuple[str, list[str]]:
    deadline = time.time() + CI_WAIT_S
    state, failed = "pending", []
    while time.time() < deadline:
        sha = g.get(f"/repos/{repo}/branches/{branch}")["commit"]["id"]
        st = g.get(f"/repos/{repo}/commits/{sha}/status")
        state = st["state"]
        failed = [s["context"] for s in st["statuses"] if s["status"] in ("failure", "error")]
        if st["statuses"] and state in ("success", "failure", "error"):
            return state, failed
        time.sleep(20)
    return "timeout", failed


def acceptance(wt: Path) -> bool:
    """Independent check of the result: the endpoint exists and returns exactly the package version."""
    code = ("from fastapi.testclient import TestClient\nfrom app.main import create_app\nimport tomllib\n"
            "c=TestClient(create_app())\nr=c.get('/version')\n"
            "assert r.status_code==200 and r.json()=={'version': tomllib.load(open('pyproject.toml','rb'))['project']['version']}, r.text\n"
            "assert c.post('/version').status_code==405\nprint('ACCEPTED')")
    res = subprocess.run(["uv", "run", "python", "-c", code], cwd=wt, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH="src"))
    return res.returncode == 0 and "ACCEPTED" in res.stdout


def run_dispatch(tmp: Path, repo: str, work: Path, extra: list[str]) -> dict:
    data = tmp / "factory" / "data"
    before = len(read_runs(data))
    env = dict(os.environ, WORK=str(work), FACTORY_MAX_BUDGET_USD=os.environ.get("FACTORY_MAX_BUDGET_USD", "5"))
    proc = subprocess.run([sys.executable, str(tmp / "factory" / "scripts" / "dispatch.py"), repo, "1", "implement", *extra],
                          capture_output=True, text=True, env=env)
    runs = read_runs(data)
    rec = runs[-1] if len(runs) > before else {}
    return {"rc": proc.returncode, "tail": (proc.stdout + proc.stderr)[-600:], **rec}


def read_runs(data: Path) -> list[dict]:
    path = data / "agent_runs.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template-ref", default="main")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="create nothing; print the plan")
    a = ap.parse_args()
    if a.dry_run:
        print(f"would create a scratch repo from factory-template@{a.template_ref}, run implement for: {TASK}")
        return 0
    g = Gitea()
    name = f"bench-scratch-{time.strftime('%Y%m%d-%H%M%S')}"
    repo = f"{g.user}/{name}"
    tmp = Path(tempfile.mkdtemp(prefix="factory-bench-"))
    result: dict = {"ts": now_iso(), "task": TASK, "template_ref": a.template_ref, "status": "error"}
    try:
        (tmp / "factory" / "scripts").mkdir(parents=True)
        for f in ("dispatch.py", "gitea.py"):
            shutil.copy(SCRIPTS / f, tmp / "factory" / "scripts" / f)
        g.call("POST", "/user/repos", {"name": name, "private": True, "description": "factory bench scratch, safe to delete"})
        work = tmp / "work"
        work.mkdir()
        clone = work / name
        sh("git", "clone", "-q", "--branch", a.template_ref, g.repo_url(f"{g.user}/factory-template"), str(clone), cwd=work)
        result["template_sha"] = sh("git", "rev-parse", "--short", "HEAD", cwd=clone)
        sh("git", "config", "user.name", HUMAN[0], cwd=clone)
        sh("git", "config", "user.email", HUMAN[1], cwd=clone)
        sh("git", "remote", "set-url", "origin", g.repo_url(repo), cwd=clone)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/main", cwd=clone)
        g.call("POST", f"/repos/{repo}/issues", {"title": "Bench: " + TASK, "body": "Scratch bench run."})
        sh("git", "checkout", "-q", "-b", "agent/issue-1", cwd=clone)
        t = clone / "thoughts" / "1"
        t.mkdir(parents=True, exist_ok=True)
        for f in ("spec.md", "threat-model.md", "research.md", "plan.md"):
            shutil.copy(HERE / "fixtures" / f, t / f)
        sh("git", "add", "-A", cwd=clone)
        sh("git", "commit", "-q", "-m", "#1 bench inputs, plan approved", cwd=clone, env=human_env())
        sh("git", "push", "-q", "-u", "origin", "agent/issue-1", cwd=clone)
        sh("git", "checkout", "-q", "main", cwd=clone)  # the worktree for agent/issue-1 is created by dispatch.py

        run1 = run_dispatch(tmp, name, work, [])
        result.update(minutes=round((run1.get("duration_s") or 0) / 60, 2), cost_usd=run1.get("cost_usd"),
                      turns=run1.get("turns"), dispatch_ok=run1["rc"] == 0, rework_runs=0)
        if run1["rc"] != 0:
            result.update(error=run1["tail"], first_pass=False)
        else:
            state, failed = wait_ci(g, repo, "agent/issue-1")
            result.update(ci_state=state, ci_failed=failed, first_pass=state == "success")
            if state != "success":
                note = f"CI state {state}; failing checks: {', '.join(failed) or 'unknown'}"
                run2 = run_dispatch(tmp, name, work, ["--note", note])
                result["rework_runs"] = 1
                result["rework_dispatch_ok"] = run2["rc"] == 0
                if run2["rc"] != 0:
                    result["rework_error"] = run2["tail"][-300:]
                state2, failed2 = wait_ci(g, repo, "agent/issue-1")
                result.update(final_ci_state=state2, final_ci_failed=failed2)
                result["minutes"] = round(result["minutes"] + (run2.get("duration_s") or 0) / 60, 2)
                result["cost_usd"] = round((result["cost_usd"] or 0) + (run2.get("cost_usd") or 0), 4)
                result["turns"] = (result["turns"] or 0) + (run2.get("turns") or 0)
            wt = work / "worktrees" / f"{name}-issue-1"
            result["acceptance_ok"] = acceptance(wt)
            result["status"] = "ok"
    except (RuntimeError, GiteaError, subprocess.SubprocessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:600]
    finally:
        if not a.keep:
            try:
                g.call("DELETE", f"/repos/{repo}")
                result["scratch_repo"] = "deleted"
            except GiteaError as exc:
                result["scratch_repo"] = f"NOT DELETED, remove {repo} by hand ({str(exc)[:80]})"
        else:
            result["scratch_repo"] = repo
        subprocess.run(["git", "worktree", "prune"], cwd=tmp, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    prev = [r for r in read_jsonl("bench.jsonl") if r.get("status") == "ok"]
    if prev and result["status"] == "ok":
        last = prev[-1]
        result["vs_previous"] = {k: (round(result[k] - last[k], 4) if isinstance(result.get(k), (int, float))
                                      and isinstance(last.get(k), (int, float)) else None)
                                 for k in ("minutes", "cost_usd", "turns")}
        result["vs_previous"]["first_pass_was"] = last.get("first_pass")
    append_jsonl("bench.jsonl", result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
