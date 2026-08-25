/**
 * Pasay Cloudflare Worker — single unified entry for:
 *   (A) Telegram webhook ingress → enqueue
 *   (C) Queue consumer          → official Cloudflare Container instance
 *   (F) Cron scheduled()        → enqueue (same queue, same container)
 *
 * Architectural invariants (Issue #31 — PASAY-TASK-011):
 *   - Worker NEVER runs business logic (no PTB, no DB write, no LLM).
 *   - Worker ONLY validates ingress + envelopes + calls Container binding.
 *   - There is ONE queue. ONE container. ONE FastAPI app. ONE DB boundary.
 *
 * ND_RETURN PASAY-TASK-011 FIX2 #1 compliance (OFFICIAL Container class):
 *   - Uses the OFFICIAL @cloudflare/containers library:
 *       import { Container, getContainer } from "@cloudflare/containers"
 *       export class PasayContainer extends Container { defaultPort = 8000 }
 *       getContainer(env.PASAY_CONTAINER, PASAY_CONTAINER_INSTANCE_ID)
 *         → containerInstance.fetch(absolute_url_request)
 *   - NO self-invented container-discovery interface / no fake Durable Object registry.
 *     (Production uses the official `getContainer(env.DO_NS, id)` factory only.)
 *   - Container class registered via [[durable_objects.bindings]] +
 *     [[migrations]] new_sqlite_classes=["PasayContainer"] in wrangler.toml.
 *   - Request URL is a LEGAL absolute URL pointing at the named container
 *     with pasay-container path semantics.
 */
import {
  ENVELOPE_VERSION,
  make_scheduled_event_id,
  make_telegram_event_id,
  type PasayQueueEnvelope,
  type ScheduledJobEnvelope,
  type TelegramUpdateEnvelope,
} from "./envelope";

// ── OFFICIAL Cloudflare Containers library imports ──────────────────────
// ND_RETURN FIX2 #1: we MUST import Container + getContainer from
// @cloudflare/containers; the PasayContainer class below extends Container.
import { Container, getContainer } from "@cloudflare/containers";

type BindingQueue = Queue;

interface Env {
  PASAY_QUEUE: BindingQueue;
  PASAY_CONTAINER?: DurableObjectNamespace<any>;
  TELEGRAM_WEBHOOK_SECRET?: string;
  PASAY_CONTAINER_INGEST_TOKEN?: string;
  DATABASE_URL?: string;
  DATABASE_URL_UNPOOLED?: string;
  TELEGRAM_BOT_TOKEN?: string;
}

// ── OFFICIAL Container class declaration ────────────────────────────────
// ND_RETURN FIX2 #1: each unique instance ID passed to getContainer() spins
// up one Pasay Container running the Dockerfile image. For our
// single-global-singleton topology we always use the instance id
// "pasay-singleton" so we get exactly one shared container instance.
const PASAY_CONTAINER_INSTANCE_ID = "pasay-singleton";
const PASAY_CONTAINER_ORIGIN = "https://pasay-container";
const TELEGRAM_WEBHOOK_PATH = "/telegram/webhook";
const CONTAINER_INGEST_PATH = "/internal/ingest";
const INGEST_AUTH_HEADER = "X-Pasay-Ingest-Token";

export class PasayContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  envVars: Record<string, string>;

  constructor(ctx: any = {}, env: Env = {} as Env, options?: any) {
    super(ctx, env, options);
    this.envVars = {
      DATABASE_URL: env.DATABASE_URL ?? "",
      DATABASE_URL_UNPOOLED: env.DATABASE_URL_UNPOOLED ?? "",
      TELEGRAM_BOT_TOKEN: env.TELEGRAM_BOT_TOKEN ?? "",
      TELEGRAM_WEBHOOK_SECRET: env.TELEGRAM_WEBHOOK_SECRET ?? "",
      CONTAINER_INGEST_TOKEN: env.PASAY_CONTAINER_INGEST_TOKEN ?? "",
      PASAY_RUNTIME_MODE: "cloudflare-container",
    };
  }
}

type IngestAckResult = "ack" | "retry" | "terminal";

function now_iso(): string {
  return new Date().toISOString();
}

export function mask_sensitive(input: string): string {
  let s = String(input);
  s = s.replace(/postgres(?:ql)?:\/\/[^\s"'<>]+/gi, "postgres://***:***@***:***/***");
  const kvs: Array<string> = [
    "DATABASE_URL", "DATABASE_URL_UNPOOLED",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
    "CONTAINER_INGEST_TOKEN", "PASAY_CONTAINER_INGEST_TOKEN",
    "TOKEN", "SECRET", "KEY",
  ].sort((a, b) => b.length - a.length);
  const kvUpper = new Set(kvs.map((k) => k.toUpperCase()));
  for (const k of kvs) {
    const re = new RegExp(
      `(${k})(["']?)\\s*[:=]\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'&,;]+))`,
      "gi",
    );
    s = s.replace(re, (_m, key, qKey, dqVal, sqVal, uqVal) => {
      if (dqVal !== undefined) return `${key}${qKey}:"***"`;
      if (sqVal !== undefined) return `${key}${qKey}:'***'`;
      return `${key}${qKey}=***`;
    });
  }
  s = s.replace(/[a-zA-Z0-9_\-]{20,}/g, (m) => {
    if (kvUpper.has(m.toUpperCase())) return m;
    if (m.includes(":")) return m;
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(m)) return m;
    return "***";
  });
  return s;
}

function opaque_req_id(): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  let out = "r_";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

function header_eq(a: string | null, b: string): boolean {
  if (!a) return !b;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function extract_telegram_meta(raw: Record<string, unknown>): { update_id: number; chat_id?: number } {
  const update_id = Number(raw["update_id"]);
  let chat_id: number | undefined = undefined;
  for (const key of ["message", "edited_message", "callback_query", "channel_post", "edited_channel_post"]) {
    const node = raw[key] as Record<string, unknown> | undefined;
    if (node && typeof node === "object") {
      const chat = (node as Record<string, unknown>)["chat"] as Record<string, unknown> | undefined;
      if (chat && typeof chat["id"] === "number") {
        chat_id = chat["id"];
        break;
      }
    }
  }
  return { update_id: Number.isFinite(update_id) ? update_id : 0, chat_id };
}

// ───────────────────────────────────────────────────────────
// (A) Telegram ingress — Worker fetch handler
// ───────────────────────────────────────────────────────────
async function handle_telegram_ingress(
  request: Request,
  env: Env,
): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(JSON.stringify({ ok: false, error: "method_not_allowed" }), {
      status: 405,
      headers: { "Content-Type": "application/json", "Allow": "POST" },
    });
  }

  const ct = request.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().includes("application/json")) {
    return new Response(JSON.stringify({ ok: false, error: "bad_content_type" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const configured_secret = env.TELEGRAM_WEBHOOK_SECRET;
  if (!configured_secret || !configured_secret.trim()) {
    return new Response(
      JSON.stringify({ ok: false, error: "webhook_not_configured" }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    );
  }

  const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!header_eq(received, configured_secret)) {
    return new Response(JSON.stringify({ ok: false, error: "forbidden" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return new Response(JSON.stringify({ ok: false, error: "invalid_json" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return new Response(JSON.stringify({ ok: false, error: "malformed_payload" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const payload = raw as Record<string, unknown>;

  const meta = extract_telegram_meta(payload);
  if (!Number.isFinite(meta.update_id) || meta.update_id <= 0) {
    return new Response(JSON.stringify({ ok: false, error: "missing_update_id" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const occurred_at = now_iso();
  const envelope: TelegramUpdateEnvelope = {
    version: ENVELOPE_VERSION,
    kind: "telegram_update",
    event_id: make_telegram_event_id(meta.update_id),
    occurred_at,
    payload,
    _telegram_meta: meta,
  };

  try {
    await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest<any>);
  } catch (err) {
    const req_id = opaque_req_id();
    const raw = err instanceof Error ? `${err.name}: ${err.message}\n${err.stack ?? ""}` : String(err);
    console.error(`[${req_id}] handle_telegram_ingress enqueue_failed: ${mask_sensitive(raw)}`);
    return new Response(
      JSON.stringify({ ok: false, error: "enqueue_failed", req_id }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    );
  }

  return new Response(
    JSON.stringify({ ok: true, state: "enqueued", event_id: envelope.event_id }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

// ───────────────────────────────────────────────────────────
// (C) Queue consumer → OFFICIAL Cloudflare Container instance
// ───────────────────────────────────────────────────────────
async function deliver_envelope_to_container(
  env: Env,
  envelope: PasayQueueEnvelope,
): Promise<IngestAckResult> {
  // ND_RETURN FIX2 #1: require the real DurableObjectNamespace binding that
  // corresponds to the Container DO class declared above.
  if (!env.PASAY_CONTAINER) {
    return "retry";
  }
  const token = env.PASAY_CONTAINER_INGEST_TOKEN;
  if (!token || !token.trim()) {
    return "retry";
  }

  // ── OFFICIAL Container instance resolution ──────────────────────────
  // getContainer(env.PASAY_CONTAINER, instanceId) returns a bound container
  // instance handle whose .fetch() routes into the Dockerfile runtime on
  // defaultPort=8000.  We always use PASAY_CONTAINER_INSTANCE_ID =
  // "pasay-singleton" so we reuse the single global container across all
  // queue messages.
  // FIX11: Avoid `ReturnType<typeof getContainer>` here — with the generic
  // T bound, that unwraps into DurableObjectStub<T>.  When T resolves to
  // <any> via the Env binding declaration, the Rpc branded-graph types
  // recurse infinitely and trigger TS2589 ("Type instantiation is
  // excessively deep").  We declare the handle structurally instead; the
  // fetch() call below is the runtime contract we actually care about.
  type ContainerHandle = { fetch: (req: Request) => Promise<Response> };
  let containerHandle: ContainerHandle | undefined;
  try {
    // FIX11: Cast the binding to `any` BEFORE calling getContainer().  Even
    // with a structurally-typed ContainerHandle on the LHS, the generic
    // Rpc.FilterMethodsByProtocol branded graph inside `getContainer<T>`
    // still recurses infinitely when the input binding is declared as
    // DurableObjectNamespace<any>.  Coercing to `any` at the argument site
    // cuts off the generic type instantiation entirely.
    const bindingAny = env.PASAY_CONTAINER as unknown as any;
    containerHandle = getContainer(
      bindingAny,
      PASAY_CONTAINER_INSTANCE_ID,
    ) as unknown as ContainerHandle;
  } catch {
    return "retry";
  }
  if (!containerHandle || typeof (containerHandle as { fetch?: unknown }).fetch !== "function") {
    return "retry";
  }

  const absolute_url = `${PASAY_CONTAINER_ORIGIN}${CONTAINER_INGEST_PATH}`;
  let req: Request;
  try {
    req = new Request(absolute_url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [INGEST_AUTH_HEADER]: token,
      },
      body: JSON.stringify(envelope),
    });
  } catch {
    // Should be unreachable with a well-formed absolute URL above;
    // conservatively mark retry (transient build/env issue).
    return "retry";
  }

  let resp: Response;
  try {
    resp = await (containerHandle as any).fetch(req);
  } catch {
    return "retry";
  }

  const s = resp.status;
  if (s === 200 || s === 202 || s === 208) return "ack";
  if (s === 400 || s === 415 || s === 422) return "terminal";
  if (s === 401 || s === 403) return "retry";
  if (s >= 500 && s <= 599) return "retry";
  return "retry";
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === TELEGRAM_WEBHOOK_PATH) {
      return handle_telegram_ingress(request, env);
    }

    if (url.pathname === "/health" || url.pathname === "/healthz") {
      const queue_bound = typeof env.PASAY_QUEUE?.send === "function";
      // ND_RETURN FIX2 #1: Container binding is a DurableObjectNamespace
      // (env.PASAY_CONTAINER).  Health checks that the namespace exists.
      const container_bound = typeof env.PASAY_CONTAINER === "object" && env.PASAY_CONTAINER !== null;
      const secrets = {
        telegram_secret_configured: Boolean(env.TELEGRAM_WEBHOOK_SECRET?.trim()),
        container_ingest_token_configured: Boolean(env.PASAY_CONTAINER_INGEST_TOKEN?.trim()),
      };
      const body = {
        worker: "alive",
        architecture: "worker→queue→container→neon",
        bindings: { queue: queue_bound, container: container_bound },
        container_class: "PasayContainer",
        container_instance_id: PASAY_CONTAINER_INSTANCE_ID,
        container_origin: PASAY_CONTAINER_ORIGIN,
        secrets_configured: secrets,
        envelope_version: ENVELOPE_VERSION,
      };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ ok: false, error: "not_found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  },

  // (C) Queue consumer batch handler — explicit ack/retry semantics are
  // carried over from FIX1 (already correct — keep them unchanged).
  async queue(batch: MessageBatch<PasayQueueEnvelope>, env: Env, _ctx: ExecutionContext): Promise<void> {
    for (const msg of batch.messages) {
      const envelope = msg.body;
      if (
        typeof envelope !== "object" ||
        envelope === null ||
        envelope.version !== ENVELOPE_VERSION ||
        (envelope.kind !== "telegram_update" && envelope.kind !== "scheduled_job")
      ) {
        msg.ack();
        continue;
      }
      const result = await deliver_envelope_to_container(env, envelope as PasayQueueEnvelope);
      if (result === "ack") {
        msg.ack();
      } else if (result === "terminal") {
        msg.ack();
      } else {
        msg.retry();
      }
    }
  },

  // (F) Cron scheduled() → same PASAY_QUEUE as Telegram ingress.
  // wrangler.toml [triggers] crons = ["*/5 * * * *"] fires every 5 minutes.
  async scheduled(controller: ScheduledController, env: Env, _ctx: ExecutionContext): Promise<void> {
    const occurred_at = now_iso();
    const cron = controller.cron ?? "unscheduled";
    const job_name = "pasay_heartbeat";
    const envelope: ScheduledJobEnvelope = {
      version: ENVELOPE_VERSION,
      kind: "scheduled_job",
      event_id: make_scheduled_event_id(job_name, occurred_at),
      occurred_at,
      payload: {
        job_name,
        scheduled_at: occurred_at,
        params: { cron_expression: cron },
      },
    };
    try {
      await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest<any>);
    } catch {
      // Best effort; Cron will fire again next window.
    }
  },
};
