import { ArrowClockwise, ArrowRight } from "@phosphor-icons/react";
import { Link } from "react-router";

import { api } from "@/api/client";
import { ProblemAlert } from "@/components/problem-alert";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useResource } from "@/hooks/use-resource";
import { formatDateTime } from "@/lib/utils";

export function InvestigationList({ limit }: { limit?: number }) {
  const res = useResource((s) => api.listInvestigations(s), [], { pollMs: 10000 });
  const items = res.data?.items ?? [];
  const shown = limit ? items.slice(0, limit) : items;

  if (res.state === "loading" && !res.data) {
    return (
      <div className="grid gap-2" aria-busy="true" aria-label="Loading investigations">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-12" />
        ))}
      </div>
    );
  }
  if (res.state === "error" && !res.data) {
    return (
      <ProblemAlert
        error={res.error}
        action={
          <Button size="sm" variant="outline" onClick={res.reload}>
            <ArrowClockwise aria-hidden /> Retry
          </Button>
        }
      />
    );
  }
  if (shown.length === 0) {
    return (
      <p className="rounded-md border border-dashed border-border px-4 py-6 text-sm text-muted-foreground">
        No investigations yet. Create one from a synthetic scenario to see it here.
      </p>
    );
  }
  return (
    <ul className="divide-y divide-border rounded-md border border-border" data-testid="investigation-list">
      {shown.map((inv) => (
        <li key={inv.id}>
          <Link
            to={`/investigations/${encodeURIComponent(inv.id)}`}
            className="group grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 px-3 py-2.5 hover:bg-muted focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:grid-cols-[minmax(0,1fr)_auto_auto]"
          >
            <span className="min-w-0">
              <span className="block truncate font-mono text-[13px]">{inv.id}</span>
              <span className="block truncate text-xs text-muted-foreground">
                {inv.scenario_id} <span aria-hidden>/</span> {formatDateTime(inv.created_at)}
              </span>
            </span>
            <StatusBadge status={inv.status} />
            <ArrowRight
              aria-hidden
              className="hidden size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 sm:block"
            />
          </Link>
        </li>
      ))}
    </ul>
  );
}
