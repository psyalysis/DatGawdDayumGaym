/**
 * Rank badge HTML + sessionStorage keys for rank-up popups.
 */

export const RANK_BASELINE_KEY = "cookup_match_rank_index";
export const RANK_PENDING_KEY = "cookup_pending_rank_up";
/** Last self rank key shown after leaving lobby — hide repeat badge on re-entry if unchanged. */
export const LOBBY_LAST_SELF_RANK_KEY = "cookup_lobby_last_self_rank_key";

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const RANK_KEY_RE = /^[a-z0-9_]+$/;

function rankImageSrc(key) {
  const k = String(key || "").trim();
  if (!k || !RANK_KEY_RE.test(k)) return "";
  return new URL(`../imgs/ranks/${k}.png`, import.meta.url).href;
}

/** Small tag next to a name — rank comes from /me or lobby payload. */
export function rankBadgeHtml(rank) {
  const src = rank?.key ? rankImageSrc(rank.key) : "";
  if (!src) return "";
  const title = escapeHtml(rank.label || "");
  return `<span class="rank-badge"><img class="rank-badge__img" src="${src}" alt="${title}" width="28" height="28" decoding="async" /></span>`;
}

export function showRankUpOverlay(payload) {
  const label = payload?.label || "New rank";
  const color = String(payload?.color || "#cd7f32").replace(/[<>'"]/g, "");
  const src = payload?.key ? rankImageSrc(payload.key) : "";

  const wrap = document.createElement("div");
  wrap.className = "rank-up-overlay";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-modal", "true");
  wrap.setAttribute("aria-label", "Rank unlocked");
  const imgBlock = src
    ? `<div class="rank-up-badge-wrap"><img class="rank-up-badge-img" src="${src}" alt="" width="72" height="72" decoding="async" /></div>`
    : "";
  wrap.innerHTML = `
    <div class="rank-up-card" style="--rank-up-accent: ${color}">
      <p class="rank-up-kicker">Rank unlocked</p>
      <p class="rank-up-title">${escapeHtml(label)}</p>
      ${imgBlock}
      <button type="button" class="arcade-btn arcade-btn-primary rank-up-dismiss">OK</button>
    </div>
  `;

  const close = () => {
    wrap.classList.add("rank-up-overlay--out");
    window.setTimeout(() => wrap.remove(), 320);
  };

  wrap.addEventListener("click", (e) => {
    if (e.target === wrap) close();
  });
  wrap.querySelector(".rank-up-dismiss")?.addEventListener("click", close);

  document.body.appendChild(wrap);
  requestAnimationFrame(() => wrap.classList.add("rank-up-overlay--in"));
}
