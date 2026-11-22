#!/usr/bin/env bash
# Wake the host that serves the local model (Wake-on-LAN through the router's API). model_shim.py runs this once when
# no local model answers and the arbiter does not answer either (LOCAL_MODEL_WAKE_CMD in local-model.env).
#
# Config: ~/.config/factory/wake.env (mode 600), all required:
#   WAKE_API_URL   the router's Wake-on-LAN API endpoint (OPNsense: https://<router>/api/wol/wol/set)
#   WAKE_KEY       API key of a user whose only privilege is Wake on LAN
#   WAKE_SECRET    its secret
#   WAKE_IF        the router interface to send the packet on
#   WAKE_MAC       the host's MAC address
# Exit 0 when the router accepted the request.
set -euo pipefail
CONF="${WAKE_ENV:-$HOME/.config/factory/wake.env}"
[ -r "$CONF" ] || { echo "no $CONF: waking is not set up (ops/devbox/README.md, Wake)"; exit 2; }
set -a; . "$CONF"; set +a
for v in WAKE_API_URL WAKE_KEY WAKE_SECRET WAKE_IF WAKE_MAC; do
  [ -n "${!v:-}" ] || { echo "$v is empty in $CONF"; exit 2; }
done
AUTH=$(mktemp); trap 'rm -f "$AUTH"' EXIT; chmod 600 "$AUTH"
printf 'user = "%s:%s"\n' "$WAKE_KEY" "$WAKE_SECRET" > "$AUTH"   # never on the command line
out=$(curl -sk --max-time 15 -K "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"wake\":{\"interface\":\"$WAKE_IF\",\"mac\":\"$WAKE_MAC\"}}" "$WAKE_API_URL") || { echo "router unreachable"; exit 1; }
case "$out" in
  *'"status":"error"'*|"") echo "router refused: ${out:-no answer}"; exit 1 ;;
  *) echo "magic packet sent" ;;
esac
