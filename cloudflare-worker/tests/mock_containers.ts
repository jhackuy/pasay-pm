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
 * Test callers drive behaviour via the exported mutable globals:
 *   - lastGetContainerArgs: capture (doNamespace, instanceId) tuples
 *   - containerInstances[instanceId] = fakeContainer whose fetch() tests can assert on
 */

export abstract class Container {
  /** Default HTTP port the container image listens on. */
  defaultPort: number = 8000;
  /** Instance sleep-after-idle timeout (Cloudflare runtime only; no-op in tests). */
  sleepAfter?: string;
  /**
   * Optional environment-variable provisioning table (Cloudflare Containers
   * runtime feature).  Worker code declares envVars on the Container
   * subclass; the mock base class accepts any record so tests can assert
   * on the declared map without needing a live Cloudflare build.
   */
  envVars?: Record<string, (env: any) => string>;
}

export interface MockContainerHandle {
  fetch_calls: Array<{ url: string; method: string; headers: Record<string, string>; body: any }>;
  fetch_response_status: number;
  fetch_response_body?: any;
  fetch_throw?: any;
  fetch: (req: Request) => Promise<Response>;
}

export const containerInstances = new Map<string, MockContainerHandle>();
export const lastGetContainerArgs: Array<[any, string]> = [];

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
 */
export function getContainer(doNamespace: any, instanceId: string): MockContainerHandle {
  lastGetContainerArgs.push([doNamespace, instanceId]);
  const existing = containerInstances.get(instanceId);
  if (existing) return existing;
  const fresh = makeMockContainerHandle(200);
  containerInstances.set(instanceId, fresh);
  return fresh;
}
