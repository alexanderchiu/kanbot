import { $, board } from "./dom.js";
import { showAgents } from "./agents.js";
import { showStats } from "./stats.js";

// The header switch: the hash picks the board, Stats or Agents.
const VIEWS = { "#stats": "stats", "#agents": "agents" };

export const currentView = () => VIEWS[location.hash] ?? "board";

export function applyView() {
  const view = currentView();
  document.body.dataset.view = view;
  board.hidden = view !== "board";
  $("stats-view").hidden = view !== "stats";
  $("agents-view").hidden = view !== "agents";
  for (const name of ["board", "stats", "agents"]) {
    const link = $(`view-${name}`);
    if (name === view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  showStats(view === "stats");
  showAgents(view === "agents");
}

window.addEventListener("hashchange", applyView);
