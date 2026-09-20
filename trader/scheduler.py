"""When the agent wakes up.

The crypto version of this agent chose between one and five wake-ups a day and
spent a whole section of its README defending the floor. That question does not
survive the move to equities, and it is worth being explicit about why rather
than quietly shipping a different design:

* **A daily bar closes once.** Waking twice between two closing bells reads the
  same unchanged data twice. There is no honest cadence above one a session.
* **The market is shut most of the time.** Two thirds of the week has no price
  at all, so "every four hours" is not a schedule, it is a way of generating
  no-ops.

So the agent wakes once per session, shortly after the closing bell, and the
only real question is the opposite one: how *rarely* can it wake before the
results fall apart? Measured, the answer is "much more rarely than you would
think" — the backtest is flat from every session out to one session in ten, and
only breaks down at monthly. `decide_every_n_sessions` exists to let that be
configured, and the README carries the table.

Holidays are not hardcoded here. The agent wakes on the next weekday close; if
the exchange was shut, no new session appears in the data and no decision is
taken. A table of holidays would be one more thing to keep correct, and this is
self-correcting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import Settings
from .models import EXCHANGE_TZ

CLOSING_BELL = time(16, 0)
"""Regular US equity close, exchange-local. Half-days close at 13:00; the agent
simply waits until 16:00 on those, which costs it nothing because the data is
already final."""


@dataclass
class Plan:
    decide_every: int
    reason: str
    next_run: datetime
    """UTC instant of the next wake-up."""


def is_weekday(day: datetime) -> bool:
    return day.weekday() < 5


def next_session_close(now: datetime, settings: Settings) -> datetime:
    """The next weekday closing bell, plus the settle delay, in UTC.

    Anchored to exchange-local time rather than UTC, so the schedule does not
    drift by an hour twice a year when the US and Europe change clocks on
    different dates.
    """
    local = now.astimezone(EXCHANGE_TZ)
    for offset in range(0, 8):
        day = (local + timedelta(days=offset)).date()
        candidate = datetime.combine(day, CLOSING_BELL, tzinfo=EXCHANGE_TZ) + timedelta(
            minutes=settings.settle_minutes
        )
        if is_weekday(candidate) and candidate > local:
            return candidate.astimezone(now.tzinfo or candidate.tzinfo)
    raise RuntimeError("no weekday found in the next eight days")  # pragma: no cover


def describe(
    open_positions: int,
    pending_orders: int,
    vol_ratio: float,
    near_trigger: int,
    settings: Settings,
) -> str:
    """What the agent is watching for, in words, for the log and the status
    report. It does not change the schedule — on daily bars there is nothing to
    change it to — it explains what the next wake-up is expected to deal with.
    """
    notes = []
    if settings.decide_every_n_sessions == 1:
        notes.append("reads every close")
    else:
        notes.append(f"reads every {settings.decide_every_n_sessions} closes")
    if pending_orders:
        notes.append(f"{pending_orders} order(s) resting for the open")
    if open_positions:
        notes.append(f"{open_positions} position(s) on a stop")
    if vol_ratio >= 1.25:
        notes.append(f"volatility {vol_ratio:.2f}x normal")
    if near_trigger:
        notes.append(f"{near_trigger} name(s) within half an ATR of a breakout")
    return "; ".join(notes)


def build_plan(
    now: datetime,
    open_positions: int,
    pending_orders: int,
    vol_ratio: float,
    near_trigger: int,
    settings: Settings,
) -> Plan:
    return Plan(
        decide_every=settings.decide_every_n_sessions,
        reason=describe(
            open_positions, pending_orders, vol_ratio, near_trigger, settings
        ),
        next_run=next_session_close(now, settings),
    )
