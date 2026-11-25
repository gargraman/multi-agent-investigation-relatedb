import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <section className="max-w-xl">
      <h1 className="text-xl font-semibold tracking-tight">Page not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        That route does not exist.{" "}
        <Link to="/" className="underline underline-offset-4">
          Start a new investigation
        </Link>
        .
      </p>
    </section>
  );
}
