"""Asset sourcing seam.

When Mapwright decides a zone needs "a medium ruined stone arch", something
else has to find one. This is the interface that separates the two jobs.
Mapwright never depends on an external asset service: the built-in provider
searches what the scene already uses, and an external Asset Director can be
plugged in behind the same contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from mapwright.core.scene_ir import SceneIR, SceneObject


@dataclass(frozen=True)
class AssetRequirement:
    """What a zone needs, stated in design terms rather than by filename."""

    description: str
    zone: str | None = None
    tags: tuple[str, ...] = ()
    minimum_size: float | None = None
    maximum_size: float | None = None
    exclude_assets: tuple[str, ...] = ()

    def matches_size(self, size: float) -> bool:
        """Return whether a candidate's size score falls in the requested band."""
        if self.minimum_size is not None and size < self.minimum_size:
            return False
        if self.maximum_size is not None and size > self.maximum_size:
            return False
        return True


@dataclass(frozen=True)
class AssetCandidate:
    """One asset offered in answer to a requirement."""

    identifier: str
    name: str
    path: str | None
    size_score: float
    tags: tuple[str, ...] = ()
    provider: str = "unknown"
    notes: str | None = None


class AssetProvider(ABC):
    """Finds assets that satisfy a design requirement."""

    name: str = "provider"

    @abstractmethod
    def search(self, requirement: AssetRequirement) -> tuple[AssetCandidate, ...]:
        """Return candidates for a requirement, best first."""

    def available(self) -> tuple[bool, str]:
        """Return whether this provider can answer searches right now."""
        return True, f"{self.name} available"


@dataclass
class SceneAssetProvider(AssetProvider):
    """Offers assets the scene already uses.

    Substituting a repeated prop for one already in the level is the safe
    correction: the asset is known to load, match the art style, and sit at a
    plausible scale.
    """

    scene: SceneIR
    name: str = "scene"
    _catalog: tuple[AssetCandidate, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        seen: dict[str, SceneObject] = {}
        for obj in self.scene.props:
            key = obj.asset or obj.name
            if key not in seen:
                seen[key] = obj
        self._catalog = tuple(
            AssetCandidate(
                identifier=key,
                name=obj.name,
                path=obj.asset,
                size_score=obj.size_score(),
                tags=obj.tags,
                provider=self.name,
            )
            for key, obj in sorted(seen.items())
        )

    def search(self, requirement: AssetRequirement) -> tuple[AssetCandidate, ...]:
        """Return in-scene assets matching the requested size band and tags."""
        wanted = {tag.casefold() for tag in requirement.tags}
        words = {
            word
            for word in requirement.description.casefold().replace("-", " ").split()
            if len(word) > 2
        }
        matches = []
        for candidate in self._catalog:
            if candidate.identifier in requirement.exclude_assets:
                continue
            if not requirement.matches_size(candidate.size_score):
                continue
            haystack = f"{candidate.name} {candidate.path or ''}".casefold()
            tags = {tag.casefold() for tag in candidate.tags}
            score = len(wanted & tags) + sum(1 for word in words if word in haystack)
            matches.append((score, candidate))
        return tuple(
            candidate
            for score, candidate in sorted(
                matches, key=lambda item: (-item[0], item[1].identifier)
            )
        )


class AssetDirectorProvider(AssetProvider):
    """Seam for an external Asset Director service.

    Deliberately not wired up in v0.2: Mapwright must stay useful with no
    network service, so this raises rather than silently returning nothing.
    """

    name = "asset-director"

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def available(self) -> tuple[bool, str]:
        """Return that the external service is not connected in this version."""
        return False, "Asset Director integration is not enabled in Mapwright v0.2."

    def search(self, requirement: AssetRequirement) -> tuple[AssetCandidate, ...]:
        """Raise, explaining that no external Asset Director is connected."""
        raise NotImplementedError(
            "Asset Director integration is a v0.2 seam, not a shipped feature. "
            "Use SceneAssetProvider, or implement AssetProvider.search against "
            "your own asset service."
        )


def describe_requirements(
    requirements: Sequence[AssetRequirement],
) -> tuple[str, ...]:
    """Return one readable line per requirement, for reports and hand-off."""
    return tuple(
        f"{requirement.zone or 'scene'}: {requirement.description}"
        + (f" [{', '.join(requirement.tags)}]" if requirement.tags else "")
        for requirement in requirements
    )
