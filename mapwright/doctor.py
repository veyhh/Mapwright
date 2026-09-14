"""Environment checks.

``mapwright doctor`` answers one question before any analysis runs: which
parts of the pipeline actually work here? Engine-free analysis needs almost
nothing, so the checks are explicit about what is required versus what only
unlocks capture or engine import.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from mapwright.core.config import (
    MapwrightConfig,
    available_profiles,
)


MINIMUM_PYTHON = (3, 10)


@dataclass(frozen=True)
class Check:
    """One environment check and what it means for the pipeline."""

    name: str
    passed: bool
    detail: str
    required: bool = True

    @property
    def status(self) -> str:
        """Return the terminal marker for this result."""
        if self.passed:
            return "OK"
        return "FAIL" if self.required else "SKIP"


@dataclass(frozen=True)
class DoctorReport:
    """The full environment assessment."""

    checks: tuple[Check, ...]

    @property
    def healthy(self) -> bool:
        """Return whether every required check passed."""
        return all(check.passed for check in self.checks if check.required)

    @property
    def failures(self) -> tuple[Check, ...]:
        """Return the required checks that failed."""
        return tuple(
            check for check in self.checks if check.required and not check.passed
        )

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        return {
            "healthy": self.healthy,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "required": check.required,
                }
                for check in self.checks
            ],
        }


def run_doctor(config: MapwrightConfig, config_path: str | Path | None = None) -> DoctorReport:
    """Check everything Mapwright needs, and everything it can optionally use."""
    checks: list[Check] = [
        _python_check(),
        _yaml_check(),
        _profile_check(config),
        _config_check(config_path),
        *_adapter_checks(config),
        _writable_check("report directory", config.report_dir),
        _writable_check("capture directory", config.capture_output_dir),
    ]
    return DoctorReport(tuple(checks))


def format_report(report: DoctorReport) -> str:
    """Format the assessment as an aligned terminal table."""
    width = max((len(check.name) for check in report.checks), default=0)
    lines = ["Mapwright environment check", ""]
    lines.extend(
        f"[{check.status:4s}] {check.name.ljust(width)}  {check.detail}"
        for check in report.checks
    )
    lines.append("")
    if report.healthy:
        lines.append(
            "Result: READY — engine-independent analysis is fully available."
        )
    else:
        names = ", ".join(check.name for check in report.failures)
        lines.append(f"Result: BLOCKED — required check(s) failing: {names}")
    return "\n".join(lines)


def _python_check() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    passed = sys.version_info[:2] >= MINIMUM_PYTHON
    required = ".".join(str(part) for part in MINIMUM_PYTHON)
    return Check(
        name="Python",
        passed=passed,
        detail=f"{version} (requires {required}+)",
    )


def _yaml_check() -> Check:
    try:
        import yaml
    except ImportError:  # pragma: no cover - dependency is declared
        return Check("PyYAML", False, "not installed; run pip install PyYAML")
    return Check("PyYAML", True, f"{yaml.__version__}")


def _profile_check(config: MapwrightConfig) -> Check:
    profiles = available_profiles()
    if not profiles:
        return Check("Genre profiles", False, "no profiles found in mapwright/profiles")
    return Check(
        "Genre profiles",
        True,
        f"{len(profiles)} available ({', '.join(profiles)}); active: {config.profile}",
    )


def _config_check(config_path: str | Path | None) -> Check:
    if config_path is None:
        return Check(
            "Config file",
            True,
            "none supplied; built-in defaults in use",
            required=False,
        )
    path = Path(config_path)
    if not path.is_file():
        return Check("Config file", False, f"not found: {path}")
    return Check("Config file", True, f"loaded {path}")


def _adapter_checks(config: MapwrightConfig) -> list[Check]:
    checks: list[Check] = []
    try:
        from mapwright.adapters.base import default_registry

        registry = default_registry()
    except Exception as error:  # pragma: no cover - import-time failure is fatal
        return [Check("Adapters", False, f"registry failed to load: {error}")]

    extensions = sorted(
        {extension for importer in registry.importers for extension in importer.extensions}
    )
    checks.append(
        Check(
            "Scene adapters",
            True,
            f"{', '.join(registry.engines)} — reads {', '.join(extensions)}",
        )
    )
    for engine, capturer in sorted(registry.capturers.items()):
        available, detail = capturer.available(config)
        checks.append(
            Check(f"{engine} capture", available, detail, required=False)
        )
    if config.engine.project_path:
        project = Path(config.engine.project_path).expanduser()
        checks.append(
            Check(
                "Engine project",
                project.exists(),
                f"{project}" if project.exists() else f"missing: {project}",
                required=False,
            )
        )
    if shutil.which("xvfb-run") is None and sys.platform.startswith("linux"):
        checks.append(
            Check(
                "xvfb-run",
                False,
                "not installed; needed for capture on a display-less Linux host",
                required=False,
            )
        )
    return checks


def _writable_check(name: str, directory: str) -> Check:
    path = Path(directory).expanduser()
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    writable = os.access(target, os.W_OK)
    return Check(
        name=name,
        passed=writable,
        detail=f"{path} ({'writable' if writable else 'not writable'})",
        required=False,
    )
