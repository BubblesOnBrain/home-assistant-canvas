"""Comprehensive unit tests for the Canvas LMS integration setup and lifecycle."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas import (
    async_reload_entry,
    async_unload_entry,
)
from custom_components.canvas.const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    DOMAIN,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator

from .conftest import (
    MOCK_COURSES_RESPONSE,
    MOCK_OBSERVEES_RESPONSE,
    MOCK_SUBMISSIONS_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    TEST_USER_ID,
)


def _setup_standard_mock_routes(
    aioclient_mock: AiohttpClientMocker, base_url: str = TEST_BASE_URL
) -> None:
    """Register standard Canvas LMS mock endpoints on aioclient_mock."""
    aioclient_mock.get(
        f"{base_url}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{base_url}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE,
    )
    aioclient_mock.get(
        f"{base_url}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{base_url}{ENDPOINT_USER_COURSES.format(user_id=4899)}",
        json=[],
    )
    aioclient_mock.get(
        f"{base_url}/api/v1/courses/7349/students/submissions",
        json=MOCK_SUBMISSIONS_RESPONSE,
    )


# ============================================================================
# Tier 1: Core Feature Coverage Tests (>= 5 cases)
# ============================================================================


async def test_async_setup_entry_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test successful setup of Canvas LMS integration from config entry."""
    _setup_standard_mock_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hasattr(mock_config_entry, "runtime_data")
    assert mock_config_entry.runtime_data is not None
    assert isinstance(mock_config_entry.runtime_data, CanvasDataUpdateCoordinator)
    assert mock_config_entry.runtime_data.data is not None
    assert mock_config_entry.runtime_data.data.user.id == TEST_USER_ID


async def test_async_setup_entry_auth_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setup failure when Canvas returns authentication failure."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=401,
        text="Token invalid (HTTP 401)",
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_async_setup_entry_not_ready_connection_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setup enters retry state when Canvas is unreachable during first refresh."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=aiohttp.ClientError("Connection timeout"),
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_async_unload_entry_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test clean unloading of platforms and config entry."""
    _setup_standard_mock_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    # Unload through Home Assistant so entry cleanup (timers) also runs.
    unload_result = await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert unload_result is True
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_async_reload_entry_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reloading a loaded config entry."""
    _setup_standard_mock_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    await async_reload_entry(hass, mock_config_entry)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


# ============================================================================
# Tier 2: Boundary, Corner & Adversarial Cases (>= 5 cases)
# ============================================================================


async def test_async_setup_entry_unexpected_error_retries(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setup handling when an unexpected Canvas server error occurs on startup."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=500,
        text="Internal Server Error",
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_multiple_config_entries_coexistence(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test that two separate Canvas entries instantiate independent coordinators."""
    entry_1 = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "https://canvas.school1.edu",
            CONF_ACCESS_TOKEN: "token_school_1",
        },
        unique_id="11111",
        title="Student 1 Account",
    )
    entry_2 = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "https://canvas.school2.edu",
            CONF_ACCESS_TOKEN: "token_school_2",
        },
        unique_id="22222",
        title="Student 2 Account",
    )
    entry_1.add_to_hass(hass)
    entry_2.add_to_hass(hass)

    # Routes for School 1
    aioclient_mock.get(
        "https://canvas.school1.edu/api/v1/users/self",
        json={"id": 11111, "name": "Student 1"},
    )
    aioclient_mock.get(
        "https://canvas.school1.edu/api/v1/users/self/observees",
        json=[],
    )
    aioclient_mock.get(
        "https://canvas.school1.edu/api/v1/users/11111/courses",
        json=[],
    )

    # Routes for School 2
    aioclient_mock.get(
        "https://canvas.school2.edu/api/v1/users/self",
        json={"id": 22222, "name": "Student 2"},
    )
    aioclient_mock.get(
        "https://canvas.school2.edu/api/v1/users/self/observees",
        json=[],
    )
    aioclient_mock.get(
        "https://canvas.school2.edu/api/v1/users/22222/courses",
        json=[],
    )

    await hass.config_entries.async_setup(entry_1.entry_id)
    if entry_2.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(entry_2.entry_id)
    await hass.async_block_till_done()

    assert entry_1.state is ConfigEntryState.LOADED
    assert entry_2.state is ConfigEntryState.LOADED

    # Verify both entries have distinct runtime data
    assert entry_1.runtime_data is not entry_2.runtime_data
    assert entry_1.runtime_data.data.user.id == 11111
    assert entry_2.runtime_data.data.user.id == 22222

    # Unload entry 1, verify entry 2 remains loaded
    await hass.config_entries.async_unload(entry_1.entry_id)
    await hass.async_block_till_done()
    assert entry_1.state is ConfigEntryState.NOT_LOADED
    assert entry_2.state is ConfigEntryState.LOADED

    # Unload entry 2
    await hass.config_entries.async_unload(entry_2.entry_id)
    await hass.async_block_till_done()
    assert entry_2.state is ConfigEntryState.NOT_LOADED


async def test_async_unload_entry_when_not_loaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test unloading an entry that was never loaded returns cleanly."""
    result = await async_unload_entry(hass, mock_config_entry)
    assert result is True


async def test_async_setup_and_unload_with_forwarded_platforms(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setup and unload entry forwards platforms when PLATFORMS is configured."""
    _setup_standard_mock_routes(aioclient_mock)

    with (
        patch(
            "custom_components.canvas.PLATFORMS",
            [Platform.SENSOR],
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            return_value=True,
        ) as mock_forward,
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            return_value=True,
        ) as mock_unload,
    ):
        setup_result = await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert setup_result is True
        assert len(mock_forward.mock_calls) == 1

        unload_result = await hass.config_entries.async_unload(
            mock_config_entry.entry_id
        )
        await hass.async_block_till_done()

        assert unload_result is True
        assert len(mock_unload.mock_calls) == 1
