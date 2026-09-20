"""Partial exits: implemented, tested, and off by default because they lose.

They are kept working rather than deleted so the measurement in the README can
be reproduced, and so the mechanics are not rebuilt wrongly the next time
someone wants to try them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import bars_from_closes, trending_closes

from trader.backtest import run_backtest
from trader.engine import Engine, SymbolView
from trader.models import Order, Position
from trader.portfolio import Portfolio
from trader.store import Store
from trader.strategy import analyze


@pytest.fixture
def cfg(settings):
    return replace(
        settings,
        universe=("AAA",),
        market_anchor="",
        min_notional=0.0,
        whole_shares=False,
        scale_out_r=2.0,
        scale_out_fraction=0.5,
    )


@pytest.fixture
def series():
    return {"AAA": bars_from_closes(trending_closes(seed=3))}


def _a_held_session(cfg, a) -> int:
    """A session where the exit rules say hold, so the test is about the
    scale-out and not about an exit that happens to fire first."""
    return next(
        i
        for i in range(cfg.warmup_bars + 5, len(a.bars))
        if a.closes[i] > a.ema_regime[i]
        and not (a.closes[i] < a.ema_trend[i] and a.ema_fast[i] < a.ema_slow[i])
    )


def test_a_scale_out_is_queued_for_the_next_open_like_everything_else(cfg, series):
    a = analyze(series["AAA"], cfg)
    i = _a_held_session(cfg, a)
    pf = Portfolio(cfg)
    pf.positions["AAA"] = Position(
        symbol="AAA", qty=100, entry_price=a.closes[i] * 0.5, entry_time=0,
        stop=1.0, peak=a.closes[i], atr_at_entry=1.0,
        initial_risk=a.closes[i] * 0.1, bars_held=50,
    )
    engine = Engine(cfg, pf)
    result = engine.step(
        {"AAA": SymbolView(a, i)}, i, 1e5, 1e5, decide=True
    )
    partials = [o for o in result.orders if o.side == "SELL" and o.fraction < 1]
    assert partials, "a position well past the target queued no partial exit"
    assert pf.positions["AAA"].qty == 100, "it was sold at the close it was decided on"


def test_the_stop_moves_to_breakeven_once_part_is_banked(cfg, series):
    a = analyze(series["AAA"], cfg)
    i = cfg.warmup_bars + 30
    pf = Portfolio(cfg)
    pf.buy("AAA", 100, a.opens[i] * 0.5, ts=0, stop=1.0, atr=1.0)
    pf.positions["AAA"].initial_risk = a.opens[i] * 0.1

    engine = Engine(cfg, pf)
    engine.pending = [
        Order("AAA", "SELL", "banked half", a.closes[i - 1], fraction=0.5,
              created_ts=a.bars[i - 1].open_time)
    ]
    engine.step({"AAA": SymbolView(a, i)}, i, 1e5, 1e5, decide=False)
    pos = pf.positions["AAA"]
    assert pos.scaled_out
    assert pos.stop >= pos.entry_price


def test_a_position_is_only_ever_scaled_once(cfg, series):
    a = analyze(series["AAA"], cfg)
    i = cfg.warmup_bars + 30
    pf = Portfolio(cfg)
    pf.buy("AAA", 100, a.opens[i] * 0.5, ts=0, stop=1.0, atr=1.0)
    pf.positions["AAA"].initial_risk = a.opens[i] * 0.1
    pf.positions["AAA"].scaled_out = True
    pf.positions["AAA"].bars_held = 50

    engine = Engine(cfg, pf)
    result = engine.step({"AAA": SymbolView(a, i)}, i, 1e5, 1e5, decide=True)
    assert not [o for o in result.orders if o.fraction < 1]


def test_scaling_out_is_off_by_default(settings):
    assert settings.scale_out_r == 0.0


def test_the_measurement_that_keeps_it_off_can_be_rerun(settings):
    """The README claims scaling out loses. The claim has to stay reproducible,
    or it is just an assertion in a document."""
    names = ("AAA", "BBB", "CCC")
    base = replace(settings, universe=names, min_notional=0.0, whole_shares=False)
    store = Store(":memory:")
    for k, sym in enumerate(names, 1):
        store.save_bars(
            sym, base.interval, bars_from_closes(trending_closes(n=2500, seed=k))
        )
    store.save_bars(
        "SPY",
        base.interval,
        bars_from_closes(trending_closes(n=2500, seed=9, amp=0.02)),
    )
    off = run_backtest(store, base)
    on = run_backtest(store, replace(base, scale_out_r=1.0, scale_out_fraction=0.5))
    store.close()
    assert on.partial_exits > 0, "nothing scaled, so the comparison is empty"
    assert off.total_return != on.total_return
