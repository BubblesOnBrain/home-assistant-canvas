/*
 * canvas-workflow-card: the student's work list for the Canvas integration.
 *
 * Shows Canvas assignments with a stage dropdown and a note field, plus
 * teacher follow-ups. Edits call the canvas.set_assignment_stage and
 * canvas.set_followup_stage services; nothing here can mark a Canvas
 * assignment complete. Vanilla custom element, no dependencies.
 */

const DOMAIN = "canvas";
const NOTE_MAX = 500;
const SAVED_MS = 1500;
const SECTIONS = ["attention", "upcoming", "followups"];

const STAGES = [
  "not_started",
  "working",
  "done_not_submitted",
  "submitted_claimed",
];
const FOLLOWUP_STAGES = ["needs_contact", "contacted", "resolved"];
const FOLLOWUP_METHODS = ["email", "late_form", "in_person"];

// English fallbacks; translated labels come from the integration's strings.
const FALLBACK = {
  stage: {
    not_started: "Not started",
    working: "Working on it",
    done_not_submitted: "Done – needs submitting",
    submitted_claimed: "I submitted it",
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
  },
  display_state: {
    confirmed: "In Canvas",
    not_in_canvas: "Not in Canvas",
    claimed_pending: "Submitted?",
    paper_not_graded: "Paper not graded",
    paper_waiting: "Turned in",
    done_not_submitted: "Done, submit it",
    zeroed: "Zero",
    missing: "Missing",
    working: "Working on it",
    not_started: "Not started",
  },
};

// Chip tone per display state: critical, warning, info, ok or plain.
const TONE = {
  not_in_canvas: "critical",
  paper_not_graded: "critical",
  missing: "critical",
  zeroed: "critical",
  done_not_submitted: "warning",
  claimed_pending: "info",
  paper_waiting: "info",
  working: "info",
  confirmed: "ok",
  not_started: "plain",
};

const SECTION_TITLES = {
  attention: "Needs attention",
  upcoming: "Coming up",
  followups: "Teacher follow-ups",
};

function urgency(item) {
  const s = item.display_state;
  if (s === "not_in_canvas") return 0;
  if (s === "paper_not_graded") return 1;
  if (s === "done_not_submitted" && item.overdue) return 2;
  if (s === "missing") return 3;
  if (s === "zeroed") return 4;
  return 5;
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
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(
      (id) =>
        id.startsWith("todo.") &&
        hass.states[id].attributes.followup_items !== undefined,
    );
    return {
      entity: entity || "",
      show: ["attention", "upcoming", "followups"],
    };
  }

  setConfig(config) {
    if (!config || !config.entity || !config.entity.startsWith("todo.")) {
      throw new Error("Set 'entity' to a Canvas to-do list (todo.…)");
    }
    const show = config.show || ["attention", "upcoming", "followups"];
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
    const rows =
      (a.attention_items || []).length +
      (a.upcoming_items || []).length +
      (a.followup_items || []).length;
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
      for (const section of this._config.show) {
        if (section === "followups") {
          const items = a.followup_items || [];
          body.append(
            this._section(
              section,
              items.map((f) => this._followupRow(f)),
              "No follow-ups.",
            ),
          );
        } else {
          const key =
            section === "attention" ? "attention_items" : "upcoming_items";
          const items = [...(a[key] || [])].sort(byUrgencyThenDue);
          body.append(
            this._section(
              section,
              items.map((i) => this._assignmentRow(i)),
              section === "attention"
                ? "Nothing missing. Nice."
                : "Nothing due in the next two weeks.",
            ),
          );
        }
      }
    }
    this.shadowRoot.replaceChildren(h("style", { text: STYLES }), card);
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

  _assignmentRow(item) {
    const key = `a-${item.uid}`;
    // Assignment ids are shared by siblings in one class; say whose it is.
    const studentId = this._stateObj.attributes.student_id;
    const set = (data) =>
      this._call(
        "set_assignment_stage",
        {
          assignment_id: item.uid,
          ...(studentId !== undefined ? { student_id: studentId } : {}),
          ...data,
        },
        key,
      );
    const state = item.display_state || "not_started";
    const nudge = state === "done_not_submitted" && item.overdue;
    return h("li", { class: nudge ? "row nudge" : "row" }, [
      h("div", { class: "head" }, [
        h("span", {
          class: `chip ${TONE[state] || "plain"}`,
          text: this._label("display_state", state),
        }),
        item.paper ? h("span", { class: "paper", text: "📝 Paper" }) : null,
        h("span", { class: "class", text: item.class || item.course || "" }),
      ]),
      h("div", { class: "title" }, this._link(item.assignment, item.url)),
      this._due(item.due, item.overdue),
      h("div", { class: "controls" }, [
        this._select(
          `${key}-stage`,
          "stage",
          STAGES,
          item.stage,
          (ev) => set({ stage: ev.target.value }),
          null,
        ),
        this._note(`${key}-note`, item.note, (note) => set({ note })),
      ]),
      this._errors[key]
        ? h("p", { class: "error", role: "alert", text: this._errors[key] })
        : null,
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
  .class, .paper { font-size: 0.85rem; color: var(--secondary-text-color); }
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
      "Student work list: stage, note and teacher follow-ups for Canvas assignments.",
    documentationURL:
      "https://github.com/BubblesOnBrain/home-assistant-canvas#canvas-workflow-card",
  });
}
