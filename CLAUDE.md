# CLAUDE.md

Guidance for AI assistants working in this repo. Keep it short; the README has
the full architecture. Update this file when the facts below change.

## What this is
Unofficial Home Assistant custom integration (`copper_labs`) that reads
gas/water/electric meter data from Copper Labs' **private, reverse-engineered**
consumer API and feeds the HA Energy dashboard. US/North America hardware only.

## Non-negotiables
- **Never invent API endpoints or fields.** The API is undocumented and captured
  from the app; only use endpoints/shapes already present in `api.py` /
  `copper_client.py`. If something's unknown, say so — don't guess.
- **Never commit secrets or PII.** No tokens/JWTs/PKCE values, `.har` captures,
  real emails, addresses, `premise_id`s, lat/lng, or real meter serials. Use
  placeholders (`12:0000000000`, `IQ:00000000`, `you@example.com`). The public
  OAuth `client_id` and endpoint paths are OK to keep.
- **`manifest.json` `requirements` is deliberately empty** — `requests` ships
  with HA core, and an exact pin here can conflict with (even downgrade) core's
  copy. If a new requirement is ever added, it must be pinned (`pkg==x.y.z`) or
  hassfest CI fails.
- Keep `manifest.json` `domain` == `const.py` `DOMAIN` == folder name
  (`copper_labs`).

## Layout
- `custom_components/copper_labs/` — the integration (required path for HACS).
- `custom_components/copper_labs/translations/` — `en.json`, `de.json`. Translate
  values only; never change keys.
- `custom_components/copper_labs/brand/` — icon + logo, light and dark variants
  (`icon.png`/`icon@2x.png`, `logo.png`/`logo@2x.png`, and `dark_*` versions).
  HA's Brands Proxy API (2026.3+) serves these locally and they take priority
  over the brands CDN. **Do not submit these to the `home-assistant/brands`
  repo — it no longer accepts custom integrations.** Sizes: icon 256/512 square;
  logo shortest side 128 (`logo.png`) / 256 (`logo@2x.png`).
- `copper_client.py` (repo root) — standalone version of `api.py` for testing
  outside HA. Not part of the shipped component.
- `tests/` — pure unit tests for the HA-free modules only (`const.py`,
  `api.py`), imported as top-level modules via `tests/conftest.py` so the
  package `__init__.py` (which needs HA) never runs. Keep new tests HA-free or
  they'll break CI, which installs only pytest + requests.
- `hacs.json`, `LICENSE`, `.github/` — packaging/CI, not runtime code.

## Data model (don't break)
- Per meter: a `total_increasing` register sensor (from the cumulative `value`
  field — drives the Energy dashboard) + a `measurement` rate sensor (`power`),
  grouped as one HA device per meter. Electric rate is kW (power device class).
- Poll every 15 min; the API caches `max-age=900`, so faster is pointless.
- The API never reports units. Native units: gas=CCF, water=US gallons.
  `convert_volume()` in `const.py` converts to the user's chosen display unit.
- Auth0 **rotates the refresh token**; the client's `token_callback` persists it
  immediately (wired in `__init__.py`). The entry-update listener only reloads
  when units changed — reloading on token persists would loop forever.
- Auth failures raise `ConfigEntryAuthFailed` → HA's reauth flow
  (`async_step_reauth` in `config_flow.py`). `diagnostics.py` provides redacted
  downloads (token/premise redacted, meter serials aliased).
- Logging: standard `logging.getLogger(__name__)` everywhere. A persistent
  `debug_logging` option (options flow) sets the package logger to DEBUG /
  NOTSET via `_apply_log_level()` in `__init__.py` — applied on setup and on
  options change without a reload. **Never log token values, the emailed code,
  or PKCE secrets** — endpoints, status codes and events only.

## Code style
- Comment code where it makes sense: docstrings on functions/classes, and
  inline comments on non-obvious lines (API quirks, unit handling, HA-specific
  behavior). Don't comment the self-explanatory.

## Releases (versioning)
- HACS shows versions from **GitHub releases**; without one it falls back to
  commit SHAs. HA's integration page shows `manifest.json`'s `version`.
- To release: bump `version` in `manifest.json`, commit, then
  `gh release create vX.Y.Z` with the **same number** (tag `v`-prefixed).
  Never let the tag and manifest diverge.

## Before pushing
- CI runs hassfest + HACS validate + pytest (tests/) on push. Keep all green.
- JSON files must parse; translation keys must match across en/de.
- The coordinator lives in `entry.runtime_data` (HA 2024.4+, min version set in
  hacs.json) — don't reintroduce `hass.data[DOMAIN]`.

## Unverified (needs a real HA instance + Copper account)
- End-to-end Auth0 email-code login (OTP grant may be disabled; there's an
  untested browser-flow fallback in `api.py`).
- Recorder / Energy-dashboard behavior of the `total_increasing` sensors.
