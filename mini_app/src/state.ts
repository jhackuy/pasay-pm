/** In-memory session state only. NEVER persist business truth in
 *  localStorage (AGENTS.md §4). Session token survives a page refresh by
 *  being re-derived from the URL bootstrap callback or by being re-entered
 *  by the operator.
 */

import type { PasayClient } from "./api";

type Listener = () => void;

export class SessionStore {
  apiKey: string | null = null;
  orgId: number | null = null;
  userId: number | null = null;
  role: string | null = null;
  listeners = new Set<Listener>();
  client: PasayClient;

  constructor(client: PasayClient) {
    this.client = client;
  }

  bootstrap(apiKey: string, orgId: number, userId: number, role: string): void {
    this.apiKey = apiKey;
    this.orgId = orgId;
    this.userId = userId;
    this.role = role;
    this.client.apiKey = apiKey;
    this.client.orgId = orgId;
    this.emit();
  }

  setOrg(orgId: number): void {
    this.orgId = orgId;
    this.client.orgId = orgId;
    this.emit();
  }

  signOut(): void {
    this.apiKey = null;
    this.orgId = null;
    this.userId = null;
    this.role = null;
    this.client.apiKey = null;
    this.client.orgId = null;
    this.emit();
  }

  isAuthenticated(): boolean {
    return this.apiKey !== null && this.orgId !== null;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    this.listeners.forEach((fn) => fn());
  }
}
