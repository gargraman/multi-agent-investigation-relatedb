import { describe, expect, it } from "vitest";

import { normalizeHealth, normalizeScenarios } from "./client";
import { latestStatus, mergeEvent, parseTimelineEvent, summarize } from "./timeline";
import type { TimelineEvent } from "./types";

const ev = (seq: number, payload: Record<string, unknown> = { to_status: "CREATED" }): TimelineEvent => ({
  seq,
  investigation_id: "inv-1",
  type: "investigation.status_changed",
  payload,
  created_at: "2026-01-01T00:00:00Z",
});

describe("timeline (mocked data, not AC2)", () => {
  it("dedups by seq and keeps order", () => {
    let list: TimelineEvent[] = [];
    for (const n of [1, 2, 2, 4, 3, 1]) list = mergeEvent(list, ev(n));
    expect(list.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it("parses envelopes and rejects junk", () => {
    expect(parseTimelineEvent(JSON.stringify(ev(7)))?.seq).toBe(7);
    expect(parseTimelineEvent("not json")).toBeNull();
    expect(parseTimelineEvent(JSON.stringify({ seq: 0, type: "x" }))).toBeNull();
  });

  it("keeps markup inert: summaries are plain strings", () => {
    const e = ev(1, { to_status: "<img src=x onerror=alert(1)>" });
    expect(summarize(e)).toBe("Investigation <img src=x onerror=alert(1)>");
  });

  it("derives the latest status", () => {
    expect(latestStatus([ev(1), ev(2, { from_status: "CREATED", to_status: "RUNNING" })])).toBe("RUNNING");
  });
});

describe("api normalisers (mocked data, not AC2)", () => {
  it("accepts list, {items} and manifest-like scenarios", () => {
    expect(normalizeScenarios([{ scenario_id: "a", objective: "o", task_count: 3, version: "1" }])[0]).toEqual({
      scenario_id: "a",
      objective: "o",
      task_count: 3,
      version: "1",
    });
    expect(normalizeScenarios({ items: [{ id: "b", template: { tasks: [1, 2] } }] })[0]?.task_count).toBe(2);
    expect(normalizeScenarios({ nope: true })).toEqual([]);
  });

  it("defaults unknown health to DEGRADED and accepts map-shaped components", () => {
    const h = normalizeHealth({ overall: "weird", components: { orchestrator: { status: "OK" } } });
    expect(h.overall).toBe("DEGRADED");
    expect(h.components[0]).toEqual({ name: "orchestrator", status: "OK", detail: null });
  });
});
