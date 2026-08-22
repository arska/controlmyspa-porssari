"""Spot price handling: fetching, aggregating and remembering hourly prices.

spot-hinta.fi serves 15-minute prices for today and (from ~14:00) tomorrow.
These helpers turn that into the hourly (or PRICE_INTERVAL) dict the scheduler
works with, and decide how long past hours stay in memory for the chart.
"""

import collections
import datetime
import logging
import os

import requests

SPOT_HINTA_API = "https://api.spot-hinta.fi"
# Named tuple for the except clause — ruff's formatter strips inline parens.
FETCH_ERRORS = (requests.exceptions.RequestException, ValueError)
NOT_FOUND = 404
NIGHT_START = 22
NIGHT_END = 7

logger = logging.getLogger(__name__)


def margin(hour: int) -> float:
    """Return the price margin (EUR/kWh) for the given hour of day.

    Covers transmission costs, retailer margin, etc. Configurable via
    PRICE_MARGIN_NIGHT (22:00-07:00) and PRICE_MARGIN_DAY (07:00-22:00).
    Default: 0 (no margin).
    """
    night = float(os.getenv("PRICE_MARGIN_NIGHT", "0")) / 100  # c/kWh → EUR/kWh
    day = float(os.getenv("PRICE_MARGIN_DAY", "0")) / 100
    if hour >= NIGHT_START or hour < NIGHT_END:
        return night
    return day


def fetch_entries() -> list[dict]:
    """Fetch raw 15-min price entries from spot-hinta.fi /Today and /DayForward."""
    entries: list[dict] = []
    for endpoint in ("/Today", "/DayForward"):
        try:
            resp = requests.get(f"{SPOT_HINTA_API}{endpoint}", timeout=10)
            if resp.status_code == NOT_FOUND and endpoint == "/DayForward":
                # Day-ahead prices aren't published yet (normal before ~13:00 CET)
                logger.info("day-ahead prices not yet available")
                continue
            resp.raise_for_status()
            entries.extend(resp.json())
        except FETCH_ERRORS:
            logger.exception("failed to fetch %s", endpoint)
    return entries


def aggregate(entries: list[dict], interval: int) -> dict[str, float]:
    """Group raw entries by interval boundary, average, and add margin."""
    slots_per_interval = interval // 15
    groups: dict[str, list[float]] = collections.defaultdict(list)
    for entry in entries:
        dt = datetime.datetime.fromisoformat(entry["DateTime"])
        minute = (dt.minute // interval) * interval
        interval_start = dt.replace(minute=minute, second=0, microsecond=0)
        groups[interval_start.isoformat()].append(entry["PriceWithTax"])
    result = {}
    for time_key, prices in groups.items():
        if len(prices) == slots_per_interval:
            hour = datetime.datetime.fromisoformat(time_key).hour
            result[time_key] = sum(prices) / len(prices) + margin(hour)
    return result


def within_memory(time_key: str, memory_hours: int) -> bool:
    """Return whether a price interval is recent enough to keep in memory."""
    try:
        price_time = datetime.datetime.fromisoformat(time_key)
    except ValueError:
        logger.warning("ignoring price with unparsable timestamp %r", time_key)
        return False
    if price_time.tzinfo is None:
        price_time = price_time.replace(tzinfo=datetime.UTC)
    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
        hours=memory_hours
    )
    return price_time >= cutoff


def merge(
    remembered: dict[str, float], fresh: dict[str, float], memory_hours: int
) -> dict[str, float]:
    """Merge freshly fetched prices over remembered ones, dropping stale entries.

    spot-hinta.fi only serves today and tomorrow, so past prices would vanish
    from the chart on every fetch. Keep them for memory_hours; freshly fetched
    values always win over remembered ones for the same interval.
    """
    kept = {
        k: v
        for k, v in remembered.items()
        if k not in fresh and within_memory(k, memory_hours)
    }
    return kept | fresh


def cheapest_first(candidates: dict[str, float]) -> list[str]:
    """Order hours by price, resolving ties to the earliest hour.

    Equal prices are common on Nordpool; without the time tiebreak the order
    would be dict insertion order, which is arbitrary for prices restored from
    SQLite.
    """
    return sorted(
        candidates, key=lambda k: (candidates[k], datetime.datetime.fromisoformat(k))
    )
