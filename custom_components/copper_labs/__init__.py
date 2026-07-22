"""Integration entry points: set up and tear down a Copper Labs config entry.

Home Assistant calls async_setup_entry() when the integration is configured (or
on every restart) and async_unload_entry() when it's removed/reloaded.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import CopperAuthError, CopperClient
from .const import CONF_DEBUG, CONF_PREMISE_ID, CONF_REFRESH_TOKEN, CONF_UNITS, DEFAULT_UNITS
from .coordinator import CopperCoordinator

# Which entity platforms this integration provides. Only sensors here.
PLATFORMS = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)
# The package logger ("custom_components.copper_labs") — setting its level
# covers every module here (api, coordinator, config_flow, ...).
_PKG_LOGGER = logging.getLogger(__package__)


def _apply_log_level(entry: ConfigEntry) -> None:
    """Honour the persistent debug-logging option.

    DEBUG when the option is on; NOTSET (inherit HA's configured level) when
    off, so a user's `logger:` YAML setup still works as expected.
    """
    _PKG_LOGGER.setLevel(
        logging.DEBUG if entry.options.get(CONF_DEBUG, False) else logging.NOTSET
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one configured account (config entry). Returns True on success."""
    # Apply the debug-logging option first so even the login/refresh below logs.
    _apply_log_level(entry)
    # Build the client from the stored refresh token (the only persisted secret).
    client = CopperClient(refresh_token=entry.data[CONF_REFRESH_TOKEN])

    # Persist rotated refresh tokens the moment the client sees them. Auth0's
    # reuse detection can revoke the whole token family if a stale token is
    # replayed, so waiting until the end of an update cycle risks losing the
    # only valid token (e.g. a failure between rotation and persist). The client
    # calls this from an executor thread -> hop to the event loop to touch HA.
    def _persist_refresh_token(new_token: str) -> None:
        def _update() -> None:
            if entry.data.get(CONF_REFRESH_TOKEN) != new_token:
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
                )
        hass.loop.call_soon_threadsafe(_update)

    client.token_callback = _persist_refresh_token

    # Client is sync -> run its network calls in the executor. Refresh first to
    # obtain a valid access token, then fetch /state for premise + meter discovery.
    try:
        await hass.async_add_executor_job(client.refresh)
        state = await hass.async_add_executor_job(client.get_state)
    except CopperAuthError as err:
        # Bad/revoked refresh token -> start the reauth flow, don't retry forever.
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        # Network trouble etc. -> HA retries setup with backoff.
        raise ConfigEntryNotReady(str(err)) from err

    # Find the premise this entry represents; fall back to the first one if the
    # stored id isn't found (e.g. account changed).
    premises = state.get("premise_list", [])
    if not premises:
        # No premise -> no meters -> zero entities. Treat as "not ready" rather
        # than silently setting up an empty integration.
        raise ConfigEntryNotReady("Copper account has no premises")
    premise = next(
        (p for p in premises if p["id"] == entry.data.get(CONF_PREMISE_ID)),
        premises[0],
    )

    # Merge saved unit choices over the defaults so any meter the user didn't
    # explicitly set still has a sensible unit.
    units = {**DEFAULT_UNITS, **entry.options.get(CONF_UNITS, {})}

    # Create the coordinator and do the first fetch synchronously: if it fails,
    # setup fails cleanly (HA will retry) instead of adding broken entities.
    coordinator = CopperCoordinator(hass, client, premise, units)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug(
        "Setup complete: %d meter(s) discovered, polling every %s",
        len(coordinator.meters),
        coordinator.update_interval,
    )

    # Stash the coordinator on the entry so the sensor platform, options flow
    # and diagnostics can find it (modern runtime_data pattern, HA 2024.4+).
    entry.runtime_data = coordinator
    # Create the sensor entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # When the user changes options (units), reload the entry so entities pick up
    # the new units. async_on_unload ensures the listener is removed on unload.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its *options* (units) changed.

    This listener fires on ANY entry update, including the refresh-token
    persistence above. Reloading on those would loop forever (reload -> setup
    -> token refresh -> rotation -> persist -> reload ...), so only reload when
    the effective units actually differ from what the coordinator is using.
    """
    # The debug toggle takes effect immediately — no reload required.
    _apply_log_level(entry)
    coordinator = getattr(entry, "runtime_data", None)
    new_units = {**DEFAULT_UNITS, **entry.options.get(CONF_UNITS, {})}
    if coordinator and coordinator.units == new_units:
        return  # only the token/debug toggle (or title) changed
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the entry: remove its platforms.

    The coordinator lives in entry.runtime_data, which is not persisted and is
    replaced on the next setup — no manual cleanup needed.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
