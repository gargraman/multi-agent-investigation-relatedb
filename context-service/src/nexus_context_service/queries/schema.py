"""Schema bootstrap DDL (spec §9.3; syntax confirmed by the S0 spike).

Idempotent (``IF NOT EXISTS``), run at startup in autocommit mode. The point index on
``Location.position`` is **forward compatibility only**: Relate 1.2.0's planner never uses it
(ADR-001 R6), so nothing may claim indexed spatial performance.
"""

_ID_LABELS = (
    "Finding",
    "Assertion",
    "Assessment",
    "Decision",
    "AuditEvent",
    "Submission",
    "Investigation",
)

SCHEMA_DDL: tuple[str, ...] = (
    *(
        f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
        for label in _ID_LABELS
    ),
    "CREATE CONSTRAINT entity_uid IF NOT EXISTS FOR (n:Entity) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT evidence_uid IF NOT EXISTS FOR (n:Evidence) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT submission_idem IF NOT EXISTS "
    "FOR (n:Submission) REQUIRE n.idempotency_key IS UNIQUE",
    "CREATE INDEX finding_inv IF NOT EXISTS FOR (n:Finding) ON (n.investigation_id)",
    "CREATE INDEX entity_inv IF NOT EXISTS FOR (n:Entity) ON (n.investigation_id)",
    "CREATE INDEX evidence_inv IF NOT EXISTS FOR (n:Evidence) ON (n.investigation_id)",
    "CREATE INDEX entity_manifest IF NOT EXISTS FOR (n:Entity) ON (n.manifest_id)",
    "CREATE INDEX assertion_type IF NOT EXISTS FOR (n:Assertion) ON (n.type)",
    "CREATE POINT INDEX location_position IF NOT EXISTS FOR (n:Location) ON (n.position)",
)
