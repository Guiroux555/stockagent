"""Technical indicators, in pure Python.

The corpus is a few thousand sessions per symbol, so a numpy dependency would
buy nothing. Every function returns a list the same length as its input, with
``None`` in the warm-up slots, so index ``i`` of the result always lines up with
session ``i``. That alignment is what keeps the strategy free of off-by-one
lookahead bugs.
"""

from __future__ import annotations

from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period must be positive")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first `period`
    values (the conventional seeding, and the one TradingView uses)."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Wilder's RSI. Needs `period + 1` closes to produce its first value."""
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    out = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Series:
    """Wilder's ATR: the volatility unit every stop and position size in this
    agent is expressed in."""
    tr = true_range(highs, lows, closes)
    out: Series = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def rolling_high(values: Sequence[float], period: int) -> Series:
    """Highest value over the `period` sessions *before* each index.

    Excluding the current bar is what makes this a breakout level rather than
    a running maximum: a close can only clear a high that was already set.

    Implemented with a monotonic deque — the naive slice-and-max is O(n*period)
    and, over tens of thousands of sessions, noticeably slows a backtest.
    """
    out: Series = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be positive")

    window: list[int] = []  # indices, values decreasing
    for i in range(len(values)):
        if i >= period:
            out[i] = values[window[0]]
        while window and values[window[-1]] <= values[i]:
            window.pop()
        window.append(i)
        if window[0] <= i - period:
            window.pop(0)
    return out


def crossed_above(fast: Series, slow: Series, i: int) -> bool:
    """True when `fast` closed above `slow` on bar `i` having been at or below
    it on bar `i - 1`."""
    if i < 1:
        return False
    a, b = fast[i], slow[i]
    pa, pb = fast[i - 1], slow[i - 1]
    if None in (a, b, pa, pb):
        return False
    return pa <= pb and a > b


def crossed_below(fast: Series, slow: Series, i: int) -> bool:
    if i < 1:
        return False
    a, b = fast[i], slow[i]
    pa, pb = fast[i - 1], slow[i - 1]
    if None in (a, b, pa, pb):
        return False
    return pa >= pb and a < b


def slope(series: Series, i: int, lookback: int) -> float | None:
    """Average change per session over `lookback` sessions ending at `i`."""
    j = i - lookback
    if j < 0 or i >= len(series):
        return None
    a, b = series[i], series[j]
    if a is None or b is None:
        return None
    return (a - b) / lookback


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile; `q` in [0, 1]."""
    if not values:
        raise ValueError("empty series")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac
