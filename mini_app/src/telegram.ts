/** Telegram Mini App integration (Issue #119).
 *
 *  The Mini App boots inside Telegram's WebApp WebView, which exposes a
 *  ``Telegram.WebApp`` object on ``window``.  Telegram signs the
 *  ``initData`` string with the bot token; we POST it to
 *  ``/api/v1/webapp/auth`` (see app/v1/api/webapp_auth.py) and use the
 *  returned bearer session for the rest of the SPA session.
 *
 *  Three states this module surfaces:
 *    - "ok"       : initData is present + signed + owner-only → SPA is
 *                   allowed to render the shell.
 *    - "disabled" : Telegram is NOT present (local dev / browser test)
 *                   → SPA falls back to the bootstrap form.
 *    - "error"    : initData is present but invalid / non-owner /
 *                   misconfigured → SPA shows a clear Owner-only screen.
 *
 *  No business truth lives in this module — the initData string is
 *  treated as an opaque credential and immediately sent over HTTPS.
 */

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
        initDataUnsafe?: {
          user?: {
            id: number;
            username?: string;
            first_name?: string;
            last_name?: string;
          };
        };
        ready?: () => void;
        expand?: () => void;
        close?: () => void;
      };
    };
  }
}

export type TelegramInitDataStatus =
  | { kind: "ok"; initData: string }
  | { kind: "disabled" }
  | { kind: "error"; code: string; message: string };

export function readTelegramInitData(): TelegramInitDataStatus {
  const webapp = window.Telegram?.WebApp;
  if (!webapp) {
    // Outside Telegram (dev preview, CI browser harness, etc.) — the
    // SPA must NOT crash; the bootstrap form remains the fallback.
    return { kind: "disabled" };
  }
  try {
    webapp.ready?.();
    webapp.expand?.();
  } catch {
    // ready/expand failures must never break the SPA — fall through.
  }
  const initData = (webapp.initData ?? "").trim();
  if (!initData) {
    return {
      kind: "error",
      code: "init_data_empty",
      message: "Telegram 客户端未提供 initData，无法登录。",
    };
  }
  return { kind: "ok", initData };
}

/** Render a stable Owner-only error screen — used when initData is
 *  present but the backend rejects the exchange (bad signature,
 *  non-owner id, missing config).
 */
export function renderWebappError(
  root: HTMLElement,
  status: Extract<TelegramInitDataStatus, { kind: "error" }>,
  locale: "zh" | "en",
): void {
  const title = locale === "zh" ? "管理后台未开放" : "Console unavailable";
  const hint =
    locale === "zh"
      ? "请确认您是工作区 Owner，并通过 Telegram 的「打开管理后台」按钮重新进入。"
      : "Confirm you are the workspace Owner and reopen via the “打开管理后台” Telegram menu button.";
  root.innerHTML = `
    <header class="app-header">
      <p class="eyebrow">PASAY RENT</p>
      <h1>${title}</h1>
    </header>
    <main id="view-root" class="view-root">
      <section class="panel">
        <p class="muted">${hint}</p>
        <p class="muted form-error" data-code="${status.code}">${status.message}</p>
      </section>
    </main>
  `;
}