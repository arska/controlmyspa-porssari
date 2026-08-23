"""Prometheus metrics for the spa controller.

One process and one replica, so the default registry is enough: no
multiprocess mode and no file-backed collectors. Every value here is state
app.py already holds; this module only names it and hands it to the scraper.
"""

import prometheus_client

API_LAST_SUCCESS = prometheus_client.Gauge(
    "spa_api_last_success_timestamp_seconds",
    "Unix time of the last successful ControlMySpa read.",
)
API_FAILURES = prometheus_client.Counter(
    "spa_api_failures",
    "ControlMySpa calls that exhausted their retries.",
)
POOL_TEMPERATURE = prometheus_client.Gauge(
    "spa_pool_temperature_celsius",
    "Pool water temperature as last reported by the spa.",
)
DESIRED_TEMPERATURE = prometheus_client.Gauge(
    "spa_desired_temperature_celsius",
    "Setpoint the spa is currently holding.",
)
OUTSIDE_TEMPERATURE = prometheus_client.Gauge(
    "spa_outside_temperature_celsius",
    "Outside air temperature used by the cooling model.",
)
COOLING_K = prometheus_client.Gauge(
    "spa_cooling_k",
    "Measured Newton cooling constant, per hour.",
)
HEATING_RATE = prometheus_client.Gauge(
    "spa_heating_rate_celsius_per_hour",
    "Measured heating rate.",
)
PRICE_HOURS_KNOWN = prometheus_client.Gauge(
    "spa_price_hours_known",
    "Future hours with a known spot price.",
)
OVERRIDE_REMAINING = prometheus_client.Gauge(
    "spa_manual_override_seconds_remaining",
    "Seconds until manual override expires, 0 when not overridden.",
)
HEATING_SCHEDULED = prometheus_client.Gauge(
    "spa_heating_scheduled",
    "1 when the current hour is booked for heating.",
)


def render() -> tuple[bytes, str]:
    """Return the exposition payload and the content type to serve it with."""
    return prometheus_client.generate_latest(), prometheus_client.CONTENT_TYPE_LATEST
