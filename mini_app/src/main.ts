/** PASAY Mini App — entrypoint.
 *
 *  Wires the typed API client, the in-memory session, the hash router,
 *  and the view modules. No business truth is ever persisted in
 *  localStorage (AGENTS.md §4).
 */

import { PasayClient } from "./api";
import { router } from "./router";
import { SessionStore } from "./state";
import { renderShell } from "./shell";
import { type Locale } from "./i18n";
import { renderHome } from "./views/home";
import { renderProperties, renderPropertyDetail } from "./views/properties";
import { renderWork } from "./views/work";
import { renderFinance } from "./views/finance";
import { renderMore } from "./views/more";
import { renderMoveOutDetail } from "./views/move_out";
import { renderRentClaimDetail } from "./views/rent_payment";

const root = document.querySelector<HTMLDivElement>("#app");
if (!root) throw new Error("Mini App mount point #app is missing");

const client = new PasayClient();
const session = new SessionStore(client);
let locale: Locale = (localStorage.getItem("pasay.locale") === "en" ? "en" : "zh") as Locale;

// Test affordance: expose the in-memory session + typed client on
// `window` so browser-driven tests can seed extra entities (e.g. an
// ACTIVE lease before opening the renewal form) without inventing a
// separate "test mode" path. AGENTS.md §4 is honored: nothing here is
// persisted to localStorage; these are scratch globals on `window`
// only, present for the lifetime of the page.
declare global {
  interface Window {
    __PASAY_CLIENT__?: PasayClient;
    __PASAY_SESSION__?: {
      api_key: string;
      org_id: number;
      user_id: number;
      role: string;
    };
    __PASAY_PROPERTY_ID__?: number;
  }
}
window.__PASAY_CLIENT__ = client;

function setLocale(next: Locale): void {
  locale = next;
  localStorage.setItem("pasay.locale", next);
  paint();
}

async function paint(): Promise<void> {
  const route = router.current;
  if (!session.isAuthenticated()) {
    renderBootstrap();
    return;
  }
  const orgId = session.orgId ?? 0;
  renderShell(
    {
      root: root!,
      locale,
      onLocaleChange: setLocale,
      isAuthenticated: () => session.isAuthenticated(),
      onSignOut: () => {
        session.signOut();
        window.location.hash = "";
        paint();
      },
    },
    route,
    `<section class="card loading"><p>${locale === "zh" ? "加载中…" : "Loading…"}</p></section>`,
  );
  switch (route.name) {
    case "home":
      await renderHome(client, orgId, locale);
      break;
    case "properties":
      await renderProperties(client, orgId, locale);
      break;
    case "properties.detail":
      await renderPropertyDetail(client, orgId, route.propertyId, locale);
      break;
    case "work":
      await renderWork(client, orgId, locale);
      break;
    case "finance":
      await renderFinance(client, orgId, locale);
      break;
    case "more":
      await renderMore(client, orgId, session, locale);
      break;
    case "move_out.detail":
      await renderMoveOutDetail(client, orgId, route.moveOutId, locale);
      break;
    case "rent_claim.detail":
      await renderRentClaimDetail(client, orgId, route.paymentId, locale);
      break;
  }
}

function renderBootstrap(): void {
  root!.innerHTML = `
    <header class="app-header">
      <p class="eyebrow">PASAY RENT</p>
      <h1>${locale === "zh" ? "引导第一个工作区" : "Bootstrap workspace"}</h1>
    </header>
    <main id="view-root" class="view-root">
      <section class="panel">
        <form id="bootstrap-form" class="form">
          <label>${locale === "zh" ? "工作区名称" : "Workspace name"}<input name="workspace_name" required maxlength="120" value="Demo Workspace" /></label>
          <label>${locale === "zh" ? "用户名" : "Username"}<input name="owner_username" maxlength="64" value="owner" /></label>
          <label>${locale === "zh" ? "显示名" : "Display name"}<input name="owner_display_name" maxlength="120" value="Demo Owner" /></label>
          <div class="form-actions">
            <button class="primary-btn" type="submit">${locale === "zh" ? "引导" : "Bootstrap"}</button>
          </div>
          <p class="muted form-error" id="bootstrap-error" hidden></p>
        </form>
        <p class="muted">${locale === "zh" ? "Bootstrap 仅在数据库无用户时可用（dev/test）" : "Bootstrap is only available when no users exist (dev/test)."}</p>
      </section>
    </main>
  `;
  const formEl = document.querySelector<HTMLFormElement>("#bootstrap-form");
  formEl?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.target as HTMLFormElement;
    const data = new FormData(formEl);
    try {
      const response = await client.bootstrap({
        workspace_name: String(data.get("workspace_name") || "").trim(),
        owner_username: String(data.get("owner_username") || "") || null,
        owner_display_name: String(data.get("owner_display_name") || "") || null,
      });
      session.bootstrap(response.api_key, response.org_id, response.user_id, response.role);
      // Mirror session into a window global so browser-driven tests can
      // seed extra entities (e.g. an ACTIVE lease) without rerunning
      // bootstrap. The data is in-memory only.
      window.__PASAY_SESSION__ = {
        api_key: response.api_key,
        org_id: response.org_id,
        user_id: response.user_id,
        role: response.role,
      };
      await paint();
    } catch (err) {
      const errorEl = document.querySelector<HTMLElement>("#bootstrap-error");
      if (errorEl) {
        errorEl.textContent = err instanceof Error ? err.message : String(err);
        errorEl.hidden = false;
      }
    }
  });
}

router.subscribe(() => {
  void paint();
});

void paint();
