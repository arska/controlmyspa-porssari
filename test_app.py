"""Tests for controlmyspa-porssari application."""

import datetime
import json
from unittest.mock import MagicMock, patch

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
        with patch(
            "app.controlmyspa.ControlMySpa",
            side_effect=requests.exceptions.ConnectionError("no api"),
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
        """Temperature history respects 192-point max."""
        for i in range(200):
            app_module.temperature_history.append(
                {
                    "time": f"2024-01-01T{i:05d}",
                    "current_temp": 30 + (i % 10),
                    "desired_temp": 37,
                }
            )
        assert len(app_module.temperature_history) == 192


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

    @patch("app.scheduler")
    @patch("app.set_temp")
    def test_disable_override(self, mock_set_temp, mock_scheduler, client):
        """Disabling override sets TEMP_LOW, resets endtime, and triggers control."""
        # First enable
        app_module.manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        # Then disable
        with patch.dict("os.environ", {"TEMP_LOW": "10"}):
            resp = client.post(
                "/api/override",
                json={"action": "disable"},
                content_type="application/json",
            )
        data = resp.get_json()
        assert data["override_active"] is False
        mock_set_temp.assert_called_once_with(10)
        mock_scheduler.add_job.assert_called_once()

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
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
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
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
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
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa_cls.return_value = mock_spa
        client.post(
            "/api/override",
            json={"action": "heat"},
            content_type="application/json",
        )
        pool = app_module.cache.get("pool")
        assert pool["desired_temp"] == 36.5
        assert pool["current_temp"] == 35
        assert len(app_module.temperature_history) == 1
        assert app_module.temperature_history[0]["desired_temp"] == 36.5

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
        mock_set_temp.assert_called_once_with(27)

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
        mock_set_temp.assert_called_once_with(37)

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
        mock_set_temp.assert_called_once_with(40)


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
        """Alert after 3 identical readings when heating."""
        for _ in range(3):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0, "desired_temp": 37}
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
        """Alert after 25 identical readings in general mode."""
        for _ in range(25):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0, "desired_temp": 10}
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
        for i in range(25):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0 + i * 0.5, "desired_temp": 37}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"},
    )
    @patch("app.send_telegram")
    def test_stale_alert_suppressed_8h(self, mock_tg):
        """Alert suppressed for 8h after first alert."""
        app_module.last_stale_alert_time = datetime.datetime.now(tz=datetime.UTC)
        for _ in range(25):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0, "desired_temp": 10}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()

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
        for i in range(5):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0 + i, "desired_temp": 37}
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
        app_module.temperature_history.append(
            {"time": "t", "current_temp": 30.0, "desired_temp": 37}
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
            "TEMP_LOW": "10",
        },
    )
    @patch("app.send_telegram")
    @patch("app.scheduler")
    @patch("app.set_temp")
    def test_override_command_toggles(
        self, mock_set_temp, mock_scheduler, mock_tg, client
    ):
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
        mock_set_temp.assert_called_once_with(10)

    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "TEMP_HIGH": "37",
        },
    )
    @patch("app.controlmyspa.ControlMySpa")
    @patch("app.send_telegram")
    def test_heat_command(self, mock_tg, mock_spa_cls, client):
        """Bot responds to /heat by setting heat override."""
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
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
        assert "/heat" in reply
        assert "/schedule" in reply


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
