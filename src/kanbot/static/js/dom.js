// Static markup only. Ticket text never goes through innerHTML.
export const ICONS = {
  plus: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9"/></svg>',
  bars: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><rect x="1" y="7" width="2.4" height="4" rx="0.6"/><rect x="4.8" y="4" width="2.4" height="7" rx="0.6"/><rect x="8.6" y="1" width="2.4" height="10" rx="0.6"/></svg>',
  note: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 3.5h11v8h-6l-3 2.5v-2.5h-2z"/></svg>',
};

export const $ = (id) => document.getElementById(id);

export const board = $("board"), filter = $("filter"), ideasOnly = $("ideas-only"), showCancelled = $("show-cancelled"), dialog = $("dialog"), form = $("form");

// All ticket text comes from agents, so it is only ever inserted as text nodes.
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function icon(name, className) {
  const node = el("span", className);
  node.innerHTML = ICONS[name];
  return node;
}

// Only http and https match, so ticket text can't create a javascript: link.
const URL_PATTERN = /https?:\/\/[^\s<>"']+/g;

function trimUrl(url) {
  const count = (char) => url.split(char).length - 1;
  for (;;) {
    const trimmed = url.replace(/[.,;:!?\]}]+$/, "");
    if (trimmed !== url) url = trimmed;
    else if (url.endsWith(")") && count(")") > count("(")) url = url.slice(0, -1);
    else return url;
  }
}

export function urlsIn(text) {
  return [...text.matchAll(URL_PATTERN)].map((match) => trimUrl(match[0]));
}

export function linkNode(url) {
  const link = el("a", "", url);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.draggable = false;
  return link;
}

export function appendLinked(parent, text) {
  let rest = text;
  for (const url of urlsIn(text)) {
    const start = rest.indexOf(url);
    parent.append(rest.slice(0, start), linkNode(url));
    rest = rest.slice(start + url.length);
  }
  parent.append(rest);
}
