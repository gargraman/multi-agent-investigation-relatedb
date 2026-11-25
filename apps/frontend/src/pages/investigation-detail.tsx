import { ArrowClockwise, Robot } from "@phosphor-icons/react";
import { useParams } from "react-router";

import { api } from "@/api/client";
import { latestStatus, summarize } from "@/api/timeline";
import type { Investigation, TimelineEvent } from "@/api/types";
import { ConnectionBadge } from "@/components/connection-badge";
import { ProblemAlert } from "@/components/problem-alert";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useInvestigationEvents } from "@/hooks/use-investigation-events";
import { useResource } from "@/hooks/use-resource";
import { formatDateTime, formatTime } from "@/lib/utils";

export function InvestigationDetailPage() {
  const { id = "" } = useParams();
  return <InvestigationDetail key={id} id={id} />;
}

function InvestigationDetail({ id }: { id: string }) {
  const { events, state } = useInvestigationEvents(id);
  // Re-read the investigation whenever a state change lands, so header + tasks stay current.
  const changeCount = events.filter((e) => e.type.endsWith("status_changed")).length;
  const inv = useResource((s) => api.getInvestigation(id, s), [id, changeCount]);

  if (inv.state === "loading" && !inv.data) return <DetailSkeleton />;
  if (inv.state === "error" && !inv.data) {
    return (
      <div className="max-w-2xl">
        <ProblemAlert
          error={inv.error}
          action={
            <Button size="sm" variant="outline" onClick={inv.reload}>
              <ArrowClockwise aria-hidden /> Retry
            </Button>
          }
        />
      </div>
    );
  }
  const data = inv.data!;
  const status = latestStatus(events) ?? data.status;

  return (
    <div className="grid gap-8">
      <Header inv={data} status={status} />
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Timeline events={events} state={state} />
        <aside className="grid content-start gap-8">
          <Tasks inv={data} />
          <section aria-labelledby="objective-heading">
            <h2 id="objective-heading" className="text-sm font-semibold">
              Objective
            </h2>
            <p className="mt-2 max-w-[65ch] whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{data.objective}</p>
          </section>
        </aside>
      </div>
    </div>
  );
}

function Header({ inv, status }: { inv: Investigation; status: string }) {
  const facts: [string, string][] = [
    ["Scenario", inv.scenario_id],
    ["Created", formatDateTime(inv.created_at)],
    ["Deadline", formatDateTime(inv.deadline)],
    ["Tokens", `${inv.tokens_used.toLocaleString()} / ${inv.token_budget.toLocaleString()}`],
  ];
  return (
    <header className="grid gap-4 border-b border-border pb-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-lg font-semibold tracking-tight break-all" data-testid="investigation-id">
          {inv.id}
        </h1>
        <StatusBadge status={status} data-testid="investigation-status" />
        {inv.status_reason && <span className="font-mono text-xs text-muted-foreground">{inv.status_reason}</span>}
      </div>
      <dl className="grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
        {facts.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-xs text-muted-foreground">{k}</dt>
            <dd className="truncate font-mono text-[13px]" data-testid={k === "Scenario" ? "investigation-scenario" : undefined}>
              {v}
            </dd>
          </div>
        ))}
      </dl>
    </header>
  );
}

function Timeline({ events, state }: { events: TimelineEvent[]; state: Parameters<typeof ConnectionBadge>[0]["state"] }) {
  const newestFirst = [...events].reverse();
  return (
    <section aria-labelledby="timeline-heading">
      <div className="flex items-center justify-between gap-4">
        <h2 id="timeline-heading" className="text-sm font-semibold">
          Timeline <span className="font-mono text-xs font-normal text-muted-foreground">({events.length})</span>
        </h2>
        <ConnectionBadge state={state} />
      </div>
      {events.length === 0 ? (
        <p className="mt-3 rounded-md border border-dashed border-border px-4 py-6 text-sm text-muted-foreground">
          {state === "live" ? "Connected. Waiting for the first event." : "Opening the event stream…"}
        </p>
      ) : (
        <ol className="mt-3 divide-y divide-border rounded-md border border-border" aria-live="polite" data-testid="timeline">
          {newestFirst.map((e) => (
            <li key={e.seq} className="event-enter px-3 py-2.5" data-testid="timeline-event" data-type={e.type}>
              <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-3 sm:grid-cols-[4.5rem_3rem_minmax(0,1fr)]">
                <time dateTime={e.created_at} className="font-mono text-xs text-muted-foreground tabular-nums">
                  {formatTime(e.created_at)}
                </time>
                <span className="hidden font-mono text-xs text-muted-foreground tabular-nums sm:block">#{e.seq}</span>
                <div className="min-w-0">
                  <p className="text-sm">{summarize(e)}</p>
                  <details className="group mt-0.5">
                    <summary className="w-fit cursor-pointer rounded-sm font-mono text-[11px] text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
                      {e.type}
                    </summary>
                    {/* Payload is untrusted: shown as JSON text inside <pre>, never parsed as markup. */}
                    <pre className="mt-1.5 max-h-64 overflow-auto rounded-sm bg-muted p-2 font-mono text-[11px] leading-relaxed">
                      {JSON.stringify(e.payload ?? {}, null, 2)}
                    </pre>
                  </details>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function Tasks({ inv }: { inv: Investigation }) {
  const tasks = inv.tasks ?? [];
  return (
    <section aria-labelledby="tasks-heading">
      <h2 id="tasks-heading" className="text-sm font-semibold">
        Agent tasks <span className="font-mono text-xs font-normal text-muted-foreground">({tasks.length})</span>
      </h2>
      {tasks.length === 0 ? (
        <div className="mt-3 flex gap-3 rounded-md border border-dashed border-border px-4 py-5" data-testid="tasks-empty">
          <Robot aria-hidden className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">No agents dispatched yet. Agents arrive in later slices.</p>
        </div>
      ) : (
        <ul className="mt-3 divide-y divide-border rounded-md border border-border">
          {tasks.map((t) => (
            <li key={t.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-2.5">
              <span className="min-w-0">
                <span className="block truncate font-mono text-[13px]">{t.agent_id}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {t.task_type}, attempt {t.attempt}
                </span>
              </span>
              <StatusBadge status={t.status} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function DetailSkeleton() {
  return (
    <div className="grid gap-8" aria-busy="true" aria-label="Loading investigation">
      <div className="grid gap-4 border-b border-border pb-6">
        <Skeleton className="h-7 w-80" />
        <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      </div>
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Skeleton className="h-64" />
        <Skeleton className="h-32" />
      </div>
    </div>
  );
}
