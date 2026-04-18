/**
 * Voting round: each beat plays (capped at 45s), waveform matches what you hear.
 */
import { authHeadersMultipart } from "../authApi.js";
import { mountAuthCornerLeave } from "../authCorner.js";
import { notifyMpServerError, setAppErrorContext } from "../errorToast.js";
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
import {
  ingestMpChatMessage,
  mountMpChat,
  mpChatHandleErrorPayload,
} from "../mpChat.js";
import { supporterDisplayNameInnerHtml } from "../supporters.js";
import {
  applyMatchWsToLobby,
  lobbyLikeFromMatchSync,
  normalizeLobbyLike,
  phaseTimerRowHtml,
  progressHintSlotHtml,
  syncMatchProgressHint,
  updatePhaseTimerBar,
} from "../mpMatchRoster.js";
import { fetchMatchSync, pollMatchSync } from "../mpMatchSync.js";
import { playSfxMinor } from "../sfx.js";
import { getVolume } from "../volume.js";
import { mountResultsScreen } from "./results.js";
import { mountVoteSelectionScreen } from "./voteSelection.js";

/** @type {Record<string, string>} */
const BEAT_REACTION_EMOJI = {
  fire: "🔥",
  thumbs_up: "👍",
  thumbs_down: "👎",
  hundred: "💯",
};

const BEAT_REACTION_KEYS = Object.keys(BEAT_REACTION_EMOJI);

/** @type {Record<string, string>} */
const BEAT_REACTION_ARIA = {
  fire: "Fire",
  thumbs_up: "Thumbs up",
  thumbs_down: "Thumbs down",
  hundred: "One hundred",
};

const CLIP_MAX_SEC = 45;

/** Fallback if server omits `votes_close_at` (matches backend `VOTING_COLLECT_S`). */
const VOTE_COLLECT_FALLBACK_S = 60;

/** Don't spam reactions — server would yell anyway. */
const BEAT_REACTION_COOLDOWN_MS = 3000;

const BEAT_TOAST_VISIBLE_MS = 1000;
const BEAT_TOAST_FADE_MS = 200;
const BEAT_TOAST_HOST_ID = "beat-reaction-toast-host";

function getWaveSurfer() {
  const g = globalThis;
  if (g.WaveSurfer && typeof g.WaveSurfer.create === "function")
    return g.WaveSurfer;
  throw new Error("WaveSurfer not loaded");
}

/**
 * First `maxSec` seconds only (same sample rate / channels).
 * @param {AudioContext} ac
 * @param {AudioBuffer} input
 * @param {number} maxSec
 */
function clipAudioBuffer(ac, input, maxSec) {
  const sr = input.sampleRate;
  const maxFrames = Math.min(input.length, Math.floor(maxSec * sr));
  if (maxFrames <= 0) throw new Error("Empty audio.");
  const out = ac.createBuffer(input.numberOfChannels, maxFrames, sr);
  for (let c = 0; c < input.numberOfChannels; c++) {
    out.copyToChannel(input.getChannelData(c).subarray(0, maxFrames), c);
  }
  return out;
}

/** Tiny WAV blob so WaveSurfer is happy. */
function audioBufferToWav(buffer) {
  const numChannels = buffer.numberOfChannels;
  const sampleRate = buffer.sampleRate;
  const numSamples = buffer.length;
  const bytesPerSample = 2;
  const blockAlign = numChannels * bytesPerSample;
  const dataSize = numSamples * blockAlign;
  const wav = new ArrayBuffer(44 + dataSize);
  const view = new DataView(wav);
  const writeStr = (off, str) => {
    for (let i = 0; i < str.length; i++)
      view.setUint8(off + i, str.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, dataSize, true);
  let o = 44;
  for (let i = 0; i < numSamples; i++) {
    for (let ch = 0; ch < numChannels; ch++) {
      const s = Math.max(-1, Math.min(1, buffer.getChannelData(ch)[i]));
      view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      o += 2;
    }
  }
  return new Blob([wav], { type: "audio/wav" });
}

/**
 * Decode fetched audio, clip to CLIP_MAX_SEC, return WAV blob for URL.
 * @param {Blob} blob
 */
async function blobToClippedWav(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  const ac = new AudioContext();
  try {
    const decoded = await ac.decodeAudioData(arrayBuffer.slice(0));
    const clipped = clipAudioBuffer(ac, decoded, CLIP_MAX_SEC);
    return audioBufferToWav(clipped);
  } finally {
    await ac.close().catch(() => {});
  }
}

export function mountVotingSlideshowScreen(root, ctx) {
  mountAuthCornerLeave(ctx);
  setAppErrorContext({ screen: "Listen & vote", phase: "Beat slideshow" });

  const playerId = ctx.playerId;
  const lobbyId = ctx.lobbyId;
  const beats = ctx.beats || [];
  let votesUnlockWall =
    typeof ctx.votesUnlockAt === "number" && Number.isFinite(ctx.votesUnlockAt)
      ? ctx.votesUnlockAt
      : null;
  let votesCloseAt =
    typeof ctx.votesCloseAt === "number" && Number.isFinite(ctx.votesCloseAt)
      ? ctx.votesCloseAt
      : votesUnlockWall != null
        ? votesUnlockWall + VOTE_COLLECT_FALLBACK_S
        : null;
  let preserveWs = false;
  let resultsPollNav = false;
  /** We killed the socket on purpose — don't flash "server restarting". */
  let teardownClose = false;
  let activeWsur = null;
  let idx = 0;
  /** @type {string | null} */
  let slideObjectUrl = null;
  /** @type {string | null} */
  let currentBeatOwnerId = null;

  const lid0 = String(lobbyId || "").trim();
  const pid0 = String(playerId || "").trim();
  if (lid0 && pid0) saveMpSeat(lid0, pid0);

  let unmountMpChat =
    ctx.mpWs instanceof WebSocket
      ? mountMpChat({
          ws: ctx.mpWs,
          getWs: () => ctx.mpWs,
          playerId,
          continueSession: true,
        })
      : () => {};

  /** @type {ReturnType<typeof normalizeLobbyLike>} */
  let lobbyView = normalizeLobbyLike({});
  const syncProgressHint = () =>
    syncMatchProgressHint(root, "mp-corner-slide", "vote", lobbyView);

  void (async () => {
    const sync = await fetchMatchSync(String(lobbyId));
    if (sync && String(sync.match_state) === "voting") {
      const vu = sync.votes_unlock_at;
      if (typeof vu === "number" && Number.isFinite(vu)) votesUnlockWall = vu;
      const vc = sync.votes_close_at;
      if (typeof vc === "number" && Number.isFinite(vc)) votesCloseAt = vc;
    }
    const L = lobbyLikeFromMatchSync(sync);
    if (L && Array.isArray(L.players) && L.players.length) {
      lobbyView = normalizeLobbyLike(L);
      syncProgressHint();
    }
  })();

  const voteCollectWindowS = () =>
    votesCloseAt != null &&
    votesUnlockWall != null &&
    votesCloseAt > votesUnlockWall
      ? votesCloseAt - votesUnlockWall
      : VOTE_COLLECT_FALLBACK_S;

  const slideMountSec = Date.now() / 1000;

  const stopResultsPoll = pollMatchSync(
    String(lobbyId),
    (sync) => {
      if (!resultsPollNav && !preserveWs) {
        const L = lobbyLikeFromMatchSync(sync);
        if (L && Array.isArray(L.players) && L.players.length) {
          lobbyView = normalizeLobbyLike(L);
          syncProgressHint();
        }
      }
      if (String(sync.match_state) === "voting") {
        const vu = sync.votes_unlock_at;
        if (typeof vu === "number" && Number.isFinite(vu)) votesUnlockWall = vu;
        const vc = sync.votes_close_at;
        if (typeof vc === "number" && Number.isFinite(vc)) votesCloseAt = vc;
        tickSlideDeadline();
      }
      if (resultsPollNav || preserveWs) return;
      if (
        String(sync.match_state) !== "results" ||
        !sync.results ||
        typeof sync.results !== "object"
      ) {
        return;
      }
      resultsPollNav = true;
      preserveWs = true;
      stopResultsPoll();
      ctx.navigate(mountResultsScreen, {
        mpWs: ctx.mpWs,
        results: sync.results,
        playerId,
      });
    },
    4500,
  );

  root.innerHTML = `
    <div class="screen slideshow arcade-panel">
      <div class="mp-panel-head mp-panel-head--slideshow">
        <h2 class="arcade-heading mp-panel-head-title" id="slide-title">VOTING</h2>
        <div class="mp-panel-head-timer">
          <div class="slideshow-vote-deadline-wrap"${votesCloseAt == null ? " hidden" : ""} aria-live="polite">
            ${votesCloseAt != null ? phaseTimerRowHtml("mp-slide-vote-phase") : ""}
          </div>
        </div>
        <div class="mp-panel-head-roster">${progressHintSlotHtml("mp-corner-slide")}</div>
      </div>
      <p class="slide-player" id="slide-player"></p>
      <div id="slide-wave" class="slideshow-wave"></div>
      <div class="slideshow-reactions" id="slideshow-reactions" hidden>
        <div class="slideshow-reaction-btns" role="group" aria-label="Beat reactions">
          ${BEAT_REACTION_KEYS.map(
            (k) =>
              `<button type="button" class="slideshow-reaction-btn" data-beat-reaction="${k}" aria-label="${BEAT_REACTION_ARIA[k] || k}">${BEAT_REACTION_EMOJI[k]}</button>`,
          ).join("")}
        </div>
      </div>
      <p class="arcade-hint" id="slide-progress"></p>
    </div>
  `;

  const titleEl = root.querySelector("#slide-title");
  const nameEl = root.querySelector("#slide-player");
  const waveEl = root.querySelector("#slide-wave");
  const progEl = root.querySelector("#slide-progress");
  const reactionsEl = root.querySelector("#slideshow-reactions");
  /** @type {ReturnType<typeof setInterval> | null} */
  let slideDeadlineInterval = null;

  const tickSlideDeadline = () => {
    if (votesCloseAt == null || votesUnlockWall == null) return;
    const now = Date.now() / 1000;
    const label = root.querySelector("#mp-slide-vote-phase-label");
    if (now < votesUnlockWall) {
      const totalListen = Math.max(1, votesUnlockWall - slideMountSec);
      const remainListen = Math.max(0, votesUnlockWall - now);
      updatePhaseTimerBar(
        root,
        "mp-slide-vote-phase",
        totalListen,
        remainListen,
      );
    } else {
      const remainVote = Math.max(0, votesCloseAt - now);
      updatePhaseTimerBar(
        root,
        "mp-slide-vote-phase",
        voteCollectWindowS(),
        remainVote,
      );
      if (label && remainVote <= 0) label.textContent = "Closing…";
    }
  };
  tickSlideDeadline();
  slideDeadlineInterval = window.setInterval(tickSlideDeadline, 500);
  syncProgressHint();

  /** @type {number} */
  let beatReactionCooldownUntil = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let beatReactionCooldownTimer = null;

  /** @param {boolean} on */
  const setBeatReactionButtonsCooldown = (on) => {
    const wrap = root.querySelector(".slideshow-reaction-btns");
    if (!wrap) return;
    wrap.classList.toggle("slideshow-reaction-btns--cooldown", on);
    wrap.querySelectorAll("button").forEach((b) => {
      if (b instanceof HTMLButtonElement) b.disabled = on;
    });
  };

  /** @type {Set<ReturnType<typeof setTimeout>>} */
  const beatToastTimeouts = new Set();

  const clearBeatToastTimers = () => {
    beatToastTimeouts.forEach((id) => clearTimeout(id));
    beatToastTimeouts.clear();
  };

  const removeBeatToastHost = () => {
    document.getElementById(BEAT_TOAST_HOST_ID)?.remove();
  };

  /**
   * @param {string} fromName
   * @param {string} reactionKey
   */
  const showBeatReactionToast = (fromName, reactionKey) => {
    const ch = BEAT_REACTION_EMOJI[reactionKey] || "·";
    let host = document.getElementById(BEAT_TOAST_HOST_ID);
    if (!host) {
      host = document.createElement("div");
      host.id = BEAT_TOAST_HOST_ID;
      host.className = "lobby-wave-toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }

    const card = document.createElement("div");
    card.className = "lobby-wave-toast";
    card.setAttribute("role", "status");
    const toastName = document.createElement("span");
    toastName.className = "lobby-wave-toast-name";
    toastName.innerHTML = supporterDisplayNameInnerHtml(fromName);
    const emojiEl = document.createElement("span");
    emojiEl.className = "lobby-wave-toast-emoji";
    emojiEl.setAttribute("aria-hidden", "true");
    emojiEl.textContent = ch;
    card.append(toastName, emojiEl);
    host.appendChild(card);

    requestAnimationFrame(() => {
      requestAnimationFrame(() =>
        card.classList.add("lobby-wave-toast--visible"),
      );
    });

    const hide = window.setTimeout(() => {
      beatToastTimeouts.delete(hide);
      card.classList.remove("lobby-wave-toast--visible");
      const remove = window.setTimeout(() => {
        beatToastTimeouts.delete(remove);
        card.remove();
        if (host && host.childElementCount === 0) removeBeatToastHost();
      }, BEAT_TOAST_FADE_MS);
      beatToastTimeouts.add(remove);
    }, BEAT_TOAST_VISIBLE_MS);
    beatToastTimeouts.add(hide);
  };

  const goVote = () => {
    preserveWs = true;
    if (activeWsur) {
      try {
        activeWsur.destroy();
      } catch {
        /* ignore */
      }
      activeWsur = null;
    }
    try {
      ctx.mpWs.send(JSON.stringify({ type: "slideshow_complete" }));
    } catch {
      /* ignore */
    }
    const nowSec = Date.now() / 1000;
    const wallUnlock =
      votesUnlockWall != null && Number.isFinite(votesUnlockWall)
        ? votesUnlockWall
        : nowSec;
    ctx.navigate(mountVoteSelectionScreen, {
      mpWs: ctx.mpWs,
      playerId,
      lobbyId: ctx.lobbyId,
      beats,
      votesUnlockAt: Math.min(wallUnlock, nowSec),
      votesUnlockWall: wallUnlock,
      votesCloseAt,
      slideshowCompleted: true,
    });
  };

  const playNext = async () => {
    if (idx >= beats.length) {
      goVote();
      return;
    }
    const b = beats[idx];
    currentBeatOwnerId = b.player_id || null;
    clearBeatToastTimers();
    removeBeatToastHost();
    const listeningOthers = b.player_id && b.player_id !== playerId;
    if (reactionsEl) {
      reactionsEl.hidden = !listeningOthers;
    }
    if (nameEl)
      nameEl.innerHTML = supporterDisplayNameInnerHtml(String(b.name ?? ""));
    if (titleEl) titleEl.textContent = "NOW PLAYING";
    if (progEl) progEl.textContent = `${idx + 1} / ${beats.length}`;
    if (waveEl) waveEl.innerHTML = "";

    const fullUrl = `${ctx.apiBase}${b.url}?requester=${encodeURIComponent(playerId)}`;

    let wsur = null;

    /** @type {ReturnType<typeof setInterval> | null} */
    let poll = null;
    /** @type {(() => void) | null} */
    let removeTimeCap = null;
    let slideClosed = false;

    const teardownSlide = () => {
      if (poll != null) {
        clearInterval(poll);
        poll = null;
      }
      if (removeTimeCap) {
        try {
          removeTimeCap();
        } catch {
          /* ignore */
        }
        removeTimeCap = null;
      }
      try {
        wsur?.destroy();
      } catch {
        /* ignore */
      }
      if (slideObjectUrl) {
        URL.revokeObjectURL(slideObjectUrl);
        slideObjectUrl = null;
      }
      if (activeWsur === wsur) activeWsur = null;
      wsur = null;
    };

    const advance = () => {
      if (slideClosed) return;
      slideClosed = true;
      teardownSlide();
      idx += 1;
      void playNext();
    };

    try {
      const res = await fetch(fullUrl, { headers: authHeadersMultipart() });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();

      let clipped = true;
      let playBlob = blob;
      try {
        playBlob = await blobToClippedWav(blob);
      } catch {
        clipped = false;
      }

      slideObjectUrl = URL.createObjectURL(playBlob);
      const WaveSurfer = getWaveSurfer();
      wsur = WaveSurfer.create({
        container: waveEl,
        height: 120,
        waveColor: "#b01010",
        progressColor: "#ffffff",
        cursorWidth: 2,
        url: slideObjectUrl,
      });
      activeWsur = wsur;

      wsur.on("finish", () => {
        advance();
      });

      wsur.on("ready", () => {
        if (!wsur || slideClosed) return;
        wsur.setVolume(getVolume());
        wsur.play();

        if (!clipped) {
          const dur =
            typeof wsur.getDuration === "function"
              ? wsur.getDuration()
              : Number.NaN;
          const end = Math.min(
            CLIP_MAX_SEC,
            Number.isFinite(dur) ? dur : CLIP_MAX_SEC,
          );
          const media = wsur.getMediaElement?.();
          const cap = () => {
            if (slideClosed || !wsur) return;
            const t =
              typeof wsur.getCurrentTime === "function"
                ? wsur.getCurrentTime()
                : (media?.currentTime ?? 0);
            if (t >= end - 0.05) {
              advance();
            }
          };
          poll = window.setInterval(cap, 50);
          if (media) {
            const onTime = () => cap();
            media.addEventListener("timeupdate", onTime);
            removeTimeCap = () =>
              media.removeEventListener("timeupdate", onTime);
          }
        }
      });

      wsur.on("error", () => {
        advance();
      });
    } catch {
      teardownSlide();
      idx += 1;
      void playNext();
    }
  };

  const appendReactionLine = (fromName, reactionKey) => {
    if (!currentBeatOwnerId) return;
    showBeatReactionToast(fromName, reactionKey);
  };

  const reactionClick = (e) => {
    const origin = e.target instanceof Element ? e.target : null;
    const btn = origin?.closest?.("[data-beat-reaction]");
    if (
      !(btn instanceof HTMLButtonElement) ||
      !reactionsEl ||
      reactionsEl.hidden
    )
      return;
    if (Date.now() < beatReactionCooldownUntil) return;
    const target = currentBeatOwnerId;
    const reaction = btn.dataset.beatReaction;
    if (!target || !reaction) return;
    playSfxMinor();
    try {
      ctx.mpWs.send(
        JSON.stringify({
          type: "beat_reaction",
          target_player_id: target,
          reaction,
        }),
      );
      beatReactionCooldownUntil = Date.now() + BEAT_REACTION_COOLDOWN_MS;
      setBeatReactionButtonsCooldown(true);
      if (beatReactionCooldownTimer != null)
        clearTimeout(beatReactionCooldownTimer);
      beatReactionCooldownTimer = setTimeout(() => {
        beatReactionCooldownTimer = null;
        beatReactionCooldownUntil = 0;
        setBeatReactionButtonsCooldown(false);
      }, BEAT_REACTION_COOLDOWN_MS);
    } catch {
      /* ignore */
    }
  };
  reactionsEl?.addEventListener("click", reactionClick);

  const onSocket = async (ev) => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (m.type === "connected") return;
    if (m.type === "match_resync") {
      await applyMatchResyncFromPayload(ctx, m, "voting_slideshow");
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
    if (
      m.type === "beat_reaction" &&
      m.from_name &&
      m.reaction &&
      m.target_player_id
    ) {
      if (m.target_player_id === currentBeatOwnerId) {
        appendReactionLine(String(m.from_name), String(m.reaction));
      }
    }
    if (m.type === "results") {
      preserveWs = true;
      stopResultsPoll();
      ctx.navigate(mountResultsScreen, {
        mpWs: ctx.mpWs,
        results: m,
        playerId,
      });
    }
    if (m.type === "votes_timing" && String(m.lobby_id) === String(lobbyId)) {
      if (
        typeof m.votes_unlock_at === "number" &&
        Number.isFinite(m.votes_unlock_at)
      ) {
        votesUnlockWall = m.votes_unlock_at;
      }
      if (
        typeof m.votes_close_at === "number" &&
        Number.isFinite(m.votes_close_at)
      ) {
        votesCloseAt = m.votes_close_at;
      }
      tickSlideDeadline();
      return;
    }
    if (
      m.type === "lobby_update" ||
      m.type === "cook_finished_update" ||
      m.type === "beat_uploaded" ||
      m.type === "vote_cast"
    ) {
      lobbyView = applyMatchWsToLobby(lobbyView, m);
      syncProgressHint();
    }
  };
  const onVoteSlideSocketClose = (ev) => {
    if (preserveWs || teardownClose) return;
    showServerRestartingWait();
    dismissServerRestartingWait();
    void runMpWsReconnect(ev, {
      ctx,
      intentionalLeave: () => teardownClose,
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
        nw.onmessage = onSocket;
        nw.addEventListener("close", onVoteSlideSocketClose, { once: true });
      },
    });
  };
  ctx.mpWs.addEventListener("close", onVoteSlideSocketClose, { once: true });
  ctx.mpWs.onmessage = onSocket;

  const onVolumeChange = (/** @type {CustomEvent} */ ev) => {
    activeWsur?.setVolume(ev.detail);
  };
  window.addEventListener("bb-volume", onVolumeChange);

  if (beats.length === 0) {
    goVote();
  } else {
    void playNext();
  }

  return () => {
    window.removeEventListener("bb-volume", onVolumeChange);
    if (slideDeadlineInterval != null) {
      clearInterval(slideDeadlineInterval);
      slideDeadlineInterval = null;
    }
    stopResultsPoll();
    unmountMpChat();
    if (beatReactionCooldownTimer != null)
      clearTimeout(beatReactionCooldownTimer);
    clearBeatToastTimers();
    removeBeatToastHost();
    reactionsEl?.removeEventListener("click", reactionClick);
    if (slideObjectUrl) {
      URL.revokeObjectURL(slideObjectUrl);
      slideObjectUrl = null;
    }
    if (activeWsur) {
      try {
        activeWsur.destroy();
      } catch {
        /* ignore */
      }
    }
    teardownClose = true;
    root.innerHTML = "";
    if (!preserveWs) {
      try {
        ctx.mpWs.removeEventListener("close", onVoteSlideSocketClose);
      } catch {
        /* ignore */
      }
      try {
        ctx.mpWs.close();
      } catch {
        /* ignore */
      }
    }
  };
}
