"""The virtual account: costs, whole shares, and the arithmetic of a partial
exit."""

from __future__ import annotations

from dataclasses import replace

import pytest

from trader.portfolio import InsufficientFunds, Portfolio


def test_a_round_trip_at_a_flat_price_loses_exactly_the_costs(settings):
    pf = Portfolio(settings)
    pf.buy("AAPL", 100, 100.0, ts=1, stop=90.0, atr=2.0)
    trade = pf.sell("AAPL", 100.0, ts=2, reason="flat")
    expected = 100 * 100.0 * (2 * settings.slippage + 2 * settings.fee_rate)
    assert trade.pnl == pytest.approx(-expected, rel=1e-3)
    assert trade.pnl < 0, "a costless round trip is a backtest telling lies"


def test_the_buy_pays_slippage_up_and_the_sell_takes_it_down(settings):
    pf = Portfolio(settings)
    assert pf.buy_fill_price(100.0) > 100.0
    assert pf.sell_fill_price(100.0) < 100.0


def test_buying_more_than_the_cash_covers_is_refused(settings):
    pf = Portfolio(settings, cash=1000.0)
    with pytest.raises(InsufficientFunds):
        pf.buy("AAPL", 1000, 100.0, ts=1, stop=90.0, atr=1.0)


def test_the_agent_never_averages_into_a_name_it_already_holds(settings):
    pf = Portfolio(settings)
    pf.buy("AAPL", 10, 100.0, ts=1, stop=90.0, atr=1.0)
    with pytest.raises(ValueError):
        pf.buy("AAPL", 10, 105.0, ts=2, stop=95.0, atr=1.0)


def test_the_initial_risk_is_frozen_at_the_fill_not_the_signal(settings):
    pf = Portfolio(settings)
    pf.buy("AAPL", 10, 100.0, ts=1, stop=90.0, atr=1.0)
    pos = pf.positions["AAPL"]
    assert pos.initial_risk == pytest.approx(pos.entry_price - 90.0)
    assert pos.entry_price > 100.0


def test_a_scale_out_and_the_close_pay_the_same_fees_as_one_exit(settings):
    """Otherwise scaling out would look cheaper than it is, and the comparison
    with not scaling would be rigged."""
    cfg = replace(settings, whole_shares=False)
    whole = Portfolio(cfg)
    whole.buy("AAPL", 100, 100.0, ts=1, stop=90.0, atr=1.0)
    one_exit = whole.sell("AAPL", 120.0, ts=2, reason="x")

    split = Portfolio(cfg)
    split.buy("AAPL", 100, 100.0, ts=1, stop=90.0, atr=1.0)
    first = split.reduce("AAPL", 0.5, 120.0, ts=2, reason="half")
    second = split.sell("AAPL", 120.0, ts=3, reason="rest")

    assert first.fees + second.fees == pytest.approx(one_exit.fees)
    assert first.pnl + second.pnl == pytest.approx(one_exit.pnl)


def test_a_partial_exit_leaves_the_entry_price_and_risk_alone(settings):
    cfg = replace(settings, whole_shares=False)
    pf = Portfolio(cfg)
    pf.buy("AAPL", 100, 100.0, ts=1, stop=90.0, atr=1.0)
    before = (pf.positions["AAPL"].entry_price, pf.positions["AAPL"].initial_risk)
    pf.reduce("AAPL", 0.5, 130.0, ts=2, reason="half")
    after = (pf.positions["AAPL"].entry_price, pf.positions["AAPL"].initial_risk)
    assert before == after
    assert pf.positions["AAPL"].scaled_out


def test_half_of_seven_shares_is_not_three_and_a_half(settings):
    """With whole shares on, a fractional slice has to be resolved somehow, and
    silently selling 3.5 shares is the one answer a broker would reject."""
    pf = Portfolio(settings)
    pf.buy("AAPL", 7, 100.0, ts=1, stop=90.0, atr=1.0)
    trade = pf.reduce("AAPL", 0.5, 130.0, ts=2, reason="half")
    assert trade.qty == int(trade.qty)
    remaining = pf.positions.get("AAPL")
    assert remaining is None or remaining.qty == int(remaining.qty)


def test_equity_is_cash_plus_what_the_positions_are_worth(settings):
    pf = Portfolio(settings, cash=10_000.0)
    pf.buy("AAPL", 10, 100.0, ts=1, stop=90.0, atr=1.0)
    assert pf.equity({"AAPL": 150.0}) == pytest.approx(pf.cash + 1500.0)
