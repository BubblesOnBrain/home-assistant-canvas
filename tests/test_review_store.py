"""Tests for review, close-without-grade, kind overrides and the grade log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from custom_components.canvas.const import GRADE_LOG_DAYS, REVIEW_BACKFILL_DAYS
from custom_components.canvas.models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasData,
    CanvasSubmission,
    CanvasUser,
)
from custom_components.canvas.store import (
    CanvasWorkflowStore,
    in_review_window,
    storage_key,
)
from custom_components.canvas.workflow import (
    AssignmentKind,
    CloseReason,
    FollowupReason,
    FollowupStage,
    ReviewRole,
    Stage,
)

from .conftest import TEST_USER_ID

STUDENT = 6021
AID = "5506356"
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
DUE = NOW - timedelta(days=2)
ONLINE = timedelta(hours=12)
PAPER = timedelta(days=7)
KEY = storage_key(str(TEST_USER_ID))
COURSE = CanvasCourse(id=88, name="Biology - P3 Rivera")
GROUPS = {88: {1: "Minor", 2: "Major"}}


def _assignment(
    *,
    aid: int = int(AID),
    name: str = "Cell Lab Report",
    paper: bool = False,
    score: float | None = None,
    grade: str | None = None,
    graded_at: datetime | None = None,
    submitted_at: datetime | None = None,
    late: bool = False,
    missing: bool = False,
    group: int | None = 1,
    due: datetime | None = DUE,
) -> CanvasAssignment:
    return CanvasAssignment(
        id=aid,
        course_id=COURSE.id,
        name=name,
        due_at=due,
        points_possible=20.0,
        submission_types=("on_paper",) if paper else ("online_upload",),
        assignment_group_id=group,
        submission=CanvasSubmission(
            id=1,
            assignment_id=aid,
            user_id=STUDENT,
            score=score,
            grade=grade,
            graded_at=graded_at,
            submitted_at=submitted_at,
            late=late,
            missing=missing,
        ),
    )


def _data(
    *term: CanvasAssignment, todo: list[CanvasAssignment] | None = None
) -> CanvasData:
    return CanvasData(
        user=CanvasUser(id=TEST_USER_ID, name="Parent"),
        courses_by_student={STUDENT: [COURSE]},
        assignments_by_student={STUDENT: list(term) if todo is None else todo},
        term_assignments_by_student={STUDENT: list(term)},
        assignment_groups=GROUPS,
    )


def _reconcile(store: CanvasWorkflowStore, data: CanvasData, now: datetime) -> bool:
    return store.reconcile(data, now, ONLINE, PAPER)


async def test_review_since_backfills_on_first_load_and_persists(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Review tracking starts 14 days back once, and keeps that date."""
    freezer.move_to(NOW)
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await store.async_load()
    assert store.review_since == NOW - timedelta(days=REVIEW_BACKFILL_DAYS)
    await store.async_close()
    assert hass_storage[KEY]["data"]["meta"]["review_since"] == (
        store.review_since.isoformat()
    )

    freezer.move_to(NOW + timedelta(days=30))
    reloaded = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await reloaded.async_load()
    assert reloaded.review_since == store.review_since


async def test_load_upgrades_minor_version_1_data(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """0.2.x data (no review, grades or meta) loads with defaults."""
    freezer.move_to(NOW)
    hass_storage[KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": KEY,
        "data": {
            "assignments": {
                f"{STUDENT}:{AID}": {
                    "student_id": STUDENT,
                    "assignment_id": AID,
                    "stage": "working",
                }
            },
            "followups": {},
        },
    }
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await store.async_load()
    record = store.assignment(STUDENT, AID)
    assert record.stage is Stage.WORKING
    assert record.kind_override is None
    assert record.student_reviewed_at is None
    assert record.closed_reason is None
    assert store.grades == {}
    assert store.review_since is not None


async def test_mark_reviewed_records_who_and_undo(hass: HomeAssistant) -> None:
    """Student then parent review, each attributed; undoing the student clears both."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_mark_reviewed(
        AID, STUDENT, NOW, role=ReviewRole.STUDENT, by="Sydney"
    )
    later = NOW + timedelta(hours=2)
    record = await store.async_mark_reviewed(
        AID, STUDENT, later, role=ReviewRole.PARENT, by="Josh"
    )
    assert (record.student_reviewed_at, record.student_reviewed_by) == (NOW, "Sydney")
    assert (record.parent_reviewed_at, record.parent_reviewed_by) == (later, "Josh")

    await store.async_mark_reviewed(
        AID, STUDENT, later, role=ReviewRole.PARENT, by="Josh", undo=True
    )
    assert record.parent_reviewed_at is None
    assert record.student_reviewed_by == "Sydney"

    await store.async_mark_reviewed(
        AID, STUDENT, later, role=ReviewRole.PARENT, by="Josh"
    )
    await store.async_mark_reviewed(
        AID, STUDENT, later, role=ReviewRole.STUDENT, by="Sydney", undo=True
    )
    assert record.student_reviewed_at is None
    assert record.parent_reviewed_at is None
    assert record.parent_reviewed_by is None


async def test_close_without_grade_and_reopen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Closing stores reason, note and who; undo clears it all."""
    store = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await store.async_load()
    record = await store.async_close_without_grade(
        AID,
        STUDENT,
        NOW,
        reason=CloseReason.FEEDBACK_RECEIVED,
        note="Went over it in class",
        by="Sydney",
    )
    saved = hass_storage[KEY]["data"]["assignments"][f"{STUDENT}:{AID}"]
    assert saved["closed_reason"] == "feedback_received"
    assert saved["closed_note"] == "Went over it in class"
    assert saved["closed_by"] == "Sydney"
    assert record.closed_at == NOW

    reloaded = CanvasWorkflowStore(hass, str(TEST_USER_ID))
    await reloaded.async_load()
    assert reloaded.assignment(STUDENT, AID).closed_reason is (
        CloseReason.FEEDBACK_RECEIVED
    )

    await store.async_close_without_grade(
        AID, STUDENT, NOW, reason=None, note=None, by="Sydney", undo=True
    )
    assert record.closed_at is None
    assert record.closed_reason is None
    assert record.closed_note == ""

    # No reason given defaults to "other".
    await store.async_close_without_grade(
        AID, STUDENT, NOW, reason=None, note=None, by=None
    )
    assert record.closed_reason is CloseReason.OTHER


async def test_kind_override_set_and_cleared(hass: HomeAssistant) -> None:
    """A kind correction is stored; "auto" (clear_kind) removes it."""
    store = CanvasWorkflowStore(hass, "acct")
    record = await store.async_set_assignment(
        AID, STUDENT, NOW, kind=AssignmentKind.TEST
    )
    assert record.kind_override is AssignmentKind.TEST
    await store.async_set_assignment(AID, STUDENT, NOW, stage=Stage.STUDYING)
    assert record.kind_override is AssignmentKind.TEST
    await store.async_set_assignment(AID, STUDENT, NOW, clear_kind=True)
    assert record.kind_override is None


async def test_grade_log_adds_updates_and_drops(hass: HomeAssistant) -> None:
    """Graded work is logged with percent and category; a removed grade drops out."""
    store = CanvasWorkflowStore(hass, "acct")
    graded_at = NOW - timedelta(days=1)
    quiz = _assignment(
        name="Quiz: Cell Organelles",
        paper=True,
        score=15.0,
        grade="15",
        graded_at=graded_at,
        group=2,
    )
    assert _reconcile(store, _data(quiz), NOW)
    entry = store.grades[f"{STUDENT}:{AID}"]
    assert entry.kind is AssignmentKind.QUIZ
    assert entry.category == "Major"
    assert entry.percent == 75.0
    assert entry.class_name == "Biology"
    assert entry.graded_at == graded_at
    # Graded work alone creates no assignment record.
    assert store.assignments == {}

    # Unchanged: nothing to save.
    assert not _reconcile(store, _data(quiz), NOW)

    new_grade = _assignment(
        name="Quiz: Cell Organelles",
        paper=True,
        score=18.0,
        grade="18",
        graded_at=NOW,
        group=2,
    )
    assert _reconcile(store, _data(new_grade), NOW)
    assert store.grades[f"{STUDENT}:{AID}"].percent == 90.0

    ungraded = _assignment(name="Quiz: Cell Organelles", paper=True, group=2)
    assert _reconcile(store, _data(ungraded), NOW)
    assert store.grades == {}


async def test_grade_log_skips_placeholder_zero(hass: HomeAssistant) -> None:
    """A 0 on homework never turned in is a placeholder, not a grade."""
    store = CanvasWorkflowStore(hass, "acct")
    zero = _assignment(score=0.0, grade="0", graded_at=NOW)
    _reconcile(store, _data(zero), NOW)
    assert store.grades == {}

    # On a paper quiz, a 0 is a real grade.
    quiz = _assignment(name="Quiz 3", paper=True, score=0.0, grade="0", graded_at=NOW)
    _reconcile(store, _data(quiz), NOW)
    assert store.grades[f"{STUDENT}:{AID}"].percent == 0.0


async def test_grade_log_kept_after_canvas_drops_it_then_pruned(
    hass: HomeAssistant,
) -> None:
    """Grades outlive their assignment records, until the log's age limit."""
    store = CanvasWorkflowStore(hass, "acct")
    graded = _assignment(score=18.0, grade="18", graded_at=NOW)
    _reconcile(store, _data(graded), NOW)
    _reconcile(store, _data(), NOW + timedelta(days=GRADE_LOG_DAYS))
    assert f"{STUDENT}:{AID}" in store.grades
    assert _reconcile(store, _data(), NOW + timedelta(days=GRADE_LOG_DAYS + 1))
    assert store.grades == {}


async def test_followups_only_open_for_work_on_the_list(hass: HomeAssistant) -> None:
    """Old late work (term only) opens no follow-up; list work still does."""
    store = CanvasWorkflowStore(hass, "acct")
    old_late = _assignment(submitted_at=NOW - timedelta(days=20), late=True)
    _reconcile(store, _data(old_late, todo=[]), NOW)
    assert store.followups == {}

    _reconcile(store, _data(old_late), NOW)
    assert f"{STUDENT}:{AID}:late_work" in store.followups


async def test_paper_quiz_past_due_opens_no_followup(hass: HomeAssistant) -> None:
    """A paper quiz Canvas calls missing is awaiting a grade, not late work."""
    store = CanvasWorkflowStore(hass, "acct")
    quiz = _assignment(name="Quiz: Newton's Second Law", paper=True, missing=True)
    assert not _reconcile(store, _data(quiz), NOW)
    assert store.followups == {}


async def test_makeup_followup_opens_and_resolves_on_grade(hass: HomeAssistant) -> None:
    """Missing a quiz opens a make-up follow-up; the grade arriving resolves it."""
    store = CanvasWorkflowStore(hass, "acct")
    await store.async_set_assignment(AID, STUDENT, NOW, stage=Stage.NEEDS_MAKEUP)
    quiz = _assignment(name="Unit 2 Test", paper=True, missing=True)
    _reconcile(store, _data(quiz), NOW)
    fid = f"{STUDENT}:{AID}:{FollowupReason.MAKEUP}"
    assert store.followups[fid].stage is FollowupStage.NEEDS_CONTACT

    graded = _assignment(
        name="Unit 2 Test", paper=True, score=17.0, grade="17", graded_at=NOW
    )
    _reconcile(store, _data(graded), NOW + timedelta(days=3))
    assert store.followups[fid].stage is FollowupStage.RESOLVED
    assert store.followups[fid].auto_resolved


async def test_kind_override_changes_reconcile(hass: HomeAssistant) -> None:
    """Marking paper homework a quiz stops it counting as missing work."""
    store = CanvasWorkflowStore(hass, "acct")
    worksheet = _assignment(name="Chapter 4 Check", paper=True, missing=True)
    await store.async_set_assignment(AID, STUDENT, NOW, stage=Stage.SUBMITTED_CLAIMED)
    _reconcile(store, _data(worksheet), NOW + timedelta(days=8))
    assert f"{STUDENT}:{AID}:paper_not_graded" in store.followups

    other = "5506357"
    await store.async_set_assignment(
        other, STUDENT, NOW, stage=Stage.SUBMITTED_CLAIMED, kind=AssignmentKind.QUIZ
    )
    quiz = _assignment(aid=int(other), name="Chapter 4 Check", paper=True, missing=True)
    _reconcile(store, _data(quiz), NOW + timedelta(days=8))
    assert not any(f.assignment_id == other for f in store.followups.values())


def test_in_review_window() -> None:
    """Graded after review started (or due then, if no grading time)."""
    since = NOW - timedelta(days=14)
    assert in_review_window(since, None, since)
    assert not in_review_window(since - timedelta(seconds=1), NOW, since)
    assert in_review_window(None, NOW, since)
    assert not in_review_window(None, None, since)
    assert in_review_window(since - timedelta(days=100), None, None)
