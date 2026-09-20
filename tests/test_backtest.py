"""The backtester: the honesty layer.

The single most important test in this file is the one that truncates the data
and checks the decisions do not change. Everything else the project claims
rests on it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import bars_from_closes, falling_closes, trending_closes

from trader.backtest import run_backtest
from trader.store import Store


@pytest.fixture
def store(settings):
    s = Store(":memory:")
    for k, sym in enumerate(("AAA", "BBB", "CCC"), 1):
        s.save_bars(sym, settings.interval, bars_from_closes(trending_closes(seed=k)))
    s.save_bars(
        "SPY", settings.interval, bars_from_closes(trending_closes(seed=9, amp=0.02))
    )
    yield s
    s.close()


@pytest.fixture
def cfg(settings):
    return replace(settings, universe=("AAA", "BBB", "CCC"), min_notional=0.0)


def test_the_strategy_does_not_peek_at_the_future(cfg, store):
    """Replay a truncated history and it must reproduce, trade for trade, what
    the full run decided over the same window.

    If it does not, something in the signal path is reading a session that had
    not happened yet, and every other number in this project is worthless.
    """
    stamps = sorted({b.open_time for b in store.load_bars("AAA", cfg.interval)})
    cut = stamps[-60]

    full = run_backtest(store, cfg, end=cut)
    trimmed_store = Store(":memory:")
    for sym in (*cfg.universe, "SPY"):
        bars = [b for b in store.load_bars(sym, cfg.interval) if b.open_time <= cut]
        trimmed_store.save_bars(sym, cfg.interval, bars)
    trimmed = run_backtest(trimmed_store, cfg)
    trimmed_store.close()

    assert [(t.symbol, t.entry_time, t.exit_time) for t in full.trades] == [
        (t.symbol, t.entry_time, t.exit_time) for t in trimmed.trades
    ]
    assert full.final == pytest.approx(trimmed.final)


def test_the_benchmark_is_measured_over_the_same_window(cfg, store):
    """Comparing a windowed strategy run against a full-history benchmark is
    how an in-sample report ends up quoting the wrong number to beat."""
    stamps = sorted({b.open_time for b in store.load_bars("AAA", cfg.interval)})
    early = run_backtest(store, cfg, end=stamps[len(stamps) // 2])
    late = run_backtest(store, cfg, start=stamps[len(stamps) // 2])
    full = run_backtest(store, cfg)
    for r in (early, late, full):
        assert r.benchmarks
    assert early.benchmarks[0].final != full.benchmarks[0].final


def test_both_benchmarks_are_reported(cfg, store):
    """The equal-weight basket is chosen with hindsight; the index is not.
    Printing only the flattering one is the point of not doing this."""
    names = [b.name for b in run_backtest(store, cfg).benchmarks]
    assert any("Equal-weight" in n for n in names)
    assert any("SPY" in n for n in names)


def test_open_positions_are_closed_at_the_last_price_seen(cfg, store):
    """Marking an open position out at a price the run never reached would
    price the exit in the future."""
    stamps = sorted({b.open_time for b in store.load_bars("AAA", cfg.interval)})
    r = run_backtest(store, cfg, end=stamps[-80])
    assert all(t.exit_time <= stamps[-80] for t in r.trades)


def test_a_falling_market_produces_no_positions(cfg):
    s = Store(":memory:")
    for sym in (*cfg.universe, "SPY"):
        s.save_bars(sym, cfg.interval, bars_from_closes(falling_closes(n=700)))
    r = run_backtest(s, cfg)
    s.close()
    assert not r.round_trips
    assert r.final == pytest.approx(cfg.initial_capital)


def test_a_slower_cadence_takes_fewer_decisions(cfg, store):
    fast = run_backtest(store, cfg, decide_every=1)
    slow = run_backtest(store, cfg, decide_every=5)
    assert slow.decisions < fast.decisions
    assert slow.sessions == fast.sessions, "sleeping does not skip sessions"


def test_a_sleeping_agent_still_trades_on_sessions_it_did_not_decide(cfg, store):
    """Resting stops and queued orders are at the broker. Skipping those
    sessions entirely would hand a low cadence protection it never paid for —
    and would let it escape losses it really took."""
    every = 10
    slow = run_backtest(store, cfg, decide_every=every)
    assert slow.trades, "nothing traded, so this test proves nothing"

    timeline = [ts for ts, _ in slow.equity_curve]
    decision_sessions = {ts for k, ts in enumerate(timeline) if k % every == 0}
    assert any(t.exit_time not in decision_sessions for t in slow.trades)


def test_the_win_rate_is_computed_per_position_not_per_exit(cfg, store):
    """A scale-out books a second, almost always winning, trade for the same
    entry. Counting exits would inflate the rate for free."""
    scaling = replace(cfg, scale_out_r=2.0, scale_out_fraction=0.5)
    r = run_backtest(store, scaling)
    if r.partial_exits:
        assert len(r.round_trips) < len(r.trades)


def test_concentration_is_reported(cfg, store):
    r = run_backtest(store, cfg)
    if r.round_trips:
        assert 0.0 <= r.top_trade_share <= 1.0
        assert "Best trade" in r.summary(cfg)


def test_a_halted_run_says_so_instead_of_reporting_a_frozen_account(cfg):
    """A run that freezes and reports a lower return is a run that lies by
    omission."""
    s = Store(":memory:")
    closes = trending_closes(n=400) + falling_closes(n=300, start=1000.0)
    for sym in (*cfg.universe, "SPY"):
        s.save_bars(sym, cfg.interval, bars_from_closes(closes))
    jumpy = replace(cfg, max_drawdown_stop=0.001, drawdown_resume=None)
    r = run_backtest(s, jumpy)
    s.close()
    if r.halted_at is not None:
        assert "HALTED" in r.summary(jumpy)


def test_exposure_is_reported_next_to_the_drawdown(cfg, store):
    """An agent that is half in cash should have half the drawdown, and saying
    so is the difference between a risk-adjusted claim and a flattering one."""
    r = run_backtest(store, cfg)
    assert 0.0 <= r.avg_exposure <= 1.0
    assert "Avg exposure" in r.summary(cfg)


def test_a_benchmark_that_does_not_cover_the_window_is_not_compared(cfg, store):
    """SPY starts in 1993 and this history starts in 1990. Quoting a 0%
    benchmark there would hand the agent a spurious thirty-point edge."""
    trimmed = Store(":memory:")
    for sym in cfg.universe:
        trimmed.save_bars(sym, cfg.interval, store.load_bars(sym, cfg.interval))
    # Enough history to be analysed, but starting well after the strategy did.
    trimmed.save_bars("SPY", cfg.interval, store.load_bars("SPY", cfg.interval)[300:])

    r = run_backtest(trimmed, cfg)
    trimmed.close()
    spy = next(b for b in r.benchmarks if "SPY" in b.name)
    assert not spy.available and spy.coverage < 0.9
    assert "n/a" in r.summary(cfg)
