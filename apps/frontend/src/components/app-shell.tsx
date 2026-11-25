import { Graph } from "@phosphor-icons/react";
import { NavLink, Outlet } from "react-router";

import { HealthIndicator } from "@/components/health-indicator";
import { SyntheticBanner } from "@/components/synthetic-banner";
import { cn } from "@/lib/utils";

const navCls = ({ isActive }: { isActive: boolean }) =>
  cn(
    "rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
    isActive ? "text-foreground bg-muted" : "text-muted-foreground",
  );

export function AppShell() {
  return (
    <div className="flex min-h-[100dvh] flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-40 focus:rounded-md focus:bg-background focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <SyntheticBanner />
      <header className="border-b border-border">
        <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4 md:px-6">
          <NavLink to="/" className="flex items-center gap-2 rounded-md font-semibold tracking-tight focus-visible:outline-2 focus-visible:outline-ring">
            <Graph weight="duotone" aria-hidden className="size-5 text-accent" />
            Nexus
            <span className="hidden font-mono text-[11px] font-normal text-muted-foreground sm:inline">operator console</span>
          </NavLink>
          <nav aria-label="Primary" className="flex items-center gap-1">
            <NavLink to="/" end className={navCls}>
              New investigation
            </NavLink>
            <NavLink to="/investigations" end className={navCls}>
              Investigations
            </NavLink>
          </nav>
          <div className="ml-auto">
            <HealthIndicator />
          </div>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 md:px-6 md:py-8">
        <Outlet />
      </main>
    </div>
  );
}
