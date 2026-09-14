"""Turning findings into a score you can argue with.

A number nobody can trace is worse than no number. Every category starts at
100 and loses ``penalty(severity)`` points for each *kind* of finding, and the
overall score is a weighted mean of the categories that could actually be
measured. Read a scorecard next to the issue list and the arithmetic is
visible in both directions: no finding is invisible, and no point is lost
without one.

Repeated findings of the same kind are damped rather than summed. Validators
report one finding per crowded pair because each pair is separately
actionable, but ten crowded pairs is one problem of some size, not ten
independent defects. A linear sum drives any busy level to zero, and a score
pinned at zero cannot show whether a correction helped — which is the only
thing the improvement loop needs it for.

Applicability matters as much as the arithmetic. A single-player level has no
teams to treat unequally, so its fairness category is not a zero — it is not a
score at all, and averaging it in would punish a level for a question that
does not apply to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from mapwright.core.config import MapwrightConfig
from mapwright.core.issues import Category, Issue, sort_issues

MAXIMUM_SCORE = 100.0
NOT_APPLICABLE_LABEL = "n/a"
_OVERALL_LABEL = "Overall score:"
_DEFAULT_REASON = "not applicable to this scene"


@dataclass(frozen=True)
class CategoryScore:
    """One category's score and the arithmetic that produced it."""

    category: Category
    score: float
    issue_count: int
    penalty: float
    weight: float
    applicable: bool
    reason: str | None = None

    @property
    def display(self) -> int:
        """Return the rounded score used in reports."""
        return int(round(self.score))

    @property
    def title(self) -> str:
        """Return the category's report heading."""
        return self.category.title

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        data: dict[str, Any] = {
            "category": self.category.value,
            "score": round(self.score, 2),
            "display": self.display,
            "issue_count": self.issue_count,
            "penalty": round(self.penalty, 2),
            "weight": self.weight,
            "applicable": self.applicable,
        }
        if not self.applicable:
            data["reason"] = self.reason or _DEFAULT_REASON
        return data


@dataclass(frozen=True)
class Scorecard:
    """Every category's score and the weighted overall."""

    categories: tuple[CategoryScore, ...]
    overall: float

    @property
    def applicable(self) -> tuple[CategoryScore, ...]:
        """Return only the categories that count toward the overall."""
        return tuple(item for item in self.categories if item.applicable)

    @property
    def scorable(self) -> bool:
        """Return whether any category could be scored at all."""
        return bool(self.applicable)

    @property
    def display(self) -> int:
        """Return the rounded overall score."""
        return int(round(self.overall))

    def category(self, category: Category) -> CategoryScore | None:
        """Return one category's score."""
        return next(
            (item for item in self.categories if item.category is category), None
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "overall": round(self.overall, 2) if self.scorable else None,
            "overall_display": self.display if self.scorable else None,
            "categories": [item.to_dict() for item in self.categories],
        }

    def format_table(self) -> str:
        """Render the scorecard as the report's score table."""
        width = _label_width(self.categories)
        lines = [
            f"{item.title:<{width}}"
            + (
                f"{item.display}"
                if item.applicable
                else f"{NOT_APPLICABLE_LABEL} ({item.reason or _DEFAULT_REASON})"
            )
            for item in self.categories
        ]
        lines.append("")
        overall = f"{self.display} / 100" if self.scorable else NOT_APPLICABLE_LABEL
        lines.append(f"{_OVERALL_LABEL:<{width}}{overall}")
        return "\n".join(lines) + "\n"


def score_issues(
    issues: Iterable[Issue],
    config: MapwrightConfig,
    applicable: Mapping[Category, bool] | None = None,
) -> Scorecard:
    """Score every category from its findings and weight the applicable ones."""
    collected = sort_issues(issues)
    flags = dict(applicable or {})
    scores: list[CategoryScore] = []

    for category in Category:
        found = tuple(item for item in collected if item.category is category)
        penalty = _category_penalty(found, config)
        is_applicable = bool(flags.get(category, True))
        scores.append(
            CategoryScore(
                category=category,
                score=min(MAXIMUM_SCORE, max(0.0, MAXIMUM_SCORE - penalty)),
                issue_count=len(found),
                penalty=penalty,
                weight=config.scoring.weight(category),
                applicable=is_applicable,
                reason=None if is_applicable else _reason(found),
            )
        )

    weighted = [(item.score, item.weight) for item in scores if item.applicable]
    total_weight = sum(weight for _, weight in weighted)
    overall = (
        sum(score * weight for score, weight in weighted) / total_weight
        if total_weight > 0
        else 0.0
    )
    return Scorecard(categories=tuple(scores), overall=overall)


def compare(before: Scorecard, after: Scorecard) -> str:
    """Render per-category and overall movement between two scorecards."""
    width = _label_width(after.categories or before.categories)
    lines = []
    for item in after.categories:
        previous = before.category(item.category)
        lines.append(
            f"{item.title:<{width}}"
            + _delta(
                previous.display if previous and previous.applicable else None,
                item.display if item.applicable else None,
            )
        )
    lines.append("")
    lines.append(
        f"{_OVERALL_LABEL:<{width}}"
        + _delta(
            before.display if before.scorable else None,
            after.display if after.scorable else None,
        )
    )
    return "\n".join(lines) + "\n"


def _category_penalty(issues: tuple[Issue, ...], config: MapwrightConfig) -> float:
    """Return what one category's findings cost it in total.

    Each distinct finding code costs its worst instance's penalty, grown by
    ``1 + ln(n)`` over its ``n`` instances: more is worse, monotonically, but
    the tenth crowded pair moves the score far less than the first.
    """
    by_code: dict[str, list[float]] = {}
    for issue in issues:
        by_code.setdefault(issue.code, []).append(_penalty(issue, config))
    return sum(
        max(costs) * (1.0 + math.log(len(costs)))
        for _, costs in sorted(by_code.items())
    )


def _penalty(issue: Issue, config: MapwrightConfig) -> float:
    """Return what one finding costs its category.

    Advisory findings measure a proxy rather than the property itself, so a
    review note cannot sink a score the way a proven defect does.
    """
    cost = config.scoring.penalty(issue.severity)
    return cost * config.scoring.advisory_factor if issue.advisory else cost


def _reason(issues: tuple[Issue, ...]) -> str:
    """Return why a category was not scored, taken from its own findings."""
    skipped = next(
        (item for item in issues if item.code.endswith("not_applicable")), None
    )
    if skipped is not None:
        return skipped.summary
    return issues[0].summary if issues else _DEFAULT_REASON


def _label_width(categories: tuple[CategoryScore, ...]) -> int:
    """Return the column at which score values start."""
    labels = [item.title for item in categories] + [_OVERALL_LABEL]
    return max(len(label) for label in labels) + 3


def _delta(before: int | None, after: int | None) -> str:
    """Render one before-and-after pair with its signed change."""
    if before is None and after is None:
        return f"{NOT_APPLICABLE_LABEL} -> {NOT_APPLICABLE_LABEL}"
    if before is None:
        return f"{NOT_APPLICABLE_LABEL} -> {after} (newly scorable)"
    if after is None:
        return f"{before} -> {NOT_APPLICABLE_LABEL} (no longer scorable)"
    return f"{before} -> {after} ({after - before:+d})"
