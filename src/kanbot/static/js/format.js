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
