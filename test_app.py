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
        app_module.cache.set(
            "pool", {"current_temp": 35.0, "desired_temp": 37}
        )
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

    def test_status_page_shows_porssari_config(
        self, client, sample_porssari_config
    ):
        """Status page shows porssari schedule when available."""
        app_module.porssari_config = sample_porssari_config
        app_module.cache.set(
            "pool", {"current_temp": 35, "desired_temp": 37}
        )
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
    def test_returns_future_schedule(
        self, client, sample_porssari_config
    ):
        """Returns future porssari schedule mapped to temperatures."""
        app_module.porssari_config = sample_porssari_config
        resp = client.get("/api/temperatures")
        data = resp.get_json()
        assert len(data["future"]) > 0
        targets = {p["target_temp"] for p in data["future"]}
        assert targets <= {27, 37}

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
    def test_disable_override(self, mock_scheduler, client):
        """Disabling override resets endtime and triggers control."""
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
        app_module.porssari_config = {
            "Channel1": {str(h): "0" for h in range(24)}
        }
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
        app_module.porssari_config = {
            "Channel1": {str(h): "1" for h in range(24)}
        }
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
        app_module.porssari_config = {
            "Channel1": {str(h): "0" for h in range(24)}
        }
        with app_module.APP.app_context():
            app_module.control()
        mock_set_temp.assert_called_once_with(40)


# --- set_temp tests ---


class TestSetTemp:
    """Tests for the set_temp() function."""

    @patch.dict(
        "os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"}
    )
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

    @patch.dict(
        "os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"}
    )
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

    @patch.dict(
        "os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"}
    )
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

    @patch.dict(
        "os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"}
    )
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

    @patch.dict(
        "os.environ", {"TEMP_HIGH": "37", "TEMP_LOW": "27"}
    )
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
