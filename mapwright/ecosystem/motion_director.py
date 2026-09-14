"""Motion sourcing seam.

Some level decisions imply animation: an NPC patrol route needs walk and turn
cycles, a moving platform needs a loop. Mapwright does not generate motion and
does not depend on anything that does — it only states the requirement in a
form another tool can answer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from mapwright.core.geometry import Vec3


@dataclass(frozen=True)
class MotionRequirement:
    """A motion a level decision implies, stated in design terms."""

    description: str
    kind: str = "locomotion"
    zone: str | None = None
    waypoints: tuple[Vec3, ...] = ()
    duration: float | None = None
    looping: bool = True

    @property
    def path_length(self) -> float:
        """Return the total distance along the requested waypoints."""
        return sum(
            first.distance_to(second)
            for first, second in zip(self.waypoints, self.waypoints[1:])
        )


@dataclass(frozen=True)
class MotionCandidate:
    """One motion offered in answer to a requirement."""

    identifier: str
    name: str
    path: str | None
    kind: str
    provider: str = "unknown"
    notes: str | None = None


class MotionProvider(ABC):
    """Finds or generates motion that satisfies a requirement."""

    name: str = "provider"

    @abstractmethod
    def request(self, requirement: MotionRequirement) -> tuple[MotionCandidate, ...]:
        """Return motion candidates for a requirement, best first."""

    def available(self) -> tuple[bool, str]:
        """Return whether this provider can answer requests right now."""
        return True, f"{self.name} available"


class MotionDirectorProvider(MotionProvider):
    """Seam for an external Motion Director service, not wired up in v0.2."""

    name = "motion-director"

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def available(self) -> tuple[bool, str]:
        """Return that the external service is not connected in this version."""
        return False, "Motion Director integration is not enabled in Mapwright v0.2."

    def request(self, requirement: MotionRequirement) -> tuple[MotionCandidate, ...]:
        """Raise, explaining that no external Motion Director is connected."""
        raise NotImplementedError(
            "Motion Director integration is a v0.2 seam, not a shipped feature. "
            "Implement MotionProvider.request against your own motion service."
        )
