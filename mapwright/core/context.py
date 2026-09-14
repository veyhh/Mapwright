"""The shared analysis context.

Building an occupancy grid and a route graph is the expensive part of an
analysis, and almost every check needs one or both. The context derives each
once, on demand, and hands the same objects to every analyzer.

Derivation can legitimately fail — a scene with no ground has no walkable
space to measure — so failures are recorded as a reason rather than raised.
Analyzers that need a grid report that they could not run and why, instead of
crashing the whole report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Bounds
from mapwright.core.graph import GraphError, RouteGraph, build_route_graph
from mapwright.core.metrics import (
    GridError,
    OccupancyGrid,
    build_occupancy_grid,
)
from mapwright.core.scene_ir import SceneIR
from mapwright.core.zones import ZoneSummary, summarize_zones


@dataclass
class AnalysisContext:
    """Everything an analyzer needs about one scene, derived once."""

    scene: SceneIR
    config: MapwrightConfig
    _failures: dict[str, str] = field(default_factory=dict, repr=False)

    @cached_property
    def ground(self) -> Bounds | None:
        """Return the playable area, or ``None`` when the scene declares none."""
        return self.scene.ground_bounds()

    @cached_property
    def grid(self) -> OccupancyGrid | None:
        """Return the walkability grid, or ``None`` when one cannot be built."""
        try:
            return build_occupancy_grid(
                self.scene,
                cell_size=self.config.thresholds.navigation_cell_size,
                agent_radius=self.config.minimum_path_width * 0.5,
            )
        except GridError as error:
            self._failures["grid"] = str(error)
            return None

    @cached_property
    def clearance(self) -> tuple[tuple[float, ...], ...] | None:
        """Return the per-cell distance to the nearest obstacle."""
        return self.grid.clearance_field() if self.grid is not None else None

    @cached_property
    def graph(self) -> RouteGraph | None:
        """Return the route graph, or ``None`` when topology cannot be derived."""
        if self.grid is None:
            self._failures.setdefault("graph", self.reason("grid") or "no walkable grid")
            return None
        try:
            return build_route_graph(
                self.scene,
                self.grid,
                walk_speed=self.config.player.walk_speed,
                clearance=self.clearance,
            )
        except GraphError as error:
            self._failures["graph"] = str(error)
            return None

    @cached_property
    def zone_summaries(self) -> tuple[ZoneSummary, ...]:
        """Return measured contents for every declared zone."""
        return summarize_zones(
            self.scene,
            self.grid,
            cover_height_range=(
                self.config.thresholds.cover_height_min,
                self.config.thresholds.cover_height_max,
            ),
            high_ground_delta=self.config.thresholds.high_ground_delta,
        )

    def reason(self, name: str) -> str | None:
        """Return why a derived structure is unavailable, when it is."""
        return self._failures.get(name)

    @property
    def grid_reason(self) -> str:
        """Return a reportable explanation for a missing grid."""
        return self.reason("grid") or "the scene has no measurable walkable area"

    @property
    def graph_reason(self) -> str:
        """Return a reportable explanation for a missing route graph."""
        return self.reason("graph") or "the scene has no derivable route topology"
