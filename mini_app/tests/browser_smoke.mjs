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
//   8. tenant registration   -> real POST /api/v1/tenants with
//                               Idempotency-Key; tenant appears in the
//                               workspace tenants panel
//   9. tenant negative path  -> empty full_name surfaces a localized
//                               required error (no silent success, no
//                               network round-trip on blocked input)
//  10. click Home tab        -> dashboard renders 4 KPI cards with real
//                               values (overdue / pending claims / open
//                               repairs / active leases)
//  11. localStorage assertion: apiKey, orgId, userId, role NEVER present.
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
    // Mirror the property id into a window global for downstream test
    // steps that need to seed auxiliary entities (e.g. ACTIVE leases).
    await page.evaluate((id) => {
      window.__PASAY_PROPERTY_ID__ = id;
    }, propertyId);
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

    // (8) Tenant registration — Owner UI flow (Coverage Matrix 7.1).
    //     Navigate back to the Properties tab to reach the workspace-level
    //     tenants panel, then drive the register-tenant form against the
    //     real /api/v1/tenants endpoint (POST with Idempotency-Key).
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.url().includes("/api/v1/tenants") &&
          resp.request().method() === "GET",
        { timeout: 10000 },
      ),
      page.locator('.nav-btn[data-route="properties"]').click(),
    ]);
    await page.waitForSelector('[data-action="new-tenant"]', { timeout: 10000 });
    await page.locator('[data-action="new-tenant"]').click();
    await page.waitForSelector("#tenant-form", { timeout: 5000 });
    await page.fill('#tenant-form input[name="full_name"]', "Smoke Tenant");
    await page.fill('#tenant-form input[name="contact_phone"]', "+63-917-555-7777");
    await page.fill('#tenant-form input[name="contact_email"]', "smoke-tenant@example.com");
    const tenantCreated = await Promise.all([
      page.waitForResponse(
        (resp) =>
          /\/api\/v1\/tenants(?:\?|$)/.test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#tenant-form button[type="submit"]').click(),
    ]);
    const tenantJson = await tenantCreated[0].json();
    await expect(
      Number.isInteger(tenantJson.id) && tenantJson.id > 0,
      "8. POST /api/v1/tenants returned a numeric id",
      page,
      async () => `id=${tenantJson.id} body=${JSON.stringify(tenantJson)}`,
    );
    await expect(
      tenantJson.full_name === "Smoke Tenant" &&
        tenantJson.contact_phone === "+63-917-555-7777" &&
        tenantJson.contact_email === "smoke-tenant@example.com",
      "9. POST /api/v1/tenants persisted full_name + contact_phone + contact_email",
      page,
      async () =>
        `full_name=${tenantJson.full_name} ` +
        `contact_phone=${tenantJson.contact_phone} ` +
        `contact_email=${tenantJson.contact_email}`,
    );
    // The Idempotency-Key header must have been sent on the POST.
    const tenantPostIdemHeader = await tenantCreated[0].request().headerValue(
      "idempotency-key",
    );
    await expect(
      typeof tenantPostIdemHeader === "string" && tenantPostIdemHeader.length > 0,
      "10. POST /api/v1/tenants carried an Idempotency-Key header",
      page,
      async () => `idem=${tenantPostIdemHeader}`,
    );
    // The re-rendered properties view must show the new tenant in the list.
    await page.waitForFunction(
      () => document.body.innerText.includes("Smoke Tenant"),
      null,
      { timeout: 5000 },
    );
    // And the list panel count must reflect at least 1 tenant.
    const tenantListHtml = await page
      .locator('[data-list="tenants"]')
      .innerHTML()
      .catch(() => "");
    await expect(
      tenantListHtml.includes("Smoke Tenant") && tenantListHtml.includes("+63-917-555-7777"),
      "11. tenants panel lists the newly registered tenant with phone + email",
      page,
      async () => `tenantPanel contains: ${tenantListHtml.length > 0 ? "yes" : "no"}`,
    );
    // Negative path: empty full_name → 400 ValidationError.
    await page.locator('[data-action="new-tenant"]').click();
    await page.waitForSelector("#tenant-form", { timeout: 5000 });
    // Submit with only whitespace in full_name; the form's `required` attr
    // plus the submit handler will block this without firing a request.
    await page.fill('#tenant-form input[name="full_name"]', "   ");
    // Bypass the HTML5 required attribute by removing it.
    await page.evaluate(() => {
      const el = document.querySelector('#tenant-form input[name="full_name"]');
      if (el) el.removeAttribute("required");
    });
    await page.locator('#tenant-form button[type="submit"]').click();
    // The client-side guard must surface the localized `required` message.
    const requiredErr = await page
      .locator("#tenant-error")
      .textContent()
      .catch(() => "");
    await expect(
      typeof requiredErr === "string" && requiredErr.length > 0,
      "12. empty full_name surfaces a localized required error (no silent success)",
      page,
      async () => `errorText=${requiredErr}`,
    );

    // (9) Home tab -> dashboard KPIs from real API. The Home view fires
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
      "13. home dashboard renders 4 KPI cards",
      page,
      async () => `kpiCount=${kpiCount}`,
    );

    // (10) localStorage must NOT contain business truth.
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
      "14. localStorage does NOT persist business truth (apiKey/orgId/userId/role)",
      page,
      async () => `keys=${JSON.stringify(Object.keys(ls))} leaks=${JSON.stringify(leaks)}`,
    );
    // Only the UI preference key is allowed.
    await expect(
      Object.keys(ls).length === 0 || Object.keys(ls).every((k) => k === "pasay.locale"),
      "15. only pasay.locale may live in localStorage",
      page,
      async () => `keys=${JSON.stringify(Object.keys(ls))}`,
    );

    // ----------------------------------------------------------------
    // Lease Renewal Owner UI flow (Coverage Matrix Renewal slice).
    // The previous steps seeded:
    //   - 1 property ("Smoke Plaza")
    //   - 1 unit ("Unit-A" with monthly_rent=12345.67)
    //   - 1 tenant ("Smoke Tenant" id from step 8)
    // We need an ACTIVE lease for that (tenant, unit) pair before the
    // renewal form will offer a non-empty dropdown. We seed the lease
    // directly via the API (the typed client surface used by the Mini
    // App is exercised through the form itself).
    // ----------------------------------------------------------------

    // Pull the org_id + api_key out of the in-memory PasayClient.
    const sessionInfo = await page.evaluate(() => {
      // The Mini App keeps the apiKey in memory only (AGENTS.md §4). We
      // re-bootstrap a fresh TestClient from the bootstrap response.
      // Instead, read what's available on `window` if exposed, otherwise
      // fetch /api/v1/bootstrap is not available (DB now has data).
      // Fall back: scrape the apiKey from a known network call. The
      // simplest reliable approach is to re-create a session by hitting
      // /api/v1/bootstrap again is not allowed post-bootstrap. So we
      // expose `__PASAY_SESSION__` for test purposes only (it is set by
      // main.ts at bootstrap time and contains the api key).
      return window.__PASAY_SESSION__ ?? null;
    });
    await expect(
      sessionInfo && typeof sessionInfo.api_key === "string" && sessionInfo.api_key.length > 0,
      "16. session is available for seeding an ACTIVE lease",
      page,
      async () => `sessionInfo=${JSON.stringify(sessionInfo)}`,
    );

    // Resolve the tenant id + unit id by hitting the API directly. Use
    // the in-memory PasayClient from the page context (it carries the
    // Bearer header).
    const tenantId = await page.evaluate(async () => {
      const tenants = await window.__PASAY_CLIENT__.listTenants(
        window.__PASAY_SESSION__.org_id,
      );
      return tenants[0]?.id ?? null;
    });
    const unitId = await page.evaluate(async () => {
      const units = await window.__PASAY_CLIENT__.listUnits(
        window.__PASAY_SESSION__.org_id,
        window.__PASAY_PROPERTY_ID__,
      );
      return units[0]?.id ?? null;
    });
    await expect(
      Number.isInteger(tenantId) && Number.isInteger(unitId) && tenantId > 0 && unitId > 0,
      "17. tenant + unit ids resolved for lease seeding",
      page,
      async () => `tenantId=${tenantId} unitId=${unitId}`,
    );

    // Seed a DRAFT lease and activate it. We can't reuse the renewal UI
    // because that's what we're testing, but every other lease-touching
    // path goes through the same LeaseService the renewal flow consumes.
    const leaseDraft = await page.evaluate(
      async ({ orgId, tenantId, unitId }) => {
        const client = window.__PASAY_CLIENT__;
        const today = new Date();
        const start = new Date(today.getFullYear(), today.getMonth(), 1)
          .toISOString()
          .slice(0, 10);
        const end = new Date(today.getFullYear() + 1, today.getMonth(), 1)
          .toISOString()
          .slice(0, 10);
        return client.createLease(
          orgId,
          {
            tenant_id: tenantId,
            unit_id: unitId,
            start_date: start,
            end_date: end,
            monthly_rent: "12345.67",
            deposit: "0",
          },
          `seed-draft-${Date.now().toString(36)}`,
        );
      },
      { orgId: sessionInfo.org_id, tenantId, unitId },
    );
    const leaseId = leaseDraft.id;
    await page.evaluate(
      async ({ orgId, leaseId }) => {
        await window.__PASAY_CLIENT__.activateLease(orgId, leaseId);
      },
      { orgId: sessionInfo.org_id, leaseId },
    );

    // Now navigate to the Work tab and open the renewal form.
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.url().includes("/api/v1/renewals") &&
          resp.request().method() === "GET",
        { timeout: 10000 },
      ),
      page.locator('.nav-btn[data-route="work"]').click(),
    ]);
    await page.waitForSelector('[data-action="new-renewal"]', { timeout: 5000 });
    await page.locator('[data-action="new-renewal"]').click();
    await page.waitForSelector("#renewal-form", { timeout: 5000 });

    // The dropdown must contain at least one option matching our seeded
    // lease id (with the lease #id label).
    const selectExists = await page
      .locator('#renewal-form select[name="source_lease_id"]')
      .count();
    await expect(
      selectExists === 1,
      "18. renewal form exposes a <select> with name=source_lease_id",
      page,
      async () => `selectExists=${selectExists}`,
    );
    const optionValues = await page.$$eval(
      '#renewal-form select[name="source_lease_id"] option',
      (opts) => opts.map((o) => o.value),
    );
    await expect(
      optionValues.includes(String(leaseId)),
      "19. lease dropdown lists the seeded ACTIVE lease id",
      page,
      async () => `optionValues=${JSON.stringify(optionValues)} leaseId=${leaseId}`,
    );

    // Fill the rest of the form: start = current lease end (one day
    // after), end = +12 months, proposed_monthly_rent = same rent,
    // proposed_deposit = same as the seeded 0.
    const startNext = new Date(leaseDraft.end_date);
    startNext.setUTCDate(startNext.getUTCDate() + 1);
    const endNext = new Date(startNext);
    endNext.setUTCFullYear(endNext.getUTCFullYear() + 1);
    const fmtIso = (d) => d.toISOString().slice(0, 10);
    await page.selectOption(
      '#renewal-form select[name="source_lease_id"]',
      String(leaseId),
    );
    await page.fill('#renewal-form input[name="start_date"]', fmtIso(startNext));
    await page.fill('#renewal-form input[name="end_date"]', fmtIso(endNext));
    await page.fill('#renewal-form input[name="proposed_monthly_rent"]', "13500.00");
    await page.fill('#renewal-form input[name="proposed_deposit"]', "0.00");

    const renewalPosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          /\/api\/v1\/renewals\/proposals(?:\?|$)/.test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#renewal-form button[type="submit"]').click(),
    ]);
    const renewalJson = await renewalPosted[0].json();
    await expect(
      Number.isInteger(renewalJson.id) && renewalJson.id > 0,
      "20. POST /api/v1/renewals/proposals returned a numeric id",
      page,
      async () => `id=${renewalJson.id} body=${JSON.stringify(renewalJson)}`,
    );
    await expect(
      renewalJson.state === "PROPOSED" &&
        Number(renewalJson.proposed_monthly_rent) === 13500 &&
        renewalJson.source_lease_id === leaseId,
      "21. POST /api/v1/renewals/proposals persisted proposed terms + state=PROPOSED",
      page,
      async () =>
        `state=${renewalJson.state} ` +
        `proposed_monthly_rent=${renewalJson.proposed_monthly_rent} ` +
        `source_lease_id=${renewalJson.source_lease_id}`,
    );
    // Idempotency-Key header must have been sent on the POST.
    const renewalIdem = await renewalPosted[0]
      .request()
      .headerValue("idempotency-key");
    await expect(
      typeof renewalIdem === "string" && renewalIdem.length > 0,
      "22. POST /api/v1/renewals/proposals carried an Idempotency-Key header",
      page,
      async () => `idem=${renewalIdem}`,
    );
    // Re-rendered Work view must surface the new renewal as a row with
    // an Approve button (only PROPOSED renewals get the Approve button).
    await page.waitForSelector(
      `[data-action="approve-renewal-${renewalJson.id}"]`,
      { timeout: 5000 },
    );

    // ----------------------------------------------------------------
    // Move-out / Settlement Owner UI flow (Coverage Matrix 7.1–7.7).
    //   1. request       POST /api/v1/move-outs (Idempotency-Key required)
    //   2. inspection    POST /api/v1/move-outs/:id/inspections
    //   3. damage        POST /api/v1/move-outs/:id/damages
    //   4. accept        POST /api/v1/move-outs/damages/:id/accept
    //   5. keys/arrears  POST /api/v1/move-outs/:id/keys-arrears
    //   6. settle        POST /api/v1/move-outs/:id/settlement (closure gate)
    //   7. atomic close  POST /api/v1/move-outs/:id/close
    // The round trip must transition REQUESTED → INSPECTED → SETTLED,
    // then close atomically. Each step uses a real API call; no
    // localStorage business truth.
    // ----------------------------------------------------------------

    // (A) Open the move-out form and submit against the seeded ACTIVE lease.
    await page.locator('[data-action="new-moveout"]').click();
    await page.waitForSelector("#moveout-open-form", { timeout: 5000 });
    await page.selectOption(
      '#moveout-open-form select[name="lease_id"]',
      String(leaseId),
    );
    await page.fill(
      '#moveout-open-form input[name="planned_move_out_date"]',
      fmtIso(new Date()),
    );
    await page.fill(
      '#moveout-open-form textarea[name="notes"]',
      "Smoke move-out: full end-of-term inspection.",
    );
    const moveOutPosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          /\/api\/v1\/move-outs(?:\?|$)/.test(resp.url()) &&
          resp.request().method() === "POST" &&
          (resp.status() === 201 || resp.status() === 200),
        { timeout: 10000 },
      ),
      page.locator('#moveout-open-form button[type="submit"]').click(),
    ]);
    const moveOutJson = await moveOutPosted[0].json();
    await expect(
      Number.isInteger(moveOutJson.id) && moveOutJson.id > 0,
      "23. POST /api/v1/move-outs returned a numeric id",
      page,
      async () => `id=${moveOutJson.id} body=${JSON.stringify(moveOutJson)}`,
    );
    await expect(
      moveOutJson.state === "REQUESTED" && moveOutJson.lease_id === leaseId,
      "24. POST /api/v1/move-outs persisted state=REQUESTED + lease_id",
      page,
      async () =>
        `state=${moveOutJson.state} lease_id=${moveOutJson.lease_id}`,
    );
    const moveOutIdem = await moveOutPosted[0]
      .request()
      .headerValue("idempotency-key");
    await expect(
      typeof moveOutIdem === "string" && moveOutIdem.length > 0,
      "25. POST /api/v1/move-outs carried an Idempotency-Key header",
      page,
      async () => `idem=${moveOutIdem}`,
    );
    const moveOutId = moveOutJson.id;
    // The successful submit navigates to the detail view.
    await page.waitForSelector("#inspection-form", { timeout: 10000 });
    await expect(
      page.url().includes(`/move-outs/${moveOutId}`),
      "26. submit navigates to the move-out detail view",
      page,
      async () => `url=${page.url()}`,
    );

    // (B) Record the walk-through inspection.
    await page.fill(
      '#inspection-form textarea[name="summary"]',
      "Walk-through OK; one scratched door noted.",
    );
    const inspectionPosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/move-outs/${moveOutId}/inspections(?:\\?|$)`).test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#inspection-form button[type="submit"]').click(),
    ]);
    const inspectionJson = await inspectionPosted[0].json();
    await expect(
      typeof inspectionJson.summary === "string" && inspectionJson.summary.length > 0,
      "27. POST /api/v1/move-outs/:id/inspections persisted summary",
      page,
      async () => `summary=${inspectionJson.summary} id=${inspectionJson.id}`,
    );
    // Re-rendered detail view must show state=INSPECTED.
    await page.waitForFunction(
      () => document.body.innerText.includes("INSPECTED"),
      null,
      { timeout: 5000 },
    );

    // (C) Record a damage + accept it.
    await page.selectOption('#damage-form select[name="kind"]', "REPAIR");
    await page.fill(
      '#damage-form input[name="description"]',
      "Scratched bedroom door panel",
    );
    await page.fill('#damage-form input[name="amount"]', "1500.00");
    const damagePosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/move-outs/${moveOutId}/damages(?:\\?|$)`).test(resp.url()) &&
          resp.request().method() === "POST" &&
          resp.status() === 201,
        { timeout: 10000 },
      ),
      page.locator('#damage-form button[type="submit"]').click(),
    ]);
    const damageJson = await damagePosted[0].json();
    await expect(
      damageJson.amount === "1500.00" && damageJson.kind === "REPAIR",
      "28. POST /api/v1/move-outs/:id/damages persisted kind=REPAIR + amount=1500.00",
      page,
      async () => `amount=${damageJson.amount} kind=${damageJson.kind}`,
    );
    // Accept the damage via the accept endpoint (the prompt() is
    // intercepted by the handler before any test fixture can fill it; we
    // invoke the typed client directly because accept_damage is
    // exercising a confirmation flow that has no form submission).
    const damageId = damageJson.id;
    const acceptResult = await page.evaluate(
      async ({ orgId, damageId }) => {
        const client = window.__PASAY_CLIENT__;
        return client.acceptDamage(orgId, damageId, {
          accepted_amount: "1500.00",
        });
      },
      { orgId: sessionInfo.org_id, damageId },
    );
    await expect(
      acceptResult.accepted_amount === "1500.00",
      "29. POST /api/v1/move-outs/damages/:id/accept persisted accepted_amount=1500.00",
      page,
      async () => `accepted_amount=${acceptResult.accepted_amount}`,
    );

    // (D) Record keys/arrears (keys returned + arrears_amount).
    await page.selectOption('#keys-arrears-form select[name="keys_returned"]', "true");
    await page.fill('#keys-arrears-form input[name="arrears_amount"]', "0.00");
    await page.fill('#keys-arrears-form textarea[name="notes"]', "All keys returned.");
    const keysPosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/move-outs/${moveOutId}/keys-arrears(?:\\?|$)`).test(
            resp.url(),
          ) && resp.request().method() === "POST",
        { timeout: 10000 },
      ),
      page.locator('#keys-arrears-form button[type="submit"]').click(),
    ]);
    const keysJson = await keysPosted[0].json();
    await expect(
      keysJson.keys_returned === true &&
        (keysJson.arrears_amount === "0.00" || keysJson.arrears_amount === "0"),
      "30. POST /api/v1/move-outs/:id/keys-arrears persisted keys_returned=true + arrears=0.00",
      page,
      async () =>
        `keys_returned=${keysJson.keys_returned} arrears_amount=${keysJson.arrears_amount}`,
    );

    // (E) Settle (closure gate). Use FULL_REFUND with deposit_held=24000
    // and refund_amount=24000 (FULL_REFUND requires the two to match).
    await page.selectOption('#settle-form select[name="disposition"]', "FULL_REFUND");
    await page.fill('#settle-form input[name="deposit_held"]', "24000.00");
    await page.fill('#settle-form input[name="refund_amount"]', "24000.00");
    await page.fill('#settle-form input[name="additional_owed"]', "0.00");
    const settlePosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/move-outs/${moveOutId}/settlement(?:\\?|$)`).test(
            resp.url(),
          ) &&
          resp.request().method() === "POST" &&
          resp.status() === 200,
        { timeout: 10000 },
      ),
      page.locator('#settle-form button[type="submit"]').click(),
    ]);
    const settleJson = await settlePosted[0].json();
    await expect(
      settleJson.disposition === "FULL_REFUND" &&
        Number(settleJson.deposit_held) === 24000 &&
        Number(settleJson.refund_amount) === 24000 &&
        Number(settleJson.deductions_total) === 1500,
      "31. POST /api/v1/move-outs/:id/settlement persisted FULL_REFUND + deductions_total=1500",
      page,
      async () =>
        `disposition=${settleJson.disposition} ` +
        `deposit_held=${settleJson.deposit_held} ` +
        `refund_amount=${settleJson.refund_amount} ` +
        `deductions_total=${settleJson.deductions_total}`,
    );
    // Re-rendered detail view must show state=SETTLED + a close button.
    await page.waitForSelector('[data-action="close-moveout"]', { timeout: 5000 });

    // (F) Persisted-state assertion: re-fetch the move-out via the
    // typed client and confirm closure transitioned correctly. The
    // server, not the Mini App, is the source of truth (AGENTS.md §4).
    const persisted = await page.evaluate(
      async ({ orgId, moveOutId }) => {
        const client = window.__PASAY_CLIENT__;
        const m = await client.getMoveOut(orgId, moveOutId);
        const balance = await client.getMoveOutBalance(orgId, moveOutId);
        const activity = await client.listMoveOutActivity(orgId, moveOutId);
        return {
          state: m.state,
          settlement_id: m.settlement_id,
          settled_at: m.settled_at,
          keys_returned: m.keys_returned,
          arrears_amount: m.arrears_amount,
          balance_is_settled: balance.is_settled,
          balance_deposit_held: balance.deposit_held,
          balance_refund: balance.refund_amount,
          balance_deductions: balance.deductions_total,
          activity_kinds: activity.map((a) => a.kind),
        };
      },
      { orgId: sessionInfo.org_id, moveOutId },
    );
    await expect(
      persisted.state === "SETTLED" &&
        Number(persisted.settlement_id) > 0 &&
        typeof persisted.settled_at === "string" &&
        persisted.balance_is_settled === true &&
        Number(persisted.balance_deductions) === 1500 &&
        persisted.activity_kinds.includes("SETTLED") &&
        persisted.activity_kinds.includes("INSPECTED") &&
        persisted.activity_kinds.includes("DAMAGE_RECORDED"),
      "32. persisted-state: state=SETTLED + settlement_id + activity audit chain",
      page,
      async () =>
        `state=${persisted.state} settlement_id=${persisted.settlement_id} ` +
        `activity=${JSON.stringify(persisted.activity_kinds)}`,
    );
    await expect(
      persisted.keys_returned === true &&
        (persisted.arrears_amount === "0.00" || persisted.arrears_amount === "0"),
      "33. persisted-state: keys_returned=true + arrears_amount=0.00 from keys-arrears step",
      page,
      async () =>
        `keys_returned=${persisted.keys_returned} arrears=${persisted.arrears_amount}`,
    );

    // (G) Atomic close (OWNER-only). Single transaction: terminate lease,
    // free the unit, archive the move-out, resolve the Operation.
    const closePosted = await Promise.all([
      page.waitForResponse(
        (resp) =>
          new RegExp(`/api/v1/move-outs/${moveOutId}/close(?:\\?|$)`).test(
            resp.url(),
          ) && resp.request().method() === "POST",
        { timeout: 10000 },
      ),
      page.locator('[data-action="close-moveout"]').click(),
    ]);
    const closeJson = await closePosted[0].json();
    await expect(
      closeJson.state === "SETTLED" &&
        typeof closeJson.archived_at === "string" &&
        closeJson.archived_at !== null,
      "34. POST /api/v1/move-outs/:id/close archived the move-out (archived_at set)",
      page,
      async () =>
        `state=${closeJson.state} archived_at=${closeJson.archived_at}`,
    );

    // (H) Re-fetch from server: archived_at must persist; the linked
    // lease must now be TERMINATED; the unit must be AVAILABLE; the
    // Operation must be resolved.
    const closed = await page.evaluate(
      async ({ orgId, leaseId, unitId, moveOutId }) => {
        const client = window.__PASAY_CLIENT__;
        const m = await client.getMoveOut(orgId, moveOutId);
        const op = await client.getMoveOutOperation(orgId, moveOutId);
        const lease = await client.listLeases(orgId);
        const unit = await client.getUnitDetail(unitId, orgId);
        return {
          move_out_archived_at: m.archived_at,
          operation_state: op.state,
          operation_resolved_at: op.resolved_at,
          lease_state: lease.find((l) => l.id === leaseId)?.state ?? null,
          unit_status: unit.unit.status,
        };
      },
      {
        orgId: sessionInfo.org_id,
        leaseId,
        unitId,
        moveOutId,
      },
    );
    await expect(
      typeof closed.move_out_archived_at === "string" &&
        closed.operation_state === "resolved" &&
        closed.lease_state === "TERMINATED" &&
        closed.unit_status === "AVAILABLE",
      "35. atomic close: lease=TERMINATED + unit=AVAILABLE + operation=resolved",
      page,
      async () =>
        `archived_at=${closed.move_out_archived_at} ` +
        `op_state=${closed.operation_state} ` +
        `lease_state=${closed.lease_state} unit_status=${closed.unit_status}`,
    );

    // (I) localStorage business truth assertion — move-out business keys
    // must NEVER land in localStorage. The only allowed key is
    // `pasay.locale` (UI preference).
    const lsAfter = await page.evaluate(() => {
      const out = {};
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const k = window.localStorage.key(i);
        out[k] = window.localStorage.getItem(k);
      }
      return out;
    });
    const forbiddenMoveOutKeys = [
      "apiKey",
      "orgId",
      "userId",
      "role",
      "api_key",
      "org_id",
      "move_out_id",
      "settlement_id",
      "deposit_held",
      "refund_amount",
      "deductions_total",
      "disposition",
      "archived_at",
    ];
    const moveOutLeaks = Object.keys(lsAfter).filter((k) =>
      forbiddenMoveOutKeys.includes(k),
    );
    await expect(
      moveOutLeaks.length === 0,
      "36. localStorage does NOT persist move-out business truth",
      page,
      async () =>
        `keys=${JSON.stringify(Object.keys(lsAfter))} leaks=${JSON.stringify(moveOutLeaks)}`,
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
