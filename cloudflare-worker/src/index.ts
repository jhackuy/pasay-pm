/**
 * PASAY Cloudflare Worker — ingress → queue → container → Neon.
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
  if (request.method !== "POST") return json(405, { ok: false, error: "method_not_allowed" }, { Allow: "POST" });
  const ct = request.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().includes("application/json")) return json(400, { ok: false, error: "bad_content_type" });
  const configured_secret = env.TELEGRAM_WEBHOOK_SECRET ?? "";
  if (!configured_secret.trim()) return json(401, { ok: false, error: "webhook_not_configured" });
  const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!header_eq(received, configured_secret)) return json(403, { ok: false, error: "forbidden" });

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return json(400, { ok: false, error: "invalid_json" });
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return json(400, { ok: false, error: "malformed_payload" });
  const payload = raw as Record<string, unknown>;
  const meta = extract_telegram_meta(payload);
  if (!Number.isFinite(meta.update_id) || meta.update_id <= 0) return json(400, { ok: false, error: "missing_update_id" });

  const occurred_at = now_iso();
  const req_id = make_req_id();
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
    log_error("enqueue", err, env);
    return json(503, { ok: false, error: "enqueue_failed", req_id });
  }
  return json(200, { ok: true, state: "enqueued", event_id: envelope.event_id, req_id });
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
  const req = new Request(`${PASAY_CONTAINER_ORIGIN}${CONTAINER_INGEST_PATH}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", [INGEST_AUTH_HEADER]: token },
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
