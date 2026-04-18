/**
 * Cook phase: kit grid, download, server timer counting down.
 */
import { authHeaders, fetchMe } from "../authApi.js";
import { RANK_BASELINE_KEY } from "../rankUi.js";
import { getApiBase } from "../apiOrigin.js";
import { mountAuthCornerLeave } from "../authCorner.js";
import {
  notifyMpServerError,
  setAppErrorContext,
  showAppError,
} from "../errorToast.js";
import {
  dismissServerRestartingWait,
  showServerRestartingWait,
} from "../serverRestartOverlay.js";
import { applyMatchResyncFromPayload } from "../mpMatchResync.js";
import { runMpWsReconnect } from "../mpReconnect.js";
import { saveMpSeat } from "../mpSeatStorage.js";
import {
  navigateToMenuAfterLobbyDissolved,
  notifyMpPlayerDisconnected,
  notifyMpPlayerJoin,
  notifyMpPlayerLeave,
} from "../mpPresenceToast.js";
import { playSfxMinor } from "../sfx.js";
import { getVolume } from "../volume.js";
import {
  fetchKitManifest,
  KIT_SOUND_FILE_EXT,
  loadDrumKitBase64Parallel,
  loadSynthBuffersAndMp3Base64Parallel,
  SYNTH_KEYS,
} from "../kitFromSeed.js";
import { runSynthReveal } from "../synthReveal.js";
import { mountKitLayoutShell } from "../kitGridLayout.js";
import {
  applyMatchWsToLobby,
  lobbyLikeFromMatchSync,
  normalizeLobbyLike,
  phaseTimerRowHtml,
  syncMatchProgressHint,
  updatePhaseTimerBar,
} from "../mpMatchRoster.js";
import { fetchMatchSync, pollMatchSync } from "../mpMatchSync.js";
import {
  ingestMpChatMessage,
  mountMpChat,
  mpChatHandleErrorPayload,
} from "../mpChat.js";
import { mountUploadScreen } from "./upload.js";
import { mountVotingSlideshowScreen } from "./votingSlideshow.js";
import { mountResultsScreen } from "./results.js";

const SOUND_KEYS = [
  "snare",
  "clap",
  "hihat",
  "open_hat",
  "808",
  "perc",
  "fx",
  "vox",
  "synth1",
  "synth2",
  "synth3",
  "kick",
];

function base64ToAudioSrc(b64) {
  return "data:audio/ogg;base64," + b64;
}

function getJSZip() {
  const g = globalThis;
  if (g.JSZip) return g.JSZip;
  throw new Error("JSZip not loaded");
}

function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function downloadKitZip(sounds) {
  const JSZip = getJSZip();
  const zip = new JSZip();
  const folder = zip.folder("beat_battle_kit");
  if (!folder) return;
  for (const key of SOUND_KEYS) {
    const b64 = sounds[key];
    if (!b64) continue;
    folder.file(`${key}.${KIT_SOUND_FILE_EXT}`, base64ToBytes(b64), {
      binary: true,
    });
  }
  const blob = await zip.generateAsync({ type: "blob" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "beat_battle_kit.zip";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function downloadOneSound(key, b64) {
  if (!b64) return;
  const blob = new Blob([base64ToBytes(b64)], { type: "audio/ogg" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${key}.${KIT_SOUND_FILE_EXT}`;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function getWaveSurfer() {
  const g = globalThis;
  if (g.WaveSurfer && typeof g.WaveSurfer.create === "function")
    return g.WaveSurfer;
  throw new Error("WaveSurfer not loaded");
}

function kitNeedsFetch(sounds) {
  if (!sounds || typeof sounds !== "object") return true;
  return !SOUND_KEYS.every((k) => Boolean(sounds[k]));
}

/**
 * Full kit row from GET /api/lobby/:id/kit (seed/spice + match phase for recovery).
 * @param {object} ctx
 */
async function fetchLobbyKitMeta(ctx) {
  const res = await fetch(
    `${getApiBase()}/api/lobby/${encodeURIComponent(String(ctx.lobbyId))}/kit`,
    { headers: authHeaders() },
  );
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  const data = await res.json();
  if (data.seed == null || data.spice == null)
    throw new Error("Invalid kit metadata.");
  const ur = data.upload_deadline_ts;
  const vu = data.votes_unlock_at;
  const vc = data.votes_close_at;
  return {
    seed: Number(data.seed),
    spice: Number(data.spice),
    matchState: data.match_state != null ? String(data.match_state) : null,
    cookRemainingS:
      data.cook_remaining_s != null &&
      Number.isFinite(Number(data.cook_remaining_s))
        ? Math.max(0, Number(data.cook_remaining_s))
        : null,
    uploadDeadlineTs:
      ur != null && Number.isFinite(Number(ur)) ? Number(ur) : null,
    beats: Array.isArray(data.beats) ? data.beats : null,
    votesUnlockAt:
      vu != null && Number.isFinite(Number(vu)) ? Number(vu) : undefined,
    votesCloseAt:
      vc != null && Number.isFinite(Number(vc)) ? Number(vc) : undefined,
    results:
      data.results && typeof data.results === "object" ? data.results : null,
  };
}

/**
 * If the match already left the cook phase, navigate and return true.
 * @param {object} ctx
 * @param {WebSocket} ws
 * @param {Awaited<ReturnType<typeof fetchLobbyKitMeta>>} meta
 */
function tryNavigatePastCookPhase(ctx, ws, meta) {
  const st = meta.matchState;
  if (st === "upload" && meta.uploadDeadlineTs != null) {
    ctx.navigate(mountUploadScreen, {
      mpWs: ws,
      playerId: ctx.playerId,
      lobbyId: ctx.lobbyId,
      uploadDeadlineTs: meta.uploadDeadlineTs,
    });
    return true;
  }
  if (st === "voting") {
    ctx.navigate(mountVotingSlideshowScreen, {
      mpWs: ws,
      playerId: ctx.playerId,
      lobbyId: ctx.lobbyId,
      beats: meta.beats || [],
      votesUnlockAt: meta.votesUnlockAt,
      votesCloseAt: meta.votesCloseAt,
    });
    return true;
  }
  if (st === "results" && meta.results) {
    ctx.navigate(mountResultsScreen, {
      mpWs: ws,
      playerId: ctx.playerId,
      results: meta.results,
    });
    return true;
  }
  return false;
}

/**
 * @param {HTMLElement} root
 * @param {object} ctx
 * @param {(sounds: Record<string, string>) => void} start
 * @param {{ getCancelled: () => boolean; skipSynthReveal?: boolean }} opts
 */
async function buildKitClientSide(root, ctx, start, opts) {
  const meta = await fetchLobbyKitMeta(ctx);
  if (opts.getCancelled()) return;
  if (tryNavigatePastCookPhase(ctx, ctx.mpWs, meta)) return;

  const { seed, spice } = meta;
  const apiBase = getApiBase();
  const ac = new AudioContext({ sampleRate: 44100 });
  let drumsPending = true;

  root.innerHTML = `
    <div class="screen cook arcade-panel screen--vert-center">
      <p class="arcade-status" id="cook-load">Loading kit…</p>
    </div>`;
  const loadEl = root.querySelector("#cook-load");

  let stopPhasePoll = () => {};
  stopPhasePoll = pollMatchSync(
    String(ctx.lobbyId),
    (sync) => {
      if (opts.getCancelled()) return;
      const st = sync.match_state != null ? String(sync.match_state) : "";
      const ur = sync.upload_deadline_ts;
      if (st === "upload" && ur != null && Number.isFinite(Number(ur))) {
        ctx.navigate(mountUploadScreen, {
          mpWs: ctx.mpWs,
          playerId: ctx.playerId,
          lobbyId: ctx.lobbyId,
          uploadDeadlineTs: Number(ur),
        });
      } else if (st === "voting") {
        ctx.navigate(mountVotingSlideshowScreen, {
          mpWs: ctx.mpWs,
          playerId: ctx.playerId,
          lobbyId: ctx.lobbyId,
          beats: Array.isArray(sync.beats) ? sync.beats : [],
          votesUnlockAt: sync.votes_unlock_at,
          votesCloseAt: sync.votes_close_at,
        });
      } else if (
        st === "results" &&
        sync.results &&
        typeof sync.results === "object"
      ) {
        ctx.navigate(mountResultsScreen, {
          mpWs: ctx.mpWs,
          playerId: ctx.playerId,
          results: sync.results,
        });
      }
    },
    4500,
  );

  try {
    const manifest = await fetchKitManifest(apiBase);
    if (opts.getCancelled()) {
      stopPhasePoll();
      return;
    }
    const drumPromise = loadDrumKitBase64Parallel({
      seed,
      spice,
      apiBase,
      manifest,
      onProgress: ({ step, total }) => {
        if (loadEl) loadEl.textContent = `Loading kit ${step} / ${total}…`;
      },
    }).finally(() => {
      drumsPending = false;
    });

    const { buffers: synthBuffers, base64: synthB64 } =
      await loadSynthBuffersAndMp3Base64Parallel({
        seed,
        spice,
        apiBase,
        audioContext: ac,
        manifest,
      });

    if (opts.getCancelled()) {
      stopPhasePoll();
      return;
    }
    if (loadEl) loadEl.textContent = "";

    if (!opts.skipSynthReveal) {
      await runSynthReveal(ac, synthBuffers, () => drumsPending);
    }
    if (opts.getCancelled()) {
      stopPhasePoll();
      return;
    }

    const drumSounds = await drumPromise;
    if (opts.getCancelled()) {
      stopPhasePoll();
      return;
    }
    const sounds = { ...drumSounds, ...synthB64 };
    await ac.close().catch(() => {});
    stopPhasePoll();
    start(sounds);
  } catch (e) {
    stopPhasePoll();
    await ac.close().catch(() => {});
    throw e;
  }
}

/**
 * @param {HTMLElement} root
 * @param {object} ctx
 * @param {Record<string, string>} sounds
 * @param {{
 *   initialCookRemainingS?: number | null;
 *   onBridgeDetach?: () => void;
 *   unmountFlags?: { teardownClose: boolean };
 * }} [phaseOpts]
 * @returns {() => void}
 */
function setupCookUI(root, ctx, sounds, phaseOpts) {
  phaseOpts?.onBridgeDetach?.();
  const unmountFlags = phaseOpts?.unmountFlags;
  const playerId = ctx.playerId;
  const lobbyId = ctx.lobbyId;
  const cookMin = Number(ctx.cookDurationMin) || 10;
  let remaining = cookMin * 60;
  /** When cook ends (server nudges this via timer_update; we redraw every second). */
  let cookEndAtMs = Date.now() + remaining * 1000;
  const initRs = phaseOpts?.initialCookRemainingS;
  if (initRs != null && Number.isFinite(initRs)) {
    remaining = Math.max(0, Math.floor(initRs));
    cookEndAtMs = Date.now() + remaining * 1000;
  }
  let preserveWs = false;
  let selfFinished = false;
  /** @type {ReturnType<typeof setInterval> | null} */
  let localTimerId = null;

  const waveSurfers = new Map();
  const clickFullPlayback = new Map();

  const destroyWaveSurfers = () => {
    waveSurfers.forEach((wsur) => {
      try {
        wsur.destroy();
      } catch {
        /* ignore */
      }
    });
    waveSurfers.clear();
  };

  mountAuthCornerLeave(ctx);
  let unmountMpChat = mountMpChat({
    ws: ctx.mpWs,
    getWs: () => ctx.mpWs,
    playerId,
    continueSession: true,
  });

  root.innerHTML = `
    <div class="screen cook arcade-panel">
      <div class="mp-panel-head">
        <h2 class="arcade-heading mp-panel-head-title">COOK!</h2>
        <div class="mp-panel-head-timer">${phaseTimerRowHtml("mp-cook-phase")}</div>
        <div class="mp-panel-head-roster">
          <span id="mp-corner-cook" class="mp-progress-hint-wrap hidden" aria-live="polite"></span>
        </div>
      </div>
      <p class="arcade-hint cook-connection-hint hidden" id="cook-connection-hint">Connection lost — timer may not match the server.</p>
      <div class="cook-download-row">
        <button type="button" class="arcade-btn arcade-btn-secondary cook-action-btn" id="mp-download-kit">Download all</button>
        <button type="button" class="arcade-btn arcade-btn-primary cook-action-btn" id="mp-finished">Finished</button>
      </div>
      <div id="mp-sound-grid" class="kit-layout mp-grid" aria-label="Match kit"></div>
    </div>
  `;

  const grid = root.querySelector("#mp-sound-grid");
  const cookTotalSec = Math.max(1, cookMin * 60);
  /** @type {ReturnType<typeof normalizeLobbyLike>} */
  let lobbyView = normalizeLobbyLike({});
  const syncProgressHint = () =>
    syncMatchProgressHint(root, "mp-corner-cook", "cook", lobbyView);

  void (async () => {
    const sync = await fetchMatchSync(String(lobbyId));
    const L = lobbyLikeFromMatchSync(sync);
    if (L) {
      lobbyView = normalizeLobbyLike(L);
      syncProgressHint();
      syncSelfCookFinishedFromLobby();
    }
  })();

  const stopLobbySyncPoll = pollMatchSync(
    String(lobbyId),
    (sync) => {
      const L = lobbyLikeFromMatchSync(sync);
      if (L) {
        lobbyView = normalizeLobbyLike(L);
        syncProgressHint();
        syncSelfCookFinishedFromLobby();
      }
    },
    5000,
  );

  const bindWaveformPlayback = (key, waveWrap, audio) => {
    const setClickFull = (v) => clickFullPlayback.set(key, v);
    audio.addEventListener("ended", () => setClickFull(false));
    waveWrap.addEventListener("click", (e) => {
      e.preventDefault();
      if (!audio.src) return;
      setClickFull(true);
      audio.currentTime = 0;
      audio.play().catch(() => {});
    });
    waveWrap.addEventListener("mouseenter", () => {
      if (!audio.src) return;
      if (clickFullPlayback.get(key)) return;
      audio.currentTime = 0;
      audio.play().catch(() => {});
    });
    waveWrap.addEventListener("mouseleave", () => {
      if (clickFullPlayback.get(key)) return;
      audio.pause();
      audio.currentTime = 0;
    });
  };

  const renderWaveform = (key, dataUrl) => {
    const container = grid?.querySelector(`#mp-wave-${key}`);
    if (!container) return;
    const prev = waveSurfers.get(key);
    if (prev) {
      try {
        prev.destroy();
      } catch {
        /* ignore */
      }
      waveSurfers.delete(key);
    }
    container.innerHTML = "";
    container.classList.remove("empty");
    const WaveSurfer = getWaveSurfer();
    const waveH = Math.max(68, Math.floor(container.clientHeight) || 68);
    const wsur = WaveSurfer.create({
      container,
      height: waveH,
      waveColor: "#b01010",
      progressColor: "#ffffff",
      cursorWidth: 0,
      interact: false,
      url: dataUrl,
    });
    waveSurfers.set(key, wsur);
  };

  const appendCookCard = (key) => {
    const card = document.createElement("article");
    card.className = "card";
    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("h2");
    title.className = "card-title";
    title.textContent = key.replace(/_/g, " ");
    const dl = document.createElement("button");
    dl.type = "button";
    dl.className = "card-download";
    dl.dataset.soundKey = key;
    dl.setAttribute("aria-label", `Download ${key}`);
    dl.textContent = "↓";
    head.append(title, dl);
    const waveWrap = document.createElement("div");
    waveWrap.className = "waveform-wrap empty";
    waveWrap.id = `mp-wave-${key}`;
    waveWrap.textContent = "—";
    const audio = document.createElement("audio");
    audio.id = `mp-audio-${key}`;
    audio.preload = "auto";
    audio.volume = getVolume();
    bindWaveformPlayback(key, waveWrap, audio);
    card.append(head, waveWrap, audio);
    return card;
  };

  if (grid)
    mountKitLayoutShell(grid, {
      synthKeys: SYNTH_KEYS,
      appendCard: appendCookCard,
    });

  clickFullPlayback.clear();
  SOUND_KEYS.forEach((key) => {
    const b64 = sounds[key];
    if (!b64) return;
    const src = base64ToAudioSrc(b64);
    const audio = grid?.querySelector(`#mp-audio-${key}`);
    if (audio && audio instanceof HTMLAudioElement) audio.src = src;
  });

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      SOUND_KEYS.forEach((key) => {
        const b64 = sounds[key];
        if (!b64) return;
        renderWaveform(key, base64ToAudioSrc(b64));
      });
    });
  });

  const syncRemainingFromDeadline = () => {
    remaining = Math.max(0, Math.ceil((cookEndAtMs - Date.now()) / 1000));
    updatePhaseTimerBar(root, "mp-cook-phase", cookTotalSec, remaining);
  };

  syncRemainingFromDeadline();
  localTimerId = window.setInterval(syncRemainingFromDeadline, 1000);

  root.querySelector("#mp-download-kit")?.addEventListener("click", () => {
    playSfxMinor();
    downloadKitZip(sounds).catch(() => {});
  });

  const finishedBtn = root.querySelector("#mp-finished");
  const setFinishedUi = () => {
    if (finishedBtn instanceof HTMLButtonElement) {
      finishedBtn.disabled = selfFinished;
      finishedBtn.textContent = selfFinished ? "Done" : "Finished";
    }
  };

  const syncSelfCookFinishedFromLobby = () => {
    if (!lobbyView.cook_finished.some((id) => String(id) === String(playerId)))
      return;
    if (selfFinished) return;
    selfFinished = true;
    setFinishedUi();
    syncProgressHint();
  };

  setFinishedUi();

  finishedBtn?.addEventListener("click", () => {
    if (selfFinished) return;
    playSfxMinor();
    selfFinished = true;
    setFinishedUi();
    syncProgressHint();
    try {
      ctx.mpWs.send(JSON.stringify({ type: "cook_finished" }));
    } catch {
      /* ignore */
    }
  });

  grid?.addEventListener("click", (e) => {
    const btn =
      e.target instanceof Element ? e.target.closest(".card-download") : null;
    if (!(btn instanceof HTMLButtonElement)) return;
    e.preventDefault();
    e.stopPropagation();
    const key = btn.dataset.soundKey;
    if (!key || !sounds[key]) return;
    playSfxMinor();
    downloadOneSound(key, sounds[key]);
  });

  const prevOnClose = ctx.mpWs.onclose;

  const onMessage = async (ev) => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (m.type === "connected") return;
    if (m.type === "match_resync") {
      const navigated = await applyMatchResyncFromPayload(ctx, m, "cook");
      if (
        !navigated &&
        Array.isArray(m.cook_finished) &&
        m.cook_finished.length > 0
      ) {
        lobbyView = applyMatchWsToLobby(lobbyView, {
          type: "cook_finished_update",
          finished_player_ids: m.cook_finished,
          player_count: m.player_count,
        });
        syncProgressHint();
        syncSelfCookFinishedFromLobby();
      }
      return;
    }
    ingestMpChatMessage(m);
    if (m.type === "lobby_dissolved") {
      preserveWs = true;
      void navigateToMenuAfterLobbyDissolved(ctx, ctx.mpWs, m);
      return;
    }
    notifyMpPlayerJoin(m, playerId);
    notifyMpPlayerLeave(m, playerId);
    notifyMpPlayerDisconnected(m, playerId);
    if (m.type === "error") {
      mpChatHandleErrorPayload(m);
      notifyMpServerError(m);
    }
    if (m.type === "timer_update" && m.phase === "cooking") {
      const rs = m.remaining_s;
      if (rs != null && Number.isFinite(Number(rs))) {
        cookEndAtMs = Date.now() + Math.max(0, Number(rs)) * 1000;
      }
      syncRemainingFromDeadline();
    }
    if (
      m.type === "lobby_update" ||
      m.type === "cook_finished_update" ||
      m.type === "beat_uploaded" ||
      m.type === "vote_cast"
    ) {
      lobbyView = applyMatchWsToLobby(lobbyView, m);
      syncProgressHint();
      syncSelfCookFinishedFromLobby();
    }
    if (m.type === "upload_phase_start") {
      preserveWs = true;
      destroyWaveSurfers();
      ctx.navigate(mountUploadScreen, {
        mpWs: ctx.mpWs,
        playerId,
        lobbyId,
        uploadDeadlineTs: m.upload_deadline_ts,
      });
    }
  };

  const onCookSocketClose = (ev) => {
    if (unmountFlags?.teardownClose) return;
    if (!preserveWs) {
      showServerRestartingWait();
    }
    const hint = root.querySelector("#cook-connection-hint");
    if (hint) hint.classList.remove("hidden");
    if (typeof prevOnClose === "function") {
      try {
        prevOnClose();
      } catch {
        /* ignore */
      }
    }
    if (preserveWs) return;
    dismissServerRestartingWait();
    void runMpWsReconnect(ev, {
      ctx,
      intentionalLeave: () => Boolean(unmountFlags?.teardownClose),
      preserveWs: () => preserveWs,
      onReplaceSocket: (nw) => {
        ctx.mpWs = nw;
        unmountMpChat();
        unmountMpChat = mountMpChat({
          ws: nw,
          getWs: () => ctx.mpWs,
          playerId,
          continueSession: true,
        });
        nw.onmessage = onMessage;
        nw.addEventListener("close", onCookSocketClose, { once: true });
      },
    });
  };

  ctx.mpWs.addEventListener("close", onCookSocketClose, { once: true });

  ctx.mpWs.onmessage = onMessage;

  return () => {
    if (unmountFlags) unmountFlags.teardownClose = true;
    stopLobbySyncPoll();
    unmountMpChat();
    if (localTimerId != null) {
      clearInterval(localTimerId);
      localTimerId = null;
    }
    ctx.mpWs.onclose = prevOnClose ?? null;
    try {
      ctx.mpWs.removeEventListener("close", onCookSocketClose);
    } catch {
      /* ignore */
    }
    destroyWaveSurfers();
    root.innerHTML = "";
    if (!preserveWs) {
      try {
        ctx.mpWs.close();
      } catch {
        /* ignore */
      }
    }
  };
}

export function mountCookScreen(root, ctx) {
  setAppErrorContext({ screen: "Cook", phase: "Cook phase — kit and timer" });
  const ws = ctx.mpWs;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    import("./multiplayerHub.js").then((m) =>
      ctx.navigate(m.mountMultiplayerHubScreen),
    );
    return () => {};
  }

  const lid0 = String(ctx.lobbyId || "").trim();
  const pid0 = String(ctx.playerId || "").trim();
  if (lid0 && pid0) saveMpSeat(lid0, pid0);

  void fetchMe()
    .then((me) =>
      sessionStorage.setItem(RANK_BASELINE_KEY, String(me.rank_index ?? 0)),
    )
    .catch(() => {});

  let cancelled = false;
  let innerCleanup = () => {};
  /** Seconds left while we're still fetching stems — timer_update hammers this in. */
  const pending = { lastCookRemainingS: /** @type {number | null} */ null };
  let bridgeActive = true;
  const unmountFlags = { teardownClose: false };

  const bridgeOnMessage = async (ev) => {
    if (!bridgeActive) return;
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (m.type === "connected") return;
    if (m.type === "match_resync") {
      await applyMatchResyncFromPayload(ctx, m, "cook");
      return;
    }
    ingestMpChatMessage(m);
    if (m.type === "lobby_dissolved") {
      bridgeActive = false;
      void navigateToMenuAfterLobbyDissolved(ctx, ctx.mpWs, m);
      return;
    }
    notifyMpPlayerJoin(m, ctx.playerId);
    notifyMpPlayerLeave(m, ctx.playerId);
    notifyMpPlayerDisconnected(m, ctx.playerId);
    if (m.type === "error") {
      mpChatHandleErrorPayload(m);
      notifyMpServerError(m);
    }
    if (m.type === "timer_update" && m.phase === "cooking") {
      const rs = m.remaining_s;
      if (rs != null && Number.isFinite(Number(rs))) {
        pending.lastCookRemainingS = Math.max(0, Number(rs));
      }
    }
    if (m.type === "upload_phase_start") {
      cancelled = true;
      bridgeActive = false;
      ctx.navigate(mountUploadScreen, {
        mpWs: ctx.mpWs,
        playerId: ctx.playerId,
        lobbyId: ctx.lobbyId,
        uploadDeadlineTs: m.upload_deadline_ts,
      });
    }
  };

  const bridgePrevOnClose = ctx.mpWs.onclose;
  const onBridgeSocketClose = (ev) => {
    if (typeof bridgePrevOnClose === "function") {
      try {
        bridgePrevOnClose();
      } catch {
        /* ignore */
      }
    }
    if (!bridgeActive || cancelled || unmountFlags.teardownClose) return;
    showServerRestartingWait();
    dismissServerRestartingWait();
    void runMpWsReconnect(ev, {
      ctx,
      intentionalLeave: () => cancelled || unmountFlags.teardownClose,
      preserveWs: () => !bridgeActive,
      onReplaceSocket: (nw) => {
        ctx.mpWs = nw;
        nw.onmessage = bridgeOnMessage;
        nw.addEventListener("close", onBridgeSocketClose, { once: true });
      },
    });
  };
  ctx.mpWs.addEventListener("close", onBridgeSocketClose, { once: true });
  ctx.mpWs.onmessage = bridgeOnMessage;

  const start = (/** @type {Record<string, string>} */ sounds) => {
    if (cancelled) return;
    bridgeActive = false;
    const initial = pending.lastCookRemainingS;
    pending.lastCookRemainingS = null;
    innerCleanup = setupCookUI(root, ctx, sounds, {
      initialCookRemainingS: initial,
      onBridgeDetach: () => {},
      unmountFlags,
    });
  };

  if (kitNeedsFetch(ctx.sounds)) {
    mountAuthCornerLeave(ctx);
    void (async () => {
      try {
        await buildKitClientSide(root, ctx, start, {
          getCancelled: () => cancelled,
          skipSynthReveal: Boolean(ctx.skipSynthReveal),
        });
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : "Unknown error";
          showAppError({
            message: `Could not build the sound kit (${msg}).`,
            hint: "Try leaving and rejoining the match, or refresh if this repeats.",
            errorCode: "KIT_CLIENT",
          });
          root.innerHTML = `
          <div class="screen cook arcade-panel screen--vert-center">
            <p class="arcade-error">Could not load kit.</p>
            <p class="arcade-hint" id="cook-err-detail"></p>
          </div>`;
          const det = root.querySelector("#cook-err-detail");
          if (det) det.textContent = msg;
        }
      }
    })();
    return () => {
      unmountFlags.teardownClose = true;
      cancelled = true;
      bridgeActive = false;
      try {
        ctx.mpWs.removeEventListener("close", onBridgeSocketClose);
      } catch {
        /* ignore */
      }
      innerCleanup();
      root.innerHTML = "";
    };
  }

  void (async () => {
    try {
      const meta = await fetchLobbyKitMeta(ctx);
      if (cancelled) return;
      if (tryNavigatePastCookPhase(ctx, ctx.mpWs, meta)) {
        cancelled = true;
        bridgeActive = false;
        return;
      }
      if (meta.cookRemainingS != null && Number.isFinite(meta.cookRemainingS)) {
        pending.lastCookRemainingS = meta.cookRemainingS;
      }
      start(ctx.sounds);
    } catch (e) {
      if (!cancelled) {
        const msg = e instanceof Error ? e.message : "Unknown error";
        showAppError({
          message: `Could not sync your cook session: ${msg}`,
          hint: "You were sent back to the multiplayer menu. Try rejoining if the match is still open.",
          errorCode: "KIT_SYNC",
        });
        import("./multiplayerHub.js").then((m) =>
          ctx.navigate(m.mountMultiplayerHubScreen),
        );
      }
    }
  })();

  return () => {
    unmountFlags.teardownClose = true;
    cancelled = true;
    bridgeActive = false;
    try {
      ctx.mpWs.removeEventListener("close", onBridgeSocketClose);
    } catch {
      /* ignore */
    }
    innerCleanup();
    root.innerHTML = "";
  };
}
