"""The factory board: one static HTML page with a kanban of every work item, who holds it and why, the factory's health,
and a few numbers from the data the factory already keeps. Written by the status job every minute (scripts/status.py)
to $WORK/state/board/index.html and served read-only by factory-board.service on the foreman host.

Nothing here decides or changes anything. The page refreshes itself every 60 s and loads nothing from outside.

Columns (from the issue's last foreman comment, its labels and the processes on this host):
  Inbox          open, no checkpoint yet
  Your turn      a checkpoint before the code (CP1 spec, CP2 threat model, CP2b questions, CP3 plan): you answer
  Agent working  a step runs now
  Review         CP4: the PR and the staged build wait for you
  Blocked        a failed or blocked step, retries used up, or something only you can fix
  Done           closed in the last 7 days
"""

from __future__ import annotations

import html
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

COLUMNS = [("inbox", "Inbox"), ("you", "Your turn"), ("agent", "Agent working"), ("review", "Review"),
           ("blocked", "Blocked"), ("done", "Done (7 days)")]
CP_LABEL = re.compile(r"^🛑 CHECKPOINT\s+(\S+?):\s*(.*)$")
BLOCKED = re.compile(r"-(blocked|failed|retry|fix)$|^manual")
MODEL_IN_CLAIM = re.compile(r"on model `([^`]+)`")
DONE_DAYS = 7
NIGHT_LABEL = "night/ok"


def _age(ts: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = int((now - then).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h {mins % 60:02d}"
    return f"{mins // 1440} d"


def column_of(pivot: dict | None, running: bool) -> tuple[str, str]:
    """(column key, checkpoint label) from the foreman's last checkpoint or claim."""
    if running:
        return "agent", ""
    if pivot is None:
        return "inbox", ""
    m = CP_LABEL.match(pivot["body"].strip().splitlines()[0])
    label = m.group(1) if m else ""
    if BLOCKED.search(label):
        return "blocked", label
    if label.startswith("4") or label.startswith("after_gate4"):
        return "review", label
    return "you", label


def card(issue: dict, repo: str, base_url: str, view: dict | None, runs: list[dict], now: datetime,
         steps: dict) -> dict[str, Any]:
    app = repo.split("/")[1]
    n = issue["number"]
    labels = [lab["name"] for lab in issue.get("labels", [])]
    pivot = view["pivot"] if view else None
    running = bool(view and view["running"])
    col, label = column_of(pivot, running)
    if issue.get("state") == "closed":
        col = "done"
    why, who, since = "", "you", pivot["created_at"] if pivot else issue.get("created_at", "")
    if pivot:
        first = pivot["body"].strip().splitlines()[0]
        why = CP_LABEL.sub(r"\2", first) if first.startswith("🛑") else first.lstrip("▶ ").strip()
    if col == "agent":
        model = MODEL_IN_CLAIM.search(pivot["body"]) if pivot else None
        step = steps.get((app, n))
        who = f"agent · {model.group(1) if model else 'default'}"
        why = f"step `{step[0]}`, {step[1] // 60} min" if step else "CI, staging or the next phase"
    elif col == "inbox":
        who, why = "nobody yet", "no checkpoint: start it with new_change.py, or label it for the night shift"
    elif col == "done":
        who, why, since = "", "closed", issue.get("closed_at") or ""
    mine = [r for r in runs if r.get("repo") == app and r.get("issue") == n]
    return {
        "key": f"{app}#{n}", "title": issue.get("title", ""), "url": f"{base_url}/{repo}/issues/{n}", "col": col,
        "label": label, "why": why, "who": who, "since": _age(since, now) if since else "",
        "night": NIGHT_LABEL in labels, "pending": len(view["pending"]) if view else 0,
        "cost": sum(float(r.get("cost_usd") or 0) for r in mine), "runs": len(mine),
        "agent_min": sum(float(r.get("duration_s") or 0) for r in mine) / 60,
    }


def analytics(data: Path, now: datetime) -> dict[str, Any]:
    def rows(name: str) -> list[dict]:
        p = data / f"{name}.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    runs, minutes, waits = rows("agent_runs"), rows("human_minutes"), rows("gate_waits")
    week = (now - timedelta(days=7)).isoformat()
    recent = [r for r in runs if str(r.get("ts", "")) >= week[:19]]
    by_model: dict[str, float] = defaultdict(float)
    count_model: dict[str, int] = defaultdict(int)
    for r in recent:
        m = str(r.get("model") or "default")
        by_model[m] += float(r.get("cost_usd") or 0)
        count_model[m] += 1
    by_step: dict[str, list[float]] = defaultdict(list)
    for r in recent:
        by_step[str(r.get("step"))].append(float(r.get("duration_s") or 0) / 60)
    src: dict[str, int] = defaultdict(int)
    for m in minutes:
        src[str(m.get("source") or "typed")] += int(m.get("minutes") or 0)
    by_gate: dict[str, list[float]] = defaultdict(list)
    for w in waits:
        by_gate[str(w.get("checkpoint", "?"))].append(float(w.get("wait_min") or 0))
    local = count_model.get("local", 0)
    return {
        "runs_7d": len(recent), "cost_7d": sum(by_model.values()), "errors_7d": sum(1 for r in recent if r.get("is_error")),
        "local_share": (local / len(recent)) if recent else 0.0,
        "by_model": sorted(((m, count_model[m], c) for m, c in by_model.items()), key=lambda x: -x[1]),
        "by_step": sorted(((s, len(v), sum(v) / len(v)) for s, v in by_step.items()), key=lambda x: -x[1]),
        "human_min": dict(src),
        "waits": sorted(((g, len(v), sum(v) / len(v)) for g, v in by_gate.items()), key=lambda x: -x[2])[:6],
    }


def esc(x: object) -> str:
    return html.escape(str(x), quote=True)


CSS = """
:root{--bg:#0f172a;--panel:#1e293b;--card:#273449;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--you:#c084fc}
@media (prefers-color-scheme: light){:root{--bg:#f1f5f9;--panel:#fff;--card:#f8fafc;--line:#cbd5e1;--text:#0f172a;
--muted:#475569;--accent:#0369a1;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--you:#7e22ce}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;padding:14px 16px 6px}
h1{font-size:18px;margin:0}small,.muted{color:var(--muted)}a{color:var(--accent);text-decoration:none}
.health{display:flex;flex-wrap:wrap;gap:8px;padding:6px 16px 12px}.pill{background:var(--panel);border:1px solid var(--line);
border-radius:999px;padding:4px 10px;white-space:nowrap}.pill b{font-weight:600}.ok{color:var(--ok)}.warn{color:var(--warn)}
.bad{color:var(--bad)}.board{display:grid;grid-template-columns:repeat(6,minmax(200px,1fr));gap:10px;padding:0 16px 16px;
overflow-x:auto}.col{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:120px}
.col h2{font-size:13px;margin:2px 4px 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.col h2 span{float:right}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 9px;
margin-bottom:8px}.card .k{font-weight:600}.card .t{margin:2px 0 4px}.card .why{color:var(--muted);font-size:13px}
.card .meta{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:12px;color:var(--muted);margin-top:5px}
.who-you{color:var(--you)}.who-agent{color:var(--accent)}.tag{border:1px solid var(--line);border-radius:4px;padding:0 4px}
section{padding:0 16px 16px}section h2{font-size:15px;margin:8px 0}.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:150px}
.tile .v{font-size:22px;font-weight:600}table{border-collapse:collapse;width:100%;max-width:900px;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}td,th{padding:5px 8px;border-bottom:1px solid var(--line);
text-align:left;font-size:13px}th{color:var(--muted);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums}
pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;overflow-x:auto;font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
@media (max-width:760px){.board{grid-template-columns:1fr;overflow:visible}}
"""


def render(cards: list[dict], health: list[tuple[str, str, str]], stats: dict[str, Any], events: list[str],
           now: datetime, host: str) -> str:
    cols = {k: [c for c in cards if c["col"] == k] for k, _ in COLUMNS}
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           "<meta http-equiv='refresh' content='60'><title>Factory board</title>", f"<style>{CSS}</style></head><body>",
           f"<header><h1>Factory board</h1><small>updated {now.strftime('%Y-%m-%d %H:%M')} UTC on {esc(host)} · "
           "refreshes every minute · read-only</small></header><div class='health'>"]
    for name, value, level in health:
        out.append(f"<span class='pill'>{esc(name)}: <b class='{level}'>{esc(value)}</b></span>")
    out.append("</div><div class='board'>")
    for key, title in COLUMNS:
        out.append(f"<div class='col'><h2>{esc(title)}<span>{len(cols[key])}</span></h2>")
        for c in cols[key]:
            who_cls = "who-agent" if c["who"].startswith("agent") else "who-you" if c["who"] == "you" else ""
            tags = []
            if c["night"]:
                tags.append("<span class='tag'>🌙 night</span>")
            if c["label"]:
                tags.append(f"<span class='tag'>CP {esc(c['label'])}</span>")
            if c["pending"]:
                tags.append(f"<span class='tag warn'>{c['pending']} command(s) queued</span>")
            out.append(
                f"<div class='card'><div class='k'><a href='{esc(c['url'])}'>{esc(c['key'])}</a></div>"
                f"<div class='t'>{esc(c['title'])}</div><div class='why'>{esc(c['why'])}</div><div class='meta'>"
                + (f"<span class='{who_cls}'>{esc(c['who'])}</span>" if c["who"] else "")
                + (f"<span>{esc(c['since'])}</span>" if c["since"] else "")
                + (f"<span>{c['runs']} runs · {c['agent_min']:.0f} min · ${c['cost']:.2f}</span>" if c["runs"] else "")
                + "".join(tags) + "</div></div>")
        out.append("</div>")
    out.append("</div>")
    hm = stats["human_min"]
    out += ["<section><h2>Last 7 days</h2><div class='tiles'>",
            f"<div class='tile'><div class='v'>{stats['runs_7d']}</div><small>agent steps</small></div>",
            f"<div class='tile'><div class='v'>${stats['cost_7d']:.2f}</div><small>cloud spend</small></div>",
            f"<div class='tile'><div class='v'>{stats['local_share'] * 100:.0f}%</div><small>steps on the local model</small></div>",
            f"<div class='tile'><div class='v {'bad' if stats['errors_7d'] else ''}'>{stats['errors_7d']}</div>"
            "<small>failed steps</small></div>",
            f"<div class='tile'><div class='v'>{hm.get('typed', 0) + hm.get('default', 0)} min</div><small>your time at gates "
            f"({hm.get('typed', 0)} typed, {hm.get('default', 0)} default, {hm.get('auto', 0)} auto)</small></div>",
            "</div></section><section class='grid2'><div><h2>By model</h2><table><tr><th>model</th><th>steps</th>"
            "<th>cost</th></tr>"]
    out += [f"<tr><td>{esc(m)}</td><td class='n'>{n}</td><td class='n'>${c:.2f}</td></tr>" for m, n, c in stats["by_model"]]
    out.append("</table></div><div><h2>By step</h2><table><tr><th>step</th><th>runs</th><th>avg min</th></tr>")
    out += [f"<tr><td>{esc(s)}</td><td class='n'>{n}</td><td class='n'>{a:.1f}</td></tr>" for s, n, a in stats["by_step"]]
    out.append("</table></div><div><h2>Checkpoints: how long they waited for you</h2><table><tr><th>checkpoint</th>"
               "<th>times</th><th>avg min</th></tr>")
    out += [f"<tr><td>{esc(g)}</td><td class='n'>{n}</td><td class='n'>{a:.0f}</td></tr>" for g, n, a in stats["waits"]]
    out.append("</table></div></section>")
    if events:
        out.append("<section><h2>Last events</h2><pre>" + esc("\n".join(events)) + "</pre></section>")
    out.append("</body></html>")
    return "\n".join(out)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # the server never serves half a page
