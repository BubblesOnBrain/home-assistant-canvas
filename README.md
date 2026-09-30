# Canvas LMS Integration for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/allenporter/home-assistant-canvas)](https://github.com/allenporter/home-assistant-canvas/releases)
[![Test](https://github.com/allenporter/home-assistant-canvas/actions/workflows/test.yaml/badge.svg)](https://github.com/allenporter/home-assistant-canvas/actions/workflows/test.yaml)
[![Lint](https://github.com/allenporter/home-assistant-canvas/actions/workflows/lint.yaml/badge.svg)](https://github.com/allenporter/home-assistant-canvas/actions/workflows/lint.yaml)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Home Assistant custom component that integrates with **Instructure Canvas LMS** to track academic progress, grades, actionable assignments, and course calendars for students and observing parents.

---

## Features

- 🎓 **Multi-Student Auto-Discovery**:

  - Automatically discovers all observed students for parent accounts, or connects directly to a student login.
  - Registers distinct **Device Profiles** in Home Assistant's Device Registry for each student.

- 📊 **Real-Time Grade Sensors (`sensor`)**:

  - Monitors current score percentages (`%`) and letter grades (e.g., `A-`, `B+`) per course.
  - Tracks instructor name, course code, academic term, and grading period title as entity attributes.

- ✅ **Actionable To-Do Lists (`todo`)**:

  - Creates an interactive To-Do list per student (`todo.<student>_assignments`).
  - **Chronologically Sorted**: Orders upcoming deadlines and overdue tasks chronologically.
  - **Smart In-Class Noise Filter**: Filters out 5-minute bell-ringers (_Do Nows_), lecture guided notes, and in-class period rehearsals, while preserving real homework (both online uploads and physical paper worksheets).
  - **Canvas decides what's done**: an assignment is checked off only when Canvas shows it submitted, graded or excused. Placeholder zeros stay open.

- 📝 **Student workflow** (stages, notes and teacher follow-ups): see [Student workflow](#student-workflow).

- 📅 **Classroom Learning Calendars (`calendar`)**:

  - Maps assignment due dates, daily classroom topics, and project deadlines directly to Home Assistant's Calendar view (`calendar.<student>_assignments`).
  - Supports datetime range queries and upcoming event sensors.

- 🛡️ **Intelligent Heuristics & Noise Filtering**:
  - Filters out archived, concluded, or placeholder courses.
  - Excludes pre-term cloned syllabus templates and already graded/excused submissions.

---

## Installation

### Method 1: HACS (Recommended)

1. Ensure [HACS (Home Assistant Community Store)](https://hacs.xyz/) is installed.
2. In Home Assistant, open **HACS** > **Integrations**.
3. Click the three dots in the top-right corner and select **Custom repositories**.
4. Enter `https://github.com/BubblesOnBrain/home-assistant-canvas` and select **Integration** as the category.
5. Search for **Canvas**, click **Download**, and restart Home Assistant.

### Method 2: Manual Installation

1. Download the latest release from the [Releases page](https://github.com/allenporter/home-assistant-canvas/releases).
2. Copy the `custom_components/canvas` folder into your Home Assistant `<config>/custom_components/` directory:
   ```bash
   cp -r custom_components/canvas /path/to/homeassistant/config/custom_components/
   ```
3. Restart Home Assistant.

---

## Configuration

### 1. Find Your Canvas Base URL

Locate the web address you use to sign in to Canvas:

- Standard Instructure URL: `https://<school-name>.instructure.com`
- Custom District Domain: `https://canvas.<district>.edu`

### 2. Generate a Personal Access Token

1. Log in to your Canvas LMS account.
2. In the left navigation menu, click **Account** > **Settings**.
3. Scroll down to the **Approved Integrations** section.
4. Click **+ New Access Token**.
5. Enter a purpose (e.g., `Home Assistant`) and click **Generate Token**.
6. Copy the generated token string.

### 3. Add Integration in Home Assistant

1. In Home Assistant, go to **Settings** > **Devices & Services** > **Add Integration**.
2. Search for **Canvas**.
3. Enter your **Canvas Base URL** and paste your **Access Token**.
4. Click **Submit**.

---

## Supported Entities & Devices

| Platform   | Entity Pattern                    | Description                                                         |
| :--------- | :-------------------------------- | :------------------------------------------------------------------ |
| `sensor`   | `sensor.<student>_<course>_grade` | Course grade sensor (% state, letter grade, instructor, term).      |
| `todo`     | `todo.<student>_assignments`      | Chronologically sorted homework plus teacher follow-up tasks.       |
| `calendar` | `calendar.<student>_assignments`  | Assignment deadlines and classroom learning topics on the calendar. |

---

## Student workflow

Canvas says what reached Canvas. The student adds what she's done: a **stage**
and a **note** on each assignment. The two are stored separately and only
combined for display, so the interesting cases (she says she submitted it,
Canvas doesn't show it) are visible instead of hidden.

### Stages

| Stage                | Label                   |
| :------------------- | :---------------------- |
| `not_started`        | Not started             |
| `working`            | Working on it           |
| `done_not_submitted` | Done – needs submitting |
| `submitted_claimed`  | I submitted it          |

Quizzes and tests have their own stages: `not_started`, `studying`, `ready`
and `needs_makeup`, which opens a make-up follow-up.

Marking **I submitted it** records the time. If Canvas still doesn't show the
work after the grace period (12 hours for online work, 7 days for paper), the
item shows **⚠️ NOT IN CANVAS** or **⚠️ PAPER NOT GRADED** and a follow-up
opens. A stage never checks an assignment off; only Canvas does.

The label shown on each item (`display_state`) is decided in this order, first
match wins: in Canvas (`confirmed`) → not in Canvas / submitted? / paper not
graded / turned in (her claim, by grace period) → done, submit it → zero →
missing → working / not started.

### Homework, quizzes and tests

Each assignment is sorted into a kind: homework (something to hand in), a
quiz or a test (something to study for and take). Names win over Canvas
groups: study guides, reviews, practice and corrections stay homework even
when they mention a quiz or test; otherwise "test", "exam", "midterm", "final"
and similar mean a test, "quiz" or a Canvas quiz means a quiz, and then the
assignment group name (`Tests`, `Quizzes`) decides. A wrong guess can be fixed
from the card (**More → Kind**) or with the `kind` field of
`canvas.set_assignment_stage`; `auto` goes back to the automatic guess.

A paper quiz or test past its date is shown **⏳ AWAITING GRADE**, not missing:
it was almost always taken in class. A 0 on one counts as a real grade.

### Lists

All current-term work lands in exactly one list, first match wins:

| List            | What's in it                                                         |
| :-------------- | :------------------------------------------------------------------- |
| `review`        | Graded since review tracking started; the student hasn't reviewed it |
| `parent_review` | The student reviewed it; a parent hasn't                             |
| `closed`        | Closed without a grade (until a grade arrives, which reopens it)     |
| `attention`     | Missing, zero, not in Canvas, make-up needed, done-but-overdue       |
| `waiting`       | Turned in or taken, waiting on the teacher — as long as it takes     |
| `upcoming`      | Homework due in the next 14 days                                     |
| `assessment`    | Quizzes and tests coming up within their lead time (3 and 7 days)    |

Graded work reviewed by both the student and a parent, or graded before
review tracking started (it starts 14 days before 0.3.0 was first loaded),
drops off. Each review records who did it (the Home Assistant user). Grades
below the escalation threshold (80%, a C or lower) are marked 🔺.

Only a Home Assistant administrator can mark the parent review. A grade that
changes after it was reviewed goes back to review.

Waiting work that won't get a grade can be **closed without grade** with a
reason (`feedback_received`, `not_graded`, `other`) and a note, and reopened. It stays on the closed list for 30 days. Closing
doesn't hide work Canvas still shows missing or zeroed.

Every grade is also kept in a grade log (score, percent, kind, Canvas group,
graded time) for trends, for 400 days.

### Teacher follow-ups

| Reason             | Opens when                                                | Closes when                        |
| :----------------- | :-------------------------------------------------------- | :--------------------------------- |
| `late_work`        | Canvas shows a late submission                            | Only when the student resolves it  |
| `not_in_canvas`    | Online work marked submitted, grace passed, not in Canvas | Automatically when Canvas shows it |
| `paper_not_graded` | Paper work marked turned in, grace passed, not graded     | Automatically when Canvas shows it |
| `makeup`           | A quiz or test set to "needs make-up"                     | Automatically when it's graded     |

Each follow-up appears as its own task on the to-do list (for example
**✉️ EMAIL TEACHER · [Biology] Cell Lab Report**), due the next day. Ticking the
task sets it to **contacted**; the card can also set **resolved**, how the
teacher was contacted (`email`, `late_form`, `in_person`) and a note. If Canvas
closes a follow-up, anything she wrote on it is kept. Resolved
follow-ups stay visible for 7 days. If Canvas stops returning the assignment,
the task stays and is marked **No longer in Canvas**.

### Options

**Settings → Devices & Services → Canvas → Configure**:

| Option                 | Default | Range  |
| :--------------------- | :------ | :----- |
| `online_grace_hours`   | 12      | 1–72   |
| `paper_grace_days`     | 7       | 1–21   |
| `quiz_lead_days`       | 3       | 1–14   |
| `test_lead_days`       | 7       | 1–30   |
| `escalation_threshold` | 80 (%)  | 50–100 |

### Services

All work for non-admin users. `config_entry_id` is optional when only one
Canvas account is set up.

```yaml
action: canvas.set_assignment_stage
data:
  assignment_id: "5506356" # the to-do item's uid
  student_id: 6021 # optional; needed only if two students share the assignment
  stage: submitted_claimed # optional
  note: Uploaded the PDF # optional; "" clears it; max 500 characters
  kind: quiz # optional; homework | quiz | test | auto
```

```yaml
action: canvas.review_assignment
data:
  assignment_id: "5506356"
  role: student # student (default) | parent
  undo: false # true clears it; undoing the student's review clears the parent's
```

```yaml
action: canvas.close_assignment
data:
  assignment_id: "5506356"
  reason: feedback_received # feedback_received | not_graded | other
  note: Went over it in class
  undo: false # true reopens it
```

```yaml
action: canvas.set_followup_stage
data:
  followup_id: "6021:5506356:late_work" # student:assignment:reason
  stage: contacted # needs_contact | contacted | resolved
  method: email # email | late_form | in_person
  note: Emailed Ms. Rivera
```

### Attributes for automations

On `todo.<student>_assignments`:

- Counts: `<canvas status>_count`, `display_<state>_count`, `overdue_count`,
  `done_not_submitted_overdue_count`, `not_in_canvas_count`,
  `followup_open_count`, `followup_needs_contact_count`,
  `followup_overdue_count`, `undated_count`, `attention_count`,
  `upcoming_count`, `assessment_count`, `waiting_count`, `review_count`,
  `parent_review_count`, `closed_count`, `escalated_count` (flagged grades
  still in review).
- Lists (not recorded to history): `attention_items`, `upcoming_items`,
  `assessment_items`, `waiting_items`, `review_items`, `parent_review_items`,
  `closed_items`, `late_items`, `followup_items`. Assignment items include
  `kind`, `stage`, `note`, `display_state`, `overdue`, `claimed_submitted_at`,
  `score`, `percent`, `escalated`, `graded_at`, who reviewed it and when, and
  close details.

### canvas-workflow-card

The integration serves this card and adds it to your dashboard resources
itself (Settings → Dashboards → ⋮ → Resources), keeping its version up to date.
There is nothing to add by hand. After updating, reload the browser once. If
your dashboards are managed in YAML, the card is loaded on every page instead.

```yaml
type: custom:canvas-workflow-card
entity: todo.sydney_kohl_assignments
# Any of: attention, upcoming, assessments, waiting, review, parent_review,
# closed, followups (in the order given). Default: all but parent_review and
# closed, which suit a parent-only view.
show: [attention, upcoming, assessments, waiting, review, followups]
title: Work list # optional
config_entry_id: <id> # optional; only needed with several Canvas accounts
```

Each row has the status, class, assignment (opens Canvas), due date (red when
overdue), a stage dropdown and a note field (saved on Enter or when leaving
the field). Quizzes and tests offer study stages. Waiting items can be
closed without a grade; graded items show the score (🔺 when flagged) with
**Mark reviewed**, and in the parent review list **Parent reviewed** or
**Send back**. Follow-ups have stage, method and note. The card never marks a
Canvas assignment complete.

### Where the data is stored

Stages, notes, follow-ups, reviews and the grade log are saved in
`/config/.storage/canvas.workflow.<canvas user id>`. Home Assistant backups
include it, and restoring a backup restores it. The file is keyed by the
Canvas account, so removing and re-adding the integration finds the same data;
removing the integration does **not** delete it. To start over, remove the
integration, delete that file, and restart. **Download diagnostics** on the
integration shows the stored data. Going back to 0.2.x drops the reviews,
closes and grade log the next time it saves.

The to-do list itself is not stored: assignments are fetched from Canvas every
hour. Personal reminders that aren't Canvas assignments belong in a separate
list, such as a Local To-do list.

---

## Development & Testing

This project uses modern Python development tooling with `uv`, `pytest`, `ruff`, and `ty`.

### Environment Setup

```bash
# Bootstrap virtual environment and tools
./script/bootstrap

# Install dependencies and pre-commit hooks
./script/setup
```

### Running Tests & Quality Checks

```bash
# Run complete test suite with coverage
./script/test

# Run linters (ruff, ty check, codespell, yamllint, prettier)
./script/lint

# Start local Home Assistant development server
./script/server
```

---

## License

This project is licensed under the [Apache 2.0 License](LICENSE).
