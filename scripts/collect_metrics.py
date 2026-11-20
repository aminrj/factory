#!/usr/bin/env python3
"""Collect software factory metrics from every Gitea repo with topic `factory-app`.

Writes:
  data/prs.jsonl         one line per merged PR (rewritten each run, sorted, idempotent)
  data/weekly.jsonl      one snapshot per ISO week (replaced if re-run in the same week)
  data/SUMMARY.md        human-readable tables for the public log
  BOARD.md               the board: issues grouped by their stage/* label (factory repo and app repos)
  data/agent_runs.jsonl  is written by dispatch.py; summarised here (agent minutes and cost)

Needs: a Gitea token in ~/.config/git/gitea-token, Python 3.10+, no other dependencies.
Run it by hand or from cron on my machine, then commit and push. Public links point at the GitHub mirror.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import GITHUB_OWNER, STAGES, Gitea  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GATES = ["spec", "threat_model", "plan_review", "code_review", "rework"]

MINUTES_RE = re.compile(
    r"##\s*Human minutes\s*\n\|[^\n]*\|\s*\n\|[-|: ]+\|\s*\n\|([^\n]*)\|", re.IGNORECASE
)
FIRST_PASS_RE = re.compile(r"-\s*\[[xX]\]\s*Holdout tests passed on the agent", re.IGNORECASE)


def parse_minutes(body: str) -> dict[str, int | None]:
    match = MINUTES_RE.search(body or "")
    if not match:
        return dict.fromkeys(GATES)
    cells = [c.strip() for c in match.group(1).split("|")]
    values: list[int | None] = []
    for cell in cells[: len(GATES)]:
        values.append(int(cell) if cell.isdigit() else None)
    values += [None] * (len(GATES) - len(values))
    return dict(zip(GATES, values, strict=True))


def hours_between(start: str, end: str) -> float:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return round(delta.total_seconds() / 3600, 1)


def collect(g: Gitea) -> tuple[list[dict], dict[str, int]]:
    found = g.paged("/repos/search?q=factory-app&topic=true")
    records: list[dict] = []
    escaped: dict[str, int] = {}
    for repo in sorted(r["full_name"] for r in found):
        prs = g.paged(f"/repos/{repo}/pulls?state=closed")
        for pr in prs:
            if not pr.get("merged"):
                continue
            labels = sorted(label["name"] for label in pr["labels"])
            records.append({
                "repo": repo.split("/")[-1],
                "number": pr["number"],
                "title": pr["title"],
                "created_at": pr["created_at"],
                "merged_at": pr["merged_at"],
                "lead_hours": hours_between(pr["created_at"], pr["merged_at"]),
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "agent_branch": pr["head"]["ref"].startswith("agent/"),
                "dependabot": False,
                "rework": "rework" in labels,
                "first_pass": bool(FIRST_PASS_RE.search(pr["body"] or "")),
                "minutes": parse_minutes(pr["body"] or ""),
                "labels": labels,
            })
        bugs = g.paged(f"/repos/{repo}/issues?state=all&type=issues&labels=escaped-bug")
        escaped[repo.split("/")[-1]] = len(bugs)
    records.sort(key=lambda r: (r["repo"], r["number"]))
    return records, escaped


def write_board(g: Gitea) -> None:
    repos = {r["full_name"] for r in g.paged("/repos/search?q=factory-app&topic=true")}
    repos.add(f"{g.user}/factory")
    rows: dict[str, list[str]] = {stage: [] for stage in STAGES}
    for repo in sorted(repos):
        name = repo.split("/")[-1]
        for issue in g.paged(f"/repos/{repo}/issues?state=all&type=issues"):
            stage = next((lb["name"][6:] for lb in issue["labels"] if lb["name"].startswith("stage/")), None)
            if stage in rows:
                rows[stage].append(f"| {issue['title']} | {name}#{issue['number']} |")
    lines = ["# Board", "", f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} by `scripts/collect_metrics.py`._", ""]
    for stage in STAGES:
        lines += [f"## {stage}", "", "| Item | Issue |", "|---|---|", *(rows[stage] or ["| (none) | |"]), ""]
    (ROOT / "BOARD.md").write_text("\n".join(lines))


def agent_totals() -> dict[str, float]:
    path = DATA / "agent_runs.jsonl"
    runs = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return {
        "agent_runs": len(runs),
        "agent_minutes": round(sum(r.get("duration_s", 0) for r in runs) / 60, 1),
        "agent_cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 2),
        "fallbacks": sum(1 for r in runs if r.get("fallback")),
    }


def human_minutes_override() -> dict:
    """Minutes recorded at each /go (data/human_minutes.jsonl) win over the PR tables, which are filled from them."""
    path = DATA / "human_minutes.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    if not rows:
        return {}
    by_gate = dict.fromkeys(GATES, 0)
    for r in rows:
        if r["gate"] in by_gate:
            by_gate[r["gate"]] += r["minutes"]
    return {"human_minutes_total": sum(by_gate.values()), "human_minutes_by_gate": by_gate}


def summarize(records: list[dict], escaped: dict[str, int]) -> dict:
    agent = [r for r in records if r["agent_branch"]]
    minutes_by_gate = {
        g: [r["minutes"][g] for r in agent if r["minutes"][g] is not None] for g in GATES
    }
    total_minutes = sum(sum(v) for v in minutes_by_gate.values())
    return {
        "week": datetime.now(UTC).strftime("%G-W%V"),
        "taken_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "apps": len(escaped),
        "merged_prs": len(records),
        "agent_prs": len(agent),
        "rework_rate": round(sum(r["rework"] for r in agent) / len(agent), 2) if agent else None,
        "first_pass_rate": round(sum(r["first_pass"] for r in agent) / len(agent), 2) if agent else None,
        "median_lead_hours": statistics.median(r["lead_hours"] for r in agent) if agent else None,
        "human_minutes_total": total_minutes,
        "human_minutes_by_gate": {g: sum(v) for g, v in minutes_by_gate.items()},
        **human_minutes_override(),
        "escaped_bugs": sum(escaped.values()),
        **agent_totals(),
    }


def write(records: list[dict], escaped: dict[str, int], snap: dict) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / "prs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))

    weekly_path = DATA / "weekly.jsonl"
    weeks = [json.loads(x) for x in weekly_path.read_text().splitlines()] if weekly_path.exists() else []
    weeks = [w for w in weeks if w["week"] != snap["week"]] + [snap]
    weekly_path.write_text("".join(json.dumps(w) + "\n" for w in weeks))

    def fmt(value: object) -> str:
        return "n/a" if value is None else str(value)

    lines = [
        "# Factory metrics",
        "",
        f"_Last snapshot: {snap['taken_at']} ({snap['week']}). Generated by `scripts/collect_metrics.py`._",
        "",
        "## Totals",
        "",
        "| Apps | Merged PRs | Agent PRs | Rework rate | First-pass holdout rate | Median lead time (h) | Human minutes | Escaped bugs |",
        "|---|---|---|---|---|---|---|---|",
        (
            f"| {snap['apps']} | {snap['merged_prs']} | {snap['agent_prs']} | "
            f"{fmt(snap['rework_rate'])} | {fmt(snap['first_pass_rate'])} | "
            f"{fmt(snap['median_lead_hours'])} | {snap['human_minutes_total']} | "
            f"{snap['escaped_bugs']} |"
        ),
        "",
        "## Agent runs",
        "",
        "| Runs | Agent minutes | Cost (USD) | Fallbacks |",
        "|---|---|---|---|",
        f"| {snap['agent_runs']} | {snap['agent_minutes']} | {snap['agent_cost_usd']} | {snap['fallbacks']} |",
        "",
        "## Where human time goes (agent PRs)",
        "",
        "| Spec | Threat model | Plan review | Code review | Rework |",
        "|---|---|---|---|---|",
        "| " + " | ".join(str(snap["human_minutes_by_gate"][g]) for g in GATES) + " |",
        "",
        "## Per app",
        "",
        "| Repo | Merged PRs | Agent PRs | Rework | Escaped bugs |",
        "|---|---|---|---|---|",
    ]
    for repo in sorted(escaped):
        rs = [r for r in records if r["repo"] == repo]
        ag = [r for r in rs if r["agent_branch"]]
        lines.append(
            f"| [{repo}](https://github.com/{GITHUB_OWNER}/{repo}) | {len(rs)} | {len(ag)} | "
            f"{sum(r['rework'] for r in ag)} | {escaped[repo]} |"
        )
    lines += ["", "## Weekly history", "", "| Week | Agent PRs | Rework rate | Human minutes |", "|---|---|---|---|"]
    for w in weeks:
        lines.append(f"| {w['week']} | {w['agent_prs']} | {fmt(w['rework_rate'])} | {w['human_minutes_total']} |")
    (DATA / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    g = Gitea()
    write_board(g)
    records, escaped = collect(g)
    snap = summarize(records, escaped)
    write(records, escaped, snap)
    print(json.dumps(snap, indent=2))


if __name__ == "__main__":
    main()
