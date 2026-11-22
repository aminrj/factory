#!/usr/bin/env python3
"""One place that says what the factory is doing, and an answer for every command at once.

Runs every minute from its own timer (factory-status.timer), next to the foreman tick and independent of it: a tick that
runs a build for half an hour cannot update anything or answer anyone, and silence looked like a broken foreman
(2026-11-21). This job never runs a step, never takes an item lock and never decides anything. It:

1. rewrites the body of one pinned issue, "Factory status", in the factory repo: the host and the tick, the step running
   now, what waits for the owner, staged builds, the GitHub mirrors, today's spend and the last log lines;
2. answers a command at once when the foreman cannot act on it right away, saying what will happen to it:
   - sent to the issue whose step is running: it will not run (the issue moves to a new checkpoint first); `/stop` is
     heard between two phases;
   - sent to another issue while a tick is busy: it runs when that tick is done;
   - sent to a closed issue: it never runs (the foreman reads open issues only);
3. publishes the factory's own repos (factory, factory-template) to their GitHub mirrors when main moved, through the
   mirror guard. App repos are published by the foreman after a merge (checkpoint 4b), never here.

Usage: status.py [--print]   (--print renders the page to stdout and changes nothing)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mirror  # noqa: E402
import board  # noqa: E402
import commands  # noqa: E402
import lanes  # noqa: E402
import lease  # noqa: E402
import orchestrate as o  # noqa: E402
from gitea import Gitea, Unreachable, by_owner, ours  # noqa: E402

TITLE = "Factory status"
FACTORY_REPOS = ("factory", "factory-template")  # published here; app repos only after a merge (checkpoint 4b)
CLOSED_WINDOW_H = 24  # commands on issues closed longer ago than this are history
DISPATCH = re.compile(r"dispatch\.py\s+(\S+)\s+(\d+)\s+(\S+)")
MODEL_IN_CLAIM = re.compile(r"on model `([^`]+)`")
GATEWAY = "http://127.0.0.1:8090/v1/models"


def state_file(name: str) -> Path:
    return o.STATE / name  # resolved at call time: tests point o.STATE at a temp dir


# ---------------------------------------------------------------- what runs on this host

def processes() -> list[tuple[int, str]]:
    """(seconds running, command line) of this user's processes."""
    out = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "etimes=,args="], capture_output=True,
                         text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        secs, _, args = line.strip().partition(" ")
        if secs.isdigit():
            rows.append((int(secs), args))
    return rows


def busy_tick(procs: list[tuple[int, str]]) -> int | None:
    """Seconds the current tick has been running, or None when no tick runs."""
    return max((s for s, a in procs if "orchestrate.py tick" in a), default=None)


def agent_steps(procs: list[tuple[int, str]]) -> dict[tuple[str, int], tuple[str, int]]:
    """(app, issue) -> (step, seconds) for every agent step running now."""
    out = {}
    for secs, args in procs:
        if "dispatch.py" in args and (m := DISPATCH.search(args)):
            out[(m.group(1), int(m.group(2)))] = (m.group(3), secs)
    return out


def model_reachable(url: str = GATEWAY, timeout: float = 4) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed loopback URL
            return r.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not reachable now"
        return False


# ---------------------------------------------------------------- what each issue is at

def age(ts: str, now: datetime) -> str:
    mins = int((now - o.parse_ts(ts)).total_seconds() // 60)
    return f"{mins} min" if mins < 120 else f"{mins // 60} h {mins % 60:02d}"


def issue_view(g: Gitea, repo: str, n: int) -> dict:
    """The last checkpoint or claim of the foreman, and the owner's commands after it."""
    comments = g.paged(f"/repos/{repo}/issues/{n}/comments")
    mine = [c for c in comments if ours(g, c)]
    pivot = next((c for c in reversed(mine) if c["body"].startswith((o.CP_PREFIX, o.CLAIM_PREFIX))), None)
    owner = [c for c in mine if by_owner(g, c) and not c["body"].startswith((o.CP_PREFIX, o.CLAIM_PREFIX, o.NOTICE_PREFIX, "Agent step"))]
    after = pivot["id"] if pivot else 0
    app = repo.split("/")[1]
    claimed = bool(pivot and pivot["body"].startswith(o.CLAIM_PREFIX))
    # a claim is only a claim: the item runs while a process holds its lock (brevkoll#15 showed "Agent working" for
    # hours after an old reply was posted as a claim)
    alive = claimed and (o.item_is_running(app, n) or lanes.item_running(o.STATE, app, n))
    return {"repo": repo, "n": n, "app": app, "pivot": pivot, "running": alive, "stale": claimed and not alive,
            "pending": [c for c in owner if c["id"] > after and o.is_command(c["body"])]}


def first_line(body: str, limit: int = 110) -> str:
    line = body.strip().splitlines()[0] if body.strip() else ""
    return line if len(line) <= limit else line[:limit - 1] + "…"


def closed_recently(g: Gitea, repos: list[str], now: datetime) -> list[tuple[str, int, str]]:
    since = (now - timedelta(hours=CLOSED_WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [(r, i["number"], i.get("closed_at") or "") for r in repos
            for i in g.paged(f"/repos/{r}/issues?state=closed&type=issues&since={since}")]


# ---------------------------------------------------------------- answers

def ack_text(view: dict, cmd: dict, tick_s: int | None, running: dict | None) -> str | None:
    """What to tell the owner about a command the foreman cannot act on now; None when the next tick will act on it.
    Never writes "(comment N)": the foreman reads that as "already answered" and would skip the command."""
    word = first_line(cmd["body"], 60)
    if view["running"]:
        if o.STOP.match(cmd["body"]):
            return (f"{o.NOTICE_PREFIX} Received `{word}` while a step of this issue is running. The foreman hears it "
                    "between two phases and stops cleanly then; a single step is not cut off midway.")
        step = f"`{running['step']}`" if running else "a step"
        return (f"{o.NOTICE_PREFIX} Received `{word}`, but {step} of this issue is running now. When it ends the issue "
                "moves to a new checkpoint, and this command will not run there (the same words can mean something "
                "else at the next checkpoint). Answer that checkpoint when it appears; `/stop` ends the run between two "
                "phases.")
    if view.get("pivot") and o.parse_marker(view["pivot"]["body"]).get("resume") == "manual":
        return None  # nothing will start: the tick answers at once that a session is needed
    if lanes.enabled() and (o.GO.match(cmd["body"]) or o.REWORK.match(cmd["body"])):
        opts = o.command_options(cmd["body"])
        lane = lanes.lane_of(opts.model or commands.DEFAULT_MODEL)
        if not opts.errors and not lanes.has_room(o.STATE, lane):
            holders = lanes.describe(lanes.busy(o.STATE, lane))
            other = ("To use the cloud lane instead, send it again with `model=fast` (spends Claude tokens)."
                     if lane == "local" else "It starts when a cloud slot frees up.")
            return (f"{o.NOTICE_PREFIX} Received `{word}`. The {lane} lane is busy with {holders}; this starts as "
                    f"soon as it frees up. {other} Nothing to resend.")
        return None
    if tick_s is not None and running and (running["app"], running["n"]) != (view["app"], view["n"]):
        return (f"{o.NOTICE_PREFIX} Received `{word}`. The foreman is busy with {running['app']}#{running['n']} "
                f"(`{running['step']}`, {tick_s // 60} min so far) and acts on this right after it. Nothing to resend.")
    return None


def answer_commands(g: Gitea, views: list[dict], tick_s: int | None, running: dict | None, now: datetime) -> list[str]:
    """Post each answer once. Commands younger than a minute are left to the tick when no tick is busy."""
    path = state_file("acked.json")
    acked = set(json.loads(path.read_text())) if path.exists() else set()
    posted = []
    for v in views:
        for c in v["pending"]:
            key = f"{v['repo']}#{c['id']}"
            if key in acked:
                continue
            text = ack_text(v, c, tick_s, running)
            if text is None:
                continue
            g.comment(v["repo"], v["n"], text)
            acked.add(key)
            posted.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(acked)[-500:]))
    return posted


def answer_closed(g: Gitea, closed: list[tuple[str, int, str]]) -> list[str]:
    path = state_file("acked.json")
    acked = set(json.loads(path.read_text())) if path.exists() else set()
    posted = []
    for repo, n, closed_at in closed:
        for c in g.paged(f"/repos/{repo}/issues/{n}/comments"):
            key = f"{repo}#{c['id']}"
            if (not by_owner(g, c) or key in acked or not o.is_command(c["body"])
                    or not closed_at or c["created_at"] < closed_at):
                continue
            g.comment(repo, n, f"{o.NOTICE_PREFIX} Received `{first_line(c['body'], 60)}`, but this issue is closed, and "
                               "the foreman reads open issues only: it will not run. For a further change, open a new "
                               "issue (factory `scripts/new_change.py`) or reopen this one and answer its checkpoint.")
            o.notify(f"{repo.split('/')[1]}: command on a closed issue", f"#{n}: it will not run; the issue is closed.",
                     click=f"{g.repo_url(repo).removesuffix('.git')}/issues/{n}")
            acked.add(key)
            posted.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(acked)[-500:]))
    return posted


# ---------------------------------------------------------------- mirrors

def mirror_state(clone: Path) -> str:
    """'in sync', 'behind (...)' or 'unknown (...)', from the local refs: mirror.push records what it published."""
    if not (clone / ".git").exists():
        return "unknown (no clone here)"
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, capture_output=True, check=False)
    # the mirror ref is GitHub's main as last fetched: a fresh clone (a new foreman host) has none until it fetches
    subprocess.run(["git", "fetch", "-q", "github"], cwd=clone, capture_output=True, check=False)
    pub = mirror.git(clone, "rev-parse", "-q", "--verify", f"{mirror.MIRROR_REF}^{{tree}}", check=False).strip()
    src = mirror.git(clone, "rev-parse", "-q", "--verify", "origin/main^{tree}", check=False).strip()
    if not pub or not src:
        return "unknown (no mirror ref)"
    if pub == src:
        return "in sync"
    when = mirror.git(clone, "log", "-1", "--format=%cI", "origin/main", check=False).strip()
    return f"behind since {when[11:16]} UTC" if when else "behind"


def publish_factory_repos(work: Path) -> dict[str, str]:
    out = {}
    for name in FACTORY_REPOS:
        clone = work / name
        if mirror_state(clone).startswith("behind"):
            try:
                subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True, capture_output=True)
                out[name] = mirror.push(clone, "origin/main")
            except (SystemExit, subprocess.CalledProcessError) as e:
                out[name] = f"refused or failed: {str(e)[:160]}"
                o.alarm(f"mirror-{name}", f"{name}: mirror not published", out[name])
    return out


# ---------------------------------------------------------------- the page

def spend_today(now: datetime) -> dict[str, float]:
    day = now.strftime("%Y-%m-%d")
    out: dict[str, float] = {}
    for r in o.jsonl(o.DATA / "agent_runs.jsonl"):
        if str(r.get("ts", "")).startswith(day):
            m = str(r.get("model") or "default")
            out[m] = out.get(m, 0.0) + board.real_cost(r)
    return out


def recent_log(work: Path, n: int = 8) -> list[str]:
    path = work / "RUNLOG.md"
    if not path.exists():
        return []
    lines = [x for x in path.read_text(errors="replace").splitlines() if re.match(r"^\d{4}-\d\d-\d\dT", x)]
    return [x[11:16] + " " + x[26:].removeprefix("orchestrate: ")[:140] for x in lines[-n:]]


def staged() -> list[dict]:
    path = state_file("staging.json")
    reg = json.loads(path.read_text()) if path.exists() else {}
    names = subprocess.run(["docker", "ps", "--filter", "label=factory.stage", "--format", "{{.Names}}"],
                           capture_output=True, text=True, check=False).stdout.split()
    return [{**v, "alive": f"stage-{k}" in names} for k, v in reg.items()]


def link(g: Gitea, repo: str, n: int) -> str:
    return f"[{repo.split('/')[1]}#{n}]({g.repo_url(repo).removesuffix('.git')}/issues/{n})"


def render(g: Gitea, now: datetime, host: str, tick_s: int | None, beat_age_s: float | None, model_up: bool,
           views: list[dict], steps: dict, builds: list[dict], mirrors: dict[str, str], spend: dict[str, float],
           log_lines: list[str]) -> str:
    out = [f"_Updated {now.strftime('%Y-%m-%d %H:%M')} UTC by `scripts/status.py` on `{host}`, every minute. "
           "This text is rewritten in place; comments here are not read._", "", "## Now", ""]
    if tick_s is not None:
        out.append(f"- **Foreman: busy** for {tick_s // 60} min. New commands on other issues wait until it is done.")
    elif beat_age_s is None or beat_age_s > 300:
        out.append(f"- **Foreman: ⚠️ no tick for {int((beat_age_s or 0) // 60)} min.** Check the timer on `{host}`.")
    else:
        out.append(f"- **Foreman: idle**, last tick {int(beat_age_s)} s ago. A command is picked up within a minute.")
    out.append(f"- **Local model (`model=local`):** {'reachable' if model_up else '⚠️ not reachable (the GPU machine may be asleep)'}.")
    for v in views:
        if not v["running"]:
            continue
        claim = v["pivot"]
        step = steps.get((v["app"], v["n"]))
        model = MODEL_IN_CLAIM.search(claim["body"])
        what = f"agent step `{step[0]}` for {step[1] // 60} min" if step else "CI, staging or the next phase"
        out.append(f"- **Running:** {link(g, v['repo'], v['n'])}, {what} "
                   f"(model `{model.group(1) if model else 'default'}`; started {age(claim['created_at'], now)} ago).")
    out += ["", "## Waiting for you", ""]
    waiting = [v for v in views if v["pivot"] and not v["running"] and not v["pending"]]
    if not waiting:
        out.append("Nothing.")
    for v in waiting:
        cp = first_line(v["pivot"]["body"]).removeprefix(o.CP_PREFIX).strip(" :")
        out.append(f"- {link(g, v['repo'], v['n'])}: **{cp}** (for {age(v['pivot']['created_at'], now)})")
    queued = [(v, c) for v in views for c in v["pending"]]
    if queued:
        out += ["", "## Commands received, not handled yet", ""]
        for v, c in queued:
            out.append(f"- {link(g, v['repo'], v['n'])}: `{first_line(c['body'], 60)}` ({age(c['created_at'], now)} ago)")
    out += ["", "## Staged builds", ""]
    if not builds:
        out.append("None.")
    for b in builds:
        state = "running" if b["alive"] else "⚠️ container gone"
        out.append(f"- {b['app']}#{b.get('issue')} `{b.get('ref', '')}`: {b.get('url', '')} ({state}, since "
                   f"{str(b.get('started', ''))[11:16]} UTC)")
    out += ["", "## GitHub mirrors", ""]
    out += [f"- {name}: {state}" for name, state in mirrors.items()]
    total = sum(spend.values())
    out += ["", "## Spend today (agent steps)", "",
            f"${total:.2f}" + (" (" + ", ".join(f"{m} ${v:.2f}" for m, v in sorted(spend.items())) + ")" if spend else "")]
    if log_lines:
        out += ["", "## Last events", "", "```", *log_lines, "```"]
    return "\n".join(out) + "\n"


def status_issue(g: Gitea) -> int:
    path = state_file("status.json")
    saved = json.loads(path.read_text()) if path.exists() else {}
    repo = f"{g.user}/factory"
    if n := saved.get("issue"):
        return int(n)
    found = [i for i in g.paged(f"/repos/{repo}/issues?state=open&type=issues") if i["title"] == TITLE]
    if found:
        n = found[0]["number"]
    else:
        n = g.call("POST", f"/repos/{repo}/issues", {"title": TITLE, "body": "Starting…"})["number"]
        try:
            g.call("POST", f"/repos/{repo}/issues/{n}/pin", ok=(200, 201, 204))
        except Exception:  # noqa: BLE001 - pinning is a convenience (older Gitea has no such endpoint)
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"issue": n}))
    return int(n)


def run(g: Gitea, print_only: bool = False) -> str:
    now = datetime.now(UTC)
    procs = processes()
    tick_s = busy_tick(procs)
    steps = agent_steps(procs)
    issues = o.active_issues(g)
    views = [issue_view(g, r, n) for r, n in issues]
    running = next(({"app": v["app"], "n": v["n"], "step": steps.get((v["app"], v["n"]), ("a step", 0))[0]}
                    for v in views if v["running"]), None)
    try:
        beat_age = time.time() - float(o.HEARTBEAT.read_text())
    except (OSError, ValueError):
        beat_age = None
    if not print_only:
        answer_commands(g, views, tick_s, running, now)
        apps = sorted({r for r, _ in issues})
        answer_closed(g, closed_recently(g, apps, now))
        publish_factory_repos(o.WORK)
    names = sorted({*FACTORY_REPOS, *(r.split("/")[1] for r, _ in issues)})
    mirrors = {name: mirror_state(o.WORK / name) for name in names}
    body = render(g, now, socket.gethostname(), tick_s, beat_age, model_reachable(), views, steps, staged(), mirrors,
                  spend_today(now), recent_log(o.WORK))
    if not print_only:
        g.call("PATCH", f"/repos/{g.user}/factory/issues/{status_issue(g)}", {"body": body})
        try:
            write_board(g, now, views, steps, tick_s, beat_age, mirrors, spend_today(now), running)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001 - the board is a view; the status issue above is the fallback
            o.alarm(f"board-{type(e).__name__}", "the board page failed", f"{type(e).__name__}: {str(e)[:200]}")
    return body


BOARD_FILE = "board/index.html"


def board_cards(g: Gitea, views: list[dict], steps: dict, now: datetime) -> list[dict]:
    """Every open issue of the factory's app repos, plus the ones closed in the last week."""
    by_key = {(v["repo"], v["n"]): v for v in views}
    runs = o.jsonl(o.DATA / "agent_runs.jsonl")
    base = g.repo_url(f"{g.user}/factory").removesuffix(".git").rsplit("/", 2)[0]
    since = (now - timedelta(days=board.DONE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cards = []
    for repo in sorted(x["full_name"] for x in g.paged("/repos/search?q=factory-app&topic=true")):
        issues = g.paged(f"/repos/{repo}/issues?state=open&type=issues")
        issues += [i for i in g.paged(f"/repos/{repo}/issues?state=closed&type=issues&since={since}")
                   if (i.get("closed_at") or "") >= since]
        for i in issues:
            view = by_key.get((repo, i["number"]))
            if view is None and i.get("state") == "open" and any(
                    lab["name"].startswith("stage/") for lab in i.get("labels", [])):
                view = issue_view(g, repo, i["number"])  # an issue the foreman does not watch (shipped, parked)
            cards.append(board.card(i, repo, base, view, runs, now, steps))
    return cards


def board_health(now: datetime, tick_s: int | None, beat_age: float | None, views: list[dict],
                 mirrors: dict[str, str], spend: dict[str, float], holder: str | None) -> list[tuple[str, str, str]]:
    h: list[tuple[str, str, str]] = [("host", socket.gethostname(), "")]
    if holder:
        h.append(("lease", holder, "ok" if holder == socket.gethostname() else "warn"))
    if tick_s is not None:
        h.append(("foreman", f"busy {tick_s // 60} min", "ok"))
    elif beat_age is None or beat_age > 300:
        h.append(("foreman", f"no tick for {int((beat_age or 0) // 60)} min", "bad"))
    else:
        h.append(("foreman", f"idle, tick {int(beat_age)} s ago", "ok"))
    h.append(("local model", "reachable" if model_reachable() else "not reachable", "ok" if model_reachable() else "warn"))
    queued = sum(len(v["pending"]) for v in views)
    h.append(("commands waiting", str(queued), "warn" if queued else "ok"))
    behind = [n for n, st in mirrors.items() if not st.startswith("in sync")]
    h.append(("mirrors", "in sync" if not behind else ", ".join(behind) + " not in sync", "ok" if not behind else "warn"))
    h.append(("spend today", f"${sum(spend.values()):.2f}", ""))
    return h


def busy_text(tick_s: int | None, running: dict | None) -> str:
    if lanes.enabled():
        out = []
        for lane in ("local", "cloud"):
            recs = lanes.busy(o.STATE, lane)
            out.append(f"{lane} lane {len(recs)}/{lanes.capacity(lane)}: {lanes.describe(recs)}")
        return ("Each step runs in its own process, in a lane: " + "; ".join(out) + ". A command waits only when its "
                "lane is full, and is told so.")
    if tick_s is None:
        return ""
    what = f"{running['app']}#{running['n']} (`{running['step']}`)" if running else "a step"
    return (f"The foreman has been busy for {tick_s // 60} min with {what}. It runs one item at a time: commands on "
            "other items wait until that step ends (each gets an acknowledgement right away); a command on that same "
            "item is answered at its next checkpoint.")


def write_board(g: Gitea, now: datetime, views: list[dict], steps: dict, tick_s: int | None, beat_age: float | None,
                mirrors: dict[str, str], spend: dict[str, float], running: dict | None = None) -> None:
    holder = None
    if lease.enabled():
        try:
            holder = lease.Lease(g).read().get("host")
        except Exception:  # noqa: BLE001
            holder = None
    page = board.render(board_cards(g, views, steps, now), board_health(now, tick_s, beat_age, views, mirrors, spend,
                        holder), board.analytics(o.DATA, now), recent_log(o.WORK, 15), now, socket.gethostname(),
                        busy_text(tick_s, running))
    board.write(state_file(BOARD_FILE), page)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="render to stdout, change nothing")
    a = ap.parse_args()
    try:
        g = Gitea()
        if not a.print and lease.enabled():
            try:
                other = lease.Lease(g).elsewhere()
            except Unreachable:
                raise
            except Exception:  # noqa: BLE001 - no lease API: behave as before (one host)
                other = None
            if other:
                return  # the foreman runs on `other`; its status job writes the page
        body = run(g, print_only=a.print)
    except Unreachable as e:
        o.log(f"status: gitea unreachable: {str(e)[:120]}")
        return
    except Exception as e:
        o.alarm(f"status-{type(e).__name__}", "the status page failed", f"{type(e).__name__}: {str(e)[:200]}")
        raise
    if a.print:
        print(body)


if __name__ == "__main__":
    main()
