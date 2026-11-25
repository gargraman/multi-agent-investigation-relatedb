import { ArrowClockwise, LockSimple, Play } from "@phosphor-icons/react";
import { useId, useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, api } from "@/api/client";
import { InvestigationList } from "@/components/investigation-list";
import { ProblemAlert } from "@/components/problem-alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { useResource } from "@/hooks/use-resource";

export function NewInvestigationPage() {
  const navigate = useNavigate();
  const scenarios = useResource((s) => api.listScenarios(s), []);
  const [selected, setSelected] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const selectId = useId();
  const objectiveId = useId();

  const list = scenarios.data ?? [];
  const scenario = list.find((s) => s.scenario_id === selected) ?? null;
  const fieldErr = submitError?.problem.errors?.find((e) => e.loc.includes("scenario_id"));

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!scenario || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      // Objective omitted on purpose: the orchestrator applies the template's (locked in replay).
      const inv = await api.createInvestigation({ scenario_id: scenario.scenario_id });
      navigate(`/investigations/${encodeURIComponent(inv.id)}`);
    } catch (err) {
      setSubmitError(
        err instanceof ApiError
          ? err
          : new ApiError({ type: "about:blank", title: "Create failed", status: 0, detail: String(err) }),
      );
      setSubmitting(false);
    }
  }

  return (
    <div className="grid gap-10 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] lg:gap-14">
      <section aria-labelledby="new-heading">
        <h1 id="new-heading" className="text-xl font-semibold tracking-tight">
          New investigation
        </h1>
        <p className="mt-1 max-w-[60ch] text-sm text-muted-foreground">
          Start from a synthetic scenario template. Agents and their inputs come from the template.
        </p>

        <form onSubmit={onSubmit} className="mt-6 grid max-w-xl gap-5" noValidate>
          <div className="grid gap-2">
            <Label htmlFor={selectId}>Scenario</Label>
            {scenarios.state === "loading" ? (
              <Skeleton className="h-9" />
            ) : (
              <NativeSelect
                id={selectId}
                name="scenario_id"
                value={selected}
                onChange={(e) => {
                  setSelected(e.target.value);
                  setSubmitError(null);
                }}
                disabled={submitting || list.length === 0}
                aria-invalid={fieldErr ? true : undefined}
                aria-describedby={`${selectId}-help`}
                data-testid="scenario-select"
              >
                <option value="">{list.length === 0 ? "No scenarios available" : "Select a scenario"}</option>
                {list.map((s) => (
                  <option key={s.scenario_id} value={s.scenario_id}>
                    {s.scenario_id}
                    {s.version ? ` (v${s.version})` : ""}
                  </option>
                ))}
              </NativeSelect>
            )}
            <p id={`${selectId}-help`} className="text-xs text-muted-foreground">
              {scenario?.task_count != null
                ? `${scenario.task_count} agent task${scenario.task_count === 1 ? "" : "s"} in this template.`
                : "Templates are fixed, versioned scenario packs."}
            </p>
            {fieldErr && <p className="text-xs text-fail">{fieldErr.msg}</p>}
          </div>

          {scenarios.state === "error" && (
            <ProblemAlert
              error={scenarios.error}
              action={
                <Button type="button" size="sm" variant="outline" onClick={scenarios.reload}>
                  <ArrowClockwise aria-hidden /> Retry
                </Button>
              }
            />
          )}

          <div className="grid gap-2">
            <div className="flex items-center gap-2">
              <Label htmlFor={objectiveId}>Objective</Label>
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <LockSimple aria-hidden className="size-3.5" /> Read-only, set by the template
              </span>
            </div>
            <textarea
              id={objectiveId}
              readOnly
              rows={4}
              value={scenario?.objective || ""}
              placeholder={scenario ? "This template has no objective text." : "Select a scenario to see its objective."}
              className="w-full resize-none rounded-md border border-input bg-muted px-3 py-2 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              data-testid="scenario-objective"
            />
          </div>

          {submitError && <ProblemAlert error={submitError} />}

          <div className="flex items-center gap-3">
            <Button type="submit" variant="accent" disabled={!scenario || submitting} aria-busy={submitting}>
              <Play weight="fill" aria-hidden />
              {submitting ? "Creating…" : "Create investigation"}
            </Button>
            {!scenario && scenarios.state === "ready" && list.length > 0 && (
              <span className="text-xs text-muted-foreground">Pick a scenario to continue.</span>
            )}
          </div>
        </form>
      </section>

      <section aria-labelledby="recent-heading" className="lg:border-l lg:border-border lg:pl-10">
        <div className="flex items-baseline justify-between">
          <h2 id="recent-heading" className="text-sm font-semibold">
            Recent
          </h2>
          <Link to="/investigations" className="rounded-sm text-xs text-muted-foreground underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring">
            View all
          </Link>
        </div>
        <div className="mt-3">
          <InvestigationList limit={6} />
        </div>
      </section>
    </div>
  );
}
