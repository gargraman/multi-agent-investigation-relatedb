/** Pure timeline helpers: SSE parsing, seq-dedup, text summaries. No DOM, unit-tested. */
import type { InvestigationStatus, TimelineEvent } from "./types";

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Parse one SSE `data:` frame. Returns null for anything that is not a plausible envelope. */
export function parseTimelineEvent(data: string, fallbackType?: string): TimelineEvent | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isRecord(raw)) return null;
  const seq = typeof raw.seq === "number" && Number.isInteger(raw.seq) && raw.seq >= 1 ? raw.seq : null;
  const type = typeof raw.type === "string" ? raw.type : fallbackType;
  if (seq === null || !type) return null;
  return {
    seq,
    type: type as TimelineEvent["type"],
    investigation_id: typeof raw.investigation_id === "string" ? raw.investigation_id : "",
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    payload: isRecord(raw.payload) ? raw.payload : {},
  };
}

/** Insert by seq, ignoring duplicates (SSE replay-then-live + reconnects resend events). */
export function mergeEvent(list: readonly TimelineEvent[], ev: TimelineEvent): TimelineEvent[] {
  if (list.some((e) => e.seq === ev.seq)) return list as TimelineEvent[];
  const next = [...list, ev];
  if (list.length > 0 && ev.seq < list[list.length - 1]!.seq) next.sort((a, b) => a.seq - b.seq);
  return next;
}

/** Latest investigation status announced on the timeline, if any. */
export function latestStatus(list: readonly TimelineEvent[]): InvestigationStatus | null {
  for (let i = list.length - 1; i >= 0; i--) {
    const e = list[i]!;
    if (e.type === "investigation.status_changed" && typeof e.payload?.to_status === "string") {
      return e.payload.to_status as InvestigationStatus;
    }
  }
  return null;
}

function s(v: unknown): string | null {
  return typeof v === "string" || typeof v === "number" ? String(v) : null;
}

/** One-line plain-text summary. Output is rendered as a React text node, never as HTML. */
export function summarize(ev: TimelineEvent): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "investigation.status_changed": {
      const from = s(p.from_status);
      const to = s(p.to_status) ?? "?";
      return from ? `Investigation ${from} → ${to}` : `Investigation ${to}`;
    }
    case "task.status_changed": {
      const who = s(p.agent_id) ?? "task";
      const from = s(p.from_state);
      const to = s(p.to_state) ?? "?";
      return from ? `${who} ${from} → ${to}` : `${who} ${to}`;
    }
    case "task.late_result":
      return `Late result for ${s(p.task_id) ?? "task"} (recorded ${s(p.recorded_state) ?? "?"})`;
    case "transition.dropped":
      return "Transition dropped (version conflict)";
    case "llm.replayed":
      return "LLM call replayed from recording";
    default:
      return String(ev.type);
  }
}
