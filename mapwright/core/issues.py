"""Structured, explainable findings shared by every Mapwright analyzer.

Mapwright never reports a bare verdict like "bad flow". Every finding carries
the measurement that produced it, why that measurement matters, and what to do
about it, so a reader can disagree with the conclusion on the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Severity(str, Enum):
    """How strongly a finding argues for a change."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """Return a 0-3 ordinal for sorting and score penalties."""
        return _SEVERITY_RANK[self]

    @classmethod
    def parse(cls, value: Any) -> Severity:
        """Parse a severity name, case-insensitively."""
        try:
            return cls(str(value).upper())
        except ValueError as exc:
            supported = ", ".join(item.value for item in cls)
            raise ValueError(
                f"Unknown severity '{value}'. Supported: {supported}."
            ) from exc


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


class Category(str, Enum):
    """The report section a finding belongs to."""

    SPATIAL = "spatial"
    FLOW = "flow"
    NAVIGATION = "navigation"
    PACING = "pacing"
    ENCOUNTER = "encounter"
    FAIRNESS = "fairness"
    VISUAL = "visual"

    @property
    def title(self) -> str:
        """Return the human-readable section heading."""
        return _CATEGORY_TITLES[self]


_CATEGORY_TITLES = {
    Category.SPATIAL: "Spatial Quality",
    Category.FLOW: "Flow",
    Category.NAVIGATION: "Navigation",
    Category.PACING: "Pacing",
    Category.ENCOUNTER: "Encounter",
    Category.FAIRNESS: "Fairness",
    Category.VISUAL: "Visual Readability",
}


@dataclass(frozen=True)
class Issue:
    """One explainable finding about a level.

    ``evidence`` states the measurement in plain language, ``explanation`` says
    why it matters, and ``recommendation`` says what would resolve it. Findings
    are review notes, not orders: ``advisory`` marks the ones that measure a
    proxy rather than the design property itself.
    """

    code: str
    category: Category
    severity: Severity
    summary: str
    evidence: str
    recommendation: str
    explanation: str | None = None
    zone: str | None = None
    subjects: tuple[str, ...] = ()
    view: str | None = None
    metrics: tuple[tuple[str, float], ...] = ()
    advisory: bool = False

    @property
    def sort_key(self) -> tuple:
        """Return a deterministic ordering key: most severe first."""
        return (
            -self.severity.rank,
            self.category.value,
            self.code,
            self.zone or "",
            self.subjects,
        )

    def metric(self, name: str) -> float | None:
        """Return one recorded measurement by name."""
        return dict(self.metrics).get(name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        data: dict[str, Any] = {
            "issue": self.code,
            "category": self.category.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }
        if self.explanation is not None:
            data["explanation"] = self.explanation
        if self.zone is not None:
            data["zone"] = self.zone
        if self.subjects:
            data["subjects"] = list(self.subjects)
        if self.view is not None:
            data["view"] = self.view
        if self.metrics:
            data["metrics"] = dict(self.metrics)
        if self.advisory:
            data["advisory"] = True
        return data

    def to_text(self) -> str:
        """Return the multi-line terminal form used across Mapwright reports."""
        lines = [self.severity.value, self.evidence]
        if self.explanation:
            lines.append(self.explanation)
        lines.append("Recommendation:")
        lines.append(self.recommendation)
        return "\n".join(lines)


def sort_issues(issues: Iterable[Issue]) -> tuple[Issue, ...]:
    """Return findings ordered most severe first, deterministically."""
    return tuple(sorted(issues, key=lambda issue: issue.sort_key))


def count_by_severity(issues: Iterable[Issue]) -> dict[Severity, int]:
    """Return how many findings fall into each severity."""
    counts = {severity: 0 for severity in Severity}
    for issue in issues:
        counts[issue.severity] += 1
    return counts


def filter_category(issues: Iterable[Issue], category: Category) -> tuple[Issue, ...]:
    """Return only the findings belonging to one report section."""
    return tuple(issue for issue in issues if issue.category is category)


@dataclass
class IssueCollector:
    """Accumulates findings for one analyzer, keeping construction terse."""

    category: Category
    issues: list[Issue] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: Severity,
        summary: str,
        evidence: str,
        recommendation: str,
        *,
        explanation: str | None = None,
        zone: str | None = None,
        subjects: Iterable[str] = (),
        view: str | None = None,
        metrics: Iterable[tuple[str, float]] = (),
        advisory: bool = False,
    ) -> Issue:
        """Record one finding and return it."""
        issue = Issue(
            code=code,
            category=self.category,
            severity=severity,
            summary=summary,
            evidence=evidence,
            recommendation=recommendation,
            explanation=explanation,
            zone=zone,
            subjects=tuple(subjects),
            view=view,
            metrics=tuple(metrics),
            advisory=advisory,
        )
        self.issues.append(issue)
        return issue

    def result(self) -> tuple[Issue, ...]:
        """Return the collected findings, most severe first."""
        return sort_issues(self.issues)
