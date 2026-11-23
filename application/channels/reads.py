"""Public channel hydration read boundary.

Collection owns accepted worksets, estimates and readiness. HTTP callers use
this read facade while supplying the existing repository scope explicitly.
"""
from collection.api import load_initial_hydration_state

__all__ = ["load_initial_hydration_state"]
