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
        # Set when this flow is a re-authentication of an existing entry (the
        # stored refresh token died); _create_entry then updates it in place.
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input=None):
        """First screen: let the user pick how to sign in."""
        # A menu with two options; each maps to async_step_<id> below.
        return self.async_show_menu(step_id="user", menu_options=["email", "token"])

    async def async_step_reauth(self, entry_data):
        """The stored refresh token was rejected — ask the user to sign in again.

        HA starts this flow when setup/update raises ConfigEntryAuthFailed. We
        remember which entry to fix, then reuse the normal sign-in menu.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user()

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
        await self.async_set_unique_id(premise["id"])

        if self._reauth_entry is not None:
            # Re-authenticating an existing entry: require the same premise so a
            # login with a different account can't silently hijack this entry,
            # then swap in the fresh token and reload.
            if (
                self._reauth_entry.unique_id
                and self._reauth_entry.unique_id != premise["id"]
            ):
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data={
                    **self._reauth_entry.data,
                    CONF_REFRESH_TOKEN: refresh_token,
                    CONF_PREMISE_ID: premise["id"],
                },
            )

        # Prevent adding the same premise twice.
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
            # Merge over any previously saved units so meter types not shown
            # this time (e.g. meter removed from the premise) keep their choice.
            merged = {**self.entry.options.get(CONF_UNITS, {}), **user_input}
            # Save the chosen units; the update listener in __init__ reloads the
            # entry so entities adopt them.
            return self.async_create_entry(title="", data={CONF_UNITS: merged})

        # Pre-fill each dropdown with the current value (saved option, else default).
        current = {**DEFAULT_UNITS, **self.entry.options.get(CONF_UNITS, {})}
        # Only offer units for meter types that actually exist on this premise.
        # Fall back to all volume types if the entry isn't loaded right now.
        coordinator = getattr(self.entry, "runtime_data", None)
        present = (
            {m["type"] for m in coordinator.meters}
            if coordinator
            else {"gas", "water_indoor", "water_outdoor"}
        )
        # Electric is fixed kWh (energy, not volume), so it's never configurable.
        choices = {"gas": GAS_UNITS, "water_indoor": WATER_UNITS, "water_outdoor": WATER_UNITS}
        schema = {
            # vol.In(...) renders as a dropdown limited to valid units.
            vol.Required(t, default=current[t]): vol.In(choices[t])
            for t in ("gas", "water_indoor", "water_outdoor")
            if t in present
        }
        if not schema:
            # e.g. an electric-only premise: nothing unit-related to configure.
            return self.async_abort(reason="no_units")
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
