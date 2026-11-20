"""Shared helpers for the scheduled quality jobs (nightly health, lookback, rework digest).

Standard library only. Jobs run on the workstation (they need `claude` and git credentials).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gitea import Gitea

DATA = Path(__file__).resolve().parent.parent / "data"
APP_TOPIC = "factory-app"
ORCHESTRATOR = {"name": "factory-orchestrator", "email": "factory-orchestrator@users.noreply.local"}
TEMPLATE_REPO = "factory-template"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    with (DATA / name).open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(name: str) -> list[dict[str, Any]]:
    path = DATA / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def app_repos(g: Gitea, only: str | None = None) -> list[str]:
    """Names of the user's repos that carry the `factory-app` topic."""
    repos = g.paged(f"/repos/search?uid={g.get('/user')['id']}&limit=50")
    names = [r["name"] for r in repos if APP_TOPIC in (r.get("topics") or []) and r["owner"]["login"] == g.user]
    return sorted(n for n in names if only in (None, n))


@contextmanager
def read_only_clone(g: Gitea, repo: str, ref: str = "main") -> Iterator[Path]:
    """Shallow clone of `ref` into a temp dir. The remote is removed so nothing can be pushed from it."""
    with tempfile.TemporaryDirectory(prefix=f"quality-{repo}-") as tmp:
        dest = Path(tmp) / repo
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", ref, g.repo_url(f"{g.user}/{repo}"),
                        str(dest)], check=True, capture_output=True, env=env, timeout=300)
        subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=True, capture_output=True)
        yield dest


def sanitize(text: str, limit: int) -> str:
    """Untrusted text (agent output, PR bodies) before it goes into an issue or a data file."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")
    return text[:limit]
