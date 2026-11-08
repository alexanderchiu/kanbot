import { ALL_COLUMNS, CANCELLED, CONFIRM_MS } from "./constants.js";
import { $, appendLinked, board, dialog, el, filter, form, ideasOnly, urlsIn, linkNode } from "./dom.js";
import { api } from "./api.js";
import { findFocusTarget, focusTarget } from "./board.js";
import { timestamp } from "./format.js";
import { refresh } from "./refresh.js";
import { ticketById, state } from "./state.js";
import { showToast } from "./toast.js";

let editing = null; // The ticket open in the panel; null when creating one.
let initialValues = null;
let warnedUnsaved = false;
let pendingDependencies = [];
let addedDependencies = new Set(); // What the user changed, applied to the fresh list on save.
let removedDependencies = new Set();
let editingGone = false;
let opener = null; // Where focus returns when the panel closes.

let confirmTimer;

function renderDescriptionLinks() {
  const urls = [...new Set(urlsIn(form.elements.description.value))];
  $("description-links").replaceChildren(...urls.map(linkNode));
}

function formValues() {
  return {
    title: form.elements.title.value,
    description: form.elements.description.value,
    status: form.elements.status.value,
    priority: form.elements.priority.value,
    project: form.elements.project.value.trim().toLowerCase() || null,
    idea: form.elements.idea.checked,
    assignee: form.elements.assignee.value.trim().toLowerCase() || null,
    depends_on: [...pendingDependencies].sort((a, b) => a - b),
  };
}

function dependencyRow(id, onRemove) {
  const row = el("div", "dependency");
  const other = ticketById(id);
  const open = el("button", "dependency-link");
  open.type = "button";
  open.dataset.status = other?.status ?? "";
  open.title = other ? `Open #${id}` : "";
  open.append(el("span", "", other ? `#${id} ${other.title}` : `#${id}`));
  open.addEventListener("click", () => {
    if (!other) return;
    if (isDirty()) {
      showError("Save or discard your changes before opening another ticket.");
      return;
    }
    dialog.close();
    openPanel(other);
  });
  row.append(open);
  if (onRemove) {
    const remove = el("button", "icon-button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove dependency on #${id}`);
    remove.addEventListener("click", onRemove);
    row.append(remove);
  }
  return row;
}

function renderDependencies() {
  $("depends-on").replaceChildren(
    ...pendingDependencies.map((id) =>
      dependencyRow(id, () => {
        pendingDependencies = pendingDependencies.filter((other) => other !== id);
        if (!addedDependencies.delete(id)) removedDependencies.add(id);
        renderDependencies();
      })
    )
  );
  const blocks = editing?.blocks ?? [];
  $("blocks-section").hidden = !blocks.length;
  $("blocks").replaceChildren(...blocks.map((id) => dependencyRow(id)));
}

// A leading number wins; otherwise an exact title, then a unique partial title.
function findDependency(text) {
  const number = text.match(/^#?(\d+)/)?.[1];
  if (number) return ticketById(Number(number));
  const query = text.toLowerCase();
  const exact = state.tickets.find((t) => t.title.toLowerCase() === query);
  if (exact) return exact;
  const partial = state.tickets.filter((t) => t.title.toLowerCase().includes(query));
  return partial.length === 1 ? partial[0] : undefined;
}

function addDependency(announce = true) {
  const input = $("add-dependency");
  if (!input.value.trim()) return;
  const id = findDependency(input.value.trim())?.id;
  if (!id) {
    if (announce) showError("Choose an existing ticket by number, or by a title that matches only one.");
    return;
  }
  if (id === editing?.id) {
    showError("A ticket can't depend on itself.");
    return;
  }
  $("error").hidden = true;
  if (!pendingDependencies.includes(id)) {
    pendingDependencies.push(id);
    if (!removedDependencies.delete(id)) addedDependencies.add(id);
  }
  input.value = "";
  renderDependencies();
}

function isDirty() {
  return (
    JSON.stringify(formValues()) !== JSON.stringify(initialValues) ||
    $("note-input").value.trim() !== ""
  );
}

function renderStamps(ticket) {
  const rows = [["Created", `${timestamp(ticket.created_at)}${ticket.created_by ? ` by ${ticket.created_by}` : ""}`]];
  if (ticket.completed_at) rows.push(["Completed", timestamp(ticket.completed_at)]);
  rows.push(["Updated", timestamp(ticket.updated_at)]);
  $("stamps").replaceChildren(
    ...rows.map(([label, value]) => {
      const row = el("div", "stamp");
      row.append(el("span", "", label), el("span", "", value));
      return row;
    })
  );
}

export function openPanel(ticket, status = "follow_up") {
  const trigger = focusTarget(document.activeElement);
  opener = ticket ? { selector: `.card[data-id="${ticket.id}"]`, index: 0, status: ticket.status } : { ...trigger, status };
  editing = ticket;
  editingGone = false;
  addedDependencies = new Set();
  removedDependencies = new Set();
  warnedUnsaved = false;
  $("dialog-title").textContent = ticket ? `#${ticket.id}` : "New ticket";
  dialog.setAttribute("aria-label", ticket ? `Ticket #${ticket.id}: ${ticket.title}` : "New ticket");
  form.elements.title.value = ticket?.title ?? "";
  form.elements.description.value = ticket?.description ?? "";
  renderDescriptionLinks();
  form.elements.status.value = ticket?.status ?? status;
  form.elements.priority.value = ticket?.priority ?? "medium";
  form.elements.project.value = ticket ? ticket.project ?? "" : filter.value;
  form.elements.assignee.value = ticket?.assignee ?? "";
  form.elements.idea.checked = ticket ? ticket.idea : ideasOnly.checked;
  $("note-input").value = "";
  $("add-dependency").value = "";
  pendingDependencies = [...(ticket?.depends_on ?? [])];
  renderDependencies();
  initialValues = formValues();

  disarmDelete();
  $("delete").hidden = !ticket;
  $("cancel-ticket").hidden = !ticket || ticket.status === CANCELLED.status;
  $("error").hidden = true;
  $("stamps").hidden = !ticket;
  if (ticket) renderStamps(ticket);
  $("activity").hidden = !ticket;
  $("notes").replaceChildren();
  $("history").replaceChildren();
  if (ticket) loadActivity(ticket.id);
  dialog.showModal();
  form.elements.title.focus();
}

// Fold a newer copy of the open ticket into the panel: untouched fields follow it, edited ones stay.
export function mergeOpenTicket() {
  if (!editing || !dialog.open) return;
  const fresh = ticketById(editing.id);
  if (!fresh) {
    editingGone = true;
    showError("This ticket was deleted. Close the panel to discard your changes.");
    return;
  }
  // updated_at only has second resolution, so compare the whole ticket.
  if (JSON.stringify(fresh) === JSON.stringify(editing)) return;
  const current = formValues();
  const setters = {
    title: (v) => { form.elements.title.value = v; },
    description: (v) => { form.elements.description.value = v; renderDescriptionLinks(); },
    status: (v) => { form.elements.status.value = v; },
    priority: (v) => { form.elements.priority.value = v; },
    project: (v) => { form.elements.project.value = v ?? ""; },
    idea: (v) => { form.elements.idea.checked = v; },
    assignee: (v) => { form.elements.assignee.value = v ?? ""; },
  };
  for (const [key, set] of Object.entries(setters)) {
    if (JSON.stringify(current[key]) !== JSON.stringify(initialValues[key])) continue;
    set(fresh[key]);
    initialValues[key] = formValues()[key];
  }
  for (const id of addedDependencies) if (fresh.depends_on.includes(id)) addedDependencies.delete(id);
  for (const id of removedDependencies) if (!fresh.depends_on.includes(id)) removedDependencies.delete(id);
  pendingDependencies = [
    ...fresh.depends_on.filter((id) => !removedDependencies.has(id)),
    ...addedDependencies,
  ];
  initialValues.depends_on = [...fresh.depends_on].sort((a, b) => a - b);
  editing = fresh;
  $("cancel-ticket").hidden = fresh.status === CANCELLED.status;
  renderDependencies();
  renderStamps(fresh);
  loadActivity(fresh.id);
}

// Closing with edits in progress takes a second attempt, so a stray click can't lose them.
function requestClose() {
  if (isDirty() && !warnedUnsaved) {
    warnedUnsaved = true;
    showError("You have unsaved changes. Close again to discard them.");
    return;
  }
  dialog.close();
}

function restoreOpenerFocus() {
  if (!opener || dialog.open) return;
  const target = (opener.selector && findFocusTarget(opener)) || board.querySelector(`.column[data-status="${opener.status}"] .add`);
  target?.focus({ preventScroll: true });
}

dialog.addEventListener("close", restoreOpenerFocus);

async function loadActivity(id) {
  try {
    const detail = await api("GET", `/api/tickets/${id}`);
    if (editing?.id !== id) return;
    $("notes").replaceChildren(...detail.notes.map(renderNote));
    $("history").replaceChildren(...detail.history.map(renderHistoryEntry));
  } catch (error) {
    showError(error.message);
  }
}

function describeChange({ field, old_value, new_value }) {
  if (field === "created") return "Created the ticket";
  if (field === "description") return "Changed the description";
  const label = (value) => ALL_COLUMNS.find((c) => c.status === value)?.label ?? value ?? "none";
  const name = field === "depends_on" ? "dependencies" : field;
  return `Changed ${name}: ${label(old_value)} → ${label(new_value)}`;
}

function renderHistoryEntry(entry) {
  const node = el("li");
  const by = entry.actor ? `${entry.actor} · ` : "";
  node.append(el("span", "history-change", describeChange(entry)), el("span", "note-meta", `${by}${timestamp(entry.at)}`));
  return node;
}

function renderNote(note) {
  const node = el("div", "note");
  const by = note.author ? `${note.author} · ` : "";
  const body = el("div", "note-body");
  appendLinked(body, note.body);
  node.append(el("div", "note-meta", `${by}${timestamp(note.created_at)}`), body);
  return node;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function disarmDelete() {
  clearTimeout(confirmTimer);
  $("delete").textContent = "Delete";
  $("delete").classList.remove("armed");
}

async function addNote() {
  const body = $("note-input").value.trim();
  if (!body) return;
  try {
    await api("POST", `/api/tickets/${editing.id}/notes`, { body, author: "user" });
    $("note-input").value = "";
    $("error").hidden = true;
    warnedUnsaved = false;
    await loadActivity(editing.id);
    await refresh(true);
  } catch (error) {
    showError(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = formValues();
  try {
    if (editing) {
      if (editingGone) {
        showError("This ticket was deleted. Close the panel to discard your changes.");
        return;
      }
      // Send only what changed, so saving can't undo an agent's concurrent edit.
      const changes = Object.fromEntries(
        Object.entries(values).filter(
          ([key, value]) => key !== "depends_on" && JSON.stringify(value) !== JSON.stringify(editing[key])
        )
      );
      if (addedDependencies.size || removedDependencies.size) changes.depends_on = values.depends_on;
      if (Object.keys(changes).length) {
        await api("PATCH", `/api/tickets/${editing.id}`, { ...changes, actor: "user" });
      }
      if (changes.status === CANCELLED.status) showToast(`Ticket #${editing.id} cancelled`);
    } else {
      await api("POST", "/api/tickets", { ...values, created_by: "user" });
    }
    dialog.close();
    await refresh(true);
    restoreOpenerFocus();
  } catch (error) {
    showError(error.message);
  }
});

$("close").addEventListener("click", requestClose);

dialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  requestClose();
});

dialog.addEventListener("click", (event) => {
  if (event.target !== dialog) return;
  const box = dialog.getBoundingClientRect();
  if (event.clientX < box.left) requestClose();
});

form.elements.description.addEventListener("input", renderDescriptionLinks);

form.elements.title.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  form.requestSubmit();
});

$("add-dependency").addEventListener("change", () => addDependency(false));

$("add-dependency").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  addDependency();
});

// Any edit re-arms the unsaved-changes warning.
const rearm = () => { warnedUnsaved = false; };

for (const target of [form, $("note-input")]) {
  target.addEventListener("input", rearm);
  target.addEventListener("change", rearm);
}

$("add-note").addEventListener("click", addNote);

$("note-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) addNote();
});

// Submitting the form keeps any edits made in the panel before cancelling.
$("cancel-ticket").addEventListener("click", () => {
  form.elements.status.value = CANCELLED.status;
  form.requestSubmit();
});

$("delete").addEventListener("click", async () => {
  if (!$("delete").classList.contains("armed")) {
    $("delete").textContent = "Confirm delete";
    $("delete").classList.add("armed");
    confirmTimer = setTimeout(disarmDelete, CONFIRM_MS);
    return;
  }
  disarmDelete();
  try {
    await api("DELETE", `/api/tickets/${editing.id}`);
    showToast(`Ticket #${editing.id} deleted`);
    dialog.close();
    await refresh(true);
    restoreOpenerFocus();
  } catch (error) {
    showError(error.message);
  }
});

$("new-ticket").addEventListener("click", () => openPanel(null));
