"""Tests for the SQLite store.

These import nothing but the storage module — no Flask app, no globals.
"""

import datetime
import sqlite3

import pytest

import storage

NOW = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(tmp_path):
    """Open a store on a temporary database."""
    open_store = storage.Store(str(tmp_path / "test.db"))
    yield open_store
    open_store.close()


def test_a_store_without_a_path_is_disabled():
    """No path means no persistence, and no errors either."""
    disabled = storage.Store()

    assert disabled.enabled is False
    disabled.save_reading(NOW.isoformat(), 35.0, 37.0)
    disabled.save_prices({"2026-08-20T04:00:00+03:00": 0.10})
    assert disabled.newest_readings(10) == []
    assert disabled.prices_since(NOW) == {}


def test_a_missing_directory_disables_the_store(tmp_path):
    """A path under a directory that does not exist runs in memory instead."""
    assert storage.Store(str(tmp_path / "nope" / "test.db")).enabled is False


def test_readings_round_trip(store):
    """A saved reading comes back in the shape the app passes around.

    Its outside temperature is the stored weather for that hour.
    """
    store.save_weather({NOW.isoformat(): 5.0})
    store.save_reading(NOW.isoformat(), 35.0, 37.0)

    assert store.newest_readings(10) == [
        {
            "time": NOW.isoformat(),
            "current_temp": 35.0,
            "desired_temp": 37.0,
            "outside_temp": 5.0,
        }
    ]


def test_newest_readings_returns_the_latest_oldest_first(store):
    """The newest rows are kept, and handed back in chronological order."""
    for i in range(10):
        store.save_reading(
            (NOW + datetime.timedelta(minutes=i)).isoformat(), 30.0 + i, 37.0
        )

    newest = store.newest_readings(3)

    assert [r["current_temp"] for r in newest] == [37.0, 38.0, 39.0]


def test_readings_between_excludes_rows_outside_the_range(store):
    """Only rows inside from/to come back."""
    store.save_reading((NOW - datetime.timedelta(days=2)).isoformat(), 30.0, 37.0)
    store.save_reading(NOW.isoformat(), 35.0, 37.0)

    inside = store.readings_between(
        NOW - datetime.timedelta(hours=1), NOW + datetime.timedelta(hours=1), 100
    )

    assert [r["current_temp"] for r in inside] == [35.0]


def test_saving_a_price_twice_keeps_the_newer_value(store):
    """Re-fetched hours correct what was stored before."""
    store.save_prices({"2026-08-20T04:00:00+03:00": 0.10})
    store.save_prices({"2026-08-20T04:00:00+03:00": 0.12})

    prices = store.prices_since(NOW - datetime.timedelta(days=365))

    assert prices == {"2026-08-20T04:00:00+03:00": pytest.approx(0.12)}


def test_prices_since_drops_older_intervals(store):
    """The cutoff is applied to the parsed timestamp, offset and all."""
    tz = datetime.timezone(datetime.timedelta(hours=3))
    recent = datetime.datetime(2026, 8, 20, 4, 0, tzinfo=tz)
    old = datetime.datetime(2026, 8, 1, 4, 0, tzinfo=tz)
    store.save_prices({recent.isoformat(): 0.10, old.isoformat(): 0.20})

    prices = store.prices_since(NOW - datetime.timedelta(days=7))

    assert list(prices) == [recent.isoformat()]


def test_unparsable_timestamps_are_skipped(store):
    """A corrupt row must not take the endpoint down with it."""
    store.save_prices({"garbage": 0.10, "2026-08-20T04:00:00+03:00": 0.12})

    prices = store.prices_since(NOW - datetime.timedelta(days=365))

    assert list(prices) == ["2026-08-20T04:00:00+03:00"]


def test_prices_between_returns_a_range_oldest_first(store):
    """The range is inclusive at both ends and ordered."""
    tz = datetime.timezone(datetime.timedelta(hours=3))
    hours = [datetime.datetime(2026, 8, 20, h, 0, tzinfo=tz) for h in (1, 4, 23)]
    store.save_prices({h.isoformat(): 0.1 * i for i, h in enumerate(hours, start=1)})

    between = store.prices_between(hours[0], hours[1])

    assert [p["time"] for p in between] == [hours[0].isoformat(), hours[1].isoformat()]


def test_data_survives_reopening(tmp_path):
    """What a store wrote, the next store reads — this is the point of it."""
    path = str(tmp_path / "test.db")
    first = storage.Store(path)
    first.save_reading(NOW.isoformat(), 35.0, 37.0)
    first.close()

    second = storage.Store(path)
    assert len(second.newest_readings(10)) == 1
    second.close()


def test_closing_disables_the_store(store):
    """After close() the store behaves like one that was never opened."""
    store.close()

    assert store.enabled is False
    assert store.newest_readings(10) == []


def test_weather_round_trips_oldest_first_within_the_range(store):
    """Hourly outside temperatures come back inside from/to, inclusive, in order."""
    hours = [NOW + datetime.timedelta(hours=h) for h in (-30, -2, -1, 0)]
    store.save_weather({h.isoformat(): 10.0 + i for i, h in enumerate(hours)})

    between = store.weather_between(hours[1], hours[3])

    assert between == [
        {"time": hours[1].isoformat(), "outside_temp": 11.0},
        {"time": hours[2].isoformat(), "outside_temp": 12.0},
        {"time": hours[3].isoformat(), "outside_temp": 13.0},
    ]


def test_saving_an_hour_of_weather_twice_keeps_the_newer_value(store):
    """Re-fetched hours overwrite, so the hourly backfill never duplicates rows."""
    store.save_weather({NOW.isoformat(): 10.0})
    store.save_weather({NOW.isoformat(): 11.5})

    assert store.weather_between(NOW, NOW) == [
        {"time": NOW.isoformat(), "outside_temp": 11.5}
    ]


def test_a_reading_takes_the_previous_hour_while_its_own_is_not_fetched(store):
    """The weather job is not aligned to the clock: 13:05 may precede the 13:00 row."""
    store.save_weather({(NOW - datetime.timedelta(hours=1)).isoformat(): 7.0})
    store.save_reading((NOW + datetime.timedelta(minutes=5)).isoformat(), 35.0, 37.0)

    assert store.newest_readings(1)[0]["outside_temp"] == 7.0


def test_a_reading_without_nearby_weather_has_no_outside_temp(store):
    """Older weather is not stretched over a gap: the estimators skip None."""
    store.save_weather({(NOW - datetime.timedelta(hours=2)).isoformat(): 7.0})
    store.save_reading(NOW.isoformat(), 35.0, 37.0)

    assert store.newest_readings(1)[0]["outside_temp"] is None


def _legacy_database(path, readings):
    """Create a database as it was before weather had its own table."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE temperature_readings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, "
        "current_temp REAL NOT NULL, desired_temp REAL NOT NULL, outside_temp REAL)"
    )
    conn.executemany(
        "INSERT INTO temperature_readings "
        "(time, current_temp, desired_temp, outside_temp) VALUES (?, ?, ?, ?)",
        readings,
    )
    conn.commit()
    conn.close()


def _columns(path):
    conn = sqlite3.connect(path)
    names = [row[1] for row in conn.execute("PRAGMA table_info(temperature_readings)")]
    conn.close()
    return names


def test_opening_a_legacy_database_moves_outside_temp_to_the_weather_table(tmp_path):
    """Hourly averages land in weather_readings, and the old column is dropped."""
    path = str(tmp_path / "legacy.db")
    _legacy_database(
        path,
        [
            (NOW.isoformat(), 35.0, 37.0, 4.0),
            ((NOW + datetime.timedelta(minutes=30)).isoformat(), 35.0, 37.0, 6.0),
            ((NOW + datetime.timedelta(hours=1)).isoformat(), 35.0, 37.0, 8.0),
            ((NOW + datetime.timedelta(hours=2)).isoformat(), 35.0, 37.0, None),
        ],
    )

    migrated = storage.Store(path)

    assert migrated.weather_between(NOW, NOW + datetime.timedelta(hours=2)) == [
        {"time": NOW.isoformat(), "outside_temp": 5.0},
        {"time": (NOW + datetime.timedelta(hours=1)).isoformat(), "outside_temp": 8.0},
    ]
    assert [r["outside_temp"] for r in migrated.newest_readings(10)] == [
        5.0,
        5.0,
        8.0,
        8.0,  # its own hour has no weather, so the previous hour's applies
    ]
    migrated.close()
    assert "outside_temp" not in _columns(path)


def test_migration_keeps_weather_already_fetched_for_an_hour(tmp_path):
    """Open-Meteo's hourly value wins over an average of stale readings."""
    path = str(tmp_path / "legacy.db")
    _legacy_database(path, [(NOW.isoformat(), 35.0, 37.0, 4.0)])
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE weather_readings "
        "(time TEXT PRIMARY KEY, outside_temp REAL NOT NULL)"
    )
    conn.execute("INSERT INTO weather_readings VALUES (?, ?)", (NOW.isoformat(), 9.5))
    conn.commit()
    conn.close()

    migrated = storage.Store(path)

    assert migrated.weather_between(NOW, NOW) == [
        {"time": NOW.isoformat(), "outside_temp": 9.5}
    ]
    migrated.close()


def test_a_failed_migration_changes_nothing(tmp_path, monkeypatch):
    """If any hour did not arrive, nothing is written and the column stays."""
    path = str(tmp_path / "legacy.db")
    _legacy_database(path, [(NOW.isoformat(), 35.0, 37.0, 4.0)])
    monkeypatch.setattr(storage.Store, "_missing_hours", lambda _self, hours: hours)

    kept = storage.Store(path)

    assert kept.enabled
    assert kept.weather_between(NOW, NOW) == []
    kept.close()
    assert "outside_temp" in _columns(path)


def test_migration_runs_once(tmp_path):
    """Reopening a migrated database is a no-op, and its data stays."""
    path = str(tmp_path / "legacy.db")
    _legacy_database(path, [(NOW.isoformat(), 35.0, 37.0, 4.0)])
    storage.Store(path).close()

    reopened = storage.Store(path)

    assert reopened.weather_between(NOW, NOW) == [
        {"time": NOW.isoformat(), "outside_temp": 4.0}
    ]
    assert len(reopened.newest_readings(10)) == 1
    reopened.close()


def test_a_new_database_has_no_outside_temp_column(tmp_path):
    """Fresh databases start in the new shape."""
    path = str(tmp_path / "new.db")
    storage.Store(path).close()

    assert "outside_temp" not in _columns(path)


def test_a_disabled_store_has_no_weather():
    """Weather follows the same no-op rule as readings and prices."""
    disabled = storage.Store()
    disabled.save_weather({NOW.isoformat(): 10.0})

    assert disabled.weather_between(NOW, NOW) == []
