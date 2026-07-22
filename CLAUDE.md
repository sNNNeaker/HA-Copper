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
- `hacs.json`, `LICENSE`, `.github/` — packaging/CI, not runtime code.

## Data model (don't break)
- Per meter: a `total_increasing` register sensor (from the cumulative `value`
  field — drives the Energy dashboard) + a `measurement` rate sensor (`power`).
- Poll every 15 min; the API caches `max-age=900`, so faster is pointless.
- The API never reports units. Native units: gas=CCF, water=US gallons.
  `convert_volume()` in `const.py` converts to the user's chosen display unit.

## Code style
- Comment code where it makes sense: docstrings on functions/classes, and
  inline comments on non-obvious lines (API quirks, unit handling, HA-specific
  behavior). Don't comment the self-explanatory.

## Before pushing
- CI runs hassfest + HACS validate on push. Keep both green.
- `requests` stays pinned; JSON files must parse; translation keys must match.

## Unverified (needs a real HA instance + Copper account)
- End-to-end Auth0 email-code login (OTP grant may be disabled; there's an
  untested browser-flow fallback in `api.py`).
- Recorder / Energy-dashboard behavior of the `total_increasing` sensors.
