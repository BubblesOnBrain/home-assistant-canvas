"""Tier 4 Real-World Workload End-to-End Scenarios for Canvas LMS Integration.

This test module verifies the five comprehensive end-to-end workload scenarios
defined in TEST_INFRA.md:
1. Scenario 1: Dual-Student Parent Observer Workflow (Quentin & Theodore)
2. Scenario 2: Cloned Syllabus Assignment Purge (AP US History master template)
3. Scenario 3: Expired Zombie Course Purge (Counselor's Corner 2022 & English 9A)
4. Scenario 4: Direct Single-Student Login (Observees empty array fallback)
5. Scenario 5: Network Dropout & Token Expiration Recovery
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from custom_components.canvas.api import CanvasApiClient
from custom_components.canvas.const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.exceptions import (
    CanvasAuthError,
    CanvasConnectionError,
    CanvasError,
)
from custom_components.canvas.filtering import (
    filter_active_courses,
    filter_pending_assignments,
)
from custom_components.canvas.models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasData,
    CanvasObservee,
)

from .conftest import (
    TEST_ACCESS_TOKEN,
    TEST_BASE_URL,
    TEST_USER_ID,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_observee_dict,
    build_mock_submission_dict,
    build_mock_teacher_dict,
    build_mock_term_dict,
    build_mock_user_dict,
)

_LOGGER = logging.getLogger(__name__)
FROZEN_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


async def async_fetch_coordinator_data(
    client: CanvasApiClient,
    now: datetime | None = None,
) -> CanvasData:
    """Execute the full end-to-end coordinator data update workflow."""
    user = await client.async_get_current_user()
    observees = await client.async_get_observees()
    if not observees:
        observees = [
            CanvasObservee(
                id=user.id,
                name=user.name,
                sortable_name=user.sortable_name,
                short_name=user.short_name,
            )
        ]

    courses_by_student: dict[int, list[CanvasCourse]] = {}
    assignments_by_student: dict[int, list[CanvasAssignment]] = {}

    for student in observees:
        raw_courses = await client.async_get_student_courses(student.id)
        active_courses = filter_active_courses(raw_courses, now=now)
        courses_by_student[student.id] = active_courses

        student_assignments: list[CanvasAssignment] = []
        for course in active_courses:
            raw_assignments = await client.async_get_student_assignments(
                course.id, student.id
            )
            pending_assignments = filter_pending_assignments(
                raw_assignments, course, now=now
            )
            student_assignments.extend(pending_assignments)
        assignments_by_student[student.id] = student_assignments

    return CanvasData(
        user=user,
        observees=tuple(observees),
        courses_by_student=courses_by_student,
        assignments_by_student=assignments_by_student,
    )


class E2ETestCoordinator(DataUpdateCoordinator[CanvasData]):
    """Test harness coordinator executing the Canvas LMS polling contract."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: CanvasApiClient,
        scan_interval: timedelta = timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    ) -> None:
        """Initialize test coordinator."""
        self.client = client
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=scan_interval,
            always_update=True,
        )

    async def _async_update_data(self) -> CanvasData:
        """Fetch data from Canvas LMS and handle error translation."""
        try:
            return await async_fetch_coordinator_data(self.client, now=FROZEN_NOW)
        except CanvasAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except CanvasConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except CanvasError as err:
            raise UpdateFailed(f"Canvas error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err


# ==============================================================================
# Scenario 1: Dual-Student Parent Observer Workflow (Quentin & Theodore)
# ==============================================================================


async def test_scenario_1_dual_student_parent_observer_workflow(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Scenario 1: Full dual-student parent observer lifecycle.

    - Parent account with 2 linked students (Quentin, ID: 101, Theodore, ID: 102).
    - Quentin has 4 courses (Calculus, English Lit, AP Biology, AP US History).
    - Theodore has 3 courses (Chemistry, Algebra II, Studio Art).
    - Diverse grading details, teacher metadata, and noisy items (graded & excused).
    - Verification: Multi-student isolation, correct grades, and filtered assignments.
    """
    parent_user_id = 12345
    quentin_id = 101
    theodore_id = 102

    # 1. Mock Parent User Self
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(
            user_id=parent_user_id,
            name="Allen Porter",
            primary_email="allen@example.edu",
        ),
        status=200,
    )

    # 2. Mock Observees list returning Quentin and Theodore
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[
            build_mock_observee_dict(
                observee_id=quentin_id,
                name="Quentin Porter",
                sortable_name="Porter, Quentin",
                short_name="Quentin",
                pronouns="He/Him",
            ),
            build_mock_observee_dict(
                observee_id=theodore_id,
                name="Theodore Porter",
                sortable_name="Porter, Theodore",
                short_name="Theodore",
                pronouns="He/Him",
            ),
        ],
        status=200,
    )

    # Fall 2026 Term
    term_fall_2026 = build_mock_term_dict(
        term_id=501,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
        workflow_state="active",
    )

    # 3. Mock Quentin's 4 Courses
    quentin_courses = [
        build_mock_course_dict(
            course_id=1001,
            name="AP Calculus BC",
            course_code="MATH-APCALC",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=501, display_name="Mr. Leonard Euler"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": quentin_id,
                    "enrollment_state": "active",
                    "computed_current_score": 95.5,
                    "computed_current_grade": "A",
                    "computed_final_score": 94.0,
                    "computed_final_grade": "A",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=1002,
            name="AP English Literature",
            course_code="ENG-APLIT",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(teacher_id=502, display_name="Ms. Jane Austen")
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": quentin_id,
                    "enrollment_state": "active",
                    "computed_current_score": 91.5,
                    "computed_current_grade": "A-",
                    "computed_final_score": 90.0,
                    "computed_final_grade": "A-",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=1003,
            name="AP Biology",
            course_code="BIO-AP",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=503, display_name="Dr. Charles Darwin"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": quentin_id,
                    "enrollment_state": "active",
                    "computed_current_score": 88.5,
                    "computed_current_grade": "B+",
                    "computed_final_score": 87.0,
                    "computed_final_grade": "B+",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=1004,
            name="AP US History",
            course_code="HIST-APUSH",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(teacher_id=504, display_name="Dr. Jon Asoulin")
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": quentin_id,
                    "enrollment_state": "active",
                    "computed_current_score": 96.0,
                    "computed_current_grade": "A",
                    "computed_final_score": 95.0,
                    "computed_final_grade": "A",
                }
            ],
        ),
    ]

    quentin_courses_endpoint = ENDPOINT_USER_COURSES.format(user_id=quentin_id)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{quentin_courses_endpoint}",
        json=quentin_courses,
        status=200,
    )

    # 4. Mock Theodore's 3 Courses
    theodore_courses = [
        build_mock_course_dict(
            course_id=2001,
            name="Honors Chemistry",
            course_code="CHEM-HON",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=601, display_name="Mr. Dmitri Mendeleev"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": theodore_id,
                    "enrollment_state": "active",
                    "computed_current_score": 84.0,
                    "computed_current_grade": "B",
                    "computed_final_score": 83.5,
                    "computed_final_grade": "B",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=2002,
            name="Algebra II",
            course_code="MATH-ALG2",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=602, display_name="Mrs. Ada Lovelace"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": theodore_id,
                    "enrollment_state": "active",
                    "computed_current_score": 93.0,
                    "computed_current_grade": "A",
                    "computed_final_score": 92.0,
                    "computed_final_grade": "A",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=2003,
            name="Studio Art 2D",
            course_code="ART-2D",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(teacher_id=603, display_name="Mr. Claude Monet")
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": theodore_id,
                    "enrollment_state": "active",
                    "computed_current_score": 98.0,
                    "computed_current_grade": "A+",
                    "computed_final_score": 97.5,
                    "computed_final_grade": "A+",
                }
            ],
        ),
    ]

    theodore_courses_endpoint = ENDPOINT_USER_COURSES.format(user_id=theodore_id)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{theodore_courses_endpoint}",
        json=theodore_courses,
        status=200,
    )

    # 5. Mock Quentin's Submissions / Assignments per course
    # Course 1001: 1 active assignment, 1 graded assignment (noise)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=1001)}",
        json=[
            build_mock_submission_dict(
                submission_id=10001,
                assignment_id=1101,
                user_id=quentin_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=1101,
                    course_id=1001,
                    name="Derivatives Problem Set 1",
                    due_at="2026-09-10T23:59:59Z",
                    points_possible=20.0,
                ),
            ),
            build_mock_submission_dict(
                submission_id=10002,
                assignment_id=1102,
                user_id=quentin_id,
                workflow_state="graded",
                score=48.5,
                grade="A",
                assignment=build_mock_assignment_dict(
                    assignment_id=1102,
                    course_id=1001,
                    name="Limits Diagnostic Test",
                    due_at="2026-08-20T23:59:59Z",
                    points_possible=50.0,
                ),
            ),
        ],
        status=200,
    )

    # Course 1002: 1 active assignment
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=1002)}",
        json=[
            build_mock_submission_dict(
                submission_id=10003,
                assignment_id=1201,
                user_id=quentin_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=1201,
                    course_id=1002,
                    name="Hamlet Act 1 Essay",
                    due_at="2026-09-15T23:59:59Z",
                    points_possible=50.0,
                ),
            )
        ],
        status=200,
    )

    # Course 1003: 1 active assignment
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=1003)}",
        json=[
            build_mock_submission_dict(
                submission_id=10004,
                assignment_id=1301,
                user_id=quentin_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=1301,
                    course_id=1003,
                    name="Cellular Respiration Lab Report",
                    due_at="2026-09-20T23:59:59Z",
                    points_possible=30.0,
                ),
            )
        ],
        status=200,
    )

    # Course 1004: 1 active assignment
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=1004)}",
        json=[
            build_mock_submission_dict(
                submission_id=10005,
                assignment_id=1401,
                user_id=quentin_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=1401,
                    course_id=1004,
                    name="Colonial DBQ Analysis",
                    due_at="2026-09-25T23:59:59Z",
                    points_possible=40.0,
                ),
            )
        ],
        status=200,
    )

    # 6. Mock Theodore's Submissions / Assignments per course
    # Course 2001: 1 active assignment, 1 excused assignment (noise)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=2001)}",
        json=[
            build_mock_submission_dict(
                submission_id=20001,
                assignment_id=2101,
                user_id=theodore_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=2101,
                    course_id=2001,
                    name="Stoichiometry Worksheet",
                    due_at="2026-09-12T23:59:59Z",
                    points_possible=15.0,
                ),
            ),
            build_mock_submission_dict(
                submission_id=20002,
                assignment_id=2102,
                user_id=theodore_id,
                workflow_state="unsubmitted",
                excused=True,
                assignment=build_mock_assignment_dict(
                    assignment_id=2102,
                    course_id=2001,
                    name="Lab Safety Quiz",
                    due_at="2026-08-18T23:59:59Z",
                    points_possible=10.0,
                ),
            ),
        ],
        status=200,
    )

    # Course 2002: 1 active assignment
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=2002)}",
        json=[
            build_mock_submission_dict(
                submission_id=20003,
                assignment_id=2201,
                user_id=theodore_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=2201,
                    course_id=2002,
                    name="Quadratics Review",
                    due_at="2026-09-14T23:59:59Z",
                    points_possible=25.0,
                ),
            )
        ],
        status=200,
    )

    # Course 2003: 1 active assignment, 1 non-graded placeholder (noise)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=2003)}",
        json=[
            build_mock_submission_dict(
                submission_id=20004,
                assignment_id=2301,
                user_id=theodore_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=2301,
                    course_id=2003,
                    name="Perspective Sketchbook",
                    due_at="2026-09-18T23:59:59Z",
                    points_possible=20.0,
                ),
            ),
            build_mock_submission_dict(
                submission_id=20005,
                assignment_id=2302,
                user_id=theodore_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=2302,
                    course_id=2003,
                    name="Syllabus Acknowledgement",
                    grading_type="not_graded",
                    points_possible=0.0,
                ),
            ),
        ],
        status=200,
    )

    # Execute Coordinator Refresh
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(parent_user_id),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    # Verification 1: Coordinator state container
    assert coordinator.last_update_success is True
    data: CanvasData = coordinator.data
    assert isinstance(data, CanvasData)

    # Verification 2: Parent User Profile
    assert data.user.id == parent_user_id
    assert data.user.name == "Allen Porter"
    assert data.user.primary_email == "allen@example.edu"

    # Verification 3: Observees isolation & parsing
    assert len(data.observees) == 2
    obs_ids = {obs.id for obs in data.observees}
    assert obs_ids == {quentin_id, theodore_id}

    quentin_obs = next(obs for obs in data.observees if obs.id == quentin_id)
    theodore_obs = next(obs for obs in data.observees if obs.id == theodore_id)
    assert quentin_obs.name == "Quentin Porter"
    assert theodore_obs.name == "Theodore Porter"

    # Verification 4: Course breakdown and grade validation
    assert len(data.courses_by_student[quentin_id]) == 4
    assert len(data.courses_by_student[theodore_id]) == 3

    # Quentin Grades
    q_courses = {c.id: c for c in data.courses_by_student[quentin_id]}
    assert q_courses[1001].primary_grade is not None
    assert q_courses[1001].primary_grade.current_score == 95.5
    assert q_courses[1001].primary_grade.current_grade == "A"
    assert q_courses[1001].primary_teacher is not None
    assert q_courses[1001].primary_teacher.display_name == "Mr. Leonard Euler"

    assert q_courses[1002].primary_grade is not None
    assert q_courses[1002].primary_grade.current_score == 91.5
    assert q_courses[1002].primary_grade.current_grade == "A-"

    assert q_courses[1003].primary_grade is not None
    assert q_courses[1003].primary_grade.current_score == 88.5
    assert q_courses[1003].primary_grade.current_grade == "B+"

    assert q_courses[1004].primary_grade is not None
    assert q_courses[1004].primary_grade.current_score == 96.0
    assert q_courses[1004].primary_grade.current_grade == "A"

    # Theodore Grades
    t_courses = {c.id: c for c in data.courses_by_student[theodore_id]}
    assert t_courses[2001].primary_grade is not None
    assert t_courses[2001].primary_grade.current_score == 84.0
    assert t_courses[2001].primary_grade.current_grade == "B"
    assert t_courses[2001].primary_teacher is not None
    assert t_courses[2001].primary_teacher.display_name == "Mr. Dmitri Mendeleev"

    assert t_courses[2002].primary_grade is not None
    assert t_courses[2002].primary_grade.current_score == 93.0
    assert t_courses[2002].primary_grade.current_grade == "A"

    assert t_courses[2003].primary_grade is not None
    assert t_courses[2003].primary_grade.current_score == 98.0
    assert t_courses[2003].primary_grade.current_grade == "A+"

    # Verification 5: Actionable To-Do Assignment Filtering
    # Quentin: 4 open assignments plus graded 1102, retained as completed
    # because it was due within DONE_RETENTION_DAYS
    q_assignments = data.assignments_by_student[quentin_id]
    q_asg_ids = {a.id for a in q_assignments}
    assert q_asg_ids == {1101, 1102, 1201, 1301, 1401}

    # Theodore: 3 open assignments plus recently excused 2102 (retained as
    # completed); not_graded 2302 filtered out
    t_assignments = data.assignments_by_student[theodore_id]
    t_asg_ids = {a.id for a in t_assignments}
    assert t_asg_ids == {2101, 2102, 2201, 2301}
    assert 2302 not in t_asg_ids

    # Verification 6: Complete cross-student isolation
    assert q_asg_ids.isdisjoint(t_asg_ids)
    assert set(q_courses.keys()).isdisjoint(set(t_courses.keys()))


# ==============================================================================
# Scenario 2: Cloned Syllabus Assignment Purge
# ==============================================================================


async def test_scenario_2_cloned_syllabus_assignment_purge(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Scenario 2: Cloned syllabus assignment purge in AP US History.

    - AP US History course with term start date 2026-08-15.
    - Teacher cloned an old master template containing 15 historical assignments
      with due dates in 2023, 2024, and early 2025.
    - 3 authentic assignments for Fall 2026 (September/October).
    - Verification: All 15 cloned items are purged; only the 3 valid 2026 items remain.
    """
    student_id = 101
    course_id = 7349

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(user_id=TEST_USER_ID),
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[build_mock_observee_dict(observee_id=student_id)],
        status=200,
    )

    term_fall_2026 = build_mock_term_dict(
        term_id=413,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
    )

    course_apush = build_mock_course_dict(
        course_id=course_id,
        name="AP US History",
        course_code="APUSH-101",
        term=term_fall_2026,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=[course_apush],
        status=200,
    )

    # 15 historical cloned assignments from master syllabus template (2023-2025)
    historical_due_dates = [
        ("2023 Summer Reading Guide", "2023-08-10T23:59:59Z"),
        ("2023 Unit 1 Primary Source", "2023-09-05T23:59:59Z"),
        ("2023 Unit 2 DBQ Essay", "2023-10-12T23:59:59Z"),
        ("2023 Midterm Exam Review", "2023-11-01T23:59:59Z"),
        ("2023 Final Term Project", "2023-12-15T23:59:59Z"),
        ("2024 Constitution Analysis", "2024-01-20T23:59:59Z"),
        ("2024 Civil War Debate Prep", "2024-02-18T23:59:59Z"),
        ("2024 Reconstruction Synthesis", "2024-03-25T23:59:59Z"),
        ("2024 Gilded Age DBQ", "2024-04-14T23:59:59Z"),
        ("2024 AP National Exam Review", "2024-05-02T23:59:59Z"),
        ("2024 Summer Prep Packet", "2024-08-01T23:59:59Z"),
        ("2024 Fall Unit 1 Review", "2024-09-10T23:59:59Z"),
        ("2025 Progressive Era Term Paper", "2025-02-15T23:59:59Z"),
        ("2025 WWI Propaganda Project", "2025-04-10T23:59:59Z"),
        ("2025 Pre-Term Readiness Quiz", "2025-08-10T23:59:59Z"),
    ]

    submissions_payload: list[dict[str, Any]] = []
    asg_id_counter = 100

    # Add 15 historical cloned assignments
    for name, due_date in historical_due_dates:
        asg_id_counter += 1
        submissions_payload.append(
            build_mock_submission_dict(
                submission_id=asg_id_counter * 10,
                assignment_id=asg_id_counter,
                user_id=student_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=asg_id_counter,
                    course_id=course_id,
                    name=name,
                    due_at=due_date,
                    points_possible=20.0,
                ),
            )
        )

    # Add 3 authentic 2026 assignments
    valid_2026_assignments = [
        (301, "Chapter 1 Reflection & Synthesis", "2026-09-01T23:59:59Z", 20.0),
        (302, "Colonial America DBQ Draft", "2026-09-22T23:59:59Z", 50.0),
        (303, "Unit 1 Exam Prep Worksheet", "2026-10-05T23:59:59Z", 30.0),
    ]

    for asg_id, name, due_date, points in valid_2026_assignments:
        submissions_payload.append(
            build_mock_submission_dict(
                submission_id=asg_id * 10,
                assignment_id=asg_id,
                user_id=student_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=asg_id,
                    course_id=course_id,
                    name=name,
                    due_at=due_date,
                    points_possible=points,
                ),
            )
        )

    assert len(submissions_payload) == 18

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=course_id)}",
        json=submissions_payload,
        status=200,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(TEST_USER_ID),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data: CanvasData = coordinator.data

    # Verify exactly 3 assignments remain
    student_assignments = data.assignments_by_student[student_id]
    assert len(student_assignments) == 3

    retained_ids = [a.id for a in student_assignments]
    assert retained_ids == [301, 302, 303]
    retained_names = [a.name for a in student_assignments]
    assert retained_names == [
        "Chapter 1 Reflection & Synthesis",
        "Colonial America DBQ Draft",
        "Unit 1 Exam Prep Worksheet",
    ]

    # Verify all 15 historical assignments were discarded
    for historical_id in range(101, 116):
        assert historical_id not in retained_ids


# ==============================================================================
# Scenario 3: Expired Zombie Course Purge
# ==============================================================================


async def test_scenario_3_expired_zombie_course_purge(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Scenario 3: Expired zombie course purge.

    - Student is enrolled in 5 courses according to the Canvas API:
      1. AP Biology (Active Fall 2026)
      2. AP Calculus BC (Active Fall 2026)
      3. AP World History (Active Fall 2026)
      4. Counselor's Corner 2022 (Term ended 2022-06-01) -> Zombie Course
      5. English 9A (Term ended 2025-12-15) -> Zombie Course
    - In Canvas API, both zombie courses still return active student enrollment state.
    - Verification: Zombie courses and their assignments are discarded; only 3 active courses retained.
    """
    student_id = 101

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(user_id=TEST_USER_ID),
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[build_mock_observee_dict(observee_id=student_id)],
        status=200,
    )

    term_fall_2026 = build_mock_term_dict(
        term_id=501,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
        workflow_state="active",
    )

    term_past_2022 = build_mock_term_dict(
        term_id=201,
        name="2021-2022 School Year",
        start_at="2021-08-15T00:00:00Z",
        end_at="2022-06-01T23:59:59Z",
        workflow_state="active",  # API marks term active even though date is expired
    )

    term_fall_2025 = build_mock_term_dict(
        term_id=301,
        name="Fall 2025",
        start_at="2025-08-15T00:00:00Z",
        end_at="2025-12-15T23:59:59Z",
        workflow_state="active",
    )

    # 3 active courses
    active_courses = [
        build_mock_course_dict(
            course_id=7001,
            name="AP Biology",
            term=term_fall_2026,
            enrollments=[{"enrollment_state": "active"}],
        ),
        build_mock_course_dict(
            course_id=7002,
            name="AP Calculus BC",
            term=term_fall_2026,
            enrollments=[{"enrollment_state": "active"}],
        ),
        build_mock_course_dict(
            course_id=7003,
            name="AP World History",
            term=term_fall_2026,
            enrollments=[{"enrollment_state": "active"}],
        ),
    ]

    # 2 zombie courses (unarchived, active enrollment in API, but past term dates)
    zombie_courses = [
        build_mock_course_dict(
            course_id=8001,
            name="Counselor's Corner 2022",
            term=term_past_2022,
            enrollments=[{"enrollment_state": "active"}],
        ),
        build_mock_course_dict(
            course_id=8002,
            name="English 9A",
            term=term_fall_2025,
            enrollments=[{"enrollment_state": "active"}],
        ),
    ]

    all_5_courses = active_courses + zombie_courses

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=all_5_courses,
        status=200,
    )

    # Mock assignments for active courses
    for c_id in (7001, 7002, 7003):
        aioclient_mock.get(
            f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=c_id)}",
            json=[
                build_mock_submission_dict(
                    submission_id=c_id * 10,
                    assignment_id=c_id + 100,
                    user_id=student_id,
                    workflow_state="unsubmitted",
                    assignment=build_mock_assignment_dict(
                        assignment_id=c_id + 100,
                        course_id=c_id,
                        name=f"Active Task {c_id}",
                        due_at="2026-09-15T23:59:59Z",
                    ),
                )
            ],
            status=200,
        )

    # Mock assignments for zombie courses (should never be processed into active tasks)
    for z_id in (8001, 8002):
        aioclient_mock.get(
            f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=z_id)}",
            json=[
                build_mock_submission_dict(
                    submission_id=z_id * 10,
                    assignment_id=z_id + 100,
                    user_id=student_id,
                    workflow_state="unsubmitted",
                    assignment=build_mock_assignment_dict(
                        assignment_id=z_id + 100,
                        course_id=z_id,
                        name=f"Zombie Task {z_id}",
                        due_at="2022-05-01T23:59:59Z",
                    ),
                )
            ],
            status=200,
        )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(TEST_USER_ID),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data: CanvasData = coordinator.data

    # Verification 1: Exactly 3 active courses retained
    courses = data.courses_by_student[student_id]
    assert len(courses) == 3
    course_ids = {c.id for c in courses}
    assert course_ids == {7001, 7002, 7003}
    assert 8001 not in course_ids
    assert 8002 not in course_ids

    # Verification 2: Only assignments from active courses are present
    assignments = data.assignments_by_student[student_id]
    assert len(assignments) == 3
    asg_course_ids = {a.course_id for a in assignments}
    assert asg_course_ids == {7001, 7002, 7003}


# ==============================================================================
# Scenario 4: Direct Single-Student Login
# ==============================================================================


async def test_scenario_4_direct_single_student_login(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Scenario 4: Direct student login (Observees list is empty).

    - Student logs in with personal token.
    - `/api/v1/users/self/observees` returns `[]`.
    - Verification: Integration falls back to self as observee, populating courses,
      grades, and assignments for the student user themselves.
    """
    student_user_id = 501
    student_name = "Alex Mercer"

    # User self profile
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(
            user_id=student_user_id,
            name=student_name,
            primary_email="alex@school.org",
            sortable_name="Mercer, Alex",
            short_name="Alex",
        ),
        status=200,
    )

    # Empty observees endpoint (direct student account)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[],
        status=200,
    )

    # 2 Enrolled courses for this student
    term_fall_2026 = build_mock_term_dict(
        term_id=501,
        name="Fall 2026",
        start_at="2026-08-15T00:00:00Z",
        end_at="2026-12-20T23:59:59Z",
    )

    courses = [
        build_mock_course_dict(
            course_id=9001,
            name="Computer Science 101",
            course_code="CS-101",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=701, display_name="Prof. Grace Hopper"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": student_user_id,
                    "enrollment_state": "active",
                    "computed_current_score": 97.0,
                    "computed_current_grade": "A",
                }
            ],
        ),
        build_mock_course_dict(
            course_id=9002,
            name="Physics Mechanics",
            course_code="PHYS-101",
            term=term_fall_2026,
            teachers=[
                build_mock_teacher_dict(
                    teacher_id=702, display_name="Prof. Richard Feynman"
                )
            ],
            enrollments=[
                {
                    "type": "student",
                    "role": "StudentEnrollment",
                    "user_id": student_user_id,
                    "enrollment_state": "active",
                    "computed_current_score": 89.0,
                    "computed_current_grade": "B+",
                }
            ],
        ),
    ]

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_user_id)}",
        json=courses,
        status=200,
    )

    # Assignments for both courses
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=9001)}",
        json=[
            build_mock_submission_dict(
                submission_id=90001,
                assignment_id=9101,
                user_id=student_user_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=9101,
                    course_id=9001,
                    name="Python Algorithm Lab 1",
                    due_at="2026-09-10T23:59:59Z",
                    points_possible=25.0,
                ),
            )
        ],
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=9002)}",
        json=[
            build_mock_submission_dict(
                submission_id=90002,
                assignment_id=9201,
                user_id=student_user_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=9201,
                    course_id=9002,
                    name="Kinematics Motion Worksheet",
                    due_at="2026-09-15T23:59:59Z",
                    points_possible=20.0,
                ),
            )
        ],
        status=200,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(student_user_id),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data: CanvasData = coordinator.data

    # Fallback observee creation check
    assert len(data.observees) == 1
    observee = data.observees[0]
    assert observee.id == student_user_id
    assert observee.name == student_name

    # Courses and assignments mapped to student ID
    assert len(data.courses_by_student[student_user_id]) == 2
    assert len(data.assignments_by_student[student_user_id]) == 2

    cs_course = data.courses_by_student[student_user_id][0]
    assert cs_course.name == "Computer Science 101"
    assert cs_course.primary_grade is not None
    assert cs_course.primary_grade.current_score == 97.0
    assert cs_course.primary_grade.current_grade == "A"


# ==============================================================================
# Scenario 5: Network Dropout & Token Expiration Recovery
# ==============================================================================


async def test_scenario_5_network_dropout_and_token_expiration_recovery(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Scenario 5: Network timeout, auth expiration, and re-authentication recovery.

    - Cycle 1 (Initial Setup): Successful refresh, coordinator data populated.
    - Cycle 2 (Network Dropout): Endpoint timeout -> raises UpdateFailed, data preserved.
    - Cycle 3 (Token Expired): HTTP 401 Unauthorized -> raises ConfigEntryAuthFailed.
    - Cycle 4 (Reauth Recovery): Updated token succeeds -> coordinator recovers with fresh data.
    """
    user_id = 12345
    student_id = 101
    initial_token = "valid_initial_token_123"
    new_token = "valid_renewed_token_456"

    # Setup initial mock routes
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(user_id=user_id, name="Test Observer"),
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[build_mock_observee_dict(observee_id=student_id, name="Student One")],
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=[
            build_mock_course_dict(
                course_id=5001,
                name="AP Physics",
                term=build_mock_term_dict(
                    start_at="2026-08-15T00:00:00Z", end_at="2026-12-20T23:59:59Z"
                ),
            )
        ],
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=5001)}",
        json=[
            build_mock_submission_dict(
                submission_id=50001,
                assignment_id=5101,
                user_id=student_id,
                assignment=build_mock_assignment_dict(
                    assignment_id=5101,
                    course_id=5001,
                    name="Newton Laws Lab",
                    due_at="2026-09-10T23:59:59Z",
                ),
            )
        ],
        status=200,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: initial_token},
        unique_id=str(user_id),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, initial_token, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)

    # ----------------------------------------------------------------------
    # Phase 1: Normal Initial Refresh
    # ----------------------------------------------------------------------
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    initial_data = coordinator.data
    assert initial_data is not None
    assert initial_data.user.id == user_id
    assert len(initial_data.courses_by_student[student_id]) == 1
    assert len(initial_data.assignments_by_student[student_id]) == 1

    # ----------------------------------------------------------------------
    # Phase 2: Network Timeout Dropout
    # ----------------------------------------------------------------------
    # Simulate network timeout on subsequent poll
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        exc=asyncio.TimeoutError("Connection timed out"),
    )

    await coordinator.async_refresh()

    # Polling fails gracefully with UpdateFailed
    assert coordinator.last_update_success is False
    assert coordinator.last_exception is not None
    assert isinstance(coordinator.last_exception, UpdateFailed)

    # Verify previous valid data is retained in memory
    assert coordinator.data is not None
    assert coordinator.data.user.id == user_id
    assert len(coordinator.data.courses_by_student[student_id]) == 1
    assert (
        coordinator.data.assignments_by_student[student_id][0].name == "Newton Laws Lab"
    )

    # ----------------------------------------------------------------------
    # Phase 3: Token Expiration (HTTP 401 Unauthorized)
    # ----------------------------------------------------------------------
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        text="Invalid access token",
        status=401,
    )

    # Polling raises ConfigEntryAuthFailed
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    # ----------------------------------------------------------------------
    # Phase 4: Successful Re-Authentication & Recovery
    # ----------------------------------------------------------------------
    # Update config entry and client with new access token
    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            CONF_BASE_URL: TEST_BASE_URL,
            CONF_ACCESS_TOKEN: new_token,
        },
    )

    # Re-register valid mock responses for renewed token
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(user_id=user_id, name="Test Observer"),
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[build_mock_observee_dict(observee_id=student_id, name="Student One")],
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=[
            build_mock_course_dict(
                course_id=5001,
                name="AP Physics C: Mechanics",
                term=build_mock_term_dict(
                    start_at="2026-08-15T00:00:00Z", end_at="2026-12-20T23:59:59Z"
                ),
            )
        ],
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=5001)}",
        json=[
            build_mock_submission_dict(
                submission_id=50002,
                assignment_id=5102,
                user_id=student_id,
                assignment=build_mock_assignment_dict(
                    assignment_id=5102,
                    course_id=5001,
                    name="Rotational Dynamics Quiz",
                    due_at="2026-09-28T23:59:59Z",
                ),
            )
        ],
        status=200,
    )

    # Update client access token
    client._access_token = new_token

    # Poll again
    await coordinator.async_refresh()

    # Recovery verified
    assert coordinator.last_update_success is True
    recovered_data = coordinator.data
    assert recovered_data is not None
    assert recovered_data.user.id == user_id
    assert (
        recovered_data.courses_by_student[student_id][0].name
        == "AP Physics C: Mechanics"
    )
    assert (
        recovered_data.assignments_by_student[student_id][0].name
        == "Rotational Dynamics Quiz"
    )


# ==============================================================================
# Adversarial Edge Cases & Multi-Student Stress Scenarios
# ==============================================================================


async def test_scenario_adversarial_escaping_and_special_characters(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Adversarial Verification 1: HTML escaping, unicode accents, and meta-characters.

    Tests that special characters in course titles, student names, teacher pronouns,
    and HTML formatted descriptions are safely parsed without escaping loss or crashes.
    """
    student_id = 999
    course_id = 8888

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(
            user_id=TEST_USER_ID,
            name="François Müller-L'Hôpital <admin@canvas>",
        ),
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[
            build_mock_observee_dict(
                observee_id=student_id,
                name="Zoë O'Connor & Sons — 100% 🚀",
                pronouns="they/them/iel",
            )
        ],
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=[
            build_mock_course_dict(
                course_id=course_id,
                name="Français Avancé: Littérature & Théâtre <script>alert(1)</script>",
                course_code="FR-301 & AP",
                term=build_mock_term_dict(
                    start_at="2026-08-15T00:00:00Z", end_at="2026-12-20T23:59:59Z"
                ),
                teachers=[
                    build_mock_teacher_dict(
                        teacher_id=404,
                        display_name="M. Jean-Luc Godard / Prof. d'Art",
                    )
                ],
                enrollments=[
                    {
                        "type": "student",
                        "role": "StudentEnrollment",
                        "user_id": student_id,
                        "enrollment_state": "active",
                        "computed_current_score": 99.99,
                        "computed_current_grade": "A+",
                    }
                ],
            )
        ],
        status=200,
    )

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=course_id)}",
        json=[
            build_mock_submission_dict(
                submission_id=77777,
                assignment_id=6666,
                user_id=student_id,
                workflow_state="unsubmitted",
                assignment=build_mock_assignment_dict(
                    assignment_id=6666,
                    course_id=course_id,
                    name="Dissertation: « L'Étranger » d'Albert Camus",
                    description="<p>Analysez le thème: <em>x &lt; 5 &amp; y &gt; 2</em></p>",
                    due_at="2026-09-30T23:59:59Z",
                    points_possible=100.0,
                ),
            )
        ],
        status=200,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(TEST_USER_ID),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data: CanvasData = coordinator.data

    assert data.user.name == "François Müller-L'Hôpital <admin@canvas>"
    assert data.observees[0].name == "Zoë O'Connor & Sons — 100% 🚀"
    assert (
        data.courses_by_student[student_id][0].name
        == "Français Avancé: Littérature & Théâtre <script>alert(1)</script>"
    )
    asg = data.assignments_by_student[student_id][0]
    assert asg.name == "Dissertation: « L'Étranger » d'Albert Camus"
    assert (
        asg.description == "<p>Analysez le thème: <em>x &lt; 5 &amp; y &gt; 2</em></p>"
    )


async def test_scenario_adversarial_zero_courses_and_empty_assignments(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Adversarial Verification 2: Boundary test with zero courses and empty assignments."""
    student_id = 200

    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}",
        json=build_mock_user_dict(user_id=TEST_USER_ID),
        status=200,
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[build_mock_observee_dict(observee_id=student_id)],
        status=200,
    )
    # Student has zero courses enrolled
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=student_id)}",
        json=[],
        status=200,
    )

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: TEST_BASE_URL, CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN},
        unique_id=str(TEST_USER_ID),
    )
    mock_entry.add_to_hass(hass)

    session = async_get_clientsession(hass)
    client = CanvasApiClient(TEST_BASE_URL, TEST_ACCESS_TOKEN, session)
    coordinator = E2ETestCoordinator(hass, mock_entry, client)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data = coordinator.data
    assert data.courses_by_student[student_id] == []
    assert data.assignments_by_student[student_id] == []
