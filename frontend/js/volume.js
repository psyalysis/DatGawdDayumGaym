const STORAGE_KEY = "bb_volume";
const DEFAULT = 1;

let _vol = (() => {
  const s = localStorage.getItem(STORAGE_KEY);
  const n = s !== null ? parseFloat(s) : DEFAULT;
  return isNaN(n) ? DEFAULT : Math.max(0, Math.min(1, n));
})();

export function getVolume() {
  return _vol;
}

export function setVolume(v) {
  _vol = Math.max(0, Math.min(1, v));
  localStorage.setItem(STORAGE_KEY, String(_vol));
  document.querySelectorAll("audio").forEach((el) => {
    el.volume = _vol;
  });
  window.dispatchEvent(new CustomEvent("bb-volume", { detail: _vol }));
}


export function initVolumeWidget(el) {
  const slider = el.querySelector(".vol-slider");
  const btn = el.querySelector(".vol-trigger");
  if (!slider || !btn) return;

  
  let _preMute = _vol > 0 ? _vol : 1;

  const sync = (v) => {
    slider.value = String(Math.round(v * 100));
    el.dataset.volLevel = v === 0 ? "mute" : v < 0.5 ? "low" : "high";
    btn.setAttribute(
      "aria-label",
      v === 0 ? "Unmute" : `Volume: ${Math.round(v * 100)}%`,
    );
  };

  sync(_vol);

  slider.addEventListener("input", () => {
    const v = parseInt(slider.value, 10) / 100;
    if (v > 0) _preMute = v;
    setVolume(v);
    sync(v);
  });

  
  btn.addEventListener("click", (ev) => {
    
    if (ev.target === slider) return;
    if (_vol > 0) {
      _preMute = _vol;
      setVolume(0);
    } else {
      setVolume(_preMute);
    }
    sync(_vol);
  });

  window.addEventListener("bb-volume", (ev) => sync(ev.detail));

  let hideTimer = null;

  el.addEventListener('mouseenter', () => {
    clearTimeout(hideTimer);
    el.classList.add('vol-open');
  });

  el.addEventListener('mouseleave', () => {
    hideTimer = setTimeout(() => {
      el.classList.remove('vol-open');
    }, 300);
  });
}