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
    RECORD_PRUNE_DAYS,
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
    FollowupAction,
    FollowupMethod,
    FollowupReason,
    FollowupStage,
    Stage,
    display_state,
    followup_actions,
    followup_id,
    should_prune,
)

_LOGGER = logging.getLogger(__name__)

# `last_seen_at` only moves forward this often, so hourly fetches don't rewrite
# the file just to bump a timestamp.
LAST_SEEN_RESOLUTION = timedelta(days=1)
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


def _enum[E: (Stage, FollowupStage, FollowupReason, FollowupMethod)](
    kind: type[E], value: Any, default: E
) -> E:
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
    stage: Stage = Stage.NOT_STARTED
    note: str = ""
    claimed_submitted_at: datetime | None = None
    stage_changed_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None
    snapshot: AssignmentSnapshot | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "student_id": self.student_id,
            "stage": self.stage.value,
            "note": self.note,
            "claimed_submitted_at": _iso(self.claimed_submitted_at),
            "stage_changed_at": _iso(self.stage_changed_at),
            "updated_at": _iso(self.updated_at),
            "last_seen_at": _iso(self.last_seen_at),
            "snapshot": self.snapshot.as_dict() if self.snapshot else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssignmentRecord:
        """Parse the stored form."""
        snapshot = raw.get("snapshot")
        return cls(
            student_id=int(raw["student_id"]),
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

    @property
    def id(self) -> str:
        """Return the follow-up id."""
        return followup_id(self.assignment_id, self.reason)

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
        )


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
        """Migrate stored data. Version 1.1 is the first format."""
        return old_data


@dataclass
class _Data:
    assignments: dict[str, AssignmentRecord] = field(default_factory=dict)
    followups: dict[str, FollowupRecord] = field(default_factory=dict)


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

    @property
    def key(self) -> str:
        """Return the storage key (the file name under .storage)."""
        return self._store.key

    @property
    def assignments(self) -> dict[str, AssignmentRecord]:
        """Return assignment records keyed by Canvas assignment id."""
        return self._data.assignments

    @property
    def followups(self) -> dict[str, FollowupRecord]:
        """Return follow-ups keyed by follow-up id."""
        return self._data.followups

    async def async_load(self) -> None:
        """Load stored data. Unreadable records are skipped, not fatal."""
        raw = await self._store.async_load() or {}
        data = _Data()
        for aid, record in (raw.get("assignments") or {}).items():
            try:
                data.assignments[str(aid)] = AssignmentRecord.from_dict(record)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unreadable workflow record %s: %s", aid, err)
        for fid, record in (raw.get("followups") or {}).items():
            try:
                followup = FollowupRecord.from_dict(record)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unreadable follow-up %s: %s", fid, err)
                continue
            data.followups[followup.id] = followup
        self._data = data

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form of all data."""
        return {
            "assignments": {
                aid: rec.as_dict() for aid, rec in self._data.assignments.items()
            },
            "followups": {
                fid: rec.as_dict() for fid, rec in self._data.followups.items()
            },
        }

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        return self.as_dict()

    def _schedule_save(self) -> None:
        """Save soon (debounced); used for changes the student didn't make."""
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    async def _async_save_now(self) -> None:
        """Save immediately; used for the student's own edits."""
        await self._store.async_save(self._data_to_save())

    async def async_flush(self) -> None:
        """Write any pending debounced save now (on unload)."""
        if self._save_pending:
            await self._async_save_now()

    async def async_remove(self) -> None:
        """Delete the storage file."""
        await self._store.async_remove()
        self._data = _Data()
        self._save_pending = False

    def followups_for(self, assignment_id: str) -> dict[FollowupReason, FollowupRecord]:
        """Return the follow-ups for one assignment, keyed by reason."""
        return {
            f.reason: f
            for f in self._data.followups.values()
            if f.assignment_id == assignment_id
        }

    async def async_set_assignment(
        self,
        assignment_id: str,
        student_id: int,
        now: datetime,
        *,
        stage: Stage | None = None,
        note: str | None = None,
        snapshot: AssignmentSnapshot | None = None,
    ) -> AssignmentRecord:
        """Set the stage and/or note on an assignment and save immediately."""
        record = self._data.assignments.get(assignment_id)
        if record is None:
            record = AssignmentRecord(student_id=student_id)
            self._data.assignments[assignment_id] = record
        if stage is not None and stage is not record.stage:
            record.stage = stage
            record.stage_changed_at = now
            record.claimed_submitted_at = (
                now if stage is Stage.SUBMITTED_CLAIMED else None
            )
        if note is not None:
            record.note = note
        if snapshot is not None:
            record.snapshot = snapshot
            record.last_seen_at = now
        record.updated_at = now
        await self._async_save_now()
        return record

    async def async_set_followup(
        self,
        followup_id_: str,
        now: datetime,
        *,
        stage: FollowupStage | None = None,
        method: FollowupMethod | None = None,
        note: str | None = None,
    ) -> FollowupRecord:
        """Update a follow-up and save immediately. Raises KeyError if unknown."""
        followup = self._data.followups[followup_id_]
        if stage is not None and stage is not followup.stage:
            followup.stage = stage
            followup.stage_changed_at = now
            followup.resolved_at = now if stage is FollowupStage.RESOLVED else None
        if method is not None:
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
        changed = False
        seen: set[str] = set()
        by_assignment: dict[str, dict[FollowupReason, FollowupRecord]] = defaultdict(
            dict
        )
        for followup in self._data.followups.values():
            by_assignment[followup.assignment_id][followup.reason] = followup

        for student_id, assignments in data.assignments_by_student.items():
            courses = {c.id: c for c in data.courses_by_student.get(student_id, [])}
            for assignment in assignments:
                if not is_actionable_todo_assignment(assignment):
                    continue
                aid = str(assignment.id)
                seen.add(aid)
                status = submission_status(assignment, now)
                paper = not is_online_submission_assignment(assignment)
                record = self._data.assignments.get(aid)
                state = display_state(
                    status,
                    record.stage if record else Stage.NOT_STARTED,
                    record.claimed_submitted_at if record else None,
                    paper,
                    now,
                    online_grace,
                    paper_grace,
                )
                existing = by_assignment.get(aid, {})
                actions = followup_actions(
                    status, state, {r: f.stage for r, f in existing.items()}
                )
                if record is None and not actions:
                    continue
                if record is None:
                    record = AssignmentRecord(student_id=student_id)
                    self._data.assignments[aid] = record
                    changed = True

                snapshot = build_snapshot(
                    assignment, courses.get(assignment.course_id), now
                )
                if record.snapshot != snapshot:
                    record.snapshot = snapshot
                    changed = True
                if (
                    record.last_seen_at is None
                    or now - record.last_seen_at >= LAST_SEEN_RESOLUTION
                ):
                    record.last_seen_at = now
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
                        followup = existing[reason]
                        followup.stage = FollowupStage.RESOLVED
                        followup.method = None
                        followup.note = AUTO_RESOLVED_NOTE
                        followup.stage_changed_at = now
                        followup.resolved_at = now

        prune_after = timedelta(days=RECORD_PRUNE_DAYS)
        for aid, record in list(self._data.assignments.items()):
            if aid in seen:
                continue
            followups = by_assignment.get(aid, {})
            # A record never matched to Canvas ages from its last edit instead.
            if should_prune(
                record.last_seen_at or record.updated_at,
                [f.stage for f in followups.values()],
                now,
                prune_after,
            ):
                del self._data.assignments[aid]
                for followup in followups.values():
                    self._data.followups.pop(followup.id, None)
                changed = True

        if changed:
            self._schedule_save()
        return changed
