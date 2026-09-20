"""Position sizing and the circuit breakers."""

from __future__ import annotations

from dataclasses import replace

import pytest

from trader.portfolio import Portfolio
from trader.risk import entry_guard, size_position


def size(pf, price, stop, settings, equity=None, prices=None):
    return size_position(
        pf, price, stop, equity or settings.initial_capital, prices or {}, settings
    )


def test_a_wider_stop_buys_less(settings):
    """The whole point of sizing off the stop: the loss if it is hit is the
    same either way."""
    pf = Portfolio(settings)
    tight = size(pf, 100.0, 95.0, settings)
    wide = size(pf, 100.0, 80.0, settings)
    assert tight.qty > wide.qty


def test_the_loss_at_the_stop_is_the_configured_fraction_of_equity(settings):
    cfg = replace(settings, whole_shares=False, max_position_pct=1.0)
    pf = Portfolio(cfg)
    equity = cfg.initial_capital
    sizing = size(pf, 100.0, 95.0, cfg, equity=equity)
    loss = sizing.qty * (pf.buy_fill_price(100.0) - 95.0)
    assert loss == pytest.approx(equity * cfg.risk_per_trade, rel=1e-6)


def test_a_stop_above_the_entry_is_refused(settings):
    pf = Portfolio(settings)
    assert not size(pf, 100.0, 105.0, settings).ok


def test_whole_shares_round_down_never_up(settings):
    """Rounding up would break whichever cap was binding a line earlier, and
    the one that binds is usually the cash."""
    pf = Portfolio(settings, cash=1000.0)
    sizing = size(pf, 99.0, 50.0, settings, equity=1000.0)
    assert sizing.qty == int(sizing.qty)
    assert sizing.qty * pf.buy_fill_price(99.0) <= 1000.0


def test_a_share_too_expensive_for_the_budget_is_refused_not_rounded_to_one(settings):
    """A 1,200 USD share against a 300 USD risk budget cannot express a small
    position. Buying one share anyway would silently risk four times the
    budget."""
    cfg = replace(settings, initial_capital=10_000.0, min_notional=500.0)
    pf = Portfolio(cfg, cash=400.0)
    sizing = size(pf, 1200.0, 1100.0, cfg, equity=10_000.0)
    assert not sizing.ok


def test_fractional_shares_can_be_turned_back_on(settings):
    cfg = replace(settings, whole_shares=False)
    pf = Portfolio(cfg)
    assert size(pf, 100.0, 95.0, cfg).qty % 1 != 0


def test_the_position_cap_binds_before_the_risk_budget_does(settings):
    cfg = replace(settings, max_position_pct=0.01, whole_shares=False)
    pf = Portfolio(cfg)
    sizing = size(pf, 100.0, 99.9, cfg)
    assert "max position" in sizing.reason


def test_the_exposure_cap_counts_what_is_already_held(settings):
    cfg = replace(settings, whole_shares=False)
    pf = Portfolio(cfg)
    # 74,500 USD held against a 75,000 cap leaves room for 500, not for the
    # 3,000 the risk budget alone would have bought.
    pf.buy("AAPL", 745, 100.0, ts=1, stop=90.0, atr=1.0)
    sizing = size(
        pf, 100.0, 90.0, cfg, equity=cfg.initial_capital, prices={"AAPL": 100.0}
    )
    assert "exposure" in sizing.reason
    assert sizing.qty * 100.0 < 600.0


# --- circuit breakers -------------------------------------------------------


def guard(settings, equity, day_start, peak, halted=False, positions=0):
    pf = Portfolio(settings)
    for k in range(positions):
        pf.buy(f"S{k}", 1, 10.0, ts=1, stop=1.0, atr=1.0)
    return entry_guard(pf, equity, day_start, peak, settings, halted=halted)


def test_a_normal_day_allows_entries(settings):
    assert guard(settings, 100_000, 100_000, 100_000).allowed


def test_the_daily_loss_limit_blocks_entries(settings):
    g = guard(settings, 95_000, 100_000, 100_000)
    assert not g.allowed and "daily loss" in g.reason


def test_the_drawdown_breaker_latches(settings):
    g = guard(settings, 60_000, 60_000, 100_000)
    assert not g.allowed and g.halted


def test_a_latched_breaker_stays_latched_until_it_recovers(settings):
    """Otherwise the agent flaps on and off as equity oscillates around the
    threshold."""
    still_down = guard(settings, 80_000, 80_000, 100_000, halted=True)
    assert not still_down.allowed and still_down.halted

    recovered = guard(settings, 90_000, 90_000, 100_000, halted=True)
    assert recovered.allowed and not recovered.halted


def test_a_breaker_with_no_resume_level_needs_a_human(settings):
    cfg = replace(settings, drawdown_resume=None)
    g = guard(cfg, 99_000, 99_000, 100_000, halted=True)
    assert not g.allowed and "manual reset" in g.reason


def test_the_position_limit_blocks_entries(settings):
    cfg = replace(settings, max_concurrent=2)
    g = guard(cfg, 100_000, 100_000, 100_000, positions=2)
    assert not g.allowed and "position limit" in g.reason
