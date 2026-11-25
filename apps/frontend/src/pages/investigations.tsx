import { InvestigationList } from "@/components/investigation-list";

export function InvestigationsPage() {
  return (
    <section aria-labelledby="list-heading" className="max-w-4xl">
      <h1 id="list-heading" className="text-xl font-semibold tracking-tight">
        Investigations
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">Most recent first. Refreshes every 10 seconds.</p>
      <div className="mt-6">
        <InvestigationList />
      </div>
    </section>
  );
}
