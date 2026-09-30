"""Tests for the workflow services."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import voluptuous as vol

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.auth.models import User
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    DOMAIN,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.workflow import (
    AssignmentKind,
    CloseReason,
    FollowupMethod,
    FollowupStage,
    Stage,
)

from .conftest import (
    MOCK_OBSERVEES_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
)

AID = "134664"


@pytest.fixture(name="coordinator")
async def fixture_coordinator(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> CanvasDataUpdateCoordinator:
    """Set up one student with one late (submitted after due) assignment."""
    freezer.move_to("2026-09-03T12:00:00Z")
    course = build_mock_course_dict(course_id=7349, name="AP US History - P2")
    asg = build_mock_assignment_dict(assignment_id=int(AID), course_id=7349)
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_SELF}", json=MOCK_USER_SELF_RESPONSE
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USERS_OBSERVEES}",
        json=[MOCK_OBSERVEES_RESPONSE[0]],
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}{ENDPOINT_USER_COURSES.format(user_id=6021)}", json=[course]
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}"
        f"{ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=7349)}",
        json=[
            build_mock_submission_dict(
                assignment=asg, submitted_at="2026-09-02T10:00:00Z", late=True
            )
        ],
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry.runtime_data


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], **kwargs: Any
) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True, **kwargs)


async def test_set_assignment_stage_and_note(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Stage and note are stored, with a Canvas snapshot, and no re-fetch."""
    calls = aioclient_mock.call_count
    await _call(
        hass,
        "set_assignment_stage",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "assignment_id": AID,
            "stage": "submitted_claimed",
            "note": "  Uploaded the PDF  ",
        },
    )
    record = coordinator.workflow.assignment(6021, AID)
    assert record.stage is Stage.SUBMITTED_CLAIMED
    assert record.claimed_submitted_at is not None
    assert record.note == "Uploaded the PDF"
    assert record.student_id == 6021
    assert record.snapshot is not None
    assert record.snapshot.class_name == "AP US History"
    assert aioclient_mock.call_count == calls

    # Note only: stage unchanged. Empty note clears it.
    await _call(hass, "set_assignment_stage", {"assignment_id": AID, "note": ""})
    assert record.note == ""
    assert record.stage is Stage.SUBMITTED_CLAIMED
    # Assignment ids may be given as numbers.
    await _call(
        hass, "set_assignment_stage", {"assignment_id": int(AID), "stage": "working"}
    )
    assert record.stage is Stage.WORKING


async def test_set_assignment_stage_validation(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Unknown ids, long notes, bad stages and empty calls are rejected."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "set_assignment_stage", {"assignment_id": "999", "stage": "working"}
        )
    assert err.value.translation_key == "unknown_assignment"

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "set_assignment_stage", {"assignment_id": AID, "note": "x" * 501}
        )
    assert err.value.translation_key == "note_too_long"
    await _call(hass, "set_assignment_stage", {"assignment_id": AID, "note": "x" * 500})

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "set_assignment_stage", {"assignment_id": AID})
    assert err.value.translation_key == "nothing_to_set"

    with pytest.raises(vol.Invalid):
        await _call(
            hass, "set_assignment_stage", {"assignment_id": AID, "stage": "finished"}
        )

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "set_assignment_stage",
            {"config_entry_id": "nope", "assignment_id": AID, "stage": "working"},
        )


async def test_stored_record_editable_after_leaving_canvas(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """A record whose assignment Canvas no longer returns can still be edited."""
    await coordinator.workflow.async_set_assignment(
        "777", 6021, dt_util.utcnow(), note="old"
    )
    await _call(hass, "set_assignment_stage", {"assignment_id": "777", "note": "new"})
    assert coordinator.workflow.assignment(6021, "777").note == "new"


async def test_set_followup_stage(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Follow-up stage, method and note are stored; resolved stamps a time."""
    fid = f"6021:{AID}:late_work"
    assert coordinator.workflow.followups[fid].stage is FollowupStage.NEEDS_CONTACT

    await _call(
        hass,
        "set_followup_stage",
        {"followup_id": fid, "stage": "resolved", "method": "email", "note": "Sent"},
    )
    followup = coordinator.workflow.followups[fid]
    assert followup.stage is FollowupStage.RESOLVED
    assert followup.method is FollowupMethod.EMAIL
    assert followup.note == "Sent"
    assert followup.resolved_at is not None

    await _call(hass, "set_followup_stage", {"followup_id": fid, "stage": "contacted"})
    assert followup.resolved_at is None


async def test_set_followup_stage_validation(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """Unknown follow-ups and empty calls are rejected."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "set_followup_stage",
            {"followup_id": f"6021:{AID}:not_in_canvas", "stage": "resolved"},
        )
    assert err.value.translation_key == "unknown_followup"
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "set_followup_stage", {"followup_id": f"6021:{AID}:late_work"}
        )
    assert err.value.translation_key == "nothing_to_set"
    with pytest.raises(vol.Invalid):
        await _call(
            hass,
            "set_followup_stage",
            {"followup_id": f"6021:{AID}:late_work", "method": "carrier_pigeon"},
        )


async def test_non_admin_user_can_call_services(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    hass_read_only_user: User,
) -> None:
    """The student's non-admin account can use both services."""
    context = Context(user_id=hass_read_only_user.id)
    await _call(
        hass,
        "set_assignment_stage",
        {"assignment_id": AID, "stage": "working"},
        context=context,
    )
    await _call(
        hass,
        "set_followup_stage",
        {"followup_id": f"6021:{AID}:late_work", "stage": "contacted"},
        context=context,
    )
    assert coordinator.workflow.assignment(6021, AID).stage is Stage.WORKING


async def test_followup_method_can_be_cleared(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """An empty method clears it."""
    fid = f"6021:{AID}:late_work"
    await _call(hass, "set_followup_stage", {"followup_id": fid, "method": "email"})
    await _call(hass, "set_followup_stage", {"followup_id": fid, "method": ""})
    assert coordinator.workflow.followups[fid].method is None


async def test_assignment_shared_by_siblings_needs_student_id(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """With two students on one assignment id, the student must be given."""
    await coordinator.workflow.async_set_assignment(
        AID, 4899, dt_util.utcnow(), note="sibling"
    )
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "set_assignment_stage", {"assignment_id": AID, "stage": "working"}
        )
    assert err.value.translation_key == "ambiguous_assignment"

    await _call(
        hass,
        "set_assignment_stage",
        {"assignment_id": AID, "student_id": 6021, "stage": "working"},
    )
    mine = coordinator.workflow.assignment(6021, AID)
    theirs = coordinator.workflow.assignment(4899, AID)
    assert mine is not None and mine.stage is Stage.WORKING
    assert theirs is not None and theirs.stage is Stage.NOT_STARTED


async def test_review_assignment_records_user_name(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    hass_read_only_user: User,
    hass_admin_user: User,
) -> None:
    """Anyone can review; the calling user's name is kept for each role."""
    await _call(
        hass,
        "review_assignment",
        {"assignment_id": AID},
        context=Context(user_id=hass_read_only_user.id),
    )
    record = coordinator.workflow.assignment(6021, AID)
    assert record.student_reviewed_by == hass_read_only_user.name
    assert record.student_reviewed_at is not None
    assert record.parent_reviewed_at is None

    await _call(
        hass,
        "review_assignment",
        {"assignment_id": AID, "role": "parent"},
        context=Context(user_id=hass_admin_user.id),
    )
    assert record.parent_reviewed_by == hass_admin_user.name

    await _call(hass, "review_assignment", {"assignment_id": AID, "undo": True})
    assert record.student_reviewed_at is None
    assert record.parent_reviewed_at is None

    # Called without a user (e.g. an automation): no name recorded.
    await _call(hass, "review_assignment", {"assignment_id": AID})
    assert record.student_reviewed_at is not None
    assert record.student_reviewed_by is None


async def test_close_assignment_and_reopen(
    hass: HomeAssistant,
    coordinator: CanvasDataUpdateCoordinator,
    hass_read_only_user: User,
) -> None:
    """Close without grade stores reason, note and who; undo reopens."""
    await _call(
        hass,
        "close_assignment",
        {"assignment_id": AID, "reason": "not_graded", "note": "  Not for a grade "},
        context=Context(user_id=hass_read_only_user.id),
    )
    record = coordinator.workflow.assignment(6021, AID)
    assert record.closed_reason is CloseReason.NOT_GRADED
    assert record.closed_note == "Not for a grade"
    assert record.closed_by == hass_read_only_user.name

    await _call(hass, "close_assignment", {"assignment_id": AID, "undo": True})
    assert record.closed_at is None

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "close_assignment", {"assignment_id": AID, "note": "x" * 501})
    assert err.value.translation_key == "note_too_long"
    with pytest.raises(vol.Invalid):
        await _call(
            hass, "close_assignment", {"assignment_id": AID, "reason": "lost_it"}
        )
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "review_assignment", {"assignment_id": "999"})
    assert err.value.translation_key == "unknown_assignment"


async def test_set_assignment_kind_and_auto(
    hass: HomeAssistant, coordinator: CanvasDataUpdateCoordinator
) -> None:
    """The kind can be corrected alone, and "auto" clears the correction."""
    await _call(hass, "set_assignment_stage", {"assignment_id": AID, "kind": "quiz"})
    record = coordinator.workflow.assignment(6021, AID)
    assert record.kind_override is AssignmentKind.QUIZ
    assert record.stage is Stage.NOT_STARTED
    await _call(hass, "set_assignment_stage", {"assignment_id": AID, "kind": "auto"})
    assert record.kind_override is None
    with pytest.raises(vol.Invalid):
        await _call(
            hass, "set_assignment_stage", {"assignment_id": AID, "kind": "essay"}
        )
