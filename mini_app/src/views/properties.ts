/** Properties view — list + detail (with units) + workspace tenant panel.
 *
 *  Real workflow: list → open detail → list units → add new property/unit.
 *  Plus the workspace-level tenants panel (Coverage Matrix 7.1: tenant
 *  registration). Registration creates the tenant entity in the org; the
 *  follow-on step (create lease on a unit) is reached from the Work tab.
 *  No localStorage. Idempotency-Key generated fresh per submit.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { router } from "../router";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import { formatMoney, makeIdempotencyKey, statusLabel, statusToneClass } from "../format";
import type { Property, Tenant, Unit } from "../types";

export async function renderProperties(
  client: PasayClient,
  orgId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [items, tenants] = await Promise.all([
      client.listProperties(orgId),
      client.listTenants(orgId),
    ]);
    const body = `
      <section class="panel">
        <header class="panel-header">
          <h2>${t(locale, "properties")}</h2>
          <button class="primary-btn" data-action="new-property" type="button">+ ${t(locale, "newProperty")}</button>
        </header>
        ${items.length === 0
          ? `<div class="empty"><b>${t(locale, "empty")}</b><span>${t(locale, "newProperty")} →</span></div>`
          : `<ul class="list">${items.map((p) => renderPropertyRow(p, locale)).join("")}</ul>`}
      </section>
      ${renderTenantsPanel(tenants, locale)}
    `;
    setViewContent(body);
    bindPropertyHandlers(client, orgId, locale, items);
    bindTenantsPanelHandlers(client, orgId, locale, tenants);
  } catch (err) {
    setViewContent(renderPropertiesError(err, locale));
  }
}

function renderPropertyRow(p: Property, locale: Locale): string {
  return `<li class="list-row" data-action="open-property" data-id="${p.id}">
    <div class="row-main"><b>${escapeHtml(p.name)}</b>
      <span class="muted">${escapeHtml([p.address_line1, p.city, p.region].filter(Boolean).join(" · "))}</span>
    </div>
    <span class="chevron" aria-hidden="true">›</span>
  </li>`;
}

function bindPropertyHandlers(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  items: Property[],
): void {
  document.querySelectorAll<HTMLElement>("[data-action='open-property']").forEach((el) => {
    el.addEventListener("click", () => {
      const id = Number(el.dataset.id);
      if (Number.isFinite(id)) router.navigate({ name: "properties.detail", propertyId: id });
    });
  });
  const newBtn = document.querySelector<HTMLButtonElement>("[data-action='new-property']");
  newBtn?.addEventListener("click", () => renderNewPropertyForm(client, orgId, locale, items));
}

function renderNewPropertyForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  current: Property[],
): void {
  const form = `
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "newProperty")}</h2></header>
      <form id="property-form" class="form">
        <label>${t(locale, "propertyName")}<input name="name" required maxlength="120" /></label>
        <label>${t(locale, "addressLine1")}<input name="address_line1" maxlength="200" /></label>
        <label>${t(locale, "city")}<input name="city" maxlength="80" /></label>
        <label>${t(locale, "region")}<input name="region" maxlength="80" /></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="property-error" hidden></p>
      </form>
    </section>
  `;
  setViewContent(form);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => {
      renderProperties(client, orgId, locale).catch(() => undefined);
    });
  const formEl = document.querySelector<HTMLFormElement>("#property-form");
  formEl?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(formEl);
    const payload = {
      name: String(data.get("name") || "").trim(),
      address_line1: String(data.get("address_line1") || "").trim() || undefined,
      city: String(data.get("city") || "").trim() || undefined,
      region: String(data.get("region") || "").trim() || undefined,
    };
    if (!payload.name) {
      showFormError(propertyErrorEl(), t(locale, "required"));
      return;
    }
    try {
      const created = await client.createProperty(
        orgId,
        payload,
        makeIdempotencyKey("prop"),
      );
      current.push(created);
      renderProperties(client, orgId, locale).catch(() => undefined);
    } catch (err) {
      showFormError(propertyErrorEl(), formatError(err, locale));
    }
  });
}

export async function renderPropertyDetail(
  client: PasayClient,
  orgId: number,
  propertyId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [properties, units] = await Promise.all([
      client.listProperties(orgId),
      client.listUnits(orgId, propertyId),
    ]);
    const property = properties.find((p) => p.id === propertyId);
    if (!property) throw new ApiError(404, null, "property not found");
    const body = `
      <section class="panel">
        <header class="panel-header">
          <h2>${escapeHtml(property.name)}</h2>
          <button class="primary-btn" data-action="new-unit" type="button">+ ${t(locale, "addUnit")}</button>
        </header>
        <p class="muted">${escapeHtml([property.address_line1, property.city, property.region].filter(Boolean).join(" · "))}</p>
        <h3>${t(locale, "units")}</h3>
        ${units.length === 0
          ? `<div class="empty"><b>${t(locale, "empty")}</b></div>`
          : `<ul class="list">${units.map((u) => renderUnitRow(u, locale)).join("")}</ul>`}
      </section>
      <button class="ghost-btn back-btn" data-action="back" type="button">‹ ${t(locale, "properties")}</button>
    `;
    setViewContent(body);
    document
      .querySelector<HTMLButtonElement>("[data-action='back']")
      ?.addEventListener("click", () => router.navigate({ name: "properties" }));
    document
      .querySelector<HTMLButtonElement>("[data-action='new-unit']")
      ?.addEventListener("click", () => renderNewUnitForm(client, orgId, propertyId, locale, units));
  } catch (err) {
    setViewContent(renderPropertiesError(err, locale));
  }
}

function renderUnitRow(u: Unit, locale: Locale): string {
  return `<li class="list-row">
    <div class="row-main">
      <b>${escapeHtml(u.label)}</b>
      <span class="muted">${u.bedrooms}br / ${u.bathrooms}ba · ${formatMoney(u.monthly_rent)}</span>
    </div>
    <span class="${statusToneClass(u.status)}">${statusLabel(u.status, locale)}</span>
  </li>`;
}

function renderNewUnitForm(
  client: PasayClient,
  orgId: number,
  propertyId: number,
  locale: Locale,
  current: Unit[],
): void {
  const form = `
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "addUnit")}</h2></header>
      <form id="unit-form" class="form">
        <label>${t(locale, "unitLabel")}<input name="label" required maxlength="40" /></label>
        <label>${t(locale, "bedrooms")}<input name="bedrooms" type="number" min="0" max="20" value="1" /></label>
        <label>${t(locale, "bathrooms")}<input name="bathrooms" type="number" min="0" max="20" value="1" /></label>
        <label>${t(locale, "monthlyRent")}<input name="monthly_rent" required inputmode="decimal" placeholder="0.00" /></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="unit-error" hidden></p>
      </form>
    </section>
  `;
  setViewContent(form);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () =>
      renderPropertyDetail(client, orgId, propertyId, locale).catch(() => undefined),
    );
  document
    .querySelector<HTMLFormElement>("#unit-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const monthlyRent = String(data.get("monthly_rent") || "").trim();
      const label = String(data.get("label") || "").trim();
      if (!label || !monthlyRent) {
        showFormError(unitErrorEl(), t(locale, "required"));
        return;
      }
      try {
        const created = await client.createUnit(
          orgId,
          propertyId,
          {
            label,
            bedrooms: Number(data.get("bedrooms") || 1),
            bathrooms: Number(data.get("bathrooms") || 1),
            monthly_rent: monthlyRent,
          },
          makeIdempotencyKey("unit"),
        );
        current.push(created);
        await renderPropertyDetail(client, orgId, propertyId, locale);
      } catch (err) {
        showFormError(unitErrorEl(), formatError(err, locale));
      }
    });
}

function renderPropertiesError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "apiOffline")}</h2>
    <p class="muted">${escapeHtml(formatError(err, locale))}</p>
    <button class="primary-btn" data-action="retry" type="button">${t(locale, "retry")}</button>
  </section>`;
}

function showFormError(el: HTMLElement | null, msg: string): void {
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

function propertyErrorEl(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#property-error");
}

function unitErrorEl(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#unit-error");
}

function formatError(err: unknown, locale: Locale): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return t(locale, "accessDenied");
    if (err.status === 400) return t(locale, "validationError");
    if (err.status === 409) return t(locale, "conflictError");
    return err.message;
  }
  return t(locale, "networkError");
}

// ---- Tenant registration panel ------------------------------------------
//
// Workspace-level tenant panel. Tenant CRUD lives in `app/v1/services/tenant.py`
// (Coverage Matrix 7.1) and `app/v1/api/tenants.py` (thin router). This view
// is the Owner UI for the "Register tenant" flow: form fields are validated
// client-side, then the typed API client posts to /api/v1/tenants with a
// fresh Idempotency-Key. The list is refreshed locally after a successful
// create so the new tenant appears without a full reload.

function renderTenantsPanel(tenants: Tenant[], locale: Locale): string {
  return `
    <section class="panel" data-panel="tenants">
      <header class="panel-header">
        <h2>${t(locale, "tenants")} (${tenants.length})</h2>
        <button class="primary-btn" data-action="new-tenant" type="button">+ ${t(locale, "registerTenant")}</button>
      </header>
      <p class="muted">${t(locale, "tenantFormHint")}</p>
      ${tenants.length === 0
        ? `<div class="empty"><b>${t(locale, "tenantsEmpty")}</b><span>${t(locale, "registerTenant")} →</span></div>`
        : `<ul class="list" data-list="tenants">${tenants.map((tn) => renderTenantRow(tn, locale)).join("")}</ul>`}
    </section>
  `;
}

function renderTenantRow(tn: Tenant, locale: Locale): string {
  const phone = tn.contact_phone || "—";
  const email = tn.contact_email || "—";
  return `<li class="list-row" data-tenant-id="${tn.id}">
    <div class="row-main">
      <b>${escapeHtml(tn.full_name)}</b>
      <span class="muted">${t(locale, "phone")}: ${escapeHtml(phone)} · ${t(locale, "email")}: ${escapeHtml(email)}</span>
    </div>
  </li>`;
}

function bindTenantsPanelHandlers(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  current: Tenant[],
): void {
  const newBtn = document.querySelector<HTMLButtonElement>("[data-action='new-tenant']");
  newBtn?.addEventListener("click", () =>
    renderTenantRegisterForm(client, orgId, locale, current),
  );
}

function renderTenantRegisterForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  current: Tenant[],
): void {
  const form = `
    <section class="panel" data-panel="tenant-form">
      <header class="panel-header"><h2>${t(locale, "registerTenant")}</h2></header>
      <form id="tenant-form" class="form">
        <label>${t(locale, "fullName")}<input name="full_name" required maxlength="120" /></label>
        <label>${t(locale, "contactPhone")}<input name="contact_phone" inputmode="tel" maxlength="32" /></label>
        <label>${t(locale, "contactEmail")}<input name="contact_email" inputmode="email" maxlength="120" /></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel-tenant">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="tenant-error" hidden></p>
      </form>
    </section>
  `;
  setViewContent(form);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel-tenant']")
    ?.addEventListener("click", () =>
      renderProperties(client, orgId, locale).catch(() => undefined),
    );
  const formEl = document.querySelector<HTMLFormElement>("#tenant-form");
  formEl?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(formEl);
    const fullName = String(data.get("full_name") || "").trim();
    const contactPhone = String(data.get("contact_phone") || "").trim();
    const contactEmail = String(data.get("contact_email") || "").trim();
    if (!fullName) {
      showFormError(tenantErrorEl(), t(locale, "required"));
      return;
    }
    const payload: {
      full_name: string;
      contact_phone?: string | null;
      contact_email?: string | null;
    } = { full_name: fullName };
    if (contactPhone) payload.contact_phone = contactPhone;
    if (contactEmail) payload.contact_email = contactEmail;
    try {
      const created = await client.createTenant(
        orgId,
        payload,
        makeIdempotencyKey("tenant"),
      );
      current.push(created);
      await renderProperties(client, orgId, locale);
    } catch (err) {
      showFormError(tenantErrorEl(), formatError(err, locale));
    }
  });
}

function tenantErrorEl(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#tenant-error");
}
