/** Rent payment detail (Owner view).
 *
 *  Coverage Matrix Rent slice (4.1–4.7) and Issue #99 #99 OWNER ADDENDUM:
 *  "real API-backed payment detail Owner view with persisted evidence,
 *  verification/status/activity/balance rendering".
 *
 *  The Mini App never invents business truth. Every state it shows is
 *  read from a real GET endpoint on entry and is re-fetched after every
 *  mutation. The state machine lives in
 *  `app.v1.services.rent_payment.RentPaymentService`; the view is
 *  intentionally thin.
 *
 *  No business truth in localStorage. No silent success: every form
 *  has explicit validation and surfaces 4xx/5xx messages to the Owner.
 *
 *  Required behaviors locked in by the browser smoke:
 *    1. attach evidence (positive + negative — oversize/empty rejected)
 *    2. verify (positive) — flips status to VERIFIED, writes verification row
 *    3. duplicate verification of an already-verified claim — surfaces 409
 *    4. reject / reverse — both require a non-empty `reason`
 *    5. cross-org navigation — surfaces a localized access-denied error
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { router } from "../router";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import {
  formatDate,
  formatMoney,
  makeIdempotencyKey,
  statusLabel,
  statusToneClass,
} from "../format";
import type {
  Operation,
  RentActivity,
  RentBalance,
  RentDueSchedule,
  RentEvidence,
  RentPayment,
  RentVerification,
} from "../types";

const EVIDENCE_KINDS = ["PHOTO", "DOCUMENT", "TEXT", "TELEGRAM_FILE"];

// ---------- detail entry point ----------

export async function renderRentClaimDetail(
  client: PasayClient,
  orgId: number,
  paymentId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(
    `<section class="card loading"><p>${t(locale, "loading")}</p></section>`,
  );
  try {
    // The V1 backend exposes the claim only via the org-scoped list
    // endpoint; the detail view finds the claim in that list rather
    // than depending on a non-existent single-resource GET. Cross-org
    // access returns an empty list → "not found" surface below.
    const claims = await client.listClaims(orgId);
    const payment = claims.find((c) => c.id === paymentId) ?? null;
    if (!payment) {
      setViewContent(renderDetailError(
        Object.assign(new Error("not found"), { status: 404 }),
        locale,
      ));
      return;
    }
    const [evidence, verifications, scheduleList] = await Promise.all([
      client.listEvidence(orgId, paymentId).catch(() => [] as RentEvidence[]),
      client.listVerifications(orgId, paymentId).catch(() => [] as RentVerification[]),
      client.listDueSchedules(orgId),
    ]);
    const schedule = scheduleList.find((s) => s.id === payment.due_schedule_id) ?? null;
    let balance: RentBalance | null = null;
    let operation: Operation | null = null;
    let activity: RentActivity[] = [];
    if (schedule) {
      [balance, operation, activity] = await Promise.all([
        client.getBalance(orgId, schedule.id).catch(() => null),
        client.getRentOperation(orgId, schedule.id).catch(() => null),
        client.listActivity(orgId, schedule.id).catch(() => [] as RentActivity[]),
      ]);
    }
    const body = renderDetailBody(
      payment,
      evidence,
      verifications,
      schedule,
      balance,
      operation,
      activity,
      locale,
    );
    setViewContent(body);
    bindDetailHandlers(client, orgId, paymentId, locale);
  } catch (err) {
    setViewContent(renderDetailError(err, locale));
  }
}

function renderDetailBody(
  payment: RentPayment,
  evidence: RentEvidence[],
  verifications: RentVerification[],
  schedule: RentDueSchedule | null,
  balance: RentBalance | null,
  operation: Operation | null,
  activity: RentActivity[],
  locale: Locale,
): string {
  const header = `
    <section class="panel">
      <header class="panel-header">
        <h2>${t(locale, "rentClaimTitle")} #${payment.id}</h2>
        <a class="ghost-btn" href="#/finance" data-action="back">← ${t(locale, "finance")}</a>
      </header>
      <p>
        <span class="${statusToneClass(payment.status)}">${statusLabel(payment.status, locale)}</span>
        <span class="muted">${t(locale, "rentClaimed")}: ${formatMoney(payment.claimed_amount)}</span>
        ${payment.verified_amount !== null
          ? `· <span class="muted">${t(locale, "rentVerified")}: ${formatMoney(payment.verified_amount)}</span>`
          : ""}
        <br/>
        <span class="muted">${t(locale, "rentSubmitted")}: ${escapeHtml(formatDate(payment.claimed_at))}</span>
        ${schedule ? `· <span class="muted">${t(locale, "rentDueSchedule")}: #${schedule.id} ${escapeHtml(schedule.due_date)} (${formatMoney(schedule.amount_due)})</span>` : ""}
        <br/>
        <span class="muted">${t(locale, "rentIdempotencyKey")}: <code>${escapeHtml(payment.idempotency_key)}</code></span>
      </p>
    </section>
  `;
  const balanceSection = balance
    ? `
    <section class="panel">
      <h3>${t(locale, "rentBalance")}</h3>
      <ul class="list">
        <li><b>${t(locale, "rentAmountDue")}:</b> ${formatMoney(balance.amount_due)}</li>
        <li><b>${t(locale, "rentVerifiedTotal")}:</b> ${formatMoney(balance.verified_total)}</li>
        <li><b>${t(locale, "rentRemainingBalance")}:</b> ${formatMoney(balance.remaining_balance)}</li>
        <li><b>${t(locale, "rentIsPaid")}:</b> ${balance.is_paid ? t(locale, "yes") : t(locale, "no")}</li>
      </ul>
      ${operation ? `<p class="muted">${t(locale, "rentOperation")}: <span class="${statusToneClass(operation.state)}">${statusLabel(operation.state, locale)}</span>${operation.resolved_at ? ` · ${escapeHtml(formatDate(operation.resolved_at))}` : ""}</p>` : ""}
    </section>`
    : "";
  return (
    header +
    balanceSection +
    renderEvidenceSection(payment, evidence, locale) +
    renderVerificationSection(payment, verifications, locale) +
    renderDecisionSection(payment, locale) +
    renderActivitySection(activity, locale)
  );
}

// ---------- evidence section ----------

function renderEvidenceSection(
  payment: RentPayment,
  evidence: RentEvidence[],
  locale: Locale,
): string {
  const canAttach = payment.status === "PENDING";
  const listHtml = evidence.length === 0
    ? `<p class="muted" data-state="no-evidence">${t(locale, "rentNoEvidence")}</p>`
    : `<ul class="list" data-list="evidence">${evidence
        .map(
          (e) => `<li data-evidence-id="${e.id}">
            <b>${escapeHtml(e.kind)}</b>
            <span class="muted">${escapeHtml(e.reference)}</span>
            <span class="muted">${escapeHtml(formatDate(e.created_at))}</span>
          </li>`,
        )
        .join("")}</ul>`;
  const formHtml = canAttach
    ? `<form id="evidence-form" class="form" data-form="evidence">
        <label>${t(locale, "rentEvidenceKind")}
          <select name="kind" required>
            ${EVIDENCE_KINDS.map((k) => `<option value="${k}">${k}</option>`).join("")}
          </select>
        </label>
        <label>${t(locale, "rentEvidenceReference")}
          <input name="reference" required minlength="1" maxlength="500" />
        </label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "rentAttachEvidence")}</button>
        </div>
        <p class="muted form-error" id="evidence-error" hidden></p>
      </form>`
    : "";
  return `<section class="panel">
    <h3>${t(locale, "rentEvidenceSection")}</h3>
    ${listHtml}
    ${formHtml}
  </section>`;
}

// ---------- verification section ----------

function renderVerificationSection(
  payment: RentPayment,
  verifications: RentVerification[],
  locale: Locale,
): string {
  const listHtml = verifications.length === 0
    ? `<p class="muted">${t(locale, "rentNoVerifications")}</p>`
    : `<ul class="list" data-list="verifications">${verifications
        .map(
          (v) => `<li data-verification-id="${v.id}">
            <span class="${statusToneClass(v.decision)}">${statusLabel(v.decision, locale)}</span>
            ${v.verified_amount !== null ? `<span>${formatMoney(v.verified_amount)}</span>` : ""}
            <span class="muted">${escapeHtml(formatDate(v.decided_at))}</span>
            ${v.reason ? `<span class="muted">${escapeHtml(v.reason)}</span>` : ""}
          </li>`,
        )
        .join("")}</ul>`;
  const canVerify = payment.status === "PENDING";
  const formHtml = canVerify
    ? `<form id="verify-form" class="form" data-form="verify">
        <label>${t(locale, "rentVerifiedAmountOptional")}
          <input name="verified_amount" inputmode="decimal" placeholder="${escapeHtml(payment.claimed_amount)}" pattern="^[0-9]+(\.[0-9]{1,2})?$" />
        </label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "rentVerify")}</button>
        </div>
        <p class="muted form-error" id="verify-error" hidden></p>
      </form>`
    : "";
  return `<section class="panel">
    <h3>${t(locale, "rentVerificationSection")}</h3>
    ${listHtml}
    ${formHtml}
  </section>`;
}

// ---------- decision section (reject / reverse) ----------

function renderDecisionSection(
  payment: RentPayment,
  locale: Locale,
): string {
  const canReject = payment.status === "PENDING";
  const canReverse = payment.status === "VERIFIED";
  if (!canReject && !canReverse) {
    return `<section class="panel">
      <h3>${t(locale, "rentDecisionSection")}</h3>
      <p class="muted">${t(locale, "rentDecisionClosed")}</p>
    </section>`;
  }
  return `<section class="panel">
    <h3>${t(locale, "rentDecisionSection")}</h3>
    ${canReject
      ? `<form id="reject-form" class="form" data-form="reject">
          <label>${t(locale, "rentReason")}
            <textarea name="reason" rows="2" required minlength="1" maxlength="500"></textarea>
          </label>
          <div class="form-actions">
            <button class="ghost-btn" type="submit">${t(locale, "rentReject")}</button>
          </div>
          <p class="muted form-error" id="reject-error" hidden></p>
        </form>`
      : ""}
    ${canReverse
      ? `<form id="reverse-form" class="form" data-form="reverse">
          <label>${t(locale, "rentReason")}
            <textarea name="reason" rows="2" required minlength="1" maxlength="500"></textarea>
          </label>
          <div class="form-actions">
            <button class="ghost-btn" type="submit">${t(locale, "rentReverse")}</button>
          </div>
          <p class="muted form-error" id="reverse-error" hidden></p>
        </form>`
      : ""}
  </section>`;
}

// ---------- activity feed ----------

function renderActivitySection(activity: RentActivity[], locale: Locale): string {
  if (activity.length === 0) {
    return `<section class="panel">
      <h3>${t(locale, "rentActivitySection")}</h3>
      <p class="muted">${t(locale, "empty")}</p>
    </section>`;
  }
  return `<section class="panel">
    <h3>${t(locale, "rentActivitySection")}</h3>
    <ul class="list" data-list="activity">${activity
      .map(
        (a) => `<li data-activity-id="${a.id}">
          <b>${escapeHtml(a.kind)}</b>
          <span class="muted">${escapeHtml(formatDate(a.occurred_at))}</span>
          ${a.detail ? `<span class="muted">${escapeHtml(a.detail)}</span>` : ""}
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

// ---------- handlers ----------

function bindDetailHandlers(
  client: PasayClient,
  orgId: number,
  paymentId: number,
  locale: Locale,
): void {
  document
    .querySelector<HTMLAnchorElement>("[data-action='back']")
    ?.addEventListener("click", (e) => {
      e.preventDefault();
      router.navigate({ name: "finance" });
    });

  document
    .querySelector<HTMLFormElement>("[data-form='evidence']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const kind = String(data.get("kind") || "");
      const reference = String(data.get("reference") || "").trim();
      if (!kind || !reference) {
        showFormError(document.querySelector("#evidence-error"), t(locale, "required"));
        return;
      }
      try {
        await client.addEvidence(orgId, paymentId, { kind, reference });
        await renderRentClaimDetail(client, orgId, paymentId, locale);
      } catch (err) {
        showFormError(document.querySelector("#evidence-error"), formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='verify']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const raw = String(data.get("verified_amount") || "").trim();
      const body: { verified_amount?: string | null } = raw.length > 0 ? { verified_amount: raw } : {};
      try {
        await client.verifyPayment(orgId, paymentId, body);
        await renderRentClaimDetail(client, orgId, paymentId, locale);
      } catch (err) {
        showFormError(document.querySelector("#verify-error"), formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='reject']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const reason = String(data.get("reason") || "").trim();
      if (!reason) {
        showFormError(document.querySelector("#reject-error"), t(locale, "required"));
        return;
      }
      try {
        await client.rejectPayment(orgId, paymentId, reason);
        await renderRentClaimDetail(client, orgId, paymentId, locale);
      } catch (err) {
        showFormError(document.querySelector("#reject-error"), formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='reverse']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const reason = String(data.get("reason") || "").trim();
      if (!reason) {
        showFormError(document.querySelector("#reverse-error"), t(locale, "required"));
        return;
      }
      try {
        await client.reversePayment(orgId, paymentId, reason);
        await renderRentClaimDetail(client, orgId, paymentId, locale);
      } catch (err) {
        showFormError(document.querySelector("#reverse-error"), formatError(err, locale));
      }
    });
}

// ---------- helpers ----------

function showFormError(el: Element | null, msg: string): void {
  if (!el) return;
  (el as HTMLElement).textContent = msg;
  (el as HTMLElement).hidden = false;
}

function formatError(err: unknown, locale: Locale): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return t(locale, "accessDenied");
    if (err.status === 403) return t(locale, "accessDenied");
    if (err.status === 404) return t(locale, "rentNotFound");
    if (err.status === 400) return t(locale, "validationError");
    if (err.status === 409) return t(locale, "conflictError");
    return err.message;
  }
  return t(locale, "networkError");
}

function renderDetailError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "rentClaimTitle")}</h2>
    <p class="muted">${escapeHtml(formatError(err, locale))}</p>
    <a class="primary-btn" href="#/finance" data-action="back">← ${t(locale, "finance")}</a>
  </section>`;
}