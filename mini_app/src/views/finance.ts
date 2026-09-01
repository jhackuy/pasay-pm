/** Finance view — overdue rent, pending claims, expense claims.
 *
 *  Real workflows: claim a payment; verify a claim; open expense;
 *  add receipt; verify / reject / reverse.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import { formatDate, formatMoney, makeIdempotencyKey, statusLabel, statusToneClass } from "../format";
import type { ExpenseClaim, RentDueSchedule, RentPayment } from "../types";

export async function renderFinance(
  client: PasayClient,
  orgId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(`<section class="card loading"><p>${t(locale, "loading")}</p></section>`);
  try {
    const [overdue, claims, expenses] = await Promise.all([
      client.listOverdue(orgId),
      client.listClaims(orgId),
      client.listExpenseClaims(orgId),
    ]);
    const pendingClaims = claims.filter((c) => c.status === "PENDING");
    const openExpenses = expenses.filter(
      (e) => e.status !== "VERIFIED" && e.status !== "CANCELLED",
    );
    const body = `
      <section class="panel">
        <header class="panel-header">
          <h2>${t(locale, "finance")}</h2>
        </header>
        <div class="work-actions">
          <button class="primary-btn" data-action="new-expense" type="button">+ ${t(locale, "financeOpenExpense")}</button>
        </div>
      </section>
      ${renderOverdueSection(overdue, client, orgId, locale)}
      ${renderClaimsSection(pendingClaims, client, orgId, locale)}
      ${renderClaimsHistorySection(claims, locale)}
      ${renderExpenseSection(openExpenses, client, orgId, locale)}
    `;
    setViewContent(body);
    bindFinanceHandlers(client, orgId, locale);
  } catch (err) {
    setViewContent(renderFinanceError(err, locale));
  }
}

function bindFinanceHandlers(
  client: PasayClient,
  orgId: number,
  locale: Locale,
): void {
  document
    .querySelector<HTMLButtonElement>("[data-action='new-expense']")
    ?.addEventListener("click", () => renderExpenseForm(client, orgId, locale, []));

  document.querySelectorAll<HTMLButtonElement>("[data-action^='claim-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.scheduleId);
      const amount = window.prompt(`${t(locale, "financeAmount")}:`, "0.00");
      if (!amount) return;
      runAction(
        () => client.claimPayment(orgId, id, { claimed_amount: amount, evidence: [] }, makeIdempotencyKey("claim")),
        locale,
      ).then(() => renderFinance(client, orgId, locale).catch(() => undefined));
    });
  });

  document.querySelectorAll<HTMLButtonElement>("[data-action^='verify-claim-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.paymentId);
      runAction(() => client.verifyPayment(orgId, id, {}), locale).then(() =>
        renderFinance(client, orgId, locale).catch(() => undefined),
      );
    });
  });

  document.querySelectorAll<HTMLButtonElement>("[data-action^='verify-expense-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.claimId);
      runAction(() => client.verifyExpense(orgId, id), locale).then(() =>
        renderFinance(client, orgId, locale).catch(() => undefined),
      );
    });
  });

  document.querySelectorAll<HTMLButtonElement>("[data-action^='reject-expense-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.claimId);
      const reason = window.prompt(t(locale, "workReason") + ":", "");
      if (!reason) return;
      runAction(() => client.rejectExpense(orgId, id, reason), locale).then(() =>
        renderFinance(client, orgId, locale).catch(() => undefined),
      );
    });
  });

  document.querySelectorAll<HTMLButtonElement>("[data-action^='reverse-expense-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.claimId);
      const reason = window.prompt(t(locale, "workReason") + ":", "");
      if (!reason) return;
      runAction(() => client.reverseExpense(orgId, id, reason), locale).then(() =>
        renderFinance(client, orgId, locale).catch(() => undefined),
      );
    });
  });

  document.querySelectorAll<HTMLButtonElement>("[data-action^='add-receipt-']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.claimId);
      const reference = window.prompt(`${t(locale, "financeReceiptReference")}:`, "");
      if (!reference) return;
      runAction(
        () =>
          client.addReceipt(
            orgId,
            id,
            { kind: "TEXT", reference },
            makeIdempotencyKey("receipt"),
          ),
        locale,
      ).then(() => renderFinance(client, orgId, locale).catch(() => undefined));
    });
  });
}

function renderOverdueSection(
  items: RentDueSchedule[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) {
    return `<section class="panel"><h3>${t(locale, "financeOverdueRent")}</h3><p class="muted">${t(locale, "empty")}</p></section>`;
  }
  return `<section class="panel">
    <h3>${t(locale, "financeOverdueRent")}</h3>
    <ul class="list">${items
      .map(
        (item) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(item.due_date)}</b>
            <span class="${statusToneClass(item.state)}">${statusLabel(item.state, locale)}</span>
            <span class="muted">${formatMoney(item.amount_due)}</span>
          </div>
          <div class="row-actions">
            <button class="primary-btn" data-action="claim-${item.id}" data-schedule-id="${item.id}" type="button">${t(locale, "submit")}</button>
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderClaimsSection(
  items: RentPayment[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "financeListClaims")}</h3>
    <ul class="list">${items
      .map(
        (claim) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(formatDate(claim.claimed_at))}</b>
            <span class="${statusToneClass(claim.status)}">${statusLabel(claim.status, locale)}</span>
            <span class="muted">${formatMoney(claim.claimed_amount)}</span>
          </div>
          <div class="row-actions">
            <a class="ghost-btn" href="#/rent/claims/${claim.id}" data-action="open-claim-${claim.id}" data-payment-id="${claim.id}">${t(locale, "rentClaimTitle")}</a>
            ${claim.status === "PENDING"
              ? `<button class="primary-btn" data-action="verify-claim-${claim.id}" data-payment-id="${claim.id}" type="button">${t(locale, "financeVerify")}</button>`
              : ""}
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderClaimsHistorySection(
  items: RentPayment[],
  locale: Locale,
): string {
  // Render every non-PENDING claim so the Owner can drill into the
  // detail view (evidence / verifications / activity / balance) for
  // any historical claim, not only the ones awaiting verification.
  const history = items.filter((c) => c.status !== "PENDING");
  if (history.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "financeClaimsHistory")}</h3>
    <ul class="list">${history
      .map(
        (claim) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(formatDate(claim.claimed_at))}</b>
            <span class="${statusToneClass(claim.status)}">${statusLabel(claim.status, locale)}</span>
            <span class="muted">${formatMoney(claim.claimed_amount)}</span>
          </div>
          <div class="row-actions">
            <a class="ghost-btn" href="#/rent/claims/${claim.id}" data-action="open-claim-${claim.id}" data-payment-id="${claim.id}">${t(locale, "rentClaimTitle")}</a>
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderExpenseSection(
  items: ExpenseClaim[],
  _client: PasayClient,
  _orgId: number,
  locale: Locale,
): string {
  if (items.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "financeOpenExpense")}</h3>
    <ul class="list">${items
      .map(
        (e) => `<li class="list-row list-row--action">
          <div class="row-main">
            <b>${escapeHtml(e.title)}</b>
            <span class="muted">${escapeHtml(e.category)} · ${formatMoney(e.claimed_amount)}</span>
            <span class="${statusToneClass(e.status)}">${statusLabel(e.status, locale)}</span>
          </div>
          <div class="row-actions">
            ${e.status === "OPEN" || e.status === "SUBMITTED"
              ? `<button class="ghost-btn" data-action="add-receipt-${e.id}" data-claim-id="${e.id}" type="button">${t(locale, "financeAddReceipt")}</button>`
              : ""}
            ${e.status === "SUBMITTED"
              ? `<button class="primary-btn" data-action="verify-expense-${e.id}" data-claim-id="${e.id}" type="button">${t(locale, "financeVerify")}</button>`
              : ""}
            ${e.status === "VERIFIED"
              ? `<button class="ghost-btn" data-action="reverse-expense-${e.id}" data-claim-id="${e.id}" type="button">${t(locale, "financeReverse")}</button>`
              : ""}
            ${e.status === "SUBMITTED"
              ? `<button class="ghost-btn" data-action="reject-expense-${e.id}" data-claim-id="${e.id}" type="button">${t(locale, "financeReject")}</button>`
              : ""}
          </div>
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

function renderExpenseForm(
  client: PasayClient,
  orgId: number,
  locale: Locale,
  current: ExpenseClaim[],
): void {
  setViewContent(`
    <section class="panel">
      <header class="panel-header"><h2>${t(locale, "financeOpenExpense")}</h2></header>
      <form id="expense-form" class="form">
        <label>${t(locale, "financeTitle")}<input name="title" required maxlength="120" /></label>
        <label>${t(locale, "financeCategory")}<select name="category">
          <option value="UTILITIES">UTILITIES</option>
          <option value="REPAIRS">REPAIRS</option>
          <option value="SUPPLIES">SUPPLIES</option>
          <option value="TAX">TAX</option>
          <option value="INSURANCE">INSURANCE</option>
          <option value="SERVICE">SERVICE</option>
          <option value="OTHER">OTHER</option>
        </select></label>
        <label>${t(locale, "financeAmount")}<input name="claimed_amount" required inputmode="decimal" placeholder="0.00" /></label>
        <label>${t(locale, "workNotes")}<textarea name="description" rows="3" maxlength="500"></textarea></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="expense-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => renderFinance(client, orgId, locale).catch(() => undefined));
  document
    .querySelector<HTMLFormElement>("#expense-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const title = String(data.get("title") || "").trim();
      const amount = String(data.get("claimed_amount") || "").trim();
      if (!title || !amount) {
        showFormError(document.querySelector("#expense-error"), t(locale, "required"));
        return;
      }
      try {
        const claim = await client.openExpenseClaim(
          orgId,
          {
            title,
            category: String(data.get("category")),
            claimed_amount: amount,
            description: String(data.get("description") || "") || null,
          },
          makeIdempotencyKey("expense"),
        );
        current.push(claim);
        await renderFinance(client, orgId, locale);
      } catch (err) {
        showFormError(document.querySelector("#expense-error"), formatError(err, locale));
      }
    });
}

function renderFinanceError(err: unknown, locale: Locale): string {
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
