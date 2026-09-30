"""Noise filtering and data cleansing heuristics engine for Canvas LMS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .const import (
    ACTIVE_ENROLLMENT_STATES,
    COMPLETED_TERM_STATES,
    DEFAULT_STALE_DAYS_THRESHOLD,
    DONE_RETENTION_DAYS,
    FILTER_NOT_GRADED,
    IN_CLASS_KEYWORDS,
    ONLINE_SUBMISSION_TYPES,
)
from .models import CanvasAssignment, CanvasCourse


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to UTC-aware datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SubmissionStatus(StrEnum):
    """Truthful status of an assignment, derived from Canvas submission data.

    Canvas grade columns are ambiguous: teachers often enter a placeholder 0
    for work that was never turned in, which sets workflow_state=graded and
    score=0. Only submitted_at (or an explicit excusal) proves work reached
    Canvas, so "done" is decided from that, not from the presence of a grade.
    """

    UPCOMING = "upcoming"  # not due yet, nothing submitted
    MISSING = "missing"  # past due / flagged missing, nothing submitted
    ZEROED = "zeroed"  # graded 0 with no submission: placeholder or real 0
    SUBMITTED = "submitted"  # turned in on time
    LATE = "late"  # turned in after the due date
    GRADED = "graded"  # graded with no online submission (e.g. paper work)
    EXCUSED = "excused"


OPEN_STATUSES = frozenset(
    {SubmissionStatus.UPCOMING, SubmissionStatus.MISSING, SubmissionStatus.ZEROED}
)


def submission_status(
    assignment: CanvasAssignment, now: datetime | None = None
) -> SubmissionStatus:
    """Classify an assignment's submission state.

    Notes:
    - Canvas only sets the `missing` flag for online submission types. Paper
      work that is past due and ungraded is treated as missing here.
    - A 0 with no submission is never "done"; it stays open as ZEROED so it
      can be turned in or raised with the teacher.

    """
    current_time = _to_utc(now) or datetime.now(timezone.utc)
    sub = assignment.submission

    if sub is not None:
        if sub.excused:
            return SubmissionStatus.EXCUSED
        if sub.submitted_at is not None:
            if sub.late or sub.late_policy_status == "late":
                return SubmissionStatus.LATE
            return SubmissionStatus.SUBMITTED
        if sub.missing or sub.late_policy_status == "missing":
            return SubmissionStatus.MISSING
        if sub.score == 0:
            return SubmissionStatus.ZEROED
        if (sub.score is not None and sub.score > 0) or sub.grade not in (
            None,
            "",
            "0",
        ):
            return SubmissionStatus.GRADED

    due_time = _to_utc(assignment.due_at)
    if due_time is not None and due_time < current_time:
        return SubmissionStatus.MISSING
    return SubmissionStatus.UPCOMING


def is_done(status: SubmissionStatus) -> bool:
    """Return True if the status means the work is finished."""
    return status not in OPEN_STATUSES


def is_active_course(course: CanvasCourse, now: datetime | None = None) -> bool:
    """Return True if course belongs to an active, current academic term.

    Discards unnamed courses, non-available courses, inactive enrollments,
    and courses whose term/course end date is in the past.
    """
    current_time = _to_utc(now) or datetime.now(timezone.utc)

    # 1. Filter out empty or placeholder course names
    if (
        not course.name
        or course.name.strip() in ("", "Unnamed Course", "None")
        or (course.name.startswith("Course ") and course.name[7:].isdigit())
    ):
        return False

    # 2. Check workflow state
    if course.workflow_state != "available":
        return False

    # 3. Check enrollment states (if enrollments are provided)
    if course.enrollments:
        if not any(
            e.enrollment_state in ACTIVE_ENROLLMENT_STATES for e in course.enrollments
        ):
            return False

    # 4. Check term end date and workflow state
    if course.term is not None:
        if course.term.workflow_state in COMPLETED_TERM_STATES:
            return False
        if course.term.end_at is not None:
            term_end = _to_utc(course.term.end_at)
            if term_end is not None and term_end < current_time:
                return False

    # 5. Check course end date
    if course.end_at is not None:
        course_end = _to_utc(course.end_at)
        if course_end is not None and course_end < current_time:
            return False

    return True


def filter_active_courses(
    courses: list[CanvasCourse],
    now: datetime | None = None,
) -> list[CanvasCourse]:
    """Filter a list of courses down to only active courses."""
    return [c for c in courses if is_active_course(c, now=now)]


def is_active_todo_assignment(
    assignment: CanvasAssignment,
    course: CanvasCourse,
    now: datetime | None = None,
) -> bool:
    """Return True if assignment is an actionable, pending task for the student.

    Discards non-graded placeholders, pre-term cloned syllabus templates,
    and already assessed/excused items.
    """
    current_time = _to_utc(now) or datetime.now(timezone.utc)

    # 0. Reject assignments not belonging to this course
    if assignment.course_id != 0 and assignment.course_id != course.id:
        return False

    # 1. Workflow state check
    if assignment.workflow_state != "published":
        return False

    # 2. Non-graded & zero point placeholder filtering
    if assignment.grading_type == FILTER_NOT_GRADED:
        return False
    if FILTER_NOT_GRADED in assignment.submission_types:
        return False
    if assignment.points_possible == 0.0 and assignment.omit_from_final_grade:
        return False

    # 3. Reject future locked assignments
    if assignment.unlock_at is not None:
        unlock_time = _to_utc(assignment.unlock_at)
        if unlock_time is not None and unlock_time > current_time:
            return False

    # 4. Term start date boundary (Cloned template filtering)
    term_start: datetime | None = None
    if course.term and course.term.start_at:
        term_start = _to_utc(course.term.start_at)
    elif course.start_at:
        term_start = _to_utc(course.start_at)

    if (due_time := _to_utc(assignment.due_at)) is not None:
        if term_start is not None:
            if due_time < term_start:
                # Assignment due date predates term start
                return False
        else:
            # Fallback when term start date is missing: discard stale historical dates
            stale_boundary = current_time - timedelta(days=DEFAULT_STALE_DAYS_THRESHOLD)
            if due_time < stale_boundary:
                return False

    # 5. Finished work: keep briefly so the list shows it completed, then drop.
    #    Open work (upcoming, missing, zeroed) is always kept.
    if is_done(submission_status(assignment, now=current_time)):
        sub = assignment.submission
        reference = _to_utc(assignment.due_at) or (
            _to_utc(sub.submitted_at or sub.graded_at) if sub else None
        )
        if reference is None or reference < current_time - timedelta(
            days=DONE_RETENTION_DAYS
        ):
            return False

    return True


def filter_pending_assignments(
    assignments: list[CanvasAssignment],
    course: CanvasCourse,
    now: datetime | None = None,
) -> list[CanvasAssignment]:
    """Filter assignments to only pending, actionable items for a course."""
    return [a for a in assignments if is_active_todo_assignment(a, course, now=now)]


def is_online_submission_assignment(assignment: CanvasAssignment) -> bool:
    """Return True if assignment requires an actionable online student submission."""
    return any(st in ONLINE_SUBMISSION_TYPES for st in assignment.submission_types)


def is_in_class_activity(assignment: CanvasAssignment) -> bool:
    """Return True if assignment is an in-class activity, non-submittable item, or warm-up."""
    stypes = assignment.submission_types
    if not stypes or stypes == ("none",) or stypes == ("not_graded",):
        return True
    name_lower = assignment.name.lower() if assignment.name else ""
    return any(kw in name_lower for kw in IN_CLASS_KEYWORDS)


def is_actionable_todo_assignment(assignment: CanvasAssignment) -> bool:
    """Return True if assignment is an actionable homework/task for the student To-Do list."""
    return not is_in_class_activity(assignment)
