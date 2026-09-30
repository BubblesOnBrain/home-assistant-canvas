"""Services for the student workflow: set an assignment's stage/note and follow-ups.

These are deliberately not admin-only: the student's own (non-admin) account
calls them from the dashboard card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, service as service_helper
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NOTE_MAX_LENGTH
from .models import CanvasAssignment, CanvasCourse, CanvasData
from .store import build_snapshot
from .workflow import FollowupMethod, FollowupStage, Stage

if TYPE_CHECKING:
    from .coordinator import CanvasDataUpdateCoordinator

SERVICE_SET_ASSIGNMENT_STAGE = "set_assignment_stage"
SERVICE_SET_FOLLOWUP_STAGE = "set_followup_stage"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ASSIGNMENT_ID = "assignment_id"
ATTR_FOLLOWUP_ID = "followup_id"
ATTR_STAGE = "stage"
ATTR_METHOD = "method"
ATTR_NOTE = "note"

SET_ASSIGNMENT_STAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_ASSIGNMENT_ID): vol.All(vol.Coerce(str), cv.string),
        vol.Optional(ATTR_STAGE): vol.In([s.value for s in Stage]),
        vol.Optional(ATTR_NOTE): vol.Any(cv.string, ""),
    }
)

SET_FOLLOWUP_STAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_FOLLOWUP_ID): cv.string,
        vol.Optional(ATTR_STAGE): vol.In([s.value for s in FollowupStage]),
        vol.Optional(ATTR_METHOD): vol.In([m.value for m in FollowupMethod]),
        vol.Optional(ATTR_NOTE): vol.Any(cv.string, ""),
    }
)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> CanvasDataUpdateCoordinator:
    """Return the coordinator for the requested (or only) Canvas entry."""
    entry = service_helper.async_get_config_entry(
        hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)
    )
    coordinator: CanvasDataUpdateCoordinator = entry.runtime_data
    return coordinator


def _validate_note(note: str | None) -> str | None:
    """Return the note stripped of surrounding whitespace, within the length limit."""
    if note is None:
        return None
    note = note.strip()
    if len(note) > NOTE_MAX_LENGTH:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="note_too_long",
            translation_placeholders={"max": str(NOTE_MAX_LENGTH)},
        )
    return note


def _require_change(call: ServiceCall, *fields: str) -> None:
    """Raise unless at least one of the fields was given."""
    if not any(field in call.data for field in fields):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="nothing_to_set",
            translation_placeholders={"fields": ", ".join(fields)},
        )


def _find_assignment(
    data: CanvasData | None, assignment_id: str
) -> tuple[int, CanvasAssignment, CanvasCourse | None] | None:
    """Find an assignment in the latest Canvas data."""
    if data is None:
        return None
    for student_id, assignments in data.assignments_by_student.items():
        for assignment in assignments:
            if str(assignment.id) == assignment_id:
                course = next(
                    (
                        c
                        for c in data.courses_by_student.get(student_id, [])
                        if c.id == assignment.course_id
                    ),
                    None,
                )
                return student_id, assignment, course
    return None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the workflow services."""

    async def set_assignment_stage(call: ServiceCall) -> None:
        _require_change(call, ATTR_STAGE, ATTR_NOTE)
        note = _validate_note(call.data.get(ATTR_NOTE))
        coordinator = _coordinator(hass, call)
        assignment_id = call.data[ATTR_ASSIGNMENT_ID].strip()
        found = _find_assignment(coordinator.data, assignment_id)
        record = coordinator.workflow.assignments.get(assignment_id)
        now = dt_util.utcnow()
        if found is not None:
            student_id, assignment, course = found
            snapshot = build_snapshot(assignment, course, now)
        elif record is not None:
            # No longer in Canvas, but she can still update her own record.
            student_id, snapshot = record.student_id, None
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_assignment",
                translation_placeholders={"assignment_id": assignment_id},
            )
        stage = call.data.get(ATTR_STAGE)
        await coordinator.workflow.async_set_assignment(
            assignment_id,
            student_id,
            now,
            stage=Stage(stage) if stage else None,
            note=note,
            snapshot=snapshot,
        )
        coordinator.async_workflow_changed()

    async def set_followup_stage(call: ServiceCall) -> None:
        _require_change(call, ATTR_STAGE, ATTR_METHOD, ATTR_NOTE)
        note = _validate_note(call.data.get(ATTR_NOTE))
        coordinator = _coordinator(hass, call)
        followup_id = call.data[ATTR_FOLLOWUP_ID].strip()
        if followup_id not in coordinator.workflow.followups:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_followup",
                translation_placeholders={"followup_id": followup_id},
            )
        stage = call.data.get(ATTR_STAGE)
        method = call.data.get(ATTR_METHOD)
        await coordinator.workflow.async_set_followup(
            followup_id,
            dt_util.utcnow(),
            stage=FollowupStage(stage) if stage else None,
            method=FollowupMethod(method) if method else None,
            note=note,
        )
        coordinator.async_workflow_changed()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ASSIGNMENT_STAGE,
        set_assignment_stage,
        schema=SET_ASSIGNMENT_STAGE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_FOLLOWUP_STAGE,
        set_followup_stage,
        schema=SET_FOLLOWUP_STAGE_SCHEMA,
    )
