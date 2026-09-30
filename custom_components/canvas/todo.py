"""Canvas LMS To-Do List platform.

The list is a view: Canvas assignments are rebuilt from each fetch, and the
student's stages, notes and follow-ups come from the workflow store. Nothing
is stored by the entity itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CanvasConfigEntry
from .const import (
    CONF_BASE_URL,
    DOMAIN,
    FOLLOWUP_UID_PREFIX,
    FOLLOWUP_VISIBLE_DAYS,
    UPCOMING_WINDOW_DAYS,
)
from .coordinator import CanvasDataUpdateCoordinator
from .filtering import (
    SubmissionStatus,
    is_online_submission_assignment,
    submission_status,
)
from .models import CanvasAssignment, CanvasCourse, CanvasObservee
from .store import (
    AssignmentRecord,
    FollowupRecord,
    assignment_graded,
    assignment_kind,
    in_review_window,
    is_tracked,
)
from .workflow import (
    FOLLOWUP_PREFIX,
    AssignmentKind,
    Bucket,
    DisplayState,
    FollowupStage,
    Stage,
    display_state,
    grade_percent,
    is_escalated,
    is_overdue,
    summary_prefix,
    work_bucket,
    work_done,
)

_LOGGER = logging.getLogger(__name__)

NOT_EDITABLE = (
    "Canvas assignments can't be edited here. Set a stage or note with the "
    "Canvas workflow card instead."
)
COMPLETED_BY_CANVAS = (
    "Canvas assignments complete automatically once Canvas shows them "
    "submitted. Submit it in Canvas, then refresh."
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CanvasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Canvas LMS todo entities from config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        CanvasTodoListEntity(coordinator=coordinator, student=student, entry=entry)
        for student in coordinator.data.observees
    )


# Lists shown on the dashboard, in the order the card shows them.
_LISTED_BUCKETS: tuple[Bucket, ...] = (
    Bucket.ATTENTION,
    Bucket.UPCOMING,
    Bucket.ASSESSMENT,
    Bucket.WAITING,
    Bucket.REVIEW,
    Bucket.PARENT_REVIEW,
    Bucket.CLOSED,
)
# Graded work is listed newest grade first; everything else by due date.
_NEWEST_FIRST: dict[Bucket, str] = {
    Bucket.REVIEW: "graded_at",
    Bucket.PARENT_REVIEW: "student_reviewed_at",
    Bucket.CLOSED: "closed_at",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sorted(bucket: Bucket, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort one list for display."""
    if (field := _NEWEST_FIRST.get(bucket)) is not None:
        return sorted(items, key=lambda i: i[field] or "", reverse=True)
    return sorted(items, key=lambda i: i["due"] or "9999")


def _grade_fields(
    assignment: CanvasAssignment,
    graded: bool,
    percent: float | None,
    escalated: bool,
) -> dict[str, Any]:
    """Grade details for a compact item."""
    sub = assignment.submission if graded else None
    return {
        "graded": graded,
        "score": sub.score if sub else None,
        "points_possible": assignment.points_possible,
        "grade": sub.grade if sub else None,
        "percent": percent,
        "escalated": escalated,
        "graded_at": _iso(sub.graded_at) if sub else None,
    }


def _review_fields(record: AssignmentRecord | None) -> dict[str, Any]:
    """Review and close details for a compact item."""
    if record is None:
        return {
            "student_reviewed_at": None,
            "student_reviewed_by": None,
            "parent_reviewed_at": None,
            "parent_reviewed_by": None,
            "closed_at": None,
            "closed_by": None,
            "closed_reason": None,
            "closed_note": "",
        }
    return {
        "student_reviewed_at": _iso(record.student_reviewed_at),
        "student_reviewed_by": record.student_reviewed_by,
        "parent_reviewed_at": _iso(record.parent_reviewed_at),
        "parent_reviewed_by": record.parent_reviewed_by,
        "closed_at": _iso(record.closed_at),
        "closed_by": record.closed_by,
        "closed_reason": record.closed_reason.value if record.closed_reason else None,
        "closed_note": record.closed_note,
    }


def _followup_visible(followup: FollowupRecord, now: datetime) -> bool:
    """Open follow-ups always show; resolved ones for a week."""
    if followup.stage is not FollowupStage.RESOLVED:
        return True
    resolved_at = followup.resolved_at or followup.stage_changed_at
    return resolved_at is not None and now - resolved_at <= timedelta(
        days=FOLLOWUP_VISIBLE_DAYS
    )


class CanvasTodoListEntity(
    CoordinatorEntity[CanvasDataUpdateCoordinator], TodoListEntity
):
    """To-do list of Canvas assignments and follow-ups for one student."""

    _attr_has_entity_name = True
    _attr_translation_key = "assignments"
    # Item lists are for dashboards/automations; keep them out of the recorder.
    _unrecorded_attributes = frozenset(
        {
            "attention_items",
            "upcoming_items",
            "assessment_items",
            "waiting_items",
            "review_items",
            "parent_review_items",
            "closed_items",
            "late_items",
            "followup_items",
        }
    )
    # Only follow-up tasks can be ticked. Canvas owns assignments; personal
    # reminders belong in a separate (e.g. Local To-do) list.
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(
        self,
        coordinator: CanvasDataUpdateCoordinator,
        student: CanvasObservee,
        entry: CanvasConfigEntry,
    ) -> None:
        """Initialize the Canvas student To-Do list entity."""
        super().__init__(coordinator)
        self.student = student
        self.entry = entry

        self._attr_unique_id = f"{entry.unique_id}_{student.id}_todo"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_{student.id}")},
            name=student.name,
            manufacturer="Instructure Canvas",
            model="Student Profile",
            configuration_url=entry.data[CONF_BASE_URL],
            entry_type=DeviceEntryType.SERVICE,
        )
        self._update_todo_items()

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the current list of To-Do items."""
        return self._attr_todo_items

    def _get_course_map(self) -> dict[int, CanvasCourse]:
        """Return a mapping of course ID to CanvasCourse for this student."""
        courses = self.coordinator.data.courses_by_student.get(self.student.id, [])
        return {course.id: course for course in courses}

    def _update_todo_items(self) -> None:
        """Combine Canvas assignments and workflow records into TodoItems.

        To-do items come from the to-do list (open work plus a week of
        finished work). The dashboard lists come from all current-term work,
        since grades and "waiting on the teacher" can come weeks later.
        """
        items: list[TodoItem] = []
        counts: dict[SubmissionStatus, int] = {}
        display_counts: dict[DisplayState, int] = {}
        overdue_count = 0
        done_not_submitted_overdue = 0
        late_items: list[dict[str, Any]] = []
        lists: dict[Bucket, list[dict[str, Any]]] = {
            bucket: [] for bucket in _LISTED_BUCKETS
        }
        undated_count = 0
        escalated_count = 0
        now = dt_util.utcnow()
        upcoming_window = timedelta(days=UPCOMING_WINDOW_DAYS)
        online_grace, paper_grace = self.coordinator.grace_windows
        threshold = self.coordinator.escalation_threshold
        workflow = self.coordinator.workflow
        data = self.coordinator.data
        course_map = self._get_course_map()
        on_list = {
            str(a.id) for a in data.assignments_by_student.get(self.student.id, [])
        }
        in_canvas: set[str] = set()

        for assignment in data.term_assignments(self.student.id):
            uid = str(assignment.id)
            in_canvas.add(uid)
            record = workflow.assignment(self.student.id, uid)
            kind = assignment_kind(assignment, data, record)
            if not is_tracked(assignment, kind):
                continue

            course = course_map.get(assignment.course_id)
            canvas_status = submission_status(assignment, now)
            on_paper = not is_online_submission_assignment(assignment)
            listed = uid in on_list

            # Undated, never-submitted items are mostly stale course-template
            # clutter; count them but keep them off the list.
            if canvas_status is SubmissionStatus.UPCOMING and assignment.due_at is None:
                undated_count += listed
                continue

            stage = record.stage if record else Stage.NOT_STARTED
            claimed_at = record.claimed_submitted_at if record else None
            state = display_state(
                canvas_status,
                stage,
                claimed_at,
                on_paper,
                now,
                online_grace,
                paper_grace,
                kind=kind,
            )
            overdue = is_overdue(canvas_status, assignment.due_at, now)
            graded = assignment_graded(assignment, kind, now)
            sub = assignment.submission
            graded_at = sub.graded_at if sub else None
            percent = (
                grade_percent(sub.score, assignment.points_possible)
                if graded and sub
                else None
            )
            escalated = graded and is_escalated(percent, threshold)
            bucket = work_bucket(
                kind=kind,
                state=state,
                canvas_status=canvas_status,
                graded=graded,
                overdue=overdue,
                due_at=assignment.due_at,
                now=now,
                upcoming_window=upcoming_window,
                lead=self.coordinator.lead_time(kind),
                in_review_window=in_review_window(
                    graded_at, assignment.due_at, workflow.review_since
                ),
                student_reviewed=bool(record and record.student_reviewed_at),
                parent_reviewed=bool(record and record.parent_reviewed_at),
                closed=bool(record and record.closed_at),
            )
            compact = {
                "uid": uid,
                "course": course.name if course else None,
                "class": (course.name.split(" - ")[0].strip() if course else ""),
                "assignment": assignment.name.strip(),
                "kind": kind.value,
                "kind_override": bool(record and record.kind_override),
                "category": data.group_name(assignment),
                "status": canvas_status.value,
                "paper": on_paper,
                "due": assignment.due_at.isoformat() if assignment.due_at else None,
                "url": assignment.html_url,
                "stage": stage.value,
                "note": record.note if record else "",
                "display_state": state.value,
                "overdue": overdue,
                "claimed_submitted_at": _iso(claimed_at),
                "bucket": bucket.value,
                **_grade_fields(assignment, graded, percent, escalated),
                **_review_fields(record),
            }
            if bucket in lists:
                lists[bucket].append(compact)
                escalated_count += escalated and bucket in (
                    Bucket.REVIEW,
                    Bucket.PARENT_REVIEW,
                )
            if not listed:
                continue

            counts[canvas_status] = counts.get(canvas_status, 0) + 1
            display_counts[state] = display_counts.get(state, 0) + 1
            overdue_count += overdue
            done_not_submitted_overdue += (
                overdue and state is DisplayState.DONE_NOT_SUBMITTED
            )
            if canvas_status is SubmissionStatus.LATE:
                late_items.append(compact)
            items.append(
                self._todo_item(assignment, course, kind, state, canvas_status, record)
            )

        followup_items, followup_counts = self._followups(now, in_canvas, items)
        items.sort(key=self._item_sort_key)

        self._attr_todo_items = items
        self._attr_extra_state_attributes = {
            # The card sends this so siblings in one class stay separate.
            "student_id": self.student.id,
            **{
                f"{status.value}_count": counts.get(status, 0)
                for status in SubmissionStatus
            },
            **{
                f"display_{state.value}_count": display_counts.get(state, 0)
                for state in DisplayState
            },
            "undated_count": undated_count,
            "overdue_count": overdue_count,
            "done_not_submitted_overdue_count": done_not_submitted_overdue,
            "not_in_canvas_count": display_counts.get(DisplayState.NOT_IN_CANVAS, 0),
            **{
                f"{bucket.value}_count": len(listed) for bucket, listed in lists.items()
            },
            "escalated_count": escalated_count,
            **followup_counts,
            "late_items": late_items,
            **{
                f"{bucket.value}_items": _sorted(bucket, listed)
                for bucket, listed in lists.items()
            },
            "followup_items": followup_items,
        }

    def _todo_item(
        self,
        assignment: CanvasAssignment,
        course: CanvasCourse | None,
        kind: AssignmentKind,
        state: DisplayState,
        canvas_status: SubmissionStatus,
        record: AssignmentRecord | None,
    ) -> TodoItem:
        """Build the to-do item for an assignment on the list."""
        on_paper = not is_online_submission_assignment(assignment)
        course_prefix = f"[{course.name}] " if course and course.name else ""
        note = record.note if record else ""
        status_line = f"Status: {state.value}" + (" (paper)" if on_paper else "")
        if kind is not AssignmentKind.HOMEWORK:
            status_line += f" · {kind.value}"
        return TodoItem(
            uid=str(assignment.id),
            summary=(
                f"{summary_prefix(state, canvas_status, kind)}{course_prefix}"
                f"{assignment.name}{' 📝' if on_paper else ''}"
            ),
            # Canvas is the only thing that completes an assignment; a
            # claimed submission never does.
            status=(
                TodoItemStatus.COMPLETED
                if work_done(canvas_status, kind, on_paper)
                else TodoItemStatus.NEEDS_ACTION
            ),
            due=assignment.due_at,
            description="\n".join(
                filter(
                    None,
                    [
                        status_line,
                        f"Note: {note}" if note else None,
                        assignment.html_url,
                    ],
                )
            ),
        )

    def _followups(
        self, now: datetime, in_canvas: set[str], items: list[TodoItem]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Add follow-up tasks to `items`; return the compact list and counts."""
        workflow = self.coordinator.workflow
        today = dt_util.as_local(now).date()
        compact_items: list[dict[str, Any]] = []
        open_count = needs_contact = overdue = 0

        for followup in workflow.followups.values():
            if followup.student_id != self.student.id:
                continue
            if followup.stage is not FollowupStage.RESOLVED:
                open_count += 1
            if followup.stage is FollowupStage.NEEDS_CONTACT:
                needs_contact += 1
                overdue += followup.contact_by < today
            if not _followup_visible(followup, now):
                continue

            record = workflow.assignments.get(followup.record_key)
            snapshot = record.snapshot if record else None
            name = snapshot.name if snapshot else f"Assignment {followup.assignment_id}"
            course = snapshot.course if snapshot else None
            url = snapshot.url if snapshot else None
            still_in_canvas = followup.assignment_id in in_canvas
            stage_line = f"Follow-up: {followup.reason.value} · {followup.stage.value}"
            if followup.method:
                stage_line += f" · {followup.method.value}"

            items.append(
                TodoItem(
                    uid=f"{FOLLOWUP_UID_PREFIX}{followup.id}",
                    summary=(
                        f"{FOLLOWUP_PREFIX[followup.reason]}"
                        f"{f'[{course}] ' if course else ''}{name}"
                    ),
                    # Ticking means "contacted": the part the student controls.
                    status=(
                        TodoItemStatus.NEEDS_ACTION
                        if followup.stage is FollowupStage.NEEDS_CONTACT
                        else TodoItemStatus.COMPLETED
                    ),
                    due=followup.contact_by,
                    description="\n".join(
                        filter(
                            None,
                            [
                                stage_line,
                                f"Note: {followup.note}" if followup.note else None,
                                None if still_in_canvas else "No longer in Canvas",
                                url,
                            ],
                        )
                    ),
                )
            )
            compact_items.append(
                {
                    "id": followup.id,
                    "assignment_id": followup.assignment_id,
                    "reason": followup.reason.value,
                    "stage": followup.stage.value,
                    "method": followup.method.value if followup.method else None,
                    "note": followup.note,
                    "class": snapshot.class_name if snapshot else "",
                    "assignment": name,
                    "url": url,
                    "created_at": followup.created_at.isoformat(),
                    "contact_by": followup.contact_by.isoformat(),
                    "resolved_at": (
                        followup.resolved_at.isoformat()
                        if followup.resolved_at
                        else None
                    ),
                    "overdue": followup.stage is FollowupStage.NEEDS_CONTACT
                    and followup.contact_by < today,
                    "in_canvas": still_in_canvas,
                }
            )

        compact_items.sort(key=lambda i: (i["stage"] == "resolved", i["contact_by"]))
        return compact_items, {
            "followup_open_count": open_count,
            "followup_needs_contact_count": needs_contact,
            "followup_overdue_count": overdue,
        }

    @staticmethod
    def _item_sort_key(item: TodoItem) -> tuple[int, datetime, str]:
        """Sort key to order To-Do items by due date ascending, then summary."""
        due = item.due
        summary = item.summary or ""
        if due is None:
            return (1, datetime.max.replace(tzinfo=timezone.utc), summary)
        if isinstance(due, datetime):
            due_dt = due if due.tzinfo is not None else due.replace(tzinfo=timezone.utc)
            return (0, due_dt, summary)
        due_dt = datetime.combine(due, datetime.min.time(), tzinfo=timezone.utc)
        return (0, due_dt, summary)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_todo_items()
        super()._handle_coordinator_update()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Tick or untick a follow-up task; Canvas assignments are read-only."""
        current = next(
            (i for i in self._attr_todo_items or [] if i.uid == item.uid), None
        )
        if current is None or item.uid is None:
            return
        if item.summary is not None and item.summary != current.summary:
            raise HomeAssistantError(NOT_EDITABLE)
        status_changed = item.status is not None and item.status != current.status

        if not item.uid.startswith(FOLLOWUP_UID_PREFIX):
            if status_changed:
                raise HomeAssistantError(COMPLETED_BY_CANVAS)
            return
        if not status_changed:
            return

        stage = (
            FollowupStage.CONTACTED
            if item.status == TodoItemStatus.COMPLETED
            else FollowupStage.NEEDS_CONTACT
        )
        await self.coordinator.workflow.async_set_followup(
            item.uid.removeprefix(FOLLOWUP_UID_PREFIX), dt_util.utcnow(), stage=stage
        )
        self.coordinator.async_workflow_changed()
