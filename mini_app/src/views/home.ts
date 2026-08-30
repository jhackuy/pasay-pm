/** Home view — landing dashboard.
 *
 *  Pulls real numbers from the API. Never invents values.
 *  Shows: overdue rent, pending payment claims, open repairs, active leases,
 *  plus a quick action to jump to other tabs.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { formatMoney, statusLabel, statusToneClass } from "../format";

export async function renderHome(
  client: PasayClient,
  orgId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [overdue, claims, repairs, leases] = await Promise.all([
      client.listOverdue(orgId),
      client.listClaims(orgId),
      client.listRepairs(orgId),
      client.listLeases(orgId),
    ]);
    const overdueTotal = overdue.reduce((acc, item) => acc + Number(item.amount_due || 0), 0);
    const activeLeases = leases.filter((lease) => lease.state === "ACTIVE").length;
    const pendingClaims = claims.filter((c) => c.status === "PENDING");
    const openRepairs = repairs.filter(
      (r) => r.state !== "COMPLETED" && r.state !== "CANCELLED",
    );
    const body = `
      <section class="grid grid--kpi">
        <article class="kpi kpi--bad">
          <p class="eyebrow">${t(locale, "overdue")}</p>
          <strong>${formatMoney(overdueTotal.toFixed(2))}</strong>
          <span>${overdue.length} ${t(locale, "tasks").toLowerCase()}</span>
        </article>
        <article class="kpi kpi--pending">
          <p class="eyebrow">${t(locale, "pendingClaims")}</p>
          <strong>${pendingClaims.length}</strong>
          <span>${pendingClaims.length > 0 ? t(locale, "workApprove") : t(locale, "empty")}</span>
        </article>
        <article class="kpi kpi--info">
          <p class="eyebrow">${t(locale, "openRepairs")}</p>
          <strong>${openRepairs.length}</strong>
          <span>${t(locale, "work")}</span>
        </article>
        <article class="kpi kpi--ok">
          <p class="eyebrow">${t(locale, "activeLeases")}</p>
          <strong>${activeLeases}</strong>
          <span>${leases.length} ${t(locale, "leases").toLowerCase()}</span>
        </article>
      </section>

      <section class="panel">
        <h2>${t(locale, "openOperations")}</h2>
        ${renderClaimsList(pendingClaims, locale)}
        ${renderRepairList(openRepairs.slice(0, 5), locale)}
      </section>
    `;
    setViewContent(body);
  } catch (err) {
    setViewContent(renderHomeError(err, locale));
  }
}

function renderClaimsList(items: Awaited<ReturnType<PasayClient["listClaims"]>>, locale: Locale): string {
  if (items.length === 0) return `<p class="muted">${t(locale, "empty")}</p>`;
  return `<ul class="list">
    ${items.slice(0, 5)
      .map(
        (claim) => `<li>
          <span class="${statusToneClass(claim.status)}">${statusLabel(claim.status, locale)}</span>
          <span>${formatMoney(claim.amount)}</span>
        </li>`,
      )
      .join("")}
  </ul>`;
}

function renderRepairList(items: Awaited<ReturnType<PasayClient["listRepairs"]>>, locale: Locale): string {
  if (items.length === 0) return "";
  return `<h3>${t(locale, "openRepairs")}</h3>
    <ul class="list">
      ${items
        .map(
          (r) => `<li>
            <span class="${statusToneClass(r.state)}">${statusLabel(r.state, locale)}</span>
            <span>${escapeHtml(r.title)}</span>
          </li>`,
        )
        .join("")}
    </ul>`;
}

function renderHomeError(err: unknown, locale: Locale): string {
  const apiErr = err instanceof ApiError ? err : null;
  const detail = apiErr ? apiErr.status : (err as Error)?.message ?? "—";
  return `<section class="panel error">
    <h2>${t(locale, "apiOffline")}</h2>
    <p class="muted">${escapeHtml(String(detail))}</p>
    <button class="primary-btn" data-action="retry" type="button">${t(locale, "retry")}</button>
  </section>`;
}

export function escapeHtml(raw: string | number | null | undefined): string {
  if (raw === null || raw === undefined) return "";
  return String(raw)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
