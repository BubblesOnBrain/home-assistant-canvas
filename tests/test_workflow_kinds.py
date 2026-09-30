"""Tests for quiz/test handling, grading and list placement rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.canvas.filtering import SubmissionStatus as S
from custom_components.canvas.workflow import (
    AssignmentKind as K,
    Bucket,
    DisplayState as D,
    FollowupAction,
    FollowupReason,
    FollowupStage,
    Stage,
    classify_kind,
    display_state,
    followup_actions,
    grade_percent,
    is_escalated,
    is_graded,
    summary_prefix,
    work_bucket,
    work_done,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
GRACE = (timedelta(hours=12), timedelta(days=7))


@pytest.mark.parametrize(
    ("name", "is_quiz", "group", "expected"),
    [
        # Real names from Sydney's classes
        ("Quiz: Newton's Second Law", False, "", K.QUIZ),
        ("Unit 1 Vocabulary Quiz", False, "", K.QUIZ),
        ("Welcome Module Quiz", True, "", K.QUIZ),
        ("Biological Quiz 1.1", False, "", K.QUIZ),
        ("Kinematics Exam FRQ", False, "", K.TEST),
        ("Revolution Quiz Study Guide", False, "", K.HOMEWORK),
        ("Quiz 1 Study Guide", False, "", K.HOMEWORK),
        ("Unit 1 Test Review", False, "", K.HOMEWORK),
        ("Kinematics Practice #31-35", False, "", K.HOMEWORK),
        ("Neurotransmitter Project", False, "Major Assessments", K.HOMEWORK),
        ("Dynamics Multiple Choice #1-11", False, "Homework", K.HOMEWORK),
        # Group names only when they say test/exam/quiz
        ("Chapter 3", False, "Tests", K.TEST),
        ("Chapter 3", False, "Quizzes", K.QUIZ),
        ("Chapter 3", False, "Major", K.HOMEWORK),
        ("Use Quizlet to review", False, "", K.HOMEWORK),
    ],
)
def test_classify_kind(name: str, is_quiz: bool, group: str, expected: K) -> None:
    """Quizzes and tests are told apart from homework, including prep work."""
    assert classify_kind(name, is_quiz, group) is expected


def _state(status: S, stage: Stage = Stage.NOT_STARTED, *, paper: bool, kind: K) -> D:
    return display_state(status, stage, None, paper, NOW, *GRACE, kind=kind)


@pytest.mark.parametrize("kind", [K.QUIZ, K.TEST])
def test_paper_assessment_past_date_awaits_grade(kind: K) -> None:
    """A paper quiz/test past its date is waiting on a grade, not missing."""
    assert _state(S.MISSING, paper=True, kind=kind) is D.AWAITING_GRADE
    # A 0 on a paper test is a real grade.
    assert _state(S.ZEROED, paper=True, kind=kind) is D.CONFIRMED
    assert work_done(S.ZEROED, kind, True) is True
    # Online: no submission really is missing.
    assert _state(S.MISSING, paper=False, kind=kind) is D.MISSING
    assert _state(S.ZEROED, paper=False, kind=kind) is D.ZEROED
    assert work_done(S.ZEROED, kind, False) is False


def test_assessment_study_stages_and_makeup() -> None:
    """Study stages show before the date; make-up needed wins after it."""
    assert _state(S.UPCOMING, Stage.STUDYING, paper=True, kind=K.QUIZ) is D.STUDYING
    assert _state(S.UPCOMING, Stage.READY, paper=True, kind=K.TEST) is D.READY
    assert _state(S.UPCOMING, paper=True, kind=K.TEST) is D.NOT_STARTED
    assert (
        _state(S.MISSING, Stage.NEEDS_MAKEUP, paper=True, kind=K.TEST) is D.NEEDS_MAKEUP
    )
    assert _state(S.GRADED, Stage.NEEDS_MAKEUP, paper=True, kind=K.TEST) is D.CONFIRMED


def test_homework_unchanged() -> None:
    """Homework keeps its existing rules (paper homework past due is missing)."""
    assert _state(S.MISSING, paper=True, kind=K.HOMEWORK) is D.MISSING
    assert _state(S.ZEROED, paper=True, kind=K.HOMEWORK) is D.ZEROED
    assert work_done(S.ZEROED, K.HOMEWORK, True) is False


def test_summary_prefix_marks_quizzes_and_tests() -> None:
    """Not-started quizzes and tests are labelled as such."""
    assert summary_prefix(D.NOT_STARTED, S.UPCOMING, K.QUIZ) == "📚 QUIZ · "
    assert summary_prefix(D.NOT_STARTED, S.UPCOMING, K.TEST) == "📚 TEST · "
    assert summary_prefix(D.NOT_STARTED, S.UPCOMING, K.HOMEWORK) == ""
    assert summary_prefix(D.AWAITING_GRADE, S.MISSING, K.TEST).startswith("⏳")


@pytest.mark.parametrize(
    ("score", "grade", "excused", "status", "kind", "paper", "expected"),
    [
        (8.0, "8", False, S.SUBMITTED, K.HOMEWORK, False, True),
        (0.0, "0", False, S.SUBMITTED, K.HOMEWORK, False, True),  # real 0 on work
        (0.0, "0", False, S.ZEROED, K.HOMEWORK, False, False),  # placeholder
        (0.0, "0", False, S.ZEROED, K.TEST, True, True),  # 0 on a paper test
        (0.0, "0", False, S.ZEROED, K.QUIZ, False, False),  # online, not taken
        (None, "complete", False, S.GRADED, K.HOMEWORK, True, True),
        (None, None, False, S.SUBMITTED, K.HOMEWORK, False, False),
        (9.0, "9", True, S.EXCUSED, K.HOMEWORK, False, False),
        (0.0, "0", False, S.MISSING, K.HOMEWORK, False, False),
    ],
)
def test_is_graded(
    score: float | None,
    grade: str | None,
    excused: bool,
    status: S,
    kind: K,
    paper: bool,
    expected: bool,
) -> None:
    """Only real grades count; placeholder zeros don't."""
    assert is_graded(score, grade, excused, status, kind, paper) is expected


def test_grade_percent_and_escalation() -> None:
    """C or lower means below the threshold."""
    assert grade_percent(7, 10) == 70.0
    assert grade_percent(7, 0) is None
    assert grade_percent(None, 10) is None
    assert is_escalated(79.9, 80) is True
    assert is_escalated(80.0, 80) is False
    assert is_escalated(None, 80) is False


def test_makeup_followup_opens_and_resolves_on_grade() -> None:
    """Make-up needed opens a follow-up that closes once a grade arrives."""
    assert followup_actions(S.MISSING, D.NEEDS_MAKEUP, {}) == [
        (FollowupReason.MAKEUP, FollowupAction.OPEN)
    ]
    assert followup_actions(
        S.ZEROED,
        D.CONFIRMED,
        {FollowupReason.MAKEUP: FollowupStage.CONTACTED},
        graded=True,
    ) == [(FollowupReason.MAKEUP, FollowupAction.AUTO_RESOLVE)]


def _bucket(**kw: object) -> Bucket:
    args: dict[str, object] = {
        "kind": K.HOMEWORK,
        "state": D.NOT_STARTED,
        "canvas_status": S.UPCOMING,
        "graded": False,
        "overdue": False,
        "due_at": NOW + timedelta(days=2),
        "now": NOW,
        "upcoming_window": timedelta(days=14),
        "lead": timedelta(days=3),
        "in_review_window": True,
        "student_reviewed": False,
        "parent_reviewed": False,
        "closed": False,
    }
    args.update(kw)
    return work_bucket(**args)  # type: ignore[arg-type]


def test_buckets_for_homework() -> None:
    """Homework moves attention -> upcoming -> waiting -> review."""
    assert _bucket() is Bucket.UPCOMING
    assert _bucket(due_at=NOW + timedelta(days=20)) is Bucket.LATER
    assert _bucket(due_at=None) is Bucket.HIDDEN
    assert _bucket(state=D.MISSING, canvas_status=S.MISSING) is Bucket.ATTENTION
    assert _bucket(state=D.NOT_IN_CANVAS) is Bucket.ATTENTION
    assert _bucket(state=D.DONE_NOT_SUBMITTED) is Bucket.UPCOMING
    assert _bucket(state=D.DONE_NOT_SUBMITTED, overdue=True) is Bucket.ATTENTION
    assert _bucket(state=D.CLAIMED_PENDING) is Bucket.WAITING
    assert _bucket(state=D.PAPER_NOT_GRADED) is Bucket.WAITING
    assert _bucket(state=D.CONFIRMED, canvas_status=S.SUBMITTED) is Bucket.WAITING
    assert _bucket(state=D.CONFIRMED, canvas_status=S.EXCUSED) is Bucket.HIDDEN


def test_buckets_for_review_and_close() -> None:
    """Graded work goes student review -> parent review -> done."""
    graded = {"state": D.CONFIRMED, "canvas_status": S.GRADED, "graded": True}
    assert _bucket(**graded) is Bucket.REVIEW
    assert _bucket(**graded, student_reviewed=True) is Bucket.PARENT_REVIEW
    assert _bucket(**graded, student_reviewed=True, parent_reviewed=True) is Bucket.DONE
    # Parent reviewed on its own still leaves the student's review.
    assert _bucket(**graded, parent_reviewed=True) is Bucket.REVIEW
    assert _bucket(**graded, in_review_window=False) is Bucket.DONE
    # Closing hides waiting work; a later grade reopens it into review.
    waiting = {"state": D.CONFIRMED, "canvas_status": S.SUBMITTED}
    assert _bucket(**waiting, closed=True) is Bucket.CLOSED
    assert _bucket(**graded, closed=True) is Bucket.REVIEW


def test_buckets_for_assessments() -> None:
    """Quizzes/tests show within their study lead time, then wait for grades."""
    quiz = {"kind": K.QUIZ}
    assert _bucket(**quiz) is Bucket.ASSESSMENT
    assert _bucket(**quiz, due_at=NOW + timedelta(days=5)) is Bucket.LATER
    assert (
        _bucket(**quiz, due_at=NOW + timedelta(days=5), lead=timedelta(days=7))
        is Bucket.ASSESSMENT
    )
    assert _bucket(**quiz, state=D.AWAITING_GRADE, canvas_status=S.MISSING) is (
        Bucket.WAITING
    )
    assert _bucket(**quiz, state=D.NEEDS_MAKEUP) is Bucket.ATTENTION
