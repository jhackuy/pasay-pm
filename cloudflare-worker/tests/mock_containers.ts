/**
 * Local mock of @cloudflare/containers for unit testing under tsx/node.
 *
 * ND_RETURN FIX2 #3 REQUIRES that tests actually import + execute the real
 * Worker handlers (worker.fetch/queue/scheduled) — we CANNOT ship a parallel
 * copy of the logic.  The build/test environment has no live Cloudflare
 * runtime, so we point tsconfig "paths" at this module so the production
 * import statement ``import { Container, getContainer } from "@cloudflare/containers"``
 * resolves to this test mock.
 *
 * FIX11: This module MUST mirror the REAL runtime-shape signatures exported
 * by @cloudflare/containers@0.3.7 as closely as possible.
 *   - Container.envVars    -> Record<string, string> (REAL — no function vals)
 *   - Container.sleepAfter -> string | number         (REAL — non-optional)
 *   - getContainer(binding, name?)  -> second arg is OPTIONAL (REAL)
 *
 * TYPE BRAND NOTE:
 *   The Cloudflare workers-types package uses a PACKAGE-LOCAL unique symbol
 *   for `Rpc.DurableObjectBranded`.  This mock cannot replicate the EXACT
 *   same unique symbol (by definition of "unique symbol"), so for the TEST
 *   tsconfig build we widen the generic signatures with `any` to keep the
 *   structural contract test-friendly while the REAL build (tsconfig.json,
 *   no mock) exercises the exact branded generics.
 *
 * Test callers drive behaviour via the exported mutable globals:
 *   - lastGetContainerArgs: capture (doNamespace, instanceId) tuples
 *   - containerInstances[instanceId] = fakeContainer whose fetch() tests can assert on
 */

export interface ContainerOptions {
  [key: string]: unknown;
}

export abstract class Container {
  /** Default HTTP port the container image listens on. */
  defaultPort?: number = 8000;

  /** Instance sleep-after-idle timeout — REAL @cloudflare/containers 0.3.7: `string | number`. */
  sleepAfter: string | number = "15m";

  /**
   * Environment-variable provisioning table (Cloudflare Containers runtime).
   *
   * REAL @cloudflare/containers@0.3.7 signature: Record<string, string>.
   * Pasay closeout RETURN-1 §2: explicit 6-key envVars (5 dynamic mapped
   * from env + 1 static PASAY_RUNTIME_MODE).
   */
  envVars: Record<string, string> = {};

  constructor(public ctx: unknown = {}, public env: unknown = {}, public options?: ContainerOptions) {}
}

export interface MockContainerHandle {
  fetch_calls: Array<{ url: string; method: string; headers: Record<string, string>; body: any }>;
  fetch_response_status: number;
  fetch_response_body?: any;
  fetch_throw?: any;
  fetch: (req: Request) => Promise<Response>;
}

export const containerInstances = new Map<string, MockContainerHandle>();
export const lastGetContainerArgs: Array<[any, string?]> = [];

export function makeMockContainerHandle(initialStatus = 200): MockContainerHandle {
  const h: MockContainerHandle = {
    fetch_calls: [],
    fetch_response_status: initialStatus,
    fetch_response_body: { ok: true },
    fetch: async (req: Request) => {
      if ((h as any).fetch_throw !== undefined) {
        throw (h as any).fetch_throw;
      }
      let body: any = null;
      try {
        body = await req.clone().json();
      } catch {
        /* ignore */
      }
      const headers: Record<string, string> = {};
      req.headers.forEach((v, k) => {
        headers[k] = v;
      });
      h.fetch_calls.push({
        url: req.url,
        method: req.method,
        headers,
        body,
      });
      return new Response(JSON.stringify(h.fetch_response_body ?? { ok: true }), {
        status: h.fetch_response_status,
        headers: { "Content-Type": "application/json" },
      });
    },
  };
  return h;
}

/**
 * OFFICIAL-matching getContainer signature — tests inspect lastGetContainerArgs
 * to prove the real Worker code calls the real @cloudflare/containers entrypoint
 * (albeit pointed here by the tsconfig paths alias) with the correct Durable
 * Object namespace binding + singleton instance id.
 *
 * REAL @cloudflare/containers@0.3.7:
 *   getContainer<T extends Container>(binding, name?) -> DurableObjectStub<T>
 *
 * The local mock accepts any DurableObjectNamespace binding via `any` widening
 * because tests compile with the mock paths alias — unique-symbol brand checks
 * are exercised by the src build (tsconfig.json, real package types, no mock).
 *
 * @param binding - Container's Durable Object namespace binding
 * @param name    - Optional instance name; defaults to "cf-singleton-container".
 */
export function getContainer(
  binding: any,
  name?: string,
): MockContainerHandle {
  lastGetContainerArgs.push([binding, name]);
  const key = name ?? "cf-singleton-container";
  const existing = containerInstances.get(key);
  if (existing) return existing;
  const fresh = makeMockContainerHandle(200);
  containerInstances.set(key, fresh);
  return fresh;
}
