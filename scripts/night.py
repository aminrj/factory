"""The night shift: the foreman moves labeled work forward on the local model while the owner sleeps, then stops for review.

The owner puts the label `night/ok` on an issue of a factory app. That label is the owner's standing approval for the
gates BEFORE the code (CP1 spec, CP2 threat model, CP2b research questions, CP3 plan) under this policy:

- Only inside the night window (FACTORY_NIGHT_WINDOW, default 22-07 local time; `always` for daytime runs too), only
  on the local model (never the cloud), one item per tick.
- An issue with the label and no checkpoint yet is picked up: an agent drafts the spec and the holdout tests (the `spec`
  step), the foreman posts Checkpoint 1, and the next tick passes it.
- A gate passed this way gets a commit by factory-orchestrator with the trailer `Approved-by-policy: night/ok`. The
  guards accept such an approval only while the label is on the issue. An agent can never approve anything.
- The policy refuses to pass a plan that touches risky areas (deploy, CI workflows, auth, secrets, TLS, dependencies):
  that item stops at a "needs you" checkpoint.
- A failed step is retried within the normal budget (2). A step blocked because the model was busy or loading is tried
  again after 20 minutes, at most 3 times. Anything else (a NeedsHuman checkpoint, retries used up) waits for the owner.
- It NEVER passes Checkpoint 4: the pull request and the staged build wait for the owner, who reads the diff, sees what
  the policy approved (listed on the checkpoint) and merges, or sends `/rework`. It never merges and never deploys.
- At the end of the window one push summarises the night.

Remove the label to stop the night shift for that issue; `/stop` works as always. FACTORY_NIGHT=off turns it off.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LABEL = "night/ok"
TRAILER = "Approved-by-policy: night/ok"
PRE_CODE = {"pick_up", "after_gate1", "after_gate2", "after_research_questions", "after_gate3"}
RETRY_CLASSES = ("model_unavailable", "rate_limit")
BLOCKED_WAIT_MIN = 20
BLOCKED_MAX = 3
RISKY = re.compile(r"(^|[\s`/(])(deploy/|\.gitea/|\.github/|Dockerfile|docker-compose|pyproject\.toml|uv\.lock|"
                   r"requirements[\w.-]*\.txt|auth\w*|secret\w*|password\w*|credential\w*|tls\b|mtls\b|certificate\w*|"
                   r"token\w*|crypt\w*)", re.IGNORECASE)
CP_LABEL = re.compile(r"^🛑 CHECKPOINT\s+(\S+?):")
CLASS = re.compile(r"\[class: (\w+)\]")


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("FACTORY_TZ", "Europe/Stockholm"))


def window() -> tuple[int, int] | None:
    """(start hour, end hour) local, or None for `always`."""
    raw = os.environ.get("FACTORY_NIGHT_WINDOW", "22-07").strip().lower()
    if raw == "always":
        return None
    a, _, b = raw.partition("-")
    return int(a), int(b)


def enabled() -> bool:
    return os.environ.get("FACTORY_NIGHT", "on") != "off"  # off: no night shift, no label, no morning push


def in_window(now: datetime) -> bool:
    if not enabled():
        return False
    w = window()
    if w is None:
        return True
    h = now.astimezone(tz()).hour
    start, end = w
    return start <= h or h < end if start > end else start <= h < end


def window_ended_today(now: datetime) -> datetime | None:
    """The moment today's window ended, if it has (for the morning summary)."""
    w = window()
    if w is None:
        return None
    local = now.astimezone(tz())
    end = local.replace(hour=w[1], minute=0, second=0, microsecond=0)
    return end if local >= end else None


def checkpoint_label(body: str) -> str:
    m = CP_LABEL.match(body.strip())
    return m.group(1) if m else ""


def may_pass(pivot: dict, marker: dict[str, str], labels: set[str], recent: list[dict], now: datetime) -> str | None:
    """Why the night shift may answer this checkpoint itself, or None. `recent`: the foreman's comments, oldest first."""
    if LABEL not in labels or not pivot["body"].startswith("🛑"):
        return None
    resume, label = marker.get("resume", ""), checkpoint_label(pivot["body"])
    if resume not in PRE_CODE:
        return None  # Checkpoint 4, after a merge, manual: always the owner's
    if label.endswith("-fix") or label.endswith("-failed"):
        return None
    if label.endswith("-blocked"):
        cls = CLASS.search(pivot["body"])
        if not cls or cls.group(1) not in RETRY_CLASSES:
            return None
        created = datetime.fromisoformat(pivot["created_at"].replace("Z", "+00:00"))
        if now - created < timedelta(minutes=BLOCKED_WAIT_MIN):
            return None
        streak = 0
        for c in reversed(recent):
            lab = checkpoint_label(c["body"])
            if not lab:
                continue
            if not lab.endswith("-blocked"):
                break
            streak += 1
        return f"the model was {cls.group(1).replace('_', ' ')}; trying again" if streak <= BLOCKED_MAX else None
    if label.endswith("-retry"):
        return "a step failed; retrying within the budget"
    return {"pick_up": "drafting the spec again", "after_gate1": "the spec and holdout tests", "after_gate2": "the threat model",
            "after_research_questions": "the research questions (unanswered ones are deferred by the planner)",
            "after_gate3": "the plan"}[resume]


def risky_mentions(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for m in RISKY.finditer(text):
        word = m.group(2)
        if word.lower() not in (s.lower() for s in seen):
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def risky_files(files: list[str]) -> list[str]:
    return [f for f in files if RISKY.search(" " + f)]


# ---------------------------------------------------------------- the night's record and the morning push

def events_path(state: Path) -> Path:
    return state / "night-events.jsonl"


def record(state: Path, now: datetime, item: str, what: str, result: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    with events_path(state).open("a") as fh:
        fh.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "item": item, "what": what, "result": result}) + "\n")


def summary_due(state: Path, now: datetime) -> list[dict] | None:
    """The events of the night that just ended, once per day; None when nothing is due."""
    end = window_ended_today(now)
    if end is None:
        return None
    marker = state / "night-summary.txt"
    day = end.date().isoformat()
    if marker.exists() and marker.read_text().strip() == day:
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(day)
    p = events_path(state)
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    since = (end - timedelta(hours=24)).isoformat(timespec="seconds")
    return [r for r in rows if r["ts"] >= since]


def summary_text(rows: list[dict]) -> str:
    if not rows:
        return "Nothing was labeled night/ok, or nothing could move."
    items: dict[str, list[str]] = {}
    for r in rows:
        items.setdefault(r["item"], []).append(f"{r['what']}: {r['result']}")
    return "\n".join(f"{k}: " + "; ".join(v[-3:]) for k, v in items.items())
