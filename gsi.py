"""CS2 Game State Integration listener — live Premier match capture.

The GSI cfg on the gaming PC POSTs game state to /gsi while CS2 runs.
We watch map/phase transitions to detect match start and end, track the
round score and the player's K/D/A, and finalise a Premier match record
on 'gameover' or when the state stream goes quiet (quit to menu).

Side detection (CT vs T) reads player.team / allplayers when present;
if Valve's payload never tells us the side, the match is stored with
result 'unknown' rather than guessing.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from store import Match

QUIET_SECS = 45                      # no GSI posts this long => match over
CAPTURED_MODES = {"competitive", "premier", "wingman", ""}  # "" = not sent
SIDES = ("CT", "T")


class GsiTracker:
    def __init__(self, store, on_match=None):
        self.store = store
        self.on_match = on_match      # callback(match) on finalise
        self.lock = threading.Lock()
        self.cur: Optional[dict] = None
        self.last_post = 0.0

    # ---- ingest ---------------------------------------------------------
    def ingest(self, gs: dict) -> Optional[str]:
        """One GSI payload. Returns 'started' | 'ended' | None."""
        with self.lock:
            self.last_post = time.time()
            mapd = gs.get("map") or {}
            name = mapd.get("name") or ""
            if not name.startswith("de_"):
                return None                      # menu / lobby / no map yet

            mode = (mapd.get("mode") or "").lower()
            if mode not in CAPTURED_MODES:
                return None                      # casual/DM/custom etc.

            ev = None
            if self.cur is None:
                self.cur = self._new(name, mode, gs)
                ev = "started"
            elif self.cur["map"] != name:
                self._finalise()
                self.cur = self._new(name, mode, gs)
                ev = "started"

            self._update(mapd, gs)
            if mapd.get("phase") == "gameover":
                self._finalise()
                ev = "ended"
            return ev

    # ---- internals ---------------------------------------------------------
    def _new(self, name: str, mode: str, gs: dict) -> dict:
        steamid = (gs.get("provider") or {}).get("steamid", "")
        return {
            "match_id": f"premier-{name}-{int(time.time())}",
            "source": "premier", "ts": time.time(),
            "map": name, "mode": mode, "steamid": steamid,
            "ct": 0, "t": 0, "side": None,
            "kills": 0, "deaths": 0, "assists": 0, "mvp": 0,
        }

    def _update(self, mapd: dict, gs: dict):
        c = self.cur
        ct = (mapd.get("team_ct") or {}).get("score")
        t = (mapd.get("team_t") or {}).get("score")
        try:
            c["ct"], c["t"] = int(ct), int(t)
        except (TypeError, ValueError):
            pass

        player = gs.get("player") or {}
        ms = player.get("match_stats") or {}
        c["kills"] = int(ms.get("kills") or 0)
        c["deaths"] = int(ms.get("deaths") or 0)
        c["assists"] = int(ms.get("assists") or 0)
        c["mvp"] = int(ms.get("mvps") or 0)

        side = self._my_side(player, gs)
        if side in SIDES:
            c["side"] = side

    @staticmethod
    def _my_side(player: dict, gs: dict) -> Optional[str]:
        """CS2 GSI exposes the player side in a few shapes; try them all."""
        for candidate in (player.get("team"),
                          ((gs.get("allplayers") or {})
                           .get(player.get("steamid") or "", {})
                           .get("team"))):
            if isinstance(candidate, str) and candidate.upper() in SIDES:
                return candidate.upper()
            if isinstance(candidate, dict):
                v = candidate.get("side") or candidate.get("name")
                if isinstance(v, str) and v.upper() in SIDES:
                    return v.upper()
        return None

    def _finalise(self):
        if not self.cur:
            return
        c = self.cur
        self.cur = None
        ct, t = c["ct"], c["t"]
        if c["side"] == "CT":
            mine, enemy = ct, t
        elif c["side"] == "T":
            mine, enemy = t, ct
        else:
            mine, enemy = max(ct, t), min(ct, t)   # side never observed
        if c["side"]:
            result = "win" if mine > enemy else ("loss" if mine < enemy
                                                 else "draw")
        else:
            result = "unknown"
        m = Match(
            match_id=c["match_id"], source="premier", ts=c["ts"],
            map=c["map"], result=result,
            team_rounds=mine, enemy_rounds=enemy,
            kills=c["kills"], deaths=c["deaths"], assists=c["assists"],
        )
        try:
            self.store.upsert_match(m)
        except Exception:
            return
        if self.on_match:
            try:
                self.on_match(m)
            except Exception:
                pass

    # ---- maintenance ----------------------------------------------------
    def check_quiet(self) -> bool:
        """Finalise if the stream went silent mid-match (alt-F4 etc.)."""
        with self.lock:
            if self.cur and time.time() - self.last_post > QUIET_SECS:
                self._finalise()
                return True
            return False

    def finalize(self) -> bool:
        """Force-finalise (tests / shutdown)."""
        with self.lock:
            if self.cur:
                self._finalise()
                return True
            return False
