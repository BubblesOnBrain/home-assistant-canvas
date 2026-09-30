/*
 * canvas-workflow-card: the student's work list for the Canvas integration.
 *
 * Shows Canvas work by where it stands (needs attention, coming up, quizzes
 * and tests, waiting on the teacher, grades to review, parent review,
 * closed) plus teacher follow-ups. Edits call the integration's services;
 * nothing here can mark a Canvas assignment complete. Vanilla custom
 * element, no dependencies.
 */

const DOMAIN = "canvas";
const NOTE_MAX = 500;
const SAVED_MS = 1500;
const SECTIONS = [
  "attention",
  "upcoming",
  "assessments",
  "waiting",
  "review",
  "parent_review",
  "closed",
  "followups",
];
// The student's view; the parent view adds parent_review and closed.
const DEFAULT_SHOW = [
  "attention",
  "upcoming",
  "assessments",
  "waiting",
  "review",
  "followups",
];
const ITEMS_KEY = {
  attention: "attention_items",
  upcoming: "upcoming_items",
  assessments: "assessment_items",
  waiting: "waiting_items",
  review: "review_items",
  parent_review: "parent_review_items",
  closed: "closed_items",
  followups: "followup_items",
};

const HOMEWORK_STAGES = [
  "not_started",
  "working",
  "done_not_submitted",
  "submitted_claimed",
];
const ASSESSMENT_STAGES = ["not_started", "studying", "ready", "needs_makeup"];
const KINDS = ["auto", "homework", "quiz", "test"];
const CLOSE_REASONS = ["feedback_received", "not_graded", "other"];
const FOLLOWUP_STAGES = ["needs_contact", "contacted", "resolved"];
const FOLLOWUP_METHODS = ["email", "late_form", "in_person"];

// English fallbacks; translated labels come from the integration's strings.
const FALLBACK = {
  stage: {
    not_started: "Not started",
    working: "Working on it",
    done_not_submitted: "Done – needs submitting",
    submitted_claimed: "I submitted it",
    studying: "Studying",
    ready: "Ready",
    needs_makeup: "Missed it – needs make-up",
  },
  assignment_kind: {
    auto: "Automatic",
    homework: "Homework",
    quiz: "Quiz",
    test: "Test",
  },
  close_reason: {
    feedback_received: "Teacher feedback received",
    not_graded: "Not graded",
    other: "Other",
  },
  followup_stage: {
    needs_contact: "Needs contact",
    contacted: "Contacted",
    resolved: "Resolved",
  },
  followup_method: {
    email: "Email",
    late_form: "Late-work form",
    in_person: "In person",
  },
  followup_reason: {
    late_work: "Late work",
    not_in_canvas: "Not in Canvas",
    paper_not_graded: "Paper not graded",
    makeup: "Make-up needed",
  },
  display_state: {
    confirmed: "In Canvas",
    needs_makeup: "Make-up needed",
    not_in_canvas: "Not in Canvas",
    claimed_pending: "Submitted?",
    paper_not_graded: "Paper not graded",
    paper_waiting: "Turned in",
    done_not_submitted: "Done, submit it",
    zeroed: "Zero",
    missing: "Missing",
    working: "Working on it",
    awaiting_grade: "Awaiting grade",
    studying: "Studying",
    ready: "Ready",
    not_started: "Not started",
  },
};

// Chip tone per display state: critical, warning, info, ok or plain.
const TONE = {
  not_in_canvas: "critical",
  paper_not_graded: "critical",
  missing: "critical",
  zeroed: "critical",
  needs_makeup: "critical",
  done_not_submitted: "warning",
  awaiting_grade: "info",
  studying: "info",
  ready: "ok",
  claimed_pending: "info",
  paper_waiting: "info",
  working: "info",
  confirmed: "ok",
  not_started: "plain",
};

const SECTION_TITLES = {
  attention: "Needs attention",
  upcoming: "Coming up",
  assessments: "Quizzes & tests",
  waiting: "Waiting on teacher",
  review: "New grades to review",
  parent_review: "Parent review",
  closed: "Closed without a grade",
  followups: "Teacher follow-ups",
};

const EMPTY_TEXT = {
  attention: "Nothing missing. Nice.",
  upcoming: "Nothing due in the next two weeks.",
  assessments: "No quizzes or tests coming up.",
  waiting: "Nothing waiting on a teacher.",
  review: "No new grades.",
  parent_review: "Nothing to review.",
  closed: "Nothing closed.",
  followups: "No follow-ups.",
};

// Lists the backend already sorts newest first; the rest sort here.
const PRESORTED = new Set(["review", "parent_review", "closed", "followups"]);

function urgency(item) {
  const s = item.display_state;
  if (s === "not_in_canvas") return 0;
  if (s === "paper_not_graded") return 1;
  if (s === "done_not_submitted" && item.overdue) return 2;
  if (s === "missing") return 3;
  if (s === "zeroed") return 4;
  if (s === "needs_makeup") return 5;
  return 6;
}

function byUrgencyThenDue(a, b) {
  return (
    urgency(a) - urgency(b) || (a.due || "9999").localeCompare(b.due || "9999")
  );
}

function h(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) {
      el.append(child);
    }
  }
  return el;
}

function localDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

class CanvasWorkflowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._stateObj = undefined;
    this._pendingRender = false;
    this._errors = {};
    this._saved = {};
    this._translationsRequested = false;
    // Close-without-grade forms that are open, with what's been entered.
    this._closing = {};
    this._moreOpen = new Set();
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(
      (id) =>
        id.startsWith("todo.") &&
        hass.states[id].attributes.followup_items !== undefined,
    );
    return { entity: entity || "", show: DEFAULT_SHOW };
  }

  setConfig(config) {
    if (!config || !config.entity || !config.entity.startsWith("todo.")) {
      throw new Error("Set 'entity' to a Canvas to-do list (todo.…)");
    }
    const show = config.show || DEFAULT_SHOW;
    const unknown = show.filter((s) => !SECTIONS.includes(s));
    if (unknown.length) {
      throw new Error(`Unknown section(s) in 'show': ${unknown.join(", ")}`);
    }
    this._config = { ...config, show };
    this._stateObj = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._translationsRequested && hass.loadBackendTranslation) {
      this._translationsRequested = true;
      hass
        .loadBackendTranslation("selector", DOMAIN)
        .then(() => this._render())
        .catch(() => {});
    }
    const stateObj = this._config && hass.states[this._config.entity];
    if (stateObj !== this._stateObj) {
      this._stateObj = stateObj;
      this._render();
    }
  }

  getCardSize() {
    const a = (this._stateObj && this._stateObj.attributes) || {};
    const show = (this._config && this._config.show) || DEFAULT_SHOW;
    const rows = show.reduce(
      (n, section) => n + (a[ITEMS_KEY[section]] || []).length,
      0,
    );
    return 1 + Math.ceil(rows * 1.5);
  }

  _label(kind, value) {
    const key = `component.${DOMAIN}.selector.${kind}.options.${value}`;
    const translated = this._hass && this._hass.localize(key);
    return translated || (FALLBACK[kind] && FALLBACK[kind][value]) || value;
  }

  _render() {
    if (!this._config || !this._hass) return;
    // Don't rebuild under the student's fingers (typing a note or with a
    // dropdown open); finish when she leaves the field.
    const active = this.shadowRoot.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "SELECT")) {
      this._pendingRender = true;
      return;
    }
    this._pendingRender = false;
    const card = h("ha-card", { header: this._config.title || undefined });
    const body = h("div", { class: "body" });
    card.append(body);

    const stateObj = this._stateObj;
    if (!stateObj) {
      body.append(
        h("p", {
          class: "empty",
          text: `Entity ${this._config.entity} not found.`,
        }),
      );
    } else {
      const a = stateObj.attributes;
      // Drop drafts for work that has left the waiting list (e.g. graded).
      const waiting = new Set((a.waiting_items || []).map((i) => i.uid));
      for (const uid of Object.keys(this._closing)) {
        if (!waiting.has(uid)) delete this._closing[uid];
      }
      for (const section of this._config.show) {
        const items = [...(a[ITEMS_KEY[section]] || [])];
        if (!PRESORTED.has(section)) items.sort(byUrgencyThenDue);
        body.append(
          this._section(
            section,
            items.map((i) => this._row(section, i)),
            EMPTY_TEXT[section],
          ),
        );
      }
    }
    this.shadowRoot.replaceChildren(h("style", { text: STYLES }), card);
  }

  _row(section, item) {
    switch (section) {
      case "followups":
        return this._followupRow(item);
      case "waiting":
        return this._waitingRow(item);
      case "review":
      case "parent_review":
        return this._reviewRow(item, section === "parent_review");
      case "closed":
        return this._closedRow(item);
      default:
        return this._assignmentRow(item);
    }
  }

  _section(name, rows, emptyText) {
    return h("section", {}, [
      h("h3", { text: SECTION_TITLES[name] }),
      rows.length
        ? h("ul", { class: "rows" }, rows)
        : h("p", { class: "empty", text: emptyText }),
    ]);
  }

  _due(iso, overdue) {
    if (!iso) return h("span", { class: "due", text: "No due date" });
    const due = new Date(iso);
    const days = Math.round((localDay(due) - localDay(new Date())) / 86400000);
    const lang = (this._hass.locale && this._hass.locale.language) || "en";
    let text;
    try {
      text = new Intl.RelativeTimeFormat(lang, { numeric: "auto" }).format(
        days,
        "day",
      );
    } catch (err) {
      text = due.toLocaleDateString();
    }
    if (Math.abs(days) <= 1 && iso.includes("T")) {
      text += ` ${due.toLocaleTimeString(lang, {
        hour: "numeric",
        minute: "2-digit",
      })}`;
    }
    return h("span", {
      class: overdue ? "due overdue" : "due",
      title: due.toLocaleString(lang),
      text: overdue ? `Overdue · ${text}` : `Due ${text}`,
    });
  }

  _link(text, url) {
    return url
      ? h("a", { href: url, target: "_blank", rel: "noopener", text })
      : h("span", { text });
  }

  _select(id, kind, options, value, onChange, placeholder) {
    const select = h(
      "select",
      {
        id,
        "aria-label": placeholder || kind,
        onchange: onChange,
        onblur: () => this._flushRender(),
      },
      [
        placeholder ? h("option", { value: "", text: placeholder }) : null,
        ...options.map((o) =>
          h("option", { value: o, text: this._label(kind, o) }),
        ),
      ],
    );
    select.value = value || "";
    return select;
  }

  _flushRender() {
    if (this._pendingRender) this._render();
  }

  _note(id, value, save) {
    // Kept per field, not on the element: saving usually re-renders the row
    // before the service call returns.
    const recent = Date.now() - (this._saved[id] || 0) < SAVED_MS;
    const saved = h("span", {
      class: "saved",
      "aria-hidden": "true",
      text: recent ? "✓" : "",
    });
    const input = h("input", {
      id,
      type: "text",
      maxlength: String(NOTE_MAX),
      placeholder: "Add a note",
      "aria-label": "Note",
      onkeydown: (ev) => {
        if (ev.key === "Enter") input.blur();
      },
      onblur: async () => {
        const next = input.value.trim().slice(0, NOTE_MAX);
        if (next !== (value || "") && (await save(next))) {
          this._saved[id] = Date.now();
          saved.textContent = "✓";
          this._pendingRender = true; // show the tick on the rebuilt row
          setTimeout(() => {
            this._pendingRender = true;
            this._flushRender();
          }, SAVED_MS + 50);
        }
        this._flushRender();
      },
    });
    input.value = value || "";
    return h("div", { class: "note" }, [input, saved]);
  }

  async _call(service, data, errorKey) {
    const payload = { ...data };
    if (this._config.config_entry_id) {
      payload.config_entry_id = this._config.config_entry_id;
    }
    try {
      await this._hass.callService(DOMAIN, service, payload);
      delete this._errors[errorKey];
      return true;
    } catch (err) {
      this._errors[errorKey] = (err && err.message) || "Couldn't save.";
      this._render();
      return false;
    }
  }

  _setter(item, key, service = "set_assignment_stage") {
    // Assignment ids are shared by siblings in one class; say whose it is.
    const studentId = this._stateObj.attributes.student_id;
    return (data) =>
      this._call(
        service,
        {
          assignment_id: item.uid,
          ...(studentId !== undefined ? { student_id: studentId } : {}),
          ...data,
        },
        key,
      );
  }

  _head(item, chip, tone) {
    const kind = item.kind && item.kind !== "homework" ? item.kind : null;
    return h("div", { class: "head" }, [
      h("span", { class: `chip ${tone || "plain"}`, text: chip }),
      kind
        ? h("span", {
            class: "kind",
            text: `📚 ${this._label("assignment_kind", kind)}`,
          })
        : null,
      item.paper ? h("span", { class: "paper", text: "📝 Paper" }) : null,
      h("span", { class: "class", text: item.class || item.course || "" }),
    ]);
  }

  _error(key) {
    return this._errors[key]
      ? h("p", { class: "error", role: "alert", text: this._errors[key] })
      : null;
  }

  _stageSelect(item, key, set) {
    const stages =
      item.kind && item.kind !== "homework"
        ? ASSESSMENT_STAGES
        : HOMEWORK_STAGES;
    // Keep a stage from the other set visible until she changes it.
    const options = stages.includes(item.stage)
      ? stages
      : [...stages, item.stage];
    return this._select(
      `${key}-stage`,
      "stage",
      options,
      item.stage,
      (ev) => set({ stage: ev.target.value }),
      null,
    );
  }

  _kindSelect(item, key, set) {
    return h("label", { class: "kind-pick" }, [
      h("span", { text: "Kind" }),
      this._select(
        `${key}-kind`,
        "assignment_kind",
        KINDS,
        item.kind_override ? item.kind : "auto",
        (ev) => set({ kind: ev.target.value }),
        null,
      ),
    ]);
  }

  _assignmentRow(item) {
    const key = `a-${item.uid}`;
    const set = this._setter(item, key);
    const state = item.display_state || "not_started";
    const nudge = state === "done_not_submitted" && item.overdue;
    return h("li", { class: nudge ? "row nudge" : "row" }, [
      this._head(item, this._label("display_state", state), TONE[state]),
      h("div", { class: "title" }, this._link(item.assignment, item.url)),
      this._due(item.due, item.overdue),
      h("div", { class: "controls" }, [
        this._stageSelect(item, key, set),
        this._note(`${key}-note`, item.note, (note) => set({ note })),
      ]),
      h(
        "details",
        {
          class: "more",
          // Kept on the card so a state update doesn't fold it shut.
          open: this._moreOpen.has(item.uid),
          ontoggle: (ev) => {
            if (ev.target.open) this._moreOpen.add(item.uid);
            else this._moreOpen.delete(item.uid);
          },
        },
        [h("summary", { text: "More" }), this._kindSelect(item, key, set)],
      ),
      this._error(key),
    ]);
  }

  _waitingRow(item) {
    const key = `a-${item.uid}`;
    const set = this._setter(item, key);
    const state = item.display_state || "confirmed";
    const chip =
      state === "confirmed" ? "Turned in" : this._label("display_state", state);
    return h("li", { class: "row" }, [
      this._head(item, chip, state === "confirmed" ? "info" : TONE[state]),
      h("div", { class: "title" }, this._link(item.assignment, item.url)),
      this._due(item.due, false),
      h("div", { class: "controls" }, [
        this._stageSelect(item, key, set),
        this._note(`${key}-note`, item.note, (note) => set({ note })),
      ]),
      this._closeForm(item, key),
      this._error(key),
    ]);
  }

  _closeForm(item, key) {
    const close = this._setter(item, key, "close_assignment");
    const draft = this._closing[item.uid];
    if (!draft) {
      return h("div", { class: "actions" }, [
        h("button", {
          class: "secondary",
          text: "Close without grade…",
          onclick: () => {
            this._closing[item.uid] = { reason: "feedback_received", note: "" };
            this._render();
          },
        }),
      ]);
    }
    // The draft lives on the card, so a re-render keeps what she typed. A
    // held-back re-render waits while focus moves to a button in this form,
    // or clicking Close would miss the button it rebuilt.
    const flushUnlessForm = (ev) => {
      const next = ev.relatedTarget;
      if (!next || !next.closest || next.closest(".close-form") === null) {
        this._flushRender();
      }
    };
    const reason = h(
      "select",
      {
        id: `${key}-reason`,
        "aria-label": "Why",
        onchange: (ev) => {
          draft.reason = ev.target.value;
        },
        onblur: flushUnlessForm,
      },
      CLOSE_REASONS.map((r) =>
        h("option", { value: r, text: this._label("close_reason", r) }),
      ),
    );
    reason.value = draft.reason;
    const note = h("input", {
      id: `${key}-close-note`,
      type: "text",
      maxlength: String(NOTE_MAX),
      placeholder: "What happened?",
      "aria-label": "Close note",
      oninput: (ev) => {
        draft.note = ev.target.value;
      },
      onblur: flushUnlessForm,
    });
    note.value = draft.note;
    return h("div", { class: "close-form" }, [
      h("div", { class: "controls" }, [reason, note]),
      h("div", { class: "actions" }, [
        h("button", {
          text: "Close it",
          onclick: async () => {
            const ok = await close({
              reason: draft.reason,
              note: draft.note.trim().slice(0, NOTE_MAX),
            });
            if (ok) delete this._closing[item.uid];
            this._render();
          },
        }),
        h("button", {
          class: "secondary",
          text: "Cancel",
          onclick: () => {
            delete this._closing[item.uid];
            this._render();
          },
        }),
      ]),
    ]);
  }

  _score(item) {
    let text;
    if (item.score !== null && item.score !== undefined) {
      text = item.points_possible
        ? `${item.score}/${item.points_possible}`
        : `${item.score}`;
      if (item.percent !== null && item.percent !== undefined) {
        text += ` · ${item.percent}%`;
      }
    } else {
      text = item.grade || "Graded";
    }
    return item.escalated ? `🔺 ${text}` : text;
  }

  _when(prefix, iso, by) {
    if (!iso) return null;
    const lang = (this._hass.locale && this._hass.locale.language) || "en";
    const day = new Date(iso).toLocaleDateString(lang, {
      month: "short",
      day: "numeric",
    });
    return h("span", {
      class: "due",
      text: by ? `${prefix} by ${by} · ${day}` : `${prefix} ${day}`,
    });
  }

  _reviewRow(item, parent) {
    const key = `r-${item.uid}`;
    const review = this._setter(item, key, "review_assignment");
    return h("li", { class: item.escalated ? "row nudge-bad" : "row" }, [
      this._head(item, this._score(item), item.escalated ? "critical" : "ok"),
      h("div", { class: "title" }, this._link(item.assignment, item.url)),
      this._when("Graded", item.graded_at),
      parent
        ? this._when(
            "Reviewed",
            item.student_reviewed_at,
            item.student_reviewed_by,
          )
        : null,
      h("div", { class: "actions" }, [
        h("button", {
          text: parent ? "Parent reviewed" : "Mark reviewed",
          onclick: () =>
            review({ role: parent ? "parent" : "student", undo: false }),
        }),
        parent
          ? h("button", {
              class: "secondary",
              text: "Send back",
              title: "Undo the student's review",
              onclick: () => review({ role: "student", undo: true }),
            })
          : null,
      ]),
      this._error(key),
    ]);
  }

  _closedRow(item) {
    const key = `c-${item.uid}`;
    const close = this._setter(item, key, "close_assignment");
    return h("li", { class: "row done" }, [
      this._head(
        item,
        this._label("close_reason", item.closed_reason || "other"),
        "plain",
      ),
      h("div", { class: "title" }, this._link(item.assignment, item.url)),
      this._when("Closed", item.closed_at, item.closed_by),
      item.closed_note
        ? h("p", { class: "closed-note", text: item.closed_note })
        : null,
      h("div", { class: "actions" }, [
        h("button", {
          class: "secondary",
          text: "Reopen",
          onclick: () => close({ undo: true }),
        }),
      ]),
      this._error(key),
    ]);
  }

  _followupRow(f) {
    const key = `f-${f.id}`;
    const set = (data) =>
      this._call("set_followup_stage", { followup_id: f.id, ...data }, key);
    return h("li", { class: f.stage === "resolved" ? "row done" : "row" }, [
      h("div", { class: "head" }, [
        h("span", {
          class: `chip ${
            f.stage === "resolved" ? "ok" : f.overdue ? "critical" : "warning"
          }`,
          text: this._label("followup_reason", f.reason),
        }),
        h("span", { class: "class", text: f.class || "" }),
        f.in_canvas
          ? null
          : h("span", { class: "gone", text: "No longer in Canvas" }),
      ]),
      h("div", { class: "title" }, this._link(f.assignment, f.url)),
      f.stage === "needs_contact"
        ? h("span", {
            class: f.overdue ? "due overdue" : "due",
            text: `Contact by ${new Date(
              `${f.contact_by}T12:00:00`,
            ).toLocaleDateString()}`,
          })
        : null,
      h("div", { class: "controls three" }, [
        this._select(
          `${key}-stage`,
          "followup_stage",
          FOLLOWUP_STAGES,
          f.stage,
          (ev) => set({ stage: ev.target.value }),
          null,
        ),
        this._select(
          `${key}-method`,
          "followup_method",
          FOLLOWUP_METHODS,
          f.method,
          // Picking "How?" sends "" and clears the method.
          (ev) => set({ method: ev.target.value }),
          "How?",
        ),
        this._note(`${key}-note`, f.note, (note) => set({ note })),
      ]),
      this._errors[key]
        ? h("p", { class: "error", role: "alert", text: this._errors[key] })
        : null,
    ]);
  }
}

const STYLES = `
  :host { display: block; }
  .body { padding: 0 16px 16px; display: grid; gap: 16px; }
  ha-card:not([header]) .body { padding-top: 16px; }
  h3 {
    margin: 0 0 8px; font-size: 0.8rem; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--secondary-text-color);
  }
  .rows { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .row {
    display: grid; gap: 6px; padding: 10px 12px; border-radius: 12px;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color, var(--ha-card-background));
  }
  .row.nudge { border-color: var(--warning-color); border-width: 2px; }
  .row.nudge-bad { border-color: var(--error-color); border-width: 2px; }
  .row.done { opacity: 0.7; }
  .head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
  .chip {
    font-size: 0.75rem; font-weight: 600; padding: 2px 10px;
    border-radius: 999px; border: 1px solid currentColor;
    color: var(--secondary-text-color);
  }
  .chip.critical { color: var(--error-color); }
  .chip.warning { color: var(--warning-color); }
  .chip.info { color: var(--info-color, var(--primary-color)); }
  .chip.ok { color: var(--success-color); }
  .class, .paper, .kind {
    font-size: 0.85rem; color: var(--secondary-text-color);
  }
  .gone { font-size: 0.75rem; color: var(--warning-color); }
  .title { font-size: 1rem; font-weight: 500; overflow-wrap: anywhere; }
  .title a { color: var(--primary-text-color); text-decoration: none; }
  .title a:hover, .title a:focus-visible { text-decoration: underline; }
  .due { font-size: 0.85rem; color: var(--secondary-text-color); }
  .due.overdue { color: var(--error-color); font-weight: 600; }
  .controls { display: grid; gap: 8px; grid-template-columns: 1fr; }
  select, input {
    min-height: 44px; box-sizing: border-box; width: 100%;
    padding: 0 10px; border-radius: 8px; font: inherit;
    color: var(--primary-text-color);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color);
  }
  select:focus-visible, input:focus-visible {
    outline: 2px solid var(--primary-color); outline-offset: 1px;
  }
  .note { position: relative; }
  .note input { padding-right: 32px; }
  .saved {
    position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
    color: var(--success-color); font-weight: 600;
  }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  button {
    min-height: 44px; padding: 0 16px; border-radius: 8px; font: inherit;
    font-weight: 600; cursor: pointer; border: 1px solid var(--primary-color);
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  button.secondary {
    background: transparent; color: var(--primary-color);
  }
  button:focus-visible {
    outline: 2px solid var(--primary-color); outline-offset: 2px;
  }
  .close-form { display: grid; gap: 8px; }
  .closed-note { margin: 0; font-size: 0.9rem; }
  .more summary {
    cursor: pointer; font-size: 0.85rem; color: var(--secondary-text-color);
  }
  .kind-pick {
    display: grid; grid-template-columns: auto minmax(0, 12rem);
    gap: 8px; align-items: center; margin-top: 8px; font-size: 0.85rem;
  }
  .empty { margin: 0; color: var(--secondary-text-color); }
  .error { margin: 0; color: var(--error-color); font-size: 0.85rem; }
  @media (min-width: 600px) {
    .controls { grid-template-columns: minmax(0, 14rem) minmax(0, 1fr); }
    .controls.three {
      grid-template-columns: minmax(0, 11rem) minmax(0, 10rem) minmax(0, 1fr);
    }
  }
`;

if (!customElements.get("canvas-workflow-card")) {
  customElements.define("canvas-workflow-card", CanvasWorkflowCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "canvas-workflow-card")) {
  window.customCards.push({
    type: "canvas-workflow-card",
    name: "Canvas workflow",
    description:
      "Student work list: stages, notes, grade review and teacher follow-ups for Canvas work.",
    documentationURL:
      "https://github.com/BubblesOnBrain/home-assistant-canvas#canvas-workflow-card",
  });
}
