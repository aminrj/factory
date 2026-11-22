#!/usr/bin/env python3
"""Switch the loaded local model from the phone: `/llm <target>` on the pinned "Factory status" issue.

Runs every minute on the GPU workstation (factory-llm-switch.timer; only where FACTORY_LLM_SWITCH=on). It obeys only
the owner's comments, answers each one at once, and switches only through the owner's own `llm` script, which stops
every model server and waits for the GPU memory to be free before it starts the next one. Nothing here can load a
model on top of another: there is one way to load, and it unloads first. One switch at a time (a lock).

  /llm status        what is loaded now (the output of `llm status`)
  /llm code          Qwen3.6-35B-A3B (fast, the factory's local model for now)
  /llm 38            Qwen3.8-27B (slower, stronger)
  /llm agent         stop everything and let the arbiter bring back the assistant's model (it waits 10 quiet minutes)
  /llm off           stop everything
  add `now` to switch while a local step runs (that step fails and is tried again later)

Without `now`, a switch waits until the local lane is empty (the foreman publishes it in the FOREMAN_LANES variable)
and the foreman starts no new local step while a switch is pending (FOREMAN_LLM_SWITCH). It gives up after 3 hours.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lease  # noqa: E402
from gitea import Gitea, by_owner  # noqa: E402

CMD = re.compile(r"^\s*/llm\s+(status|code|38|agent|off)(\s+now)?\s*$", re.IGNORECASE | re.MULTILINE)
ANY_LLM = re.compile(r"^\s*/llm\b(.*)$", re.IGNORECASE | re.MULTILINE)
TARGETS = {"code": ["code"], "38": ["38"], "off": ["off"], "agent": ["off", "agent"], "status": ["status"]}
GIVE_UP_S = 3 * 3600
NOTICE = "ℹ️"
STATE = Path(os.environ.get("WORK", Path.home() / "git" / "software-factory")) / "state"
LLM = os.environ.get("FACTORY_LLM_CMD", str(Path.home() / "git" / "local-llms" / "local-llm-ops" / "scripts" / "llm.sh"))
TITLE = "Factory status"


def enabled() -> bool:
    return os.environ.get("FACTORY_LLM_SWITCH", "off") == "on"


def status_issue(g: Gitea) -> int | None:
    found = [i for i in g.paged(f"/repos/{g.user}/factory/issues?state=open&type=issues") if i["title"] == TITLE]
    return found[0]["number"] if found else None


def lanes_report(ls: lease.Lease) -> dict:
    """The lanes as the foreman host last published them (every minute, from its status job)."""
    try:
        return ls.read("FOREMAN_LANES")
    except Exception:  # noqa: BLE001
        return {}


def local_busy(ls: lease.Lease) -> list[str]:
    return list(lanes_report(ls).get("local") or [])


def safe_to_switch(ls: lease.Lease, since: float, now: float) -> tuple[bool, str]:
    """Only on a report published after the request (from then on the foreman starts no new local step), and only when
    the local lane is empty. A foreman that has not reported for 5 minutes is taken as down: nothing to wait for."""
    rep = lanes_report(ls)
    at = float(rep.get("at") or 0)
    if now - at > 300:
        return True, "the foreman has not reported for 5 minutes"
    if at < since:
        return False, "waiting for the foreman's next report"
    busy = list(rep.get("local") or [])
    return (not busy), (f"waiting for {', '.join(busy)}" if busy else "the local lane is empty")


def run_llm(args: list[str], timeout: int = 900) -> tuple[int, str]:
    out = []
    code = 0
    for a in args:
        proc = subprocess.run(["bash", LLM, a], capture_output=True, text=True, timeout=timeout, check=False)
        out.append((proc.stdout + proc.stderr).strip())
        code = proc.returncode
        if code != 0:
            break
    if args == ["status"]:
        return code, out[0][-2500:]
    status = subprocess.run(["bash", LLM, "status"], capture_output=True, text=True, timeout=60, check=False)
    return code, "\n".join([*out, "", "$ llm status", status.stdout.strip()]).strip()[-2500:]


def tick(g: Gitea, now: float | None = None) -> str:
    now = now or time.time()
    n = status_issue(g)
    if n is None:
        return "no status issue"
    ls = lease.Lease(g)
    state_path = STATE / "llm-switch.json"
    st = json.loads(state_path.read_text()) if state_path.exists() else {}
    seen = int(st.get("seen", 0))
    comments = g.paged(f"/repos/{g.user}/factory/issues/{n}/comments")
    if not st:  # first run: only commands from now on
        st["seen"] = max([c["id"] for c in comments], default=0)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(st))
        return "started"
    for c in comments:
        if c["id"] <= seen or not by_owner(g, c) or c["body"].startswith(NOTICE):
            continue
        st["seen"] = c["id"]
        if m := CMD.search(c["body"]):
            target, force = m.group(1).lower(), bool(m.group(2))
            if target == "status":
                _, text = run_llm(["status"])
                g.comment(f"{g.user}/factory", n, f"{NOTICE} `/llm status` (comment {c['id']}):\n\n```\n{text}\n```")
                continue
            st["pending"] = {"target": target, "force": force, "comment": c["id"], "since": now}
            ls.write({"target": target, "since": now}, "FOREMAN_LLM_SWITCH")
            busy = local_busy(ls)
            g.comment(f"{g.user}/factory", n, f"{NOTICE} Received `/llm {target}{' now' if force else ''}` (comment "
                      f"{c['id']}). " + (f"A local step runs ({', '.join(busy)}): I switch when it ends and start no "
                                         "new local step meanwhile. `/llm " + target + " now` switches at once."
                                         if busy and not force else "Switching now; this takes 1 to 3 minutes."))
        elif ANY_LLM.search(c["body"]):
            g.comment(f"{g.user}/factory", n, f"{NOTICE} Not understood (comment {c['id']}): use `/llm status`, "
                      "`/llm code`, `/llm 38`, `/llm agent` or `/llm off`, optionally followed by `now`.")
    state_path.write_text(json.dumps(st))
    p = st.get("pending")
    if not p:
        return "idle"
    if now - float(p["since"]) > GIVE_UP_S:
        st.pop("pending")
        state_path.write_text(json.dumps(st))
        ls.write({}, "FOREMAN_LLM_SWITCH")
        g.comment(f"{g.user}/factory", n, f"{NOTICE} Gave up `/llm {p['target']}` (comment {p['comment']}): the local "
                  "lane stayed busy for 3 hours. Send it again with `now` to switch anyway.")
        return "gave up"
    ok, why = safe_to_switch(ls, float(p["since"]), now)
    if not ok and not p["force"]:
        return why
    with (STATE / "llm-switch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one switch at a time, whatever happens
        code, text = run_llm(TARGETS[p["target"]])
    st.pop("pending")
    state_path.write_text(json.dumps(st))
    ls.write({}, "FOREMAN_LLM_SWITCH")
    g.comment(f"{g.user}/factory", n, f"{NOTICE} {'✅ Switched' if code == 0 else '❌ Switch failed'}: `/llm "
              f"{p['target']}` (comment {p['comment']}).\n\n```\n{text}\n```")
    return f"switched to {p['target']}" if code == 0 else "failed"


def main() -> None:
    if not enabled():
        return
    try:
        print(tick(Gitea()))
    except Exception as e:  # noqa: BLE001 - the next minute tries again; the journal keeps the reason
        print(f"llm switch: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
