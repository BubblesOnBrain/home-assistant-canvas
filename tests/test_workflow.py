"""Tests for the pure student workflow rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.canvas.filtering import SubmissionStatus
from custom_components.canvas.workflow import (
    DisplayState,
    FollowupAction,
    FollowupReason,
    FollowupStage,
    Stage,
    display_state,
    followup_actions,
    followup_id,
    grace_passed,
    is_overdue,
    should_prune,
    summary_prefix,
)

NOW = datetime(2026, 9, 29, 14, 10, tzinfo=timezone.utc)
ONLINE = timedelta(hours=12)
PAPER = timedelta(days=7)

DONE = (
    SubmissionStatus.SUBMITTED,
    SubmissionStatus.LATE,
    SubmissionStatus.GRADED,
    SubmissionStatus.EXCUSED,
)
OPEN = (SubmissionStatus.UPCOMING, SubmissionStatus.MISSING, SubmissionStatus.ZEROED)

# Claim times relative to NOW: within grace, exactly at it, and past it.
WITHIN = {False: NOW - timedelta(hours=1), True: NOW - timedelta(days=1)}
AT = {False: NOW - ONLINE, True: NOW - PAPER}
AFTER = {
    False: NOW - ONLINE - timedelta(seconds=1),
    True: NOW - PAPER - timedelta(seconds=1),
}


def _state(
    status: SubmissionStatus,
    stage: Stage,
    claimed: datetime | None = None,
    paper: bool = False,
) -> DisplayState:
    return display_state(status, stage, claimed, paper, NOW, ONLINE, PAPER)


@pytest.mark.parametrize("status", DONE)
@pytest.mark.parametrize("stage", list(Stage))
@pytest.mark.parametrize("paper", [False, True])
def test_row1_canvas_done_is_confirmed(
    status: SubmissionStatus, stage: Stage, paper: bool
) -> None:
    """Canvas completion wins over any stage, including stale claims."""
    assert _state(status, stage, AFTER[paper], paper) is DisplayState.CONFIRMED


@pytest.mark.parametrize("status", OPEN)
@pytest.mark.parametrize(
    ("paper", "claims", "expected"),
    [
        (False, AFTER, DisplayState.NOT_IN_CANVAS),
        (False, WITHIN, DisplayState.CLAIMED_PENDING),
        (False, AT, DisplayState.CLAIMED_PENDING),
        (True, AFTER, DisplayState.PAPER_NOT_GRADED),
        (True, WITHIN, DisplayState.PAPER_WAITING),
        (True, AT, DisplayState.PAPER_WAITING),
    ],
)
def test_rows2to5_claimed_submission(
    status: SubmissionStatus,
    paper: bool,
    claims: dict[bool, datetime],
    expected: DisplayState,
) -> None:
    """A claim ranks above missing/zeroed; the grace boundary is inclusive."""
    assert _state(status, Stage.SUBMITTED_CLAIMED, claims[paper], paper) is expected


def test_claim_without_timestamp_counts_as_grace_passed() -> None:
    """A claim with no time surfaces instead of waiting forever."""
    assert (
        _state(SubmissionStatus.MISSING, Stage.SUBMITTED_CLAIMED, None)
        is DisplayState.NOT_IN_CANVAS
    )


@pytest.mark.parametrize("status", OPEN)
@pytest.mark.parametrize("paper", [False, True])
def test_done_not_submitted_ranks_above_missing_and_zeroed(
    status: SubmissionStatus, paper: bool
) -> None:
    """Done-but-not-submitted must still show once Canvas calls it missing."""
    assert (
        _state(status, Stage.DONE_NOT_SUBMITTED, paper=paper)
        is DisplayState.DONE_NOT_SUBMITTED
    )


@pytest.mark.parametrize("stage", [Stage.NOT_STARTED, Stage.WORKING])
def test_zeroed_and_missing_beat_early_stages(stage: Stage) -> None:
    """Canvas zero/missing outranks "working" and "not started"."""
    assert _state(SubmissionStatus.ZEROED, stage) is DisplayState.ZEROED
    assert _state(SubmissionStatus.MISSING, stage) is DisplayState.MISSING


def test_upcoming_shows_stage() -> None:
    """Upcoming work shows the student's stage."""
    assert _state(SubmissionStatus.UPCOMING, Stage.WORKING) is DisplayState.WORKING
    assert (
        _state(SubmissionStatus.UPCOMING, Stage.NOT_STARTED) is DisplayState.NOT_STARTED
    )


def test_grace_passed_boundaries() -> None:
    """Online and paper windows are measured separately."""
    assert not grace_passed(AT[False], False, NOW, ONLINE, PAPER)
    assert grace_passed(AFTER[False], False, NOW, ONLINE, PAPER)
    # The online window has passed, but paper uses the longer window.
    assert not grace_passed(AFTER[False], True, NOW, ONLINE, PAPER)
    assert grace_passed(AFTER[True], True, NOW, ONLINE, PAPER)


@pytest.mark.parametrize(
    ("status", "due", "expected"),
    [
        (SubmissionStatus.MISSING, NOW - timedelta(minutes=1), True),
        (SubmissionStatus.ZEROED, NOW - timedelta(days=3), True),
        (SubmissionStatus.UPCOMING, NOW + timedelta(minutes=1), False),
        (SubmissionStatus.UPCOMING, NOW, False),
        (SubmissionStatus.UPCOMING, None, False),
        (SubmissionStatus.LATE, NOW - timedelta(days=1), False),
        (SubmissionStatus.GRADED, NOW - timedelta(days=1), False),
    ],
)
def test_is_overdue(
    status: SubmissionStatus, due: datetime | None, expected: bool
) -> None:
    """Overdue means past due and not done in Canvas."""
    assert is_overdue(status, due, NOW) is expected


def test_summary_prefix() -> None:
    """Late work keeps its late marker; other states use the display prefix."""
    assert summary_prefix(DisplayState.CONFIRMED, SubmissionStatus.LATE) == (
        "✉️ LATE · "
    )
    assert summary_prefix(DisplayState.CONFIRMED, SubmissionStatus.GRADED) == "✅ "
    assert summary_prefix(DisplayState.NOT_STARTED, SubmissionStatus.UPCOMING) == ""
    assert summary_prefix(
        DisplayState.DONE_NOT_SUBMITTED, SubmissionStatus.MISSING
    ).startswith("🟡")


def test_followups_late_work_opens_once() -> None:
    """A late submission opens late_work once and never auto-resolves it."""
    assert followup_actions(SubmissionStatus.LATE, DisplayState.CONFIRMED, {}) == [
        (FollowupReason.LATE_WORK, FollowupAction.OPEN)
    ]
    assert (
        followup_actions(
            SubmissionStatus.LATE,
            DisplayState.CONFIRMED,
            {FollowupReason.LATE_WORK: FollowupStage.NEEDS_CONTACT},
        )
        == []
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (DisplayState.NOT_IN_CANVAS, FollowupReason.NOT_IN_CANVAS),
        (DisplayState.PAPER_NOT_GRADED, FollowupReason.PAPER_NOT_GRADED),
    ],
)
def test_followups_claim_not_shown(state: DisplayState, reason: FollowupReason) -> None:
    """Grace passing on a claim opens a follow-up; an existing one is not reopened."""
    assert followup_actions(SubmissionStatus.MISSING, state, {}) == [
        (reason, FollowupAction.OPEN)
    ]
    for stage in FollowupStage:
        assert followup_actions(SubmissionStatus.MISSING, state, {reason: stage}) == []


@pytest.mark.parametrize(
    "state",
    [
        DisplayState.CLAIMED_PENDING,
        DisplayState.PAPER_WAITING,
        DisplayState.MISSING,
        DisplayState.DONE_NOT_SUBMITTED,
    ],
)
def test_followups_not_opened_for_other_states(state: DisplayState) -> None:
    """Nothing opens while within grace or without a claim."""
    assert followup_actions(SubmissionStatus.MISSING, state, {}) == []


@pytest.mark.parametrize("status", DONE)
def test_followups_auto_resolve_when_canvas_shows_it(
    status: SubmissionStatus,
) -> None:
    """Claim follow-ups close when Canvas shows the work; late_work never does."""
    existing = {
        FollowupReason.NOT_IN_CANVAS: FollowupStage.NEEDS_CONTACT,
        FollowupReason.PAPER_NOT_GRADED: FollowupStage.CONTACTED,
        FollowupReason.LATE_WORK: FollowupStage.NEEDS_CONTACT,
    }
    actions = followup_actions(status, DisplayState.CONFIRMED, existing)
    assert (FollowupReason.NOT_IN_CANVAS, FollowupAction.AUTO_RESOLVE) in actions
    assert (FollowupReason.PAPER_NOT_GRADED, FollowupAction.AUTO_RESOLVE) in actions
    assert (FollowupReason.LATE_WORK, FollowupAction.AUTO_RESOLVE) not in actions


def test_followups_resolved_not_resolved_again() -> None:
    """Already-resolved follow-ups are left alone."""
    assert (
        followup_actions(
            SubmissionStatus.SUBMITTED,
            DisplayState.CONFIRMED,
            {FollowupReason.NOT_IN_CANVAS: FollowupStage.RESOLVED},
        )
        == []
    )


def test_late_and_not_in_canvas_can_coexist() -> None:
    """A late submission that was claimed early can carry both follow-ups."""
    actions = followup_actions(
        SubmissionStatus.LATE,
        DisplayState.CONFIRMED,
        {FollowupReason.NOT_IN_CANVAS: FollowupStage.NEEDS_CONTACT},
    )
    assert set(actions) == {
        (FollowupReason.LATE_WORK, FollowupAction.OPEN),
        (FollowupReason.NOT_IN_CANVAS, FollowupAction.AUTO_RESOLVE),
    }


def test_should_prune() -> None:
    """Prune only when long unseen and every follow-up is resolved."""
    after = timedelta(days=60)
    old = NOW - timedelta(days=61)
    recent = NOW - timedelta(days=59)
    assert should_prune(old, [], NOW, after)
    assert should_prune(old, [FollowupStage.RESOLVED], NOW, after)
    assert not should_prune(old, [FollowupStage.CONTACTED], NOW, after)
    assert not should_prune(recent, [], NOW, after)
    assert not should_prune(NOW - after, [], NOW, after)
    assert should_prune(None, [], NOW, after)


def test_followup_id() -> None:
    """Follow-up ids combine assignment and reason."""
    assert followup_id("5506356", FollowupReason.LATE_WORK) == "5506356:late_work"
