"""Tests for controlmyspa-porssari application."""

import datetime
import json
import sqlite3
import time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

import app as app_module


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset global state between tests."""
    app_module.porssari_config = {}
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
    yield


@pytest.fixture
def client():
    """Flask test client."""
    app_module.APP.config["TESTING"] = True
    with app_module.APP.test_client() as c:
        yield c


@pytest.fixture
def sample_porssari_config():
    """Sample porssari.fi API response."""
    return {
        "Metadata": {
            "Mac": "A1B2C3D4E5F6",
            "Channels": "1",
            "Date": "2023-12-16",
            "Time": "21:26:00",
            "Timestamp": "1702754760",
            "Timestamp_offset": "7200",
            "Hours_count": 24,
        },
        "Channel1": {
            "0": "0",
            "1": "1",
            "2": "1",
            "3": "0",
            "10": "0",
            "11": "0",
            "12": "1",
            "13": "0",
            "21": "1",
            "22": "1",
            "23": "0",
        },
    }


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

    def test_status_page_shows_porssari_config(self, client, sample_porssari_config):
        """Status page shows porssari schedule when available."""
        app_module.porssari_config = sample_porssari_config
        app_module.cache.set("pool", {"current_temp": 35, "desired_temp": 37})
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Porssari Schedule" in resp.data


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

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    def test_returns_future_schedule(self, client, sample_porssari_config):
        """Returns future porssari schedule mapped to temperatures."""
        app_module.porssari_config = sample_porssari_config
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert len(data["future"]) > 0
        targets = {p["target_temp"] for p in data["future"]}
        assert targets <= {27, 37}

    @patch.dict("os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"})
    def test_future_schedule_within_24h(self, client, sample_porssari_config):
        """Future schedule entries are all within 24h from now."""
        app_module.porssari_config = sample_porssari_config
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        now = datetime.datetime.now(tz=datetime.UTC)
        limit = now + datetime.timedelta(hours=24)
        for point in data["future"]:
            dt = datetime.datetime.fromisoformat(point["time"])
            assert dt > now, f"Future entry should be after now, got {dt}"
            assert dt <= limit, f"Future entry should be within 24h, got {dt}"

    def test_history_maxlen(self, client):
        """Temperature history respects 999-point max."""
        for i in range(1100):
            app_module.temperature_history.append(
                {
                    "time": f"2024-01-01T{i:05d}",
                    "current_temp": 30 + (i % 10),
                    "desired_temp": 37,
                }
            )
        assert len(app_module.temperature_history) == 999


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
    def test_no_config_skips_control(self, mock_set_temp):
        """Control does nothing without porssari config."""
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TEMP_HIGH": "37", "TEMP_LOW": "27", "TEMP_OVERRIDE": "0"},
    )
    @patch("app.set_temp")
    def test_command_0_sets_low(self, mock_set_temp):
        """Command '0' sets TEMP_LOW."""
        app_module.porssari_config = {"Channel1": {str(h): "0" for h in range(24)}}
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(27, skip_override_detection=False)

    @patch.dict(
        "os.environ",
        {"TEMP_HIGH": "37", "TEMP_LOW": "27", "TEMP_OVERRIDE": "0"},
    )
    @patch("app.set_temp")
    def test_command_1_sets_high(self, mock_set_temp):
        """Command '1' sets TEMP_HIGH."""
        app_module.porssari_config = {"Channel1": {str(h): "1" for h in range(24)}}
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(37, skip_override_detection=False)

    @patch.dict(
        "os.environ",
        {"TEMP_HIGH": "37", "TEMP_LOW": "27", "TEMP_OVERRIDE": "40"},
    )
    @patch("app.set_temp")
    def test_override_env_sets_override_temp(self, mock_set_temp):
        """TEMP_OVERRIDE env var overrides all logic."""
        app_module.porssari_config = {"Channel1": {str(h): "0" for h in range(24)}}
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


# --- update_porssari tests ---


class TestUpdatePorssari:
    """Tests for the update_porssari() function."""

    @patch("app.scheduler")
    @patch("app.requests.get")
    def test_parses_config(self, mock_get, mock_scheduler):
        """Successfully parses porssari API response."""
        config = {
            "Channel1": {"0": "1", "1": "0"},
            "Metadata": {"Mac": "test"},
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(config)
        mock_get.return_value = mock_response

        with app_module.APP.app_context():
            app_module.update_porssari()

        assert app_module.porssari_config == config

    @patch("app.scheduler")
    @patch("app.requests.get")
    def test_handles_whitespace(self, mock_get, mock_scheduler):
        """Handles extra whitespace in porssari response."""
        config = {"Channel1": {"0": "1"}, "Metadata": {}}
        mock_response = MagicMock()
        mock_response.text = "\n  " + json.dumps(config) + "\n"
        mock_get.return_value = mock_response

        with app_module.APP.app_context():
            app_module.update_porssari()

        assert app_module.porssari_config == config


# --- update_weather tests ---


class TestUpdateWeather:
    """Tests for the update_weather() function."""

    @patch("app.requests.get")
    def test_fetches_outside_temp(self, mock_get):
        """Successfully parses Open-Meteo response and stores outside temp."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"current": {"temperature_2m": 12.3}}
        mock_get.return_value = mock_response

        with app_module.APP.app_context():
            app_module.update_weather()

        assert app_module.latest_outside_temp == 12.3

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
        """Bot responds to /schedule with error when no config."""
        client.post(
            "/telegram/tok",
            json={"message": {"chat": {"id": 123}, "text": "/schedule"}},
        )
        mock_tg.assert_called_once()
        assert "no schedule" in mock_tg.call_args[0][0].lower()

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
    def test_schedule_command(self, mock_tg, client, sample_porssari_config):
        """Bot responds to /schedule with porssari schedule."""
        app_module.porssari_config = sample_porssari_config
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
        old = (now - datetime.timedelta(hours=72)).isoformat()
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

        # Only the recent row (within 48h) should be backfilled
        assert len(app_module.temperature_history) == 1
        assert app_module.temperature_history[0]["current_temp"] == 35.0
        assert app_module.temperature_history[0]["outside_temp"] == 5.0
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


class TestCalculateSchedule:
    """Tests for cheapest-hours schedule calculation."""

    @patch.dict("os.environ", {"HEATING_HOURS": "2", "TEMP_HIGH": "37"})
    def test_picks_cheapest_hours(self):
        """calculate_schedule() picks the N cheapest future hours."""
        now = datetime.datetime.now(ZoneInfo("Europe/Helsinki"))
        # Create prices for next 6 hours
        prices = {}
        for i in range(6):
            dt = (now + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = float(5 - i)  # 5,4,3,2,1,0 — last is cheapest
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Should pick the 2 cheapest (hours 4 and 5, prices 1.0 and 0.0)
        assert len(app_module.heating_schedule) == 2
        cheapest_keys = sorted(prices, key=prices.get)[:2]
        assert app_module.heating_schedule == set(cheapest_keys)

    @patch.dict("os.environ", {"HEATING_HOURS": "3", "TEMP_HIGH": "37"})
    def test_respects_24h_budget(self):
        """calculate_schedule() reduces budget based on recent heating history."""
        tz = ZoneInfo("Europe/Helsinki")
        now_local = datetime.datetime.now(tz)
        # Simulate 2 distinct heated hours in the last 24h by anchoring to
        # exact hour boundaries so the count is timing-independent.
        hour_minus_3 = (now_local - datetime.timedelta(hours=3)).replace(
            minute=0, second=0, microsecond=0
        )
        hour_minus_2 = (now_local - datetime.timedelta(hours=2)).replace(
            minute=0, second=0, microsecond=0
        )
        for base_hour in (hour_minus_3, hour_minus_2):
            for m in (0, 15, 30, 45):
                t = base_hour + datetime.timedelta(minutes=m)
                app_module.temperature_history.append(
                    {
                        "time": t.isoformat(),
                        "current_temp": 35.0,
                        "desired_temp": 37.0,
                        "outside_temp": 10.0,
                    }
                )
        # Create prices for next 6 hours
        prices = {}
        for i in range(6):
            dt = (now_local + datetime.timedelta(hours=i)).replace(
                minute=0, second=0, microsecond=0
            )
            prices[dt.isoformat()] = float(i)  # 0,1,2,3,4,5
        app_module.hourly_prices = prices
        app_module.calculate_schedule()
        # Budget is 3, used 2, so only 1 hour should be scheduled
        assert len(app_module.heating_schedule) == 1

    @patch.dict("os.environ", {"HEATING_HOURS": "3", "TEMP_HIGH": "37"})
    def test_skips_past_hours(self):
        """calculate_schedule() ignores prices for past hours."""
        tz = ZoneInfo("Europe/Helsinki")
        now_local = datetime.datetime.now(tz)
        past = (now_local - datetime.timedelta(hours=2)).replace(
            minute=0, second=0, microsecond=0
        )
        future = (now_local + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        app_module.hourly_prices = {
            past.isoformat(): 0.001,  # super cheap but in the past
            future.isoformat(): 0.05,
        }
        app_module.calculate_schedule()
        assert past.isoformat() not in app_module.heating_schedule
        assert future.isoformat() in app_module.heating_schedule
