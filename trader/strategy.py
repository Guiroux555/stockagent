"""Signal generation: a long-only trend-following breakout system on daily bars.

The thesis, stated plainly so it can be falsified: large-cap equities trend,
and a medium-term horizon is paid for being long the ones already above their
long moving average, entering on a confirmed break of a recent high rather than
a dip of unknown depth, and leaving on a volatility-scaled trailing stop.

Everything is a pure function of the sessions up to index `i`. No function here
reads `i + 1`, which is what makes the backtest honest, and none of them touch
the account, which is what makes them testable.

The signal is computed on a *close*. It is never filled on that close — see
`engine.py`. Anything in this module that looks like a price is an input to a
decision, not a price the agent gets.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import indicators as ind
from .config import Settings
from .models import Bar, Position, Signal


@dataclass
class Analysis:
    """Every indicator series for one symbol, aligned session-for-session."""

    bars: list[Bar]
    closes: list[float]
    highs: list[float]
    lows: list[float]
    opens: list[float]
    ema_fast: ind.Series
    ema_slow: ind.Series
    ema_trend: ind.Series
    ema_regime: ind.Series
    rsi: ind.Series
    atr: ind.Series
    donchian: ind.Series
    """Highest high of the previous `breakout_bars` sessions — the level a
    close has to clear to be an entry."""

    def __len__(self) -> int:
        return len(self.bars)

    def ready(self, i: int) -> bool:
        """True when every series has a value at `i`."""
        if i < 0:
            i += len(self.bars)
        if i < 1 or i >= len(self.bars):
            return False
        return all(
            s[i] is not None
            for s in (
                self.ema_fast,
                self.ema_slow,
                self.ema_trend,
                self.ema_regime,
                self.rsi,
                self.atr,
                self.donchian,
            )
        )

    def atr_pct(self, i: int) -> float:
        a, c = self.atr[i], self.closes[i]
        return (a / c) if (a is not None and c) else 0.0


def analyze(bars: list[Bar], settings: Settings) -> Analysis:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    opens = [b.open for b in bars]
    return Analysis(
        bars=bars,
        closes=closes,
        highs=highs,
        lows=lows,
        opens=opens,
        ema_fast=ind.ema(closes, settings.fast_ema),
        ema_slow=ind.ema(closes, settings.slow_ema),
        ema_trend=ind.ema(closes, settings.trend_ema),
        ema_regime=ind.ema(closes, settings.regime_ema),
        rsi=ind.rsi(closes, settings.rsi_period),
        atr=ind.atr(highs, lows, closes, settings.atr_period),
        donchian=ind.rolling_high(highs, settings.breakout_bars),
    )


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def entry_signal(a: Analysis, settings: Settings, i: int = -1) -> Signal:
    """Decide whether to queue a long at the close of session `i`.

    Five gates, in the order they most often reject: regime, trend direction,
    volatility sanity, not-already-extended, and finally the trigger itself.
    """
    if i < 0:
        i += len(a.bars)
    symbol = ""
    price = a.closes[i] if 0 <= i < len(a.closes) else 0.0

    if not a.ready(i):
        return Signal(symbol, "HOLD", "warming up", price)

    close = a.closes[i]
    atr = a.atr[i]
    rsi_now = a.rsi[i]

    # 1. Regime — only ever long above the long-term average.
    if close <= a.ema_regime[i]:
        return Signal(symbol, "HOLD", "below regime EMA200", price, atr)

    # 2. Trend must actually be rising, not merely below price by accident.
    sl = ind.slope(a.ema_trend, i, settings.trend_slope_lookback)
    if sl is None or sl <= 0:
        return Signal(symbol, "HOLD", "EMA50 not rising", price, atr)

    # 3. Volatility sanity — a dead tape has no edge, a berserk one has no
    #    survivable stop distance.
    apct = a.atr_pct(i)
    if apct < settings.min_atr_pct:
        return Signal(symbol, "HOLD", f"volatility too low ({apct:.2%})", price, atr)
    if apct > settings.max_atr_pct:
        return Signal(symbol, "HOLD", f"volatility too high ({apct:.2%})", price, atr)

    # 4. Refuse to buy what has already run.
    if rsi_now >= settings.rsi_overbought:
        return Signal(symbol, "HOLD", f"overbought (RSI {rsi_now:.0f})", price, atr)

    # 5. Trigger: a break of the N-session high, with price still above the
    #    level it broke.
    broke, level = _broke_out_recently(a, settings, i)
    if not broke:
        top = a.donchian[i]
        gap = (top - close) / atr if atr else 0.0
        return Signal(
            symbol,
            "HOLD",
            f"no breakout ({gap:.1f} ATR below the {top:.2f} high)",
            price,
            atr,
        )

    stop = close - settings.stop_atr_mult * atr
    if stop <= 0:
        return Signal(symbol, "HOLD", "stop below zero", price, atr)

    return Signal(
        symbol=symbol,
        action="BUY",
        reason=(
            f"broke {settings.breakout_bars}-session high at {level:.2f}"
            f"; price>EMA200, EMA50 rising, RSI {rsi_now:.0f}"
        ),
        price=price,
        atr=atr,
        stop=stop,
        strength=_strength(a, settings, i, level),
    )


def _window_start(settings: Settings, i: int) -> int:
    return max(1, i - settings.trigger_window + 1)


def _broke_out_recently(
    a: Analysis, settings: Settings, i: int
) -> tuple[bool, float | None]:
    """Did a close clear the N-session high within the window, and is price
    still above the level it cleared?

    The second half is what keeps a stale signal honest. A breakout that has
    since been given back is not a late entry, it is a failed breakout, and
    buying it is buying the trap.
    """
    most_recent: float | None = None
    for j in range(_window_start(settings, i), i + 1):
        level = a.donchian[j]
        if level is None:
            continue
        if a.closes[j] > level and a.closes[i] > level:
            most_recent = level
    return most_recent is not None, most_recent


def _strength(a: Analysis, settings: Settings, i: int, level: float) -> float:
    """Rank competing buy signals in [0, 1].

    Distance above the trend line, measured in ATR, is the dominant term: it
    says "this trend is established" without rewarding raw price. The last term
    prefers a break the price has not yet run away from — entering nearer the
    level means the stop is nearer too.
    """
    atr = a.atr[i] or 0.0
    if atr <= 0:
        return 0.0
    above = _clamp((a.closes[i] - a.ema_trend[i]) / atr / 4.0)
    sl = ind.slope(a.ema_trend, i, settings.trend_slope_lookback) or 0.0
    steep = _clamp(sl / atr * 10.0)
    extension = _clamp((a.closes[i] - level) / atr)
    return round(0.55 * above + 0.30 * steep + 0.15 * (1.0 - extension), 4)


def stop_hit(pos: Position, bar: Bar) -> tuple[bool, float]:
    """Would this session have taken out the stop, and at what price?

    A stop is a *resting order*: it lives at the broker and can be filled on
    any session, including ones the agent slept through. That is the one thing
    an unattended agent gets for free, and modelling it correctly is what makes
    a low cadence survivable.

    An overnight gap through the stop fills at the open, not at the stop —
    assuming otherwise is the single most common way a backtest invents money,
    and in equities, where every session begins with a gap, it is not a corner
    case but the normal thing.
    """
    if bar.low > pos.stop:
        return False, 0.0
    return True, min(pos.stop, bar.open)


def exit_signal(a: Analysis, pos: Position, settings: Settings, i: int = -1) -> Signal:
    """Decide whether to queue a close of `pos` at the close of session `i`,
    stops excluded.

    The stop is checked separately against each session's low, before this runs.
    """
    if i < 0:
        i += len(a.bars)
    price = a.closes[i]
    atr = a.atr[i] if a.atr[i] is not None else 0.0

    if not a.ready(i):
        return Signal(pos.symbol, "HOLD", "warming up", price, atr)

    if settings.take_profit_r > 0 and pos.r_multiple(price) >= settings.take_profit_r:
        return Signal(
            pos.symbol,
            "SELL",
            f"target reached ({pos.r_multiple(price):.1f}R)",
            price,
            atr,
        )

    # Below the long average, the reason for being long is gone. This one
    # ignores the minimum hold: regime loss is not noise.
    if price < a.ema_regime[i]:
        return Signal(pos.symbol, "SELL", "lost regime (below EMA200)", price, atr)

    if pos.bars_held < settings.min_hold_bars:
        return Signal(pos.symbol, "HOLD", "min hold not elapsed", price, atr)

    if price < a.ema_trend[i] and a.ema_fast[i] < a.ema_slow[i]:
        return Signal(pos.symbol, "SELL", "trend broken (EMA50 + cross)", price, atr)

    return Signal(
        pos.symbol,
        "HOLD",
        f"holding, {pos.r_multiple(price):+.1f}R, stop {pos.stop:.2f}",
        price,
        atr,
    )


def should_scale_out(pos: Position, price: float, settings: Settings) -> bool:
    """Is this the moment to bank part of the position?

    Once only: the remainder runs on the trailing stop, which is what lets a
    trend pay for the many small losses around it.
    """
    return (
        settings.scale_out_r > 0
        and not pos.scaled_out
        and 0 < settings.scale_out_fraction < 1
        and pos.r_multiple(price) >= settings.scale_out_r
    )


def move_stop_to_breakeven(pos: Position, settings: Settings) -> float:
    """Lift the stop to the entry price, never lower it."""
    if settings.breakeven_after_scale:
        pos.stop = max(pos.stop, pos.entry_price)
    return pos.stop


def update_trailing_stop(
    pos: Position, bar: Bar, atr: float | None, settings: Settings
) -> float:
    """Chandelier trail: highest high since entry, less a multiple of ATR.

    Raise-only. A stop that can move down is not a stop.

    Moving it is an *order* — a cancel and a replace at the broker — so it only
    happens on a session the agent is awake for. An agent that decides once a
    week carries a week-old stop, and that cost shows up in the cadence table
    in the README rather than being quietly assumed away.
    """
    pos.peak = max(pos.peak, bar.high)
    if atr is None or atr <= 0:
        return pos.stop
    candidate = pos.peak - settings.trail_atr_mult * atr
    if candidate > pos.stop:
        pos.stop = candidate
    return pos.stop
