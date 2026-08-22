"""Tests for controlmyspa-porssari application."""

import datetime
import os
import sqlite3
import time
import typing
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

import app as app_module


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset global state between tests."""
    app_module.temperature_history.clear()
    app_module.manual_override_endtime = datetime.datetime.fromtimestamp(
        0, tz=datetime.UTC
    )
    app_module.cache.clear()
    app_module.last_stale_alert_time = datetime.datetime.fromtimestamp(
        0, tz=datetime.UTC
    )
    app_module.STALE_ALERT_ACTIVE = False
    app_module.latest_outside_temp = None
    app_module.db_conn = None
    app_module.hourly_prices = {}
    app_module.heating_schedule = set()
    app_module.cooling_k = app_module.DEFAULT_COOLING_K
    app_module.heating_rate = app_module.DEFAULT_HEATING_RATE
    app_module.weather_forecast = {}
    yield


@pytest.fixture
def client():
    """Flask test client."""
    app_module.APP.config["TESTING"] = True
    with app_module.APP.test_client() as c:
        yield c


# --- Status page tests ---


class TestStatusPage:
    """Tests for the main status page."""

    def test_status_page_loads_with_cached_pool(self, client):
        """Status page renders when pool data is cached."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"35" in resp.data
        assert b"37" in resp.data

    def test_status_page_draws_now_line(self, client):
        """Status page chart includes the 'now' marker plugin."""
        resp = client.get("/")
        assert b"nowLine" in resp.data

    def test_status_page_loads_without_cache(self, client):
        """Status page renders even with no cached data (API failure)."""
        real_monotonic = time.monotonic
        fake_offset = [0.0]

        def advancing_monotonic():
            return real_monotonic() + fake_offset[0]

        def advancing_sleep(seconds):
            fake_offset[0] += seconds

        with (
            patch(
                "app.controlmyspa.ControlMySpa",
                side_effect=requests.exceptions.ConnectionError("no api"),
            ),
            patch("time.monotonic", side_effect=advancing_monotonic),
            patch("tenacity.nap.time.sleep", side_effect=advancing_sleep),
        ):
            resp = client.get("/")
            assert resp.status_code == 200

    def test_status_page_loads_with_heating_schedule(self, client):
        """Status page renders when heating schedule is set."""
        tz = ZoneInfo("Europe/Helsinki")
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.heating_schedule = {current_hour.isoformat()}
        app_module.cache.set("pool", {"current_temp": 35, "desired_temp": 37})
        resp = client.get("/")
        assert resp.status_code == 200


# --- Temperature API tests ---


class TestTemperatureAPI:
    """Tests for /api/temperatures endpoint."""

    def test_returns_empty_history(self, client):
        """Returns empty history when no data recorded."""
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert data["history"] == []
        assert data["future"] == []

    def test_returns_history_data(self, client):
        """Returns recorded temperature history."""
        app_module.temperature_history.append(
            {
                "time": "2024-01-01T12:00:00+00:00",
                "current_temp": 35.0,
                "desired_temp": 37,
            }
        )
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert len(data["history"]) == 1
        assert data["history"][0]["current_temp"] == 35.0

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    def test_returns_temp_bounds(self, client):
        """Returns configured temp_high and temp_low."""
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert data["temp_high"] == 37
        assert data["temp_low"] == 27

    def test_returns_outside_temp(self, client):
        """Returns the latest outside temperature."""
        app_module.latest_outside_temp = 7.5
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert data["outside_temp"] == 7.5

    def test_history_maxlen(self, client):
        """Temperature history is a bounded ring buffer, a week deep."""
        capacity = app_module.temperature_history.maxlen
        for i in range(capacity + 100):
            app_module.temperature_history.append(
                {
                    "time": f"2024-01-01T{i:05d}",
                    "current_temp": 30 + (i % 10),
                    "desired_temp": 37,
                }
            )
        assert len(app_module.temperature_history) == capacity


# --- Override API tests ---


class TestOverrideAPI:
    """Tests for /api/override endpoint."""

    def test_enable_override(self, client):
        """Enabling override sets future endtime."""
        resp = client.post(
            "/api/override",
            json={"action": "enable"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["override_active"] is True
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )

    @patch("app.control")
    def test_disable_override(self, mock_control, client):
        """Disabling override resets endtime and calls control with skip flag."""
        # First enable
        app_module.manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        # Then disable
        resp = client.post(
            "/api/override",
            json={"action": "disable"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["override_active"] is False
        mock_control.assert_called_once_with(skip_override_detection=True)

    def test_invalid_action(self, client):
        """Invalid action returns current state without changes."""
        resp = client.post(
            "/api/override",
            json={"action": "invalid"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["override_active"] is False

    def test_no_body(self, client):
        """Request with no JSON body doesn't crash."""
        resp = client.post(
            "/api/override",
            content_type="application/json",
        )
        assert resp.status_code == 200

    @patch("app.controlmyspa.ControlMySpa")
    def test_heat_override(self, mock_spa_cls, client, monkeypatch):
        """Heat action sets spa temp to TEMP_HIGH - 0.5 and enables override."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "10")
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        resp = client.post(
            "/api/override",
            json={"action": "heat"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["override_active"] is True
        assert mock_spa.desired_temp == 36.5

    @patch("app.controlmyspa.ControlMySpa")
    def test_heat_override_sets_12h_endtime(self, mock_spa_cls, client, monkeypatch):
        """Heat action sets manual override endtime 12 hours in the future."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "10")
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/api/override",
            json={"action": "heat"},
            content_type="application/json",
        )
        expected_min = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
            hours=11, minutes=59
        )
        assert app_module.manual_override_endtime > expected_min

    @patch("app.controlmyspa.ControlMySpa")
    def test_heat_override_updates_cache_and_history(
        self, mock_spa_cls, client, monkeypatch
    ):
        """Heat action updates cached pool data and temperature history."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "10")
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/api/override",
            json={"action": "heat"},
            content_type="application/json",
        )
        pool = app_module.cache.get("pool")
        # set_temp reads desired_temp from API before setting the new value
        assert pool["desired_temp"] == 37
        assert pool["current_temp"] == 35
        assert len(app_module.temperature_history) == 1
        assert app_module.temperature_history[0]["desired_temp"] == 37

    @patch("app.controlmyspa.ControlMySpa")
    def test_heat_override_button_label(self, mock_spa_cls, client, monkeypatch):
        """Status page shows heat button with correct temperature."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("CONTROLMYSPA_USER", "test")
        monkeypatch.setenv("CONTROLMYSPA_PASS", "test")
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        resp = client.get("/")
        assert b"36.5" in resp.data
        assert b"Keep the pool heated to 36.5" in resp.data

    @patch("app.controlmyspa.ControlMySpa")
    def test_cold_override(self, mock_spa_cls, client, monkeypatch):
        """Cold action sets spa temp to TEMP_LOW and enables 24h override."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "10")
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        resp = client.post(
            "/api/override",
            json={"action": "cold"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["override_active"] is True
        assert mock_spa.desired_temp == 10.5
        expected_min = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
            hours=23, minutes=59
        )
        assert app_module.manual_override_endtime > expected_min


# --- Control logic tests ---


class TestControlLogic:
    """Tests for the control() function."""

    @patch("app.set_temp")
    def test_no_prices_sets_low(self, mock_set_temp, monkeypatch):
        """Control sets TEMP_LOW when no prices/schedule available."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "27")
        monkeypatch.setenv("TEMP_OVERRIDE", "0")
        app_module.heating_schedule = set()
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(27, skip_override_detection=False)

    @patch("app.set_temp")
    def test_heating_hour_sets_high(self, mock_set_temp, monkeypatch):
        """Control sets TEMP_HIGH when current hour is in heating_schedule."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "27")
        monkeypatch.setenv("TEMP_OVERRIDE", "0")
        tz = ZoneInfo("Europe/Helsinki")
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.heating_schedule = {current_hour.isoformat()}
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(37, skip_override_detection=False)

    @patch("app.set_temp")
    def test_non_heating_hour_sets_low(self, mock_set_temp, monkeypatch):
        """Control sets TEMP_LOW when current hour is not in heating_schedule."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "27")
        monkeypatch.setenv("TEMP_OVERRIDE", "0")
        tz = ZoneInfo("Europe/Helsinki")
        # Put a different hour in the schedule
        other_hour = (datetime.datetime.now(tz) + datetime.timedelta(hours=5)).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.heating_schedule = {other_hour.isoformat()}
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(27, skip_override_detection=False)

    @patch("app.set_temp")
    def test_temp_override_env(self, mock_set_temp, monkeypatch):
        """TEMP_OVERRIDE env var overrides all logic."""
        monkeypatch.setenv("TEMP_HIGH", "37")
        monkeypatch.setenv("TEMP_LOW", "27")
        monkeypatch.setenv("TEMP_OVERRIDE", "40")
        app_module.heating_schedule = set()
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(40, skip_override_detection=False)


# --- set_temp tests ---


class TestSetTemp:
    """Tests for the set_temp() function."""

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_records_temperature_history(self, mock_api_class):
        """set_temp appends to temperature_history."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api

        with app_module.APP.app_context():
            app_module.set_temp(37)

        assert len(app_module.temperature_history) == 1
        assert app_module.temperature_history[0]["current_temp"] == 34.5
        assert app_module.temperature_history[0]["desired_temp"] == 37

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_caches_pool_data(self, mock_api_class):
        """set_temp caches pool data."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api

        with app_module.APP.app_context():
            app_module.set_temp(37)
            pool = app_module.cache.get("pool")

        assert pool["current_temp"] == 34.5
        assert pool["desired_temp"] == 37

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_sets_temp_when_different(self, mock_api_class):
        """set_temp updates API when desired differs from target."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 27
        mock_api_class.return_value = mock_api

        with app_module.APP.app_context():
            app_module.set_temp(37)

        assert mock_api.desired_temp == 37

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_skips_when_same(self, mock_api_class):
        """set_temp doesn't update API when desired == target."""
        mock_api = MagicMock()
        mock_api.current_temp = 35
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api

        with app_module.APP.app_context():
            app_module.set_temp(37)

        # desired_temp was read as 37, and we're setting 37
        # so the property setter should not be called with a new value
        # (it stays 37 from the mock)

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_records_outside_temp_in_history(self, mock_api_class):
        """set_temp records the latest outside temp with each reading."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api
        app_module.latest_outside_temp = -3.2
        with app_module.APP.app_context():
            app_module.set_temp(37)
        assert app_module.temperature_history[-1]["outside_temp"] == -3.2

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.check_stale_temperature")
    @patch("app.controlmyspa.ControlMySpa")
    def test_calls_check_stale_temperature(self, mock_api_class, mock_check):
        """set_temp calls check_stale_temperature after recording data."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api
        with app_module.APP.app_context():
            app_module.set_temp(37)
        mock_check.assert_called_once()

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_manual_override_detection(self, mock_api_class):
        """Detects manual override when temp differs from HIGH/LOW."""
        mock_api = MagicMock()
        mock_api.current_temp = 35
        mock_api.desired_temp = 33  # neither 37 nor 27
        mock_api_class.return_value = mock_api

        with app_module.APP.app_context():
            app_module.set_temp(37)

        # Should have set a 12h override
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_retries_on_keyerror(self, mock_api_class):
        """set_temp retries on KeyError and logs RetryError gracefully."""
        mock_api_class.side_effect = KeyError("currentState")
        real_monotonic = time.monotonic
        fake_offset = [0.0]

        def advancing_monotonic():
            return real_monotonic() + fake_offset[0]

        def advancing_sleep(seconds):
            fake_offset[0] += seconds

        with (
            patch("time.monotonic", side_effect=advancing_monotonic),
            patch("tenacity.nap.time.sleep", side_effect=advancing_sleep),
            app_module.APP.app_context(),
        ):
            app_module.set_temp(37)

        # Should have retried multiple times before giving up
        assert mock_api_class.call_count > 1
        # No temperature history recorded since all attempts failed
        assert len(app_module.temperature_history) == 0

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_active_override_skips_temp_change(self, mock_api_class):
        """set_temp returns early when manual override endtime is in future."""
        mock_api = MagicMock()
        mock_api.current_temp = 35
        mock_api.desired_temp = 33  # neither 37 nor 27
        mock_api_class.return_value = mock_api
        # Set an active override
        app_module.manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=6)

        with app_module.APP.app_context():
            app_module.set_temp(37)

        # Should not have changed the temp — override is active
        assert mock_api.desired_temp == 33


# --- update_weather tests ---


class TestUpdateWeather:
    """Tests for the update_weather() function."""

    @patch("app.requests.get")
    def test_fetches_outside_temp_and_forecast(self, mock_get):
        """Successfully parses Open-Meteo response with current + hourly forecast."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "current": {"temperature_2m": 12.3},
            "hourly": {
                "time": ["2026-07-22T21:00", "2026-07-22T22:00"],
                "temperature_2m": [12.0, 11.5],
            },
        }
        mock_get.return_value = mock_response

        with app_module.APP.app_context():
            app_module.update_weather()

        assert app_module.latest_outside_temp == 12.3
        assert len(app_module.weather_forecast) == 2
        assert app_module.weather_forecast["2026-07-22T21:00"] == 12.0

    @patch("app.requests.get", side_effect=requests.exceptions.ConnectionError("no"))
    def test_keeps_last_value_on_error(self, mock_get):
        """Leaves the previous value untouched when the API is unreachable."""
        app_module.latest_outside_temp = 5.0
        with app_module.APP.app_context():
            app_module.update_weather()
        assert app_module.latest_outside_temp == 5.0

    @patch("app.requests.get")
    def test_handles_malformed_response(self, mock_get):
        """Does not crash when the response is missing expected keys."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"unexpected": "shape"}
        mock_get.return_value = mock_response
        with app_module.APP.app_context():
            app_module.update_weather()
        assert app_module.latest_outside_temp is None


# --- Telegram tests ---


class TestTelegram:
    """Tests for Telegram notification functions."""

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("app.requests.post")
    def test_send_telegram_sends_message(self, mock_post):
        """send_telegram sends a message via Telegram Bot API."""
        mock_post.return_value = MagicMock(status_code=200)
        with app_module.APP.app_context():
            app_module.send_telegram("hello")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "tok" in args[0]
        assert kwargs["json"]["chat_id"] == "123"
        assert kwargs["json"]["text"] == "hello"

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "111,222"},
    )
    @patch("app.requests.post")
    def test_send_telegram_multiple_chat_ids(self, mock_post):
        """send_telegram sends to all chat IDs when no specific chat_id given."""
        mock_post.return_value = MagicMock(status_code=200)
        with app_module.APP.app_context():
            app_module.send_telegram("hello")
        assert mock_post.call_count == 2
        chat_ids = [c.kwargs["json"]["chat_id"] for c in mock_post.call_args_list]
        assert "111" in chat_ids
        assert "222" in chat_ids

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "111,222"},
    )
    @patch("app.requests.post")
    def test_send_telegram_specific_chat_id(self, mock_post):
        """send_telegram sends to specific chat_id when provided."""
        mock_post.return_value = MagicMock(status_code=200)
        with app_module.APP.app_context():
            app_module.send_telegram("hello", chat_id="333")
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["json"]["chat_id"] == "333"

    @patch("app.requests.post")
    def test_send_telegram_no_config_does_nothing(self, mock_post):
        """send_telegram does nothing if env vars are missing."""
        with app_module.APP.app_context():
            app_module.send_telegram("hello")
        mock_post.assert_not_called()

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("app.requests.post", side_effect=requests.exceptions.ConnectionError("fail"))
    def test_send_telegram_handles_exception(self, mock_post):
        """send_telegram logs and continues on request failure."""
        with app_module.APP.app_context():
            app_module.send_telegram("hello")  # should not raise
        mock_post.assert_called_once()

    @patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111, 222 ,333"})
    def test_get_allowed_chat_ids(self):
        """get_allowed_chat_ids parses comma-separated list with whitespace."""
        result = app_module.get_allowed_chat_ids()
        assert result == {"111", "222", "333"}

    def test_get_allowed_chat_ids_empty(self):
        """get_allowed_chat_ids returns empty set when env var missing."""
        result = app_module.get_allowed_chat_ids()
        assert result == set()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_heating_mode(self, mock_tg):
        """Alert after 3h of identical readings when heating."""
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(13):
            t = now - datetime.timedelta(minutes=181 - i * 15)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 37}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()
        assert "stuck" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_no_alert_when_a_heating_block_has_just_started(self, mock_tg):
        """A night of quiet cooling must not read as a stuck heater.

        Replays 19 Aug: three hours at a steady 35.5°C (the pool loses 0.26°C,
        which the spa's 0.5°C sensor cannot show), then the 04:00 heating block
        begins. The old check switched to its 3h heating window the moment the
        setpoint went up and judged the pool against hours of cooling.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(24):
            t = now - datetime.timedelta(minutes=185 - i * 7.5)
            app_module.temperature_history.append(
                {
                    "time": t.isoformat(),
                    "current_temp": 35.5,
                    "desired_temp": 10.0,
                    "outside_temp": 15.0,
                }
            )
        for minutes in (5, 0):  # heating just started
            app_module.temperature_history.append(
                {
                    "time": (now - datetime.timedelta(minutes=minutes)).isoformat(),
                    "current_temp": 35.5,
                    "desired_temp": 37.0,
                    "outside_temp": 15.0,
                }
            )

        with app_module.APP.app_context():
            app_module.check_stale_temperature()

        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_no_alert_while_a_pool_cools_as_slowly_as_expected(self, mock_tg):
        """A pool losing 0.26°C in 12h is behaving, not stuck.

        The model expects little movement in summer, so a flat 0.5°C threshold
        would report every warm night as a dead gateway.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(49):
            t = now - datetime.timedelta(minutes=725 - i * 15)
            app_module.temperature_history.append(
                {
                    "time": t.isoformat(),
                    "current_temp": 35.5 - i * 0.005,
                    "desired_temp": 10.0,
                    "outside_temp": 22.0,  # warm night: barely any heat loss
                }
            )
        app_module.cooling_k = 0.004

        with app_module.APP.app_context():
            app_module.check_stale_temperature()

        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_alerts_when_a_full_heating_block_moves_nothing(self, mock_tg):
        """90 minutes of heating with no rise is a real fault."""
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(13):
            t = now - datetime.timedelta(minutes=95 - i * 7.5)
            app_module.temperature_history.append(
                {
                    "time": t.isoformat(),
                    "current_temp": 34.0,
                    "desired_temp": 37.0,
                    "outside_temp": 15.0,
                }
            )

        with app_module.APP.app_context():
            app_module.check_stale_temperature()

        mock_tg.assert_called_once()
        message = mock_tg.call_args[0][0]
        assert "stuck" in message.lower()
        assert "expected" in message.lower()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_general_mode(self, mock_tg):
        """Alert after 12h of identical readings in general mode."""
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(37):
            t = now - datetime.timedelta(minutes=721 - i * 20)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 10}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_no_alert_when_temp_changing(self, mock_tg):
        """No alert when temperatures are changing."""
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(35):
            t = now - datetime.timedelta(minutes=479 - i * 14)
            app_module.temperature_history.append(
                {
                    "time": t.isoformat(),
                    "current_temp": 30.0 + i * 0.5,
                    "desired_temp": 37,
                }
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_suppressed_within_window(self, mock_tg):
        """Alert suppressed within the stale window (12h idle) after first alert."""
        app_module.last_stale_alert_time = datetime.datetime.now(tz=datetime.UTC)
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(37):
            t = now - datetime.timedelta(minutes=721 - i * 20)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 10}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_repeats_after_window(self, mock_tg):
        """Alert repeats once per stale window (12h idle) while stale."""
        app_module.STALE_ALERT_ACTIVE = True
        app_module.last_stale_alert_time = datetime.datetime.now(
            tz=datetime.UTC
        ) - datetime.timedelta(hours=13)
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(37):
            t = now - datetime.timedelta(minutes=721 - i * 20)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 10}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()
        assert "stuck" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_heating_repeats_after_3h(self, mock_tg):
        """Heating re-alert interval tracks its 3h window, not the idle one."""
        app_module.STALE_ALERT_ACTIVE = True
        # 4h ago: past the 3h heating window, but within the old fixed 8h.
        app_module.last_stale_alert_time = datetime.datetime.now(
            tz=datetime.UTC
        ) - datetime.timedelta(hours=4)
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(13):
            t = now - datetime.timedelta(minutes=181 - i * 15)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 37}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()
        assert "stuck" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_recovery_message(self, mock_tg):
        """Recovery message sent when temp changes after stale alert."""
        app_module.last_stale_alert_time = datetime.datetime.now(
            tz=datetime.UTC
        ) - datetime.timedelta(hours=1)
        app_module.STALE_ALERT_ACTIVE = True
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(10):
            t = now - datetime.timedelta(minutes=190 - i * 20)
            app_module.temperature_history.append(
                {
                    "time": t.isoformat(),
                    "current_temp": 30.0 + i * 0.5,
                    "desired_temp": 37,
                }
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()
        assert "back" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_not_enough_readings_no_alert(self, mock_tg):
        """No alert with insufficient readings."""
        now = datetime.datetime.now(tz=datetime.UTC)
        app_module.temperature_history.append(
            {"time": now.isoformat(), "current_temp": 30.0, "desired_temp": 37}
        )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_no_false_stale_alert_after_restart_idle(self, mock_tg):
        """No stale alert when app just restarted with insufficient history.

        Reproduces: app starts, collects 3 readings over ~45min in idle mode,
        all temps similar. Should NOT alert "stuck for 6h" since we only have
        45min of data.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(4):
            t = now - datetime.timedelta(minutes=45 - i * 15)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 36.0, "desired_temp": 10}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_no_false_stale_alert_after_restart_heating(self, mock_tg):
        """No stale alert when app just restarted with insufficient history.

        In heating mode, needs 3h of data. With only 3 readings over 20min,
        should not alert.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(3):
            t = now - datetime.timedelta(minutes=20 - i * 10)
            app_module.temperature_history.append(
                {"time": t.isoformat(), "current_temp": 30.0, "desired_temp": 37}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()


class TestTelegramWebhook:
    """Tests for Telegram webhook route."""

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.send_telegram")
    def test_status_command(self, mock_tg, client):
        """Bot responds to /status with temperature info."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        resp = client.post(
            "/telegram/tok", json={"message": {"chat": {"id": 123}, "text": "/status"}}
        )
        assert resp.status_code == 200
        mock_tg.assert_called_once()
        reply_text = mock_tg.call_args[0][0]
        assert "35" in reply_text

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.send_telegram")
    def test_status_command_no_pool_data(self, mock_tg, client):
        """Bot responds to /status with error when no pool data."""
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/status"}},
        )
        mock_tg.assert_called_once()
        assert "no pool data" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.send_telegram")
    def test_status_command_with_override(self, mock_tg, client):
        """Bot shows override info in /status when override is active."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        app_module.manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=6)
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/status"}},
        )
        mock_tg.assert_called_once()
        assert "override" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.send_telegram")
    def test_schedule_command_no_config(self, mock_tg, client):
        """Bot responds to /schedule with error when no price data available."""
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/schedule"}},
        )
        mock_tg.assert_called_once()
        assert "no price data" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.send_telegram")
    def test_unauthorized_chat_rejected(self, mock_tg, client):
        """Messages from unauthorized chat IDs are rejected."""
        resp = client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 999}, "text": "/status"}},
        )
        assert resp.status_code == 200
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.send_telegram")
    def test_wrong_token_rejected(self, mock_tg, client):
        """Webhook with wrong token returns 404."""
        resp = client.post(
            "/telegram/wrong",
            json={"message": {"chat": {"id": 123}, "text": "/status"}},
        )
        assert resp.status_code == 404

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.send_telegram")
    @patch("app.control")
    def test_override_command_toggles(self, mock_control, mock_tg, client):
        """Bot responds to /override by toggling override."""
        # Enable
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/override"}},
        )
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )
        # Disable
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/override"}},
        )
        assert app_module.manual_override_endtime == datetime.datetime.fromtimestamp(
            0, tz=datetime.UTC
        )
        mock_control.assert_called_once_with(skip_override_detection=True)

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.controlmyspa.ControlMySpa")
    @patch("app.send_telegram")
    def test_heat_command(self, mock_tg, mock_spa_cls, client):
        """Bot responds to /heat by setting heat override."""
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/heat"}},
        )
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )
        mock_tg.assert_called()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.send_telegram")
    def test_schedule_command(self, mock_tg, client):
        """Bot responds to /schedule with price-based schedule."""
        tz = ZoneInfo("Europe/Helsinki")
        future_hour = (datetime.datetime.now(tz) + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        key = future_hour.isoformat()
        app_module.hourly_prices = {key: 0.05}
        app_module.heating_schedule = {key}
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/schedule"}},
        )
        mock_tg.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.send_telegram")
    def test_unknown_command_shows_help(self, mock_tg, client):
        """Unknown command returns help text."""
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/unknown"}},
        )
        mock_tg.assert_called_once()
        reply = mock_tg.call_args[0][0]
        assert "/status" in reply
        assert "/override" in reply
        assert "/hot" in reply
        assert "/cold" in reply
        assert "/schedule" in reply

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.controlmyspa.ControlMySpa")
    @patch("app.send_telegram")
    def test_hot_command(self, mock_tg, mock_spa_cls, client):
        """Bot responds to /hot same as /heat."""
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/hot"}},
        )
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )
        mock_tg.assert_called()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
        },
    )
    @patch("app.controlmyspa.ControlMySpa")
    @patch("app.send_telegram")
    def test_cold_command(self, mock_tg, mock_spa_cls, client):
        """Bot responds to /cold by setting TEMP_LOW for 24h."""
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa.desired_temp = 37
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/cold"}},
        )
        assert app_module.manual_override_endtime > datetime.datetime.now(
            tz=datetime.UTC
        )
        expected_min = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
            hours=23, minutes=59
        )
        assert app_module.manual_override_endtime > expected_min
        mock_tg.assert_called()


class TestAdminAuth:
    """Tests for admin password auth on write endpoints."""

    @patch.dict("os.environ", {"ADMIN_PASSWORD": "secret123"})
    def test_override_requires_auth(self, client):
        """POST /api/override returns 401 without auth when ADMIN_PASSWORD is set."""
        resp = client.post(
            "/api/override",
            json={"action": "enable"},
            content_type="application/json",
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"

    @patch.dict("os.environ", {"ADMIN_PASSWORD": "secret123"})
    def test_override_accepts_correct_auth(self, client):
        """POST /api/override works with correct Bearer token."""
        resp = client.post(
            "/api/override",
            json={"action": "enable"},
            content_type="application/json",
            headers={"Authorization": "Bearer secret123"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["override_active"] is True

    @patch.dict("os.environ", {"ADMIN_PASSWORD": "secret123"})
    def test_override_rejects_wrong_auth(self, client):
        """POST /api/override returns 401 with wrong Bearer token."""
        resp = client.post(
            "/api/override",
            json={"action": "enable"},
            content_type="application/json",
            headers={"Authorization": "Bearer wrongpassword"},
        )
        assert resp.status_code == 401

    def test_override_works_without_admin_password(self, client):
        """POST /api/override works normally when ADMIN_PASSWORD is not set."""
        resp = client.post(
            "/api/override",
            json={"action": "enable"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["override_active"] is True

    @patch.dict("os.environ", {"ADMIN_PASSWORD": "secret123"})
    def test_status_page_sets_auth_required_true(self, client):
        """GET / passes auth_required=True when ADMIN_PASSWORD is set."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"authRequired = true" in resp.data

    def test_status_page_sets_auth_required_false(self, client):
        """GET / passes auth_required=False when ADMIN_PASSWORD is not set."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"authRequired = false" in resp.data

    @patch.dict("os.environ", {"ADMIN_PASSWORD": "secret123"})
    def test_get_endpoints_remain_open(self, client):
        """GET endpoints don't require auth even when ADMIN_PASSWORD is set."""
        resp = client.get("/api/temperatures")
        assert resp.status_code == 200


class TestInitialize:
    """Tests for the initialize() function."""

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("app.scheduler")
    @patch("app.send_telegram")
    def test_sends_startup_telegram(self, mock_tg, mock_scheduler):
        """initialize() sends a Telegram healthcheck on startup."""
        app_module.initialize()
        mock_tg.assert_called_once()
        assert "start" in mock_tg.call_args[0][0].lower()

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_WEBHOOK_URL": "https://example.com",
        },
    )
    @patch("app.requests.post")
    @patch("app.scheduler")
    def test_registers_telegram_webhook(self, mock_scheduler, mock_post):
        """initialize() registers Telegram webhook on startup."""
        mock_post.return_value = MagicMock(status_code=200)
        app_module.initialize()
        webhook_calls = [c for c in mock_post.call_args_list if "setWebhook" in str(c)]
        assert len(webhook_calls) == 1
        assert "https://example.com/telegram/tok" in str(webhook_calls[0])

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
        },
    )
    @patch("app.requests.post")
    @patch("app.scheduler")
    def test_skips_webhook_without_url(self, mock_scheduler, mock_post):
        """initialize() skips webhook registration when TELEGRAM_WEBHOOK_URL not set."""
        mock_post.return_value = MagicMock(status_code=200)
        app_module.initialize()
        webhook_calls = [c for c in mock_post.call_args_list if "setWebhook" in str(c)]
        assert len(webhook_calls) == 0


class TestSQLitePersistence:
    """Tests for SQLite persistence."""

    def test_init_db_creates_table(self, tmp_path, monkeypatch):
        """init_db() creates the temperature_readings table."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)
        with app_module.APP.app_context():
            app_module.init_db()
        assert app_module.db_conn is not None
        cursor = app_module.db_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='temperature_readings'"
        )
        assert cursor.fetchone() is not None
        app_module.db_conn.close()

    def test_sqlite_disabled_when_dir_missing(self, monkeypatch):
        """init_db() sets db_conn to None when SQLITE_PATH dir doesn't exist."""
        monkeypatch.setenv("SQLITE_PATH", "/nonexistent/path/test.db")
        with app_module.APP.app_context():
            app_module.init_db()
        assert app_module.db_conn is None

    def test_startup_backfill_from_sqlite(self, tmp_path, monkeypatch):
        """init_db() backfills the deque from existing SQLite data."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)

        # Pre-populate the DB with rows
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE temperature_readings "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "time TEXT NOT NULL, current_temp REAL NOT NULL, "
            "desired_temp REAL NOT NULL, outside_temp REAL)"
        )
        now = datetime.datetime.now(tz=datetime.UTC)
        recent = (now - datetime.timedelta(hours=1)).isoformat()
        old = (now - datetime.timedelta(hours=100)).isoformat()
        conn.execute(
            "INSERT INTO temperature_readings "
            "(time, current_temp, desired_temp, outside_temp) "
            "VALUES (?, ?, ?, ?)",
            (recent, 35.0, 37.0, 5.0),
        )
        conn.execute(
            "INSERT INTO temperature_readings "
            "(time, current_temp, desired_temp, outside_temp) "
            "VALUES (?, ?, ?, ?)",
            (old, 30.0, 27.0, -2.0),
        )
        conn.commit()
        conn.close()

        app_module.temperature_history.clear()
        with app_module.APP.app_context():
            app_module.init_db()

        # Both rows load: the estimators need days of history, not hours
        assert len(app_module.temperature_history) == 2
        assert app_module.temperature_history[0]["current_temp"] == 30.0
        assert app_module.temperature_history[-1]["current_temp"] == 35.0
        assert app_module.temperature_history[-1]["outside_temp"] == 5.0
        app_module.db_conn.close()

    def test_startup_backfill_is_capped_at_deque_capacity(self, tmp_path, monkeypatch):
        """Backfill loads the newest readings and stops at the buffer size."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE temperature_readings "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "time TEXT NOT NULL, current_temp REAL NOT NULL, "
            "desired_temp REAL NOT NULL, outside_temp REAL)"
        )
        now = datetime.datetime.now(tz=datetime.UTC)
        capacity = app_module.temperature_history.maxlen
        for i in range(capacity + 50):
            conn.execute(
                "INSERT INTO temperature_readings "
                "(time, current_temp, desired_temp, outside_temp) VALUES (?, ?, ?, ?)",
                (
                    (
                        now - datetime.timedelta(minutes=8 * (capacity + 50 - i))
                    ).isoformat(),
                    30.0 + i % 5,
                    10.0,
                    5.0,
                ),
            )
        conn.commit()
        conn.close()

        with app_module.APP.app_context():
            app_module.init_db()

        assert len(app_module.temperature_history) == capacity
        newest = datetime.datetime.fromisoformat(
            app_module.temperature_history[-1]["time"]
        )
        assert (now - newest) < datetime.timedelta(minutes=10)
        app_module.db_conn.close()

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_set_temp_writes_to_sqlite(self, mock_api_class, tmp_path, monkeypatch):
        """set_temp() writes a row to SQLite alongside the deque."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)
        with app_module.APP.app_context():
            app_module.init_db()

        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api
        app_module.latest_outside_temp = 8.0

        with app_module.APP.app_context():
            app_module.set_temp(37)

        rows = app_module.db_conn.execute(
            "SELECT current_temp, desired_temp, outside_temp FROM temperature_readings"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == (34.5, 37.0, 8.0)
        app_module.db_conn.close()

    def test_init_db_creates_price_table(self, tmp_path, monkeypatch):
        """init_db() creates the price_history table."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)
        with app_module.APP.app_context():
            app_module.init_db()
        cursor = app_module.db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='price_history'"
        )
        assert cursor.fetchone() is not None
        app_module.db_conn.close()

    def test_startup_backfills_prices_from_sqlite(self, tmp_path, monkeypatch):
        """init_db() backfills recent prices from the price_history table."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE price_history (time TEXT PRIMARY KEY, price REAL NOT NULL)"
        )
        tz = ZoneInfo("Europe/Helsinki")
        now = datetime.datetime.now(tz).replace(minute=0, second=0, microsecond=0)
        recent = (now - datetime.timedelta(hours=100)).isoformat()
        old = (now - datetime.timedelta(hours=200)).isoformat()
        conn.execute(
            "INSERT INTO price_history (time, price) VALUES (?, ?)", (recent, 0.07)
        )
        conn.execute(
            "INSERT INTO price_history (time, price) VALUES (?, ?)", (old, 0.09)
        )
        conn.commit()
        conn.close()

        with app_module.APP.app_context():
            app_module.init_db()

        # Only prices within the in-memory window are backfilled
        assert app_module.hourly_prices == {recent: pytest.approx(0.07)}
        app_module.db_conn.close()

    def test_startup_ignores_unparsable_price_times(self, tmp_path, monkeypatch):
        """init_db() skips price rows with a broken timestamp."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SQLITE_PATH", db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE price_history (time TEXT PRIMARY KEY, price REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO price_history (time, price) VALUES (?, ?)", ("garbage", 0.07)
        )
        conn.commit()
        conn.close()

        with app_module.APP.app_context():
            app_module.init_db()

        assert app_module.hourly_prices == {}
        app_module.db_conn.close()

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    @patch("app.controlmyspa.ControlMySpa")
    def test_set_temp_works_without_sqlite(self, mock_api_class):
        """set_temp() works normally when SQLite is disabled (db_conn is None)."""
        mock_api = MagicMock()
        mock_api.current_temp = 34.5
        mock_api.desired_temp = 37
        mock_api_class.return_value = mock_api

        assert app_module.db_conn is None
        with app_module.APP.app_context():
            app_module.set_temp(37)

        assert len(app_module.temperature_history) == 1


class TestUpdatePrices:
    """Tests for spot-hinta.fi price fetching."""

    @patch("app.requests.get")
    def test_fetches_and_aggregates_hourly(self, mock_get):
        """update_prices() averages 15-min prices to hourly."""
        # 4 quarter-hour entries for hour 14:00
        today_data = [
            {"DateTime": "2026-07-18T14:00:00+03:00", "PriceWithTax": 0.04},
            {"DateTime": "2026-07-18T14:15:00+03:00", "PriceWithTax": 0.06},
            {"DateTime": "2026-07-18T14:30:00+03:00", "PriceWithTax": 0.08},
            {"DateTime": "2026-07-18T14:45:00+03:00", "PriceWithTax": 0.02},
        ]
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        assert "2026-07-18T14:00:00+03:00" in app_module.hourly_prices
        assert app_module.hourly_prices["2026-07-18T14:00:00+03:00"] == pytest.approx(
            0.05
        )

    @patch("app.requests.get")
    def test_keeps_recent_past_prices_in_memory(self, mock_get):
        """update_prices() keeps recently seen prices the API no longer returns."""
        tz = ZoneInfo("Europe/Helsinki")
        now = datetime.datetime.now(tz).replace(minute=0, second=0, microsecond=0)
        yesterday = (now - datetime.timedelta(hours=5)).isoformat()
        app_module.hourly_prices = {yesterday: 0.03}

        today_data = [
            {"DateTime": "2026-07-18T10:00:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:15:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:30:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:45:00+03:00", "PriceWithTax": 0.02},
        ]
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        assert app_module.hourly_prices[yesterday] == pytest.approx(0.03)
        assert "2026-07-18T10:00:00+03:00" in app_module.hourly_prices

    @patch("app.requests.get")
    def test_drops_stale_past_prices_from_memory(self, mock_get):
        """update_prices() forgets in-memory prices older than the chart window."""
        tz = ZoneInfo("Europe/Helsinki")
        now = datetime.datetime.now(tz).replace(minute=0, second=0, microsecond=0)
        stale = (now - datetime.timedelta(hours=200)).isoformat()
        app_module.hourly_prices = {stale: 0.03}

        today_data = [
            {"DateTime": "2026-07-18T10:00:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:15:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:30:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:45:00+03:00", "PriceWithTax": 0.02},
        ]
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        assert stale not in app_module.hourly_prices

    @patch("app.requests.get")
    def test_fresh_prices_override_remembered_ones(self, mock_get):
        """A re-fetched hour uses the fresh price, not the remembered one."""
        today_data = [
            {"DateTime": "2026-07-18T10:00:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:15:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:30:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:45:00+03:00", "PriceWithTax": 0.02},
        ]
        app_module.hourly_prices = {"2026-07-18T10:00:00+03:00": 0.99}
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        assert app_module.hourly_prices["2026-07-18T10:00:00+03:00"] == pytest.approx(
            0.02
        )

    @patch("app.requests.get")
    def test_persists_past_prices_across_restart(self, mock_get, tmp_path, monkeypatch):
        """Prices fetched before a restart are still available after init_db()."""
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
        with app_module.APP.app_context():
            app_module.init_db()

        tz = ZoneInfo("Europe/Helsinki")
        past_hour = (datetime.datetime.now(tz) - datetime.timedelta(hours=5)).replace(
            minute=0, second=0, microsecond=0
        )
        today_data = [
            {
                "DateTime": (past_hour + datetime.timedelta(minutes=m)).isoformat(),
                "PriceWithTax": 0.02,
            }
            for m in (0, 15, 30, 45)
        ]
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()
        app_module.db_conn.close()

        # Simulate a restart: memory is empty, SQLite still has the prices
        app_module.hourly_prices = {}
        with app_module.APP.app_context():
            app_module.init_db()

        assert app_module.hourly_prices[past_hour.isoformat()] == pytest.approx(0.02)
        app_module.db_conn.close()

    @patch("app.requests.get", side_effect=requests.exceptions.ConnectionError("no"))
    def test_keeps_old_prices_on_failure(self, mock_get):
        """update_prices() keeps previous data on API failure."""
        app_module.hourly_prices = {"2026-07-18T10:00:00+03:00": 0.03}
        with app_module.APP.app_context():
            app_module.update_prices()
        assert app_module.hourly_prices == {"2026-07-18T10:00:00+03:00": 0.03}

    @patch("app.requests.get")
    def test_persists_prices_to_sqlite(self, mock_get, tmp_path, monkeypatch):
        """update_prices() writes prices to price_history table."""
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
        with app_module.APP.app_context():
            app_module.init_db()

        today_data = [
            {"DateTime": "2026-07-18T10:00:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:15:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:30:00+03:00", "PriceWithTax": 0.02},
            {"DateTime": "2026-07-18T10:45:00+03:00", "PriceWithTax": 0.02},
        ]
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        rows = app_module.db_conn.execute(
            "SELECT time, price FROM price_history"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("2026-07-18T10:00:00+03:00", pytest.approx(0.02))
        app_module.db_conn.close()

    @patch.dict(
        "os.environ", {"PRICE_MARGIN_NIGHT": "4.02", "PRICE_MARGIN_DAY": "4.91"}
    )
    @patch("app.requests.get")
    def test_applies_price_margin(self, mock_get):
        """update_prices() adds time-of-day margin to spot prices."""
        today_data = [
            {"DateTime": "2026-07-18T03:00:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T03:15:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T03:30:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T03:45:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T14:00:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T14:15:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T14:30:00+03:00", "PriceWithTax": 0.01},
            {"DateTime": "2026-07-18T14:45:00+03:00", "PriceWithTax": 0.01},
        ]
        today_response = MagicMock()
        today_response.json.return_value = today_data
        today_response.raise_for_status = MagicMock()
        tomorrow_response = MagicMock()
        tomorrow_response.json.return_value = []
        tomorrow_response.raise_for_status = MagicMock()
        mock_get.side_effect = [today_response, tomorrow_response]

        with app_module.APP.app_context():
            app_module.update_prices()

        # Night hour (03:00): 0.01 + 4.02/100 = 0.0502
        assert app_module.hourly_prices["2026-07-18T03:00:00+03:00"] == pytest.approx(
            0.0502
        )
        # Day hour (14:00): 0.01 + 4.91/100 = 0.0591
        assert app_module.hourly_prices["2026-07-18T14:00:00+03:00"] == pytest.approx(
            0.0591
        )


class TestPriceScheduleAPI:
    """Tests for price-based schedule in API and Telegram."""

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    def test_api_temperatures_returns_prices(self, client):
        """GET /api/temperatures returns price schedule with heating flag."""
        tz = ZoneInfo("Europe/Helsinki")
        future_hour = (datetime.datetime.now(tz) + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        key = future_hour.isoformat()
        app_module.hourly_prices = {key: 0.05}
        app_module.heating_schedule = {key}
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert len(data["future"]) == 1
        assert data["future"][0]["price"] == pytest.approx(0.05)
        assert data["future"][0]["heating"] is True

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    def test_api_temperatures_returns_past_prices(self, client):
        """GET /api/temperatures includes past prices for the chart overlay."""
        tz = ZoneInfo("Europe/Helsinki")
        past_hour = (datetime.datetime.now(tz) - datetime.timedelta(hours=3)).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.hourly_prices = {past_hour.isoformat(): 0.06}
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert data["future"] == []
        assert len(data["prices"]) == 1
        assert data["prices"][0]["price"] == pytest.approx(0.06)

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"},
    )
    @patch("app.send_telegram")
    def test_telegram_schedule_shows_prices(self, mock_tg, client):
        """Bot /schedule shows prices with heating hours marked."""
        tz = ZoneInfo("Europe/Helsinki")
        future_hour = (datetime.datetime.now(tz) + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        key = future_hour.isoformat()
        app_module.hourly_prices = {key: 0.045}
        app_module.heating_schedule = {key}
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/schedule"}},
        )
        mock_tg.assert_called_once()
        reply = mock_tg.call_args[0][0]
        assert "0.045" in reply or "4.5" in reply


class TestCoolingModel:
    """Tests for cooling rate estimation and temperature prediction."""

    def test_estimate_cooling_rate(self):
        """estimate_cooling_rate() calculates k from temperature drops."""
        now = datetime.datetime.now(tz=datetime.UTC)
        # Simulate cooling: pool at 37°C, outside 20°C, dropping 0.5°C over 5h
        # k = (0.5/5) / (37 - 20) = 0.1 / 17 ≈ 0.00588
        for i in range(3):
            app_module.temperature_history.append(
                {
                    "time": (now - datetime.timedelta(hours=10 - i * 5)).isoformat(),
                    "current_temp": 37.0 - i * 0.5,
                    "desired_temp": 10.0,
                    "outside_temp": 20.0,
                }
            )
        result = app_module.estimate_cooling_rate()
        assert result == pytest.approx(0.00588, abs=0.001)
        assert app_module.cooling_k == result

    def test_estimate_cooling_rate_default(self):
        """estimate_cooling_rate() returns DEFAULT_COOLING_K with insufficient data."""
        result = app_module.estimate_cooling_rate()
        assert result == app_module.DEFAULT_COOLING_K
        assert app_module.cooling_k == app_module.DEFAULT_COOLING_K

    def test_predict_time_to_temp(self):
        """predict_time_to_temp() calculates hours until target temp."""
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        # Pool 37°C, outside 20°C, target 34°C
        # Iterative simulation gives ~33h (compounds cooling rate as pool cools)
        hours = app_module.predict_time_to_temp(34.0, 37.0)
        assert 25 < hours < 40

    def test_predict_time_to_temp_already_below(self):
        """predict_time_to_temp() returns 0 when pool is already at or below target."""
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        hours = app_module.predict_time_to_temp(34.0, 33.0)
        assert hours == 0.0

    def test_predict_time_to_temp_cold_outside(self):
        """predict_time_to_temp() returns shorter time with cold outside temp."""
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        hours_warm = app_module.predict_time_to_temp(34.0, 37.0)
        app_module.latest_outside_temp = 0.0
        hours_cold = app_module.predict_time_to_temp(34.0, 37.0)
        assert hours_cold < hours_warm


class TestEnhancedAPI:
    """Tests for enhanced /api/temperatures response."""

    @patch.dict(
        "os.environ",
        {
            "TEMP_HIGH": "37",
            "TEMP_LOW": "10",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_api_returns_predicted_temps(self, client):
        """API returns predicted future temperature curve."""
        tz = ZoneInfo("Europe/Helsinki")
        now = datetime.datetime.now(tz=datetime.UTC)
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 36.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        now_local = datetime.datetime.now(tz)
        future_hour = (now_local + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.hourly_prices = {future_hour.isoformat(): 0.05}
        app_module.heating_schedule = set()
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert "predicted_temps" in data
        assert "cooling_k" in data
        assert "temp_min" in data
        assert "predicted_deadline" in data
        assert "prices" in data
        assert data["cooling_k"] == pytest.approx(0.006)
        assert data["temp_min"] == 34
        assert len(data["predicted_temps"]) > 0
        assert len(data["prices"]) > 0


class TestCalculateSchedule:
    """Tests for cooling-rate-based schedule calculation."""

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_schedule_heats_before_deadline(self):
        """Schedules heating in cheapest hours before pool hits TEMP_MIN."""
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        # Pool at 35°C, outside 20°C, k=0.006
        # Deadline: (35-34) / (0.006 * 15) = 11.1 hours
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 35.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        # A full day of prices, hour 5 cheapest: with only a few hours of
        # prices the scheduler would rightly wait for the rest to publish.
        now_local = datetime.datetime.now(tz)
        prices = {}
        for i in range(24):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = 0.05 if i != 5 else 0.001
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Should include the cheapest hour (hour 5)
        cheapest_key = sorted(prices, key=prices.get)[0]
        assert cheapest_key in app_module.heating_schedule
        # hours_needed = ceil((37-35)/2.5) = 1 (based on current temp, not TEMP_MIN)
        assert len(app_module.heating_schedule) == 1

    ENV: typing.ClassVar = {
        "HEATING_HOURS": "6",
        "TEMP_HIGH": "37",
        "TEMP_MIN": "34",
        "HEATING_RATE": "1.6",
    }

    @staticmethod
    def _seed(current_temp, hours, price_at=None):
        """Seed one reading plus `hours` of hourly prices; return the hour keys."""
        tz = ZoneInfo("Europe/Helsinki")
        app_module.latest_outside_temp = 15.0
        app_module.temperature_history.append(
            {
                "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "current_temp": current_temp,
                "desired_temp": 10.0,
                "outside_temp": 15.0,
            }
        )
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        keys = [
            (current_hour + datetime.timedelta(hours=i)).isoformat()
            for i in range(hours)
        ]
        price_at = price_at or {}
        app_module.hourly_prices = {
            k: price_at.get(i, 0.20) for i, k in enumerate(keys)
        }
        return keys

    @patch.dict("os.environ", ENV)
    def test_no_schedule_before_the_first_reading(self):
        """A restart must not book heating before any temperature is known."""
        self._seed(35.0, 24)
        app_module.temperature_history.clear()  # as on a cold start
        app_module.calculate_schedule()

        assert app_module.heating_schedule == set()

    @patch.dict("os.environ", ENV)
    def test_waits_when_the_deadline_is_beyond_the_price_horizon(self):
        """Nothing is booked while cheaper hours may still be published.

        Only 6 hours of prices are known and TEMP_MIN is ~13h away, so every
        known hour may be dearer than what publishes at 14:00.
        """
        self._seed(35.5, 6, price_at={3: 0.05})
        app_module.calculate_schedule()

        assert app_module.heating_schedule == set()

    @patch.dict("os.environ", ENV)
    def test_waiting_keeps_hours_already_booked(self):
        """Deferring must not cancel a block that is about to start."""
        keys = self._seed(35.5, 6, price_at={3: 0.05})
        app_module.heating_schedule = {keys[1]}
        app_module.calculate_schedule()

        assert app_module.heating_schedule == {keys[1]}

    @patch.dict("os.environ", ENV)
    def test_a_sensor_tick_below_target_does_not_book_heating(self):
        """A 0.2°C dip stays inside TEMP_DEADBAND, so no hour is booked.

        The spa reports in 0.5°C steps; without a deadband every step down
        books a fresh hour at whatever price happens to be cheapest next.
        """
        self._seed(36.8, 24, price_at={1: 0.01})
        app_module.calculate_schedule()

        assert app_module.heating_schedule == set()

    @patch.dict("os.environ", ENV)
    def test_sizes_the_block_from_the_temperature_at_its_start(self):
        """Hours are sized from the pool's temperature when heating begins.

        The pool is 0.8°C below target now — one hour's worth — but the cheap
        hours are 10h away, by which time it has cooled enough to need two.
        """
        keys = self._seed(36.2, 24, price_at={10: 0.01, 11: 0.02})
        app_module.calculate_schedule()

        assert app_module.heating_schedule == {keys[10], keys[11]}

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "1.6",
        },
    )
    def test_rejects_a_cheaper_hour_beyond_the_deadline(self):
        """A bargain after the TEMP_MIN deadline loses to hours before it."""
        tz = ZoneInfo("Europe/Helsinki")
        app_module.latest_outside_temp = 15.0
        app_module.temperature_history.append(
            {
                "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "current_temp": 34.2,  # ~2h of slack before TEMP_MIN
                "desired_temp": 10.0,
                "outside_temp": 15.0,
            }
        )
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        hour = {i: (current_hour + datetime.timedelta(hours=i)) for i in range(24)}
        prices = {dt.isoformat(): 0.20 for dt in hour.values()}
        prices[hour[0].isoformat()] = 0.05
        prices[hour[1].isoformat()] = 0.06
        prices[hour[20].isoformat()] = 0.001  # cheapest, but far too late
        app_module.hourly_prices = prices
        app_module.calculate_schedule()

        assert hour[20].isoformat() not in app_module.heating_schedule
        assert app_module.heating_schedule == {
            hour[0].isoformat(),
            hour[1].isoformat(),
        }

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "0.5",
        },
    )
    def test_looks_past_the_deadline_when_it_cannot_be_met(self):
        """With too few hours before the deadline, the whole future is fair game.

        The pool cannot be brought up in time either way, so the scheduler
        stops honouring the deadline rather than buying expensive hours that
        would not save it.
        """
        tz = ZoneInfo("Europe/Helsinki")
        app_module.latest_outside_temp = 15.0
        app_module.temperature_history.append(
            {
                "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "current_temp": 34.2,
                "desired_temp": 10.0,
                "outside_temp": 15.0,
            }
        )
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        hour = {i: (current_hour + datetime.timedelta(hours=i)) for i in range(24)}
        prices = {dt.isoformat(): 0.20 for dt in hour.values()}
        prices[hour[20].isoformat()] = 0.001
        app_module.hourly_prices = prices
        app_module.calculate_schedule()

        # 6 hours needed at 0.5°C/h, only 3 before the deadline -> fall back
        assert hour[20].isoformat() in app_module.heating_schedule

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "1.6",
        },
    )
    def test_chooses_the_cheapest_set_on_a_jagged_curve(self):
        """The chosen hours are exactly the cheapest available, not merely cheap."""
        tz = ZoneInfo("Europe/Helsinki")
        app_module.latest_outside_temp = 15.0
        app_module.temperature_history.append(
            {
                "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "current_temp": 32.5,  # below TEMP_MIN: every future hour qualifies
                "desired_temp": 10.0,
                "outside_temp": 15.0,
            }
        )
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        prices = {
            (current_hour + datetime.timedelta(hours=i)).isoformat(): c / 100
            for i, c in enumerate(NORDPOOL_DAY)
        }
        app_module.hourly_prices = prices
        app_module.calculate_schedule()

        # ceil((37 - 32.5) / 1.6) = 3 hours
        assert app_module.heating_schedule == set(sorted(prices, key=prices.get)[:3])

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "1.6",
        },
    )
    def test_equal_prices_resolve_to_the_earliest_hours(self):
        """Ties go to the earliest hour, whatever order the prices arrived in."""
        tz = ZoneInfo("Europe/Helsinki")
        app_module.latest_outside_temp = 15.0
        app_module.temperature_history.append(
            {
                "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "current_temp": 32.5,
                "desired_temp": 10.0,
                "outside_temp": 15.0,
            }
        )
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        hours = [current_hour + datetime.timedelta(hours=i) for i in range(12)]
        # Insert newest first: dict order is the opposite of chronological order
        app_module.hourly_prices = {dt.isoformat(): 0.05 for dt in reversed(hours)}
        app_module.calculate_schedule()

        assert app_module.heating_schedule == {h.isoformat() for h in hours[:3]}

    @patch.dict(
        "os.environ",
        {"HEATING_HOURS": "6", "TEMP_HIGH": "37", "TEMP_MIN": "34"},
    )
    def test_default_heating_rate_books_enough_hours(self):
        """The default heating rate books 2 hours to close a 2°C gap.

        Measured production rate is ~1.6°C/h. Booking a single hour (as a
        2.5°C/h estimate would) leaves the pool short, forcing a top-off in
        whatever hour is cheapest *next* — typically a pricier one.
        """
        os.environ.pop("HEATING_RATE", None)
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 35.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        now_local = datetime.datetime.now(tz)
        prices = {}
        for i in range(24):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = {5: 0.001, 6: 0.002}.get(i, 0.05)
        app_module.hourly_prices = prices
        app_module.calculate_schedule()

        two_cheapest = set(sorted(prices, key=prices.get)[:2])
        assert app_module.heating_schedule == two_cheapest

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_schedule_immediate_when_below_min(self):
        """Schedules heating immediately when pool is below TEMP_MIN."""
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 33.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        now_local = datetime.datetime.now(tz)
        current_hour = now_local.replace(minute=0, second=0, microsecond=0)
        prices = {}
        for i in range(6):
            dt = current_hour + datetime.timedelta(hours=i)
            prices[dt.isoformat()] = float(i + 1)
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Current hour should be in schedule (immediate heating)
        assert current_hour.isoformat() in app_module.heating_schedule

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "2",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_schedule_capped_by_budget(self):
        """Schedule respects HEATING_HOURS safety cap."""
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 33.0,  # below TEMP_MIN, needs heating
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        now_local = datetime.datetime.now(tz)
        prices = {}
        for i in range(12):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = 0.01
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Cap at HEATING_HOURS=2 even though model might want more
        assert len(app_module.heating_schedule) <= 2

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_budget_window_14_to_14(self):
        """Schedule counts heated hours in 14:00-14:00 window only."""
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        # Pool needs heating
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 33.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        # Add 5 heated hours in the current 14:00 window
        now_local = datetime.datetime.now(tz)
        window_start = now_local.replace(hour=14, minute=0, second=0, microsecond=0)
        if now_local.hour < 14:
            window_start -= datetime.timedelta(days=1)
        for i in range(5):
            t = window_start + datetime.timedelta(hours=i)
            app_module.temperature_history.append(
                {
                    "time": t.astimezone(datetime.UTC).isoformat(),
                    "current_temp": 36.0,
                    "desired_temp": 37.0,
                    "outside_temp": 20.0,
                }
            )
        prices = {}
        for i in range(12):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = 0.01
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Budget is 6, used 5 in window, remaining 1
        assert len(app_module.heating_schedule) <= 1

    @patch.dict(
        "os.environ",
        {
            "HEATING_HOURS": "6",
            "TEMP_HIGH": "37",
            "TEMP_MIN": "34",
            "HEATING_RATE": "2.5",
        },
    )
    def test_schedule_with_hot_pool(self):
        """Hot pool schedules at least one cheap hour far out."""
        now = datetime.datetime.now(tz=datetime.UTC)
        tz = ZoneInfo("Europe/Helsinki")
        app_module.cooling_k = 0.006
        app_module.latest_outside_temp = 20.0
        app_module.temperature_history.append(
            {
                "time": now.isoformat(),
                "current_temp": 37.0,
                "desired_temp": 10.0,
                "outside_temp": 20.0,
            }
        )
        now_local = datetime.datetime.now(tz)
        prices = {}
        for i in range(24):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = float(i)  # cheapest is hour 0
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Pool is already at TEMP_HIGH — no heating needed
        assert len(app_module.heating_schedule) == 0


# --- Scheduling simulation ---


DAY_AHEAD_PUBLISH_HOUR = 14  # spot-hinta.fi publishes tomorrow's prices ~14:00


class _Clock(datetime.datetime):
    """A datetime whose now() returns a settable simulated instant."""

    _now = datetime.datetime.now(tz=datetime.UTC)

    @classmethod
    def now(cls, tz=None):
        """Return the simulated now, converted to tz when one is given."""
        return cls._now.astimezone(tz) if tz else cls._now

    @classmethod
    def advance(cls, **kwargs):
        """Move the simulated clock forward."""
        cls._now += datetime.timedelta(**kwargs)


def _fake_datetime_module():
    """Build a stand-in for app's `datetime` import, with a controllable clock.

    Patching app.datetime (rather than datetime.datetime globally) keeps the
    simulated clock confined to the application module.
    """
    return SimpleNamespace(
        datetime=_Clock, timedelta=datetime.timedelta, UTC=datetime.UTC
    )


class SpaSimulator:
    """Run the real scheduler through simulated time against a thermal model.

    Each 15-minute tick calls calculate_schedule() and control() exactly as
    production does, then advances a pool model: heating adds `heating_rate`
    per hour while the setpoint is above the pool, cooling follows the same
    Newton's law the app uses. Readings are quantised to 0.5°C to reproduce the
    spa's sensor resolution, which is what makes the pool flap around the
    setpoint in production.
    """

    TICK_HOURS = 0.25

    def __init__(  # noqa: PLR0913
        self,
        prices,
        *,
        start_temp,
        outside=15.0,
        heating_rate=1.6,
        cooling_k=0.004,
        temp_high=37,
        day_ahead_known=False,
    ):
        """Set up the pool model and the price curve it is scheduled against."""
        self.prices = prices
        self.day_ahead_known = day_ahead_known
        self.window = None
        self.temp = start_temp
        self.outside = outside
        self.heating_rate = heating_rate
        self.cooling_k = cooling_k
        self.temp_high = temp_high
        self.heating_hours = 0.0
        self.cost = 0.0
        self.starts = 0  # times the setpoint went high (the blue line's steps)
        self.min_temp = start_temp
        self._setpoint_high = False

    def _hour_key(self):
        return (
            _Clock.now(ZoneInfo("Europe/Helsinki"))
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )

    def set_temp(self, temp, *, skip_override_detection=False):
        """Stand in for app.set_temp(): apply the setpoint, then step physics."""
        setpoint_high = temp >= self.temp_high
        if setpoint_high and not self._setpoint_high:
            self.starts += 1
        self._setpoint_high = setpoint_high
        heating = temp > self.temp

        if heating:
            self.temp = min(self.temp + self.heating_rate * self.TICK_HOURS, temp)
            self.heating_hours += self.TICK_HOURS
            self.cost += self.TICK_HOURS * self.prices.get(self._hour_key(), 0.0)
        else:
            self.temp -= self.cooling_k * (self.temp - self.outside) * self.TICK_HOURS

        self.min_temp = min(self.min_temp, self.temp)
        app_module.temperature_history.append(
            {
                "time": _Clock.now(datetime.UTC).isoformat(),
                "current_temp": round(self.temp * 2) / 2,  # 0.5°C sensor resolution
                "desired_temp": float(temp),
                "outside_temp": self.outside,
            }
        )

    def _publish_prices(self):
        """Expose only the prices spot-hinta.fi would have published by now.

        Today's prices are always known; tomorrow's appear at 14:00. Before
        that the scheduler can only choose among the remaining hours of today,
        which is what makes an expensive morning hour look like the best
        available option.
        """
        if self.day_ahead_known:
            app_module.hourly_prices = dict(self.prices)
            return
        now_local = _Clock.now(ZoneInfo("Europe/Helsinki"))
        horizon = now_local.date() + datetime.timedelta(
            days=2 if now_local.hour >= DAY_AHEAD_PUBLISH_HOUR else 1
        )
        app_module.hourly_prices = {
            k: v
            for k, v in self.prices.items()
            if datetime.datetime.fromisoformat(k).date() < horizon
        }

    def run(self, hours):
        """Step scheduler + control for the given number of simulated hours."""
        app_module.latest_outside_temp = self.outside
        start = _Clock.now(ZoneInfo("Europe/Helsinki"))
        self.window = (start, start + datetime.timedelta(hours=hours))
        with (
            patch("app.datetime", _fake_datetime_module()),
            patch("app.set_temp", self.set_temp),
        ):
            for _ in range(int(hours / self.TICK_HOURS)):
                self._publish_prices()
                app_module.calculate_schedule()
                app_module.control()
                _Clock.advance(minutes=15)
        return self

    def cheapest_possible_cost(self):
        """Cost of the same number of heating hours bought at the best prices.

        Only hours inside the simulated window count, so the baseline is one
        the scheduler could actually have picked.
        """
        start, end = self.window
        cheapest = sorted(
            v
            for k, v in self.prices.items()
            if start <= datetime.datetime.fromisoformat(k) < end
        )
        whole, part = divmod(self.heating_hours, 1)
        return sum(cheapest[: int(whole)]) + part * cheapest[int(whole)]


# Realistic Finnish day: cheap night trough, morning ramp, evening peak (c/kWh)
NORDPOOL_DAY = [
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


def _price_curve(start, days=2, shape=None):
    """Build {ISO local hour: EUR/kWh} from an hourly c/kWh shape."""
    shape = NORDPOOL_DAY if shape is None else shape
    return {
        (start + datetime.timedelta(days=d, hours=i)).isoformat(): c / 100
        for d in range(days)
        for i, c in enumerate(shape)
    }


def _simulate(start_temp, *, hours=24, day_ahead_known=False, **kwargs):
    """Run a simulation starting at midnight on a fixed date."""
    start = datetime.datetime(
        2026, 8, 20, 0, 0, tzinfo=ZoneInfo("Europe/Helsinki")
    ) + datetime.timedelta(hours=kwargs.pop("start_hour", 0))
    prices = _price_curve(
        start.replace(hour=0), shape=kwargs.pop("shape", None), days=3
    )
    _Clock._now = start.astimezone(datetime.UTC)  # noqa: SLF001
    app_module.heating_schedule = set()
    return SpaSimulator(
        prices, start_temp=start_temp, day_ahead_known=day_ahead_known, **kwargs
    ).run(hours)


@patch.dict(
    "os.environ",
    {"HEATING_HOURS": "6", "TEMP_HIGH": "37", "TEMP_LOW": "10", "TEMP_MIN": "34"},
)
class TestSchedulingSimulation:
    """End-to-end scheduling behaviour over simulated time.

    Static schedule tests only prove one decision in a frozen world. These run
    the real calculate_schedule() + control() loop every 15 minutes against a
    thermal model, which is where re-planning behaviour actually shows up.
    """

    def test_picks_the_cheapest_hours_when_the_whole_curve_is_known(self):
        """With all prices known, heating lands in the night trough."""
        sim = _simulate(35.5, day_ahead_known=True)
        assert sim.heating_hours > 0
        # Not exactly optimal: the cooling estimate starts at DEFAULT_COOLING_K
        # until enough history accumulates, so the first block is sized a
        # little long and its tail spills into a pricier hour.
        assert sim.cost <= sim.cheapest_possible_cost() * 1.10

    def test_pool_never_falls_below_temp_min(self):
        """The safety floor holds across a full day from a cold-ish start."""
        sim = _simulate(34.5)
        assert sim.min_temp >= 34.0

    def test_pool_recovers_from_below_temp_min(self):
        """A pool starting under TEMP_MIN is heated back above it."""
        sim = _simulate(33.0, hours=12)
        assert sim.temp > 34.0

    def test_respects_the_heating_hours_budget(self):
        """Heating stays within HEATING_HOURS over a 14:00-14:00 window."""
        sim = _simulate(33.0)
        assert sim.heating_hours <= 6

    def test_cost_stays_near_optimal_with_realistic_day_ahead_timing(self):
        """Heating stays near the cheapest hours despite a truncated horizon.

        Tomorrow's prices only publish at 14:00, so before then the cheapest
        *known* hour is an expensive one later today. TEMP_MIN is not at risk
        that soon, so the scheduler waits for the real trough to appear.
        """
        sim = _simulate(36.5, start_hour=6)
        assert sim.cost <= sim.cheapest_possible_cost() * 1.10

    def test_does_not_cycle_the_setpoint(self):
        """A day should need a couple of heating blocks, not many short ones."""
        sim = _simulate(36.5, start_hour=6)
        assert sim.starts <= 3


class TestHeatingRateEstimation:
    """Tests for measuring the spa's heating rate from history."""

    @staticmethod
    def _stretch(start_time, temps, desired):
        """Append readings 15 minutes apart with the given desired temp."""
        for i, temp in enumerate(temps):
            app_module.temperature_history.append(
                {
                    "time": (
                        start_time + datetime.timedelta(minutes=15 * i)
                    ).isoformat(),
                    "current_temp": temp,
                    "desired_temp": desired,
                    "outside_temp": 15.0,
                }
            )

    @patch.dict("os.environ", {"TEMP_HIGH": "37"})
    def test_measures_the_rate_from_heating_stretches(self):
        """Three uncapped stretches gaining 1.0°C/h estimate 1.0°C/h."""
        base = datetime.datetime(2026, 8, 20, 0, 0, tzinfo=datetime.UTC)
        for day in range(3):
            start = base + datetime.timedelta(days=day)
            self._stretch(start, [34.0, 34.25, 34.5, 34.75, 35.0], 37.0)
            self._stretch(start + datetime.timedelta(hours=2), [35.0], 10.0)

        assert app_module.estimate_heating_rate() == pytest.approx(1.0)

    @patch.dict("os.environ", {"TEMP_HIGH": "37"})
    def test_ignores_stretches_capped_at_the_setpoint(self):
        """A stretch that reaches TEMP_HIGH understates the rate, so it is skipped."""
        base = datetime.datetime(2026, 8, 20, 0, 0, tzinfo=datetime.UTC)
        self._stretch(base, [36.5, 36.75, 37.0, 37.0, 37.0], 37.0)
        self._stretch(base + datetime.timedelta(hours=2), [37.0], 10.0)

        assert app_module.estimate_heating_rate() == app_module.DEFAULT_HEATING_RATE

    @patch.dict("os.environ", {"TEMP_HIGH": "37"})
    def test_falls_back_to_the_default_without_enough_stretches(self):
        """One stretch is not enough evidence to move the estimate."""
        base = datetime.datetime(2026, 8, 20, 0, 0, tzinfo=datetime.UTC)
        self._stretch(base, [34.0, 34.5, 35.0, 35.5, 36.0], 37.0)
        self._stretch(base + datetime.timedelta(hours=2), [36.0], 10.0)

        assert app_module.estimate_heating_rate() == app_module.DEFAULT_HEATING_RATE

    @patch.dict("os.environ", {"TEMP_HIGH": "37"})
    def test_clamps_an_implausible_estimate(self):
        """A 0.5°C sensor step over 15 minutes reads as 2°C/h; clamped, not trusted."""
        base = datetime.datetime(2026, 8, 20, 0, 0, tzinfo=datetime.UTC)
        for day in range(3):
            start = base + datetime.timedelta(days=day)
            self._stretch(start, [30.0, 33.0, 34.0], 37.0)  # 6°C/h
            self._stretch(start + datetime.timedelta(hours=2), [34.0], 10.0)

        assert app_module.estimate_heating_rate() == app_module.MAX_HEATING_RATE

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "HEATING_RATE": "2.2"})
    def test_env_override_wins_over_the_measurement(self):
        """An explicit HEATING_RATE is respected even once a rate is measured."""
        app_module.heating_rate = 1.1
        assert app_module._heating_rate() == pytest.approx(2.2)  # noqa: SLF001


class TestHistoryAPI:
    """Tests for the SQLite-backed history endpoint."""

    @staticmethod
    def _db(tmp_path, monkeypatch):
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
        with app_module.APP.app_context():
            app_module.init_db()

    def test_returns_readings_and_prices_in_range(self, client, tmp_path, monkeypatch):
        """The endpoint serves rows straight from SQLite, not the memory window."""
        self._db(tmp_path, monkeypatch)
        old = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=30)
        app_module.db_conn.execute(
            "INSERT INTO temperature_readings "
            "(time, current_temp, desired_temp, outside_temp) VALUES (?, ?, ?, ?)",
            (old.isoformat(), 35.0, 37.0, 5.0),
        )
        app_module.db_conn.execute(
            "INSERT INTO price_history (time, price) VALUES (?, ?)",
            (old.astimezone(ZoneInfo("Europe/Helsinki")).isoformat(), 0.07),
        )
        app_module.db_conn.commit()

        resp = client.get(
            "/api/history",
            query_string={
                "from": (old - datetime.timedelta(days=1)).isoformat(),
                "to": (old + datetime.timedelta(days=1)).isoformat(),
            },
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data["readings"]) == 1
        assert data["readings"][0]["current_temp"] == 35.0
        assert len(data["prices"]) == 1
        assert data["prices"][0]["price"] == pytest.approx(0.07)
        app_module.db_conn.close()

    def test_excludes_rows_outside_the_range(self, client, tmp_path, monkeypatch):
        """Rows outside from/to are not returned."""
        self._db(tmp_path, monkeypatch)
        old = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=30)
        app_module.db_conn.execute(
            "INSERT INTO temperature_readings "
            "(time, current_temp, desired_temp, outside_temp) VALUES (?, ?, ?, ?)",
            (old.isoformat(), 35.0, 37.0, 5.0),
        )
        app_module.db_conn.commit()

        resp = client.get("/api/history")  # defaults to the last 7 days
        assert resp.get_json()["readings"] == []
        app_module.db_conn.close()

    def test_rejects_an_unparsable_timestamp(self, client, tmp_path, monkeypatch):
        """A bad from/to is a client error, not a 500."""
        self._db(tmp_path, monkeypatch)
        resp = client.get("/api/history", query_string={"from": "yesterday"})
        assert resp.status_code == 400
        app_module.db_conn.close()

    def test_reports_when_sqlite_is_disabled(self, client):
        """Without SQLite there is no history to serve."""
        assert app_module.db_conn is None
        resp = client.get("/api/history")
        assert resp.status_code == 503
