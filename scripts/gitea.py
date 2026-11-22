"""Tiny Gitea API client for the factory scripts. Standard library only.

The token is read from ~/.config/git/gitea-token (the same file the git credential helper uses)
and is never printed. The server URL comes from GITEA_URL, or from the git credential config.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_FILE = Path(os.environ.get("GITEA_TOKEN_FILE", Path.home() / ".config/git/gitea-token"))
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "aminrj")


GITHUB_HOSTS = ("https://github.com", "https://gist.github.com")  # gh registers these helpers too (the devbox)


def url_from_credential_config(config: str) -> str | None:
    """The Gitea URL from `git config --get-regexp credential.*.helper` output: the helper that reads the token file,
    else the first https one that is not GitHub. On the devbox `gh auth setup-git` lists github.com first."""
    urls: list[tuple[str, str]] = []
    for line in config.splitlines():
        key, _, value = line.partition(" ")  # credential.<url>.helper <command>
        url = key[len("credential."):-len(".helper")].rstrip("/")
        if url.startswith("https://") and url not in GITHUB_HOSTS:
            urls.append((url, value))
    for url, value in urls:
        if TOKEN_FILE.name in value:
            return url
    return urls[0][0] if urls else None


def _server_url() -> str:
    url = os.environ.get("GITEA_URL")
    if url:
        return url.rstrip("/")
    out = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^credential\..*\.helper$"],
        capture_output=True, text=True, check=False,
    ).stdout
    found = url_from_credential_config(out)
    if found:
        return found
    raise SystemExit("Set GITEA_URL (no Gitea URL found in the git credential config).")


def is_owner_email(email: str, login: str, configured_email: str) -> bool:
    """Is this commit author the owner? Commits made in Gitea's web editor carry `<id>+<login>@noreply.<host>`, not the
    address in the owner's git config; both count. The factory identities (factory-agent, factory-orchestrator) never do."""
    return email == configured_email or re.fullmatch(rf"\d+\+{re.escape(login)}@noreply\..+", email, re.IGNORECASE) is not None


class GiteaError(RuntimeError):
    pass


class Unreachable(GiteaError):
    """The server did not answer in time, even after retries. Not a bad request: the network path is down or stalled."""


TIMEOUT = float(os.environ.get("GITEA_TIMEOUT", "15"))  # seconds per request; a healthy answer takes well under one
READ_TRIES = 3  # reads only: a write that timed out may have happened, so it is never repeated here
BACKOFF = (1.0, 3.0)
PAGE_SIZE = 50
MAX_PAGES = 100  # 5000 items; a loop that long is a bug, not data


class Gitea:
    def __init__(self) -> None:
        self.base = _server_url()
        self._token = TOKEN_FILE.read_text().strip()
        # Two logins (scripts/identity.py): the token is the factory's own (`factory-bot`), and FACTORY_OWNER names the
        # owner, whose repos these are and whose commands count. One login (no FACTORY_OWNER): both are the token's.
        self.bot: str = self.get("/user")["login"]
        self.user: str = os.environ.get("FACTORY_OWNER") or self.bot

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204),
             text: bool = False) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/api/v1{path}", data=data, method=method,
            headers={"Authorization": f"token {self._token}", "Content-Type": "application/json"},
        )
        tries = READ_TRIES if method == "GET" else 1
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                    raw = resp.read()
                    status = resp.status
                break
            except urllib.error.HTTPError as err:
                raw = err.read()
                status = err.code
                break
            except (urllib.error.URLError, socket.timeout, ConnectionError) as err:
                if attempt + 1 == tries:
                    raise Unreachable(f"{method} {path}: no answer after {tries} try(ies): {type(err).__name__}") from err
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
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
        """All items of a list endpoint. Some endpoints (an issue's comments) ignore `page` and `limit` and return
        everything every time, so a page with nothing new in it ends the loop; so does a short page and a hard cap.
        Without that, an issue with 50 or more comments made this loop forever (2026-11-21)."""
        sep = "&" if "?" in path else "?"
        items: list[Any] = []
        seen: set[Any] = set()
        for page in range(1, MAX_PAGES + 1):
            chunk = self.get(f"{path}{sep}limit={PAGE_SIZE}&page={page}")
            if isinstance(chunk, dict):  # search endpoints wrap results
                chunk = chunk.get("data", [])
            new = [x for x in chunk if not (isinstance(x, dict) and x.get("id") in seen)]
            seen.update(x["id"] for x in new if isinstance(x, dict) and "id" in x)
            items += new
            if len(chunk) < PAGE_SIZE or not new:
                break
        return items

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


# ---------------------------------------------------------------- who wrote a comment
# Fakes in the tests have only `user`; getattr keeps them working as one login.

def ours(g: Any, c: dict) -> bool:
    """Written by the owner or by the factory (the comments the foreman reads at all)."""
    return c["user"]["login"] in (g.user, getattr(g, "bot", g.user))


def by_owner(g: Any, c: dict) -> bool:
    """Written by the owner: only these can be commands. With one login the factory's comments are told apart by their
    prefix at the call site; with two logins a command from the factory's login is never obeyed."""
    return c["user"]["login"] == g.user


def label_added_by_owner(g: Any, repo: str, n: int, name: str) -> bool:
    """Did the owner add `name` (the last change to that label)? With one login Gitea cannot tell, so True."""
    if getattr(g, "bot", g.user) == g.user:
        return True
    last = None
    for e in g.paged(f"/repos/{repo}/issues/{n}/timeline"):
        if e.get("type") == "label" and (e.get("label") or {}).get("name") == name:
            last = e
    return bool(last and last.get("body") == "1" and (last.get("user") or {}).get("login") == g.user)

