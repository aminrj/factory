#!/usr/bin/env python3
"""The only way to push to a public GitHub mirror. Refuses when the commits to be pushed add internal names.

Usage:
  mirror.py check <repo> [--all]   scan the lines the next push would add (or every tracked file with --all)
  mirror.py push  <repo>           check, then `git push github main`

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


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True).stdout


def added_lines(repo: Path, whole_tree: bool) -> list[tuple[str, int, str]]:
    """(path, line number, text) for every line the next push adds. Falls back to the whole tree for a new mirror."""
    has_base = subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/remotes/github/main"], cwd=repo,
                              capture_output=True).returncode == 0
    if whole_tree or not has_base:
        rows = []
        for path in git(repo, "ls-files", "-z").split("\0"):
            f = repo / path
            if not path or not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            try:
                rows += [(path, n, t) for n, t in enumerate(f.read_text().splitlines(), 1)]
            except (UnicodeDecodeError, OSError):
                continue
        return rows
    diff = git(repo, "diff", "--unified=0", "--no-color", "github/main...HEAD")
    rows, path, line = [], "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
        elif m := re.match(r"@@ -\S+ \+(\d+)", raw):
            line = int(m.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            rows.append((path, line, raw[1:]))
            line += 1
    return rows


TEST_PATH = re.compile(r"(^|/)tests?/")
SELF = re.compile(r"(^|/)(mirror\.py|test_mirror\.py)$")  # the guard describes the patterns it blocks


def check(repo: Path, whole_tree: bool = False) -> list[str]:
    """Test fixtures may use private IPs (rate-limit tests); every other rule applies everywhere."""
    pats = rules()
    return [f"{p}:{n}  ({name})" for p, n, text in added_lines(repo, whole_tree)
            for name, rx in pats.items()
            if rx.search(text) and not SELF.search(p) and not (name == "private-ip" and TEST_PATH.search(p))]


def push(repo: Path, branch: str = "main") -> None:
    if repo.name == "factory-ops":
        raise SystemExit("REFUSED: factory-ops is private and is never mirrored.")
    subprocess.run(["git", "fetch", "-q", "github"], cwd=repo, check=False, capture_output=True)
    hits = check(repo)
    if hits:
        raise SystemExit("REFUSED: the push would publish internal names. Fix them in a new commit, then retry:\n  "
                         + "\n  ".join(hits))
    subprocess.run(["git", "push", "-q", "github", branch], cwd=repo, check=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("check", "push"):
        print(__doc__)
        return 2
    repo = Path(argv[2]).resolve()
    if argv[1] == "push":
        push(repo)
        print(f"pushed {repo.name} main to github")
        return 0
    hits = check(repo, "--all" in argv)
    print("\n".join(hits) if hits else "clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
