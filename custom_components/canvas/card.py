"""Serve and register the canvas-workflow-card Lovelace card."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "canvas-workflow-card.js"
STATIC_URL = "/canvas_static"
CARD_DIR = Path(__file__).parent / "frontend"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card file and load it on every dashboard (no manual resource)."""
    if hass.http is None:
        _LOGGER.debug("HTTP not available; not registering %s", CARD_FILENAME)
        return
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(CARD_DIR), cache_headers=False)]
    )
    if DATA_EXTRA_MODULE_URL not in hass.data:
        _LOGGER.debug("Frontend not loaded; %s served but not added", CARD_FILENAME)
        return
    # The version query string makes browsers fetch the new file after an update.
    add_extra_js_url(hass, f"{STATIC_URL}/{CARD_FILENAME}?v={integration.version}")
