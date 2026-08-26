#!/usr/bin/env python3
"""CS2 Tracker — personal FACEIT + Premier dashboard (Flask, :8092).

Reads secrets from ./secrets/ (faceit_key.txt, steam_key.txt,
player.txt). All upstream API calls happen in a background refresh
thread; request handlers only ever read the local store.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Optional

from flask import Flask, jsonify, render_template, request

import faceit as faceit_mod
import steam as steam_mod
from gsi import GsiTracker
from healer import SelfHealer
from store import Match, Store, summarize

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
SECRETS = os.path.join(BASE, "secrets")
STORE_PATH = os.path.join(DATA, "tracker.json")
PORT = int(os.environ.get("CS2TRACKER_PORT", "8092"))

log = logging.getLogger("cs2tracker")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def read_secret(name: str) -> Optional[str]:
    try:
        with open(os.path.join(SECRETS, name)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_secret(name: str, value: str):
    """Persist a rotated secret (keeps whatever perms the file has)."""
    try:
        with open(os.path.join(SECRETS, name), "w") as f:
            f.write(value)
    except OSError:
        pass


app = Flask(__name__)
store = Store(os.path.join(DATA, "tracker.json"))


def notify_match(m: Match):
    """ntfy push when a Premier match finalises (best-effort)."""
    url, tok = read_secret("ntfy_url.txt"), read_secret("ntfy_token.txt")
    if not (url and tok):
        return
    try:
        score = f"{m.team_rounds}:{m.enemy_rounds}"
        tag = {"win": "W", "loss": "L"}.get(m.result, "?")
        subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {tok}",
             "-H", f"Title: CS2 {tag} {score} {m.map}",
             "-d", f"{m.result} {score} · {m.kills}K/{m.deaths}D/{m.assists}A",
             f"{url}/cs2"],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ntfy(title: str, body: str):
    """Push an arbitrary note (healer alerts etc)."""
    url, tok = read_secret("ntfy_url.txt"), read_secret("ntfy_token.txt")
    if not (url and tok):
        return
    try:
        subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {tok}",
             "-H", f"Title: {title}", "-d", body, f"{url}/cs2"],
            timeout=5, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except Exception:
        pass


gsi = GsiTracker(store, on_match=notify_match)


# --------------------------------------------------------------------------
# background refresh
# --------------------------------------------------------------------------

class Refresher(threading.Thread):
    INTERVAL = 600          # 10 min

    def __init__(self):
        super().__init__(daemon=True, name="cs2tracker-refresh")
        self.stop_evt = threading.Event()

    def run(self):
        self.stop_evt.wait(20)                 # boot settle
        while not self.stop_evt.is_set():
            try:
                self.refresh_once()
            except Exception as e:
                log.warning("refresh failed: %s", e)
            self.stop_evt.wait(self.INTERVAL)

    def refresh_once(self):
        cfg = self.config()
        if not cfg:
            log.debug("no player config yet")
            return
        if cfg.get("faceit_nickname") and read_secret("faceit_key.txt"):
            self.pull_faceit(cfg)
            store.set_meta(last_refresh_success=time.time())
        sc_key = read_secret("steam_key.txt")
        sid = cfg.get("steam_id64")
        if not sid and sc_key and cfg.get("steam_vanity"):
            sc = steam_mod.SteamClient(sc_key)
            sid = sc.resolve_vanity(cfg["steam_vanity"])
            if sid:
                cfg["steam_id64"] = sid
                store.set_meta(steam_id64=sid)
        if sc_key and sid:
            stats = None
            try:
                stats = steam_mod.SteamClient(sc_key).cs2_lifetime(sid)
            except Exception as e:
                cookie = read_secret("steam_cookie.txt")
                if cookie:
                    try:
                        stats = steam_mod.authed_lifetime(
                            sid, cookie, sc_key,
                            on_rotate=lambda c: write_secret(
                                "steam_cookie.txt", c))
                    except Exception as e2:
                        log.info("steam authed lifetime: %s", e2)
                log.info("steam lifetime: %s", e)
            if stats:
                store.set_lifetime("steam", stats)
                store.set_meta(steam_checked=time.time())

    @staticmethod
    def config() -> dict:
        raw = read_secret("player.txt") or ""
        cfg = {}
        for line in raw.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
        return cfg

    def pull_faceit(self, cfg: dict):
        fc = faceit_mod.FaceitClient(read_secret("faceit_key.txt"))
        nick = cfg["faceit_nickname"]
        pid = store.meta("faceit_player_id")
        if not pid:
            p = fc.player(nickname=nick)
            pid = p["player_id"]
            store.set_meta(faceit_player_id=pid)
        elo = None
        try:
            p = fc.player(nickname=nick)
            games = p.get("games") or {}
            elo = games.get("cs2", {}).get("faceit_elo")
            if elo:
                store.set_meta(faceit_elo=elo)
                store.record_elo(elo)
        except Exception:
            pass
        known = {m.match_id for m in store.matches(limit=300)}
        new_count = 0
        for offset in (0, 20):
            try:
                items = fc.player_history(pid, limit=20, offset=offset)
            except Exception:
                break
            if not items:
                break
            for entry in items:
                mid = f"faceit-{entry.get('match_id')}"
                if mid in known:
                    continue
                stats = None
                try:
                    stats = fc.match_stats(entry["match_id"])
                except Exception:
                    pass
                d = faceit_mod.to_match(entry, pid, stats)
                if d:
                    store.upsert_match(Match(**d))
                    new_count += 1
            time.sleep(1.0)        # be gentle with rate limits
        if new_count:
            log.info("faceit: stored %d new matches", new_count)


refresher = Refresher()
refresher.start()


def _refresher_alive() -> bool:
    return refresher.is_alive()


def _restart_refresher():
    global refresher
    if refresher.is_alive():
        return
    refresher = Refresher()
    refresher.start()


healer = SelfHealer(
    port=PORT,
    refresher_starter=_restart_refresher,
    refresher_probe=_refresher_alive,
    gsi=gsi, store=store,
    notify=_ntfy,
    store_path=STORE_PATH,
)
healer.start()


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/summary")
def api_summary():
    ms = store.matches(limit=500)
    faceit = [m for m in ms if m.source == "faceit"]
    premier = [m for m in ms if m.source == "premier"]
    return jsonify({
        "faceit": {"elo": store.meta("faceit_elo"), **summarize(faceit)},
        "premier": summarize(premier),
        "meta": {"steam_lifetime": store.lifetime("steam"),
                 "updated": time.time()},
    })


@app.route("/api/matches")
def api_matches():
    source = request.args.get("source")
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify({"matches": [m.__dict__ for m in store.matches(
        source=source, limit=limit)]})


@app.route("/api/premier/lifetime")
def api_lifetime():
    return jsonify(store.lifetime("steam") or {"stats": {}})


@app.route("/api/elo")
def api_elo():
    return jsonify({"elo": store.meta("faceit_elo"),
                    "series": store.elo_series()})


@app.post("/gsi")
def gsi_ingest():
    gs = request.get_json(force=True, silent=True)
    if not isinstance(gs, dict):
        return jsonify(error="bad payload"), 400
    ev = gsi.ingest(gs)
    if ev == "started":
        log.info("GSI: match started on %s", gs.get("map", {}).get("name"))
    return jsonify(ok=True, event=ev)


@app.get("/healthz")
def healthz():
    try:
        v = healer.vitals()
    except Exception:
        v = {}
    return jsonify(ok=True, matches=len(store.matches(limit=1000)),
                   live=gsi.cur is not None, **v)


@app.get("/healthz/quiet")
def healthz_quiet():
    """For the systemd timer: finalise silent matches."""
    return jsonify(finalised=gsi.check_quiet())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
