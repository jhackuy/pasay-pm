/** Typed API client for the PASAY V1 rewrite.
 *
 *  Consumes the FastAPI app at `/api/v1`. Every method goes through
 *  `request()` so that auth, error normalization, and idempotency-key
 *  handling live in exactly one place. No business truth is ever cached
 *  client-side; this client is a thin transport.
 */
import type {
  ApiKey,
  AuditEvent,
  BootstrapResponse,
  DashboardHome,
  ExpenseClaim,
  Lease,
  Membership,
  MoveOut,
  Operation,
  Organization,
  Property,
  RenewalProposal,
  RentDueSchedule,
  RentPayment,
  Repair,
  SecretaryInvite,
  Task,
  Tenant,
  Unit,
  UnitDetail,
  Workspace,
  WorkspaceMember,
} from "./types";

const API_ROOT = (import.meta.env.VITE_API_ROOT ?? "/api/v1").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

type RequestInit = {
  method?: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  orgId?: number;
  idempotencyKey?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
};

function buildQuery(query: RequestInit["query"]): string {
  if (!query) return "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length === 0 ? "" : `?${parts.join("&")}`;
}

export class PasayClient {
  apiKey: ApiKey | null = null;
  orgId: number | null = null;
  baseUrl = API_ROOT;

  constructor(apiKey: ApiKey | null = null, orgId: number | null = null) {
    this.apiKey = apiKey;
    this.orgId = orgId;
  }

  isAuthenticated(): boolean {
    return this.apiKey !== null && this.orgId !== null;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const url = `${this.baseUrl}${path}${buildQuery({
      org_id: init.orgId ?? this.orgId ?? undefined,
      ...(init.query ?? {}),
    })}`;
    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (init.body !== undefined) headers["Content-Type"] = "application/json";
    if (init.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
    if (this.apiKey) headers["Authorization"] = `Bearer ${this.apiKey}`;
    const response = await fetch(url, {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? null : JSON.stringify(init.body),
    });
    const text = await response.text();
    let payload: unknown = null;
    if (text.length > 0) {
      try { payload = JSON.parse(text); } catch { payload = text; }
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "detail" in (payload as object)
          ? (payload as { detail: unknown }).detail
          : payload;
      throw new ApiError(response.status, detail, `API ${response.status} ${path}`);
    }
    return payload as T;
  }

  bootstrap(body: {
    workspace_name: string;
    owner_username?: string | null;
    owner_display_name?: string | null;
  }): Promise<BootstrapResponse> {
    return this.request<BootstrapResponse>("/bootstrap", { method: "POST", body });
  }

  // Workspaces + members
  listWorkspaces(): Promise<{ organizations: Organization[]; memberships: WorkspaceMember[] }> {
    return this.request("/workspaces");
  }
  listMembers(orgId: number): Promise<WorkspaceMember[]> {
    return this.request(`/workspaces/members?org_id=${orgId}`);
  }

  // Properties + Units
  listProperties(orgId: number): Promise<Property[]> {
    return this.request("/properties", { orgId });
  }
  createProperty(
    orgId: number,
    body: { name: string; address_line1?: string; city?: string; region?: string; postal_code?: string },
    idempotencyKey: string,
  ): Promise<Property> {
    return this.request("/properties", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  listUnits(orgId: number, propertyId: number): Promise<Unit[]> {
    return this.request(`/properties/${propertyId}/units`, { orgId });
  }
  createUnit(
    orgId: number,
    propertyId: number,
    body: { label: string; bedrooms?: number; bathrooms?: number; monthly_rent?: string },
    idempotencyKey: string,
  ): Promise<Unit> {
    return this.request(`/properties/${propertyId}/units`, {
      method: "POST", orgId, idempotencyKey, body,
    });
  }

  // Tenants
  listTenants(orgId: number): Promise<Tenant[]> {
    return this.request<Tenant[]>("/tenants", { orgId });
  }
  createTenant(
    orgId: number,
    body: { full_name: string; contact_phone?: string | null; contact_email?: string | null },
    idempotencyKey: string,
  ): Promise<Tenant> {
    return this.request<Tenant>("/tenants", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  softDeleteTenant(orgId: number, tenantId: number): Promise<Tenant> {
    return this.request<Tenant>(`/tenants/${tenantId}`, {
      method: "DELETE", orgId,
    });
  }

  // Dashboard / audit
  getDashboardHome(orgId: number): Promise<DashboardHome> {
    return this.request("/dashboard/home", { orgId });
  }
  listAuditEvents(
    orgId: number,
    options: { limit?: number } = {},
  ): Promise<AuditEvent[]> {
    return this.request("/audit", { orgId, ...options });
  }

  // Workspaces
  createWorkspaceInvite(
    orgId: number,
    body: { invitee_username?: string | null },
  ): Promise<SecretaryInvite> {
    return this.request(`/workspaces/${orgId}/invites`, {
      orgId,
      method: "POST",
      body,
    });
  }
  cancelWorkspaceInvite(
    orgId: number,
    inviteId: number,
  ): Promise<SecretaryInvite> {
    return this.request(`/workspaces/${orgId}/invites/${inviteId}/cancel`, {
      orgId,
      method: "POST",
    });
  }
  removeWorkspaceMember(
    orgId: number,
    memberId: number,
  ): Promise<Membership> {
    return this.request(`/workspaces/${orgId}/members/${memberId}`, {
      orgId,
      method: "DELETE",
    });
  }

  // Properties
  getProperty(propertyId: number, orgId: number): Promise<Property> {
    return this.request(`/properties/${propertyId}`, { orgId });
  }
  archiveProperty(propertyId: number, orgId: number): Promise<Property> {
    return this.request(`/properties/${propertyId}/archive`, {
      orgId,
      method: "POST",
    });
  }
  getUnitDetail(unitId: number, orgId: number): Promise<UnitDetail> {
    return this.request(`/properties/units/${unitId}`, { orgId });
  }
  setUnitStatus(
    unitId: number,
    orgId: number,
    body: { status: string; note?: string | null },
  ): Promise<Unit> {
    return this.request(`/properties/units/${unitId}/status`, {
      orgId,
      method: "PATCH",
      body,
    });
  }

  // Leases
  listLeases(orgId: number): Promise<Lease[]> {
    return this.request("/leases", { orgId });
  }
  createLease(
    orgId: number,
    body: {
      tenant_id: number;
      unit_id: number;
      start_date: string;
      end_date: string;
      monthly_rent: string;
      deposit_amount: string;
    },
    idempotencyKey: string,
  ): Promise<Lease> {
    return this.request("/leases", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  activateLease(orgId: number, leaseId: number): Promise<Lease> {
    return this.request(`/leases/${leaseId}/activate`, { method: "POST", orgId });
  }
  terminateLease(orgId: number, leaseId: number): Promise<Lease> {
    return this.request(`/leases/${leaseId}/terminate`, { method: "POST", orgId });
  }

  // Rent — due schedules, payments, claims, verifications
  listDueSchedules(orgId: number): Promise<RentDueSchedule[]> {
    return this.request("/rent/due-schedules", { orgId });
  }
  listOverdue(orgId: number): Promise<RentDueSchedule[]> {
    return this.request("/rent/overdue", { orgId });
  }
  createDueSchedule(
    orgId: number,
    body: { lease_id: number; due_date: string; amount_due: string },
    idempotencyKey: string,
  ): Promise<RentDueSchedule> {
    return this.request("/rent/due-schedules", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  listClaims(orgId: number): Promise<RentPayment[]> {
    return this.request("/rent/claims", { orgId });
  }
  claimPayment(
    orgId: number,
    dueScheduleId: number,
    body: { amount: string; note?: string | null },
    idempotencyKey: string,
  ): Promise<RentPayment> {
    return this.request(
      `/rent/due-schedules/${dueScheduleId}/claim`,
      { method: "POST", orgId, idempotencyKey, body },
    );
  }
  verifyPayment(orgId: number, paymentId: number): Promise<RentPayment> {
    return this.request(`/rent/claims/${paymentId}/verify`, { method: "POST", orgId });
  }
  remainingBalance(orgId: number, leaseId: number): Promise<{ lease_id: number; amount_due: string; verified_total: string; remaining_balance: string }> {
    return this.request(`/rent/leases/${leaseId}/remaining-balance`, { orgId });
  }
  listOperations(orgId: number, scope: string): Promise<Operation[]> {
    return this.request<Operation[]>(`/${scope}/operations`, { orgId }).catch(
      (): Operation[] => [],
    );
  }

  // Expenses
  listExpenseClaims(orgId: number): Promise<ExpenseClaim[]> {
    return this.request("/expenses/claims", { orgId });
  }
  openExpenseClaim(
    orgId: number,
    body: {
      title: string;
      category: string;
      claimed_amount: string;
      property_id?: number | null;
      unit_id?: number | null;
      description?: string | null;
    },
    idempotencyKey: string,
  ): Promise<ExpenseClaim> {
    return this.request("/expenses/claims", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  verifyExpense(orgId: number, claimId: number): Promise<ExpenseClaim> {
    return this.request(`/expenses/claims/${claimId}/verify`, { method: "POST", orgId });
  }
  rejectExpense(orgId: number, claimId: number, reason: string): Promise<ExpenseClaim> {
    return this.request(`/expenses/claims/${claimId}/reject`, {
      method: "POST", orgId, body: { reason },
    });
  }
  reverseExpense(orgId: number, claimId: number, reason: string): Promise<ExpenseClaim> {
    return this.request(`/expenses/claims/${claimId}/reverse`, {
      method: "POST", orgId, body: { reason },
    });
  }
  addReceipt(
    orgId: number,
    claimId: number,
    body: { kind: string; reference: string },
    idempotencyKey: string,
  ): Promise<{ id: number; kind: string; reference: string }> {
    return this.request(`/expenses/claims/${claimId}/receipts`, {
      method: "POST", orgId, idempotencyKey, body,
    });
  }

  // Repairs
  listRepairs(orgId: number): Promise<Repair[]> {
    return this.request("/repairs", { orgId });
  }
  openRepair(
    orgId: number,
    body: {
      title: string;
      description: string;
      unit_id?: number | null;
      severity?: string;
    },
    idempotencyKey: string,
  ): Promise<Repair> {
    return this.request("/repairs", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  confirmRepair(orgId: number, repairId: number): Promise<Repair> {
    return this.request(`/repairs/${repairId}/confirm`, { method: "POST", orgId });
  }
  submitQuote(
    orgId: number,
    repairId: number,
    body: { amount: string; note?: string | null },
    idempotencyKey: string,
  ): Promise<Repair> {
    return this.request(`/repairs/${repairId}/quote`, {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  approveQuote(orgId: number, repairId: number): Promise<Repair> {
    return this.request(`/repairs/${repairId}/quote/approve`, {
      method: "POST", orgId, body: { decision: "APPROVED" },
    });
  }
  rejectQuote(orgId: number, repairId: number, reason: string): Promise<Repair> {
    return this.request(`/repairs/${repairId}/quote/reject`, {
      method: "POST", orgId, body: { reason },
    });
  }
  startWork(orgId: number, repairId: number): Promise<Repair> {
    return this.request(`/repairs/${repairId}/start-work`, { method: "POST", orgId });
  }
  claimCompletion(orgId: number, repairId: number, note: string): Promise<Repair> {
    return this.request(`/repairs/${repairId}/completion-claim`, {
      method: "POST", orgId, body: { note },
    });
  }
  verifyCompletion(orgId: number, repairId: number): Promise<Repair> {
    return this.request(`/repairs/${repairId}/completion-verify`, {
      method: "POST", orgId,
    });
  }
  rejectCompletion(orgId: number, repairId: number, reason: string): Promise<Repair> {
    return this.request(`/repairs/${repairId}/completion-reject`, {
      method: "POST", orgId, body: { reason },
    });
  }

  // Renewals
  listRenewals(orgId: number): Promise<RenewalProposal[]> {
    return this.request("/renewals", { orgId });
  }
  proposeRenewal(
    orgId: number,
    body: {
      lease_id: number;
      proposed_start_date: string;
      proposed_end_date: string;
      proposed_monthly_rent: string;
      notes?: string | null;
    },
    idempotencyKey: string,
  ): Promise<RenewalProposal> {
    return this.request("/renewals", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  approveRenewal(orgId: number, renewalId: number): Promise<RenewalProposal> {
    return this.request(`/renewals/${renewalId}/approve`, { method: "POST", orgId });
  }
  rejectRenewal(orgId: number, renewalId: number, reason: string): Promise<RenewalProposal> {
    return this.request(`/renewals/${renewalId}/reject`, {
      method: "POST", orgId, body: { reason },
    });
  }
  executeRenewal(orgId: number, renewalId: number): Promise<RenewalProposal> {
    return this.request(`/renewals/${renewalId}/execute`, { method: "POST", orgId });
  }

  // Move-out
  listMoveOuts(orgId: number): Promise<MoveOut[]> {
    return this.request("/move-outs", { orgId });
  }
  requestMoveOut(
    orgId: number,
    body: { lease_id: number; requested_at?: string; reason?: string | null },
    idempotencyKey: string,
  ): Promise<MoveOut> {
    return this.request("/move-outs", {
      method: "POST", orgId, idempotencyKey, body,
    });
  }
  inspectMoveOut(
    orgId: number,
    moveOutId: number,
    body: { inspector_user_id?: number; notes: string },
  ): Promise<MoveOut> {
    return this.request(`/move-outs/${moveOutId}/inspection`, {
      method: "POST", orgId, body,
    });
  }
  settleMoveOut(
    orgId: number,
    moveOutId: number,
    body: {
      outcome: string;
      deposit_held: string;
      refund_amount: string;
      additional_owed: string;
      notes?: string | null;
    },
  ): Promise<MoveOut> {
    return this.request(`/move-outs/${moveOutId}/settlement`, {
      method: "POST", orgId, body,
    });
  }

  // Tasks (consolidated across rent/expense/repair/renewal/move-out)
  listTasks(orgId: number, scope: string, parentId: number): Promise<Task[]> {
    return this.request<Task[]>(`/${scope}/${parentId}/follow-ups`, { orgId }).catch(
      (): Task[] => [],
    );
  }
  completeTask(orgId: number, scope: string, taskId: number): Promise<Task> {
    return this.request(`/${scope}/follow-ups/${taskId}/complete`, { method: "POST", orgId });
  }
  health(): Promise<{ status: string; version: string }> {
    return this.request("/health", { orgId: undefined });
  }
}

export const client = new PasayClient();
