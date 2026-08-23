# Prometheus + Telegram Alerting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alert on Telegram when the spa stops being controlled, from outside the process, and retire the in-app heuristic that cannot fire when the app is dead.

**Architecture:** Three layers, each shippable alone. Namespaced RBAC lets the CI service account manage monitoring objects at all. A `PrometheusRule` plus `AlertmanagerConfig` in `deploy/` covers the "app is gone" half using metrics the cluster already collects. A `/metrics` endpoint on the Flask app plus a `ServiceMonitor` covers the "app is alive but not in control" half, and only then is the in-app `check_stale_temperature()` deleted.

**Tech Stack:** OpenShift 4 on APPUiO (`exoscale-ch-gva-2-0`, namespace `aarno-playground`), prometheus-operator CRDs (`monitoring.coreos.com/v1`, `AlertmanagerConfig` on `v1beta1`), `prometheus_client` for Python, Flask, nox, pytest.

**Spec:** `docs/plans/2026-08-22-prometheus-telegram-alerting.md`

## Global Constraints

- Python 3.14. `uv` for dependencies, `uvx nox` for tasks.
- `uvx nox -s ruff pylint tests` must pass before any commit. CI checks `ruff check` **and** `ruff format --check`.
- ruff is configured `select = ["ALL"]`. New modules need a module docstring and a docstring on every public function.
- Any new Python module imported by `app.py` MUST be added to the `COPY` line in `Dockerfile`, to `session.run("pylint", ...)` and to the `--cov=` list in `noxfile.py`. `test_dockerfile.py` fails otherwise.
- No em dashes in prose, comments, commit messages or PR text.
- Cluster namespace is `aarno-playground`. The CI service account is `github-deploy`. The human bootstrap identity is `aarno.aukia`, which holds `monitoring-edit` via the `vshn` group.
- `oc apply -f deploy/` is a single deploy step. Any object the CI account may not create fails the whole deploy, app included.
- Telegram credentials already exist in the namespace Secret `controlmyspa-porssari`, keys `TELEGRAM_BOT_TOKEN` (46 bytes) and `TELEGRAM_CHAT_ID` (10 bytes, a single id). Never print either value.

## Out of scope

The deploy-failure Telegram notification described at the end of the spec is deliberately not in this plan. It answers a different question (why a deploy failed, not whether the spa is being controlled), touches only `.github/workflows/docker-image.yml`, and needs none of the work here. Do it as its own change.

## File Structure

| File | Responsibility |
|---|---|
| `deploy/rbac.yaml` (new) | `Role` `monitoring-editor` plus `RoleBinding` to `github-deploy`. Applied once by a human, then re-applied by CI. |
| `deploy/alertmanagerconfig.yaml` (new) | Telegram receiver. Bot token by reference to the existing Secret; chat id inline because the CRD has no secret reference for it. |
| `deploy/prometheusrule.yaml` (new) | All alert rules, infra first, spa rules added in PR 3. |
| `deploy/servicemonitor.yaml` (new) | Scrape `/metrics` on the named service port. |
| `deploy/service.yaml` (modify) | Add `name: web` to the port. A `ServiceMonitor` endpoint can only reference a named port. |
| `metrics.py` (new) | Every gauge and counter, and `render()`. No Flask, no globals, no clock. |
| `app.py` (modify) | Call sites only: set gauges where readings arrive, increment the failure counter, serve `/metrics`. |
| `test_metrics.py` (new) | Covers `metrics.py` directly, no Flask. |
| `test_app.py` (modify) | `/metrics` route test; deletion of the stale-alert tests in PR 3. |
| `Dockerfile`, `noxfile.py` (modify) | Keep the new module in the image and under lint and coverage. |

---

## Phase 0: RBAC bootstrap (manual, blocks everything)

### Task 1: Grant the CI account the monitoring verbs

**Files:**
- Create: `deploy/rbac.yaml`

**Interfaces:**
- Produces: a `Role` named `monitoring-editor` and a `RoleBinding` named `monitoring-editor-github`, after which `system:serviceaccount:aarno-playground:github-deploy` may create `servicemonitors`, `prometheusrules`, `alertmanagerconfigs` and `probes`.

**Why this is manual:** the CI account cannot create the Role that grants it these verbs. Kubernetes escalation prevention refuses with `attempting to grant RBAC permissions not currently held`. Once the binding exists the account holds the verbs, so later `oc apply -f deploy/` runs re-apply both objects without trouble.

- [ ] **Step 1: Prove the gap first, so the fix is verifiable**

```bash
oc auth can-i create servicemonitors.monitoring.coreos.com -n aarno-playground \
  --as=system:serviceaccount:aarno-playground:github-deploy
```
Expected: `no`

- [ ] **Step 2: Write the manifest**

```yaml
# deploy/rbac.yaml
# ClusterRole "admin", which github-deploy is bound to, does not cover
# monitoring.coreos.com. Without this the deploy fails on the first
# monitoring object, taking the app deploy down with it.
#
# The first apply has to be done by a human who already holds these verbs
# (the vshn group has monitoring-edit here): a service account may not grant
# itself permissions it does not hold. After that CI re-applies it normally.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: monitoring-editor
  labels:
    app: controlmyspa-porssari
rules:
  - apiGroups: ["monitoring.coreos.com"]
    resources:
      - alertmanagerconfigs
      - prometheusrules
      - servicemonitors
      - probes
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: monitoring-editor-github
  labels:
    app: controlmyspa-porssari
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: monitoring-editor
subjects:
  - kind: ServiceAccount
    name: github-deploy
    namespace: aarno-playground
```

`probes` is included so the blackbox fallback in Task 3 needs no second RBAC round trip.

- [ ] **Step 3: Apply it as a human**

```bash
oc apply -f deploy/rbac.yaml -n aarno-playground
```
Expected: `role.rbac.authorization.k8s.io/monitoring-editor created` and `rolebinding.rbac.authorization.k8s.io/monitoring-editor-github created`

- [ ] **Step 4: Re-run the check that failed in Step 1**

```bash
oc auth can-i create servicemonitors.monitoring.coreos.com -n aarno-playground \
  --as=system:serviceaccount:aarno-playground:github-deploy
oc auth can-i create prometheusrules.monitoring.coreos.com -n aarno-playground \
  --as=system:serviceaccount:aarno-playground:github-deploy
oc auth can-i create alertmanagerconfigs.monitoring.coreos.com -n aarno-playground \
  --as=system:serviceaccount:aarno-playground:github-deploy
```
Expected: `yes` three times.

- [ ] **Step 5: Prove CI can now re-apply the Role itself**

```bash
oc apply -f deploy/rbac.yaml -n aarno-playground --dry-run=server \
  --as=system:serviceaccount:aarno-playground:github-deploy
```
Expected: both objects `configured` or unchanged, no `Forbidden`. If this fails, CI will fail on every future deploy and the manifest must be kept out of `deploy/`.

- [ ] **Step 6: Commit**

```bash
git add deploy/rbac.yaml
git commit -m "deploy: let the CI account manage monitoring objects"
```

---

## PR 1: Alert when the app is gone

### Task 2: Telegram receiver, proven end to end

**Files:**
- Create: `deploy/alertmanagerconfig.yaml`

**Interfaces:**
- Consumes: the `monitoring-editor` RoleBinding from Task 1; the existing Secret `controlmyspa-porssari`.
- Produces: an `AlertmanagerConfig` named `controlmyspa-porssari` routing every alert in this namespace to Telegram.

**Input needed before starting:** the numeric chat id. `AlertmanagerConfig.spec.receivers[].telegramConfigs[].chatID` is an int64 field with no secret reference, so it goes in the manifest in plaintext, as Landingpager does. Read it without pasting it anywhere else:

```bash
oc get secret controlmyspa-porssari -n aarno-playground \
  -o jsonpath='{.data.TELEGRAM_CHAT_ID}' | base64 -d
```

- [ ] **Step 1: Write the manifest**

```yaml
# deploy/alertmanagerconfig.yaml
# Namespace-scoped, so no cluster-admin is involved. The bot token is read by
# reference from the Secret the app already uses, which is why this needs no
# new secret and no SOPS round trip. chatID has no secret reference in the
# CRD, so it is inline.
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: controlmyspa-porssari
  labels:
    app: controlmyspa-porssari
spec:
  receivers:
    - name: telegram
      telegramConfigs:
        - apiURL: https://api.telegram.org
          botToken:
            name: controlmyspa-porssari
            key: TELEGRAM_BOT_TOKEN
          chatID: REPLACE_WITH_NUMERIC_CHAT_ID
          parseMode: HTML
  route:
    receiver: telegram
    groupBy: [alertname]
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 12h
```

- [ ] **Step 2: Apply it**

```bash
oc apply -f deploy/alertmanagerconfig.yaml -n aarno-playground
oc get alertmanagerconfig -n aarno-playground
```
Expected: the object exists. A malformed spec is rejected here by the CRD schema, not silently.

- [ ] **Step 3: Write a throwaway always-firing rule to test the whole chain**

The chain has four links (rule evaluated, alert routed, secret read, Telegram accepts). Test them together before trusting any real rule.

```yaml
# /tmp/pingtest.yaml, NOT committed
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: alerting-pipeline-test
  labels:
    app: controlmyspa-porssari
spec:
  groups:
    - name: alerting-pipeline-test
      rules:
        - alert: AlertingPipelineTest
          expr: vector(1) == 1
          labels:
            severity: info
          annotations:
            summary: "Alerting pipeline test, delete me"
```

- [ ] **Step 4: Apply it and wait for Telegram**

```bash
oc apply -f /tmp/pingtest.yaml -n aarno-playground
```
Expected: a Telegram message within roughly 2 minutes (`groupWait` is 30s on top of evaluation).

If nothing arrives, stop and diagnose before continuing. Check in order: `oc get prometheusrule -n aarno-playground` (object exists), then whether user workload monitoring evaluates rules in this namespace at all. Everything downstream of this plan assumes this step passed.

- [ ] **Step 5: Delete the test rule**

```bash
oc delete -f /tmp/pingtest.yaml -n aarno-playground
```
Expected: a resolved notification arrives shortly after.

- [ ] **Step 6: Commit**

```bash
git add deploy/alertmanagerconfig.yaml
git commit -m "deploy: route this namespace's alerts to Telegram"
```

### Task 3: Infrastructure alerts

**Files:**
- Create: `deploy/prometheusrule.yaml`

**Interfaces:**
- Consumes: the receiver from Task 2.
- Produces: a `PrometheusRule` named `controlmyspa-porssari` with group `controlmyspa-porssari-infra`. PR 3 appends a second group to this same file.

**Known risk:** these rules use `kube_*` series from kube-state-metrics, which are collected by the platform monitoring stack. Whether the user workload Prometheus that evaluates namespace rules can see them is unverified: `thanos-querier` refuses a namespace user's token, so it could not be checked by query. Step 1 turns that unknown into an alert instead of a silent hole.

- [ ] **Step 1: Write the rule file, starting with a probe for the risk above**

```yaml
# deploy/prometheusrule.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: controlmyspa-porssari
  labels:
    app: controlmyspa-porssari
spec:
  groups:
    - name: controlmyspa-porssari-infra
      rules:
        # If kube-state-metrics is not visible to the Prometheus that
        # evaluates this rule, every other rule in this group is dead code
        # that will never fire. absent() makes that fact page us once,
        # instead of hiding as silence. Delete this rule once it has stayed
        # quiet for a day.
        - alert: SpaMetricsSourceMissing
          expr: absent(kube_deployment_status_replicas_available{deployment="controlmyspa-porssari"})
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: "kube_deployment_status_replicas_available is not visible here, so the infra alerts cannot fire"

        - alert: SpaControllerDown
          expr: kube_deployment_status_replicas_available{deployment="controlmyspa-porssari"} == 0
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "The spa controller has no available replica, so the spa is not being controlled"

        - alert: SpaControllerRestartLooping
          expr: increase(kube_pod_container_status_restarts_total{container="controlmyspa-porssari"}[15m]) > 2
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "Pod {{ $labels.pod }} is restart-looping"

        - alert: SpaControllerOOMKilled
          expr: kube_pod_container_status_last_state_terminated_reason{container="controlmyspa-porssari", reason="OOMKilled"} > 0
          labels:
            severity: critical
          annotations:
            summary: "Pod {{ $labels.pod }} was OOM-killed"

        - alert: SpaControllerNotReady
          expr: kube_pod_status_ready{pod=~"controlmyspa-porssari.*", condition="false"} > 0
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "Pod {{ $labels.pod }} has not been ready for 10 minutes"
```

- [ ] **Step 2: Apply and confirm acceptance**

```bash
oc apply -f deploy/prometheusrule.yaml -n aarno-playground
oc get prometheusrule controlmyspa-porssari -n aarno-playground \
  -o jsonpath='{.spec.groups[0].rules[*].alert}'
```
Expected: the five alert names.

- [ ] **Step 3: Wait 15 minutes and check `SpaMetricsSourceMissing` stayed quiet**

Expected: no Telegram message. If it fires, `kube_*` series are not available to namespace rules. Fall back to a blackbox `Probe` against the public URL (`probe_success == 0`), modelled on `~/dev/landingpager/deploy/blackbox-exporter.yaml`, and record the finding in the spec doc.

- [ ] **Step 4: Prove `SpaControllerDown` end to end**

This pauses spa control for roughly 6 minutes. The control loop runs every 15 minutes and the spa holds its setpoint meanwhile, so the water is unaffected.

```bash
oc scale deployment/controlmyspa-porssari -n aarno-playground --replicas=0
# wait for the Telegram alert (5m `for:` plus 30s groupWait)
oc scale deployment/controlmyspa-porssari -n aarno-playground --replicas=1
oc rollout status deployment/controlmyspa-porssari -n aarno-playground --timeout=120s
```
Expected: a firing message, then a resolved message. This is the outage of 2026-08-22 reproduced deliberately, and it is the acceptance test for the whole PR.

- [ ] **Step 5: Run the local checks and commit**

```bash
uvx nox -s ruff pylint tests
git add deploy/prometheusrule.yaml
git commit -m "deploy: alert on Telegram when the controller is not running"
```

- [ ] **Step 6: Open the PR**

Body must record: that Task 1 was applied by hand and why CI could not do it, the result of the Step 4 scale-to-zero test, and whether `SpaMetricsSourceMissing` stayed quiet.

---

## PR 2: Expose what the app knows

### Task 4: The metrics module

**Files:**
- Create: `metrics.py`
- Test: `test_metrics.py`
- Modify: `pyproject.toml` (add `prometheus-client` to `dependencies`), `noxfile.py:49` (pylint list), `noxfile.py:58-62` (coverage list), `Dockerfile:21` (COPY line)

**Interfaces:**
- Produces:
  - `metrics.API_LAST_SUCCESS: Gauge` (`spa_api_last_success_timestamp_seconds`)
  - `metrics.API_FAILURES: Counter` (`spa_api_failures_total`)
  - `metrics.POOL_TEMPERATURE: Gauge` (`spa_pool_temperature_celsius`)
  - `metrics.DESIRED_TEMPERATURE: Gauge` (`spa_desired_temperature_celsius`)
  - `metrics.OUTSIDE_TEMPERATURE: Gauge` (`spa_outside_temperature_celsius`)
  - `metrics.COOLING_K: Gauge` (`spa_cooling_k`)
  - `metrics.HEATING_RATE: Gauge` (`spa_heating_rate_celsius_per_hour`)
  - `metrics.PRICE_HOURS_KNOWN: Gauge` (`spa_price_hours_known`)
  - `metrics.OVERRIDE_REMAINING: Gauge` (`spa_manual_override_seconds_remaining`)
  - `metrics.HEATING_SCHEDULED: Gauge` (`spa_heating_scheduled`)
  - `metrics.render() -> tuple[bytes, str]`

- [ ] **Step 1: Add the dependency**

```bash
uv add prometheus-client
```
This edits `pyproject.toml` and `uv.lock`. Confirm `prometheus-client` landed in `[project].dependencies`, not in the dev group.

- [ ] **Step 2: Write the failing test**

```python
# test_metrics.py
"""The exposition payload must carry every metric the alert rules query."""

import metrics


def test_render_returns_a_prometheus_payload():
    payload, content_type = metrics.render()
    assert b"spa_api_last_success_timestamp_seconds" in payload
    assert "text/plain" in content_type


def test_render_reflects_the_last_value_set():
    metrics.POOL_TEMPERATURE.set(36.5)
    payload, _ = metrics.render()
    assert b"spa_pool_temperature_celsius 36.5" in payload


def test_failure_counter_increments():
    before, _ = metrics.render()
    metrics.API_FAILURES.inc()
    after, _ = metrics.render()
    assert before != after
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest test_metrics.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'metrics'`

- [ ] **Step 4: Write the module**

```python
# metrics.py
"""Prometheus metrics for the spa controller.

One process and one replica, so the default registry is enough: no
multiprocess mode and no file-backed collectors. Every value here is state
app.py already holds; this module only names it and hands it to the scraper.
"""

import prometheus_client

API_LAST_SUCCESS = prometheus_client.Gauge(
    "spa_api_last_success_timestamp_seconds",
    "Unix time of the last successful ControlMySpa read.",
)
API_FAILURES = prometheus_client.Counter(
    "spa_api_failures",
    "ControlMySpa calls that exhausted their retries.",
)
POOL_TEMPERATURE = prometheus_client.Gauge(
    "spa_pool_temperature_celsius",
    "Pool water temperature as last reported by the spa.",
)
DESIRED_TEMPERATURE = prometheus_client.Gauge(
    "spa_desired_temperature_celsius",
    "Setpoint the spa is currently holding.",
)
OUTSIDE_TEMPERATURE = prometheus_client.Gauge(
    "spa_outside_temperature_celsius",
    "Outside air temperature used by the cooling model.",
)
COOLING_K = prometheus_client.Gauge(
    "spa_cooling_k",
    "Measured Newton cooling constant, per hour.",
)
HEATING_RATE = prometheus_client.Gauge(
    "spa_heating_rate_celsius_per_hour",
    "Measured heating rate.",
)
PRICE_HOURS_KNOWN = prometheus_client.Gauge(
    "spa_price_hours_known",
    "Future hours with a known spot price.",
)
OVERRIDE_REMAINING = prometheus_client.Gauge(
    "spa_manual_override_seconds_remaining",
    "Seconds until manual override expires, 0 when not overridden.",
)
HEATING_SCHEDULED = prometheus_client.Gauge(
    "spa_heating_scheduled",
    "1 when the current hour is booked for heating.",
)


def render() -> tuple[bytes, str]:
    """Return the exposition payload and the content type to serve it with."""
    return prometheus_client.generate_latest(), prometheus_client.CONTENT_TYPE_LATEST
```

Note the counter is declared as `spa_api_failures`: `prometheus_client` appends `_total` itself, and declaring it with the suffix produces `spa_api_failures_total_total`.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest test_metrics.py -v`
Expected: 3 passed

- [ ] **Step 6: Keep the module inside the image, the linter and coverage**

In `Dockerfile`, extend the COPY line:
```dockerfile
COPY app.py metrics.py pricing.py scheduling.py storage.py thermal.py ./
```
In `noxfile.py`, add `"metrics"` to the pylint argument list and `"--cov=metrics"` to the pytest arguments.

- [ ] **Step 7: Run everything**

Run: `uvx nox -s ruff pylint tests`
Expected: all pass, including `test_dockerfile.py`, which fails if the COPY line was missed.

- [ ] **Step 8: Commit**

```bash
git add metrics.py test_metrics.py pyproject.toml uv.lock Dockerfile noxfile.py
git commit -m "feat: add a metrics module for the spa gauges"
```

### Task 5: Fill the gauges and serve them

**Files:**
- Modify: `app.py` (import, `set_temp()` around line 565 and its `RetryError` handler around line 641, new route near the other routes)
- Test: `test_app.py`

**Interfaces:**
- Consumes: everything Task 4 produces.
- Produces: `GET /metrics` returning `200` with the exposition payload; `app._refresh_gauges() -> None`.

Derived gauges are filled at scrape time rather than at every state change, so there is one place that knows how to read the globals instead of a dozen scattered writes.

- [ ] **Step 1: Write the failing tests**

```python
# test_app.py, alongside the other route tests
def test_metrics_endpoint_serves_the_registry(self):
    """/metrics exposes the spa gauges for the ServiceMonitor."""
    response = self.client.get("/metrics")
    assert response.status_code == 200
    assert b"spa_pool_temperature_celsius" in response.data


def test_metrics_endpoint_reports_known_price_hours(self):
    """The price-hours gauge counts future hours with a price."""
    now = datetime.datetime.now(tz=datetime.UTC)
    app_module.hourly_prices = {
        (now + datetime.timedelta(hours=n))
        .replace(minute=0, second=0, microsecond=0)
        .isoformat(): 0.05
        for n in range(1, 4)
    }
    response = self.client.get("/metrics")
    assert b"spa_price_hours_known 3.0" in response.data


@patch("controlmyspa.ControlMySpa")
def test_set_temp_records_api_success_timestamp(self, mock_api_class):
    """A successful read stamps the API success gauge."""
    mock_api = mock_api_class.return_value
    mock_api.current_temp = 36.0
    mock_api.desired_temp = 37.0
    app_module.metrics.API_LAST_SUCCESS.set(0)
    app_module.set_temp(37)
    payload, _ = app_module.metrics.render()
    assert b"spa_api_last_success_timestamp_seconds 0.0" not in payload
```

Read the gauge through `render()` rather than through `_value.get()`: ruff runs with `select = ["ALL"]`, and `SLF001` rejects private member access in tests as well as in application code.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest test_app.py -v -k "metrics or api_success"`
Expected: FAIL, 404 on `/metrics` and `AttributeError` on `app_module.metrics`

- [ ] **Step 3: Import the module and stamp the gauges on a successful read**

In `app.py`, add `import metrics` to the local imports. Directly after the `store.save_reading(...)` call inside `set_temp()`:

```python
                metrics.API_LAST_SUCCESS.set(
                    datetime.datetime.now(tz=datetime.UTC).timestamp()
                )
                metrics.POOL_TEMPERATURE.set(pool["current_temp"])
                metrics.DESIRED_TEMPERATURE.set(pool["desired_temp"])
                if latest_outside_temp is not None:
                    metrics.OUTSIDE_TEMPERATURE.set(latest_outside_temp)
```

- [ ] **Step 4: Count the failures that today only reach the log**

In the `except tenacity.RetryError` handler at the end of `set_temp()`, before the existing log call:

```python
    except tenacity.RetryError as exception:
        metrics.API_FAILURES.inc()
        APP.logger.info(
```

This is the branch a gateway outage actually takes, and it is the reason the in-app check never fires in that case.

- [ ] **Step 5: Add the refresh helper and the route**

Place both next to the other routes in `app.py`:

```python
def _refresh_gauges() -> None:
    """Fill the derived gauges from current state, at scrape time."""
    now = datetime.datetime.now(tz=datetime.UTC)
    metrics.COOLING_K.set(cooling_k)
    metrics.HEATING_RATE.set(_heating_rate())
    metrics.PRICE_HOURS_KNOWN.set(
        sum(1 for key in hourly_prices if datetime.datetime.fromisoformat(key) >= now)
    )
    remaining = (manual_override_endtime - now).total_seconds()
    metrics.OVERRIDE_REMAINING.set(max(remaining, 0))
    hour_key = now.replace(minute=0, second=0, microsecond=0).isoformat()
    metrics.HEATING_SCHEDULED.set(1 if hour_key in heating_schedule else 0)


@APP.route("/metrics")
def metrics_endpoint() -> flask.Response:
    """Expose Prometheus metrics for the ServiceMonitor to scrape.

    Unauthenticated, like `/`. It exposes water temperature, price counts and
    the measured constants, none of which are secret.
    """
    _refresh_gauges()
    payload, content_type = metrics.render()
    return flask.Response(payload, mimetype=content_type)
```

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest test_app.py -v -k "metrics or api_success"`
Expected: 3 passed

- [ ] **Step 7: Run everything and commit**

```bash
uvx nox -s ruff pylint tests
git add app.py test_app.py
git commit -m "feat: serve the spa state on /metrics"
```

### Task 6: Let Prometheus find the endpoint

**Files:**
- Modify: `deploy/service.yaml`
- Create: `deploy/servicemonitor.yaml`

**Interfaces:**
- Consumes: `/metrics` from Task 5.
- Produces: a scrape target. PR 3's rules query the series it collects.

- [ ] **Step 1: Name the service port**

A `ServiceMonitor` endpoint references a port by name, so the port needs one:

```yaml
# deploy/service.yaml
spec:
  ports:
    - name: web
      port: 8080
      protocol: TCP
      targetPort: 8080
```

- [ ] **Step 2: Write the ServiceMonitor**

```yaml
# deploy/servicemonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: controlmyspa-porssari
  labels:
    app: controlmyspa-porssari
spec:
  selector:
    matchLabels:
      app: controlmyspa-porssari
  endpoints:
    - port: web
      interval: 60s
      path: /metrics
      scheme: http
```

`interval: 60s` rather than Landingpager's 30s: the underlying readings only change every 15 minutes.

- [ ] **Step 3: Run the checks and commit**

```bash
uvx nox -s ruff pylint tests
git add deploy/service.yaml deploy/servicemonitor.yaml
git commit -m "deploy: scrape the spa metrics endpoint"
```

- [ ] **Step 4: After the deploy lands, verify the endpoint really serves**

```bash
oc rollout status deployment/controlmyspa-porssari -n aarno-playground --timeout=120s
oc port-forward -n aarno-playground deployment/controlmyspa-porssari 18080:8080 &
sleep 3
curl -s localhost:18080/metrics | grep -E '^spa_'
kill %1
```
Expected: every `spa_*` metric present. `spa_api_last_success_timestamp_seconds` stays 0 until the first control loop runs, which is normal right after a restart.

- [ ] **Step 5: Confirm the target is actually being scraped**

```bash
oc get servicemonitor controlmyspa-porssari -n aarno-playground
```
Then wait for one scrape interval and confirm through PR 3's first rule rather than by query: `thanos-querier` refuses namespace-user tokens, so an ad-hoc query is not available here. Treat "the ServiceMonitor exists" as necessary but not sufficient until Task 7's rules prove data is arriving.

---

## PR 3: Alert on the spa, and delete the heuristic

### Task 7: The spa alert rules

**Files:**
- Modify: `deploy/prometheusrule.yaml` (append a second group)

**Interfaces:**
- Consumes: the series exposed in Task 5 and scraped in Task 6.

- [ ] **Step 1: Append the group**

```yaml
    - name: controlmyspa-porssari-spa
      rules:
        # Same reasoning as SpaMetricsSourceMissing: if the ServiceMonitor is
        # not working, silence would look identical to health.
        - alert: SpaMetricsNotScraped
          expr: absent(spa_api_last_success_timestamp_seconds)
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "The spa metrics endpoint is not being scraped, so no spa alert can fire"

        # A fact, not an inference: the control loop runs every 15 minutes, so
        # an hour without a successful read means the API or the gateway is gone.
        - alert: SpaApiUnreachable
          expr: time() - spa_api_last_success_timestamp_seconds > 3600
          labels:
            severity: critical
          annotations:
            summary: "No successful ControlMySpa read for over an hour"

        # The API can answer while the gateway behind it is dead, returning the
        # same value forever. 12h because a pool holding at setpoint legitimately
        # sits in one 0.5 C bucket for hours.
        - alert: SpaReadingFrozen
          expr: changes(spa_pool_temperature_celsius[12h]) == 0
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "Pool temperature has not moved in 12 hours, the gateway may be stuck"

        - alert: SpaTooCold
          expr: spa_pool_temperature_celsius < 33
          for: 1h
          labels:
            severity: warning
          annotations:
            summary: "Pool is below TEMP_MIN and has not recovered in an hour"

        - alert: SpaOverrideLeftOn
          expr: min_over_time(spa_manual_override_seconds_remaining[24h]) > 0
          labels:
            severity: info
          annotations:
            summary: "Manual override has been continuously active for 24 hours"

        - alert: SpaPricesStale
          expr: spa_price_hours_known < 4
          for: 3h
          labels:
            severity: warning
          annotations:
            summary: "Fewer than 4 future hours have a known price, spot-hinta may be failing"
```

`SpaTooCold` uses 33 rather than `TEMP_MIN` (34) so a single sensor tick below the target does not page. `SpaPricesStale` uses 3h because the horizon is legitimately short just before the 14:00 publication.

- [ ] **Step 2: Apply and confirm `SpaMetricsNotScraped` stays quiet for 30 minutes**

```bash
oc apply -f deploy/prometheusrule.yaml -n aarno-playground
```
Expected: no Telegram message. If it fires, the ServiceMonitor from Task 6 is not working and Task 8 must not proceed: deleting the in-app check while nothing has replaced it would leave the spa unmonitored.

- [ ] **Step 3: Record how far `SpaApiUnreachable` was proven**

Forcing a real hour-long API outage is not worth doing deliberately. Step 2 proves the series arrives and the rule parses, so record in the PR that this rule is proven by construction rather than by observation. If a real gateway outage happens before the PR merges, note the observed behaviour instead.

- [ ] **Step 4: Commit**

```bash
git add deploy/prometheusrule.yaml
git commit -m "deploy: alert on the spa going quiet or freezing"
```

### Task 8: Delete the in-app heuristic

**Files:**
- Modify: `app.py` (remove `check_stale_temperature()` at lines 145-211, the constants at lines 82-88, and the call site at line 640)
- Modify: `thermal.py` (remove `mode_stretch()`, `expected_gain()`, `expected_drop()`)
- Modify: `test_app.py` (remove the stale-alert tests and the reset lines at 27-30)

**Precondition:** Task 7 Step 2 passed. Do not start otherwise.

- [ ] **Step 1: Confirm nothing else calls the helpers**

```bash
grep -rn 'mode_stretch\|expected_gain\|expected_drop\|check_stale_temperature\|STALE_\|STUCK_FRACTION\|MIN_STALE_READINGS' --include='*.py' .
```
Expected: hits only in `app.py`, `thermal.py` and `test_app.py`. Anything else means this task needs rescoping.

- [ ] **Step 2: Delete the tests first, and watch the suite stay green**

Remove from `test_app.py`: `test_calls_check_stale_temperature`, `test_stale_alert_heating_mode`, `test_stale_alert_general_mode`, `test_stale_alert_suppressed_within_window`, `test_stale_alert_repeats_after_window`, `test_stale_alert_heating_repeats_after_3h`, and every other test whose body calls `app_module.check_stale_temperature()`, plus the `last_stale_alert_time` and `STALE_ALERT_ACTIVE` resets in the autouse fixture at lines 27-30.

Run: `uv run pytest test_app.py -v`
Expected: PASS. The suite must be green with the tests gone but the code still present, which proves the deletions that follow are not load-bearing for anything else.

- [ ] **Step 3: Delete the code**

From `app.py`: the whole `check_stale_temperature()` function, the `last_stale_alert_time`, `STALE_ALERT_ACTIVE`, `STALE_HEATING_MINUTES`, `STALE_IDLE_MINUTES`, `STUCK_FRACTION` and `MIN_STALE_READINGS` globals, and the `check_stale_temperature()` call at the end of `set_temp()`.

From `thermal.py`: `mode_stretch()`, `expected_gain()` and `expected_drop()`.

- [ ] **Step 4: Run everything**

Run: `uvx nox -s ruff pylint tests`
Expected: all pass. ruff will flag any import left unused by the deletions.

- [ ] **Step 5: Update the docs**

In `CLAUDE.md`, replace the "Stale Temperature Alerts" section with a pointer to the Prometheus rules and `metrics.py`. In `docs/plans/2026-08-22-prometheus-telegram-alerting.md`, change the status line from "not started" to done, with the date.

- [ ] **Step 6: Commit**

```bash
git add app.py thermal.py test_app.py CLAUDE.md docs/plans/2026-08-22-prometheus-telegram-alerting.md
git commit -m "refactor: retire the in-app stale-temperature heuristic"
```

---

## Verification summary

Each PR has one acceptance test that is not a unit test:

| PR | Proof |
|---|---|
| 1 | Scaling the deployment to zero produces a Telegram message, and scaling back produces a resolved one. |
| 2 | `curl localhost:18080/metrics` through a port-forward lists every `spa_*` metric. |
| 3 | `SpaMetricsNotScraped` stays quiet for 30 minutes, proving the series arrive. |

Anything that could not be run gets reported as unverified, never as passing.
