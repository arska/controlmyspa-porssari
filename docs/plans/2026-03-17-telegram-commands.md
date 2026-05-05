# Telegram Bot Commands Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow controlling the spa via Telegram bot commands (/status, /override, /heat, /schedule) with webhook-based message receiving.

**Architecture:** Add a `POST /telegram/<token>` webhook route to Flask. On first HTTP request, auto-register the webhook URL with Telegram using `flask.request.url_root`. Parse incoming commands, check authorization against comma-separated `TELEGRAM_CHAT_ID`, and reply. Update `send_telegram()` to support multi-chat and per-chat replies.

**Tech Stack:** Python, Flask, requests, Telegram Bot API (sendMessage, setWebhook)

---

### Task 1: Update `send_telegram()` to support multiple chat IDs and per-chat replies

**Files:**
- Modify: `app.py:42-55`
- Test: `test_app.py`

**Step 1: Write the failing tests**

Add to `TestTelegram` class in `test_app.py`:

```python
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "111,222"})
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

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "111,222"})
    @patch("app.requests.post")
    def test_send_telegram_specific_chat_id(self, mock_post):
        """send_telegram sends to specific chat_id when provided."""
        mock_post.return_value = MagicMock(status_code=200)
        with app_module.APP.app_context():
            app_module.send_telegram("hello", chat_id="333")
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["json"]["chat_id"] == "333"
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_app.py::TestTelegram::test_send_telegram_multiple_chat_ids test_app.py::TestTelegram::test_send_telegram_specific_chat_id -v`
Expected: FAIL

**Step 3: Update implementation**

Replace `send_telegram()` in `app.py`:

```python
def send_telegram(message: str, *, chat_id: str | None = None) -> None:
    """Send a message via Telegram Bot API.

    If chat_id is provided, sends to that specific chat.
    Otherwise sends to all chat IDs in TELEGRAM_CHAT_ID (comma-separated).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids_env = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_ids_env:
        return
    targets = [chat_id] if chat_id else [c.strip() for c in chat_ids_env.split(",")]
    for target in targets:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target, "text": message},
                timeout=10,
            )
        except requests.exceptions.RequestException:
            APP.logger.exception("failed to send telegram message to %s", target)
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS (existing test sends to single chat_id "123" which still works)

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: support multiple chat IDs and per-chat replies in send_telegram()"
```

---

### Task 2: Add `get_allowed_chat_ids()` helper and authorization check

**Files:**
- Modify: `app.py`
- Test: `test_app.py`

**Step 1: Write the failing tests**

Add to `TestTelegram` class:

```python
    @patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111, 222 ,333"})
    def test_get_allowed_chat_ids(self):
        """get_allowed_chat_ids parses comma-separated list with whitespace."""
        result = app_module.get_allowed_chat_ids()
        assert result == {"111", "222", "333"}

    def test_get_allowed_chat_ids_empty(self):
        """get_allowed_chat_ids returns empty set when env var missing."""
        result = app_module.get_allowed_chat_ids()
        assert result == set()
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_app.py::TestTelegram::test_get_allowed_chat_ids -v`
Expected: FAIL

**Step 3: Implement**

Add to `app.py` after `send_telegram()`:

```python
def get_allowed_chat_ids() -> set[str]:
    """Return set of allowed Telegram chat IDs from env var."""
    chat_ids_env = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_ids_env:
        return set()
    return {c.strip() for c in chat_ids_env.split(",")}
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: add get_allowed_chat_ids() helper for authorization"
```

---

### Task 3: Add webhook route and command handlers

**Files:**
- Modify: `app.py`
- Test: `test_app.py`

**Step 1: Write the failing tests**

Add new test class `TestTelegramWebhook` in `test_app.py`:

```python
class TestTelegramWebhook:
    """Tests for Telegram webhook route."""

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
        "TEMP_HIGH": "37",
        "TEMP_LOW": "10",
    })
    @patch("app.send_telegram")
    def test_status_command(self, mock_tg, client):
        """Bot responds to /status with temperature info."""
        app_module.cache.set("pool", {"current_temp": 35.0, "desired_temp": 37})
        resp = client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/status"}
        })
        assert resp.status_code == 200
        mock_tg.assert_called_once()
        msg = mock_tg.call_args.kwargs.get("chat_id", mock_tg.call_args[0][0] if len(mock_tg.call_args[0]) > 0 else "")
        reply_text = mock_tg.call_args[0][0]
        assert "35" in reply_text

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
    })
    @patch("app.send_telegram")
    def test_unauthorized_chat_rejected(self, mock_tg, client):
        """Messages from unauthorized chat IDs are rejected."""
        resp = client.post("/telegram/tok", json={
            "message": {"chat": {"id": 999}, "text": "/status"}
        })
        assert resp.status_code == 200
        mock_tg.assert_not_called()

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
    })
    @patch("app.send_telegram")
    def test_wrong_token_rejected(self, mock_tg, client):
        """Webhook with wrong token returns 404."""
        resp = client.post("/telegram/wrong", json={
            "message": {"chat": {"id": 123}, "text": "/status"}
        })
        assert resp.status_code == 404

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
    })
    @patch("app.send_telegram")
    @patch("app.scheduler")
    def test_override_command_toggles(self, mock_scheduler, mock_tg, client):
        """Bot responds to /override by toggling override."""
        # Enable
        client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/override"}
        })
        assert app_module.manual_override_endtime > datetime.datetime.now(tz=datetime.UTC)
        # Disable
        client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/override"}
        })
        assert app_module.manual_override_endtime == datetime.datetime.fromtimestamp(0, tz=datetime.UTC)

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
        "TEMP_HIGH": "37",
    })
    @patch("app.controlmyspa.ControlMySpa")
    @patch("app.send_telegram")
    def test_heat_command(self, mock_tg, mock_spa_cls, client):
        """Bot responds to /heat by setting heat override."""
        mock_spa = MagicMock()
        mock_spa.current_temp = 35
        mock_spa_cls.return_value = mock_spa
        client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/heat"}
        })
        assert app_module.manual_override_endtime > datetime.datetime.now(tz=datetime.UTC)
        mock_tg.assert_called()

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
        "TEMP_HIGH": "37",
        "TEMP_LOW": "10",
    })
    @patch("app.send_telegram")
    def test_schedule_command(self, mock_tg, client, sample_porssari_config):
        """Bot responds to /schedule with porssari schedule."""
        app_module.porssari_config = sample_porssari_config
        client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/schedule"}
        })
        mock_tg.assert_called_once()
        assert "schedule" in mock_tg.call_args[0][0].lower() or any(
            c.isdigit() for c in mock_tg.call_args[0][0]
        )

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
    })
    @patch("app.send_telegram")
    def test_unknown_command_shows_help(self, mock_tg, client):
        """Unknown command returns help text."""
        client.post("/telegram/tok", json={
            "message": {"chat": {"id": 123}, "text": "/unknown"}
        })
        mock_tg.assert_called_once()
        reply = mock_tg.call_args[0][0]
        assert "/status" in reply
        assert "/override" in reply
        assert "/heat" in reply
        assert "/schedule" in reply
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_app.py::TestTelegramWebhook -v`
Expected: FAIL with 404 (route doesn't exist)

**Step 3: Implement webhook route**

Add to `app.py` after the `/api/temperatures` route:

```python
@APP.route("/telegram/<token>", methods=["POST"])
def telegram_webhook(token: str) -> flask.Response:
    """Handle incoming Telegram bot messages."""
    if token != os.getenv("TELEGRAM_BOT_TOKEN"):
        flask.abort(404)

    data = flask.request.get_json(silent=True) or {}
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if chat_id not in get_allowed_chat_ids():
        return flask.jsonify({"ok": True})

    if text == "/status":
        _handle_telegram_status(chat_id)
    elif text == "/override":
        _handle_telegram_override(chat_id)
    elif text == "/heat":
        _handle_telegram_heat(chat_id)
    elif text == "/schedule":
        _handle_telegram_schedule(chat_id)
    else:
        send_telegram(
            "Available commands:\n"
            "/status - Current temperature and status\n"
            "/override - Toggle manual override\n"
            "/heat - Start heating\n"
            "/schedule - Show porssari schedule",
            chat_id=chat_id,
        )

    return flask.jsonify({"ok": True})


def _handle_telegram_status(chat_id: str) -> None:
    """Handle /status command."""
    pool = cache.get("pool")
    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    override_active = manual_override_endtime > datetime.datetime.now(tz=datetime.UTC)

    if pool:
        lines = [
            f"🌡 Current: {pool['current_temp']}°C",
            f"🎯 Desired: {pool['desired_temp']}°C",
            f"⬆️ TEMP_HIGH: {temp_high}°C",
        ]
        if pool["current_temp"] < temp_high:
            remaining = temp_high - pool["current_temp"]
            minutes = int(remaining / HEATING_RATE_PER_HOUR * 60)
            lines.append(f"⏱ Est. heating time: {minutes}min")
        if override_active:
            tz = ZoneInfo("Europe/Helsinki")
            lines.append(
                f"⚠️ Manual override until"
                f" {manual_override_endtime.astimezone(tz).strftime('%H:%M')}"
            )
        send_telegram("\n".join(lines), chat_id=chat_id)
    else:
        send_telegram("❌ No pool data available", chat_id=chat_id)


def _handle_telegram_override(chat_id: str) -> None:
    """Handle /override command — toggle on/off."""
    global manual_override_endtime  # noqa: PLW0603
    if manual_override_endtime > datetime.datetime.now(tz=datetime.UTC):
        manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
        scheduler.add_job(
            control, "date", run_date=datetime.datetime.now(tz=datetime.UTC)
        )
        send_telegram("✅ Manual override disabled", chat_id=chat_id)
    else:
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        send_telegram(
            f"⏸ Manual override enabled for 12h"
            f" (until {manual_override_endtime.astimezone(ZoneInfo('Europe/Helsinki')).strftime('%H:%M')})",
            chat_id=chat_id,
        )


def _handle_telegram_heat(chat_id: str) -> None:
    """Handle /heat command — start heating."""
    global manual_override_endtime  # noqa: PLW0603
    override_temp = int(os.getenv("TEMP_HIGH", "0")) - 0.5
    manual_override_endtime = datetime.datetime.now(
        tz=datetime.UTC
    ) + datetime.timedelta(hours=12)
    try:
        for attempt in tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(
                requests.exceptions.RequestException
            ),
            wait=tenacity.wait_random_exponential(multiplier=1, max=60),
            stop=tenacity.stop_after_attempt(5),
            before_sleep=tenacity.before_sleep_log(APP.logger, logging.INFO),
        ):
            with attempt:
                api = controlmyspa.ControlMySpa(
                    os.getenv("CONTROLMYSPA_USER"),
                    os.getenv("CONTROLMYSPA_PASS"),
                )
                api.desired_temp = override_temp
                pool = {
                    "desired_temp": override_temp,
                    "current_temp": api.current_temp,
                }
                cache.set("pool", pool, timeout=15 * 60)
                temperature_history.append(
                    {
                        "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                        "current_temp": pool["current_temp"],
                        "desired_temp": pool["desired_temp"],
                    }
                )
                send_telegram(
                    f"🔥 Heating to {override_temp}°C (current: {pool['current_temp']}°C)",
                    chat_id=chat_id,
                )
    except tenacity.RetryError:
        send_telegram("❌ Failed to set heating", chat_id=chat_id)


def _handle_telegram_schedule(chat_id: str) -> None:
    """Handle /schedule command."""
    if not porssari_config.get("Channel1"):
        send_telegram("❌ No schedule available", chat_id=chat_id)
        return

    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    temp_low = int(os.getenv("TEMP_LOW", "0"))
    lines = ["📋 Porssari schedule:"]
    for hour in range(24):
        command = porssari_config["Channel1"].get(str(hour))
        if command is None:
            continue
        temp = temp_high if command == "1" else temp_low
        marker = "🔥" if command == "1" else "❄️"
        lines.append(f"{hour:02d}:00 {marker} {temp}°C")
    send_telegram("\n".join(lines), chat_id=chat_id)
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS

**Step 5: Run linters**

Run: `ruff check . && ruff format .`

**Step 6: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: add Telegram webhook with /status /override /heat /schedule commands"
```

---

### Task 4: Auto-register webhook on first request

**Files:**
- Modify: `app.py`
- Test: `test_app.py`

**Step 1: Write the failing test**

Add to `TestTelegramWebhook`:

```python
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("app.requests.post")
    def test_webhook_auto_registered(self, mock_post, client):
        """Webhook is auto-registered with Telegram on first request."""
        mock_post.return_value = MagicMock(status_code=200)
        app_module.TELEGRAM_WEBHOOK_REGISTERED = False
        client.get("/")  # any request triggers registration
        # Find the setWebhook call
        webhook_calls = [
            c for c in mock_post.call_args_list
            if "setWebhook" in str(c)
        ]
        assert len(webhook_calls) == 1
        assert "/telegram/tok" in str(webhook_calls[0])

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("app.requests.post")
    def test_webhook_registered_only_once(self, mock_post, client):
        """Webhook registration only happens once."""
        mock_post.return_value = MagicMock(status_code=200)
        app_module.TELEGRAM_WEBHOOK_REGISTERED = False
        client.get("/")
        client.get("/")
        webhook_calls = [
            c for c in mock_post.call_args_list
            if "setWebhook" in str(c)
        ]
        assert len(webhook_calls) == 1
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_app.py::TestTelegramWebhook::test_webhook_auto_registered -v`
Expected: FAIL

**Step 3: Implement**

Add global to `app.py` near other globals:

```python
TELEGRAM_WEBHOOK_REGISTERED = False
```

Add `before_request` handler in `app.py` (after the route definitions, before `if __name__`):

```python
@APP.before_request
def _register_telegram_webhook() -> None:
    """Register Telegram webhook URL on first request."""
    global TELEGRAM_WEBHOOK_REGISTERED  # noqa: PLW0603
    if TELEGRAM_WEBHOOK_REGISTERED:
        return
    TELEGRAM_WEBHOOK_REGISTERED = True
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    webhook_url = flask.request.url_root.rstrip("/") + f"/telegram/{token}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url},
            timeout=10,
        )
        APP.logger.info("registered telegram webhook: %s", webhook_url)
    except requests.exceptions.RequestException:
        APP.logger.exception("failed to register telegram webhook")
        TELEGRAM_WEBHOOK_REGISTERED = False  # retry on next request
```

**Step 4: Run tests**

Run: `pytest test_app.py -v`
Expected: ALL PASS

**Step 5: Run linters and update fixture**

Add to `_reset_state` fixture: `app_module.TELEGRAM_WEBHOOK_REGISTERED = False`

Run: `ruff check . && ruff format .`

**Step 6: Commit**

```bash
git add app.py test_app.py
git commit -m "feat: auto-register Telegram webhook on first HTTP request"
```

---

### Task 5: Update CLAUDE.md and run final verification

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

Add `/telegram/<token>` to the Routes section:
```
- `POST /telegram/<token>` — Telegram bot webhook (commands: /status, /override, /heat, /schedule)
```

**Step 2: Run full verification**

Run: `uvx nox -s ruff pylint tests`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Telegram webhook route"
```
