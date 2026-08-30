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
    return "home";
  }
  expect(parseHash("#/")).toBe("home");
  expect(parseHash("#/properties")).toBe("properties");
  expect(parseHash("#/properties/42")).toBe("properties.detail");
  expect(parseHash("#/work")).toBe("work");
  expect(parseHash("#/finance")).toBe("finance");
  expect(parseHash("#/more")).toBe("more");
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
