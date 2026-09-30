"""Canvas LMS custom component integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .api import CanvasApiClient
from .const import CONF_ACCESS_TOKEN, CONF_BASE_URL, DOMAIN, WORKFLOW_TICK_MINUTES
from .coordinator import CanvasDataUpdateCoordinator
from .store import CanvasWorkflowStore

_LOGGER = logging.getLogger(__name__)

type CanvasConfigEntry = ConfigEntry[CanvasDataUpdateCoordinator]

PLATFORMS: list[Platform] = [Platform.TODO, Platform.SENSOR, Platform.CALENDAR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Canvas integration (services shared by all entries)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CanvasConfigEntry) -> bool:
    """Set up Canvas LMS from a config entry."""
    client = CanvasApiClient(
        base_url=entry.data[CONF_BASE_URL],
        access_token=entry.data[CONF_ACCESS_TOKEN],
        session=async_get_clientsession(hass),
    )
    # Keyed by the Canvas account, not the entry, so removing and re-adding
    # the integration finds the same data again.
    workflow = CanvasWorkflowStore(hass, entry.unique_id or entry.entry_id)
    await workflow.async_load()

    coordinator = CanvasDataUpdateCoordinator(
        hass=hass,
        client=client,
        entry=entry,
        workflow=workflow,
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    # Grace windows run out between hourly fetches; re-evaluate regularly.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            coordinator.async_workflow_changed,
            timedelta(minutes=WORKFLOW_TICK_MINUTES),
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CanvasConfigEntry) -> bool:
    """Unload a Canvas LMS config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Write any debounced save now, so a reload never reads a stale file.
    if (coordinator := getattr(entry, "runtime_data", None)) is not None:
        await coordinator.workflow.async_flush()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: CanvasConfigEntry) -> None:
    """Reload a Canvas LMS config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


__all__ = [
    "CanvasApiClient",
    "CanvasConfigEntry",
    "CanvasDataUpdateCoordinator",
    "PLATFORMS",
    "async_reload_entry",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
]
