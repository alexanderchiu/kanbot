export const shortDate = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });

export const timestamp = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "<1m";
  const days = Math.floor(minutes / 1440), hours = Math.floor((minutes % 1440) / 60), mins = minutes % 60;
  const parts = days ? [[days, "d"], [hours, "h"]] : hours ? [[hours, "h"], [mins, "m"]] : [[mins, "m"]];
  return parts.filter(([n], i) => n || i === 0).map(([n, u]) => n + u).join(" ");
}

// Live figures: seconds under a minute, then minutes and hours.
export function formatElapsed(seconds) {
  if (seconds < 60) return `${Math.max(0, seconds)}s`;
  if (seconds < 600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return formatDuration(seconds);
}

export function formatAgo(seconds) {
  return seconds < 10 ? "just now" : `${formatElapsed(seconds)} ago`;
}

export function formatTokens(count) {
  if (count < 1000) return String(count);
  if (count < 1e6) {
    const thousands = count / 1000;
    const text = thousands < 100 ? thousands.toFixed(1) : String(Math.round(thousands));
    if (!text.startsWith("1000")) return `${text}k`;
  }
  return `${(count / 1e6).toFixed(1)}M`;
}

// "claude-opus-5-5" becomes "Opus 5.5". Anything unrecognised is shown as it is.
const VERSION = "(\\d+(?:-\\d{1,2}(?!\\d))?)";
const SUFFIX = "(?:-\\d{8})?(?:\\[.*\\])?$";
const FAMILY_FIRST = new RegExp(`^claude-(opus|sonnet|haiku)-${VERSION}${SUFFIX}`);
const VERSION_FIRST = new RegExp(`^claude-${VERSION}-(opus|sonnet|haiku)${SUFFIX}`);

export function shortModel(id) {
  if (!id) return "Unknown model";
  const name = (family) => family[0].toUpperCase() + family.slice(1);
  let match = FAMILY_FIRST.exec(id);
  if (match) return `${name(match[1])} ${match[2].replace("-", ".")}`;
  match = VERSION_FIRST.exec(id);
  if (match) return `${name(match[2])} ${match[1].replace("-", ".")}`;
  return id;
}
