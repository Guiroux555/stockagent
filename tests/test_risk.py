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


# --- the two ways of deciding how much -------------------------------------


def test_risk_sizing_is_the_default(settings):
    assert settings.sizing_mode == "risk"


def test_risk_sizing_gives_the_volatile_name_less(settings):
    """This is inverse-volatility weighting wearing a risk-management hat, and
    naming it that way is the point of the comparison below."""
    cfg = replace(settings, whole_shares=False, max_position_pct=1.0)
    pf = Portfolio(cfg)
    calm = size(pf, 100.0, 100 * (1 - 4 * 0.0135), cfg)
    wild = size(pf, 100.0, 100 * (1 - 4 * 0.0431), cfg)
    assert calm.qty > wild.qty * 2


def test_notional_sizing_gives_every_name_the_same(settings):
    cfg = replace(settings, sizing_mode="notional", whole_shares=False)
    pf = Portfolio(cfg)
    calm = size(pf, 100.0, 100 * (1 - 4 * 0.0135), cfg)
    wild = size(pf, 100.0, 100 * (1 - 4 * 0.0431), cfg)
    assert calm.qty == pytest.approx(wild.qty)
    assert "fixed notional" in calm.reason


def test_notional_sizing_lets_the_risk_vary_instead(settings):
    """Same money in, different money at stake. That is exactly the quantity
    the risk mode holds constant, so the two modes trade one for the other."""
    cfg = replace(settings, sizing_mode="notional", whole_shares=False)
    pf = Portfolio(cfg)
    calm = size(pf, 100.0, 100 * (1 - 4 * 0.0135), cfg)
    wild = size(pf, 100.0, 100 * (1 - 4 * 0.0431), cfg)
    assert wild.risk_amount > calm.risk_amount * 2


def test_the_default_slot_size_comes_from_the_exposure_cap(settings):
    """Derived rather than set, so the two modes run at comparable exposure and
    the comparison is about weighting rather than about leverage."""
    cfg = replace(settings, sizing_mode="notional", whole_shares=False)
    pf = Portfolio(cfg)
    sizing = size(pf, 100.0, 90.0, cfg)
    expected = cfg.max_exposure_pct / cfg.max_concurrent
    assert sizing.qty * 100.0 / cfg.initial_capital == pytest.approx(expected, rel=1e-3)


def test_the_slot_size_can_be_set_explicitly(settings):
    cfg = replace(
        settings, sizing_mode="notional", whole_shares=False,
        notional_per_slot=0.05, max_position_pct=0.10,
    )
    pf = Portfolio(cfg)
    sizing = size(pf, 100.0, 90.0, cfg)
    assert sizing.qty * 100.0 / cfg.initial_capital == pytest.approx(0.05, rel=1e-3)


def test_notional_sizing_still_obeys_every_cap(settings):
    cfg = replace(
        settings, sizing_mode="notional", whole_shares=False,
        notional_per_slot=0.5, max_position_pct=0.02,
    )
    pf = Portfolio(cfg)
    sizing = size(pf, 100.0, 90.0, cfg)
    assert "max position" in sizing.reason
    assert sizing.qty * 100.0 <= cfg.initial_capital * 0.02 * 1.001


def test_notional_sizing_still_refuses_a_stop_above_the_entry(settings):
    """The stop is kept for the exit. Removing it from the size calculation
    does not make a nonsensical one acceptable."""
    cfg = replace(settings, sizing_mode="notional")
    assert not size(Portfolio(cfg), 100.0, 105.0, cfg).ok


# --- whether a budget can express a position at all -------------------------
#
# A share is indivisible and some of them cost a thousand dollars, so an
# account can be too small to open anything — while reading every signal
# correctly and looking perfectly healthy. Crypto never faces this.


def test_the_minimum_capital_is_where_the_two_caps_cross(settings):
    """A position is capped at `max_position_pct` of equity and refused below
    `min_notional`. Below their crossing point nothing is expressible."""
    assert settings.min_capital == settings.min_notional / settings.max_position_pct


def test_a_thousand_euro_account_cannot_open_anything_on_the_defaults(settings):
    from trader.report import can_enter

    cap = 1000 * settings.max_position_pct
    assert not can_enter(50.0, cap, settings)
    assert not can_enter(500.0, cap, settings)


def test_fractional_shares_make_the_share_price_irrelevant(settings):
    """Which is exactly what a small account needs: the question stops being
    'does a whole number of shares fit' and becomes 'is the window open'."""
    from dataclasses import replace

    from trader.report import can_enter

    small = replace(settings, whole_shares=False, min_notional=5.0)
    cap = 1000 * small.max_position_pct
    assert can_enter(1_200.0, cap, small)
    assert can_enter(22.0, cap, small)


def test_whole_shares_need_a_multiple_inside_the_window(settings):
    """Not just one affordable share: a whole number of them has to land above
    the broker minimum and below the cap at the same time."""
    from dataclasses import replace

    from trader.report import can_enter

    cfg = replace(settings, min_notional=500.0, whole_shares=True)
    assert can_enter(600.0, 800.0, cfg), "one share at 600 sits in [500, 800]"
    assert not can_enter(900.0, 800.0, cfg), "one share is already over the cap"
    assert not can_enter(400.0, 450.0, cfg), "one is under the minimum, two over the cap"
    assert can_enter(300.0, 700.0, cfg), "two shares at 300 sit in [500, 700]"


def test_the_budget_check_names_what_cannot_be_reached(settings, tmp_path):
    from dataclasses import replace

    from conftest import bars_from_closes

    from trader.report import budget_check
    from trader.store import Store

    cfg = replace(settings, universe=("CHEAP", "DEAR"), benchmark="", market_anchor="")
    with Store(tmp_path / "b.db") as store:
        store.save_bars("CHEAP", cfg.interval, bars_from_closes([20.0] * 5))
        store.save_bars("DEAR", cfg.interval, bars_from_closes([4_000.0] * 5))

        check = budget_check(store, cfg, 100_000)
        assert check.reachable == ["CHEAP", "DEAR"] and not check.unreachable
        assert check.report() == "", "nothing to warn about"

        check = budget_check(store, cfg, 20_000)
        assert check.reachable == ["CHEAP"]
        assert [s for s, _ in check.unreachable] == ["DEAR"]
        assert "cannot be entered" in check.report()

        check = budget_check(store, cfg, 1_000)
        assert not check.viable
        assert "cannot open a single position" in check.report()


# --- the flat per-order fee -------------------------------------------------


def test_the_flat_fee_is_off_by_default(settings):
    """Every figure in the README was measured without it, and they have to
    stay reproducible."""
    assert settings.fee_per_order == 0.0


def test_a_flat_fee_is_charged_on_each_side(settings):
    from dataclasses import replace

    from trader.portfolio import Portfolio

    cfg = replace(settings, fee_per_order=1.0, fee_rate=0.0, slippage=0.0)
    book = Portfolio(cfg, cash=1000.0)
    book.buy("AAA", 10.0, 10.0, 0, stop=9.0, atr=1.0)
    assert book.cash == pytest.approx(1000.0 - 100.0 - 1.0)

    trade = book.sell("AAA", 10.0, 1, "test")
    assert book.cash == pytest.approx(899.0 + 100.0 - 1.0)
    assert trade.fees == pytest.approx(2.0), "one euro in, one euro out"


def test_a_scale_out_and_its_close_book_one_entry_fee(settings):
    """The invariant `reduce` already kept for the proportional fee: a partial
    exit plus the later close must cost exactly what one full exit would."""
    from dataclasses import replace

    from trader.portfolio import Portfolio

    cfg = replace(settings, fee_per_order=1.0, fee_rate=0.0, slippage=0.0)
    book = Portfolio(cfg, cash=1000.0)
    book.buy("AAA", 10.0, 10.0, 0, stop=9.0, atr=1.0)
    first = book.reduce("AAA", 0.5, 10.0, 1, "scale out")
    second = book.sell("AAA", 10.0, 2, "close")

    entry_fees = first.fees + second.fees - 2.0  # two exits, one euro each
    assert entry_fees == pytest.approx(1.0), "the entry is charged once in total"
