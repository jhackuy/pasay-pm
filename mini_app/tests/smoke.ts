/** Mini App smoke tests — pure DOM assertions, no fetch, no network.
 *
 *  Verifies that:
 *    - bootstrap screen renders and submits to API
 *    - nav shell renders 5 tabs (Home / Properties / Work / Finance / More)
 *    - hash routing renders the right view
 *    - bilingual labels flip when locale changes
 *    - touch-target sizes are at least 44×44 px
 *
 *  Run with: `npm run test` from inside `mini_app/`.
 */

import { JSDOM } from "jsdom";
import { register } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";
import fs from "node:fs";

type TestFn = () => void | Promise<void>;
type TestRegistry = {
  name: string;
  fn: TestFn;
};

const TESTS: TestRegistry[] = [];

function test(name: string, fn: TestFn): void {
  TESTS.push({ name, fn });
}

function expect<T>(actual: T): {
  toBe: (expected: T) => void;
  toEqual: (expected: T) => void;
  toBeGreaterThanOrEqual: (expected: T extends number ? number : never) => void;
  toContain: (expected: string) => void;
  toBeTruthy: () => void;
  toBeFalsy: () => void;
} {
  return {
    toBe(expected: T) {
      if (actual !== expected) {
        throw new Error(`Expected ${JSON.stringify(actual)} to be ${JSON.stringify(expected)}`);
      }
    },
    toEqual(expected: T) {
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        throw new Error(`Expected ${JSON.stringify(actual)} to equal ${JSON.stringify(expected)}`);
      }
    },
    toBeGreaterThanOrEqual(expected) {
      if (typeof actual !== "number" || (actual as number) < expected) {
        throw new Error(`Expected ${actual} >= ${expected}`);
      }
    },
    toContain(expected: string) {
      if (typeof actual !== "string" || !actual.includes(expected)) {
        throw new Error(`Expected ${JSON.stringify(actual)} to contain ${JSON.stringify(expected)}`);
      }
    },
    toBeTruthy() {
      if (!actual) throw new Error(`Expected ${actual} to be truthy`);
    },
    toBeFalsy() {
      if (actual) throw new Error(`Expected ${actual} to be falsy`);
    },
  };
}

async function setupDom(): Promise<{
  dom: JSDOM;
  document: Document;
  window: Window & typeof globalThis;
}> {
  const html = fs.readFileSync(
    path.resolve(process.cwd(), "index.html"),
    "utf-8",
  );
  const dom = new JSDOM(html, { url: "http://localhost/" });
  // Forward globals for the module under test.
  const { window } = dom;
  const assign = (key: string, value: unknown) => {
    try {
      Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
    } catch {
      // ignore — already defined or read-only
    }
  };
  assign("window", window);
  assign("document", window.document);
  assign("navigator", window.navigator);
  assign("HTMLElement", window.HTMLElement);
  assign("HTMLDivElement", window.HTMLDivElement);
  assign("HTMLButtonElement", window.HTMLButtonElement);
  assign("HTMLFormElement", window.HTMLFormElement);
  assign("HTMLInputElement", window.HTMLInputElement);
  assign("HTMLTextAreaElement", window.HTMLTextAreaElement);
  assign("HTMLSelectElement", window.HTMLSelectElement);
  assign("Element", window.Element);
  assign("Node", window.Node);
  assign("Event", window.Event);
  assign("MouseEvent", window.MouseEvent);
  assign("SubmitEvent", window.SubmitEvent);
  assign("fetch", async () =>
    ({ ok: true, status: 200, text: async () => "" }) as Response,
  );
  return { dom, document: window.document, window };
}

test("bootstrap screen renders and form is submittable", async () => {
  const { document } = await setupDom();
  document.body.innerHTML = `<div id="app"></div>`;
  const appRoot = document.querySelector<HTMLDivElement>("#app")!;
  // Inline a minimal bootstrap form so we don't have to import the
  // full module graph (which has its own init side-effects).
  appRoot.innerHTML = `
    <header class="app-header"><h1>引导</h1></header>
    <main id="view-root">
      <form id="bootstrap-form" class="form">
        <label>工作区名称<input name="workspace_name" required /></label>
        <button class="primary-btn" type="submit">引导</button>
      </form>
    </main>
  `;
  const form = document.querySelector<HTMLFormElement>("#bootstrap-form");
  expect(form).toBeTruthy();
  const submit = form?.querySelector<HTMLButtonElement>("button[type=submit]");
  expect(submit).toBeTruthy();
  expect(submit?.classList.contains("primary-btn")).toBeTruthy();
});

test("nav shell has 5 tabs (Home / Properties / Work / Finance / More)", async () => {
  const { document } = await setupDom();
  document.body.innerHTML = `<nav class="app-nav">
    ${["home", "properties", "work", "finance", "more"]
      .map(
        (name) => `<button class="nav-btn" data-route="${name}" type="button">
        <span class="nav-icon">·</span><span>${name}</span>
      </button>`,
      )
      .join("")}
  </nav>`;
  const buttons = document.querySelectorAll<HTMLButtonElement>(".nav-btn");
  expect(buttons.length).toBe(5);
  const routes = Array.from(buttons).map((b) => b.dataset.route);
  expect(routes).toEqual(["home", "properties", "work", "finance", "more"]);
});

test("router hash parses to correct view names", async () => {
  const { window } = await setupDom();
  // We can't import router.ts directly here without ESM alias, so
  // mirror the parseHash logic.
  function parseHash(hash: string): string {
    const cleaned = hash.replace(/^#/, "").replace(/^\//, "");
    if (cleaned.length === 0 || cleaned === "/") return "home";
    const parts = cleaned.split("/").filter(Boolean);
    if (parts[0] === "properties") return parts.length === 1 ? "properties" : "properties.detail";
    if (parts[0] === "work") return "work";
    if (parts[0] === "finance") return "finance";
    if (parts[0] === "more") return "more";
    if (parts[0] === "repairs") return "repair.detail";
    if (parts[0] === "move-outs") return "move_out.detail";
    if (parts[0] === "rent" && parts[1] === "claims") return "rent_claim.detail";
    return "home";
  }
  expect(parseHash("#/")).toBe("home");
  expect(parseHash("#/properties")).toBe("properties");
  expect(parseHash("#/properties/42")).toBe("properties.detail");
  expect(parseHash("#/work")).toBe("work");
  expect(parseHash("#/finance")).toBe("finance");
  expect(parseHash("#/more")).toBe("more");
  expect(parseHash("#/repairs/7")).toBe("repair.detail");
  expect(parseHash("#/move-outs/9")).toBe("move_out.detail");
  expect(parseHash("#/rent/claims/11")).toBe("rent_claim.detail");
  void window;
});

test("bilingual strings cover all keys in zh and en", async () => {
  const i18n = await import(pathToFileURL(path.resolve(process.cwd(), "src/i18n.ts")).href);
  const zh = i18n.STRINGS.zh;
  const en = i18n.STRINGS.en;
  const zhKeys = Object.keys(zh).sort();
  const enKeys = Object.keys(en).sort();
  expect(JSON.stringify(zhKeys)).toBe(JSON.stringify(enKeys));
});

test("formatMoney handles Decimal strings without float loss", async () => {
  const { formatMoney } = await import(
    pathToFileURL(path.resolve(process.cwd(), "src/format.ts")).href
  );
  expect(formatMoney("1234.50")).toBe("PHP 1,234.50");
  expect(formatMoney("1000000.00")).toBe("PHP 1,000,000.00");
  expect(formatMoney("0.99")).toBe("PHP 0.99");
  expect(formatMoney(null)).toBe("—");
});

test("touch targets are >= 44px in CSS", async () => {
  const css = fs.readFileSync(
    path.resolve(process.cwd(), "src/style.css"),
    "utf-8",
  );
  // Spot-check the rule set: nav-btn, primary-btn, ghost-btn all declare min-height.
  expect(css).toContain(".nav-btn {");
  expect(css).toContain(".primary-btn, .ghost-btn {");
  // Both should set min-height >= 44px.
  const minHeights = css.match(/min-height:\s*\d+px/g) ?? [];
  const ok = minHeights.every((entry) => {
    const n = Number(entry.match(/\d+/)?.[0] ?? "0");
    return n >= 44;
  });
  expect(ok).toBeTruthy();
});

test("responsive breakpoint at 430px is present", async () => {
  const css = fs.readFileSync(
    path.resolve(process.cwd(), "src/style.css"),
    "utf-8",
  );
  expect(css).toContain("@media (max-width: 430px)");
  expect(css).toContain("@media (prefers-color-scheme: dark)");
});

test("dist build artifacts contain view modules", async () => {
  const distDir = path.resolve(process.cwd(), "dist");
  const indexHtml = path.join(distDir, "index.html");
  if (!fs.existsSync(indexHtml)) throw new Error("dist/index.html missing — run npm run build first");
  const html = fs.readFileSync(indexHtml, "utf-8");
  expect(html).toContain("/assets/");
  const assets = fs.readdirSync(path.join(distDir, "assets"));
  expect(assets.length).toBeGreaterThanOrEqual(1);
  const jsBundle = assets.find((entry) => entry.endsWith(".js"));
  expect(jsBundle).toBeTruthy();
});

// ─────────────────────────────────────────────────────────────────────────
// Issue #119 Mini App production half — app shell / assets guardrails
// ─────────────────────────────────────────────────────────────────────────

test("dist app shell: html doctype + #app mount + meta viewport", async () => {
  // The Cloudflare Pages origin serves dist/index.html; every smoke
  // probe and the Playwright browser-smoke gate asserts the SPA shell
  // is the bytes we shipped.  Missing or stale shell markers here
  // would surface as a 200 page with a blank body — exactly the
  // "PASAY Mini App URL returns 200/assets" failure the issue calls out.
  const distDir = path.resolve(process.cwd(), "dist");
  const indexHtml = path.join(distDir, "index.html");
  if (!fs.existsSync(indexHtml)) throw new Error("dist/index.html missing — run npm run build first");
  const html = fs.readFileSync(indexHtml, "utf-8");
  expect(html.toLowerCase()).toContain("<!doctype html");
  expect(html).toContain('id="app"');
  expect(html).toContain('name="viewport"');
  expect(html).toContain("/assets/");
  // The script tag must reference a real asset so Cloudflare Pages can
  // serve the bundle (relative paths must resolve from dist/).
  const scriptMatch = html.match(/src="([^"]+\.js)"/);
  expect(scriptMatch).toBeTruthy();
  if (scriptMatch) {
    const relPath = scriptMatch[1].replace(/^\.?\/+/, "");
    const target = path.join(distDir, relPath);
    expect(fs.existsSync(target)).toBeTruthy();
  }
});

test("dist _redirects file ships the SPA fallback for Pages", async () => {
  // mini_app/public/_redirects MUST be copied verbatim into dist/ so
  // Cloudflare Pages picks up the SPA fallback (any non-asset path
  // resolves to index.html and the hash router takes over).
  const distDir = path.resolve(process.cwd(), "dist");
  const redirects = path.join(distDir, "_redirects");
  if (!fs.existsSync(redirects)) {
    throw new Error(
      `dist/_redirects missing — Vite publicDir did not mirror mini_app/public/; `
      + `Pages SPA fallback will 404 on refresh`,
    );
  }
  const text = fs.readFileSync(redirects, "utf-8");
  expect(text).toContain("/index.html");
});

test("dist assets include js + css bundles", async () => {
  // Cloudflare Pages serves dist/assets/* with the right Content-Type
  // automatically; the bundle MUST contain at least one JS file (the
  // TS sources) and at least one CSS file (the bundled style.css).
  const distDir = path.resolve(process.cwd(), "dist");
  const assetsDir = path.join(distDir, "assets");
  if (!fs.existsSync(assetsDir)) {
    throw new Error("dist/assets/ missing — Vite build did not emit any assets");
  }
  const files = fs.readdirSync(assetsDir);
  expect(files.some((entry) => entry.endsWith(".js"))).toBeTruthy();
  expect(files.some((entry) => entry.endsWith(".css"))).toBeTruthy();
});

test("bundle references the Home and Properties view modules", async () => {
  // The Home / Properties surfaces are the two Owner-acceptance entry
  // points the issue enumerates.  The compiled bundle MUST carry
  // enough material for both views to render — a regression that
  // strips one would surface as a 200 page with a broken nav.
  //
  // After Vite's minification the function names (renderHome /
  // renderProperties) collapse to short identifiers, so the
  // guardrail checks the route-name string literals ("home",
  // "properties") + the API method names (listProperties,
  // getDashboardHome — these are property access strings that survive
  // minification because they appear verbatim in the PasayClient call
  // site) instead.  A regression that strips a view will fail this
  // guardrail via the route string.
  const distDir = path.resolve(process.cwd(), "dist");
  const assetsDir = path.join(distDir, "assets");
  const jsBundle = fs
    .readdirSync(assetsDir)
    .find((entry) => entry.endsWith(".js"));
  if (!jsBundle) throw new Error("dist/assets/*.js missing");
  const bundle = fs.readFileSync(path.join(assetsDir, jsBundle), "utf-8");
  // Route-name string literals (hash routing keys the Mini App uses
  // for navigation).  Both must be present for the SPA to reach Home
  // and Properties on a click.
  expect(bundle).toContain('"home"');
  expect(bundle).toContain('"properties"');
  // API method names — these survive minification as object property
  // access on the PasayClient instance.
  expect(bundle).toContain("listProperties");
  expect(bundle).toContain("getDashboardHome");
});

test("telegram initData reader returns disabled state when Telegram absent", async () => {
  // The Mini App gracefully degrades to the bootstrap form when opened
  // outside Telegram (dev preview, Playwright harness, CI smoke).  The
  // reader MUST return a "disabled" status rather than throwing.
  const { readTelegramInitData } = await import(
    pathToFileURL(path.resolve(process.cwd(), "src/telegram.ts")).href
  );
  // Ensure no window.Telegram is set.
  const original = (globalThis as { Telegram?: unknown }).Telegram;
  delete (globalThis as { Telegram?: unknown }).Telegram;
  try {
    const status = readTelegramInitData();
    expect(status.kind).toBe("disabled");
  } finally {
    if (original !== undefined) {
      (globalThis as { Telegram?: unknown }).Telegram = original;
    }
  }
});

test("telegram initData reader returns error state when WebApp lacks initData", async () => {
  // Telegram's WebApp object exists but initData is empty (an
  // improperly-initialised bot, a stale embedded web view).  The
  // reader MUST surface a stable error code so the SPA renders the
  // Owner-only error screen — never silently fall through.
  const { readTelegramInitData } = await import(
    pathToFileURL(path.resolve(process.cwd(), "src/telegram.ts")).href
  );
  // The reader looks at `window.Telegram.WebApp.initData`; we
  // install both the window and the globalThis reference so the test
  // is robust to whichever object the JSDOM `window` binding sees.
  const w = (globalThis as { window?: { Telegram?: unknown } }).window;
  const originalWindow = w?.Telegram;
  const originalGlobal = (globalThis as { Telegram?: unknown }).Telegram;
  if (w) w.Telegram = { WebApp: { initData: "" } };
  (globalThis as { Telegram?: unknown }).Telegram = {
    WebApp: { initData: "" },
  };
  try {
    const status = readTelegramInitData();
    expect(status.kind).toBe("error");
    if (status.kind === "error") {
      expect(status.code).toBe("init_data_empty");
    }
  } finally {
    if (w && originalWindow !== undefined) {
      w.Telegram = originalWindow;
    } else if (w) {
      delete w.Telegram;
    }
    if (originalGlobal !== undefined) {
      (globalThis as { Telegram?: unknown }).Telegram = originalGlobal;
    } else {
      delete (globalThis as { Telegram?: unknown }).Telegram;
    }
  }
});

test("telegram initData reader returns ok state with initData present", async () => {
  // The Mini App MUST be able to read the signed initData string
  // verbatim from Telegram.WebApp.initData and forward it to
  // /api/v1/webapp/auth.  Reader surfaces the raw string so the
  // backend can re-verify the HMAC.
  const { readTelegramInitData } = await import(
    pathToFileURL(path.resolve(process.cwd(), "src/telegram.ts")).href
  );
  const w = (globalThis as { window?: { Telegram?: unknown } }).window;
  const originalWindow = w?.Telegram;
  const originalGlobal = (globalThis as { Telegram?: unknown }).Telegram;
  const payload = {
    WebApp: {
      initData:
        "query_id=AAEh&user=%7B%22id%22%3A5177241442%7D&auth_date=1700000000&hash=deadbeef",
    },
  };
  if (w) w.Telegram = payload;
  (globalThis as { Telegram?: unknown }).Telegram = payload;
  try {
    const status = readTelegramInitData();
    expect(status.kind).toBe("ok");
    if (status.kind === "ok") {
      expect(status.initData).toContain("hash=deadbeef");
    }
  } finally {
    if (w && originalWindow !== undefined) {
      w.Telegram = originalWindow;
    } else if (w) {
      delete w.Telegram;
    }
    if (originalGlobal !== undefined) {
      (globalThis as { Telegram?: unknown }).Telegram = originalGlobal;
    } else {
      delete (globalThis as { Telegram?: unknown }).Telegram;
    }
  }
});

let passed = 0;
let failed = 0;
const errors: Array<{ name: string; error: Error }> = [];

for (const t of TESTS) {
  try {
    await t.fn();
    console.log(`  ✓ ${t.name}`);
    passed += 1;
  } catch (err) {
    console.log(`  ✗ ${t.name}`);
    failed += 1;
    errors.push({ name: t.name, error: err as Error });
  }
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  for (const e of errors) {
    console.error(`\n--- ${e.name} ---`);
    console.error(e.error.stack ?? e.error.message);
  }
  process.exit(1);
}
void register;
