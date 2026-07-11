# Phase B — custom app design sketch

Once the evcc trial has validated the Tesla command flow and the slot
economics, replace evcc with a single-file Flask + APScheduler app
("tesla-porssari" style) mirroring `../../app.py`. This is a design sketch,
not an implementation spec — revisit after living with evcc for a few weeks.

## Why replace evcc at all?

Same reason the spa app exists instead of a generic controller: exact control
over the algorithm, a UI/Telegram surface shaped for one household, and fewer
moving parts (no sqlite/planner state; the plan is recomputed from scratch
every run, like `control()` recomputes the spa temperature).

## Architecture

```
TeslaMate ── MQTT ──► app (Flask + APScheduler) ──► tesla-http-proxy ──► Tesla
spot-hinta.fi ──────►      │       │
                    Telegram bot  web GUI (price bars + planned slots + SOC)
```

Keep TeslaMate, Mosquitto, Postgres and tesla-http-proxy from Phase A;
drop only evcc.

## Inputs

- **SOC / plugged-in / location** — primary: TeslaMate MQTT via `paho-mqtt`
  (`teslamate/cars/1/battery_level`, `charge_limit_soc`, `plugged_in`,
  `state`, `geofence`, `charger_power`). Free, sleep-friendly, push-based.
- **Verification poll** — one Fleet API `vehicle_data` request immediately
  before issuing any command ($0.002), so decisions never rely on stale MQTT.
- **Prices** — spot-hinta.fi (free, no registration, 15-min resolution),
  ENTSO-E as fallback (free registration). Fetch with the tenacity
  retry/backoff pattern from `app.py:250-312` (`update_porssari`).

## Planner (pure function, heavily unit-tested)

```
needed_kwh   = (charge_limit_soc - battery_level) / 100 * USABLE_KWH / EFFICIENCY
needed_slots = ceil(needed_kwh / (CHARGE_KW * 0.25)) + 1   # +1 safety slot
plan         = cheapest needed_slots 15-min slots in [now, DEADLINE)
             → sorted chronologically → adjacent slots merged into
               (start, stop) command pairs
```

- `USABLE_KWH` (~75), `CHARGE_KW` (11), `EFFICIENCY` (~0.90), `DEADLINE`
  (default 08:00) as env vars, spa-app style. Taper above ~90 % is ignored;
  the safety slot covers it for typical 80 % limits.
- Re-plan every hour on fresh SOC (charging faster/slower than modeled
  self-corrects, like the spa's 15-min control loop).
- Guards before any command: `plugged_in` AND `geofence == Home`.

## Command executor

- POST to the same self-hosted tesla-http-proxy:
  `/api/1/vehicles/{vin}/command/charge_start|charge_stop`, preceded by
  `wake_up` + poll-until-online (≤ 2 min, tenacity backoff).
- OAuth: refresh-token grant on 401; keep tokens in memory, persist rotations
  to a small PVC file (the one piece of durable state this app needs).
- Alternative worth evaluating: the `tesla-fleet-api` PyPI library signs
  commands natively in Python, which would remove the proxy container.

## Parity features (reuse from ../../app.py)

| Feature | Reuse from |
|---|---|
| Telegram send + allowed-chat auth + webhook self-registration + dispatch | `app.py:43-70`, `app.py:605-740` |
| Manual-override state machine (12 h pause when the human intervenes — here: charging seen while no slot active, or charging stopped during a planned slot) | `app.py:391-438` |
| History ring buffer + JSON API (SOC instead of temperature) | `app.py:567+`, `collections.deque` |
| Stale-data watchdog (MQTT silent > N hours → Telegram alert) | `check_stale_temperature`, `app.py:81-137` |
| APScheduler job setup (15-min control loop, hourly re-plan) | `initialize()`, `app.py:210-247` |
| nox / ruff(ALL) / pytest+mock / Dockerfile / GH Actions / sops deploy | repo root scaffold |

Telegram commands: `/status`, `/plan`, `/charge` (force now), `/stop`,
`/limit 80`, `/deadline 07:30`, `/override`.

Web GUI: price bar chart with selected slots highlighted, SOC history graph,
plugged-in/override status — same single-template approach as
`templates/index.html`.

## Testing

- Planner: pure-function tests over synthetic price curves (flat, spiky,
  negative prices, fewer available slots than needed, already-at-limit).
- MQTT: mock `paho.mqtt.client.Client`; feed retained-message snapshots.
- Commands/prices: mock `requests` as in `test_app.py`; reuse the fake-clock
  tenacity pattern (`test_app.py:471-493`).
- Global-state reset via autouse fixture, as `test_app.py:14-27`.

## Open questions to answer during the evcc trial

1. How often does the car actually need waking for `charge_start`? (drives
   wake cost + latency margin around slot starts)
2. Does charging at 11 kW hold, or does the car derate (adjust `CHARGE_KW`)?
3. Are non-contiguous slots annoying in practice (relay clicks, app
   notifications at 03:00)? If so, bias the planner toward contiguity with a
   small price penalty for gaps.
4. Is TeslaMate MQTT reliable enough as primary SOC source, or should the
   app poll Fleet API hourly regardless (≈ $1.44/mo at hourly cadence)?
