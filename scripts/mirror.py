#!/usr/bin/env python3
"""The only way to push to a public GitHub mirror. Publishes a snapshot of `main` and refuses when its files hold
internal names.

Usage:
  mirror.py check <repo>   scan every tracked file of `main` (what the next push would publish)
  mirror.py push  <repo>   check, then push one snapshot commit to `github main`

Snapshots, not history: the mirror gets one new commit per push, whose tree is `main`'s tree and whose parent is the
mirror's previous commit. The private history stays on Gitea. So a name that once reached a commit and was removed
later blocks nothing: the published files are what is checked, and fixing a file in a new commit unblocks the push.
(Pushing history made that impossible: brevkoll and factory were stuck 37 commits behind on 2026-11-21.)

What counts as internal (the repos are private on Gitea and only `main` is mirrored read-only):
  - private and tailnet IPv4 ranges (10/8, 172.16/12, 192.168/16, 100.64/10), except in tests/ fixtures
  - `*.ts.net` tailnet names and `*.lab.*` lab names
  - anything in ~/.config/factory/mirror-deny.txt (one regex per line, never tracked: put your own domain
    and host names there so this file does not leak them)
Matches are reported as file:line and the rule, never copied into a tracked file.
Links to the staging environment and to Gitea live only in Gitea comments, never in files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

DENY_FILE = Path(os.environ.get("FACTORY_MIRROR_DENY", Path.home() / ".config/factory/mirror-deny.txt"))
BUILTIN: dict[str, str] = {
    "private-ip": r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                  r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b",
    "tailnet-name": r"\.ts\.net\b",
    "lab-name": r"\.lab\.[a-z0-9-]+\.[a-z]{2,}\b",
}


def rules() -> dict[str, re.Pattern[str]]:
    out = {k: re.compile(v, re.I) for k, v in BUILTIN.items()}
    if DENY_FILE.exists():
        for i, line in enumerate(DENY_FILE.read_text().splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                out[f"deny-list:{i}"] = re.compile(line, re.I)
    return out


URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s)>\]\"'`]+", re.I)


def scrub(text: str) -> str:
    """Text the factory writes into a mirrored repo (thoughts/N/issue.md: the owner's comments, CI notes, job logs) with
    every private name taken out: a link that names one becomes `<internal link>`, any other hit `<internal name>`.
    brevkoll#29 (2026-11-22): the foreman's own CI note put the PR's internal URL into thoughts/29/issue.md, and the
    mirror refused `main` after the merge."""
    pats = list(rules().values())
    text = URL.sub(lambda m: "<internal link>" if any(p.search(m.group(0)) for p in pats) else m.group(0), text)
    for p in pats:
        text = p.sub("<internal name>", text)
    return text


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True).stdout


MIRROR_IDENTITY = {"GIT_AUTHOR_NAME": "factory-orchestrator", "GIT_AUTHOR_EMAIL": "factory-orchestrator@users.noreply.local",
                   "GIT_COMMITTER_NAME": "factory-orchestrator",
                   "GIT_COMMITTER_EMAIL": "factory-orchestrator@users.noreply.local"}
MIRROR_REF = "refs/remotes/github/main"


def tree_lines(repo: Path, rev: str = "main") -> list[tuple[str, int, str]]:
    """(path, line number, text) for every text line of every tracked file at `rev`: exactly what a snapshot publishes."""
    paths = [p for p in git(repo, "ls-tree", "-r", "-z", "--name-only", rev).split("\0") if p]
    batch = "".join(f"{rev}:{p}\n" for p in paths)
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=batch.encode(), capture_output=True,
                         check=True).stdout
    rows: list[tuple[str, int, str]] = []
    pos = 0
    for path in paths:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        blob = out[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1
        if size > 2_000_000 or b"\0" in blob[:8000]:
            continue  # large or binary
        try:
            rows += [(path, n, t) for n, t in enumerate(blob.decode().splitlines(), 1)]
        except UnicodeDecodeError:
            continue
    return rows


def snapshot(repo: Path, branch: str = "main") -> str | None:
    """The commit to push: `branch`'s tree on top of the mirror's last commit. None when the mirror has that tree."""
    tree = git(repo, "rev-parse", f"{branch}^{{tree}}").strip()
    parent = git(repo, "rev-parse", "--verify", "-q", MIRROR_REF, check=False).strip()
    if parent and git(repo, "rev-parse", f"{parent}^{{tree}}").strip() == tree:
        return None
    source = git(repo, "rev-parse", branch).strip()
    subject = git(repo, "log", "-1", "--format=%s", branch).strip()
    message = f"Mirror of {source[:12]}: {subject}\n\nA snapshot of main on the private server; its history stays there."
    args = ["commit-tree", tree, "-m", message, *(["-p", parent] if parent else [])]
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env={**os.environ, **MIRROR_IDENTITY}).stdout.strip()


TEST_PATH = re.compile(r"(^|/)tests?/")
SELF = re.compile(r"(^|/)(mirror\.py|test_mirror\.py)$")  # the guard describes the patterns it blocks


def check(repo: Path, rev: str = "main") -> list[str]:
    """Test fixtures may use private IPs (rate-limit tests); every other rule applies everywhere."""
    pats = rules()
    return [f"{p}:{n}  ({name})" for p, n, text in tree_lines(repo, rev)
            for name, rx in pats.items()
            if rx.search(text) and not SELF.search(p) and not (name == "private-ip" and TEST_PATH.search(p))]


def push(repo: Path, branch: str = "main") -> str:
    """Publish `branch` as one snapshot commit. Returns what happened, for the caller's log."""
    if repo.name == "factory-ops":
        raise SystemExit("REFUSED: factory-ops is private and is never mirrored.")
    subprocess.run(["git", "fetch", "-q", "github"], cwd=repo, check=False, capture_output=True)
    hits = check(repo, branch)
    if hits:
        raise SystemExit("REFUSED: the files on main hold internal names. Fix them in a new commit, then retry:\n  "
                         + "\n  ".join(hits))
    commit = snapshot(repo, branch)
    if commit is None:
        return "the mirror already has this tree"
    subprocess.run(["git", "push", "-q", "github", f"{commit}:refs/heads/main"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", MIRROR_REF, commit], cwd=repo, check=True)
    return f"pushed snapshot {commit[:12]}"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("check", "push"):
        print(__doc__)
        return 2
    repo = Path(argv[2]).resolve()
    if argv[1] == "push":
        print(f"{repo.name}: {push(repo)}")
        return 0
    hits = check(repo)
    print("\n".join(hits) if hits else "clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
