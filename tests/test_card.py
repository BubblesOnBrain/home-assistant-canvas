"""Tests for serving and registering the canvas-workflow-card."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant

from custom_components.canvas.card import (
    CARD_DIR,
    CARD_FILENAME,
    CARD_URL,
    STATIC_URL,
    async_register_card,
)

CARD = CARD_DIR / CARD_FILENAME
URL = f"{CARD_URL}?v=0.2.1"


@dataclass
class FakeResources:
    """Stand-in for the Lovelace resource storage collection."""

    items: list[dict[str, Any]] = field(default_factory=list)

    async def async_get_info(self) -> dict[str, int]:
        return {"resources": len(self.items)}

    def async_items(self) -> list[dict[str, Any]]:
        return list(self.items)

    async def async_create_item(self, data: dict[str, Any]) -> dict[str, Any]:
        item = {
            "id": f"id{len(self.items)}",
            "type": data["res_type"],
            "url": data["url"],
        }
        self.items.append(item)
        return item

    async def async_update_item(self, item_id: str, data: dict[str, Any]) -> None:
        for item in self.items:
            if item["id"] == item_id:
                item.update(type=data["res_type"], url=data["url"])

    async def async_delete_item(self, item_id: str) -> None:
        self.items = [i for i in self.items if i["id"] != item_id]


def _setup(
    hass: HomeAssistant, mode: str | None, items: list[dict[str, Any]] | None = None
) -> tuple[FakeResources, AsyncMock]:
    register = AsyncMock()
    hass.http = MagicMock(async_register_static_paths=register)
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])
    resources = FakeResources(items or [])
    if mode is not None:
        hass.data[LOVELACE_DATA] = MagicMock(resource_mode=mode, resources=resources)
    else:
        hass.data.pop(LOVELACE_DATA, None)
    return resources, register


async def test_card_served_and_added_as_dashboard_resource(hass: HomeAssistant) -> None:
    """The card is served uncached and registered as a module resource."""
    resources, register = _setup(hass, "storage")

    await async_register_card(hass)

    (paths,), _ = register.call_args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [
        (STATIC_URL, str(CARD_DIR), False)
    ]
    assert resources.items == [{"id": "id0", "type": "module", "url": URL}]
    # Not also loaded as an extra module: that races the first render.
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == frozenset()


async def test_existing_resources_updated_and_deduplicated(hass: HomeAssistant) -> None:
    """An old version or hand-added copy is updated; extra copies removed."""
    other = {"id": "x", "type": "module", "url": "/hacsfiles/other/card.js"}
    resources, _ = _setup(
        hass,
        "storage",
        [
            {"id": "a", "type": "module", "url": f"{CARD_URL}?v=0.2.0&res=1"},
            other,
            {"id": "b", "type": "module", "url": f"{CARD_URL}?v=0.2.0"},
        ],
    )

    await async_register_card(hass)

    assert resources.items == [{"id": "a", "type": "module", "url": URL}, other]

    # Idempotent on the next start.
    await async_register_card(hass)
    assert resources.items == [{"id": "a", "type": "module", "url": URL}, other]


@pytest.mark.parametrize("mode", ["yaml", None])
async def test_falls_back_to_extra_module_without_storage_resources(
    hass: HomeAssistant, mode: str | None
) -> None:
    """YAML-managed (or absent) dashboards get the card on every page instead."""
    _setup(hass, mode)

    await async_register_card(hass)

    assert hass.data[DATA_EXTRA_MODULE_URL].urls == {URL}


async def test_card_registration_without_http_or_frontend(
    hass: HomeAssistant,
) -> None:
    """Missing HTTP or frontend (e.g. in tests) is not an error."""
    setattr(hass, "http", None)
    await async_register_card(hass)

    register = AsyncMock()
    hass.http = MagicMock(async_register_static_paths=register)
    hass.data.pop(DATA_EXTRA_MODULE_URL, None)
    hass.data.pop(LOVELACE_DATA, None)
    await async_register_card(hass)
    register.assert_awaited_once()


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
