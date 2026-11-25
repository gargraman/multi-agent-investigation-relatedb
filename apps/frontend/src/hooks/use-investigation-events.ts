import { useEffect, useState } from "react";

import { api } from "@/api/client";
import { TimelineEventTypeValues } from "@/api/generated/enums";
import { mergeEvent, parseTimelineEvent } from "@/api/timeline";
import type { TimelineEvent } from "@/api/types";

export type StreamState = "connecting" | "live" | "reconnecting" | "offline";

const RETRY_MS = [1000, 2000, 5000, 10000];

/**
 * Live timeline over SSE. Server contract: `id` = seq, `event` = TimelineEventType, `data` =
 * TimelineEvent JSON. EventSource retries transient drops itself (sending Last-Event-ID); if
 * the browser gives up (readyState CLOSED, e.g. a 502 from the proxy) we reopen with backoff.
 * Replays are harmless: events are merged by seq. Mount with `key={id}` so a new
 * investigation starts from empty state.
 */
export function useInvestigationEvents(id: string): { events: TimelineEvent[]; state: StreamState } {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");

  useEffect(() => {
    let es: EventSource | null = null;
    let retry = 0;
    let timer: number | undefined;
    let disposed = false;
    let everOpened = false;

    const onEvent = (msg: MessageEvent<string>) => {
      const ev = parseTimelineEvent(msg.data, msg.type === "message" ? undefined : msg.type);
      if (ev) setEvents((list) => mergeEvent(list, ev));
    };

    const open = () => {
      if (disposed) return;
      es = new EventSource(api.eventsUrl(id));
      es.onopen = () => {
        everOpened = true;
        retry = 0;
        setState("live");
      };
      es.onerror = () => {
        if (!es || disposed) return;
        if (es.readyState === EventSource.CLOSED) {
          es.close();
          setState("offline");
          const delay = RETRY_MS[Math.min(retry++, RETRY_MS.length - 1)];
          timer = window.setTimeout(() => {
            setState(everOpened ? "reconnecting" : "connecting");
            open();
          }, delay);
        } else {
          setState("reconnecting");
        }
      };
      es.addEventListener("message", onEvent);
      for (const t of TimelineEventTypeValues) es.addEventListener(t, onEvent as EventListener);
    };

    open();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      es?.close();
    };
  }, [id]);

  return { events, state };
}
