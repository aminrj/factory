#!/usr/bin/env bash
# Install the factory timers as systemd USER units: the foreman tick and the three quality timers. They need claude and
# git credentials, so they run here, not on the CI runner. The units live in this folder (the factory repo is the one home
# of the code and of the units) and are linked from here; they run %h/git/software-factory/factory, the runtime clone. Run
# only after the branch is merged and that clone is updated. Usage: ops/install-timers.sh [--uninstall]
set -euo pipefail
here="$(cd "$(dirname "$0")/systemd" && pwd)"
units=(factory-tick factory-status nightly-health lookback rework-digest factory-hygiene)
if [ "${1:-}" = "--uninstall" ]; then
  for u in "${units[@]}"; do systemctl --user disable --now "$u.timer" 2>/dev/null || true; done
  systemctl --user disable --now factory-board.service 2>/dev/null || true
  systemctl --user daemon-reload
  exit 0
fi
for u in "${units[@]}"; do
  systemd-analyze verify --user "$here/$u.service" "$here/$u.timer" || true
  systemctl --user link "$here/$u.service" "$here/$u.timer"
  systemctl --user enable --now "$u.timer"
done
# the /llm model switcher: only on the GPU workstation (host.env says FACTORY_LLM_SWITCH=on)
if grep -qs '^FACTORY_LLM_SWITCH=on' "$HOME/.config/factory/host.env"; then
  systemctl --user link "$here/factory-llm-switch.service" "$here/factory-llm-switch.timer"
  systemctl --user enable --now factory-llm-switch.timer
fi
# the board page (served, not timed): only where staging.env names the address to listen on
if [ -f "$HOME/.config/factory/staging.env" ]; then
  systemctl --user link "$here/factory-board.service"
  systemctl --user enable --now factory-board.service
fi
systemctl --user list-timers --no-pager | grep -E 'factory-tick|factory-status|nightly-health|lookback|rework-digest|factory-hygiene' || true
