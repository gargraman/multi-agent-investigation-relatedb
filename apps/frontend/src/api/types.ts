/**
 * Public API types. Wire types are re-exported from src/api/generated (derived from
 * contracts/schemas via `npm run gen:types`). Only shapes with NO JSON Schema in contracts/
 * are declared here, and each says where it comes from.
 */
export type {
  AgentId,
  ErrorCode,
  Investigation,
  InvestigationStatus,
  OrchestratorTaskState,
  TaskView,
} from "./generated/investigation";
export type { InvestigationCreateRequest } from "./generated/investigation-create-request";
export type { InvestigationPage } from "./generated/investigation-page";
export type { TimelineEvent, TimelineEventType } from "./generated/timeline-event";
export type { FieldError, ProblemDetails } from "./generated/problem-details";

import type { InvestigationStatus, OrchestratorTaskState } from "./generated/investigation";

/**
 * Timeline payloads. The JSON Schema types `payload` as a free-form object, so these mirror the
 * Pydantic models in contracts/src/nexus_contracts/events/timeline.py by hand. Treat every field
 * as untrusted and read it through the guards in timeline.ts.
 */
export interface InvestigationStatusChangedPayload {
  from_status?: InvestigationStatus | null;
  to_status: InvestigationStatus;
  reason?: string | null;
}
export interface TaskStatusChangedPayload {
  task_id: string;
  agent_id: string;
  attempt: number;
  from_state?: OrchestratorTaskState | null;
  to_state: OrchestratorTaskState;
  reason?: string | null;
}

/** `GET /api/v1/scenarios` item, normalised. No schema in contracts/ yet: see normalizeScenarios. */
export interface ScenarioSummary {
  scenario_id: string;
  version: string | null;
  objective: string;
  task_count: number | null;
}

export type HealthLevel = "OK" | "DEGRADED" | "DOWN";

/** `GET /api/v1/system/health`, normalised. No schema in contracts/ yet. */
export interface SystemHealth {
  overall: HealthLevel;
  components: { name: string; status: string; detail: string | null }[];
}
