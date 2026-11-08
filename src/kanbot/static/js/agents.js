import { ALL_COLUMNS, CONFIRM_MS, POLL_MS } from "./constants.js";
import { $, el, filter } from "./dom.js";
import { api } from "./api.js";
import { formatAgo, formatElapsed, formatTokens, shortModel, timestamp } from "./format.js";
import { openPanel } from "./panel.js";
import { loadSetting, saveSetting } from "./settings.js";
import { ticketById } from "./state.js";
import { showToast } from "./toast.js";

// Agents view. Everything from sessions (models, projects, paths, errors) goes in as text nodes.
const agentsView = $("agents-view");
const badge = $("agents-badge");

const BADGE_MS = 10000;
const STALE_SECONDS = 12 * 3600;

// Order matters: the sort puts sessions that need the user first.
const STATES = {
  needs_input: { label: "Needs input", rank: 0 },
  rate_limited: { label: "Rate limited", rank: 0 },
  working: { label: "Working", rank: 1 },
  idle: { label: "Idle", rank: 2 },
  error: { label: "Error", rank: 3 },
  ended: { label: "Ended", rank: 4 },
  stale: { label: "Stale", rank: 4 },
};
const SUMMARY = ["needs_input", "rate_limited", "working", "idle", "error", "ended", "stale"];
const ATTENTION = new Set(["needs_input", "rate_limited"]);

let signature = null;
let sessions = [];
let request = 0;
let showEnded = loadSetting("kanban-show-ended") === "1";
let armedKey = null;
let armTimer;
let lastTicketKey = null;

const isAgentsView = () => location.hash === "#agents";

// A session with no event for 12 hours is shown as stale, whatever it last said it was doing.
const ownState = (s) => (s.state !== "ended" && s.idle_seconds >= STALE_SECONDS ? "stale" : s.state);

// A subagent waiting on the user means its session needs the user too.
function effectiveState(session) {
  const own = ownState(session);
  if (ATTENTION.has(own) || own === "stale" || own === "ended") return own;
  return session.subagents.map(ownState).find((s) => ATTENTION.has(s)) ?? own;
}

const visibleSessions = (list, project = filter.value) => (project ? list.filter((s) => s.project === project) : list);

export function attentionCount(list) {
  return list.filter((s) => ATTENTION.has(effectiveState(s))).length;
}

function sortSessions(list) {
  return [...list].sort((a, b) => STATES[effectiveState(a)].rank - STATES[effectiveState(b)].rank || a.idle_seconds - b.idle_seconds);
}

function setBadge(count) {
  badge.textContent = String(count);
  badge.hidden = count === 0;
  const link = $("view-agents");
  if (count) link.setAttribute("aria-label", `Agents, ${count} need${count === 1 ? "s" : ""} you`);
  else link.removeAttribute("aria-label");
}

// Seconds-based figures change on every poll, so they're left out of the change check
// and updated in place instead. That keeps focus and hover steady.
const stripSeconds = (key, value) => (key.endsWith("_seconds") ? undefined : value);

function flatten(list) {
  return list.flatMap((s) => [s, ...s.subagents]);
}

const FORMATS = {
  elapsed: (v) => formatElapsed(v),
  ago: (v) => formatAgo(v),
};

// A node whose text follows one of a session's live figures.
function live(tag, className, session, field, format) {
  const node = el(tag, className, FORMATS[format](session[field]));
  node.dataset.key = session.key;
  node.dataset.field = field;
  node.dataset.format = format;
  return node;
}

function tickLive() {
  const byKey = new Map(flatten(sessions).map((s) => [s.key, s]));
  for (const node of agentsView.querySelectorAll("[data-field]")) {
    const session = byKey.get(node.dataset.key);
    if (session) node.textContent = FORMATS[node.dataset.format](session[node.dataset.field]);
  }
}

function statePill(session) {
  const state = ownState(session);
  const pill = el("span", "state-pill", STATES[state].label);
  pill.dataset.state = state;
  return pill;
}

function sinceNode(session) {
  const state = ownState(session);
  if (state === "stale") {
    const node = el("span", "agent-since");
    node.append("quiet for ", live("span", "", session, "idle_seconds", "elapsed"));
    node.title = `Last event ${timestamp(session.last_event_at)}, last seen ${session.state.replace("_", " ")}`;
    return node;
  }
  const node = el("span", "agent-since");
  node.append("for ", live("span", "", session, "state_seconds", "elapsed"));
  node.title = `Since ${timestamp(session.state_since)}`;
  return node;
}

function ticketButton(session) {
  const { ticket } = session;
  const button = el("button", "agent-ticket");
  button.type = "button";
  button.dataset.status = ticket.status;
  button.title = `Open #${ticket.id}`;
  const status = ALL_COLUMNS.find((c) => c.status === ticket.status)?.label ?? ticket.status;
  button.append(el("span", "ticket-title", `#${ticket.id} ${ticket.title}`), el("span", "ticket-status", status));
  button.addEventListener("click", () => {
    const full = ticketById(ticket.id);
    if (!full) {
      showToast(`Ticket #${ticket.id} isn't on the board.`);
      return;
    }
    lastTicketKey = session.key;
    openPanel(full);
  });
  return button;
}

function statCell(label, value, sub, title) {
  const cell = el("div");
  const dd = el("dd");
  if (typeof value === "string") dd.textContent = value; else dd.append(value);
  if (sub) dd.append(" ", el("span", "sub", sub));
  if (title) cell.title = title;
  cell.append(el("dt", "", label), dd);
  return cell;
}

function tokenSummary(s) {
  const io = s.input_tokens + s.output_tokens;
  const cache = s.cache_read_tokens + s.cache_creation_tokens;
  const n = (v) => v.toLocaleString();
  const title = `Approximate. Read from session transcripts, best-effort.\nInput ${n(s.input_tokens)}, output ${n(s.output_tokens)}\nCache read ${n(s.cache_read_tokens)}, cache written ${n(s.cache_creation_tokens)}`;
  return { io: `≈${formatTokens(io)}`, cache: `+ ${formatTokens(cache)} cache`, title };
}

function dismissButton(session) {
  const button = el("button", "button quiet agent-dismiss", armedKey === session.key ? "Confirm dismiss" : "Dismiss");
  button.type = "button";
  button.title = "Remove this session from the list";
  if (armedKey === session.key) button.classList.add("danger", "armed");
  button.addEventListener("click", () => dismiss(session, button));
  return button;
}

async function dismiss(session, button) {
  if (armedKey !== session.key) {
    armedKey = session.key;
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { armedKey = null; renderAgents(); }, CONFIRM_MS);
    renderAgents();
    return;
  }
  clearTimeout(armTimer);
  armedKey = null;
  button.disabled = true;
  try {
    await api("DELETE", `/api/agents/${encodeURIComponent(session.session_id)}`);
    showToast("Session dismissed.");
  } catch (error) {
    showToast(error.message);
  }
  refreshAgents(true);
}

function subagentRow(sub) {
  const row = el("li", "agent-sub");
  const label = sub.agent_type || "Subagent";
  const type = el("span", "agent-type", label);
  type.title = `Subagent ${sub.agent_id ?? ""}`.trim();
  const bits = [sub.model ? shortModel(sub.model) : null].filter(Boolean);
  row.append(statePill(sub), sinceNode(sub), type);
  if (bits.length) {
    const model = el("span", "agent-meta", bits[0]);
    model.title = sub.model;
    row.append(model);
  }
  const work = el("span", "agent-meta");
  work.append("working ", live("span", "", sub, "working_seconds", "elapsed"));
  const tools = el("span", "agent-meta", `${sub.tool_calls} tool call${sub.tool_calls === 1 ? "" : "s"}${sub.last_tool ? `, last ${sub.last_tool}` : ""}`);
  const tokens = tokenSummary(sub);
  const tokenNode = el("span", "agent-meta", `${tokens.io} tokens`);
  tokenNode.title = tokens.title;
  const ago = live("span", "agent-meta", sub, "idle_seconds", "ago");
  row.append(work, tools);
  if (sub.input_tokens + sub.output_tokens + sub.cache_read_tokens + sub.cache_creation_tokens) row.append(tokenNode);
  row.append(ago);
  if (sub.state_detail) {
    const detail = el("p", "agent-detail", sub.state_detail);
    detail.title = sub.state_detail;
    row.append(detail);
  }
  return row;
}

function sessionCard(session) {
  const own = ownState(session);
  const effective = effectiveState(session);
  const card = el("li", "agent");
  card.dataset.key = session.key;
  card.dataset.state = effective;
  if (ATTENTION.has(effective)) card.dataset.attention = "";

  const head = el("div", "agent-head");
  const model = el("span", "agent-model", shortModel(session.model));
  if (session.model) model.title = session.model;
  head.append(statePill(session), sinceNode(session), model);
  if (session.project) {
    const project = el("span", "agent-project", session.project);
    if (session.cwd) project.title = session.cwd;
    head.append(project);
  } else if (session.cwd) {
    const cwd = el("span", "agent-meta", session.cwd.split("/").filter(Boolean).at(-1) ?? session.cwd);
    cwd.title = session.cwd;
    head.append(cwd);
  }
  head.append(el("span", "agent-spacer"));
  if (own === "ended" || own === "stale") head.append(dismissButton(session));
  card.append(head);

  if (session.state_detail) {
    const detail = el("p", "agent-detail", session.state_detail);
    detail.title = session.state_detail;
    card.append(detail);
  }
  if (session.ticket) card.append(ticketButton(session));

  const tokens = tokenSummary(session);
  const stats = el("dl", "agent-stats");
  stats.append(
    statCell("Working", live("span", "", session, "working_seconds", "elapsed")),
    statCell("Age", live("span", "", session, "age_seconds", "elapsed")),
    statCell("Turns", String(session.turns)),
    statCell("Tool calls", String(session.tool_calls), session.last_tool ? `last ${session.last_tool}` : ""),
    statCell("Tokens", tokens.io, tokens.cache, tokens.title),
    statCell("Last activity", live("span", "", session, "idle_seconds", "ago")),
  );
  stats.lastChild.title = `Last event ${timestamp(session.last_event_at)}`;
  card.append(stats);

  const subagents = session.subagents.filter((s) => showEnded || s.state !== "ended");
  if (subagents.length) {
    const subs = el("ul", "agent-subs");
    subs.setAttribute("aria-label", `Subagents of ${shortModel(session.model)} session`);
    subs.append(...subagents.map(subagentRow));
    card.append(subs);
  }
  return card;
}

function summaryRow(list) {
  const counts = Object.fromEntries(SUMMARY.map((state) => [state, 0]));
  for (const s of list) counts[effectiveState(s)] += 1;
  const row = el("ul", "agent-summary");
  row.setAttribute("aria-label", "Sessions by state");
  for (const state of SUMMARY) {
    if (!counts[state] && (state === "ended" || state === "stale")) continue;
    const item = el("li");
    item.dataset.state = state;
    if (!counts[state]) item.dataset.zero = "";
    item.append(el("b", "", String(counts[state])), STATES[state].label);
    row.append(item);
  }
  return row;
}

function showEndedToggle() {
  const label = el("label", "toggle");
  const input = el("input");
  input.type = "checkbox";
  input.id = "show-ended";
  input.checked = showEnded;
  input.addEventListener("change", () => {
    showEnded = input.checked;
    saveSetting("kanban-show-ended", showEnded ? "1" : "0");
    refreshAgents(true);
  });
  label.append(input, " Show ended");
  return label;
}

function emptyState(project) {
  const box = el("div", "agents-empty");
  box.append(el("p", "", project ? "No sessions in this project." : "No agent sessions yet."));
  const help = el("div", "agents-help");
  const run = el("p");
  run.append("To track Claude Code sessions, run ", el("code", "", "uv run kanbot-hooks install"), " yourself, then restart your Claude Code sessions.");
  const docs = el("p");
  docs.append("Details: ", el("code", "", "docs/agent-hooks.md"));
  help.append(run, docs);
  box.append(help);
  return box;
}

// Remember which control had focus so a rebuild doesn't drop it.
function focusedControl() {
  const node = document.activeElement;
  if (!node || !agentsView.contains(node) || node === agentsView) return null;
  const card = node.closest("[data-key]");
  if (node.id) return { id: node.id };
  const cls = ["agent-ticket", "agent-dismiss"].find((c) => node.classList.contains(c));
  return card && cls ? { key: card.dataset.key, cls } : null;
}

function restoreFocus(target) {
  if (!target) return;
  const found = target.id ? $(target.id) : [...agentsView.querySelectorAll(`li.agent .${target.cls}`)].find((n) => n.closest("li.agent").dataset.key === target.key);
  found?.focus({ preventScroll: true });
}

function renderAgents() {
  const scroll = agentsView.scrollTop;
  const focus = focusedControl();
  const project = filter.value;
  const list = sortSessions(visibleSessions(sessions, project));
  const body = el("div", "agents-body");
  const bar = el("div", "agents-bar");
  bar.append(summaryRow(list), showEndedToggle());
  body.append(bar);
  if (!list.length) {
    body.append(emptyState(project));
  } else {
    const ul = el("ul", "agent-list");
    ul.setAttribute("aria-label", "Agent sessions");
    ul.append(...list.map(sessionCard));
    body.append(ul);
    body.append(el("p", "agents-note", "Token figures are approximate: they're read from session transcripts and are best-effort."));
  }
  agentsView.replaceChildren(body);
  agentsView.scrollTop = scroll;
  restoreFocus(focus);
}

async function fetchSessions(all) {
  const response = await fetch(all ? "/api/agents?all=true" : "/api/agents");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

async function refreshAgents(force = false) {
  if (!isAgentsView() || document.hidden) return;
  const mine = ++request;
  try {
    const text = await fetchSessions(showEnded);
    if (mine !== request || !isAgentsView()) return;
    $("banner").hidden = true;
    const next = JSON.parse(text);
    sessions = next;
    setBadge(attentionCount(next));
    // Going stale depends on the clock, not on any field that changes.
    const nextSignature = JSON.stringify(next, stripSeconds) + flatten(next).map(ownState).join() + showEnded + filter.value;
    if (!force && nextSignature === signature) {
      tickLive();
      return;
    }
    signature = nextSignature;
    renderAgents();
  } catch {
    if (mine === request) $("banner").hidden = false;
  }
}

// Light poll for the header badge, only while another view is showing.
async function refreshBadge() {
  if (isAgentsView() || document.hidden) return;
  try {
    setBadge(attentionCount(JSON.parse(await fetchSessions(false))));
  } catch {
    // The board's own refresh reports an unreachable server.
  }
}

export function showAgents(active) {
  signature = null;
  if (active) refreshAgents(true); else refreshBadge();
}

filter.addEventListener("change", () => { signature = null; refreshAgents(true); });
document.addEventListener("visibilitychange", () => { refreshAgents(); refreshBadge(); });
setInterval(() => refreshAgents(), POLL_MS);
setInterval(refreshBadge, BADGE_MS);

// The panel returns focus to the board, which isn't showing, so put it back on the ticket link.
$("dialog").addEventListener("close", () => {
  if (!isAgentsView() || lastTicketKey === null) return;
  const key = lastTicketKey;
  lastTicketKey = null;
  restoreFocus({ key, cls: "agent-ticket" });
});
