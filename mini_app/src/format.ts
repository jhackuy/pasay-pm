/** Format helpers — money, dates, status labels.
 *  Money is a Decimal-as-string everywhere (no float, per AGENTS.md §4).
 *  Dates are ISO 8601 UTC; we format in zh-CN locale by default.
 */

export function formatMoney(raw: string | number | null | undefined, currency = "PHP"): string {
  if (raw === null || raw === undefined) return "—";
  const str = typeof raw === "number" ? raw.toFixed(2) : String(raw);
  const [intPart, fracPart] = str.split(".");
  const intDigits = (intPart ?? "0").replace(/[^0-9-]/g, "");
  const negative = intDigits.startsWith("-");
  const body = negative ? intDigits.slice(1) : intDigits;
  const padded = body.padStart(1, "0");
  const grouped = padded.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const signed = negative ? `-${grouped}` : grouped;
  const fraction = (fracPart ?? "00").padEnd(2, "0").slice(0, 2);
  return `${currency} ${signed}.${fraction}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const y = date.getUTCFullYear();
    const m = String(date.getUTCMonth() + 1).padStart(2, "0");
    const d = String(date.getUTCDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  } catch {
    return iso;
  }
}

export function relativeFromNow(iso: string | null | undefined, locale = "zh-CN"): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diffMs = then - Date.now();
  const absMin = Math.round(Math.abs(diffMs) / 60000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (absMin < 60) return rtf.format(Math.round(diffMs / 60000), "minute");
  if (absMin < 60 * 24) return rtf.format(Math.round(diffMs / 3600000), "hour");
  return rtf.format(Math.round(diffMs / 86400000), "day");
}

const STATUS_LABELS: Record<string, { zh: string; en: string }> = {
  DRAFT: { zh: "草稿", en: "Draft" },
  ACTIVE: { zh: "生效中", en: "Active" },
  TERMINATED: { zh: "已终止", en: "Terminated" },
  AVAILABLE: { zh: "可租", en: "Available" },
  OCCUPIED: { zh: "已租", en: "Occupied" },
  MAINTENANCE: { zh: "维护中", en: "Maintenance" },
  RETIRED: { zh: "停用", en: "Retired" },
  DUE: { zh: "待收", en: "Due" },
  OVERDUE: { zh: "逾期", en: "Overdue" },
  PAID: { zh: "已结清", en: "Paid" },
  PENDING: { zh: "待核验", en: "Pending" },
  VERIFIED: { zh: "已核验", en: "Verified" },
  FAILED: { zh: "未通过", en: "Failed" },
  REVERSED: { zh: "已撤回", en: "Reversed" },
  OPEN: { zh: "已登记", en: "Open" },
  SUBMITTED: { zh: "已提交", en: "Submitted" },
  SETTLED: { zh: "已结算", en: "Settled" },
  CANCELLED: { zh: "已取消", en: "Cancelled" },
  REPORTED: { zh: "已报修", en: "Reported" },
  CONFIRMED: { zh: "已确认", en: "Confirmed" },
  AWAITING_TECHNICIAN: { zh: "等待技师", en: "Awaiting technician" },
  QUOTE_REQUESTED: { zh: "报价待询", en: "Quote requested" },
  QUOTE_RECEIVED: { zh: "已收到报价", en: "Quote received" },
  QUOTE_APPROVED: { zh: "报价已批准", en: "Quote approved" },
  IN_PROGRESS: { zh: "维修进行中", en: "In progress" },
  COMPLETION_CLAIMED: { zh: "完工申报", en: "Completion claimed" },
  COMPLETED: { zh: "已完成", en: "Completed" },
  PROPOSED: { zh: "待批准", en: "Proposed" },
  APPROVED: { zh: "已批准", en: "Approved" },
  EXECUTED: { zh: "已执行", en: "Executed" },
  REJECTED: { zh: "已驳回", en: "Rejected" },
  REQUESTED: { zh: "已申请", en: "Requested" },
  INSPECTED: { zh: "已验收", en: "Inspected" },
};

export function statusLabel(status: string, locale: "zh" | "en" = "zh"): string {
  const entry = STATUS_LABELS[status];
  if (!entry) return status;
  return locale === "zh" ? entry.zh : entry.en;
}

export function statusToneClass(status: string): string {
  if (["PAID", "VERIFIED", "ACTIVE", "APPROVED", "COMPLETED", "SETTLED", "EXECUTED", "CONFIRMED", "INSPECTED"].includes(status)) {
    return "status status--ok";
  }
  if (["OVERDUE", "FAILED", "REJECTED", "CANCELLED", "TERMINATED", "REVERSED"].includes(status)) {
    return "status status--bad";
  }
  if (["PENDING", "OPEN", "DUE", "QUOTE_REQUESTED", "REQUESTED", "AWAITING_TECHNICIAN", "REPORTED", "PROPOSED", "DRAFT"].includes(status)) {
    return "status status--pending";
  }
  if (["SUBMITTED", "IN_PROGRESS", "QUOTE_RECEIVED", "QUOTE_APPROVED", "COMPLETION_CLAIMED"].includes(status)) {
    return "status status--info";
  }
  return "status status--neutral";
}

let counter = 0;
export function makeIdempotencyKey(prefix = "mini"): string {
  counter = (counter + 1) % 1_000_000;
  const rand = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${counter.toString(36)}-${rand}`;
}
