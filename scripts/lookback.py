#!/usr/bin/env python3
"""Look back at shipped apps 14 and 30 days after `stage/shipped`.

For each `factory-app` repo, each issue with the `stage/shipped` label: find the date the label was added
(issue timeline events). When 14 or 30 days have passed and that window has no record in
data/lookbacks.jsonl, run a fresh read-only `claude -p` (Read, Grep, Glob only) over a read-only clone of `main`
with `.claude/commands/lookback.md`. The agent returns JSON; THIS SCRIPT validates it and files the issues
(label `lookback`). The agent has no write tools and no network.

Usage: lookback.py [--repo NAME] [--issue N] [--window 14|30] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea  # noqa: E402
from quality_lib import app_repos, append_jsonl, now_iso, parse_ts, read_jsonl, read_only_clone, sanitize  # noqa: E402

WINDOWS = (14, 30)
COMMAND = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "lookback.md"
MAX_BUDGET = "5"
TIMEOUT_S = 1800
SEVERITIES = {"high", "medium", "low"}
CATEGORIES = {"drift", "weakened-check", "dead-code", "missing-test", "contract"}
FINDING_CAP = 15


def shipped_date(g: Gitea, repo: str, issue: int) -> datetime | None:
    """Latest time the `stage/shipped` label was added, from the issue timeline."""
    events = g.paged(f"/repos/{g.user}/{repo}/issues/{issue}/timeline")
    added = [parse_ts(e["created_at"]) for e in events
             if e.get("type") == "label" and (e.get("label") or {}).get("name") == "stage/shipped"
             and e.get("body") == "1"]  # body "1" = label added, "" = removed
    return max(added) if added else None


def due_windows(ship: datetime, now: datetime, done: set[int]) -> list[int]:
    age = (now - ship).days
    return [w for w in WINDOWS if age >= w and w not in done]


def command_prompt(issue: int) -> str:
    text = COMMAND.read_text().split("---", 2)[2].strip()
    return text.replace("$ARGUMENTS", str(issue))


def extract_findings(result_text: str) -> list[dict]:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", result_text, re.S)
    if not blocks:
        raise ValueError("no JSON block in the agent output")
    raw = json.loads(blocks[-1]).get("findings", [])
    clean = []
    for f in raw[:FINDING_CAP]:
        if not isinstance(f, dict) or f.get("severity") not in SEVERITIES or f.get("category") not in CATEGORIES:
            continue
        clean.append({
            "severity": f["severity"], "category": f["category"],
            "file": sanitize(str(f.get("file", "")), 200), "line": f.get("line") if isinstance(f.get("line"), int) else None,
            "title": sanitize(str(f.get("title", "")), 100).strip(), "detail": sanitize(str(f.get("detail", "")), 1500),
            "reference": sanitize(str(f.get("reference", "")), 200)})
    return [f for f in clean if f["title"]]


def run_agent(clone: Path, issue: int) -> tuple[dict, float]:
    cmd = ["claude", "-p", "--output-format", "json", "--max-budget-usd", MAX_BUDGET, "--setting-sources", "project",
           "--strict-mcp-config", "--allowedTools", "Read,Grep,Glob",
           "--disallowedTools", "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task"]
    t0 = time.time()
    proc = subprocess.run(cmd, input=command_prompt(issue), cwd=clone, capture_output=True, text=True, timeout=TIMEOUT_S)
    try:
        return json.loads(proc.stdout), round(time.time() - t0, 1)
    except json.JSONDecodeError:
        return {"is_error": True, "result": (proc.stdout + proc.stderr)[-500:]}, round(time.time() - t0, 1)


def file_findings(g: Gitea, repo: str, issue: int, window: int, findings: list[dict]) -> list[int]:
    full = f"{g.user}/{repo}"
    g.ensure_label(full, "lookback", "FBCA04", "Found by a post-ship lookback review")
    lid = g.label_id(full, "lookback")
    existing = {i["title"] for i in g.paged(f"/repos/{full}/issues?state=all&labels=lookback&type=issues")}
    filed = []
    for f in findings:
        title = f"[lookback {window}d] {f['title']}"
        if title in existing:
            continue
        where = f"`{f['file']}`" + (f" line {f['line']}" if f["line"] else "")
        body = (f"Found by the {window}-day lookback of #{issue} (severity **{f['severity']}**, {f['category']}).\n\n"
                f"**Where:** {where}\n**Reference:** {f['reference'] or 'n/a'}\n\n{f['detail']}\n\n"
                "---\n_Written by a read-only review agent from the code at `main`; verify before acting. "
                "Filed by `scripts/lookback.py`._")
        created = g.call("POST", f"/repos/{full}/issues", {"title": title, "body": body, "labels": [lid]})
        filed.append(created["number"])
    return filed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--issue", type=int)
    ap.add_argument("--window", type=int, choices=WINDOWS)
    ap.add_argument("--dry-run", action="store_true", help="list what is due; run nothing")
    ap.add_argument("--force", action="store_true", help="ignore the ship date (manual test), still records the run")
    a = ap.parse_args()
    g = Gitea()
    now = datetime.now(UTC)
    done = {(r["repo"], r["issue"], r["window_days"]) for r in read_jsonl("lookbacks.jsonl") if r.get("status") == "ok"}
    for repo in app_repos(g, a.repo):
        issues = g.paged(f"/repos/{g.user}/{repo}/issues?state=all&type=issues&labels=stage/shipped")
        for issue in issues:
            n = issue["number"]
            if a.issue and n != a.issue:
                continue
            ship = shipped_date(g, repo, n)
            if ship is None:
                print(f"{repo}#{n}: shipped label has no timeline date, skipped")
                continue
            have = {w for (r, i, w) in done if r == repo and i == n}
            windows = [a.window] if a.force and a.window else due_windows(ship, now, have)
            if not windows:
                nxt = min((w for w in WINDOWS if w not in have), default=None)
                print(f"{repo}#{n}: shipped {ship:%Y-%m-%d}; next window {nxt}d" if nxt else f"{repo}#{n}: all windows done")
                continue
            window = max(windows)  # one review covers every window that is already due
            print(f"{repo}#{n}: shipped {ship:%Y-%m-%d}, window {window}d is due")
            if a.dry_run:
                continue
            record = {"ts": now_iso(), "repo": repo, "issue": n, "window_days": window,
                      "ship_date": ship.strftime("%Y-%m-%d"), "status": "ok", "findings": 0, "filed_issues": [],
                      "by_severity": {}, "by_category": {}}
            try:
                with read_only_clone(g, repo) as clone:
                    result, secs = run_agent(clone, n)
                record.update(duration_s=secs, cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"))
                if result.get("is_error"):
                    raise RuntimeError(f"agent failed: {str(result.get('result'))[-300:]}")
                findings = extract_findings(str(result.get("result", "")))
                filed = file_findings(g, repo, n, window, findings)
                record.update(findings=len(findings), filed_issues=filed,
                              by_severity={s: sum(f["severity"] == s for f in findings) for s in SEVERITIES},
                              by_category={c: sum(f["category"] == c for f in findings) for c in CATEGORIES})
            except Exception as exc:
                record.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            append_jsonl("lookbacks.jsonl", record)
            print(json.dumps(record))


if __name__ == "__main__":
    main()
