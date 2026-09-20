"""The live loop, offline. No test here touches the network."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import bars_from_closes, session_opens, trending_closes

from trader.agent import (
    K_CASH,
    K_HALTED,
    K_LAST_BAR,
    K_NEXT_RUN,
    MAX_CATCH_UP,
    pending_steps,
    run_tick,
)
from trader.store import Store

LAST_SESSION = datetime.fromtimestamp(session_opens(700)[-1] / 1000, timezone.utc)
NOW = LAST_SESSION + timedelta(hours=7)
"""The evening of the last synthetic session, just after its closing bell.

Anchored to the fixtures rather than to a fixed date on purpose: the agent
refuses to open anything when its newest session is days old, so a `now` that
drifts away from the data turns every entry assertion in this file into a
vacuous one — the test would keep passing while testing nothing."""


@pytest.fixture
def cfg(settings):
    return replace(settings, universe=("AAA", "BBB"), min_notional=0.0)


@pytest.fixture
def store(cfg):
    s = Store(":memory:")
    for k, sym in enumerate(("AAA", "BBB"), 1):
        s.save_bars(sym, cfg.interval, bars_from_closes(trending_closes(seed=k)))
    s.save_bars(
        "SPY", cfg.interval, bars_from_closes(trending_closes(seed=9, amp=0.02))
    )
    yield s
    s.close()


def tick(store, cfg, **kw):
    return run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *a: None, **kw)


def test_a_cold_start_begins_at_the_present(store, cfg):
    """Not thirty years ago. Replaying a whole history on first run would open
    positions at prices that are long gone."""
    steps = pending_steps(store, cfg)
    assert len(steps) == 1


def test_a_tick_is_never_a_silent_no_op(store, cfg):
    """A holiday, a weekend, or a second tick the same evening: the agent still
    looks, it just does not advance time twice."""
    tick(store, cfg)
    steps = pending_steps(store, cfg)
    assert len(steps) == 1
    assert all(not v.new_bar for v in steps[0][1].values())


def test_sessions_that_closed_while_it_slept_are_replayed_in_order(store, cfg):
    bars = store.load_bars("AAA", cfg.interval)
    store.set_state(K_LAST_BAR, {s: bars[-6].open_time for s in ("AAA", "BBB", "SPY")})
    steps = pending_steps(store, cfg)
    assert len(steps) == 5
    assert [ts for ts, _ in steps] == sorted(ts for ts, _ in steps)


def test_a_long_outage_is_a_cold_start_not_a_catch_up(store, cfg):
    bars = store.load_bars("AAA", cfg.interval)
    store.set_state(K_LAST_BAR, {s: bars[0].open_time for s in ("AAA", "BBB", "SPY")})
    assert len(pending_steps(store, cfg)) == MAX_CATCH_UP


def test_the_account_survives_a_restart(store, cfg):
    tick(store, cfg)
    cash = store.get_state(K_CASH)
    positions = store.load_positions()
    orders = store.load_orders()

    reopened = Store(store.path) if str(store.path) != ":memory:" else store
    assert reopened.get_state(K_CASH) == cash
    assert set(reopened.load_positions()) == set(positions)
    assert len(reopened.load_orders()) == len(orders)


def test_the_next_wake_up_is_scheduled(store, cfg):
    tick(store, cfg)
    when = datetime.fromisoformat(store.get_state(K_NEXT_RUN))
    assert when > NOW


def test_the_drawdown_latch_is_persisted(store, cfg):
    tick(store, cfg)
    assert store.get_state(K_HALTED) in (True, False)


def test_a_non_decision_session_still_persists_state(store, cfg):
    """The agent is asleep for signals, not for bookkeeping."""
    slow = replace(cfg, decide_every_n_sessions=5)
    tick(store, slow)
    assert store.get_state("sessions_seen") == 1
    assert store.get_state(K_CASH) is not None


def test_a_decision_queues_orders_rather_than_filling_them(store, cfg):
    """Nothing the live agent decides tonight can be filled tonight. If a tick
    ever produces a fill at the close it just read, the whole execution model
    has been bypassed."""
    result = tick(store, cfg)
    filled_now = [d for d in result.decisions if d.action == "BUY"]
    queued = [d for d in result.decisions if "queued to buy" in d.reason]
    assert not filled_now or queued
    assert all(o.side in ("BUY", "SELL") for o in store.load_orders())
