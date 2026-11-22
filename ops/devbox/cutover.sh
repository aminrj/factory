#!/usr/bin/env bash
# Move the foreman tick from this host (the GPU workstation) to the devbox VM, or back with --back. Run it on the GPU workstation.
#   cutover.sh <ssh target of the devbox>            move: stop here, copy state/, start there, wait for a tick there
#   cutover.sh <ssh target of the devbox> --back     roll back: stop there, copy state/ back, start here
# Two hosts must never tick at the same time (both would answer the same /go), so the old host always stops first and
# its running tick is allowed to finish. Staged builds stay where they run; `stage.py down <app>` removes one by hand.
set -euo pipefail
target="${1:?usage: cutover.sh <ssh target> [--back]}"
work="$HOME/git/software-factory"
timers=(factory-tick factory-status nightly-health lookback rework-digest)
base="$(git -C "$work/factory" remote get-url origin)"; base="${base%/factory.git}"

stop_here() {
  for t in "${timers[@]}"; do systemctl --user disable --now "$t.timer" 2>/dev/null || true; done
  while systemctl --user is-active -q factory-tick.service; do echo "waiting for the running tick to finish"; sleep 20; done
}
stop_there() {
  ssh "$target" 'for t in factory-tick factory-status nightly-health lookback rework-digest; do systemctl --user disable --now $t.timer 2>/dev/null || true; done
                 while systemctl --user is-active -q factory-tick.service; do sleep 20; done'
}
copy_state() {  # copy_state <from> <to>; the locks are per host and never copied
  rsync -a --exclude '*.lock' "$1" "$2"
}

if [ "${2:-}" = "--back" ]; then
  stop_there
  copy_state "$target:git/software-factory/state/" "$work/state/"
  bash "$work/factory/ops/install-timers.sh"
  echo "rolled back: the tick runs here again"
  exit 0
fi

boot="$work/factory/ops/devbox/bootstrap.sh"   # sent over stdin: the first run has no clone on the devbox yet
ssh "$target" bash -s -- "$base" < "$boot" \
  || { echo "bootstrap on the devbox failed: nothing was stopped here"; exit 1; }
stop_here
copy_state "$work/state/" "$target:git/software-factory/state/"
ssh "$target" bash -s -- "$base" --enable-tick < "$boot"
since="$(ssh "$target" date -Is)"
for _ in $(seq 1 18); do
  sleep 10
  if ssh "$target" "journalctl --user -u factory-tick.service --since '$since' --no-pager" | grep -q "Finished factory-tick.service"; then
    echo "moved: a tick finished on the devbox"
    exit 0
  fi
done
echo "no tick finished on the devbox within 3 minutes: rolling back"
"$0" "$target" --back
exit 1
