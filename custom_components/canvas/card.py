"""Serve and register the canvas-workflow-card Lovelace card."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "canvas-workflow-card.js"
STATIC_URL = "/canvas_static"
CARD_DIR = Path(__file__).parent / "frontend"
CARD_URL = f"{STATIC_URL}/{CARD_FILENAME}"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card file and make every dashboard load it.

    Registered as a dashboard resource (like cards installed through HACS) so
    dashboards wait for it. Loading it as a frontend "extra module" instead
    races the first render: some browsers show "Custom element doesn't exist"
    after a refresh and never recover.
    """
    if hass.http is None:
        _LOGGER.debug("HTTP not available; not registering %s", CARD_FILENAME)
        return
    integration = await async_get_integration(hass, DOMAIN)
    # The version query string makes browsers fetch the new file after an update.
    url = f"{CARD_URL}?v={integration.version}"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(CARD_DIR), cache_headers=False)]
    )
    if await _async_ensure_resource(hass, url):
        return
    # Dashboards managed in YAML: resources can't be added from here, so load
    # the card on every page instead.
    if DATA_EXTRA_MODULE_URL in hass.data:
        add_extra_js_url(hass, url)
    else:
        _LOGGER.debug("Frontend not loaded; %s served but not added", CARD_FILENAME)


async def _async_ensure_resource(hass: HomeAssistant, url: str) -> bool:
    """Create or update the card's dashboard resource; return False if not possible."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or lovelace.resource_mode != MODE_STORAGE:
        return False
    # Storage mode means the resources are the editable storage collection.
    resources = cast(ResourceStorageCollection, lovelace.resources)
    await resources.async_get_info()  # loads the collection if needed
    ours = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).startswith(CARD_URL)
    ]
    if not ours:
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.info("Added dashboard resource %s", url)
        return True
    first, *duplicates = ours
    if first.get("url") != url or first.get("type") != "module":
        await resources.async_update_item(
            first["id"], {"res_type": "module", "url": url}
        )
    for duplicate in duplicates:
        await resources.async_delete_item(duplicate["id"])
    return True
