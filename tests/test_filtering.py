"""Comprehensive unit tests for the Canvas LMS noise filtering engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.canvas.filtering import (
    OPEN_STATUSES,
    SubmissionStatus,
    _to_utc,
    filter_active_courses,
    filter_pending_assignments,
    is_actionable_todo_assignment,
    is_active_course,
    is_active_todo_assignment,
    is_done,
    is_in_class_activity,
    is_online_submission_assignment,
    submission_status,
)
from custom_components.canvas.models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasEnrollment,
    CanvasGrade,
    CanvasSubmission,
    CanvasTerm,
)

FROZEN_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_to_utc_helper() -> None:
    """Test datetime UTC normalization helper."""
    assert _to_utc(None) is None

    naive_dt = datetime(2026, 8, 24, 12, 0, 0)
    aware_dt = _to_utc(naive_dt)
    assert aware_dt is not None
    assert aware_dt.tzinfo == timezone.utc
    assert aware_dt == datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

    already_aware = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert _to_utc(already_aware) == already_aware

    offset_dt = datetime(2026, 8, 24, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    converted_dt = _to_utc(offset_dt)
    assert converted_dt == datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_is_active_course_valid() -> None:
    """Test active course validation with valid active term and enrollments."""
    course = CanvasCourse(
        id=101,
        name="AP Biology",
        workflow_state="available",
        term=CanvasTerm(
            id=1,
            name="Fall 2026",
            start_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            end_at=datetime(2026, 12, 20, tzinfo=timezone.utc),
            workflow_state="active",
        ),
        enrollments=(
            CanvasEnrollment(
                type="student",
                role="StudentEnrollment",
                user_id=1,
                enrollment_state="active",
                grade=CanvasGrade(current_score=94.5),
            ),
        ),
    )
    assert is_active_course(course, now=FROZEN_NOW) is True
    # Test default now argument
    assert is_active_course(course) is True


@pytest.mark.parametrize(
    "invalid_name",
    ["", "   ", "Unnamed Course", "None", "Course 5388", "Course 12345"],
)
def test_is_active_course_invalid_name(invalid_name: str) -> None:
    """Test rejection of blank or placeholder course names."""
    course = CanvasCourse(
        id=102,
        name=invalid_name,
        workflow_state="available",
    )
    assert is_active_course(course, now=FROZEN_NOW) is False


def test_is_active_course_unavailable_workflow_state() -> None:
    """Test rejection of unpublished or deleted courses."""
    for state in ["unpublished", "completed", "deleted"]:
        course = CanvasCourse(
            id=103,
            name="Physics 101",
            workflow_state=state,
        )
        assert is_active_course(course, now=FROZEN_NOW) is False


def test_is_active_course_enrollments_check() -> None:
    """Test active enrollment verification."""
    # Active enrollment -> True
    c_active = CanvasCourse(
        id=104,
        name="Chemistry",
        workflow_state="available",
        enrollments=(
            CanvasEnrollment(
                type="student",
                role="StudentEnrollment",
                user_id=1,
                enrollment_state="active",
                grade=CanvasGrade(),
            ),
        ),
    )
    assert is_active_course(c_active, now=FROZEN_NOW) is True

    # Invited enrollment -> True
    c_invited = CanvasCourse(
        id=105,
        name="Art",
        workflow_state="available",
        enrollments=(
            CanvasEnrollment(
                type="student",
                role="StudentEnrollment",
                user_id=1,
                enrollment_state="invited",
                grade=CanvasGrade(),
            ),
        ),
    )
    assert is_active_course(c_invited, now=FROZEN_NOW) is True

    # Inactive/completed enrollments only -> False
    c_inactive = CanvasCourse(
        id=106,
        name="History",
        workflow_state="available",
        enrollments=(
            CanvasEnrollment(
                type="student",
                role="StudentEnrollment",
                user_id=1,
                enrollment_state="completed",
                grade=CanvasGrade(),
            ),
            CanvasEnrollment(
                type="student",
                role="StudentEnrollment",
                user_id=1,
                enrollment_state="inactive",
                grade=CanvasGrade(),
            ),
        ),
    )
    assert is_active_course(c_inactive, now=FROZEN_NOW) is False

    # Empty enrollments tuple -> True (does not reject solely on empty enrollments)
    c_empty = CanvasCourse(
        id=107,
        name="Calculus",
        workflow_state="available",
        enrollments=(),
    )
    assert is_active_course(c_empty, now=FROZEN_NOW) is True


def test_is_active_course_term_validation() -> None:
    """Test course term status and end date boundaries."""
    # Completed term state -> False
    c_completed_term = CanvasCourse(
        id=108,
        name="World History",
        workflow_state="available",
        term=CanvasTerm(
            id=1,
            name="Spring 2026",
            start_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            workflow_state="completed",
        ),
    )
    assert is_active_course(c_completed_term, now=FROZEN_NOW) is False

    # Deleted term state -> False
    c_deleted_term = CanvasCourse(
        id=109,
        name="Spanish",
        workflow_state="available",
        term=CanvasTerm(
            id=2,
            name="Archived",
            workflow_state="deleted",
        ),
    )
    assert is_active_course(c_deleted_term, now=FROZEN_NOW) is False

    # Expired term end date -> False
    c_expired_term = CanvasCourse(
        id=110,
        name="French 1",
        workflow_state="available",
        term=CanvasTerm(
            id=3,
            name="Spring 2026",
            end_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
            workflow_state="active",
        ),
    )
    assert is_active_course(c_expired_term, now=FROZEN_NOW) is False

    # Active future term end date -> True
    c_future_term = CanvasCourse(
        id=111,
        name="French 2",
        workflow_state="available",
        term=CanvasTerm(
            id=4,
            name="Fall 2026",
            end_at=datetime(2026, 12, 15, tzinfo=timezone.utc),
            workflow_state="active",
        ),
    )
    assert is_active_course(c_future_term, now=FROZEN_NOW) is True

    # Term with no end date -> True
    c_no_end_date = CanvasCourse(
        id=112,
        name="Independent Study",
        workflow_state="available",
        term=CanvasTerm(
            id=5,
            name="Ongoing",
            end_at=None,
            workflow_state="active",
        ),
    )
    assert is_active_course(c_no_end_date, now=FROZEN_NOW) is True


def test_is_active_course_end_at_validation() -> None:
    """Test course-specific end date boundaries."""
    # Course end date in the past -> False
    c_past_end = CanvasCourse(
        id=113,
        name="Summer Math",
        workflow_state="available",
        end_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert is_active_course(c_past_end, now=FROZEN_NOW) is False

    # Course end date in the future -> True
    c_future_end = CanvasCourse(
        id=114,
        name="Fall Math",
        workflow_state="available",
        end_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    assert is_active_course(c_future_end, now=FROZEN_NOW) is True


def test_filter_active_courses() -> None:
    """Test filtering a list of mixed active and inactive courses."""
    valid_c = CanvasCourse(id=1, name="English", workflow_state="available")
    invalid_c = CanvasCourse(id=2, name="None", workflow_state="available")
    concluded_c = CanvasCourse(id=3, name="Gym", workflow_state="completed")

    filtered = filter_active_courses([valid_c, invalid_c, concluded_c], now=FROZEN_NOW)
    assert len(filtered) == 1
    assert filtered[0].id == 1


def test_is_active_todo_assignment_workflow_state() -> None:
    """Test assignment workflow state filtering."""
    course = CanvasCourse(id=1, name="Course")
    asg_pub = CanvasAssignment(
        id=1, course_id=1, name="Quiz 1", workflow_state="published"
    )
    assert is_active_todo_assignment(asg_pub, course, now=FROZEN_NOW) is True

    # Reject assignment belonging to a different course
    asg_diff_course = CanvasAssignment(
        id=1, course_id=999, name="Quiz 1", workflow_state="published"
    )
    assert is_active_todo_assignment(asg_diff_course, course, now=FROZEN_NOW) is False

    asg_unpub = CanvasAssignment(
        id=2, course_id=1, name="Draft Quiz", workflow_state="unpublished"
    )
    assert is_active_todo_assignment(asg_unpub, course, now=FROZEN_NOW) is False


def test_is_active_todo_assignment_not_graded_placeholders() -> None:
    """Test rejection of non-graded and omitted zero-point placeholders."""
    course = CanvasCourse(id=1, name="Course")

    # grading_type == "not_graded" -> False
    asg_not_graded = CanvasAssignment(
        id=1, course_id=1, name="Syllabus", grading_type="not_graded"
    )
    assert is_active_todo_assignment(asg_not_graded, course, now=FROZEN_NOW) is False

    # "not_graded" in submission_types -> False
    asg_sub_not_graded = CanvasAssignment(
        id=2, course_id=1, name="Reading", submission_types=("not_graded",)
    )
    assert (
        is_active_todo_assignment(asg_sub_not_graded, course, now=FROZEN_NOW) is False
    )

    # points_possible == 0.0 and omit_from_final_grade == True -> False
    asg_zero_omitted = CanvasAssignment(
        id=3,
        course_id=1,
        name="Header",
        points_possible=0.0,
        omit_from_final_grade=True,
    )
    assert is_active_todo_assignment(asg_zero_omitted, course, now=FROZEN_NOW) is False

    # points_possible == 0.0 but omit_from_final_grade == False -> True (e.g. check-in task)
    asg_zero_required = CanvasAssignment(
        id=4,
        course_id=1,
        name="Participation Check",
        points_possible=0.0,
        omit_from_final_grade=False,
    )
    assert is_active_todo_assignment(asg_zero_required, course, now=FROZEN_NOW) is True

    # points_possible > 0 and omit_from_final_grade == True -> True (e.g. extra practice)
    asg_practice = CanvasAssignment(
        id=5,
        course_id=1,
        name="Practice Worksheet",
        points_possible=10.0,
        omit_from_final_grade=True,
    )
    assert is_active_todo_assignment(asg_practice, course, now=FROZEN_NOW) is True


def test_is_active_todo_assignment_unlock_at() -> None:
    """Test future locked assignments filtering."""
    course = CanvasCourse(id=1, name="Course")

    # Future locked assignment -> False
    asg_future_lock = CanvasAssignment(
        id=1,
        course_id=1,
        name="Future Exam",
        unlock_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_future_lock, course, now=FROZEN_NOW) is False

    # Already unlocked assignment -> True
    asg_unlocked = CanvasAssignment(
        id=2,
        course_id=1,
        name="Active Exam",
        unlock_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_unlocked, course, now=FROZEN_NOW) is True


def test_is_active_todo_assignment_cloned_template_term_start() -> None:
    """Test cloned master course template filtering with term start date."""
    term = CanvasTerm(
        id=1,
        name="Fall 2026",
        start_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )
    course = CanvasCourse(id=1, name="AP Lit", term=term)

    # Historical due date from 2024 (pre-dating term start) -> False
    asg_stale = CanvasAssignment(
        id=1,
        course_id=1,
        name="2024 Essay",
        due_at=datetime(2024, 10, 15, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_stale, course, now=FROZEN_NOW) is False

    # Valid current due date after term start -> True
    asg_current = CanvasAssignment(
        id=2,
        course_id=1,
        name="2026 Essay",
        due_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_current, course, now=FROZEN_NOW) is True


def test_is_active_todo_assignment_cloned_template_course_start() -> None:
    """Test cloned template filtering using course start_at fallback."""
    course = CanvasCourse(
        id=1,
        name="Chemistry",
        start_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        term=None,
    )

    # Due date before course start -> False
    asg_stale = CanvasAssignment(
        id=1,
        course_id=1,
        name="Old Lab",
        due_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_stale, course, now=FROZEN_NOW) is False

    # Due date after course start -> True
    asg_valid = CanvasAssignment(
        id=2,
        course_id=1,
        name="Current Lab",
        due_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    assert is_active_todo_assignment(asg_valid, course, now=FROZEN_NOW) is True


def test_is_active_todo_assignment_stale_fallback_threshold() -> None:
    """Test 180-day fallback threshold when neither term nor course start date is set."""
    course = CanvasCourse(id=1, name="Elective", term=None, start_at=None)

    # Due date older than 180 days -> False
    asg_very_old = CanvasAssignment(
        id=1,
        course_id=1,
        name="Ancient Task",
        due_at=FROZEN_NOW - timedelta(days=181),
    )
    assert is_active_todo_assignment(asg_very_old, course, now=FROZEN_NOW) is False

    # Due date within 180 days -> True
    asg_recent = CanvasAssignment(
        id=2,
        course_id=1,
        name="Recent Task",
        due_at=FROZEN_NOW - timedelta(days=30),
    )
    assert is_active_todo_assignment(asg_recent, course, now=FROZEN_NOW) is True

    # Due date is None -> True (ongoing project)
    asg_no_due = CanvasAssignment(
        id=3,
        course_id=1,
        name="Ongoing Project",
        due_at=None,
    )
    assert is_active_todo_assignment(asg_no_due, course, now=FROZEN_NOW) is True


def test_is_active_todo_assignment_submission_evaluation() -> None:
    """Test exclusion of graded or excused submissions."""
    course = CanvasCourse(id=1, name="Math")

    # Graded with score -> False
    asg_scored = CanvasAssignment(
        id=1,
        course_id=1,
        name="HW 1",
        submission=CanvasSubmission(id=1, assignment_id=1, user_id=1, score=10.0),
    )
    assert is_active_todo_assignment(asg_scored, course, now=FROZEN_NOW) is False

    # Zero score with no submission stays open (placeholder zero) -> True
    asg_zero_scored = CanvasAssignment(
        id=2,
        course_id=1,
        name="HW 2",
        submission=CanvasSubmission(id=2, assignment_id=2, user_id=1, score=0.0),
    )
    assert is_active_todo_assignment(asg_zero_scored, course, now=FROZEN_NOW) is True

    # Graded with letter grade -> False
    asg_letter = CanvasAssignment(
        id=3,
        course_id=1,
        name="HW 3",
        submission=CanvasSubmission(id=3, assignment_id=3, user_id=1, grade="A"),
    )
    assert is_active_todo_assignment(asg_letter, course, now=FROZEN_NOW) is False

    # Excused -> False
    asg_excused = CanvasAssignment(
        id=4,
        course_id=1,
        name="HW 4",
        submission=CanvasSubmission(id=4, assignment_id=4, user_id=1, excused=True),
    )
    assert is_active_todo_assignment(asg_excused, course, now=FROZEN_NOW) is False

    # "graded" with no score, grade or submission proves nothing -> stays open
    asg_graded_state = CanvasAssignment(
        id=5,
        course_id=1,
        name="HW 5",
        submission=CanvasSubmission(
            id=5, assignment_id=5, user_id=1, workflow_state="graded"
        ),
    )
    assert is_active_todo_assignment(asg_graded_state, course, now=FROZEN_NOW) is True

    # Unsubmitted / unassessed -> True
    asg_pending = CanvasAssignment(
        id=6,
        course_id=1,
        name="HW 6",
        submission=CanvasSubmission(
            id=6, assignment_id=6, user_id=1, workflow_state="unsubmitted"
        ),
    )
    assert is_active_todo_assignment(asg_pending, course, now=FROZEN_NOW) is True

    # No submission object -> True
    asg_no_sub = CanvasAssignment(
        id=7,
        course_id=1,
        name="HW 7",
        submission=None,
    )
    assert is_active_todo_assignment(asg_no_sub, course, now=FROZEN_NOW) is True
    # Test default now argument
    assert is_active_todo_assignment(asg_no_sub, course) is True


def test_filter_pending_assignments() -> None:
    """Test filtering assignment list down to actionable items."""
    course = CanvasCourse(id=1, name="Science")
    asg1 = CanvasAssignment(id=1, course_id=1, name="Lab 1", workflow_state="published")
    asg2 = CanvasAssignment(id=2, course_id=1, name="Lab 2", grading_type="not_graded")
    asg3 = CanvasAssignment(
        id=3,
        course_id=1,
        name="Lab 3",
        submission=CanvasSubmission(id=3, assignment_id=3, user_id=1, score=100.0),
    )

    pending = filter_pending_assignments([asg1, asg2, asg3], course, now=FROZEN_NOW)
    assert len(pending) == 1
    assert pending[0].id == 1


@pytest.mark.parametrize(
    ("submission_types", "expected"),
    [
        (("online_upload",), True),
        (("online_text_entry",), True),
        (("online_url",), True),
        (("media_recording",), True),
        (("online_quiz",), True),
        (("discussion_topic",), True),
        (("external_tool",), True),
        (("on_paper", "online_upload"), True),
        (("on_paper",), False),
        (("none",), False),
        (("not_graded",), False),
        ((), False),
    ],
)
def test_is_online_submission_assignment(
    submission_types: tuple[str, ...], expected: bool
) -> None:
    """Test online submission detection for actionable to-do assignments."""
    asg = CanvasAssignment(
        id=10,
        course_id=1,
        name="Assignment",
        submission_types=submission_types,
    )
    assert is_online_submission_assignment(asg) is expected


@pytest.mark.parametrize(
    ("name", "submission_types", "expected_in_class"),
    [
        ("Do Now 8/19 and 8/21", ("online_text_entry",), True),
        ("Guided Notes - Unit 1", ("online_url",), True),
        ("GN - Chapter 2", ("online_upload",), True),
        ("CLASS WEEK 2_Thursday_Chords", ("media_recording",), True),
        ("CLASS_WEEK 3_Monday", ("online_text_entry",), True),
        ("RWL Workshop 8.17-8.21", ("online_text_entry",), True),
        ("Weekly Participation", ("online_text_entry",), True),
        ("Advisory Attendance", ("none",), True),
        ("Reading Material", ("not_graded",), True),
        ("Empty Submission Types", (), True),
        ("Thinking in Systems", ("on_paper",), False),
        ("Cell Mitosis Lab", ("online_upload",), False),
        ("Chapter 1 Reflection", ("online_text_entry",), False),
        ("Carlos Santana Questions", ("online_text_entry",), False),
    ],
)
def test_is_in_class_activity_and_actionable(
    name: str, submission_types: tuple[str, ...], expected_in_class: bool
) -> None:
    """Test classification of in-class activities vs actionable to-do tasks."""
    asg = CanvasAssignment(
        id=20,
        course_id=1,
        name=name,
        submission_types=submission_types,
    )
    assert is_in_class_activity(asg) is expected_in_class
    assert is_actionable_todo_assignment(asg) is not expected_in_class


# --- Truthful submission status (placeholder-zero handling) -------------------

PAST_DUE = datetime(2026, 8, 20, 23, 59, tzinfo=timezone.utc)
FUTURE_DUE = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
SUBMITTED = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


def _asg(
    sub: CanvasSubmission | None, due: datetime | None = PAST_DUE
) -> CanvasAssignment:
    return CanvasAssignment(id=1, course_id=1, name="HW", due_at=due, submission=sub)


def _sub(**kwargs: object) -> CanvasSubmission:
    return CanvasSubmission(id=1, assignment_id=1, user_id=1, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        # Placeholder zero on never-submitted work: open, not done
        (
            _asg(_sub(workflow_state="graded", score=0.0, grade="0")),
            SubmissionStatus.ZEROED,
        ),
        # Canvas missing flag / late policy wins over a placeholder grade
        (
            _asg(_sub(workflow_state="graded", score=0.0, missing=True)),
            SubmissionStatus.MISSING,
        ),
        (_asg(_sub(score=0.0, late_policy_status="missing")), SubmissionStatus.MISSING),
        # Paper work: Canvas never flags missing, so past due + nothing = missing
        (_asg(_sub(workflow_state="unsubmitted")), SubmissionStatus.MISSING),
        (_asg(None), SubmissionStatus.MISSING),
        (_asg(None, due=FUTURE_DUE), SubmissionStatus.UPCOMING),
        (_asg(None, due=None), SubmissionStatus.UPCOMING),
        # Turned in
        (
            _asg(_sub(workflow_state="submitted", submitted_at=SUBMITTED)),
            SubmissionStatus.SUBMITTED,
        ),
        (
            _asg(_sub(workflow_state="submitted", submitted_at=SUBMITTED, late=True)),
            SubmissionStatus.LATE,
        ),
        # A zero on work that WAS submitted is a real grade, still done
        (
            _asg(_sub(workflow_state="graded", submitted_at=SUBMITTED, score=0.0)),
            SubmissionStatus.SUBMITTED,
        ),
        # Paper work graded with no online submission
        (
            _asg(_sub(workflow_state="graded", score=18.0, grade="18")),
            SubmissionStatus.GRADED,
        ),
        (_asg(_sub(excused=True, score=0.0)), SubmissionStatus.EXCUSED),
    ],
)
def test_submission_status(
    assignment: CanvasAssignment, expected: SubmissionStatus
) -> None:
    """Classify submissions truthfully regardless of gradebook placeholders."""
    assert submission_status(assignment, now=FROZEN_NOW) is expected
    assert is_done(expected) is (expected not in OPEN_STATUSES)


def test_done_items_retained_then_dropped() -> None:
    """Finished work stays for DONE_RETENTION_DAYS after due, open work always stays."""
    course = CanvasCourse(id=1, name="Math")
    done_recent = _asg(_sub(workflow_state="submitted", submitted_at=SUBMITTED))
    done_old = CanvasAssignment(
        id=2,
        course_id=1,
        name="Old",
        due_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        submission=_sub(
            workflow_state="submitted",
            submitted_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        ),
    )
    zero_old = CanvasAssignment(
        id=3,
        course_id=1,
        name="Old zero",
        due_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        submission=_sub(workflow_state="graded", score=0.0),
    )
    assert is_active_todo_assignment(done_recent, course, now=FROZEN_NOW) is True
    assert is_active_todo_assignment(done_old, course, now=FROZEN_NOW) is False
    assert is_active_todo_assignment(zero_old, course, now=FROZEN_NOW) is True
