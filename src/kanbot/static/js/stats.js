import { PRIORITIES, POLL_MS } from "./constants.js";
import { $, el, filter } from "./dom.js";
import { formatDuration } from "./format.js";
import { loadSetting } from "./settings.js";

// Stats view. Ticket-derived text only ever goes in as text nodes.
const statsView = $("stats-view");

const STAT_TABLES = [
  { key: "by_creator", title: "By creator", column: "Creator", unknown: "Unknown" },
  { key: "by_assignee", title: "By assignee", column: "Assignee", unknown: "Unassigned" },
  { key: "by_project", title: "By project", column: "Project", unknown: "No project" },
  { key: "by_priority", title: "By priority", column: "Priority", unknown: "None" },
];

let statsPayload = null;
let statsRequest = 0;

function statsRow(row, spec, max) {
  const tr = el("tr");
  let name = row.name === null ? spec.unknown : row.name;
  if (spec.key === "by_priority") name = PRIORITIES.find((p) => p.value === row.name)?.label ?? name;
  const head = el("th", row.name === null ? "muted" : "", name);
  head.scope = "row";
  head.title = name;
  const count = el("td");
  const cell = el("div", "count-cell");
  const bar = el("span", "count-bar");
  bar.setAttribute("aria-hidden", "true");
  const fill = el("span");
  fill.style.width = `${(row.tickets / max) * 100}%`;
  bar.append(fill);
  cell.append(bar, String(row.tickets));
  count.append(cell);
  tr.append(head, count, el("td", "", String(row.open)), el("td", "", String(row.done)), el("td", "", String(row.cancelled)));
  for (const key of ["avg_completion_seconds", "median_completion_seconds"]) {
    const value = row[key];
    tr.append(el("td", value === null ? "muted" : "", formatDuration(value)));
  }
  return tr;
}

function statsTable(spec, rows) {
  const section = el("section", "stat-section");
  const heading = el("h2", "", spec.title);
  heading.id = `stats-${spec.key}`;
  const scroller = el("div", "table-scroll");
  scroller.tabIndex = 0;
  scroller.setAttribute("role", "region");
  scroller.setAttribute("aria-labelledby", heading.id);
  const table = el("table", "stat-table");
  const headRow = el("tr");
  [spec.column, "Tickets", "Open", "Done", "Cancelled", "Average", "Median"].forEach((label) => {
    const th = el("th", "", label);
    th.scope = "col";
    headRow.append(th);
  });
  const max = Math.max(1, ...rows.map((r) => r.tickets));
  table.append(el("thead"), el("tbody"));
  table.tHead.append(headRow);
  table.tBodies[0].append(...rows.map((row) => statsRow(row, spec, max)));
  scroller.append(table);
  section.append(heading, scroller);
  return section;
}

function renderStats(stats) {
  const scroll = statsView.scrollTop;
  const inner = new Map([...statsView.querySelectorAll(".table-scroll")].map((n) => [n.getAttribute("aria-labelledby"), n.scrollLeft]));
  const body = el("div", "stats-body");
  if (!stats.total) {
    body.append(el("p", "stats-empty", filter.value ? "No tickets in this project yet." : "No tickets yet. Stats appear once the board has some."));
  } else {
    const tiles = el("dl", "tiles");
    for (const [label, value] of [
      ["Total tickets", stats.total],
      ["Open", stats.open],
      ["Done", stats.by_status.done],
      ["Average completion time", formatDuration(stats.avg_completion_seconds)],
      ["Median completion time", formatDuration(stats.median_completion_seconds)],
    ]) {
      const tile = el("div", "tile");
      tile.append(el("dt", "", label), el("dd", "", String(value)));
      tiles.append(tile);
    }
    const tables = el("div", "stat-tables");
    tables.append(...STAT_TABLES.map((spec) => statsTable(spec, stats[spec.key])));
    body.append(tiles, tables, el("p", "stats-note", "Completion time runs from creation to Done, for done tickets. Tickets moved to Done before completion tracking use their last-updated time."));
  }
  statsView.replaceChildren(body);
  statsView.scrollTop = scroll;
  for (const node of statsView.querySelectorAll(".table-scroll")) node.scrollLeft = inner.get(node.getAttribute("aria-labelledby")) ?? 0;
}

async function refreshStats(force = false) {
  if (location.hash !== "#stats" || document.hidden) return;
  const project = filter.value || loadSetting("kanban-project");
  const request = ++statsRequest;
  try {
    const response = await fetch(project ? `/api/stats?project=${encodeURIComponent(project)}` : "/api/stats");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.text();
    if (request !== statsRequest || location.hash !== "#stats") return;
    $("banner").hidden = true;
    if (!force && payload === statsPayload) return;
    statsPayload = payload;
    renderStats(JSON.parse(payload));
  } catch {
    if (request === statsRequest) $("banner").hidden = false;
  }
}

export function showStats(active) {
  statsPayload = null;
  if (active) refreshStats(true);
}

filter.addEventListener("change", () => { statsPayload = null; refreshStats(true); });

document.addEventListener("visibilitychange", () => refreshStats());

setInterval(() => refreshStats(), POLL_MS);
