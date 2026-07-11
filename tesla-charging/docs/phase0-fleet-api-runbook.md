# Phase 0 — Tesla Fleet API onboarding runbook

One-time manual setup to get your own Fleet API application, signing keys, user
tokens, and the virtual key paired to the car. Everything here is free within
the $10/month developer discount (see [costs.md](costs.md)).

Throughout, replace `tesla.example.com` with the real app domain (a dedicated
subdomain served by the OpenShift ingress, e.g. `tesla.aukia.com`).

## 1. Pick the app domain

One subdomain serves both the public key (unauthenticated, required by Tesla)
and the evcc UI. Create the DNS record pointing at the APPUiO ingress before
step 5 — Tesla fetches the public key during partner registration.

## 2. Developer account and application

1. Tesla account with verified email + MFA.
2. On <https://developer.tesla.com> → *Request app access*:
   - App name: e.g. `aukia-charging`; purpose: personal smart charging.
   - **Allowed origin**: `https://tesla.example.com`
   - **Redirect URI**: `https://tesla.example.com/callback`
     (nothing needs to listen there — you copy the `code` from the browser URL)
   - **Scopes**: `openid`, `offline_access`, `vehicle_device_data`,
     `vehicle_cmds`, `vehicle_charging_cmds`, `vehicle_location`
     (`vehicle_location` is required for evcc's geofencing).
3. Note the **Client ID** and **Client Secret**.

## 3. Generate the command-signing key pair

```sh
openssl ecparam -genkey -name prime256v1 -noout -out fleet-private-key.pem
openssl ec -in fleet-private-key.pem -pubout -out com.tesla.3p.public-key.pem
```

- `fleet-private-key.pem` → goes only into the sops-encrypted
  `deploy/proxy-secret.yaml`. Never commit it unencrypted.
- `com.tesla.3p.public-key.pem` → public, goes into
  `deploy/wellknown-configmap.yaml`.

## 4. Host the public key

Tesla requires the public key at exactly:

```
https://tesla.example.com/.well-known/appspecific/com.tesla.3p.public-key.pem
```

Paste the PEM into `deploy/wellknown-configmap.yaml` and deploy the wellknown
Deployment/Service plus the Ingress. Verify from outside the cluster:

```sh
curl https://tesla.example.com/.well-known/appspecific/com.tesla.3p.public-key.pem
```

## 5. Partner registration (EU region)

```sh
CID=<client id>; CSECRET=<client secret>

# 5a. partner token (client-credentials grant)
PARTNER_TOKEN=$(curl -s -X POST https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token \
  -d grant_type=client_credentials -d client_id=$CID -d client_secret=$CSECRET \
  -d scope="openid vehicle_device_data vehicle_cmds vehicle_charging_cmds vehicle_location" \
  -d audience=https://fleet-api.prd.eu.vn.cloud.tesla.com | jq -r .access_token)

# 5b. register the domain (Tesla validates the well-known public key now)
curl -X POST https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/partner_accounts \
  -H "Authorization: Bearer $PARTNER_TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"tesla.example.com"}'

# 5c. confirm Tesla sees the key
curl https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/partner_accounts/public_key?domain=tesla.example.com \
  -H "Authorization: Bearer $PARTNER_TOKEN"
```

> Note: token host `fleet-auth.prd.vn.cloud.tesla.com` and user-authorization
> host `auth.tesla.com` are current as of writing — cross-check against the
> developer portal docs if a call 404s.

## 6. User tokens (authorization-code flow)

Open in a browser (one line):

```
https://auth.tesla.com/oauth2/v3/authorize?response_type=code&client_id=<CID>&redirect_uri=https://tesla.example.com/callback&scope=openid%20offline_access%20vehicle_device_data%20vehicle_cmds%20vehicle_charging_cmds%20vehicle_location&state=x
```

Log in, approve. The browser lands on
`https://tesla.example.com/callback?code=XXX&state=x` (page may 404 — fine).
Copy `code=XXX` and exchange it:

```sh
curl -s -X POST https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token \
  -d grant_type=authorization_code -d client_id=$CID -d client_secret=$CSECRET \
  -d code=XXX -d redirect_uri=https://tesla.example.com/callback \
  -d audience=https://fleet-api.prd.eu.vn.cloud.tesla.com | jq .
```

Save `access_token` (~8 h lifetime) and `refresh_token` → both go into
`deploy/evcc-secret.yaml`. evcc refreshes and persists rotated tokens on its
PVC afterwards. (The token-generator page at myteslamate.com is an acceptable
convenience for this step — it drives *your* client_id; no subscription.)

## 7. Virtual key pairing

On the phone that has the Tesla app (and is a key to the car), open:

```
https://tesla.com/_ak/tesla.example.com
```

and approve adding the key. Requires reasonably current vehicle firmware.
(Pre-2021 Model S/X don't support virtual keys but also don't require signed
commands — tesla-http-proxy falls back to plain REST for those.)

## 8. Billing sanity

- Developer dashboard → Billing: the default limit is $0. Verify usage within
  the $10 monthly discount works without adding a payment method; if not, add
  one and set a low limit (e.g. $12).
- After the first full day, check the usage page: expect ~100 data requests,
  a handful of commands, 1–2 wakes.
- Do not lower evcc's vehicle `cache` below 15 m — polling cost scales
  linearly (see [costs.md](costs.md)).
