<p align="center">
  <!-- Plain <img> with an absolute URL: HACS's markdown renderer doesn't
       support <picture>/<source>, and relative paths only resolve on GitHub. -->
  <img alt="Copper Labs" src="https://raw.githubusercontent.com/sNNNeaker/HA-Copper/main/custom_components/copper_labs/brand/logo%402x.png" width="420">
</p>

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

### Why the 15-minute interval is fixed (and not configurable)
The poll timer is an API limitation, not a preference. Copper's server only
recomputes readings every 15 minutes and serves cached responses in between
(`Cache-Control: max-age=900`) — polling more often just downloads the **same
cached data again** while putting extra, conspicuous load on an unofficial
API. There is deliberately no option to shorten it, because no smaller value
can produce fresher data.

If you want a *slower* or custom schedule instead: disable polling in the
integration's **System options** (⋮ menu on the integration page) and trigger
updates yourself with an automation calling `homeassistant.update_entity` on
any Copper entity — one call refreshes all meters.

## Install

### Via HACS (recommended)
1. In HACS, open the ⋮ menu → **Custom repositories**, add
   `https://github.com/sNNNeaker/HA-Copper` with type **Integration**.
2. Search for **Copper Labs** in HACS and download it.
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Copper Labs**.

HACS also notifies you about updates.

### Manual (alternative)
1. Copy `custom_components/copper_labs/` from this repo to
   `config/custom_components/copper_labs/` in your Home Assistant install.
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
Settings → Energy → add a Gas or Water source and pick the meter's **Total**
entity (e.g. `sensor.copper_gas_meter_total`).

- Only **Total** entities go in as consumption sources — adding a **Rate**
  entity there causes an "unexpected device class" warning.
- The gas **Rate** entity can optionally be set as the **Gas flow rate**
  statistic in the gas source's settings.
- Rate entities report m³/h (a standard flow-rate unit); to display gal/min,
  L/min etc., change the unit in the entity's settings (gear icon) — HA
  converts natively.
- Newly added entities show "statistics not defined" for a short while: the
  first poll can take up to 15 min and statistics compile a few minutes later.

## Endpoints used (reverse-engineered)
- `GET /api/v2/app/state` — premises, meters, gateways.
- `GET /api/v2/app/usage/{meter_id}?start=&end=&granularity=auto&include_start=true`
  — `results[].value` cumulative reading, `usage` interval, `power` per-hour rate.
- Auth: Auth0 passwordless — `POST /passwordless/start` (emails a code) then
  `POST /oauth/token` (OTP grant) → refresh token; `refresh_token` grant renews.

## Architecture (how the files fit together)

| File | Role |
| --- | --- |
| `manifest.json` | Integration metadata: domain, `config_flow: true`, `iot_class: cloud_polling`, version. No `requirements` — `requests` ships with HA core. |
| `const.py` | Constants (domain, config keys, 15-min interval) plus the unit tables and `convert_volume()`. Pure Python, no HA imports, so it's importable by scripts/tests. |
| `api.py` | Synchronous `CopperClient` — Auth0 refresh, `/state`, `/usage/{meter}`. The only code that talks HTTP to Copper. |
| `coordinator.py` | `CopperCoordinator` (a `DataUpdateCoordinator`): the single timer-driven poller. Fetches every meter, converts units, exposes one shared `data` dict, and persists a rotated refresh token. |
| `config_flow.py` | The setup UI (paste refresh token → validate → create entry) and the options UI (per-meter unit dropdowns). |
| `__init__.py` | Entry lifecycle: `async_setup_entry` builds the client + coordinator and forwards to the sensor platform; `async_unload_entry` tears it down; an options listener reloads on unit changes. |
| `sensor.py` | The entities: a `total_increasing` register sensor + a `measurement` rate sensor per meter (grouped as one device per meter), both reading from the coordinator. |
| `diagnostics.py` | "Download diagnostics" support: a redacted snapshot (token/premise redacted, meter serials aliased) for bug reports. |
| `translations/` | UI strings for the config/options forms and error messages (`en.json`, plus a German `de.json`). |
| `brand/` | Integration icon + logo (light and dark variants). Served locally by Home Assistant's Brands Proxy API (2026.3+), which takes priority over the brands CDN — so no submission to the `home-assistant/brands` repo is needed. |

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

## Logging
Everything logs into Home Assistant's standard log (Settings → System → Logs).
Two ways to get debug detail (API calls, token refreshes, per-meter readings):

- **Configure → Debug logging** — a persistent toggle in the integration's
  options; survives restarts (handy for watching a cloud issue over days).
- The usual YAML route:
  ```yaml
  logger:
    logs:
      custom_components.copper_labs: debug
  ```

Debug logs never contain tokens or sign-in codes, but they do include meter
serials — scrub those before attaching logs to a public issue.

## Development
- Requires Home Assistant **2024.4+** (uses `entry.runtime_data`).
- `tests/` holds pure unit tests for the HA-free modules (`const.py`, `api.py`);
  they run with just `pip install pytest requests` — no HA install needed —
  and run in CI alongside hassfest and HACS validation.

## Caveats
- Unofficial API — endpoints or auth could change without notice.
- Built with AI assistance — this integration was largely written with the help
  of AI tooling. Review the code yourself before trusting it, and expect the odd
  rough edge.
