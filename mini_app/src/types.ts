/** Shared TypeScript types for the Mini App API client.
 *  Mirror the Pydantic schemas in app/v1/schemas/*.py
 */

export type ApiKey = string;

export type Money = string;

export type BootstrapResponse = {
  org_id: number;
  user_id: number;
  api_key: string;
  role: string;
};

/** Response from POST /api/v1/webapp/auth — Telegram initData exchange.
 *
 *  Mirror of app.v1.api.webapp_auth.WebappAuthResponse.  The Mini App
 *  treats `expires_at` as opaque (Unix-seconds; the SPA simply forgets
 *  the bearer when the in-memory store is cleared, no localStorage).
 */
export type WebappAuthResponse = {
  org_id: number;
  user_id: number;
  api_key: string;
  role: string;
  expires_at: number;
};

export type Organization = {
  id: number;
  name: string;
};

export type WorkspaceMember = {
  user_id: number;
  org_id: number;
  role: "OWNER" | "SECRETARY";
  state: "ACTIVE" | "REMOVED";
  username: string | null;
  display_name: string | null;
};

export type Workspace = {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
};

export type Membership = {
  id: number;
  org_id: number;
  user_id: number;
  role: "OWNER" | "SECRETARY";
  state: "ACTIVE" | "REMOVED";
};

export type SecretaryInvite = {
  id: number;
  org_id: number;
  invite_token: string;
  invitee_username: string | null;
  invitee_telegram_id: number | null;
  role: string;
  state: "PENDING" | "ACCEPTED" | "CANCELLED" | "EXPIRED";
  expires_at: string;
  accepted_at: string | null;
  accepted_by_user_id: number | null;
};

export type UnitLifecycleEvent = {
  id: number;
  unit_id: number;
  org_id: number;
  kind:
    | "STATUS_CHANGE"
    | "RENT_CHANGE"
    | "ARCHIVED"
    | "MAINTENANCE_START"
    | "MAINTENANCE_END";
  from_state: string | null;
  to_state: string | null;
  note: string | null;
  actor_user_id: number | null;
  created_at: string;
};

export type UnitDetail = {
  unit: Unit;
  lifecycle_events: UnitLifecycleEvent[];
};

export type DashboardHome = {
  open_operations: Array<{
    id: number;
    kind: string;
    subject_type: string;
    subject_id: number;
    state: string;
    due_at: string | null;
  }>;
  overdue_rent_count: number;
  overdue_rent: Array<{
    id: number;
    lease_id: number;
    due_date: string;
    monthly_rent: string;
    state: string;
  }>;
  open_repairs_count: number;
  open_repairs: Array<{
    id: number;
    title: string;
    state: string;
    severity: string;
    category: string;
  }>;
  pending_renewals_count: number;
  pending_renewals: Array<{
    id: number;
    lease_id: number;
    state: string;
    proposed_start_date: string | null;
  }>;
  open_move_outs_count: number;
  open_move_outs: Array<{
    id: number;
    lease_id: number;
    state: string;
  }>;
  pending_expense_claims_count: number;
  pending_expense_claims: Array<{
    id: number;
    title: string;
    category: string;
    status: string;
    claimed_amount: string;
  }>;
  open_tasks: Array<{
    id: number;
    operation_id: number;
    kind: string;
    state: string;
  }>;
  generated_at: string;
};

export type AuditEvent = {
  id: number;
  kind: string;
  subject_type: string;
  subject_id: number;
  org_id: number;
  created_at: string;
};

export type Property = {
  id: number;
  org_id: number;
  name: string;
  address_line1: string | null;
  city: string | null;
  region: string | null;
};

export type Unit = {
  id: number;
  property_id: number;
  label: string;
  status: "AVAILABLE" | "OCCUPIED" | "MAINTENANCE" | "RETIRED";
  bedrooms: number;
  bathrooms: number;
  monthly_rent: Money;
};

export type Tenant = {
  id: number;
  org_id: number;
  user_id: number | null;
  full_name: string;
  contact_phone: string | null;
  contact_email: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
};

export type Lease = {
  id: number;
  tenant_id: number;
  unit_id: number;
  state: "DRAFT" | "ACTIVE" | "TERMINATED";
  start_date: string;
  end_date: string;
  monthly_rent: Money;
  deposit_amount: Money;
};

export type RentDueSchedule = {
  id: number;
  org_id: number;
  lease_id: number;
  period_start: string;
  due_date: string;
  amount_due: Money;
  state: "DUE" | "OVERDUE" | "PAID";
  created_at: string;
  updated_at: string;
};

export type RentPayment = {
  id: number;
  org_id: number;
  due_schedule_id: number;
  claimed_amount: Money;
  verified_amount: Money | null;
  status: "PENDING" | "VERIFIED" | "FAILED" | "REVERSED";
  claimed_by_user_id: number | null;
  claimed_at: string;
  idempotency_key: string;
};

export type RentEvidence = {
  id: number;
  org_id: number;
  rent_payment_id: number;
  kind: string;
  reference: string;
  uploaded_by_user_id: number | null;
  created_at: string;
};

export type RentVerification = {
  id: number;
  org_id: number;
  rent_payment_id: number;
  decision: "VERIFIED" | "REJECTED" | "REVERSED";
  verified_amount: Money | null;
  verifier_user_id: number | null;
  decided_at: string;
  reason: string | null;
};

export type RentActivity = {
  id: number;
  org_id: number;
  due_schedule_id: number | null;
  rent_payment_id: number | null;
  kind: string;
  detail: string | null;
  actor_user_id: number | null;
  occurred_at: string;
};

export type RentBalance = {
  due_schedule_id: number;
  amount_due: Money;
  verified_total: Money;
  remaining_balance: Money;
  is_paid: boolean;
};

export type ExpenseClaim = {
  id: number;
  org_id: number;
  property_id: number | null;
  unit_id: number | null;
  title: string;
  description: string | null;
  category: string;
  claimed_amount: Money;
  verified_amount: Money | null;
  status: "OPEN" | "SUBMITTED" | "VERIFIED" | "FAILED" | "CANCELLED";
  submitted_at: string;
};

export type Repair = {
  id: number;
  org_id: number;
  unit_id: number | null;
  title: string;
  description: string;
  state:
    | "REPORTED"
    | "CONFIRMED"
    | "AWAITING_TECHNICIAN"
    | "QUOTE_REQUESTED"
    | "QUOTE_RECEIVED"
    | "QUOTE_APPROVED"
    | "IN_PROGRESS"
    | "COMPLETION_CLAIMED"
    | "COMPLETED"
    | "CANCELLED";
  category: string;
  severity: string;
  reported_by_user_id: number | null;
  reported_at: string;
  technician_name: string | null;
  technician_source: string | null;
  technician_eta_at: string | null;
  quoted_amount: Money | null;
  idempotency_key: string;
  linked_expense_payment_id: number | null;
  completed_at: string | null;
};

export type RepairQuote = {
  id: number;
  org_id: number;
  report_id: number;
  amount: Money;
  description: string;
  decision: "SUBMITTED" | "APPROVED" | "REJECTED";
  technician_name: string;
  submitted_by_user_id: number | null;
  decided_by_user_id: number | null;
  decided_at: string | null;
  reason: string | null;
};

export type RepairWork = {
  id: number;
  org_id: number;
  report_id: number;
  state: string;
  note: string;
  actor_user_id: number | null;
  occurred_at: string;
};

export type RepairCompletionClaim = {
  id: number;
  org_id: number;
  report_id: number;
  summary: string;
  claimed_by_user_id: number | null;
  claimed_at: string;
};

export type RepairVerification = {
  id: number;
  org_id: number;
  report_id: number;
  decision: "VERIFIED" | "REJECTED" | "REVERSED";
  verifier_user_id: number | null;
  decided_at: string;
  reason: string | null;
};

export type RepairActivity = {
  id: number;
  org_id: number;
  report_id: number | null;
  quote_id: number | null;
  work_id: number | null;
  claim_id: number | null;
  kind: string;
  detail: string | null;
  actor_user_id: number | null;
  occurred_at: string;
};

export type RenewalProposal = {
  id: number;
  lease_id: number;
  state: "PROPOSED" | "APPROVED" | "EXECUTED" | "REJECTED" | "CANCELLED";
  proposed_start_date: string;
  proposed_end_date: string;
  proposed_monthly_rent: Money;
  notes: string | null;
};

export type MoveOut = {
  id: number;
  org_id: number;
  lease_id: number;
  state: "REQUESTED" | "INSPECTED" | "SETTLED" | "CANCELLED";
  requested_at: string;
  requested_by_user_id: number | null;
  planned_move_out_date: string | null;
  inspected_at: string | null;
  inspected_by_user_id: number | null;
  inspection_notes: string | null;
  settled_at: string | null;
  settlement_id: number | null;
  cancelled_at: string | null;
  cancel_reason: string | null;
  keys_returned: boolean | null;
  arrears_amount: Money | null;
  keys_arrears_notes: string | null;
  archived_at: string | null;
  idempotency_key: string;
};

export type MoveOutInspection = {
  id: number;
  org_id: number;
  move_out_id: number;
  inspected_at: string;
  inspected_by_user_id: number | null;
  summary: string;
};

export type MoveOutDamage = {
  id: number;
  org_id: number;
  move_out_id: number;
  kind: "CLEANING" | "REPAIR" | "REPLACEMENT" | "UTILITIES" | "OTHER";
  description: string;
  amount: Money;
  accepted_amount: Money;
  recorded_by_user_id: number | null;
};

export type DepositDisposition =
  | "FULL_REFUND"
  | "PARTIAL_REFUND"
  | "NO_REFUND"
  | "ADDITIONAL_OWED";

export type DepositSettlement = {
  id: number;
  org_id: number;
  move_out_id: number;
  disposition: DepositDisposition;
  deposit_held: Money;
  deductions_total: Money;
  refund_amount: Money;
  additional_owed: Money;
  notes: string | null;
  settled_by_user_id: number | null;
  settled_at: string;
};

export type MoveOutBalance = {
  move_out_id: number;
  deposit_held: Money;
  deductions_total: Money;
  refund_amount: Money;
  additional_owed: Money;
  is_settled: boolean;
};

export type MoveOutActivity = {
  id: number;
  org_id: number;
  move_out_id: number;
  kind: string;
  actor_user_id: number | null;
  detail: string | null;
  occurred_at: string;
};

export type Operation = {
  id: number;
  org_id: number;
  subject_type: string;
  subject_id: number;
  kind: string;
  state: "open" | "in_progress" | "resolved" | "cancelled";
  due_at: string | null;
  resolved_at: string | null;
};

export type Task = {
  id: number;
  operation_id: number;
  state: "open" | "done" | "cancelled";
  title: string;
  due_at: string | null;
  done_at: string | null;
};
