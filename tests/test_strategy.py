"""Entry and exit rules. Pure functions of closed sessions, so every test here
is a statement about the rules rather than about the account.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import bars_from_closes, falling_closes, trending_closes

from trader.models import Position
from trader.strategy import (
    analyze,
    entry_signal,
    exit_signal,
    should_scale_out,
    stop_hit,
    update_trailing_stop,
)


def test_a_downtrend_never_produces_a_buy(settings, downtrend):
    a = analyze(downtrend, settings)
    for i in range(settings.warmup_bars, len(a.bars)):
        assert entry_signal(a, settings, i).action != "BUY"


def test_an_uptrend_produces_buys(settings, uptrend):
    a = analyze(uptrend, settings)
    signals = [entry_signal(a, settings, i) for i in range(settings.warmup_bars, len(a.bars))]
    assert any(s.action == "BUY" for s in signals)


def test_the_warm_up_never_signals(settings, uptrend):
    a = analyze(uptrend, settings)
    for i in range(0, settings.regime_ema):
        assert entry_signal(a, settings, i).action == "HOLD"


def test_no_rule_reads_a_later_session(settings, uptrend):
    """Truncating the series must not change what was decided on the sessions
    that remain. This is the property the whole backtest rests on."""
    a_full = analyze(uptrend, settings)
    cut = len(uptrend) - 40
    a_cut = analyze(uptrend[:cut], settings)
    for i in range(settings.warmup_bars, cut):
        full = entry_signal(a_full, settings, i)
        trimmed = entry_signal(a_cut, settings, i)
        assert (full.action, full.reason) == (trimmed.action, trimmed.reason)


def test_the_trigger_needs_price_still_above_the_level_it_cleared(settings):
    """A breakout given back is not a late entry, it is a failed breakout."""
    closes = trending_closes(n=400)
    broke = analyze(bars_from_closes(closes), settings)
    given_back = analyze(bars_from_closes(closes[:-1] + [min(closes[-60:])]), settings)
    i = len(closes) - 1
    assert entry_signal(given_back, settings, i).action == "HOLD"
    assert broke is not given_back


def test_the_stop_is_a_fixed_multiple_of_atr_below_the_close(settings, uptrend):
    a = analyze(uptrend, settings)
    for i in range(settings.warmup_bars, len(a.bars)):
        sig = entry_signal(a, settings, i)
        if sig.action == "BUY":
            assert sig.stop == pytest.approx(
                a.closes[i] - settings.stop_atr_mult * a.atr[i]
            )
            return
    pytest.fail("no buy signal to check")


def test_an_overbought_name_is_refused(settings, uptrend):
    tight = replace(settings, rsi_overbought=1.0)
    a = analyze(uptrend, tight)
    for i in range(tight.warmup_bars, len(a.bars)):
        assert entry_signal(a, tight, i).action != "BUY"


# --- stops ------------------------------------------------------------------


def _pos(stop: float, entry: float = 100.0) -> Position:
    return Position(
        symbol="AAA", qty=10, entry_price=entry, entry_time=0, stop=stop,
        peak=entry, atr_at_entry=1.0, initial_risk=entry - stop,
    )


def test_a_stop_below_the_session_low_does_not_fire(settings):
    bar = bars_from_closes([100.0, 101.0])[-1]
    assert stop_hit(_pos(bar.low * 0.9), bar) == (False, 0.0)


def test_a_stop_inside_the_session_fills_at_the_stop(settings):
    bar = bars_from_closes([100.0, 101.0])[-1]
    hit, price = stop_hit(_pos(bar.low * 1.001), bar)
    assert hit and price == pytest.approx(bar.low * 1.001)


def test_a_stop_gapped_through_fills_at_the_open(settings):
    bar = bars_from_closes([100.0, 101.0])[-1]
    hit, price = stop_hit(_pos(bar.open * 1.2), bar)
    assert hit and price == pytest.approx(bar.open)


def test_the_trailing_stop_only_ever_rises(settings):
    bars = bars_from_closes(trending_closes(n=300))
    a = analyze(bars, settings)
    pos = _pos(stop=1.0, entry=a.closes[settings.warmup_bars])
    last = pos.stop
    for i in range(settings.warmup_bars, len(bars)):
        update_trailing_stop(pos, bars[i], a.atr[i], settings)
        assert pos.stop >= last
        last = pos.stop


def test_the_r_multiple_uses_the_risk_frozen_at_entry(settings):
    """Recomputing it from the live stop sends the denominator to zero as the
    trail rises, and then negative, which makes every target meaningless."""
    pos = _pos(stop=90.0)
    assert pos.r_multiple(110.0) == pytest.approx(1.0)
    pos.stop = 105.0
    assert pos.r_multiple(110.0) == pytest.approx(1.0)


# --- exits ------------------------------------------------------------------


def test_losing_the_regime_exits_regardless_of_the_minimum_hold(settings):
    bars = bars_from_closes(falling_closes(n=500))
    a = analyze(bars, settings)
    pos = _pos(stop=0.0, entry=a.closes[-1])
    pos.bars_held = 0
    sig = exit_signal(a, pos, settings, len(bars) - 1)
    assert sig.action == "SELL" and "regime" in sig.reason


def test_the_minimum_hold_blocks_a_trend_exit_but_the_regime_ignores_it(
    settings, uptrend
):
    """The two exits are not the same kind of event. A broken trend is a
    judgement the agent can afford to sit on for a few sessions; price below
    its 200-session average is not noise, and waiting it out is how a small
    loss becomes a large one."""
    a = analyze(uptrend, settings)
    held = replace(settings, min_hold_bars=10)

    trend_break = next(
        i
        for i in range(held.warmup_bars, len(uptrend))
        if a.closes[i] > a.ema_regime[i]
        and a.closes[i] < a.ema_trend[i]
        and a.ema_fast[i] < a.ema_slow[i]
    )
    pos = _pos(stop=0.0, entry=a.closes[trend_break])
    pos.bars_held = 0
    assert exit_signal(a, pos, held, trend_break).action == "HOLD"
    pos.bars_held = 50
    assert exit_signal(a, pos, held, trend_break).action == "SELL"


def test_scale_out_is_off_unless_configured(settings):
    pos = _pos(stop=90.0)
    assert not should_scale_out(pos, 200.0, settings)
    on = replace(settings, scale_out_r=2.0)
    assert should_scale_out(pos, 130.0, on)


def test_scale_out_happens_once(settings):
    on = replace(settings, scale_out_r=2.0)
    pos = _pos(stop=90.0)
    assert should_scale_out(pos, 130.0, on)
    pos.scaled_out = True
    assert not should_scale_out(pos, 200.0, on)
