# CS2 Tracker

Personal Counter-Strike 2 tracker: recent matches and stats across
**FACEIT** and **Valve Premier**, on one dashboard, served from the Pi
over the tailnet.

## Why two data paths

- **FACEIT** — official Data API v4: full match history, per-match
  K/D/A, headshot %, win/loss, ELO over time. Backfilled automatically.
- **Premier** — Valve publishes **no match-history API** for matchmaking.
  The Steam Web API gives lifetime stats (wins, K/D, MVPs, accuracy…),
  and a built-in **Game State Integration (GSI) listener** captures each
  Premier match live as he plays it — map, score, result — building a
  Premier match history from install day forward.

## Endpoints

| Path | What |
|---|---|
| `/` | dashboard UI |
| `/api/summary` | combined stats cards |
| `/api/matches` | unified match feed (faceit + premier) |
| `/api/elo` | FACEIT elo history — one sample per elo change, recorded at each hourly refresh since install |
| `/api/premier/lifetime` | Steam lifetime stats |
| `/gsi` | CS2 GSI ingest (POST from his gaming PC) |

## Setup

    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    # secrets (chmod 600, outside git):
    #   faceit_key.txt   — FACEIT Data API key
    #   steam_key.txt    — Steam Web API key
    #   player.txt       — lines: faceit_nickname=..., steam_id64=... (or steam_vanity=...)
    venv/bin/python app.py            # :8092

GSI capture: drop `deploy/cs2tracker.cfg` into the CS2 cfg directory on
the gaming PC (`.../Counter-Strike Global Offensive/game/csgo/cfg/`).
The listener ignores non-official modes.

## Tests

    venv/bin/python -m pytest tests/ -q
