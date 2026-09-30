"""Student workflow rules: stages, display state and follow-ups.

Canvas facts (SubmissionStatus) and the student's own claims (Stage) are kept
as separate inputs and only combined here, into a display state that is never
stored. Everything in this module is pure so it can be tested with fixed times.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
import re

from .filtering import SubmissionStatus, is_done


class AssignmentKind(StrEnum):
    """What sort of work an assignment is."""

    HOMEWORK = "homework"  # something she hands in
    QUIZ = "quiz"  # something she studies for and takes
    TEST = "test"


class Stage(StrEnum):
    """Where the student says the work is."""

    NOT_STARTED = "not_started"
    # Homework
    WORKING = "working"
    DONE_NOT_SUBMITTED = "done_not_submitted"
    SUBMITTED_CLAIMED = "submitted_claimed"
    # Quizzes and tests
    STUDYING = "studying"
    READY = "ready"
    NEEDS_MAKEUP = "needs_makeup"


HOMEWORK_STAGES: tuple[Stage, ...] = (
    Stage.NOT_STARTED,
    Stage.WORKING,
    Stage.DONE_NOT_SUBMITTED,
    Stage.SUBMITTED_CLAIMED,
)
ASSESSMENT_STAGES: tuple[Stage, ...] = (
    Stage.NOT_STARTED,
    Stage.STUDYING,
    Stage.READY,
    Stage.NEEDS_MAKEUP,
)


class DisplayState(StrEnum):
    """What the list shows: Canvas facts combined with the student's stage."""

    CONFIRMED = "confirmed"
    NEEDS_MAKEUP = "needs_makeup"
    NOT_IN_CANVAS = "not_in_canvas"
    CLAIMED_PENDING = "claimed_pending"
    PAPER_NOT_GRADED = "paper_not_graded"
    PAPER_WAITING = "paper_waiting"
    DONE_NOT_SUBMITTED = "done_not_submitted"
    ZEROED = "zeroed"
    MISSING = "missing"
    WORKING = "working"
    AWAITING_GRADE = "awaiting_grade"  # quiz/test taken on paper, not graded
    STUDYING = "studying"
    READY = "ready"
    NOT_STARTED = "not_started"


class FollowupReason(StrEnum):
    """Why the teacher may need to be contacted."""

    LATE_WORK = "late_work"
    NOT_IN_CANVAS = "not_in_canvas"
    PAPER_NOT_GRADED = "paper_not_graded"
    MAKEUP = "makeup"


class Bucket(StrEnum):
    """Which list an assignment belongs in."""

    ATTENTION = "attention"  # she needs to act
    UPCOMING = "upcoming"  # homework due soon
    ASSESSMENT = "assessment"  # quiz/test coming up: study
    WAITING = "waiting"  # handed in, waiting on the teacher
    REVIEW = "review"  # graded, student hasn't reviewed
    PARENT_REVIEW = "parent_review"  # student reviewed, parent hasn't
    CLOSED = "closed"  # closed without a grade
    LATER = "later"  # not due soon
    DONE = "done"
    HIDDEN = "hidden"  # excused or undated


class ReviewRole(StrEnum):
    """Who is marking a grade reviewed."""

    STUDENT = "student"
    PARENT = "parent"


class CloseReason(StrEnum):
    """Why waiting work was closed without a grade."""

    FEEDBACK_RECEIVED = "feedback_received"
    NOT_GRADED = "not_graded"
    OTHER = "other"


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
    DisplayState.NEEDS_MAKEUP: "🔴 MAKE-UP NEEDED · ",
    DisplayState.NOT_IN_CANVAS: "⚠️ NOT IN CANVAS · ",
    DisplayState.CLAIMED_PENDING: "⏳ SUBMITTED? · ",
    DisplayState.PAPER_NOT_GRADED: "⚠️ PAPER NOT GRADED · ",
    DisplayState.PAPER_WAITING: "📝 TURNED IN · ",
    DisplayState.DONE_NOT_SUBMITTED: "🟡 DONE, SUBMIT IT · ",
    DisplayState.ZEROED: "⭕ ZERO · ",
    DisplayState.MISSING: "🔴 MISSING · ",
    DisplayState.WORKING: "🔵 ",
    DisplayState.AWAITING_GRADE: "⏳ AWAITING GRADE · ",
    DisplayState.STUDYING: "📖 STUDYING · ",
    DisplayState.READY: "🟢 READY · ",
    DisplayState.NOT_STARTED: "",
}
KIND_PREFIX: dict[AssignmentKind, str] = {
    AssignmentKind.QUIZ: "📚 QUIZ · ",
    AssignmentKind.TEST: "📚 TEST · ",
}
LATE_PREFIX = "✉️ LATE · "

FOLLOWUP_PREFIX: dict[FollowupReason, str] = {
    FollowupReason.LATE_WORK: "✉️ EMAIL TEACHER · ",
    FollowupReason.NOT_IN_CANVAS: "⚠️ CHECK CANVAS · ",
    FollowupReason.PAPER_NOT_GRADED: "📝 ASK ABOUT PAPER · ",
    FollowupReason.MAKEUP: "📅 SCHEDULE MAKE-UP · ",
}

# Follow-ups Canvas can close on its own once it shows the work or a grade.
AUTO_RESOLVING_REASONS = frozenset(
    {
        FollowupReason.NOT_IN_CANVAS,
        FollowupReason.PAPER_NOT_GRADED,
        FollowupReason.MAKEUP,
    }
)

ATTENTION_STATES = frozenset(
    {
        DisplayState.NOT_IN_CANVAS,
        DisplayState.MISSING,
        DisplayState.ZEROED,
        DisplayState.NEEDS_MAKEUP,
    }
)
# Attention states that come from Canvas (or a missed quiz), not from a claim.
CANVAS_ATTENTION_STATES = frozenset(
    {DisplayState.MISSING, DisplayState.ZEROED, DisplayState.NEEDS_MAKEUP}
)
WAITING_STATES = frozenset(
    {
        DisplayState.CLAIMED_PENDING,
        DisplayState.PAPER_WAITING,
        DisplayState.PAPER_NOT_GRADED,
        DisplayState.AWAITING_GRADE,
    }
)

# Names that mark work to hand in even if they mention a quiz or test.
_PREP_WORK = re.compile(
    r"study guide|\breview\b|practice|correction|\bprep\b|project|quizlet",
    re.IGNORECASE,
)
_TEST_NAME = re.compile(
    # Not bare "final" or "FRQ": "Final Draft" and "FRQ #3" are homework.
    r"\b(tests?|exams?|midterms?|final (exams?|tests?)|semester finals?"
    r"|summative|unit assessment)\b",
    re.IGNORECASE,
)
_QUIZ_NAME = re.compile(r"\bquiz(zes)?\b", re.IGNORECASE)
_TEST_GROUP = re.compile(r"\b(tests?|exams?)\b", re.IGNORECASE)
_QUIZ_GROUP = re.compile(r"\bquiz(zes)?\b", re.IGNORECASE)


def classify_kind(name: str, is_quiz: bool, group_name: str) -> AssignmentKind:
    """Decide whether an assignment is homework, a quiz or a test.

    Study guides, reviews and practice mention quizzes and tests but are
    handed in, so they stay homework. Group names only count when they say
    test/exam/quiz ("Major" groups also hold projects).
    """
    if _PREP_WORK.search(name):
        return AssignmentKind.HOMEWORK
    if _TEST_NAME.search(name):
        return AssignmentKind.TEST
    if _QUIZ_NAME.search(name) or is_quiz:
        return AssignmentKind.QUIZ
    if _TEST_GROUP.search(group_name):
        return AssignmentKind.TEST
    if _QUIZ_GROUP.search(group_name):
        return AssignmentKind.QUIZ
    return AssignmentKind.HOMEWORK


def work_done(
    canvas_status: SubmissionStatus, kind: AssignmentKind, on_paper: bool
) -> bool:
    """Return True if Canvas shows the work finished.

    A 0 on a paper quiz or test is a real grade (she took it in class), not a
    placeholder for missing work.
    """
    if is_done(canvas_status):
        return True
    return (
        kind is not AssignmentKind.HOMEWORK
        and on_paper
        and canvas_status is SubmissionStatus.ZEROED
    )


def is_graded(
    score: float | None,
    grade: str | None,
    excused: bool,
    canvas_status: SubmissionStatus,
    kind: AssignmentKind,
    on_paper: bool,
) -> bool:
    """Return True if the teacher has entered a real grade.

    Placeholder zeros (no submission) and work flagged missing don't count.
    """
    if excused or canvas_status is SubmissionStatus.MISSING:
        return False
    if score is None and grade in (None, ""):
        return False
    if canvas_status is SubmissionStatus.ZEROED:
        return work_done(canvas_status, kind, on_paper)
    return True


def grade_percent(score: float | None, points_possible: float | None) -> float | None:
    """Return the score as a percentage, if it can be worked out."""
    if score is None or not points_possible:
        return None
    return round(score / points_possible * 100, 1)


def is_escalated(percent: float | None, threshold: float) -> bool:
    """Return True for a grade below the escalation threshold (C or lower)."""
    return percent is not None and percent < threshold


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
    *,
    kind: AssignmentKind = AssignmentKind.HOMEWORK,
) -> DisplayState:
    """Combine Canvas's status and the student's stage into one display state.

    First match wins. A claimed submission ranks above missing/zeroed because
    the question becomes "did it arrive?". "Done, needs submitting" also ranks
    above missing/zeroed: Canvas marks every past-due, unsubmitted assignment
    missing, so otherwise that state could never show once it mattered most.
    """
    if work_done(canvas_status, kind, on_paper):
        return DisplayState.CONFIRMED
    if kind is not AssignmentKind.HOMEWORK:
        return _assessment_state(canvas_status, stage, on_paper)
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


def _assessment_state(
    canvas_status: SubmissionStatus, stage: Stage, on_paper: bool
) -> DisplayState:
    """Display state for a quiz or test Canvas doesn't show finished.

    A paper quiz or test past its date was almost always taken in class and
    is waiting for a grade, not missing; she says so if she missed it.
    """
    if stage is Stage.NEEDS_MAKEUP:
        return DisplayState.NEEDS_MAKEUP
    if on_paper and canvas_status is SubmissionStatus.MISSING:
        return DisplayState.AWAITING_GRADE
    if canvas_status is SubmissionStatus.ZEROED:
        return DisplayState.ZEROED
    if canvas_status is SubmissionStatus.MISSING:
        return DisplayState.MISSING
    if stage in (Stage.STUDYING, Stage.WORKING):
        return DisplayState.STUDYING
    if stage is Stage.READY:
        return DisplayState.READY
    return DisplayState.NOT_STARTED


def summary_prefix(
    state: DisplayState,
    canvas_status: SubmissionStatus,
    kind: AssignmentKind = AssignmentKind.HOMEWORK,
) -> str:
    """Return the to-do summary prefix for a display state."""
    if state is DisplayState.CONFIRMED and canvas_status is SubmissionStatus.LATE:
        return LATE_PREFIX
    if state is DisplayState.NOT_STARTED and kind in KIND_PREFIX:
        return KIND_PREFIX[kind]
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
    *,
    graded: bool = False,
    closed: bool = False,
) -> list[tuple[FollowupReason, FollowupAction]]:
    """Return the follow-up changes for one assignment.

    Idempotent: a reason that already has a follow-up (in any stage) is never
    opened again, so a follow-up the student resolved stays resolved. Work
    closed without a grade opens no "did it arrive?" follow-ups.
    """
    actions: list[tuple[FollowupReason, FollowupAction]] = []
    wanted = {
        FollowupReason.LATE_WORK: canvas_status is SubmissionStatus.LATE,
        FollowupReason.NOT_IN_CANVAS: state is DisplayState.NOT_IN_CANVAS,
        FollowupReason.PAPER_NOT_GRADED: state is DisplayState.PAPER_NOT_GRADED,
        FollowupReason.MAKEUP: state is DisplayState.NEEDS_MAKEUP,
    }
    if closed:
        wanted[FollowupReason.NOT_IN_CANVAS] = False
        wanted[FollowupReason.PAPER_NOT_GRADED] = False
    for reason, open_it in wanted.items():
        if open_it and reason not in existing:
            actions.append((reason, FollowupAction.OPEN))
    if is_done(canvas_status) or graded:
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


def record_key(student_id: int, assignment_id: str) -> str:
    """Return the storage key for a student's assignment record.

    Canvas assignment ids are per course, not per student, so two students in
    the same class (observed by one parent) need separate records.
    """
    return f"{student_id}:{assignment_id}"


def followup_id(student_id: int, assignment_id: str, reason: FollowupReason) -> str:
    """Return the storage key for a follow-up."""
    return f"{record_key(student_id, assignment_id)}:{reason.value}"


def work_bucket(
    *,
    kind: AssignmentKind,
    state: DisplayState,
    canvas_status: SubmissionStatus,
    graded: bool,
    overdue: bool,
    due_at: datetime | None,
    now: datetime,
    upcoming_window: timedelta,
    lead: timedelta,
    in_review_window: bool,
    student_reviewed: bool,
    parent_reviewed: bool,
    closed: bool,
) -> Bucket:
    """Decide which list an assignment belongs in; first match wins.

    Graded work goes through review (student, then parent) unless it was
    graded before review tracking started. Closing without a grade only
    applies while no grade exists: a grade that arrives later reopens it
    into review.
    """
    if canvas_status is SubmissionStatus.EXCUSED:
        return Bucket.HIDDEN
    if graded:
        if not in_review_window or (student_reviewed and parent_reviewed):
            return Bucket.DONE
        return Bucket.PARENT_REVIEW if student_reviewed else Bucket.REVIEW
    # Closing only stops waiting on the teacher: work Canvas still shows
    # missing or zeroed (or a missed quiz) keeps needing attention.
    if closed and state not in CANVAS_ATTENTION_STATES:
        return Bucket.CLOSED
    if state in ATTENTION_STATES or (
        state is DisplayState.DONE_NOT_SUBMITTED and overdue
    ):
        return Bucket.ATTENTION
    if state in WAITING_STATES or canvas_status in (
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.LATE,
        SubmissionStatus.GRADED,
    ):
        return Bucket.WAITING
    if due_at is None:
        return Bucket.HIDDEN
    homework = kind is AssignmentKind.HOMEWORK
    if due_at > now + (upcoming_window if homework else lead):
        return Bucket.LATER
    return Bucket.UPCOMING if homework else Bucket.ASSESSMENT
