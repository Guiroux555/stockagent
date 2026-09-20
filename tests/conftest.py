"""Shared fixtures: synthetic session series with known shapes.

Building bars from a close series (rather than downloading real ones) keeps the
tests deterministic and offline — a test suite that needs the internet is a
test suite that fails for reasons unrelated to the code.

The synthetic calendar skips weekends, because a good deal of this codebase
exists precisely to cope with a timeline that has holes in it.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from trader.config import Settings
from trader.models import Bar

SESSION_SECONDS = int(6.5 * 3600)
FIRST_OPEN = datetime(2015, 1, 2, 14, 30, tzinfo=timezone.utc)
"""A Friday, 9:30 in New York."""


def session_opens(n: int, start: datetime = FIRST_OPEN) -> list[int]:
    """`n` consecutive weekday opens, as epoch milliseconds."""
    out: list[int] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(int(day.timestamp() * 1000))
        day += timedelta(days=1)
    return out


def bars_from_closes(
    closes: list[float],
    wick: float = 0.008,
    gap: float = 0.0,
    start: datetime = FIRST_OPEN,
) -> list[Bar]:
    """One bar per close. The open is the previous close, shifted by `gap`."""
    opens = session_opens(len(closes), start)
    out = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        o = prev * (1 + gap)
        out.append(
            Bar(
                open_time=opens[i],
                open=o,
                high=max(o, c) * (1 + wick),
                low=min(o, c) * (1 - wick),
                close=c,
                volume=1_000_000.0,
                close_time=opens[i] + SESSION_SECONDS * 1000,
            )
        )
    return out


def trending_closes(
    n: int = 700,
    drift: float = 0.0009,
    amp: float = 0.09,
    period: int = 90,
    noise: float = 0.008,
    start: float = 100.0,
    seed: int = 7,
) -> list[float]:
    """An uptrend with regular pullbacks plus seeded noise.

    The defaults are tuned so the pullbacks are deep enough to reset the
    Donchian channel — a smooth ramp would break out on the first bar and then
    never again, and would silently test nothing.
    """
    rnd = random.Random(seed)
    return [
        start
        * math.exp(drift * i)
        * (1 + amp * math.sin(2 * math.pi * i / period))
        * (1 + rnd.gauss(0, noise))
        for i in range(n)
    ]


def falling_closes(
    n: int = 600, drift: float = -0.002, start: float = 100.0
) -> list[float]:
    return [start * math.exp(drift * i) for i in range(n)]


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def uptrend() -> list[Bar]:
    return bars_from_closes(trending_closes())


@pytest.fixture
def downtrend() -> list[Bar]:
    return bars_from_closes(falling_closes())
