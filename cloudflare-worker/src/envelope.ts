/**
 * PASAY-QUEUE-ENVELOPE-V1
 *
 * Single versioned envelope contract shared by:
 *   - Telegram ingress (Worker fetch handler)
 *   - Cron scheduler    (Worker scheduled handler)
 *   - Queue consumer     (Worker queue handler → Container)
 *   - Container ingestion (FastAPI /internal/ingest endpoint)
 *
 * NO Avro / Protobuf / schema registry.
 * TypeScript type + Python Pydantic model are the only schema.
 */

export const ENVELOPE_VERSION = "1" as const;

export type EnvelopeKind =
  | "telegram_update"
  | "scheduled_job";

export interface BaseEnvelope {
  version: typeof ENVELOPE_VERSION;
  kind: EnvelopeKind;
  /**
   * Stable, deterministic event identity.
   * - telegram_update: decimal string of update_id  (exact same value, NOT hashed)
   * - scheduled_job:  `${job_name}:${iso_five_minute_bucket}`  (idempotent per 5-min window)
   * Used by Container idempotency + Queue at-least-once dedupe.
   */
  event_id: string;
  /** ISO-8601 UTC when the event occurred / was generated. */
  occurred_at: string;
}

export interface TelegramUpdateEnvelope extends BaseEnvelope {
  kind: "telegram_update";
  /** Raw Telegram Update JSON as delivered (Worker MUST NOT interpret fields). */
  payload: Record<string, unknown>;
  /** Minimal extraction for observability / routing only — not business logic. */
  _telegram_meta: {
    update_id: number;
    chat_id?: number;
  };
}

export interface ScheduledJobEnvelope extends BaseEnvelope {
  kind: "scheduled_job";
  payload: {
    job_name: string;
    scheduled_at: string;
    params?: Record<string, unknown>;
  };
}

export type PasayQueueEnvelope =
  | TelegramUpdateEnvelope
  | ScheduledJobEnvelope;

export function make_telegram_event_id(update_id: number): string {
  // Telegram update_id is globally unique for a bot; decimal string avoids
  // 2^53 precision loss and matches Container-side idempotency key format.
  return `tg:${String(update_id)}`;
}

export function make_scheduled_event_id(
  job_name: string,
  occurred_at_iso: string,
): string {
  // Deterministic 5-minute bucket so retries within the same window produce
  // the same event_id → Container can idempotently reject duplicates.
  const d = new Date(occurred_at_iso);
  const floored_min = Math.floor(d.getUTCMinutes() / 5) * 5;
  const bucket = new Date(Date.UTC(
    d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(),
    d.getUTCHours(), floored_min, 0,
  ));
  const bucket_iso = bucket.toISOString().replace(/[:.]/g, "-").slice(0, 16);
  return `sched:${job_name}:${bucket_iso}`;
}
