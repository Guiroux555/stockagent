"""The data layer, offline. Every payload here is a hand-built fixture: a test
suite that needs the internet is a test suite that fails for reasons unrelated
to the code.
"""

from __future__ import annotations

import pytest

from trader.data import _session_seconds, _to_bars, drop_unclosed

OPEN = 1_600_000_000  # a Friday, in seconds


def payload(
    closes, opens=None, adj=None, volumes=None, span=6.5 * 3600, stamps=None
):
    n = len(closes)
    opens = opens or closes
    return {
        "meta": {
            "currentTradingPeriod": {"regular": {"start": OPEN, "end": OPEN + span}}
        },
        "timestamp": stamps or [OPEN + i * 86400 for i in range(n)],
        "indicators": {
            "quote": [
                {
                    "open": opens,
                    "high": [c * 1.01 if c else None for c in closes],
                    "low": [c * 0.99 if c else None for c in closes],
                    "close": closes,
                    "volume": volumes or [1000] * n,
                }
            ],
            **({"adjclose": [{"adjclose": adj}]} if adj else {}),
        },
    }


def test_a_session_with_no_data_is_dropped_not_invented(settings):
    """Yahoo emits null for an exchange holiday it lists anyway. A bar that
    never traded has no high and no low, and inventing them would feed the stop
    a price that never existed."""
    bars = _to_bars(payload([100.0, None, 102.0]))
    assert len(bars) == 2
    assert [b.close for b in bars] == [100.0, 102.0]


def test_prices_are_adjusted_for_dividends(settings):
    """Raw OHLC is split-adjusted only, so a payment shows up as an overnight
    gap down — and a trailing stop would be hit by money the holder received."""
    bars = _to_bars(payload([100.0, 100.0], adj=[99.0, 100.0]))
    assert bars[0].close == pytest.approx(99.0)
    assert bars[1].close == pytest.approx(100.0)


def test_the_adjustment_scales_the_whole_session_not_just_the_close(settings):
    """Scaling the close alone would leave the high below it, which is not a
    session, it is a corrupt bar."""
    bars = _to_bars(payload([100.0], adj=[50.0]))
    b = bars[0]
    assert b.low <= b.open <= b.high and b.low <= b.close <= b.high
    assert b.high == pytest.approx(100.0 * 1.01 * 0.5)


def test_an_unadjusted_payload_is_left_alone(settings):
    bars = _to_bars(payload([100.0, 101.0]))
    assert [b.close for b in bars] == [100.0, 101.0]


def test_the_session_in_progress_is_discarded(settings):
    """Acting on a bar that has not closed means the signal repaints, and a
    backtest of a repainting strategy reports profits that cannot be earned."""
    bars = _to_bars(payload([100.0, 101.0]))
    closed = drop_unclosed(bars, at=(bars[1].close_time - 1) * 1)
    assert len(closed) == 1


def test_a_closed_session_is_kept(settings):
    bars = _to_bars(payload([100.0, 101.0]))
    assert len(drop_unclosed(bars, at=bars[-1].close_time)) == 2


def test_the_session_length_comes_from_the_payload(settings):
    assert _session_seconds(
        {"currentTradingPeriod": {"regular": {"start": 0, "end": 1234}}}
    ) == 1234


def test_a_payload_without_a_trading_period_falls_back(settings):
    assert _session_seconds({}) == pytest.approx(6.5 * 3600)


def test_bars_come_back_in_order(settings):
    bars = _to_bars(payload([100.0, 101.0, 102.0]))
    assert [b.open_time for b in bars] == sorted(b.open_time for b in bars)


def test_a_session_is_named_by_its_exchange_local_date(settings):
    """A 14:30 UTC open in September and a 13:30 UTC open in January are the
    same 9:30 local bell on either side of daylight saving, and both must be
    dated by the local day rather than the UTC one."""
    summer = _to_bars(payload([100.0], stamps=[1_789_738_200]))[0]
    winter = _to_bars(payload([100.0], stamps=[1_768_746_600]))[0]
    assert summer.session == "2026-09-18"
    assert winter.session == "2026-01-18"
