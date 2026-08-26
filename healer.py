"""Self-healing supervisor for the CS2 tracker.

Runs as a daemon thread inside the app process and continuously fixes
the failure modes this service can actually hit:

  * stuck GSI match        -> finalise (CS2 crashed without 'gameover')
  * dead refresher thread  -> restart it, alert once
  * wedged HTTP loop       -> self-check via localhost; after 3 misses
                              stop pinging the systemd watchdog, so
                              systemd restarts the whole process
  * stale upstream data    -> alert (ntfy) if no successful refresh in 2h
  * corrupted store file   -> hourly .bak snapshots + load-time fallback

systemd integration (Type=notify, WatchdogSec): the healer is the only
thing that pings WATCHDOG=1, and only when it considers the app healthy
— so a hung Flask loop or a wedged thread leads to a clean restart.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger("cs2tracker.heal")

GSI_STUCK_SECS = 6 * 3600       # match object older than this -> finalise
STALE_REFRESH_SECS = 2 * 3600   # no successful refresh this long -> alert
ALERT_EVERY = 6 * 3600          # per-key alert rate limit
HTTP_FAIL_LIMIT = 3             # consecutive self-check misses -> stop ping
BACKUP_EVERY = 3600             # store snapshot cadence


def sd_notify(msg: str) -> bool:
    """Minimal sd_notify(3) over the NOTIFY_SOCKET (stdlib only)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
        return True
    except OSError:
        return False


class SelfHealer(threading.Thread):
    def __init__(self, port: int, refresher_starter: Callable[[], None],
                 refresher_probe: Callable[[], bool], gsi, store,
                 notify: Optional[Callable[[str, str], None]] = None,
                 store_path: Optional[str] = None):
        super().__init__(daemon=True, name="cs2tracker-healer")
        self.port = port
        self.start_refresher = refresher_starter
        self.refresher_alive = refresher_probe
        self.gsi = gsi
        self.store = store
        self.notify = notify or (lambda title, body: None)
        self.store_path = store_path or getattr(store, "path", None)

        self.http_failures = 0
        self.last_alert: dict[str, float] = {}
        self.last_backup = 0.0
        self.started = time.time()
        self.watchdog_on = True
        self._ready_sent = False

    # ---- main loop -------------------------------------------------------
    def run(self):
        while True:
            try:
                self.cycle()
            except Exception as e:            # never let the healer die
                log.warning("healer cycle error: %s", e)
            time.sleep(30)

    def cycle(self):
        if not self._ready_sent:
            sd_notify("READY=1")
            self._ready_sent = True

        healed = []

        # 1. GSI: silent match finalisation + stuck match force-finalise
        try:
            if self.gsi.check_quiet():
                healed.append("gsi quiet-finalised")
            cur = getattr(self.gsi, "cur", None)
            if cur and time.time() - cur.get("ts", 0) > GSI_STUCK_SECS:
                self.gsi.finalize()
                healed.append("gsi stuck-finalised")
        except Exception as e:
            log.warning("gsi heal failed: %s", e)

        # 2. refresher thread liveness
        try:
            if not self.refresher_alive():
                self.start_refresher()
                healed.append("refresher restarted")
                self.alert("refresher",
                           "CS2 tracker: refresher restarted",
                           "background refresh thread had died")
        except Exception as e:
            log.warning("refresher heal failed: %s", e)

        # 3. self-HTTP check (end-to-end: Flask loop actually serving)
        ok = False
        try:
            r = requests.get(f"http://127.0.0.1:{self.port}/healthz",
                             timeout=8)
            ok = r.status_code == 200
        except requests.RequestException:
            ok = False
        if ok:
            self.http_failures = 0
        else:
            self.http_failures += 1
            log.warning("self-check failed (%d/%d)",
                        self.http_failures, HTTP_FAIL_LIMIT)
            if self.http_failures >= HTTP_FAIL_LIMIT:
                # stop pinging -> systemd watchdog fires -> full restart
                self.watchdog_on = False
                log.error("HTTP self-check failing; "
                          "relinquishing watchdog for systemd restart")
                return

        # 4. stale upstream data (key configured but no success in 2h)
        try:
            last = getattr(self.store.meta("__dummy__"), "x", None)
            last_ok = self.store.meta("last_refresh_success") or 0
            key = os.path.exists(
                os.path.join(os.path.dirname(self.store_path or "."),
                             "..", "secrets", "faceit_key.txt"))
            if key and last_ok and time.time() - last_ok > STALE_REFRESH_SECS:
                self.alert("stale",
                           "CS2 tracker: data is stale",
                           f"no successful FACEIT refresh since "
                           f"{time.strftime('%H:%M', time.localtime(last_ok))}"
                           f" — check API key / quota")
            elif key and not last_ok and time.time() - self.started > 3600:
                self.alert("stale",
                           "CS2 tracker: never refreshed",
                           "faceit key present but no successful refresh "
                           "within the first hour")
        except Exception as e:
            log.debug("stale check skipped: %s", e)

        # 5. hourly store backup
        if (self.store_path and os.path.exists(self.store_path)
                and time.time() - self.last_backup > BACKUP_EVERY):
            try:
                shutil.copy2(self.store_path, self.store_path + ".bak")
                self.last_backup = time.time()
            except OSError as e:
                log.warning("backup failed: %s", e)

        if healed:
            log.info("healed: %s", ", ".join(healed))

        # 6. watchdog ping — only while we consider ourselves healthy
        if self.watchdog_on:
            sd_notify("WATCHDOG=1")

    # ---- helpers -----------------------------------------------------------
    def alert(self, key: str, title: str, body: str):
        now = time.time()
        if now - self.last_alert.get(key, 0) < ALERT_EVERY:
            return
        self.last_alert[key] = now
        log.warning("%s: %s", title, body)
        try:
            self.notify(title, body)
        except Exception:
            pass

    def vitals(self) -> dict:
        cur = getattr(self.gsi, "cur", None)
        return {
            "healthy": self.http_failures < HTTP_FAIL_LIMIT
                       and self.refresher_alive(),
            "http_failures": self.http_failures,
            "watchdog": self.watchdog_on,
            "refresher_alive": self.refresher_alive(),
            "gsi_live_match": bool(cur),
            "gsi_stuck": bool(cur and time.time() - cur.get("ts", 0)
                              > GSI_STUCK_SECS),
            "last_refresh_success": self.store.meta("last_refresh_success"),
            "uptime_secs": round(time.time() - self.started),
        }
