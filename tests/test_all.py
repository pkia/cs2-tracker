"""Offline tests: store, faceit parsing, GSI state machine, API smoke."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import Match, Store, summarize                       # noqa: E402
from faceit import to_match, find_my_faction                    # noqa: E402
from gsi import GsiTracker                                      # noqa: E402

import app as app_mod                                           # noqa: E402


# ---------------------------------------------------------------- store

def test_store_roundtrip(tmp_path):
    s = Store(str(tmp_path / "t.json"))
    s.upsert_match(Match(match_id="x1", source="faceit", ts=100, map="de_mirage",
                         result="win", team_rounds=13, enemy_rounds=7,
                         kills=20, deaths=14, assists=3))
    s2 = Store(str(tmp_path / "t.json"))
    assert len(s2.matches()) == 1
    assert s2.matches()[0].kd == pytest.approx(1.43)


def test_store_upsert_same_id_no_dup(tmp_path):
    s = Store(str(tmp_path / "t.json"))
    s.upsert_match(Match(match_id="x1", source="faceit", ts=1, map="de_nuke",
                         result="win"))
    s.upsert_match(Match(match_id="x1", source="faceit", ts=1, map="de_nuke",
                         result="win"))
    assert len(s.matches()) == 1


def test_summarize():
    ms = [
        Match(match_id="a", source="faceit", ts=1, map="de_mirage",
              result="win", kills=20, deaths=10, assists=2, hs_pct=50),
        Match(match_id="b", source="faceit", ts=2, map="de_inferno",
              result="loss", kills=10, deaths=20, assists=1, hs_pct=40),
    ]
    s = summarize(ms)
    assert s["games"] == 2 and s["wins"] == 1
    assert s["kd"] == pytest.approx(1.0)
    assert s["hs_pct"] == 45


# ---------------------------------------------------------------- faceit parsing

def entry(win=True, my_pid="p1"):
    return {
        "match_id": "m-1",
        "started_at": 1756000000,
        "voting": {},
        "teams": {
            "faction1": {"team_id": "t1", "players": [
                {"player_id": my_pid}]},
            "faction2": {"team_id": "t2", "players": [
                {"player_id": "p2"}]},
        },
        "results": {"winner": "faction1" if win else "faction2",
                    "score": {"faction1": 13 if win else 7,
                              "faction2": 7 if win else 13}},
    }


def stats_payload():
    return {
        "rounds": [{
            "round_stats": {"map": "Mirage", "score": "13 / 7"},
            "teams": [{
                "team_id": "t1",
                "players": [{"player_id": "p1", "player_stats": {
                    "Kills": "24", "Deaths": "17", "Assists": "3",
                    "Headshots %": "46", "ADR": "88.4"}}],
            }, {"team_id": "t2", "players": []}],
        }]
    }


def test_faceit_win_with_stats():
    m = to_match(entry(), "p1", stats_payload())
    assert m["result"] == "win"
    assert m["team_rounds"] == 13 and m["enemy_rounds"] == 7
    assert m["map"] == "Mirage"
    assert m["kills"] == 24 and m["deaths"] == 17
    assert m["hs_pct"] == 46 and m["adr"] == 88.4


def test_faceit_loss_no_stats():
    m = to_match(entry(win=False), "p1", None)
    assert m["result"] == "loss"
    assert m["team_rounds"] == 7 and m["enemy_rounds"] == 13


def test_faceit_not_my_match():
    assert to_match(entry(), "someone-else", None) is None


# ---------------------------------------------------------------- GSI

class FakeStore:
    def __init__(self):
        self.saved = []

    def upsert_match(self, m):
        self.saved.append(m)
        return True


def gsi_payload(map_name="de_mirage", phase="live", ct=7, t=5, kills=12,
                deaths=8, assists=2, side="CT", mode="premier"):
    return {
        "provider": {"steamid": "765"},
        "map": {"name": map_name, "phase": phase, "mode": mode,
                "team_ct": {"score": ct}, "team_t": {"score": t}},
        "player": {"steamid": "me", "team": side,
                   "match_stats": {"kills": kills, "deaths": deaths,
                                   "assists": assists, "mvps": 1}},
    }


def test_gsi_full_match():
    fs = FakeStore()
    g = GsiTracker(fs)
    assert g.ingest(gsi_payload(ct=5, t=3)) == "started"
    assert g.ingest(gsi_payload(ct=13, t=7, kills=24, deaths=15)) is None
    assert g.ingest(gsi_payload(phase="gameover", ct=13, t=7,
                                kills=24, deaths=15)) == "ended"
    assert len(fs.saved) == 1
    m = fs.saved[0]
    assert m.result == "win" and m.team_rounds == 13 and m.enemy_rounds == 7
    assert m.kills == 24 and m.deaths == 15 and m.assists == 2


def test_gsi_quiet_finalises():
    fs = FakeStore()
    g = GsiTracker(fs)
    g.ingest(gsi_payload(ct=8, t=8))
    g.last_post = time.time() - 999
    assert g.check_quiet() is True
    assert len(fs.saved) == 1


def test_gsi_ignores_dm():
    fs = FakeStore()
    g = GsiTracker(fs)
    assert g.ingest(gsi_payload(mode="deathmatch")) is None
    assert g.cur is None


def test_gsi_side_t_loss():
    fs = FakeStore()
    g = GsiTracker(fs)
    g.ingest(gsi_payload(side="T", ct=13, t=9))
    g.finalize()
    m = fs.saved[0]
    assert m.result == "loss"
    assert m.team_rounds == 9 and m.enemy_rounds == 13


# ---------------------------------------------------------------- elo series

def test_elo_series_dedup_persist_cap(tmp_path):
    s = Store(str(tmp_path / "t.json"))
    assert s.record_elo(1600, ts=100) is True
    assert s.record_elo(1600, ts=200) is False      # repeat -> skipped
    assert s.record_elo(1650, ts=300) is True
    assert s.elo_series() == [{"ts": 100, "elo": 1600},
                              {"ts": 300, "elo": 1650}]
    s2 = Store(str(tmp_path / "t.json"))            # survives reload
    assert s2.elo_series() == s.elo_series()
    for i in range(600):                            # capped, oldest dropped
        s.record_elo(1000 + i, ts=1000 + i)
    assert len(s.elo_series()) == Store.ELO_CAP
    assert s.elo_series()[0]["elo"] == 1000 + 600 - Store.ELO_CAP


def test_api_elo(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "a.json"))
    st.set_meta(faceit_elo=1600)
    st.record_elo(1600, ts=100)
    monkeypatch.setattr(app_mod, "store", st)
    c = app_mod.app.test_client()
    r = c.get("/api/elo")
    assert r.status_code == 200
    d = r.get_json()
    assert d["elo"] == 1600
    assert d["series"] == [{"ts": 100, "elo": 1600}]


# ---------------------------------------------------------------- API smoke

def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "store", Store(str(tmp_path / "a.json")))
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()
    r = c.get("/api/summary")
    assert r.status_code == 200
    d = r.get_json()
    assert d["faceit"]["games"] == 0
    r = c.get("/api/matches")
    assert r.status_code == 200
    r = c.post("/gsi", json={"map": {"name": "de_inferno", "phase": "live",
                                     "mode": "premier",
                                     "team_ct": {"score": 3},
                                     "team_t": {"score": 1}},
                             "player": {"team": "CT", "match_stats": {
                                 "kills": 5, "deaths": 2, "assists": 0}}})
    assert r.get_json()["event"] == "started"
    r = c.get("/healthz")
    assert r.get_json()["live"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
