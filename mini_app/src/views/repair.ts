/** Repair detail (Owner view) — full Owner workflow through closure.
 *
 *  Coverage Matrix Repair slice (rows 6.1–6.9) + Issue #99 OWNER ADDENDUM
 *  repair/closure row. The Mini App is a thin transport over
 *  `app.v1.services.repair.RepairService`. Every state transition is
 *  delegated to the typed client; no business state machine lives here.
 *
 *  No business truth in localStorage. No silent success: every form
 *  surfaces 4xx/5xx messages explicitly.
 *
 *  Coverage 5.8 / 5.9 invariants enforced by the service and surfaced
 *  here:
 *    - Payment/approval/evidence can never close a Repair. The only
 *      closure transition is the OWNER's explicit `close()` (via
 *      `verifyRepairCompletion`), gated by
 *      `assert_not_closed_by_payment` server-side.
 *    - Invalid or repeated close/action raises 4xx (not silent
 *      success). Verified-reversal (REVERSED) re-opens the report.
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
  Repair,
  RepairActivity,
  RepairCompletionClaim,
  RepairQuote,
  RepairVerification,
  RepairWork,
} from "../types";

const REPAIR_WORK_STATES = ["STARTED", "BLOCKED", "PROGRESS", "DONE_ON_SITE"] as const;
const TECHNICIAN_SOURCES = ["INTERNAL", "EXTERNAL"] as const;

// ---------- entry point ----------

export async function renderRepairDetail(
  client: PasayClient,
  orgId: number,
  repairId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(
    `<section class="card loading"><p>${t(locale, "loading")}</p></section>`,
  );
  try {
    const reports = await client.listRepairs(orgId);
    const report = reports.find((r) => r.id === repairId) ?? null;
    if (!report) {
      setViewContent(
        renderError(Object.assign(new Error("not found"), { status: 404 }), locale),
      );
      return;
    }
    const [op, activity, quotes, work, claims, verifications] = await Promise.all([
      client.getRepairOperation(orgId, repairId).catch(() => null),
      client.listRepairActivity(orgId, repairId).catch(() => [] as RepairActivity[]),
      client.listRepairQuotes(orgId, repairId).catch(() => [] as RepairQuote[]),
      client.listRepairWork(orgId, repairId).catch(() => [] as RepairWork[]),
      client
        .listRepairCompletionClaims(orgId, repairId)
        .catch(() => [] as RepairCompletionClaim[]),
      client
        .listRepairVerifications(orgId, repairId)
        .catch(() => [] as RepairVerification[]),
    ]);
    const body = renderBody(
      report,
      op,
      activity,
      quotes,
      work,
      claims,
      verifications,
      locale,
    );
    setViewContent(body);
    bindHandlers(client, orgId, repairId, locale);
  } catch (err) {
    setViewContent(renderError(err, locale));
  }
}

// ---------- body ----------

function renderBody(
  report: Repair,
  operation: Operation | null,
  activity: RepairActivity[],
  quotes: RepairQuote[],
  work: RepairWork[],
  claims: RepairCompletionClaim[],
  verifications: RepairVerification[],
  locale: Locale,
): string {
  return (
    renderHeader(report, operation, locale) +
    renderAssignSection(report, locale) +
    renderQuoteSection(report, quotes, locale) +
    renderWorkSection(report, work, locale) +
    renderCompletionSection(report, claims, locale) +
    renderVerificationSection(report, verifications, locale) +
    renderActivitySection(activity, locale)
  );
}

function renderHeader(
  report: Repair,
  operation: Operation | null,
  locale: Locale,
): string {
  const techLine = report.technician_name
    ? `<span class="muted">${t(locale, "repairTechnician")}: ${escapeHtml(report.technician_name)} (${escapeHtml(report.technician_source ?? "?")})</span>` +
      (report.technician_eta_at
        ? ` · <span class="muted">${t(locale, "repairTechnicianEta")}: ${escapeHtml(formatDate(report.technician_eta_at))}</span>`
        : "")
    : "";
  const linkedLine =
    report.linked_expense_payment_id !== null
      ? `<span class="muted">${t(locale, "repairLinkedExpense")}: #${escapeHtml(String(report.linked_expense_payment_id))}</span>`
      : "";
  return `<section class="panel">
    <header class="panel-header">
      <h2>${t(locale, "repairTitle")} #${escapeHtml(String(report.id))}</h2>
      <a class="ghost-btn" href="#/work" data-action="back">← ${t(locale, "work")}</a>
    </header>
    <p>
      <span class="${statusToneClass(report.state)}">${statusLabel(report.state, locale)}</span>
      <b>${escapeHtml(report.title)}</b>
      <span class="muted">${escapeHtml(report.description.slice(0, 200))}</span>
    </p>
    <ul class="list">
      <li><b>${t(locale, "repairCategory")}:</b> ${escapeHtml(report.category)}</li>
      <li><b>${t(locale, "repairSeverity")}:</b> ${escapeHtml(report.severity)}</li>
      <li><b>${t(locale, "reportedAt")}:</b> ${escapeHtml(formatDate(report.reported_at))}</li>
      ${report.quoted_amount !== null
        ? `<li><b>${t(locale, "repairQuoteAmount")}:</b> ${formatMoney(report.quoted_amount)}</li>`
        : ""}
      ${techLine ? `<li>${techLine}</li>` : ""}
      ${linkedLine ? `<li>${linkedLine}</li>` : ""}
      <li><b>${t(locale, "repairIdempotencyKey")}:</b> <code>${escapeHtml(report.idempotency_key)}</code></li>
      ${report.completed_at
        ? `<li><b>${t(locale, "repairAlreadyClosed")}:</b> ${escapeHtml(formatDate(report.completed_at))}</li>`
        : ""}
    </ul>
    ${operation
      ? `<p class="muted">${t(locale, "repairOperation")}: <span class="${statusToneClass(operation.state)}">${statusLabel(operation.state, locale)}</span>${operation.resolved_at ? ` · ${escapeHtml(formatDate(operation.resolved_at))}` : ""}</p>`
      : ""}
    <div class="row-actions">
      ${renderTopBar(report, locale)}
    </div>
  </section>`;
}

function renderTopBar(report: Repair, locale: Locale): string {
  const parts: string[] = [];
  if (report.state === "REPORTED") {
    parts.push(
      `<button class="primary-btn" type="button" data-action="confirm">${t(locale, "repairConfirmAction")}</button>`,
    );
  }
  if (
    report.state === "CONFIRMED" ||
    report.state === "AWAITING_TECHNICIAN" ||
    report.state === "QUOTE_REQUESTED" ||
    report.state === "QUOTE_RECEIVED"
  ) {
    parts.push(
      `<button class="ghost-btn" type="button" data-action="request-quote">${t(locale, "repairRequestQuote")}</button>`,
    );
  }
  if (
    report.state !== "COMPLETED" &&
    report.state !== "CANCELLED"
  ) {
    parts.push(
      `<button class="ghost-btn" type="button" data-action="cancel">${t(locale, "repairCancelReport")}</button>`,
    );
  }
  return parts.join("");
}

// ---------- assign technician ----------

function renderAssignSection(report: Repair, locale: Locale): string {
  const canAssign =
    report.state === "CONFIRMED" ||
    report.state === "AWAITING_TECHNICIAN" ||
    report.state === "QUOTE_REQUESTED";
  if (!canAssign) return "";
  return `<section class="panel" id="assign-panel">
    <h3>${t(locale, "repairAssignSection")}</h3>
    <form id="assign-form" class="form" data-form="assign">
      <label>${t(locale, "repairAssignName")}
        <input name="technician_name" required maxlength="200" />
      </label>
      <label>${t(locale, "repairAssignSource")}
        <select name="technician_source" required>
          ${TECHNICIAN_SOURCES.map(
            (s) => `<option value="${s}">${s}</option>`,
          ).join("")}
        </select>
      </label>
      <label>${t(locale, "repairAssignEta")}
        <input name="technician_eta_at" type="datetime-local" />
      </label>
      <div class="form-actions">
        <button class="primary-btn" type="submit">${t(locale, "repairAssignAction")}</button>
      </div>
      <p class="muted form-error" id="assign-error" hidden></p>
    </form>
  </section>`;
}

// ---------- quotes ----------

function renderQuoteSection(
  report: Repair,
  quotes: RepairQuote[],
  locale: Locale,
): string {
  const canSubmit =
    report.state === "AWAITING_TECHNICIAN" ||
    report.state === "QUOTE_REQUESTED" ||
    report.state === "QUOTE_RECEIVED";
  const quoteList = quotes.length === 0
    ? `<p class="muted">${t(locale, "repairNoQuotes")}</p>`
    : `<ul class="list" data-list="quotes">${quotes
        .map(
          (q) => `<li data-quote-id="${q.id}">
            <b>${formatMoney(q.amount)}</b>
            <span class="${statusToneClass(q.decision)}">${quoteDecisionLabel(q.decision, locale)}</span>
            <span class="muted">${escapeHtml(q.technician_name)}</span>
            <span class="muted">${escapeHtml(formatDate(q.decided_at))}</span>
            <p class="muted">${escapeHtml(q.description.slice(0, 200))}</p>
            ${q.reason ? `<p class="muted">${escapeHtml(q.reason)}</p>` : ""}
            ${quoteActions(q, locale)}
          </li>`,
        )
        .join("")}</ul>`;
  const submitForm = canSubmit
    ? `<form id="quote-form" class="form" data-form="quote">
        <label>${t(locale, "repairQuoteAmount")}
          <input name="amount" required inputmode="decimal" pattern="^[0-9]+(\.[0-9]{1,2})?$" placeholder="0.00" />
        </label>
        <label>${t(locale, "repairQuoteTechnician")}
          <input name="technician_name" required maxlength="200" />
        </label>
        <label>${t(locale, "repairQuoteDescription")}
          <textarea name="description" required rows="3" maxlength="2000"></textarea>
        </label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "repairQuoteDecisionSubmit")}</button>
        </div>
        <p class="muted form-error" id="quote-error" hidden></p>
      </form>`
    : "";
  return `<section class="panel" id="quote-panel">
    <h3>${t(locale, "repairQuotesSection")}</h3>
    ${quoteList}
    ${submitForm}
  </section>`;
}

function quoteDecisionLabel(decision: RepairQuote["decision"], locale: Locale): string {
  if (decision === "SUBMITTED") return t(locale, "repairQuoteSubmitted");
  if (decision === "APPROVED") return t(locale, "repairQuoteApproved");
  if (decision === "REJECTED") return t(locale, "repairQuoteRejected");
  return decision;
}

function quoteActions(q: RepairQuote, locale: Locale): string {
  if (q.decision !== "SUBMITTED") return "";
  return `<div class="row-actions">
    <button class="ghost-btn" type="button" data-action="approve-quote" data-quote-id="${q.id}">${t(locale, "repairQuoteApprove")}</button>
    <button class="ghost-btn" type="button" data-action="reject-quote" data-quote-id="${q.id}">${t(locale, "repairQuoteReject")}</button>
  </div>`;
}

// ---------- work ----------

function renderWorkSection(report: Repair, work: RepairWork[], locale: Locale): string {
  const canAppend =
    report.state === "QUOTE_APPROVED" ||
    report.state === "IN_PROGRESS" ||
    report.state === "COMPLETION_CLAIMED";
  const workList = work.length === 0
    ? `<p class="muted">${t(locale, "repairNoWork")}</p>`
    : `<ul class="list" data-list="work">${work
        .map(
          (w) => `<li data-work-id="${w.id}">
            <b>${escapeHtml(w.state)}</b>
            <span class="muted">${escapeHtml(formatDate(w.occurred_at))}</span>
            <p class="muted">${escapeHtml(w.note)}</p>
          </li>`,
        )
        .join("")}</ul>`;
  const appendForm = canAppend
    ? `<form id="work-form" class="form" data-form="work">
        <label>${t(locale, "repairWorkState")}
          <select name="state" required>
            ${REPAIR_WORK_STATES.map(
              (s) => `<option value="${s}">${s}</option>`,
            ).join("")}
          </select>
        </label>
        <label>${t(locale, "repairWorkNote")}
          <textarea name="note" required rows="3" maxlength="2000"></textarea>
        </label>
        <p class="muted">${t(locale, "repairWorkStatesTip")}</p>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "repairWorkAppend")}</button>
        </div>
        <p class="muted form-error" id="work-error" hidden></p>
      </form>`
    : "";
  return `<section class="panel" id="work-panel">
    <h3>${t(locale, "repairWorkSection")}</h3>
    ${workList}
    ${appendForm}
  </section>`;
}

// ---------- completion ----------

function renderCompletionSection(
  report: Repair,
  claims: RepairCompletionClaim[],
  locale: Locale,
): string {
  const canClaim =
    report.state === "IN_PROGRESS" ||
    report.state === "COMPLETION_CLAIMED";
  const claimList = claims.length === 0
    ? `<p class="muted">${t(locale, "repairNoCompletionClaims")}</p>`
    : `<ul class="list" data-list="claims">${claims
        .map(
          (c) => `<li data-claim-id="${c.id}">
            <b>${escapeHtml(c.summary.slice(0, 200))}</b>
            <span class="muted">${escapeHtml(formatDate(c.claimed_at))}</span>
          </li>`,
        )
        .join("")}</ul>`;
  const claimForm = canClaim
    ? `<form id="claim-form" class="form" data-form="claim">
        <label>${t(locale, "repairCompletionSummary")}
          <textarea name="summary" required rows="3" maxlength="2000"></textarea>
        </label>
        <div class="form-actions">
          <button class="primary-btn" type="submit">${t(locale, "repairCompletionClaim")}</button>
        </div>
        <p class="muted form-error" id="claim-error" hidden></p>
      </form>`
    : "";
  return `<section class="panel" id="claim-panel">
    <h3>${t(locale, "repairCompletionSection")}</h3>
    ${claimList}
    ${claimForm}
  </section>`;
}

// ---------- verification (closure gate) ----------

function renderVerificationSection(
  report: Repair,
  verifications: RepairVerification[],
  locale: Locale,
): string {
  const verifList = verifications.length === 0
    ? `<p class="muted">${t(locale, "rentNoVerifications")}</p>`
    : `<ul class="list" data-list="verifications">${verifications
        .map(
          (v) => `<li data-verification-id="${v.id}">
            <span class="${statusToneClass(v.decision)}">${statusLabel(v.decision, locale)}</span>
            <span class="muted">${escapeHtml(formatDate(v.decided_at))}</span>
            ${v.reason ? `<span class="muted">${escapeHtml(v.reason)}</span>` : ""}
          </li>`,
        )
        .join("")}</ul>`;
  const canVerify =
    report.state === "COMPLETION_CLAIMED" ||
    report.state === "IN_PROGRESS";
  const canReject = report.state === "COMPLETION_CLAIMED";
  const canReverse =
    verifications.some((v) => v.decision === "VERIFIED") &&
    report.state === "COMPLETED";
  const canClose = report.state === "COMPLETED";
  if (!canVerify && !canReject && !canReverse && !canClose) {
    return `<section class="panel" id="verify-panel">
      <h3>${t(locale, "repairVerificationSection")}</h3>
      ${verifList}
      <p class="muted">${t(locale, "repairDecisionClosed")}</p>
      <p class="muted">${t(locale, "repairCloseNote")}</p>
    </section>`;
  }
  return `<section class="panel" id="verify-panel">
    <h3>${t(locale, "repairVerificationSection")}</h3>
    ${verifList}
    ${canVerify
      ? `<form id="verify-form" class="form" data-form="verify">
          <p class="muted">${t(locale, "repairVerifyNote")}</p>
          <label>${t(locale, "workReason")}
            <textarea name="reason" required minlength="1" maxlength="500" rows="2"></textarea>
          </label>
          <div class="form-actions">
            <button class="primary-btn" type="submit">${t(locale, "repairVerifyCompletion")}</button>
          </div>
          <p class="muted form-error" id="verify-error" hidden></p>
        </form>`
      : ""}
    ${canReject
      ? `<form id="reject-form" class="form" data-form="reject-completion">
          <p class="muted">${t(locale, "repairRejectNote")}</p>
          <label>${t(locale, "workReason")}
            <textarea name="reason" required minlength="1" maxlength="500" rows="2"></textarea>
          </label>
          <div class="form-actions">
            <button class="ghost-btn" type="submit">${t(locale, "repairRejectCompletion")}</button>
          </div>
          <p class="muted form-error" id="reject-error" hidden></p>
        </form>`
      : ""}
    ${canReverse
      ? `<form id="reverse-form" class="form" data-form="reverse-verification">
          <label>${t(locale, "workReason")}
            <textarea name="reason" required minlength="1" maxlength="500" rows="2"></textarea>
          </label>
          <div class="form-actions">
            <button class="ghost-btn" type="submit">${t(locale, "repairReverseVerification")}</button>
          </div>
          <p class="muted form-error" id="reverse-error" hidden></p>
        </form>`
      : ""}
    ${canClose
      ? `<button class="primary-btn" type="button" data-action="close">${t(locale, "repairClose")}</button>
         <p class="muted">${t(locale, "repairCloseNote")}</p>`
      : ""}
  </section>`;
}

// ---------- activity feed ----------

function renderActivitySection(activity: RepairActivity[], locale: Locale): string {
  if (activity.length === 0) {
    return `<section class="panel">
      <h3>${t(locale, "repairActivitySection")}</h3>
      <p class="muted">${t(locale, "empty")}</p>
    </section>`;
  }
  return `<section class="panel">
    <h3>${t(locale, "repairActivitySection")}</h3>
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

function bindHandlers(
  client: PasayClient,
  orgId: number,
  repairId: number,
  locale: Locale,
): void {
  document
    .querySelector<HTMLAnchorElement>("[data-action='back']")
    ?.addEventListener("click", (e) => {
      e.preventDefault();
      router.navigate({ name: "work" });
    });

  document
    .querySelector<HTMLButtonElement>("[data-action='confirm']")
    ?.addEventListener("click", () =>
      runAndRefresh(() => client.confirmRepair(orgId, repairId), client, orgId, repairId, locale),
    );
  document
    .querySelector<HTMLButtonElement>("[data-action='request-quote']")
    ?.addEventListener("click", () =>
      runAndRefresh(() => client.requestQuote(orgId, repairId), client, orgId, repairId, locale),
    );
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => {
      const reason = window.prompt(t(locale, "workReason") + ":", "");
      if (!reason) return;
      runAndRefresh(
        () => client.cancelRepair(orgId, repairId, reason),
        client, orgId, repairId, locale,
      );
    });
  document
    .querySelector<HTMLButtonElement>("[data-action='close']")
    ?.addEventListener("click", () =>
      runAndRefresh(
        () => client.closeRepair(orgId, repairId),
        client, orgId, repairId, locale,
      ),
    );

  document.querySelectorAll<HTMLButtonElement>("[data-action='approve-quote']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const quoteId = Number(btn.dataset.quoteId);
      runAndRefresh(
        () => client.approveQuote(orgId, repairId, quoteId),
        client, orgId, repairId, locale,
      );
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-action='reject-quote']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const quoteId = Number(btn.dataset.quoteId);
      const reason = window.prompt(t(locale, "workReason") + ":", "");
      if (!reason) return;
      runAndRefresh(
        () => client.rejectQuote(orgId, repairId, quoteId, reason),
        client, orgId, repairId, locale,
      );
    });
  });

  document
    .querySelector<HTMLFormElement>("[data-form='assign']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const technician_name = String(data.get("technician_name") || "").trim();
      const technician_source = String(data.get("technician_source") || "");
      const etaRaw = String(data.get("technician_eta_at") || "").trim();
      if (!technician_name || !technician_source) {
        showFormError("assign-error", t(locale, "required"));
        return;
      }
      const body: {
        technician_name: string;
        technician_source: string;
        technician_eta_at?: string | null;
      } = { technician_name, technician_source };
      if (etaRaw.length > 0) body.technician_eta_at = etaRaw;
      try {
        await client.assignTechnician(orgId, repairId, body);
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("assign-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='quote']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const amount = String(data.get("amount") || "").trim();
      const technician_name = String(data.get("technician_name") || "").trim();
      const description = String(data.get("description") || "").trim();
      if (!amount || !technician_name || !description) {
        showFormError("quote-error", t(locale, "required"));
        return;
      }
      try {
        await client.submitQuote(
          orgId,
          repairId,
          { amount, description, technician_name },
          makeIdempotencyKey("repair-quote"),
        );
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("quote-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='work']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const state = String(data.get("state") || "");
      const note = String(data.get("note") || "").trim();
      if (!state || !note) {
        showFormError("work-error", t(locale, "required"));
        return;
      }
      try {
        await client.recordRepairWork(orgId, repairId, { state, note });
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("work-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='claim']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const summary = String(data.get("summary") || "").trim();
      if (!summary) {
        showFormError("claim-error", t(locale, "required"));
        return;
      }
      try {
        await client.claimRepairCompletion(orgId, repairId, summary);
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("claim-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='verify']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const reason = String(data.get("reason") || "").trim();
      if (!reason) {
        showFormError("verify-error", t(locale, "required"));
        return;
      }
      try {
        await client.verifyRepairCompletion(orgId, repairId, reason);
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("verify-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='reject-completion']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const reason = String(data.get("reason") || "").trim();
      if (!reason) {
        showFormError("reject-error", t(locale, "required"));
        return;
      }
      try {
        await client.rejectRepairCompletion(orgId, repairId, reason);
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("reject-error", formatError(err, locale));
      }
    });

  document
    .querySelector<HTMLFormElement>("[data-form='reverse-verification']")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target as HTMLFormElement);
      const reason = String(data.get("reason") || "").trim();
      if (!reason) {
        showFormError("reverse-error", t(locale, "required"));
        return;
      }
      try {
        await client.reverseRepairVerification(orgId, repairId, reason);
        await renderRepairDetail(client, orgId, repairId, locale);
      } catch (err) {
        showFormError("reverse-error", formatError(err, locale));
      }
    });
}

async function runAndRefresh(
  action: () => Promise<unknown>,
  client: PasayClient,
  orgId: number,
  repairId: number,
  locale: Locale,
): Promise<void> {
  try {
    await action();
    await renderRepairDetail(client, orgId, repairId, locale);
  } catch (err) {
    window.alert(formatError(err, locale));
  }
}

// ---------- helpers ----------

function showFormError(id: string, msg: string): void {
  const el = document.querySelector(`#${id}`);
  if (!el) return;
  (el as HTMLElement).textContent = msg;
  (el as HTMLElement).hidden = false;
}

function formatError(err: unknown, locale: Locale): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) return t(locale, "accessDenied");
    if (err.status === 404) return t(locale, "repairNotFound");
    if (err.status === 400) return t(locale, "validationError");
    if (err.status === 409) return t(locale, "conflictError");
    return err.message;
  }
  return t(locale, "networkError");
}

function renderError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "repairTitle")}</h2>
    <p class="muted">${escapeHtml(formatError(err, locale))}</p>
    <a class="primary-btn" href="#/work" data-action="back">← ${t(locale, "work")}</a>
  </section>`;
}
