import { $, dialog } from "./dom.js";
import { openPanel } from "./panel.js";

document.addEventListener("keydown", (event) => {
  if (dialog.open || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("textarea, select, input:not([type='checkbox'], [type='radio'], [type='button'], [type='submit'])")) return;
  if (event.key === "/") {
    event.preventDefault();
    $("search").focus();
  } else if (event.key.toLowerCase() === "n") {
    event.preventDefault();
    openPanel(null);
  }
});
