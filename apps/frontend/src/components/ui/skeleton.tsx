import * as React from "react";

import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="skeleton" aria-hidden className={cn("rounded-sm bg-muted motion-safe:animate-pulse", className)} {...props} />;
}

export { Skeleton };
