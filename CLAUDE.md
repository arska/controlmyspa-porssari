# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Nordpool electricity-price-based temperature control for Balboa ControlMySpa hot tubs/spas. Fetches spot prices directly from [spot-hinta.fi](https://spot-hinta.fi) and heats the spa during the cheapest hours (TEMP_HIGH), cooling during expensive hours (TEMP_LOW).

## Architecture

Single-file Flask app (`app.py`). Temperature and price history are persisted to SQLite (optional, enabled when `SQLITE_PATH` directory exists). Other state is in-memory:
- `hourly_prices` — dict of ISO datetime → price (EUR/kWh) fetched from spot-hinta.fi. Fetched prices are merged over remembered ones and pruned to the last `PRICE_MEMORY_HOURS` (168h / 7 days) so past hours stay on the chart; the `price_history` SQLite table keeps every price forever (source of truth, backfilled into memory on startup) for retroactive evaluation of the scheduling algorithm
- `heating_schedule` — set of ISO datetime keys for hours to heat (determined by cooling model)
- `cooling_k` — estimated cooling constant (Newton's law), updated from temperature history
- `heating_rate` — estimated °C/h, measured from uncapped heating stretches (clamped to 0.8-3.0, default 1.6 until 3 stretches are seen); `HEATING_RATE` overrides it
- `temperature_history` — in-memory ring buffer of temp readings (`collections.deque(maxlen=999)`); each entry records `current_temp`, `desired_temp`, and `outside_temp`. SQLite is the source of truth; deque is backfilled from the last 48h on startup.
- `manual_override_endtime` — datetime for manual override expiry
- `latest_outside_temp` — most recent outside air temperature (°C), refreshed hourly
- `cache` — Flask-Caching SimpleCache for pool temps (15min TTL)

Background jobs via APScheduler:
- `update_prices()` (every 15 min) — fetches spot prices from spot-hinta.fi, aggregates to PRICE_INTERVAL-minute slots, then calls `calculate_schedule()`
- `calculate_schedule()` — estimates cooling/heating rates, predicts when the pool hits TEMP_MIN, picks the cheapest hours before that deadline (capped by HEATING_HOURS per 14:00-14:00 window). Books nothing before the first reading, nothing while within TEMP_DEADBAND of TEMP_HIGH, and nothing while TEMP_MIN is further away than the last known price hour — spot-hinta publishes tomorrow at ~14:00, so waiting is how the cheap hours get seen at all. Blocks are sized from the pool temperature predicted *at the hour they start*, not the current one. Already-booked hours survive a deferral so a block that is about to start is never cancelled.
- `control()` (every 15 min) — sets spa temperature via ControlMySpa API based on current hour's `heating_schedule` membership
- `update_weather()` (hourly) — fetches outside air temperature from Open-Meteo for the configured location (default 20900 Turku). Used by the cooling model to predict heat loss rate.

## Routes

- `GET /` — Web GUI with temp graph, pool status, override toggle, schedule grid
- `GET /api/temperatures` — JSON: temperature history (incl. `outside_temp` per entry), latest `outside_temp`, + future price schedule with `heating` flag
- `POST /api/override` — JSON body `{"action": "enable"|"disable"}` to toggle manual override
- `GET /api/history?from=&to=&limit=` — JSON: readings and prices read straight from SQLite (not the memory window), for retroactive evaluation. 503 when SQLite is disabled
- `POST /telegram/<token>` — Telegram bot webhook (commands: /status, /override, /heat, /schedule)

## Environment Variables

```
CONTROLMYSPA_USER    # Balboa account email
CONTROLMYSPA_PASS    # Balboa account password
TEMP_HIGH=37         # Temperature during cheap hours
TEMP_LOW=27          # Temperature during expensive hours
TEMP_OVERRIDE=0      # If non-zero, overrides all logic with this temp
TEMP_MIN=34          # Minimum pool temperature — system heats to prevent dropping below this
HEATING_HOURS=6      # Max heating hours per 14:00-14:00 window (safety cap)
HEATING_RATE         # Optional override for the measured heating rate (°C/h)
TEMP_DEADBAND=1.0    # Don't book heating while within this many °C of TEMP_HIGH
PRICE_INTERVAL=60    # Aggregation interval in minutes (15 or 60)
WEATHER_LAT=60.45    # Latitude for outside-temperature lookup (default: 20900 Turku)
WEATHER_LON=22.27    # Longitude for outside-temperature lookup (default: 20900 Turku)
SQLITE_PATH=/data/temperatures.db  # Path to SQLite DB for persistent temp history (disabled if dir missing)
ADMIN_PASSWORD       # Optional password protecting write endpoints (POST /api/override)
PORT=8080            # Web server port
SENTRY_URL           # Optional Sentry DSN for error tracking
TELEGRAM_BOT_TOKEN   # Optional Telegram bot token for stale temp alerts
TELEGRAM_CHAT_ID     # Optional Telegram chat ID(s), comma-separated for multiple users
TELEGRAM_WEBHOOK_URL # Optional base URL for Telegram webhook registration (e.g. https://poreallas.aukia.com)
```

## Development Workflow

- **Test-driven development**: Write tests first, then implement. Always run tests after every change.
- **Always format before committing**: CI checks both `ruff check` AND `ruff format --check` — formatting violations fail the build.
- **Always verify**: Run `uvx nox -s ruff pylint tests` before considering any change complete.

## Development Commands

```bash
# Install dependencies
uv sync

# Run all checks (default: ruff, pylint, tests, docker)
uvx nox

# Run specific sessions
uvx nox -s ruff
uvx nox -s tests
uvx nox -s pylint

# Run a single test
pytest test_app.py -v -k "test_name"

# Run tests with coverage
pytest --cov=app --cov-report=term

# Format code
ruff format .
```

## Code Style

- ruff with `select = ["ALL"]` — very strict. See `pyproject.toml` for ignores.
- ruff format (Black-compatible), 88 char line length, double quotes.
- Target: Python 3.14.

## Testing

Tests in `test_app.py` mock `controlmyspa.ControlMySpa` and `requests.get` to avoid external API calls. Global state is reset between tests via an autouse fixture.

`SpaSimulator` (bottom of `test_app.py`) runs the real `calculate_schedule()` + `control()` loop every simulated 15 minutes against a thermal model (Newton cooling, `heating_rate` °C/h, readings quantised to the spa's 0.5°C sensor resolution). It patches `app.datetime` with a controllable clock — patching only app's namespace, not the global `datetime` module — and reproduces spot-hinta.fi's publication schedule (tomorrow's prices appear at 14:00), which is what surfaces re-planning bugs that static single-call tests cannot. `sim.cheapest_possible_cost()` gives the brute-force optimum for the hours actually consumed, so tests can assert on cost, not just on which hours were picked. The two defects it originally pinned as `xfail` (daytime top-offs, setpoint cycling from 0.5°C sensor ticks) are fixed; the tests now assert the cost stays within 10% of the brute-force optimum and that a day needs at most 3 setpoint starts.

## External APIs

1. **spot-hinta.fi** (`https://api.spot-hinta.fi/Today` and `/DayForward`): Returns 15-min interval Nordpool spot prices with tax for Finland. `update_prices()` fetches both endpoints and averages to PRICE_INTERVAL-minute slots.
2. **ControlMySpa** (via `controlmyspa` package): Authenticates to `iot.controlmyspa.com`, reads/writes spa temperatures. Retries with exponential backoff (tenacity, up to 10 minutes).
3. **Open-Meteo** (`https://api.open-meteo.com/v1/forecast`): Free, keyless weather API. `update_weather()` reads `current.temperature_2m` for WEATHER_LAT/WEATHER_LON. On failure the last value is kept.

## Manual Override Logic

When the spa's desired temp doesn't match TEMP_HIGH or TEMP_LOW, the system assumes manual control via physical spa controls and pauses automatic control for 12 hours. The web GUI also allows enabling/disabling override via `/api/override`.
