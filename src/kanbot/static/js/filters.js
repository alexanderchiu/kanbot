import { $, board, filter, ideasOnly, showCancelled } from "./dom.js";
import { state } from "./state.js";
import { loadSetting, saveSetting } from "./settings.js";
import { render } from "./board.js";

export function renderFilter() {
  const projects = [...new Set(state.tickets.map((t) => t.project).filter(Boolean))].sort();
  // A saved project that no longer has tickets, for example after a rename, would hide
  // the whole board, so fall back to all projects.
  const saved = filter.value || loadSetting("kanban-project");
  const current = projects.includes(saved) ? saved : "";
  if (saved !== current) saveSetting("kanban-project", "");

  filter.replaceChildren(new Option("All projects", ""), ...projects.map((p) => new Option(p, p)));
  filter.value = current;
  $("project-list").replaceChildren(...projects.map((p) => new Option(p, p)));
  $("ticket-list").replaceChildren(...state.tickets.map((t) => new Option(`#${t.id} ${t.title}`)));
}

export function searchText(ticket) {
  const { id, title, description, project, assignee, created_by } = ticket;
  return [`#${id}`, title, description, project, assignee, created_by].join(" ").toLowerCase();
}

filter.addEventListener("change", () => {
  saveSetting("kanban-project", filter.value);
  render();
});

$("search").addEventListener("input", render);

$("clear-filters").addEventListener("click", () => {
  filter.value = "";
  ideasOnly.checked = false;
  $("search").value = "";
  saveSetting("kanban-project", "");
  saveSetting("kanban-ideas-only", "");
  render();
});

$("search").addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  $("search").value = "";
  $("search").blur();
  render();
});

ideasOnly.addEventListener("change", () => {
  saveSetting("kanban-ideas-only", ideasOnly.checked ? "1" : "");
  render();
});

showCancelled.addEventListener("change", () => {
  saveSetting("kanban-show-cancelled", showCancelled.checked ? "1" : "");
  render();
  if (showCancelled.checked) board.querySelector('[data-status="cancelled"]')?.scrollIntoView({ inline: "nearest", block: "nearest" });
});
