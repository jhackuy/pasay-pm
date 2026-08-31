// Playwright real-browser smoke for the PASAY V1 Mini App.
//
// Issue #99 OWNER ADDENDUM + Spec Kit T-066: an API-backed Owner core
// flow must be exercised through a real headless browser, not just JSDOM.
// This script drives the actual Vite bundle against a running FastAPI
// instance.
//
// Coverage (one Owner core flow):
//   1. GET /                 -> bootstrap form is visible (no API key yet)
//   2. submit bootstrap      -> real POST /api/v1/bootstrap, redirect to
//                               home shell with 5-tab nav rendered
//   3. assert shell          -> 5 nav buttons (Home / Properties / Work /
//                               Finance / More) plus locale toggle, sign-out
//   4. click Properties tab  -> renders real GET /api/v1/properties
//   5. open new-property form + submit
//                            -> real POST /api/v1/properties, list now
//                               shows the property
//   6. click property row    -> renders real GET /api/v1/properties/:id/units
//   7. open add-unit form + submit
//                            -> real POST /api/v1/properties/:id/units,
//                               unit now visible in detail view
//   8. click Home tab        -> dashboard renders 4 KPI cards with real
//                               values (overdue / pending claims / open
//                               repairs / active leases)
//   9. localStorage assertion: apiKey, orgId, userId, role NEVER present.
//                               Only pasay.locale (UI preference) is
//                               allowed. This is the AGENTS.md §4 invariant
//                               for the Mini App.
//
// Inputs:
//   - BASE_URL env var (e.g. http://127.0.0.1:51873)
//
// Output:
//   - exits 0 on full pass, 1 on first failure.
//   - on failure: dumps the failing page URL, the failing assertion
//     message, and a body snippet (last 600 chars of #view-root innerHTML).
//
// Usage (called by run_browser_smoke.mjs, not directly):
//   node tests/browser_smoke.mjs

import { chromium } from "playwright";

const BASE = process.env.BASE_URL;
if (!BASE) {
  console.error("FAIL: BASE_URL env var is required");
  process.exit(2);
}

const checks = [];
let failed = false;
function record(label, ok, detail = "") {
  checks.push({ label, ok, detail });
  if (!ok) failed = true;
  const tag = ok ? "OK  " : "FAIL";
  console.log(`${tag} ${label}${detail ? " — " + detail : ""}`);
}

function failDetail(page, label, err) {
  let snippet = "";
  try {
    snippet = page
      .evaluate(() => {
        const root = document.querySelector("#view-root");
        const text = root ? root.innerText : "";
        return text.length > 600 ? text.slice(-600) : text;
      })
      .catch(() => "(could not read #view-root)");
  } catch (_) {
    snippet = "(evaluate failed)";
  }
  console.error(`\n--- FAILURE: ${label} ---`);
  console.error(`URL: ${page.url()}`);
  console.error(`Error: ${err && err.stack ? err.stack : String(err)}`);
  console.error(`Last view snippet: ${snippet}`);
}

async function expect(cond, label, page, detailFn) {
  if (cond) {
    record(label, true);
    return;
  }
  const detail = detailFn ? await detailFn() : "";
  record(label, false, detail);
  if (page) {
    failDetail(page, label, new Error("assertion failed"));
  }
  throw new Error(`assertion failed: ${label}`);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 410, height: 820 },
    locale: "en-US",
  });
  context.on("weberror", (e) => {
    console.error("weberror:", e.error());
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => {
    console.error("pageerror:", e.message);
  });
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.error("[browser console.error]", msg.text());
    }
  });

  try {
    // (1) bootstrap screen is visible.
    await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
    await page.waitForSelector("#bootstrap-form", { timeout: 10000 });
    await expect(
      await page.locator("#bootstrap-form").isVisible(),
      "1. bootstrap form is rendered before auth",
      page,
    );

    // (2) submit bootstrap.
    await page.fill('input[name="workspace_name"]', "Browser Smoke Workspace");
    await page.fill('input[name="owner_username"]', "smoke-owner");
    await page.fill('input[name="owner_display_name"]', "Smoke Owner");
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.url().endsWith("/api/v1/bootstrap") && resp.request().method() === "POST",
        { timeout: 10000 },
      ),
      page.locator('button[type="submit"]').first().click(),
    ]);

    // (3) shell with 5 tabs.
    await page.waitForSelector(".app-nav", { timeout: 10000 });
    const navButtons = await page.locator(".app-nav .nav-btn").all();
    await expect(
      navButtons.length === 5,
      "2. nav shell renders 5 tabs after bootstrap",
      page,
      async () => `actual=${navButtons.length}`,
    );
    // The buttons always carry the English route name in `data-route`;
    // only the visible label flips with locale (zh default / en toggle).
    const navRoutes = await Promise.all(
      navButtons.map((b) => b.getAttribute("data-route")),
    );
    await expect(
      navRoutes.includes("properties") &&
        navRoutes.includes("work") &&
        navRoutes.includes("finance") &&
        navRoutes.includes("more"),
      "3. nav data-route attributes include properties / work / finance / more",
      page,
      async () => `actual=${JSON.stringify(navRoutes)}`,
    );
    // Visible labels must be non-empty for every tab in the active locale.
    const navLabels = await Promise.all(
      navButtons.map((b) => b.locator("span").nth(1).innerText()),
    );
    await expect(
      navLabels.every((label) => label && label.length > 0),
      "4. every nav tab has a non-empty visible label",
      page,
      async () => `actual=${JSON.stringify(navLabels)}`,
    );

    // (4) Properties tab.
    const propertiesTab = page.locator('.nav-btn[data-route="properties"]');
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          /\/api\/v1\/properties(?:\?|$)/.test(resp.url()) &&
          resp.request().method() === "GET",
        { timeout: 10000 },
      ),
      propertiesTab.click(),
    ]);
    await page.waitForSelector('[data-action="new-property"]', { timeout: 10000 });

    // (5) New property form + submit.
    await page.locator('[data-action="new-property"]').click();
    await page.waitForSelector("#property-form", { timeout: 5000 });
    await page.fill('#property-form input[name="name"]', "Smoke Plaza");
    await page.fill('#property-form input[name="city"]', "Pasay City");
    const created = await Promise.all([
      page.waitForResponse(
        (resp) =>
          /\/api\/v1\/properties(?:\?|$)/.test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#property-form button[type="submit"]').click(),
    ]);
    const createdJson = await created[0].json();
    const propertyId = createdJson.id;
    await expect(
      Number.isInteger(propertyId) && propertyId > 0,
      "5. POST /api/v1/properties returned a numeric id",
      page,
      async () => `id=${propertyId}`,
    );
    // The property should now appear in the list.
    await page.waitForSelector(`[data-action="open-property"][data-id="${propertyId}"]`, {
      timeout: 5000,
    });

    // (6) Click property row -> detail + units.
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/properties/${propertyId}/units(?:\\?|$)`).test(resp.url()) &&
          resp.request().method() === "GET",
        { timeout: 10000 },
      ),
      page.locator(`[data-action="open-property"][data-id="${propertyId}"]`).click(),
    ]);
    await page.waitForSelector('[data-action="new-unit"]', { timeout: 5000 });

    // (7) Add unit form + submit.
    await page.locator('[data-action="new-unit"]').click();
    await page.waitForSelector("#unit-form", { timeout: 5000 });
    await page.fill('#unit-form input[name="label"]', "Unit-A");
    await page.fill('#unit-form input[name="monthly_rent"]', "12345.67");
    const unitCreated = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/properties/${propertyId}/units(?:\\?|$)`).test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#unit-form button[type="submit"]').click(),
    ]);
    const unitJson = await unitCreated[0].json();
    await expect(
      unitJson.label === "Unit-A" && unitJson.monthly_rent === "12345.67",
      "6. POST /api/v1/properties/:id/units persisted Decimal money as string",
      page,
      async () =>
        `label=${unitJson.label} monthly_rent=${unitJson.monthly_rent} ` +
        `status=${unitJson.status}`,
    );
    // Re-rendered detail view should show the new unit.
    await page.waitForFunction(
      (id) => document.body.innerText.includes("Unit-A"),
      propertyId,
      { timeout: 5000 },
    );

    // (8) Home tab -> dashboard KPIs from real API. The Home view fires
    //     four parallel GETs (overdue / claims / repairs / leases); any of
    //     them resolves before the .kpi cards render.
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.request().method() === "GET" &&
          /\/api\/v1\/(?:rent|repairs|leases)\b/.test(resp.url()),
        { timeout: 10000 },
      ),
      page.locator('.nav-btn[data-route="home"]').click(),
    ]);
    await page.waitForSelector(".kpi", { timeout: 10000 });
    const kpiCount = await page.locator(".kpi").count();
    await expect(
      kpiCount === 4,
      "7. home dashboard renders 4 KPI cards",
      page,
      async () => `kpiCount=${kpiCount}`,
    );

    // (9) localStorage must NOT contain business truth.
    const ls = await page.evaluate(() => {
      const out = {};
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const k = window.localStorage.key(i);
        out[k] = window.localStorage.getItem(k);
      }
      return out;
    });
    const forbiddenKeys = ["apiKey", "orgId", "userId", "role", "api_key", "org_id"];
    const leaks = Object.keys(ls).filter((k) => forbiddenKeys.includes(k));
    await expect(
      leaks.length === 0,
      "7. localStorage does NOT persist business truth (apiKey/orgId/userId/role)",
      page,
      async () => `keys=${JSON.stringify(Object.keys(ls))} leaks=${JSON.stringify(leaks)}`,
    );
    // Only the UI preference key is allowed.
    await expect(
      Object.keys(ls).length === 0 || Object.keys(ls).every((k) => k === "pasay.locale"),
      "8. only pasay.locale may live in localStorage",
      page,
      async () => `keys=${JSON.stringify(Object.keys(ls))}`,
    );
  } catch (err) {
    if (!failed) {
      record("(uncaught)", false, String(err && err.message ? err.message : err));
      failed = true;
    }
    if (page) failDetail(page, "(uncaught)", err);
  } finally {
    await context.close();
    await browser.close();
  }

  const passed = checks.filter((c) => c.ok).length;
  const total = checks.length;
  console.log(`\nbrowser_smoke: ${passed}/${total} checks passed`);
  if (failed) {
    console.error("FAILED checks:");
    for (const c of checks) {
      if (!c.ok) console.error(`  - ${c.label}${c.detail ? " :: " + c.detail : ""}`);
    }
    process.exit(1);
  }
  process.exit(0);
}

main().catch((err) => {
  console.error("browser_smoke top-level error:", err);
  process.exit(3);
});
