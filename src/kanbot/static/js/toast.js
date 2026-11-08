import { TOAST_MS } from "./constants.js";
import { $ } from "./dom.js";

let toastTimer;

export function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}
