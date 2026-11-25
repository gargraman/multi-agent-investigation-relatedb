"""Cypher for the Context Service (ADR-001: Cypher lives in ``queries/``, never in routes).

Every statement is parameterized (``$param``); no value is ever interpolated into Cypher text.
"""
