"""Nexus Context Service: the only gateway to the shared Relate graph."""

import os

# ADR-001 R4: Relate telemetry off. Set here, in the package root, so it is in os.environ before
# any submodule can ``import relate``. Assigned (not setdefault): nothing may turn it back on.
os.environ["DO_NOT_TRACK"] = "1"
os.environ["RELATE_TELEMETRY"] = "0"

__version__ = "0.1.0"
