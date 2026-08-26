"""Steam Web API client — CS2 lifetime stats + Premier support data.

Valve publishes NO matchmaking match-history API, and as of 2026 the
Web API keys created after Valve's restriction return 400 for
GetUserStatsForGame even on public profiles (verified against other
public accounts) — so lifetime stats via API are unavailable to us;
the same goes for the community stats/games pages, which now require
sign-in. The call below is kept anyway: it self-heals if Valve ever
re-allows it. Premier match history comes from GSI (see gsi.py).
"""
from __future__ import annotations

from typing import Optional

import requests

BASE = "https://api.steampowered.com"


class SteamError(Exception):
    pass


class SteamClient:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None):
        self.key = api_key
        self.s = session or requests.Session()

    def _get(self, path: str, **params) -> dict:
        params["key"] = self.key
        r = self.s.get(f"{BASE}{path}", params=params, timeout=15)
        if r.status_code == 403:
            raise SteamError("403 — bad key or private profile")
        if r.status_code == 429:
            raise SteamError("rate-limited (429)")
        r.raise_for_status()
        return r.json()

    def resolve_vanity(self, vanity: str) -> Optional[str]:
        """vanity URL name -> steamid64, or None."""
        try:
            d = self._get("/ISteamUser/ResolveVanityURL/v1",
                          vanityurl=vanity).get("response", {})
            return d.get("steamid") if d.get("success") == 1 else None
        except SteamError:
            return None

    def cs2_lifetime(self, steamid64: str) -> dict:
        """Lifetime competitive stats: wins, kills, deaths, MVPs, shots..."""
        d = self._get("/ISteamUserStats/GetUserStatsForGame/v2",
                      steamid=steamid64, appid=730)
        stats = d.get("playerstats", {}).get("stats", [])
        out = {}
        for s in stats:
            out[s["name"]] = s.get("value")
        return out

    def player_summary(self, steamid64: str) -> dict:
        d = self._get("/ISteamUser/GetPlayerSummaries/v2",
                      steamids=steamid64)
        pls = d.get("response", {}).get("players", [])
        return pls[0] if pls else {}
