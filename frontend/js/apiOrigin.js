/**
 * Where the frontend talks to: sessionStorage override first, then bare :8000 for local uvicorn,
 * then a meta tag, then whatever origin we're on.
 * Override: sessionStorage.setItem("beatBattleApiBase", "https://…"); reload.
 */
function readMetaApiBase() {
  const el =
    document.querySelector('meta[name="cookup-api"]') ||
    document.querySelector('meta[name="beat-battle-api"]');
  const c = el?.getAttribute("content")?.trim();
  return c && c.length > 0 ? c.replace(/\/+$/, "") : "";
}

function readMetaCdnBase() {
  const el = document.querySelector('meta[name="beat-battle-cdn"]');
  const c = el?.getAttribute("content")?.trim();
  return c && c.length > 0 ? c.replace(/\/+$/, "") : "";
}

/** Production game hostnames → default R2 public URL when meta is missing (deploy safety net). */
const DEFAULT_CDN_FOR_HOSTNAMES = new Set([
  "beat-battle.net",
  "www.beat-battle.net",
]);
const DEFAULT_CDN_BASE = "https://assets.beat-battle.net";

/**
 * Public asset host (R2 custom domain). Empty → dataset + SFX load from API origin paths.
 * Override: sessionStorage.setItem("beatBattleCdnBase", "https://assets.example.com"); reload.
 */
export function getCdnBase() {
  try {
    const fromStorage = sessionStorage.getItem("beatBattleCdnBase")?.trim();
    if (fromStorage) return fromStorage.replace(/\/+$/, "");
  } catch {
    /* ignore */
  }
  const fromMeta = readMetaCdnBase();
  if (fromMeta) return fromMeta;
  try {
    const h = window.location?.hostname || "";
    if (DEFAULT_CDN_FOR_HOSTNAMES.has(h)) return DEFAULT_CDN_BASE;
  } catch {
    /* ignore */
  }
  return "";
}

export function getApiBase() {
  const fromStorage = sessionStorage.getItem("beatBattleApiBase")?.trim();
  if (fromStorage) return fromStorage.replace(/\/+$/, "");
  if (window.location.port === "8000") {
    return window.location.origin;
  }
  const fromMeta = readMetaApiBase();
  if (fromMeta) return fromMeta;
  return window.location.origin;
}

/** Same storage key as authApi TOKEN_KEY — can't import authApi here (cycles). */
const WS_TOKEN_KEY = "cookup_token";

/**
 * Fetch a short-lived, single-use WS ticket from the server.
 * Returns the ticket string, or null if the request fails.
 */
export async function fetchWsTicket() {
  try {
    const t = localStorage.getItem(WS_TOKEN_KEY)?.trim();
    if (!t) return null;
    const res = await fetch(`${getApiBase()}/api/ws-ticket`, {
      method: "POST",
      headers: { Authorization: `Bearer ${t}` },
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data.ticket || null;
  } catch {
    return null;
  }
}

/**
 * MP socket; same host as API, ?token= when you're signed in.
 * @param {{ resumePlayerId?: string, ticket?: string }} [opts]
 */
export function getWsUrl(opts = {}) {
  const u = new URL(getApiBase());
  const proto = u.protocol === "https:" ? "wss:" : "ws:";
  const base = `${proto}//${u.host}/ws`;
  const resume = String(opts.resumePlayerId || "").trim();

  // Prefer short-lived ticket over long-lived JWT
  const tokenValue = opts.ticket || (() => {
    try {
      return localStorage.getItem(WS_TOKEN_KEY)?.trim() || "";
    } catch {
      return "";
    }
  })();

  if (tokenValue) {
    let q = `token=${encodeURIComponent(tokenValue)}`;
    if (resume) q += `&resume_player_id=${encodeURIComponent(resume)}`;
    return `${base}?${q}`;
  }
  if (resume) return `${base}?resume_player_id=${encodeURIComponent(resume)}`;
  return base;
}

/**
 * Convenience: fetch a WS ticket, then build the URL with it.
 * Falls back to JWT if the ticket fetch fails.
 * @param {{ resumePlayerId?: string }} [opts]
 */
export async function getWsUrlWithTicket(opts = {}) {
  const ticket = await fetchWsTicket();
  return getWsUrl({ ...opts, ticket: ticket || undefined });
}

