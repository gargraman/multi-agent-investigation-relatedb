"""Shared specialist-agent runtime: task store, Context Service client, manifest catalog, settings.

Moved out of ``nexus_sigint`` so a second agent (GEOINT) reuses the same plumbing. Requires the
``agent`` extra of ``nexus-shared``.
"""
