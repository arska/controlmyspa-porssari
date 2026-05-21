# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Nordpool electricity-price-based temperature control for Balboa ControlMySpa hot tubs/spas. Integrates with [Pörssäri.fi](https://porssari.fi) to heat the spa during cheap electricity hours (TEMP_HIGH) and cool during expensive hours (TEMP_LOW).

## Architecture

Single-file Flask app (`app.py`) with no persistent storage. All state is in-memory:
- `porssari_config` — cached Pörssäri schedule (dict)
- `temperature_history` — 48h ring buffer of temp readings (`collections.deque(maxlen=192)`)
- `manual_override_endtime` — datetime for manual override expiry
- `cache` — Flask-Caching SimpleCache for pool temps (15min TTL)

Background jobs via APScheduler run every 15 minutes:
- `update_porssari()` — fetches hourly on/off schedule from Pörssäri API
- `control()` — sets spa temperature via ControlMySpa API based on current hour's command

## Routes

- `GET /` — Web GUI with temp graph, pool status, override toggle, schedule grid
- `GET /api/temperatures` — JSON: temperature history + future porssari schedule
- `POST /api/override` — JSON body `{"action": "enable"|"disable"}` to toggle manual override
- `POST /telegram/<token>` — Telegram bot webhook (commands: /status, /override, /heat, /schedule)

## Environment Variables

```
CONTROLMYSPA_USER    # Balboa account email
CONTROLMYSPA_PASS    # Balboa account password
PORSSARI_MAC         # Device MAC registered on porssari.fi
TEMP_HIGH=37         # Temperature during cheap hours
TEMP_LOW=27          # Temperature during expensive hours
TEMP_OVERRIDE=0      # If non-zero, overrides all logic with this temp
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

## External APIs

1. **Pörssäri.fi** (`https://api.porssari.fi/getcontrols.php`): Returns hourly on/off commands per channel. May have leading whitespace in response (handled with `.strip()`).
2. **ControlMySpa** (via `controlmyspa` package): Authenticates to `iot.controlmyspa.com`, reads/writes spa temperatures. Retries with exponential backoff (tenacity, up to 5 attempts).

## Manual Override Logic

When the spa's desired temp doesn't match TEMP_HIGH or TEMP_LOW, the system assumes manual control via physical spa controls and pauses automatic control for 12 hours. The web GUI also allows enabling/disabling override via `/api/override`.
