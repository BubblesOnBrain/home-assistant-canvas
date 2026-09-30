"""Comprehensive unit and integration tests for Canvas LMS To-Do platform."""

from __future__ import annotations

import pytest

from datetime import date, datetime, timezone
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
)
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry
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
from custom_components.canvas.todo import (
    STATUS_PREFIX,
    CanvasTodoListEntity,
    CanvasTodoListExtraStoredData,
)

from .conftest import (
    MOCK_COURSES_RESPONSE,
    MOCK_OBSERVEES_RESPONSE,
    MOCK_SUBMISSIONS_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    TEST_USER_ID,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
    build_mock_term_dict,
)

FROZEN_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _base(summary: str | None) -> str:
    """Strip Canvas status prefixes and the paper marker from a summary."""
    text = summary or ""
    for prefix in STATUS_PREFIX.values():
        text = text.removeprefix(prefix)
    return text.removesuffix(" 📝")


def _setup_dual_student_routes(aioclient_mock: AiohttpClientMocker) -> None:
    """Set up routes for Quentin (6021) and Theodore (4899)."""
    term = build_mock_term_dict(term_id=101, name="Fall 2026", workflow_state="active")
    course_apush = build_mock_course_dict(
        course_id=7349, name="AP US History", term=term
    )
    course_bio = build_mock_course_dict(course_id=7350, name="AP Biology", term=term)

    asg_apush = build_mock_assignment_dict(
        assignment_id=134664,
        course_id=7349,
        name="Chapter 1 Reflection",
        due_at="2026-09-01T23:59:59Z",
        description="Read Chapter 1 and submit reflections.",
    )
    sub_apush = build_mock_submission_dict(
        submission_id=5196766,
        assignment_id=134664,
        user_id=6021,
        assignment=asg_apush,
    )

    asg_bio = build_mock_assignment_dict(
        assignment_id=134665,
        course_id=7350,
        name="Cell Mitosis Lab",
        due_at="2026-09-05T23:59:59Z",
        description="Submit lab report PDF.",
    )
    sub_bio = build_mock_submission_dict(
        submission_id=5196767,
        assignment_id=134665,
        user_id=4899,
        assignment=asg_bio,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE,
    )
    # Quentin courses & submissions
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[course_apush],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[sub_apush],
    )
    # Theodore courses & submissions
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=4899)}",
        json=[course_bio],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7350)}",
        json=[sub_bio],
    )


def _extract_items(response: Any, entity_id: str) -> list[dict[str, Any]]:
    """Extract item list from a service response dictionary."""
    if not isinstance(response, dict):
        return []
    entity_data = response.get(entity_id, {})
    if not isinstance(entity_data, dict):
        return []
    items = entity_data.get("items", [])
    return list(items) if isinstance(items, list) else []


async def test_todo_entities_and_device_registry_dual_students(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test that setting up config entry creates To-Do entities and devices per student."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Verify 2 distinct devices were registered
    device_quentin = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.unique_id}_6021")}
    )
    assert device_quentin is not None
    assert device_quentin.name == "Quentin Porter"
    assert device_quentin.manufacturer == "Instructure Canvas"
    assert device_quentin.model == "Student Profile"

    device_theodore = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.unique_id}_4899")}
    )
    assert device_theodore is not None
    assert device_theodore.name == "Theodore Porter"
    assert device_theodore.manufacturer == "Instructure Canvas"

    # Verify 2 distinct entities in registry
    quentin_entity_id = entity_registry.async_get_entity_id(
        Platform.TODO, DOMAIN, f"{mock_config_entry.unique_id}_6021_todo"
    )
    assert quentin_entity_id == "todo.quentin_porter_assignments"

    theodore_entity_id = entity_registry.async_get_entity_id(
        Platform.TODO, DOMAIN, f"{mock_config_entry.unique_id}_4899_todo"
    )
    assert theodore_entity_id == "todo.theodore_porter_assignments"


async def test_todo_items_projection_and_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that active assignments are projected into TodoItems with course tag and due date."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "1"  # 1 pending task

    response = await hass.services.async_call(
        "todo",
        "get_items",
        {"entity_id": ["todo.quentin_porter_assignments"], "status": ["needs_action"]},
        blocking=True,
        return_response=True,
    )
    items = _extract_items(response, "todo.quentin_porter_assignments")
    assert len(items) == 1
    assert items[0]["uid"] == "134664"
    assert _base(items[0]["summary"]) == "[AP US History] Chapter 1 Reflection"
    assert items[0]["status"] == "needs_action"
    # Descriptions are compact: status line + link, never the teacher's HTML
    assert items[0]["description"].startswith("Status: ")
    assert "assignments/134664" in items[0]["description"]
    assert "Read Chapter 1" not in items[0]["description"]
    assert "2026-09-01" in items[0]["due"]


async def test_todo_canvas_item_completion_is_owned_by_canvas(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Checking off a Canvas item is rejected; only a Canvas submission completes it."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="complete automatically"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {
                "entity_id": "todo.quentin_porter_assignments",
                "item": "134664",
                "status": "completed",
            },
            blocking=True,
        )

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "1"  # still open

    # Manual items can still be checked off normally
    entity = hass.data["entity_components"]["todo"].get_entity(
        "todo.quentin_porter_assignments"
    )
    assert isinstance(entity, CanvasTodoListEntity)
    await entity.async_create_todo_item(TodoItem(summary="Study for quiz"))
    manual = [i for i in entity.todo_items or [] if i.summary == "Study for quiz"][0]
    await entity.async_update_todo_item(
        TodoItem(
            uid=manual.uid, summary=manual.summary, status=TodoItemStatus.COMPLETED
        )
    )
    manual = [i for i in entity.todo_items or [] if i.uid == manual.uid][0]
    assert manual.status == TodoItemStatus.COMPLETED

    # Status counts are exposed for automations
    assert "missing_count" in state.attributes or "upcoming_count" in state.attributes
    assert "late_items" in state.attributes


async def test_todo_manual_item_creation_update_and_deletion(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test creating, updating, and deleting custom manual To-Do items."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Add a manual task
    await hass.services.async_call(
        "todo",
        "add_item",
        {
            "entity_id": "todo.quentin_porter_assignments",
            "item": "Buy poster board for science fair",
            "due_date": "2026-09-10",
            "description": "Get blue tri-fold board",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "2"  # 1 Canvas + 1 manual

    response = await hass.services.async_call(
        "todo",
        "get_items",
        {"entity_id": ["todo.quentin_porter_assignments"], "status": ["needs_action"]},
        blocking=True,
        return_response=True,
    )
    items = _extract_items(response, "todo.quentin_porter_assignments")
    assert len(items) == 2
    manual_item = [
        i for i in items if i["summary"] == "Buy poster board for science fair"
    ][0]
    manual_uid = manual_item["uid"]

    # Update the manual task
    await hass.services.async_call(
        "todo",
        "update_item",
        {
            "entity_id": "todo.quentin_porter_assignments",
            "item": manual_uid,
            "rename": "Buy tri-fold poster board and markers",
            "status": "completed",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "1"

    # Delete the manual task
    await hass.services.async_call(
        "todo",
        "remove_item",
        {
            "entity_id": "todo.quentin_porter_assignments",
            "item": [manual_uid],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "1"


async def test_todo_delete_canvas_synced_item(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test deleting/dismissing a Canvas synced assignment item."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "1"

    # Delete Canvas assignment
    await hass.services.async_call(
        "todo",
        "remove_item",
        {
            "entity_id": "todo.quentin_porter_assignments",
            "item": ["134664"],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "0"

    # Trigger coordinator refresh; deleted item stays deleted
    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("todo.quentin_porter_assignments")
    assert state is not None
    assert state.state == "0"


async def test_todo_custom_item_overrides_on_canvas_assignment(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test custom overrides (edited summary, due date, description) on Canvas assignments."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "todo",
        "update_item",
        {
            "entity_id": "todo.quentin_porter_assignments",
            "item": "134664",
            "rename": "[AP US History] Ch 1 Reflection (Typed)",
            "due_date": "2026-09-03",
            "description": "Updated notes",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        "todo",
        "get_items",
        {"entity_id": ["todo.quentin_porter_assignments"], "status": ["needs_action"]},
        blocking=True,
        return_response=True,
    )
    items = _extract_items(response, "todo.quentin_porter_assignments")
    assert len(items) == 1
    assert items[0]["summary"] == "[AP US History] Ch 1 Reflection (Typed)"
    assert items[0]["description"] == "Updated notes"
    assert "2026-09-03" in items[0]["due"]


async def test_todo_single_student_fallback_direct_login(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test single student direct login creates entity linked to user profile."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=TEST_USER_ID)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=MOCK_SUBMISSIONS_RESPONSE,
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.unique_id}_{TEST_USER_ID}")}
    )
    assert device is not None
    assert device.name == "Allen Porter"

    entity_id = entity_registry.async_get_entity_id(
        Platform.TODO, DOMAIN, f"{mock_config_entry.unique_id}_{TEST_USER_ID}_todo"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "1"


async def test_todo_entity_unit_edge_cases(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test unit edge cases: empty uids, missing course names, explicit manual uids."""
    _setup_dual_student_routes(aioclient_mock)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = hass.data["entity_components"]["todo"].get_entity(
        "todo.quentin_porter_assignments"
    )
    assert entity is not None

    # Update item with empty / None uid -> returns immediately without error
    await entity.async_update_todo_item(TodoItem(uid=None, summary="Test"))
    await entity.async_update_todo_item(TodoItem(uid="", summary="Test"))

    # Create manual item with explicit uid and explicit status
    await entity.async_create_todo_item(
        TodoItem(
            uid="custom_explicit_uid_123",
            summary="Manual item with explicit ID",
            status=TodoItemStatus.COMPLETED,
        )
    )
    assert entity.todo_items is not None
    explicit_item = [
        i for i in entity.todo_items if i.uid == "custom_explicit_uid_123"
    ][0]
    assert explicit_item.status == TodoItemStatus.COMPLETED

    # Update Canvas item with partial fields
    await entity.async_update_todo_item(
        TodoItem(
            uid="134664",
            summary="Only new summary",
            status=TodoItemStatus.NEEDS_ACTION,
        )
    )
    canvas_item = [i for i in entity.todo_items if i.uid == "134664"][0]
    assert canvas_item.summary == "Only new summary"
    assert canvas_item.status == TodoItemStatus.NEEDS_ACTION

    # Update Canvas item with status=None and summary=None (due date only)
    new_due = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    await entity.async_update_todo_item(
        TodoItem(
            uid="134664",
            due=new_due,
            status=None,
        )
    )
    canvas_item = [i for i in entity.todo_items if i.uid == "134664"][0]
    assert canvas_item.due == new_due


async def test_todo_state_restoration_on_startup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test restoring completed and manual items across Home Assistant restarts."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        mock_restore_cache_with_extra_data,
    )

    _setup_dual_student_routes(aioclient_mock)

    extra_data = CanvasTodoListExtraStoredData(
        completed_uids=["134664"],
        deleted_uids=["999999"],
        custom_overrides={
            "134664": {"summary": "[AP US History] Custom Restored Title"}
        },
        manual_items=[
            {
                "uid": "restored_manual_1",
                "summary": "Restored Task",
                "status": "needs_action",
                "due": "2026-09-15T10:00:00+00:00",
                "description": "Restored note",
            },
            {
                "uid": "restored_invalid_due",
                "summary": "Task with invalid due",
                "status": "needs_action",
                "due": "invalid-datetime-format",
            },
            {
                "uid": "",
                "summary": "No UID item",
            },
        ],
    )

    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("todo.quentin_porter_assignments", "1"),
                extra_data.as_dict(),
            )
        ],
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = hass.data["entity_components"]["todo"].get_entity(
        "todo.quentin_porter_assignments"
    )
    assert entity is not None
    assert entity.extra_restore_state_data is not None
    stored_dict = entity.extra_restore_state_data.as_dict()
    assert "134664" in stored_dict["completed_uids"]

    # Check from_dict helper
    reconstructed = CanvasTodoListExtraStoredData.from_dict(stored_dict)
    assert reconstructed.completed_uids == ["134664"]

    items = entity.todo_items or []
    canvas_item = [i for i in items if i.uid == "134664"][0]
    # A restored local completion no longer overrides Canvas: still open
    assert canvas_item.status == TodoItemStatus.NEEDS_ACTION
    assert canvas_item.summary == "[AP US History] Custom Restored Title"

    manual_item = [i for i in items if i.uid == "restored_manual_1"][0]
    assert manual_item.summary == "Restored Task"
    assert manual_item.due == datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)


async def test_todo_items_ordered_by_due_date(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that To-Do items are ordered chronologically by due date, with undated items at the end."""
    sub_late: dict[str, Any] = {
        "id": 881,
        "user_id": 6021,
        "assignment_id": 101,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 101,
            "name": "Late Assignment",
            "course_id": 7349,
            "due_at": "2026-09-10T23:59:59Z",
            "submission_types": ["online_upload"],
        },
    }
    sub_early: dict[str, Any] = {
        "id": 882,
        "user_id": 6021,
        "assignment_id": 102,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 102,
            "name": "Early Assignment",
            "course_id": 7349,
            "due_at": "2026-09-01T12:00:00Z",
            "submission_types": ["online_text_entry"],
        },
    }
    sub_undated: dict[str, Any] = {
        "id": 883,
        "user_id": 6021,
        "assignment_id": 103,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 103,
            "name": "Undated Assignment",
            "course_id": 7349,
            "due_at": None,
            "submission_types": ["online_url"],
        },
    }

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[MOCK_OBSERVEES_RESPONSE[0]],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[{"id": 7349, "name": "AP US History", "workflow_state": "available"}],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[sub_late, sub_undated, sub_early],
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = hass.data["entity_components"]["todo"].get_entity(
        "todo.quentin_porter_assignments"
    )
    assert isinstance(entity, CanvasTodoListEntity)

    await entity.async_create_todo_item(
        TodoItem(summary="Mid Assignment", due=date(2026, 9, 5))
    )

    items = entity.todo_items or []
    summaries = [_base(i.summary) for i in items]
    assert summaries == [
        "[AP US History] Early Assignment",
        "Mid Assignment",
        "[AP US History] Late Assignment",
    ]

    # Undated never-submitted item is counted, not listed; compact lists exposed
    attrs = entity.extra_state_attributes or {}
    assert attrs["undated_count"] == 1
    listed = attrs["attention_items"] + attrs["upcoming_items"]
    assert all(
        {"class", "assignment", "status", "due", "url", "paper"} <= set(i)
        for i in listed
    )
    assert [i["due"] for i in attrs["attention_items"]] == sorted(
        i["due"] for i in attrs["attention_items"]
    )


async def test_todo_filters_in_class_and_paper_assignments(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that in-class activities and warm-ups are excluded, while paper and online homework are kept."""
    sub_paper: dict[str, Any] = {
        "id": 901,
        "user_id": 6021,
        "assignment_id": 201,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 201,
            "name": "Paper Worksheet",
            "course_id": 7349,
            "due_at": "2026-09-01T12:00:00Z",
            "submission_types": ["on_paper"],
        },
    }
    sub_none: dict[str, Any] = {
        "id": 902,
        "user_id": 6021,
        "assignment_id": 202,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 202,
            "name": "In Class Check-in",
            "course_id": 7349,
            "due_at": "2026-09-02T12:00:00Z",
            "submission_types": ["none"],
        },
    }
    sub_donow: dict[str, Any] = {
        "id": 903,
        "user_id": 6021,
        "assignment_id": 203,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 203,
            "name": "Do Now 8/19 and 8/21",
            "course_id": 7349,
            "due_at": "2026-09-02T12:00:00Z",
            "submission_types": ["online_text_entry"],
        },
    }
    sub_online: dict[str, Any] = {
        "id": 904,
        "user_id": 6021,
        "assignment_id": 204,
        "workflow_state": "unsubmitted",
        "assignment": {
            "id": 204,
            "name": "Online Homework Upload",
            "course_id": 7349,
            "due_at": "2026-09-03T12:00:00Z",
            "submission_types": ["online_upload"],
        },
    }

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[MOCK_OBSERVEES_RESPONSE[0]],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[{"id": 7349, "name": "AP US History", "workflow_state": "available"}],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[sub_paper, sub_none, sub_donow, sub_online],
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = hass.data["entity_components"]["todo"].get_entity(
        "todo.quentin_porter_assignments"
    )
    assert isinstance(entity, CanvasTodoListEntity)

    items = entity.todo_items or []
    summaries = [_base(i.summary) for i in items]
    assert summaries == [
        "[AP US History] Paper Worksheet",
        "[AP US History] Online Homework Upload",
    ]
