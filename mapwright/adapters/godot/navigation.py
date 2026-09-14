"""Seam for engine-mode navigation backed by Godot's NavigationServer.

Mapwright's supported navigation is the engine-independent "approximate" mode
in :mod:`mapwright.core.metrics`: an occupancy grid rasterized from Scene IR
footprints. Engine mode would instead bake a real navigation mesh inside a
running Godot project and read the polygons back. That needs a project export
and an in-engine round trip Mapwright does not yet drive, so this module
declares the interface and refuses honestly rather than shipping a partial
integration whose numbers could not be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Vec3


@dataclass(frozen=True)
class NavigationData:
    """A baked navigation surface: walkable polygons in canonical world space."""

    source: str
    polygons: tuple[tuple[Vec3, ...], ...] = ()
    agent_radius: float = 0.0
    cell_size: float = 0.0

    @property
    def empty(self) -> bool:
        """Return whether the bake produced no walkable surface."""
        return not self.polygons


class GodotNavigationProvider:
    """Bakes navigation with Godot's NavigationServer instead of the grid."""

    engine = "godot"
    mode = "engine"

    def available(self, config: MapwrightConfig) -> tuple[bool, str]:
        """Return whether engine-mode navigation can run now, and why not."""
        return False, (
            "Engine-mode navigation is not available in Mapwright v0.2: baking "
            "requires a running Godot project export that Mapwright does not "
            "drive yet. Set navigation.mode to 'approximate'."
        )

    def bake(self, scene_path: str | Path, config: MapwrightConfig) -> NavigationData:
        """Bake a navigation mesh for a scene; not wired up in v0.2."""
        raise NotImplementedError(
            f"Godot engine-mode navigation cannot bake '{scene_path}': v0.2 has no "
            "in-engine bake step. Use the approximate occupancy-grid mode "
            "(navigation.mode: approximate), which is the supported path."
        )
