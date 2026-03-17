# CLAUDE.md — controlmyspa-porssari

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

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main application — Flask routes, control logic, scheduled jobs |
| `templates/index.html` | Jinja2 template — Chart.js temp graph, override toggle, schedule grid |
| `test_app.py` | Pytest test suite (23 tests) |
| `noxfile.py` | Nox sessions: ruff, pylint, tests, docker |
| `pyproject.toml` | Project config, dependencies, ruff/pylint settings |
| `Dockerfile` | Python 3.14-alpine, uv for dependency management |
| `get_certificate.py` | SSL cert setup for iot.controlmyspa.com (used in Docker build) |
| `.github/workflows/docker-image.yml` | GitHub Actions CI |
| `.gitlab-ci.yml` | GitLab CI pipeline |

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

- **Test-driven development**: Write tests first, then implement. Always run tests (`uvx nox` or `pytest test_app.py -v`) after every change.
- **Always format before committing**: Run `ruff format .` after every code change. CI checks both `ruff check` AND `ruff format --check` — formatting violations fail the build.
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

# Run tests directly
pytest test_app.py -v
pytest --cov=app --cov-report=term

# Lint and format
ruff check .
ruff format --check .
```

## Code Style

- **Linter**: ruff with `select = ["ALL"]` (very strict). Key ignores: D203, D213, COM812, ISC001.
- **Test file ignores**: S101 (assert), ANN (annotations), PLR2004 (magic values), PT022, ARG002.
- **Formatter**: ruff format (Black-compatible), 88 char line length, double quotes.
- **Pylint**: Disables `pointless-string-statement`, `global-statement`.
- **Target**: Python 3.14.

## Testing

23 tests in `test_app.py` covering:
- Status page rendering (with/without cache, with porssari config)
- `/api/temperatures` endpoint (empty history, data, bounds, future schedule, maxlen)
- `/api/override` endpoint (enable, disable, invalid action, no body)
- `control()` logic (no config, command 0/1, TEMP_OVERRIDE)
- `set_temp()` (history recording, caching, temp changes, manual override detection)
- `update_porssari()` (config parsing, whitespace handling)

Tests mock `controlmyspa.ControlMySpa` and `requests.get` to avoid external API calls. Global state is reset between tests via autouse fixture.

## CI/CD

- **GitHub Actions** (`.github/workflows/docker-image.yml`): runs `uvx nox` then builds/pushes Docker image to GHCR
- **GitLab CI** (`.gitlab-ci.yml`): `lint-and-test` stage (uv + nox), `docker` stage (build + smoke test)

## External APIs

1. **Pörssäri.fi** (`https://api.porssari.fi/getcontrols.php`): Returns hourly on/off commands per channel. May have leading whitespace in response (handled with `.strip()`).
2. **ControlMySpa** (via `controlmyspa` package): Authenticates to `iot.controlmyspa.com`, reads/writes spa temperatures. Retries with exponential backoff (tenacity, up to 5 attempts).

## Manual Override Logic

When the spa's desired temp doesn't match TEMP_HIGH or TEMP_LOW, the system assumes manual control via physical spa controls and pauses automatic control for 12 hours. The web GUI also allows enabling/disabling override via `/api/override`.
