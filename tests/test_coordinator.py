"""Comprehensive unit tests for the Canvas LMS DataUpdateCoordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiohttp
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.api import CanvasApiClient
from custom_components.canvas.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.models import CanvasData

from .conftest import (
    MOCK_COURSES_RESPONSE,
    MOCK_OBSERVEES_RESPONSE,
    MOCK_SUBMISSIONS_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_ACCESS_TOKEN,
    TEST_BASE_URL,
    TEST_USER_ID,
    TEST_USER_NAME,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
    build_mock_teacher_dict,
    build_mock_term_dict,
)

FROZEN_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _create_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> CanvasDataUpdateCoordinator:
    """Create a coordinator instance with a real CanvasApiClient."""
    session = async_get_clientsession(hass)
    client = CanvasApiClient(
        base_url=TEST_BASE_URL,
        access_token=TEST_ACCESS_TOKEN,
        session=session,
    )
    return CanvasDataUpdateCoordinator(
        hass=hass,
        client=client,
        entry=entry,
    )


# ============================================================================
# Tier 1: Core Feature Coverage Tests (>= 5 cases)
# ============================================================================


async def test_coordinator_initialization(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test coordinator initialization with 60-minute interval and metadata."""
    coordinator = _create_coordinator(hass, mock_config_entry)

    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    assert coordinator.update_interval == timedelta(minutes=60)
    assert coordinator.name == DOMAIN or coordinator.name == mock_config_entry.title
    assert coordinator.data is None


async def test_coordinator_update_data_dual_students_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test successful data update fetching user, observees, courses, and pending assignments."""
    # Active term (Fall 2026)
    active_term = build_mock_term_dict(
        term_id=413,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
        workflow_state="active",
    )
    # Expired past term (Fall 2024)
    past_term = build_mock_term_dict(
        term_id=200,
        name="Fall 2024",
        start_at="2024-08-15T00:00:00Z",
        end_at="2024-12-20T23:59:59Z",
        workflow_state="completed",
    )

    teacher = build_mock_teacher_dict(teacher_id=4857, display_name="Dr. Jon Asoulin")

    course_apush = build_mock_course_dict(
        course_id=7349,
        name="AP US History",
        course_code="APUSH-101",
        term=active_term,
        teachers=[teacher],
    )
    course_bio = build_mock_course_dict(
        course_id=7350,
        name="AP Biology",
        course_code="BIO-201",
        term=active_term,
        teachers=[teacher],
    )
    course_zombie = build_mock_course_dict(
        course_id=7000,
        name="Counselor's Corner 2024",
        course_code="COUNS-24",
        term=past_term,
        teachers=[],
    )

    # Assignments for APUSH (1 active, 1 cloned preterm, 1 graded)
    asg_active = build_mock_assignment_dict(
        assignment_id=134664,
        course_id=7349,
        name="Chapter 1 Reflection",
        due_at="2026-09-01T23:59:59Z",
    )
    asg_cloned_preterm = build_mock_assignment_dict(
        assignment_id=134665,
        course_id=7349,
        name="Historical Syllabus Acknowledgement (2023)",
        due_at="2023-08-20T23:59:59Z",
    )
    asg_graded = build_mock_assignment_dict(
        assignment_id=134666,
        course_id=7349,
        name="Summer Reading Quiz",
        due_at="2026-08-25T23:59:59Z",
    )

    sub_active = build_mock_submission_dict(
        submission_id=5196766,
        assignment_id=134664,
        user_id=6021,
        workflow_state="unsubmitted",
        assignment=asg_active,
    )
    sub_cloned = build_mock_submission_dict(
        submission_id=5196767,
        assignment_id=134665,
        user_id=6021,
        workflow_state="unsubmitted",
        assignment=asg_cloned_preterm,
    )
    sub_graded = build_mock_submission_dict(
        submission_id=5196768,
        assignment_id=134666,
        user_id=6021,
        workflow_state="graded",
        grade="A",
        score=20.0,
        graded_at="2026-08-24T10:00:00Z",
        assignment=asg_graded,
    )

    # Register fake routes
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE,
    )
    # Student 6021 (Quentin): 2 active courses, 1 zombie course
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[course_apush, course_bio, course_zombie],
    )
    # Student 4899 (Theodore): 1 active course
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=4899)}",
        json=[course_apush],
    )
    # Submissions for APUSH (6021)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[sub_active, sub_cloned, sub_graded],
    )
    # Submissions for AP Biology (6021)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7350)}",
        json=[],
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.canvas.filtering.datetime",
            type(
                "MockDateTime",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None: FROZEN_NOW)},
            ),
        )
        data = await coordinator._async_update_data()

    assert isinstance(data, CanvasData)
    assert data.user.id == TEST_USER_ID
    assert data.user.name == TEST_USER_NAME
    assert len(data.observees) == 2
    assert data.observees[0].id == 6021
    assert data.observees[1].id == 4899

    # Verify zombie course was filtered out
    assert 6021 in data.courses_by_student
    quentin_courses = data.courses_by_student[6021]
    assert len(quentin_courses) == 2
    assert {c.id for c in quentin_courses} == {7349, 7350}

    assert 4899 in data.courses_by_student
    assert len(data.courses_by_student[4899]) == 1

    # Verify cloned preterm assignment was filtered out; the recently graded
    # quiz is retained (shown as completed) within DONE_RETENTION_DAYS
    assert 6021 in data.assignments_by_student
    quentin_assignments = data.assignments_by_student[6021]
    assert {a.id for a in quentin_assignments} == {134664, 134666}


async def test_coordinator_observee_fallback_single_student(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test observee fallback when user has no linked observees (treats self as student)."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[],  # No observees
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=TEST_USER_ID)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=MOCK_SUBMISSIONS_RESPONSE,
    )

    coordinator = _create_coordinator(hass, mock_config_entry)
    data = await coordinator._async_update_data()

    assert len(data.observees) == 1
    assert data.observees[0].id == TEST_USER_ID
    assert data.observees[0].name == TEST_USER_NAME
    assert TEST_USER_ID in data.courses_by_student
    assert len(data.courses_by_student[TEST_USER_ID]) == 1


async def test_coordinator_auth_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that authentication error is translated to ConfigEntryAuthFailed."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=401,
        text="Token expired (HTTP 401)",
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_connection_error_raises_update_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that connection error is translated to UpdateFailed."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=aiohttp.ClientError("Server unreachable"),
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_unexpected_error_raises_update_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that server 500 error raises UpdateFailed."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=500,
        text="Internal Server Error",
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_polling_cadence_60_minutes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that advancing time by 60 minutes triggers coordinator update."""
    freezer.move_to(FROZEN_NOW)

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE[:1],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=MOCK_SUBMISSIONS_RESPONSE,
    )

    coordinator = _create_coordinator(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    # Initial refresh
    await coordinator.async_refresh()
    initial_call_count = len(aioclient_mock.mock_calls)
    assert initial_call_count >= 1

    # Advance time by 30 minutes: no new poll
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == initial_call_count

    # Advance time by another 30 minutes (total 60 min): triggers update
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) > initial_call_count
    unsub()


# ============================================================================
# Tier 2: Boundary, Corner & Adversarial Cases (>= 5 cases)
# ============================================================================


async def test_coordinator_rate_limit_error_raises_update_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that rate limit error is translated to UpdateFailed."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        status=429,
        text="Rate limit exceeded",
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_response_error_raises_update_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that invalid non-JSON payload raises UpdateFailed."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        text="<html>Bad Gateway</html>",
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_student_with_zero_courses(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test coordinator handles student with zero enrolled courses gracefully."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE[:1],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[],
    )

    coordinator = _create_coordinator(hass, mock_config_entry)
    data = await coordinator._async_update_data()

    assert 6021 in data.courses_by_student
    assert data.courses_by_student[6021] == []
    assert data.assignments_by_student.get(6021, []) == []


async def test_coordinator_course_with_zero_assignments(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test coordinator handles course with zero assignments gracefully."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE[:1],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[],
    )

    coordinator = _create_coordinator(hass, mock_config_entry)
    data = await coordinator._async_update_data()

    assert len(data.courses_by_student[6021]) == 1
    assert data.assignments_by_student.get(6021, []) == []


async def test_coordinator_course_fetch_partial_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test error raised when fetching student courses fails mid-update."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        exc=aiohttp.ClientError("Timeout fetching courses"),
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_submissions_fetch_partial_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test error raised when fetching student submissions fails mid-update."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=MOCK_USER_SELF_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=MOCK_OBSERVEES_RESPONSE[:1],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=MOCK_COURSES_RESPONSE,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        exc=aiohttp.ClientError("Timeout fetching submissions"),
    )

    coordinator = _create_coordinator(hass, mock_config_entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_skips_course_forbidden_to_observer(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 on one course (e.g. a club hub) is skipped, not an auth failure."""
    active_term = build_mock_term_dict(
        term_id=413,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
        workflow_state="active",
    )
    course_ok = build_mock_course_dict(
        course_id=7349, name="AP US History", term=active_term
    )
    course_club = build_mock_course_dict(
        course_id=7351, name="SOA Club Hub", term=active_term
    )
    asg = build_mock_assignment_dict(
        assignment_id=134664,
        course_id=7349,
        name="Chapter 1 Reflection",
        due_at="2026-09-01T23:59:59Z",
    )
    sub = build_mock_submission_dict(
        submission_id=1,
        assignment_id=134664,
        user_id=6021,
        workflow_state="unsubmitted",
        assignment=asg,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}", json=MOCK_USER_SELF_RESPONSE
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}", json=[MOCK_OBSERVEES_RESPONSE[0]]
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}",
        json=[course_ok, course_club],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[sub],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7351)}",
        status=403,
        text='{"status":"unauthorized","errors":[{"message":"user not authorized to perform that action"}]}',
    )

    coordinator = _create_coordinator(hass, mock_config_entry)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.canvas.filtering.datetime",
            type(
                "MockDateTime",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None: FROZEN_NOW)},
            ),
        )
        data = await coordinator._async_update_data()

    assert {c.id for c in data.courses_by_student[6021]} == {7349, 7351}
    assert [a.id for a in data.assignments_by_student[6021]] == [134664]
    assert "Skipping course 7351 (SOA Club Hub)" in caplog.text
