"""Services for the student workflow: stages, notes, reviews and follow-ups.

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
from .store import AssignmentSnapshot, build_snapshot
from .workflow import (
    AssignmentKind,
    CloseReason,
    FollowupMethod,
    FollowupStage,
    ReviewRole,
    Stage,
)

if TYPE_CHECKING:
    from .coordinator import CanvasDataUpdateCoordinator

SERVICE_SET_ASSIGNMENT_STAGE = "set_assignment_stage"
SERVICE_SET_FOLLOWUP_STAGE = "set_followup_stage"
SERVICE_REVIEW_ASSIGNMENT = "review_assignment"
SERVICE_CLOSE_ASSIGNMENT = "close_assignment"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ASSIGNMENT_ID = "assignment_id"
ATTR_STUDENT_ID = "student_id"
ATTR_FOLLOWUP_ID = "followup_id"
ATTR_STAGE = "stage"
ATTR_METHOD = "method"
ATTR_NOTE = "note"
ATTR_KIND = "kind"
ATTR_ROLE = "role"
ATTR_REASON = "reason"
ATTR_UNDO = "undo"

# Sets the kind back to automatic classification.
KIND_AUTO = "auto"

_ASSIGNMENT_FIELDS = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_ASSIGNMENT_ID): vol.All(vol.Coerce(str), cv.string),
    vol.Optional(ATTR_STUDENT_ID): vol.Coerce(int),
}

SET_ASSIGNMENT_STAGE_SCHEMA = vol.Schema(
    {
        **_ASSIGNMENT_FIELDS,
        vol.Optional(ATTR_STAGE): vol.In([s.value for s in Stage]),
        vol.Optional(ATTR_NOTE): vol.Any(cv.string, ""),
        vol.Optional(ATTR_KIND): vol.In(
            [KIND_AUTO, *(k.value for k in AssignmentKind)]
        ),
    }
)

REVIEW_ASSIGNMENT_SCHEMA = vol.Schema(
    {
        **_ASSIGNMENT_FIELDS,
        vol.Optional(ATTR_ROLE, default=ReviewRole.STUDENT.value): vol.In(
            [r.value for r in ReviewRole]
        ),
        vol.Optional(ATTR_UNDO, default=False): cv.boolean,
    }
)

CLOSE_ASSIGNMENT_SCHEMA = vol.Schema(
    {
        **_ASSIGNMENT_FIELDS,
        vol.Optional(ATTR_REASON): vol.In([r.value for r in CloseReason]),
        vol.Optional(ATTR_NOTE): vol.Any(cv.string, ""),
        vol.Optional(ATTR_UNDO, default=False): cv.boolean,
    }
)

SET_FOLLOWUP_STAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_FOLLOWUP_ID): cv.string,
        vol.Optional(ATTR_STAGE): vol.In([s.value for s in FollowupStage]),
        # "" clears the method.
        vol.Optional(ATTR_METHOD): vol.In(["", *(m.value for m in FollowupMethod)]),
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
) -> dict[int, tuple[CanvasAssignment, CanvasCourse | None]]:
    """Find an assignment in the latest Canvas data, per student."""
    found: dict[int, tuple[CanvasAssignment, CanvasCourse | None]] = {}
    if data is None:
        return found
    students = data.assignments_by_student.keys() | (
        data.term_assignments_by_student.keys()
    )
    for student_id in students:
        for assignment in data.term_assignments(student_id):
            if str(assignment.id) == assignment_id:
                course = next(
                    (
                        c
                        for c in data.courses_by_student.get(student_id, [])
                        if c.id == assignment.course_id
                    ),
                    None,
                )
                found[student_id] = (assignment, course)
    return found


def _resolve_assignment(
    coordinator: CanvasDataUpdateCoordinator, call: ServiceCall
) -> tuple[str, int, AssignmentSnapshot | None]:
    """Return (assignment id, student id, fresh snapshot) for a service call.

    Assignment ids are per course, so siblings in one class share them. The
    snapshot is None when Canvas no longer returns the assignment; its
    record can still be updated.
    """
    assignment_id = call.data[ATTR_ASSIGNMENT_ID].strip()
    found = _find_assignment(coordinator.data, assignment_id)
    students = set(found) | {
        r.student_id
        for r in coordinator.workflow.assignments.values()
        if r.assignment_id == assignment_id
    }
    if (wanted := call.data.get(ATTR_STUDENT_ID)) is not None:
        students &= {wanted}
    if not students:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_assignment",
            translation_placeholders={"assignment_id": assignment_id},
        )
    if len(students) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ambiguous_assignment",
            translation_placeholders={"assignment_id": assignment_id},
        )
    student_id = students.pop()
    snapshot = None
    if student_id in found:
        assignment, course = found[student_id]
        snapshot = build_snapshot(assignment, course, dt_util.utcnow())
    return assignment_id, student_id, snapshot


async def _async_user_name(hass: HomeAssistant, call: ServiceCall) -> str | None:
    """Return the name of the Home Assistant user who made the call."""
    if call.context.user_id is None:
        return None
    user = await hass.auth.async_get_user(call.context.user_id)
    return user.name if user else None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the workflow services."""

    async def set_assignment_stage(call: ServiceCall) -> None:
        _require_change(call, ATTR_STAGE, ATTR_NOTE, ATTR_KIND)
        note = _validate_note(call.data.get(ATTR_NOTE))
        coordinator = _coordinator(hass, call)
        assignment_id, student_id, snapshot = _resolve_assignment(coordinator, call)
        stage = call.data.get(ATTR_STAGE)
        kind = call.data.get(ATTR_KIND)
        await coordinator.workflow.async_set_assignment(
            assignment_id,
            student_id,
            dt_util.utcnow(),
            stage=Stage(stage) if stage else None,
            note=note,
            kind=AssignmentKind(kind) if kind and kind != KIND_AUTO else None,
            clear_kind=kind == KIND_AUTO,
            snapshot=snapshot,
        )
        coordinator.async_workflow_changed()

    async def review_assignment(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        assignment_id, student_id, snapshot = _resolve_assignment(coordinator, call)
        await coordinator.workflow.async_mark_reviewed(
            assignment_id,
            student_id,
            dt_util.utcnow(),
            role=ReviewRole(call.data[ATTR_ROLE]),
            by=await _async_user_name(hass, call),
            undo=call.data[ATTR_UNDO],
            snapshot=snapshot,
        )
        coordinator.async_workflow_changed()

    async def close_assignment(call: ServiceCall) -> None:
        note = _validate_note(call.data.get(ATTR_NOTE))
        coordinator = _coordinator(hass, call)
        assignment_id, student_id, snapshot = _resolve_assignment(coordinator, call)
        reason = call.data.get(ATTR_REASON)
        await coordinator.workflow.async_close_without_grade(
            assignment_id,
            student_id,
            dt_util.utcnow(),
            reason=CloseReason(reason) if reason else None,
            note=note,
            by=await _async_user_name(hass, call),
            undo=call.data[ATTR_UNDO],
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
            clear_method=method == "",
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
    hass.services.async_register(
        DOMAIN,
        SERVICE_REVIEW_ASSIGNMENT,
        review_assignment,
        schema=REVIEW_ASSIGNMENT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLOSE_ASSIGNMENT,
        close_assignment,
        schema=CLOSE_ASSIGNMENT_SCHEMA,
    )
