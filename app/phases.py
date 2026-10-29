"""
SDLC phase order, in one place. `project_memory.sdlc_phase` is stored as text under a CHECK
constraint (migration 0009); the CHECK says which names are allowed, this says what order they
come in, which is what "include earlier phases" needs. tests/test_schema.py proves the two
lists agree, so a phase added in one place and not the other fails a test.
"""
from typing import Literal

SDLC_PHASES = ("planning", "analysis", "design", "development", "testing", "deployment")

SdlcPhase = Literal["planning", "analysis", "design", "development", "testing", "deployment"]


def phases_up_to(phase: str, include_earlier: bool = True) -> list[str]:
    """['design'] -> planning, analysis, design (default) or just design."""
    index = SDLC_PHASES.index(phase)  # ValueError for an unknown name: callers validate first
    return list(SDLC_PHASES[: index + 1]) if include_earlier else [phase]
