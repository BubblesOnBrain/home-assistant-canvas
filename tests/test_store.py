"""Tests for durable workflow storage and reconciliation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.canvas.const import (
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
)
from custom_components.canvas.coordinator import CanvasDataUpdateCoordinator
from custom_components.canvas.models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasData,
    CanvasSubmission,
    CanvasUser,
)
from custom_components.canvas.store import (
    AUTO_RESOLVED_NOTE,
    CanvasWorkflowStore,
    storage_key,
)
from custom_components.canvas.workflow import (
    FollowupMethod,
    FollowupReason,
    FollowupStage,
    Stage,
)

from .conftest import (
    MOCK_OBSERVEES_RESPONSE,
    MOCK_USER_SELF_RESPONSE,
    TEST_BASE_URL,
    TEST_USER_ID,
    build_mock_assignment_dict,
    build_mock_course_dict,
    build_mock_submission_dict,
)

STUDENT = 6021
AID = "5506356"
DUE = datetime(2026, 9, 29, 3, 59, tzinfo=timezone.utc)
CLAIM = datetime(2026, 9, 29, 2, 10, tzinfo=timezone.utc)
ONLINE = timedelta(hours=12)
PAPER = timedelta(days=7)
KEY = storage_key(str(TEST_USER_ID))
COURSE = CanvasCourse(id=88, name="Biology - P3 Rivera")


def _assignment(
    *,
    aid: int = int(AID),
    submitted_at: datetime | None = None,
    late: bool = False,
    paper: bool = False,
    due: datetime | None = DUE,
) -> CanvasAssignment:
    return CanvasAssignment(
        id=aid,
        course_id=COURSE.id,
        name="Cell Lab Report",
        due_at=due,
        submission_types=("on_paper",) if paper else ("online_upload",),
        html_url=f"https://canvas.example.edu/courses/88/assignments/{aid}",
        submission=CanvasSubmission(
            id=1,
            assignment_id=aid,
            user_id=STUDENT,
            submitted_at=submitted_at,
            late=late,
        ),
    )


def _data(*assignments: CanvasAssignment) -> CanvasData:
    return CanvasData(
        user=CanvasUser(id=TEST_USER_ID, name="Parent"),
        courses_by_student={STUDENT: [COURSE]},
        assignments_by_student={STUDENT: list(assignments)},
    )


def _reconcile(store: CanvasWorkflowStore, data: CanvasData, now: datetime) -> bool:
    return store.reconcile(data, now, ONLINE, PAPER)


async def test_student_edit_saves_immediately_and_round_trips(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A stage or note change is on disk before the call returns and reloads intact."""
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await store.async_load()
    await store.async_set_assignment(
        AID, STUDENT, CLAIM, stage=Stage.SUBMITTED_CLAIMED, note="Uploaded from phone"
    )

    saved = hass_storage[KEY]
    assert saved["version"] == 1
    assert saved["minor_version"] == 1
    record = saved["data"]["assignments"][AID]
    assert record["stage"] == "submitted_claimed"
    assert record["note"] == "Uploaded from phone"
    assert record["claimed_submitted_at"] == CLAIM.isoformat()

    reloaded = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await reloaded.async_load()
    assert reloaded.as_dict() == store.as_dict()
    assert reloaded.assignments[AID].stage is Stage.SUBMITTED_CLAIMED


async def test_claim_timestamp_set_and_cleared(hass: HomeAssistant) -> None:
    """'I submitted it' stamps the claim time; any other stage clears it."""
    store = CanvasWorkflowStore(hass, "acct")
    record = await store.async_set_assignment(
        AID, STUDENT, CLAIM, stage=Stage.SUBMITTED_CLAIMED
    )
    assert record.claimed_submitted_at == CLAIM
    later = CLAIM + timedelta(hours=1)
    # Setting only a note keeps the stage and claim time.
    await store.async_set_assignment(AID, STUDENT, later, note="x")
    assert record.claimed_submitted_at == CLAIM
    assert record.stage_changed_at == CLAIM
    assert record.updated_at == later
    await store.async_set_assignment(AID, STUDENT, later, stage=Stage.WORKING)
    assert record.claimed_submitted_at is None
    assert record.stage_changed_at == later


async def test_load_skips_unreadable_records(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Bad records are skipped; unknown enum values fall back to defaults."""
    hass_storage[KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": KEY,
        "data": {
            "assignments": {
                "1": {"stage": "working"},
                "2": {"student_id": STUDENT, "stage": "bogus", "note": "kept"},
            },
            "followups": {
                "2:late_work": {
                    "assignment_id": "2",
                    "student_id": STUDENT,
                    "reason": "late_work",
                    "stage": "contacted",
                    "method": "email",
                    "created_at": "2026-09-29T20:05:00+00:00",
                },
                "3:nope": {"assignment_id": "3", "student_id": 1, "reason": "nope"},
            },
        },
    }
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await store.async_load()
    assert list(store.assignments) == ["2"]
    assert store.assignments["2"].stage is Stage.NOT_STARTED
    assert store.assignments["2"].note == "kept"
    followup = store.followups["2:late_work"]
    assert followup.stage is FollowupStage.CONTACTED
    assert followup.method is FollowupMethod.EMAIL
    # Missing contact_by falls back to the day after creation (local time).
    assert followup.contact_by == date(2026, 9, 30)
    assert list(store.followups) == ["2:late_work"]


def test_storage_key_is_filename_safe() -> None:
    """Account ids are sanitised for use as a file name."""
    assert storage_key("12345") == "canvas.workflow.12345"
    assert storage_key("a/b c") == "canvas.workflow.a_b_c"


async def test_reconcile_saves_debounced(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Changes made by reconcile are written after a short delay."""
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    now = DUE + timedelta(hours=16)
    assert _reconcile(store, _data(_assignment(submitted_at=now, late=True)), now)
    assert KEY not in hass_storage

    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "5506356:late_work" in hass_storage[KEY]["data"]["followups"]


async def test_flush_writes_pending_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Flushing writes a pending debounced save straight away."""
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    now = DUE + timedelta(hours=16)
    _reconcile(store, _data(_assignment(submitted_at=now, late=True)), now)
    await store.async_flush()
    assert "5506356:late_work" in hass_storage[KEY]["data"]["followups"]


async def test_untouched_assignments_get_no_record(hass: HomeAssistant) -> None:
    """Records are only created on an edit or when a follow-up opens."""
    store = CanvasWorkflowStore(hass, "acct")
    assert not _reconcile(store, _data(_assignment()), DUE + timedelta(days=1))
    assert store.assignments == {}


async def test_claim_flow_opens_and_auto_resolves_followups(
    hass: HomeAssistant,
) -> None:
    """The Cell Lab Report walkthrough, end to end."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(AID, STUDENT, CLAIM, stage=Stage.SUBMITTED_CLAIMED)

    # Within the 12 h grace window: nothing opens.
    at_grace = CLAIM + ONLINE
    _reconcile(store, _data(_assignment()), at_grace)
    assert store.followups == {}

    # Grace passed with nothing in Canvas: not_in_canvas opens, due next day.
    after = at_grace + timedelta(minutes=5)
    assert _reconcile(store, _data(_assignment()), after)
    followup = store.followups["5506356:not_in_canvas"]
    assert followup.stage is FollowupStage.NEEDS_CONTACT
    assert followup.contact_by == date(2026, 9, 30)
    snapshot = store.assignments[AID].snapshot
    assert snapshot is not None
    assert snapshot.canvas_status == "missing"

    # Idempotent: nothing new on the next pass.
    assert not _reconcile(store, _data(_assignment()), after + timedelta(minutes=15))

    # Canvas shows a late submission: auto-resolve + late_work opens.
    uploaded = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    resolved_at = uploaded + timedelta(minutes=5)
    _reconcile(store, _data(_assignment(submitted_at=uploaded, late=True)), resolved_at)
    assert followup.stage is FollowupStage.RESOLVED
    assert followup.resolved_at == resolved_at
    assert followup.note == AUTO_RESOLVED_NOTE
    late = store.followups["5506356:late_work"]
    assert late.stage is FollowupStage.NEEDS_CONTACT
    # Her stage is left as she set it: facts and claims are never merged.
    assert store.assignments[AID].stage is Stage.SUBMITTED_CLAIMED
    snapshot = store.assignments[AID].snapshot
    assert snapshot is not None
    assert snapshot.canvas_status == "late"


async def test_paper_claim_uses_paper_grace(hass: HomeAssistant) -> None:
    """Paper work waits for the longer window before a follow-up opens."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(AID, STUDENT, CLAIM, stage=Stage.SUBMITTED_CLAIMED)
    paper = _data(_assignment(paper=True))
    _reconcile(store, paper, CLAIM + timedelta(days=2))
    assert store.followups == {}
    _reconcile(store, paper, CLAIM + PAPER + timedelta(minutes=1))
    assert list(store.followups) == ["5506356:paper_not_graded"]


async def test_student_resolved_followup_not_reopened(hass: HomeAssistant) -> None:
    """A follow-up she resolved stays resolved even if the condition persists."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(AID, STUDENT, CLAIM, stage=Stage.SUBMITTED_CLAIMED)
    now = CLAIM + ONLINE + timedelta(hours=1)
    _reconcile(store, _data(_assignment()), now)
    await store.async_set_followup(
        "5506356:not_in_canvas", now, stage=FollowupStage.RESOLVED
    )
    assert not _reconcile(store, _data(_assignment()), now + timedelta(hours=1))
    assert store.followups["5506356:not_in_canvas"].stage is FollowupStage.RESOLVED


async def test_set_followup_stage_method_and_note(hass: HomeAssistant) -> None:
    """Resolving stamps resolved_at; moving back clears it."""
    store = CanvasWorkflowStore(hass, "acct")
    now = DUE + timedelta(hours=16)
    _reconcile(store, _data(_assignment(submitted_at=now, late=True)), now)
    fid = "5506356:late_work"
    later = now + timedelta(days=1)
    await store.async_set_followup(
        fid, later, stage=FollowupStage.RESOLVED, method=FollowupMethod.EMAIL
    )
    followup = store.followups[fid]
    assert followup.resolved_at == later
    assert followup.method is FollowupMethod.EMAIL
    await store.async_set_followup(fid, later, stage=FollowupStage.CONTACTED, note="x")
    assert followup.resolved_at is None
    assert followup.note == "x"
    with pytest.raises(KeyError):
        await store.async_set_followup("nope:late_work", later)


async def test_last_seen_updates_at_most_daily(hass: HomeAssistant) -> None:
    """Hourly fetches don't rewrite the file just to bump last_seen_at."""
    store = CanvasWorkflowStore(hass, "acct")
    first = DUE - timedelta(days=1)
    await store.async_set_assignment(AID, STUDENT, first, stage=Stage.WORKING)
    data = _data(_assignment(due=DUE + timedelta(days=5)))
    assert _reconcile(store, data, first)  # snapshot + last_seen recorded
    assert not _reconcile(store, data, first + timedelta(hours=1))
    assert _reconcile(store, data, first + timedelta(days=1))
    assert store.assignments[AID].last_seen_at == first + timedelta(days=1)


async def test_gone_records_kept_then_pruned(hass: HomeAssistant) -> None:
    """Leaving Canvas never deletes by itself; prune after 60 days if resolved."""
    store = CanvasWorkflowStore(hass, "acct")
    now = DUE + timedelta(hours=16)
    _reconcile(store, _data(_assignment(submitted_at=now, late=True)), now)
    await store.async_set_assignment(AID, STUDENT, now, note="Emailed Ms. Rivera")
    empty = _data()

    # Gone for 61 days, but the late_work follow-up is still open: kept.
    assert not _reconcile(store, empty, now + timedelta(days=61))
    assert AID in store.assignments

    await store.async_set_followup(
        "5506356:late_work", now, stage=FollowupStage.RESOLVED
    )
    assert not _reconcile(store, empty, now + timedelta(days=59))
    assert _reconcile(store, empty, now + timedelta(days=61))
    assert store.assignments == {}
    assert store.followups == {}


async def test_record_reattaches_when_assignment_returns(hass: HomeAssistant) -> None:
    """Same Canvas id coming back finds the stage and note intact."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(
        AID, STUDENT, DUE, stage=Stage.WORKING, note="Need graph"
    )
    _reconcile(store, _data(), DUE + timedelta(days=10))
    _reconcile(store, _data(_assignment()), DUE + timedelta(days=20))
    assert store.assignments[AID].note == "Need graph"
    assert store.assignments[AID].last_seen_at == DUE + timedelta(days=20)


# --- Integration lifecycle -------------------------------------------------


def _routes(aioclient_mock: AiohttpClientMocker) -> None:
    course = build_mock_course_dict(course_id=7349, name="AP US History")
    asg = build_mock_assignment_dict(assignment_id=134664, course_id=7349)
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
        json=[build_mock_submission_dict(assignment=asg)],
    )


async def test_data_survives_reload_and_entry_removal(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Workflow data survives a reload and removing/re-adding the entry."""
    _routes(aioclient_mock)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: CanvasDataUpdateCoordinator = mock_config_entry.runtime_data
    assert coordinator.workflow.key == KEY
    await coordinator.workflow.async_set_assignment(
        "134664", 6021, DUE, stage=Stage.WORKING, note="Outline done"
    )

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    reloaded: CanvasDataUpdateCoordinator = mock_config_entry.runtime_data
    assert reloaded is not coordinator
    assert reloaded.workflow.assignments["134664"].note == "Outline done"

    # Removing the integration keeps the file, so re-adding finds the data.
    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert KEY in hass_storage

    new_entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        data=dict(mock_config_entry.data),
        unique_id=mock_config_entry.unique_id,
    )
    new_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(new_entry.entry_id)
    await hass.async_block_till_done()
    assert new_entry.state is ConfigEntryState.LOADED
    assert new_entry.runtime_data.workflow.assignments["134664"].stage is Stage.WORKING


async def test_options_change_grace_windows(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Grace windows come from options and apply after the options reload."""
    _routes(aioclient_mock)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.grace_windows == (ONLINE, PAPER)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"online_grace_hours": 2, "paper_grace_days": 3},
    )
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.grace_windows == (
        timedelta(hours=2),
        timedelta(days=3),
    )


async def test_options_reject_out_of_range(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Grace windows are limited to their ranges."""
    from homeassistant.data_entry_flow import InvalidData

    for bad in (
        {"online_grace_hours": 0, "paper_grace_days": 7},
        {"online_grace_hours": 73, "paper_grace_days": 7},
        {"online_grace_hours": 12, "paper_grace_days": 22},
    ):
        result = await hass.config_entries.options.async_init(
            mock_config_entry.entry_id
        )
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=bad
            )


async def test_tick_reconciles_between_fetches(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The 15-minute tick opens follow-ups when grace runs out, without a fetch."""
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    _routes(aioclient_mock)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: CanvasDataUpdateCoordinator = mock_config_entry.runtime_data
    # Claimed 11 h 50 min ago: grace runs out in 10 minutes.
    await coordinator.workflow.async_set_assignment(
        "134664",
        6021,
        start - timedelta(hours=11, minutes=50),
        stage=Stage.SUBMITTED_CLAIMED,
    )
    coordinator.async_workflow_changed()
    assert coordinator.workflow.followups == {}
    calls = aioclient_mock.call_count

    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == calls  # no Canvas fetch
    assert (
        coordinator.workflow.followups["134664:not_in_canvas"].reason
        is FollowupReason.NOT_IN_CANVAS
    )


async def test_record_without_snapshot_ages_from_last_edit(
    hass: HomeAssistant,
) -> None:
    """A record never matched to Canvas is not pruned straight away."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(AID, STUDENT, DUE, note="kept")
    assert not _reconcile(store, _data(), DUE + timedelta(days=59))
    assert _reconcile(store, _data(), DUE + timedelta(days=61))


async def test_diagnostics_include_workflow_and_redact_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Diagnostics show the stored data and never the access token."""
    from custom_components.canvas.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    _routes(aioclient_mock)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await mock_config_entry.runtime_data.workflow.async_set_assignment(
        "134664", 6021, DUE, note="Outline done"
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["entry"]["access_token"] == "**REDACTED**"
    assert diagnostics["storage_file"] == f".storage/{KEY}"
    assert diagnostics["workflow"]["assignments"]["134664"]["note"] == "Outline done"
