# Prometheus Monitoring with Telegram Alerting

**Status:** not started. Adopt the pattern already running in Landingpager.

## Why

On 2026-08-22 the app was down for roughly 40 minutes and **no alert fired at all**. The Dockerfile copied only `app.py` after the module split, so the container died at startup, three deploys in a row timed out on the rollout, and the site served 503.

Every alert this app can send (the stale-temperature warning, the startup healthcheck) requires the app to be running. That excludes the failure mode that matters most. Monitoring has to live outside the process it watches.

A deploy-failure notification (see below) would have caught this particular case, but only this case: the spa can stop being controlled by an OOM kill, a node drain, a Balboa API outage or an override left on, none of which involve a deploy.

## What to adopt from Landingpager

Landingpager runs Prometheus + Alertmanager with Telegram alerting. Its manifests were read on 2026-08-23 and live in `~/dev/landingpager/deploy/`:

- `alertmanagerconfig.yaml`: a `monitoring.coreos.com/v1beta1` `AlertmanagerConfig` with `telegramConfigs`, the bot token pulled from a secret named `alertmanager-telegram` under key `token`, and `chatID` in plaintext. It is **namespace-scoped and needs no cluster-admin**, which was the thing this note originally expected to be the blocker.
- `servicemonitor.yaml`: selects on the `app:` label, scrapes the named port `web` at `/metrics` every 30s.
- `prometheusrule.yaml`: nine rules. The infrastructure ones (restart-looping, OOMKilled, PodNotReady, memory headroom, CPU throttling) port over with only the container name changed.
- `blackbox-exporter.yaml`: their own blackbox exporter for external probing.
- CI creates the `alertmanager-telegram` secret with `oc create secret ... --dry-run=client -o yaml | oc apply -f -`, then applies `deploy/`.

Two differences on our side:

- Our deploy job already runs `oc apply -f deploy/`, so new manifests cost nothing in CI. The bot token is already in the SOPS-encrypted `deploy/secret.yaml`, so a second Secret document there named `alertmanager-telegram` avoids adding a GitHub secret at all.
- Our `deploy/service.yaml` port has no `name:`. A `ServiceMonitor` endpoint needs one.

## Why this beats fixing the in-app check

For a dead app, "monitoring lives outside the process" is the whole argument. For an offline gateway it is not: the app is healthy and could perfectly well send that alert itself. The reasons are narrower, and worth stating precisely so the work is not justified by the wrong one.

**1. The current check is unreachable in the loudest version of the failure.** `check_stale_temperature()` is the last statement inside the tenacity retry block in `set_temp()` (`app.py:640`), so reaching it requires the ControlMySpa call to have *succeeded*. When the spa is unreachable the attempt raises `SpaOfflineError` or a `RequestException`, tenacity retries for 600 seconds, and the handler on the next line logs `ignoring controlmyspa API error, retrying next control loop` and returns. A hard outage produces a log line and nothing else. Only the quiet variant, where the API answers with a frozen temperature, is caught at all. Every manual-override branch also returns before line 640, so an override left on silences the check entirely.

A gauge does not share that fate. It is written on a successful read and evaluated by an external scraper, so the rule fires no matter which code path ran or failed to run.

**2. It becomes a fact instead of an inference.** Today "stuck" is derived: take the current mode stretch, pick a 90-minute or 12-hour window, compute expected movement from `heating_rate` or `cooling_k x dT`, compare against 25%. `STUCK_FRACTION`, `MIN_STALE_READINGS`, `STALE_HEATING_MINUTES`, `STALE_IDLE_MINUTES` and `thermal.mode_stretch()` exist only because the app is inferring whether readings are fresh from whether the water moved. That inference is what produced the nightly false alerts fixed in March. "The last successful read was 70 minutes ago" needs none of it and cannot be wrong.

**3. Alert lifecycle stops being our code.** `STALE_ALERT_ACTIVE`, `last_stale_alert_time`, the repeat-once-per-window throttle and the "gateway appears to be back online" message are a hand-rolled Alertmanager. `for:`, `repeat_interval` and resolved notifications replace them, and silences become possible, which today they are not.

**What it does not buy.** The gauge still needs the app alive to be exposed, so on that axis it is no stricter than the in-app check. The gain is that one rule set covers both halves: `up == 0` and replicas-available answer "the app is dead", the spa gauges answer "the app is alive but not in control".

## Two tiers

### Tier 1: no app changes

kube-state-metrics ships with OpenShift:

```yaml
- alert: SpaControllerDown
  expr: kube_deployment_status_replicas_available{deployment="controlmyspa-porssari"} == 0
  for: 5m
```

Catches the 2026-08-22 outage, crash loops, OOM kills, evictions, node drains.

### Tier 2: a `/metrics` endpoint

The app can be up and still not controlling the spa. All of this is state the app already holds. One process, one replica, so a plain `prometheus_client` registry is enough; no multiprocess mode.

#### Two signals for the spa, not one

These fail independently and must be measured separately.

**Is the API answering?** Record the moment of the last successful ControlMySpa read.

| metric | set where |
|---|---|
| `spa_api_last_success_timestamp_seconds` | end of a successful `set_temp()` attempt |
| `spa_api_failures_total` | the `RetryError` handler, which today only logs |

```yaml
- alert: SpaApiUnreachable
  expr: time() - spa_api_last_success_timestamp_seconds > 3600
```

No `for:` needed: the expression is already a duration test. The counter is not for alerting but for the graph that turns "it has been flaky since Tuesday" from a hunch into a fact.

**Is the data changing?** The API can answer while the gateway behind it is dead, returning the same reading forever. That is the case the current heuristic was written for, and it needs no new metric beyond the temperature gauge:

```yaml
- alert: SpaReadingFrozen
  expr: changes(spa_pool_temperature_celsius[12h]) == 0
  for: 30m
```

Stated honestly: a pool holding at setpoint can legitimately sit in the same 0.5 C bucket for hours, which is why the window is 12 hours and not one. That threshold is still a guess. What is no longer a guess is the *logic*: no mode stretches, no expected-movement model, no dependence on `cooling_k` being well estimated. And it is tuned by editing a rule against recorded data rather than by changing the control loop.

Both fire together when the API is down, since a gauge that stops being updated is also one that stops changing. Either add an Alertmanager inhibition so `SpaApiUnreachable` suppresses `SpaReadingFrozen`, or accept two messages that say the same thing.

#### The rest

| metric | catches |
|---|---|
| `spa_pool_temperature_celsius` | below `TEMP_MIN` for an hour |
| `spa_price_hours_known` | spot-hinta outage, prices going stale |
| `spa_cooling_k`, `spa_heating_rate` | the estimators drifting, the graph that would have shown 2.5 vs 1.6 C/h at a glance |
| `spa_manual_override_seconds_remaining` | an override quietly left on for days |

`deploy/deployment.yaml` and `deploy/service.yaml` already carry `app: controlmyspa-porssari`, so a `ServiceMonitor` is nearly free once the service port is named.

### This lets us delete code

Once `SpaApiUnreachable` and `SpaReadingFrozen` are live, `check_stale_temperature()` goes, and with it `STALE_ALERT_ACTIVE`, `last_stale_alert_time`, `STUCK_FRACTION`, `MIN_STALE_READINGS`, `STALE_HEATING_MINUTES` and `STALE_IDLE_MINUTES`. `thermal.mode_stretch()`, `thermal.expected_gain()` and `thermal.expected_drop()` have no other caller, so they go too, along with their tests.

## What the cluster allows (checked 2026-08-23)

Checked against the `exoscale-ch-gva-2-0` APPUiO cluster, namespace `aarno-playground`, where `deployment/controlmyspa-porssari` runs.

All three CRDs exist, at the versions Landingpager's manifests use: `servicemonitors` and `prometheusrules` on `monitoring.coreos.com/v1`, `alertmanagerconfigs` on `v1beta1`. As a human namespace owner, `oc auth can-i create` answers yes to all three, so the original question (can we reach an Alertmanager we control?) is settled.

**The CI service account cannot create them.** `github-deploy`, the account the deploy job logs in as, is bound only to ClusterRole `admin`, which does not cover `monitoring.coreos.com`:

```
oc auth can-i create servicemonitors.monitoring.coreos.com -n aarno-playground \
  --as=system:serviceaccount:aarno-playground:github-deploy
no
```

Same answer for `prometheusrules` and `alertmanagerconfigs`. Since `oc apply -f deploy/` is a single step, dropping the manifests into `deploy/` without fixing this fails the whole deploy, app included.

Landingpager solves it with one extra namespaced Role and RoleBinding, which is why its CI account answers yes:

```yaml
kind: Role
metadata:
  name: monitoring-editor
rules:
  - apiGroups: [monitoring.coreos.com]
    resources: [alertmanagerconfigs, probes, prometheusrules, servicemonitors]
    verbs: [get, list, watch, create, update, patch, delete]
```

bound to `system:serviceaccount:<namespace>:<ci-account>`.

**CI cannot bootstrap that for itself.** A server-side dry-run as `github-deploy` is refused with `attempting to grant RBAC permissions not currently held`, which is Kubernetes' escalation prevention working as designed. The Role and RoleBinding have to be applied once by someone who already holds those verbs; in this namespace the `vshn` group has `monitoring-edit` and `alert-routing-edit`. Keep both objects in `deploy/` so they are recorded and re-applied, but the first apply is manual and has to happen before the manifests that need them.

Still unverified: whether the platform Thanos exposes `kube_deployment_status_replicas_available` to us. Querying `thanos-querier-openshift-monitoring` with a namespace user's token returns `Forbidden (resource=prometheuses, subresource=api)`, so tier 1's expression has never been run against real data. Rule evaluation happens inside Prometheus and may well work where the ad-hoc query does not, but treat it as unproven until an alert actually arrives in Telegram.

Instrumenting `/metrics` is worth doing regardless: it is useful for graphs before any alerting exists, and it is the prerequisite for all of the above.

## Also worth doing, separately

A deploy-failure notification in the `deploy` job (`if: failure()`, Telegram, including the failing pod's last log lines). It answers a different question: Prometheus says *the spa is not being controlled*, this says *why the deploy failed*. On 2026-08-22 it would have said `ModuleNotFoundError: No module named 'pricing'` at 22:09 rather than nothing at all. The job already decrypts `deploy/secret.yaml`, which holds the bot token, so no new secret is needed.
