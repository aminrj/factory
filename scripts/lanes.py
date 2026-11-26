"""Work lanes: the foreman hands each step to its own process, so the tick never waits for a step.

2026-11-22: a 22-minute step on the local model held the tick, so a `/go model=fast` on another item waited although
the cloud was idle, and nothing could be tried while something built. Now the tick only decides: when an item has a
command to run and its lane has room, the tick starts `orchestrate.py run <app> <n>` as its own process (a transient
systemd user unit, so it outlives the tick) and goes back to watching. The child takes the item's lock and runs the
handler exactly as the tick used to.

Lanes and their capacity (how many items run at once):
  local  FACTORY_LANE_LOCAL, default 1: one GPU, one model, one step at a time. Every `/go` without `model=`, every
         night-shift step and every pick-up lands here: the owner's cloud tokens are spent only when asked for.
  cloud  FACTORY_LANE_CLOUD, default 3: `model=smart|fast|cheap|grok`.
A command whose lane is full waits (the status job tells the owner which item holds the lane) and starts when a slot
frees. FACTORY_LANES=off runs every step inside the tick, as before.

The record of a running item is state/lanes/<app>-<n>.json; the child removes it when it ends, and a record whose
process is gone is dropped on the next look.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PASS_ENV = ("FACTORY_", "GIT_CONFIG", "GITEA_", "LOCAL_MODEL_")
PASS_KEYS = ("WORK", "PATH", "HOME", "LANG", "TZ")


def enabled() -> bool:
    return os.environ.get("FACTORY_LANES", "on") != "off" and not os.environ.get("FACTORY_CHILD")


def capacity(lane: str) -> int:
    return int(os.environ.get(f"FACTORY_LANE_{lane.upper()}", {"local": "1", "cloud": "3"}.get(lane, "1")))


def lane_of(model: str | None) -> str:
    return "local" if model in (None, "", "local") else "cloud"


def folder(state: Path) -> Path:
    return state / "lanes"


def alive(rec: dict) -> bool:
    pid = int(rec.get("pid") or 0)
    if pid > 0:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    unit = rec.get("unit")
    if unit and shutil.which("systemctl"):
        res = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, check=False)
        return res.stdout.strip() in ("active", "activating")
    return time.time() - float(rec.get("started") or 0) < 120  # just started, no pid yet


def running(state: Path) -> list[dict]:
    """Records of the items running now; records of dead processes are removed."""
    out = []
    d = folder(state)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if alive(rec):
            out.append(rec)
        else:
            f.unlink(missing_ok=True)
    return out


def busy(state: Path, lane: str) -> list[dict]:
    return [r for r in running(state) if r.get("lane") == lane]


def has_room(state: Path, lane: str) -> bool:
    return len(busy(state, lane)) < capacity(lane)


def item_running(state: Path, app: str, n: int) -> bool:
    return any(r.get("app") == app and int(r.get("n", 0)) == n for r in running(state))


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.startswith(PASS_ENV) or k in PASS_KEYS}
    env["FACTORY_CHILD"] = "1"
    return env


def spawn(state: Path, app: str, n: int, lane: str, args: list[str], what: str, script: Path) -> dict:
    """Start `orchestrate.py <args>` for this item in its own process and record it in the lane."""
    folder(state).mkdir(parents=True, exist_ok=True)
    unit = f"factory-{app}-{n}-{int(time.time())}"
    argv = [sys.executable, "-I", str(script), *args]
    env = child_env()
    rec = {"app": app, "n": n, "lane": lane, "what": what, "unit": unit, "pid": 0, "started": time.time()}
    if shutil.which("systemd-run") and os.environ.get("FACTORY_SPAWN") != "popen":
        cmd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
               f"--working-directory={env.get('WORK', os.getcwd())}", *[f"--setenv={k}={v}" for k, v in env.items()],
               *argv]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        pid = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True, check=False).stdout.strip()
        rec["pid"] = int(pid) if pid.isdigit() else 0
    else:
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{unit}.log").open("w") as out:
            proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env={**os.environ, **env},
                                    start_new_session=True)
        rec["pid"] = proc.pid
    (folder(state) / f"{app}-{n}.json").write_text(json.dumps(rec))
    return rec


def finished(state: Path, app: str, n: int) -> None:
    (folder(state) / f"{app}-{n}.json").unlink(missing_ok=True)


def describe(recs: list[dict]) -> str:
    now = time.time()
    return ", ".join(f"{r['app']}#{r['n']} ({r.get('what', 'a step')}, {int((now - float(r['started'])) // 60)} min)"
                     for r in recs) or "nothing"
