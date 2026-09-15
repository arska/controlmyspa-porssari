"""Persistence: the SQLite tables holding temperature readings, prices and weather.

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
    (
        "CREATE TABLE IF NOT EXISTS temperature_readings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "time TEXT NOT NULL, "
        "current_temp REAL NOT NULL, "
        "desired_temp REAL NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS idx_readings_time ON temperature_readings(time)",
    (
        "CREATE TABLE IF NOT EXISTS price_history ("
        "time TEXT PRIMARY KEY, "
        "price REAL NOT NULL)"
    ),
    # Its own table, not a column on readings: a reading only exists when the
    # spa answers, and the outside temperature must not vanish when it doesn't.
    (
        "CREATE TABLE IF NOT EXISTS weather_readings ("
        "time TEXT PRIMARY KEY, "
        "outside_temp REAL NOT NULL)"
    ),
)


def _hour_key(time_key: str) -> str | None:
    """Return the UTC hour a timestamp falls in, keyed like weather_readings."""
    when = _as_aware(time_key)
    if when is None:
        return None
    return (
        when.astimezone(datetime.UTC)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
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
        self._migrate_outside_temp()

    def _migrate_outside_temp(self) -> None:
        """Move outside temperatures off the readings table, then drop the column.

        Databases from before weather had its own table carry outside_temp on
        every reading. They are averaged per UTC hour into weather_readings,
        without replacing hours already fetched from Open-Meteo. The column is
        dropped only after every hour is confirmed present, in the same
        transaction, so a failure leaves the database exactly as it was.
        """
        assert self._conn is not None  # noqa: S101 - only called once opened
        columns = [
            row[1]
            for row in self._conn.execute("PRAGMA table_info(temperature_readings)")
        ]
        if "outside_temp" not in columns:
            return
        sums: dict[str, list[float]] = {}
        for time_key, outside in self._conn.execute(
            "SELECT time, outside_temp FROM temperature_readings "
            "WHERE outside_temp IS NOT NULL"
        ):
            hour = _hour_key(time_key)
            if hour is not None:
                total = sums.setdefault(hour, [0.0, 0])
                total[0] += outside
                total[1] += 1
        hourly = {hour: total / count for hour, (total, count) in sums.items()}
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.executemany(
                "INSERT OR IGNORE INTO weather_readings (time, outside_temp) "
                "VALUES (?, ?)",
                hourly.items(),
            )
            missing = self._missing_hours(set(hourly))
            if missing:
                msg = f"{len(missing)} hours did not reach weather_readings"
                raise sqlite3.DatabaseError(msg)  # noqa: TRY301
            self._conn.execute(
                "ALTER TABLE temperature_readings DROP COLUMN outside_temp"
            )
            self._conn.commit()
        except sqlite3.DatabaseError:
            self._conn.rollback()
            logger.exception("outside_temp migration failed, database left unchanged")
            return
        logger.info(
            "moved outside_temp for %d hours to weather_readings, dropped the column",
            len(hourly),
        )

    def _missing_hours(self, hours: set[str]) -> set[str]:
        """Return the hours that have no row in weather_readings."""
        assert self._conn is not None  # noqa: S101 - only called once opened
        present = {
            row[0] for row in self._conn.execute("SELECT time FROM weather_readings")
        }
        return hours - present

    @property
    def enabled(self) -> bool:
        """Whether anything is actually being persisted."""
        return self._conn is not None

    def close(self) -> None:
        """Close the connection, leaving the store disabled."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def save_reading(self, time: str, current_temp: float, desired_temp: float) -> None:
        """Append one temperature reading. Its outside temperature is weather's."""
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO temperature_readings "
                "(time, current_temp, desired_temp) VALUES (?, ?, ?)",
                (time, current_temp, desired_temp),
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

    def save_weather(self, temps: dict[str, float]) -> None:
        """Record hourly outside temperatures, replacing hours already stored."""
        if self._conn is None:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO weather_readings (time, outside_temp) "
                "VALUES (?, ?)",
                temps.items(),
            )
            self._conn.commit()

    def weather_between(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> list[dict]:
        """Return the outside temperatures in a time range, oldest first.

        Keys are UTC ISO strings, so they compare lexically like readings do.
        """
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, outside_temp FROM weather_readings "
                "WHERE time >= ? AND time <= ? ORDER BY time",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [{"time": t, "outside_temp": v} for t, v in rows]

    def newest_readings(self, limit: int) -> list[dict]:
        """Return the `limit` most recent readings, oldest first."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, current_temp, desired_temp "
                "FROM temperature_readings ORDER BY time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self._with_outside(list(reversed(rows)))

    def readings_between(
        self, start: datetime.datetime, end: datetime.datetime, limit: int
    ) -> list[dict]:
        """Return the readings in a time range, oldest first."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, current_temp, desired_temp "
                "FROM temperature_readings WHERE time >= ? AND time <= ? "
                "ORDER BY time LIMIT ?",
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return self._with_outside(rows)

    def _with_outside(self, rows: list[tuple]) -> list[dict]:
        """Turn reading rows into dicts, with the outside temperature of their hour.

        The weather job runs hourly but not on the hour, so a reading may come
        before its own hour is fetched; the previous hour stands in for it.
        Anything older is not stretched over a gap.
        """
        if not rows:
            return []
        first = _as_aware(rows[0][0])
        last = _as_aware(rows[-1][0])
        weather = {}
        if first is not None and last is not None:
            weather = {
                row["time"]: row["outside_temp"]
                for row in self.weather_between(
                    first.astimezone(datetime.UTC) - datetime.timedelta(hours=2),
                    last.astimezone(datetime.UTC),
                )
            }
        readings = []
        for time_str, current_temp, desired_temp in rows:
            hour = _hour_key(time_str)
            outside = None
            if hour is not None:
                previous = (
                    datetime.datetime.fromisoformat(hour) - datetime.timedelta(hours=1)
                ).isoformat()
                outside = weather.get(hour, weather.get(previous))
            readings.append(
                {
                    "time": time_str,
                    "current_temp": current_temp,
                    "desired_temp": desired_temp,
                    "outside_temp": outside,
                }
            )
        return readings

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
