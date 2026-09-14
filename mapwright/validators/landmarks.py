"""Landmark hierarchy estimated from object size.

Size is a proxy, not a meaning: the biggest box in a scene is not necessarily
the thing a player should navigate by. Every finding here is a review note to
weigh against the design intent, never an instruction to resize geometry.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Vec3
from mapwright.core.issues import Category, Issue, IssueCollector, Severity


@dataclass(frozen=True)
class ObjectSize:
    """One measured object and its position in the scene's size hierarchy."""

    id: str
    name: str
    asset: str | None
    position: Vec3
    dimensions: Vec3
    size_score: float
    relative_score: float
    large: bool
    candidate: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "id": self.id,
            "name": self.name,
            "asset": self.asset,
            "position": self.position.to_list(),
            "dimensions": self.dimensions.to_list(),
            "size_score": self.size_score,
            "relative_score": self.relative_score,
            "large": self.large,
            "candidate": self.candidate,
        }


@dataclass(frozen=True)
class UnmeasuredObject:
    """One placed object that carries no usable dimensions."""

    id: str
    name: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {"id": self.id, "name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class LandmarkReport:
    """Size hierarchy measurements for one scene."""

    issues: tuple[Issue, ...]
    scene: str
    candidate_ratio: float
    hierarchy_factor: float
    median_size_score: float
    largest_size_score: float
    objects: tuple[ObjectSize, ...]
    unmeasured: tuple[UnmeasuredObject, ...]
    no_clear_hierarchy: bool
    competing_landmarks: bool

    @property
    def candidates(self) -> tuple[ObjectSize, ...]:
        """Return the objects inside the top size band."""
        return tuple(obj for obj in self.objects if obj.candidate)

    @property
    def large_objects(self) -> tuple[ObjectSize, ...]:
        """Return the objects at or above the large-object line."""
        return tuple(obj for obj in self.objects if obj.large)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "candidate_ratio": self.candidate_ratio,
            "hierarchy_factor": self.hierarchy_factor,
            "median_size_score": self.median_size_score,
            "largest_size_score": self.largest_size_score,
            "objects": [obj.to_dict() for obj in self.objects],
            "unmeasured": [item.to_dict() for item in self.unmeasured],
            "no_clear_hierarchy": self.no_clear_hierarchy,
            "competing_landmarks": self.competing_landmarks,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_landmarks(context: AnalysisContext) -> LandmarkReport:
    """Rank placed objects by world size and review the resulting hierarchy."""
    thresholds = context.config.thresholds
    candidate_ratio = thresholds.landmark_candidate_ratio
    hierarchy_factor = thresholds.landmark_hierarchy_factor

    scored: list[tuple[str, str, str | None, Vec3, Vec3, float]] = []
    unmeasured: list[UnmeasuredObject] = []
    for obj in context.scene.props:
        box = obj.world_bounds()
        if box is None:
            unmeasured.append(
                UnmeasuredObject(obj.id, obj.name, "no resolvable dimensions")
            )
            continue
        score = obj.size_score()
        if score <= 0.0:
            unmeasured.append(UnmeasuredObject(obj.id, obj.name, "world size is zero"))
            continue
        scored.append((obj.id, obj.name, obj.asset, obj.position, box.size, score))

    median_score = statistics.median(item[5] for item in scored) if scored else 0.0
    largest_score = max((item[5] for item in scored), default=0.0)
    candidate_minimum = largest_score / candidate_ratio if scored else 0.0
    large_minimum = median_score * hierarchy_factor
    no_clear_hierarchy = bool(scored) and largest_score < large_minimum

    objects = tuple(
        sorted(
            (
                ObjectSize(
                    id=identifier,
                    name=name,
                    asset=asset,
                    position=position,
                    dimensions=dimensions,
                    size_score=score,
                    relative_score=score / median_score if median_score else 0.0,
                    large=score >= large_minimum,
                    candidate=score >= candidate_minimum,
                )
                for identifier, name, asset, position, dimensions, score in scored
            ),
            key=lambda obj: (-obj.size_score, obj.name.casefold(), obj.id),
        )
    )
    competing = (
        not no_clear_hierarchy
        and sum(obj.candidate and obj.large for obj in objects) > 1
    )

    report = LandmarkReport(
        issues=(),
        scene=context.scene.scene,
        candidate_ratio=candidate_ratio,
        hierarchy_factor=hierarchy_factor,
        median_size_score=median_score,
        largest_size_score=largest_score,
        objects=objects,
        unmeasured=tuple(sorted(unmeasured, key=lambda item: item.id)),
        no_clear_hierarchy=no_clear_hierarchy,
        competing_landmarks=competing,
    )
    return replace(report, issues=_issues(context, report))


def format_report(report: LandmarkReport) -> str:
    """Format the size hierarchy table and its review notes for a terminal."""
    rows = []
    for obj in report.objects:
        status = []
        if obj.candidate:
            status.append("CANDIDATE")
        if obj.large:
            status.append("LARGE")
        rows.append(
            (
                obj.name,
                obj.id,
                f"{obj.dimensions.x:.2f}x{obj.dimensions.y:.2f}x{obj.dimensions.z:.2f}",
                f"{obj.size_score:.3f}",
                f"{obj.relative_score:.2f}x",
                ", ".join(status) or "-",
            )
        )

    if report.no_clear_hierarchy:
        conclusion = "REVIEW: NO CLEAR SIZE HIERARCHY"
    elif report.competing_landmarks:
        conclusion = "REVIEW: COMPETING LANDMARK CANDIDATES"
    elif report.objects:
        conclusion = "CLEAR SIZE HIERARCHY"
    else:
        conclusion = "NO MEASURABLE OBJECTS"

    lines = [
        f"Mapwright landmark report: {report.scene}",
        (
            f"Measured props: {len(report.objects)} | "
            f"Unmeasured: {len(report.unmeasured)} | "
            f"Median size score: {report.median_size_score:.3f}"
        ),
        (
            f"Large threshold: >= {report.hierarchy_factor:g}x median | "
            f"Candidate band: largest / score <= {report.candidate_ratio:g}"
        ),
        "",
    ]
    if rows:
        lines.extend(
            _render_table(
                ("Object", "Id", "World dimensions", "Score", "Relative", "Status"),
                rows,
            )
        )
    else:
        lines.append("No object in this scene has resolvable dimensions.")
    lines.extend(
        [
            "",
            f"Landmark candidate(s): "
            f"{', '.join(obj.name for obj in report.candidates) or 'none'}",
            f"Result: {conclusion}",
        ]
    )
    if report.unmeasured:
        lines.extend(["", "Unmeasured objects:"])
        lines.extend(
            f"- {item.name} ({item.id}): {item.reason}" for item in report.unmeasured
        )
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _issues(context: AnalysisContext, report: LandmarkReport) -> tuple[Issue, ...]:
    """Return the review notes this hierarchy raises."""
    collector = IssueCollector(Category.SPATIAL)
    largest = report.objects[0] if report.objects else None

    if report.no_clear_hierarchy and largest is not None:
        required = report.median_size_score * report.hierarchy_factor
        collector.add(
            "no_landmark_hierarchy",
            context.config.severity_for("no_landmark_hierarchy", Severity.WARNING),
            "No object stands out by size",
            evidence=(
                f"The largest object '{largest.name}' ({largest.id}) scores "
                f"{largest.size_score:.2f} against a median of "
                f"{report.median_size_score:.2f} ({largest.relative_score:.2f}x); a "
                f"size hierarchy needs at least {report.hierarchy_factor:g}x the "
                f"median, or {required:.2f}."
            ),
            explanation=(
                "When everything is roughly the same size, a player crossing the "
                "space has nothing fixed to steer by and the level reads as "
                "undifferentiated. Size is only one way to make a landmark: "
                "colour, lighting, silhouette, and placement do it too."
            ),
            recommendation=(
                f"If the intended landmark is in this list, raise its size score "
                f"above {required:.2f} or give it another kind of prominence; if "
                "the level is meant to read flat, record this as intentional."
            ),
            subjects=(largest.id,),
            metrics=(
                ("largest_size_score", report.largest_size_score),
                ("median_size_score", report.median_size_score),
                ("largest_to_median", largest.relative_score),
                ("hierarchy_factor", report.hierarchy_factor),
            ),
            advisory=True,
        )

    competitors = tuple(obj for obj in report.objects if obj.candidate and obj.large)
    if report.competing_landmarks and largest is not None:
        listed = ", ".join(
            f"'{obj.name}' ({obj.size_score:.2f})" for obj in competitors[:5]
        )
        collector.add(
            "competing_landmarks",
            context.config.severity_for("competing_landmarks", Severity.WARNING),
            f"{len(competitors)} objects compete for the same landmark role",
            evidence=(
                f"{len(competitors)} objects sit within "
                f"{report.candidate_ratio:g}x of the largest score "
                f"{report.largest_size_score:.2f} and above "
                f"{report.hierarchy_factor:g}x the median "
                f"{report.median_size_score:.2f}: {listed}."
            ),
            explanation=(
                "Several near-equal masses give a player more than one thing to "
                "orient by, so neither becomes the reference point that makes a "
                "space memorable."
            ),
            recommendation=(
                "Choose which of these is the primary landmark and separate the "
                "others from it by size, distance, or visual treatment; if they "
                "belong to different zones, record this as intentional."
            ),
            subjects=tuple(obj.id for obj in competitors),
            metrics=(
                ("competitors", float(len(competitors))),
                ("largest_size_score", report.largest_size_score),
                ("median_size_score", report.median_size_score),
                ("candidate_ratio", report.candidate_ratio),
            ),
            advisory=True,
        )

    if report.unmeasured:
        total = len(report.objects) + len(report.unmeasured)
        named = ", ".join(
            f"'{item.name}' ({item.id})" for item in report.unmeasured[:5]
        )
        remainder = len(report.unmeasured) - 5
        if remainder > 0:
            named = f"{named}, and {remainder} more"
        collector.add(
            "unmeasured_objects",
            context.config.severity_for("unmeasured_objects", Severity.INFO),
            f"{len(report.unmeasured)} of {total} objects have no dimensions",
            evidence=(
                f"{len(report.unmeasured)} of {total} placed object(s) carry no "
                f"resolvable bounds and were left out of every size measurement: "
                f"{named}."
            ),
            explanation=(
                "An object without bounds is invisible to the size, navigation, "
                "and visibility checks, so those results describe only the part "
                "of the scene that could be measured."
            ),
            recommendation=(
                "Give these objects explicit dimensions in the source scene, or "
                "extend the adapter that imported them, then rerun the analysis."
            ),
            subjects=tuple(item.id for item in report.unmeasured),
            metrics=(
                ("unmeasured", float(len(report.unmeasured))),
                ("placed_objects", float(total)),
                (
                    "unmeasured_share_percent",
                    len(report.unmeasured) / total * 100.0 if total else 0.0,
                ),
            ),
            advisory=True,
        )

    return collector.result()


def _render_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> list[str]:
    """Return a padded text table with a divider under the header row."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    return [
        render(headers),
        "  ".join("-" * width for width in widths),
        *(render(row) for row in rows),
    ]


def _findings(issues: Sequence[Issue]) -> list[str]:
    """Return the findings section shared by every Mapwright text report."""
    if not issues:
        return ["", "Findings: none"]
    lines = ["", "Findings:"]
    for issue in issues:
        suffix = " (advisory)" if issue.advisory else ""
        lines.append("")
        lines.append(f"- {issue.code}{suffix}: {issue.summary}")
        lines.extend(f"  {line}" for line in issue.to_text().splitlines())
    return lines
