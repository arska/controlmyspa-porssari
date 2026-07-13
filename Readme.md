# Controlmyspa Pörssäri.fi

Nordpool electricity-price-based temperature control for [Balboa ControlMySpa](https://github.com/arska/controlmyspa) hot tubs. Integrates with [Pörssäri.fi](https://porssari.fi) to heat the spa during the cheapest hours ("Pörssisähkö") and lower the temperature during expensive hours.

## Features

- **Price-based heating** — heats to `TEMP_HIGH` during cheap Nordpool hours, cools to `TEMP_LOW` during expensive hours
- **Web GUI** — temperature graph, pool status, Pörssäri schedule grid, manual override controls
- **Telegram bot** — remote status checks, override toggle, heat/cold commands
- **Outside temperature tracking** — hourly weather data from [Open-Meteo](https://open-meteo.com) (free, no API key), recorded alongside spa temps for future cooling-rate analysis
- **Persistent history** — temperature readings stored in SQLite, surviving restarts
- **Stale temperature alerts** — Telegram notifications when spa readings stop changing (gateway may be offline)

## Usage

Clone this repo or pull the Docker image:

```bash
docker run -p 8080:8080 ghcr.io/arska/controlmyspa-porssari
```

The web GUI is available at http://127.0.0.1:8080/.

## Configuration

Configure using environment variables. For local development, put them in a `.env` file.

### Required

| Variable | Description |
|----------|-------------|
| `CONTROLMYSPA_USER` | Balboa account email |
| `CONTROLMYSPA_PASS` | Balboa account password |
| `PORSSARI_MAC` | MAC address registered on porssari.fi (must be unique on the platform) |
| `TEMP_HIGH` | Temperature (°C) during cheap hours (e.g. `37`) |
| `TEMP_LOW` | Temperature (°C) during expensive hours (e.g. `27`) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMP_OVERRIDE` | `0` | If non-zero, overrides all price logic with this temperature |
| `WEATHER_LAT` | `60.45` | Latitude for weather lookup (default: Turku) |
| `WEATHER_LON` | `22.27` | Longitude for weather lookup (default: Turku) |
| `SQLITE_PATH` | `/data/temperatures.db` | Path to SQLite DB for persistent temp history. SQLite is disabled if the parent directory doesn't exist, so local dev works without creating `/data/` |
| `PORT` | `8080` | Web server port |
| `SENTRY_URL` | | Sentry DSN for error tracking |
| `TELEGRAM_BOT_TOKEN` | | Telegram bot token for notifications and commands |
| `TELEGRAM_CHAT_ID` | | Telegram chat ID(s), comma-separated for multiple users |
| `TELEGRAM_WEBHOOK_URL` | | Base URL for Telegram webhook (e.g. `https://poreallas.aukia.com`) |

### Pörssäri.fi setup

On porssari.fi, create a new device of type "PICO W" with the MAC address defined in `PORSSARI_MAC`. Only one control channel is supported. Configure the "number of cheapest hours per day" to control how many hours the spa heats to `TEMP_HIGH`.

## Manual override

The system detects manual temperature changes made via the physical spa controls or the ControlMySpa app. If the spa's desired temperature doesn't match `TEMP_HIGH` or `TEMP_LOW`, automatic control is paused for 12 hours.

This is useful for pre-heating before guests: set the spa to 36.5°C (neither `TEMP_HIGH=37` nor `TEMP_LOW=27`) via the app, and Pörssäri control pauses automatically.

Override can also be toggled via the web GUI or Telegram bot (`/override`, `/heat`, `/cold`). Automatic control resumes when the 12h timeout expires or override is manually disabled.

**Note:** If the spa temperature doesn't match `TEMP_HIGH` or `TEMP_LOW` at startup, override detection triggers immediately with a 12h delay.

## Telegram bot

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the bot responds to:

| Command | Description |
|---------|-------------|
| `/status` | Current and desired temperature, heating estimate |
| `/override` | Toggle manual override on/off |
| `/heat` / `/hot` | Heat to TEMP_HIGH-0.5°C for 12h |
| `/cold` | Cool to TEMP_LOW+0.5°C for 24h |
| `/schedule` | Show Pörssäri hourly schedule |

The bot also sends alerts for stale temperature readings and manual override events.

## Deployment (Kubernetes / GitOps)

Kubernetes manifests are in `deploy/`. Secrets are encrypted with [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age). A PVC provides persistent storage for the SQLite database.

### Editing secrets

```bash
SOPS_AGE_KEY_FILE=.sops-age-key.txt sops deploy/secret.yaml
```

### Setting up on a new machine

1. Get the age private key from a team member or your password manager
2. Save it to `.sops-age-key.txt` in the repo root (gitignored)
3. Verify: `SOPS_AGE_KEY_FILE=.sops-age-key.txt sops --decrypt deploy/secret.yaml`

### CI/CD

GitHub Actions deploys to OpenShift on push to `main`. Required GitHub secrets:

- `SOPS_AGE_KEY` — age private key for decrypting secrets
- `OPENSHIFT_TOKEN` — OpenShift service account token
- `OPENSHIFT_SERVER` — OpenShift API server URL

## References

- Pörssäri client reference: https://github.com/Porssari/PicoW-client/tree/main/release
- ControlMySpa Python module: https://github.com/arska/controlmyspa
- Open-Meteo weather API: https://open-meteo.com
