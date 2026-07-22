"""Nordpool based temperature control for Balboa ControlMySpa Whirlpools.

Fetches Nordpool spot prices via spot-hinta.fi, selects the cheapest hours
to heat, and controls https://github.com/arska/controlmyspa[Balboa ControlMySpa]
based whirlpools accordingly.
"""

import collections
import datetime
import functools
import logging
import os
import pathlib
import sqlite3
import threading
from zoneinfo import ZoneInfo

import controlmyspa
import flask
import requests
import sentry_sdk
import tenacity
from apscheduler.schedulers.background import BackgroundScheduler
from controlmyspa import SpaOfflineError
from dotenv import load_dotenv
from flask_caching import Cache
from sentry_sdk.integrations.flask import FlaskIntegration
from werkzeug.middleware.proxy_fix import ProxyFix

APP = flask.Flask(__name__)
cache = Cache(APP, config={"CACHE_TYPE": "SimpleCache"})
scheduler = BackgroundScheduler()
# Free, keyless weather API. Defaults below point at 20900 Turku, Finland.
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"
DEFAULT_WEATHER_LAT = "60.45"
DEFAULT_WEATHER_LON = "22.27"
# A weather fetch may fail on the network (RequestException) or while parsing
# the JSON (KeyError for a missing field, ValueError for bad/no JSON). Kept as a
# named tuple because ruff 0.15.21's formatter mangles inline `except (...)`.
WEATHER_FETCH_ERRORS = (requests.exceptions.RequestException, KeyError, ValueError)
# Average heating rate in °C per hour, measured empirically
HEATING_RATE_PER_HOUR = 1.5
SPOT_HINTA_API = "https://api.spot-hinta.fi"
# Named tuples for except clauses — ruff 0.15.21 strips inline parentheses.
PRICE_FETCH_ERRORS = (requests.exceptions.RequestException, ValueError)
PRICE_UPDATE_ERRORS = (requests.exceptions.RequestException, KeyError, ValueError)
hourly_prices: dict[str, float] = {}
heating_schedule: set[str] = set()
# Generous in-memory buffer; SQLite is the source of truth for persistence
temperature_history: collections.deque[dict] = collections.deque(maxlen=999)

# Latest outside air temperature in °C (or None), refreshed hourly by
# update_weather(). Recorded alongside spa temps to later model
# temperature-dependent cooling. Mutable global, not a constant.
latest_outside_temp = None  # pylint: disable=invalid-name

db_conn: sqlite3.Connection | None = None  # pylint: disable=invalid-name
db_lock = threading.Lock()

# set to datetime.datetime.now(tz=datetime.UTC) to disable manual override on startup
manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)

last_stale_alert_time = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
STALE_ALERT_ACTIVE = False


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


def get_allowed_chat_ids() -> set[str]:
    """Return set of allowed Telegram chat IDs from env var."""
    chat_ids_env = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_ids_env:
        return set()
    return {c.strip() for c in chat_ids_env.split(",")}


def format_duration(total_minutes: int) -> str:
    """Format minutes as 'Xh Ymin' or just 'Ymin' if under an hour."""
    hours, mins = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h {mins}min"
    return f"{mins}min"


def check_stale_temperature() -> None:
    """Check if temperature readings are stale and alert via Telegram."""
    global last_stale_alert_time, STALE_ALERT_ACTIVE  # noqa: PLW0603

    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    history = list(temperature_history)

    if len(history) < 3:  # noqa: PLR2004
        return

    # Determine if we're in heating mode
    latest = history[-1]
    heating = latest["desired_temp"] >= temp_high > latest["current_temp"]
    stale_minutes = 180 if heating else 720  # 3h heating, 12h idle

    # Find readings within the stale window using actual timestamps
    now = datetime.datetime.now(tz=datetime.UTC)
    cutoff = now - datetime.timedelta(minutes=stale_minutes)
    window = [
        r for r in history if datetime.datetime.fromisoformat(r["time"]) >= cutoff
    ]

    if len(window) < 3:  # noqa: PLR2004
        return

    # Require that our history actually covers the full stale window.
    # After a restart we may only have a few minutes of data — don't
    # claim "stuck for 6h" when we've only been running for 45 minutes.
    oldest_in_history = datetime.datetime.fromisoformat(history[0]["time"])
    if oldest_in_history > cutoff:
        return

    temps = [r["current_temp"] for r in window]
    is_stale = (max(temps) - min(temps)) < 0.5  # noqa: PLR2004

    if is_stale:
        # Repeat the alert once per stale window (3h heating, 12h idle)
        if (now - last_stale_alert_time).total_seconds() < stale_minutes * 60:
            STALE_ALERT_ACTIVE = True
            return
        mode = "heating" if heating else "idle"
        duration = format_duration(stale_minutes)
        send_telegram(
            f"\u26a0\ufe0f Spa temperature stuck at {latest['current_temp']}\u00b0C"
            f" for {duration} ({mode} mode,"
            f" desired {latest['desired_temp']}\u00b0C)."
            f" Gateway may be offline."
        )
        last_stale_alert_time = datetime.datetime.now(tz=datetime.UTC)
        STALE_ALERT_ACTIVE = True
    elif STALE_ALERT_ACTIVE:
        send_telegram(
            f"\u2705 Spa temperature is changing again"
            f" (now {latest['current_temp']}\u00b0C)."
            f" Gateway appears to be back online."
        )
        STALE_ALERT_ACTIVE = False


def init_db() -> None:
    """Open the SQLite database and backfill the temperature deque.

    If the parent directory of SQLITE_PATH does not exist, SQLite is
    silently disabled and the app runs in-memory only.
    """
    global db_conn  # noqa: PLW0603
    db_path = pathlib.Path(os.getenv("SQLITE_PATH", "/data/temperatures.db"))
    if not db_path.parent.exists():
        APP.logger.warning(
            "SQLite disabled: directory %s does not exist", db_path.parent
        )
        return
    db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute(
        "CREATE TABLE IF NOT EXISTS temperature_readings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "time TEXT NOT NULL, "
        "current_temp REAL NOT NULL, "
        "desired_temp REAL NOT NULL, "
        "outside_temp REAL)"
    )
    db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_time ON temperature_readings(time)"
    )
    db_conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history ("
        "time TEXT PRIMARY KEY, "
        "price REAL NOT NULL)"
    )
    db_conn.commit()

    # Backfill the in-memory deque from the last 48h
    cutoff = (
        datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=48)
    ).isoformat()
    rows = db_conn.execute(
        "SELECT time, current_temp, desired_temp, outside_temp "
        "FROM temperature_readings WHERE time >= ? ORDER BY time",
        (cutoff,),
    ).fetchall()
    for time_str, current_temp, desired_temp, outside_temp in rows:
        temperature_history.append(
            {
                "time": time_str,
                "current_temp": current_temp,
                "desired_temp": desired_temp,
                "outside_temp": outside_temp,
            }
        )
    APP.logger.info("loaded %d temperature readings from SQLite", len(rows))


def initialize() -> None:
    """Initialize scheduled jobs and run the control loop."""
    init_db()
    scheduler.start()
    scheduler.add_job(
        control,
        "cron",
        minute="*/15",
        id="control",
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        update_prices,
        "interval",
        minutes=15,
        id="update_prices",
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.datetime.now(tz=datetime.UTC),
    )
    scheduler.add_job(
        update_weather,
        "interval",
        minutes=60,
        id="update_weather",
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.datetime.now(tz=datetime.UTC),
    )
    send_telegram("\U0001f6c1 controlmyspa-porssari started")

    # Register Telegram webhook if URL is configured
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if webhook_url and token:
        full_url = f"{webhook_url.rstrip('/')}/telegram/{token}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": full_url},
                timeout=10,
            )
            APP.logger.info("registered telegram webhook: %s", full_url)
        except requests.exceptions.RequestException:
            APP.logger.exception("failed to register telegram webhook")


def update_weather() -> None:
    """Fetch the current outside air temperature and cache it in memory.

    Uses the free, keyless Open-Meteo API for the configured location
    (WEATHER_LAT/WEATHER_LON, defaulting to 20900 Turku, Finland). On any
    failure the previous value is kept so a transient outage doesn't wipe
    the reading recorded with the next spa temperature sample.
    """
    global latest_outside_temp  # noqa: PLW0603
    with (
        sentry_sdk.start_transaction(op="task", name="Update Weather"),
        APP.app_context(),
    ):
        try:
            response = requests.get(
                OPEN_METEO_API,
                {
                    "latitude": os.getenv("WEATHER_LAT", DEFAULT_WEATHER_LAT),
                    "longitude": os.getenv("WEATHER_LON", DEFAULT_WEATHER_LON),
                    "current": "temperature_2m",
                },
                timeout=10,
            )
            response.raise_for_status()
            latest_outside_temp = response.json()["current"]["temperature_2m"]
            APP.logger.info("got outside temperature: %s°C", latest_outside_temp)
        except WEATHER_FETCH_ERRORS:
            APP.logger.exception("failed to fetch outside temperature")


def _fetch_price_entries() -> list[dict]:
    """Fetch raw 15-min price entries from spot-hinta.fi /Today and /DayForward."""
    all_entries: list[dict] = []
    for endpoint in ("/Today", "/DayForward"):
        try:
            resp = requests.get(f"{SPOT_HINTA_API}{endpoint}", timeout=10)
            if resp.status_code == 404 and endpoint == "/DayForward":  # noqa: PLR2004
                # Day-ahead prices aren't published yet (normal before ~13:00 CET)
                APP.logger.info("day-ahead prices not yet available")
                continue
            resp.raise_for_status()
            all_entries.extend(resp.json())
        except PRICE_FETCH_ERRORS:
            APP.logger.exception("failed to fetch %s", endpoint)
    return all_entries


def _aggregate_prices(all_entries: list[dict], interval: int) -> dict[str, float]:
    """Group raw entries by interval boundary and average the prices."""
    slots_per_interval = interval // 15
    groups: dict[str, list[float]] = collections.defaultdict(list)
    for entry in all_entries:
        dt = datetime.datetime.fromisoformat(entry["DateTime"])
        minute = (dt.minute // interval) * interval
        interval_start = dt.replace(minute=minute, second=0, microsecond=0)
        groups[interval_start.isoformat()].append(entry["PriceWithTax"])
    return {
        time_key: sum(prices) / len(prices)
        for time_key, prices in groups.items()
        if len(prices) == slots_per_interval
    }


def _persist_prices(new_prices: dict[str, float]) -> None:
    """Write aggregated prices to the price_history SQLite table."""
    if db_conn is None:
        return
    with db_lock:
        for time_key, price in new_prices.items():
            db_conn.execute(
                "INSERT OR REPLACE INTO price_history (time, price) VALUES (?, ?)",
                (time_key, price),
            )
        db_conn.commit()


def update_prices() -> None:
    """Fetch electricity prices from spot-hinta.fi and aggregate to hourly.

    Calls /Today and /DayForward endpoints which return 15-min interval prices.
    Averages to hourly (or PRICE_INTERVAL) and stores in hourly_prices dict.
    On failure, previous prices are kept.
    """
    global hourly_prices  # noqa: PLW0603
    with (
        sentry_sdk.start_transaction(op="task", name="Update Prices"),
        APP.app_context(),
    ):
        try:
            interval = int(os.getenv("PRICE_INTERVAL", "60"))
            all_entries = _fetch_price_entries()
            if not all_entries:
                APP.logger.warning("no price data received from spot-hinta.fi")
                return
            new_prices = _aggregate_prices(all_entries, interval)
            if new_prices:
                hourly_prices = new_prices
                APP.logger.info(
                    "got %d price intervals from spot-hinta.fi", len(new_prices)
                )
                _persist_prices(new_prices)
                calculate_schedule()
        except PRICE_UPDATE_ERRORS:
            APP.logger.exception("failed to update prices")


def calculate_schedule() -> None:
    """Pick the cheapest future hours to heat, respecting the rolling 24h budget.

    Examines temperature_history to count hours already heated in the
    trailing 24h window, then greedily picks the cheapest remaining
    eligible hours up to HEATING_HOURS total.
    """
    global heating_schedule  # noqa: PLW0603
    tz = ZoneInfo("Europe/Helsinki")
    now_local = datetime.datetime.now(tz)
    now_hour = now_local.replace(minute=0, second=0, microsecond=0)
    heating_hours_budget = int(os.getenv("HEATING_HOURS", "3"))
    temp_high = int(os.getenv("TEMP_HIGH", "0"))

    # Count distinct hours heated in the last 24h from temperature_history
    cutoff_24h = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=24)
    heated_hours: set[str] = set()
    for entry in temperature_history:
        entry_time = datetime.datetime.fromisoformat(entry["time"])
        if entry_time >= cutoff_24h and entry["desired_temp"] >= temp_high:
            entry_local = entry_time.astimezone(tz)
            hour_key = entry_local.replace(
                minute=0, second=0, microsecond=0
            ).isoformat()
            heated_hours.add(hour_key)

    already_heated = len(heated_hours)
    remaining_budget = max(0, heating_hours_budget - already_heated)

    # Filter to future hours only and sort by price
    future_prices = {
        k: v
        for k, v in hourly_prices.items()
        if datetime.datetime.fromisoformat(k) >= now_hour
    }
    sorted_hours = sorted(future_prices, key=future_prices.get)

    # Pick cheapest hours up to remaining budget
    heating_schedule = set(sorted_hours[:remaining_budget])
    APP.logger.info(
        "schedule: %d hours planned (budget %d, used %d): %s",
        len(heating_schedule),
        heating_hours_budget,
        already_heated,
        sorted(heating_schedule),
    )


def control(*, skip_override_detection: bool = False) -> None:
    """Set the pool temperature based on the price-optimized heating schedule."""
    with sentry_sdk.start_transaction(op="task", name="Update Controlmyspa"):
        tz = ZoneInfo("Europe/Helsinki")
        current_hour = datetime.datetime.now(tz).replace(
            minute=0, second=0, microsecond=0
        )
        if int(os.getenv("TEMP_OVERRIDE", "0")):
            set_temp(
                int(os.getenv("TEMP_OVERRIDE", "0")),
                skip_override_detection=skip_override_detection,
            )
        elif current_hour.isoformat() in heating_schedule:
            set_temp(
                int(os.getenv("TEMP_HIGH", "0")),
                skip_override_detection=skip_override_detection,
            )
        else:
            set_temp(
                int(os.getenv("TEMP_LOW", "0")),
                skip_override_detection=skip_override_detection,
            )


def set_temp(temp: float, *, skip_override_detection: bool = False) -> None:
    """Update the pool temperature.

    Also fetch the current pool temperatures and cache them for 15 minutes.
    """
    try:
        for attempt in tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(
                (
                    requests.exceptions.RequestException,
                    KeyError,
                    TypeError,
                    SpaOfflineError,
                )
            ),
            wait=tenacity.wait_random_exponential(multiplier=1, max=60),
            stop=tenacity.stop_after_delay(600),
            before_sleep=tenacity.before_sleep_log(APP.logger, logging.INFO),
        ):
            with attempt:
                api = controlmyspa.ControlMySpa(
                    os.getenv("CONTROLMYSPA_USER"), os.getenv("CONTROLMYSPA_PASS")
                )
                info = getattr(api, "_info", None)  # pylint: disable=protected-access
                sentry_sdk.set_context(
                    "controlmyspa_info",
                    {"info_keys": list(info.keys())}
                    if isinstance(info, dict)
                    else {"info": repr(info)},
                )
                pool = {
                    "desired_temp": api.desired_temp,
                    "current_temp": api.current_temp,
                }
                cache.set("pool", pool, timeout=15 * 60)
                temperature_history.append(
                    {
                        "time": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                        "current_temp": pool["current_temp"],
                        "desired_temp": pool["desired_temp"],
                        "outside_temp": latest_outside_temp,
                    }
                )
                if db_conn is not None:
                    with db_lock:
                        db_conn.execute(
                            "INSERT INTO temperature_readings "
                            "(time, current_temp, desired_temp, outside_temp) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                temperature_history[-1]["time"],
                                pool["current_temp"],
                                pool["desired_temp"],
                                latest_outside_temp,
                            ),
                        )
                        db_conn.commit()

                APP.logger.info(
                    "current temp: %s, desired temp: %s",
                    pool["current_temp"],
                    pool["desired_temp"],
                )
                if (
                    not skip_override_detection
                    and int(pool["desired_temp"]) != int(os.getenv("TEMP_HIGH", "0"))
                    and int(pool["desired_temp"]) != int(os.getenv("TEMP_LOW", "0"))
                ):
                    # somebody set a manual temperature through the pool controls
                    # let's disable automatic control for 12h
                    global manual_override_endtime  # noqa: PLW0603
                    if manual_override_endtime > datetime.datetime.now(tz=datetime.UTC):
                        # the end time is in the future -> let's wait
                        APP.logger.info(
                            "not changing the temperature until %s"
                            " due to manual override",
                            manual_override_endtime,
                        )
                        return

                    if manual_override_endtime == datetime.datetime.fromtimestamp(
                        0, tz=datetime.UTC
                    ):
                        # end time not set -> this is the first detection
                        # of the manual override -> set the timer
                        manual_override_endtime = datetime.datetime.now(
                            tz=datetime.UTC
                        ) + datetime.timedelta(hours=12)
                        APP.logger.info(
                            "manual override detected, not changing"
                            " the temperature until %s",
                            manual_override_endtime,
                        )
                        tz = ZoneInfo("Europe/Helsinki")
                        until = manual_override_endtime.astimezone(tz).strftime("%H:%M")
                        send_telegram(
                            f"\U0001f6c1 Manual override detected"
                            f" (spa set to {pool['desired_temp']}\u00b0C)."
                            f" Pausing automatic control until {until}."
                        )
                        return

                    # the manual override time expired
                    # reset the timer for the next override
                    manual_override_endtime = datetime.datetime.fromtimestamp(
                        0, tz=datetime.UTC
                    )
                    send_telegram(
                        "\u2705 Manual override expired."
                        " Resuming automatic temperature control."
                    )
                    # take control over the temperature below

                if pool["desired_temp"] != float(temp):
                    api.desired_temp = float(temp)
                    APP.logger.info("set desired temp %s", temp)
                else:
                    APP.logger.info("not changing desired temp %s", temp)
                check_stale_temperature()
    except tenacity.RetryError as exception:
        APP.logger.info(
            "ignoring controlmyspa API error, retrying next control loop: %s",
            exception,
        )


def require_auth(f):  # noqa: ANN001, ANN201
    """Require Authorization: Bearer <ADMIN_PASSWORD> on protected endpoints.

    If ADMIN_PASSWORD is not set, all requests pass through (no auth).
    """

    @functools.wraps(f)
    def decorated(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        password = os.getenv("ADMIN_PASSWORD")
        if not password:
            return f(*args, **kwargs)
        auth = flask.request.headers.get("Authorization", "")
        if auth != f"Bearer {password}":
            return flask.jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


@APP.route("/")
def status() -> str:
    """WebGUI to show current heating schedule and (cached) pool temperatures."""
    pool = cache.get("pool")
    # Estimate time to reach TEMP_HIGH based on average heating rate
    heat_estimate_minutes = None
    heat_estimate_time = None
    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    if pool and pool["current_temp"] < temp_high:
        degrees_remaining = temp_high - pool["current_temp"]
        heat_estimate_minutes = int(degrees_remaining / HEATING_RATE_PER_HOUR * 60)
        heat_estimate_time = datetime.datetime.now(
            ZoneInfo("Europe/Helsinki")
        ) + datetime.timedelta(minutes=heat_estimate_minutes)

    # Filter prices to future hours only for the template
    tz = ZoneInfo("Europe/Helsinki")
    now_local = datetime.datetime.now(tz)
    future_prices = [
        (k, v)
        for k, v in sorted(hourly_prices.items())
        if datetime.datetime.fromisoformat(k)
        >= now_local.replace(minute=0, second=0, microsecond=0)
    ]

    return flask.render_template(
        "index.html",
        future_prices=future_prices,
        heating_schedule=heating_schedule,
        api=pool,
        manual_override_endtime=manual_override_endtime.astimezone(
            ZoneInfo("Europe/Helsinki")
        ),
        now=datetime.datetime.now(tz=datetime.UTC),
        temp_heat=int(os.getenv("TEMP_HIGH", "0")) - 0.5,
        heat_estimate_minutes=heat_estimate_minutes,
        heat_estimate_time=heat_estimate_time,
        temp_high=temp_high,
        temp_low=int(os.getenv("TEMP_LOW", "0")),
        outside_temp=latest_outside_temp,
        auth_required=bool(os.getenv("ADMIN_PASSWORD")),
    )


@APP.route("/api/override", methods=["POST"])
@require_auth
def api_override() -> flask.Response:
    """Toggle manual override on/off via the web GUI."""
    global manual_override_endtime  # noqa: PLW0603
    body = flask.request.get_json(silent=True) or {}
    action = body.get("action")
    if action == "enable":
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        APP.logger.info(
            "manual override enabled via web GUI until %s",
            manual_override_endtime,
        )
        tz = ZoneInfo("Europe/Helsinki")
        until = manual_override_endtime.astimezone(tz).strftime("%H:%M")
        send_telegram(f"\u23f8 Manual override enabled via web for 12h (until {until})")
    elif action == "disable":
        manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
        APP.logger.info("manual override disabled via web GUI")
        send_telegram(
            "\u2705 Manual override disabled via web."
            " Resuming automatic temperature control."
        )
        # skip override detection since API may still return stale desired_temp
        control(skip_override_detection=True)
    elif action == "heat":
        override_temp = int(os.getenv("TEMP_HIGH", "0")) - 0.5
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        set_temp(override_temp, skip_override_detection=True)
        send_telegram(
            f"\U0001f525 Heat override enabled via web"
            f" (target {override_temp}\u00b0C for 12h)"
        )
    elif action == "cold":
        override_temp = int(os.getenv("TEMP_LOW", "0")) + 0.5
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=24)
        set_temp(override_temp, skip_override_detection=True)
        send_telegram(
            f"\u2744\ufe0f Cold override enabled via web"
            f" (target {override_temp}\u00b0C for 24h)"
        )
    return flask.jsonify(
        {
            "override_active": manual_override_endtime
            > datetime.datetime.now(tz=datetime.UTC),
            "override_endtime": manual_override_endtime.isoformat(),
        }
    )


@APP.route("/api/temperatures")
def api_temperatures() -> flask.Response:
    """Return temperature history and future price schedule as JSON."""
    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    temp_low = int(os.getenv("TEMP_LOW", "0"))

    # Future schedule from prices
    future = []
    tz = ZoneInfo("Europe/Helsinki")
    for time_key, price in sorted(hourly_prices.items()):
        dt = datetime.datetime.fromisoformat(time_key)
        if dt < datetime.datetime.now(tz):
            continue
        future.append(
            {
                "time": dt.astimezone(datetime.UTC).isoformat(),
                "price": price,
                "heating": time_key in heating_schedule,
            }
        )

    return flask.jsonify(
        {
            "history": list(temperature_history),
            "future": future,
            "temp_high": temp_high,
            "temp_low": temp_low,
            "outside_temp": latest_outside_temp,
        }
    )


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
    elif text in ("/heat", "/hot"):
        _handle_telegram_heat(chat_id)
    elif text == "/cold":
        _handle_telegram_cold(chat_id)
    elif text == "/schedule":
        _handle_telegram_schedule(chat_id)
    else:
        send_telegram(
            "Available commands:\n"
            "/status - Current temperature and status\n"
            "/override - Toggle manual override\n"
            "/hot - Heat to TEMP_HIGH-0.5 for 12h\n"
            "/cold - Cool to TEMP_LOW for 24h\n"
            "/schedule - Show heating schedule",
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
            f"\U0001f321 Current: {pool['current_temp']}\u00b0C",
            f"\U0001f3af Desired: {pool['desired_temp']}\u00b0C",
            f"\u2b06\ufe0f TEMP_HIGH: {temp_high}\u00b0C",
        ]
        if pool["current_temp"] < temp_high:
            remaining = temp_high - pool["current_temp"]
            minutes = int(remaining / HEATING_RATE_PER_HOUR * 60)
            lines.append(f"\u23f1 Est. heating time: {format_duration(minutes)}")
        if override_active:
            tz = ZoneInfo("Europe/Helsinki")
            lines.append(
                f"\u26a0\ufe0f Manual override until"
                f" {manual_override_endtime.astimezone(tz).strftime('%H:%M')}"
            )
        send_telegram("\n".join(lines), chat_id=chat_id)
    else:
        send_telegram("\u274c No pool data available", chat_id=chat_id)


def _handle_telegram_override(chat_id: str) -> None:  # noqa: ARG001  # pylint: disable=unused-argument
    """Handle /override command -- toggle on/off."""
    global manual_override_endtime  # noqa: PLW0603
    if manual_override_endtime > datetime.datetime.now(tz=datetime.UTC):
        manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
        # skip override detection since API may still return stale desired_temp
        control(skip_override_detection=True)
        send_telegram(
            "\u2705 Manual override disabled via Telegram."
            " Resuming automatic temperature control."
        )
    else:
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        tz = ZoneInfo("Europe/Helsinki")
        until = manual_override_endtime.astimezone(tz).strftime("%H:%M")
        send_telegram(
            f"\u23f8 Manual override enabled via Telegram for 12h (until {until})"
        )


def _handle_telegram_heat(chat_id: str) -> None:  # noqa: ARG001  # pylint: disable=unused-argument
    """Handle /heat and /hot commands -- start heating."""
    global manual_override_endtime  # noqa: PLW0603
    override_temp = int(os.getenv("TEMP_HIGH", "0")) - 0.5
    manual_override_endtime = datetime.datetime.now(
        tz=datetime.UTC
    ) + datetime.timedelta(hours=12)
    set_temp(override_temp, skip_override_detection=True)
    pool = cache.get("pool")
    current = pool["current_temp"] if pool else "?"
    send_telegram(
        f"\U0001f525 Heat override enabled via Telegram"
        f" (target {override_temp}\u00b0C, current: {current}\u00b0C)"
    )


def _handle_telegram_cold(chat_id: str) -> None:  # noqa: ARG001  # pylint: disable=unused-argument
    """Handle /cold command -- set TEMP_LOW for 24h."""
    global manual_override_endtime  # noqa: PLW0603
    override_temp = int(os.getenv("TEMP_LOW", "0")) + 0.5
    manual_override_endtime = datetime.datetime.now(
        tz=datetime.UTC
    ) + datetime.timedelta(hours=24)
    set_temp(override_temp, skip_override_detection=True)
    pool = cache.get("pool")
    current = pool["current_temp"] if pool else "?"
    send_telegram(
        f"\u2744\ufe0f Cold override enabled via Telegram"
        f" (target {override_temp}\u00b0C for 24h, current: {current}\u00b0C)"
    )


def _handle_telegram_schedule(chat_id: str) -> None:
    """Handle /schedule command — show prices with heating hours marked."""
    if not hourly_prices:
        send_telegram("\u274c No price data available", chat_id=chat_id)
        return

    tz = ZoneInfo("Europe/Helsinki")
    now_local = datetime.datetime.now(tz)
    lines = ["\U0001f4cb Electricity prices (c/kWh):"]
    for time_key in sorted(hourly_prices):
        dt = datetime.datetime.fromisoformat(time_key)
        if dt < now_local:
            continue
        price_cents = hourly_prices[time_key] * 100
        heating = time_key in heating_schedule
        marker = "\U0001f525" if heating else "  "
        lines.append(f"{dt.strftime('%H:%M')} {marker} {price_cents:.1f}c")
    send_telegram("\n".join(lines), chat_id=chat_id)


if __name__ == "__main__":
    load_dotenv()
    sentry_sdk.init(
        os.environ.get("SENTRY_URL"),
        integrations=[FlaskIntegration()],
        enable_tracing=True,
        traces_sample_rate=0.8,
    )
    initialize()
    APP.wsgi_app = ProxyFix(APP.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    APP.logger.setLevel("DEBUG")
    APP.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
