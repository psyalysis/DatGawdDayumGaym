/**
 * Pre-game room: roster, ready up, host starts → cook.
 */
import { notifyMpServerError, setAppErrorContext } from "../errorToast.js";
import { showServerRestartingWait } from "../serverRestartOverlay.js";
import {
  navigateToMenuAfterLobbyDissolved,
  notifyMpPlayerDisconnected,
  notifyMpPlayerJoin,
  notifyMpPlayerLeave,
  showKickedFromMatchToast,
} from "../mpPresenceToast.js";
import { mountAuthCornerLeave } from "../authCorner.js";
import {
  escapeHtml,
  LOBBY_LAST_SELF_RANK_KEY,
  rankBadgeHtml,
} from "../rankUi.js";
import { supporterDisplayNameInnerHtml } from "../supporters.js";
import {
  clearMpChatSession,
  ingestMpChatMessage,
  mountMpChat,
  mpChatHandleErrorPayload,
} from "../mpChat.js";
import { playSfxBeatBattle, playSfxMajor, playSfxMinor } from "../sfx.js";
import { mountCookScreen } from "./cook.js";

/**
 * @param {HTMLElement} root
 * @param {object} lobby
 * @param {string} selfId
 * @param {null | { step?: number; total?: number; message?: string; percent?: number }} kitProgress
 * @param {boolean} settingsPanelOpen
 * @param {boolean} hideSelfRankBadge — omit self row badge when re-entering lobby at same rank
 */
function renderLobby(
  root,
  lobby,
  selfId,
  kitProgress,
  settingsPanelOpen,
  hideSelfRankBadge,
) {
  const players = lobby.players || [];
  const hostId = lobby.host_id || "";
  const isHost = Boolean(selfId && hostId && selfId === hostId);
  const cookMin = Number(lobby.cook_duration_min) || 10;
  const anonymousVoting = Boolean(lobby.anonymous_voting);
  const generating = lobby.state === "generating" || kitProgress != null;
  const pct = generating ? Math.min(100, Number(kitProgress?.percent) || 0) : 0;
  const kitMsg = kitProgress?.message || "Preparing kit…";
  const step = kitProgress?.step ?? 0;
  const total = kitProgress?.total ?? 11;
  const label = kitProgress?.label;
  const stepLine =
    label && step > 0
      ? `${step} / ${total} — ${String(label).replace(/_/g, " ")}`
      : `${step} / ${total}`;
  const lobbyState = String(lobby.state ?? "");
  const canKick = isHost && !generating && lobbyState === "lobby";

  const rows = players
    .map((p) => {
      const pid = String(p.id ?? "");
      const isSelf = Boolean(selfId && pid === String(selfId));
      const kickSlot =
        canKick && !isSelf
          ? `<span class="lobby-kick-slot">
        <button type="button" class="lobby-kick-btn" data-kick-id="${escapeHtml(pid)}" aria-label="Kick">−</button>
        <span class="lobby-kick-tip" role="tooltip">Kick</span>
      </span>`
          : "";
      const nameWrapClass =
        kickSlot !== ""
          ? "lobby-row-name-wrap lobby-row-name-wrap--kick-hover"
          : "lobby-row-name-wrap";
      const rankHtml =
        isSelf && hideSelfRankBadge ? "" : rankBadgeHtml(p.rank);
      return `
    <div class="lobby-row${kickSlot !== "" ? " lobby-row--kickable" : ""}">
      <div class="${nameWrapClass}">
        ${kickSlot}
        <span class="lobby-name name-with-rank">${rankHtml}${supporterDisplayNameInnerHtml(p.name)}${p.id === hostId ? " · host" : ""}</span>
      </div>
      <span class="lobby-ready">${p.ready ? "✔" : ""}</span>
    </div>
  `;
    })
    .join("");

  const selfReady = Boolean(
    selfId && players.some((p) => String(p.id) === String(selfId) && p.ready),
  );

  const hostSettings =
    isHost && !generating
      ? `
    <details class="lobby-settings"${settingsPanelOpen ? " open" : ""}>
      <summary class="lobby-settings-summary">
        <span>Lobby settings</span>
        <span class="lobby-settings-chevron" aria-hidden="true"></span>
      </summary>
      <div class="lobby-settings-body">
        <div class="lobby-settings-field lobby-settings-field--stack">
          <label class="arcade-label lobby-settings-label" for="cook-duration-select">Cook time</label>
          <select id="cook-duration-select" class="arcade-select lobby-settings-select" aria-label="Cook duration in minutes">
            <option value="5"${cookMin === 5 ? " selected" : ""}>5 min</option>
            <option value="10"${cookMin === 10 ? " selected" : ""}>10 min</option>
            <option value="15"${cookMin === 15 ? " selected" : ""}>15 min</option>
            <option value="20"${cookMin === 20 ? " selected" : ""}>20 min</option>
            <option value="30"${cookMin === 30 ? " selected" : ""}>30 min</option>
          </select>
        </div>
        <div class="lobby-settings-field lobby-settings-field--toggle-row">
          <span class="arcade-label lobby-settings-label" id="lobby-anon-label">Anonymous voting</span>
          <button
            type="button"
            class="lobby-toggle${anonymousVoting ? " lobby-toggle--on" : ""}"
            id="lobby-anonymous-toggle"
            role="switch"
            aria-labelledby="lobby-anon-label"
            aria-checked="${anonymousVoting ? "true" : "false"}"
          >
            <span class="lobby-toggle-track" aria-hidden="true">
              <span class="lobby-toggle-knob"></span>
            </span>
          </button>
        </div>
      </div>
    </details>
  `
      : "";

  root.innerHTML = `
    <div class="screen lobby arcade-panel">
      <h2 class="arcade-heading">LOBBY <span class="lobby-id">${escapeHtml(lobby.lobby_id || "")}</span></h2>
      <p class="arcade-hint">Spice ${lobby.spice} · ${lobby.is_public ? "Public" : "Code only"} · min 2 players · all ready · cook ${cookMin} min${
        anonymousVoting ? " · anonymous voting" : ""
      }</p>
      ${hostSettings}
      <div class="lobby-list">${rows}</div>
      <p class="arcade-error" id="lobby-err"></p>
      <div class="arcade-actions"${generating ? " hidden" : ""}>
        <button type="button" class="arcade-btn arcade-btn-primary" id="btn-ready"${
          selfReady ? " disabled" : ""
        }>READY</button>
        <button type="button" class="arcade-btn arcade-btn-secondary" id="btn-leave">Leave</button>
      </div>
      ${
        generating
          ? `
      <div class="lobby-kit-overlay" id="lobby-kit-overlay" role="status" aria-live="polite">
        <div class="lobby-kit-overlay-card">
          <p class="arcade-heading lobby-kit-overlay-title">BUILDING KIT</p>
          <p class="arcade-hint lobby-kit-overlay-msg">${escapeHtml(kitMsg)}</p>
          <div class="lobby-kit-progress" aria-hidden="true">
            <div class="lobby-kit-progress-fill" style="width:${pct}%"></div>
          </div>
          <p class="arcade-hint lobby-kit-overlay-step">${escapeHtml(stepLine)}</p>
        </div>
      </div>`
          : ""
      }
    </div>
  `;
  root.dataset.selfId = selfId;
}

/**
 * @param {object} lobby
 * @param {null | { step?: number; total?: number; message?: string; percent?: number }} kitProgress
 */
function syncLobbyContext(lobby, kitProgress) {
  const players = lobby.players || [];
  const generating = lobby.state === "generating" || kitProgress != null;
  const st = String(lobby.state ?? "");
  let phase = "In lobby";
  if (generating) phase = "Building shared kit";
  else if (st === "lobby") {
    const readyCount = players.filter((p) => p.ready).length;
    phase = `Ready ${readyCount}/${players.length}`;
  }
  setAppErrorContext({
    screen: "Lobby",
    phase,
    lobbyId: lobby.lobby_id ? String(lobby.lobby_id) : undefined,
  });
}

export function mountLobbyScreen(root, ctx) {
  const ws = ctx.mpWs;
  const playerId = ctx.playerId;
  let lobby = ctx.lobby;
  /** @type {null | { step?: number; total?: number; message?: string; percent?: number }} */
  let kitProgress = null;
  let preserveWs = false;
  let intentionalLeave = false;
  /** Persists across re-renders when the host expands/collapses settings */
  let lobbySettingsOpen = false;
  /** Set once per mount from sessionStorage vs first snapshot — stable across lobby_update paints. */
  let hideSelfRankBadge = false;
  let hideSelfRankBadgeInit = false;

  const paint = () => {
    const prevSettings = root.querySelector("details.lobby-settings");
    if (prevSettings instanceof HTMLDetailsElement) {
      lobbySettingsOpen = prevSettings.open;
    }
    if (!hideSelfRankBadgeInit) {
      const sid = String(playerId || "");
      const pl = lobby.players || [];
      const self = pl.find((p) => String(p.id) === sid);
      const key = self?.rank?.key ? String(self.rank.key).trim() : "";
      const initialLast = sessionStorage.getItem(LOBBY_LAST_SELF_RANK_KEY);
      hideSelfRankBadge =
        initialLast != null &&
        initialLast !== "" &&
        key !== "" &&
        initialLast === key;
      hideSelfRankBadgeInit = true;
    }
    renderLobby(
      root,
      lobby,
      playerId,
      kitProgress,
      lobbySettingsOpen,
      hideSelfRankBadge,
    );
    syncLobbyContext(lobby, kitProgress);
  };

  const errEl = () => root.querySelector("#lobby-err");

  if (!ws || ws.readyState !== WebSocket.OPEN) {
    import("./multiplayerHub.js").then((m) =>
      ctx.navigate(m.mountMultiplayerHubScreen),
    );
    return () => {};
  }

  mountAuthCornerLeave(ctx);
  const unmountMpChat = mountMpChat({ ws, playerId });

  const onMessage = (ev) => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    ingestMpChatMessage(m);
    if (m.type === "kit_progress") {
      kitProgress = m;
      paint();
    }
    notifyMpPlayerJoin(m, playerId);
    notifyMpPlayerLeave(m, playerId);
    notifyMpPlayerDisconnected(m, playerId);
    if (m.type === "kicked_from_lobby") {
      intentionalLeave = true;
      showKickedFromMatchToast();
      clearMpChatSession();
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      import("./modeSelect.js").then((mod) =>
        ctx.navigate(mod.mountModeSelectScreen),
      );
      return;
    }
    if (m.type === "lobby_update" && m.lobby) {
      lobby = m.lobby;
      if (m.lobby.state !== "generating") kitProgress = null;
      paint();
    }
    if (m.type === "player_ready") {
      /* lobby_update follows */
    }
    if (m.type === "error") {
      mpChatHandleErrorPayload(m);
      if (m.error_code !== "MP_CHAT_COOLDOWN") {
        const e = errEl();
        if (e) e.textContent = m.message || "Error";
      }
      notifyMpServerError(m);
    }
    if (m.type === "lobby_dissolved") {
      intentionalLeave = true;
      void navigateToMenuAfterLobbyDissolved(ctx, ws, m);
      return;
    }
    if (m.type === "start_game") {
      playSfxBeatBattle();
      preserveWs = true;
      ctx.navigate(mountCookScreen, {
        mpWs: ws,
        playerId,
        lobbyId: m.lobby_id,
        seed: m.seed,
        spice: m.spice,
        sounds: m.sounds,
        cookDurationMin: m.cook_duration_min ?? 10,
      });
    }
  };

  ws.onclose = () => {
    if (preserveWs || intentionalLeave) return;
    showServerRestartingWait();
    import("./multiplayerHub.js").then((m) =>
      ctx.navigate(m.mountMultiplayerHubScreen),
    );
  };

  ws.onmessage = onMessage;

  const changeHandler = (e) => {
    const t = e.target;
    if (!(t instanceof HTMLSelectElement) || t.id !== "cook-duration-select")
      return;
    playSfxMinor();
    if (ws.readyState !== WebSocket.OPEN) {
      const err = errEl();
      if (err) err.textContent = "Not connected.";
      return;
    }
    try {
      ws.send(
        JSON.stringify({
          type: "set_cook_duration",
          minutes: parseInt(t.value, 10),
        }),
      );
    } catch {
      /* ignore */
    }
  };

  const clickHandler = (e) => {
    const t = e.target;
    const origin =
      t instanceof Element
        ? t
        : t && "parentElement" in t
          ? t.parentElement
          : null;
    const anonBtn = origin?.closest?.("#lobby-anonymous-toggle");
    if (anonBtn instanceof HTMLButtonElement) {
      e.preventDefault();
      e.stopPropagation();
      playSfxMinor();
      if (ws.readyState !== WebSocket.OPEN) {
        const err = errEl();
        if (err) err.textContent = "Not connected.";
        return;
      }
      const next = !Boolean(lobby.anonymous_voting);
      try {
        ws.send(
          JSON.stringify({ type: "set_anonymous_voting", enabled: next }),
        );
      } catch {
        /* ignore */
      }
      return;
    }
    const kickBtn = origin?.closest?.(".lobby-kick-btn");
    if (kickBtn instanceof HTMLButtonElement) {
      const tid = kickBtn.dataset.kickId;
      if (!tid) return;
      playSfxMinor();
      if (ws.readyState !== WebSocket.OPEN) {
        const err = errEl();
        if (err) err.textContent = "Not connected.";
        return;
      }
      try {
        ws.send(JSON.stringify({ type: "kick_player", target_player_id: tid }));
      } catch {
        /* ignore */
      }
      return;
    }
    const btn = origin?.closest?.("#btn-ready, #btn-leave");
    if (!btn) return;

    if (btn.id === "btn-ready") {
      const pl = lobby.players || [];
      if (pl.some((p) => String(p.id) === String(playerId) && p.ready)) return;
      if (/** @type {HTMLButtonElement} */ (btn).disabled) return;
      playSfxMajor();
      if (ws.readyState !== WebSocket.OPEN) {
        const err = errEl();
        if (err) err.textContent = "Not connected. Leave and rejoin.";
        return;
      }
      try {
        ws.send(JSON.stringify({ type: "player_ready" }));
      } catch {
        /* keep button usable — lobby state unchanged */
      }
      return;
    }

    if (btn.id === "btn-leave") {
      playSfxMinor();
      intentionalLeave = true;
      clearMpChatSession();
      try {
        ws.send(JSON.stringify({ type: "leave_lobby" }));
      } catch {
        /* ignore */
      }
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      import("./modeSelect.js").then((mod) =>
        ctx.navigate(mod.mountModeSelectScreen),
      );
    }
  };
  const toggleHandler = (e) => {
    const t = e.target;
    if (
      !(t instanceof HTMLDetailsElement) ||
      !t.classList.contains("lobby-settings")
    )
      return;
    if (!t.isConnected) return;
    lobbySettingsOpen = t.open;
  };

  root.addEventListener("click", clickHandler);
  root.addEventListener("change", changeHandler);
  root.addEventListener("toggle", toggleHandler);

  paint();

  return () => {
    intentionalLeave = true;
    const sid = String(playerId || "");
    const pl = lobby.players || [];
    const self = pl.find((p) => String(p.id) === sid);
    const rk = self?.rank?.key ? String(self.rank.key).trim() : "";
    if (rk) {
      try {
        sessionStorage.setItem(LOBBY_LAST_SELF_RANK_KEY, rk);
      } catch {
        /* ignore */
      }
    }
    if (!preserveWs) {
      clearMpChatSession();
    }
    unmountMpChat();
    root.removeEventListener("click", clickHandler);
    root.removeEventListener("change", changeHandler);
    root.removeEventListener("toggle", toggleHandler);
    root.innerHTML = "";
    ws.onclose = null;
    if (!preserveWs) {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    }
  };
}
