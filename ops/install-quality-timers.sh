#!/usr/bin/env bash
# Install the three quality timers as systemd USER units (they need claude and git credentials, so they run here,
# not on the CI runner). The units use %h/git/software-factory/factory (the runtime clone). Run only after the
# branch is merged and that clone is updated. Usage: ops/install-quality-timers.sh [--uninstall]
set -euo pipefail
here="$(cd "$(dirname "$0")/systemd" && pwd)"
units=(nightly-health lookback rework-digest)
if [ "${1:-}" = "--uninstall" ]; then
  for u in "${units[@]}"; do systemctl --user disable --now "$u.timer" 2>/dev/null || true; done
  systemctl --user daemon-reload
  exit 0
fi
for u in "${units[@]}"; do
  systemd-analyze verify --user "$here/$u.service" "$here/$u.timer" || true
  systemctl --user link "$here/$u.service" "$here/$u.timer"
  systemctl --user enable --now "$u.timer"
done
systemctl --user list-timers --no-pager | grep -E 'nightly-health|lookback|rework-digest' || true
