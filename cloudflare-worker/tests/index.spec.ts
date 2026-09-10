/**
 * PASAY-TASK-011 FIX2 #3 — Worker targeted validation using REAL worker handlers.
 *
 * ND_RETURN FIX2 REQUIRES:
 *   - NO inline/mirror copy of handler logic in tests
 *   - directly import + EXECUTE actual { fetch, queue, scheduled } default export
 *   - 6 contract scenarios proven end-to-end through real code paths
 *
 * Official helpers mocked ONLY at the @cloudflare/containers boundary (via
 * tsconfig "paths" @cloudflare/containers -> ./mock_containers.ts) and via
 * fake Env objects injected into the handlers (Queue / DO namespace / secrets).
 *
 * Run with:  npx tsx tests/index.spec.ts
 * Typecheck: npx tsc --noEmit
 * Dry-run:   npx wrangler deploy --dry-run
 */

import * as fs from "node:fs";
import * as path from "node:path";

// ---------------------------------------------------------------------------
// 1. Source-level contracts (proven statically before runtime handlers fire)
// ---------------------------------------------------------------------------

const WORKER_SRC = fs.readFileSync(
  path.join(__dirname, "..", "src", "index.ts"),
  "utf-8",
);
const WRANGLER_TOML = fs.readFileSync(
  path.join(__dirname, "..", "wrangler.toml"),
  "utf-8",
);

// ---------------------------------------------------------------------------
// 2. Fake env + queue primitives used to DRIVE the real Worker handlers.
//    The REAL index.ts default export methods are what we call.
// ---------------------------------------------------------------------------

import worker, { PasayContainer, mask_sensitive } from "../src/index";
import {
  ENVELOPE_VERSION,
  make_scheduled_event_id,
  make_telegram_event_id,
  type PasayQueueEnvelope,
} from "../src/envelope";
import {
  containerInstances,
  lastGetContainerArgs,
  makeMockContainerHandle,
  type MockContainerHandle,
} from "./mock_containers";

type AnyFn = (...a: any[]) => any;

interface FakeQueue {
  send_calls: Array<any>;
  send: (body: any) => Promise<void>;
  // Minimal Queue interface stubs for tsc strict type match against @cloudflare/workers-types
  metrics: any;
  sendBatch: (messages: Iterable<any>) => Promise<void>;
  close?: () => Promise<void>;
}
interface FakeMsg {
  body: any;
  ack_calls: number;
  retry_calls: number;
  ack: () => void;
  retry: (options?: any) => void;
  timestamp?: Date;
  attempts?: number;
}

function makeFakeQueue(): FakeQueue {
  const q: FakeQueue = {
    send_calls: [],
    metrics: { dropped: 0, enqueued: 0 },
    send: async (body: any) => {
      q.send_calls.push(body);
    },
    sendBatch: async () => { /* no-op for tests */ },
    close: async () => { /* no-op */ },
  };
  return q;
}

function makeFakeMsg(body: any): FakeMsg {
  const m: FakeMsg = {
    body,
    ack_calls: 0,
    retry_calls: 0,
    ack: () => { m.ack_calls++; },
    retry: () => { m.retry_calls++; },
    timestamp: new Date(),
    attempts: 0,
  };
  return m;
}

interface TestEnv {
  PASAY_QUEUE: FakeQueue;
  PASAY_CONTAINER: any; // DurableObjectNamespace shape — object identity, opaque to runtime
  TELEGRAM_WEBHOOK_SECRET?: string;
  PASAY_CONTAINER_INGEST_TOKEN?: string;
}

function makeEnv(overrides: Partial<TestEnv> = {}): TestEnv {
  return Object.assign({
    PASAY_QUEUE: makeFakeQueue(),
    PASAY_CONTAINER: { __namespace: "PASAY_CONTAINER" },
    TELEGRAM_WEBHOOK_SECRET: "correct-secret",
    PASAY_CONTAINER_INGEST_TOKEN: "ingest-token",
  } as TestEnv, overrides);
}

// Minimal Request factory — we use the real global Request (node 18+) so that
// the real handler can parse it through the real code paths.
function makeWorkerRequest(pathname: string, opts: {
  method?: string;
  headers?: Record<string, string>;
  body?: any;
} = {}): Request {
  const method = opts.method ?? "GET";
  const headers = new Headers(opts.headers ?? {});
  const url = `https://worker.example${pathname}`;
  const init: RequestInit = {
    method,
    headers,
  };
  if (opts.body !== undefined) {
    (init as any).body = typeof opts.body === "string"
      ? opts.body
      : JSON.stringify(opts.body);
    headers.set("content-type", "application/json");
  }
  return new Request(url, init);
}

// ---------------------------------------------------------------------------
// 3. Assertion helpers (tiny; no test framework dependency — runs with tsx).
// ---------------------------------------------------------------------------

let passed = 0;
let total = 0;

function assert(cond: any, msg: string) {
  if (!cond) {
    throw new Error("ASSERTION FAILED: " + msg);
  }
}
function assert_eq<T>(actual: T, expected: T, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(
      `ASSERTION FAILED: ${msg}\n  actual   = ${a}\n  expected = ${e}`,
    );
  }
}

const all_pending: Array<{ name: string; fn: () => void | Promise<void> }> = [];

function run(name: string, fn: () => void | Promise<void>) {
  total++;
  all_pending.push({ name, fn });
}

async function flushAllPending() {
  for (const { name, fn } of all_pending) {
    try {
      const result = fn();
      if (result && typeof (result as any).then === "function") {
        await result;
      }
      passed++;
      console.log(`  ✓ ${name}`);
    } catch (err: any) {
      console.log(`  ✗ ${name}`);
      console.log(`      ${(err && err.message) || String(err)}`);
      process.exitCode = 1;
    }
  }
}

// ---------------------------------------------------------------------------
// 4. Source-level FIX2 contracts
// ---------------------------------------------------------------------------

console.log("Cloudflare Worker — PASAY-TASK-011 FIX2 targeted validation (REAL handler import)\n");

run("FIX2#1 source: PasayContainer extends Container from @cloudflare/containers", () => {
  assert(
    /import\s*\{\s*Container[^}]*\}\s*from\s*"@cloudflare\/containers"/.test(WORKER_SRC),
    "index.ts MUST import Container from @cloudflare/containers (official API)",
  );
  assert(
    /import\s*\{\s*[^}]*getContainer[^}]*\}\s*from\s*"@cloudflare\/containers"/.test(WORKER_SRC),
    "index.ts MUST import getContainer from @cloudflare/containers (official API)",
  );
  assert(
    /export\s+class\s+PasayContainer\s+extends\s+Container\b/.test(WORKER_SRC),
    "index.ts MUST export class PasayContainer extends Container (NOT a self-invented registry stub)",
  );
  // Sanity: the imported class extends our mock Container base (type-level
  // contract validated at tsc time — runtime instanceof proves we imported
  // the real declaration).
  const instance: any = new (PasayContainer as any)();
  assert(
    typeof instance.defaultPort === "number" && instance.defaultPort === 8000,
    "PasayContainer.defaultPort MUST equal 8000 (matches Dockerfile CMD)",
  );
});

run("FIX2#1 wrangler: [[containers]] + [[durable_objects.bindings]] + [[migrations]] new_sqlite_classes", () => {
  assert(/^\[\[containers\]\]\s*\nclass_name\s*=\s*"PasayContainer"/m.test(WRANGLER_TOML),
    "wrangler.toml MUST have [[containers]] with class_name=PasayContainer");
  assert(/^\[\[durable_objects\.bindings\]\]\s*\nname\s*=\s*"PASAY_CONTAINER"\s*\nclass_name\s*=\s*"PasayContainer"/m.test(WRANGLER_TOML),
    "wrangler.toml MUST have [[durable_objects.bindings]] name=PASAY_CONTAINER class_name=PasayContainer");
  assert(/^\[\[migrations\]\]\s*\nnew_sqlite_classes\s*=\s*\[\s*"PasayContainer"\s*\]/m.test(WRANGLER_TOML),
    "wrangler.toml MUST register Container via [[migrations]] new_sqlite_classes=[\"PasayContainer\"]");
  assert(/\[\[containers\]\][\s\S]*?image\s*=\s*"..\/Dockerfile"/.test(WRANGLER_TOML),
    "wrangler.toml [[containers]] MUST reference the real ../Dockerfile image path");
});

run("FIX2#2 wrangler: [triggers] crons includes 5-minute heartbeat", () => {
  assert(/^\[triggers\]\s*\ncrons\s*=\s*\[.*"\*\/5 \* \* \* \*".*\]/m.test(WRANGLER_TOML),
    "wrangler.toml MUST declare [triggers] crons with 5-minute interval (pasay_heartbeat)");
});

run("FIX2#3 design: NO self-invented ContainersBinding.getByName fake API", () => {
  assert(
    !/ContainersBinding/.test(WORKER_SRC) && !/getByName\s*\(/.test(WORKER_SRC),
    "Worker MUST NOT invent ContainersBinding.getByName() — use real getContainer(env.PASAY_CONTAINER, id)"
  );
  assert(
    !/PasayContainersRegistry/.test(WORKER_SRC),
    "Worker MUST NOT export a fake PasayContainersRegistry DO stub"
  );
});

run("FIX2#3 design: real index.ts calls getContainer(env.PASAY_CONTAINER, instanceId)", () => {
  assert(
    /getContainer\(\s*env\.PASAY_CONTAINER\s*,\s*[A-Z_]+INSTANCE_ID/.test(WORKER_SRC),
    "Worker MUST call getContainer(env.PASAY_CONTAINER, PASAY_CONTAINER_INSTANCE_ID) directly"
  );
});

run("FIX1 carry-over: queue handler calls msg.retry() explicitly for retry case", () => {
  assert(
    !/"retry" is the default/.test(WORKER_SRC),
    "Must NOT rely on silent-default fallthrough semantics (drops messages)"
  );
  assert(
    /msg\s*\.\s*retry\s*\(\s*\)/.test(WORKER_SRC),
    "Worker queue handler MUST call msg.retry() explicitly for transient failures"
  );
});

run("FIX1 carry-over: Request uses absolute URL https://pasay-container/internal/ingest", () => {
  assert(
    /(?:PASAY_CONTAINER_ORIGIN\s*\+\s*CONTAINER_INGEST_PATH|PASAY_CONTAINER_ORIGIN\}\$\{CONTAINER_INGEST_PATH)/.test(WORKER_SRC),
    "Worker MUST construct Request with an absolute URL combining origin + path (string concat or template literal)"
  );
  assert(
    /const PASAY_CONTAINER_ORIGIN\s*=\s*"https:\/\/pasay-container"/.test(WORKER_SRC),
    "PASAY_CONTAINER_ORIGIN absolute URL const must exist"
  );
});

// ---------------------------------------------------------------------------
// 5. Runtime scenarios (driving real Worker handlers via default export)
// ---------------------------------------------------------------------------

beforeEachPerTestCleanup(); // initial reset (below)

function beforeEachPerTestCleanup() {
  containerInstances.clear();
  lastGetContainerArgs.length = 0;
}

// ── Scenario 1: fetch(valid Telegram) → BYPASS Queue, direct forward ────
// Issue #119 P0 LATENCY: interactive Telegram updates no longer go through
// the Cloudflare Queue. The Worker must call container.fetch directly
// (synchronous) so the Owner-visible tap → reply round-trip is no longer
// dominated by queue max_batch_timeout=1s + consumer scheduling overhead.
run("FIX2#3 S1: worker.fetch valid Telegram → DIRECT container.forward (NO queue.send)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Issue #119 P0 LATENCY: the direct-forward path requires a Container
  // handle to be available so getContainer() succeeds. Mock it.
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = {
    ok: true, state: "done", dur_ms: 42,
    attempts: 1, cross_attempt: 1,
  };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 9001, message: { chat: { id: 123, type: "private" }, text: "hi" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "valid update → 200 (direct forward success)");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0,
    "interactive Telegram path BYPASSES Queue (zero queue.send calls)");
  assert_eq(container.fetch_calls.length, 1,
    "direct-forward must call container.fetch exactly once");
  // Verify the forwarded envelope shape and trace headers
  const fc = container.fetch_calls[0];
  assert_eq(fc.method, "POST", "direct forward method POST");
  assert(/\/internal\/ingest$/.test(fc.url),
    "direct forward URL ends with /internal/ingest");
  assert_eq(fc.headers["content-type"], "application/json",
    "Content-Type application/json");
  assert_eq(typeof fc.headers["x-pasay-trace-id"], "string",
    "X-Pasay-Trace-Id header propagated to Container");
  assert_eq(typeof fc.headers["x-pasay-trace-t0"], "string",
    "X-Pasay-Trace-T0 header propagated to Container");
  assert_eq(fc.headers["x-pasay-trace-source"], "telegram_webhook_direct",
    "X-Pasay-Trace-Source marks the direct-forward path");
  // Body carries the envelope
  const body_json = fc.body;
  assert_eq(body_json.version, ENVELOPE_VERSION, "envelope.version");
  assert_eq(body_json.kind, "telegram_update", "envelope.kind");
  assert_eq(body_json.event_id, make_telegram_event_id(9001), "envelope.event_id");
  assert(/^2\d{3}-/.test(body_json.occurred_at), "occurred_at looks ISO");
});

// ── Scenario 2: bad secret → no enqueue ──────────────────────────────────
run("FIX2#3 S2: worker.fetch mismatched secret → NO enqueue", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv({ TELEGRAM_WEBHOOK_SECRET: "server-side-correct" });
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "WRONG-secret",
    },
    body: { update_id: 9002, message: { chat: { id: 456 }, text: "nope" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 403, "mismatched secret → 403");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0, "ZERO queue sends for bad secret");
});

// ── Scenario 3: queue + container 2xx → ack ─────────────────────────────
run("FIX2#3 S3: worker.queue — container 200 → msg.ack() + real getContainer reached", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  const envelope: PasayQueueEnvelope = {
    version: "1",
    kind: "telegram_update",
    event_id: make_telegram_event_id(1),
    occurred_at: new Date().toISOString(),
    payload: { update_id: 1, message: {} } as any,
    _telegram_meta: { update_id: 1 },
  } as any;
  const msg = makeFakeMsg(envelope as any);
  const batch = { messages: [msg] };
  await worker.queue(batch as any, env as any, undefined as any);
  assert_eq(msg.ack_calls, 1, "msg.ack() called exactly once (200 = ack)");
  assert_eq(msg.retry_calls, 0, "msg.retry() never called for 2xx success");
  assert_eq(lastGetContainerArgs.length, 1, "real getContainer() reached exactly once");
  assert_eq(lastGetContainerArgs[0][1], "pasay-singleton", "getContainer instanceId == pasay-singleton");
  assert_eq(container.fetch_calls.length, 1, "container.fetch called once");
  const fetchCall = container.fetch_calls[0];
  assert_eq(fetchCall.method, "POST", "fetch method POST");
  assert(/\/internal\/ingest$/.test(fetchCall.url), "fetch URL ends with /internal/ingest");
  assert_eq(fetchCall.url.startsWith("https://pasay-container"), true, "fetch URL uses absolute origin https://pasay-container");
  assert_eq(fetchCall.headers["x-pasay-ingest-token"], "ingest-token", "ingest token header propagated");
  assert_eq(fetchCall.headers["content-type"], "application/json", "Content-Type application/json");
});

// ── Scenario 4: queue + container 503/throw → explicit retry ────────────
run("FIX2#3 S4: worker.queue — container 503 → msg.retry() EXPLICIT (no silent ack)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(503);
  containerInstances.set("pasay-singleton", container);
  const envelope: PasayQueueEnvelope = {
    version: "1",
    kind: "telegram_update",
    event_id: make_telegram_event_id(2),
    occurred_at: new Date().toISOString(),
    payload: { update_id: 2 } as any,
  } as any;
  const msg = makeFakeMsg(envelope as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.retry_calls, 1, "msg.retry() called EXACTLY once (503 transient)");
  assert_eq(msg.ack_calls, 0, "msg.ack() NEVER called for transient 503");
  assert_eq(lastGetContainerArgs.length, 1, "still reach real getContainer");
});

run("FIX2#3 S4b: worker.queue — container.fetch throws → explicit msg.retry()", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("container cold boot network error");
  containerInstances.set("pasay-singleton", container);
  const envelope: PasayQueueEnvelope = {
    version: "1",
    kind: "telegram_update",
    event_id: make_telegram_event_id(3),
    occurred_at: new Date().toISOString(),
    payload: { update_id: 3 } as any,
  } as any;
  const msg = makeFakeMsg(envelope as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.retry_calls, 1, "fetch throw → msg.retry() once");
  assert_eq(msg.ack_calls, 0, "fetch throw → msg.ack() NEVER");
});

// ── Scenario 5: malformed envelope → terminal (ack) + NO container call ─
run("FIX2#3 S5: worker.queue — malformed envelope (bad version) → terminal/ack, ZERO container calls", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  const bad = { version: "99", kind: "telegram_update", event_id: "tg:1", occurred_at: new Date().toISOString(), payload: {} };
  const msg = makeFakeMsg(bad as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.ack_calls, 1, "malformed version → msg.ack() (terminal / permanent poison)");
  assert_eq(msg.retry_calls, 0, "malformed version → msg.retry() NEVER");
  assert_eq(lastGetContainerArgs.length, 0, "malformed envelope must NEVER reach getContainer/container.fetch");
  assert_eq(container.fetch_calls.length, 0, "container.fetch NEVER called for poison envelope");
});

run("FIX2#3 S5b: worker.queue — malformed envelope unknown kind → terminal/ack", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  const bad = { version: "1", kind: "unknown_xyz", event_id: "tg:1", occurred_at: new Date().toISOString(), payload: {} };
  const msg = makeFakeMsg(bad as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.ack_calls, 1, "unknown kind → terminal ack");
  assert_eq(msg.retry_calls, 0, "unknown kind → never retry");
  assert_eq(container.fetch_calls.length, 0, "no container call");
});

// ── Scenario 6: scheduled() → same PASAY_QUEUE.enqueue ──────────────────
run("FIX2#3 S6: worker.scheduled → enqueues scheduled_job into the SAME PASAY_QUEUE", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const controller = { cron: "*/5 * * * *", scheduledTime: new Date(2026, 7, 20, 12, 0, 0).getTime() };
  await worker.scheduled(controller as any, env as any, undefined as any);
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1, "scheduled() enqueues exactly 1 envelope");
  const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[0] as any;
  assert_eq(envl.version, ENVELOPE_VERSION, "scheduled envelope.version");
  assert_eq(envl.kind, "scheduled_job", "scheduled envelope.kind");
  assert(/^sched:pasay_heartbeat:/.test(envl.event_id), `scheduled event_id starts sched:pasay_heartbeat: got ${envl.event_id}`);
  assert(typeof envl.payload === "object" && envl.payload !== null, "payload is object");
  assert_eq(envl.payload.job_name, "pasay_heartbeat", "payload.job_name == pasay_heartbeat");
  assert_eq(envl.payload.params.cron_expression, "*/5 * * * *", "payload.params.cron_expression propagated from ScheduledController.cron");
  // Occurred_at / scheduled_at ISO UTC timestamp pattern
  for (const field of [envl.occurred_at, envl.payload.scheduled_at] as string[]) {
    assert(/Z|[+-]00:00$/.test(field), `${field} must end with Z or +00:00 (UTC)`);
  }
  // Event id must match deterministic 5-minute bucket for that timestamp
  const expected_id = make_scheduled_event_id("pasay_heartbeat", envl.occurred_at);
  assert_eq(envl.event_id, expected_id, "event_id 5-minute bucket deterministic match");
});

// ---------------------------------------------------------------------------
// 6. Helper contract spot checks (envelope.ts)
// ---------------------------------------------------------------------------

run("Helper: make_telegram_event_id tg: prefix matches decimal update_id", () => {
  assert_eq(make_telegram_event_id(77), "tg:77", "telegram event_id = tg:<update_id>");
});

run("Helper: make_scheduled_event_id 5-minute bucket floored", () => {
  const bucketed = make_scheduled_event_id("job", "2026-08-20T12:07:59.999Z");
  assert(bucketed.endsWith(":2026-08-20T12-05"), `5-min floored to HH-05; got ${bucketed}`);
});

// ---------------------------------------------------------------------------
// RETURN-1 §3: NEW closeout test scenarios (9 categories)
// ---------------------------------------------------------------------------

run("CLOSEOUT#1a: worker.fetch — TELEGRAM_WEBHOOK_SECRET missing → 401 webhook_not_configured", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv({ TELEGRAM_WEBHOOK_SECRET: undefined });
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: { update_id: 1001 },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 401, "missing secret → 401");
  const body = await resp.json() as any;
  assert_eq(body.error, "webhook_not_configured", "error code == webhook_not_configured");
  assert_eq(body.ok, false, "body.ok == false");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0, "NO queue.send when secret missing");
});

run("CLOSEOUT#1b: worker.fetch — empty TELEGRAM_WEBHOOK_SECRET → 401 webhook_not_configured", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv({ TELEGRAM_WEBHOOK_SECRET: "   " });
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "anything" },
    body: { update_id: 1002 },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 401, "whitespace-only secret → 401");
  const body = await resp.json() as any;
  assert_eq(body.error, "webhook_not_configured", "whitespace secret → webhook_not_configured");
});

run("CLOSEOUT#2a: worker.fetch — non-JSON body → 400 bad_content_type", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const req = new Request("http://x/telegram/webhook", {
    method: "POST",
    headers: { "content-type": "text/plain", "X-Telegram-Bot-Api-Secret-Token": "correct-secret" },
    body: "not json content",
  });
  const resp = await worker.fetch(req, env as any, undefined as any);
  assert(resp.status === 400, `non-json → 400 (got ${resp.status})`);
  const body = await resp.json() as any;
  assert_eq(body.error, "bad_content_type", `bad_content_type exact, got ${body.error}`);
});

run("CLOSEOUT#2b: worker.fetch — update_id missing / 0 / negative / non-number → 400", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const cases = [
    { name: "missing update_id", body: { message: {} }, expect_err: "missing_update_id" },
    { name: "update_id = 0", body: { update_id: 0 }, expect_err: "missing_update_id" },
    { name: "update_id negative", body: { update_id: -5 }, expect_err: "missing_update_id" },
    { name: "update_id non-number", body: { update_id: "abc" }, expect_err: "missing_update_id" },
    { name: "update_id NaN", body: { update_id: NaN }, expect_err: "missing_update_id" },
    { name: "payload is array", body: [1, 2, 3], expect_err: "malformed_payload" },
    { name: "payload is null", body: null as any, expect_err: "malformed_payload" },
  ];
  for (const c of cases) {
    beforeEachPerTestCleanup();
    const req = makeWorkerRequest("/telegram/webhook", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "correct-secret" },
      body: c.body,
    });
    const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
    assert_eq(resp.status, 400, `${c.name} → 400 (got ${resp.status})`);
    const json = await resp.json() as any;
    assert(json.error === c.expect_err, `${c.name} expected ${c.expect_err}, got ${json.error}`);
  }
});

run("CLOSEOUT#3a: enqueue success → 200, no enqueue_failed fields", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 2001, message: { chat: { id: 1 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "success → 200");
  const text = await resp.text();
  assert(!/"enqueue_failed"/.test(text), "success resp MUST NOT mention enqueue_failed");
  assert(!/"detail":/.test(text), "success resp MUST NOT contain raw detail field");
  const body = JSON.parse(text) as any;
  assert_eq(body.ok, true, "success ok=true");
  assert_eq(body.event_id, make_telegram_event_id(2001), "success event_id correct");
});

run("CLOSEOUT#3b: direct+enqueue both fail → fixed body {ok:false, error:'enqueue_failed', req_id} NO err.message", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Issue #119 P0 LATENCY: force the direct-forward path to fail (container
  // cold-boot / transient) AND the fallback enqueue to also fail. The Worker
  // must still surface 503 + opaque req_id + NO secret leak.
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("Container cold boot network error");
  containerInstances.set("pasay-singleton", container);
  (env.PASAY_QUEUE as any).send = async () => {
    throw new Error("Queue internal error TELEGRAM_BOT_TOKEN=leakme postgres://user:pass@host/db DATABASE_URL_UNPOOLED=xxyyzz112233445566778899");
  };
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 2002, message: { chat: { id: 1 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 503, "both direct + enqueue fail → 503");
  const text = await resp.text();
  const body = JSON.parse(text) as any;
  assert_eq(body.ok, false, "enqueue_failed ok=false");
  assert_eq(body.error, "enqueue_failed", "error enum == enqueue_failed");
  assert(typeof body.req_id === "string" && body.req_id.startsWith("r_"),
    `req_id must be opaque r_ prefixed, got ${body.req_id}`);
  assert(!("detail" in body), "enqueue_failed resp MUST NOT include raw detail field");
  assert(!/leakme/.test(text), "resp must NOT contain internal leakme substring");
  assert(!/postgres:\/\//.test(text), "resp must NOT expose postgres:// URL");
  assert(!/xxyyzz112233445566778899/.test(text), "resp must NOT expose secret token value");
  assert(!/TELEGRAM_BOT_TOKEN=/.test(text), "resp must NOT include raw k=v secret pair");
  assert(!text.includes("leakme"), "raw secret value leakme must NOT appear in output text");
  assert(!text.includes("xxyyzz112233445566778899"), "raw secret token value must NOT appear in output text");
});

run("CLOSEOUT#3c: mask_sensitive — fabricated secret injection NEVER appears when both direct + enqueue fail", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const FABRICATED_DB = "postgres://admin:SuperSecretPass123!@db.prod.pasay.io:5432/tenant_main_v2";
  const FABRICATED_SECRET = "tg_webhook_XYZ987ABCdef_2026_production";
  const FABRICATED_TOKEN = "ingest_prod_abcdef12345678900987fedcba";
  // Force the direct-forward path to also fail so the fallback enqueue is
  // exercised.
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("Container cold boot network error");
  containerInstances.set("pasay-singleton", container);
  (env.PASAY_QUEUE as any).send = async () => {
    throw new Error(
      `boom: DATABASE_URL=${FABRICATED_DB}; TELEGRAM_WEBHOOK_SECRET=${FABRICATED_SECRET}; PASAY_CONTAINER_INGEST_TOKEN=${FABRICATED_TOKEN}; random_long_id=0123456789abcdef0123456789abcdef END`
    );
  };
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 2003, message: { chat: { id: 2 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 503, "both direct + enqueue fail → 503");
  const text = await resp.text();
  const body = JSON.parse(text) as any;
  assert_eq(body.ok, false, "ok=false");
  assert_eq(body.error, "enqueue_failed", "error=enqueue_failed");
  assert(typeof body.req_id === "string" && body.req_id.length > 6, `req_id looks opaque (len=${body.req_id?.length})`);
  const deny = [FABRICATED_DB, FABRICATED_SECRET, FABRICATED_TOKEN, "SuperSecretPass123",
    "db.prod.pasay.io", "admin:SuperSecret", "tenant_main_v2"];
  for (const d of deny) {
    assert(!text.includes(d), `Response MUST NOT leak fabricated secret: '${d.slice(0, 20)}…'`);
  }
  assert(!text.includes("detail"), "response must NOT have raw detail key");
});

run("MASK_SENSITIVE#1: unquoted KEY=value style masked correctly", () => {
  const SECRET_VAL = "secret1234567890abcdef";
  const input = `TOKEN=${SECRET_VAL}; KEY=otherpass; SECRET=xyz123abc`;
  const result = mask_sensitive(input);
  assert(result.includes("TOKEN=***"), `unquoted TOKEN should mask to TOKEN=***, got: ${result}`);
  assert(result.includes("KEY=***"), `unquoted KEY should mask to KEY=***, got: ${result}`);
  assert(result.includes("SECRET=***"), `unquoted SECRET should mask to SECRET=***, got: ${result}`);
  assert(!result.includes(SECRET_VAL), `raw secret value '${SECRET_VAL}' MUST NOT appear in masked output`);
  assert(!result.includes("otherpass"), "raw secret 'otherpass' MUST NOT appear in masked output");
  assert(!result.includes("xyz123abc"), "raw secret 'xyz123abc' MUST NOT appear in masked output");
});

run("MASK_SENSITIVE#2: double-quoted JSON \"KEY\":\"value\" style masked correctly", () => {
  const SECRET_VAL = "secret1234567890abcdef";
  const input = `{"TOKEN":"${SECRET_VAL}", "TELEGRAM_BOT_TOKEN":"bot-abc-123-xyz", "DATABASE_URL":"postgres://u:p@h/db"}`;
  const result = mask_sensitive(input);
  assert(result.includes('"TOKEN":"***"'), `double-quoted TOKEN should mask to "TOKEN":"***", got: ${result}`);
  assert(result.includes('"TELEGRAM_BOT_TOKEN":"***"'), `double-quoted TELEGRAM_BOT_TOKEN masked, got: ${result}`);
  assert(!result.includes(SECRET_VAL), `raw secret value '${SECRET_VAL}' MUST NOT appear in masked output`);
  assert(!result.includes("bot-abc-123-xyz"), "raw TELEGRAM_BOT_TOKEN value MUST NOT appear in output");
  assert(!result.includes("postgres://u:p@h/db"), "raw DATABASE_URL value MUST NOT appear in output");
});

run("MASK_SENSITIVE#3: single-quoted 'KEY':'value' style masked correctly", () => {
  const SECRET_VAL = "secret1234567890abcdef";
  const input = `{'TOKEN':'${SECRET_VAL}', 'SECRET':'my-single-quote-secret-98765'}`;
  const result = mask_sensitive(input);
  assert(result.includes("'TOKEN':'***'"), `single-quoted TOKEN should mask to 'TOKEN':'***', got: ${result}`);
  assert(result.includes("'SECRET':'***'"), `single-quoted SECRET should mask to 'SECRET':'***', got: ${result}`);
  assert(!result.includes(SECRET_VAL), `raw secret value '${SECRET_VAL}' MUST NOT appear in masked output`);
  assert(!result.includes("my-single-quote-secret-98765"), "raw single-quoted secret MUST NOT appear in output");
});

run("MASK_SENSITIVE#4: mixed styles in same string all masked", () => {
  const raw1 = "unquoted_secret_val_12345";
  const raw2 = "double_quoted_secret_val_67890";
  const raw3 = "single_quoted_secret_val_abcde";
  const input = `config: KEY=${raw1}, JSON: "DATABASE_URL_UNPOOLED":"${raw2}", shell: 'CONTAINER_INGEST_TOKEN':'${raw3}'`;
  const result = mask_sensitive(input);
  assert(!result.includes(raw1), `raw1 '${raw1}' MUST be masked away`);
  assert(!result.includes(raw2), `raw2 '${raw2}' MUST be masked away`);
  assert(!result.includes(raw3), `raw3 '${raw3}' MUST be masked away`);
  assert(result.includes("KEY=***"), "unquoted KEY=*** present");
  assert(result.includes('"DATABASE_URL_UNPOOLED":"***"'), 'double-quoted DATABASE_URL_UNPOOLED="***" present');
  assert(result.includes("'CONTAINER_INGEST_TOKEN':'***'"), "single-quoted CONTAINER_INGEST_TOKEN='***' present");
});

run("MASK_SENSITIVE#5: bare hex tokens (20+ chars, no label, non-UUID shape) masked to *** while canonical UUIDs stay intact", () => {
  const HEX32 = "0123456789abcdef0123456789abcdef";          // 32 chars – bare hex API key
  const HEX40 = "deadbeefcafebabec0ffeef00dfeedc0cdeadd0d";    // 40 chars – SHA-1-like token
  const HEX64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  const LEGAL_UUID = "123e4567-e89b-12d3-a456-426614174000";
  const ANOTHER_UUID = "F47AC10B-58CC-4372-A567-0E02B2C3D479"; // uppercase is also a legal UUID shape
  const LEGAL_TIMESTAMP = "2026-08-17T12:34:56.789Z";           // contains ":" so kept per the "timestamp/ratio" guard
  const SHORT_HEX = "abcd1234ef";                              // 10 chars, below 20 threshold kept unchanged
  const input = [
    `token=${HEX32}`,
    `trace=${LEGAL_UUID}`,
    `sig=${HEX40}`,
    `req=${ANOTHER_UUID}`,
    `longkey=${HEX64}`,
    `created:${LEGAL_TIMESTAMP}`,
    `short=${SHORT_HEX}`,
  ].join(" | ");
  const out = mask_sensitive(input);
  assert(!out.includes(HEX32), `bare 32-char hex token must be masked: ${HEX32.slice(0, 8)}… in:\n${out}`);
  assert(!out.includes(HEX40), `bare 40-char hex token must be masked: ${HEX40.slice(0, 8)}… in:\n${out}`);
  assert(!out.includes(HEX64), `bare 64-char hex token must be masked: ${HEX64.slice(0, 8)}… in:\n${out}`);
  assert(out.includes(LEGAL_UUID), `canonical hyphenated UUID ${LEGAL_UUID} must NOT be masked (trace IDs must survive)\n${out}`);
  assert(out.includes(ANOTHER_UUID), `uppercase canonical UUID ${ANOTHER_UUID} must NOT be masked\n${out}`);
  assert(out.includes(LEGAL_TIMESTAMP), `colon-including timestamp ${LEGAL_TIMESTAMP} must NOT be masked (kept via includes-colon guard)\n${out}`);
  assert(out.includes(SHORT_HEX), `sub-20-char hex fragment ${SHORT_HEX} must NOT be masked\n${out}`);
});

run("CLOSEOUT#4: make_telegram_event_id deterministic format + 5-min bucket floored", () => {
  assert_eq(make_telegram_event_id(123), "tg:123", "make_telegram_event_id(123) == tg:123");
  assert_eq(make_telegram_event_id(0), "tg:0", "make_telegram_event_id(0) still formats (0 is invalid but helper is pure)");
  const ts = "2026-08-20T12:34:56.999Z";
  const floored = make_scheduled_event_id("j", ts);
  assert(floored.includes(":2026-08-20T12-30"), `5-min bucket 34:56 floored HH-30, got ${floored}`);
  const ts2 = "2026-08-20T12:00:00.000Z";
  const floored2 = make_scheduled_event_id("j", ts2);
  assert(floored2.includes(":2026-08-20T12-00"), `5-min bucket 12:00 stays 00, got ${floored2}`);
});

run("CLOSEOUT#5a: deliver → X-Pasay-Ingest-Token header EXACTLY equals env.PASAY_CONTAINER_INGEST_TOKEN", async () => {
  beforeEachPerTestCleanup();
  const token = "custom-ingest-token-RETURN1";
  const env = makeEnv({ PASAY_CONTAINER_INGEST_TOKEN: token });
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  const envelope: PasayQueueEnvelope = {
    version: "1", kind: "telegram_update",
    event_id: make_telegram_event_id(3001), occurred_at: new Date().toISOString(),
    payload: { update_id: 3001 } as any,
  } as any;
  const msg = makeFakeMsg(envelope as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.ack_calls, 1, "200 → ack");
  assert_eq(container.fetch_calls.length, 1, "one fetch");
  assert_eq(container.fetch_calls[0].headers["x-pasay-ingest-token"], token,
    "Container fetch header X-Pasay-Ingest-Token == env PASAY_CONTAINER_INGEST_TOKEN EXACTLY");
});

run("CLOSEOUT#5b: deliver → PASAY_CONTAINER_INGEST_TOKEN undefined/empty → retry (NOT ack/terminal)", async () => {
  beforeEachPerTestCleanup();
  for (const val of [undefined, "", "   "]) {
    beforeEachPerTestCleanup();
    const container: MockContainerHandle = makeMockContainerHandle(200);
    containerInstances.set("pasay-singleton", container);
    const env = makeEnv({ PASAY_CONTAINER_INGEST_TOKEN: val });
    const envelope: PasayQueueEnvelope = {
      version: "1", kind: "telegram_update",
      event_id: make_telegram_event_id(3002), occurred_at: new Date().toISOString(),
      payload: { update_id: 3002 } as any,
    } as any;
    const msg = makeFakeMsg(envelope as any);
    await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
    assert_eq(msg.retry_calls, 1,
      `token=${JSON.stringify(val)} → retry (transient bad config)`);
    assert_eq(msg.ack_calls, 0,
      `token=${JSON.stringify(val)} → NEVER ack (never terminal or success)`);
    assert_eq(container.fetch_calls.length, 0,
      `token=${JSON.stringify(val)} → NEVER reach container.fetch`);
  }
});

run("CLOSEOUT#6: Container status codes → EXACT ack/retry/terminal mapping (200/202/208 → ack; 400/415/422 → terminal ack; 401/403/500-599 → retry)", async () => {
  const mapping: Array<{ status: number; expect_ack: number; expect_retry: number; label: string }> = [
    { status: 200, expect_ack: 1, expect_retry: 0, label: "200 OK → ack" },
    { status: 202, expect_ack: 1, expect_retry: 0, label: "202 Accepted → ack" },
    { status: 208, expect_ack: 1, expect_retry: 0, label: "208 Already Reported → ack" },
    { status: 400, expect_ack: 1, expect_retry: 0, label: "400 Bad Request → terminal/ack (permanent poison)" },
    { status: 415, expect_ack: 1, expect_retry: 0, label: "415 Unsupported Media → terminal/ack" },
    { status: 422, expect_ack: 1, expect_retry: 0, label: "422 Unprocessable → terminal/ack" },
    { status: 401, expect_ack: 0, expect_retry: 1, label: "401 Unauthorized → retry (ingest token misconfig, transient)" },
    { status: 403, expect_ack: 0, expect_retry: 1, label: "403 Forbidden → retry" },
    { status: 500, expect_ack: 0, expect_retry: 1, label: "500 Internal Server → retry" },
    { status: 503, expect_ack: 0, expect_retry: 1, label: "503 Service Unavailable → retry" },
    { status: 599, expect_ack: 0, expect_retry: 1, label: "599 (custom) → retry (5xx range)" },
    { status: 520, expect_ack: 0, expect_retry: 1, label: "520 Cloudflare → retry" },
  ];
  for (const row of mapping) {
    beforeEachPerTestCleanup();
    const env = makeEnv();
    const container: MockContainerHandle = makeMockContainerHandle(row.status);
    containerInstances.set("pasay-singleton", container);
    const envelope: PasayQueueEnvelope = {
      version: "1", kind: "telegram_update",
      event_id: make_telegram_event_id(4000 + row.status),
      occurred_at: new Date().toISOString(),
      payload: { update_id: 4000 + row.status } as any,
    } as any;
    const msg = makeFakeMsg(envelope as any);
    await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
    assert_eq(msg.ack_calls, row.expect_ack, `${row.label}: ack_calls`);
    assert_eq(msg.retry_calls, row.expect_retry, `${row.label}: retry_calls`);
  }
});

run("CLOSEOUT#7a: PasayContainer envVars keyset — SOURCE-LEVEL 15 unique keys (5 mapped from env + 1 static + 9 PASSAY_ bot keys forwarded under their canonical pasay_bot names)", () => {
  // Issue #119 P0: the bot's pasay_bot.config.Settings loader reads env vars
  // under their `PASSAY_*` names. The Worker must forward them under those
  // names so the runtime bot can find the token / API key / job key it
  // actually needs. Source-level guardrail: the key assignments in the
  // PasayContainer constructor body must cover every name the bot reads.
  const requiredNames = [
    "PASAY_RUNTIME_MODE",
    "DATABASE_URL",
    "DATABASE_URL_UNPOOLED",
    "TELEGRAM_BOT_TOKEN",
    "PASSAY_TG_BOT_TOKEN",
    "PASSAY_API_BASE",
    "PASSAY_API_KEY",
    "PASSAY_ADMIN_API_KEY",
    "PASSAY_JOB_API_KEY",
    "PASSAY_SYSTEM_ORG_ID",
    "PASSAY_HTTP_TIMEOUT_SECONDS",
    "PASSAY_ARCHIVE_CHAT_ID",
    "PASSAY_MINI_APP_URL",
    "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
    "TELEGRAM_WEBHOOK_SECRET",
    "CONTAINER_INGEST_TOKEN",
  ];
  for (const name of requiredNames) {
    const re = new RegExp(`${name}\\s*:`);
    assert(re.test(WORKER_SRC),
      `PasayContainer envVars must assign ${name} (Issue #119 P0 bot-env forwarding)`);
  }
});

run("CLOSEOUT#7b: PasayContainer envVars — runtime instantiated with full Env → exact keyset + per-key mapping source verified", () => {
  const fullEnv = {
    PASAY_QUEUE: makeFakeQueue(),
    PASAY_CONTAINER: { x: 1 },
    TELEGRAM_WEBHOOK_SECRET: "wh_sec_v1",
    PASAY_CONTAINER_INGEST_TOKEN: "ingest_v1",
    DATABASE_URL: "postgres://u:p@h/d",
    DATABASE_URL_UNPOOLED: "postgres://u:p@h/d_direct",
    TELEGRAM_BOT_TOKEN: "123:abc",
    // Issue #119 P0: bot env vars the operator provisions as Worker secrets
    // (separate `wrangler secret put <NAME>` per var). Empty defaults are
    // intentional: an unprovisioned Worker still boots, the bot fails closed
    // on its first API call instead of impersonating anyone.
    PASSAY_API_BASE: "http://127.0.0.1:8000/api/v1",
    PASSAY_API_KEY: "manager-key",
    PASSAY_ADMIN_API_KEY: "admin-key",
    PASSAY_JOB_API_KEY: "system-key",
    PASSAY_SYSTEM_ORG_ID: "42",
    PASSAY_HTTP_TIMEOUT_SECONDS: "45",
    PASSAY_ARCHIVE_CHAT_ID: "-1001234567890",
    PASSAY_MINI_APP_URL: "https://pasay-mini-app.pages.dev",
    PASSAY_MINI_APP_OWNER_TELEGRAM_IDS: "111,222",
  };
  const inst = new (PasayContainer as any)({ id: "stub" }, fullEnv);
  const keys = Object.keys(inst.envVars).sort();
  const expected = [
    "CONTAINER_INGEST_TOKEN", "DATABASE_URL", "DATABASE_URL_UNPOOLED",
    "PASSAY_ADMIN_API_KEY", "PASSAY_API_BASE", "PASSAY_API_KEY",
    "PASSAY_ARCHIVE_CHAT_ID", "PASSAY_HTTP_TIMEOUT_SECONDS",
    "PASSAY_JOB_API_KEY", "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
    "PASSAY_MINI_APP_URL", "PASSAY_SYSTEM_ORG_ID", "PASAY_RUNTIME_MODE", "PASSAY_TG_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
  ].sort();
  assert_eq(keys.length, expected.length,
    `envVars keys length = ${expected.length} unique — got ${keys.length}: ${keys}`);
  for (const k of expected) {
    assert(k in inst.envVars, `envVars must contain key ${k}`);
  }
  // Per-key exact mapping source:
  //   4 keys = direct 1:1 from Env → envVars (legacy Worker secrets)
  assert_eq(inst.envVars.DATABASE_URL, fullEnv.DATABASE_URL,
    "DATABASE_URL <-- env.DATABASE_URL (1:1)");
  assert_eq(inst.envVars.DATABASE_URL_UNPOOLED, fullEnv.DATABASE_URL_UNPOOLED,
    "DATABASE_URL_UNPOOLED <-- env.DATABASE_URL_UNPOOLED (1:1)");
  assert_eq(inst.envVars.TELEGRAM_BOT_TOKEN, fullEnv.TELEGRAM_BOT_TOKEN,
    "TELEGRAM_BOT_TOKEN <-- env.TELEGRAM_BOT_TOKEN (1:1)");
  assert_eq(inst.envVars.TELEGRAM_WEBHOOK_SECRET, fullEnv.TELEGRAM_WEBHOOK_SECRET,
    "TELEGRAM_WEBHOOK_SECRET <-- env.TELEGRAM_WEBHOOK_SECRET (1:1)");
  //   1 key = NAME MAPPING (PASAY_CONTAINER_INGEST_TOKEN in Env →
  //     CONTAINER_INGEST_TOKEN in envVars), to match backend snake_case
  //     "container_ingest_token" Settings key.
  assert_eq(inst.envVars.CONTAINER_INGEST_TOKEN, fullEnv.PASAY_CONTAINER_INGEST_TOKEN,
    "CONTAINER_INGEST_TOKEN <-- env.PASAY_CONTAINER_INGEST_TOKEN (NAME MAPPING: PASAY_ prefix stripped)");
  //   1 key = SHARED SOURCE (TELEGRAM_BOT_TOKEN in Env → BOTH
  //     TELEGRAM_BOT_TOKEN and PASSAY_TG_BOT_TOKEN in envVars). The bot
  //     reads PASSAY_TG_BOT_TOKEN; we forward the same Worker secret under
  //     both names so the operator needs to provision only ONE secret.
  assert_eq(inst.envVars.PASSAY_TG_BOT_TOKEN, fullEnv.TELEGRAM_BOT_TOKEN,
    "PASSAY_TG_BOT_TOKEN <-- env.TELEGRAM_BOT_TOKEN (shared source: operator provisions only TELEGRAM_BOT_TOKEN)");
  //   7 keys = direct 1:1 from Env → envVars (new bot env vars the operator
  //     provisions as dedicated Worker secrets).
  assert_eq(inst.envVars.PASSAY_API_BASE, fullEnv.PASSAY_API_BASE,
    "PASSAY_API_BASE <-- env.PASSAY_API_BASE (1:1)");
  assert_eq(inst.envVars.PASSAY_API_KEY, fullEnv.PASSAY_API_KEY,
    "PASSAY_API_KEY <-- env.PASSAY_API_KEY (1:1)");
  assert_eq(inst.envVars.PASSAY_ADMIN_API_KEY, fullEnv.PASSAY_ADMIN_API_KEY,
    "PASSAY_ADMIN_API_KEY <-- env.PASSAY_ADMIN_API_KEY (1:1)");
  assert_eq(inst.envVars.PASSAY_JOB_API_KEY, fullEnv.PASSAY_JOB_API_KEY,
    "PASSAY_JOB_API_KEY <-- env.PASSAY_JOB_API_KEY (1:1)");
  assert_eq(inst.envVars.PASSAY_SYSTEM_ORG_ID, fullEnv.PASSAY_SYSTEM_ORG_ID,
    "PASSAY_SYSTEM_ORG_ID <-- env.PASSAY_SYSTEM_ORG_ID (1:1)");
  assert_eq(inst.envVars.PASSAY_HTTP_TIMEOUT_SECONDS, fullEnv.PASSAY_HTTP_TIMEOUT_SECONDS,
    "PASSAY_HTTP_TIMEOUT_SECONDS <-- env.PASSAY_HTTP_TIMEOUT_SECONDS (1:1)");
  assert_eq(inst.envVars.PASSAY_ARCHIVE_CHAT_ID, fullEnv.PASSAY_ARCHIVE_CHAT_ID,
    "PASSAY_ARCHIVE_CHAT_ID <-- env.PASSAY_ARCHIVE_CHAT_ID (1:1)");
  assert_eq(inst.envVars.PASSAY_MINI_APP_URL, fullEnv.PASSAY_MINI_APP_URL,
    "PASSAY_MINI_APP_URL <-- env.PASSAY_MINI_APP_URL (1:1)");
  assert_eq(inst.envVars.PASSAY_MINI_APP_OWNER_TELEGRAM_IDS, fullEnv.PASSAY_MINI_APP_OWNER_TELEGRAM_IDS,
    "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS <-- env.PASSAY_MINI_APP_OWNER_TELEGRAM_IDS (1:1)");
  //   1 key = STATIC (not from env, never changes regardless of env values)
  assert_eq(inst.envVars.PASAY_RUNTIME_MODE, "cloudflare-container",
    "PASAY_RUNTIME_MODE is STATIC (NOT from env) == cloudflare-container");
  // Container constructor passes the original env object through Container base
  // super() as-is, so DurableObject storage / bindings (PASAY_QUEUE, PASAY_CONTAINER)
  // remain accessible through Container.env. The extra keys are intentionally
  // NOT forwarded into envVars because they are platform bindings, not env vars.
});

run("Issue#119 P0 TELEGRAM-RUNTIME: when TELEGRAM_BOT_TOKEN is the ONLY token env, PASSAY_TG_BOT_TOKEN in envVars MUST equal it (bot must not boot with token '0:UNSET')", () => {
  // This is the exact user-visible failure mode from Issue #119: the bot
  // was built with pasay_tg_bot_token="" → PTB ApplicationBuilder fell
  // back to "0:UNSET" → every bot.send_message hit api.telegram.org/bot
  // 0:UNSET/sendMessage → Telegram returned InvalidToken → handler failed
  // PERMANENTLY → update marked failed → Owner saw NOTHING. This regression
  // guardrail asserts the Worker's PasayContainer.forwarder covers the
  // bot's expected env var name out of the SAME Worker secret.
  const fullEnv = {
    PASAY_QUEUE: makeFakeQueue(),
    PASAY_CONTAINER: { x: 1 },
    TELEGRAM_WEBHOOK_SECRET: "wh_sec",
    PASAY_CONTAINER_INGEST_TOKEN: "ingest_v",
    DATABASE_URL: "postgres://u@h/d",
    DATABASE_URL_UNPOOLED: "postgres://u@h/d_direct",
    TELEGRAM_BOT_TOKEN: "555:REAL_PROD_TOKEN",
  };
  const inst = new (PasayContainer as any)({ id: "stub" }, fullEnv);
  assert_eq(inst.envVars.PASSAY_TG_BOT_TOKEN, "555:REAL_PROD_TOKEN",
    "PASSAY_TG_BOT_TOKEN in envVars MUST equal env.TELEGRAM_BOT_TOKEN " +
    "(bot reads PASSAY_TG_BOT_TOKEN, not TELEGRAM_BOT_TOKEN)");
  assert(inst.envVars.PASSAY_TG_BOT_TOKEN !== "",
    "PASSAY_TG_BOT_TOKEN in envVars MUST NOT be empty (would cause " +
    "PTB.ApplicationBuilder.token fallback to '0:UNSET')");
});

run("CLOSEOUT#7c: PasayContainer envVars — partial/missing env → empty strings, no crash, no undefined", () => {
  const emptyEnv: any = {};
  const inst = new (PasayContainer as any)(undefined, emptyEnv);
  for (const [k, v] of Object.entries(inst.envVars)) {
    assert(typeof v === "string", `${k} must be string (no undefined)`);
  }
  assert_eq(inst.envVars.PASAY_RUNTIME_MODE, "cloudflare-container",
    "static tag still present with empty env");
  // Issue #119 P0 regression guardrail: even when the operator has not
  // provisioned any Worker secrets yet, the envVars map must still expose
  // every name the bot reads (defaulting to "" so the bot fails closed on
  // its first API call instead of silently impersonating anyone). The one
  // exception is PASAY_RUNTIME_MODE which is the STATIC tag — it is always
  // "cloudflare-container" regardless of any operator provisioning.
  const dynamicRequiredNames = [
    "PASSAY_TG_BOT_TOKEN", "PASSAY_API_BASE", "PASSAY_API_KEY",
    "PASSAY_ADMIN_API_KEY", "PASSAY_JOB_API_KEY", "PASSAY_SYSTEM_ORG_ID",
    "PASSAY_HTTP_TIMEOUT_SECONDS", "PASSAY_ARCHIVE_CHAT_ID",
    "PASSAY_MINI_APP_URL", "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
    "DATABASE_URL", "DATABASE_URL_UNPOOLED", "CONTAINER_INGEST_TOKEN",
  ];
  for (const k of dynamicRequiredNames) {
    assert(k in inst.envVars, `envVars must expose ${k} even with empty env`);
    assert_eq(inst.envVars[k], "", `${k} must default to empty string with empty env`);
  }
});

// ---------------------------------------------------------------------------
// 8. Issue #119 Mini App public API proxy — Worker forwards /api/v1/* to
//    the Container over the same native binding the queue path uses, so
//    the Cloudflare Pages Mini App (pasay-mini-app.pages.dev) can reach
//    the FastAPI V1 surface for the Owner-only initData exchange + Home +
//    Properties. Without this hop the SPA cannot reach the backend
//    because Pages is static and the Container is not publicly bound.
// ---------------------------------------------------------------------------

run("Issue#119 P0-A: worker.fetch GET /api/v1/properties → container.forward (no ingest token, no /internal prefix)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { properties: [{ id: 1, name: "Pioneer" }] };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/api/v1/properties?org_id=42", {
    method: "GET",
    headers: { Authorization: "Bearer test-key" },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "container 200 → 200 pass-through");
  assert_eq(container.fetch_calls.length, 1, "container.fetch called exactly once");
  const fc = container.fetch_calls[0];
  assert_eq(fc.method, "GET", "forwarded method GET");
  assert_eq(fc.url, "https://pasay-container/api/v1/properties?org_id=42",
    "forwarded URL preserves path + query on Container origin");
  // No /internal/ingest token must ever leak into the /api/v1 path —
  // the Container's Bearer / membership middleware is the only auth gate.
  assert_eq(fc.headers["x-pasay-ingest-token"], undefined,
    "/api/v1 must NOT carry internal ingest token");
  assert_eq(fc.headers["authorization"], "Bearer test-key",
    "Authorization header forwarded verbatim");
  const body = await resp.json() as any;
  assert_eq(body.properties[0].name, "Pioneer",
    "Container body returned unchanged to SPA");
});

run("Issue#119 P0-B: worker.fetch POST /api/v1/webapp/auth → container.forward with JSON body + boundary headers", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { api_key: "ak_xxx", org_id: 7, user_id: 99, role: "owner" };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/api/v1/webapp/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: { init_data: "query_id=abc&user=%7B%7D" },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "/api/v1/webapp/auth pass-through → 200");
  assert_eq(container.fetch_calls.length, 1, "container called once");
  const fc = container.fetch_calls[0];
  assert_eq(fc.method, "POST", "POST preserved");
  assert_eq(fc.url, "https://pasay-container/api/v1/webapp/auth",
    "POST forwarded to Container /api/v1/webapp/auth");
  // Body must reach the Container verbatim for HMAC-SHA256 verification.
  const body_text = typeof fc.body === "string" ? fc.body : JSON.stringify(fc.body);
  assert(body_text.includes("init_data") && body_text.includes("query_id=abc"),
    "Container received the JSON body intact");
  assert_eq(fc.headers["x-pasay-ingest-token"], undefined,
    "/api/v1 never carries the internal ingest token");
});

run("Issue#119 P0-C: worker.fetch /api/v1/* with container cold-boot throw → 503 (no silent 200, no silent 404)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("Container cold boot timeout");
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/api/v1/dashboard/home", {
    method: "GET",
    headers: { Authorization: "Bearer test-key" },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 503,
    "container transient → 503 (the SPA surfaces a clear retry signal)");
  const body = await resp.json() as any;
  assert_eq(body.ok, false, "fail-closed body");
  assert_eq(body.error, "container_fetch_failed",
    "stable error code for the SPA retry path");
});

run("Issue#119 P0-D: worker.fetch /api/v1/* with NO PASAY_CONTAINER binding → 503 container_unbound (fail closed)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv({ PASAY_CONTAINER: undefined as any });
  const req = makeWorkerRequest("/api/v1/properties", {
    method: "GET",
    headers: { Authorization: "Bearer test-key" },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 503, "no binding → 503");
  const body = await resp.json() as any;
  assert_eq(body.error, "container_unbound",
    "stable error code (operator fix: configure PASAY_CONTAINER binding)");
});

run("Issue#119 P0-E: worker.fetch /api/v1/* response set-cookie / content-type from Container pass through unchanged", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  // Override the mock to attach a non-JSON Content-Type + a custom header.
  (container as any).fetch = async (req: Request) => {
    container.fetch_calls.push({
      url: req.url, method: req.method, headers: { "content-type": "application/json" },
      body: null,
    });
    return new Response("plain-text response", {
      status: 200,
      headers: { "Content-Type": "text/plain", "X-Pasay-Trace": "ok" },
    });
  };
  const req = makeWorkerRequest("/api/v1/health/extended", {
    method: "GET",
    headers: { Authorization: "Bearer test-key" },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "passthrough 200");
  assert_eq(resp.headers.get("Content-Type"), "text/plain",
    "non-JSON Content-Type propagated");
  assert_eq(resp.headers.get("X-Pasay-Trace"), "ok",
    "custom upstream header propagated");
  assert_eq(await resp.text(), "plain-text response",
    "upstream body propagated verbatim");
});

run("Issue#119 P0-F: worker.fetch OPTIONS /api/v1/* preflight returns 204 + CORS for the Pages Mini App", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // No container call expected — preflight must short-circuit.
  const req = makeWorkerRequest("/api/v1/properties", {
    method: "OPTIONS",
    headers: {
      Origin: "https://pasay-mini-app.pages.dev",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": "Authorization,Content-Type",
    },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 204, "preflight → 204 No Content (no Container wake)");
  assert_eq(resp.headers.get("Access-Control-Allow-Origin"), "https://pasay-mini-app.pages.dev",
    "preflight Allow-Origin matches Pages origin");
  assert_eq(resp.headers.get("Vary"), "Origin", "Vary: Origin set for cache key");
});

run("Issue#119 P0-G: worker.fetch GET /api/v1/* from Pages origin → CORS Allow-Origin echoed", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { hello: "world" };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/api/v1/properties", {
    method: "GET",
    headers: {
      Authorization: "Bearer test-key",
      Origin: "https://pasay-mini-app.pages.dev",
    },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "200 from Container");
  assert_eq(resp.headers.get("Access-Control-Allow-Origin"), "https://pasay-mini-app.pages.dev",
    "Allow-Origin set on the proxied response too");
});

// ---------------------------------------------------------------------------
// 9. Issue #119 P0 LATENCY — interactive Telegram path BYPASSES the Queue.
//    Direct forward → Container (synchronous); Queue reserved for scheduled /
//    background / retry. Regression suite proves (a) direct happy path
//    bypasses Queue, (b) direct transient failure FALLBACKs to Queue,
//    (c) worker.scheduled() still enqueues scheduled_job envelopes to Queue,
//    (d) the queue consumer still handles BOTH envelope kinds so the
//    fallback path is exercised end-to-end.
// ---------------------------------------------------------------------------

run("Issue#119 P0-LATENCY-A: valid Telegram → direct container.fetch (NO queue.send), 200 response carries Container body", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = {
    ok: true, state: "done", dur_ms: 53, attempts: 1, cross_attempt: 1,
  };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 7001, message: { chat: { id: 5177241442 }, text: "🏠 首页" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "direct forward happy path → 200");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok=true (Container returned ok)");
  assert_eq(body.state, "done", "state=done propagated from Container");
  assert_eq(body.dur_ms, 53, "dur_ms (Container measured) propagated");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0,
    "Queue bypassed: zero queue.send calls for interactive path");
  assert_eq(container.fetch_calls.length, 1, "exactly one container.fetch");
  const fc = container.fetch_calls[0];
  assert_eq(fc.url, "https://pasay-container/internal/ingest",
    "URL = https://pasay-container/internal/ingest (direct path)");
  assert_eq(fc.headers["x-pasay-ingest-token"], "ingest-token",
    "internal ingest token forwarded");
  assert_eq(fc.headers["x-pasay-trace-source"], "telegram_webhook_direct",
    "trace source = telegram_webhook_direct");
  assert_eq(typeof body.worker_ingress_ms, "number",
    "Worker side latency field worker_ingress_ms present in response");
});

run("Issue#119 P0-LATENCY-B: direct forward fails (503) → FALLBACK to queue.send (1 call), Worker returns 2xx ACK (Issue #119 webhook ACK contract fix)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Direct forward returns 503 (Container cold-boot / transient).
  const container: MockContainerHandle = makeMockContainerHandle(503);
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 7002, message: { chat: { id: 5177241442 }, text: "🏘 房源" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  // Webhook ACK contract (Issue #119 CURRENT): the envelope was
  // durably accepted by PASAY_QUEUE.send(), so Worker MUST return a 2xx
  // acknowledgement to Telegram — preventing it from logging
  // ``last_error_message = Wrong response from the webhook: 503`` and
  // retrying an update the queue consumer is about to dispatch
  // (idempotently, via the Container's update_id claim).
  assert_eq(resp.status, 200,
    "transient direct failure + queue send OK → 2xx ACK (NOT 503)");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok=true (Worker ACK'd the update)");
  assert_eq(body.path, "enqueued_fallback",
    "path tag identifies the fallback-enqueued envelope");
  assert_eq(body.state, "enqueued_fallback", "state=enqueued_fallback");
  assert_eq(body.delivery, "queue", "delivery hint marks queue path");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1,
    "exactly ONE queue.send call (fallback after direct failure)");
  const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[0] as any;
  assert_eq(envl.kind, "telegram_update", "fallback envelope is telegram_update");
  assert_eq(envl.event_id, make_telegram_event_id(7002),
    "fallback envelope event_id matches the original update_id");
  assert_eq(envl.payload.update_id, 7002, "fallback payload preserved");
});

run("Issue#119 P0-LATENCY-C: direct forward fetch throws (cold-boot) → FALLBACK to queue.send (1 call) + 2xx ACK", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("Container cold boot timeout");
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 7003, message: { chat: { id: 5177241442 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  // Same ACK contract: queue.send() accepted the envelope → 2xx ACK.
  assert_eq(resp.status, 200,
    "container fetch throws + queue send OK → 2xx ACK (NOT 503)");
  const body = await resp.json() as any;
  assert_eq(body.path, "enqueued_fallback", "path tag = enqueued_fallback");
  assert_eq(body.state, "enqueued_fallback", "state = enqueued_fallback");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1,
    "fallback enqueue still happens after fetch throw (idempotency preserved)");
});

run("Issue#119 P0-LATENCY-D: NO PASAY_CONTAINER binding → fallback to queue.send + 2xx ACK", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv({ PASAY_CONTAINER: undefined as any });
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 7004, message: { chat: { id: 5177241442 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  // Same ACK contract: queue.send() accepted the envelope → 2xx ACK.
  assert_eq(resp.status, 200,
    "no binding + queue send OK → 2xx ACK (NOT 503)");
  const body = await resp.json() as any;
  assert_eq(body.path, "enqueued_fallback", "path tag = enqueued_fallback");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1,
    "no binding → fallback enqueue (queue consumer takes over once operator fixes binding)");
});

run("Issue#119 P0-LATENCY-E: scheduled cron STILL enqueues scheduled_job envelopes to the Queue (unchanged)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const controller = { cron: "*/5 * * * *", scheduledTime: new Date(2026, 8, 8, 12, 0, 0).getTime() };
  await worker.scheduled(controller as any, env as any, undefined as any);
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1,
    "scheduled() STILL enqueues scheduled_job (background path unchanged)");
  const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[0] as any;
  assert_eq(envl.kind, "scheduled_job", "scheduled_job envelope preserved");
  assert_eq(envl.payload.job_name, "pasay_heartbeat", "pasay_heartbeat job name");
});

run("Issue#119 P0-LATENCY-F: queue consumer STILL handles telegram_update (fallback path is exercised end-to-end)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  containerInstances.set("pasay-singleton", container);
  const envelope: PasayQueueEnvelope = {
    version: "1", kind: "telegram_update",
    event_id: make_telegram_event_id(7005), occurred_at: new Date().toISOString(),
    payload: { update_id: 7005 } as any,
  } as any;
  const msg = makeFakeMsg(envelope as any);
  await worker.queue({ messages: [msg] } as any, env as any, undefined as any);
  assert_eq(msg.ack_calls, 1, "queue consumer ack on Container 200");
  assert_eq(msg.retry_calls, 0, "no retry on Container 200");
  // Verify the trace header on the queue path too.
  assert_eq(container.fetch_calls[0].headers["x-pasay-trace-source"], "queue_consumer",
    "queue-driven path stamps x-pasay-trace-source=queue_consumer");
});

run("Issue#119 P0-LATENCY-G: idempotency — same update_id arriving twice → Container sees ONE state row", async () => {
  // Two consecutive webhooks with the same update_id (Telegram redelivery
  // scenario). Both bypass the Queue (direct forward). The Container's
  // claim_update_or_short_circuit owns the dedup; we assert that BOTH
  // Worker requests forward to Container and the Queue is never touched.
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { ok: true, state: "done" };
  containerInstances.set("pasay-singleton", container);
  const mk_req = () => makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 7006, message: { chat: { id: 5177241442 }, text: "💰 租金" } },
  });
  const r1 = await worker.fetch(mk_req() as unknown as Request, env as any, undefined as any);
  const r2 = await worker.fetch(mk_req() as unknown as Request, env as any, undefined as any);
  assert_eq(r1.status, 200, "first webhook → 200");
  assert_eq(r2.status, 200, "second webhook (same update_id) → 200 (Container dedups)");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0,
    "NEVER enqueue for interactive path (idempotent redelivery stays direct)");
  assert_eq(container.fetch_calls.length, 2, "Container saw both direct forwards");
});

run("Issue#119 P0-LATENCY-H: source-level — Worker has NO unconditional PASAY_QUEUE.send in handle_telegram_ingress (interactive path never enqueues unconditionally)", () => {
  // Source-level guardrail: the interactive Telegram ingress handler must
  // NOT call env.PASAY_QUEUE.send unconditionally. The fix removes the
  // always-enqueue first step and replaces it with direct-forward-first
  // + Queue-as-fallback. A regression that re-adds an unconditional
  // enqueue (re-introducing the >1s queue latency on the interactive
  // path) is caught here without any runtime machinery.
  // Find handle_telegram_ingress body
  const fn_match = WORKER_SRC.match(/async function handle_telegram_ingress[\s\S]*?\n\}/);
  if (!fn_match) {
    throw new Error("Worker must define handle_telegram_ingress (source-level assertion failed)");
  }
  const body: string = fn_match[0];
  // Count PASAY_QUEUE.send calls inside this function.
  const send_count = (body.match(/PASAY_QUEUE\.send\(/g) || []).length;
  assert_eq(send_count, 1,
    `handle_telegram_ingress must call PASAY_QUEUE.send AT MOST once (the fallback branch only); got ${send_count}`);
  // And it must NOT be in the success path: the direct-forward success
  // branch returns BEFORE any queue.send.
  const success_marker_idx = body.indexOf("if (direct_result.outcome === \"ack\")");
  const send_idx = body.indexOf("PASAY_QUEUE.send(");
  assert(success_marker_idx >= 0, "handle_telegram_ingress must have direct-forward ack branch");
  assert(send_idx >= 0, "handle_telegram_ingress must have a PASAY_QUEUE.send fallback");
  assert(send_idx > success_marker_idx,
    "PASAY_QUEUE.send must come AFTER the direct-forward ack branch (not unconditional)");
});

run("Issue#119 P0-LATENCY-I: source-level — direct_forward_envelope_to_container helper is defined and used by handle_telegram_ingress", () => {
  // Source-level guardrail: the bypass-Queue contract requires a
  // dedicated direct-forward helper that synchronously POSTs to the
  // Container. A regression that breaks the helper (renames it, drops
  // the import, calls getContainer inside a try-catch that swallows
  // errors silently) is caught here.
  assert(/async function direct_forward_envelope_to_container\s*\(/.test(WORKER_SRC),
    "Worker MUST define direct_forward_envelope_to_container");
  const fn_match = WORKER_SRC.match(/async function direct_forward_envelope_to_container[\s\S]*?\n\}/);
  if (!fn_match) {
    throw new Error("direct_forward_envelope_to_container function body extractable (source-level assertion failed)");
  }
  const body: string = fn_match[0];
  // The function MUST call getContainer + handle.fetch.
  assert(/getContainer\(/.test(body), "direct_forward must call getContainer()");
  assert(/handle\.fetch\(/.test(body), "direct_forward must call handle.fetch()");
  // It MUST stamp the trace headers on the forwarded request.
  assert(/TRACE_HEADER/.test(body) && /TRACE_T0_HEADER/.test(body),
    "direct_forward MUST stamp X-Pasay-Trace-Id and X-Pasay-Trace-T0");
});

// ---------------------------------------------------------------------------
// 9b. Issue #119 P0 Webhook ACK Contract — the bounded successor fix
//     scopes ONLY the four ACK scenarios listed in the issue comment:
//       (1) direct success => 2xx
//       (2) transient direct + queue success => 2xx / EXACTLY one enqueue
//       (3) transient direct + queue failure => non-2xx
//       (4) no duplicate processing / replay introduced by the new ACK
//     These tests are deliberately a tight contract set so they can be
//     read alongside the source-level diff without parsing the larger
//     LATENCY-* suite. They DO NOT touch Queue semantics, sleepAfter,
//     or any other code path; they only verify the ACK contract.
// ---------------------------------------------------------------------------

run("Issue#119 ACK-1: direct Worker→Container success returns 2xx (Telegram contract: ACK only on success)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { ok: true, state: "done", dur_ms: 11 };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 8001, message: { chat: { id: 5177241442 }, text: "🏠 首页" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "direct Container 200 ⇒ 2xx ACK");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok=true on direct success");
  assert_eq(body.path, "direct", "path=direct");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0,
    "direct success path: ZERO queue.send calls (no fallback)");
  assert_eq(container.fetch_calls.length, 1, "exactly one direct container.fetch");
});

run("Issue#119 ACK-2: transient direct + PASAY_QUEUE.send OK returns 2xx ACK with EXACTLY one queue.send and trace/path preserved", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Container returns 503 → direct forward fails transient → fallback enqueue.
  const container: MockContainerHandle = makeMockContainerHandle(503);
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 8002, message: { chat: { id: 5177241442 }, text: "🏘 房源" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  // ACK contract (Issue #119 CURRENT): queue.send() succeeded ⇒ 2xx ACK.
  assert_eq(resp.status, 200, "transient direct + queue send OK ⇒ 2xx ACK");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok=true on queue ACK");
  assert_eq(body.state, "enqueued_fallback", "state=enqueued_fallback");
  assert_eq(body.path, "enqueued_fallback", "path=enqueued_fallback");
  assert_eq(body.delivery, "queue", "delivery hint = queue (operator-facing trace field preserved)");
  assert_eq(body.event_id, make_telegram_event_id(8002),
    "event_id correlates with the original update_id for trace continuity");
  // Exactly one queue.send: nothing more (no double-enqueue), nothing less.
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1,
    "EXACTLY ONE queue.send (idempotency preserved, no duplicate processing)");
  const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[0] as any;
  assert_eq(envl.kind, "telegram_update", "fallback envelope kind = telegram_update");
  assert_eq(envl.event_id, make_telegram_event_id(8002),
    "fallback envelope event_id matches the original update_id");
  assert_eq(envl.payload.update_id, 8002, "fallback payload update_id preserved");
});

run("Issue#119 ACK-3: transient direct + PASAY_QUEUE.send FAIL returns NON-2xx (worker refuses to ACK unaccepted data)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Force direct forward to throw (transient).
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("Container cold-boot network error");
  containerInstances.set("pasay-singleton", container);
  // Force the fallback queue.send itself to also throw.
  (env.PASAY_QUEUE as any).send = async () => {
    throw new Error("Queue unavailable: pasay-events binding offline");
  };
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 8003, message: { chat: { id: 5177241442 } } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  // Webhook ACK contract invariant: if the envelope has NOT been durably
  // accepted (neither direct nor fallback), the Worker MUST return non-2xx
  // so Telegram retries via its own redelivery contract.
  assert(
    resp.status >= 500 || (resp.status >= 400 && resp.status < 500),
    `enqueue-also-failed → non-2xx (got ${resp.status})`,
  );
  const body = await resp.json() as any;
  assert_eq(body.ok, false, "body.ok=false on enqueue failure");
  assert_eq(body.error, "enqueue_failed", "error enum = enqueue_failed");
  // Container received exactly one attempt (the direct fetch that threw).
  assert_eq(container.fetch_calls.length, 0,
    "direct container.fetch threw before reaching counter; still ZERO direct-ack enqueues");
});

run("Issue#119 ACK-3b: source-level guardrail — enqueue_failed branch returns non-2xx verbatim", () => {
  // Source-level guardrail: the enqueue_failed branch MUST return a
  // non-2xx status. A regression that accidentally uses 200/202/204 here
  // would silently lose updates (Worker ACK's updates that the Queue
  // has not durably accepted).
  const fn_match = WORKER_SRC.match(/async function handle_telegram_ingress[\s\S]*?\n\}/);
  if (!fn_match) throw new Error("handle_telegram_ingress not found");
  const body: string = fn_match[0];
  // The LAST `return json(` in handle_telegram_ingress is the
  // enqueue_failed branch's return (all earlier branches have returned
  // by this point). Find it and parse the status literal — the literal
  // may sit on the next line if the call uses multi-line formatting, so
  // the regex tolerates any whitespace before the digits.
  const all_returns = [...body.matchAll(/return\s+json\s*\(\s*(\d+)/g)];
  if (all_returns.length === 0) {
    throw new Error("handle_telegram_ingress has no `return json(<int>, …)` — malformed Worker source");
  }
  // Find the LAST return-json whose body includes the enqueue_failed tag.
  // Walk the function body backwards from the end.
  const last_return_block = body.lastIndexOf("return json(");
  if (last_return_block < 0) throw new Error("no return json( in handle_telegram_ingress");
  const tail = body.slice(last_return_block);
  const m = tail.match(/return\s+json\s*\(\s*(\d+)/);
  if (!m) throw new Error("enqueue_failed branch: no `return json(<int>, …)` detected");
  const status = Number(m[1]);
  assert(
    status < 200 || status >= 300,
    `enqueue_failed branch (last return) MUST return non-2xx (got ${status})`,
  );
  // And confirm the branch is actually the enqueue_failed branch — the
  // body must contain error: "enqueue_failed".
  const branch_body_end = body.indexOf("}", last_return_block + 200);
  const branch_body = body.slice(last_return_block, branch_body_end > 0 ? branch_body_end + 1 : last_return_block + 1000);
  assert(/error:\s*"enqueue_failed"/.test(branch_body),
    "last return-json branch must contain error: \"enqueue_failed\"");
});

run("Issue#119 ACK-4a: no duplicate processing / replay — direct success does NOT also enqueue (direct + enqueue double-dispatch guard)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = { ok: true, state: "done" };
  containerInstances.set("pasay-singleton", container);
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 8101, message: { chat: { id: 5177241442 }, text: "✅ 待办" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "direct success ⇒ 200");
  // Hard guard: if the Worker ALSO enqueues when direct succeeds, the
  // queue consumer would re-dispatch the same update_id → the Container
  // would see it twice (idempotency layer catches it, but the
  // double-network-round-trip and double-log still corrupts the trace
  // correlation surface). The source-level guardrail below catches any
  // regression that re-introduces an unconditional enqueue on direct success.
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 0,
    "direct success path MUST NOT enqueue (no duplicate processing / replay)");
  assert_eq(container.fetch_calls.length, 1,
    "direct success: exactly one container.fetch (no duplicate)");
});

run("Issue#119 ACK-4b: no duplicate processing / replay — successive identical update_ids still have exactly ONE queue.send per request (idempotency contract surface)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  // Container throws transient on every direct; queue.send always succeeds.
  const container: MockContainerHandle = makeMockContainerHandle(200);
  (container as any).fetch_throw = new Error("transient");
  containerInstances.set("pasay-singleton", container);
  const mk_req = (update_id: number) => makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id, message: { chat: { id: 5177241442 }, text: "💸 支出" } },
  });
  const r1 = await worker.fetch(mk_req(8201) as unknown as Request, env as any, undefined as any);
  const r2 = await worker.fetch(mk_req(8201) as unknown as Request, env as any, undefined as any);
  const r3 = await worker.fetch(mk_req(8201) as unknown as Request, env as any, undefined as any);
  assert_eq(r1.status, 200, "first webhook → 200");
  assert_eq(r2.status, 200, "Telegram redelivery (same update_id) → 200");
  assert_eq(r3.status, 200, "third redelivery → 200");
  // Each webhook enqueued exactly one fallback envelope — never zero, never
  // more than one (proving the enqueue loop is bounded to one per request,
  // and never collides with itself across requests).
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 3,
    "three webhooks → three queue.send calls (one per webhook, bounded)");
  // All three envelopes carry the same event_id (= tg:<update_id>) so
  // the Container claim layer deduplicates them on dispatch.
  for (let i = 0; i < 3; i++) {
    const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[i] as any;
    assert_eq(envl.event_id, make_telegram_event_id(8201),
      `envelope #${i + 1} event_id matches update_id for idempotent dedup`);
  }
});

// ---------------------------------------------------------------------------
// 10. Issue #119 P0 WARM-PATH TRACE — observability-only instrumentation
//     that proves the Worker-side ``pasay_worker_latency`` structured
//     log line is emitted with the required field set + token redaction.
//     PR scope is observability ONLY (no latency fix); these tests guard
//     against accidental leaks of secrets and ensure the operator-side
//     grep contract stays stable.
// ---------------------------------------------------------------------------

run("Issue#119 P0-WARM-A: Worker emits ``pasay_worker_latency`` log on direct-forward ack path with all required fields", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = {
    ok: true, state: "done", dur_ms: 53, attempts: 1, cross_attempt: 1,
  };
  containerInstances.set("pasay-singleton", container);

  const captured: string[] = [];
  const orig_log = console.log;
  const orig_err = console.error;
  console.log = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  console.error = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  try {
    const req = makeWorkerRequest("/telegram/webhook", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
      },
      body: { update_id: 7101, message: { chat: { id: 5177241442 }, text: "🏠 首页" } },
    });
    const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
    assert_eq(resp.status, 200, "direct forward ack → 200");
  } finally {
    console.log = orig_log;
    console.error = orig_err;
  }

  const line = captured.find((l) => l.includes("pasay_worker_latency"));
  if (!line) {
    throw new Error(
      "Worker MUST emit exactly one 'pasay_worker_latency' line on direct-forward ack path; got:\n"
      + captured.join("\n"),
    );
  }
  // Required fields per the observability contract:
  assert(/trace_id=tg:7101/.test(line), `trace_id (=envelope.event_id) present: ${line}`);
  assert(/path=direct/.test(line), `path tag (direct) present: ${line}`);
  assert(/outcome=ack/.test(line), `outcome=ack present: ${line}`);
  assert(/status=200/.test(line), `status field present: ${line}`);
  assert(/worker_arrival_iso=/.test(line), `worker_arrival_iso present: ${line}`);
  assert(/worker_arrival_ms=/.test(line), `worker_arrival_ms present: ${line}`);
  assert(/worker_total_ms=/.test(line), `worker_total_ms present: ${line}`);
  assert(/container_fetch_ms=/.test(line), `container_fetch_ms present: ${line}`);
  // container_fetch_ms must be a non-negative number — proves the timer
  // was attached, not a literal zero placeholder.
  const cfm = line.match(/container_fetch_ms=(-?\d+(?:\.\d+)?)/);
  if (!cfm) throw new Error(`container_fetch_ms not numeric: ${line}`);
  assert(parseFloat(cfm[1]) >= 0, `container_fetch_ms must be >= 0 (got ${cfm[1]})`);
});

run("Issue#119 P0-WARM-B: Worker emits ``pasay_worker_latency`` log on enqueued_fallback path with status=200 (Issue #119 ACK contract)", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const container: MockContainerHandle = makeMockContainerHandle(503);
  containerInstances.set("pasay-singleton", container);

  const captured: string[] = [];
  const orig_log = console.log;
  const orig_err = console.error;
  console.log = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  console.error = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  try {
    const req = makeWorkerRequest("/telegram/webhook", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
      },
      body: { update_id: 7102, message: { chat: { id: 5177241442 }, text: "🏘 房源" } },
    });
    const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
    // ACK contract: queue.send() accepted the envelope → 2xx ACK.
    assert_eq(resp.status, 200, "direct forward fails + queue.send OK → 2xx ACK");
  } finally {
    console.log = orig_log;
    console.error = orig_err;
  }

  const line = captured.find((l) => l.includes("pasay_worker_latency"));
  if (!line) {
    throw new Error("Worker MUST emit 'pasay_worker_latency' on fallback path; got:\n" + captured.join("\n"));
  }
  assert(/trace_id=tg:7102/.test(line), `trace_id present: ${line}`);
  assert(/path=enqueued_fallback/.test(line), `path tag (enqueued_fallback): ${line}`);
  assert(/outcome=enqueued_fallback/.test(line), `outcome tag matches: ${line}`);
  // Worker ACK on the fallback path is 2xx (issue #119 contract fix).
  assert(/status=200/.test(line), `status=200 (ACK contract): ${line}`);
});

run("Issue#119 P0-WARM-C: Worker pasay_worker_latency NEVER leaks bot token / webhook secret / ingest token (regression guard)", async () => {
  beforeEachPerTestCleanup();
  // Custom env with UNIQUE secret values we can grep for.
  const env = makeEnv({
    TELEGRAM_WEBHOOK_SECRET: "warm-trace-secret-NEVER-LEAK-9182",
    PASAY_CONTAINER_INGEST_TOKEN: "warm-trace-ingest-NEVER-LEAK-7381",
  });
  const container: MockContainerHandle = makeMockContainerHandle(200);
  container.fetch_response_body = {
    ok: true, state: "done", dur_ms: 11, attempts: 1, cross_attempt: 1,
  };
  containerInstances.set("pasay-singleton", container);

  const captured: string[] = [];
  const orig_log = console.log;
  const orig_err = console.error;
  console.log = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  console.error = (...args: any[]) => { captured.push(args.map(String).join(" ")); };
  try {
    const req = makeWorkerRequest("/telegram/webhook", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // Bot token injected via the secret header (which is normally
        // header_eq compared) — pick a value that would be catastrophic
        // if it leaked.
        "X-Telegram-Bot-Api-Secret-Token": "warm-trace-secret-NEVER-LEAK-9182",
      },
      body: { update_id: 7103, message: { chat: { id: 5177241442 }, text: "💸 支出" } },
    });
    await worker.fetch(req as unknown as Request, env as any, undefined as any);
  } finally {
    console.log = orig_log;
    console.error = orig_err;
  }

  const line = captured.find((l) => l.includes("pasay_worker_latency"));
  if (!line) {
    throw new Error("Worker MUST emit 'pasay_worker_latency' on direct-forward ack path; got:\n" + captured.join("\n"));
  }
  // The structured log line MUST NOT contain the configured secrets —
  // mask_sensitive is applied at log time so even an accidental value
  // injection cannot leak. Any regression here means a future PR can
  // ship an accidentally-tokened observability surface.
  assert(!line.includes("warm-trace-secret-NEVER-LEAK-9182"),
    `webhook secret leaked into pasay_worker_latency: ${line}`);
  assert(!line.includes("warm-trace-ingest-NEVER-LEAK-7381"),
    `ingest token leaked into pasay_worker_latency: ${line}`);
});

run("Issue#119 P0-WARM-D: source-level — pasay_worker_latency emits via mask_sensitive so accidental token leaks are redacted", () => {
  // Source-level guardrail: the Worker MUST route the structured log
  // line through ``mask_sensitive`` so a regression that accidentally
  // injects a secret value into a future field still has the
  // redaction layer in place.
  assert(/function log_worker_latency\s*\(/.test(WORKER_SRC),
    "Worker MUST define log_worker_latency helper");
  assert(/mask_sensitive\(line, args\.env\)/.test(WORKER_SRC)
      || /mask_sensitive\(line,\s*args\.env\)/.test(WORKER_SRC),
    "log_worker_latency MUST call mask_sensitive with the full line and env");
  // The line MUST carry the canonical token-redaction marker:
  assert(/pasay_worker_latency trace_id=/.test(WORKER_SRC),
    "Worker source MUST contain the pasay_worker_latency structured line shape");
  assert(/worker_arrival_iso=/.test(WORKER_SRC),
    "Worker source MUST emit worker_arrival_iso");
  assert(/worker_total_ms=/.test(WORKER_SRC),
    "Worker source MUST emit worker_total_ms");
  assert(/container_fetch_ms=/.test(WORKER_SRC),
    "Worker source MUST emit container_fetch_ms");
});

// ---------------------------------------------------------------------------
// 7. Execute async tests + report summary
// ---------------------------------------------------------------------------

(async function main() {
  await flushAllPending();
  console.log(
    `\nCloudflare Worker FIX2 + RETURN-1 CLOSEOUT tests ${passed}/${total} passed`
    + (process.exitCode ? ` (${total - passed} FAILED — exitCode=1)` : " (OK)")
  );
})();
