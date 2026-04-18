/**
 * Boots the app: swap screens, hand around ctx, surface ugly JS errors.
 */
import { getApiBase } from "./apiOrigin.js";
import { clearAuthCorner } from "./authCorner.js";
import { getUsername, isLoggedIn, validateSession } from "./authApi.js";
import {
  resetAppErrorContext,
  setAppErrorContext,
  showAppError,
} from "./errorToast.js";
import { playSfxBeatBattle } from "./sfx.js";
import { mountModeSelectScreen } from "./screens/modeSelect.js";
import { initCornerSocialTooltips } from "./cornerSocialTooltips.js";
import { initCornerSocialMenu } from "./cornerSocialMenu.js";
import { initCreditsCornerControl } from "./creditsOverlay.js";
import { initDevStatsPanel, recordPageVisit } from "./devStatsPanel.js";
import { initSupportersClient } from "./supporters.js";
import { initVolumeWidget } from "./volume.js";

function boot() {
  initSupportersClient();
  recordPageVisit();
  initCornerSocialTooltips();
  const cornerMenu = document.querySelector(".corner-social-menu");
  if (cornerMenu instanceof HTMLElement) initCornerSocialMenu(cornerMenu);
  const creditsBtn = document.getElementById("credits-corner-btn");
  if (creditsBtn instanceof HTMLElement) initCreditsCornerControl(creditsBtn);
  const volWidget = document.getElementById("vol-widget");
  if (volWidget instanceof HTMLElement) initVolumeWidget(volWidget);
  window.addEventListener("error", (ev) => {
    const fn = ev.filename || "";
    if (!fn || fn.includes("extension://") || fn.includes("moz-extension://"))
      return;
    if (!fn.includes("/js/") && !/\/main\.js(\?|$)/.test(fn)) return;
    showAppError({
      message: ev.message || "Something in the page hit an error.",
      hint: "Try refreshing. If it happens again, copy the details below.",
      errorCode: ev.lineno ? `SCRIPT_L${ev.lineno}` : "SCRIPT",
    });
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    const msg = r instanceof Error ? r.message : String(r);
    showAppError({
      message: msg || "Something failed in the background.",
      hint: "Try again or refresh. Copy details if you need to report it.",
      errorCode: "UNHANDLED_REJECTION",
    });
  });

  setTimeout(() => playSfxBeatBattle(), 0);

  const root = document.getElementById("app-root");
  if (!root) return;

  let unmount = null;

  /** @param {(el: HTMLElement, ctx: object) => () => void} mountFn */
  const navigate = (mountFn, extra = {}) => {
    if (unmount) unmount();
    clearAuthCorner();
    resetAppErrorContext();
    const ctx = {
      apiBase: getApiBase(),
      navigate,
      username: getUsername(),
      ...extra,
    };
    if (ctx.playerId != null)
      setAppErrorContext({ playerId: String(ctx.playerId) });
    if (ctx.lobbyId != null)
      setAppErrorContext({ lobbyId: String(ctx.lobbyId) });
    unmount = mountFn(root, ctx);
  };

  navigate(mountModeSelectScreen);

  if (isLoggedIn()) {
    void validateSession().then((ok) => {
      if (!ok) navigate(mountModeSelectScreen);
      else initDevStatsPanel();
    });
  }
}

document.addEventListener("DOMContentLoaded", boot);
