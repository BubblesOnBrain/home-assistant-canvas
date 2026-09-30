"""Student workflow rules: stages, display state and follow-ups.

Canvas facts (SubmissionStatus) and the student's own claims (Stage) are kept
as separate inputs and only combined here, into a display state that is never
stored. Everything in this module is pure so it can be tested with fixed times.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum

from .filtering import SubmissionStatus, is_done


class Stage(StrEnum):
    """Where the student says the work is."""

    NOT_STARTED = "not_started"
    WORKING = "working"
    DONE_NOT_SUBMITTED = "done_not_submitted"
    SUBMITTED_CLAIMED = "submitted_claimed"


class DisplayState(StrEnum):
    """What the list shows: Canvas facts combined with the student's stage."""

    CONFIRMED = "confirmed"
    NOT_IN_CANVAS = "not_in_canvas"
    CLAIMED_PENDING = "claimed_pending"
    PAPER_NOT_GRADED = "paper_not_graded"
    PAPER_WAITING = "paper_waiting"
    DONE_NOT_SUBMITTED = "done_not_submitted"
    ZEROED = "zeroed"
    MISSING = "missing"
    WORKING = "working"
    NOT_STARTED = "not_started"


class FollowupReason(StrEnum):
    """Why the teacher may need to be contacted."""

    LATE_WORK = "late_work"
    NOT_IN_CANVAS = "not_in_canvas"
    PAPER_NOT_GRADED = "paper_not_graded"


class FollowupStage(StrEnum):
    """Progress on a follow-up."""

    NEEDS_CONTACT = "needs_contact"
    CONTACTED = "contacted"
    RESOLVED = "resolved"


class FollowupMethod(StrEnum):
    """How the teacher was contacted."""

    EMAIL = "email"
    LATE_FORM = "late_form"
    IN_PERSON = "in_person"


class FollowupAction(StrEnum):
    """A change the reconcile step should make to a follow-up."""

    OPEN = "open"
    AUTO_RESOLVE = "auto_resolve"


DISPLAY_PREFIX: dict[DisplayState, str] = {
    DisplayState.CONFIRMED: "✅ ",
    DisplayState.NOT_IN_CANVAS: "⚠️ NOT IN CANVAS · ",
    DisplayState.CLAIMED_PENDING: "⏳ SUBMITTED? · ",
    DisplayState.PAPER_NOT_GRADED: "⚠️ PAPER NOT GRADED · ",
    DisplayState.PAPER_WAITING: "📝 TURNED IN · ",
    DisplayState.DONE_NOT_SUBMITTED: "🟡 DONE, SUBMIT IT · ",
    DisplayState.ZEROED: "⭕ ZERO · ",
    DisplayState.MISSING: "🔴 MISSING · ",
    DisplayState.WORKING: "🔵 ",
    DisplayState.NOT_STARTED: "",
}
LATE_PREFIX = "✉️ LATE · "

FOLLOWUP_PREFIX: dict[FollowupReason, str] = {
    FollowupReason.LATE_WORK: "✉️ EMAIL TEACHER · ",
    FollowupReason.NOT_IN_CANVAS: "⚠️ CHECK CANVAS · ",
    FollowupReason.PAPER_NOT_GRADED: "📝 ASK ABOUT PAPER · ",
}

# Follow-ups Canvas can close on its own once it shows the work.
AUTO_RESOLVING_REASONS = frozenset(
    {FollowupReason.NOT_IN_CANVAS, FollowupReason.PAPER_NOT_GRADED}
)

# Display states that need attention first, in the order they are sorted.
URGENT_DISPLAY_STATES: tuple[DisplayState, ...] = (
    DisplayState.NOT_IN_CANVAS,
    DisplayState.PAPER_NOT_GRADED,
    DisplayState.DONE_NOT_SUBMITTED,
    DisplayState.MISSING,
    DisplayState.ZEROED,
)


def grace_passed(
    claimed_submitted_at: datetime | None,
    on_paper: bool,
    now: datetime,
    online_grace: timedelta,
    paper_grace: timedelta,
) -> bool:
    """Return True once Canvas has had its grace window to show a claimed submission.

    The window is inclusive: exactly at the boundary it has not passed yet. A
    missing claim time is treated as passed so the item surfaces instead of
    waiting forever.
    """
    if claimed_submitted_at is None:
        return True
    grace = paper_grace if on_paper else online_grace
    return now - claimed_submitted_at > grace


def display_state(
    canvas_status: SubmissionStatus,
    stage: Stage,
    claimed_submitted_at: datetime | None,
    on_paper: bool,
    now: datetime,
    online_grace: timedelta,
    paper_grace: timedelta,
) -> DisplayState:
    """Combine Canvas's status and the student's stage into one display state.

    First match wins. A claimed submission ranks above missing/zeroed because
    the question becomes "did it arrive?". "Done, needs submitting" also ranks
    above missing/zeroed: Canvas marks every past-due, unsubmitted assignment
    missing, so otherwise that state could never show once it mattered most.
    """
    if is_done(canvas_status):
        return DisplayState.CONFIRMED
    if stage is Stage.SUBMITTED_CLAIMED:
        passed = grace_passed(
            claimed_submitted_at, on_paper, now, online_grace, paper_grace
        )
        if on_paper:
            return (
                DisplayState.PAPER_NOT_GRADED if passed else DisplayState.PAPER_WAITING
            )
        return DisplayState.NOT_IN_CANVAS if passed else DisplayState.CLAIMED_PENDING
    if stage is Stage.DONE_NOT_SUBMITTED:
        return DisplayState.DONE_NOT_SUBMITTED
    if canvas_status is SubmissionStatus.ZEROED:
        return DisplayState.ZEROED
    if canvas_status is SubmissionStatus.MISSING:
        return DisplayState.MISSING
    if stage is Stage.WORKING:
        return DisplayState.WORKING
    return DisplayState.NOT_STARTED


def summary_prefix(state: DisplayState, canvas_status: SubmissionStatus) -> str:
    """Return the to-do summary prefix for a display state."""
    if state is DisplayState.CONFIRMED and canvas_status is SubmissionStatus.LATE:
        return LATE_PREFIX
    return DISPLAY_PREFIX[state]


def is_overdue(
    canvas_status: SubmissionStatus, due_at: datetime | None, now: datetime
) -> bool:
    """Return True if the due date has passed and Canvas doesn't show it done."""
    return due_at is not None and due_at < now and not is_done(canvas_status)


def followup_actions(
    canvas_status: SubmissionStatus,
    state: DisplayState,
    existing: Mapping[FollowupReason, FollowupStage],
) -> list[tuple[FollowupReason, FollowupAction]]:
    """Return the follow-up changes for one assignment.

    Idempotent: a reason that already has a follow-up (in any stage) is never
    opened again, so a follow-up the student resolved stays resolved.
    """
    actions: list[tuple[FollowupReason, FollowupAction]] = []
    wanted = {
        FollowupReason.LATE_WORK: canvas_status is SubmissionStatus.LATE,
        FollowupReason.NOT_IN_CANVAS: state is DisplayState.NOT_IN_CANVAS,
        FollowupReason.PAPER_NOT_GRADED: state is DisplayState.PAPER_NOT_GRADED,
    }
    for reason, open_it in wanted.items():
        if open_it and reason not in existing:
            actions.append((reason, FollowupAction.OPEN))
    if is_done(canvas_status):
        for reason, stage in existing.items():
            if reason in AUTO_RESOLVING_REASONS and stage is not FollowupStage.RESOLVED:
                actions.append((reason, FollowupAction.AUTO_RESOLVE))
    return actions


def should_prune(
    last_seen_at: datetime | None,
    followup_stages: list[FollowupStage],
    now: datetime,
    prune_after: timedelta,
) -> bool:
    """Return True if a record can be dropped from storage.

    Only when Canvas hasn't returned the assignment for `prune_after` and every
    follow-up is resolved. Leaving the Canvas list never deletes anything alone.
    """
    if any(stage is not FollowupStage.RESOLVED for stage in followup_stages):
        return False
    return last_seen_at is None or now - last_seen_at > prune_after


def followup_id(assignment_id: str, reason: FollowupReason) -> str:
    """Return the storage key for a follow-up."""
    return f"{assignment_id}:{reason.value}"
