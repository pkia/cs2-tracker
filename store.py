"""Unified CS2 match model + persistent JSON store.

One Match dataclass shape for both sources; Store handles atomic
persistence, merge-by-match_id, and summary stats.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Match:
    match_id: str
    source: str                 # 'faceit' | 'premier'
    ts: float                   # unix seconds
    map: str
    result: str                 # 'win' | 'loss' | 'draw'
    team_rounds: int = 0
    enemy_rounds: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    hs_pct: Optional[int] = None
    adr: Optional[float] = None
    elo: Optional[int] = None          # faceit elo after match
    elo_delta: Optional[int] = None
    premier_rating: Optional[int] = None  # CS Rating at match time (GSI-captured)
    friends: list = field(default_factory=list)   # names of party members

    @property
    def kd(self) -> Optional[float]:
        return round(self.kills / self.deaths, 2) if self.deaths else (
            float(self.kills) if self.kills else None)


class Store:
    """Thread-safe JSON store: matches + lifetime stats + meta."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self._matches: dict[str, Match] = {}
        self._lifetime: dict = {}
        self._meta: dict = {"faceit_elo": None, "premier_rating": None,
                            "updated": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    raw = json.load(f)
                self._matches = {m["match_id"]: Match(**m)
                                 for m in raw.get("matches", [])}
                self._lifetime = raw.get("lifetime", {})
                self._meta.update(raw.get("meta", {}))
            except (json.JSONDecodeError, TypeError, KeyError):
                # corrupt store: start fresh rather than crash the app
                self._matches, self._lifetime = {}, {}

    # ---- persistence -------------------------------------------------
    def _flush(self):
        with self.lock:
            d = {"matches": [asdict(m) for m in self._matches.values()],
                 "lifetime": self._lifetime, "meta": self._meta}
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(d, f, separators=(",", ":"))
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # ---- matches ------------------------------------------------------
    def upsert_match(self, m: Match):
        with self.lock:
            old = self._matches.get(m.match_id)
            self._matches[m.match_id] = m
            if old is None:
                self._flush()
                return True
            # keep richer data on conflicts (e.g. GSI capture then faceit backfill)
            changed = asdict(old) != asdict(m)
            if changed:
                self._flush()
            return changed

    def matches(self, source: Optional[str] = None, limit: int = 50,
                since: Optional[float] = None) -> list[Match]:
        with self.lock:
            ms = list(self._matches.values())
        if source:
            ms = [m for m in ms if m.source == source]
        if since:
            ms = [m for m in ms if m.ts >= since]
        ms.sort(key=lambda m: m.ts, reverse=True)
        return ms[:limit]

    # ---- lifetime / meta ----------------------------------------------
    def set_lifetime(self, provider: str, stats: dict):
        with self.lock:
            self._lifetime[provider] = {"stats": stats,
                                        "updated": time.time()}
            self._flush()

    def lifetime(self, provider: str) -> Optional[dict]:
        with self.lock:
            return self._lifetime.get(provider)

    def set_meta(self, **kw):
        with self.lock:
            self._meta.update(kw)
            self._flush()

    def meta(self, key: str):
        with self.lock:
            return self._meta.get(key)


def summarize(ms: list[Match]) -> dict:
    """Aggregate stats over a list of matches."""
    if not ms:
        return {"games": 0}
    wins = sum(1 for m in ms if m.result == "win")
    kills = sum(m.kills for m in ms)
    deaths = sum(m.deaths for m in ms)
    assists = sum(m.assists for m in ms)
    hs = [m.hs_pct for m in ms if m.hs_pct is not None]
    return {
        "games": len(ms),
        "wins": wins,
        "losses": sum(1 for m in ms if m.result == "loss"),
        "winrate": round(100 * wins / len(ms), 1),
        "kills": kills, "deaths": deaths, "assists": assists,
        "kd": round(kills / deaths, 2) if deaths else kills,
        "hs_pct": round(sum(hs) / len(hs)) if hs else None,
        "maps": len({m.map for m in ms}),
    }
