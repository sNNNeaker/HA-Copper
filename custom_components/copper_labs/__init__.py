"""Integration entry points: set up and tear down a Copper Labs config entry.

Home Assistant calls async_setup_entry() when the integration is configured (or
on every restart) and async_unload_entry() when it's removed/reloaded.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import CopperClient
from .const import CONF_PREMISE_ID, CONF_REFRESH_TOKEN, CONF_UNITS, DEFAULT_UNITS, DOMAIN
from .coordinator import CopperCoordinator

# Which entity platforms this integration provides. Only sensors here.
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one configured account (config entry). Returns True on success."""
    # Build the client from the stored refresh token (the only persisted secret).
    client = CopperClient(refresh_token=entry.data[CONF_REFRESH_TOKEN])
    # Client is sync -> run its network calls in the executor. Refresh first to
    # obtain a valid access token, then fetch /state for premise + meter discovery.
    await hass.async_add_executor_job(client.refresh)
    state = await hass.async_add_executor_job(client.get_state)

    # Find the premise this entry represents; fall back to the first one if the
    # stored id isn't found (e.g. account changed), or {} if none exist.
    premises = state.get("premise_list", [])
    premise = next(
        (p for p in premises if p["id"] == entry.data.get(CONF_PREMISE_ID)),
        premises[0] if premises else {},
    )

    # Merge saved unit choices over the defaults so any meter the user didn't
    # explicitly set still has a sensible unit.
    units = {**DEFAULT_UNITS, **entry.options.get(CONF_UNITS, {})}

    # Create the coordinator and do the first fetch synchronously: if it fails,
    # setup fails cleanly (HA will retry) instead of adding broken entities.
    coordinator = CopperCoordinator(hass, entry, client, premise, units)
    await coordinator.async_config_entry_first_refresh()

    # Stash the coordinator so the sensor platform (and unload) can find it,
    # keyed by entry id to support multiple accounts.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # Create the sensor entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # When the user changes options (units), reload the entry so entities pick up
    # the new units. async_on_unload ensures the listener is removed on unload.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed -> reload the entry to apply them."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the entry: remove its platforms and drop the coordinator."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Only drop our stored data if the platforms unloaded cleanly.
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
