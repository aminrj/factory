"""Tiny Gitea API client for the factory scripts. Standard library only.

The token is read from ~/.config/git/gitea-token (the same file the git credential helper uses)
and is never printed. The server URL comes from GITEA_URL, or from the git credential config.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_FILE = Path(os.environ.get("GITEA_TOKEN_FILE", Path.home() / ".config/git/gitea-token"))
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "aminrj")


def _server_url() -> str:
    url = os.environ.get("GITEA_URL")
    if url:
        return url.rstrip("/")
    out = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^credential\..*\.helper$"],
        capture_output=True, text=True, check=False,
    ).stdout
    for line in out.splitlines():
        key = line.split()[0]  # credential.<url>.helper
        url = key[len("credential."):-len(".helper")]
        if url.startswith("https://"):
            return url.rstrip("/")
    raise SystemExit("Set GITEA_URL (no Gitea URL found in the git credential config).")


def is_owner_email(email: str, login: str, configured_email: str) -> bool:
    """Is this commit author the owner? Commits made in Gitea's web editor carry `<id>+<login>@noreply.<host>`, not the
    address in the owner's git config; both count. The factory identities (factory-agent, factory-orchestrator) never do."""
    return email == configured_email or re.fullmatch(rf"\d+\+{re.escape(login)}@noreply\..+", email, re.IGNORECASE) is not None


class GiteaError(RuntimeError):
    pass


class Gitea:
    def __init__(self) -> None:
        self.base = _server_url()
        self._token = TOKEN_FILE.read_text().strip()
        self.user: str = self.get("/user")["login"]

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204),
             text: bool = False) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/api/v1{path}", data=data, method=method,
            headers={"Authorization": f"token {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as err:
            raw = err.read()
            status = err.code
        if status not in ok:
            raise GiteaError(f"{method} {path} -> {status}: {raw[:300].decode(errors='replace')}")
        if text:
            return raw.decode(errors="replace")
        return json.loads(raw) if raw.strip() else None

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def exists(self, path: str) -> bool:
        try:
            self.call("GET", path)
            return True
        except GiteaError:
            return False

    def paged(self, path: str) -> list[Any]:
        sep = "&" if "?" in path else "?"
        items: list[Any] = []
        page = 1
        while True:
            chunk = self.get(f"{path}{sep}limit=50&page={page}")
            if isinstance(chunk, dict):  # search endpoints wrap results
                chunk = chunk.get("data", [])
            items += chunk
            if len(chunk) < 50:
                return items
            page += 1

    def repo_url(self, repo: str) -> str:
        return f"{self.base}/{repo}.git"

    # --- helpers shared by the scripts -------------------------------------------------
    def ensure_label(self, repo: str, name: str, color: str, desc: str = "") -> None:
        have = {label["name"] for label in self.paged(f"/repos/{repo}/labels")}
        if name not in have:
            self.call("POST", f"/repos/{repo}/labels", {"name": name, "color": color, "description": desc})

    def label_id(self, repo: str, name: str) -> int | None:
        for label in self.paged(f"/repos/{repo}/labels"):
            if label["name"] == name:
                return label["id"]
        return None

    def set_stage(self, repo: str, issue: int, stage: str) -> None:
        """Make `stage/<stage>` the only stage label on the issue."""
        labels = self.get(f"/repos/{repo}/issues/{issue}/labels")
        for label in labels:
            if label["name"].startswith("stage/") and label["name"] != f"stage/{stage}":
                self.call("DELETE", f"/repos/{repo}/issues/{issue}/labels/{label['id']}")
        if not any(label["name"] == f"stage/{stage}" for label in labels):
            lid = self.label_id(repo, f"stage/{stage}")
            self.call("POST", f"/repos/{repo}/issues/{issue}/labels", {"labels": [lid]})

    def comment(self, repo: str, issue: int, body: str) -> None:
        self.call("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": body})


STAGES = {
    "idea": ("0E8A16", "Idea with a named user"),
    "spec-ready": ("1D76DB", "Gate 1 done"),
    "building": ("0052CC", "In the factory"),
    "shipped": ("5319E7", "A real user has it"),
    "killed": ("B60205", "Killed at the timebox or kill date"),
}
