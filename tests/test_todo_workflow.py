"""Tests for how the to-do entity shows stages, notes and follow-up tasks."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    DOMAIN,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.todo import CanvasTodoListEntity
from custom_components.canvas.workflow import FollowupStage

from .conftest import (
    MOCK_OBSERVEES_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
)

ENTITY = "todo.quentin_porter_assignments"
NOW = "2026-09-29T12:00:00Z"
MISSING, UPCOMING, LATE, PAPER = "1001", "1002", "1003", "1004"


def _sub(aid: str, due: str, **kwargs: Any) -> dict[str, Any]:
    types = kwargs.pop("types", ["online_upload"])
    asg = build_mock_assignment_dict(
        assignment_id=int(aid),
        course_id=7349,
        name=f"Assignment {aid}",
        due_at=due,
        lock_at=None,
        unlock_at=None,
        submission_types=types,
        html_url=f"{TEST_BASE_URL}/courses/7349/assignments/{aid}",
    )
    return build_mock_submission_dict(
        submission_id=int(aid), assignment_id=int(aid), assignment=asg, **kwargs
    )


def _routes(aioclient_mock: AiohttpClientMocker, subs: list[dict[str, Any]]) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}", json=MOCK_USER_SELF_RESPONSE
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[MOCK_OBSERVEES_RESPONSE[0]],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[build_mock_course_dict(course_id=7349, name="Biology - P3 Rivera")],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}"
        f"{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=subs,
    )


DEFAULT_SUBS = [
    _sub(MISSING, "2026-09-29T03:59:00Z"),
    _sub(UPCOMING, "2026-10-02T03:59:00Z"),
    _sub(LATE, "2026-09-25T03:59:00Z", submitted_at="2026-09-28T20:00:00Z", late=True),
    _sub(PAPER, "2026-10-01T03:59:00Z", types=["on_paper"]),
]


@pytest.fixture(name="coordinator")
async def fixture_coordinator(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> CanvasDataUpdateCoordinator:
    """Set up one student with missing, upcoming, late and paper work."""
    freezer.move_to(NOW)
    _routes(aioclient_mock, DEFAULT_SUBS)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry.runtime_data


def _entity(hass: HomeAssistant) -> CanvasTodoListEntity:
    entity = hass.data["entity_components"]["todo"].get_entity(ENTITY)
    assert isinstance(entity, CanvasTodoListEntity)
    return entity


def _item(hass: HomeAssistant, uid: str) -> Any:
    return next(i for i in _entity(hass).todo_items or [] if i.uid == uid)


def _attrs(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.states.get(ENTITY)
    assert state is not None
    return dict(state.attributes)


def _compact(hass: HomeAssistant, uid: str) -> dict[str, Any]:
    attrs = _attrs(hass)
    listed = attrs["attention_items"] + attrs["upcoming_items"] + attrs["late_items"]
    return next(i for i in listed if i["uid"] == uid)


async def _set_stage(hass: HomeAssistant, aid: str, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_assignment_stage",
        {"assignment_id": aid, **data},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_stage_and_note_shown_without_refetch(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Setting a stage and note re-renders summary, description and attributes."""
    calls = aioclient_mock.call_count
    await _set_stage(hass, UPCOMING, stage="working", note="Need graph")
    assert aioclient_mock.call_count == calls

    item = _item(hass, UPCOMING)
    assert item.summary == "🔵 [Biology - P3 Rivera] Assignment 1002"
    assert item.description.splitlines()[:2] == ["Status: working", "Note: Need graph"]
    compact = _compact(hass, UPCOMING)
    assert compact["stage"] == "working"
    assert compact["note"] == "Need graph"
    assert compact["display_state"] == "working"
    assert compact["overdue"] is False
    assert _attrs(hass)["display_working_count"] == 1


async def test_done_not_submitted_overdue_is_distinct(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Done-but-not-submitted still shows once Canvas calls it missing."""
    await _set_stage(hass, MISSING, stage="done_not_submitted")
    item = _item(hass, MISSING)
    assert item.summary.startswith("🟡 DONE, SUBMIT IT · ")
    compact = _compact(hass, MISSING)
    assert compact["display_state"] == "done_not_submitted"
    assert compact["overdue"] is True
    attrs = _attrs(hass)
    assert attrs["done_not_submitted_overdue_count"] == 1
    assert attrs["display_missing_count"] == 0


async def test_claim_never_completes_item(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """'I submitted it' changes the label but leaves the item open."""
    await _set_stage(hass, MISSING, stage="submitted_claimed")
    item = _item(hass, MISSING)
    assert item.summary.startswith("⏳ SUBMITTED? · ")
    assert item.status == "needs_action"
    assert _compact(hass, MISSING)["claimed_submitted_at"] is not None

    # Grace passes on the 15-minute tick: flagged and a follow-up task opens.
    freezer.tick(timedelta(hours=12, minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    item = _item(hass, MISSING)
    assert item.summary.startswith("⚠️ NOT IN CANVAS · ")
    assert item.status == "needs_action"
    assert _attrs(hass)["not_in_canvas_count"] == 1
    task = _item(hass, f"followup:{MISSING}:not_in_canvas")
    assert task.summary == "⚠️ CHECK CANVAS · [Biology - P3 Rivera] Assignment 1001"
    assert task.status == "needs_action"


async def test_paper_claim_shows_turned_in(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Paper work marked turned in shows the paper waiting label."""
    await _set_stage(hass, PAPER, stage="submitted_claimed")
    item = _item(hass, PAPER)
    assert item.summary.startswith("📝 TURNED IN · ")
    assert item.summary.endswith(" 📝")
    assert item.description.startswith("Status: paper_waiting (paper)")


async def test_late_work_followup_task(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """A late submission is checked off and adds an email-the-teacher task."""
    late = _item(hass, LATE)
    assert late.status == "completed"
    assert late.summary.startswith("✉️ LATE · ")

    task = _item(hass, f"followup:{LATE}:late_work")
    assert task.summary == "✉️ EMAIL TEACHER · [Biology - P3 Rivera] Assignment 1003"
    assert task.status == "needs_action"
    # Opened Tue 29th 5 am Pacific: due the next local day.
    assert str(task.due) == "2026-09-30"
    assert task.description.startswith("Follow-up: late_work · needs_contact")

    attrs = _attrs(hass)
    assert attrs["followup_open_count"] == 1
    assert attrs["followup_needs_contact_count"] == 1
    assert attrs["followup_overdue_count"] == 0
    followup = attrs["followup_items"][0]
    assert followup["id"] == f"{LATE}:late_work"
    assert followup["class"] == "Biology"
    assert followup["assignment"] == "Assignment 1003"
    assert followup["contact_by"] == "2026-09-30"
    assert followup["in_canvas"] is True
    assert "followup_items" in CanvasTodoListEntity._unrecorded_attributes


async def test_ticking_followup_sets_contacted(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Ticking a follow-up task means contacted; unticking reverts it."""
    uid = f"followup:{LATE}:late_work"
    await hass.services.async_call(
        "todo",
        "update_item",
        {"entity_id": ENTITY, "item": uid, "status": "completed"},
        blocking=True,
    )
    followup = coordinator.workflow.followups[f"{LATE}:late_work"]
    assert followup.stage is FollowupStage.CONTACTED
    assert _item(hass, uid).status == "completed"
    assert _attrs(hass)["followup_needs_contact_count"] == 0
    assert _attrs(hass)["followup_open_count"] == 1

    await hass.services.async_call(
        "todo",
        "update_item",
        {"entity_id": ENTITY, "item": uid, "status": "needs_action"},
        blocking=True,
    )
    assert followup.stage is FollowupStage.NEEDS_CONTACT


async def test_canvas_items_are_read_only(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Canvas items can't be ticked or renamed; items can't be added or removed."""
    with pytest.raises(HomeAssistantError, match="complete automatically"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": ENTITY, "item": UPCOMING, "status": "completed"},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError, match="can't be edited"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": ENTITY, "item": UPCOMING, "rename": "Something else"},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError, match="can't be edited"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {
                "entity_id": ENTITY,
                "item": f"followup:{LATE}:late_work",
                "rename": "Something else",
            },
            blocking=True,
        )
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            "todo", "add_item", {"entity_id": ENTITY, "item": "Poster"}, blocking=True
        )
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            "todo",
            "remove_item",
            {"entity_id": ENTITY, "item": [UPCOMING]},
            blocking=True,
        )
    assert _item(hass, UPCOMING).status == "needs_action"


async def test_followup_marked_when_gone_and_hidden_after_resolved(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A follow-up outlives its assignment, then hides a week after resolving."""
    _routes(aioclient_mock, [s for s in DEFAULT_SUBS if s["assignment_id"] != 1003])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    task = _item(hass, f"followup:{LATE}:late_work")
    assert "No longer in Canvas" in task.description
    assert _attrs(hass)["followup_items"][0]["in_canvas"] is False

    await hass.services.async_call(
        DOMAIN,
        "set_followup_stage",
        {"followup_id": f"{LATE}:late_work", "stage": "resolved"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _item(hass, f"followup:{LATE}:late_work").status == "completed"

    freezer.tick(timedelta(days=7, minutes=30))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    uids = [i.uid for i in _entity(hass).todo_items or []]
    assert f"followup:{LATE}:late_work" not in uids
    assert _attrs(hass)["followup_items"] == []
    assert f"{LATE}:late_work" in coordinator.workflow.followups
