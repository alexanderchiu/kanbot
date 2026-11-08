// Live updates: the server sends an event on GET /api/events whenever the board changes,
// and the views refetch at once. A slow poll covers missed events; while the stream is
// down, views poll as often as they used to.
import { FALLBACK_POLL_MS, POLL_MS } from "./constants.js";

const handlers = { tickets: new Set(), agents: new Set() };
let connected = false;

// Run fn for each change of these kinds ("tickets", "agents").
export function onChange(kinds, fn) {
  const run = coalesce(fn);
  for (const kind of kinds) handlers[kind].add(run);
}

// Run fn on a timer: every POLL_MS while the stream is down, or every FALLBACK_POLL_MS.
export function poll(fn) {
  let last = Date.now();
  setInterval(() => {
    if (connected && Date.now() - last < FALLBACK_POLL_MS) return;
    last = Date.now();
    fn();
  }, POLL_MS);
}

// At most one run at a time, plus one more if changes arrive meanwhile.
function coalesce(fn) {
  let running = false;
  let again = false;
  return async function run() {
    if (running) {
      again = true;
      return;
    }
    running = true;
    try {
      await fn();
    } finally {
      running = false;
      if (again) {
        again = false;
        run();
      }
    }
  };
}

function notify(kinds) {
  const due = new Set();
  for (const kind of kinds) for (const run of handlers[kind] ?? []) due.add(run);
  for (const run of due) run();
}

export function startLive() {
  if (!("EventSource" in window)) return;
  const source = new EventSource("/api/events");
  source.addEventListener("open", () => {
    connected = true;
    // Catch up on anything that changed while disconnected.
    notify(Object.keys(handlers));
  });
  source.addEventListener("error", () => {
    // The browser reconnects by itself; poll at the old rate until it does.
    connected = false;
  });
  source.addEventListener("change", (event) => {
    try {
      notify(JSON.parse(event.data).kinds);
    } catch {
      notify(Object.keys(handlers));
    }
  });
}
