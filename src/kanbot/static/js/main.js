import { ALL_COLUMNS, PRIORITIES } from "./constants.js";
import { form, ideasOnly, showCancelled } from "./dom.js";
import { loadSetting } from "./settings.js";
import { onChange, poll, startLive } from "./live.js";
import { refresh } from "./refresh.js";
import { applyView } from "./views.js";
import "./filters.js";
import "./panel.js";
import "./shortcuts.js";

form.elements.status.append(...ALL_COLUMNS.map((c) => new Option(c.label, c.status)));
form.elements.priority.append(...PRIORITIES.map((p) => new Option(p.label, p.value)));
ideasOnly.checked = loadSetting("kanban-ideas-only") === "1";
showCancelled.checked = loadSetting("kanban-show-cancelled") === "1";
refresh(true);
poll(refresh);
onChange(["tickets"], refresh);
applyView();
startLive();
