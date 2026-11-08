export function loadSetting(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}

export function saveSetting(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
