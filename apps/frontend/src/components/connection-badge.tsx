import { ArrowsClockwise, CircleNotch, Plugs, Broadcast, type Icon } from "@phosphor-icons/react";

import type { StreamState } from "@/hooks/use-investigation-events";
import { cn } from "@/lib/utils";

const MAP: Record<StreamState, { icon: Icon; label: string; cls: string }> = {
  connecting: { icon: CircleNotch, label: "Connecting", cls: "text-muted-foreground" },
  live: { icon: Broadcast, label: "Live", cls: "text-ok" },
  reconnecting: { icon: ArrowsClockwise, label: "Reconnecting", cls: "text-warn" },
  offline: { icon: Plugs, label: "Stream offline, retrying", cls: "text-fail" },
};

export function ConnectionBadge({ state }: { state: StreamState }) {
  const m = MAP[state];
  const I = m.icon;
  return (
    <span
      role="status"
      data-testid="stream-state"
      data-state={state}
      className="inline-flex items-center gap-1.5 font-mono text-[11px] font-medium uppercase tracking-wide"
    >
      <I weight="bold" aria-hidden className={cn("size-3.5", m.cls, state === "live" && "live-pulse")} />
      <span className={m.cls}>{m.label}</span>
    </span>
  );
}
