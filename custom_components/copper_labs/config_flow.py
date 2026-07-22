"""UI flows: sign-in (email code, or advanced token paste) + options (units).

The config flow is what the user sees under Settings -> Devices & Services ->
Add Integration. The options flow is the "Configure" button on the entry.

Sign-in offers two paths:
  * email  — enter Copper email, receive a 6-digit code, enter it (the normal UX).
  * token  — paste a refresh token captured manually (fallback if the email
             path isn't available on the account).
"""

from __future__ import annotations

import voluptuous as vol  # HA builds its forms from voluptuous schemas

from homeassistant import config_entries
from homeassistant.core import callback

from .api import CopperClient, CopperAuthError
from .const import (
    CONF_PREMISE_ID,
    CONF_REFRESH_TOKEN,
    CONF_UNITS,
    DEFAULT_UNITS,
    DOMAIN,
)

# Allowed units per meter type, alphabetical. Restricted to what HA's gas/water
# device classes actually accept (gas can't be L/gal, so it isn't offered).
GAS_UNITS = ["CCF", "ft³", "m³"]
WATER_UNITS = ["CCF", "ft³", "gal", "L", "m³"]


class CopperConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    # Schema version for this entry's data; bump if the stored shape changes.
    VERSION = 1

    def __init__(self):
        # State carried between the email step and the code step:
        self._client: CopperClient | None = None  # holds the pending login session
        self._email: str | None = None            # the address the code was sent to

    async def async_step_user(self, user_input=None):
        """First screen: let the user pick how to sign in."""
        # A menu with two options; each maps to async_step_<id> below.
        return self.async_show_menu(step_id="user", menu_options=["email", "token"])

    # --- Path 1: email + one-time code -----------------------------------

    async def async_step_email(self, user_input=None):
        """Collect the Copper email and trigger the code email."""
        errors = {}
        if user_input is not None:
            self._email = user_input["email"]
            self._client = CopperClient()  # fresh session for this login
            try:
                # Sync client -> executor. Asks Auth0 to email a 6-digit code.
                await self.hass.async_add_executor_job(
                    self._client.start_email_login, self._email
                )
            except CopperAuthError:
                errors["base"] = "send_failed"
            else:
                # Code sent -> advance to the code-entry screen.
                return await self.async_step_code()
        return self.async_show_form(
            step_id="email",
            data_schema=vol.Schema({vol.Required("email"): str}),
            errors=errors,
        )

    async def async_step_code(self, user_input=None):
        """Collect the emailed code and exchange it for tokens."""
        # If HA restarted (or the flow resumed) between steps, the in-memory
        # login session is gone — send the user back to the email step instead
        # of crashing on the None client below.
        if self._client is None or self._email is None:
            return await self.async_step_email()
        errors = {}
        if user_input is not None:
            try:
                # Swap the code for tokens (sets refresh_token on the client)...
                await self.hass.async_add_executor_job(
                    self._client.complete_email_login, self._email, user_input["code"]
                )
                # ...then confirm the account has data / discover the premise.
                state = await self.hass.async_add_executor_job(self._client.get_state)
            except CopperAuthError as err:
                # Distinguish "wrong code" from "this account can't use the email
                # grant" (rare) so we can point the latter at the token path.
                errors["base"] = (
                    "email_unsupported" if "grant" in str(err).lower() else "invalid_code"
                )
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return await self._create_entry(state, self._client.refresh_token)
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    # --- Path 2: paste a refresh token (advanced fallback) ---------------

    async def async_step_token(self, user_input=None):
        """Validate a manually captured refresh token."""
        errors = {}
        if user_input is not None:
            client = CopperClient(refresh_token=user_input[CONF_REFRESH_TOKEN])
            try:
                # refresh() proves the token works; get_state() proves it has data.
                await self.hass.async_add_executor_job(client.refresh)
                state = await self.hass.async_add_executor_job(client.get_state)
            except CopperAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return await self._create_entry(state, client.refresh_token)
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str}),
            errors=errors,
        )

    # --- Shared: turn a validated login into a config entry --------------

    async def _create_entry(self, state: dict, refresh_token: str):
        """Create the entry from the discovered premise (both paths end here)."""
        premises = state.get("premise_list", [])
        if not premises:
            return self.async_abort(reason="no_premise")
        premise = premises[0]  # single-home accounts are the norm
        # Prevent adding the same premise twice.
        await self.async_set_unique_id(premise["id"])
        self._abort_if_unique_id_configured()
        # Persist only the refresh token + premise id (no email/code stored).
        return self.async_create_entry(
            title=f"Copper — {premise.get('name', premise['id'])}",
            data={
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_PREMISE_ID: premise["id"],
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        # Tells HA this integration has an options ("Configure") flow.
        return CopperOptionsFlow(entry)


class CopperOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry):
        self.entry = entry  # the entry whose units we're editing

    async def async_step_init(self, user_input=None):
        """Single options step: choose a display unit per meter."""
        if user_input is not None:
            # Save the chosen units; the update listener in __init__ reloads the
            # entry so entities adopt them.
            return self.async_create_entry(title="", data={CONF_UNITS: user_input})

        # Pre-fill each dropdown with the current value (saved option, else default).
        current = {**DEFAULT_UNITS, **self.entry.options.get(CONF_UNITS, {})}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    # vol.In(...) renders as a dropdown limited to valid units.
                    vol.Required("gas", default=current["gas"]): vol.In(GAS_UNITS),
                    vol.Required("water_indoor", default=current["water_indoor"]): vol.In(WATER_UNITS),
                    vol.Required("water_outdoor", default=current["water_outdoor"]): vol.In(WATER_UNITS),
                }
            ),
        )
