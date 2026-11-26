/**
 * FR-150 (S3b): the operator cancels a running investigation from the UI.
 * Runs against a REAL stack at BASE_URL (default http://localhost:3000). No route mocking.
 *
 * Needs the replay + chaos override so the agents' replay latency (20 s per call) keeps the
 * investigation non-terminal long enough to click Cancel. The same layering scripts/chaos.sh uses:
 *   docker compose -p nexus -f infrastructure/docker/compose.yaml \
 *     -f infrastructure/docker/compose.chaos.yaml --env-file .env up -d --build --wait
 *   BASE_URL=http://localhost:3000 npm run test:e2e -- s3-cancel
 * Restore the default stack afterwards by re-applying compose.yaml alone.
 */
import { expect, test } from "@playwright/test";

const SCENARIO = "baseline-corroboration";

test("FR-150: operator cancels a running investigation from the UI", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Scenario").selectOption(SCENARIO);

  const created = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/investigations") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create investigation" }).click();
  const inv = (await (await created).json()) as { id: string };
  await expect(page).toHaveURL(new RegExp(`/investigations/${encodeURIComponent(inv.id)}$`));

  const status = page.getByTestId("investigation-status");
  await expect(status).toContainText(/CREATED|RUNNING/);

  // Cancel opens a confirm dialog; nothing is sent until it is confirmed.
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog.getByText("Cancel investigation?")).toBeVisible();

  const cancelled = page.waitForResponse(
    (r) => r.url().endsWith(`/api/v1/investigations/${inv.id}/cancel`) && r.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Cancel investigation" }).click();
  const res = await cancelled;
  expect(res.status()).toBe(200);
  expect(await res.json()).toMatchObject({ id: inv.id, status: "CANCELLED" });

  await expect(status).toContainText("CANCELLED");
  await expect(page.getByTestId("cancel-context")).toHaveText(/Context close(d| pending)/);
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toHaveCount(0);
});
