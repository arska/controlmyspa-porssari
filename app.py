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
from typing import TYPE_CHECKING
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

import metrics
import pricing
import scheduling
import storage
import thermal

if TYPE_CHECKING:
    from collections.abc import Callable

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
# Named tuple for the except clause — ruff 0.15.21 strips inline parentheses.
PRICE_UPDATE_ERRORS = (requests.exceptions.RequestException, KeyError, ValueError)
hourly_prices: dict[str, float] = {}
# How far back prices are kept in memory (for the chart): a week, so the price
# bars cover the whole temperature history the deque can hold. SQLite keeps
# them forever so past selections can be evaluated retroactively.
PRICE_MEMORY_HOURS = 168
heating_schedule: set[str] = set()
DEFAULT_HEATING_RATE = 1.6  # °C/h, measured from production history
MIN_HEATING_RATE = 0.8  # a slower estimate than this is sensor noise
MAX_HEATING_RATE = 3.0  # the heater cannot do better than this
MIN_HEATING_SAMPLES = 3
heating_rate: float = DEFAULT_HEATING_RATE  # pylint: disable=invalid-name
DEFAULT_COOLING_K = 0.006
MIN_COOLING_K = 0.002  # Pool can't cool slower than this (physical limit)
MAX_COOLING_K = 0.020  # Pool can't cool faster than this (lid off in winter)
cooling_k: float = DEFAULT_COOLING_K  # pylint: disable=invalid-name
# Holds a week at the current 8 readings/hour, matching PRICE_MEMORY_HOURS so
# the chart's temperatures and price bars span the same time. SQLite is the
# source of truth; this is refilled from it on startup.
temperature_history: collections.deque[dict] = collections.deque(maxlen=1400)

# Latest outside air temperature in °C (or None), refreshed hourly by
# update_weather(). Recorded alongside spa temps to later model
# temperature-dependent cooling. Mutable global, not a constant.
latest_outside_temp = None  # pylint: disable=invalid-name
# Hourly outside temperature forecast: ISO hour key → °C.
# Populated by update_weather() from Open-Meteo's hourly forecast.
weather_forecast: dict[str, float] = {}

store = storage.Store()  # disabled until init_db() opens SQLITE_PATH

# set to datetime.datetime.now(tz=datetime.UTC) to disable manual override on startup
manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)

last_stale_alert_time = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
STALE_ALERT_ACTIVE = False
# A heating block is 1-2h, so a longer window would never sit inside one.
STALE_HEATING_MINUTES = 90
STALE_IDLE_MINUTES = 720
STUCK_FRACTION = 0.25  # stuck if the pool moved less than this much of expected
MIN_STALE_READINGS = 3
SCHEDULE_START_DELAY = 60  # seconds; long enough for the first reading to land
FORECAST_HOURS = 48  # how far the chart's predicted temperature runs
DEFAULT_OUTSIDE_TEMP = 15.0  # used only until the first weather fetch lands
DEFAULT_POOL_TEMP = 34.0  # used only until the first reading lands


def _heating_rate() -> float:
    """Return heating rate in °C/h: the HEATING_RATE override, else measured.

    Overestimating the rate books too few hours, so the pool falls short and
    tops off in whatever hour is cheapest next — usually a dearer one than the
    hours that were skipped. estimate_heating_rate() keeps the value honest as
    the cover ages and the seasons change.
    """
    override = os.getenv("HEATING_RATE")
    return float(override) if override else heating_rate


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
    """Alert via Telegram when the spa stops responding to what we command.

    The pool is "stuck" when it moves far less than the thermal model says it
    should — not when it fails to move a fixed 0.5°C, which a slowly cooling
    pool never manages anyway. Only the current heating mode counts: the first
    minutes of a heating block say nothing about whether the gateway is alive.
    """
    global last_stale_alert_time, STALE_ALERT_ACTIVE  # noqa: PLW0603

    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    history = list(temperature_history)
    if len(history) < MIN_STALE_READINGS:
        return

    stretch, heating = thermal.mode_stretch(history, temp_high)
    stale_minutes = STALE_HEATING_MINUTES if heating else STALE_IDLE_MINUTES
    now = datetime.datetime.now(tz=datetime.UTC)
    cutoff = now - datetime.timedelta(minutes=stale_minutes)

    window = [
        r for r in stretch if datetime.datetime.fromisoformat(r["time"]) >= cutoff
    ]
    if len(window) < MIN_STALE_READINGS:
        return
    # The stretch must cover the whole window. After a restart, or 20 minutes
    # into a heating block, there is nothing to conclude yet.
    if datetime.datetime.fromisoformat(stretch[0]["time"]) > cutoff:
        return

    temps = [r["current_temp"] for r in window]
    observed = max(temps) - min(temps)
    outside = (
        latest_outside_temp if latest_outside_temp is not None else DEFAULT_OUTSIDE_TEMP
    )
    expected = (
        thermal.expected_gain(window, temp_high, _heating_rate())
        if heating
        else thermal.expected_drop(window, cooling_k, outside)
    )
    latest = history[-1]

    if expected > 0 and observed < expected * STUCK_FRACTION:
        # Repeat the alert once per stale window
        if (now - last_stale_alert_time).total_seconds() < stale_minutes * 60:
            STALE_ALERT_ACTIVE = True
            return
        stuck_minutes = int(
            (now - datetime.datetime.fromisoformat(window[0]["time"])).total_seconds()
            / 60
        )
        mode = "heating" if heating else "idle"
        send_telegram(
            f"\u26a0\ufe0f Spa temperature stuck at {latest['current_temp']}\u00b0C"
            f" for {format_duration(stuck_minutes)} ({mode} mode,"
            f" desired {latest['desired_temp']}\u00b0C):"
            f" moved {observed:.1f}\u00b0C, expected {expected:.1f}\u00b0C."
            f" Gateway may be offline."
        )
        last_stale_alert_time = now
        STALE_ALERT_ACTIVE = True
    elif STALE_ALERT_ACTIVE:
        send_telegram(
            f"\u2705 Spa temperature is changing again"
            f" (now {latest['current_temp']}\u00b0C)."
            f" Gateway appears to be back online."
        )
        STALE_ALERT_ACTIVE = False


def init_db() -> None:
    """Open the database and refill the temperature deque from it.

    If the parent directory of SQLITE_PATH does not exist, persistence is
    silently disabled and the app runs in memory only.
    """
    global store  # noqa: PLW0603
    store = storage.Store(os.getenv("SQLITE_PATH", "/data/temperatures.db"))
    if not store.enabled:
        return

    # Fill the deque to capacity with the newest readings. A shorter window
    # starves the estimators: 48h of production data holds 2 cooling periods
    # and 1 heating stretch, short of the 5 and 3 they need, so every restart
    # would fall back to the default constants for days.
    readings = store.newest_readings(temperature_history.maxlen)
    temperature_history.extend(readings)
    APP.logger.info("loaded %d temperature readings from SQLite", len(readings))

    # Backfill recent prices so the chart keeps its history across restarts
    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
        hours=PRICE_MEMORY_HOURS
    )
    hourly_prices.update(store.prices_since(cutoff))
    APP.logger.info("loaded %d price intervals from SQLite", len(hourly_prices))


def _add_interval_job(func: Callable, minutes: int, *, delay_seconds: int = 0) -> None:
    """Register a background job repeating every `minutes`, named after `func`."""
    scheduler.add_job(
        func,
        "interval",
        minutes=minutes,
        id=func.__name__,
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.datetime.now(tz=datetime.UTC)
        + datetime.timedelta(seconds=delay_seconds),
    )


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
    _add_interval_job(update_prices, 15)
    _add_interval_job(update_weather, 60)
    # Planning runs on its own timer so a price outage cannot freeze the
    # thermal estimates or stop re-planning against the prices in memory.
    # The first run waits for control() to record a temperature.
    _add_interval_job(calculate_schedule, 15, delay_seconds=SCHEDULE_START_DELAY)
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
    """Fetch current outside temperature and hourly forecast.

    Uses the free, keyless Open-Meteo API for the configured location
    (WEATHER_LAT/WEATHER_LON, defaulting to 20900 Turku, Finland). On any
    failure the previous values are kept so a transient outage doesn't wipe
    the readings.
    """
    global latest_outside_temp, weather_forecast  # noqa: PLW0603
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
                    "hourly": "temperature_2m",
                    "forecast_hours": 48,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            latest_outside_temp = data["current"]["temperature_2m"]
            # Build hourly forecast dict
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            weather_forecast = dict(zip(times, temps, strict=False))
            APP.logger.info(
                "got outside temperature: %s°C, %d forecast hours",
                latest_outside_temp,
                len(weather_forecast),
            )
        except WEATHER_FETCH_ERRORS:
            APP.logger.exception("failed to fetch outside temperature")


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
            all_entries = pricing.fetch_entries()
            if not all_entries:
                APP.logger.warning("no price data received from spot-hinta.fi")
                return
            new_prices = pricing.aggregate(all_entries, interval)
            if new_prices:
                hourly_prices = pricing.merge(
                    hourly_prices, new_prices, PRICE_MEMORY_HOURS
                )
                APP.logger.info(
                    "got %d price intervals from spot-hinta.fi (%d kept in memory)",
                    len(new_prices),
                    len(hourly_prices),
                )
                store.save_prices(new_prices)
                # Get a temperature reading in straight away, so the first
                # planning run has something to work with.
                scheduler.add_job(
                    control,
                    "date",
                    run_date=datetime.datetime.now(tz=datetime.UTC),
                )
        except PRICE_UPDATE_ERRORS:
            APP.logger.exception("failed to update prices")


def _outside_temp_at(dt: datetime.datetime) -> float:
    """Look up forecast outside temperature for the given hour.

    Falls back to latest_outside_temp, then 15.0°C default.
    Open-Meteo returns keys like '2026-07-22T21:00' (no timezone),
    so we format the lookup key accordingly.
    """
    # Open-Meteo forecast keys are naive UTC strings like "2026-07-22T21:00"
    utc_dt = dt.astimezone(datetime.UTC)
    key = utc_dt.strftime("%Y-%m-%dT%H:00")
    if key in weather_forecast:
        return weather_forecast[key]
    if latest_outside_temp is not None:
        return latest_outside_temp
    return DEFAULT_OUTSIDE_TEMP


def _outside_temp_in(hours: int) -> float:
    """Outside temperature forecast `hours` from now, for the thermal model."""
    now = datetime.datetime.now(ZoneInfo("Europe/Helsinki"))
    return _outside_temp_at(now + datetime.timedelta(hours=hours))


def estimate_cooling_rate() -> float:
    """Estimate the cooling constant k from recent temperature history.

    Finds continuous cooling periods (stretches where desired_temp < TEMP_HIGH)
    and measures the overall temperature drop across each period. This avoids
    noise from the spa's 0.5°C sensor resolution — a single 0.5°C step over
    12 minutes looks like 2°C/h but is really just rounding. By measuring
    across multi-hour cooling stretches we get the true rate.
    """
    global cooling_k
    measured = thermal.cooling_periods(
        list(temperature_history), int(os.getenv("TEMP_HIGH", "0"))
    )
    cooling_k, clamped = thermal.median_within(
        measured,
        thermal.MIN_COOLING_PERIODS,
        MIN_COOLING_K,
        MAX_COOLING_K,
        DEFAULT_COOLING_K,
    )
    if clamped:
        APP.logger.warning("cooling k clamped to %.5f", cooling_k)
    APP.logger.info("cooling rate k=%.5f (%d periods)", cooling_k, len(measured))
    return cooling_k


def estimate_heating_rate() -> float:
    """Estimate how fast the spa heats, from stretches where it was heating.

    Same shape as estimate_cooling_rate(): median of the measurements, clamped
    to a physically plausible band, falling back to the default until enough
    stretches have been seen.
    """
    global heating_rate
    measured = thermal.heating_stretches(
        list(temperature_history), int(os.getenv("TEMP_HIGH", "0"))
    )
    heating_rate, clamped = thermal.median_within(
        measured,
        MIN_HEATING_SAMPLES,
        MIN_HEATING_RATE,
        MAX_HEATING_RATE,
        DEFAULT_HEATING_RATE,
    )
    if clamped:
        APP.logger.warning("heating rate clamped to %.2f°C/h", heating_rate)
    APP.logger.info("heating rate %.2f°C/h (%d stretches)", heating_rate, len(measured))
    return heating_rate


def predict_time_to_temp(target_temp: float, current_temp: float) -> float:
    """Predict hours until pool drops from current_temp to target_temp.

    Steps hour by hour using the weather forecast for each hour's outside
    temperature. Falls back to latest_outside_temp if no forecast.
    Returns 0.0 if pool is already at or below target.
    """
    return thermal.hours_until(target_temp, current_temp, cooling_k, _outside_temp_in)


def calculate_schedule() -> None:
    """Decide which hours to heat and record the decision.

    Owns the state; scheduling.plan() owns the policy.
    """
    global heating_schedule  # noqa: PLW0603
    estimate_cooling_rate()
    estimate_heating_rate()
    policy = scheduling.Policy(
        temp_high=int(os.getenv("TEMP_HIGH", "0")),
        temp_min=float(os.getenv("TEMP_MIN", "34")),
        max_hours=int(os.getenv("HEATING_HOURS", "6")),
        deadband=float(os.getenv("TEMP_DEADBAND", "1.0")),
        cooling_k=cooling_k,
        heating_rate=_heating_rate(),
        outside_at=_outside_temp_in,
    )
    decision = scheduling.plan(
        hourly_prices,
        list(temperature_history),
        datetime.datetime.now(ZoneInfo("Europe/Helsinki")),
        policy,
        heating_schedule,
    )
    heating_schedule = decision.hours
    if decision.hours_needed > policy.max_hours - decision.budget_used:
        APP.logger.warning(
            "budget exhausted: need %d hours, %d of %d used in this window",
            decision.hours_needed,
            decision.budget_used,
            policy.max_hours,
        )
    APP.logger.info(
        "schedule: %d hours (%s; need %d, budget %d/%d, deadline %.1fh,"
        " k=%.5f, rate=%.2f): %s",
        len(heating_schedule),
        decision.reason,
        decision.hours_needed,
        decision.budget_used,
        policy.max_hours,
        decision.deadline_hours,
        cooling_k,
        policy.heating_rate,
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
                store.save_reading(
                    temperature_history[-1]["time"],
                    pool["current_temp"],
                    pool["desired_temp"],
                    latest_outside_temp,
                )
                metrics.API_LAST_SUCCESS.set(
                    datetime.datetime.now(tz=datetime.UTC).timestamp()
                )
                metrics.POOL_TEMPERATURE.set(pool["current_temp"])
                metrics.DESIRED_TEMPERATURE.set(pool["desired_temp"])
                if latest_outside_temp is not None:
                    metrics.OUTSIDE_TEMPERATURE.set(latest_outside_temp)

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
        metrics.API_FAILURES.inc()
        APP.logger.info(
            "ignoring controlmyspa API error, retrying next control loop: %s",
            exception,
        )


def _start_override(temp: float, hours: int) -> None:
    """Take manual control at `temp` for `hours`, pausing automatic control."""
    global manual_override_endtime  # noqa: PLW0603
    manual_override_endtime = datetime.datetime.now(
        tz=datetime.UTC
    ) + datetime.timedelta(hours=hours)
    set_temp(temp, skip_override_detection=True)


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
        heat_estimate_minutes = int(degrees_remaining / _heating_rate() * 60)
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

    temp_min = float(os.getenv("TEMP_MIN", "34"))
    predicted_deadline = None
    if pool:
        dh = predict_time_to_temp(temp_min, pool["current_temp"])
        if dh > 0:
            predicted_deadline = datetime.datetime.now(
                ZoneInfo("Europe/Helsinki")
            ) + datetime.timedelta(hours=dh)

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
        temp_min=temp_min,
        predicted_deadline_str=predicted_deadline.strftime("%d.%m.%Y %H:%M")
        if predicted_deadline
        else None,
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
        _start_override(override_temp, 12)
        send_telegram(
            f"\U0001f525 Heat override enabled via web"
            f" (target {override_temp}\u00b0C for 12h)"
        )
    elif action == "cold":
        override_temp = int(os.getenv("TEMP_LOW", "0")) + 0.5
        _start_override(override_temp, 24)
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


def _predict_future_temps() -> list[dict]:
    """Predict pool temperature for each future hour we have a price for.

    One forward pass: cool each hour, then add the heater's gain if that hour
    is booked, and record the result wherever a price interval starts.
    """
    tz = ZoneInfo("Europe/Helsinki")
    now_hour = datetime.datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    temp_high = float(int(os.getenv("TEMP_HIGH", "0")))
    rate = _heating_rate()
    temp = (
        temperature_history[-1]["current_temp"]
        if temperature_history
        else DEFAULT_POOL_TEMP
    )

    predictions = []
    for step in range(FORECAST_HOURS + 1):
        step_dt = now_hour + datetime.timedelta(hours=step)
        outside = _outside_temp_at(step_dt)
        if step > 0 and temp > outside:
            temp -= cooling_k * (temp - outside)
        if step_dt.isoformat() in heating_schedule:
            temp = min(temp + rate, temp_high)
        if step_dt.isoformat() in hourly_prices:
            predictions.append(
                {
                    "time": step_dt.astimezone(datetime.UTC).isoformat(),
                    "temp": round(temp, 1),
                }
            )
    return predictions


@APP.route("/api/temperatures")
def api_temperatures() -> flask.Response:  # pylint: disable=too-many-locals
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

    temp_min = float(os.getenv("TEMP_MIN", "34"))
    current_temp = (
        temperature_history[-1]["current_temp"] if temperature_history else None
    )
    predicted_deadline = None
    if current_temp is not None:
        deadline_hours = predict_time_to_temp(temp_min, current_temp)
        if deadline_hours > 0:
            predicted_deadline = (
                datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(hours=deadline_hours)
            ).isoformat()

    # All prices (past + future) for chart overlay
    all_prices = [
        {
            "time": datetime.datetime.fromisoformat(k)
            .astimezone(datetime.UTC)
            .isoformat(),
            "price": v,
            "heating": k in heating_schedule,
        }
        for k, v in sorted(hourly_prices.items())
    ]

    return flask.jsonify(
        {
            "history": list(temperature_history),
            "future": future,
            "prices": all_prices,
            "temp_high": temp_high,
            "temp_low": temp_low,
            "temp_min": temp_min,
            "outside_temp": latest_outside_temp,
            "weather_forecast": [
                {"time": t, "temp": v} for t, v in sorted(weather_forecast.items())
            ],
            "cooling_k": cooling_k,
            "heating_rate": _heating_rate(),
            "predicted_deadline": predicted_deadline,
            "predicted_temps": _predict_future_temps(),
        }
    )


MAX_HISTORY_ROWS = 50000


@APP.route("/api/history")
def api_history() -> flask.Response:
    """Return stored readings and prices for a time range, straight from SQLite.

    /api/temperatures only serves the in-memory window (a few days). This
    endpoint reaches the full tables, so past scheduling decisions can be
    replayed against the prices that were actually paid.
    """
    if not store.enabled:
        return flask.jsonify({"error": "sqlite disabled"}), 503

    now = datetime.datetime.now(tz=datetime.UTC)
    try:
        start = datetime.datetime.fromisoformat(
            flask.request.args.get("from")
            or (now - datetime.timedelta(days=7)).isoformat()
        )
        end = datetime.datetime.fromisoformat(
            flask.request.args.get("to") or now.isoformat()
        )
        limit = min(
            int(flask.request.args.get("limit", MAX_HISTORY_ROWS)), MAX_HISTORY_ROWS
        )
    except ValueError:
        return flask.jsonify({"error": "from/to must be ISO timestamps"}), 400

    return flask.jsonify(
        {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "readings": store.readings_between(start, end, limit),
            "prices": store.prices_between(start, end),
        }
    )


def _refresh_gauges() -> None:
    """Fill the derived gauges from current state, at scrape time."""
    now = datetime.datetime.now(tz=datetime.UTC)
    metrics.COOLING_K.set(cooling_k)
    metrics.HEATING_RATE.set(_heating_rate())
    metrics.PRICE_HOURS_KNOWN.set(
        sum(1 for key in hourly_prices if datetime.datetime.fromisoformat(key) >= now)
    )
    remaining = (manual_override_endtime - now).total_seconds()
    metrics.OVERRIDE_REMAINING.set(max(remaining, 0))
    current_hour = datetime.datetime.now(ZoneInfo("Europe/Helsinki")).replace(
        minute=0, second=0, microsecond=0
    )
    metrics.HEATING_SCHEDULED.set(
        1 if current_hour.isoformat() in heating_schedule else 0
    )


@APP.route("/metrics")
def metrics_endpoint() -> flask.Response:
    """Expose Prometheus metrics for the ServiceMonitor to scrape.

    Unauthenticated, like `/`. It exposes water temperature, price counts and
    the measured constants, none of which are secret.
    """
    _refresh_gauges()
    payload, content_type = metrics.render()
    return flask.Response(payload, mimetype=content_type)


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
            minutes = int(remaining / _heating_rate() * 60)
            lines.append(f"\u23f1 Est. heating time: {format_duration(minutes)}")
        if override_active:
            tz = ZoneInfo("Europe/Helsinki")
            lines.append(
                f"\u26a0\ufe0f Manual override until"
                f" {manual_override_endtime.astimezone(tz).strftime('%H:%M')}"
            )
        temp_min = float(os.getenv("TEMP_MIN", "34"))
        dh = predict_time_to_temp(temp_min, pool["current_temp"])
        if dh > 0:
            deadline = datetime.datetime.now(
                ZoneInfo("Europe/Helsinki")
            ) + datetime.timedelta(hours=dh)
            lines.append(
                f"\u23f0 Reaches {temp_min}\u00b0C at ~{deadline.strftime('%H:%M')}"
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
    override_temp = int(os.getenv("TEMP_HIGH", "0")) - 0.5
    _start_override(override_temp, 12)
    pool = cache.get("pool")
    current = pool["current_temp"] if pool else "?"
    send_telegram(
        f"\U0001f525 Heat override enabled via Telegram"
        f" (target {override_temp}\u00b0C, current: {current}\u00b0C)"
    )


def _handle_telegram_cold(chat_id: str) -> None:  # noqa: ARG001  # pylint: disable=unused-argument
    """Handle /cold command -- set TEMP_LOW for 24h."""
    override_temp = int(os.getenv("TEMP_LOW", "0")) + 0.5
    _start_override(override_temp, 24)
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
