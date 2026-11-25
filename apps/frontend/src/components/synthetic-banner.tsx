import { Flask } from "@phosphor-icons/react";

/** AC15: persistent, non-dismissable. Rendered by the shell on every route; no close control. */
export function SyntheticBanner() {
  return (
    <div
      role="note"
      aria-label="Synthetic data notice"
      data-testid="synthetic-banner"
      className="sticky top-0 z-20 border-b border-accent/60 bg-accent-soft"
    >
      <div className="synthetic-stripe h-1 opacity-70" aria-hidden />
      <p className="mx-auto flex max-w-[1400px] items-center gap-2 px-4 py-1.5 text-[13px] font-medium md:px-6">
        <Flask weight="bold" aria-hidden className="size-4 shrink-0" />
        <span className="font-mono tracking-wide">SYNTHETIC DATA</span>
        <span className="text-muted-foreground">Fictional scenario. No real people, places or intelligence.</span>
      </p>
    </div>
  );
}
