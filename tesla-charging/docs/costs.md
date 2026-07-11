# Fleet API cost model

Tesla Fleet API pricing (developer.tesla.com, July 2026):

| Metered unit | Price | Rate limit (per vehicle) |
|---|---|---|
| Data request (poll) | $1 / 500 | 60 / min |
| Command | $1 / 1,000 | 30 / min |
| Wake | $1 / 50 | 3 / min |
| Streaming signal | $1 / 150,000 | — |
| Monthly discount | **−$10 / account** | |

## Expected usage with the Phase A configuration

evcc vehicle `cache: 15m`, one vehicle, overnight charging with ~2–3
non-contiguous slot groups per night:

| What | Volume / month | Cost |
|---|---|---|
| `vehicle_data` polls (4/h × 24 h × 30 d) | ~2,880 | $5.76 |
| Commands (charge_start/stop ~4–6/night) | ~150 | $0.15 |
| Wakes (1–2/night) | ~45 | $0.90 |
| **Total** | | **≈ $6.80 < $10 discount** |

## Sensitivities

- **Polling interval dominates.** `cache: 5m` would triple the poll cost to
  ~$17/mo and blow the discount. Keep ≥ 15m.
- **Wake loops**: a misbehaving integration that wakes the car every poll
  would cost $0.02/wake × 96/day ≈ $58/mo. Watch the usage dashboard the
  first days; TeslaMate's sleep graph is a good independent witness.
- **TeslaMate** uses the legacy Owner API — $0, not metered.
- Phase B can cut poll costs to near zero by sourcing SOC from TeslaMate MQTT
  and only polling Fleet API right before issuing commands.
