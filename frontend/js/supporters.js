/**
 * Who gets a heart — live list from /api/supporters, hardcoded fallback if that fails.
 */
import { getApiBase } from "./apiOrigin.js";
import { escapeHtml } from "./rankUi.js";

export const SUPPORTER_TOOLTIP = "This person is a supporter!";

const FALLBACK_KEYS = ["globagorb", "cowguts", "kdzfake", "originalmessgetter"];

/** @type {Set<string>} */
const NORMALIZED = new Set(FALLBACK_KEYS);

/** @type {Set<() => void>} */
const LIST_SUBSCRIBERS = new Set();

let loadedFromApi = false;
/** @type {ReturnType<typeof setInterval> | null} */
let pollId = null;

// Supporter list changes rarely — poll every 5 min (was 90 s) to reduce server load.
const POLL_MS = 300_000;

/**
 * @param {() => void} cb
 * @returns {() => void}
 */
export function subscribeSupporterList(cb) {
  LIST_SUBSCRIBERS.add(cb);
  return () => LIST_SUBSCRIBERS.delete(cb);
}

function notifyListUpdate() {
  LIST_SUBSCRIBERS.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
}

/**
 * @param {string[]} keys
 */
function applyKeys(keys) {
  NORMALIZED.clear();
  for (const k of keys) {
    const s = String(k).trim().toLowerCase();
    if (s) NORMALIZED.add(s);
  }
  notifyListUpdate();
}

async function fetchSupporterKeysFromApi() {
  const base = getApiBase();
  const res = await fetch(`${base}/api/supporters`);
  if (!res.ok) throw new Error(String(res.status));
  const data = await res.json();
  const names = Array.isArray(data.names) ? data.names : [];
  return names.map((n) => String(n).trim().toLowerCase()).filter(Boolean);
}

export async function refreshSupportersFromApi() {
  try {
    const keys = await fetchSupporterKeysFromApi();
    loadedFromApi = true;
    applyKeys(keys);
  } catch {
    if (!loadedFromApi) applyKeys(FALLBACK_KEYS);
  }
}

/** Pull once on load, then every so often — works for guests too. */
export function initSupportersClient() {
  void refreshSupportersFromApi();
  if (pollId != null) return;
  pollId = window.setInterval(() => {
    void refreshSupportersFromApi();
  }, POLL_MS);
}

/**
 * @param {unknown} name
 */
export function isSupporterDisplayName(name) {
  if (name == null) return false;
  const s = String(name).trim();
  if (!s) return false;
  return NORMALIZED.has(s.toLowerCase());
}

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

/**
 * Name + optional heart + tooltip if they're on the supporter list.
 * @param {unknown} rawDisplayName
 */
export function supporterDisplayNameInnerHtml(rawDisplayName) {
  const name = String(rawDisplayName ?? "");
  const safe = escapeHtml(name);
  if (!isSupporterDisplayName(name)) return safe;
  const tip = escapeAttr(SUPPORTER_TOOLTIP);
  return `<span class="supporter-wrap" data-corner-tooltip="${tip}" aria-label="${tip}" tabindex="0"><span class="supporter-heart" aria-hidden="true">❤️</span> ${safe}</span>`;
}

/**
 * Toasts can't do HTML so slap a heart prefix when they're a supporter.
 * @param {unknown} rawDisplayName
 */
export function supporterPlainPrefix(rawDisplayName) {
  return isSupporterDisplayName(rawDisplayName) ? "❤️ " : "";
}
