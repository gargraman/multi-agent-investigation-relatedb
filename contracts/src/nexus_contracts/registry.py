"""Registry of contract models whose JSON Schema is exported and committed.

``scripts/gen_schemas.py`` writes ``contracts/schemas/<name>.schema.json`` for every entry, and
its ``--check`` mode (run by the test suite) fails if the committed files drift.
Add a model here when it becomes part of a public contract.
"""

from pydantic import BaseModel

from nexus_contracts.a2a.task import AgentCard, TaskRequest, TaskResult, TaskSubmitResponse
from nexus_contracts.api.assessments import (
    Assessment,
    AssessmentSubmission,
    AssessmentSubmissionResponse,
)
from nexus_contracts.api.findings import FindingBatchRequest, FindingBatchResponse
from nexus_contracts.api.investigations import (
    Investigation,
    InvestigationCreateRequest,
    InvestigationPage,
)
from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.api.retraction import RetractEvidenceRequest, RetractionImpact
from nexus_contracts.events.timeline import TimelineEvent, TimelinePage
from nexus_contracts.scenario.fixtures import CommsLog, SightingLog
from nexus_contracts.scenario.manifest import ScenarioManifest

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "problem-details": ProblemDetails,
    # Agent task API (§12.2)
    "agent-card": AgentCard,
    "task-request": TaskRequest,
    "task-submit-response": TaskSubmitResponse,
    "task-result": TaskResult,
    # Context Service API (§12.3)
    "finding-batch-request": FindingBatchRequest,
    "finding-batch-response": FindingBatchResponse,
    "assessment-submission": AssessmentSubmission,
    "assessment-submission-response": AssessmentSubmissionResponse,
    "assessment": Assessment,
    "retract-evidence-request": RetractEvidenceRequest,
    "retraction-impact": RetractionImpact,
    # Frontend API (§12.1)
    "investigation-create-request": InvestigationCreateRequest,
    "investigation": Investigation,
    "investigation-page": InvestigationPage,
    "timeline-event": TimelineEvent,
    "timeline-page": TimelinePage,
    # Scenario pack (§11.3)
    "scenario-manifest": ScenarioManifest,
    "fixture-comms-log": CommsLog,
    "fixture-sighting-log": SightingLog,
}
