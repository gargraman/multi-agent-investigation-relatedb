import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm border px-2 py-0.5 font-mono text-[11px] font-medium uppercase tracking-wide [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "border-border bg-muted text-foreground",
        info: "border-accent/50 bg-accent-soft text-foreground",
        ok: "border-ok/40 bg-ok/10 text-ok",
        warn: "border-warn/40 bg-warn/10 text-warn",
        fail: "border-fail/40 bg-fail/10 text-fail",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

function Badge({ className, tone, ...props }: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export { Badge, badgeVariants };
