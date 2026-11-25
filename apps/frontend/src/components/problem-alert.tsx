import { WarningOctagon } from "@phosphor-icons/react";

import type { ApiError } from "@/api/client";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/** Renders an RFC 7807 problem (title, detail, field errors) as plain text. */
export function ProblemAlert({ error, action }: { error: ApiError; action?: React.ReactNode }) {
  const p = error.problem;
  return (
    <Alert variant="destructive" data-testid="problem-alert">
      <WarningOctagon weight="bold" aria-hidden />
      <AlertTitle>
        {p.title}
        {p.status > 0 && <span className="ml-2 font-mono text-xs text-muted-foreground">HTTP {p.status}</span>}
        {p.code && <span className="ml-2 font-mono text-xs text-muted-foreground">{p.code}</span>}
      </AlertTitle>
      <AlertDescription>
        {p.detail && <p>{p.detail}</p>}
        {p.errors && p.errors.length > 0 && (
          <ul className="mt-1 grid gap-1">
            {p.errors.map((e, i) => (
              <li key={i} className="grid grid-cols-[minmax(6rem,auto)_1fr] gap-3 text-[13px]">
                <code className="font-mono text-foreground">{e.loc.filter((l) => l !== "body").join(".") || "request"}</code>
                <span>{e.msg}</span>
              </li>
            ))}
          </ul>
        )}
        {action && <div className="mt-2">{action}</div>}
      </AlertDescription>
    </Alert>
  );
}
