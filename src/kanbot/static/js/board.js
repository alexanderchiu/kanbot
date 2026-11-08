import { ALL_COLUMNS, CANCELLED, COLUMNS, PRIORITIES } from "./constants.js";
import { $, appendLinked, board, el, filter, icon, ICONS, ideasOnly, showCancelled } from "./dom.js";
import { api } from "./api.js";
import { renderFilter, searchText } from "./filters.js";
import { shortDate, timestamp } from "./format.js";
import { openPanel } from "./panel.js";
import { refresh } from "./refresh.js";
import { state, ticketById } from "./state.js";
import { showToast } from "./toast.js";

const dropLine = Object.assign(document.createElement("div"), { className: "drop-line" });

function renderCard(ticket) {
  // A div, not a button, because buttons can't contain working links.
  const card = el("div", "card");
  card.draggable = true;
  card.dataset.id = ticket.id;
  const title = el("button", "title", ticket.title);
  title.type = "button";
  card.append(title);
  if (ticket.description) {
    const snippet = el("div", "snippet");
    appendLinked(snippet, ticket.description);
    card.append(snippet);
  }

  const meta = el("div", "meta");
  const priority = PRIORITIES.find((p) => p.value === ticket.priority).label;
  const bars = icon("bars", `priority ${ticket.priority}`);
  bars.title = `${priority} priority`;
  bars.append(el("span", "sr-only", `${priority} priority`));
  meta.append(bars);
  if (ticket.idea) meta.append(el("span", "idea", "Idea"));
  if (ticket.blocked_by.length) {
    const waiting = el("span", "waiting", `Waiting on ${ticket.blocked_by.map((id) => `#${id}`).join(", ")}`);
    waiting.title = ticket.blocked_by.map((id) => `#${id} ${ticketById(id)?.title ?? ""}`).join("\n");
    meta.append(waiting);
  }
  if (ticket.project) meta.append(el("span", "project", ticket.project));
  if (ticket.assignee) {
    const assignee = el("span", "assignee");
    assignee.title = `Assigned to ${ticket.assignee}`;
    assignee.append(el("span", "avatar", ticket.assignee[0]), ticket.assignee);
    meta.append(assignee);
  }
  if (ticket.note_count) {
    const noun = ticket.note_count === 1 ? "note" : "notes";
    const notes = icon("note", "note-count");
    notes.title = `${ticket.note_count} ${noun}`;
    notes.append(String(ticket.note_count));
    meta.append(notes);
  }
  if (ticket.created_by) {
    const creator = el("span", "creator", `by ${ticket.created_by}`);
    creator.title = `Created by ${ticket.created_by}`;
    meta.append(creator);
  }
  const date = ticket.completed_at
    ? ["Completed", ticket.completed_at]
    : ["Created", ticket.created_at];
  const id = el("span", "id", `#${ticket.id} · ${shortDate(date[1])}`);
  id.title = `${date[0]} ${timestamp(date[1])}`;
  meta.append(id);
  card.append(meta);

  card.addEventListener("click", (event) => {
    if (!event.target.closest("a")) openPanel(ticket);
  });
  card.addEventListener("dragstart", (event) => {
    state.draggingId = ticket.id;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(ticket.id));
    requestAnimationFrame(() => card.classList.add("dragging"));
  });
  card.addEventListener("dragend", () => {
    if (state.draggingId === null) return;
    state.draggingId = null;
    dropLine.remove();
    refresh(true);
  });
  return card;
}

function renderColumn({ status, label }, visible) {
  const column = el("section", "column");
  column.dataset.status = status;
  column.setAttribute("aria-label", label);
  const head = el("div", "column-head");
  head.append(el("h2", "", label), el("span", "count", visible.length));
  if (status !== CANCELLED.status) {
    const add = el("button", "icon-button add");
    add.type = "button";
    add.innerHTML = ICONS.plus;
    add.setAttribute("aria-label", `Add ticket to ${label}`);
    add.title = "Add ticket";
    add.addEventListener("click", () => openPanel(null, status));
    head.append(add);
  }

  const cards = el("div", "cards");
  if (visible.length) cards.append(...visible.map(renderCard));
  else cards.append(el("p", "empty", "No tickets"));

  cards.addEventListener("dragover", (event) => {
    if (state.draggingId === null) return;
    event.preventDefault();
    cards.insertBefore(dropLine, cardAfterPointer(cards, event.clientY));
  });
  cards.addEventListener("drop", (event) => {
    if (state.draggingId === null) return;
    event.preventDefault();
    drop(status, cards, state.draggingId);
  });

  column.append(head, cards);
  return column;
}

export function render() {
  renderFilter();
  const query = $("search").value.trim().toLowerCase();
  const visible = state.tickets.filter(
    (t) =>
      (!filter.value || t.project === filter.value) &&
      (!ideasOnly.checked || t.idea) &&
      (!query || searchText(t).includes(query))
  );
  const cancelled = visible.filter((t) => t.status === CANCELLED.status);
  $("cancelled-count").textContent = cancelled.length;
  const shown = showCancelled.checked ? visible : visible.filter((t) => t.status !== CANCELLED.status);
  const filtering = Boolean(filter.value || ideasOnly.checked || query);
  $("filtered-empty").hidden = !(filtering && state.tickets.length && !shown.length);

  // Rebuilding the board mustn't lose the reader's place when an agent changes a ticket.
  const scroll = {};
  for (const column of board.children) scroll[column.dataset.status] = column.querySelector(".cards").scrollTop;
  const boardScroll = board.scrollLeft;
  const focused = focusTarget(document.activeElement);

  const columns = showCancelled.checked ? ALL_COLUMNS : COLUMNS;
  board.replaceChildren(
    ...columns.map((column) =>
      renderColumn(column, visible.filter((t) => t.status === column.status))
    )
  );

  for (const column of board.children) column.querySelector(".cards").scrollTop = scroll[column.dataset.status] ?? 0;
  board.scrollLeft = boardScroll;
  if (focused) findFocusTarget(focused)?.focus({ preventScroll: true });
}

// Describes a focusable board element so it can be found again after a rebuild.
const FOCUSABLE = "button, a[href]";

export function focusTarget(node) {
  if (!node || !board.contains(node)) return null;
  const card = node.closest(".card");
  if (card) return { selector: `.card[data-id="${card.dataset.id}"]`, index: [...card.querySelectorAll(FOCUSABLE)].indexOf(node) };
  const column = node.closest(".column");
  if (column && node.matches(".add")) return { selector: `.column[data-status="${column.dataset.status}"] .add`, index: -1 };
  return null;
}

export function findFocusTarget({ selector, index }) {
  const found = board.querySelector(selector);
  return index < 0 ? found : found?.querySelectorAll(FOCUSABLE)[index];
}

function cardAfterPointer(cards, y) {
  for (const card of cards.querySelectorAll(".card:not(.dragging)")) {
    const box = card.getBoundingClientRect();
    if (y < box.top + box.height / 2) return card;
  }
  return null;
}

// Positions index the whole column, which can hold tickets hidden by the filters.
async function drop(status, cards, id) {
  const column = state.tickets.filter((t) => t.status === status && t.id !== id);
  const indexOf = (card) => column.findIndex((t) => t.id === Number(card.dataset.id));
  const next = dropLine.nextElementSibling;
  let position = column.length;
  if (next?.classList.contains("card") && !next.classList.contains("dragging")) {
    position = indexOf(next);
  } else {
    const before = [...cards.querySelectorAll(".card:not(.dragging)")].at(-1);
    if (before) position = indexOf(before) + 1;
  }
  dropLine.remove();
  state.draggingId = null;

  // Show the move straight away, then let the next refresh confirm it.
  const ticket = state.tickets.find((t) => t.id === id);
  const others = state.tickets.filter((t) => t.id !== id);
  const after = column[position];
  others.splice(after ? others.indexOf(after) : others.length, 0, { ...ticket, status });
  state.tickets = others;
  render();

  try {
    await api("PATCH", `/api/tickets/${id}`, { status, position, actor: "user" });
  } catch (error) {
    showToast(error.message);
  }
  await refresh(true);
}
