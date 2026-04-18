/**
 * Solo mode: same light-kit path as MP — manifest + pulls from /media/dataset.
 */
import { mountAuthCornerLeave } from "./authCorner.js";
import { getApiBase } from "./apiOrigin.js";
import { setAppErrorContext } from "./errorToast.js";
import {
  fetchKitManifest,
  KIT_SOUND_FILE_EXT,
  loadDrumKitBase64Parallel,
  loadSynthBuffersAndMp3Base64Parallel,
  SYNTH_KEYS,
} from "./kitFromSeed.js";
import { mountKitLayoutShell } from "./kitGridLayout.js";
import { playSfxMajor, playSfxMinor, playSfxOn } from "./sfx.js";
import { getVolume } from "./volume.js";
import { runSynthReveal } from "./synthReveal.js";

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

function base64ToAudioSrc(base64) {
  return "data:audio/ogg;base64," + base64;
}

function labelForKey(key) {
  return key.replace(/_/g, " ");
}

function getWaveSurfer() {
  const g = globalThis;
  if (g.WaveSurfer && typeof g.WaveSurfer.create === "function")
    return g.WaveSurfer;
  throw new Error("WaveSurfer not loaded");
}

function getJSZip() {
  const g = globalThis;
  if (g.JSZip) return g.JSZip;
  throw new Error("JSZip not loaded");
}

function resolveApiBase(ctx) {
  const b = ctx.apiBase;
  if (typeof b === "string" && b.trim().length > 0) {
    return b.trim().replace(/\/$/, "");
  }
  return getApiBase();
}

export function mountSoloScreen(root, ctx) {
  setAppErrorContext({ screen: "Solo cook", phase: "Offline kit" });
  const apiBase = resolveApiBase(ctx);
  const waveSurfers = new Map();
  const audioSrcByKey = new Map();
  const clickFullPlayback = new Map();
  let lastSoundsB64 = null;
  /** @type {HTMLElement | null} */
  let activeKitOverlay = null;
  /** @type {AudioContext | null} */
  let activeKitAc = null;

  const clearKitLoadUi = () => {
    if (activeKitOverlay) {
      activeKitOverlay.remove();
      activeKitOverlay = null;
    }
    if (activeKitAc) {
      void activeKitAc.close().catch(() => {});
      activeKitAc = null;
    }
  };

  const destroyWaveSurfers = () => {
    waveSurfers.forEach((ws) => {
      try {
        ws.destroy();
      } catch {
        /* ignore */
      }
    });
    waveSurfers.clear();
  };

  mountAuthCornerLeave(ctx);

  root.innerHTML = `
    <div class="screen solo arcade-panel screen--vert-center">
      <div class="screen-topbar">
        <button type="button" class="arcade-back" id="solo-back" aria-label="Back">&lt;</button>
        <h2 class="arcade-heading screen-topbar-title">SOLO COOK</h2>
        <span class="screen-topbar-spacer" aria-hidden="true"></span>
      </div>
      <section class="toolbar solo-toolbar" aria-label="Controls">
        <label class="field field-inline">
          <span class="field-label">Spice</span>
          <div class="field-row">
            <input type="range" id="spice" min="0" max="1" step="0.1" value="0.3" />
            <span id="spice-value" class="field-value">0.3</span>
          </div>
        </label>
        <div class="toolbar-actions">
          <button type="button" id="btn-generate" class="arcade-btn arcade-btn-primary">Generate Battle Kit</button>
          <div id="kit-actions" class="kit-actions hidden">
            <button type="button" id="btn-regenerate" class="arcade-btn arcade-btn-primary">ReGenerate Battle Kit</button>
            <button type="button" id="btn-download-all" class="arcade-btn arcade-btn-secondary">Download all</button>
          </div>
        </div>
      </section>
      <p id="status" class="status arcade-status" aria-live="polite"></p>
      <main id="sound-grid" class="kit-layout mp-grid hidden" aria-label="Generated kit"></main>
    </div>
  `;

  const spice = root.querySelector("#spice");
  const spiceVal = root.querySelector("#spice-value");
  const updateSpice = () => {
    if (spiceVal && spice)
      spiceVal.textContent = Number(spice.value).toFixed(1);
  };
  spice?.addEventListener("input", () => {
    playSfxOn();
    updateSpice();
  });
  updateSpice();

  root.querySelector("#solo-back")?.addEventListener("click", async () => {
    playSfxMinor();
    clearKitLoadUi();
    destroyWaveSurfers();
    const m = await import("./screens/modeSelect.js");
    ctx.navigate(m.mountModeSelectScreen);
  });

  function buildKitCard(key) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.sound = key;

    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("h2");
    title.className = "card-title";
    title.textContent = labelForKey(key);
    const dl = document.createElement("button");
    dl.type = "button";
    dl.className = "card-download";
    dl.dataset.soundKey = key;
    dl.setAttribute("aria-label", `Download ${key}`);
    dl.textContent = "↓";
    head.append(title, dl);

    const waveWrap = document.createElement("div");
    waveWrap.className = "waveform-wrap empty";
    waveWrap.id = `wave-${key}`;
    waveWrap.textContent = "—";

    const audio = document.createElement("audio");
    audio.id = `audio-${key}`;
    audio.preload = "auto";
    audio.volume = getVolume();

    bindWaveformPlayback(key, waveWrap, audio);

    card.append(head, waveWrap, audio);
    return card;
  }

  function buildGrid() {
    const grid = root.querySelector("#sound-grid");
    if (!grid) return;
    mountKitLayoutShell(grid, {
      synthKeys: SYNTH_KEYS,
      appendCard: buildKitCard,
    });
  }

  function bindWaveformPlayback(key, waveWrap, audio) {
    const setClickFull = (v) => clickFullPlayback.set(key, v);
    audio.addEventListener("ended", () => setClickFull(false));
    waveWrap.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
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
  }

  function renderWaveform(key, dataUrl) {
    const container = root.querySelector(`#wave-${key}`);
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
  }

  function afterLayout(fn) {
    requestAnimationFrame(() => requestAnimationFrame(fn));
  }

  function attachSounds(sounds) {
    destroyWaveSurfers();
    audioSrcByKey.clear();
    clickFullPlayback.clear();
    SOUND_KEYS.forEach((key) => {
      const b64 = sounds[key];
      if (!b64) return;
      const src = base64ToAudioSrc(b64);
      audioSrcByKey.set(key, src);
      const audio = root.querySelector(`#audio-${key}`);
      if (audio instanceof HTMLAudioElement) audio.src = src;
    });
    afterLayout(() => {
      SOUND_KEYS.forEach((key) => {
        const src = audioSrcByKey.get(key);
        if (src) renderWaveform(key, src);
      });
    });
  }

  function setKitUiVisible(visible) {
    const gen = root.querySelector("#btn-generate");
    const kitActions = root.querySelector("#kit-actions");
    const grid = root.querySelector("#sound-grid");
    if (visible) {
      gen?.classList.add("hidden");
      kitActions?.classList.remove("hidden");
      grid?.classList.remove("hidden");
    } else {
      gen?.classList.remove("hidden");
      kitActions?.classList.add("hidden");
      grid?.classList.add("hidden");
    }
  }

  function base64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function downloadAllAsZip() {
    if (!lastSoundsB64) return;
    const JSZip = getJSZip();
    const zip = new JSZip();
    const folder = zip.folder("beat_battle_kit");
    if (!folder) return;
    for (const key of SOUND_KEYS) {
      const b64 = lastSoundsB64[key];
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

  function downloadOneSound(key) {
    if (!lastSoundsB64) return;
    const b64 = lastSoundsB64[key];
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

  async function generateKit() {
    const status = root.querySelector("#status");
    const spiceEl = root.querySelector("#spice");
    const btnGen = root.querySelector("#btn-generate");
    const btnReg = root.querySelector("#btn-regenerate");
    const spiceNum = spiceEl ? parseFloat(spiceEl.value) : 0.3;

    const loading = () => {
      if (status) status.textContent = "";
      if (btnGen instanceof HTMLButtonElement) btnGen.disabled = true;
      if (btnReg instanceof HTMLButtonElement) btnReg.disabled = true;
    };
    const doneLoading = () => {
      if (btnGen instanceof HTMLButtonElement) btnGen.disabled = false;
      if (btnReg instanceof HTMLButtonElement) btnReg.disabled = false;
    };

    playSfxMajor();
    clearKitLoadUi();
    loading();

    const seed = Math.floor(Math.random() * 0x80000000);
    const base = resolveApiBase(ctx);
    const loadLayer = document.createElement("div");
    loadLayer.className = "synth-reveal-overlay";
    loadLayer.setAttribute("role", "status");
    loadLayer.innerHTML =
      '<p class="arcade-status" id="solo-kit-load">Loading kit…</p>';
    document.body.appendChild(loadLayer);
    activeKitOverlay = loadLayer;
    const loadEl = loadLayer.querySelector("#solo-kit-load");

    const ac = new AudioContext({ sampleRate: 44100 });
    activeKitAc = ac;
    let drumsPending = true;

    try {
      const manifest = await fetchKitManifest(base);
      const drumPromise = loadDrumKitBase64Parallel({
        seed,
        spice: spiceNum,
        apiBase: base,
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
          spice: spiceNum,
          apiBase: base,
          audioContext: ac,
          manifest,
        });

      if (loadEl) loadEl.textContent = "";
      loadLayer.remove();
      activeKitOverlay = null;

      await runSynthReveal(ac, synthBuffers, () => drumsPending);

      const drumSounds = await drumPromise;
      const sounds = { ...drumSounds, ...synthB64 };

      await ac.close().catch(() => {});
      activeKitAc = null;

      lastSoundsB64 = sounds;
      buildGrid();
      setKitUiVisible(true);
      attachSounds(sounds);
    } catch (e) {
      console.error(e);
      clearKitLoadUi();
      if (status) {
        status.textContent =
          e instanceof Error
            ? e.message
            : "Could not load kit. Is the API running?";
      }
    } finally {
      doneLoading();
    }
  }

  root
    .querySelector("#btn-generate")
    ?.addEventListener("click", () => generateKit());
  root
    .querySelector("#btn-regenerate")
    ?.addEventListener("click", () => generateKit());
  root.querySelector("#btn-download-all")?.addEventListener("click", () => {
    playSfxMinor();
    downloadAllAsZip().catch((e) => {
      console.error(e);
      const st = root.querySelector("#status");
      if (st)
        st.textContent =
          e instanceof Error ? e.message : "Could not build ZIP.";
    });
  });

  root.querySelector("#sound-grid")?.addEventListener("click", (e) => {
    const btn =
      e.target instanceof Element ? e.target.closest(".card-download") : null;
    if (!(btn instanceof HTMLButtonElement)) return;
    e.preventDefault();
    e.stopPropagation();
    const key = btn.dataset.soundKey;
    if (!key) return;
    playSfxMinor();
    downloadOneSound(key);
  });

  return () => {
    clearKitLoadUi();
    destroyWaveSurfers();
    root.innerHTML = "";
  };
}
