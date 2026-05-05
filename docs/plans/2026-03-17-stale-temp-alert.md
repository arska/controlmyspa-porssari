# Stale Temperature Alert Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Detect when the spa gateway returns stale temperature data and alert via Telegram.

**Architecture:** Add a `send_telegram()` helper and a `check_stale_temperature()` function called at the end of `set_temp()`. Uses one global `last_stale_alert_time` for 8h suppression. Sends a startup healthcheck message from `initialize()`. No new dependencies (uses existing `requests`).

**Tech Stack:** Python, Flask, requests, Telegram Bot API

---

### Task 1: Add `send_telegram()` helper

**Files:**
- Modify: `app.py` (add function after globals, before `initialize()`)
- Test: `test_app.py`

**Step 1: Write the failing test**

Add to `test_app.py`:

```python
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

    @patch("app.requests.post")
    def test_send_telegram_no_config_does_nothing(self, mock_post):
        """send_telegram does nothing if env vars are missing."""
        with app_module.APP.app_context():
            app_module.send_telegram("hello")
        mock_post.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `pytest test_app.py::TestTelegram -v`
Expected: FAIL with "has no attribute 'send_telegram'"

**Step 3: Write minimal implementation**

Add to `app.py` after the global variables, before `initialize()`:

```python
def send_telegram(message: str) -> None:
    """Send a message via Telegram Bot API."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        APP.logger.exception("failed to send telegram message")
```

**Step 4: Run test to verify it passes**

Run: `pytest test_app.py::TestTelegram -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: add send_telegram() helper for Bot API notifications"
```

---

### Task 2: Add `check_stale_temperature()` detection

**Files:**
- Modify: `app.py` (add global + function after `send_telegram()`)
- Test: `test_app.py`

**Step 1: Write the failing tests**

Add to `TestTelegram` class in `test_app.py`:

```python
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
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
        assert "stale" in mock_tg.call_args[0][0].lower() or "stuck" in mock_tg.call_args[0][0].lower()

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
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

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
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

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
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

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
    @patch("app.send_telegram")
    def test_recovery_message(self, mock_tg):
        """Recovery message sent when temp changes after stale alert."""
        app_module.last_stale_alert_time = datetime.datetime.now(
            tz=datetime.UTC
        ) - datetime.timedelta(hours=1)
        app_module.stale_alert_active = True
        for i in range(5):
            app_module.temperature_history.append(
                {"time": "t", "current_temp": 30.0 + i, "desired_temp": 37}
            )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_called_once()
        assert "recover" in mock_tg.call_args[0][0].lower() or "back" in mock_tg.call_args[0][0].lower()

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123", "TEMP_HIGH": "37"})
    @patch("app.send_telegram")
    def test_not_enough_readings_no_alert(self, mock_tg):
        """No alert with insufficient readings."""
        app_module.temperature_history.append(
            {"time": "t", "current_temp": 30.0, "desired_temp": 37}
        )
        with app_module.APP.app_context():
            app_module.check_stale_temperature()
        mock_tg.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_app.py::TestTelegram -v`
Expected: FAIL with "has no attribute 'check_stale_temperature'"

**Step 3: Write minimal implementation**

Add globals to `app.py` (near other globals):

```python
last_stale_alert_time = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
stale_alert_active = False
```

Add `check_stale_temperature()` to `app.py` after `send_telegram()`:

```python
def check_stale_temperature() -> None:
    """Check if temperature readings are stale and alert via Telegram."""
    global last_stale_alert_time, stale_alert_active  # noqa: PLW0603

    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    history = list(temperature_history)

    if len(history) < 3:
        return

    # Determine if we're in heating mode
    latest = history[-1]
    heating = (
        latest["desired_temp"] >= temp_high and latest["current_temp"] < temp_high
    )
    threshold = 3 if heating else 25
    window = history[-threshold:]

    if len(window) < threshold:
        return

    temps = [r["current_temp"] for r in window]
    is_stale = (max(temps) - min(temps)) < 0.5

    if is_stale:
        if stale_alert_active:
            return
        # Check 8h suppression
        if (
            datetime.datetime.now(tz=datetime.UTC) - last_stale_alert_time
        ).total_seconds() < 8 * 3600:
            return
        mode = "heating" if heating else "idle"
        duration = f"{threshold * 15}min"
        send_telegram(
            f"\u26a0\ufe0f Spa temperature stuck at {latest['current_temp']}\u00b0C"
            f" for {duration} ({mode} mode,"
            f" desired {latest['desired_temp']}\u00b0C)."
            f" Gateway may be offline."
        )
        last_stale_alert_time = datetime.datetime.now(tz=datetime.UTC)
        stale_alert_active = True
    elif stale_alert_active:
        send_telegram(
            f"\u2705 Spa temperature is changing again"
            f" (now {latest['current_temp']}\u00b0C)."
            f" Gateway appears to be back online."
        )
        stale_alert_active = False
```

**Step 4: Run tests to verify they pass**

Run: `pytest test_app.py::TestTelegram -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: add stale temperature detection with Telegram alerts"
```

---

### Task 3: Wire `check_stale_temperature()` into `set_temp()`

**Files:**
- Modify: `app.py:set_temp()` (add call at end of try block)
- Test: `test_app.py`

**Step 1: Write the failing test**

Add to `TestSetTemp` class:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest test_app.py::TestSetTemp::test_calls_check_stale_temperature -v`
Expected: FAIL (check_stale_temperature not called)

**Step 3: Add the call**

In `app.py`, in `set_temp()`, add `check_stale_temperature()` after the logging lines at the end of the `with attempt:` block (after the manual override logic and temp setting, before the except):

```python
                    APP.logger.info("not changing desired temp %s", temp)
                check_stale_temperature()
    except tenacity.RetryError as exception:
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: wire stale temperature check into set_temp() control loop"
```

---

### Task 4: Add startup healthcheck Telegram message

**Files:**
- Modify: `app.py:initialize()` (add send_telegram call)
- Test: `test_app.py`

**Step 1: Write the failing test**

Add new test class:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest test_app.py::TestInitialize -v`
Expected: FAIL (send_telegram not called)

**Step 3: Add healthcheck to initialize()**

In `app.py`, add to end of `initialize()`:

```python
    send_telegram("\U0001f6c1 controlmyspa-porssari started")
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: send Telegram healthcheck message on startup"
```

---

### Task 5: Update _reset_state fixture and add env vars to docs

**Files:**
- Modify: `test_app.py` (_reset_state fixture)
- Modify: `CLAUDE.md` (add new env vars)

**Step 1: Update fixture to reset new globals**

In `test_app.py`, update `_reset_state`:

```python
@pytest.fixture(autouse=True)
def _reset_state():
    """Reset global state between tests."""
    app_module.porssari_config = {}
    app_module.temperature_history.clear()
    app_module.manual_override_endtime = datetime.datetime.fromtimestamp(
        0, tz=datetime.UTC
    )
    app_module.last_stale_alert_time = datetime.datetime.fromtimestamp(
        0, tz=datetime.UTC
    )
    app_module.stale_alert_active = False
    app_module.cache.clear()
    yield
```

**Step 2: Add env vars to CLAUDE.md**

Add to the Environment Variables section:

```
TELEGRAM_BOT_TOKEN   # Telegram bot token for stale temp alerts
TELEGRAM_CHAT_ID     # Telegram chat ID for alert messages
```

**Step 3: Run all checks**

Run: `uvx nox -s ruff pylint tests`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add test_app.py CLAUDE.md
git commit -m "chore: reset new globals in test fixture, document Telegram env vars"
```
