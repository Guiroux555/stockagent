"""Surviving a reboot and a dead router.

The agent's home is an unattended Compute Module: no screen, no battery-backed
clock, and an internet connection that is not a given. These are the three
failures that follow from that, and what the agent is supposed to do about
each.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import bars_from_closes, session_opens, trending_closes

from trader import agent, data, health
from trader import store as store_module
from trader.store import Store

LAST_SESSION = datetime.fromtimestamp(session_opens(700)[-1] / 1000, timezone.utc)
NOW = LAST_SESSION + timedelta(hours=7)
"""The evening of the last synthetic session, just after its closing bell."""

BLIND = NOW + timedelta(days=9)
"""Nine days later: past a long weekend, past any holiday, unambiguously an
agent that has not seen a price in far too long."""


@pytest.fixture(autouse=True)
def offline_host(monkeypatch, tmp_path):
    """Nothing here may depend on the machine running the tests.

    The clock checks consult a systemd flag file and a hardcoded date that this
    code is known to postdate; the synthetic sessions are from 2015-2017. Both
    are pinned so the tests describe the agent's behaviour rather than the year.
    """
    monkeypatch.setattr(health, "TIMESYNC_FLAG", tmp_path / "no-such-flag")
    monkeypatch.setattr(health, "CLOCK_FLOOR", datetime(2000, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def cfg(settings):
    return replace(settings, universe=("AAA", "BBB"), min_notional=0.0)


@pytest.fixture
def store(tmp_path, cfg):
    s = Store(tmp_path / "r.db")
    for k, sym in enumerate(("AAA", "BBB"), 1):
        s.save_bars(sym, cfg.interval, bars_from_closes(trending_closes(seed=k)))
    s.save_bars("SPY", cfg.interval, bars_from_closes(trending_closes(seed=9, amp=0.02)))
    yield s
    s.close()


# --- 1. the link is down ----------------------------------------------------


def test_a_dead_link_is_detected_once_not_per_name():
    """Eighty-eight names each retrying two hosts three times turns a
    thirty-second outage into a half-hour sync."""
    link = data.Link(tolerance=2)
    assert not link.down
    link.failed(OSError("unreachable"))
    assert not link.down
    link.failed(OSError("unreachable"))
    assert link.down
    with pytest.raises(data.OfflineError):
        link.check("AAPL")


def test_one_success_clears_the_verdict():
    link = data.Link(tolerance=2)
    link.failed(OSError("blip"))
    link.worked()
    assert not link.down
    link.check("AAPL")  # does not raise


def test_a_transport_failure_is_offline_but_an_http_status_is_not(cfg, monkeypatch):
    """A status code came from a server, so there is a link and the problem is
    this request. A refused connection is the link itself."""
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        data,
        "_get",
        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("no route")),
    )
    with pytest.raises(data.OfflineError):
        data._request("/v8/finance/chart/AAPL", {"symbol": "AAPL"}, cfg)

    def broken(*_a, **_k):
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)

    monkeypatch.setattr(data, "_get", broken)
    with pytest.raises(data.DataError) as caught:
        data._request("/v8/finance/chart/AAPL", {"symbol": "AAPL"}, cfg)
    assert not isinstance(caught.value, data.OfflineError)


def test_an_unreachable_host_is_not_asked_three_times(cfg, monkeypatch):
    """Retrying is for a server that said something. A connection that timed
    out said nothing, and asking it twice more costs a minute to learn what the
    first thirty seconds already established."""
    calls = []
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)

    def dead(url, *_a, **_k):
        calls.append(url)
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(data, "_get", dead)
    with pytest.raises(data.OfflineError):
        data._request("/v8/finance/chart/AAPL", {"symbol": "AAPL"}, cfg)
    assert len(calls) == len(cfg.hosts), "one attempt per host, then give up"

def test_a_sync_with_no_link_stops_asking(store, settings, monkeypatch):
    """The point is the cost, not the outcome: an offline sync must not take
    longer than the agent's whole margin before the next closing bell."""
    calls = []

    def unreachable(symbol, settings, bars=None, link=None):
        calls.append(symbol)
        link.failed(OSError("no route to host"))
        raise data.OfflineError("no host reachable")

    monkeypatch.setattr(agent.market, "fetch_history", unreachable)
    wide = replace(settings, universe=tuple(f"SYM{i}" for i in range(30)))
    counts = agent.sync(store, wide, log=lambda *_: None)

    assert len(calls) == data.Link().tolerance, (
        "one timeout is a blip and the next name may work; two in a row is the "
        "link, and the twenty-eight after it will fail identically"
    )
    assert set(counts) == set(wide.data_universe), "every name is still accounted for"
    assert sum(counts.values()) == 0


def test_an_empty_fetch_does_not_wipe_the_cache(store, cfg):
    """An empty response is a failed fetch, not a delisting — and the
    difference is one bad sync against a cold start."""
    before = len(store.load_bars("AAA", cfg.interval))
    assert store.replace_bars("AAA", cfg.interval, []) == 0
    assert len(store.load_bars("AAA", cfg.interval)) == before


def test_the_optional_feeds_are_not_asked_when_nothing_is_reachable(
    store, cfg, monkeypatch
):
    """Both are one request per name. With the link down they would repeat the
    half-hour of timeouts the price sync just established was pointless."""
    asked = []
    monkeypatch.setattr(agent, "sync", lambda *a, **k: dict.fromkeys(cfg.universe, 0))
    monkeypatch.setattr(
        agent.calendar, "sync", lambda *a, **k: asked.append("earnings") or {}
    )
    monkeypatch.setattr(
        agent.newsfeed, "sync", lambda *a, **k: asked.append("news") or {}
    )
    loud = replace(cfg, earnings_mode="confirmed", news_enabled=True)
    agent.run_tick(store, loud, now=NOW, do_sync=True, log=lambda *_: None)
    assert asked == []


# --- 2. the data is stale ---------------------------------------------------


def _decide_flags(store, cfg, now, monkeypatch) -> list[bool]:
    """Every `decide` the engine was stepped with during one tick."""
    flags: list[bool] = []
    original = agent.Engine.step

    def spy(self, views, ts, day_start, peak, decide=True):
        flags.append(decide)
        return original(self, views, ts, day_start, peak, decide=decide)

    monkeypatch.setattr(agent.Engine, "step", spy)
    agent.run_tick(store, cfg, now=now, do_sync=False, log=lambda *_: None)
    return flags


def test_a_fresh_tick_may_open_positions(store, cfg, monkeypatch):
    assert _decide_flags(store, cfg, NOW, monkeypatch)[-1] is True


def test_stale_data_suspends_entries_but_still_supervises_stops(store, cfg, monkeypatch):
    """A breakout read off last Tuesday's close becomes an order queued for an
    open that has already happened. A stop is a promise made at entry, so it is
    measured against the last close the agent has."""
    flags = _decide_flags(store, cfg, BLIND, monkeypatch)
    assert flags and not any(flags), "nothing new may be opened"
    assert len(flags) >= 1, "the engine still ran: stops and resting orders were seen"


def test_a_long_weekend_is_not_an_outage(store, cfg):
    """The market is shut two thirds of the week: at 16:05 on a Monday the
    newest close is three days old and nothing is wrong."""
    stale, _ = health.is_stale(store, cfg, NOW + timedelta(days=3))
    assert not stale


def test_the_suspension_is_written_down(store, cfg):
    """`trader log` has to be able to answer 'why did it do nothing last
    week?' three weeks later."""
    agent.run_tick(store, cfg, now=BLIND, do_sync=False, log=lambda *_: None)
    reasons = [d.reason for d in store.recent_decisions(10)]
    assert any("entries suspended" in r for r in reasons)


def test_a_clock_behind_its_own_data_counts_as_stale(store, cfg):
    """An unsynchronised Pi reports a time in the past. Negative staleness is
    not freshness."""
    stale, age = health.is_stale(store, cfg, LAST_SESSION - timedelta(days=6))
    assert stale and age is not None and age < 0


def test_an_empty_cache_is_stale(tmp_path, cfg):
    with Store(tmp_path / "empty.db") as empty:
        assert health.is_stale(empty, cfg, NOW) == (True, None)


# --- 3. coming back --------------------------------------------------------


def test_a_blind_tick_comes_back_sooner_than_the_closing_bell(store, cfg):
    agent.run_tick(store, cfg, now=BLIND, do_sync=False, log=lambda *_: None)

    wake = datetime.fromisoformat(store.get_state(agent.K_NEXT_RUN))
    assert wake - BLIND <= timedelta(seconds=agent.RETRY_BACKOFF[0])
    assert store.get_state(agent.K_DEGRADED_COUNT) == 1
    assert store.get_state(agent.K_DEGRADED_SINCE)


def test_the_retry_backs_off_and_stops_backing_off():
    delays = [agent.retry_delay(n) for n in range(1, 12)]
    assert delays == sorted(delays), "never gets faster"
    assert delays[-1] == agent.RETRY_BACKOFF[-1] <= 3600, "and never longer than an hour"


def test_the_degraded_flag_clears_when_the_data_comes_back(store, cfg):
    agent.run_tick(store, cfg, now=BLIND, do_sync=False, log=lambda *_: None)
    assert store.get_state(agent.K_DEGRADED_COUNT) == 1

    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    assert store.get_state(agent.K_DEGRADED_COUNT) == 0
    assert not store.get_state(agent.K_DEGRADED_SINCE)


def test_a_failed_tick_does_not_become_a_busy_loop(store, cfg, monkeypatch):
    """The schedule is written at the end of a tick, so a tick that raised
    leaves `next_run` in the past — and a wake-up time in the past is a spin,
    which on a network failure means hammering the network."""
    ticks = []

    def exploding(*_a, **_k):
        ticks.append(1)
        raise RuntimeError("kaboom")

    def stop_at_the_first_nap(_seconds):
        raise KeyboardInterrupt  # stands in for "the agent went to sleep"

    monkeypatch.setattr(agent, "run_tick", exploding)
    monkeypatch.setattr(agent.clock, "sleep", stop_at_the_first_nap)

    with pytest.raises(KeyboardInterrupt):
        agent.run_forever(store, cfg, log=lambda *_: None)

    assert len(ticks) == 1, "it slept instead of ticking again straight away"
    wake = datetime.fromisoformat(store.get_state(agent.K_NEXT_RUN))
    assert wake > datetime.now(timezone.utc) + timedelta(seconds=60)


# --- 4. the clock ----------------------------------------------------------


def test_a_clock_before_every_date_on_disk_is_not_believed(store, cfg):
    """A session cannot open in the future, so the newest one on disk is a
    lower bound on the present that maintains itself."""
    ok, note = health.clock_is_sane(store, cfg, LAST_SESSION - timedelta(days=400))
    assert not ok and "before" in note


def test_the_ntp_flag_settles_it(store, cfg, monkeypatch, tmp_path):
    """`systemd-timesyncd` touches this file once NTP has answered, which is
    the authoritative answer on the board this runs on."""
    flag = tmp_path / "synchronized"
    flag.write_text("")
    monkeypatch.setattr(health, "TIMESYNC_FLAG", flag)
    ok, _ = health.clock_is_sane(store, cfg, datetime(1970, 1, 2, tzinfo=timezone.utc))
    assert ok


def test_the_wait_for_a_clock_is_bounded(store, cfg, monkeypatch):
    """A board with no internet at all would otherwise never start, and an
    agent that is up and refusing to enter beats one that is not up."""
    monkeypatch.setattr(
        health, "clock_is_sane", lambda *_a, **_k: (False, "clock reads 1970")
    )
    slept = []
    ok = agent.await_clock(
        store, cfg, log=lambda *_: None, sleep=slept.append, timeout=20
    )
    assert ok is False
    assert sum(slept) <= 20


def test_the_wait_ends_as_soon_as_the_clock_is_set(store, cfg, monkeypatch):
    answers = iter([(False, "not yet"), (False, "not yet"), (True, "NTP synchronised")])
    monkeypatch.setattr(health, "clock_is_sane", lambda *_a, **_k: next(answers))
    slept = []
    assert agent.await_clock(
        store, cfg, log=lambda *_: None, sleep=slept.append, timeout=600
    )
    assert len(slept) == 2


# --- 5. the power went out mid-write ---------------------------------------


def test_the_database_is_durable_across_a_power_cut(tmp_path):
    """`synchronous=NORMAL` leaves the last commits in the page cache, and on a
    board with no battery a power cut is the ordinary way to turn it off."""
    with Store(tmp_path / "d.db") as s:
        assert s.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_sound_database_reports_sound(store):
    assert store.integrity_check() is None


def test_a_corrupt_database_is_reported_rather_than_raised(tmp_path, cfg):
    path = tmp_path / "broken.db"
    with Store(path) as s:
        s.save_bars("AAA", cfg.interval, bars_from_closes(trending_closes(n=400)))
    with open(path, "r+b") as fh:  # scribble over the middle of the file
        fh.seek(4096)
        fh.write(b"\xff" * 4096)
    with Store(path) as s:
        assert s.integrity_check() is not None


def test_a_backup_is_a_readable_database(store, cfg, tmp_path):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    dest = store.backup(tmp_path / "backups" / "live.db")

    assert dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()
    with Store(dest) as copy:
        assert copy.integrity_check() is None
        assert copy.get_state(agent.K_NEXT_RUN) == store.get_state(agent.K_NEXT_RUN)
        assert len(copy.load_bars("AAA", cfg.interval)) == 700


def test_an_interrupted_backup_cannot_replace_a_good_one(store, tmp_path, monkeypatch):
    """Copying onto the destination directly would turn a full disk into the
    loss of the backup as well as of the run."""
    dest = tmp_path / "live.db"
    store.backup(dest)
    good = dest.read_bytes()

    def die(*_a, **_k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store_module.sqlite3, "connect", die)
    with pytest.raises(sqlite3.OperationalError):
        store.backup(dest)
    assert dest.read_bytes() == good


# --- 6. the board only has so much of anything -----------------------------


def test_a_live_tick_reads_a_bounded_window(store, cfg, monkeypatch):
    """Reading thirty-six years of sessions for every name to look at the last
    few hundred is how a 1 GB board runs out of memory."""
    windows = []
    original = Store.load_bars

    def spy(self, symbol, interval, limit=None, until=None):
        windows.append(limit)
        return original(self, symbol, interval, limit=limit, until=until)

    monkeypatch.setattr(Store, "load_bars", spy)
    agent.pending_steps(store, cfg)
    assert windows and all(w == cfg.live_window for w in windows)
    assert cfg.live_window >= cfg.warmup_bars + agent.MAX_CATCH_UP


def test_the_window_is_the_tail_and_it_is_taken_in_sql(store, cfg):
    everything = store.load_bars("AAA", cfg.interval)
    tail = store.load_bars("AAA", cfg.interval, limit=10)
    assert tail == everything[-10:]

    plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM bars WHERE symbol=? AND interval=?"
        " ORDER BY open_time DESC LIMIT ?",
        ("AAA", cfg.interval, 10),
    ).fetchall()
    assert not any("TEMP B-TREE" in str(row) for row in plan), "the index does the work"


def test_an_ordinary_sync_writes_one_row_not_the_whole_table(store, cfg):
    """A DELETE plus a full re-INSERT rewrote several hundred thousand rows a
    day onto an SD card to record one new close. Flash wears out."""
    series = bars_from_closes(trending_closes(n=400))
    store.replace_bars("CCC", cfg.interval, series)

    writes = []
    store.conn.set_trace_callback(writes.append)
    store.replace_bars("CCC", cfg.interval, series)  # nothing changed at all
    store.conn.set_trace_callback(None)
    assert not any("INSERT" in w or "DELETE" in w for w in writes)


def test_a_restated_series_is_still_replaced_in_full(store, cfg):
    """Equity prices are restated backwards: a split rewrites every bar before
    it, and merging fresh rows onto stale ones would leave a step change the
    agent reads as a gap that never happened."""
    series = bars_from_closes(trending_closes(n=400))
    store.replace_bars("CCC", cfg.interval, series)

    halved = [replace(b, close=b.close / 2, open=b.open / 2) for b in series]
    store.replace_bars("CCC", cfg.interval, halved)
    assert store.load_bars("CCC", cfg.interval) == halved


def test_a_session_the_source_stopped_serving_is_deleted(store, cfg):
    series = bars_from_closes(trending_closes(n=400))
    store.replace_bars("CCC", cfg.interval, series)
    store.replace_bars("CCC", cfg.interval, series[:-5])
    assert len(store.load_bars("CCC", cfg.interval)) == 395


# --- 7. what the board can tell you about itself ---------------------------


def test_health_reports_a_healthy_agent(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    state = health.check(store, cfg, NOW)
    assert state.ok
    assert state.last_tick == NOW
    assert "healthy" in state.report()


def test_health_reports_an_agent_that_cannot_see_the_market(store, cfg):
    agent.run_tick(store, cfg, now=BLIND, do_sync=False, log=lambda *_: None)
    state = health.check(store, cfg, BLIND)
    assert not state.ok and state.stale
    assert "DEGRADED" in state.report()


def test_health_notices_a_loop_that_stopped_waking_up(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    much_later = NOW + timedelta(days=30)
    state = health.check(store, cfg, much_later)
    assert state.overdue is not None and state.overdue > timedelta(days=20)
    assert "OVERDUE" in state.report()


def test_the_state_round_trips_as_json(store, cfg):
    """Everything the agent has to remember across a reboot lives in the one
    file, and lives there as something a human can read."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rows = dict(store.conn.execute("SELECT key, value FROM state").fetchall())
    for key in (agent.K_NEXT_RUN, agent.K_LAST_TICK, agent.K_CASH, agent.K_HALTED):
        assert key in rows
        json.loads(rows[key])
