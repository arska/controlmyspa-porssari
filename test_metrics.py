"""The exposition payload must carry every metric the alert rules query."""

import metrics


def test_render_returns_a_prometheus_payload():
    """The payload names every gauge and carries a Prometheus content type."""
    payload, content_type = metrics.render()
    assert b"spa_api_last_success_timestamp_seconds" in payload
    assert "text/plain" in content_type


def test_render_reflects_the_last_value_set():
    """Setting a gauge changes what the next render exposes."""
    metrics.POOL_TEMPERATURE.set(36.5)
    payload, _ = metrics.render()
    assert b"spa_pool_temperature_celsius 36.5" in payload


def test_failure_counter_increments():
    """Incrementing the counter increases the exposed failure count by one."""

    def _current_value(payload: bytes) -> float:
        for line in payload.decode().splitlines():
            if line.startswith("spa_api_failures_total "):
                return float(line.split()[-1])
        return 0.0

    before, _ = metrics.render()
    starting = _current_value(before)
    metrics.API_FAILURES.inc()
    after, _ = metrics.render()
    assert _current_value(after) == starting + 1
