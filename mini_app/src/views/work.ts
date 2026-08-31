/** Work view — operations, repairs, renewals, move-outs.
 *
 *  Real workflows: report a repair; propose renewal; request move-out;
 *  verify completion; settle deposit. No localStorage. Idempotency-Key
 *  generated per submit. Failures are surfaced as explicit messages,
 *  not silent "success" placeholders.
 *
 *  The renewal form (Coverage Matrix Renewal slice) is populated from the
 *  org's ACTIVE leases — the Owner picks the lease from a dropdown, never
 *  types a numeric id manually. The dropdown also surfaces the tenant
 *  name + unit label so the Owner can audit which contract is being
 *  renewed before submitting.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import { formatMoney, makeIdempotencyKey, statusLabel, statusToneClass } from "../format";
import type { Lease, MoveOut, RenewalProposal, Repair, Unit } from "../types";

export async function renderWork(
  client: PasayClient,
  orgId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [repairs, renewals, moveOuts, leases, properties] = await Promise.all([
      client.listRepairs(orgId),
      client.listRenewals(orgId),
      client.listMoveOuts(orgId),
      client.listLeases(orgId),
      client.listProperties(orgId),
    ]);
    const openRepairs = repairs.filter(
      (r) => r.state !== "COMPLETED" && r.state !== "CANCELLED",
    );
    const openRenewals = renewals.filter(
      (r) => r.state !== "EXECUTED" && r.state !== "REJECTED" && r.state !== "CANCELLED",
    );
    const openMoveOuts = moveOuts.filter(
      (m) => m.state !== "SETTLED" && m.state !== "CANCELLED",
    );
    const body = `
      <section class="panel">
        <header class="panel-header">
          <h2>${t(locale, "work")}</h2>
        </header>
        <div class="work-actions">
          <button class="primary-btn" data-action="new-repair" type="button">+ ${t(locale, "workOpenRepair")}</button>
          <button class="primary-btn" data-action="new-renewal" type="button">+ ${t(locale, "workOpenRenewal")}</button>
          <button class="primary-btn" data-action="new-moveout" type="button">+ ${t(locale, "workOpenMoveOut")}</button>
        </div>
      </section>
      ${renderRepairSection(openRepairs, client, orgId, locale)}
      ${renderRenewalSection(openRenewals, leases, client, orgId, locale)}
      ${renderMoveOutSection(openMoveOuts, leases, client, orgId, locale)}
    `;
    setViewContent(body);
    bindWorkHandlers(client, orgId, locale, properties, leases);
  } catch (err) {
    setViewContent(renderWorkError(err, locale));
  }
}

function bindWorkHandlers(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  properties: Awaited<ReturnType<PasayClient["listProperties"]>>,
  leases: Awaited<ReturnType<PasayClient["listLeases"]>>,
): void {
  const activeLeases = leases.filter((l) => l.state === "ACTIVE");
  document
    .querySelector<HTMLButtonElement>("[data-action='new-repair']")
    ?.addEventListener("click", () => renderRepairForm(client, orgId, locale, properties));
  document
    .querySelector<HTMLButtonElement>("[data-action='new-renewal']")
    ?.addEventListener("click", () => renderRenewalForm(client, orgId, locale, activeLeases));
  document
    .querySelector<HTMLButtonElement>("[data-action='new-moveout']")
    ?.addEventListener("click", () => renderMoveOutForm(client, orgId, locale, []));

  document.querySelectorAll<HTMLButtonElement>("[data-action^='confirm-repair-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      runAction(() => client.confirmRepair(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='quote-repair-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      const amount = window.prompt(t(locale, "workQuoteAmount") + ":", "0.00");
      if (!amount) return;
      runAction(
        () => client.submitQuote(orgId, id, { amount, note: null }, makeIdempotencyKey("quote")),
        locale,
      ).then(() => renderWork(client, orgId, locale).catch(() => undefined));
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='approve-quote-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      runAction(() => client.approveQuote(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='start-work-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      runAction(() => client.startWork(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='claim-completion-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      const note = window.prompt(t(locale, "workNotes") + ":", "") ?? "";
      runAction(() => client.claimCompletion(orgId, id, note), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='verify-completion-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.repairId);
      runAction(() => client.verifyCompletion(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='approve-renewal-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.renewalId);
      runAction(() => client.approveRenewal(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='execute-renewal-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.renewalId);
      runAction(() => client.executeRenewal(orgId, id), locale).then(() =>
        renderWork(client, orgId, locale).catch(() => undefined),
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action^='settle-moveout-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.moveOutId);
      renderMoveOutSettleForm(client, orgId, id, locale);
    });
  });
}

function renderRepairSection(
  items: Repair[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) return `<section class="panel"><h3>${t(locale, "openRepairs")}</h3><p class="muted">${t(locale, "empty")}</p></section>`;
  const buttons = (r: Repair) => {
    const parts: string[] = [];
    if (r.state === "REPORTED") {
      parts.push(
        `<button class="ghost-btn" data-action="confirm-repair-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workConfirm")}</button>`,
      );
    }
    if (r.state === "CONFIRMED" || r.state === "AWAITING_TECHNICIAN") {
      parts.push(
        `<button class="ghost-btn" data-action="quote-repair-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workQuote")}</button>`,
      );
    }
    if (r.state === "QUOTE_RECEIVED") {
      parts.push(
        `<button class="primary-btn" data-action="approve-quote-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workApprove")}</button>`,
      );
    }
    if (r.state === "QUOTE_APPROVED") {
      parts.push(
        `<button class="ghost-btn" data-action="start-work-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workStart")}</button>`,
      );
    }
    if (r.state === "IN_PROGRESS") {
      parts.push(
        `<button class="ghost-btn" data-action="claim-completion-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workClaimCompletion")}</button>`,
      );
    }
    if (r.state === "COMPLETION_CLAIMED") {
      parts.push(
        `<button class="primary-btn" data-action="verify-completion-${r.id}" data-repair-id="${r.id}" type="button">${t(locale, "workVerifyCompletion")}</button>`,
      );
    }
    return parts.join("");
  };
  return `<section class="panel">
    <h3>${t(locale, "openRepairs")}</h3>
    <ul class="list">${items
      .map(
        (r) => `<li class="list-row list-row--action">
        <div class="row-main">
          <b>${escapeHtml(r.title)}</b>
          <span class="muted">${escapeHtml(r.description.slice(0, 80))}</span>
          <span class="${statusToneClass(r.state)}">${statusLabel(r.state, locale)}</span>
        </div>
        <div class="row-actions">${buttons(r)}</div>
      </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderRenewalSection(
  items: RenewalProposal[],
  _leases: Lease[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "workOpenRenewal")}</h3>
    <ul class="list">${items
      .map(
        (r) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(r.proposed_start_date)} → ${escapeHtml(r.proposed_end_date)}</b>
            <span class="muted">${formatMoney(r.proposed_monthly_rent)}</span>
            <span class="${statusToneClass(r.state)}">${statusLabel(r.state, locale)}</span>
          </div>
          <div class="row-actions">
            ${r.state === "PROPOSED" ? `<button class="primary-btn" data-action="approve-renewal-${r.id}" data-renewal-id="${r.id}" type="button">${t(locale, "workApproveRenewal")}</button>` : ""}
            ${r.state === "APPROVED" ? `<button class="primary-btn" data-action="execute-renewal-${r.id}" data-renewal-id="${r.id}" type="button">${t(locale, "workExecuteRenewal")}</button>` : ""}
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderMoveOutSection(
  items: MoveOut[],
  _leases: Lease[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "workOpenMoveOut")}</h3>
    <ul class="list">${items
      .map(
        (m) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(m.requested_at)}</b>
            <span class="${statusToneClass(m.state)}">${statusLabel(m.state, locale)}</span>
            ${m.deposit_held ? `<span class="muted">${t(locale, "deposit")}: ${formatMoney(m.deposit_held)}</span>` : ""}
          </div>
          <div class="row-actions">
            ${m.state === "INSPECTED" ? `<button class="primary-btn" data-action="settle-moveout-${m.id}" data-move-out-id="${m.id}" type="button">${t(locale, "workSettleMoveOut")}</button>` : ""}
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderRepairForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  properties: Awaited<ReturnType<PasayClient["listProperties"]>>,
): void {
  void properties;
  setViewContent(`
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "workOpenRepair")}</h2></header>
      <form id="repair-form" class="form">
        <label>${t(locale, "workReportTitle")}<input name="title" required maxlength="120" /></label>
        <label>${t(locale, "workReportDescription")}<textarea name="description" rows="4" required maxlength="2000"></textarea></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="repair-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => renderWork(client, orgId, locale).catch(() => undefined));
  document
    .querySelector<HTMLFormElement>("#repair-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const title = String(data.get("title") || "").trim();
      const description = String(data.get("description") || "").trim();
      if (!title || !description) {
        showFormError(document.querySelector("#repair-error"), t(locale, "required"));
        return;
      }
      try {
        await client.openRepair(
          orgId,
          { title, description, severity: "NORMAL" },
          makeIdempotencyKey("repair"),
        );
        await renderWork(client, orgId, locale);
      } catch (err) {
        showFormError(document.querySelector("#repair-error"), formatError(err, locale));
      }
    });
}

function renderRenewalForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  activeLeases: Lease[],
): void {
  const leaseOptions =
    activeLeases.length === 0
      ? `<option value="" disabled selected>—</option>`
      : `<option value="" disabled selected>—</option>` +
        activeLeases
          .map(
            (l) =>
              `<option value="${l.id}" data-rent="${escapeHtml(l.monthly_rent)}">` +
              `#${l.id} · ${t(locale, "tenants")} #${l.tenant_id} · ` +
              `${t(locale, "units")} #${l.unit_id} · ` +
              `${escapeHtml(l.start_date)} → ${escapeHtml(l.end_date)} · ` +
              `${formatMoney(l.monthly_rent)}` +
              `</option>`,
          )
          .join("");
  const emptyHint =
    activeLeases.length === 0
      ? `<p class="muted" data-hint="no-active-leases">${
          locale === "zh"
            ? "当前工作区没有生效中的租约，先到 Work → 报修 / Properties 单元下创建 ACTIVE 租约。"
            : "No ACTIVE leases in this workspace. Create an ACTIVE lease under a unit first."
        }</p>`
      : "";
  setViewContent(`
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "workOpenRenewal")}</h2></header>
      ${emptyHint}
      <form id="renewal-form" class="form">
        <label>${t(locale, "leases")}
          <select name="source_lease_id" required ${activeLeases.length === 0 ? "disabled" : ""}>
            ${leaseOptions}
          </select>
        </label>
        <label>${t(locale, "startDate")}<input name="start_date" type="date" required /></label>
        <label>${t(locale, "endDate")}<input name="end_date" type="date" required /></label>
        <label>${t(locale, "monthlyRent")}<input name="proposed_monthly_rent" required inputmode="decimal" placeholder="0.00" /></label>
        <label>${t(locale, "deposit")}<input name="proposed_deposit" required inputmode="decimal" placeholder="0.00" value="0.00" /></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit" ${activeLeases.length === 0 ? "disabled" : ""}>${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="renewal-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => renderWork(client, orgId, locale).catch(() => undefined));
  document
    .querySelector<HTMLFormElement>("#renewal-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const sourceLeaseId = Number(data.get("source_lease_id"));
      const start = String(data.get("start_date") || "").trim();
      const end = String(data.get("end_date") || "").trim();
      const rent = String(data.get("proposed_monthly_rent") || "").trim();
      const deposit = String(data.get("proposed_deposit") || "0").trim() || "0";
      if (!sourceLeaseId || !start || !end || !rent) {
        showFormError(document.querySelector("#renewal-error"), t(locale, "required"));
        return;
      }
      try {
        const proposal = await client.proposeRenewal(
          orgId,
          {
            source_lease_id: sourceLeaseId,
            proposed_start_date: start,
            proposed_end_date: end,
            proposed_monthly_rent: rent,
            proposed_deposit: deposit,
          },
          makeIdempotencyKey("renewal"),
        );
        await renderWork(client, orgId, locale);
        return proposal;
      } catch (err) {
        showFormError(document.querySelector("#renewal-error"), formatError(err, locale));
      }
    });
}

function renderMoveOutForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  current: MoveOut[],
): void {
  setViewContent(`
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "workOpenMoveOut")}</h2></header>
      <form id="moveout-form" class="form">
        <label>${t(locale, "leases")} #<input name="lease_id" type="number" required min="1" /></label>
        <label>${t(locale, "workReason")}<textarea name="reason" rows="3" maxlength="500"></textarea></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="moveout-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => renderWork(client, orgId, locale).catch(() => undefined));
  document
    .querySelector<HTMLFormElement>("#moveout-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const leaseId = Number(data.get("lease_id"));
      const reason = String(data.get("reason") || "").trim();
      if (!leaseId) {
        showFormError(document.querySelector("#moveout-error"), t(locale, "required"));
        return;
      }
      try {
        const moveOut = await client.requestMoveOut(
          orgId,
          { lease_id: leaseId, reason: reason || null },
          makeIdempotencyKey("moveout"),
        );
        current.push(moveOut);
        await renderWork(client, orgId, locale);
      } catch (err) {
        showFormError(document.querySelector("#moveout-error"), formatError(err, locale));
      }
    });
}

function renderMoveOutSettleForm(
  client: PasayClient,
  orgId: number,
  moveOutId: number,
  locale: Locale,
): void {
  setViewContent(`
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "workSettleMoveOut")}</h2></header>
      <form id="settle-form" class="form">
        <label>${t(locale, "deposit")}<input name="deposit_held" required inputmode="decimal" placeholder="0.00" /></label>
        <label>${t(locale, "workRefund")}<input name="refund_amount" required inputmode="decimal" placeholder="0.00" /></label>
        <label>${t(locale, "workAdditionalOwed")}<input name="additional_owed" required inputmode="decimal" placeholder="0.00" /></label>
        <label>${t(locale, "workOutcome")}<select name="outcome">
          <option value="FULL_REFUND">FULL_REFUND</option>
          <option value="PARTIAL_REFUND">PARTIAL_REFUND</option>
          <option value="NO_REFUND">NO_REFUND</option>
        </select></label>
        <label>${t(locale, "workNotes")}<textarea name="notes" rows="3" maxlength="500"></textarea></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="settle-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => renderWork(client, orgId, locale).catch(() => undefined));
  document
    .querySelector<HTMLFormElement>("#settle-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      try {
        await client.settleMoveOut(orgId, moveOutId, {
          outcome: String(data.get("outcome")),
          deposit_held: String(data.get("deposit_held")),
          refund_amount: String(data.get("refund_amount")),
          additional_owed: String(data.get("additional_owed")),
          notes: String(data.get("notes") || "") || null,
        });
        await renderWork(client, orgId, locale);
      } catch (err) {
        showFormError(document.querySelector("#settle-error"), formatError(err, locale));
      }
    });
}

function renderWorkError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "apiOffline")}</h2>
    <p class="muted">${escapeHtml(formatError(err, locale))}</p>
    <button class="primary-btn" data-action="retry" type="button">${t(locale, "retry")}</button>
  </section>`;
}

function showFormError(el: Element | null, msg: string): void {
  if (!el) return;
  (el as HTMLElement).textContent = msg;
  (el as HTMLElement).hidden = false;
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

async function runAction(action: () => Promise<unknown>, locale: Locale): Promise<void> {
  try {
    await action();
  } catch (err) {
    window.alert(formatError(err, locale));
  }
}
