import { CheckCircle, WarningCircle, XCircle, type Icon } from "@phosphor-icons/react";
import { Popover } from "radix-ui";

import { api } from "@/api/client";
import { useResource } from "@/hooks/use-resource";
import { cn } from "@/lib/utils";

const LEVEL: Record<string, { icon: Icon; cls: string; label: string }> = {
  OK: { icon: CheckCircle, cls: "text-ok", label: "OK" },
  DEGRADED: { icon: WarningCircle, cls: "text-warn", label: "Degraded" },
  DOWN: { icon: XCircle, cls: "text-fail", label: "Down" },
  UNREACHABLE: { icon: XCircle, cls: "text-fail", label: "Unavailable" },
  CHECKING: { icon: WarningCircle, cls: "text-muted-foreground", label: "Checking" },
};

export function HealthIndicator() {
  const health = useResource((s) => api.systemHealth(s), [], { pollMs: 15000 });
  const key =
    health.state === "error" ? "UNREACHABLE" : health.data ? health.data.overall : "CHECKING";
  const l = LEVEL[key]!;
  const I = l.icon;

  return (
    <Popover.Root>
      <Popover.Trigger
        data-testid="health-indicator"
        className="inline-flex h-8 items-center gap-2 rounded-md border border-border px-2.5 text-[13px] hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <I weight="fill" aria-hidden className={cn("size-4", l.cls)} />
        <span className="text-muted-foreground">System</span>
        <span className="font-medium">{l.label}</span>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={6}
          className="z-30 w-72 rounded-md border border-border bg-background p-3 text-[13px] shadow-lg shadow-black/10"
        >
          <p className="mb-2 font-medium">System health</p>
          {health.state === "error" ? (
            <p className="text-muted-foreground">{health.error.problem.title}. Retrying every 15 s.</p>
          ) : !health.data ? (
            <p className="text-muted-foreground">Checking components…</p>
          ) : health.data.components.length === 0 ? (
            <p className="text-muted-foreground">No components reported.</p>
          ) : (
            <ul className="grid gap-1.5">
              {health.data.components.map((c) => (
                <li key={c.name} className="flex items-baseline justify-between gap-3">
                  <span className="font-mono">{c.name}</span>
                  <span className="font-mono text-muted-foreground">{c.status}</span>
                </li>
              ))}
            </ul>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
