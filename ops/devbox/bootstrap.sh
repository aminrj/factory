#!/usr/bin/env bash
# Prepare the devbox VM to run the foreman. Run it ON the devbox; safe to run again. It never copies a secret: it checks
# that each one is in place and prints exactly what is missing. Usage:
#   bootstrap.sh <gitea base URL, e.g. https://git.example/owner> [--enable-tick]
# Without --enable-tick the timers are linked but the foreman tick stays off (ops/devbox/cutover.sh turns it on, after
# it turned it off on the old host: two hosts must never tick at the same time).
set -euo pipefail
base="${1:?usage: bootstrap.sh <gitea base URL> [--enable-tick]}"; base="${base%/}"
enable_tick="${2:-}"
host="${base%/*}"                       # https://git.example
work="$HOME/git/software-factory"
cfg="$HOME/.config/factory"
missing=0
need() {  # need <file> <what to do>
  if [ ! -s "$1" ]; then echo "MISSING $1 -> $2"; missing=1; return; fi
  if [ "$(stat -c %a "$1")" != "600" ]; then chmod 600 "$1"; echo "fixed   $1 mode 600"; fi
  echo "ok      $1"
}
need "$HOME/.config/git/gitea-token" "create a Gitea token (scopes: repository read/write, issue read/write) and save it here"
need "$cfg/ntfy.env"                  "copy ~/.config/factory/ntfy.env from the GPU workstation (NTFY_URL, NTFY_TOPIC, NTFY_TOKEN)"
need "$cfg/staging.env"               "write it as in ops/devbox/README.md: the gateway URL, LLM_MODEL, STAGING_HOST, STAGING_BIND_IP"
need "$cfg/local-model.env"           "names the mTLS route and the client files in $cfg/llm/"
need "$cfg/mirror-deny.txt"           "copy it from the GPU workstation (extra names the mirror guard blocks)"
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo "MISSING uv -> curl -LsSf https://astral.sh/uv/install.sh | sh"; missing=1; }
command -v claude >/dev/null || { echo "MISSING claude -> install Claude Code and log in"; missing=1; }
# The VM has no Tailscale of its own: the tailnet reaches it through OPNsense's subnet route, so a staged build listens
# on the VM's own address (STAGING_BIND_IP), not on a 100.x address.
grep -q '^STAGING_BIND_IP=' "$cfg/staging.env" 2>/dev/null && echo "ok      STAGING_BIND_IP set" \
  || echo "WARN    STAGING_BIND_IP is not in staging.env: staging will fail (no tailnet address on this VM)"
# GitHub goes through gh and its fine-grained token (homelab-foundation devbox playbook); the mirrored repos must be in
# that token's repository list.
command -v gh >/dev/null && gh auth status --hostname github.com >/dev/null 2>&1 && echo "ok      gh logged in" \
  || echo "WARN    gh is not logged in: mirror pushes to GitHub will fail (make devbox-setup after the token step)"
[ "$missing" = 0 ] || { echo "Fix the MISSING lines, then run this again."; exit 1; }

# user services must run without a login session
[ "$(loginctl show-user "$USER" -p Linger --value)" = yes ] || sudo loginctl enable-linger "$USER"
# git reads the token from its file at use time (same helper as the GPU workstation)
git config --global "credential.$host.helper" \
  '!f() { test "$1" = get && echo username='"${base##*/}"' && echo "password=$(cat ~/.config/git/gitea-token)"; }; f'
[ -d "$work/.git" ] || git clone -q "$base/factory-ops.git" "$work"   # WORK itself: RUNLOG.md, state/ (ignored)
for r in factory brevkoll factory-template; do
  [ -d "$work/$r/.git" ] || git clone -q "$base/$r.git" "$work/$r"
  git -C "$work/$r" remote get-url github >/dev/null 2>&1 || git -C "$work/$r" remote add github "https://github.com/${GITHUB_OWNER:-aminrj}/$r.git"
done
git -C "$work/factory" fetch -q origin && git -C "$work/factory" merge -q --ff-only origin/main
mkdir -p "$work/state"

units="$work/factory/ops/systemd"
systemctl --user link "$units/factory-model-gateway.service"
systemctl --user enable --now factory-model-gateway.service
if [ "$enable_tick" = "--enable-tick" ]; then
  bash "$work/factory/ops/install-timers.sh"
else
  echo "tick not enabled (run ops/devbox/cutover.sh from the old host)"
fi
python3 -I "$work/factory/scripts/doctor.py" || true
