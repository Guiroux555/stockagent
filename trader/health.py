"""Is the agent actually working? Three questions it has to be able to answer.

This file exists because of where the agent now lives. On a laptop, "did it
run?" is answered by looking at the terminal. On a Compute Module in a
cupboard, with no screen and an internet connection that comes and goes, the
agent has to be able to say so itself — and, more importantly, to *refuse to
trade* when the answer is no.

Three failures matter, and only the first is obvious:

1. **No data.** The link is down, the cache is stale, and the newest session
   the agent can see closed a week ago. Deciding on it means queuing an order
   for an open that has already happened.
2. **No clock.** A Compute Module has no battery-backed RTC. Cut the power and
   it comes back believing it is whenever it last shut down — or 1970. The
   whole schedule here is "the next weekday closing bell, exchange-local", and
   `drop_unclosed` compares a session against `now`, so a wrong clock is not a
   cosmetic problem: it silently discards good data, or accepts a session that
   is still trading.
3. **No database.** A power cut mid-write on an SD card can leave a file that
   opens and then fails on the first read.

The staleness limit is in *calendar* days rather than sessions, and that is the
one place this file differs from its crypto sibling in more than wording. The
market is shut two thirds of the week: at 16:05 on a Monday the newest close is
three days old and everything is fine, so a limit of "one bar" would suspend
the agent every weekend. Four days is a long weekend plus a holiday, and still
far shorter than any outage worth worrying about.

Everything here is a pure read. Nothing in this module changes a decision; it
reports, and `agent.py` decides what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .store import Store

TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")
"""`systemd-timesyncd` touches this once NTP has answered. Its presence is the
authoritative answer on a systemd host; its absence is not, because the box may
use chrony, or not be a systemd box at all — hence the fallback below."""

CLOCK_FLOOR = datetime(2026, 1, 1, tzinfo=timezone.utc)
"""A date this code is known to postdate.

An unsynchronised Pi does not report a subtly wrong time, it reports a wildly
wrong one — the epoch, or the moment the power went. Either is years off, so a
crude floor catches it, and a crude floor is also one that cannot misfire on a
clock that is merely a few seconds out."""


def newest_close(store: Store, settings: Settings) -> datetime | None:
    """When the most recent cached session closed, across the whole universe."""
    stamps = [
        t
        for t in (
            store.last_bar_time(symbol, settings.interval)
            for symbol in settings.data_universe
        )
        if t is not None
    ]
    if not stamps:
        return None
    # `last_bar_time` is a session open; the bell is the same day.
    return datetime.fromtimestamp(max(stamps) / 1000, timezone.utc)


def clock_floor(store: Store | None, settings: Settings | None) -> datetime:
    """The earliest instant it can honestly be right now.

    Sharpened with the cache when there is one: a session cannot open in the
    future, so the newest one on disk is a lower bound on the present that
    maintains itself, and one that keeps working long after the constant above
    has gone stale.
    """
    floor = CLOCK_FLOOR
    if store is None or settings is None:
        return floor
    newest = newest_close(store, settings)
    return max(floor, newest) if newest else floor


def clock_is_sane(
    store: Store | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Whether the wall clock can be trusted, and why."""
    now = now or datetime.now(timezone.utc)
    if TIMESYNC_FLAG.exists():
        return True, "NTP synchronised"
    floor = clock_floor(store, settings)
    if now < floor:
        return False, (
            f"clock reads {now:%Y-%m-%d %H:%M UTC}, before {floor:%Y-%m-%d %H:%M UTC}"
        )
    return True, "no NTP flag, but the clock is past every date on disk"


def data_age(
    store: Store, settings: Settings, now: datetime | None = None
) -> float | None:
    """Seconds since the newest cached session opened; `None` on an empty cache."""
    newest = newest_close(store, settings)
    if newest is None:
        return None
    return ((now or datetime.now(timezone.utc)) - newest).total_seconds()


def max_data_age(settings: Settings) -> float:
    """How old the cache may be before a decision taken on it is a fiction."""
    return settings.max_data_age_days * 86_400


def is_stale(
    store: Store, settings: Settings, now: datetime | None = None
) -> tuple[bool, float | None]:
    """Whether the newest session on disk is too old to decide on, and how old.

    A *negative* age counts as stale too, and that case is the whole reason
    this returns a pair rather than a boolean: it means the clock reads earlier
    than sessions already on disk, which no correct clock ever does. It is the
    unsynchronised board, and the danger there is the opposite of the obvious
    one — the data looks perfectly fresh against a present that has not
    happened yet.
    """
    age = data_age(store, settings, now)
    if age is None:
        return True, None
    return age > max_data_age(settings) or age < 0, age


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _human(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}min"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


@dataclass(frozen=True)
class Health:
    """One snapshot, everything a `systemctl status` cannot tell you."""

    now: datetime
    clock_ok: bool
    clock_note: str
    db_error: str | None
    age: float | None
    stale: bool
    last_tick: datetime | None
    next_run: datetime | None
    degraded_since: datetime | None
    positions: int
    orders: int
    limit: float
    """The staleness threshold `age` was judged against, in seconds."""

    @property
    def ok(self) -> bool:
        return self.clock_ok and self.db_error is None and not self.stale

    @property
    def overdue(self) -> timedelta | None:
        """How late the next wake-up is. A running agent is never overdue by
        much; one whose loop has wedged is overdue by days."""
        if self.next_run is None or self.next_run > self.now:
            return None
        return self.now - self.next_run

    def report(self) -> str:
        lines = [
            f"  clock      {'ok  ' if self.clock_ok else 'BAD '} {self.clock_note}",
            f"  database   {'ok' if self.db_error is None else 'BAD ' + self.db_error}",
        ]
        if self.age is None:
            lines.append("  data       BAD  cache is empty — run `sync`")
        else:
            lines.append(
                f"  data       {'stale' if self.stale else 'ok   '} newest session "
                f"opened {_human(self.age)} ago (limit {_human(self.limit)})"
            )
        lines.append(
            "  last tick  "
            + (
                f"{self.last_tick:%Y-%m-%d %H:%M UTC} "
                f"({_human((self.now - self.last_tick).total_seconds())} ago)"
                if self.last_tick
                else "never"
            )
        )
        lines.append(
            "  next run   "
            + (f"{self.next_run:%Y-%m-%d %H:%M %Z}" if self.next_run else "unplanned")
            + (
                f"  — OVERDUE by {_human(self.overdue.total_seconds())}"
                if self.overdue
                else ""
            )
        )
        if self.degraded_since:
            lines.append(
                f"  degraded   since {self.degraded_since:%Y-%m-%d %H:%M UTC} "
                "(no usable data; entries suspended, stops still supervised)"
            )
        lines.append(f"  positions  {self.positions} open, {self.orders} queued")
        head = "healthy" if self.ok else "DEGRADED"
        return f"health: {head}\n" + "\n".join(lines)


def check(store: Store, settings: Settings, now: datetime | None = None) -> Health:
    now = now or datetime.now(timezone.utc)
    clock_ok, note = clock_is_sane(store, settings, now)
    stale, age = is_stale(store, settings, now)
    return Health(
        now=now,
        clock_ok=clock_ok,
        clock_note=note,
        db_error=store.integrity_check(),
        age=age,
        stale=stale,
        last_tick=_parse(store.get_state("last_tick")),
        next_run=_parse(store.get_state("next_run")),
        degraded_since=_parse(store.get_state("degraded_since")),
        positions=len(store.load_positions()),
        orders=len(store.load_orders()),
        limit=max_data_age(settings),
    )
