# Prometheus Monitoring with Telegram Alerting — Design Note

**Status:** not started. Adopt the pattern already running in Landingpager.

## Why

On 2026-08-22 the app was down for roughly 40 minutes and **no alert fired at all**. The Dockerfile copied only `app.py` after the module split, so the container died at startup, three deploys in a row timed out on the rollout, and the site served 503.

Every alert this app can send — the stale-temperature warning, the startup healthcheck — requires the app to be running. That excludes the failure mode that matters most. Monitoring has to live outside the process it watches.

A deploy-failure notification (see below) would have caught this particular case, but only this case: the spa can stop being controlled by an OOM kill, a node drain, a Balboa API outage or an override left on, none of which involve a deploy.

## What to adopt from Landingpager

Landingpager already runs Prometheus + Alertmanager with Telegram alerting. Take its shape rather than reinventing: scrape config, `PrometheusRule` layout, Alertmanager `telegram_configs` receiver, and whatever it does about secret handling for the bot token.

**Before implementing, read Landingpager's actual manifests** — this note is written from the pattern, not from its config.

## Two tiers

### Tier 1 — no app changes

kube-state-metrics ships with OpenShift:

```yaml
- alert: SpaControllerDown
  expr: kube_deployment_status_replicas_available{deployment="controlmyspa-porssari"} == 0
  for: 5m
```

Catches the 2026-08-22 outage, crash loops, OOM kills, evictions, node drains.

### Tier 2 — a `/metrics` endpoint

The app can be up and still not controlling the spa. All of this is state the app already holds:

| metric | catches |
|---|---|
| `spa_reading_age_seconds` | the gateway is gone — a fact, not the heuristic `check_stale_temperature()` infers |
| `spa_pool_temperature_celsius` | below `TEMP_MIN` for an hour |
| `spa_price_hours_known` | spot-hinta outage, prices going stale |
| `spa_cooling_k`, `spa_heating_rate` | the estimators drifting — the graph that would have shown 2.5 vs 1.6 °C/h at a glance |
| `spa_manual_override_seconds_remaining` | an override quietly left on for days |

`deploy/deployment.yaml` and `deploy/service.yaml` already carry `app: controlmyspa-porssari`, so a `ServiceMonitor` is nearly free — the service port needs a `name:` adding.

### This lets us delete code

`check_stale_temperature()` infers "stuck" from mode stretches and expected-movement thresholds. As a Prometheus rule that is `time() - spa_last_reading_timestamp > 3600`, and unlike the current version it fires when the app is dead. Retire the heuristic once the rule is live.

## Open question — blocks everything

On OpenShift the alerting path depends on cluster permissions:

1. Is user-workload monitoring enabled? `oc get cm cluster-monitoring-config -n openshift-monitoring`
2. Can we create `ServiceMonitor` / `PrometheusRule` in the namespace?
3. **Can we reach an Alertmanager we control?** Usually the blocker — the platform Alertmanager is cluster-admin territory. Fallbacks: ask the platform team to route this namespace's alerts, run a small Alertmanager of our own, or poll externally.

Instrumenting `/metrics` is worth doing regardless: it is useful for graphs before any alerting exists, and it is the prerequisite for all of the above.

## Also worth doing, separately

A deploy-failure notification in the `deploy` job — `if: failure()`, Telegram, including the failing pod's last log lines. It answers a different question: Prometheus says *the spa is not being controlled*, this says *why the deploy failed*. On 2026-08-22 it would have said `ModuleNotFoundError: No module named 'pricing'` at 22:09 rather than nothing at all. The job already decrypts `deploy/secret.yaml`, which holds the bot token, so no new secret is needed.
