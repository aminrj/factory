#!/usr/bin/env python3
"""The factory's own Gitea identity, as code: one command, no clicking.

Why: the foreman and the owner shared one login (`amine`), told apart by comment prefixes. So Gitea could not say who
added a label, branch protection protected nothing, every host running agents held the owner's admin token, and every
audit line said `amine`. With its own user the factory posts, labels and pushes as `factory-bot`; only the owner's
comments are commands, only the owner's `night/ok` label is an approval, and only the owner merges.

What `apply` does (idempotent; run it again any time, it only fixes what differs):
1. the user `factory-bot` exists (admin API; not an admin, private, no password anyone knows);
2. it is a Write collaborator on factory, factory-ops, factory-template and every repo with the topic `factory-app`;
3. with --rotate, or when a host has no token yet: a new token (scopes: repository and issues write, user read) is made
   through a one-time random password that is thrown away, written to each host, and the older factory tokens are
   deleted;
4. on each host (`local` and/or `user@host` over ssh), mode 600 in ~/.config/factory/: `bot-token`,
   `bot-git-credentials`, and `identity.env`, which the foreman's systemd units load. It sets FACTORY_OWNER,
   GITEA_TOKEN_FILE and a git credential override (GIT_CONFIG_*) that applies to the foreman's processes only: the
   owner's own git on that machine is untouched.

What `protect` does, once the foreman runs as the bot (a tick has posted as `factory-bot`):
- every protected `main`: only the owner may merge; nobody pushes directly, except the bot on factory (metrics) and
  factory-ops (run log). Existing required status checks are kept.

Usage:
  identity.py plan    [--hosts local,ubuntu@devbox]     show what apply would change, change nothing
  identity.py apply   [--hosts ...] [--rotate]
  identity.py protect [--dry-run]
Run it with the owner's admin token (the default token file). Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gitea import Gitea, GiteaError  # noqa: E402

BOT = "factory-bot"
BOT_EMAIL = "factory-bot@users.noreply.local"
SCOPES = ["write:repository", "write:issue", "read:user"]
TOKEN_PREFIX = "factory-"
FACTORY_REPOS = ("factory", "factory-ops", "factory-template")
BOT_PUSHES_MAIN = {"factory": "metrics refresh commits", "factory-ops": "run log commits"}
APP_TOPIC = "factory-app"


def repos(g: Gitea) -> list[str]:
    apps = [r["name"] for r in g.paged(f"/repos/search?q={APP_TOPIC}&topic=true") if r["owner"]["login"] == g.user]
    return sorted({*FACTORY_REPOS, *apps})


# ---------------------------------------------------------------- the user, its rights, its token

def user_exists(g: Gitea) -> bool:
    return g.exists(f"/users/{BOT}")


def ensure_user(g: Gitea, dry: bool) -> list[str]:
    if user_exists(g):
        return []
    if not dry:
        g.call("POST", "/admin/users", {"username": BOT, "email": BOT_EMAIL, "password": secrets.token_urlsafe(32),
                                        "must_change_password": False, "visibility": "private",
                                        "full_name": "Software factory (foreman)"})
    return [f"create user {BOT}"]


def ensure_collaborators(g: Gitea, names: list[str], dry: bool) -> list[str]:
    out = []
    for name in names:
        path = f"/repos/{g.user}/{name}/collaborators/{BOT}"
        if user_exists(g) and g.exists(path):
            perm = g.get(f"/repos/{g.user}/{name}/collaborators/{BOT}/permission").get("permission")
            if perm in ("write", "admin"):
                continue
        out.append(f"{name}: {BOT} gets write")
        if not dry:
            g.call("PUT", path, {"permission": "write"})
    return out


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def new_token(g: Gitea) -> str:
    """A fresh token for the bot. Admins cannot mint tokens for others, so: a one-time random password, the token made
    with it (basic auth), then another random password nobody keeps."""
    once = secrets.token_urlsafe(32)
    g.call("PATCH", f"/admin/users/{BOT}", {"login_name": BOT, "source_id": 0, "password": once,
                                            "must_change_password": False})
    name = f"{TOKEN_PREFIX}{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    req = urllib.request.Request(f"{g.base}/api/v1/users/{BOT}/tokens", method="POST",
                                 data=json.dumps({"name": name, "scopes": SCOPES}).encode(),
                                 headers={"Authorization": basic(BOT, once), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - the Gitea URL from the config
            token = json.loads(r.read())["sha1"]
    except urllib.error.HTTPError as e:
        raise GiteaError(f"token for {BOT}: HTTP {e.code} {e.read()[:200]!r}") from e
    finally:
        g.call("PATCH", f"/admin/users/{BOT}", {"login_name": BOT, "source_id": 0,
                                                "password": secrets.token_urlsafe(32), "must_change_password": False})
    return token


def delete_old_tokens(g: Gitea, token: str) -> int:
    """Delete the bot's other factory tokens (needs the bot's own token)."""
    hdr = {"Authorization": f"token {token}"}

    def call(method: str, path: str) -> object:
        req = urllib.request.Request(f"{g.base}/api/v1{path}", method=method, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            raw = r.read()
            return json.loads(raw) if raw.strip() else None

    try:
        tokens = call("GET", f"/users/{BOT}/tokens")
    except urllib.error.HTTPError:
        return 0  # listing needs basic auth on some versions: the old tokens stay; harmless, they are listed in Gitea
    mine = sorted((t for t in tokens if t["name"].startswith(TOKEN_PREFIX)), key=lambda t: t["id"])  # type: ignore[union-attr]
    gone = 0
    for t in mine[:-1]:
        try:
            call("DELETE", f"/users/{BOT}/tokens/{t['id']}")
            gone += 1
        except urllib.error.HTTPError:
            pass
    return gone


# ---------------------------------------------------------------- the hosts

HOST_SCRIPT = r'''
set -eu
umask 077
read -r TOKEN
D="$HOME/.config/factory"
mkdir -p "$D"
printf '%s\n' "$TOKEN" > "$D/bot-token"
printf 'https://%s:%s@%s\n' "$BOT" "$TOKEN" "$HOSTNAME_GITEA" > "$D/bot-git-credentials"
cat > "$D/identity.env" <<EOF
# Written by scripts/identity.py: the foreman runs as $BOT; the owner's commands and merges stay the owner's.
FACTORY_OWNER=$OWNER
GITEA_TOKEN_FILE=$D/bot-token
GIT_CONFIG_COUNT=2
GIT_CONFIG_KEY_0=credential.$GITEA_URL.helper
GIT_CONFIG_VALUE_0=
GIT_CONFIG_KEY_1=credential.$GITEA_URL.helper
GIT_CONFIG_VALUE_1="store --file=$D/bot-git-credentials"
EOF
chmod 600 "$D/bot-token" "$D/bot-git-credentials" "$D/identity.env"
echo "written: $D/identity.env"
'''

CHECK_SCRIPT = r'''test -s "$HOME/.config/factory/bot-token" && test -s "$HOME/.config/factory/identity.env" && echo yes || echo no'''


def run_on(host: str, script: str, stdin: str = "", env: dict[str, str] | None = None) -> str:
    """Run a bash script locally or over ssh. The token travels on stdin, never on a command line."""
    assigns = " ".join(f"{k}={sh_quote(v)}" for k, v in (env or {}).items())
    line = f"{assigns} bash -c {sh_quote(script)}" if assigns else f"bash -c {sh_quote(script)}"
    cmd = ["bash", "-c", line] if host == "local" else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, line]
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{host}: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def host_has_token(host: str) -> bool:
    return run_on(host, CHECK_SCRIPT) == "yes"


def write_host(host: str, g: Gitea, token: str) -> str:
    gitea_host = g.base.split("://", 1)[1].rstrip("/")
    return run_on(host, HOST_SCRIPT, stdin=token + "\n",
                  env={"BOT": BOT, "OWNER": g.user, "GITEA_URL": g.base.rstrip("/"), "HOSTNAME_GITEA": gitea_host})


def token_works(g: Gitea, token: str) -> str | None:
    req = urllib.request.Request(f"{g.base}/api/v1/user", headers={"Authorization": f"token {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return json.loads(r.read())["login"]
    except urllib.error.HTTPError:
        return None


# ---------------------------------------------------------------- branch protection

def desired_protection(name: str, current: dict | None, owner: str) -> dict:
    p = {"rule_name": "main", "branch_name": "main", "enable_merge_whitelist": True, "merge_whitelist_usernames": [owner],
         "enable_push": name in BOT_PUSHES_MAIN, "enable_push_whitelist": name in BOT_PUSHES_MAIN,
         "push_whitelist_usernames": [BOT] if name in BOT_PUSHES_MAIN else [], "enable_force_push": False}
    if current:
        p["enable_status_check"] = current.get("enable_status_check", False)
        p["status_check_contexts"] = current.get("status_check_contexts", [])
    return p


def protect(g: Gitea, names: list[str], dry: bool) -> list[str]:
    out = []
    for name in names:
        base = f"/repos/{g.user}/{name}/branch_protections"
        current = next((r for r in g.get(base) if r.get("rule_name") in ("main", None) and
                        r.get("branch_name", "main") == "main"), None)
        want = desired_protection(name, current, g.user)
        if current and all(current.get(k) == v for k, v in want.items() if k not in ("rule_name", "branch_name")):
            continue
        out.append(f"{name}: protect main (merge: {g.user}; push: {', '.join(want['push_whitelist_usernames']) or 'nobody'})")
        if not dry:
            if current:
                g.call("PATCH", f"{base}/{current.get('rule_name') or 'main'}",
                       {k: v for k, v in want.items() if k not in ("rule_name", "branch_name")})
            else:
                g.call("POST", base, want)
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["plan", "apply", "protect"])
    ap.add_argument("--hosts", default="local", help="comma list: local and/or user@host (default: local)")
    ap.add_argument("--rotate", action="store_true", help="make a new token and replace it on every host")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    g = Gitea()
    if g.bot != g.user:
        raise SystemExit(f"run this with the owner's token (this token is {g.bot}).")
    if not g.get("/user").get("is_admin"):
        raise SystemExit("the owner's token must be an admin token (creating a user needs it).")
    names = repos(g)
    if a.action == "protect":
        for line in protect(g, names, a.dry_run) or ["main is already protected everywhere"]:
            print(line)
        return 0
    dry = a.action == "plan" or a.dry_run
    hosts = [h.strip() for h in a.hosts.split(",") if h.strip()]
    changes = ensure_user(g, dry) + ensure_collaborators(g, names, dry)
    need = hosts if a.rotate else [h for h in hosts if not host_has_token(h)]
    if need:
        changes.append(f"new token for {BOT}, written to: {', '.join(need)}")
    for line in changes or ["nothing to change"]:
        print(("would: " if dry else "done: ") + line)
    if dry or not need:
        return 0
    token = new_token(g)
    if token_works(g, token) != BOT:
        raise SystemExit("the new token does not work; nothing was written to the hosts")
    for h in need:
        print(f"{h}: {write_host(h, g, token)}")
    if a.rotate:
        print(f"deleted {delete_old_tokens(g, token)} older {BOT} token(s)")
    print("Next: the foreman's next tick posts as the bot. Then `identity.py protect`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
