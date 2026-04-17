/**
 * WS missed a phase change? Poll HTTP and catch up.
 */
import { authHeaders } from "./authApi.js";
import { getApiBase } from "./apiOrigin.js";

// WS fallback poll — only runs when the WebSocket misses phase changes.
// 15 s is more than fast enough for recovery; avoids hammering the server.
const DEFAULT_POLL_MS = 15000;

/**
 * @param {string} lobbyId
 * @returns {Promise<Record<string, unknown> | null>}
 */
export async function fetchMatchSync(lobbyId) {
  const res = await fetch(
    `${getApiBase()}/api/lobby/${encodeURIComponent(String(lobbyId))}/match_sync`,
    { headers: authHeaders() },
  );
  if (!res.ok) return null;
  return await res.json();
}

/**
 * @param {string} lobbyId
 * @param {(sync: Record<string, unknown>) => void} onSync
 * @param {number} [intervalMs]
 * @returns {() => void} stop
 */
export function pollMatchSync(lobbyId, onSync, intervalMs = DEFAULT_POLL_MS) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    const sync = await fetchMatchSync(lobbyId);
    if (stopped || !sync) return;
    onSync(sync);
  };
  void tick();
  const id = setInterval(() => void tick(), intervalMs);
  return () => {
    stopped = true;
    clearInterval(id);
  };
}
