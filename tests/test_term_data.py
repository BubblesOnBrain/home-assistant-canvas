"""Tests for term-wide assignment data and assignment groups."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    ENDPOINT_COURSE_ASSIGNMENT_GROUPS,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.filtering import (
    is_active_todo_assignment,
    is_term_assignment,
)
from custom_components.canvas.models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasSubmission,
)

from .conftest import (
    MOCK_OBSERVEES_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def test_assignment_parses_group_and_quiz_flag() -> None:
    """Group id and quiz flags are read from the Canvas payload."""
    quiz = CanvasAssignment.from_dict(
        {"id": 1, "assignment_group_id": 77, "is_quiz_assignment": True}
    )
    assert quiz.assignment_group_id == 77
    assert quiz.is_quiz_assignment is True
    online = CanvasAssignment.from_dict({"id": 2, "submission_types": ["online_quiz"]})
    assert online.is_quiz_assignment is True
    plain = CanvasAssignment.from_dict({"id": 3, "submission_types": ["on_paper"]})
    assert plain.assignment_group_id is None
    assert plain.is_quiz_assignment is False


def test_term_assignment_keeps_old_finished_work() -> None:
    """Finished work stays in the term list after leaving the to-do list."""
    course = CanvasCourse(id=1, name="Physics")
    graded_long_ago = CanvasAssignment(
        id=5,
        course_id=1,
        name="Lab",
        due_at=NOW - timedelta(days=30),
        submission=CanvasSubmission(
            id=1,
            assignment_id=5,
            user_id=1,
            submitted_at=NOW - timedelta(days=31),
            score=9,
            graded_at=NOW - timedelta(days=20),
        ),
    )
    assert is_term_assignment(graded_long_ago, course, now=NOW) is True
    assert is_active_todo_assignment(graded_long_ago, course, now=NOW) is False
    draft = CanvasAssignment(
        id=6, course_id=1, name="Draft", workflow_state="unpublished"
    )
    assert is_term_assignment(draft, course, now=NOW) is False


def _routes(aioclient_mock: AiohttpClientMocker, groups_status: int = 200) -> None:
    course = build_mock_course_dict(course_id=7349, name="Physics H - Cantlin")
    recent = build_mock_assignment_dict(
        assignment_id=1,
        course_id=7349,
        name="Quiz: Mass & Weight",
        due_at="2026-10-02T02:00:00Z",
        assignment_group_id=11,
    )
    old = build_mock_assignment_dict(
        assignment_id=2,
        course_id=7349,
        name="Dynamics Practice",
        due_at="2026-09-01T02:00:00Z",
        assignment_group_id=12,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}", json=MOCK_USER_SELF_RESPONSE
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}", json=[MOCK_OBSERVEES_RESPONSE[0]]
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}", json=[course]
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[
            build_mock_submission_dict(
                submission_id=1, assignment_id=1, assignment=recent
            ),
            build_mock_submission_dict(
                submission_id=2,
                assignment_id=2,
                assignment=old,
                submitted_at="2026-08-31T20:00:00Z",
                score=8.0,
                graded_at="2026-09-05T20:00:00Z",
                workflow_state="graded",
            ),
        ],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_ASSIGNMENT_GROUPS.format(course_id=7349)}",
        status=groups_status,
        json=[{"id": 11, "name": "Quizzes"}, {"id": 12, "name": "Homework"}],
    )


async def test_coordinator_collects_term_work_and_groups(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Old graded work is kept for the term; group names are cached."""
    freezer.move_to(NOW)
    _routes(aioclient_mock)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: CanvasDataUpdateCoordinator = mock_config_entry.runtime_data
    data = coordinator.data

    assert [a.id for a in data.assignments_by_student[6021]] == [1]
    assert sorted(a.id for a in data.term_assignments(6021)) == [1, 2]
    quiz = data.assignments_by_student[6021][0]
    assert data.group_name(quiz) == "Quizzes"
    assert data.assignment_groups == {7349: {11: "Quizzes", 12: "Homework"}}

    group_calls = [
        c for c in aioclient_mock.mock_calls if "assignment_groups" in str(c[1])
    ]
    await coordinator.async_refresh()
    again = [c for c in aioclient_mock.mock_calls if "assignment_groups" in str(c[1])]
    assert len(again) == len(group_calls) == 1  # cached

    freezer.tick(timedelta(hours=13))
    await coordinator.async_refresh()
    later = [c for c in aioclient_mock.mock_calls if "assignment_groups" in str(c[1])]
    assert len(later) == 2


async def test_group_fetch_failure_does_not_fail_update(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 403 on assignment groups leaves group names empty."""
    freezer.move_to(NOW)
    _routes(aioclient_mock, groups_status=403)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    data = mock_config_entry.runtime_data.data
    assert data.assignment_groups == {7349: {}}
    assert len(data.term_assignments(6021)) == 2
