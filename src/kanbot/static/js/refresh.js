import { $ } from "./dom.js";
import { render } from "./board.js";
import { mergeOpenTicket } from "./panel.js";
import { state } from "./state.js";

let lastPayload = null;

export async function refresh(force = false) {
  try {
    const response = await fetch("/api/tickets");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.text();
    $("banner").hidden = true;
    // Don't rebuild the board under a drag or when nothing has changed.
    if (state.draggingId !== null || (!force && payload === lastPayload)) return;
    lastPayload = payload;
    state.tickets = JSON.parse(payload);
    render();
    mergeOpenTicket();
  } catch {
    $("banner").hidden = false;
  }
}
