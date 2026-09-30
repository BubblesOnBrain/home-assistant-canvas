"""Tests for serving and registering the canvas-workflow-card."""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.core import HomeAssistant

from custom_components.canvas.card import (
    CARD_DIR,
    CARD_FILENAME,
    STATIC_URL,
    async_register_card,
)

CARD = CARD_DIR / CARD_FILENAME


async def test_card_static_path_and_module_url_registered(hass: HomeAssistant) -> None:
    """The card is served without caching and loaded with a versioned URL."""
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])

    await async_register_card(hass)

    (paths,), _ = hass.http.async_register_static_paths.call_args
    assert len(paths) == 1
    assert paths[0].url_path == STATIC_URL
    assert paths[0].path == str(CARD_DIR)
    assert paths[0].cache_headers is False
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == {
        f"{STATIC_URL}/{CARD_FILENAME}?v=0.2.0"
    }


async def test_card_registration_without_http_or_frontend(
    hass: HomeAssistant,
) -> None:
    """Missing HTTP or frontend (e.g. in tests) is not an error."""
    setattr(hass, "http", None)
    await async_register_card(hass)

    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    hass.data.pop(DATA_EXTRA_MODULE_URL, None)
    await async_register_card(hass)
    hass.http.async_register_static_paths.assert_awaited_once()


def test_card_file_is_small_and_self_registering() -> None:
    """The card ships as one dependency-free file under 24 KB."""
    source = CARD.read_text(encoding="utf-8")
    assert CARD.stat().st_size < 24 * 1024
    assert 'customElements.define("canvas-workflow-card"' in source
    assert "window.customCards" in source
    assert "import " not in source
    # Only the services; the card never touches to-do completion.
    assert "todo.update_item" not in source
    assert list(CARD_DIR.iterdir()) == [CARD]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_card_javascript_parses() -> None:
    """The card is valid JavaScript."""
    subprocess.run(["node", "--check", str(CARD)], check=True)
