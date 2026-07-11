# tesla-charging — Nordpool-price-optimized Tesla charging

Charge the Tesla during the cheapest Nordpool Finland hours, analogous to what
the spa app in this repo does for the hot tub. This directory is self-contained
and intended to move into its own repository once the trial phase settles.

**Phase A (this scaffold):** run [evcc](https://evcc.io) as the charging planner
plus [TeslaMate](https://docs.teslamate.org) as a statistics layer on APPUiO
OpenShift. evcc reads Nordpool FI 15-minute prices, plans the cheapest
(non-contiguous) charging slots before a departure time, and starts/stops
charging with signed Tesla vehicle commands through a self-hosted
`tesla-http-proxy`.

**Phase B (later):** replace evcc with a custom single-file Flask app in the
style of `../app.py` — see [docs/phase-b-design.md](docs/phase-b-design.md).

## Architecture

```
                 Nordpool FI day-ahead prices (15-min)
                                │
                                ▼
 ┌──────────┐  Fleet API   ┌────────┐   HTTPS (in-cluster TLS)   ┌──────────────────┐
 │  Tesla   │◄────────────►│  evcc  │───────────────────────────►│ tesla-http-proxy │
 │  cloud   │  vehicle data│planner │  /api/1/vehicles/…/command │  (signs commands │
 └──────────┘              └────────┘                            │  with own key)   │
      ▲                        ▲                                 └────────┬─────────┘
      │ Owner API (read-only)  │ web UI: tesla.example.com                │ Fleet API
 ┌────┴──────┐            [Ingress]                                       ▼
 │ TeslaMate │◄── Postgres    also serves /.well-known/appspecific/  Tesla cloud
 │ + Grafana │◄── Mosquitto   com.tesla.3p.public-key.pem            (EU region)
 └───────────┘
```

- **evcc** is the only component in the control path. `mode: pv` with no solar
  meter configured means "charge only when a charging plan says so".
- **tesla-http-proxy** (Tesla's official
  [vehicle-command](https://github.com/teslamotors/vehicle-command) proxy) holds
  the Fleet API private key and signs every command. It is cluster-internal
  only; callers authenticate with their own Tesla OAuth bearer token.
- **TeslaMate** logs SOC/charging history via the legacy Owner API (sleep
  friendly, free). It is deliberately NOT in the control path — if the Owner
  API degrades further, only statistics are lost.
- The Tesla Wall Connector Gen 1 (11 kW) is not controllable; evcc's
  `vehicle-api` charger template starts/stops charging car-side, geofenced to
  home so it never interferes at public chargers.

## Quickstart order

1. **Fleet API onboarding** (one-time, ~1 h):
   follow [docs/phase0-fleet-api-runbook.md](docs/phase0-fleet-api-runbook.md).
   You end up with: client ID/secret, a private key, the public key hosted &
   registered, user access+refresh tokens, and the virtual key paired to the car.
2. **Build the proxy image**: `docker build -t ghcr.io/<you>/tesla-http-proxy:v0.4.1 proxy/`
   and push to a registry the cluster can pull from.
3. **Fill in secrets**: copy each `deploy/*-secret.example.yaml` to
   `deploy/*-secret.yaml`, insert real values, then encrypt in place:

   ```sh
   sops --encrypt --in-place tesla-charging/deploy/evcc-secret.yaml
   sops --encrypt --in-place tesla-charging/deploy/proxy-secret.yaml
   sops --encrypt --in-place tesla-charging/deploy/teslamate-secret.yaml
   cp deploy/ingress.example.yaml deploy/ingress.yaml   # set real hostname first
   sops --encrypt --in-place tesla-charging/deploy/ingress.yaml
   ```

   The repo root `.sops.yaml` already has creation rules for these paths
   (encrypted files are committed; `*.example.yaml` files never hold secrets).
4. **Update placeholders** in `deploy/wellknown-configmap.yaml` (your public
   key) and image references (`<REGISTRY>`), namespace in the evcc config's
   `commandProxy` URL.
5. **Deploy**:

   ```sh
   oc apply -f tesla-charging/deploy/wellknown-configmap.yaml -f tesla-charging/deploy/wellknown-deployment.yaml -f tesla-charging/deploy/wellknown-service.yaml
   sops -d tesla-charging/deploy/ingress.yaml | oc apply -f -
   # ...run runbook steps 5-7 (partner registration needs the public key live)...
   sops -d tesla-charging/deploy/proxy-secret.yaml | oc apply -f -
   sops -d tesla-charging/deploy/evcc-secret.yaml | oc apply -f -
   sops -d tesla-charging/deploy/teslamate-secret.yaml | oc apply -f -
   oc apply -f tesla-charging/deploy/
   ```

   (`oc apply -f tesla-charging/deploy/` skips nothing — the still-encrypted
   sops files will fail validation and can be ignored on that pass, or apply
   files individually.)
6. **Day-to-day**: open `https://tesla.example.com`, set the vehicle charge
   limit (e.g. 80 %) and a repeating plan Mon–Fri 08:00. evcc charges only in
   the cheapest 15-minute slots overnight. Grafana/TeslaMate show what happened.

## Verification checklist

| # | Check | How |
|---|-------|-----|
| 1 | Manifests valid | `oc apply --dry-run=server -f tesla-charging/deploy/` |
| 2 | Public key reachable | `curl https://tesla.example.com/.well-known/appspecific/com.tesla.3p.public-key.pem` |
| 3 | Nordpool tariff live | in evcc pod: `evcc --config /etc/evcc/evcc.yaml tariff` → 15-min FI prices |
| 4 | Fleet API auth works | `evcc --config /etc/evcc/evcc.yaml vehicle` (read-only) |
| 5 | Signing + pairing works | from a debug pod: `curl --cacert /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt -H "Authorization: Bearer $ACCESS" -X POST https://tesla-http-proxy.<ns>.svc:4443/api/1/vehicles/<VIN>/command/flash_lights` → lights flash |
| 6 | Command path | car plugged in: evcc mode `now` → charge starts; back to `pv` → stops (watch proxy logs + TeslaMate) |
| 7 | Planner end-to-end | evening: set plan (08:00); next morning check Grafana that charging hit only the planned cheap slots; check developer-dashboard usage/cost |
| 8 | Car still sleeps | TeslaMate shows `asleep` periods despite evcc polling (`cache: 15m`) |

## Costs

Roughly **$7/month** of Fleet API usage at the configured cadence, inside the
$10/month developer discount — see [docs/costs.md](docs/costs.md). Keep evcc's
vehicle `cache` at ≥ 15m; shorter polling multiplies the data-request cost.

## Known risks / open items

- **Fleet API billing**: default account billing limit is $0 — confirm in the
  developer dashboard that usage within the $10 discount works without a
  payment method, and watch usage the first days.
- **Refresh-token rotation**: evcc persists rotated tokens in its sqlite DB on
  the PVC — the PVC is load-bearing. After long downtime (> 3 months) redo
  runbook step 6.
- **`vehicle-api` charger maturity** (merged in evcc Sep 2025): geofencing is
  mandatory (else evcc would control charging at public chargers); requires the
  `vehicle_location` scope.
- **TeslaMate Owner API decay** (energy endpoints already dead in 2026):
  statistics layer only; can be moved to Fleet API mode later.
- **OpenShift restricted SCC**: sclorg postgres and nginx-unprivileged images
  chosen for arbitrary-UID compatibility; TeslaMate and Grafana need a first
  smoke test under a random UID.
