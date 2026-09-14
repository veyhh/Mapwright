"""Turn findings into concrete, verifiable changes.

Finding a problem is half the job. This module proposes the specific edit that
would resolve each finding, applies the ones that can be applied safely, and
refuses the ones that cannot.

Two rules govern everything here. Corrections are non-destructive: nothing is
deleted, because v0.1 showed that removing props to satisfy a metric leaves
density and composition no better and the level poorer. And corrections are
honest: a change that would need new geometry, a new route, or a designer's
judgement is proposed as manual work rather than faked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.issues import Issue
from mapwright.core.scene_ir import SceneIR, SceneObject
from mapwright.ecosystem.asset_director import AssetProvider, AssetRequirement


#: Findings that a geometric edit can resolve on its own.
AUTOMATIC_CODES = frozenset(
    {
        "objects_too_close",
        "squeeze_point",
        "density_imbalance",
        "visual_mass_imbalance",
        "composition_bias",
    }
)

#: Findings that need new geometry, new routes, or a design decision.
STRUCTURAL_CODES = frozenset(
    {
        "chokepoint",
        "dead_end",
        "low_route_diversity",
        "traversal_concentration",
        "disconnected_regions",
        "no_navigable_area",
        "isolated_route_node",
        "single_entrance_encounter",
        "no_flank_route",
        "no_high_ground",
        "no_retreat_route",
        "frontal_only_engagement",
        "missing_relief",
        "consecutive_high_intensity",
        "abrupt_transition",
        "flat_pacing",
        "travel_time_imbalance",
        "objective_access_imbalance",
        "choke_width_imbalance",
        "flank_route_imbalance",
        "empty_zone",
        "empty_region_share",
        "navigation_readability",
    }
)

_RING_SAMPLES = 16
_RING_STEPS = 8


class CorrectionKind(str, Enum):
    """What kind of edit a correction represents."""

    MOVE = "move"
    ROTATE = "rotate"
    RESCALE = "rescale"
    SUBSTITUTE = "substitute"
    REMOVE = "remove"
    STRUCTURAL = "structural"


@dataclass(frozen=True)
class Correction:
    """One proposed edit, with the finding that motivated it."""

    kind: CorrectionKind
    issue_code: str
    rationale: str
    object_id: str | None = None
    position: Vec3 | None = None
    rotation: Vec3 | None = None
    scale: Vec3 | None = None
    requirement: AssetRequirement | None = None
    replacement_asset: str | None = None
    zone: str | None = None
    manual: bool = False

    def describe(self) -> str:
        """Return a one-line instruction a designer or agent can act on."""
        if self.kind is CorrectionKind.MOVE and self.position is not None:
            return (
                f"move {self.object_id} to "
                f"({self.position.x:.2f}, {self.position.y:.2f}, {self.position.z:.2f}) "
                f"— {self.rationale}"
            )
        if self.kind is CorrectionKind.ROTATE and self.rotation is not None:
            return (
                f"rotate {self.object_id} to yaw {math.degrees(self.rotation.y):+.1f}° "
                f"— {self.rationale}"
            )
        if self.kind is CorrectionKind.RESCALE and self.scale is not None:
            return (
                f"rescale {self.object_id} to "
                f"({self.scale.x:.2f}, {self.scale.y:.2f}, {self.scale.z:.2f}) "
                f"— {self.rationale}"
            )
        if self.kind is CorrectionKind.SUBSTITUTE:
            target = self.replacement_asset or (
                self.requirement.description if self.requirement else "an alternative"
            )
            return f"replace {self.object_id} with {target} — {self.rationale}"
        if self.kind is CorrectionKind.REMOVE:
            return f"remove {self.object_id} — {self.rationale}"
        return f"{self.zone or 'scene'}: {self.rationale}"

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        data: dict = {
            "kind": self.kind.value,
            "issue": self.issue_code,
            "rationale": self.rationale,
            "manual": self.manual,
            "instruction": self.describe(),
        }
        if self.object_id is not None:
            data["object"] = self.object_id
        if self.position is not None:
            data["position"] = self.position.to_list()
        if self.rotation is not None:
            data["rotation"] = self.rotation.to_list()
        if self.scale is not None:
            data["scale"] = self.scale.to_list()
        if self.zone is not None:
            data["zone"] = self.zone
        if self.replacement_asset is not None:
            data["replacement_asset"] = self.replacement_asset
        if self.requirement is not None:
            data["requirement"] = self.requirement.description
        return data


@dataclass(frozen=True)
class CorrectionPlan:
    """Every correction proposed for one analysis pass."""

    corrections: tuple[Correction, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def automatic(self) -> tuple[Correction, ...]:
        """Return the corrections Mapwright can apply itself."""
        return tuple(item for item in self.corrections if not item.manual)

    @property
    def manual(self) -> tuple[Correction, ...]:
        """Return the corrections that need a designer."""
        return tuple(item for item in self.corrections if item.manual)

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        return {
            "corrections": [item.to_dict() for item in self.corrections],
            "automatic": len(self.automatic),
            "manual": len(self.manual),
            "skipped": [
                {"issue": code, "reason": reason} for code, reason in self.skipped
            ],
        }


@dataclass(frozen=True)
class AppliedCorrection:
    """The record of one correction actually written into a scene."""

    correction: Correction
    previous_position: Vec3 | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        data = self.correction.to_dict()
        if self.previous_position is not None:
            data["previous_position"] = self.previous_position.to_list()
        return data


def plan_corrections(
    context: AnalysisContext,
    issues: Sequence[Issue],
    asset_provider: AssetProvider | None = None,
    limit: int = 12,
) -> CorrectionPlan:
    """Propose a correction for each finding that has one.

    Automatic corrections are planned against a running copy of the scene, so
    two moves never chase each other into the same spot.
    """
    scene = context.scene
    corrections: list[Correction] = []
    skipped: list[tuple[str, str]] = []
    touched: set[str] = set()

    for issue in issues:
        if len(corrections) >= limit:
            skipped.append((issue.code, "correction limit reached for this pass"))
            continue
        if issue.code in STRUCTURAL_CODES:
            corrections.append(
                Correction(
                    kind=CorrectionKind.STRUCTURAL,
                    issue_code=issue.code,
                    rationale=issue.recommendation,
                    zone=issue.zone,
                    manual=True,
                )
            )
            continue
        if issue.code == "asset_overuse":
            correction = _plan_substitution(scene, issue, asset_provider)
            if correction is None:
                skipped.append((issue.code, "no alternative asset available in scene"))
            else:
                corrections.append(correction)
            continue
        if issue.code not in AUTOMATIC_CODES:
            continue

        planned, scene = _plan_geometric(context, scene, issue, touched)
        if planned is None:
            skipped.append(
                (issue.code, "no safe position found that resolves this finding")
            )
            continue
        corrections.append(planned)
        if planned.object_id is not None:
            touched.add(planned.object_id)

    return CorrectionPlan(corrections=tuple(corrections), skipped=tuple(skipped))


def apply_corrections(
    scene: SceneIR, plan: CorrectionPlan
) -> tuple[SceneIR, tuple[AppliedCorrection, ...]]:
    """Apply every automatic correction, returning the new scene and a record."""
    applied: list[AppliedCorrection] = []
    for correction in plan.automatic:
        if correction.object_id is None:
            continue
        target = scene.object_by_id(correction.object_id)
        if target is None:
            continue
        if correction.kind is CorrectionKind.MOVE and correction.position is not None:
            scene = scene.replace_object(target.moved_to(correction.position))
            applied.append(AppliedCorrection(correction, target.position))
        elif correction.kind is CorrectionKind.ROTATE and correction.rotation is not None:
            scene = scene.replace_object(replace(target, rotation=correction.rotation))
            applied.append(AppliedCorrection(correction))
        elif correction.kind is CorrectionKind.RESCALE and correction.scale is not None:
            scene = scene.replace_object(replace(target, scale=correction.scale))
            applied.append(AppliedCorrection(correction))
        elif correction.kind is CorrectionKind.SUBSTITUTE and correction.replacement_asset:
            scene = scene.replace_object(
                replace(target, asset=correction.replacement_asset)
            )
            applied.append(AppliedCorrection(correction))
        elif correction.kind is CorrectionKind.REMOVE:
            scene = scene.remove_object(correction.object_id)
            applied.append(AppliedCorrection(correction, target.position))
    return scene, tuple(applied)


def format_plan(plan: CorrectionPlan) -> str:
    """Format a correction plan for a terminal."""
    lines = [
        f"Corrections: {len(plan.automatic)} applicable, {len(plan.manual)} manual",
    ]
    if plan.automatic:
        lines.append("")
        lines.append("Applicable now:")
        lines.extend(f"- {item.describe()}" for item in plan.automatic)
    if plan.manual:
        lines.append("")
        lines.append("Needs a design decision:")
        lines.extend(f"- [{item.issue_code}] {item.describe()}" for item in plan.manual)
    if plan.skipped:
        lines.append("")
        lines.append("No correction proposed:")
        lines.extend(f"- {code}: {reason}" for code, reason in plan.skipped)
    return "\n".join(lines)


def _plan_geometric(
    context: AnalysisContext,
    scene: SceneIR,
    issue: Issue,
    touched: set[str],
) -> tuple[Correction | None, SceneIR]:
    """Plan one positional correction and fold it into the working scene."""
    if issue.code in ("objects_too_close", "squeeze_point"):
        correction = _plan_separation(context, scene, issue, touched)
    elif issue.code == "density_imbalance":
        correction = _plan_redistribution(context, scene, issue)
    else:
        correction = _plan_rebalance(context, scene, issue)
    if correction is None or correction.position is None or correction.object_id is None:
        return correction, scene
    target = scene.object_by_id(correction.object_id)
    if target is None:
        return None, scene
    return correction, scene.replace_object(target.moved_to(correction.position))


def _plan_separation(
    context: AnalysisContext,
    scene: SceneIR,
    issue: Issue,
    touched: set[str],
) -> Correction | None:
    """Move the smaller of two crowded objects far enough apart to read."""
    pair = [scene.object_by_id(identifier) for identifier in issue.subjects[:2]]
    if len(pair) != 2 or any(item is None for item in pair):
        return None
    first, second = pair[0], pair[1]
    mover, anchor = (
        (first, second) if first.size_score() <= second.size_score() else (second, first)
    )
    if mover.id in touched:
        mover, anchor = anchor, mover
    required = _required_separation(context, mover, anchor)
    direction = Vec3(
        mover.position.x - anchor.position.x, 0.0, mover.position.z - anchor.position.z
    )
    if direction.length() < 1e-6:
        direction = Vec3(1.0, 0.0, 0.0)
    preferred = anchor.position + direction.normalized() * required
    preferred = Vec3(preferred.x, mover.position.y, preferred.z)
    position = _find_safe_position(context, scene, mover, preferred)
    if position is None:
        return None
    return Correction(
        kind=CorrectionKind.MOVE,
        issue_code=issue.code,
        rationale=(
            f"separates {mover.name} from {anchor.name} to {required:.2f}m, "
            "clearing the crowded pair without removing either"
        ),
        object_id=mover.id,
        position=position,
        zone=issue.zone,
    )


def _plan_redistribution(
    context: AnalysisContext, scene: SceneIR, issue: Issue
) -> Correction | None:
    """Move one prop from the most crowded cell into the emptiest one."""
    cells = _cell_counts(context, scene)
    if not cells:
        return None
    occupied = {key: value for key, value in cells.items() if value}
    if len(occupied) < 2:
        return None
    densest = max(
        occupied.items(), key=lambda item: (len(item[1]), -item[0][0], -item[0][1])
    )[0]
    empty = [key for key, value in cells.items() if not value]
    if not empty:
        return None
    ground = context.ground
    if ground is None:
        return None
    size = context.config.thresholds.density_cell_size
    center = _cell_center(ground, densest, size)
    destination_cell = min(
        empty,
        key=lambda key: (
            _cell_center(ground, key, size).distance_xz(center),
            key[0],
            key[1],
        ),
    )
    mover = min(
        cells[densest], key=lambda obj: (obj.size_score(), obj.id)
    )
    preferred = _cell_center(ground, destination_cell, size)
    preferred = Vec3(preferred.x, mover.position.y, preferred.z)
    position = _find_safe_position(context, scene, mover, preferred)
    if position is None:
        return None
    return Correction(
        kind=CorrectionKind.MOVE,
        issue_code=issue.code,
        rationale=(
            f"redistributes {mover.name} from the densest cell into empty space, "
            "flattening the distribution without changing the prop count"
        ),
        object_id=mover.id,
        position=position,
        zone=issue.zone,
    )


def _plan_rebalance(
    context: AnalysisContext, scene: SceneIR, issue: Issue
) -> Correction | None:
    """Move one tall object from the heavy side of the level to the light side."""
    ground = context.ground
    if ground is None:
        return None
    center = ground.center
    measured = [obj for obj in scene.props if obj.bounds is not None]
    if len(measured) < 2:
        return None
    east = [obj for obj in measured if obj.position.x >= center.x]
    west = [obj for obj in measured if obj.position.x < center.x]
    north = [obj for obj in measured if obj.position.z >= center.z]
    south = [obj for obj in measured if obj.position.z < center.z]

    def mass(group: Iterable[SceneObject]) -> float:
        return sum(obj.world_size().y for obj in group)

    if abs(mass(east) - mass(west)) >= abs(mass(north) - mass(south)):
        heavier_east = mass(east) >= mass(west)
        source = east if heavier_east else west
        light_x = (ground.min.x + center.x) * 0.5 if heavier_east else (center.x + ground.max.x) * 0.5
        light_center = Vec3(light_x, center.y, center.z)
    else:
        heavier_north = mass(north) >= mass(south)
        source = north if heavier_north else south
        light_z = (ground.min.z + center.z) * 0.5 if heavier_north else (center.z + ground.max.z) * 0.5
        light_center = Vec3(center.x, center.y, light_z)
    if not source:
        return None
    mover = max(source, key=lambda obj: (obj.world_size().y, obj.id))
    preferred = Vec3(light_center.x, mover.position.y, light_center.z)
    position = _find_safe_position(context, scene, mover, preferred)
    if position is None:
        return None
    return Correction(
        kind=CorrectionKind.MOVE,
        issue_code=issue.code,
        rationale=(
            f"moves {mover.name}, one of the tallest elements, toward the sparse "
            "side so visual mass reads evenly from every approach"
        ),
        object_id=mover.id,
        position=position,
        zone=issue.zone,
    )


def _plan_substitution(
    scene: SceneIR, issue: Issue, asset_provider: AssetProvider | None
) -> Correction | None:
    """Propose swapping one instance of an overused asset for a rarer one."""
    overused = issue.subjects[0] if issue.subjects else None
    if overused is None:
        return None
    instances = [
        obj for obj in scene.props if (obj.asset or obj.name) == overused
    ]
    if not instances:
        return None
    mover = sorted(instances, key=lambda obj: obj.id)[-1]
    requirement = AssetRequirement(
        description=f"an alternative to {mover.name} at a similar scale",
        zone=issue.zone,
        minimum_size=mover.size_score() * 0.6,
        maximum_size=mover.size_score() * 1.6,
        exclude_assets=(overused,),
    )
    replacement: str | None = None
    if asset_provider is not None:
        candidates = asset_provider.search(requirement)
        if candidates:
            replacement = candidates[0].path or candidates[0].identifier
    return Correction(
        kind=CorrectionKind.SUBSTITUTE,
        issue_code=issue.code,
        rationale=(
            f"breaks up repetition of {mover.name} by varying one instance "
            "rather than deleting placed content"
        ),
        object_id=mover.id,
        requirement=requirement,
        replacement_asset=replacement,
        zone=issue.zone,
        manual=replacement is None,
    )


def _required_separation(
    context: AnalysisContext, mover: SceneObject, anchor: SceneObject
) -> float:
    """Return the centre distance that clears both footprints and the player."""
    minimum = context.config.thresholds.minimum_spacing
    radii = 0.0
    for obj in (mover, anchor):
        box = obj.world_bounds()
        if box is not None:
            radii += math.hypot(box.width, box.depth) * 0.5
    return max(minimum, radii + context.config.player.diameter)


def _find_safe_position(
    context: AnalysisContext,
    scene: SceneIR,
    mover: SceneObject,
    preferred: Vec3,
) -> Vec3 | None:
    """Find a spot near the preferred one that crowds nothing and fits the ground.

    Candidates are tried on widening rings in a fixed order, so the same scene
    always produces the same correction.
    """
    ground = context.ground
    if ground is None:
        return None
    others = [
        obj
        for obj in scene.props
        if obj.id != mover.id and obj.bounds is not None
    ]
    step = max(context.config.thresholds.minimum_spacing, context.config.player.diameter)
    candidates = [preferred]
    for ring in range(1, _RING_STEPS + 1):
        radius = step * ring
        for index in range(_RING_SAMPLES):
            angle = 2.0 * math.pi * index / _RING_SAMPLES
            candidates.append(
                Vec3(
                    preferred.x + radius * math.cos(angle),
                    preferred.y,
                    preferred.z + radius * math.sin(angle),
                )
            )
    for candidate in candidates:
        if _is_safe(context, mover, candidate, others, ground):
            return candidate
    return None


def _is_safe(
    context: AnalysisContext,
    mover: SceneObject,
    candidate: Vec3,
    others: Sequence[SceneObject],
    ground: Bounds,
) -> bool:
    """Return whether a candidate position is inside the ground and uncrowded."""
    box = mover.bounds
    if box is None:
        return False
    moved = mover.moved_to(candidate)
    world = moved.world_bounds()
    if world is None:
        return False
    margin = context.config.player.radius
    if (
        world.min.x < ground.min.x + margin
        or world.max.x > ground.max.x - margin
        or world.min.z < ground.min.z + margin
        or world.max.z > ground.max.z - margin
    ):
        return False
    minimum = context.config.thresholds.minimum_spacing
    for other in others:
        if candidate.distance_to(other.position) < minimum:
            return False
        other_box = other.world_bounds()
        if other_box is not None and world.overlaps_xz(other_box):
            return False
    return True


def _cell_counts(
    context: AnalysisContext, scene: SceneIR
) -> dict[tuple[int, int], list[SceneObject]]:
    """Group props into the same equal-area cells the density check uses."""
    ground = context.ground
    if ground is None or ground.width <= 0 or ground.depth <= 0:
        return {}
    size = context.config.thresholds.density_cell_size
    columns = max(1, math.ceil(ground.width / size))
    rows = max(1, math.ceil(ground.depth / size))
    cells: dict[tuple[int, int], list[SceneObject]] = {
        (column, row): [] for column in range(columns) for row in range(rows)
    }
    for obj in scene.props:
        if not ground.contains_xz(obj.position):
            continue
        column = min(
            int((obj.position.x - ground.min.x) / ground.width * columns), columns - 1
        )
        row = min(
            int((obj.position.z - ground.min.z) / ground.depth * rows), rows - 1
        )
        cells[(column, row)].append(obj)
    return cells


def _cell_center(ground: Bounds, cell: tuple[int, int], size: float) -> Vec3:
    """Return the world centre of one density cell."""
    columns = max(1, math.ceil(ground.width / size))
    rows = max(1, math.ceil(ground.depth / size))
    width = ground.width / columns
    depth = ground.depth / rows
    return Vec3(
        ground.min.x + (cell[0] + 0.5) * width,
        ground.center.y,
        ground.min.z + (cell[1] + 0.5) * depth,
    )
