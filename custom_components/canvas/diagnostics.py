"""Diagnostics for Canvas LMS, including the stored workflow data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CanvasConfigEntry
from .const import CONF_ACCESS_TOKEN

TO_REDACT = {CONF_ACCESS_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CanvasConfigEntry
) -> dict[str, Any]:
    """Return the entry's settings and the student's stored workflow data."""
    workflow = entry.runtime_data.workflow
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "storage_file": f".storage/{workflow.key}",
        "workflow": workflow.as_dict(),
    }
