/**
 * PASAY Cloudflare Worker — single unified entry for:
 *   (A) Telegram webhook ingress → enqueue
 *   (C) Queue consumer          → official Cloudflare Container instance
 *   (F) Cron scheduled()        → enqueue (same queue, same container)
 *
 * Invariants:
 *   - Worker NEVER runs business logic.
 *   - Worker ONLY validates ingress + envelopes + calls Container binding.
 *   - One queue. One container. One FastAPI app. One DB boundary.
 *   - Container is the official @cloudflare/containers PasayContainer (singleton
 *     instance id "pasay-singleton"). Wrangler manages the Durable Object class.
 */
import {
  ENVELOPE_VERSION,
  make_scheduled_event_id,
  make_telegram_event_id,
  type PasayQueueEnvelope,
} from "./envelope";
import { Container, getContainer } from "@cloudflare/containers";

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

interface Env {
  PASAY_QUEUE: Queue;
  PASAY_CONTAINER?: DurableObjectNamespace;
  TELEGRAM_WEBHOOK_SECRET?: string;
  PASAY_CONTAINER_INGEST_TOKEN?: string;
  DATABASE_URL?: string;
  DATABASE_URL_UNPOOLED?: string;
  TELEGRAM_BOT_TOKEN?: string;
}

function now_iso(): string {
  return new Date().toISOString();
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

function extract_telegram_meta(
  raw: Record<string, unknown>,
): { update_id: number; chat_id?: number } {
  const update_id = Number(raw["update_id"]);
  let chat_id: number | undefined;
  for (const key of ["message", "edited_message", "callback_query", "channel_post", "edited_channel_post"]) {
    const node = raw[key] as Record<string, unknown> | undefined;
    if (node && typeof node === "object") {
      const chat = (node as Record<string, unknown>)["chat"] as
        | Record<string, unknown>
        | undefined;
      if (chat && typeof chat["id"] === "number") {
        chat_id = chat["id"];
        break;
      }
    }
  }
  return { update_id: Number.isFinite(update_id) ? update_id : 0, chat_id };
}

// (A) Telegram ingress
async function handle_telegram_ingress(
  request: Request,
  env: Env,
): Promise<Response> {
  if (request.method !== "POST") {
    return json(405, { ok: false, error: "method_not_allowed" }, { Allow: "POST" });
  }
  const ct = request.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().includes("application/json")) {
    return json(400, { ok: false, error: "bad_content_type" });
  }
  const configured_secret = env.TELEGRAM_WEBHOOK_SECRET ?? "";
  if (!configured_secret.trim()) {
    return json(401, { ok: false, error: "webhook_not_configured" });
  }
  const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!header_eq(received, configured_secret)) {
    return json(403, { ok: false, error: "forbidden" });
  }
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return json(400, { ok: false, error: "invalid_json" });
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return json(400, { ok: false, error: "malformed_payload" });
  }
  const payload = raw as Record<string, unknown>;
  const meta = extract_telegram_meta(payload);
  if (!Number.isFinite(meta.update_id) || meta.update_id <= 0) {
    return json(400, { ok: false, error: "missing_update_id" });
  }
  const occurred_at = now_iso();
  const envelope: PasayQueueEnvelope = {
    version: ENVELOPE_VERSION,
    kind: "telegram_update",
    event_id: make_telegram_event_id(meta.update_id),
    occurred_at,
    payload,
    _telegram_meta: meta,
  };
  try {
    await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest);
  } catch (err) {
    return json(503, { ok: false, error: "enqueue_failed", detail: String(err) });
  }
  return json(200, { ok: true, state: "enqueued", event_id: envelope.event_id });
}

// (C) Queue consumer → Container
async function deliver_envelope_to_container(
  env: Env,
  envelope: PasayQueueEnvelope,
): Promise<"ack" | "retry" | "terminal"> {
  if (!env.PASAY_CONTAINER) return "retry";
  const token = env.PASAY_CONTAINER_INGEST_TOKEN;
  if (!token || !token.trim()) return "retry";
  let handle: { fetch: (req: Request) => Promise<Response> } | undefined;
  try {
    handle = getContainer(env.PASAY_CONTAINER as unknown as any, PASAY_CONTAINER_INSTANCE_ID) as unknown as {
      fetch: (req: Request) => Promise<Response>;
    };
  } catch {
    return "retry";
  }
  if (!handle) return "retry";
  const url = `${PASAY_CONTAINER_ORIGIN}${CONTAINER_INGEST_PATH}`;
  const req = new Request(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [INGEST_AUTH_HEADER]: token,
    },
    body: JSON.stringify(envelope),
  });
  let resp: Response;
  try {
    resp = await (handle as any).fetch(req);
  } catch {
    return "retry";
  }
  const s = resp.status;
  if (s === 200 || s === 202 || s === 208) return "ack";
  if (s === 400 || s === 415 || s === 422) return "terminal";
  return "retry";
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extra },
  });
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === TELEGRAM_WEBHOOK_PATH) {
      return handle_telegram_ingress(request, env);
    }
    if (url.pathname === "/health" || url.pathname === "/healthz") {
      return json(200, {
        worker: "alive",
        architecture: "worker→queue→container→neon",
        bindings: {
          queue: typeof env.PASAY_QUEUE?.send === "function",
          container: typeof env.PASAY_CONTAINER === "object" && env.PASAY_CONTAINER !== null,
        },
        container_class: "PasayContainer",
        container_instance_id: PASAY_CONTAINER_INSTANCE_ID,
        envelope_version: ENVELOPE_VERSION,
      });
    }
    return json(404, { ok: false, error: "not_found" });
  },

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
      if (result === "ack" || result === "terminal") msg.ack();
      else msg.retry();
    }
  },

  async scheduled(controller: ScheduledController, env: Env, _ctx: ExecutionContext): Promise<void> {
    const occurred_at = now_iso();
    const job_name = "pasay_heartbeat";
    const envelope: PasayQueueEnvelope = {
      version: ENVELOPE_VERSION,
      kind: "scheduled_job",
      event_id: make_scheduled_event_id(job_name, occurred_at),
      occurred_at,
      payload: {
        job_name,
        scheduled_at: occurred_at,
        params: { cron_expression: controller.cron ?? "unscheduled" },
      },
    };
    try {
      await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest);
    } catch {
      // best-effort; cron will fire again next window
    }
  },
};
