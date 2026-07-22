"""DataUpdateCoordinator: the single place that talks to the API on a timer.

One coordinator per config entry fetches every meter once per interval, and all
of that entry's sensor entities read from its shared `data` dict. This is the
standard HA pattern: it avoids each entity polling independently and gives free
availability handling + staggered refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CopperAuthError, CopperClient
from .const import (
    DOMAIN,
    SCAN_INTERVAL_MINUTES,
    SOURCE_UNITS,
    convert_volume,
)

# Module-level logger; DataUpdateCoordinator wants one passed in.
_LOGGER = logging.getLogger(__name__)


def _last_reading(rows: list[dict]) -> dict:
    """Pick the newest row that actually has a register reading.

    `rows` are chronological and the trailing buckets (the near-future edge of
    the requested window) come back with value=null. We walk from the end and
    take the first row that has a real `value`, so the reading, its rate and its
    timestamp all describe the same moment rather than a mix.
    """
    row = next((r for r in reversed(rows) if r.get("value") is not None), None)
    if not row:
        # No data at all in the window -> everything None -> entities go unavailable.
        return {"value": None, "power": None, "time": None}
    return {
        "value": row["value"],       # cumulative meter reading
        "power": row.get("power"),   # per-hour rate at that moment
        "time": row.get("time"),     # timestamp of the reading
    }


class CopperCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, client: CopperClient, premise: dict, units: dict):
        # Register with HA's coordinator machinery: name for logs, and the poll
        # interval (15 min to match the API's cache).
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=SCAN_INTERVAL_MINUTES),
        )
        self.client = client        # the (sync) API client
        self.premise = premise      # this entry's premise object from /state
        self.units = units          # {meter_type: chosen display unit}
        self.meters = premise.get("meter_list", [])  # the meters we build sensors for

    async def _async_update_data(self):
        """Called by HA every interval; returns the dict all entities read from."""
        now = datetime.now(timezone.utc)
        try:
            data = {}
            for meter in self.meters:
                # The client is synchronous, so run it in the executor to avoid
                # blocking the event loop. Ask for the last 24h so there's always
                # a recent real reading even if the newest buckets are still null.
                res = await self.hass.async_add_executor_job(
                    self.client.usage, meter["id"], now - timedelta(hours=24), now
                )
                reading = _last_reading(res.get("results", []))
                # Convert from the meter's native unit into the user's chosen unit
                # (no-op when they match, which is the default).
                src = SOURCE_UNITS.get(meter["type"])
                dst = self.units.get(meter["type"])
                reading["value"] = convert_volume(reading["value"], src, dst)
                reading["power"] = convert_volume(reading["power"], src, dst)
                # Key by meter id so each entity can look up its own reading.
                data[meter["id"]] = reading
        except CopperAuthError as err:
            # The refresh token was rejected/revoked. Raising ConfigEntryAuthFailed
            # makes HA start the reauth flow (a "sign in again" repair) instead of
            # pointlessly retrying a dead token every 15 minutes.
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            # Any other failure (network etc.) -> UpdateFailed so HA marks the
            # entities unavailable and retries next interval.
            raise UpdateFailed(str(err)) from err

        # Rotated refresh tokens are persisted immediately by the client's
        # token_callback (wired up in __init__.py) — nothing to do here.
        return data
