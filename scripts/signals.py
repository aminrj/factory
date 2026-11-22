"""Messages from the owner to a step that is running now: `/stop` (end it at once) and `/steer <note>` (tell the agent).

2026-11-22: a local run on brevkoll#7 went the wrong way for 25 minutes (it tried to overwrite the acceptance tests with
git plumbing), and `/stop` was only heard between two steps. The status job (status.py, every minute) sees the owner's
command while the item runs and leaves a file here; dispatch.py watches for it every few seconds and acts:

  state/signals/<app>-<n>.stop          JSON {"comment": id}: end the agent now, keep its work on a refused/ branch
  state/signals/<app>-<n>.steer.jsonl   one JSON {"comment": id, "note": text} per line: given to the agent at once

Only the owner's comments become signals (status.py checks the login). A checkpoint clears both files, so a signal never
reaches a later step.
"""

from __future__ import annotations

import json
from pathlib import Path


def folder(state: Path) -> Path:
    return state / "signals"


def _stop(state: Path, app: str, n: int) -> Path:
    return folder(state) / f"{app}-{n}.stop"


def _steer(state: Path, app: str, n: int) -> Path:
    return folder(state) / f"{app}-{n}.steer.jsonl"


def request_stop(state: Path, app: str, n: int, comment: int) -> None:
    folder(state).mkdir(parents=True, exist_ok=True)
    _stop(state, app, n).write_text(json.dumps({"comment": comment}))


def stop_requested(state: Path, app: str, n: int) -> int | None:
    try:
        return int(json.loads(_stop(state, app, n).read_text())["comment"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def add_steer(state: Path, app: str, n: int, comment: int, note: str) -> None:
    folder(state).mkdir(parents=True, exist_ok=True)
    with _steer(state, app, n).open("a") as fh:
        fh.write(json.dumps({"comment": comment, "note": note}) + "\n")


def steers_waiting(state: Path, app: str, n: int) -> bool:
    p = _steer(state, app, n)
    return p.exists() and p.stat().st_size > 0


def take_steers(state: Path, app: str, n: int) -> list[dict]:
    """The waiting notes, oldest first; they are removed (each reaches the agent once)."""
    p = _steer(state, app, n)
    try:
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    p.unlink(missing_ok=True)
    return rows


def clear(state: Path, app: str, n: int) -> None:
    _stop(state, app, n).unlink(missing_ok=True)
    _steer(state, app, n).unlink(missing_ok=True)
