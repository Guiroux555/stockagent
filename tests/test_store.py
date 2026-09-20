"""Persistence. Everything the agent needs to survive a restart lives here."""

from __future__ import annotations

import pytest
from conftest import bars_from_closes

from trader.models import Decision, Order, Position, Trade
from trader.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_bars_round_trip(store, settings):
    bars = bars_from_closes([100.0, 101.0, 102.0])
    assert store.save_bars("AAPL", "1d", bars) == 3
    back = store.load_bars("AAPL", "1d")
    assert [b.close for b in back] == [100.0, 101.0, 102.0]


def test_replacing_a_series_does_not_merge_it(store, settings):
    """Equity prices are restated backwards: a split rewrites every bar before
    it. Merging fresh bars into stale ones leaves a step change at the join,
    and the agent reads that step as a gap that never happened."""
    store.save_bars("AAPL", "1d", bars_from_closes([100.0, 101.0, 102.0]))
    store.replace_bars("AAPL", "1d", bars_from_closes([25.0, 25.25, 25.5]))
    closes = [b.close for b in store.load_bars("AAPL", "1d")]
    assert closes == [25.0, 25.25, 25.5], "old unadjusted bars survived a split"


def test_the_order_queue_survives_a_restart(store):
    """An agent that forgets its queue buys nothing it decided on yesterday."""
    store.save_orders(
        [Order("AAPL", "BUY", "broke out", 190.0, stop=180.0, atr=3.0, created_ts=7)]
    )
    back = store.load_orders()
    assert len(back) == 1 and back[0].stop == 180.0 and back[0].created_ts == 7


def test_saving_the_queue_replaces_it(store):
    store.save_orders([Order("AAPL", "BUY", "a", 1.0, created_ts=1)])
    store.save_orders([Order("MSFT", "SELL", "b", 2.0, created_ts=2)])
    assert [o.symbol for o in store.load_orders()] == ["MSFT"]


def test_positions_round_trip_with_their_frozen_risk(store):
    pos = Position(
        symbol="AAPL", qty=13, entry_price=190.0, entry_time=1, stop=180.0,
        peak=195.0, atr_at_entry=3.0, initial_risk=10.0, bars_held=4,
    )
    store.save_position(pos)
    back = store.load_positions()["AAPL"]
    assert (back.initial_risk, back.bars_held, back.qty) == (10.0, 4, 13)


def test_the_drawdown_latch_outlives_the_process(store):
    store.set_state("halted", True)
    assert store.get_state("halted") is True


def test_holds_are_logged_too(store):
    """Knowing why the agent stood still is as important as knowing why it
    traded."""
    store.save_decision(Decision(1, "AAPL", "HOLD", "below regime EMA200", 100.0))
    assert store.recent_decisions(10)[0].reason == "below regime EMA200"


def test_reset_keeps_the_price_history(store):
    store.save_bars("AAPL", "1d", bars_from_closes([100.0, 101.0]))
    store.save_orders([Order("AAPL", "BUY", "a", 1.0, created_ts=1)])
    store.save_trade(Trade("AAPL", 1, 1.0, 2.0, 1, 2, 0.0, 1.0, "x"))
    store.reset_trading_state()
    assert store.load_bars("AAPL", "1d")
    assert not store.load_orders()
    assert not store.load_trades()


def test_an_old_database_gains_the_columns_it_lacks(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so an
    upgrade has to add them explicitly or the next insert fails."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pending_orders (symbol TEXT PRIMARY KEY, side TEXT NOT NULL,"
        " reason TEXT NOT NULL, signal_price REAL NOT NULL, stop REAL NOT NULL"
        " DEFAULT 0, atr REAL NOT NULL DEFAULT 0, created_ts INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.save_orders([Order("AAPL", "SELL", "x", 1.0, fraction=0.5, created_ts=1)])
    assert store.load_orders()[0].fraction == 0.5
    store.close()
