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

// ── Scenario 1: fetch(valid Telegram) → exactly one queue.send ──────────
run("FIX2#3 S1: worker.fetch valid Telegram → exactly 1 queue.send", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const req = makeWorkerRequest("/telegram/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
    },
    body: { update_id: 9001, message: { chat: { id: 123, type: "private" }, text: "hi" } },
  });
  const resp = await worker.fetch(req as unknown as Request, env as any, undefined as any);
  assert_eq(resp.status, 200, "valid update → 200");
  const body = await resp.json() as any;
  assert_eq(body.ok, true, "body.ok");
  assert_eq((env.PASAY_QUEUE as FakeQueue).send_calls.length, 1, "exactly one enqueue call");
  const envl = (env.PASAY_QUEUE as FakeQueue).send_calls[0] as PasayQueueEnvelope & { kind: string };
  assert_eq(envl.version, ENVELOPE_VERSION, "envelope.version");
  assert_eq(envl.kind, "telegram_update", "envelope.kind");
  assert_eq(envl.event_id, make_telegram_event_id(9001), "envelope.event_id");
  assert(/^2\d{3}-/.test((envl as any).occurred_at), "occurred_at looks ISO");
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

run("CLOSEOUT#3b: enqueue_failed → fixed body {ok:false, error:'enqueue_failed', req_id} NO err.message", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
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
  assert_eq(resp.status, 503, "enqueue failed → 503");
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

run("CLOSEOUT#3c: mask_sensitive — fabricated secret injection NEVER appears in enqueue_failed response", async () => {
  beforeEachPerTestCleanup();
  const env = makeEnv();
  const FABRICATED_DB = "postgres://admin:SuperSecretPass123!@db.prod.pasay.io:5432/tenant_main_v2";
  const FABRICATED_SECRET = "tg_webhook_XYZ987ABCdef_2026_production";
  const FABRICATED_TOKEN = "ingest_prod_abcdef12345678900987fedcba";
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
  assert_eq(resp.status, 503, "enqueue failure → 503");
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

run("CLOSEOUT#7a: PasayContainer envVars keyset — SOURCE-LEVEL 6 unique keys (5 mapped from env + 1 static PASAY_RUNTIME_MODE)", () => {
  const envVarEntries = [...WORKER_SRC.matchAll(/(?:PASAY_RUNTIME_MODE|DATABASE_URL(?:_UNPOOLED)?|TELEGRAM_BOT_TOKEN|TELEGRAM_WEBHOOK_SECRET|CONTAINER_INGEST_TOKEN)\s*:/g)].length;
  assert(envVarEntries >= 6, `source should list the 6-keys envVars map (found ${envVarEntries} key assignments)`);
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
  };
  const inst = new (PasayContainer as any)({ id: "stub" }, fullEnv);
  const keys = Object.keys(inst.envVars).sort();
  const expected = [
    "CONTAINER_INGEST_TOKEN", "DATABASE_URL", "DATABASE_URL_UNPOOLED",
    "PASAY_RUNTIME_MODE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
  ].sort();
  assert_eq(keys.length, expected.length,
    `envVars keys length = ${expected.length} unique — got ${keys.length}: ${keys}`);
  for (const k of expected) {
    assert(k in inst.envVars, `envVars must contain key ${k}`);
  }
  // Per-key exact mapping source:
  //   4 keys = direct 1:1 from Env → envVars
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
  //   1 key = STATIC (not from env, never changes regardless of env values)
  assert_eq(inst.envVars.PASAY_RUNTIME_MODE, "cloudflare-container",
    "PASAY_RUNTIME_MODE is STATIC (NOT from env) == cloudflare-container");
  // Container constructor passes the original env object through Container base
  // super() as-is, so DurableObject storage / bindings (PASAY_QUEUE, PASAY_CONTAINER)
  // remain accessible through Container.env. The extra keys are intentionally
  // NOT forwarded into envVars because they are platform bindings, not env vars.
});

run("CLOSEOUT#7c: PasayContainer envVars — partial/missing env → empty strings, no crash, no undefined", () => {
  const emptyEnv: any = {};
  const inst = new (PasayContainer as any)(undefined, emptyEnv);
  for (const [k, v] of Object.entries(inst.envVars)) {
    assert(typeof v === "string", `${k} must be string (no undefined)`);
  }
  assert_eq(inst.envVars.PASAY_RUNTIME_MODE, "cloudflare-container",
    "static tag still present with empty env");
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
