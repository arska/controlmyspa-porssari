"""Tests for the SQLite store.

These import nothing but the storage module — no Flask app, no globals.
"""

import datetime

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
    disabled.save_reading(NOW.isoformat(), 35.0, 37.0, 5.0)
    disabled.save_prices({"2026-08-20T04:00:00+03:00": 0.10})
    assert disabled.newest_readings(10) == []
    assert disabled.prices_since(NOW) == {}


def test_a_missing_directory_disables_the_store(tmp_path):
    """A path under a directory that does not exist runs in memory instead."""
    assert storage.Store(str(tmp_path / "nope" / "test.db")).enabled is False


def test_readings_round_trip(store):
    """A saved reading comes back in the shape the app passes around."""
    store.save_reading(NOW.isoformat(), 35.0, 37.0, 5.0)

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
            (NOW + datetime.timedelta(minutes=i)).isoformat(), 30.0 + i, 37.0, 5.0
        )

    newest = store.newest_readings(3)

    assert [r["current_temp"] for r in newest] == [37.0, 38.0, 39.0]


def test_readings_between_excludes_rows_outside_the_range(store):
    """Only rows inside from/to come back."""
    store.save_reading((NOW - datetime.timedelta(days=2)).isoformat(), 30.0, 37.0, 5.0)
    store.save_reading(NOW.isoformat(), 35.0, 37.0, 5.0)

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
    first.save_reading(NOW.isoformat(), 35.0, 37.0, 5.0)
    first.close()

    second = storage.Store(path)
    assert len(second.newest_readings(10)) == 1
    second.close()


def test_closing_disables_the_store(store):
    """After close() the store behaves like one that was never opened."""
    store.close()

    assert store.enabled is False
    assert store.newest_readings(10) == []
