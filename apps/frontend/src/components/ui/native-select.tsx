import { CaretUpDown } from "@phosphor-icons/react";
import * as React from "react";

import { cn } from "@/lib/utils";

/** shadcn/ui "native-select": a real <select> for keyboard/AT parity, styled to match. */
function NativeSelect({ className, ...props }: React.ComponentProps<"select">) {
  return (
    <div className="relative w-full">
      <select
        data-slot="native-select"
        className={cn(
          "h-9 w-full appearance-none rounded-md border border-input bg-surface pl-3 pr-9 font-mono text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-fail focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
          className,
        )}
        {...props}
      />
      <CaretUpDown
        aria-hidden
        className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
      />
    </div>
  );
}

export { NativeSelect };
