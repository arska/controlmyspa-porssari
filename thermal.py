"""Thermal model for the spa: how fast it cools, how fast it heats.

Pure functions — no globals, no clock, no environment. The caller supplies the
history, the constants and a callable giving the outside temperature at an
hour, and gets a number back. Keeping the physics here makes it testable
without the Flask app and keeps app.py to state and I/O.
"""

import datetime
import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

MIN_COOLING_PERIODS = 5  # below this the median is noise, not a measurement
MIN_COOLING_HOURS = 2  # shorter periods are dominated by 0.5°C sensor steps
MIN_TEMP_DIFF = 1  # pool must be meaningfully warmer than the air
MIN_HEATING_STRETCH_HOURS = 0.5
MIN_STRETCH_READINGS = 2
SETPOINT_MARGIN = 0.25  # a stretch ending this close to target was capped
MAX_PREDICTION_HOURS = 200  # ~8 days; the cap on how far ahead we look


def _cooling_k_of_period(start: dict, end: dict) -> float | None:
    """Return the cooling constant measured across one non-heating period.

    Measuring across the whole period rather than between adjacent readings
    avoids the spa's 0.5°C resolution: a single step over 12 minutes looks
    like 2°C/h but is only rounding.
    """
    drop = start["current_temp"] - end["current_temp"]
    hours = (
        datetime.datetime.fromisoformat(end["time"])
        - datetime.datetime.fromisoformat(start["time"])
    ).total_seconds() / 3600
    avg_outside = (start["outside_temp"] + end["outside_temp"]) / 2
    avg_pool = (start["current_temp"] + end["current_temp"]) / 2
    temp_diff = avg_pool - avg_outside
    if drop > 0 and hours >= MIN_COOLING_HOURS and temp_diff > MIN_TEMP_DIFF:
        return (drop / hours) / temp_diff
    return None


def cooling_periods(history: list[dict], temp_high: float) -> list[float]:
    """Measure the cooling constant across every non-heating stretch."""
    measured: list[float] = []
    start_idx = None
    for i, entry in enumerate(history):
        is_cooling = (
            entry["desired_temp"] < temp_high and entry.get("outside_temp") is not None
        )
        if not is_cooling:
            if start_idx is not None and start_idx < i - 1:
                k = _cooling_k_of_period(history[start_idx], history[i - 1])
                if k is not None:
                    measured.append(k)
            start_idx = None
        elif start_idx is None:
            start_idx = i
    if start_idx is not None and start_idx < len(history) - 1:
        k = _cooling_k_of_period(history[start_idx], history[-1])
        if k is not None:
            measured.append(k)
    return measured


def _heating_rate_of_stretch(stretch: list[dict], temp_high: float) -> float | None:
    """Return °C/h across one heating stretch, or None if it proves nothing."""
    if len(stretch) < MIN_STRETCH_READINGS:
        return None
    start, end = stretch[0], stretch[-1]
    if end["current_temp"] >= temp_high - SETPOINT_MARGIN:
        return None  # capped at the setpoint: the gain understates the rate
    hours = (
        datetime.datetime.fromisoformat(end["time"])
        - datetime.datetime.fromisoformat(start["time"])
    ).total_seconds() / 3600
    gain = end["current_temp"] - start["current_temp"]
    if hours >= MIN_HEATING_STRETCH_HOURS and gain > 0:
        return gain / hours
    return None


def heating_stretches(history: list[dict], temp_high: float) -> list[float]:
    """Measure °C/h across every stretch where the spa was heating.

    Stretches that reach the setpoint are skipped: the spa stops heating
    there, so their measured gain understates the rate.
    """
    measured: list[float] = []
    stretch: list[dict] = []
    for entry in [*history, {"desired_temp": float("-inf")}]:
        if entry["desired_temp"] >= temp_high:
            stretch.append(entry)
            continue
        rate = _heating_rate_of_stretch(stretch, temp_high)
        if rate is not None:
            measured.append(rate)
        stretch = []
    return measured


def median_within(
    measurements: list[float], minimum: int, low: float, high: float, default: float
) -> tuple[float, bool]:
    """Clamped median of the measurements, or the default if there are too few.

    Returns the value and whether clamping changed it, so the caller can log.
    """
    if len(measurements) < minimum:
        return default, False
    median = statistics.median(measurements)
    clamped = max(low, min(high, median))
    return clamped, clamped != median


def hours_until(
    target_temp: float,
    current_temp: float,
    cooling_k: float,
    outside_at: Callable[[int], float],
) -> float:
    """Hours until the pool cools from current_temp to target_temp.

    Steps hour by hour so each hour uses its own forecast air temperature.
    Returns 0.0 when the pool is already at or below the target, or when it
    can never reach it because the air is warmer.
    """
    if current_temp <= target_temp:
        return 0.0
    temp = current_temp
    for step in range(MAX_PREDICTION_HOURS):
        outside = outside_at(step)
        if temp - outside <= 0:
            return 0.0
        temp -= cooling_k * (temp - outside)
        if temp <= target_temp:
            return float(step + 1)
    return float(MAX_PREDICTION_HOURS)


def temp_after(
    current_temp: float,
    hours: float,
    cooling_k: float,
    outside_at: Callable[[int], float],
) -> float:
    """Pool temperature after cooling for the given number of hours."""
    temp = current_temp
    for step in range(max(0, int(hours))):
        outside = outside_at(step)
        if temp > outside:
            temp -= cooling_k * (temp - outside)
    return temp
