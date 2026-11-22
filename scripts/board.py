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


FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.DOTALL)


def diagnosis(body: str, limit: int = 700) -> str:
    """What went wrong, from a blocked checkpoint: its first code block (the error), else its first paragraph."""
    m = FENCE.search(body)
    if m:
        lines = [x for x in m.group(1).strip().splitlines() if x.strip()]
        text = "\n".join(lines[-8:])
    else:
        parts = [x.strip() for x in body.split("\n\n") if x.strip() and not x.lstrip().startswith(("🛑", "<!--", "_Board"))]
        text = parts[0] if parts else ""
    return text[-limit:]


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
    queued = [{"text": c["body"].strip().splitlines()[0][:90] if c["body"].strip() else "", "age": _age(c["created_at"], now)}
              for c in (view["pending"] if view else [])]
    return {
        "key": f"{app}#{n}", "title": issue.get("title", ""), "url": f"{base_url}/{repo}/issues/{n}", "col": col,
        "label": label, "why": why, "who": who, "since": _age(since, now) if since else "",
        "night": NIGHT_LABEL in labels, "pending": len(queued), "queued": queued, "app": app,
        "diagnosis": diagnosis(pivot["body"]) if pivot and col == "blocked" else "",
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
    failed = [r for r in recent if r.get("is_error")]
    classes: dict[str, int] = defaultdict(int)
    for r in failed:
        classes[str(r.get("class") or "unknown (recorded before classes)")] += 1
    return {
        "failures": [{"ts": str(r.get("ts", ""))[5:16].replace("T", " "), "item": f"{r.get('repo')}#{r.get('issue')}",
                      "step": r.get("step"), "model": r.get("model") or "default",
                      "class": r.get("class") or "unknown"} for r in reversed(failed)][:15],
        "classes": sorted(classes.items(), key=lambda x: -x[1]),
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
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}a{color:var(--accent);text-decoration:none}
header{padding:12px 16px 4px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}h1{font-size:18px;margin:0}
small,.muted{color:var(--muted)}.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 16px;
border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.bar input{flex:1 1 180px;min-width:0;font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--text)}.chip{font:inherit;font-size:13px;padding:7px 11px;border-radius:999px;
border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;min-height:36px}
.chip[aria-pressed=true]{background:var(--accent);color:var(--bg);border-color:var(--accent)}.chip b{font-weight:700}
.health{display:flex;flex-wrap:wrap;gap:6px;padding:8px 16px}.pill{background:var(--panel);border:1px solid var(--line);
border-radius:999px;padding:4px 10px;font-size:13px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.board{display:grid;grid-template-columns:repeat(6,minmax(210px,1fr));gap:10px;padding:4px 16px 16px;overflow-x:auto}
.col{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px 8px;min-height:80px}
.col>summary{list-style:none;cursor:pointer;font-size:13px;margin:4px;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em;display:flex;justify-content:space-between}.col>summary::-webkit-details-marker{display:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 10px;margin:8px 0}
.card .k{font-weight:600;display:flex;justify-content:space-between;gap:6px}.card .t{margin:2px 0 4px}
.card .why{color:var(--muted);font-size:14px}.meta{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:12px;
color:var(--muted);margin-top:6px}.who-you{color:var(--you)}.who-agent{color:var(--accent)}
.tag{border:1px solid var(--line);border-radius:4px;padding:0 5px}details.more{margin-top:6px;font-size:13px}
details.more summary{cursor:pointer;color:var(--warn)}pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:8px;font-size:12px;margin:6px 0;overflow-x:auto}
section{padding:0 16px 14px}section>details>summary,section h2{font-size:15px;font-weight:600;margin:10px 0;cursor:pointer}
.tiles{display:flex;flex-wrap:wrap;gap:10px}.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;min-width:140px;flex:1 1 140px}.tile .v{font-size:22px;font-weight:600}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px;
overflow:hidden}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
th{color:var(--muted);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.barline{display:flex;align-items:center;gap:8px;font-size:13px;margin:4px 0}.barline i{display:block;height:10px;
border-radius:5px;background:var(--bad);min-width:4px}.empty{color:var(--muted);font-size:13px;padding:4px}
.hide{display:none!important}
@media (max-width:760px){.board{grid-template-columns:1fr;overflow:visible}.col[data-col=done]:not([open]){min-height:0}}
"""

GATES = """<table><tr><th>CP</th><th>What waits for you</th><th>What you decide</th><th>After your /go</th></tr>
<tr><td>1 spec</td><td>spec.md + holdout tests</td><td>Right change, right tests? Approve the spec</td><td>threat model drafted</td></tr>
<tr><td>2 threat model</td><td>threat-model.md</td><td>Each PROPOSED row: accept, change or reject</td><td>research</td></tr>
<tr><td>2b questions</td><td>open questions (only if any)</td><td>Answer in a comment, or /go without</td><td>plan</td></tr>
<tr><td>3 plan</td><td>plan.md</td><td>Approve, or /rework &lt;what to change&gt;</td><td>build: phases, CI, validation, staging</td></tr>
<tr><td>4 review</td><td>PR (checks green) + staged build</td><td>Try it, read the diff, merge; or /rework</td><td>/go after the merge: mirror, staging removed</td></tr>
<tr><td>4b</td><td>merged and mirrored</td><td>/go closes the item (release is manual for now)</td><td>closed</td></tr></table>
<p class="muted">Suffixes: <b>-retry</b> a step failed (budget 2) · <b>-blocked</b> model, credit or login (no retry spent) ·
<b>-fix</b> something only you can fix · <b>-failed</b> retries used up. Commands: <code>/go</code> (local model;
<code>model=smart|fast|cheap</code> for Claude; <code>minutes=N</code> optional), <code>/rework &lt;note&gt;</code> (CP3, CP4),
<code>/stop</code>. Label <code>night/ok</code>: the night shift passes CP1 to CP3 on the local model and stops at CP4.</p>"""

JS = """
(function(){var q=document.getElementById('q'),chips=[].slice.call(document.querySelectorAll('.chip[data-f]')),f='all';
try{var s=JSON.parse(localStorage.getItem('fb')||'{}');if(s.q)q.value=s.q;if(s.f)f=s.f}catch(e){}
function apply(){var t=q.value.toLowerCase().trim();chips.forEach(function(c){c.setAttribute('aria-pressed',c.dataset.f===f)});
document.querySelectorAll('.card').forEach(function(c){var ok=(!t||c.dataset.s.indexOf(t)>=0)&&
(f==='all'||(' '+c.dataset.tags+' ').indexOf(' '+f+' ')>=0);c.classList.toggle('hide',!ok)});
document.querySelectorAll('.col').forEach(function(col){var n=col.querySelectorAll('.card:not(.hide)').length;
col.querySelector('.cnt').textContent=n;col.classList.toggle('hide',f!=='all'&&n===0)});
try{localStorage.setItem('fb',JSON.stringify({q:q.value,f:f}))}catch(e){}}
q.addEventListener('input',apply);chips.forEach(function(c){c.addEventListener('click',function(){f=(f===c.dataset.f&&f!=='all')?'all':c.dataset.f;apply()})});
if(window.matchMedia('(max-width:760px)').matches){document.querySelectorAll('.col[data-col=done],.col[data-col=inbox]').forEach(function(d){d.open=false})}
apply()})();
"""


def _card_html(c: dict) -> str:
    who_cls = "who-agent" if c["who"].startswith("agent") else "who-you" if c["who"] == "you" else ""
    tags = [c["col"], c["app"]]
    if c["col"] in ("you", "review", "blocked"):
        tags.append("mine")
    if c["night"]:
        tags.append("night")
    if c["pending"]:
        tags.append("queued")
    chips = []
    if c["night"]:
        chips.append("<span class='tag'>🌙 night</span>")
    if c["label"]:
        chips.append(f"<span class='tag'>CP {esc(c['label'])}</span>")
    extra = ""
    if c["queued"]:
        rows = "".join(f"<li><code>{esc(x['text'])}</code> <small>{esc(x['age'])} ago</small></li>" for x in c["queued"])
        extra += (f"<details class='more' open><summary>⏳ {len(c['queued'])} command(s) waiting for the foreman</summary>"
                  f"<ul>{rows}</ul></details>")
    if c["diagnosis"]:
        extra += f"<details class='more'><summary>What went wrong</summary><pre>{esc(c['diagnosis'])}</pre></details>"
    search = " ".join([c["key"], c["title"], c["why"], c["who"], c["label"]]).lower()
    return (f"<div class='card' data-s='{esc(search)}' data-tags='{esc(' '.join(tags))}'>"
            f"<div class='k'><a href='{esc(c['url'])}'>{esc(c['key'])}</a><a href='{esc(c['url'])}' "
            f"title='open the issue to answer'>open →</a></div>"
            f"<div class='t'>{esc(c['title'])}</div><div class='why'>{esc(c['why'])}</div><div class='meta'>"
            + (f"<span class='{who_cls}'>{esc(c['who'])}</span>" if c["who"] else "")
            + (f"<span>{esc(c['since'])}</span>" if c["since"] else "")
            + (f"<span>{c['runs']} runs · {c['agent_min']:.0f} min · ${c['cost']:.2f}</span>" if c["runs"] else "")
            + "".join(chips) + f"</div>{extra}</div>")


def render(cards: list[dict], health: list[tuple[str, str, str]], stats: dict[str, Any], events: list[str],
           now: datetime, host: str, busy: str = "") -> str:
    order = ["you", "review", "blocked", "agent", "inbox", "done"]  # whose turn first: the phone shows them stacked
    titles = dict(COLUMNS)
    cols = {k: [c for c in cards if c["col"] == k] for k in order}
    mine = sum(len(cols[k]) for k in ("you", "review", "blocked"))
    queued = [(c, x) for c in cards for x in c["queued"]]
    apps = sorted({c["app"] for c in cards})
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           "<meta http-equiv='refresh' content='60'><title>Factory board</title>", f"<style>{CSS}</style></head><body>",
           f"<header><h1>Factory board</h1><small>updated {now.strftime('%H:%M')} UTC on {esc(host)} · every minute · "
           "read-only</small></header>",
           "<div class='bar'><input id='q' type='search' placeholder='Filter: issue, title, step…' aria-label='filter'>",
           f"<button class='chip' data-f='all'>All</button><button class='chip' data-f='mine'>Needs you <b>{mine}</b></button>",
           f"<button class='chip' data-f='agent'>Agent <b>{len(cols['agent'])}</b></button>",
           f"<button class='chip' data-f='blocked'>Blocked <b>{len(cols['blocked'])}</b></button>",
           f"<button class='chip' data-f='queued'>Queued <b>{len(queued)}</b></button>",
           "<button class='chip' data-f='night'>🌙 Night</button>"]
    if len(apps) > 1:
        out += [f"<button class='chip' data-f='{esc(a)}'>{esc(a)}</button>" for a in apps]
    out.append("</div><div class='health'>")
    for name, value, level in health:
        out.append(f"<span class='pill'>{esc(name)}: <b class='{level}'>{esc(value)}</b></span>")
    out.append("</div>")
    if busy or queued:
        out.append("<section><details open><summary>⏳ What the foreman is doing, and what waits</summary>")
        if busy:
            out.append(f"<p>{esc(busy)}</p>")
        if queued:
            out.append("<table><tr><th>item</th><th>command</th><th>sent</th></tr>" + "".join(
                f"<tr><td><a href='{esc(c['url'])}'>{esc(c['key'])}</a></td><td><code>{esc(x['text'])}</code></td>"
                f"<td>{esc(x['age'])} ago</td></tr>" for c, x in queued) + "</table>")
        out.append("</details></section>")
    out.append("<div class='board'>")
    for key in order:
        out.append(f"<details class='col' data-col='{key}' open><summary><span>{esc(titles[key])}</span>"
                   f"<span class='cnt'>{len(cols[key])}</span></summary>")
        out += [_card_html(c) for c in cols[key]] or ["<div class='empty'>nothing</div>"]
        out.append("</details>")
    out.append("</div>")
    hm = stats["human_min"]
    out += ["<section><h2>Last 7 days</h2><div class='tiles'>",
            f"<div class='tile'><div class='v'>{stats['runs_7d']}</div><small>agent steps</small></div>",
            f"<div class='tile'><div class='v'>${stats['cost_7d']:.2f}</div><small>cloud spend</small></div>",
            f"<div class='tile'><div class='v'>{stats['local_share'] * 100:.0f}%</div><small>steps on the local model</small></div>",
            f"<div class='tile'><a href='#failures'><div class='v {'bad' if stats['errors_7d'] else ''}'>{stats['errors_7d']}</div>"
            "<small>failed steps ↓</small></a></div>",
            f"<div class='tile'><div class='v'>{hm.get('typed', 0) + hm.get('default', 0)} min</div><small>your gate time "
            f"({hm.get('typed', 0)} typed, {hm.get('default', 0)} default, {hm.get('auto', 0)} auto)</small></div></div></section>"]
    out.append("<section id='failures'><details" + (" open" if stats["failures"] else "") + "><summary>Failures (7 days): "
               f"{stats['errors_7d']}</summary>")
    if stats["failures"]:
        top = max(n for _, n in stats["classes"])
        out += [f"<div class='barline'><span style='min-width:11em'>{esc(cls)}</span><i style='width:{max(4, 160 * n // top)}px'>"
                f"</i><span>{n}</span></div>" for cls, n in stats["classes"]]
        out.append("<table><tr><th>when (UTC)</th><th>item</th><th>step</th><th>model</th><th>class</th></tr>" + "".join(
            f"<tr><td>{esc(f['ts'])}</td><td>{esc(f['item'])}</td><td>{esc(f['step'])}</td><td>{esc(f['model'])}</td>"
            f"<td>{esc(f['class'])}</td></tr>" for f in stats["failures"]) + "</table>")
        out.append("<p class='muted'>The full error of a blocked item is on its card (“What went wrong”) and on the issue.</p>")
    else:
        out.append("<div class='empty'>No failed step this week.</div>")
    out.append("</details></section>")
    out.append("<section><details><summary>Numbers by model, step and checkpoint</summary><div class='grid2'><div>"
               "<table><tr><th>model</th><th>steps</th><th>cost</th></tr>")
    out += [f"<tr><td>{esc(m)}</td><td class='n'>{n}</td><td class='n'>${c:.2f}</td></tr>" for m, n, c in stats["by_model"]]
    out.append("</table></div><div><table><tr><th>step</th><th>runs</th><th>avg min</th></tr>")
    out += [f"<tr><td>{esc(s_)}</td><td class='n'>{n}</td><td class='n'>{a:.1f}</td></tr>" for s_, n, a in stats["by_step"]]
    out.append("</table></div><div><table><tr><th>checkpoint</th><th>times</th><th>avg wait min</th></tr>")
    out += [f"<tr><td>{esc(g)}</td><td class='n'>{n}</td><td class='n'>{a:.0f}</td></tr>" for g, n, a in stats["waits"]]
    out.append("</table></div></div></details></section>")
    out.append(f"<section><details><summary>What the checkpoints mean</summary>{GATES}</details></section>")
    if events:
        out.append("<section><details><summary>Last events</summary><pre>" + esc("\n".join(events)) + "</pre></details></section>")
    out.append(f"<script>{JS}</script></body></html>")
    return "\n".join(out)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # the server never serves half a page
