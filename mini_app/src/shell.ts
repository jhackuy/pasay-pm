/** Top-level shell — header + bottom nav. The 5 primary tabs map to the
 *  Owner Console: Home / Properties / Work / Finance / More.
 *
 *  AGENTS.md §4 invariants:
 *  - No business truth in localStorage. Session token lives only in memory.
 *  - Touch targets ≥ 44×44 px (CSS enforces `min-height: 44px`).
 *  - Light/dark via `prefers-color-scheme`.
 *  - Loading / success / error states are explicit per view.
 */

import { router, type Route } from "./router";
import { type Locale, type StringKey, t } from "./i18n";

export type ShellDeps = {
  root: HTMLElement;
  locale: Locale;
  onLocaleChange: (next: Locale) => void;
  isAuthenticated: () => boolean;
  onSignOut: () => void;
};

export function renderShell(deps: ShellDeps, active: Route, body: string): void {
  const { root, locale, isAuthenticated, onSignOut } = deps;
  const tabs: Array<{ route: Route; label: StringKey; icon: string }> = [
    { route: { name: "home" }, label: "home", icon: "🏠" },
    { route: { name: "properties" }, label: "properties", icon: "🏢" },
    { route: { name: "work" }, label: "work", icon: "🛠" },
    { route: { name: "finance" }, label: "finance", icon: "💰" },
    { route: { name: "more" }, label: "more", icon: "⋯" },
  ];
  const navButtons = tabs
    .map((tab) => {
      const isActive =
        active.name === tab.route.name ||
        (tab.route.name === "properties" && active.name === "properties.detail");
      return `<button class="nav-btn ${isActive ? "active" : ""}" data-route="${tab.route.name}" aria-current="${isActive ? "page" : "false"}" type="button">
        <span class="nav-icon" aria-hidden="true">${tab.icon}</span>
        <span>${t(locale, tab.label)}</span>
      </button>`;
    })
    .join("");

  const header = isAuthenticated()
    ? `<header class="app-header">
        <div>
          <p class="eyebrow">${t(locale, "appName")}</p>
          <h1>${t(locale, "overview")}</h1>
        </div>
        <div class="header-actions">
          <button class="ghost-btn" data-action="toggle-locale" type="button" aria-label="Toggle language">${locale === "zh" ? "EN" : "中"}</button>
          <button class="ghost-btn" data-action="sign-out" type="button">${t(locale, "signOut")}</button>
        </div>
      </header>`
    : `<header class="app-header">
        <p class="eyebrow">${t(locale, "appName")}</p>
        <h1>${t(locale, "bootstrap")}</h1>
      </header>`;

  root.innerHTML = `${header}<main id="view-root" class="view-root">${body}</main><nav class="app-nav" aria-label="Primary">${navButtons}</nav>`;

  root.querySelectorAll<HTMLButtonElement>(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.route as Route["name"] | undefined;
      if (!target) return;
      switch (target) {
        case "home":
          router.navigate({ name: "home" });
          break;
        case "properties":
          router.navigate({ name: "properties" });
          break;
        case "work":
          router.navigate({ name: "work" });
          break;
        case "finance":
          router.navigate({ name: "finance" });
          break;
        case "more":
          router.navigate({ name: "more" });
          break;
      }
    });
  });

  const localeBtn = root.querySelector<HTMLButtonElement>("[data-action='toggle-locale']");
  localeBtn?.addEventListener("click", () => {
    deps.onLocaleChange(locale === "zh" ? "en" : "zh");
  });
  const signOutBtn = root.querySelector<HTMLButtonElement>("[data-action='sign-out']");
  signOutBtn?.addEventListener("click", () => {
    onSignOut();
  });
}

export function setViewContent(html: string): void {
  const root = document.querySelector<HTMLElement>("#view-root");
  if (root) root.innerHTML = html;
}

export function viewRoot(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#view-root");
}
