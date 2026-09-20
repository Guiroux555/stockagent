"""Core value types shared across the agent.

Everything here is a plain dataclass: no I/O, no behaviour that depends on
wall-clock time, so the strategy and risk layers stay trivially testable.

One thing is equity-specific and shapes the whole codebase: a *session* is not
a fixed slice of clock time. Crypto candles tile the calendar at a constant
interval; trading sessions do not — they skip nights, weekends and holidays,
and they are occasionally half-days. So nothing here does arithmetic on
timestamps to find "the next bar". The sequence of sessions is whatever the
data says it is, and code that needs the next one asks the series, never the
clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

Action = Literal["BUY", "SELL", "HOLD"]

EXCHANGE_TZ = ZoneInfo("America/New_York")
"""US exchange local time. Used only to name a session by its calendar date;
no rule in the agent depends on a wall-clock hour."""


def utc(ms: int) -> datetime:
    """Timestamps are stored as epoch milliseconds, always UTC."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def session_date(ms: int) -> str:
    """The exchange-local calendar date of a session that opens at `ms`.

    A session opening 13:30 UTC and one opening 14:30 UTC six months later are
    the same 9:30 local open on either side of daylight saving. Naming days by
    UTC date would split that, and worse, would put a late-evening UTC bar on
    the wrong day.
    """
    return utc(ms).astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Bar:
    """One completed trading session, OHLCV.

    Prices are adjusted for splits *and* dividends — see `data.py`. The series
    is therefore a total-return series: holding it pays the dividends without
    the portfolio having to model a cash distribution.
    """

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @property
    def opened_at(self) -> datetime:
        return utc(self.open_time)

    @property
    def closed_at(self) -> datetime:
        return utc(self.close_time)

    @property
    def session(self) -> str:
        return session_date(self.open_time)


@dataclass
class Position:
    """An open long. The agent is cash-equity only: no shorts, no margin."""

    symbol: str
    qty: float
    entry_price: float
    entry_time: int
    stop: float
    peak: float
    atr_at_entry: float
    scaled_out: bool = False
    """True once part of the position has been banked at the first target.
    The remainder runs on the trailing stop alone, and is never scaled again."""
    initial_risk: float = 0.0
    """Distance from entry to the *original* stop, frozen at entry.

    It must not be recomputed from `stop`: the trailing stop rises over the
    life of the trade, so `entry - stop` shrinks toward zero and then goes
    negative, which would make every R-multiple meaningless.
    """
    bars_held: int = 0

    def notional(self, price: float) -> float:
        return self.qty * price

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.qty

    def r_multiple(self, price: float) -> float:
        """Profit measured in units of the risk taken at entry, the only scale
        that lets trades of different sizes be compared."""
        risk = self.initial_risk or (self.entry_price - self.stop)
        if risk <= 0:
            return 0.0
        return (price - self.entry_price) / risk


@dataclass
class Trade:
    """A round trip, recorded once the position is fully closed."""

    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    entry_time: int
    exit_time: int
    fees: float
    pnl: float
    reason: str
    partial: bool = False
    """A scale-out rather than a full close. Several trades can therefore share
    one entry, which is why the report groups them into round trips before
    computing a win rate."""

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.qty
        return self.pnl / cost if cost else 0.0


@dataclass
class Signal:
    """What the strategy thinks about one symbol at one session close."""

    symbol: str
    action: Action
    reason: str
    price: float
    atr: float = 0.0
    stop: float = 0.0
    strength: float = 0.0


@dataclass
class Order:
    """An instruction queued at a session close, to be filled at the *next*
    session's open.

    This type is the whole reason the equity agent is not a copy of the crypto
    one. A 24/7 market lets an agent see a close and trade at it; an equity
    market does not — by the time the closing print exists, the book is shut.
    Filling at the close you just made the decision on is the most common way
    an equity backtest invents money, so the decision and the fill are
    separated here by construction rather than by discipline.
    """

    symbol: str
    side: Action
    reason: str
    signal_price: float
    """The close the decision was made on — kept so the fill can be compared
    with it and cancelled if the market gapped away overnight."""
    stop: float = 0.0
    atr: float = 0.0
    created_ts: int = 0

    @property
    def created_session(self) -> str:
        return session_date(self.created_ts)


@dataclass
class Decision:
    """An audit-log row. HOLDs are recorded too: knowing why the agent stood
    still is as important as knowing why it traded."""

    ts: int
    symbol: str
    action: Action
    reason: str
    price: float
    qty: float = 0.0
    equity: float = 0.0
    meta: dict = field(default_factory=dict)
