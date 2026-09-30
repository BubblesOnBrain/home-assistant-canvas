"""Tests for the dashboard lists: assessments, waiting, review and closed."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.auth.models import User

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    DOMAIN,
    ENDPOINT_COURSE_ASSIGNMENT_GROUPS,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.todo import CanvasTodoListEntity

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
QUIZZES, HOMEWORK, TESTS = 11, 12, 13

MISSING = "2001"
UPCOMING = "2002"
QUIZ_SOON = "2003"
TEST_LATER = "2004"
PAPER_QUIZ_TAKEN = "2005"
SUBMITTED = "2006"
LOW_GRADE = "2007"
OLD_GRADE = "2008"
OFF_LIST_GRADE = "2009"


def _sub(
    aid: str,
    due: str,
    *,
    name: str | None = None,
    group: int = HOMEWORK,
    types: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    asg = build_mock_assignment_dict(
        assignment_id=int(aid),
        course_id=7349,
        name=name or f"Assignment {aid}",
        due_at=due,
        lock_at=None,
        unlock_at=None,
        points_possible=20.0,
        submission_types=types or ["online_upload"],
        html_url=f"{TEST_BASE_URL}/courses/7349/assignments/{aid}",
        assignment_group_id=group,
    )
    return build_mock_submission_dict(
        submission_id=int(aid), assignment_id=int(aid), assignment=asg, **kwargs
    )


def _graded(aid: str, due: str, score: float, graded_at: str) -> dict[str, Any]:
    return _sub(
        aid,
        due,
        submitted_at=due,
        score=score,
        grade=str(int(score)),
        graded_at=graded_at,
        workflow_state="graded",
    )


DEFAULT_SUBS = [
    _sub(MISSING, "2026-09-28T03:59:00Z"),
    _sub(UPCOMING, "2026-10-01T03:59:00Z"),
    _sub(
        QUIZ_SOON,
        "2026-10-01T15:00:00Z",
        name="Chapter 5 Quiz",
        group=QUIZZES,
        types=["on_paper"],
    ),
    _sub(
        TEST_LATER,
        "2026-10-09T15:00:00Z",
        name="Unit 2 Test",
        group=TESTS,
        types=["on_paper"],
    ),
    _sub(
        PAPER_QUIZ_TAKEN,
        "2026-09-26T15:00:00Z",
        name="Quiz: Newton's Second Law",
        group=QUIZZES,
        types=["on_paper"],
    ),
    _sub(SUBMITTED, "2026-09-27T03:59:00Z", submitted_at="2026-09-27T01:00:00Z"),
    _graded(LOW_GRADE, "2026-09-25T03:59:00Z", 14.0, "2026-09-28T15:00:00Z"),
    _graded(OLD_GRADE, "2026-09-01T03:59:00Z", 18.0, "2026-09-05T15:00:00Z"),
    _graded(OFF_LIST_GRADE, "2026-09-10T03:59:00Z", 19.0, "2026-09-20T15:00:00Z"),
]


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
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_ASSIGNMENT_GROUPS.format(course_id=7349)}",
        json=[
            {"id": QUIZZES, "name": "Quizzes"},
            {"id": HOMEWORK, "name": "Homework"},
            {"id": TESTS, "name": "Tests"},
        ],
    )


@pytest.fixture(name="coordinator")
async def fixture_coordinator(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> CanvasDataUpdateCoordinator:
    """Set up one student with work in every list."""
    freezer.move_to(NOW)
    _routes(aioclient_mock, DEFAULT_SUBS)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry.runtime_data


def _attrs(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.states.get(ENTITY)
    assert state is not None
    return dict(state.attributes)


def _uids(hass: HomeAssistant, name: str) -> list[str]:
    return [i["uid"] for i in _attrs(hass)[f"{name}_items"]]


def _find(hass: HomeAssistant, name: str, uid: str) -> dict[str, Any]:
    return next(i for i in _attrs(hass)[f"{name}_items"] if i["uid"] == uid)


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], **kwargs: Any
) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True, **kwargs)
    await hass.async_block_till_done()


async def test_work_sorted_into_lists(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Each piece of work lands in exactly one list."""
    assert _uids(hass, "attention") == [MISSING]
    assert _uids(hass, "upcoming") == [UPCOMING]
    # The test is 10 days out, beyond the 7-day test lead time.
    assert _uids(hass, "assessment") == [QUIZ_SOON]
    # A paper quiz Canvas calls missing was taken in class: waiting on a grade.
    assert _uids(hass, "waiting") == [PAPER_QUIZ_TAKEN, SUBMITTED]
    # Graded before review started (14 days back) counts as reviewed.
    assert _uids(hass, "review") == [LOW_GRADE, OFF_LIST_GRADE]
    assert _uids(hass, "parent_review") == []
    assert _uids(hass, "closed") == []

    attrs = _attrs(hass)
    assert attrs["attention_count"] == 1
    assert attrs["waiting_count"] == 2
    assert attrs["review_count"] == 2
    assert attrs["assessment_count"] == 1
    assert attrs["escalated_count"] == 1

    low = _find(hass, "review", LOW_GRADE)
    assert low["percent"] == 70.0
    assert low["escalated"] is True
    assert low["score"] == 14.0
    assert low["category"] == "Homework"
    assert low["kind"] == "homework"
    assert _find(hass, "review", OFF_LIST_GRADE)["escalated"] is False
    waiting = _find(hass, "waiting", PAPER_QUIZ_TAKEN)
    assert waiting["kind"] == "quiz"
    assert waiting["display_state"] == "awaiting_grade"


async def test_todo_items_only_for_list_work(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Old graded work shows in review but not as a to-do item."""
    entity = hass.data["entity_components"]["todo"].get_entity(ENTITY)
    assert isinstance(entity, CanvasTodoListEntity)
    items = {i.uid: i for i in entity.todo_items or []}
    assert OFF_LIST_GRADE not in items
    assert OLD_GRADE not in items
    assert items[QUIZ_SOON].summary.startswith("📚 QUIZ · ")
    assert items[PAPER_QUIZ_TAKEN].summary.startswith("⏳ AWAITING GRADE · ")
    assert items[PAPER_QUIZ_TAKEN].status == "needs_action"


async def test_review_moves_through_student_then_parent(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    hass_read_only_user: User,
    hass_admin_user: User,
) -> None:
    """The student's review moves it to parent review; the parent's finishes it."""
    await _call(
        hass,
        "review_assignment",
        {"assignment_id": LOW_GRADE},
        context=Context(user_id=hass_read_only_user.id),
    )
    assert _uids(hass, "review") == [OFF_LIST_GRADE]
    item = _find(hass, "parent_review", LOW_GRADE)
    assert item["student_reviewed_by"] == hass_read_only_user.name
    assert _attrs(hass)["escalated_count"] == 1

    await _call(
        hass,
        "review_assignment",
        {"assignment_id": LOW_GRADE, "role": "parent"},
        context=Context(user_id=hass_admin_user.id),
    )
    assert _uids(hass, "parent_review") == []
    assert _attrs(hass)["escalated_count"] == 0

    # Reviewing an item that's off the to-do list works too.
    await _call(hass, "review_assignment", {"assignment_id": OFF_LIST_GRADE})
    assert _uids(hass, "parent_review") == [OFF_LIST_GRADE]


async def test_close_without_grade_then_grade_reopens(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Closed work leaves waiting; a grade arriving later puts it in review."""
    await _call(
        hass,
        "close_assignment",
        {"assignment_id": SUBMITTED, "reason": "feedback_received", "note": "Talked"},
    )
    assert _uids(hass, "waiting") == [PAPER_QUIZ_TAKEN]
    closed = _find(hass, "closed", SUBMITTED)
    assert closed["closed_reason"] == "feedback_received"
    assert closed["closed_note"] == "Talked"

    subs = [s for s in DEFAULT_SUBS if str(s["assignment_id"]) != SUBMITTED] + [
        _graded(SUBMITTED, "2026-09-27T03:59:00Z", 20.0, "2026-09-29T11:00:00Z")
    ]
    _routes(aioclient_mock, subs)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _uids(hass, "closed") == []
    assert SUBMITTED in _uids(hass, "review")


async def test_kind_override_moves_work(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Correcting the kind moves homework to assessments, and back with auto."""
    await _call(
        hass, "set_assignment_stage", {"assignment_id": UPCOMING, "kind": "quiz"}
    )
    assert UPCOMING in _uids(hass, "assessment")
    assert _find(hass, "assessment", UPCOMING)["kind_override"] is True
    await _call(
        hass, "set_assignment_stage", {"assignment_id": UPCOMING, "kind": "auto"}
    )
    assert _uids(hass, "upcoming") == [UPCOMING]


async def test_lead_time_option(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A longer test lead time brings the test into assessments."""
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"test_lead_days": 14}
    )
    await hass.async_block_till_done()
    assert TEST_LATER in _uids(hass, "assessment")


async def test_escalation_threshold_option(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A lower threshold stops flagging a 70%."""
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"escalation_threshold": 70}
    )
    await hass.async_block_till_done()
    assert _find(hass, "review", LOW_GRADE)["escalated"] is False
    assert _attrs(hass)["escalated_count"] == 0
