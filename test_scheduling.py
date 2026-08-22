"""Tests for the scheduling policy.

These import nothing but the policy module: no Flask app, no globals, no clock
patching. Every case plans for a fixed instant, which is also what makes it
possible to replay stored history through the planner.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

import scheduling

TZ = ZoneInfo("Europe/Helsinki")
NOON = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=TZ)

# A Finnish day in cents/kWh: cheap night trough, morning ramp, evening peak
DAY = [
    16.0,
    11.5,
    10.8,
    10.7,
    10.6,
    10.4,
    12.9,
    14.2,
    16.0,
    15.3,
    14.6,
    14.2,
    15.0,
    15.1,
    16.4,
    20.1,
    18.6,
    21.2,
    23.6,
    25.4,
    25.9,
    25.1,
    22.6,
    16.6,
]


def policy(**overrides):
    """Build a policy with the production defaults and a steady 15°C outside."""
    settings = {
        "temp_high": 37.0,
        "temp_min": 34.0,
        "max_hours": 6,
        "deadband": 1.0,
        "cooling_k": 0.004,
        "heating_rate": 1.6,
        "outside_at": lambda _hours: 15.0,
    }
    return scheduling.Policy(**{**settings, **overrides})


MIDNIGHT = NOON.replace(hour=0)


def prices_from(start, hours, shape=None):
    """Hourly prices (EUR/kWh) starting at `start`, repeating `shape`."""
    shape = DAY if shape is None else shape
    return {
        (start + datetime.timedelta(hours=i)).isoformat(): shape[i % len(shape)] / 100
        for i in range(hours)
    }


def hour(offset_from_midnight):
    """ISO key of an hour, counted from midnight on the day of NOON."""
    return (MIDNIGHT + datetime.timedelta(hours=offset_from_midnight)).isoformat()


def reading(when, current, desired=10.0):
    """One temperature reading as the app records it."""
    return {
        "time": when.astimezone(datetime.UTC).isoformat(),
        "current_temp": current,
        "desired_temp": desired,
        "outside_temp": 15.0,
    }


def test_books_the_cheapest_hours_before_the_deadline():
    """At 34.5°C, TEMP_MIN is ~7h away, so tonight's trough is out of reach.

    The cheapest hours of the day are 04:00 and 05:00, but they have passed
    and the pool cannot wait for the next ones: it books the cheapest hours
    that fall inside the deadline instead.
    """
    prices = prices_from(MIDNIGHT, 48)
    result = scheduling.plan(prices, [reading(NOON, 34.5)], NOON, policy())

    assert result.deadline_hours == pytest.approx(7.0)
    assert result.hours_needed == 2
    assert result.hours == {hour(12), hour(13)}  # cheapest of 12:00-19:00


def test_waits_while_cheaper_hours_may_still_publish():
    """With only this evening priced, TEMP_MIN is not close enough to commit."""
    prices = prices_from(NOON, 12)
    result = scheduling.plan(prices, [reading(NOON, 35.5)], NOON, policy())

    assert result.hours == set()
    assert "waiting" in result.reason


def test_waiting_keeps_a_block_that_is_about_to_start():
    """Deferring must not cancel hours already booked."""
    prices = prices_from(NOON, 12)
    booked = {NOON.isoformat()}
    result = scheduling.plan(prices, [reading(NOON, 35.5)], NOON, policy(), booked)

    assert result.hours == booked


def test_a_sensor_tick_below_target_books_nothing():
    """0.2°C below target is inside the deadband at the hour it would heat.

    The cheapest hour here is the very next one, so the pool has no time to
    cool out of the deadband before it would start heating.
    """
    cheap_next_hour = [20.0] * 24
    cheap_next_hour[13] = 5.0
    prices = prices_from(MIDNIGHT, 48, shape=cheap_next_hour)
    result = scheduling.plan(prices, [reading(NOON, 36.8)], NOON, policy())

    assert result.hours == set()
    assert result.hours_needed == 0
    assert "deadband" in result.reason


def test_sizes_the_block_from_the_temperature_at_its_start():
    """Cooling before the block starts is part of what the block must undo.

    The pool is 0.8°C low at noon — one hour's worth at 1.6°C/h — but the
    cheapest hours are tomorrow's trough, 16h out, by which time it has cooled
    far enough to need two.
    """
    prices = prices_from(MIDNIGHT, 48)
    result = scheduling.plan(prices, [reading(NOON, 36.2)], NOON, policy())

    assert result.hours_needed == 2
    assert result.hours == {hour(28), hour(29)}  # 04:00 and 05:00 tomorrow


def test_respects_the_heating_budget():
    """Hours already heated in this 14:00-14:00 window count against the cap."""
    prices = prices_from(NOON.replace(hour=0), 48)
    history = [
        reading(NOON.replace(hour=h), 34.0, desired=37.0) for h in range(2, 7)
    ] + [reading(NOON, 33.0)]
    result = scheduling.plan(prices, history, NOON, policy(max_hours=6))

    assert result.budget_used == 5
    assert len(result.hours) == 1


def test_books_nothing_before_the_first_reading():
    """A restart must not guess a temperature and heat on it."""
    prices = prices_from(NOON, 48)
    result = scheduling.plan(prices, [], NOON, policy())

    assert result.hours == set()
    assert "no temperature reading" in result.reason


def test_ignores_hours_that_have_already_passed():
    """Only the current hour onwards can be booked."""
    prices = prices_from(NOON.replace(hour=0), 24)
    result = scheduling.plan(prices, [reading(NOON, 34.5)], NOON, policy())

    assert all(
        datetime.datetime.fromisoformat(h) >= NOON.replace(minute=0)
        for h in result.hours
    )


def test_plans_for_any_instant_not_just_now():
    """The same inputs at two different instants give two different plans.

    Nothing here touches the clock, which is what lets stored history be
    replayed through the planner to evaluate a policy change after the fact.
    """
    prices = prices_from(NOON.replace(hour=0), 48)
    history = [reading(NOON, 34.5)]

    at_noon = scheduling.plan(prices, history, NOON, policy())
    at_three_am = scheduling.plan(prices, history, NOON.replace(hour=3), policy())

    assert at_noon.hours != at_three_am.hours
    # At 03:00 the 04:00 and 05:00 trough is still ahead and still cheapest
    assert at_three_am.hours == {
        NOON.replace(hour=4).isoformat(),
        NOON.replace(hour=5).isoformat(),
    }


def test_falls_back_past_the_deadline_when_it_cannot_be_met():
    """A pool below TEMP_MIN can use any future hour, deadline or not."""
    prices = prices_from(NOON, 24)
    result = scheduling.plan(prices, [reading(NOON, 32.0)], NOON, policy())

    assert result.deadline_hours == pytest.approx(0.0)
    assert len(result.hours) == result.hours_needed > 0
