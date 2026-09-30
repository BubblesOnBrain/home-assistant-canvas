"""Data models for the Canvas LMS integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _parse_dt(val: Any) -> datetime | None:
    """Parse ISO8601 string to timezone-aware UTC datetime.

    Handles 'Z' suffix, standard offsets, and returns None on invalid inputs.
    """
    if not val or not isinstance(val, str) or not val.strip():
        return None
    try:
        dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_float(val: Any) -> float | None:
    """Parse numeric float safely.

    Handles float, int, numeric strings, and returns None on invalid inputs or booleans.
    """
    if (
        val is None
        or isinstance(val, bool)
        or isinstance(val, (list, dict, set, tuple))
    ):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class CanvasUser:
    """Represents a Canvas user profile (parent / observer account)."""

    id: int
    name: str
    sortable_name: str | None = None
    short_name: str | None = None
    primary_email: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasUser:
        """Create CanvasUser from API dictionary."""
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"User {data['id']}"),
            sortable_name=data.get("sortable_name"),
            short_name=data.get("short_name"),
            primary_email=data.get("primary_email"),
        )


@dataclass(frozen=True, slots=True)
class CanvasObservee:
    """Represents an observed student linked to the parent account."""

    id: int
    name: str
    sortable_name: str | None = None
    short_name: str | None = None
    pronouns: str | None = None
    root_account_ids: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasObservee:
        """Create CanvasObservee from API dictionary."""
        raw_root_ids = data.get("observation_link_root_account_ids") or ()
        root_ids = tuple(int(x) for x in raw_root_ids if isinstance(x, (int, str)))
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Student {data['id']}"),
            sortable_name=data.get("sortable_name"),
            short_name=data.get("short_name"),
            pronouns=data.get("pronouns"),
            root_account_ids=root_ids,
        )


@dataclass(frozen=True, slots=True)
class CanvasTerm:
    """Represents an academic term."""

    id: int
    name: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    workflow_state: str = "active"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CanvasTerm | None:
        """Create CanvasTerm from API dictionary."""
        if not data or not isinstance(data, dict):
            return None
        return cls(
            id=int(data.get("id") or 0),
            name=str(data.get("name") or "Default Term"),
            start_at=_parse_dt(data.get("start_at")),
            end_at=_parse_dt(data.get("end_at")),
            workflow_state=str(data.get("workflow_state") or "active"),
        )


@dataclass(frozen=True, slots=True)
class CanvasTeacher:
    """Represents a course instructor."""

    id: int
    display_name: str
    avatar_image_url: str | None = None
    html_url: str | None = None
    pronouns: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasTeacher:
        """Create CanvasTeacher from API dictionary."""
        return cls(
            id=int(data["id"]),
            display_name=str(data.get("display_name") or "Teacher"),
            avatar_image_url=data.get("avatar_image_url"),
            html_url=data.get("html_url"),
            pronouns=data.get("pronouns"),
        )


@dataclass(frozen=True, slots=True)
class CanvasGrade:
    """Represents grade details for a student course enrollment."""

    current_score: float | None = None
    current_grade: str | None = None
    final_score: float | None = None
    final_grade: str | None = None
    current_period_score: float | None = None
    current_period_grade: str | None = None
    grading_period_title: str | None = None

    @classmethod
    def from_enrollment_dict(cls, data: dict[str, Any]) -> CanvasGrade:
        """Extract grade fields from an enrollment dict."""
        current_grade = data.get("computed_current_grade")
        if current_grade is None:
            current_grade = data.get("computed_current_letter_grade")
        return cls(
            current_score=_parse_float(data.get("computed_current_score")),
            current_grade=str(current_grade) if current_grade is not None else None,
            final_score=_parse_float(data.get("computed_final_score")),
            final_grade=(
                str(data["computed_final_grade"])
                if data.get("computed_final_grade") is not None
                else None
            ),
            current_period_score=_parse_float(
                data.get("current_period_computed_current_score")
            ),
            current_period_grade=(
                str(data["current_period_computed_current_grade"])
                if data.get("current_period_computed_current_grade") is not None
                else None
            ),
            grading_period_title=(
                str(data["current_grading_period_title"])
                if data.get("current_grading_period_title") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CanvasEnrollment:
    """Represents a student course enrollment."""

    type: str
    role: str
    user_id: int
    enrollment_state: str
    grade: CanvasGrade

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasEnrollment:
        """Create CanvasEnrollment from API dictionary."""
        return cls(
            type=str(data.get("type") or "student"),
            role=str(data.get("role") or "StudentEnrollment"),
            user_id=int(data.get("user_id") or 0),
            enrollment_state=str(data.get("enrollment_state") or "active"),
            grade=CanvasGrade.from_enrollment_dict(data),
        )


@dataclass(frozen=True, slots=True)
class CanvasCourse:
    """Represents an academic course."""

    id: int
    name: str
    course_code: str | None = None
    account_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    workflow_state: str = "available"
    term: CanvasTerm | None = None
    teachers: tuple[CanvasTeacher, ...] = ()
    enrollments: tuple[CanvasEnrollment, ...] = ()

    @property
    def primary_grade(self) -> CanvasGrade | None:
        """Return primary student grade for this course."""
        if self.enrollments:
            return self.enrollments[0].grade
        return None

    @property
    def primary_teacher(self) -> CanvasTeacher | None:
        """Return primary course instructor."""
        if self.teachers:
            return self.teachers[0]
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasCourse:
        """Create CanvasCourse from API dictionary."""
        teachers = tuple(
            CanvasTeacher.from_dict(t)
            for t in (data.get("teachers") or ())
            if isinstance(t, dict) and "id" in t
        )
        enrollments = tuple(
            CanvasEnrollment.from_dict(e)
            for e in (data.get("enrollments") or ())
            if isinstance(e, dict)
        )
        raw_account_id = data.get("account_id")
        account_id = int(raw_account_id) if raw_account_id is not None else None

        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Course {data['id']}"),
            course_code=data.get("course_code"),
            account_id=account_id,
            start_at=_parse_dt(data.get("start_at")),
            end_at=_parse_dt(data.get("end_at")),
            workflow_state=str(data.get("workflow_state") or "available"),
            term=CanvasTerm.from_dict(data.get("term")),
            teachers=teachers,
            enrollments=enrollments,
        )


@dataclass(frozen=True, slots=True)
class CanvasSubmission:
    """Represents a student's submission state for an assignment."""

    id: int
    assignment_id: int
    user_id: int
    workflow_state: str = "unsubmitted"
    grade: str | None = None
    score: float | None = None
    excused: bool = False
    missing: bool = False
    late: bool = False
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    submission_type: str | None = None
    late_policy_status: str | None = None

    @property
    def is_graded_or_excused(self) -> bool:
        """Return True if submission has been graded or excused."""
        return (
            self.excused
            or self.score is not None
            or self.grade is not None
            or self.workflow_state == "graded"
        )

    @property
    def is_submitted(self) -> bool:
        """Return True if submission was turned in."""
        return self.submitted_at is not None or self.workflow_state in (
            "submitted",
            "graded",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CanvasSubmission | None:
        """Create CanvasSubmission from API dictionary."""
        if not data or not isinstance(data, dict):
            return None
        return cls(
            id=int(data.get("id") or 0),
            assignment_id=int(data.get("assignment_id") or 0),
            user_id=int(data.get("user_id") or 0),
            workflow_state=str(data.get("workflow_state") or "unsubmitted"),
            grade=str(data["grade"]) if data.get("grade") is not None else None,
            score=_parse_float(data.get("score")),
            excused=bool(data.get("excused")),
            missing=bool(data.get("missing")),
            late=bool(data.get("late")),
            submitted_at=_parse_dt(data.get("submitted_at")),
            graded_at=_parse_dt(data.get("graded_at")),
            submission_type=data.get("submission_type"),
            late_policy_status=data.get("late_policy_status"),
        )


@dataclass(frozen=True, slots=True)
class CanvasAssignment:
    """Represents an academic assignment."""

    id: int
    course_id: int
    name: str
    description: str | None = None
    due_at: datetime | None = None
    lock_at: datetime | None = None
    unlock_at: datetime | None = None
    points_possible: float | None = None
    grading_type: str | None = None
    submission_types: tuple[str, ...] = ()
    omit_from_final_grade: bool = False
    workflow_state: str = "published"
    html_url: str | None = None
    submission: CanvasSubmission | None = None
    assignment_group_id: int | None = None
    is_quiz_assignment: bool = False

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        submission: CanvasSubmission | None = None,
    ) -> CanvasAssignment:
        """Create CanvasAssignment from API dictionary."""
        sub = submission or CanvasSubmission.from_dict(data.get("submission"))
        raw_types = data.get("submission_types") or ()
        sub_types = tuple(str(x) for x in raw_types)

        return cls(
            id=int(data["id"]),
            course_id=int(data.get("course_id") or 0),
            name=str(data.get("name") or f"Assignment {data['id']}"),
            description=data.get("description"),
            due_at=_parse_dt(data.get("due_at")),
            lock_at=_parse_dt(data.get("lock_at")),
            unlock_at=_parse_dt(data.get("unlock_at")),
            points_possible=_parse_float(data.get("points_possible")),
            grading_type=data.get("grading_type"),
            submission_types=sub_types,
            omit_from_final_grade=bool(data.get("omit_from_final_grade")),
            workflow_state=str(data.get("workflow_state") or "published"),
            html_url=data.get("html_url"),
            submission=sub,
            assignment_group_id=(
                int(data["assignment_group_id"])
                if data.get("assignment_group_id") is not None
                else None
            ),
            is_quiz_assignment=bool(data.get("is_quiz_assignment"))
            or "online_quiz" in sub_types,
        )


@dataclass(frozen=True, slots=True)
class CanvasStudentData:
    """Container for student specific courses and assignments."""

    student: CanvasObservee
    courses: tuple[CanvasCourse, ...] = ()
    assignments: tuple[CanvasAssignment, ...] = ()


@dataclass(frozen=True, slots=True)
class CanvasData:
    """Coordinator aggregate state container."""

    user: CanvasUser
    observees: tuple[CanvasObservee, ...] = ()
    courses_by_student: dict[int, list[CanvasCourse]] = field(default_factory=dict)
    assignments_by_student: dict[int, list[CanvasAssignment]] = field(
        default_factory=dict
    )
    # Every current-term assignment, including finished work past the to-do
    # list's retention window (for "waiting on teacher" and grade review).
    term_assignments_by_student: dict[int, list[CanvasAssignment]] = field(
        default_factory=dict
    )
    # Course id -> assignment group id -> group name (e.g. "Tests", "Major").
    assignment_groups: dict[int, dict[int, str]] = field(default_factory=dict)

    def term_assignments(self, student_id: int) -> list[CanvasAssignment]:
        """Return all current-term assignments for a student."""
        if student_id in self.term_assignments_by_student:
            return self.term_assignments_by_student[student_id]
        return self.assignments_by_student.get(student_id, [])

    def group_name(self, assignment: CanvasAssignment) -> str:
        """Return the assignment's group name, or "" if unknown."""
        if assignment.assignment_group_id is None:
            return ""
        return self.assignment_groups.get(assignment.course_id, {}).get(
            assignment.assignment_group_id, ""
        )
