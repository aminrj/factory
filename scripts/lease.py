"""Who may run the foreman now: a lease in Gitea, so a standby host takes over when the primary goes dark, and hands back.

2026-11-21: the devbox (primary) went down with its Proxmox node for 13 hours and nothing ran or said so. A second host
(the GPU workstation, which is on whenever the local model is) now runs the same timers as a standby. Exactly one host
acts at a time; the lease decides which.

The lease is one Gitea Actions variable, FOREMAN_LEASE, in the factory-ops repo: {"host", "role", "until"}. A tick
renews it every LEASE_S/3 from a background thread for as long as it runs, so a 40-minute build keeps it.

- primary: takes the lease when it is free, expired or its own. When the standby holds a live lease, the primary waits
  (it does not grab: the standby may be in the middle of a step) and marks itself seen (FOREMAN_PRIMARY_SEEN).
- standby: takes the lease only when it has been expired for TAKEOVER_S more (the primary missed ~5 minutes of
  renewals). Holding it, it hands it back between ticks once the primary has been seen again.

Both write, then read back: when two hosts write at once, the one that reads its own name acts.
Config: FACTORY_LEASE=off (one host, no lease), FACTORY_ROLE=primary|standby (default primary), FACTORY_HOST (default: the host name), from
~/.config/factory/host.env through the systemd units.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any

REPO = "factory-ops"  # FACTORY_LEASE_REPO overrides it; with the factory's own Gitea user: factory-bot/foreman-state
NAME = "FOREMAN_LEASE"
SEEN = "FOREMAN_PRIMARY_SEEN"
LEASE_S = 180
TAKEOVER_S = 120
SEEN_FRESH_S = 180


def enabled() -> bool:
    return os.environ.get("FACTORY_LEASE", "on") != "off"  # off: one host, as before the standby existed


def role() -> str:
    return os.environ.get("FACTORY_ROLE", "primary")


def host() -> str:
    return os.environ.get("FACTORY_HOST") or socket.gethostname()


class Lease:
    def __init__(self, g: Any, me: str | None = None, my_role: str | None = None, clock: Any = time.time) -> None:
        self.g, self.me, self.role, self.clock = g, me or host(), my_role or role(), clock
        repo = os.environ.get("FACTORY_LEASE_REPO", REPO)  # "owner/name", or a name in the owner's account
        self.path = f"/repos/{repo if '/' in repo else g.user + '/' + repo}/actions/variables"
        self._stop = threading.Event()

    # -- storage
    def read(self, name: str = NAME) -> dict[str, Any]:
        try:
            raw = self.g.call("GET", f"{self.path}/{name}", ok=(200,))
        except Exception as e:  # noqa: BLE001 - a missing variable is a 404
            if "404" in str(e):
                return {}
            raise
        try:
            return json.loads(raw.get("data") or "{}")
        except (json.JSONDecodeError, AttributeError):
            return {}

    def write(self, value: dict[str, Any], name: str = NAME) -> None:
        body = {"value": json.dumps(value, sort_keys=True)}
        try:
            self.g.call("PUT", f"{self.path}/{name}", body, ok=(200, 201, 204))
        except Exception as e:  # noqa: BLE001 - not created yet
            if "404" not in str(e):
                raise
            self.g.call("POST", f"{self.path}/{name}", body, ok=(200, 201, 204))

    # -- decisions
    def claim(self) -> tuple[bool, str]:
        """(may act, why). Never raises for a lease held elsewhere; Gitea errors propagate (the tick is skipped)."""
        now = self.clock()
        cur = self.read()
        holder, until = cur.get("host"), float(cur.get("until") or 0)
        mine = holder == self.me
        if self.role == "primary":
            if not mine and holder and cur.get("role") == "standby" and until > now:
                self.write({"host": self.me, "at": now}, SEEN)
                return False, f"the standby {holder} holds the lease; it hands back between ticks"
        else:
            if mine:
                seen = self.read(SEEN)
                if float(seen.get("at") or 0) > now - SEEN_FRESH_S:
                    self.write({"host": self.me, "role": self.role, "until": 0})
                    return False, f"handed the lease back to the primary {seen.get('host')}"
            elif not holder:
                return False, "no lease yet: the primary creates it on its first tick"
            elif until + TAKEOVER_S > now:
                return False, f"the primary {holder} is alive (lease until {time.strftime('%H:%M:%S', time.gmtime(until))} UTC)"
        self.write({"host": self.me, "role": self.role, "until": now + LEASE_S})
        back = self.read()
        if back.get("host") != self.me:
            return False, f"{back.get('host')} took the lease at the same moment"
        took = not mine and holder is not None and holder != self.me
        return True, (f"took over from {holder}" if took and self.role == "standby" else "holding the lease")

    def elsewhere(self) -> str | None:
        """Read-only: the other host that holds a live lease, else None. The status job runs only where the foreman
        does; a standby without the lease never acts (it returns its own name here when nobody holds it)."""
        cur = self.read()
        live = float(cur.get("until") or 0) > self.clock()
        if live and cur.get("host") != self.me:
            return str(cur.get("host"))
        if not live and self.role == "standby":
            return str(cur.get("host") or "the primary")
        return None

    def keepalive(self, every_s: float = LEASE_S / 3) -> None:
        """Renew from a daemon thread while this process runs (a long step must not lose the lease)."""
        def loop() -> None:
            while not self._stop.wait(every_s):
                try:
                    self.write({"host": self.me, "role": self.role, "until": self.clock() + LEASE_S})
                except Exception:  # noqa: BLE001, S110 - the next tick claims again
                    pass
        threading.Thread(target=loop, daemon=True, name="lease").start()

    def stop(self) -> None:
        self._stop.set()
