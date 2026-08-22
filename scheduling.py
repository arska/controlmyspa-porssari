"""Which hours to heat: the scheduling policy.

Pure functions. The caller supplies the prices, the temperature history, the
moment to plan for and the measured constants; the module returns the hours to
heat and why. No globals, no clock, no environment — so a plan can be computed
for any instant, including replaying stored history to see what a change to
the policy would have done.
"""

import dataclasses
import datetime
import math
from typing import TYPE_CHECKING

import pricing
import thermal

if TYPE_CHECKING:
    from collections.abc import Callable

BUDGET_WINDOW_START_HOUR = 14  # spot-hinta publishes tomorrow at ~14:00
SIZING_ROUNDS = 3  # sizing and picking settle in one or two; this is the guard


@dataclasses.dataclass(frozen=True)
class Policy:
    """Everything the planner needs besides the data itself.

    The temperatures and the budget come from configuration; cooling_k,
    heating_rate and outside_at are what the thermal model currently believes.
    """

    temp_high: float
    temp_min: float
    max_hours: int
    deadband: float
    cooling_k: float
    heating_rate: float
    outside_at: Callable[[int], float]


@dataclasses.dataclass(frozen=True)
class Plan:
    """The hours to heat, and the reasoning that produced them."""

    hours: set[str]
    reason: str
    deadline_hours: float = 0.0
    hours_needed: int = 0
    budget_used: int = 0


def heated_hours_in_window(
    history: list[dict], now: datetime.datetime, temp_high: float
) -> set[str]:
    """Distinct hours already heated in the current 14:00-14:00 budget window."""
    window_start = now.replace(
        hour=BUDGET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    if now.hour < BUDGET_WINDOW_START_HOUR:
        window_start -= datetime.timedelta(days=1)
    heated: set[str] = set()
    for entry in history:
        entry_time = datetime.datetime.fromisoformat(entry["time"])
        if entry_time >= window_start and entry["desired_temp"] >= temp_high:
            local = entry_time.astimezone(now.tzinfo)
            heated.add(local.replace(minute=0, second=0, microsecond=0).isoformat())
    return heated


def candidate_hours(
    future_prices: dict[str, float],
    now: datetime.datetime,
    deadline_hours: float,
    hours_needed: int,
) -> dict[str, float]:
    """Price hours before the deadline, falling back to all future prices.

    When there are not enough hours left before TEMP_MIN to matter, honouring
    the deadline would only mean buying expensive hours that cannot save it.
    """
    if deadline_hours <= 0:
        return future_prices
    deadline = now + datetime.timedelta(hours=deadline_hours)
    within = {
        k: v
        for k, v in future_prices.items()
        if datetime.datetime.fromisoformat(k) <= deadline
    }
    return within if len(within) >= hours_needed else future_prices


def hours_needed_at(
    current_temp: float,
    start: datetime.datetime,
    now: datetime.datetime,
    policy: Policy,
) -> int:
    """Hours of heating needed to reach temp_high for a block starting at `start`.

    The pool cools until the block starts, so it is sized against the
    temperature it will have then, not the warmer one it has now.

    Nothing is booked while the pool is within the deadband of the target: the
    spa's sensor resolves 0.5°C, so without one every sensor tick books a fresh
    hour of heating at whatever price happens to be cheapest next.
    """
    hours = (start - now).total_seconds() / 3600
    temp_then = thermal.temp_after(
        current_temp, hours, policy.cooling_k, policy.outside_at
    )
    if temp_then > policy.temp_high - policy.deadband:
        return 0
    return math.ceil((policy.temp_high - temp_then) / policy.heating_rate)


def can_wait_for_prices(
    deadline_hours: float, future_prices: dict[str, float], now: datetime.datetime
) -> bool:
    """Whether the pool survives past the last price hour we know about.

    spot-hinta.fi publishes tomorrow at ~14:00, so before then the cheapest
    *known* hour is often an expensive one later today. If TEMP_MIN is not at
    risk before the horizon runs out, booking can wait until the cheaper hours
    are actually visible.
    """
    if deadline_hours <= 0:
        return False
    horizon = max(datetime.datetime.fromisoformat(k) for k in future_prices)
    hours_to_horizon = (horizon - now).total_seconds() / 3600 + 1
    return deadline_hours > hours_to_horizon


def size_and_pick(
    future_prices: dict[str, float],
    now: datetime.datetime,
    deadline_hours: float,
    current_temp: float,
    policy: Policy,
) -> tuple[list[str], int]:
    """Order hours cheapest-first and size the block from its own start time.

    Sizing and picking depend on each other — a bigger block may start earlier,
    and an earlier start needs fewer hours — so this iterates to a fixed point.
    """
    needed = hours_needed_at(current_temp, now, now, policy)
    ordered: list[str] = []
    for _ in range(SIZING_ROUNDS):
        candidates = candidate_hours(future_prices, now, deadline_hours, max(needed, 1))
        ordered = pricing.cheapest_first(candidates)
        block = ordered[: max(needed, 1)]
        start = min(datetime.datetime.fromisoformat(k) for k in block)
        resized = hours_needed_at(current_temp, start, now, policy)
        if resized == needed:
            break
        needed = resized
    return ordered, needed


def plan(
    prices: dict[str, float],
    history: list[dict],
    now: datetime.datetime,
    policy: Policy,
    booked: set[str] = frozenset(),
) -> Plan:
    """Decide which hours to heat, given prices and where the pool is now.

    `booked` is the plan currently in force: deferring a decision must not
    cancel a block that is about to start.
    """
    if not history:
        # Before the first reading, assuming any temperature is a guess, and
        # guessing TEMP_MIN looks like an emergency worth heating through.
        return Plan(set(booked), "no temperature reading yet")

    current_temp = history[-1]["current_temp"]
    deadline_hours = thermal.hours_until(
        policy.temp_min, current_temp, policy.cooling_k, policy.outside_at
    )

    current_hour = now.replace(minute=0, second=0, microsecond=0)
    future_prices = {
        k: v
        for k, v in prices.items()
        if datetime.datetime.fromisoformat(k) >= current_hour
    }
    if not future_prices:
        return Plan(set(), "no future prices", deadline_hours)

    if can_wait_for_prices(deadline_hours, future_prices, now):
        return Plan(
            {k for k in booked if k in future_prices},
            "waiting for cheaper hours to publish",
            deadline_hours,
        )

    ordered, needed = size_and_pick(
        future_prices, now, deadline_hours, current_temp, policy
    )
    heated = heated_hours_in_window(history, now, policy.temp_high)
    remaining_budget = max(0, policy.max_hours - len(heated))
    pick_count = min(needed, remaining_budget)

    reason = "booked the cheapest hours before the deadline"
    if needed == 0:
        reason = "within the deadband of the target"
    elif needed > remaining_budget:
        reason = "budget exhausted, pool may drop below TEMP_MIN"
    return Plan(set(ordered[:pick_count]), reason, deadline_hours, needed, len(heated))
