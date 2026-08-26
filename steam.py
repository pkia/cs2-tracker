"""Steam Web API client — CS2 lifetime stats + Premier support data.

Valve publishes NO matchmaking match-history API, and as of 2026 the
Web API keys created after Valve's restriction return 400 for
GetUserStatsForGame even on public profiles (verified against other
public accounts) — so lifetime stats via key alone are unavailable;
the community stats/games pages likewise require sign-in for everyone.
What still works — the same way third-party stats sites do it — is the
owner's own logged-in session: `secrets/steam_cookie.txt` holds his
steamLoginSecure cookie, and authed_lifetime() uses it to call the API
as him, falling back to parsing his stats page. Premier match history
comes from GSI (see gsi.py).
"""
from __future__ import annotations

import re
from typing import Optional

import requests

BASE = "https://api.steampowered.com"
COMMUNITY = "https://steamcommunity.com"


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


# ---- owner-session path (secrets/steam_cookie.txt) ------------------------

_STAT_ROW = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>([^<]+?)</td>\s*<td[^>]*>([\d,.:]+\s*[hms%]?)</td>",
    re.I)


def parse_stat_rows(html: str) -> dict:
    """Two-column stat tables on the community stats page -> dict."""
    out = {}
    for label, value in _STAT_ROW.findall(html):
        key = label.strip()
        if key:
            out[key] = value.strip()
    return out


def _session(cookie: str) -> requests.Session:
    cookie = cookie.removeprefix("steamLoginSecure=").strip()
    s = requests.Session()
    s.headers["Cookie"] = f"steamLoginSecure={cookie}"
    s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) cs2-tracker"
    return s


def authed_lifetime(steamid64: str, cookie: str, api_key: str) -> dict:
    """Lifetime stats using the owner's logged-in session. The web API
    refuses post-restriction keys on their own but serves them when the
    request also carries the owner's steamLoginSecure cookie (verified
    live) — key + cookie is the primary path; his community stats page
    is the fallback. Raises SteamError when neither yields stats."""
    s = _session(cookie)
    try:
        r = s.get(f"{BASE}/ISteamUserStats/GetUserStatsForGame/v2",
                  params={"key": api_key, "steamid": steamid64,
                          "appid": 730}, timeout=15)
        if r.ok:
            stats = r.json().get("playerstats", {}).get("stats", [])
            if stats:
                return {x["name"]: x.get("value") for x in stats}
    except (requests.RequestException, ValueError):
        pass
    r = s.get(f"{COMMUNITY}/profiles/{steamid64}/stats/CS2",
              params={"tab": "stats"}, timeout=20)
    r.raise_for_status()
    out = parse_stat_rows(r.text)
    if not out:
        raise SteamError("stats page had no parseable rows (cookie expired?)")
    return out
