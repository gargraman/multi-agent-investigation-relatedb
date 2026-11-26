/**
 * The ONLY module that talks HTTP to the orchestrator. Same-origin, relative `/api/v1` paths
 * (nginx in Compose, Vite proxy in dev). Every error is surfaced as an ApiError carrying an
 * RFC 7807 ProblemDetails, synthesised when the server (or the network) did not send one.
 */
import type {
  HealthLevel,
  Investigation,
  InvestigationCancelResponse,
  InvestigationCreateRequest,
  InvestigationPage,
  ProblemDetails,
  ScenarioSummary,
  SystemHealth,
} from "./types";

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly problem: ProblemDetails;
  constructor(problem: ProblemDetails) {
    super(problem.detail ?? problem.title);
    this.name = "ApiError";
    this.problem = problem;
  }
  get status(): number {
    return this.problem.status;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function toProblem(status: number, statusText: string, body: unknown): ProblemDetails {
  if (isRecord(body) && typeof body.title === "string") {
    const errors = Array.isArray(body.errors)
      ? body.errors.filter(isRecord).map((e) => ({
          loc: Array.isArray(e.loc) ? e.loc.filter((x) => typeof x === "string" || typeof x === "number") : [],
          msg: String(e.msg ?? ""),
          type: String(e.type ?? ""),
        }))
      : null;
    return {
      type: str(body.type) ?? "about:blank",
      title: body.title,
      status: typeof body.status === "number" ? body.status : status,
      detail: str(body.detail),
      instance: str(body.instance),
      code: str(body.code),
      errors,
    };
  }
  // FastAPI's default {detail: "..."} or a proxy HTML page: keep the status honest.
  const title = statusText || `HTTP ${status}`;
  const raw = isRecord(body) && typeof body.detail === "string" ? body.detail : null;
  return { type: "about:blank", title, status, detail: raw && raw !== title ? raw : null };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError({
      type: "about:blank",
      title: "Orchestrator unreachable",
      status: 0,
      detail: "The request did not reach the API. Check that the stack is running.",
    });
  }
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!res.ok) throw new ApiError(toProblem(res.status, res.statusText, body));
  return body as T;
}

/* ---------- scenarios: shape not yet in contracts/, normalise defensively ---------- */

export function normalizeScenarios(body: unknown): ScenarioSummary[] {
  const list: unknown[] = Array.isArray(body)
    ? body
    : isRecord(body)
      ? ((Array.isArray(body.items) && body.items) || (Array.isArray(body.scenarios) && body.scenarios) || [])
      : [];
  const out: ScenarioSummary[] = [];
  for (const raw of list) {
    if (!isRecord(raw)) continue;
    const id = str(raw.scenario_id) ?? str(raw.id);
    if (!id) continue;
    const template = isRecord(raw.template) ? raw.template : null;
    const tasks = Array.isArray(raw.tasks) ? raw.tasks : template && Array.isArray(template.tasks) ? template.tasks : null;
    out.push({
      scenario_id: id,
      version: str(raw.version),
      objective: str(raw.objective) ?? "",
      task_count: typeof raw.task_count === "number" ? raw.task_count : tasks ? tasks.length : null,
    });
  }
  return out;
}

export function normalizeHealth(body: unknown): SystemHealth {
  const levels: HealthLevel[] = ["OK", "DEGRADED", "DOWN"];
  const rec = isRecord(body) ? body : {};
  const overallRaw = String(rec.overall ?? rec.status ?? "").toUpperCase();
  const overall = (levels as string[]).includes(overallRaw) ? (overallRaw as HealthLevel) : "DEGRADED";
  const rawComponents: unknown[] = Array.isArray(rec.components)
    ? rec.components
    : isRecord(rec.components)
      ? Object.entries(rec.components).map(([name, v]) => (isRecord(v) ? { name, ...v } : { name, status: v }))
      : [];
  const components = rawComponents.filter(isRecord).map((c) => ({
    name: str(c.name) ?? str(c.component) ?? str(c.id) ?? "unknown",
    status: String(c.status ?? c.state ?? "unknown"),
    detail: str(c.detail) ?? str(c.message),
  }));
  return { overall, components };
}

/* ---------- endpoints ---------- */

export const api = {
  async listScenarios(signal?: AbortSignal): Promise<ScenarioSummary[]> {
    return normalizeScenarios(await request<unknown>("/scenarios", { signal }));
  },
  createInvestigation(body: InvestigationCreateRequest): Promise<Investigation> {
    return request<Investigation>("/investigations", { method: "POST", body: JSON.stringify(body) });
  },
  listInvestigations(signal?: AbortSignal, cursor?: string): Promise<InvestigationPage> {
    const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return request<InvestigationPage>(`/investigations${q}`, { signal });
  },
  getInvestigation(id: string, signal?: AbortSignal): Promise<Investigation> {
    return request<Investigation>(`/investigations/${encodeURIComponent(id)}`, { signal });
  },
  /** 200 {id, status, context_closed}; 404 unknown; 409 `investigation_terminal` if already terminal. */
  cancelInvestigation(id: string): Promise<InvestigationCancelResponse> {
    return request<InvestigationCancelResponse>(`/investigations/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  },
  async systemHealth(signal?: AbortSignal): Promise<SystemHealth> {
    return normalizeHealth(await request<unknown>("/system/health", { signal }));
  },
  eventsUrl(id: string): string {
    return `${API_BASE}/investigations/${encodeURIComponent(id)}/events`;
  },
};
