"""Structured visual review: measured findings, and a door for observed ones.

Mapwright cannot see. What it can do is measure the geometry that decides what
a capture would show — where mass sits, whether silhouettes differ, whether a
landmark is occluded — and state each finding with the number behind it. Every
measured finding here is marked advisory and says in its evidence that it came
from geometry, so nobody mistakes it for having opened an image.

:func:`ingest_observations` is the other half: an agent or a human who did open
the captures feeds observations back through the same validation and the same
:class:`~mapwright.core.issues.Issue` structure, so both halves of a review
land in one report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3, segment_intersects_aabb
from mapwright.core.issues import (
    Category,
    Issue,
    IssueCollector,
    Severity,
    sort_issues,
)
from mapwright.core.scene_ir import MarkerPoint, ObjectType, SceneObject
from mapwright.design.composition import (
    OPPOSITE_SIDE,
    CompositionAnalysis,
    MassSlice,
    analyze_composition,
)
from mapwright.design.readability import (
    ReadabilityAnalysis,
    RegionReadability,
    analyze_readability,
)


class VisualReviewError(ValueError):
    """Raised when an observed finding cannot be read as a Mapwright issue."""


TOP_DOWN = "top_down"
ISO_NE = "iso_ne"
ISO_SW = "iso_sw"

#: Prefix every measured finding carries, so a proxy is never read as a render.
GEOMETRIC = "Geometric measurement, no render inspected:"

#: Height variation below this reads as one flat mass in an isometric view.
MINIMUM_SILHOUETTE_VARIETY = 0.15

#: Objects per player footprint of walkable space that starts to read as noise:
#: one object for every four places a player could stand.
CLUTTER_INDEX_LIMIT = 0.25

#: Share of a region made of one identical element before it reads as copy-paste.
IDENTICAL_COPY_SHARE = 60.0

#: Fewest identical copies that can read as repetition rather than a pair.
MINIMUM_REPEAT_GROUP = 4

#: Fewest objects a region needs before its composition can be judged at all.
MINIMUM_REGION_OBJECTS = 4

#: Fewest tall objects before their spread says anything about the skyline.
MINIMUM_TALL_SAMPLE = 3

#: Fewest placed objects before where they sit counts as a distribution.
MINIMUM_MASS_SAMPLE = 4

#: Fewest placed objects before an undeclared empty patch is worth reporting.
MINIMUM_POPULATED_SCENE = 8

#: Playable diagonal below which a player can orient without any landmark.
MINIMUM_ORIENTATION_SPAN = 30.0

_MEANINGS: dict[str, tuple[str, str]] = {
    "visual_mass_imbalance": (
        "Visual mass is concentrated on one side of the level.",
        "A level whose bulk sits on one side reads as unfinished from every "
        "angle and pulls the player's attention away from the lighter half.",
    ),
    "landmark_weakness": (
        "No element stands out enough to act as a landmark.",
        "Players navigate by silhouette. Without one element clearly larger "
        "than its neighbours, a space gives them nothing to steer by.",
    ),
    "excessive_repetition": (
        "One identical element makes up most of a region.",
        "Repeated identical props flatten a space into wallpaper: the player "
        "stops reading individual objects and loses track of where they are.",
    ),
    "poor_silhouette": (
        "Objects in a region are all the same height.",
        "Uniform heights merge into a single band in any angled view, so the "
        "space reads as one mass instead of as distinct, memorable objects.",
    ),
    "clutter": (
        "A region packs more objects than its walkable space supports.",
        "Dense placement hides sightlines and cover behind noise, and makes "
        "movement feel obstructed even where the path is technically clear.",
    ),
    "empty_zone": (
        "A large share of a region holds nothing at all.",
        "Empty ground gives the eye nothing to rest on and the player nothing "
        "to do, which reads as unfinished space rather than deliberate relief.",
    ),
    "contrast_problem": (
        "Elements that should read as different look alike.",
        "Without contrast in value, colour, or material, two objects with "
        "different gameplay roles read as the same thing.",
    ),
    "composition_bias": (
        "Content is unevenly distributed across the level's quadrants.",
        "A composition weighted into one corner leaves the rest of the frame "
        "empty in wide views and hides how much of the level is actually used.",
    ),
    "sightline_block": (
        "Geometry hides an element the player is meant to see.",
        "An occluded landmark or objective cannot do its job: the player "
        "cannot aim at what they cannot see from where they start.",
    ),
    "navigation_readability": (
        "The level offers nothing to orient by.",
        "With no visible landmark hierarchy, players navigate by trial and "
        "error and re-walk ground they have already covered.",
    ),
}

#: Every finding code the visual review may carry, measured or observed.
VISUAL_ISSUE_CODES = tuple(sorted(_MEANINGS))

_OBSERVATION_REQUIRED = ("issue", "evidence", "suggestion")
_OBSERVATION_OPTIONAL = ("zone", "view", "severity")

#: Review vocabulary an agent is likely to use, mapped onto Mapwright severities.
_SEVERITY_ALIASES = {
    "low": Severity.INFO,
    "medium": Severity.WARNING,
    "high": Severity.ERROR,
}


@dataclass(frozen=True)
class VisualReport:
    """Visual findings alongside the measurements that produced them."""

    issues: tuple[Issue, ...]
    composition: CompositionAnalysis
    readability: ReadabilityAnalysis

    @property
    def measured(self) -> bool:
        """Return whether any geometric measurement was possible."""
        return self.composition.measured or self.readability.measured

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "composition": self.composition.to_dict(),
            "readability": self.readability.to_dict(),
        }


def review_visual(context: AnalysisContext) -> VisualReport:
    """Review a scene's composition and readability from its geometry alone."""
    composition = analyze_composition(context)
    readability = analyze_readability(context)
    collector = IssueCollector(Category.VISUAL)

    _review_mass(collector, context, composition)
    _review_quadrants(collector, context, composition)
    _review_regions(collector, context, readability)
    _review_sightlines(collector, context)

    return VisualReport(
        issues=collector.result(),
        composition=composition,
        readability=readability,
    )


def format_report(report: VisualReport) -> str:
    """Render a visual review as terminal text."""
    lines = ["VISUAL REVIEW (geometric proxies, no render inspected)", ""]
    composition = report.composition
    if composition.measured:
        for split in composition.axes:
            lines.append(
                f"{split.axis.upper()}  {split.high.name} {split.high.volume_share:.0f}% "
                f"/ {split.low.name} {split.low.volume_share:.0f}% of visual mass"
            )
        if composition.tall_count:
            shares = " / ".join(
                f"{side} {share:.0f}%" for side, share in composition.tall_distribution
            )
            lines.append(f"TALL PROPS ({composition.tall_count})  {shares}")
        if composition.empty_quadrants:
            lines.append(f"EMPTY QUADRANTS  {', '.join(composition.empty_quadrants)}")
    else:
        lines.append(f"Composition not measured: {composition.reason}")

    lines.append("")
    if report.readability.measured:
        lines.append(f"REGIONS ({report.readability.source})")
        for region in report.readability.regions:
            lines.append(
                f"  {region.name}: {region.object_count} objects, "
                f"clutter {region.clutter_index:.2f}, "
                f"silhouette variety {region.silhouette_variety:.2f}, "
                f"empty {region.empty_ratio:.0f}%, "
                f"landmark x{region.landmark_strength:.2f}"
            )
    else:
        lines.append(f"Readability not measured: {report.readability.reason}")

    lines.append("")
    if not report.issues:
        lines.append("No visual findings from geometry.")
    for issue in report.issues:
        lines.append(issue.to_text())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def ingest_observations(
    observations: Sequence[Mapping[str, Any]], context: AnalysisContext
) -> tuple[Issue, ...]:
    """Convert findings observed in a rendered capture into Mapwright issues.

    Observed findings are not advisory: somebody looked at the image. Every
    field is validated against this scene so a typo in a code, zone, or view
    fails loudly instead of entering the report as a silent default.
    """
    issues: list[Issue] = []
    zone_names = {zone.name for zone in context.scene.zones}
    views = tuple(context.config.capture_views)
    for index, observation in enumerate(observations):
        label = f"observation[{index}]"
        if not isinstance(observation, Mapping):
            raise VisualReviewError(f"{label} must be a mapping.")
        unknown = sorted(
            set(observation) - set(_OBSERVATION_REQUIRED) - set(_OBSERVATION_OPTIONAL)
        )
        if unknown:
            raise VisualReviewError(
                f"Unknown key(s) in {label}: {', '.join(unknown)}. Supported: "
                f"{', '.join(_OBSERVATION_REQUIRED + _OBSERVATION_OPTIONAL)}."
            )
        missing = [key for key in _OBSERVATION_REQUIRED if not str(observation.get(key, "")).strip()]
        if missing:
            raise VisualReviewError(
                f"{label} is missing required key(s): {', '.join(missing)}. "
                "An observed finding needs an issue code, the evidence seen in "
                "the capture, and a suggested change."
            )

        code = str(observation["issue"])
        if code not in _MEANINGS:
            raise VisualReviewError(
                f"Unknown visual issue code '{code}' in {label}. Valid codes: "
                f"{', '.join(VISUAL_ISSUE_CODES)}."
            )
        zone = observation.get("zone")
        if zone is not None:
            zone = str(zone)
            if zone_names and zone not in zone_names:
                raise VisualReviewError(
                    f"{label} names unknown zone '{zone}'. Declared zones: "
                    f"{', '.join(sorted(zone_names))}."
                )
        view = observation.get("view")
        if view is not None:
            view = str(view)
            if views and view not in views:
                raise VisualReviewError(
                    f"{label} names unknown view '{view}'. Captured views: "
                    f"{', '.join(views)}."
                )
        severity = _observed_severity(observation.get("severity"), label)
        summary, explanation = _MEANINGS[code]
        issues.append(
            Issue(
                code=code,
                category=Category.VISUAL,
                severity=context.config.severity_for(code, severity),
                summary=summary,
                evidence=str(observation["evidence"]),
                recommendation=str(observation["suggestion"]),
                explanation=explanation,
                zone=zone,
                view=view,
                metrics=(("observed_in_capture", 1.0),),
            )
        )
    return sort_issues(issues)


def _observed_severity(value: Any, label: str) -> Severity:
    """Return the severity of an observed finding, defaulting to WARNING."""
    if value is None:
        return Severity.WARNING
    text = str(value).strip().lower()
    if text in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[text]
    try:
        return Severity.parse(text)
    except ValueError as error:
        raise VisualReviewError(
            f"{label} has an unusable severity: {error} "
            f"Review words are also accepted: {', '.join(sorted(_SEVERITY_ALIASES))}."
        ) from error


def _review_mass(
    collector: IssueCollector,
    context: AnalysisContext,
    composition: CompositionAnalysis,
) -> None:
    """Flag an axis whose half holds a disproportionate share of the bulk."""
    if (
        not composition.measured
        or composition.area is None
        or composition.object_count < MINIMUM_MASS_SAMPLE
    ):
        return
    limit = context.config.thresholds.visual_mass_imbalance
    area = composition.area
    for split in composition.axes:
        # One outsized central landmark is not a lopsided level, so a volume
        # claim only stands when the object count agrees with it.
        by_volume = (
            split.dominant_share >= limit
            and split.heavier.object_count >= split.lighter.object_count
        )
        by_height = (
            split.tall_total >= MINIMUM_TALL_SAMPLE
            and split.tall_dominant_share >= limit
        )
        if not (by_volume or by_height):
            continue
        heavy = split.heavier if by_volume else split.tall_dominant
        other = split.low if heavy is split.high else split.high
        counted = split.tall_total >= MINIMUM_TALL_SAMPLE
        moves = _rebalance_count(heavy, other, counted)
        subject = "tall elements" if counted else "elements"
        volume_clause = (
            f"the {split.heavier.name} half of the {area.width:.0f} x "
            f"{area.depth:.0f} m playable area holds "
            f"{split.dominant_share:.0f}% of total object volume across "
            f"{split.heavier.object_count} of {composition.object_count} objects"
        )
        tall_clause = (
            f"{split.tall_dominant_share:.0f}% of tall props "
            f"({split.tall_dominant.tall_count} of {split.tall_total} at or above "
            f"{composition.tall_height:.1f} m) are located on the "
            f"{split.tall_dominant.name} side"
        )
        if by_volume:
            evidence = f"{GEOMETRIC} {volume_clause} (limit {limit:.0f}%)"
            evidence += f"; {tall_clause}." if counted else "."
        else:
            evidence = f"{GEOMETRIC} {tall_clause} (limit {limit:.0f}%); {volume_clause}."
        collector.add(
            code="visual_mass_imbalance",
            severity=context.config.severity_for(
                "visual_mass_imbalance", Severity.WARNING
            ),
            summary=_MEANINGS["visual_mass_imbalance"][0],
            evidence=evidence,
            recommendation=(
                f"Redistribute {moves}-{moves + 1} {subject} "
                f"{OPPOSITE_SIDE[heavy.name]}ward, or add mass of a comparable "
                f"size to the {OPPOSITE_SIDE[heavy.name]} half."
            ),
            explanation=_MEANINGS["visual_mass_imbalance"][1],
            view=_view(context, ISO_NE if heavy.name in ("east", "north") else ISO_SW),
            metrics=(
                ("volume_share_percent", round(heavy.volume_share, 2)),
                ("tall_share_percent", round(split.tall_dominant_share, 2)),
                ("tall_objects", float(heavy.tall_count)),
                ("threshold_percent", limit),
            ),
            advisory=True,
        )


def _rebalance_count(heavy: MassSlice, light: MassSlice, use_tall: bool) -> int:
    """Return how many elements moving across the axis would even it out."""
    if use_tall:
        difference = heavy.tall_count - light.tall_count
    else:
        difference = heavy.object_count - light.object_count
    return max(1, math.ceil(abs(difference) / 2))


def _review_quadrants(
    collector: IssueCollector,
    context: AnalysisContext,
    composition: CompositionAnalysis,
) -> None:
    """Flag content concentrated into one quarter of the playable area."""
    limit = context.config.thresholds.density_imbalance
    if (
        not composition.measured
        or len(composition.quadrants) != 4
        or composition.object_count < MINIMUM_REGION_OBJECTS
        or composition.quadrant_imbalance < limit
    ):
        return
    counts = ", ".join(
        f"{part.name} {part.object_count}" for part in composition.quadrants
    )
    fullest = max(
        composition.quadrants, key=lambda part: (part.object_count, part.name)
    )
    empty = composition.empty_quadrants
    evidence = (
        f"{GEOMETRIC} objects per quadrant are {counts}, an imbalance of "
        f"{composition.quadrant_imbalance:.0f}/100 against a {limit:.0f} limit"
    )
    if empty:
        evidence += f"; {', '.join(empty)} hold nothing at all"
    evidence += "."
    target = empty[0] if empty else min(
        composition.quadrants, key=lambda part: (part.object_count, part.name)
    ).name
    collector.add(
        code="composition_bias",
        severity=context.config.severity_for("composition_bias", Severity.INFO),
        summary=_MEANINGS["composition_bias"][0],
        evidence=evidence,
        recommendation=(
            f"Give {target.replace('_', ' ')} a reason to exist, or move some of "
            f"the {fullest.object_count} objects in {fullest.name.replace('_', ' ')} "
            "into it."
        ),
        explanation=(
            _MEANINGS["composition_bias"][1]
            + " Quadrants are axis-aligned quarters of the playable area, so this "
            "is the coarsest of the geometric reads: check it against the "
            "top-down capture before acting."
        ),
        view=_view(context, TOP_DOWN),
        metrics=(
            ("quadrant_imbalance", round(composition.quadrant_imbalance, 2)),
            ("threshold", limit),
            ("empty_quadrants", float(len(empty))),
        ),
        advisory=True,
    )


def _review_regions(
    collector: IssueCollector,
    context: AnalysisContext,
    readability: ReadabilityAnalysis,
) -> None:
    """Flag regions that do not read: flat, cluttered, empty, or repetitive."""
    if not readability.measured:
        return
    thresholds = context.config.thresholds
    populated = sum(region.object_count for region in readability.regions)
    for region in readability.regions:
        if region.object_count >= MINIMUM_REGION_OBJECTS:
            _review_landmark(collector, context, region, thresholds.landmark_hierarchy_factor)
            _review_silhouette(collector, context, region)
            _review_repetition(collector, context, region)
        _review_clutter(collector, context, region, readability.player_footprint)
        _review_emptiness(collector, context, region, readability, populated)


def _review_landmark(
    collector: IssueCollector,
    context: AnalysisContext,
    region: RegionReadability,
    factor: float,
) -> None:
    """Flag a region whose largest object barely outsizes its neighbours."""
    if region.landmark_strength <= 0 or region.landmark_strength >= factor:
        return
    collector.add(
        code="landmark_weakness",
        severity=context.config.severity_for("landmark_weakness", Severity.WARNING),
        summary=_MEANINGS["landmark_weakness"][0],
        evidence=(
            f"{GEOMETRIC} in {_where(region)} the largest object "
            f"'{region.landmark_name}' spans {region.landmark_strength:.2f}x the "
            f"regional median box diagonal of {region.median_size:.1f} m across "
            f"{region.object_count} objects, short of the {factor:.2f}x a landmark "
            "needs to stand out."
        ),
        recommendation=(
            f"Grow '{region.landmark_name}' until its box diagonal passes "
            f"{region.median_size * factor:.1f} m, or replace it with a taller "
            f"distinct element, so one object anchors {region.name}."
        ),
        explanation=_MEANINGS["landmark_weakness"][1],
        zone=region.name if region.declared else None,
        subjects=(region.landmark_id,) if region.landmark_id else (),
        view=_view(context, ISO_NE),
        metrics=(
            ("landmark_strength", round(region.landmark_strength, 3)),
            ("median_size", round(region.median_size, 3)),
            ("required_factor", factor),
        ),
        advisory=True,
    )


def _review_silhouette(
    collector: IssueCollector, context: AnalysisContext, region: RegionReadability
) -> None:
    """Flag a region whose objects are all effectively the same height."""
    if region.silhouette_variety >= MINIMUM_SILHOUETTE_VARIETY:
        return
    collector.add(
        code="poor_silhouette",
        severity=context.config.severity_for("poor_silhouette", Severity.WARNING),
        summary=_MEANINGS["poor_silhouette"][0],
        evidence=(
            f"{GEOMETRIC} the {region.object_count} objects in {_where(region)} "
            f"stand {region.height_min:.2f}-{region.height_max:.2f} m tall, a "
            f"variation of {region.silhouette_variety * 100:.0f}% around a "
            f"{region.height_mean:.2f} m mean (below the "
            f"{MINIMUM_SILHOUETTE_VARIETY * 100:.0f}% a readable skyline needs)."
        ),
        recommendation=(
            f"Vary two or three heights in {region.name} by at least "
            f"{region.height_mean * 0.5:.1f} m, for example by raising one "
            "element and lowering another, so the group breaks into distinct "
            "silhouettes."
        ),
        explanation=_MEANINGS["poor_silhouette"][1],
        zone=region.name if region.declared else None,
        view=_view(context, ISO_NE),
        metrics=(
            ("silhouette_variety", round(region.silhouette_variety, 4)),
            ("height_mean", round(region.height_mean, 3)),
            ("height_span", round(region.height_max - region.height_min, 3)),
            ("threshold", MINIMUM_SILHOUETTE_VARIETY),
        ),
        advisory=True,
    )


def _review_repetition(
    collector: IssueCollector, context: AnalysisContext, region: RegionReadability
) -> None:
    """Flag a region built mostly from copies of one identical element."""
    if (
        region.repeat_label is None
        or region.repeat_count < MINIMUM_REPEAT_GROUP
        or region.repeat_share < IDENTICAL_COPY_SHARE
    ):
        return
    collector.add(
        code="excessive_repetition",
        severity=context.config.severity_for("excessive_repetition", Severity.WARNING),
        summary=_MEANINGS["excessive_repetition"][0],
        evidence=(
            f"{GEOMETRIC} {region.repeat_count} of {region.object_count} objects in "
            f"{_where(region)} are identical copies of {region.repeat_label} "
            f"({region.repeat_share:.0f}% of the region, limit "
            f"{IDENTICAL_COPY_SHARE:.0f}%)."
        ),
        recommendation=(
            f"Swap {max(1, region.repeat_count // 3)} of those copies for a "
            "different asset, or vary their scale and rotation, so the region "
            "stops reading as one repeated element."
        ),
        explanation=_MEANINGS["excessive_repetition"][1],
        zone=region.name if region.declared else None,
        view=_view(context, ISO_NE),
        metrics=(
            ("repeat_share_percent", round(region.repeat_share, 2)),
            ("repeat_count", float(region.repeat_count)),
            ("threshold_percent", IDENTICAL_COPY_SHARE),
        ),
        advisory=True,
    )


def _review_clutter(
    collector: IssueCollector,
    context: AnalysisContext,
    region: RegionReadability,
    footprint: float,
) -> None:
    """Flag a region packing more objects than its walkable space supports."""
    if (
        not region.walkable_measured
        or region.object_count < 2
        or region.clutter_index < CLUTTER_INDEX_LIMIT
    ):
        return
    excess = math.ceil(
        (region.clutter_index - CLUTTER_INDEX_LIMIT) * region.walkable_area / footprint
    )
    collector.add(
        code="clutter",
        severity=context.config.severity_for("clutter", Severity.WARNING),
        summary=_MEANINGS["clutter"][0],
        evidence=(
            f"{GEOMETRIC} {_where(region)} holds {region.object_count} objects in "
            f"{region.walkable_area:.0f} m2 of walkable space "
            f"({region.prop_density:.2f} per m2, or {region.clutter_index:.2f} per "
            f"{footprint:.2f} m2 player footprint, limit "
            f"{CLUTTER_INDEX_LIMIT:.2f})."
        ),
        recommendation=(
            f"Remove or merge about {max(1, excess)} objects in {region.name}, or "
            "open the space around them, so movement and cover read clearly."
        ),
        explanation=_MEANINGS["clutter"][1],
        zone=region.name if region.declared else None,
        view=_view(context, TOP_DOWN),
        metrics=(
            ("clutter_index", round(region.clutter_index, 4)),
            ("objects_per_square_metre", round(region.prop_density, 4)),
            ("walkable_area", round(region.walkable_area, 2)),
            ("threshold", CLUTTER_INDEX_LIMIT),
        ),
        advisory=True,
    )


def _review_emptiness(
    collector: IssueCollector,
    context: AnalysisContext,
    region: RegionReadability,
    readability: ReadabilityAnalysis,
    scene_objects: int,
) -> None:
    """Flag a region whose cells mostly hold nothing.

    A declared zone was given a purpose, so emptiness there is always worth
    reporting. An undeclared region is only worth reporting once the rest of
    the level is populated enough for the gap to read as a gap.
    """
    limit = context.config.thresholds.empty_zone_ratio
    if region.cell_count == 0 or region.empty_ratio < limit:
        return
    if not region.declared and scene_objects < MINIMUM_POPULATED_SCENE:
        return
    severity = Severity.WARNING if region.declared else Severity.INFO
    collector.add(
        code="empty_zone",
        severity=context.config.severity_for("empty_zone", severity),
        summary=_MEANINGS["empty_zone"][0],
        evidence=(
            f"{GEOMETRIC} {region.empty_ratio:.0f}% of {_where(region)} is empty: "
            f"{region.empty_cells} of {region.cell_count} patches of "
            f"{readability.cell_size:.0f} m hold no object, and the region holds "
            f"{region.object_count} objects in total (limit {limit:.0f}%)."
        ),
        recommendation=(
            f"Place two or three elements in the empty part of {region.name}, or "
            "shrink the region so its declared area matches the space actually "
            "used."
        ),
        explanation=_MEANINGS["empty_zone"][1],
        zone=region.name if region.declared else None,
        view=_view(context, TOP_DOWN),
        metrics=(
            ("empty_ratio_percent", round(region.empty_ratio, 2)),
            ("empty_cells", float(region.empty_cells)),
            ("cells", float(region.cell_count)),
            ("threshold_percent", limit),
        ),
        advisory=True,
    )


def _review_sightlines(collector: IssueCollector, context: AnalysisContext) -> None:
    """Flag landmarks the player cannot see from where they start."""
    scene = context.scene
    eye = context.config.eye_height
    viewpoints = tuple(
        sorted(scene.entry_points + scene.spawn_points, key=lambda point: point.id)
    )
    targets = _landmark_targets(scene.landmarks, scene.objects)
    if not viewpoints:
        return

    if not targets:
        _review_missing_landmarks(collector, context)
        return

    blockers = tuple(
        (obj, box)
        for obj in scene.props
        if (box := obj.world_bounds()) is not None
    )
    visibility = {
        identifier: [
            point
            for point in viewpoints
            if _visible(
                Vec3(point.position.x, point.position.y + eye, point.position.z),
                aim,
                blockers,
                identifier,
            )
        ]
        for identifier, _, aim in targets
    }
    if any(seen for seen in visibility.values()):
        for identifier, label, aim in targets:
            if visibility[identifier]:
                continue
            blocker = _first_blocker(viewpoints, aim, blockers, identifier, eye)
            collector.add(
                code="sightline_block",
                severity=context.config.severity_for("sightline_block", Severity.WARNING),
                summary=_MEANINGS["sightline_block"][0],
                evidence=(
                    f"{GEOMETRIC} an axis-aligned box test finds '{label}' hidden "
                    f"from all {len(viewpoints)} entry and spawn points"
                    + (f", first blocked by '{blocker}'" if blocker else "")
                    + "."
                ),
                recommendation=(
                    f"Lower or move the geometry between the start points and "
                    f"'{label}', or raise '{label}' until its top clears the "
                    "obstruction."
                ),
                explanation=_MEANINGS["sightline_block"][1],
                subjects=(identifier,),
                view=_view(context, ISO_NE),
                metrics=(
                    ("viewpoints", float(len(viewpoints))),
                    ("viewpoints_with_sight", 0.0),
                ),
                advisory=True,
            )
        return

    collector.add(
        code="navigation_readability",
        severity=context.config.severity_for(
            "navigation_readability", Severity.WARNING
        ),
        summary=_MEANINGS["navigation_readability"][0],
        evidence=(
            f"{GEOMETRIC} an axis-aligned box test finds none of the "
            f"{len(targets)} landmark(s) visible from any of the "
            f"{len(viewpoints)} entry and spawn points."
        ),
        recommendation=(
            "Raise one landmark above the surrounding geometry, or open a "
            "sightline from the start points to it, so players have something "
            "to orient by."
        ),
        explanation=_MEANINGS["navigation_readability"][1],
        view=_view(context, ISO_NE),
        metrics=(
            ("landmarks", float(len(targets))),
            ("viewpoints", float(len(viewpoints))),
            ("visible_landmarks", 0.0),
        ),
        advisory=True,
    )


def _review_missing_landmarks(
    collector: IssueCollector, context: AnalysisContext
) -> None:
    """Flag a level large enough to need a landmark that declares none."""
    area = context.ground
    props = context.scene.props
    if area is None or len(props) < 5:
        return
    span = math.hypot(area.width, area.depth)
    if span < MINIMUM_ORIENTATION_SPAN:
        return
    collector.add(
        code="navigation_readability",
        severity=context.config.severity_for(
            "navigation_readability", Severity.WARNING
        ),
        summary=_MEANINGS["navigation_readability"][0],
        evidence=(
            f"{GEOMETRIC} the {area.width:.0f} x {area.depth:.0f} m playable area "
            f"({span:.0f} m across) holds {len(props)} objects but declares no "
            "landmark marker and no landmark-type object."
        ),
        recommendation=(
            "Promote the most distinctive element to a landmark, or add one "
            "tall element visible from the entry points, and tag it as a "
            "landmark so wayfinding can be checked."
        ),
        explanation=_MEANINGS["navigation_readability"][1],
        view=_view(context, ISO_NE),
        metrics=(
            ("playable_span", round(span, 2)),
            ("objects", float(len(props))),
            ("landmarks", 0.0),
        ),
        advisory=True,
    )


def _landmark_targets(
    markers: Sequence[MarkerPoint], objects: Sequence[SceneObject]
) -> tuple[tuple[str, str, Vec3], ...]:
    """Return the points a player is meant to navigate by.

    A landmark object is aimed at three quarters of its height: the part that
    should clear ordinary cover, rather than its base.
    """
    targets: list[tuple[str, str, Vec3]] = [
        (marker.id, marker.label, marker.position) for marker in markers
    ]
    for obj in objects:
        if obj.type is not ObjectType.LANDMARK:
            continue
        box = obj.world_bounds()
        aim = (
            Vec3(box.center.x, box.min.y + box.height * 0.75, box.center.z)
            if box is not None
            else obj.position
        )
        targets.append((obj.id, obj.name, aim))
    return tuple(sorted(targets, key=lambda item: item[0]))


def _visible(
    eye: Vec3,
    target: Vec3,
    blockers: Sequence[tuple[SceneObject, Bounds]],
    exclude: str,
) -> bool:
    """Return whether an unobstructed segment reaches a target."""
    return not any(
        obj.id != exclude and segment_intersects_aabb(eye, target, box)
        for obj, box in blockers
    )


def _first_blocker(
    viewpoints: Sequence[MarkerPoint],
    target: Vec3,
    blockers: Sequence[tuple[SceneObject, Bounds]],
    exclude: str,
    eye_height: float,
) -> str | None:
    """Return the name of an object standing between a start point and a target."""
    for point in viewpoints:
        eye = Vec3(point.position.x, point.position.y + eye_height, point.position.z)
        for obj, box in blockers:
            if obj.id != exclude and segment_intersects_aabb(eye, target, box):
                return obj.name
    return None


def _where(region: RegionReadability) -> str:
    """Return a phrase naming a region, saying whether the scene declared it."""
    return f"zone '{region.name}'" if region.declared else f"the {region.name} region"


def _view(context: AnalysisContext, preferred: str) -> str:
    """Return the capture view a reviewer should open, if it is captured."""
    views = context.config.capture_views
    if not views or preferred in views:
        return preferred
    return views[0]
