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
  full_name: string;
  phone: string | null;
  email: string | null;
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
  lease_id: number;
  due_date: string;
  amount_due: Money;
  status: "DUE" | "OVERDUE" | "PAID";
};

export type RentPayment = {
  id: number;
  due_schedule_id: number;
  amount: Money;
  status: "PENDING" | "VERIFIED" | "FAILED" | "REVERSED";
  note: string | null;
  submitted_at: string;
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
  reported_at: string;
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
  lease_id: number;
  state: "REQUESTED" | "INSPECTED" | "SETTLED" | "CANCELLED";
  deposit_held: Money | null;
  refund_amount: Money | null;
  additional_owed: Money | null;
  outcome: string | null;
  requested_at: string;
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
