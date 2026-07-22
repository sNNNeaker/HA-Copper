# Copper Labs — Home Assistant custom component

Reads electric/gas/water meter data from the Copper Labs consumer API
(`api.copperlabs.com`) and exposes it to Home Assistant, including the
Energy dashboard.

## What it does
- Discovers the meters on your premise from `/state`.
- Polls the `usage/{meter}` endpoint every 15 min (the API caches for
  `max-age=900`, so faster polling only returns cached data).
- Per meter, creates:
  - **`… total`** — the cumulative meter reading (`total_increasing`,
    device class gas/water). The Energy dashboard consumes this directly and
    Home Assistant builds the long-term statistics itself.
  - **`… rate`** — the latest consumption as a per-hour rate (measurement).

This uses Copper's own cumulative register (`value`), so there is no
statistics reconstruction and nothing tied to the recorder's internal API.

## Install
1. Copy `custom_components/copper_labs/` from this repo to
   `config/custom_components/copper_labs/` in your Home Assistant install
   (or add the repo to HACS as a custom repository).
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Copper Labs**.

## Signing in
When you add the integration you'll be asked how to sign in:

1. **Sign in with email code** (normal path) — enter your Copper account email;
   Copper emails a 6-digit code; enter it. Home Assistant exchanges it for tokens
   via Auth0's passwordless grant, stores the refresh token, and renews the
   short-lived (~24 h) access token itself from then on.
2. **Advanced: paste a refresh token** (fallback) — if the email path isn't
   available on your account, capture a refresh token with a proxy: watch for
   `POST https://auth.copperlabs.com/oauth/token` while the Copper app signs in
   and copy `refresh_token` from the JSON response.

Either way, only the refresh token is stored (never your email or code), and a
rotated token is persisted across restarts.

## Units
The API never reports units. On the sample account `value` reads as **CCF**
for gas and **gallons** for water. Confirm against the Copper app or a utility
bill, and adjust under the integration's **Configure** (Options): gas
(CCF / m³ / ft³), each water meter (gal / L / m³ / ft³ / CCF). HA's gas device
class has no therm unit — convert to CCF/m³ if your utility bills in therms.

## Energy dashboard
Settings → Energy → add a Gas or Water source and pick the
`sensor.copper_gas_total` (or water) entity.

## Endpoints used (reverse-engineered)
- `GET /api/v2/app/state` — premises, meters, gateways.
- `GET /api/v2/app/usage/{meter_id}?start=&end=&granularity=auto&include_start=true`
  — `results[].value` cumulative reading, `usage` interval, `power` per-hour rate.
- Auth: Auth0 passwordless — `POST /passwordless/start` (emails a code) then
  `POST /oauth/token` (OTP grant) → refresh token; `refresh_token` grant renews.

## Architecture (how the files fit together)

| File | Role |
| --- | --- |
| `manifest.json` | Integration metadata: domain, `config_flow: true`, `requirements` (`requests`), `iot_class: cloud_polling`, version. |
| `const.py` | Constants (domain, config keys, 15-min interval) plus the unit tables and `convert_volume()`. Pure Python, no HA imports, so it's importable by scripts/tests. |
| `api.py` | Synchronous `CopperClient` — Auth0 refresh, `/state`, `/usage/{meter}`. The only code that talks HTTP to Copper. |
| `coordinator.py` | `CopperCoordinator` (a `DataUpdateCoordinator`): the single timer-driven poller. Fetches every meter, converts units, exposes one shared `data` dict, and persists a rotated refresh token. |
| `config_flow.py` | The setup UI (paste refresh token → validate → create entry) and the options UI (per-meter unit dropdowns). |
| `__init__.py` | Entry lifecycle: `async_setup_entry` builds the client + coordinator and forwards to the sensor platform; `async_unload_entry` tears it down; an options listener reloads on unit changes. |
| `sensor.py` | The entities: a `total_increasing` register sensor + a `measurement` rate sensor per meter, both reading from the coordinator. |
| `translations/en.json` | UI strings for the config/options forms and error messages. |

**Data flow.** On setup, `__init__` creates a `CopperClient` from the stored
refresh token, calls `refresh()` + `get_state()` (in the executor, since the
client is sync), and hands the premise to a `CopperCoordinator`. The coordinator
then runs every 15 minutes: for each meter it calls `client.usage(...)`, takes
the newest real reading (`_last_reading`), converts it from the meter's native
unit to the chosen display unit (`convert_volume`), and stores it under the
meter id. Every sensor entity is a `CoordinatorEntity`, so it just reads its
meter's slice of that shared dict — no entity ever calls the API itself. If a
refresh rotates the token, the coordinator writes the new one back to the config
entry so restarts keep working.

> `copper_client.py` (in the parent folder, not inside `copper_labs/`) is the
> standalone version of `api.py` for running/testing outside Home Assistant. The
> integration uses `api.py`; the two share the same logic.

## Status / caveats
- Unofficial API — endpoints or auth could change without notice.
