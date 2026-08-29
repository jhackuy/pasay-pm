/**
 * PASAY-QUEUE-ENVELOPE-V1
 * Single versioned envelope shared by Worker ingress, queue consumer, cron, and
 * the FastAPI /internal/ingest endpoint. No Avro / Protobuf / schema registry.
 */

export const ENVELOPE_VERSION = "1" as const;

export type EnvelopeKind = "telegram_update" | "scheduled_job";

export interface BaseEnvelope {
  version: typeof ENVELOPE_VERSION;
  kind: EnvelopeKind;
  /** Stable, deterministic event identity used by Container idempotency + queue dedup. */
  event_id: string;
  /** ISO-8601 UTC. */
  occurred_at: string;
}

export interface TelegramUpdateEnvelope extends BaseEnvelope {
  kind: "telegram_update";
  /** Raw Telegram Update JSON as delivered; Worker does not interpret fields. */
  payload: Record<string, unknown>;
  _telegram_meta: { update_id: number; chat_id?: number };
}

export interface ScheduledJobEnvelope extends BaseEnvelope {
  kind: "scheduled_job";
  payload: {
    job_name: string;
    scheduled_at: string;
    params?: Record<string, unknown>;
  };
}

export type PasayQueueEnvelope = TelegramUpdateEnvelope | ScheduledJobEnvelope;

export function make_telegram_event_id(update_id: number): string {
  return `tg:${String(update_id)}`;
}

export function make_scheduled_event_id(job_name: string, occurred_at_iso: string): string {
  // Deterministic 5-minute bucket so retries within the same window produce the same event_id.
  const d = new Date(occurred_at_iso);
  const floored_min = Math.floor(d.getUTCMinutes() / 5) * 5;
  const bucket = new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), floored_min, 0),
  );
  const bucket_iso = bucket.toISOString().replace(/[:.]/g, "-").slice(0, 16);
  return `sched:${job_name}:${bucket_iso}`;
}
