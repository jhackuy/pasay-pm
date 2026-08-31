/** Move-out / Settlement Owner workflow.
 *
 *  Real round trip required by Issue #99 OWNER ADDENDUM for the
 *  Coverage Matrix Move-out slice (rows 7.1–7.7):
 *
 *   1. request         — POST /api/v1/move-outs (fresh Idempotency-Key)
 *   2. inspection      — POST /api/v1/move-outs/:id/inspections
 *   3. damage          — POST /api/v1/move-outs/:id/damages
 *   4. accept damage   — POST /api/v1/move-outs/damages/:id/accept
 *   5. keys/arrears    — POST /api/v1/move-outs/:id/keys-arrears
 *   6. settle          — POST /api/v1/move-outs/:id/settlement
 *                        (closure gate: state→SETTLED, Operation→resolved)
 *   7. atomic close    — POST /api/v1/move-outs/:id/close
 *                        (terminate lease, free unit, archive, resolve op)
 *
 *  Money is Decimal-as-string. No localStorage business truth. Errors
 *  surface explicitly as 4xx/5xx — never as silent success.
 *
 *  This module is intentionally side-effect-free apart from DOM
 *  rendering and direct API calls; it has no duplicated business state
 *  machine. Every state transition is delegated to
 *  `app.v1.services.move_out.MoveOutService` via the typed
 *  `PasayClient` surface.
 */

import type { PasayClient } from "../api";
import { ApiError } from "../api";
import { setViewContent } from "../shell";
import { type Locale, t } from "../i18n";
import { escapeHtml } from "./home";
import {
  formatMoney,
  makeIdempotencyKey,
  statusLabel,
  statusToneClass,
} from "../format";
import type {
  DepositDisposition,
  Lease,
  MoveOut,
  MoveOutActivity,
  MoveOutBalance,
  MoveOutDamage,
  MoveOutInspection,
} from "../types";

const DAMAGE_KINDS: Array<MoveOutDamage["kind"]> = [
  "CLEANING",
  "REPAIR",
  "REPLACEMENT",
  "UTILITIES",
  "OTHER",
];

const DISPOSITIONS: DepositDisposition[] = [
  "FULL_REFUND",
  "PARTIAL_REFUND",
  "NO_REFUND",
  "ADDITIONAL_OWED",
];

// ---------- list (embedded inside Work view) ----------

/**
 * Render the Work view's move-out list. Each row links to the detail
 * view via the hash router.
 */
export function renderMoveOutList(
  items: MoveOut[],
  leases: Lease[],
  locale: Locale,
): string {
  if (items.length === 0) return "";
  const leaseLabels = new Map<number, Lease>();
  for (const l of leases) leaseLabels.set(l.id, l);
  return `<section class="panel">
    <h3>${t(locale, "moveOutTitle")}</h3>
    <ul class="list">${items
      .map((m) => {
        const lease = leaseLabels.get(m.lease_id);
        const leaseLabel = lease
          ? `#${m.lease_id} · ${t(locale, "tenants")} #${lease.tenant_id} · ${t(locale, "units")} #${lease.unit_id}`
          : `${t(locale, "leases")} #${m.lease_id}`;
        return `<li class="list-row list-row--action">
          <div class="row-main">
            <b>#${m.id} · ${escapeHtml(leaseLabel)}</b>
            <span class="${statusToneClass(m.state)}">${statusLabel(m.state, locale)}</span>
            <span class="muted">${escapeHtml(m.requested_at)}</span>
            ${m.planned_move_out_date ? `<span class="muted">${t(locale, "moveOutPlannedDate")}: ${escapeHtml(m.planned_move_out_date)}</span>` : ""}
            ${m.arrears_amount && Number(m.arrears_amount) > 0
              ? `<span class="status status--bad">${t(locale, "moveOutArrears")} ${formatMoney(m.arrears_amount)}</span>`
              : ""}
          </div>
          <div class="row-actions">
            <button class="primary-btn" data-action="open-moveout-${m.id}" data-move-out-id="${m.id}" type="button">${t(locale, "moveOutDetail")}</button>
          </div>
        </li>`;
      })
      .join("")}</ul>
  </section>`;
}

/** Open-form posted against the live move-out service. */
export function renderMoveOutOpenForm(
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
              `<option value="${l.id}">` +
              `#${l.id} · ${t(locale, "tenants")} #${l.tenant_id} · ` +
              `${t(locale, "units")} #${l.unit_id} · ` +
              `${escapeHtml(l.start_date)} → ${escapeHtml(l.end_date)}` +
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
      <header class="panel-header"><h2>${t(locale, "moveOutOpenTitle")}</h2></header>
      ${emptyHint}
      <form id="moveout-open-form" class="form">
        <label>${t(locale, "moveOutSelectLease")}
          <select name="lease_id" required ${activeLeases.length === 0 ? "disabled" : ""}>
            ${leaseOptions}
          </select>
        </label>
        <label>${t(locale, "moveOutPlannedDate")}<input name="planned_move_out_date" type="date" /></label>
        <label>${t(locale, "moveOutNotes")}<textarea name="notes" rows="3" maxlength="2000"></textarea></label>
        <div class="form-actions">
          <button class="primary-btn" type="submit" ${activeLeases.length === 0 ? "disabled" : ""}>${t(locale, "submit")}</button>
          <button class="ghost-btn" type="button" data-action="cancel">${t(locale, "cancel")}</button>
        </div>
        <p class="muted form-error" id="moveout-open-error" hidden></p>
      </form>
    </section>
  `);
  document
    .querySelector<HTMLButtonElement>("[data-action='cancel']")
    ?.addEventListener("click", () => {
      window.location.hash = "#/work";
    });
  document
    .querySelector<HTMLFormElement>("#moveout-open-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const leaseId = Number(data.get("lease_id"));
      if (!leaseId) {
        showFormError(
          document.querySelector("#moveout-open-error"),
          t(locale, "required"),
        );
        return;
      }
      try {
        const moveOut = await client.requestMoveOut(
          orgId,
          {
            lease_id: leaseId,
            planned_move_out_date:
              (String(data.get("planned_move_out_date") || "").trim() || null) as string | null,
            notes: (String(data.get("notes") || "").trim() || null) as string | null,
          },
          makeIdempotencyKey("moveout"),
        );
        window.location.hash = `#/move-outs/${moveOut.id}`;
      } catch (err) {
        showFormError(
          document.querySelector("#moveout-open-error"),
          formatError(err, locale),
        );
      }
    });
}

// ---------- detail (single page) ----------

/**
 * Render the full move-out detail page. Loads the move-out, its
 * inspection list, its damage list, the running balance, and the
 * activity feed. Renders the appropriate sub-form depending on the
 * current state. Every action is delegated to the typed client.
 */
export async function renderMoveOutDetail(
  client: PasayClient,
  orgId: number,
  moveOutId: number,
  locale: Locale,
): Promise<void> {
  setViewContent(
    `<section class="card loading"><p>${t(locale, "loading")}</p></section>`,
  );
  try {
    const [moveOut, inspections, damages, balance, activity, leases] =
      await Promise.all([
        client.getMoveOut(orgId, moveOutId),
        client.listInspections(orgId, moveOutId),
        client.listDamages(orgId, moveOutId),
        client.getMoveOutBalance(orgId, moveOutId),
        client.listMoveOutActivity(orgId, moveOutId),
        client.listLeases(orgId),
      ]);
    const body = renderDetailBody(
      moveOut,
      inspections,
      damages,
      balance,
      activity,
      leases,
      locale,
    );
    setViewContent(body);
    bindDetailHandlers(client, orgId, moveOutId, locale);
  } catch (err) {
    setViewContent(renderDetailError(err, locale));
  }
}

function renderDetailBody(
  m: MoveOut,
  inspections: MoveOutInspection[],
  damages: MoveOutDamage[],
  balance: MoveOutBalance,
  activity: MoveOutActivity[],
  leases: Lease[],
  locale: Locale,
): string {
  const lease = leases.find((l) => l.id === m.lease_id);
  const leaseLabel = lease
    ? `#${m.lease_id} · ${t(locale, "tenants")} #${lease.tenant_id} · ${t(locale, "units")} #${lease.unit_id}`
    : `${t(locale, "leases")} #${m.lease_id}`;
  const header = `
    <section class="panel">
      <header class="panel-header">
        <h2>${t(locale, "moveOutTitle")} #${m.id}</h2>
        <a class="ghost-btn" href="#/work" data-action="back">← ${t(locale, "work")}</a>
      </header>
      <p>
        <b>${escapeHtml(leaseLabel)}</b><br/>
        <span class="${statusToneClass(m.state)}">${statusLabel(m.state, locale)}</span>
        <span class="muted">${escapeHtml(m.requested_at)}</span>
        ${m.planned_move_out_date ? `· <span class="muted">${t(locale, "moveOutPlannedDate")}: ${escapeHtml(m.planned_move_out_date)}</span>` : ""}
        ${m.settled_at ? `· <span class="muted">${t(locale, "workSettleMoveOut")}: ${escapeHtml(m.settled_at)}</span>` : ""}
        ${m.archived_at ? `· <span class="muted">${t(locale, "moveOutCloseTitle")}: ${escapeHtml(m.archived_at)}</span>` : ""}
      </p>
    </section>
  `;
  return (
    header +
    renderInspectionSection(m, inspections, locale) +
    renderDamageSection(m, damages, locale) +
    renderKeysArrearsSection(m, balance, locale) +
    renderSettleSection(m, balance, locale) +
    renderCloseSection(m, locale) +
    renderActivitySection(activity, locale)
  );
}

function renderInspectionSection(
  m: MoveOut,
  inspections: MoveOutInspection[],
  locale: Locale,
): string {
  const canRecord = m.state === "REQUESTED" || m.state === "INSPECTED";
  return `<section class="panel">
    <h3>${t(locale, "moveOutInspectionTitle")}</h3>
    ${
      inspections.length === 0
        ? `<p class="muted" data-state="no-inspections">${t(locale, "moveOutNoInspections")}</p>`
        : `<ul class="list">${inspections
            .map(
              (i) => `<li>
                <b>${escapeHtml(i.inspected_at)}</b>
                <span class="muted">${escapeHtml(i.summary)}</span>
              </li>`,
            )
            .join("")}</ul>`
    }
    ${
      canRecord
        ? `<form id="inspection-form" class="form">
            <label>${t(locale, "moveOutInspectionSummary")}<textarea name="summary" rows="3" required maxlength="4000"></textarea></label>
            <div class="form-actions">
              <button class="primary-btn" type="submit">${t(locale, "moveOutRecordInspection")}</button>
            </div>
            <p class="muted form-error" id="inspection-error" hidden></p>
          </form>`
        : ""
    }
  </section>`;
}

function renderDamageSection(
  m: MoveOut,
  damages: MoveOutDamage[],
  locale: Locale,
): string {
  const canAdd = m.state === "INSPECTED";
  const canAccept = m.state === "INSPECTED";
  return `<section class="panel">
    <h3>${t(locale, "moveOutDamageTitle")}</h3>
    ${
      damages.length === 0
        ? `<p class="muted">${t(locale, "moveOutNoDamages")}</p>`
        : `<ul class="list">${damages
            .map(
              (d) => `<li class="list-row list-row--action">
                <div class="row-main">
                  <b>${escapeHtml(d.kind)}</b>
                  <span class="muted">${escapeHtml(d.description)}</span>
                  <span>${t(locale, "moveOutDamageAmount")}: ${formatMoney(d.amount)}</span>
                  <span>${t(locale, "moveOutDamageAccepted")}: ${formatMoney(d.accepted_amount)}</span>
                </div>
                <div class="row-actions">
                  ${
                    canAccept
                      ? `<button class="ghost-btn" data-action="open-accept-damage-${d.id}" data-damage-id="${d.id}" type="button">${t(locale, "moveOutAcceptDamage")}</button>`
                      : ""
                  }
                </div>
              </li>`,
            )
            .join("")}</ul>`
    }
    ${
      canAdd
        ? `<form id="damage-form" class="form">
            <label>${t(locale, "moveOutDamageKind")}
              <select name="kind" required>
                ${DAMAGE_KINDS.map((k) => `<option value="${k}">${k}</option>`).join("")}
              </select>
            </label>
            <label>${t(locale, "moveOutDamageDescription")}<input name="description" required maxlength="2000" /></label>
            <label>${t(locale, "moveOutDamageAmount")}<input name="amount" required inputmode="decimal" placeholder="0.00" /></label>
            <div class="form-actions">
              <button class="primary-btn" type="submit">${t(locale, "moveOutAddDamage")}</button>
            </div>
            <p class="muted form-error" id="damage-error" hidden></p>
          </form>`
        : ""
    }
  </section>`;
}

function renderKeysArrearsSection(
  m: MoveOut,
  balance: MoveOutBalance,
  locale: Locale,
): string {
  const canRecord =
    m.state !== "SETTLED" && m.state !== "CANCELLED";
  return `<section class="panel">
    <h3>${t(locale, "moveOutKeysArrearsTitle")}</h3>
    <p>
      <span>${t(locale, "moveOutKeysReturned")}: <b>${m.keys_returned === null ? "—" : m.keys_returned ? "✓" : "✗"}</b></span>
      ${m.arrears_amount && Number(m.arrears_amount) > 0 ? `<span class="status status--bad">${formatMoney(m.arrears_amount)}</span>` : ""}
    </p>
    <p class="muted">${t(locale, "workNotes")}: ${escapeHtml(m.keys_arrears_notes ?? "—")}</p>
    <p class="muted">${t(locale, "moveOutArrears")} (running): ${formatMoney(balance.deductions_total)}</p>
    ${
      canRecord
        ? `<form id="keys-arrears-form" class="form">
            <label>${t(locale, "moveOutKeysReturned")}
              <select name="keys_returned" required>
                <option value="true" ${m.keys_returned === true ? "selected" : ""}>✓</option>
                <option value="false" ${m.keys_returned === false ? "selected" : ""}>✗</option>
              </select>
            </label>
            <label>${t(locale, "moveOutArrears")}<input name="arrears_amount" inputmode="decimal" placeholder="0.00" value="${
              m.arrears_amount ?? "0.00"
            }" /></label>
            <label>${t(locale, "workNotes")}<textarea name="notes" rows="2" maxlength="2000"></textarea></label>
            <div class="form-actions">
              <button class="primary-btn" type="submit">${t(locale, "moveOutRecordKeysArrears")}</button>
            </div>
            <p class="muted form-error" id="keys-arrears-error" hidden></p>
          </form>`
        : ""
    }
  </section>`;
}

function renderSettleSection(
  m: MoveOut,
  balance: MoveOutBalance,
  locale: Locale,
): string {
  const canSettle = m.state === "INSPECTED";
  return `<section class="panel">
    <h3>${t(locale, "moveOutSettleTitle")}</h3>
    <p class="muted">${t(locale, "moveOutRequiredInspection")}</p>
    <p>
      <span>${t(locale, "moveOutDepositHeld")}: <b>${formatMoney(balance.deposit_held)}</b></span>
      <span> · ${t(locale, "moveOutDamageTitle")}: <b>${formatMoney(balance.deductions_total)}</b></span>
    </p>
    ${
      canSettle
        ? `<form id="settle-form" class="form">
            <label>${t(locale, "moveOutDisposition")}
              <select name="disposition" required>
                ${DISPOSITIONS.map(
                  (d) =>
                    `<option value="${d}">${escapeHtml(
                      dispositionLabel(d, locale),
                    )}</option>`,
                ).join("")}
              </select>
            </label>
            <label>${t(locale, "moveOutDepositHeld")}<input name="deposit_held" required inputmode="decimal" placeholder="0.00" /></label>
            <label>${t(locale, "workRefund")}<input name="refund_amount" required inputmode="decimal" placeholder="0.00" /></label>
            <label>${t(locale, "workAdditionalOwed")}<input name="additional_owed" required inputmode="decimal" placeholder="0.00" /></label>
            <label>${t(locale, "workNotes")}<textarea name="notes" rows="2" maxlength="2000"></textarea></label>
            <div class="form-actions">
              <button class="primary-btn" type="submit">${t(locale, "workSettleMoveOut")}</button>
            </div>
            <p class="muted form-error" id="settle-error" hidden></p>
          </form>`
        : m.state === "SETTLED"
          ? `<p class="status status--ok">${t(locale, "workSettleMoveOut")} ✓</p>`
          : ""
    }
  </section>`;
}

function renderCloseSection(m: MoveOut, locale: Locale): string {
  if (m.state !== "SETTLED") return "";
  if (m.archived_at) {
    return `<section class="panel">
      <h3>${t(locale, "moveOutCloseTitle")}</h3>
      <p class="status status--ok">${t(locale, "moveOutCloseAction")} ✓ ${escapeHtml(m.archived_at)}</p>
    </section>`;
  }
  return `<section class="panel">
    <h3>${t(locale, "moveOutCloseTitle")}</h3>
    <p class="muted">${t(locale, "moveOutCloseNote")}</p>
    <div class="form-actions">
      <button class="primary-btn" data-action="close-moveout" type="button">${t(locale, "moveOutCloseAction")}</button>
    </div>
    <p class="muted form-error" id="close-error" hidden></p>
  </section>`;
}

function renderActivitySection(
  activity: MoveOutActivity[],
  locale: Locale,
): string {
  if (activity.length === 0) return "";
  return `<section class="panel">
    <h3>${t(locale, "moveOutActivity")}</h3>
    <ul class="list">${activity
      .map(
        (a) => `<li>
          <span class="muted">${escapeHtml(a.occurred_at)}</span>
          <b>${escapeHtml(a.kind)}</b>
          ${a.detail ? `<span class="muted">${escapeHtml(a.detail)}</span>` : ""}
        </li>`,
      )
      .join("")}</ul>
  </section>`;
}

// ---------- detail handlers ----------

function bindDetailHandlers(
  client: PasayClient,
  orgId: number,
  moveOutId: number,
  locale: Locale,
): void {
  const repaint = () =>
    renderMoveOutDetail(client, orgId, moveOutId, locale).catch(
      () => undefined,
    );
  // Inspection form
  document
    .querySelector<HTMLFormElement>("#inspection-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const summary = String(data.get("summary") || "").trim();
      if (!summary) {
        showFormError(
          document.querySelector("#inspection-error"),
          t(locale, "required"),
        );
        return;
      }
      try {
        await client.recordInspection(orgId, moveOutId, { summary });
        repaint();
      } catch (err) {
        showFormError(
          document.querySelector("#inspection-error"),
          formatError(err, locale),
        );
      }
    });
  // Damage add form
  document
    .querySelector<HTMLFormElement>("#damage-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const kind = String(data.get("kind") || "") as MoveOutDamage["kind"];
      const description = String(data.get("description") || "").trim();
      const amount = String(data.get("amount") || "").trim();
      if (!kind || !description || !amount) {
        showFormError(
          document.querySelector("#damage-error"),
          t(locale, "required"),
        );
        return;
      }
      try {
        await client.recordDamage(orgId, moveOutId, {
          kind,
          description,
          amount,
        });
        repaint();
      } catch (err) {
        showFormError(
          document.querySelector("#damage-error"),
          formatError(err, locale),
        );
      }
    });
  // Damage accept buttons
  document
    .querySelectorAll<HTMLButtonElement>("[data-action^='open-accept-damage-']")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        const damageId = Number(btn.dataset.damageId);
        const raw = window.prompt(
          `${t(locale, "moveOutDamageAccepted")}:`,
          "0.00",
        );
        if (raw === null) return;
        const accepted = raw.trim();
        if (!accepted) return;
        client
          .acceptDamage(orgId, damageId, { accepted_amount: accepted })
          .then(() => repaint())
          .catch((err) =>
            window.alert(formatError(err, locale)),
          );
      });
    });
  // Keys / arrears form
  document
    .querySelector<HTMLFormElement>("#keys-arrears-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const keysReturned = String(data.get("keys_returned")) === "true";
      const arrearsAmount = String(data.get("arrears_amount") || "0").trim() || "0";
      const notes = String(data.get("notes") || "").trim();
      try {
        await client.recordKeysArrears(orgId, moveOutId, {
          keys_returned: keysReturned,
          arrears_amount: arrearsAmount,
          notes: notes || null,
        });
        repaint();
      } catch (err) {
        showFormError(
          document.querySelector("#keys-arrears-error"),
          formatError(err, locale),
        );
      }
    });
  // Settle form
  document
    .querySelector<HTMLFormElement>("#settle-form")
    ?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formEl = event.target as HTMLFormElement;
      const data = new FormData(formEl);
      const disposition = String(data.get("disposition")) as DepositDisposition;
      const depositHeld = String(data.get("deposit_held") || "0").trim();
      const refundAmount = String(data.get("refund_amount") || "0").trim();
      const additionalOwed = String(
        data.get("additional_owed") || "0",
      ).trim();
      const notes = String(data.get("notes") || "").trim();
      try {
        await client.settleMoveOut(orgId, moveOutId, {
          disposition,
          deposit_held: depositHeld,
          refund_amount: refundAmount,
          additional_owed: additionalOwed,
          notes: notes || null,
        });
        repaint();
      } catch (err) {
        showFormError(
          document.querySelector("#settle-error"),
          formatError(err, locale),
        );
      }
    });
  // Atomic close
  document
    .querySelector<HTMLButtonElement>("[data-action='close-moveout']")
    ?.addEventListener("click", () => {
      client
        .closeMoveOut(orgId, moveOutId)
        .then(() => repaint())
        .catch((err) =>
          showFormError(
            document.querySelector("#close-error"),
            formatError(err, locale),
          ),
        );
    });
}

// ---------- helpers ----------

function dispositionLabel(d: DepositDisposition, locale: Locale): string {
  switch (d) {
    case "FULL_REFUND":
      return t(locale, "moveOutDispositionFull");
    case "PARTIAL_REFUND":
      return t(locale, "moveOutDispositionPartial");
    case "NO_REFUND":
      return t(locale, "moveOutDispositionNone");
    case "ADDITIONAL_OWED":
      return t(locale, "moveOutDispositionOwed");
  }
}

function renderDetailError(err: unknown, locale: Locale): string {
  return `<section class="panel error">
    <h2>${t(locale, "apiOffline")}</h2>
    <p class="muted">${escapeHtml(formatError(err, locale))}</p>
    <a class="primary-btn" href="#/work" type="button">${t(locale, "work")}</a>
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
