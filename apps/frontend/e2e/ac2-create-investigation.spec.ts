/**
 * AC2 (spec §17.3): "UI creates an investigation from a synthetic scenario".
 * Runs against a REAL stack at BASE_URL (default http://localhost:3000). No route mocking.
 *   NEXUS_E2E_REPLAY=1 BASE_URL=http://localhost:3000 npm run test:e2e -- ac2
 *
 * Skipped unless NEXUS_E2E_REPLAY=1: this spec creates an investigation, which on a live-LLM stack
 * means real OpenAI calls. Set the flag only when the stack runs in replay mode. The investigation
 * is cancelled in afterEach so it never keeps dispatching agent work after the test.
 */
import { expect, test } from "@playwright/test";

const SCENARIO = "baseline-corroboration";

test.skip(process.env.NEXUS_E2E_REPLAY !== "1", "needs a replay-mode stack: set NEXUS_E2E_REPLAY=1");

// Whatever happens after creation, never leave the investigation running. 409 (already terminal)
// and 404 are fine.
let createdId: string | null = null;

test.afterEach(async ({ request }) => {
  if (!createdId) return;
  const id = createdId;
  createdId = null;
  const res = await request.post(`/api/v1/investigations/${encodeURIComponent(id)}/cancel`);
  expect([200, 404, 409]).toContain(res.status());
});

test("AC2: operator creates an investigation from a synthetic scenario", async ({ page }) => {
  await page.goto("/");

  // AC15 groundwork: the synthetic-data banner is always present.
  const banner = page.getByTestId("synthetic-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("SYNTHETIC DATA");

  // Pick the scenario; its objective is shown read-only.
  const select = page.getByLabel("Scenario");
  await expect(select).toBeEnabled();
  await select.selectOption(SCENARIO);
  const objective = page.getByLabel("Objective");
  await expect(objective).toHaveAttribute("readonly", "");
  await expect(objective).not.toHaveValue("");

  // Create, and capture the real API response.
  const created = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/investigations") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create investigation" }).click();
  const res = await created;
  expect(res.status()).toBe(201);
  const inv = (await res.json()) as { id: string; scenario_id: string; status: string };
  createdId = inv.id;
  expect(inv.scenario_id).toBe(SCENARIO);

  // Lands on the investigation page.
  await expect(page).toHaveURL(new RegExp(`/investigations/${encodeURIComponent(inv.id)}$`));
  await expect(page.getByTestId("investigation-id")).toHaveText(inv.id);
  await expect(page.getByTestId("investigation-scenario")).toHaveText(SCENARIO);
  await expect(page.getByTestId("investigation-status")).toContainText(/CREATED|RUNNING/);
  await expect(banner).toBeVisible();

  // At least one timeline event arrives over SSE.
  await expect(page.getByTestId("timeline-event").first()).toBeVisible();
  await expect(page.getByTestId("stream-state")).toHaveAttribute("data-state", /live|reconnecting/);
});
