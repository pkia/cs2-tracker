"""FACEIT Data API v4 client — match history, match stats, player elo.

Docs: https://docs.faceit.com/docs/data-api/data/
Auth: Bearer <api key>. Free tier is rate-limited; we cache aggressively
and never call the API from request handlers (background refresh only).
"""
from __future__ import annotations

from typing import Optional

import requests

BASE = "https://open.faceit.com/data/v4"


class FaceitError(Exception):
    pass


class FaceitClient:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None):
        self.key = api_key
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {api_key}"

    # ---- low level ----------------------------------------------------
    def _get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        r = self.s.get(f"{BASE}{path}", params=params, timeout=15)
        if r.status_code == 429:
            raise FaceitError("rate-limited (429) — backing off")
        if r.status_code == 404:
            raise FaceitError(f"not found: {path}")
        r.raise_for_status()
        return r.json()

    # ---- endpoints -------------------------------------------------------
    def player(self, nickname: str) -> dict:
        return self._get("/players", nickname=nickname)

    def player_history(self, player_id: str, game="cs2", limit=20,
                       offset: int = 0) -> list[dict]:
        data = self._get(f"/players/{player_id}/history",
                         game=game, limit=limit, offset=offset)
        return data.get("items", [])

    def match_stats(self, match_id: str) -> dict:
        return self._get(f"/matches/{match_id}/stats")

    def match(self, match_id: str) -> dict:
        return self._get(f"/matches/{match_id}")


# ---- payload -> store dict ---------------------------------------------

def _int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _pct(v) -> Optional[int]:
    try:
        return int(round(float(str(v).strip())))
    except (TypeError, ValueError):
        return None


def _f(v) -> Optional[float]:
    try:
        return round(float(str(v).strip()), 1)
    except (TypeError, ValueError):
        return None


def find_my_faction(entry: dict, my_player_id: str) -> Optional[str]:
    """Return 'faction1'/'faction2' if I'm on a team roster.

    History payloads put players under teams.<faction>.players; the
    match detail endpoint uses .roster. Accept both.
    """
    teams = entry.get("teams") or {}
    for faction_id, team in teams.items():
        roster = (team.get("players") or []) + (team.get("roster") or [])
        if any(p.get("player_id") == my_player_id for p in roster):
            return faction_id
    return None


def _score_of(score, faction: str) -> int:
    """Score values are flat ints in history items, nested dicts in
    match details."""
    v = score.get(faction) if isinstance(score, dict) else None
    if isinstance(v, dict):
        v = v.get("score")
    return _int(v)


def to_match(entry: dict, my_player_id: str,
             stats_payload: Optional[dict] = None) -> Optional[dict]:
    """FACEIT history entry (+ optional /stats payload) -> store dict.

    The stats payload is authoritative for map/rounds/K-D-A when present;
    the history entry alone still yields result + rounds.
    """
    my_faction = find_my_faction(entry, my_player_id)
    if my_faction is None:
        return None

    voting = entry.get("voting") or {}
    mapinfo = voting.get("map") or {}
    m = {
        "match_id": f"faceit-{entry.get('match_id')}",
        "source": "faceit",
        "ts": entry.get("started_at", 0),
        "map": mapinfo.get("name") or mapinfo.get("map_name") or "",
        "kills": 0, "deaths": 0, "assists": 0,
        "hs_pct": None, "adr": None,
        "elo": None, "elo_delta": None,
        "premier_rating": None, "friends": [],
    }

    # result + rounds from the history entry (flat scores in history)
    score = (entry.get("results") or {}).get("score") or {}
    other = "faction2" if my_faction == "faction1" else "faction1"
    t_r = _score_of(score, my_faction)
    e_r = _score_of(score, other)
    winner = (entry.get("results") or {}).get("winner")
    if winner:
        result = "win" if winner == my_faction else "loss"
    else:
        result = "win" if t_r > e_r else "loss" if t_r < e_r else "draw"

    # stats payload: authoritative detail
    if stats_payload:
        try:
            rd = stats_payload["rounds"][0]
            rs = rd.get("round_stats") or {}
            # keys are capitalised in production: Map / Score
            for k in ("Map", "map"):
                if rs.get(k):
                    m["map"] = rs[k]
                    break
            # Score is "teams[0] / teams[1]" — orient once we know my team
            score_parts = None
            for k in ("Score", "score"):
                if "/" in str(rs.get(k, "")):
                    score_parts = [_int(x) for x in str(rs[k]).split("/")[:2]]
                    break
            my_team_idx = None
            for idx, team in enumerate(rd.get("teams", [])):
                for p in team.get("players") or []:
                    if p.get("player_id") == my_player_id:
                        ps = p.get("player_stats") or {}
                        m["kills"] = _int(ps.get("Kills"))
                        m["deaths"] = _int(ps.get("Deaths"))
                        m["assists"] = _int(ps.get("Assists"))
                        m["hs_pct"] = _pct(ps.get("Headshots %"))
                        m["adr"] = _f(ps.get("ADR"))
                        my_team_idx = idx
                        break
                if my_team_idx is not None:
                    break
            if score_parts and my_team_idx is not None:
                a, b = score_parts
                t_r, e_r = (a, b) if my_team_idx == 0 else (b, a)
        except (KeyError, IndexError, AttributeError, ValueError):
            pass

    m["team_rounds"], m["enemy_rounds"] = t_r, e_r
    if result == "draw" and t_r != e_r:
        result = "win" if t_r > e_r else "loss"
    m["result"] = result
    return m
