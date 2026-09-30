"""Canvas LMS To-Do List platform."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import logging
from typing import Any, Self
from uuid import uuid4

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
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CanvasConfigEntry
from .const import CONF_BASE_URL, DOMAIN
from .coordinator import CanvasDataUpdateCoordinator
from .filtering import (
    SubmissionStatus,
    is_actionable_todo_assignment,
    is_done,
    is_online_submission_assignment,
    submission_status,
)
from .models import CanvasCourse, CanvasObservee

_LOGGER = logging.getLogger(__name__)

STATUS_PREFIX: dict[SubmissionStatus, str] = {
    SubmissionStatus.MISSING: "🔴 MISSING · ",
    SubmissionStatus.ZEROED: "⭕ ZERO · ",
    SubmissionStatus.LATE: "✉️ LATE · ",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CanvasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Canvas LMS todo entities from config entry."""
    coordinator = entry.runtime_data
    entities: list[CanvasTodoListEntity] = []

    for student in coordinator.data.observees:
        entities.append(
            CanvasTodoListEntity(
                coordinator=coordinator,
                student=student,
                entry=entry,
            )
        )

    async_add_entities(entities)


@dataclass
class CanvasTodoListExtraStoredData(ExtraStoredData):
    """Extra stored data for Canvas todo list entity across restarts."""

    completed_uids: list[str]
    deleted_uids: list[str]
    custom_overrides: dict[str, Any]
    manual_items: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        """Return dict representation for storage."""
        return {
            "completed_uids": list(self.completed_uids),
            "deleted_uids": list(self.deleted_uids),
            "custom_overrides": dict(self.custom_overrides),
            "manual_items": list(self.manual_items),
        }

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self:
        """Initialize from restored dictionary."""
        return cls(
            completed_uids=list(restored.get("completed_uids", [])),
            deleted_uids=list(restored.get("deleted_uids", [])),
            custom_overrides=dict(restored.get("custom_overrides", {})),
            manual_items=list(restored.get("manual_items", [])),
        )


class CanvasTodoListEntity(
    CoordinatorEntity[CanvasDataUpdateCoordinator], TodoListEntity, RestoreEntity
):
    """Interactive To-Do List entity for a Canvas student."""

    _attr_has_entity_name = True
    _attr_translation_key = "assignments"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

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

        self._completed_uids: set[str] = set()
        self._deleted_uids: set[str] = set()
        self._custom_item_overrides: dict[str, dict[str, Any]] = {}
        self._manual_items: dict[str, TodoItem] = {}

        self._update_todo_items()

    @property
    def extra_restore_state_data(self) -> CanvasTodoListExtraStoredData:
        """Return extra state data to persist across restarts."""
        return CanvasTodoListExtraStoredData(
            completed_uids=list(self._completed_uids),
            deleted_uids=list(self._deleted_uids),
            custom_overrides=self._custom_item_overrides,
            manual_items=[
                {
                    "uid": item.uid,
                    "summary": item.summary,
                    "status": item.status,
                    "due": item.due.isoformat() if item.due else None,
                    "description": item.description,
                }
                for item in self._manual_items.values()
            ],
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous state and subscribe to coordinator updates."""
        await super().async_added_to_hass()

        if (last_extra_data := await self.async_get_last_extra_data()) is not None:
            extra_data = CanvasTodoListExtraStoredData.from_dict(
                last_extra_data.as_dict()
            )
            self._completed_uids = set(extra_data.completed_uids)
            self._deleted_uids = set(extra_data.deleted_uids)
            self._custom_item_overrides = dict(extra_data.custom_overrides)

            for raw_item in extra_data.manual_items:
                due_val = raw_item.get("due")
                due: datetime | date | None = None
                if due_val:
                    try:
                        due = datetime.fromisoformat(due_val)
                    except ValueError:
                        due = None
                item = TodoItem(
                    uid=raw_item.get("uid"),
                    summary=raw_item.get("summary", ""),
                    status=raw_item.get("status", TodoItemStatus.NEEDS_ACTION),
                    due=due,
                    description=raw_item.get("description"),
                )
                if item.uid:
                    self._manual_items[item.uid] = item

            self._update_todo_items()
            self.async_write_ha_state()

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the current list of To-Do items."""
        return self._attr_todo_items

    def _get_course_map(self) -> dict[int, CanvasCourse]:
        """Return a mapping of course ID to CanvasCourse for this student."""
        courses = self.coordinator.data.courses_by_student.get(self.student.id, [])
        return {course.id: course for course in courses}

    def _update_todo_items(self) -> None:
        """Project coordinator assignments and local state into TodoItems."""
        items: list[TodoItem] = []
        counts: dict[SubmissionStatus, int] = {}
        late_items: list[dict[str, Any]] = []
        course_map = self._get_course_map()
        assignments = self.coordinator.data.assignments_by_student.get(
            self.student.id, []
        )

        for assignment in assignments:
            if not is_actionable_todo_assignment(assignment):
                continue

            uid = str(assignment.id)
            if uid in self._deleted_uids:
                continue

            course = course_map.get(assignment.course_id)
            course_prefix = f"[{course.name}] " if course and course.name else ""
            canvas_status = submission_status(assignment)
            on_paper = not is_online_submission_assignment(assignment)
            counts[canvas_status] = counts.get(canvas_status, 0) + 1
            if canvas_status is SubmissionStatus.LATE:
                late_items.append(
                    {
                        "uid": uid,
                        "course": course.name if course else None,
                        "assignment": assignment.name,
                        "url": assignment.html_url,
                    }
                )
            default_summary = (
                f"{STATUS_PREFIX.get(canvas_status, '')}{course_prefix}"
                f"{assignment.name}{' 📝' if on_paper else ''}"
            )

            # Canvas is the source of truth: an item is complete only when
            # Canvas shows it submitted, graded (paper work) or excused.
            status = (
                TodoItemStatus.COMPLETED
                if is_done(canvas_status)
                else TodoItemStatus.NEEDS_ACTION
            )

            # Apply any local custom overrides
            overrides = self._custom_item_overrides.get(uid, {})
            summary = overrides.get("summary", default_summary)
            due: datetime | date | None = overrides.get("due", assignment.due_at)
            status_line = f"Status: {canvas_status.value}" + (
                " (paper)" if on_paper else ""
            )
            description = overrides.get(
                "description",
                "\n".join(
                    filter(
                        None,
                        [status_line, assignment.description or assignment.html_url],
                    )
                ),
            )

            items.append(
                TodoItem(
                    uid=uid,
                    summary=summary,
                    status=status,
                    due=due,
                    description=description,
                )
            )

        # Append locally created manual items
        items.extend(self._manual_items.values())

        # Order items chronologically by due date, then summary
        items.sort(key=self._item_sort_key)

        self._attr_todo_items = items
        self._attr_extra_state_attributes = {
            **{
                f"{status.value}_count": counts.get(status, 0)
                for status in SubmissionStatus
            },
            "late_items": late_items,
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

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Create a new manual To-Do item for this student."""
        uid = item.uid or f"local_{uuid4().hex[:12]}"
        created_item = replace(
            item,
            uid=uid,
            status=item.status or TodoItemStatus.NEEDS_ACTION,
        )
        self._manual_items[uid] = created_item
        self._deleted_uids.discard(uid)
        self._update_todo_items()
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update an existing To-Do item (status, due date, summary, or description)."""
        uid = item.uid
        if not uid:
            return

        if uid in self._manual_items:
            self._manual_items[uid] = item
        else:
            # Canvas synced item: completion is owned by Canvas, not the list.
            current = next((i for i in self._attr_todo_items if i.uid == uid), None)
            if (
                current is not None
                and item.status is not None
                and item.status != current.status
            ):
                raise HomeAssistantError(
                    "Canvas assignments complete automatically once Canvas "
                    "shows them submitted. Submit it in Canvas, then refresh."
                )

            overrides = self._custom_item_overrides.setdefault(uid, {})
            if item.summary is not None:
                overrides["summary"] = item.summary
            if item.due is not None:
                overrides["due"] = item.due
            if item.description is not None:
                overrides["description"] = item.description

        self._update_todo_items()
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete one or more To-Do items."""
        for uid in uids:
            self._deleted_uids.add(uid)
            self._manual_items.pop(uid, None)
            self._custom_item_overrides.pop(uid, None)
            self._completed_uids.discard(uid)

        self._update_todo_items()
        self.async_write_ha_state()
