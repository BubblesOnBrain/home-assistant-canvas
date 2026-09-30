"""Config flow for Canvas LMS integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import CanvasApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    CONF_ESCALATION_THRESHOLD,
    CONF_ONLINE_GRACE_HOURS,
    CONF_PAPER_GRACE_DAYS,
    CONF_QUIZ_LEAD_DAYS,
    CONF_TEST_LEAD_DAYS,
    DEFAULT_ESCALATION_THRESHOLD,
    DEFAULT_ONLINE_GRACE_HOURS,
    DEFAULT_PAPER_GRACE_DAYS,
    DEFAULT_QUIZ_LEAD_DAYS,
    DEFAULT_TEST_LEAD_DAYS,
    DOMAIN,
    ESCALATION_THRESHOLD_RANGE,
    ONLINE_GRACE_HOURS_RANGE,
    PAPER_GRACE_DAYS_RANGE,
    QUIZ_LEAD_DAYS_RANGE,
    TEST_LEAD_DAYS_RANGE,
)
from .exceptions import (
    CanvasAuthError,
    CanvasConnectionError,
    CanvasError,
)
from .models import CanvasUser

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): str,
        vol.Required(CONF_ACCESS_TOKEN): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
    }
)


def _normalize_url(raw_url: str) -> str:
    """Normalize base URL by trimming whitespace, trailing slashes, and adding scheme."""
    url = raw_url.strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    if url.lower().startswith("https://"):
        url = f"https://{url[8:]}"
    else:
        url = f"http://{url[7:]}"
    return url


def _get_entry_title(user: CanvasUser, base_url: str) -> str:
    """Generate config entry title using the school host name."""
    parsed = urlparse(base_url)
    host = parsed.netloc or parsed.path.strip("/")
    if host:
        return host
    return user.name.strip() if user.name and user.name.strip() else f"User {user.id}"


class CanvasConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Canvas LMS."""

    VERSION = 1
    MINOR_VERSION = 1

    async def _async_validate_and_get_user(
        self, base_url: str, access_token: str
    ) -> tuple[CanvasUser | None, dict[str, str]]:
        """Validate credentials against Canvas LMS API and return user or error mapping."""
        errors: dict[str, str] = {}
        try:
            session = async_get_clientsession(self.hass)
            client = CanvasApiClient(
                base_url=base_url,
                access_token=access_token,
                session=session,
            )
            user = await client.async_get_current_user()
            return user, errors
        except CanvasAuthError:
            errors["base"] = "invalid_auth"
        except (CanvasConnectionError, TimeoutError):
            errors["base"] = "cannot_connect"
        except (CanvasError, Exception):
            errors["base"] = "unknown"
        return None, errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            normalized_url = _normalize_url(user_input[CONF_BASE_URL])
            token = user_input[CONF_ACCESS_TOKEN].strip()

            user, errors = await self._async_validate_and_get_user(
                normalized_url, token
            )
            if user is not None:
                await self.async_set_unique_id(str(user.id))
                self._abort_if_unique_id_configured()

                title = _get_entry_title(user, normalized_url)
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_BASE_URL: normalized_url,
                        CONF_ACCESS_TOKEN: token,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication upon token failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication with updated credentials."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            base_url = reauth_entry.data[CONF_BASE_URL]

            user, errors = await self._async_validate_and_get_user(base_url, token)
            if user is not None:
                if str(user.id) != reauth_entry.unique_id:
                    return self.async_abort(reason="reauth_account_mismatch")

                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_ACCESS_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of Canvas LMS instance URL and token."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            normalized_url = _normalize_url(user_input[CONF_BASE_URL])
            token = user_input[CONF_ACCESS_TOKEN].strip()

            user, errors = await self._async_validate_and_get_user(
                normalized_url, token
            )
            if user is not None:
                if str(user.id) != reconfigure_entry.unique_id:
                    return self.async_abort(reason="reauth_account_mismatch")

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates={
                        CONF_BASE_URL: normalized_url,
                        CONF_ACCESS_TOKEN: token,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, reconfigure_entry.data
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CanvasOptionsFlowHandler:
        """Get the options flow handler."""
        return CanvasOptionsFlowHandler()


class CanvasOptionsFlowHandler(OptionsFlow):
    """Handle options for Canvas LMS."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ONLINE_GRACE_HOURS,
                        default=options.get(
                            CONF_ONLINE_GRACE_HOURS, DEFAULT_ONLINE_GRACE_HOURS
                        ),
                    ): _whole_number(ONLINE_GRACE_HOURS_RANGE, "h"),
                    vol.Required(
                        CONF_PAPER_GRACE_DAYS,
                        default=options.get(
                            CONF_PAPER_GRACE_DAYS, DEFAULT_PAPER_GRACE_DAYS
                        ),
                    ): _whole_number(PAPER_GRACE_DAYS_RANGE, "d"),
                    vol.Required(
                        CONF_QUIZ_LEAD_DAYS,
                        default=options.get(
                            CONF_QUIZ_LEAD_DAYS, DEFAULT_QUIZ_LEAD_DAYS
                        ),
                    ): _whole_number(QUIZ_LEAD_DAYS_RANGE, "d"),
                    vol.Required(
                        CONF_TEST_LEAD_DAYS,
                        default=options.get(
                            CONF_TEST_LEAD_DAYS, DEFAULT_TEST_LEAD_DAYS
                        ),
                    ): _whole_number(TEST_LEAD_DAYS_RANGE, "d"),
                    vol.Required(
                        CONF_ESCALATION_THRESHOLD,
                        default=options.get(
                            CONF_ESCALATION_THRESHOLD, DEFAULT_ESCALATION_THRESHOLD
                        ),
                    ): _whole_number(ESCALATION_THRESHOLD_RANGE, "%"),
                }
            ),
        )


def _whole_number(bounds: tuple[int, int], unit: str) -> vol.All:
    """Return a validator for a whole number within bounds."""
    low, high = bounds
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=low,
                max=high,
                step=1,
                unit_of_measurement=unit,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Coerce(int),
        vol.Range(min=low, max=high),
    )
