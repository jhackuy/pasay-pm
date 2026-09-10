/**
 * PASAY Cloudflare Worker — interactive Telegram path bypasses the Queue.
 *
 * Architecture (Issue #119 P0 latency):
 *   * Interactive Telegram updates: webhook → direct Container
 *     /internal/ingest forward (no Queue) — eliminates the
 *     max_batch_timeout=1s scheduling + queue consumer overhead that was
 *     dominating the >10s tap→reply round-trip.
 *   * Scheduled / background / retry work: scheduled cron → PASAY_QUEUE →
 *     queue consumer → Container /internal/ingest (unchanged contract).
 *   * Direct-forward transient failure: fallback enqueue to PASAY_QUEUE so
 *     the queue consumer can pick it up; idempotency is preserved because
 *     the Container's update_id claim is the single source of truth.
 *
 * Webhook ACK contract (Issue #119):
 *   * Direct-forward ack (Container 2xx)                => 2xx to Telegram
 *   * Direct-forward transient + PASAY_QUEUE.send OK    => 2xx to Telegram
 *   * Direct-forward transient + PASAY_QUEUE.send FAIL  => non-2xx to Telegram
 *
 * Returning 2xx on the queue-fallback path is REQUIRED by the Telegram
 * webhook ACK contract: once the envelope has been durably accepted by
 * the Queue, the Container's update_id claim guarantees idempotent
 * processing — Telegram must NOT retry a delivery the Queue already owns.
 * A prior regression returned 503 here, which surfaced as
 * ``getWebhookInfo.last_error_message = "Wrong response from the webhook:
 * 503 Service Unavailable"`` even though every update had been
 * successfully enqueued.
 *
 * Business logic belongs in the FastAPI application, never in this Worker.
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
// Issue #119 P0 latency: per-update correlation id + arrival timestamp.
// Worker stamps the request at ingress (T0 = now_iso), forwards the same
// correlation to the Container via X-Pasay-Trace-Id + X-Pasay-Trace-T0.
// The Container log emits the matching fields so operator-side grep can
// compute Worker→Container→PTB→Telegram hop-by-hop latency without any
// shared clock (both sides emit monotonic offset relative to T0).
const TRACE_HEADER = "X-Pasay-Trace-Id";
const TRACE_T0_HEADER = "X-Pasay-Trace-T0";
const TRACE_PATH_DIRECT = "direct";
const TRACE_PATH_ENQUEUED_FALLBACK = "enqueued_fallback";

export class PasayContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  envVars: Record<string, string>;

  constructor(ctx: any = {}, env: Env = {} as Env, options?: any) {
    super(ctx, env, options);
    // Issue #119 P0 Telegram-runtime: the bot's pasay_bot.config.Settings
    // loader reads the env vars under their `PASSAY_*` names (e.g.
    // `PASSAY_TG_BOT_TOKEN`, `PASSAY_API_KEY`, `PASSAY_JOB_API_KEY`). Until
    // these were not forwarded into the Container, the bot was built with an
    // empty `pasay_tg_bot_token` (PTB ApplicationBuilder fell back to
    // "0:UNSET"), every `bot.send_message` call hit api.telegram.org/bot
    // 0:UNSET/sendMessage, Telegram returned InvalidToken, the handler
    // failed PERMANENTLY, the Worker marked the update `failed`, Telegram
    // stopped replaying — the user-visible result was "Owner previously got
    // no visible replies". Forwarding the Worker secret `TELEGRAM_BOT_TOKEN`
    // as both names (`TELEGRAM_BOT_TOKEN` for back-compat and the new
    // `PASSAY_TG_BOT_TOKEN` that the bot actually reads) closes the loop
    // without requiring any new secret provisioning. The other bot env vars
    // (API key, job key, optional URLs, archive id, timeout, …) are sourced
    // from dedicated Worker secrets so the operator can provision them with
    // `wrangler secret put <NAME>`. The pasay-telegram-bot runtime
    // defaults already cover everything that has a default (pasay_api_base,
    // pasay_http_timeout_seconds, pasay_mini_app_url, archive_chat_id,
    // admin_api_key); see pasay_bot/config.py::DEFAULT_MINI_APP_URL for the
    // canonical Pages origin.
    const tg_token = env.TELEGRAM_BOT_TOKEN ?? "";
    this.envVars = {
      DATABASE_URL: env.DATABASE_URL ?? "",
      DATABASE_URL_UNPOOLED: env.DATABASE_URL_UNPOOLED ?? "",
      // PTB token is sourced under BOTH names — existing secret covers both.
      TELEGRAM_BOT_TOKEN: tg_token,
      PASSAY_TG_BOT_TOKEN: tg_token,
      // Backend-bound keys / endpoints (operator provisions these as Worker
      // secrets; default to "" so an unprovisioned bot fails closed on its
      // first API call instead of silently impersonating anyone).
      PASSAY_API_BASE: env.PASSAY_API_BASE ?? "",
      PASSAY_API_KEY: env.PASSAY_API_KEY ?? "",
      PASSAY_ADMIN_API_KEY: env.PASSAY_ADMIN_API_KEY ?? "",
      PASSAY_JOB_API_KEY: env.PASSAY_JOB_API_KEY ?? "",
      // Issue #119 P0 (independent review follow-up): the SYSTEM
      // scheduled-job client must be bound to a single canonical
      // organization. The Worker secret `PASSAY_SYSTEM_ORG_ID` is
      // forwarded as-is; the bot's `_build_job_api` keeps the jobs
      // disabled when this is empty / 0 (fail closed).
      PASSAY_SYSTEM_ORG_ID: env.PASSAY_SYSTEM_ORG_ID ?? "",
      // Optional / with-defaults (worker secret can override; bot keeps its
      // own defaults so an unprovisioned Worker still boots).
      PASSAY_HTTP_TIMEOUT_SECONDS: env.PASSAY_HTTP_TIMEOUT_SECONDS ?? "",
      PASSAY_ARCHIVE_CHAT_ID: env.PASSAY_ARCHIVE_CHAT_ID ?? "",
      PASSAY_MINI_APP_URL: env.PASSAY_MINI_APP_URL ?? "",
      PASSAY_MINI_APP_OWNER_TELEGRAM_IDS: env.PASSAY_MINI_APP_OWNER_TELEGRAM_IDS ?? "",
      // Internal ingestion boundary (Worker → Container auth) — unchanged.
      TELEGRAM_WEBHOOK_SECRET: env.TELEGRAM_WEBHOOK_SECRET ?? "",
      CONTAINER_INGEST_TOKEN: env.PASAY_CONTAINER_INGEST_TOKEN ?? "",
      PASAY_RUNTIME_MODE: "cloudflare-container",
    };
  }
}

interface Env {
  PASAY_QUEUE: Queue;
  PASAY_CONTAINER?: DurableObjectNamespace<PasayContainer>;
  TELEGRAM_WEBHOOK_SECRET?: string;
  PASAY_CONTAINER_INGEST_TOKEN?: string;
  DATABASE_URL?: string;
  DATABASE_URL_UNPOOLED?: string;
  TELEGRAM_BOT_TOKEN?: string;
  // Issue #119 P0 Telegram-runtime: the bot's pasay_bot.config.Settings
  // loader reads these under their `PASSAY_*` names. Provisioned via
  // `wrangler secret put` and forwarded verbatim into the Container.
  PASSAY_API_BASE?: string;
  PASSAY_API_KEY?: string;
  PASSAY_ADMIN_API_KEY?: string;
  PASSAY_JOB_API_KEY?: string;
  // Issue #119 P0 follow-up: SYSTEM scheduled-job org binding.
  PASSAY_SYSTEM_ORG_ID?: string;
  PASSAY_HTTP_TIMEOUT_SECONDS?: string;
  PASSAY_ARCHIVE_CHAT_ID?: string;
  PASSAY_MINI_APP_URL?: string;
  PASSAY_MINI_APP_OWNER_TELEGRAM_IDS?: string;
}

function now_iso(): string {
  return new Date().toISOString();
}

/** Opaque request id used to correlate enqueue failures without leaking internals. */
function make_req_id(): string {
  // Web Crypto API is available in both Cloudflare Workers and Node >= 19.
  return `r_${crypto.randomUUID()}`;
}

function header_eq(a: string | null, b: string): boolean {
  if (!a) return !b;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Redact configured secret values + structurally redact key/value pairs and
 * bare long-hex tokens before server-side logging.
 *
 * Layered strategy (each layer is idempotent and side-effect safe):
 *   1. Value-based: redact any configured secret verbatim (back-compat).
 *   2. Unquoted KEY=value style (uppercase KEY, value without hyphens/colons
 *      so canonical UUIDs / ISO timestamps survive).
 *   3. Double-quoted JSON "KEY":"value" style (uppercase KEY).
 *   4. Single-quoted 'KEY':'value' style (uppercase KEY).
 *   5. Bare long-hex tokens (>= 20 contiguous hex chars, word-bounded so
 *      hyphenated UUIDs and colon-separated timestamps are skipped).
 *
 * `env` is OPTIONAL so unit tests can exercise the structural layer without
 * injecting a full Worker environment.
 */
export function mask_sensitive(input: unknown, env?: Partial<Env>): string {
  let text = input instanceof Error ? input.stack ?? input.message : String(input);
  const secrets = [
    env?.DATABASE_URL,
    env?.DATABASE_URL_UNPOOLED,
    env?.TELEGRAM_BOT_TOKEN,
    env?.TELEGRAM_WEBHOOK_SECRET,
    env?.PASAY_CONTAINER_INGEST_TOKEN,
  ].filter((value): value is string => Boolean(value && value.trim()));
  for (const secret of secrets) text = text.split(secret).join("[REDACTED]");

  // 2. Unquoted KEY=value (uppercase KEY; value must not contain hyphens or
  //    colons so canonical hyphenated UUIDs and ISO timestamps survive).
  text = text.replace(
    /([A-Z_][A-Z0-9_]*)=([^\s,;|'"\]\-:]+)/g,
    "$1=***",
  );

  // 3. Double-quoted JSON "KEY":"value" (uppercase KEY).
  text = text.replace(
    /("([A-Z_][A-Z0-9_]*)"\s*:\s*)"([^"\\]*(?:\\.[^"\\]*)*)"/g,
    '$1"***"',
  );

  // 4. Single-quoted 'KEY':'value' (uppercase KEY).
  text = text.replace(
    /('([A-Z_][A-Z0-9_]*)'\s*:\s*)'([^'\\]*(?:\\.[^'\\]*)*)'/g,
    "$1'***'",
  );

  // 5. Bare long-hex tokens (>= 20 contiguous hex chars, word-bounded).
  //    Hyphenated UUIDs and colon-separated timestamps are excluded by the
  //    word-boundary + all-hex requirement.
  text = text.replace(/\b([0-9a-fA-F]{20,})\b/g, "***");

  return text;
}

function log_error(scope: string, err: unknown, env: Env): void {
  console.error(`[pasay-worker:${scope}] ${mask_sensitive(err, env)}`);
}

function extract_telegram_meta(raw: Record<string, unknown>): { update_id: number; chat_id?: number } {
  const update_id = Number(raw["update_id"]);
  let chat_id: number | undefined;
  for (const key of ["message", "edited_message", "callback_query", "channel_post", "edited_channel_post"]) {
    const node = raw[key] as Record<string, unknown> | undefined;
    if (node && typeof node === "object") {
      const chat = node["chat"] as Record<string, unknown> | undefined;
      if (chat && typeof chat["id"] === "number") {
        chat_id = chat["id"];
        break;
      }
    }
  }
  return { update_id: Number.isFinite(update_id) ? update_id : 0, chat_id };
}

async function handle_telegram_ingress(request: Request, env: Env): Promise<Response> {
  // Issue #119 P0 WARM-PATH TRACE: stamp the worker-arrival wall-clock at
  // the very first line of the ingress handler so the structured
  // ``pasay_worker_latency`` log line can publish both the Worker-internal
  // ms and the Container-fetch ms from a single monotonic reading. The
  // Container side publishes its own ``pasay_ingest_latency`` with the
  // SAME ``trace_id`` (= ``envelope.event_id``) so operator grep can join
  // the two records without a shared clock. ``mask_sensitive`` is applied
  // unconditionally so a misconfigured env can never leak bot token /
  // webhook secret / ingest token into the structured log line.
  const worker_arrival_ms = Date.now();
  const worker_arrival_iso = now_iso();
  if (request.method !== "POST") return json(405, { ok: false, error: "method_not_allowed" }, { Allow: "POST" });
  const ct = request.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().includes("application/json")) return json(400, { ok: false, error: "bad_content_type" }, { Allow: "POST" });
  const configured_secret = env.TELEGRAM_WEBHOOK_SECRET ?? "";
  if (!configured_secret.trim()) return json(401, { ok: false, error: "webhook_not_configured" }, { Allow: "POST" });
  const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!header_eq(received, configured_secret)) return json(403, { ok: false, error: "forbidden" }, { Allow: "POST" });

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return json(400, { ok: false, error: "invalid_json" }, { Allow: "POST" });
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return json(400, { ok: false, error: "malformed_payload" }, { Allow: "POST" });
  const payload = raw as Record<string, unknown>;
  const meta = extract_telegram_meta(payload);
  if (!Number.isFinite(meta.update_id) || meta.update_id <= 0) return json(400, { ok: false, error: "missing_update_id" }, { Allow: "POST" });

  const occurred_at = now_iso();
  const req_id = make_req_id();
  const trace_id = make_telegram_event_id(meta.update_id);
  const envelope: PasayQueueEnvelope = {
    version: ENVELOPE_VERSION,
    kind: "telegram_update",
    event_id: trace_id,
    occurred_at,
    payload,
    _telegram_meta: meta,
  };
  // Issue #119 P0 LATENCY: the interactive Telegram fast path now goes
  // Worker → Container /internal/ingest DIRECTLY (synchronous). The
  // Cloudflare Queue + queue consumer schedule (max_batch_timeout=1s)
  // adds ~1–3s of latency on top of the cold-start window the Container
  // already pays once; routing interactive user taps through it makes
  // the menu tap → visible reply round-trip visibly slow (Owner measured
  // >10s warm p50). The Queue stays for scheduled / background /
  // retry work where the eventual-consistency semantics are correct.
  //
  // Idempotency: the Container /internal/ingest endpoint is the SINGLE
  // owner of Telegram update_id dedup (claim_update_or_short_circuit in
  // app/services/telegram_webhook.py). Both paths reach the same
  // handler, so direct + fallback enqueue are idempotent under the same
  // update_id. Telegram's webhook redelivery contract is preserved
  // because we return 2xx only when the envelope has been either
  // synchronously accepted by the Container OR durably enqueued to the
  // Queue (which guarantees eventual delivery to the same dedup layer).
  //
  // Webhook ACK contract (Issue #119 — CURRENT fix):
  //   * Direct-forward ack (Container 2xx)        => 2xx to Telegram
  //   * Direct-forward transient + queue send OK => 2xx to Telegram
  //   * Direct-forward transient + queue send FAIL => non-2xx to Telegram
  //
  // Returning 2xx on the fallback path is CORRECT: the envelope has
  // already been accepted for processing (Queue is the durable
  // delivery contract), so Telegram must NOT be told to redeliver a
  // duplicate. Previously this branch returned 503, which made
  // Telegram record a webhook failure and retry an update that was
  // already queued for processing — observable as
  // ``Wrong response from the webhook: 503 Service Unavailable`` in
  // ``getWebhookInfo.last_error_message`` even though the Queue had
  // already taken ownership.
  //
  // Fallback: a transient direct-forward failure (5xx, container fetch
  // throws, container not bound) routes to PASAY_QUEUE.send so the
  // existing queue consumer can pick it up. If the enqueue ALSO fails
  // (rare: Queue availability breach) we return a hard non-2xx so
  // Telegram retries via its own redelivery contract (no PTB state
  // mutated because the Container never received the envelope).
  const TELEGRAM_ACK_QUEUE_FALLBACK = 200;  // ACK once durable Queue accepts the envelope
  const direct_result = await direct_forward_envelope_to_container(env, envelope, {
    trace_id: envelope.event_id,
    trace_t0: occurred_at,
  });
  if (direct_result.outcome === "ack") {
    // Fast path success: latency-critical user-visible Telegram update
    // hit the Container synchronously without queueing. The reply
    // (handler sendMessage) reaches Telegram via the Container's HTTP
    // response, so the user sees it as soon as the handler completes.
    const merged: Record<string, unknown> =
      typeof direct_result.body === "object" && direct_result.body !== null
        ? { ...(direct_result.body as Record<string, unknown>) }
        : { ok: true };
    merged.event_id = envelope.event_id;
    merged.req_id = req_id;
    merged.path = TRACE_PATH_DIRECT;
    merged.worker_ingress_ms = direct_result.worker_ingress_ms;
    log_worker_latency({
      event_id: envelope.event_id,
      path: TRACE_PATH_DIRECT,
      outcome: "ack",
      status: direct_result.status,
      worker_arrival_iso,
      worker_arrival_ms,
      worker_total_ms: Date.now() - worker_arrival_ms,
      container_fetch_ms: direct_result.worker_ingress_ms,
      env,
    });
    return json(direct_result.status, merged, { Allow: "POST" });
  }
  // Direct forward failed (transient). Fall back to Queue: the queue
  // consumer will retry with the SAME envelope (event_id = tg:update_id),
  // preserving idempotency on the Container side.
  let enqueued = false;
  try {
    await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest);
    enqueued = true;
  } catch (err) {
    log_error("fallback-enqueue", err, env);
  }
  if (enqueued) {
    log_worker_latency({
      event_id: envelope.event_id,
      path: TRACE_PATH_ENQUEUED_FALLBACK,
      outcome: "enqueued_fallback",
      status: TELEGRAM_ACK_QUEUE_FALLBACK,
      worker_arrival_iso,
      worker_arrival_ms,
      worker_total_ms: Date.now() - worker_arrival_ms,
      container_fetch_ms: direct_result.worker_ingress_ms,
      env,
    });
    // ACK contract (Issue #119): the envelope has been durably accepted
    // by the queue fallback send, so return a 2xx acknowledgement to
    // Telegram to prevent it from treating this delivery as failed
    // and retrying an update that the queue consumer will pick up.
    // Preserving ``path: enqueued_fallback`` + ``state: enqueued_fallback``
    // keeps operator-side grep on the structured log line + JSON body
    // stable so watchdog / SRE dashboards continue to identify the
    // fallback path. The Container's update_id claim layer guarantees
    // idempotency: even if Telegram DOES redeliver (network-level
    // race), the second arrival will be deduplicated by
    // ``claim_update_or_short_circuit`` before any handler runs.
    return json(
      TELEGRAM_ACK_QUEUE_FALLBACK,
      {
        ok: true,
        state: "enqueued_fallback",
        event_id: envelope.event_id,
        req_id,
        path: TRACE_PATH_ENQUEUED_FALLBACK,
        delivery: "queue",
        worker_ingress_ms: direct_result.worker_ingress_ms,
      },
      { Allow: "POST" },
    );
  }
  // Both direct and enqueue failed: hard 503, Telegram retries via its
  // own redelivery contract (no PTB state mutated because the Container
  // never received the envelope).
  log_worker_latency({
    event_id: envelope.event_id,
    path: "enqueue_failed",
    outcome: "enqueue_failed",
    status: 503,
    worker_arrival_iso,
    worker_arrival_ms,
    worker_total_ms: Date.now() - worker_arrival_ms,
    container_fetch_ms: direct_result.worker_ingress_ms,
    env,
  });
  return json(
    503,
    {
      ok: false,
      error: "enqueue_failed",
      req_id,
      event_id: envelope.event_id,
      path: "enqueue_failed",
      worker_ingress_ms: direct_result.worker_ingress_ms,
    },
    { Allow: "POST" },
  );
}

/**
 * Issue #119 P0 WARM-PATH TRACE — emit ONE structured ``pasay_worker_latency``
 * log line per interactive Telegram ingress so operator-side grep can compute
 * Worker→Container→PTB→Telegram hop-by-hop latency without a shared clock.
 *
 * Field budget (all values fit on a single line, no JSON, no secrets):
 *   * ``event_id``        = ``envelope.event_id`` = ``tg:{update_id}``
 *   * ``path``            = ``direct`` / ``enqueued_fallback`` / ``enqueue_failed``
 *   * ``outcome``         = terminal outcome tag (``ack`` / ``enqueued_fallback`` / ``enqueue_failed``)
 *   * ``status``          = HTTP status returned to Telegram
 *   * ``worker_arrival_iso`` = ISO-8601 of the request arriving at Worker
 *   * ``worker_arrival_ms``  = epoch ms of the Worker arrival
 *   * ``worker_total_ms``    = wall-clock from Worker arrival to this log
 *   * ``container_fetch_ms`` = wall-clock from Worker arrival to Container.fetch() return
 *
 * Redaction: ``mask_sensitive`` is applied to the rendered line so any
 * configured secret (bot token, webhook secret, ingest token, DATABASE_URL,
 * bare long-hex tokens) can never leak through this observability surface
 * even if a future refactor accidentally adds a sensitive field.
 */
function log_worker_latency(args: {
  event_id: string;
  path: string;
  outcome: string;
  status: number;
  worker_arrival_iso: string;
  worker_arrival_ms: number;
  worker_total_ms: number;
  container_fetch_ms: number;
  env: Env;
}): void {
  const line =
    `pasay_worker_latency trace_id=${args.event_id} path=${args.path} `
    + `outcome=${args.outcome} status=${args.status} `
    + `worker_arrival_iso=${args.worker_arrival_iso} `
    + `worker_arrival_ms=${args.worker_arrival_ms} `
    + `worker_total_ms=${args.worker_total_ms} `
    + `container_fetch_ms=${args.container_fetch_ms}`;
  console.log(`[pasay-worker:warm-trace] ${mask_sensitive(line, args.env)}`);
}

interface DirectForwardResult {
  outcome: "ack" | "transient" | "permanent";
  status: number;
  body: unknown;
  error?: string;
  /** Wall-clock ms from Worker ingress to Container fetch return (success OR
   *  failure); used for the latency telemetry surface. */
  worker_ingress_ms: number;
}

/**
 * Issue #119 P0 latency: synchronous Worker → Container /internal/ingest
 * forward used by the INTERACTIVE Telegram webhook fast path. Returns the
 * raw Container response (status + parsed body) so the caller can decide
 * whether to enqueue as a fallback or return directly to Telegram.
 *
 * Trace propagation: the caller's `trace_id` and `trace_t0` are stamped on
 * the forwarded request so the Container-side log line can compute the
 * Worker→Container hop latency without a shared clock.
 *
 * Classification mirrors `deliver_envelope_to_container` (queue consumer)
 * exactly so the two paths are idempotent and observable from the same
 * telemetry contract.
 */
async function direct_forward_envelope_to_container(
  env: Env,
  envelope: PasayQueueEnvelope,
  trace: { trace_id: string; trace_t0: string },
): Promise<DirectForwardResult> {
  const t_start = Date.now();
  if (!env.PASAY_CONTAINER) {
    return {
      outcome: "transient",
      status: 503,
      body: { ok: false, error: "container_unbound" },
      error: "container_unbound",
      worker_ingress_ms: Date.now() - t_start,
    };
  }
  const token = env.PASAY_CONTAINER_INGEST_TOKEN;
  if (!token || !token.trim()) {
    return {
      outcome: "transient",
      status: 503,
      body: { ok: false, error: "ingest_not_configured" },
      error: "ingest_not_configured",
      worker_ingress_ms: Date.now() - t_start,
    };
  }
  let handle: { fetch: (req: Request) => Promise<Response> } | undefined;
  try {
    handle = getContainer(env.PASAY_CONTAINER, PASAY_CONTAINER_INSTANCE_ID);
  } catch (err) {
    log_error("direct-container-handle", err, env);
    return {
      outcome: "transient",
      status: 503,
      body: { ok: false, error: "container_handle_failed" },
      error: "container_handle_failed",
      worker_ingress_ms: Date.now() - t_start,
    };
  }
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    [INGEST_AUTH_HEADER]: token,
    [TRACE_HEADER]: trace.trace_id,
    [TRACE_T0_HEADER]: trace.trace_t0,
    "X-Pasay-Trace-Source": "telegram_webhook_direct",
  };
  const req = new Request(`${PASAY_CONTAINER_ORIGIN}${CONTAINER_INGEST_PATH}`, {
    method: "POST",
    headers,
    body: JSON.stringify(envelope),
  });
  let resp: Response;
  try {
    resp = await handle.fetch(req);
  } catch (err) {
    log_error("direct-container-fetch", err, env);
    return {
      outcome: "transient",
      status: 503,
      body: { ok: false, error: "container_fetch_failed" },
      error: "container_fetch_failed",
      worker_ingress_ms: Date.now() - t_start,
    };
  }
  const worker_ingress_ms = Date.now() - t_start;
  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    body = { ok: resp.status < 400, error: "container_response_not_json" };
  }
  if (resp.status === 200 || resp.status === 202 || resp.status === 208) {
    return { outcome: "ack", status: resp.status, body, worker_ingress_ms };
  }
  if (resp.status === 400 || resp.status === 415 || resp.status === 422) {
    return {
      outcome: "permanent",
      status: resp.status,
      body,
      error: "container_permanent_reject",
      worker_ingress_ms,
    };
  }
  return {
    outcome: "transient",
    status: resp.status,
    body,
    error: "container_transient",
    worker_ingress_ms,
  };
}

async function deliver_envelope_to_container(env: Env, envelope: PasayQueueEnvelope): Promise<"ack" | "retry" | "terminal"> {
  if (!env.PASAY_CONTAINER) return "retry";
  const token = env.PASAY_CONTAINER_INGEST_TOKEN;
  if (!token || !token.trim()) return "retry";
  let handle: { fetch: (req: Request) => Promise<Response> } | undefined;
  try {
    handle = getContainer(env.PASAY_CONTAINER, PASAY_CONTAINER_INSTANCE_ID);
  } catch (err) {
    log_error("container-handle", err, env);
    return "retry";
  }
  // Queue consumer path also propagates the trace id so operator-side
  // logs from the fallback path can correlate with the original Worker
  // ingress event. The queue-driven path also adds `X-Pasay-Trace-Source`
  // distinguishing it from the direct webhook forward — useful for
  // distinguishing "interactive user tap" from "scheduled retry" in
  // production telemetry without grepping worker logs.
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    [INGEST_AUTH_HEADER]: token,
    [TRACE_HEADER]: envelope.event_id,
    [TRACE_T0_HEADER]: envelope.occurred_at,
    "X-Pasay-Trace-Source": "queue_consumer",
  };
  const req = new Request(`${PASAY_CONTAINER_ORIGIN}${CONTAINER_INGEST_PATH}`, {
    method: "POST",
    headers,
    body: JSON.stringify(envelope),
  });
  let resp: Response;
  try {
    resp = await handle.fetch(req);
  } catch (err) {
    log_error("container-fetch", err, env);
    return "retry";
  }
  if (resp.status === 200 || resp.status === 202 || resp.status === 208) return "ack";
  if (resp.status === 400 || resp.status === 415 || resp.status === 422) return "terminal";
  return "retry";
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...extra } });
}

/**
 * CORS allow-origin for the Mini App Issue #119 SPA. Only the canonical
 * ``https://pasay-mini-app.pages.dev`` Pages origin is permitted; every
 * other Origin is *echoed* back unchanged (NOT ``*``) so the SPA still
 * gets a working CORS handshake without us lifting credentials.
 */
function cors_headers_for_request(request: Request): Headers {
  const headers = new Headers();
  const origin = request.headers.get("Origin") ?? "";
  // Pages preview / custom domains: only the canonical Pages URL is trusted.
  // Echoing the origin back is permitted because we never set
  // ``Access-Control-Allow-Credentials`` and the SPA does not include
  // credentials on this fetch (see ``mini_app/src/api.ts``).
  if (
    origin === "https://pasay-mini-app.pages.dev" ||
    origin === "https://pasay-mini-app.pages.dev/"
  ) {
    headers.set("Access-Control-Allow-Origin", "https://pasay-mini-app.pages.dev");
  } else if (origin !== "") {
    headers.set("Access-Control-Allow-Origin", origin);
  } else {
    headers.set("Access-Control-Allow-Origin", "https://pasay-mini-app.pages.dev");
  }
  headers.set("Vary", "Origin");
  headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
  headers.set(
    "Access-Control-Allow-Headers",
    "Content-Type, Authorization, X-Requested-With, X-Idempotency-Key, Idempotency-Key",
  );
  headers.set("Access-Control-Max-Age", "86400");
  return headers;
}

function apply_cors_to_response(resp: Response, request: Request): Response {
  const outgoing = new Headers(resp.headers);
  const cors = cors_headers_for_request(request);
  cors.forEach((v, k) => {
    if (k.toLowerCase() === "access-control-allow-origin") {
      outgoing.set(k, v);
    } else if (!outgoing.has(k)) {
      outgoing.set(k, v);
    }
  });
  return new Response(resp.body, {
    status: resp.status,
    statusText: resp.statusText,
    headers: outgoing,
  });
}

/**
 * Forward the inbound ``/api/v1/*`` request straight into the Container over
 * the same native binding the queue path uses. Purpose: make the FastAPI V1
 * surface reachable from the public Worker hostname so the Cloudflare Pages
 * Mini App (``https://pasay-mini-app.pages.dev``) can mount its WebAppAuth
 * + Properties + Home flows against the real backend without a Pages Function
 * proxy or a custom hostname on the Container.
 *
 * The Container itself is NOT publicly exposed (Containers do not bind a
 * public hostname by default — see ``app/main.py:do not expose /internal/*``
 * note); this proxy is the ONLY publicly reachable path to it.
 *
 * Defence-in-depth (no silent auth removal, fail closed):
 *   * No bypass of the Container's own auth/rbac — the bearer header set
 *     by ``POST /api/v1/webapp/auth`` is forwarded verbatim. The FastAPI
 *     dependency ``get_api_key`` + ``require_org_scope`` own every
 *     ownership check; this Worker MUST NOT relax any of them.
 *   * ``PASAY_CONTAINER_INGEST_TOKEN`` is NOT required for ``/api/v1/*``
 *     because the Container's Bearer / membership middleware ALREADY trusts
 *     ``Authorization: Bearer <api_key>`` and ``X-Telegram-User-Id``. The
 *     internal ``/internal/ingest`` token is for the queue path only.
 *   * Cloudflare Container binding allows ANY HTTP method on the forwarded
 *     request (GET, POST, PATCH, PUT, DELETE, OPTIONS); we forward
 *     method + body + (most) headers as-is. The Host header is rewritten
 *     to the Container's origin so routing is stable.
 *   * CORS is appended on the Worker boundary because the SPA is hosted
 *     on Pages (``https://pasay-mini-app.pages.dev``) and the API surfaces
 *     here — the cross-origin fetch needs ``Access-Control-Allow-Origin``
 *     or the browser will refuse the response.
 */
async function forward_api_v1_to_container(
  env: Env,
  request: Request,
): Promise<Response> {
  // Preflight: respond immediately so we don't wake the Container for a
  // header-only handshake.
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors_headers_for_request(request) });
  }
  if (!env.PASAY_CONTAINER) {
    const r = json(503, { ok: false, error: "container_unbound" });
    return apply_cors_to_response(r, request);
  }
  let handle: { fetch: (req: Request) => Promise<Response> };
  try {
    handle = getContainer(env.PASAY_CONTAINER, PASAY_CONTAINER_INSTANCE_ID);
  } catch (err) {
    log_error("api-v1-container-handle", err, env);
    const r = json(503, { ok: false, error: "container_handle_failed" });
    return apply_cors_to_response(r, request);
  }
  // Build the forwarded URL on the Container origin. Preserve query string;
  // keep path identical so FastAPI mounts (/api/v1/*) resolve natively.
  const incoming = new URL(request.url);
  const forward_url = `${PASAY_CONTAINER_ORIGIN}${incoming.pathname}${incoming.search}`;

  // Forward every header except Hop-by-hop + Host (the latter would either be
  // the Worker hostname — useless inside the Container — or trip Cloudflare's
  // host-equality invariant on a few sensitive endpoints).
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cf-connecting-ip");
  headers.delete("origin");
  headers.set("X-Forwarded-Proto", incoming.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", incoming.host);

  let body: ArrayBuffer | undefined;
  if (request.method !== "GET" && request.method !== "HEAD") {
    try {
      body = await request.arrayBuffer();
    } catch (err) {
      log_error("api-v1-body-read", err, env);
      const r = json(400, { ok: false, error: "body_read_failed" });
      return apply_cors_to_response(r, request);
    }
  }

  const fwd_req = new Request(forward_url, {
    method: request.method,
    headers,
    body: body as BodyInit | undefined,
  });
  let resp: Response;
  try {
    resp = await handle.fetch(fwd_req);
  } catch (err) {
    log_error("api-v1-container-fetch", err, env);
    const r = json(503, { ok: false, error: "container_fetch_failed" });
    return apply_cors_to_response(r, request);
  }
  // Append CORS so the SPA's cross-origin fetch sees the response.
  return apply_cors_to_response(resp, request);
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === TELEGRAM_WEBHOOK_PATH) return handle_telegram_ingress(request, env);
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
    // Public V1 API surface for the Mini App (Issue #119 acceptance evidence
    // — Owner signs in via Telegram initData on POST /api/v1/webapp/auth,
    // then the SPA uses the issued bearer for /api/v1/properties,
    // /api/v1/dashboard/home, …). Without this hop the SPA cannot reach the
    // Container from the Pages origin (Pages is static; the Container is not
    // publicly addressable).
    if (url.pathname.startsWith("/api/v1/")) {
      return forward_api_v1_to_container(env, request);
    }
    return json(404, { ok: false, error: "not_found" });
  },

  async queue(batch: MessageBatch<PasayQueueEnvelope>, env: Env, _ctx: ExecutionContext): Promise<void> {
    for (const msg of batch.messages) {
      const envelope = msg.body;
      if (typeof envelope !== "object" || envelope === null || envelope.version !== ENVELOPE_VERSION || (envelope.kind !== "telegram_update" && envelope.kind !== "scheduled_job")) {
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
    const envelope: PasayQueueEnvelope = {
      version: ENVELOPE_VERSION,
      kind: "scheduled_job",
      event_id: make_scheduled_event_id("pasay_heartbeat", occurred_at),
      occurred_at,
      payload: { job_name: "pasay_heartbeat", scheduled_at: occurred_at, params: { cron_expression: controller.cron ?? "unscheduled" } },
    };
    try {
      await env.PASAY_QUEUE.send(envelope as unknown as MessageSendRequest);
    } catch (err) {
      log_error("scheduled-enqueue", err, env);
    }
  },
};
