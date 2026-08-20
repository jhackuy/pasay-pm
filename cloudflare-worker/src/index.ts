/**
 * Pasay Cloudflare Worker — single unified entry for:
 *   (A) Telegram webhook ingress → enqueue
 *   (C) Queue consumer          → Container native binding
 *   (F) Cron scheduled()        → enqueue (same queue, same container)
 *
 * Architectural invariants (Issue #31 — PASAY-TASK-011):
 *   - Worker NEVER runs business logic (no PTB, no DB write, no LLM).
 *   - Worker ONLY validates ingress + envelopes + calls Container binding.
 *   - There is ONE queue. ONE container. ONE FastAPI app. ONE DB boundary.
 */
import {
  ENVELOPE_VERSION,
  make_scheduled_event_id,
  make_telegram_event_id,
  type PasayQueueEnvelope,
  type ScheduledJobEnvelope,
  type TelegramUpdateEnvelope,
} from "./envelope";

type BindingQueue = Queue;

interface Env {
  PASAY_QUEUE: BindingQueue;
  // Cloudflare Container native binding (populated by wrangler at deploy).
  // Falls back to undefined in dev / misconfigured deployments.
  PASAY_CONTAINER?: {
    fetch: (req: Request) => Promise<Response>;
  };
  // Secret env vars (wrangler secret put)
  TELEGRAM_WEBHOOK_SECRET?: string;
  PASAY_CONTAINER_INGEST_TOKEN?: string;
}

const TELEGRAM_WEBHOOK_PATH = "/telegram/webhook";
const CONTAINER_INGEST_PATH = "/internal/ingest";
const INGEST_AUTH_HEADER = "X-Pasay-Ingest-Token";

type IngestAckResult = "ack" | "retry" | "terminal";

function now_iso(): string {
  return new Date().toISOString();
}

function header_eq(a: string | null, b: string): boolean {
  if (!a) return !b;
  // Timing-safe length check first; byte-by-byte comparison after.
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
  // Minimal extraction ONLY for observability — NEVER interpret intent.
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
  // ── Gate 1: method ──
  if (request.method !== "POST") {
    return new Response(JSON.stringify({ ok: false, error: "method_not_allowed" }), {
      status: 405,
      headers: { "Content-Type": "application/json", "Allow": "POST" },
    });
  }

  // ── Gate 2: content type ──
  const ct = request.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().includes("application/json")) {
    return new Response(JSON.stringify({ ok: false, error: "bad_content_type" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // ── Gate 3: secret configured (fail closed) ──
  const configured_secret = env.TELEGRAM_WEBHOOK_SECRET;
  if (!configured_secret || !configured_secret.trim()) {
    return new Response(
      JSON.stringify({ ok: false, error: "webhook_not_configured" }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    );
  }

  // ── Gate 4: secret match ──
  const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!header_eq(received, configured_secret)) {
    return new Response(JSON.stringify({ ok: false, error: "forbidden" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }

  // ── Gate 5: parse JSON ──
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

  // ── Gate 6: update_id present ──
  const meta = extract_telegram_meta(payload);
  if (!Number.isFinite(meta.update_id) || meta.update_id <= 0) {
    return new Response(JSON.stringify({ ok: false, error: "missing_update_id" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // ── Build envelope + enqueue ──
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
    // Enqueue failed → return explicit failure to Telegram so it retries.
    // Must NOT forge success.
    const msg = err instanceof Error ? err.message : String(err);
    return new Response(
      JSON.stringify({ ok: false, error: "enqueue_failed", detail: msg }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    );
  }

  // Enqueue OK → quick 2xx. Telegram considers delivery accepted.
  return new Response(
    JSON.stringify({ ok: true, state: "enqueued", event_id: envelope.event_id }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

// ───────────────────────────────────────────────────────────
// (C) Queue consumer → Cloudflare Container native binding
// ───────────────────────────────────────────────────────────
async function deliver_envelope_to_container(
  env: Env,
  envelope: PasayQueueEnvelope,
): Promise<IngestAckResult> {
  if (!env.PASAY_CONTAINER) {
    // Operator has not bound the container. Treat as TEMPORARY so Queue retries
    // after binding is fixed; do NOT permanently drop messages.
    return "retry";
  }
  const token = env.PASAY_CONTAINER_INGEST_TOKEN;
  if (!token) {
    // Misconfigured but not malformed — temporary until operator sets secret.
    return "retry";
  }

  const req = new Request(CONTAINER_INGEST_PATH, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [INGEST_AUTH_HEADER]: token,
    },
    body: JSON.stringify(envelope),
  });

  let resp: Response;
  try {
    resp = await env.PASAY_CONTAINER.fetch(req);
  } catch {
    // Container binding threw — connectivity / startup transient.
    return "retry";
  }

  // ── Classify by status code (Container contract) ──
  //   200 / 202 / 208   → accepted (idempotent duplicate counts as accepted) → ack
  //   400 / 415 / 422   → permanently malformed → terminal (drop + maybe DLQ)
  //   401 / 403         → token issue (operator fix needed) → temporary retry
  //   5xx               → container runtime transient → retry
  const s = resp.status;
  if (s === 200 || s === 202 || s === 208) return "ack";
  if (s === 400 || s === 415 || s === 422) return "terminal";
  if (s === 401 || s === 403) return "retry";
  if (s >= 500 && s <= 599) return "retry";
  // Unknown class: be conservative and retry.
  return "retry";
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // ── (A) Telegram webhook ──
    if (url.pathname === TELEGRAM_WEBHOOK_PATH) {
      return handle_telegram_ingress(request, env);
    }

    // ── Minimal health / self-test ── (Scope G: Architecture Truth)
    if (url.pathname === "/health" || url.pathname === "/healthz") {
      const queue_bound = typeof env.PASAY_QUEUE?.send === "function";
      const container_bound = typeof env.PASAY_CONTAINER?.fetch === "function";
      const secrets = {
        telegram_secret_configured: Boolean(env.TELEGRAM_WEBHOOK_SECRET?.trim()),
        container_ingest_token_configured: Boolean(env.PASAY_CONTAINER_INGEST_TOKEN?.trim()),
      };
      const body = {
        worker: "alive",
        architecture: "worker→queue→container→neon",
        bindings: { queue: queue_bound, container: container_bound },
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

  // ── (C) Queue consumer batch handler ──
  async queue(batch: MessageBatch<PasayQueueEnvelope>, env: Env, _ctx: ExecutionContext): Promise<void> {
    for (const msg of batch.messages) {
      const envelope = msg.body;
      // Validate envelope kind/version before handing to container.
      if (
        typeof envelope !== "object" ||
        envelope === null ||
        envelope.version !== ENVELOPE_VERSION ||
        (envelope.kind !== "telegram_update" && envelope.kind !== "scheduled_job")
      ) {
        // Permanently malformed envelope: ack into DLQ-side handling, don't hammer container.
        msg.ack();
        continue;
      }
      const result = await deliver_envelope_to_container(env, envelope as PasayQueueEnvelope);
      if (result === "ack") msg.ack();
      else if (result === "terminal") msg.ack();
      // "retry" is the default: call neither ack() nor retry() and Queue will retry.
    }
  },

  // ── (F) Cron scheduled() → same queue → same container ──
  async scheduled(controller: ScheduledController, env: Env, _ctx: ExecutionContext): Promise<void> {
    const occurred_at = now_iso();
    const cron = controller.cron ?? "unscheduled";
    // Only one envelope today: a heartbeat/minimal "wake" job.
    // Future reminders/digests/task-wakeups add NEW jobs; they reuse this EXACT path.
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
