"""Diagnostics: a redacted snapshot users can attach to bug reports.

Home Assistant surfaces this via the entry's "Download diagnostics" menu.
Everything identifying (tokens, premise, meter serials) is redacted or
anonymised — see the issue template's warning about scrubbing logs.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PREMISE_ID, CONF_REFRESH_TOKEN

# Entry-data keys whose values must never leave the user's machine.
TO_REDACT = {CONF_REFRESH_TOKEN, CONF_PREMISE_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return a redacted diagnostics payload for this config entry."""
    coordinator = entry.runtime_data
    # Meter ids are physical serial numbers -> replace them with stable,
    # anonymous aliases (gas_0, water_indoor_1, ...) that still let a report
    # correlate readings to meters.
    alias = {m["id"]: f"{m['type']}_{i}" for i, m in enumerate(coordinator.meters)}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "units": coordinator.units,
        "meters": [
            {"alias": alias[m["id"]], "type": m["type"], "state": m.get("state")}
            for m in coordinator.meters
        ],
        "last_update_success": coordinator.last_update_success,
        # The latest readings, re-keyed by alias so no serials leak.
        "data": {
            alias.get(mid, "unknown"): reading
            for mid, reading in (coordinator.data or {}).items()
        },
    }
