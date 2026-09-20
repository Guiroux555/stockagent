"""The earnings calendar: parsing it, dating it, and refusing to know too much.

The tests that matter most here are the point-in-time ones. An earnings date is
usable in a backtest precisely because it was announced weeks ahead — and that
argument collapses the moment the code lets a backtest see a filing that had
not happened yet.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trader.config import Settings
from trader.events import (
    EarningsDate,
    EventWindow,
    _effect_date,
    _rows,
    known_at,
    passes,
    project_next,
    window_for,
)
from trader.store import Store


def quarterly(symbol="AAPL", start_year=2020, count=8, day=28):
    """Eight quarterly releases, every 91 days, from a fixed anchor."""
    from datetime import date, timedelta

    first = date(start_year, 1, day)
    return [
        EarningsDate(
            symbol=symbol,
            event_date=(first + timedelta(days=91 * k)).isoformat(),
            filed_date=(first + timedelta(days=91 * k - 1)).isoformat(),
            accepted_ts=0,
            status="confirmed",
        )
        for k in range(count)
    ]


# --- dating the session a release can actually move -------------------------


def test_a_release_after_the_bell_lands_on_the_next_session():
    """A release accepted at 16:30 New York time cannot move a market that
    shut at 16:00. Dating it by the filing day puts the blackout one day early
    and leaves the real day open."""
    assert _effect_date("2026-01-29T21:30:00.000Z", "2026-01-29") == "2026-01-30"


def test_a_release_before_the_open_moves_that_same_session():
    assert _effect_date("2026-01-29T12:00:00.000Z", "2026-01-29") == "2026-01-29"


def test_an_unparseable_timestamp_falls_back_to_the_filing_date():
    assert _effect_date("", "2026-01-29") == "2026-01-29"


# --- parsing ----------------------------------------------------------------


def test_only_item_2_02_is_an_earnings_release():
    """An 8-K without it is a different event — a director leaving, a covenant
    waiver — and blocking on those would block the wrong days."""
    block = {
        "form": ["8-K", "8-K", "10-Q", "8-K"],
        "items": ["2.02,9.01", "5.02", "", "7.01"],
        "filingDate": ["2026-01-29", "2026-02-02", "2026-02-03", "2026-02-04"],
        "acceptanceDateTime": ["2026-01-29T21:30:00.000Z"] * 4,
    }
    rows = _rows(block, "AAPL", now_ms=1)
    assert [r.filed_date for r in rows] == ["2026-01-29"]
    assert rows[0].status == "confirmed"


def test_a_row_without_a_filing_date_is_dropped():
    block = {"form": ["8-K"], "items": ["2.02"], "filingDate": [""],
             "acceptanceDateTime": [""]}
    assert _rows(block, "AAPL", now_ms=1) == []


# --- point in time ----------------------------------------------------------


def test_a_backtest_never_sees_a_filing_that_has_not_happened():
    """The whole justification for using this data is that it was known at the
    time. A confirmed future date is exactly the thing that breaks it."""
    history = quarterly(count=8)
    as_of = history[3].event_date
    seen = known_at(history, as_of)
    assert all(e.event_date <= as_of for e in seen if e.confirmed)
    assert history[4].event_date not in [e.event_date for e in seen if e.confirmed]


def test_the_next_release_is_projected_from_the_company_s_own_past():
    """What a human does with the same information: the same fiscal quarter
    lands within days of the same date a year later."""
    history = quarterly(count=8)
    nxt = project_next(history, history[-1].event_date)
    assert nxt is not None and nxt.status == "estimated"
    assert nxt.event_date > history[-1].event_date


def test_a_projection_needs_a_year_of_history():
    assert project_next(quarterly(count=3), "2020-06-01") is None


def test_a_projection_lands_on_the_same_weekday():
    """364 days, not 365: companies report on a weekday, and a projection that
    drifts onto a Saturday blocks nothing."""
    from datetime import date

    history = quarterly(count=8)
    nxt = project_next(history, history[-1].event_date)
    anchor = date.fromisoformat(history[-4].event_date)
    assert date.fromisoformat(nxt.event_date).weekday() == anchor.weekday()


def test_a_projection_too_far_out_is_not_reported():
    history = quarterly(count=8)
    assert known_at(history, history[-1].event_date, horizon_days=1) == [
        e for e in history if e.confirmed
    ]


# --- the window -------------------------------------------------------------


SESSIONS = [f"2026-03-{d:02d}" for d in range(1, 29)]


def blackout(settings, event_day, index, status="confirmed"):
    event = EarningsDate("AAPL", event_day, event_day, 0, status)
    return window_for("AAPL", SESSIONS, index, [event], settings)


def test_a_session_far_from_any_release_is_clear(settings):
    assert not blackout(settings, "2026-03-20", SESSIONS.index("2026-03-05")).inside


def test_the_sessions_before_a_release_are_inside(settings):
    w = blackout(settings, "2026-03-20", SESSIONS.index("2026-03-19"))
    assert w.inside and w.sessions_until == 1


def test_the_release_session_itself_is_inside(settings):
    w = blackout(settings, "2026-03-20", SESSIONS.index("2026-03-20"))
    assert w.inside and w.sessions_until == 0


def test_the_session_after_a_release_is_inside(settings):
    w = blackout(settings, "2026-03-20", SESSIONS.index("2026-03-21"))
    assert w.inside and w.sessions_since == 1


def test_an_estimated_date_opens_a_wider_window(settings):
    """A wrong date is worse than no date: it blocks the safe session and
    leaves the dangerous one open. An estimate buys width, not confidence."""
    index = SESSIONS.index("2026-03-17")
    assert not blackout(settings, "2026-03-20", index).inside
    assert blackout(settings, "2026-03-20", index, status="estimated").inside


def test_distances_are_counted_in_sessions_not_days(settings):
    """A three-session window that quietly becomes a one-session window over a
    long weekend is not a window."""
    sessions = ["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]
    event = EarningsDate("AAPL", "2026-03-10", "2026-03-09", 0, "confirmed")
    w = window_for("AAPL", sessions, 1, [event], settings)  # Friday
    assert w.sessions_until == 2


def test_a_release_dated_on_a_holiday_lands_on_the_next_session(settings):
    sessions = ["2026-03-05", "2026-03-06", "2026-03-09"]
    event = EarningsDate("AAPL", "2026-03-07", "2026-03-06", 0, "confirmed")
    w = window_for("AAPL", sessions, 2, [event], settings)
    assert w.inside


# --- the gate ---------------------------------------------------------------


def test_the_gate_is_off_by_default():
    assert Settings().earnings_mode == "off"


def test_an_off_gate_lets_everything_through(settings):
    inside = EventWindow("AAPL", sessions_until=0, status="confirmed")
    assert passes(inside, settings) == (True, 1.0, "")


def test_block_mode_refuses_the_entry(settings):
    cfg = replace(settings, earnings_mode="block")
    ok, size, why = passes(EventWindow("AAPL", sessions_until=1), cfg)
    assert not ok and size == 0.0 and "earnings in 1 session" in why


def test_reduce_mode_keeps_the_trade_and_shrinks_it(settings):
    """More surgical than refusing: the breakout is still a breakout, it is the
    unbounded gap that is the problem, and a smaller position bounds it."""
    cfg = replace(settings, earnings_mode="reduce")
    ok, size, why = passes(EventWindow("AAPL", sessions_until=1), cfg)
    assert ok and size == cfg.earnings_size_factor and "halved" in why


def test_a_clear_session_is_never_reduced(settings):
    cfg = replace(settings, earnings_mode="reduce")
    assert passes(EventWindow("AAPL"), cfg) == (True, 1.0, "")


# --- the store --------------------------------------------------------------


def test_the_calendar_round_trips():
    store = Store(":memory:")
    rows = quarterly(count=4)
    assert store.save_earnings(rows) == 4
    assert store.save_earnings(rows) == 0, "a filing date is not restated"
    back = store.load_earnings("AAPL")
    assert [e.event_date for e in back] == [e.event_date for e in rows]
    store.close()


def test_resetting_the_account_keeps_the_calendar():
    store = Store(":memory:")
    store.save_earnings(quarterly(count=4))
    store.reset_trading_state()
    assert len(store.load_earnings()) == 4
    store.close()


def test_coverage_flags_a_calendar_too_thin_for_its_price_history(settings):
    """A hole here is not cosmetic: it is a session the filter believes is
    safe. Exxon arrived with one release against twenty-one years of prices."""
    from conftest import bars_from_closes, trending_closes

    from trader.events import coverage

    cfg = replace(settings, universe=("AAA", "BBB"))
    store = Store(":memory:")
    for sym in cfg.universe:
        store.save_bars(sym, cfg.interval, bars_from_closes(trending_closes(n=800)))
    store.save_earnings(quarterly(symbol="AAA", count=8))
    store.save_earnings([quarterly(symbol="BBB", count=1)[0]])

    rows = {c.symbol: c for c in coverage(store, cfg)}
    store.close()
    assert rows["AAA"].ratio > rows["BBB"].ratio
    assert not rows["BBB"].usable(cfg)


@pytest.mark.parametrize("mode", ["off", "block", "reduce"])
def test_every_mode_is_a_known_one(settings, mode):
    cfg = replace(settings, earnings_mode=mode)
    allowed, size, _ = passes(EventWindow("AAPL", sessions_until=0), cfg)
    assert isinstance(allowed, bool) and 0.0 <= size <= 1.0
