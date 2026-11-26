// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/api/client";
import type { Investigation, TaskView } from "@/api/types";
import { InvestigationDetailPage } from "@/pages/investigation-detail";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: { getInvestigation: vi.fn(), cancelInvestigation: vi.fn(), eventsUrl: vi.fn() },
}));
vi.mock("@/hooks/use-investigation-events", () => {
  const stable = { events: [], state: "live" as const };
  return { useInvestigationEvents: () => stable };
});

const ID = "inv_abc123";
const NOW = "2026-11-26T10:00:00Z";

function task(over: Partial<TaskView>): TaskView {
  return {
    id: "tsk_1",
    agent_id: "sigint-agent",
    task_type: "collect",
    attempt: 1,
    status: "RUNNING",
    created_at: NOW,
    updated_at: NOW,
    deadline: NOW,
    ...over,
  };
}

function investigation(over: Partial<Investigation> = {}): Investigation {
  return {
    id: ID,
    scenario_id: "baseline-corroboration",
    objective: "Corroborate the sighting.",
    status: "RUNNING",
    tasks: [],
    token_budget: 1000,
    tokens_used: 10,
    created_at: NOW,
    updated_at: NOW,
    deadline: NOW,
    ...over,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/investigations/${ID}`]}>
      <Routes>
        <Route path="/investigations/:id" element={<InvestigationDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const getInvestigation = vi.mocked(api.getInvestigation);
const cancelInvestigation = vi.mocked(api.cancelInvestigation);

beforeEach(() => {
  getInvestigation.mockReset();
  cancelInvestigation.mockReset();
});
afterEach(cleanup);

describe("investigation detail: cancel", () => {
  it("hides cancel for terminal status", async () => {
    getInvestigation.mockResolvedValue(investigation({ status: "COMPLETED" }));
    renderPage();
    await screen.findByTestId("investigation-status");
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("shows cancel for each non-terminal status", async () => {
    for (const status of ["CREATED", "RUNNING", "ANALYZING"] as const) {
      getInvestigation.mockResolvedValue(investigation({ status }));
      renderPage();
      expect(await screen.findByRole("button", { name: "Cancel" })).toBeTruthy();
      cleanup();
    }
  });

  it("confirm calls cancelInvestigation and shows CANCELLED", async () => {
    getInvestigation.mockResolvedValue(investigation());
    cancelInvestigation.mockResolvedValue({ id: ID, status: "CANCELLED", context_closed: true });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Cancel investigation?")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Keep running" })).toBeTruthy();
    expect(cancelInvestigation).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel investigation" }));

    await waitFor(() => expect(screen.getByTestId("investigation-status").textContent).toContain("CANCELLED"));
    expect(cancelInvestigation).toHaveBeenCalledExactlyOnceWith(ID);
    expect(screen.getByText("Context closed")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("shows context close pending when the context was not closed", async () => {
    getInvestigation.mockResolvedValue(investigation());
    cancelInvestigation.mockResolvedValue({ id: ID, status: "CANCELLED", context_closed: false });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel investigation" }));

    expect(await screen.findByText("Context close pending")).toBeTruthy();
  });

  it("keep running closes the dialog without cancelling", async () => {
    getInvestigation.mockResolvedValue(investigation());
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Keep running" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(cancelInvestigation).not.toHaveBeenCalled();
  });

  it("409 shows problem alert", async () => {
    getInvestigation.mockResolvedValue(investigation());
    cancelInvestigation.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Conflict",
        status: 409,
        code: "investigation_terminal",
        detail: "investigation is COMPLETED",
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel investigation" }));

    const alert = await screen.findByTestId("problem-alert");
    expect(alert.textContent).toContain("investigation is COMPLETED");
    expect(alert.textContent).toContain("investigation_terminal");
    // The page re-reads so a stale non-terminal view is corrected.
    await waitFor(() => expect(getInvestigation).toHaveBeenCalledTimes(2));
  });
});

describe("investigation detail: tasks", () => {
  it("renders two task rows with attempt and reason", async () => {
    getInvestigation.mockResolvedValue(
      investigation({
        tasks: [
          task({ id: "tsk_1", agent_id: "sigint-agent", attempt: 1, status: "COMPLETED" }),
          task({ id: "tsk_2", agent_id: "geoint-agent", attempt: 2, status: "FAILED", status_reason: "llm_unavailable" }),
        ],
      }),
    );
    renderPage();

    const rows = await screen.findAllByTestId("task-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toContain("sigint-agent");
    expect(rows[0]!.textContent).toContain("attempt 1");
    expect(rows[0]!.textContent).toContain("COMPLETED");
    expect(within(rows[0]!).queryByTestId("task-reason")).toBeNull();
    expect(rows[1]!.textContent).toContain("geoint-agent");
    expect(rows[1]!.textContent).toContain("attempt 2");
    expect(rows[1]!.textContent).toContain("FAILED");
    expect(within(rows[1]!).getByTestId("task-reason").textContent).toBe("llm_unavailable");
  });
});
