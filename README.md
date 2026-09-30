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

Marking **I submitted it** records the time. If Canvas still doesn't show the
work after the grace period (12 hours for online work, 7 days for paper), the
item shows **⚠️ NOT IN CANVAS** or **⚠️ PAPER NOT GRADED** and a follow-up
opens. A stage never checks an assignment off; only Canvas does.

The label shown on each item (`display_state`) is decided in this order, first
match wins: in Canvas (`confirmed`) → not in Canvas / submitted? / paper not
graded / turned in (her claim, by grace period) → done, submit it → zero →
missing → working / not started.

### Teacher follow-ups

| Reason             | Opens when                                                | Closes when                        |
| :----------------- | :-------------------------------------------------------- | :--------------------------------- |
| `late_work`        | Canvas shows a late submission                            | Only when the student resolves it  |
| `not_in_canvas`    | Online work marked submitted, grace passed, not in Canvas | Automatically when Canvas shows it |
| `paper_not_graded` | Paper work marked turned in, grace passed, not graded     | Automatically when Canvas shows it |

Each follow-up appears as its own task on the to-do list (for example
**✉️ EMAIL TEACHER · [Biology] Cell Lab Report**), due the next day. Ticking the
task sets it to **contacted**; the card can also set **resolved**, how the
teacher was contacted (`email`, `late_form`, `in_person`) and a note. If Canvas
closes a follow-up, anything she wrote on it is kept. Resolved
follow-ups stay visible for 7 days. If Canvas stops returning the assignment,
the task stays and is marked **No longer in Canvas**.

### Options

**Settings → Devices & Services → Canvas → Configure**:

| Option               | Default | Range |
| :------------------- | :------ | :---- |
| `online_grace_hours` | 12      | 1–72  |
| `paper_grace_days`   | 7       | 1–21  |

### Services

Both work for non-admin users. `config_entry_id` is optional when only one
Canvas account is set up.

```yaml
action: canvas.set_assignment_stage
data:
  assignment_id: "5506356" # the to-do item's uid
  student_id: 6021 # optional; needed only if two students share the assignment
  stage: submitted_claimed # optional
  note: Uploaded the PDF # optional; "" clears it; max 500 characters
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
  `followup_overdue_count`, `undated_count`.
- Lists (not recorded to history): `attention_items`, `upcoming_items`,
  `late_items`, `followup_items`. Assignment items include `stage`, `note`,
  `display_state`, `overdue` and `claimed_submitted_at`.

### canvas-workflow-card

The integration serves and loads this card itself; there is no separate
resource to add. After updating, reload the browser once.

```yaml
type: custom:canvas-workflow-card
entity: todo.sydney_kohl_assignments
show: [attention, upcoming, followups] # any of these, in this order
title: Work list # optional
config_entry_id: <id> # optional; only needed with several Canvas accounts
```

Each row has the status, class, assignment (opens Canvas), due date (red when
overdue), a stage dropdown and a note field (saved on Enter or when leaving
the field). Follow-ups have stage, method and note. The card never marks a
Canvas assignment complete.

### Where the data is stored

Stages, notes and follow-ups are saved in
`/config/.storage/canvas.workflow.<canvas user id>`. Home Assistant backups
include it, and restoring a backup restores it. The file is keyed by the
Canvas account, so removing and re-adding the integration finds the same data;
removing the integration does **not** delete it. To start over, remove the
integration, delete that file, and restart. **Download diagnostics** on the
integration shows the stored data.

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
