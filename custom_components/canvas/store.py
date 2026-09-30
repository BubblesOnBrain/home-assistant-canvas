"""Durable storage for the student's workflow: stages, notes and follow-ups.

This is the store of record for everything the student enters. It lives in
`/config/.storage/canvas.workflow.<account>` (included in Home Assistant
backups), is loaded once at setup and kept in memory. Student edits are saved
immediately; changes made by the reconcile step are debounced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    GRADE_LOG_DAYS,
    RECORD_PRUNE_DAYS,
    REVIEW_BACKFILL_DAYS,
    STORAGE_KEY_PREFIX,
    STORAGE_MINOR_VERSION,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .filtering import (
    is_actionable_todo_assignment,
    is_online_submission_assignment,
    submission_status,
)
from .models import CanvasAssignment, CanvasCourse, CanvasData
from .workflow import (
    AssignmentKind,
    CloseReason,
    FollowupAction,
    FollowupMethod,
    FollowupReason,
    FollowupStage,
    ReviewRole,
    Stage,
    classify_kind,
    display_state,
    followup_actions,
    followup_id,
    grade_percent,
    is_graded,
    record_key,
    should_prune,
)

_LOGGER = logging.getLogger(__name__)

# `last_seen_at` only moves forward this often, so hourly fetches don't rewrite
# the file just to bump a timestamp.
LAST_SEEN_RESOLUTION = timedelta(days=1)
# Only used when the student hasn't written a note of her own.
AUTO_RESOLVED_NOTE = "Auto-resolved: Canvas shows it"


def _dt(value: Any) -> datetime | None:
    """Parse a stored ISO datetime."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    return dt_util.as_utc(parsed)


def _iso(value: datetime | date | None) -> str | None:
    """Serialize a datetime or date."""
    return value.isoformat() if value is not None else None


def _enum[
    E: (
        Stage,
        FollowupStage,
        FollowupReason,
        FollowupMethod,
        AssignmentKind,
        CloseReason,
    )
](kind: type[E], value: Any, default: E) -> E:
    """Parse a stored enum value, falling back to a default."""
    try:
        return kind(value)
    except ValueError:
        return default


@dataclass
class AssignmentSnapshot:
    """Copy of the Canvas details a record needs after Canvas stops returning it."""

    course: str | None
    class_name: str
    name: str
    url: str | None
    due_at: datetime | None
    paper: bool
    canvas_status: str

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "course": self.course,
            "class": self.class_name,
            "name": self.name,
            "url": self.url,
            "due_at": _iso(self.due_at),
            "paper": self.paper,
            "canvas_status": self.canvas_status,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssignmentSnapshot:
        """Parse the stored form."""
        return cls(
            course=raw.get("course"),
            class_name=str(raw.get("class") or ""),
            name=str(raw.get("name") or ""),
            url=raw.get("url"),
            due_at=_dt(raw.get("due_at")),
            paper=bool(raw.get("paper", False)),
            canvas_status=str(raw.get("canvas_status") or ""),
        )


@dataclass
class AssignmentRecord:
    """The student's workflow state for one Canvas assignment."""

    student_id: int
    assignment_id: str
    stage: Stage = Stage.NOT_STARTED
    note: str = ""
    claimed_submitted_at: datetime | None = None
    stage_changed_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None
    snapshot: AssignmentSnapshot | None = None
    # Set when someone corrects how the assignment was classified.
    kind_override: AssignmentKind | None = None
    # Grade review: the student first, then a parent. "by" is the HA user name.
    student_reviewed_at: datetime | None = None
    student_reviewed_by: str | None = None
    parent_reviewed_at: datetime | None = None
    parent_reviewed_by: str | None = None
    # Waiting work closed without a grade (escape hatch).
    closed_at: datetime | None = None
    closed_by: str | None = None
    closed_reason: CloseReason | None = None
    closed_note: str = ""

    @property
    def key(self) -> str:
        """Return the storage key."""
        return record_key(self.student_id, self.assignment_id)

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "student_id": self.student_id,
            "assignment_id": self.assignment_id,
            "stage": self.stage.value,
            "note": self.note,
            "claimed_submitted_at": _iso(self.claimed_submitted_at),
            "stage_changed_at": _iso(self.stage_changed_at),
            "updated_at": _iso(self.updated_at),
            "last_seen_at": _iso(self.last_seen_at),
            "snapshot": self.snapshot.as_dict() if self.snapshot else None,
            "kind_override": self.kind_override.value if self.kind_override else None,
            "student_reviewed_at": _iso(self.student_reviewed_at),
            "student_reviewed_by": self.student_reviewed_by,
            "parent_reviewed_at": _iso(self.parent_reviewed_at),
            "parent_reviewed_by": self.parent_reviewed_by,
            "closed_at": _iso(self.closed_at),
            "closed_by": self.closed_by,
            "closed_reason": self.closed_reason.value if self.closed_reason else None,
            "closed_note": self.closed_note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssignmentRecord:
        """Parse the stored form."""
        snapshot = raw.get("snapshot")
        return cls(
            student_id=int(raw["student_id"]),
            assignment_id=str(raw["assignment_id"]),
            stage=_enum(Stage, raw.get("stage"), Stage.NOT_STARTED),
            note=str(raw.get("note") or ""),
            claimed_submitted_at=_dt(raw.get("claimed_submitted_at")),
            stage_changed_at=_dt(raw.get("stage_changed_at")),
            updated_at=_dt(raw.get("updated_at")),
            last_seen_at=_dt(raw.get("last_seen_at")),
            snapshot=(
                AssignmentSnapshot.from_dict(snapshot)
                if isinstance(snapshot, dict)
                else None
            ),
            kind_override=(
                _enum(AssignmentKind, kind, AssignmentKind.HOMEWORK)
                if (kind := raw.get("kind_override"))
                else None
            ),
            student_reviewed_at=_dt(raw.get("student_reviewed_at")),
            student_reviewed_by=raw.get("student_reviewed_by"),
            parent_reviewed_at=_dt(raw.get("parent_reviewed_at")),
            parent_reviewed_by=raw.get("parent_reviewed_by"),
            closed_at=_dt(raw.get("closed_at")),
            closed_by=raw.get("closed_by"),
            closed_reason=(
                _enum(CloseReason, reason, CloseReason.OTHER)
                if (reason := raw.get("closed_reason"))
                else None
            ),
            closed_note=str(raw.get("closed_note") or ""),
        )


@dataclass
class FollowupRecord:
    """A teacher follow-up for one assignment and reason."""

    assignment_id: str
    student_id: int
    reason: FollowupReason
    created_at: datetime
    contact_by: date
    stage: FollowupStage = FollowupStage.NEEDS_CONTACT
    method: FollowupMethod | None = None
    note: str = ""
    stage_changed_at: datetime | None = None
    resolved_at: datetime | None = None
    # Resolved because Canvas showed the work, not by the student.
    auto_resolved: bool = False

    @property
    def id(self) -> str:
        """Return the follow-up id."""
        return followup_id(self.student_id, self.assignment_id, self.reason)

    @property
    def record_key(self) -> str:
        """Return the key of the assignment record this belongs to."""
        return record_key(self.student_id, self.assignment_id)

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "assignment_id": self.assignment_id,
            "student_id": self.student_id,
            "reason": self.reason.value,
            "stage": self.stage.value,
            "method": self.method.value if self.method else None,
            "note": self.note,
            "created_at": _iso(self.created_at),
            "contact_by": _iso(self.contact_by),
            "stage_changed_at": _iso(self.stage_changed_at),
            "resolved_at": _iso(self.resolved_at),
            "auto_resolved": self.auto_resolved,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FollowupRecord:
        """Parse the stored form."""
        created_at = _dt(raw.get("created_at")) or dt_util.utcnow()
        contact_by = dt_util.parse_date(str(raw.get("contact_by") or ""))
        method = raw.get("method")
        return cls(
            assignment_id=str(raw["assignment_id"]),
            student_id=int(raw["student_id"]),
            reason=FollowupReason(raw["reason"]),
            created_at=created_at,
            contact_by=contact_by or next_local_day(created_at),
            stage=_enum(FollowupStage, raw.get("stage"), FollowupStage.NEEDS_CONTACT),
            method=(
                _enum(FollowupMethod, method, FollowupMethod.EMAIL) if method else None
            ),
            note=str(raw.get("note") or ""),
            stage_changed_at=_dt(raw.get("stage_changed_at")),
            resolved_at=_dt(raw.get("resolved_at")),
            auto_resolved=bool(raw.get("auto_resolved", False)),
        )


@dataclass
class GradeEntry:
    """One graded assignment, kept for grade trends."""

    student_id: int
    assignment_id: str
    course: str | None
    class_name: str
    name: str
    kind: AssignmentKind
    category: str
    score: float | None
    points_possible: float | None
    percent: float | None
    grade: str | None
    graded_at: datetime | None
    late: bool

    @property
    def key(self) -> str:
        """Return the storage key."""
        return record_key(self.student_id, self.assignment_id)

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "student_id": self.student_id,
            "assignment_id": self.assignment_id,
            "course": self.course,
            "class": self.class_name,
            "name": self.name,
            "kind": self.kind.value,
            "category": self.category,
            "score": self.score,
            "points_possible": self.points_possible,
            "percent": self.percent,
            "grade": self.grade,
            "graded_at": _iso(self.graded_at),
            "late": self.late,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GradeEntry:
        """Parse the stored form."""
        return cls(
            student_id=int(raw["student_id"]),
            assignment_id=str(raw["assignment_id"]),
            course=raw.get("course"),
            class_name=str(raw.get("class") or ""),
            name=str(raw.get("name") or ""),
            kind=_enum(AssignmentKind, raw.get("kind"), AssignmentKind.HOMEWORK),
            category=str(raw.get("category") or ""),
            score=raw.get("score"),
            points_possible=raw.get("points_possible"),
            percent=raw.get("percent"),
            grade=raw.get("grade"),
            graded_at=_dt(raw.get("graded_at")),
            late=bool(raw.get("late", False)),
        )


def assignment_kind(
    assignment: CanvasAssignment,
    data: CanvasData,
    record: AssignmentRecord | None,
) -> AssignmentKind:
    """Return the assignment's kind: a stored override, else classified."""
    if record is not None and record.kind_override is not None:
        return record.kind_override
    return classify_kind(
        assignment.name, assignment.is_quiz_assignment, data.group_name(assignment)
    )


def build_grade(
    student_id: int,
    assignment: CanvasAssignment,
    course: CanvasCourse | None,
    kind: AssignmentKind,
    category: str,
) -> GradeEntry:
    """Build the grade log entry for a graded assignment."""
    sub = assignment.submission
    score = sub.score if sub else None
    return GradeEntry(
        student_id=student_id,
        assignment_id=str(assignment.id),
        course=course.name if course else None,
        class_name=course.name.split(" - ")[0].strip() if course else "",
        name=assignment.name.strip(),
        kind=kind,
        category=category,
        score=score,
        points_possible=assignment.points_possible,
        percent=grade_percent(score, assignment.points_possible),
        grade=sub.grade if sub else None,
        graded_at=sub.graded_at if sub else None,
        late=bool(sub and sub.late),
    )


def is_tracked(assignment: CanvasAssignment, kind: AssignmentKind) -> bool:
    """Return True if the workflow tracks this assignment.

    In-class activities are skipped, but quizzes and tests are always
    tracked: taken on paper, they often have no submission type at all.
    """
    return kind is not AssignmentKind.HOMEWORK or is_actionable_todo_assignment(
        assignment
    )


def assignment_graded(
    assignment: CanvasAssignment, kind: AssignmentKind, now: datetime
) -> bool:
    """Return True if the teacher has entered a real grade for the assignment."""
    sub = assignment.submission
    # Canvas fills in the auto-graded part of a quiz before the teacher has
    # graded its written answers; that partial score isn't the grade.
    if sub is None or sub.workflow_state == "pending_review":
        return False
    return is_graded(
        sub.score,
        sub.grade,
        sub.excused,
        submission_status(assignment, now),
        kind,
        not is_online_submission_assignment(assignment),
    )


def in_review_window(
    graded_at: datetime | None, due_at: datetime | None, review_since: datetime | None
) -> bool:
    """Return True if graded work should go through review.

    Work graded before review tracking started (less the backfill) is
    treated as already reviewed. Without a grading time the due date stands
    in; with neither it is left out rather than flooding the list.
    """
    moment = graded_at or due_at
    if moment is None:
        return False
    return review_since is None or moment >= review_since


def next_local_day(moment: datetime) -> date:
    """Return the day after `moment` in Home Assistant's time zone."""
    return dt_util.as_local(moment).date() + timedelta(days=1)


def build_snapshot(
    assignment: CanvasAssignment, course: CanvasCourse | None, now: datetime
) -> AssignmentSnapshot:
    """Copy the Canvas details a record keeps for display."""
    return AssignmentSnapshot(
        course=course.name if course else None,
        class_name=course.name.split(" - ")[0].strip() if course else "",
        name=assignment.name.strip(),
        url=assignment.html_url,
        due_at=assignment.due_at,
        paper=not is_online_submission_assignment(assignment),
        canvas_status=submission_status(assignment, now).value,
    )


def storage_key(account_id: str) -> str:
    """Return the storage key for a Canvas account."""
    return f"{STORAGE_KEY_PREFIX}.{re.sub(r'[^A-Za-z0-9_-]', '_', account_id)}"


class _WorkflowStorage(Store[dict[str, Any]]):
    """Store with a migration hook for future format changes."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate stored data.

        1.1 -> 1.2 only adds fields (review, close, kind override, grade log,
        meta), all of which default when missing.
        """
        return old_data


@dataclass
class _Data:
    assignments: dict[str, AssignmentRecord] = field(default_factory=dict)
    followups: dict[str, FollowupRecord] = field(default_factory=dict)
    grades: dict[str, GradeEntry] = field(default_factory=dict)
    # Graded work older than this was graded before review tracking started.
    review_since: datetime | None = None


class CanvasWorkflowStore:
    """In-memory workflow data backed by a Home Assistant Store file."""

    def __init__(self, hass: HomeAssistant, account_id: str) -> None:
        """Initialize the store for one Canvas account."""
        self._store = _WorkflowStorage(
            hass,
            STORAGE_VERSION,
            storage_key(account_id),
            minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
        )
        self._data = _Data()
        self._save_pending = False
        self._closed = False

    @property
    def key(self) -> str:
        """Return the storage key (the file name under .storage)."""
        return self._store.key

    @property
    def assignments(self) -> dict[str, AssignmentRecord]:
        """Return assignment records keyed by "<student id>:<assignment id>"."""
        return self._data.assignments

    @property
    def followups(self) -> dict[str, FollowupRecord]:
        """Return follow-ups keyed by follow-up id."""
        return self._data.followups

    @property
    def grades(self) -> dict[str, GradeEntry]:
        """Return the grade log keyed by "<student id>:<assignment id>"."""
        return self._data.grades

    @property
    def review_since(self) -> datetime | None:
        """Return when review tracking starts (graded work before it is done)."""
        return self._data.review_since

    def assignment(
        self, student_id: int, assignment_id: str
    ) -> AssignmentRecord | None:
        """Return one student's record for an assignment, if any."""
        return self._data.assignments.get(record_key(student_id, assignment_id))

    async def async_load(self) -> None:
        """Load stored data. Unreadable records are skipped, not fatal."""
        raw = await self._store.async_load() or {}
        data = _Data()
        for key, record in (raw.get("assignments") or {}).items():
            try:
                parsed = AssignmentRecord.from_dict(record)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unreadable workflow record %s: %s", key, err)
                continue
            data.assignments[parsed.key] = parsed
        for fid, record in (raw.get("followups") or {}).items():
            try:
                followup = FollowupRecord.from_dict(record)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unreadable follow-up %s: %s", fid, err)
                continue
            data.followups[followup.id] = followup
        for key, entry in (raw.get("grades") or {}).items():
            try:
                grade = GradeEntry.from_dict(entry)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unreadable grade %s: %s", key, err)
                continue
            data.grades[grade.key] = grade
        meta = raw.get("meta") or {}
        data.review_since = _dt(meta.get("review_since"))
        self._data = data
        if data.review_since is None:
            # First load with review tracking: include recent grades.
            data.review_since = dt_util.utcnow() - timedelta(days=REVIEW_BACKFILL_DAYS)
            self._schedule_save()

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form of all data."""
        return {
            "assignments": {
                key: rec.as_dict() for key, rec in self._data.assignments.items()
            },
            "followups": {
                fid: rec.as_dict() for fid, rec in self._data.followups.items()
            },
            "grades": {key: g.as_dict() for key, g in self._data.grades.items()},
            "meta": {"review_since": _iso(self._data.review_since)},
        }

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        return self.as_dict()

    def _schedule_save(self) -> None:
        """Save soon (debounced); used for changes the student didn't make."""
        if self._closed:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    async def _async_save_now(self) -> None:
        """Save immediately; used for the student's own edits."""
        await self._store.async_save(self._data_to_save())

    async def async_close(self) -> None:
        """Write any pending save and stop writing (on unload).

        A fetch still in flight from the unloading entry must not write this
        copy later, over edits made through the reloaded entry.
        """
        if self._save_pending:
            await self._async_save_now()
        self._closed = True

    async def async_remove(self) -> None:
        """Delete the storage file."""
        await self._store.async_remove()
        self._data = _Data()
        self._save_pending = False

    def followups_for(
        self, student_id: int, assignment_id: str
    ) -> dict[FollowupReason, FollowupRecord]:
        """Return the follow-ups for one student's assignment, keyed by reason."""
        key = record_key(student_id, assignment_id)
        return {
            f.reason: f for f in self._data.followups.values() if f.record_key == key
        }

    async def async_set_assignment(
        self,
        assignment_id: str,
        student_id: int,
        now: datetime,
        *,
        stage: Stage | None = None,
        note: str | None = None,
        kind: AssignmentKind | None = None,
        clear_kind: bool = False,
        snapshot: AssignmentSnapshot | None = None,
    ) -> AssignmentRecord:
        """Set the stage, note and/or kind on an assignment and save immediately.

        `clear_kind` goes back to the automatic classification.
        """
        record = self._record(student_id, assignment_id)
        if clear_kind:
            record.kind_override = None
        elif kind is not None:
            record.kind_override = kind
        if stage is not None and stage is not record.stage:
            record.stage = stage
            record.stage_changed_at = now
            record.claimed_submitted_at = (
                now if stage is Stage.SUBMITTED_CLAIMED else None
            )
        if note is not None:
            record.note = note
        return await self._async_touch(record, now, snapshot)

    def _record(self, student_id: int, assignment_id: str) -> AssignmentRecord:
        """Return the record for an assignment, creating it if needed."""
        key = record_key(student_id, assignment_id)
        record = self._data.assignments.get(key)
        if record is None:
            record = AssignmentRecord(
                student_id=student_id, assignment_id=assignment_id
            )
            self._data.assignments[key] = record
        return record

    async def _async_touch(
        self,
        record: AssignmentRecord,
        now: datetime,
        snapshot: AssignmentSnapshot | None,
    ) -> AssignmentRecord:
        """Record an edit and save immediately."""
        if snapshot is not None:
            record.snapshot = snapshot
            record.last_seen_at = now
        record.updated_at = now
        await self._async_save_now()
        return record

    async def async_mark_reviewed(
        self,
        assignment_id: str,
        student_id: int,
        now: datetime,
        *,
        role: ReviewRole,
        by: str | None,
        undo: bool = False,
        snapshot: AssignmentSnapshot | None = None,
    ) -> AssignmentRecord:
        """Mark a grade reviewed (or not) by the student or a parent.

        Undoing the student's review also undoes the parent's, which follows it.
        """
        record = self._record(student_id, assignment_id)
        if role is ReviewRole.STUDENT:
            if undo:
                record.student_reviewed_at = record.student_reviewed_by = None
                record.parent_reviewed_at = record.parent_reviewed_by = None
            else:
                record.student_reviewed_at, record.student_reviewed_by = now, by
        elif undo:
            record.parent_reviewed_at = record.parent_reviewed_by = None
        else:
            record.parent_reviewed_at, record.parent_reviewed_by = now, by
        return await self._async_touch(record, now, snapshot)

    async def async_close_without_grade(
        self,
        assignment_id: str,
        student_id: int,
        now: datetime,
        *,
        reason: CloseReason | None,
        note: str | None,
        by: str | None,
        undo: bool = False,
        snapshot: AssignmentSnapshot | None = None,
    ) -> AssignmentRecord:
        """Close waiting work without a grade, or reopen it (`undo`)."""
        record = self._record(student_id, assignment_id)
        if undo:
            record.closed_at = record.closed_by = record.closed_reason = None
            record.closed_note = ""
        else:
            record.closed_at, record.closed_by = now, by
            record.closed_reason = reason or CloseReason.OTHER
            record.closed_note = note or ""
        return await self._async_touch(record, now, snapshot)

    async def async_set_followup(
        self,
        followup_id_: str,
        now: datetime,
        *,
        stage: FollowupStage | None = None,
        method: FollowupMethod | None = None,
        clear_method: bool = False,
        note: str | None = None,
    ) -> FollowupRecord:
        """Update a follow-up and save immediately. Raises KeyError if unknown."""
        followup = self._data.followups[followup_id_]
        if stage is not None and stage is not followup.stage:
            followup.stage = stage
            followup.stage_changed_at = now
            followup.resolved_at = now if stage is FollowupStage.RESOLVED else None
            followup.auto_resolved = False
        if clear_method:
            followup.method = None
        elif method is not None:
            followup.method = method
        if note is not None:
            followup.note = note
        await self._async_save_now()
        return followup

    def reconcile(
        self,
        data: CanvasData,
        now: datetime,
        online_grace: timedelta,
        paper_grace: timedelta,
    ) -> bool:
        """Bring stored records in line with the latest successful Canvas fetch.

        Opens and auto-resolves follow-ups, refreshes snapshots and
        `last_seen_at`, and prunes long-gone records. Returns True if anything
        changed (a debounced save is then scheduled).
        """
        if self._closed:
            return False
        changed = False
        seen: set[str] = set()
        by_record: dict[str, dict[FollowupReason, FollowupRecord]] = defaultdict(dict)
        for followup in self._data.followups.values():
            by_record[followup.record_key][followup.reason] = followup

        for student_id in data.assignments_by_student.keys() | (
            data.term_assignments_by_student.keys()
        ):
            courses = {c.id: c for c in data.courses_by_student.get(student_id, [])}
            # Follow-ups only open for work on the to-do list; older term work
            # (e.g. late work from last month) must not open a flood of them.
            on_list = {
                str(a.id) for a in data.assignments_by_student.get(student_id, [])
            }
            for assignment in data.term_assignments(student_id):
                aid = str(assignment.id)
                key = record_key(student_id, aid)
                seen.add(key)
                record = self._data.assignments.get(key)
                if record is not None and (
                    record.last_seen_at is None
                    or now - record.last_seen_at >= LAST_SEEN_RESOLUTION
                ):
                    record.last_seen_at = now
                    changed = True
                kind = assignment_kind(assignment, data, record)
                course = courses.get(assignment.course_id)
                graded = assignment_graded(assignment, kind, now)
                changed |= self._log_grade(
                    student_id, assignment, course, kind, graded, data
                )
                if not is_tracked(assignment, kind):
                    continue

                status = submission_status(assignment, now)
                paper = not is_online_submission_assignment(assignment)
                state = display_state(
                    status,
                    record.stage if record else Stage.NOT_STARTED,
                    record.claimed_submitted_at if record else None,
                    paper,
                    now,
                    online_grace,
                    paper_grace,
                    kind=kind,
                )
                existing = by_record.get(key, {})
                actions = [
                    (reason, action)
                    for reason, action in followup_actions(
                        status,
                        state,
                        {r: f.stage for r, f in existing.items()},
                        graded=graded,
                        closed=bool(record and record.closed_at),
                    )
                    if action is not FollowupAction.OPEN or aid in on_list
                ]
                if record is None and not actions:
                    continue
                if record is None:
                    record = AssignmentRecord(
                        student_id=student_id, assignment_id=aid, last_seen_at=now
                    )
                    self._data.assignments[key] = record
                    changed = True

                snapshot = build_snapshot(assignment, course, now)
                if record.snapshot != snapshot:
                    record.snapshot = snapshot
                    changed = True

                for reason, action in actions:
                    changed = True
                    if action is FollowupAction.OPEN:
                        followup = FollowupRecord(
                            assignment_id=aid,
                            student_id=student_id,
                            reason=reason,
                            created_at=now,
                            contact_by=next_local_day(now),
                            stage_changed_at=now,
                        )
                        self._data.followups[followup.id] = followup
                    else:
                        # Keep whatever she wrote (method, note): it's hers.
                        followup = existing[reason]
                        followup.stage = FollowupStage.RESOLVED
                        followup.stage_changed_at = now
                        followup.resolved_at = now
                        followup.auto_resolved = True
                        if not followup.note:
                            followup.note = AUTO_RESOLVED_NOTE

        prune_after = timedelta(days=RECORD_PRUNE_DAYS)
        for key, record in list(self._data.assignments.items()):
            if key in seen:
                continue
            followups = by_record.get(key, {})
            # A record never matched to Canvas ages from its last edit instead.
            if should_prune(
                record.last_seen_at or record.updated_at,
                [f.stage for f in followups.values()],
                now,
                prune_after,
            ):
                del self._data.assignments[key]
                for followup in followups.values():
                    self._data.followups.pop(followup.id, None)
                changed = True
        # Follow-ups whose record was lost (e.g. unreadable) age on their own.
        for fid, followup in list(self._data.followups.items()):
            if followup.record_key in self._data.assignments or (
                followup.record_key in seen
            ):
                continue
            if should_prune(
                followup.stage_changed_at or followup.created_at,
                [followup.stage],
                now,
                prune_after,
            ):
                del self._data.followups[fid]
                changed = True

        # Grades are kept for trends well after Canvas stops returning them.
        keep_grades = timedelta(days=GRADE_LOG_DAYS)
        for key, grade in list(self._data.grades.items()):
            if grade.graded_at is not None and now - grade.graded_at > keep_grades:
                del self._data.grades[key]
                changed = True

        if changed:
            self._schedule_save()
        return changed

    def _log_grade(
        self,
        student_id: int,
        assignment: CanvasAssignment,
        course: CanvasCourse | None,
        kind: AssignmentKind,
        graded: bool,
        data: CanvasData,
    ) -> bool:
        """Add, update or drop an assignment's grade log entry; True if changed."""
        key = record_key(student_id, str(assignment.id))
        if not graded:
            # The grade was removed in Canvas (e.g. entered on the wrong student).
            return self._data.grades.pop(key, None) is not None
        entry = build_grade(
            student_id, assignment, course, kind, data.group_name(assignment)
        )
        if self._data.grades.get(key) == entry:
            return False
        self._data.grades[key] = entry
        return True
