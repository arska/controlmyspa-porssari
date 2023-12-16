import os
import flask
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import pprint
import logging
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import controlmyspa
from flask_caching import Cache


APP = flask.Flask(__name__)
cache = Cache(APP, config={"CACHE_TYPE": "SimpleCache"})

scheduler = BackgroundScheduler()


porssari_config = {}
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
"""


def initialize():
    global scheduler
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
        next_run_time=datetime.datetime.now(),
    )


def update_porssari():
    with APP.app_context():
        API = "https://api.porssari.fi/getcontrols.php"
        new_config = requests.get(
            API,
            {
                "device_mac": os.getenv("PORSSARI_MAC"),
                "client": "controlmyspa-porssari-1",
            },
        )
        global porssari_config
        try:
            porssari_config = new_config.json()
            APP.logger.info("got porssari config: %s", porssari_config)
            # run the control loop once after we have a (new) config, especially on startup
            control()
        except requests.exceptions.JSONDecodeError:
            APP.logger.error("porssari fetch failed: %s", new_config.content)
            if not porssari_config:
                # retry in a minute if we don't have any config at all
                # else retry in the next normal 15m interval
                global scheduler
                scheduler.add_job(
                    update_porssari,
                    "date",
                    run_date=(datetime.datetime.now() + datetime.timedelta(minutes=1)),
                )


def control():
    global porssari_config
    if not porssari_config:
        APP.logger.error("no porssari config present, not controlling")
        return
    current_hour = datetime.datetime.now(ZoneInfo("Europe/Helsinki")).hour
    command = porssari_config.get("Channel1", {}).get(str(current_hour), "0")
    if int(os.getenv("TEMP_OVERRIDE", 0)):
        # if set, override temperature independent of hour control
        set_temp(os.getenv("TEMP_OVERRIDE", 0))
    elif command == "0":
        # low temp
        set_temp(os.getenv("TEMP_LOW"))
    else:
        # command = "1"
        # high temp
        set_temp(os.getenv("TEMP_HIGH"))


def set_temp(temp):
    api = controlmyspa.ControlMySpa(
        os.getenv("CONTROLMYSPA_USER"), os.getenv("CONTROLMYSPA_PASS")
    )
    pool = {"desired_temp": api.desired_temp, "current_temp": api.current_temp}
    cache.set("pool", pool, timeout=15 * 60)

    APP.logger.info(
        "current temp: %s, desired temp: %s", pool["current_temp"], pool["desired_temp"]
    )
    api.desired_temp = int(temp)
    APP.logger.info("set desired temp %s", temp)


def ping():
    with APP.app_context():
        global porssari_config
        APP.logger.debug("ping %s", porssari_config)


@APP.route("/")
def hello():
    global porssari_config
    pool = cache.get("pool")
    if pool == None:
        api = controlmyspa.ControlMySpa(
            os.getenv("CONTROLMYSPA_USER"), os.getenv("CONTROLMYSPA_PASS")
        )
        pool = {"desired_temp": api.desired_temp, "current_temp": api.current_temp}
        cache.set("pool", pool, timeout=15 * 60)
    return flask.render_template(
        "index.html",
        porssari_config=porssari_config,
        current_hour=datetime.datetime.now(ZoneInfo("Europe/Helsinki")).hour,
        api=pool,
    )


if __name__ == "__main__":
    load_dotenv()
    initialize()
    APP.wsgi_app = ProxyFix(APP.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    APP.logger.setLevel("DEBUG")
    APP.run(host="0.0.0.0", port=os.environ.get("PORT", 8080))
