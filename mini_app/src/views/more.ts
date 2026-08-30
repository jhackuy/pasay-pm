/** More view — workspace overview, session, archive, health.
 *
 *  Read-only screen for the operator to verify session state and the
 *  underlying API health. Shows member roster, archive counts, and the
 *  current Principal's role + org scope.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import { formatMoney, statusLabel, statusToneClass } from "../format";
import type { SessionStore } from "../state";
import type { MoveOut, Operation, RenewalProposal, Repair, ExpenseClaim, RentPayment } from "../types";

export async function renderMore(
  client: PasayClient,
  orgId: number,
  session: SessionStore,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [health, members, repairs, renewals, moveOuts, expenses, claims] = await Promise.all([
      client.health(),
      client.listMembers(orgId).catch(() => []),
      client.listRepairs(orgId).catch(() => [] as Repair[]),
      client.listRenewals(orgId).catch(() => [] as RenewalProposal[]),
      client.listMoveOuts(orgId).catch(() => [] as MoveOut[]),
      client.listExpenseClaims(orgId).catch(() => [] as ExpenseClaim[]),
      client.listClaims(orgId).catch(() => [] as RentPayment[]),
    ]);
    const archive = [
      ...repairs.map((r) => ({ kind: "REPAIR", status: r.state, at: r.reported_at, label: r.title })),
      ...renewals.map((r) => ({ kind: "RENEWAL", status: r.state, at: r.proposed_start_date, label: `Lease #${r.lease_id}` })),
      ...moveOuts.map((m) => ({ kind: "MOVE_OUT", status: m.state, at: m.requested_at, label: `Lease #${m.lease_id}` })),
      ...expenses.map((e) => ({ kind: "EXPENSE", status: e.status, at: e.submitted_at, label: e.title })),
      ...claims.map((c) => ({ kind: "RENT", status: c.status, at: c.submitted_at, label: formatMoney(c.amount) })),
    ].sort((a, b) => (a.at > b.at ? -1 : a.at < b.at ? 1 : 0));
    const body = `
      <section class="panel">
        <header class="panel-header"><h2>${t(locale, "moreProfile")}</h2></header>
        <dl class="kv">
          <dt>${t(locale, "signedInAs")}</dt><dd>${escapeHtml(String(session.userId ?? "—"))}</dd>
          <dt>${t(locale, "role")}</dt><dd>${escapeHtml(session.role ?? "—")}</dd>
          <dt>${t(locale, "moreOrgScope")}</dt><dd>${escapeHtml(String(orgId))}</dd>
        </dl>
      </section>
      <section class="panel">
        <header class="panel-header"><h2>${t(locale, "moreHealth")}</h2></header>
        <p><span class="status status--ok">${escapeHtml(health.status)}</span> v${escapeHtml(health.version)}</p>
      </section>
      <section class="panel">
        <header class="panel-header"><h2>${t(locale, "tenants")} (${members.length})</h2></header>
        ${members.length === 0
          ? `<p class="muted">${t(locale, "empty")}</p>`
          : `<ul class="list">${members
              .map(
                (m) => `<li class="list-row">
                <div class="row-main">
                  <b>${escapeHtml(m.display_name ?? m.username ?? `user-${m.user_id}`)}</b>
                  <span class="muted">@${escapeHtml(m.username ?? "")}</span>
                  <span class="${statusToneClass(m.state)}">${escapeHtml(m.role)} · ${escapeHtml(m.state)}</span>
                </div>
              </li>`,
              )
              .join("")}</ul>`}
      </section>
      <section class="panel">
        <header class="panel-header"><h2>${t(locale, "moreArchive")}</h2></header>
        ${archive.length === 0
          ? `<p class="muted">${t(locale, "empty")}</p>`
          : `<ul class="list">${archive
              .slice(0, 30)
              .map(
                (entry) => `<li class="list-row">
                <div class="row-main">
                  <b>${escapeHtml(entry.label)}</b>
                  <span class="muted">${escapeHtml(entry.kind)} · ${escapeHtml(entry.at)}</span>
                  <span class="${statusToneClass(entry.status)}">${statusLabel(entry.status, locale)}</span>
                </div>
              </li>`,
              )
              .join("")}</ul>`}
      </section>
    `;
    setViewContent(body);
  } catch (err) {
    setViewContent(renderMoreError(err, locale));
  }
}

function renderMoreError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "apiOffline")}</h2>
    <p class="muted">${escapeHtml(err instanceof ApiError ? err.message : String(err))}</p>
  </section>`;
}
