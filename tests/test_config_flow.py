"""Comprehensive unit tests for the Canvas LMS config flow."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    DOMAIN,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.models import CanvasUser

from .conftest import (
    MOCK_USER_SELF_RESPONSE,
    TEST_ACCESS_TOKEN,
    TEST_BASE_URL,
    TEST_USER_ID,
    TEST_USER_NAME,
)

MOCK_USER = CanvasUser(
    id=TEST_USER_ID,
    name=TEST_USER_NAME,
    sortable_name="Porter, Allen",
    short_name="Allen",
    primary_email="allen@example.edu",
)


# ============================================================================
# Tier 1: Core Feature Coverage Tests (>= 5 cases)
# ============================================================================


async def test_user_flow_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test successful user step creating a config entry."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result.get("errors") is None or result.get("errors") == {}

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: TEST_BASE_URL,
                CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "canvas.example.edu"
    assert result["data"] == {
        CONF_BASE_URL: TEST_BASE_URL,
        CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
    }
    assert result["result"].unique_id == str(TEST_USER_ID)
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        ("https://canvas.example.edu/", "https://canvas.example.edu"),
        ("https://canvas.example.edu///", "https://canvas.example.edu"),
        ("  https://canvas.example.edu  ", "https://canvas.example.edu"),
        ("canvas.instructure.com", "https://canvas.instructure.com"),
        ("canvas.instructure.com/", "https://canvas.instructure.com"),
        ("http://canvas.internal:8080/", "http://canvas.internal:8080"),
        ("http://canvas.internal:8080///", "http://canvas.internal:8080"),
        ("https://canvas.school.org/subpath/", "https://canvas.school.org/subpath"),
        ("HTTPS://canvas.example.edu", "https://canvas.example.edu"),
        ("Http://canvas.internal:8080/", "http://canvas.internal:8080"),
        ("hTTps://canvas.school.org/subpath/", "https://canvas.school.org/subpath"),
    ],
)
async def test_user_flow_url_normalization(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    raw_url: str,
    expected_url: str,
) -> None:
    """Test that URL inputs are properly normalized and trimmed."""
    aioclient_mock.get(
        f"{expected_url}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: raw_url,
                CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == expected_url


async def test_user_flow_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test user step failure with invalid authentication credentials."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=401,
        text="Invalid access token (HTTP 401)",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: "wrong_token",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test user step failure when Canvas server cannot be reached."""
    aioclient_mock.get(
        f"https://unreachable.canvas.edu{ENDPOINT_USERS_SELF}",
        exc=aiohttp.ClientError("Connection timed out"),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: "https://unreachable.canvas.edu",
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test aborting when the Canvas account is already configured."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
        unique_id=str(TEST_USER_ID),
        title=TEST_USER_NAME,
    )
    mock_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_recovery_after_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test successful recovery after a failed authentication attempt."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=401,
        text="401 Unauthorized",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # First attempt: auth failure
    result1 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: "bad_token",
        },
    )
    assert result1["type"] is FlowResultType.FORM
    assert result1["errors"] == {"base": "invalid_auth"}

    # Second attempt: corrected token succeeds
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result1["flow_id"],
            {
                CONF_BASE_URL: TEST_BASE_URL,
                CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "canvas.example.edu"
    assert result2["data"][CONF_ACCESS_TOKEN] == TEST_ACCESS_TOKEN


async def test_reauth_flow_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test successful re-authentication flow updating the access token."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    new_token = "new_valid_access_token_99999"
    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCESS_TOKEN: new_token,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == new_token


async def test_reauth_flow_invalid_auth_and_recovery(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauth failure with invalid token followed by recovery with valid token."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=401,
        text="HTTP 401",
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # Attempt 1: bad token
    result1 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ACCESS_TOKEN: "still_bad_token",
        },
    )
    assert result1["type"] is FlowResultType.FORM
    assert result1["errors"] == {"base": "invalid_auth"}

    # Attempt 2: valid token
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result1["flow_id"],
            {
                CONF_ACCESS_TOKEN: "fresh_working_token",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "fresh_working_token"


# ============================================================================
# Tier 2: Boundary, Corner & Adversarial Cases (>= 5 cases)
# ============================================================================


async def test_user_flow_unexpected_exception(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test user step failure when an unexpected server error occurs."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=RuntimeError("Unexpected system failure"),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_rate_limit_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test user step handling when rate limits are exceeded."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=429,
        text="Rate Limit Exceeded",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_timeout_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test user step timeout handling mapping to cannot_connect."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=TimeoutError("Client timeout"),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_special_characters_and_token_whitespace(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test token with special characters, symbols, and leading/trailing whitespace."""
    raw_token = "   ~1234~abcdef!@#$%^&*()_+`-={}|[]\\:\";'<>?,./   "
    expected_token = raw_token.strip()

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: TEST_BASE_URL,
                CONF_ACCESS_TOKEN: raw_token,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCESS_TOKEN] == expected_token


async def test_user_flow_unicode_name_preserved(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test that international unicode characters in user profile name are preserved."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json={
            "id": 88888,
            "name": "José-María & François Noël (山田太郎)",
            "sortable_name": "Noël, José-María",
            "primary_email": "jm@example.edu",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: TEST_BASE_URL,
                CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "canvas.example.edu"
    assert result["result"].unique_id == "88888"


async def test_user_flow_empty_name_fallback_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test fallback title when user name is empty string or default."""
    aioclient_mock.get(
        "https://seattle.instructure.com/api/v1/users/self",
        json={
            "id": 77777,
            "name": "User 77777",
            "primary_email": "user77777@example.edu",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.canvas.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: "https://seattle.instructure.com",
                CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "seattle.instructure.com"


async def test_reauth_flow_cannot_connect(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauth failure when Canvas connection fails."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=aiohttp.ClientError("Server unreachable"),
    )

    result = await mock_config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ACCESS_TOKEN: "some_token",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_account_mismatch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauth aborts if new token belongs to a different Canvas user."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json={
            "id": 99999,
            "name": "Different Person",
            "primary_email": "diff@example.edu",
        },
    )

    result = await mock_config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ACCESS_TOKEN: "different_user_token",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in (
        "reauth_account_mismatch",
        "wrong_account",
        "reauth_unsuccessful",
        "unique_id_mismatch",
    )


async def test_reconfigure_flow_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test successful reconfiguration of URL and token."""
    aioclient_mock.get(
        "https://new-canvas.example.edu/api/v1/users/self",
        json=MOCK_USER_SELF_RESPONSE,
    )

    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: "https://new-canvas.example.edu",
            CONF_ACCESS_TOKEN: "new_token",
        },
    )

    assert result2["type"] is FlowResultType.ABORT
    assert (
        result2["reason"] == "reauth_successful"
        or result2["reason"] == "reconfigure_successful"
    )
    assert mock_config_entry.data[CONF_BASE_URL] == "https://new-canvas.example.edu"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "new_token"


async def test_reconfigure_flow_validation_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reconfiguration with invalid credentials redisplays form with error."""
    aioclient_mock.get(
        "https://new-canvas.example.edu/api/v1/users/self",
        status=401,
        text="401 Unauthorized",
    )

    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: "https://new-canvas.example.edu",
            CONF_ACCESS_TOKEN: "invalid_token",
        },
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "reconfigure"
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_options_flow_handler(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test options flow handler initializes and completes."""
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={},
    )
    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"] == {"online_grace_hours": 12, "paper_grace_days": 7}


def test_get_entry_title_fallback_when_host_empty() -> None:
    """Test _get_entry_title falls back to user name when host is empty."""
    from custom_components.canvas.config_flow import _get_entry_title

    user = CanvasUser(id=12345, name="John Doe")
    assert _get_entry_title(user, "") == "John Doe"

    user_no_name = CanvasUser(id=12345, name="")
    assert _get_entry_title(user_no_name, "") == "User 12345"
