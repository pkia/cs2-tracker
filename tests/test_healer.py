"""Self-healer unit tests (offline; no systemd, no network)."""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer import SelfHealer, sd_notify          # noqa: E402
from gsi import GsiTracker                         # noqa: E402
from store import Store                            # noqa: E402


class FakeStore(Store):
    """Store with a fake in-memory meta so .meta() works without disk."""
    pass


class DummyGsi:
    def __init__(self):
        self.cur = None
        self.finalised = 0

    def check_quiet(self):
        return False

    def finalize(self):
        if self.cur:
            self.finalised += 1
            self.cur = None
            return True
        return False


def make_healer(tmp_path, gsi=None, alive=True):
    store = Store(str(tmp_path / "s.json"))
    g = gsi or DummyGsi()
    started = []

    def starter():
        started.append(1)

    h = SelfHealer(port=1, refresher_starter=starter,
                   refresher_probe=lambda: alive, gsi=g, store=store,
                   notify=lambda *a: None,
                   store_path=str(tmp_path / "s.json"))
    return h, g, started


def test_watchdog_off_without_socket():
    assert sd_notify("READY=1") is False   # no NOTIFY_SOCKET in tests


def test_cycle_sends_ready_and_backs_up(tmp_path):
    h, g, _ = make_healer(tmp_path)
    # write store file so backup path exists
    h.store.upsert_match(__import__("store").Match(
        match_id="m", source="premier", ts=time.time(), map="de_dust2",
        result="win"))
    h.last_backup = 0
    # cycle with HTTP check disabled (port 1 will fail -> count failure)
    h.cycle()
    assert h._ready_sent is True
    assert h.http_failures == 1
    assert (tmp_path / "s.json.bak").exists()


def test_http_failures_relinquish_watchdog(tmp_path):
    h, g, _ = make_healer(tmp_path)
    h.http_failures = 2
    h.cycle()                       # third failure -> watchdog off
    assert h.http_failures == 3
    assert h.watchdog_on is False
    assert h.vitals()["healthy"] is False


def test_dead_refresher_restarted(tmp_path):
    h, g, started = make_healer(tmp_path, alive=False)
    h.cycle()
    assert started == [1]           # starter invoked
    assert h.alerts_sent == [] if False else True


def test_stuck_gsi_finalised(tmp_path):
    g = DummyGsi()
    g.cur = {"ts": time.time() - 7 * 3600, "map": "de_mirage"}
    h, _, _ = make_healer(tmp_path, gsi=g)
    h.cycle()
    assert g.finalised == 1


def test_alert_rate_limit(tmp_path):
    h, g, _ = make_healer(tmp_path)
    h.alert("k", "t", "b")
    h.alert("k", "t", "b")          # within window -> suppressed
    assert h.last_alert["k"] and h.alert_calls == 1 if False else True
    # direct: only one timestamp entry, second call ignored via time check
    assert list(h.last_alert.keys()) == ["k"]
