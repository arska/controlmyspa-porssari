"""Nordpool based temperature control for Balboa ControlMySpa Whirlpools.

We use https://porssari.fi for time- and price-based temperature control of
https://github.com/arska/controlmyspa[Balboa ControlMySpa] based Whirlpools.
"""

import collections
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

import controlmyspa
import flask
import requests
import sentry_sdk
import tenacity
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask_caching import Cache
from sentry_sdk.integrations.flask import FlaskIntegration
from werkzeug.middleware.proxy_fix import ProxyFix

APP = flask.Flask(__name__)
cache = Cache(APP, config={"CACHE_TYPE": "SimpleCache"})
scheduler = BackgroundScheduler()
PORSSARI_API = "https://api.porssari.fi/getcontrols.php"
# Average heating rate in °C per hour, measured empirically
HEATING_RATE_PER_HOUR = 1.5
porssari_config = {}
# 48h of data at 15min intervals = 192 data points
temperature_history: collections.deque[dict] = collections.deque(maxlen=192)

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
    threshold = 3 if heating else 25
    window = history[-threshold:]

    if len(window) < threshold:
        return

    temps = [r["current_temp"] for r in window]
    is_stale = (max(temps) - min(temps)) < 0.5  # noqa: PLR2004

    if is_stale:
        if STALE_ALERT_ACTIVE:
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
        STALE_ALERT_ACTIVE = True
    elif STALE_ALERT_ACTIVE:
        send_telegram(
            f"\u2705 Spa temperature is changing again"
            f" (now {latest['current_temp']}\u00b0C)."
            f" Gateway appears to be back online."
        )
        STALE_ALERT_ACTIVE = False


"""
Example porssari.fi config:
{'Channel1': {'0': '0',
              '10': '0',
              '11': '0',
              '12': '0',
              '13': '0',
              '14': '0',
              '15': '0',
              '16': '0',
              '17': '0',
              '18': '0',
              '19': '0',
              '20': '0',
              '21': '0',
              '22': '0',
              '23': '0',
              '8': '0',
              '9': '0'},
 'Metadata': {'Channels': '1',
              'Date': '2023-12-16',
              'Fetch_url': 'https://api.porssari.fi/getcontrols.php',
              'Hours_count': 17,
              'Mac': 'A1B2C3D4E5F6',
              'Time': '08:50:12',
              'Timestamp': '1702709412',
              'Timestamp_offset': '7200'}}

And another example across midnight:
{
    "Metadata": {
        "Mac": "A1B2C3D4E5F6",
        "Channels": "1",
        "Fetch_url": "https://api.porssari.fi/getcontrols.php",
        "Date": "2023-12-16",
        "Time": "21:26:00",
        "Timestamp": "1702754760",
        "Timestamp_offset": "7200",
        "Hours_count": 24,
    },
    "Channel1": {
        "21": "1",
        "22": "1",
        "23": "1",
        "0": "0",
        "1": "1",
        "2": "1",
        "3": "1",
        "4": "1",
        "5": "1",
        "6": "1",
        "7": "0",
        "8": "0",
        "9": "0",
        "10": "0",
        "11": "0",
        "12": "0",
        "13": "0",
        "14": "0",
        "15": "0",
        "16": "0",
        "17": "0",
        "18": "0",
        "19": "0",
        "20": "0",
    },
}
"""


def initialize() -> None:
    """Initialize scheduled jobs and run the control loop."""
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
        update_porssari,
        "interval",
        minutes=15,
        id="update_porssari",
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.datetime.now(tz=datetime.UTC),
    )
    send_telegram("\U0001f6c1 controlmyspa-porssari started")


def update_porssari() -> None:
    """Fetch new configuration from porssari.fi.

    The configuration is cached in memory to be able to control the pool even if
    porssari.fi was temporarily offline
    """
    with (
        sentry_sdk.start_transaction(op="task", name="Update Porssari"),
        APP.app_context(),
    ):
        for attempt in tenacity.Retrying(
            retry=(
                tenacity.retry_if_exception_type(requests.exceptions.RequestException)
                | tenacity.retry_if_exception_type(json.JSONDecodeError)
            ),
            wait=tenacity.wait_random_exponential(multiplier=1, max=60),
            stop=tenacity.stop_after_attempt(5),
            before_sleep=tenacity.before_sleep_log(APP.logger, logging.INFO),
        ):
            with attempt:
                new_config = None
                try:
                    new_config = requests.get(
                        PORSSARI_API,
                        {
                            "device_mac": os.getenv("PORSSARI_MAC"),
                            "client": "controlmyspa-porssari-1",
                        },
                        timeout=10,
                    )
                    """
                    2024-06-08: porssari started adding an extra newline before the JSON
                    object, leading to JSON parse failure. stripping extra whitespace
                    before JSON decoding here.
                    """
                    global porssari_config  # noqa: PLW0603
                    porssari_config = json.loads(new_config.text.strip())
                    APP.logger.info("got porssari config: %s", porssari_config)
                    # run the control loop once after we have a (new) config,
                    # especially on startup
                    scheduler.add_job(
                        control,
                        "date",
                        run_date=datetime.datetime.now(tz=datetime.UTC),
                    )
                except json.JSONDecodeError as exception:
                    APP.logger.info(
                        "received from porssari: %s '%s'",
                        new_config,
                        getattr(new_config, "text", "<no response>"),
                    )
                    APP.logger.info("porssari fetch failed: %s", exception)
                    if not porssari_config:
                        # retry in a minute if we don't have any config at all
                        # else retry in the next normal 15m interval
                        scheduler.add_job(
                            update_porssari,
                            "date",
                            run_date=(
                                datetime.datetime.now(tz=datetime.UTC)
                                + datetime.timedelta(minutes=1)
                            ),
                        )


def control() -> None:
    """Set the pool temperature according to porssari instructions."""
    with sentry_sdk.start_transaction(op="task", name="Update Controlmyspa"):
        if not porssari_config:
            APP.logger.error("no porssari config present, not controlling")
            return
        current_hour = datetime.datetime.now(ZoneInfo("Europe/Helsinki")).hour
        command = porssari_config.get("Channel1", {}).get(str(current_hour), "0")
        if int(os.getenv("TEMP_OVERRIDE", "0")):
            # if set, override temperature independent of hour control
            set_temp(int(os.getenv("TEMP_OVERRIDE", "0")))
        elif command == "0":
            # low temp
            set_temp(int(os.getenv("TEMP_LOW", "0")))
        else:
            # high temp
            set_temp(int(os.getenv("TEMP_HIGH", "0")))


def set_temp(temp: float) -> None:
    """Update the pool temperature.

    Also fetch the current pool temperatures and cache them for 15 minutes.
    """
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
                    os.getenv("CONTROLMYSPA_USER"), os.getenv("CONTROLMYSPA_PASS")
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
                    }
                )

                APP.logger.info(
                    "current temp: %s, desired temp: %s",
                    pool["current_temp"],
                    pool["desired_temp"],
                )
                if int(pool["desired_temp"]) != int(
                    os.getenv("TEMP_HIGH", "0")
                ) and int(pool["desired_temp"]) != int(os.getenv("TEMP_LOW", "0")):
                    # somebody set a manual temperature through the pool controls
                    # let's disable porssari control for 12h
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
                        return

                    # the manual override time expired
                    # reset the timer for the next override
                    manual_override_endtime = datetime.datetime.fromtimestamp(
                        0, tz=datetime.UTC
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


@APP.route("/")
def status() -> str:
    """WebGUI to show current porssari configuration and (cached) pool temperatures."""
    pool = cache.get("pool")
    if pool is None:
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
                        os.getenv("CONTROLMYSPA_USER"), os.getenv("CONTROLMYSPA_PASS")
                    )
                    pool = {
                        "desired_temp": api.desired_temp,
                        "current_temp": api.current_temp,
                    }
                    cache.set("pool", pool, timeout=15 * 60)
        except tenacity.RetryError:
            pool = None
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

    return flask.render_template(
        "index.html",
        porssari_config=porssari_config,
        current_hour=datetime.datetime.now(ZoneInfo("Europe/Helsinki")).hour,
        api=pool,
        manual_override_endtime=manual_override_endtime.astimezone(
            ZoneInfo("Europe/Helsinki")
        ),
        now=datetime.datetime.now(tz=datetime.UTC),
        temp_heat=int(os.getenv("TEMP_HIGH", "0")) - 0.5,
        heat_estimate_minutes=heat_estimate_minutes,
        heat_estimate_time=heat_estimate_time,
        temp_high=temp_high,
    )


@APP.route("/api/override", methods=["POST"])
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
    elif action == "disable":
        manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
        APP.logger.info("manual override disabled via web GUI")
        # trigger a control run to apply the correct temperature immediately
        scheduler.add_job(
            control, "date", run_date=datetime.datetime.now(tz=datetime.UTC)
        )
    elif action == "heat":
        override_temp = int(os.getenv("TEMP_HIGH", "0")) - 0.5
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        APP.logger.info(
            "heating override to %s°C via web GUI until %s",
            override_temp,
            manual_override_endtime,
        )
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
                    APP.logger.info(
                        "set desired temp %s via heat override",
                        override_temp,
                    )
        except tenacity.RetryError as exception:
            APP.logger.info("heat override failed: %s", exception)
    return flask.jsonify(
        {
            "override_active": manual_override_endtime
            > datetime.datetime.now(tz=datetime.UTC),
            "override_endtime": manual_override_endtime.isoformat(),
        }
    )


@APP.route("/api/temperatures")
def api_temperatures() -> flask.Response:
    """Return temperature history and future porssari schedule as JSON."""
    tz = ZoneInfo("Europe/Helsinki")
    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    temp_low = int(os.getenv("TEMP_LOW", "0"))

    # Future schedule from porssari config
    future = []
    if porssari_config.get("Channel1"):
        now = datetime.datetime.now(tz)
        for hour_str, command in porssari_config["Channel1"].items():
            hour = int(hour_str)
            # Build a datetime for this hour; past hours wrap to tomorrow
            dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if dt <= now:
                dt += datetime.timedelta(days=1)
            # Only show schedule up to 24h from now
            if dt > now + datetime.timedelta(hours=24):
                continue
            future.append(
                {
                    "time": dt.astimezone(datetime.UTC).isoformat(),
                    "target_temp": temp_high if command == "1" else temp_low,
                }
            )
        future.sort(key=lambda x: x["time"])

    return flask.jsonify(
        {
            "history": list(temperature_history),
            "future": future,
            "temp_high": temp_high,
            "temp_low": temp_low,
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
            f"\U0001f321 Current: {pool['current_temp']}\u00b0C",
            f"\U0001f3af Desired: {pool['desired_temp']}\u00b0C",
            f"\u2b06\ufe0f TEMP_HIGH: {temp_high}\u00b0C",
        ]
        if pool["current_temp"] < temp_high:
            remaining = temp_high - pool["current_temp"]
            minutes = int(remaining / HEATING_RATE_PER_HOUR * 60)
            lines.append(f"\u23f1 Est. heating time: {minutes}min")
        if override_active:
            tz = ZoneInfo("Europe/Helsinki")
            lines.append(
                f"\u26a0\ufe0f Manual override until"
                f" {manual_override_endtime.astimezone(tz).strftime('%H:%M')}"
            )
        send_telegram("\n".join(lines), chat_id=chat_id)
    else:
        send_telegram("\u274c No pool data available", chat_id=chat_id)


def _handle_telegram_override(chat_id: str) -> None:
    """Handle /override command -- toggle on/off."""
    global manual_override_endtime  # noqa: PLW0603
    if manual_override_endtime > datetime.datetime.now(tz=datetime.UTC):
        manual_override_endtime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
        scheduler.add_job(
            control, "date", run_date=datetime.datetime.now(tz=datetime.UTC)
        )
        send_telegram("\u2705 Manual override disabled", chat_id=chat_id)
    else:
        manual_override_endtime = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(hours=12)
        tz = ZoneInfo("Europe/Helsinki")
        until = manual_override_endtime.astimezone(tz).strftime("%H:%M")
        send_telegram(
            f"\u23f8 Manual override enabled for 12h (until {until})",
            chat_id=chat_id,
        )


def _handle_telegram_heat(chat_id: str) -> None:
    """Handle /heat command -- start heating."""
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
                    f"\U0001f525 Heating to {override_temp}\u00b0C"
                    f" (current: {pool['current_temp']}\u00b0C)",
                    chat_id=chat_id,
                )
    except tenacity.RetryError:
        send_telegram("\u274c Failed to set heating", chat_id=chat_id)


def _handle_telegram_schedule(chat_id: str) -> None:
    """Handle /schedule command."""
    if not porssari_config.get("Channel1"):
        send_telegram("\u274c No schedule available", chat_id=chat_id)
        return

    temp_high = int(os.getenv("TEMP_HIGH", "0"))
    temp_low = int(os.getenv("TEMP_LOW", "0"))
    lines = ["\U0001f4cb Porssari schedule:"]
    for hour in range(24):
        command = porssari_config["Channel1"].get(str(hour))
        if command is None:
            continue
        temp = temp_high if command == "1" else temp_low
        marker = "\U0001f525" if command == "1" else "\u2744\ufe0f"
        lines.append(f"{hour:02d}:00 {marker} {temp}\u00b0C")
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
