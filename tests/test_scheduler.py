"""When the agent wakes up, on a calendar with holes in it."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from trader.models import EXCHANGE_TZ
from trader.scheduler import build_plan, describe, next_session_close


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_the_next_wake_up_is_after_a_closing_bell(settings):
    nxt = next_session_close(at("2026-01-15T12:00:00+00:00"), settings)
    local = nxt.astimezone(EXCHANGE_TZ)
    assert (local.hour, local.minute) == (16, settings.settle_minutes)


def test_friday_evening_wakes_up_on_monday(settings):
    """Two thirds of the week has no price at all, which is the whole reason
    this module cannot be interval arithmetic."""
    nxt = next_session_close(at("2026-09-18T21:00:00+00:00"), settings)
    assert nxt.astimezone(EXCHANGE_TZ).weekday() == 0


def test_saturday_wakes_up_on_monday(settings):
    nxt = next_session_close(at("2026-09-19T12:00:00+00:00"), settings)
    assert nxt.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d") == "2026-09-21"


def test_the_schedule_does_not_drift_with_daylight_saving(settings):
    """Anchored to exchange-local time. Anchoring to UTC would put the agent an
    hour early or late for several weeks a year, twice a year, because the US
    and Europe do not change clocks on the same date."""
    winter = next_session_close(at("2026-01-15T12:00:00+00:00"), settings)
    summer = next_session_close(at("2026-07-15T12:00:00+00:00"), settings)
    assert winter.astimezone(timezone.utc).hour != summer.astimezone(timezone.utc).hour
    for when in (winter, summer):
        local = when.astimezone(EXCHANGE_TZ)
        assert (local.hour, local.minute) == (16, settings.settle_minutes)


def test_a_wake_up_is_always_in_the_future(settings):
    now = at("2026-09-21T20:25:00+00:00")  # just after Monday's settle
    assert next_session_close(now, settings) > now


def test_the_plan_names_what_it_is_waiting_for(settings):
    plan = build_plan(
        at("2026-09-21T12:00:00+00:00"), 3, 2, 1.4, 5, settings
    )
    assert "3 position(s)" in plan.reason
    assert "2 order(s)" in plan.reason
    assert "volatility" in plan.reason
    assert "5 name(s)" in plan.reason


def test_a_slower_cadence_is_stated_rather_than_implied(settings):
    weekly = replace(settings, decide_every_n_sessions=5)
    assert "every 5 closes" in describe(0, 0, 1.0, 0, weekly)
