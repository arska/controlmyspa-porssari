"""Persistence: the SQLite tables holding temperature readings and prices.

A Store owns its connection and its lock, so nothing outside this module
touches SQLite. Persistence is optional — a Store with no path is disabled and
every method is a no-op or an empty result, which is how the app runs in
development without a /data directory.

Readings are stored as UTC ISO strings and so compare lexically; price keys
carry a local offset and are compared as parsed datetimes.
"""

import datetime
import logging
import pathlib
import sqlite3
import threading

logger = logging.getLogger(__name__)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS temperature_readings ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "time TEXT NOT NULL, "
    "current_temp REAL NOT NULL, "
    "desired_temp REAL NOT NULL, "
    "outside_temp REAL)",
    "CREATE INDEX IF NOT EXISTS idx_readings_time ON temperature_readings(time)",
    "CREATE TABLE IF NOT EXISTS price_history ("
    "time TEXT PRIMARY KEY, "
    "price REAL NOT NULL)",
)


def _as_aware(time_key: str) -> datetime.datetime | None:
    """Parse a stored timestamp, assuming UTC when it carries no offset."""
    try:
        parsed = datetime.datetime.fromisoformat(time_key)
    except ValueError:
        logger.warning("ignoring row with unparsable timestamp %r", time_key)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


class Store:
    """Readings and prices on disk, or nothing at all when disabled."""

    def __init__(self, path: str | None = None) -> None:
        """Open `path`, or stay disabled when it is None or its directory is missing."""
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if path is None:
            return
        db_path = pathlib.Path(path)
        if not db_path.parent.exists():
            logger.warning(
                "SQLite disabled: directory %s does not exist", db_path.parent
            )
            return
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    @property
    def enabled(self) -> bool:
        """Whether anything is actually being persisted."""
        return self._conn is not None

    def close(self) -> None:
        """Close the connection, leaving the store disabled."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def save_reading(
        self,
        time: str,
        current_temp: float,
        desired_temp: float,
        outside_temp: float | None,
    ) -> None:
        """Append one temperature reading."""
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO temperature_readings "
                "(time, current_temp, desired_temp, outside_temp) VALUES (?, ?, ?, ?)",
                (time, current_temp, desired_temp, outside_temp),
            )
            self._conn.commit()

    def save_prices(self, prices: dict[str, float]) -> None:
        """Record prices, replacing any already stored for the same interval."""
        if self._conn is None:
            return
        with self._lock:
            for time_key, price in prices.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO price_history (time, price) VALUES (?, ?)",
                    (time_key, price),
                )
            self._conn.commit()

    def newest_readings(self, limit: int) -> list[dict]:
        """Return the `limit` most recent readings, oldest first."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, current_temp, desired_temp, outside_temp "
                "FROM temperature_readings ORDER BY time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_reading(row) for row in reversed(rows)]

    def readings_between(
        self, start: datetime.datetime, end: datetime.datetime, limit: int
    ) -> list[dict]:
        """Return the readings in a time range, oldest first."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, current_temp, desired_temp, outside_temp "
                "FROM temperature_readings WHERE time >= ? AND time <= ? "
                "ORDER BY time LIMIT ?",
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return [_reading(row) for row in rows]

    def prices_since(self, cutoff: datetime.datetime) -> dict[str, float]:
        """Return prices for intervals at or after `cutoff`."""
        return {
            time_key: price
            for time_key, price, when in self._all_prices()
            if when >= cutoff
        }

    def prices_between(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> list[dict]:
        """Return the prices inside a time range, oldest first."""
        return [
            {"time": time_key, "price": price}
            for time_key, price, when in self._all_prices()
            if start <= when <= end
        ]

    def _all_prices(self) -> list[tuple[str, float, datetime.datetime]]:
        """Return every stored price with its parsed timestamp, oldest first.

        The table holds one row per hour, so reading it whole costs little
        even after years, and parsing beats comparing ISO strings that carry
        different UTC offsets across a daylight-saving change.
        """
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, price FROM price_history"
            ).fetchall()
        parsed = [
            (time_key, price, _as_aware(time_key)) for time_key, price in sorted(rows)
        ]
        return [(k, p, when) for k, p, when in parsed if when is not None]


def _reading(row: tuple) -> dict:
    """Turn a readings row into the dict shape the app passes around."""
    time_str, current_temp, desired_temp, outside_temp = row
    return {
        "time": time_str,
        "current_temp": current_temp,
        "desired_temp": desired_temp,
        "outside_temp": outside_temp,
    }
