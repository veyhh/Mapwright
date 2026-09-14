"""The ``mapwright`` command line.

One entry point over the whole pipeline: inspect what an engine scene becomes,
run the full analysis, drill into a single design system, capture views, or
let Mapwright correct the scene and measure the difference.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from mapwright import __version__
from mapwright.adapters.base import AdapterError, AdapterRegistry, default_registry
from mapwright.core.config import (
    ConfigError,
    MapwrightConfig,
    available_profiles,
    load_config,
)
from mapwright.core.context import AnalysisContext
from mapwright.core.scene_ir import SceneIR, SceneIRError
from mapwright.doctor import format_report as format_doctor
from mapwright.doctor import run_doctor
from mapwright.pipeline import analyze, applicable_categories, improve
from mapwright.review.report import build_report
from mapwright.review.structural import format_report as format_structural
from mapwright.review.structural import review_structural


DEFAULT_CONFIG_NAMES = ("mapwright.config.yaml", "mapwright.config.yml")

#: Single-system commands and the analyzer section each one prints.
SECTION_COMMANDS = {
    "flow": "flow",
    "pacing": "pacing",
    "encounter": "encounter",
    "fairness": "fairness",
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Mapwright command line."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args)
    except (ConfigError, FileNotFoundError) as error:
        parser.error(str(error))

    registry = default_registry()
    try:
        if args.command == "doctor":
            return _run_doctor(args, config)
        if args.command == "inspect":
            return _run_inspect(args, config, registry)
        if args.command == "analyze":
            return _run_analyze(args, config, registry)
        if args.command in SECTION_COMMANDS:
            return _run_section(args, config, registry)
        if args.command == "capture":
            return _run_capture(args, config, registry)
        if args.command == "improve":
            return _run_improve(args, config, registry)
    except (AdapterError, SceneIRError, ConfigError, FileNotFoundError, OSError) as error:
        print(f"mapwright: {error}", file=sys.stderr)
        return 2
    parser.error(f"Unknown command: {args.command}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="mapwright",
        description=(
            "Engine-agnostic level design director: plan, measure, explain, and "
            "improve playable spaces."
        ),
    )
    parser.add_argument("--version", action="version", version=f"mapwright {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check what works in this environment")
    _add_common(doctor, scene=False)

    inspect = subparsers.add_parser(
        "inspect", help="import a scene and describe what Mapwright sees"
    )
    _add_common(inspect)
    inspect.add_argument(
        "--export", metavar="PATH", help="write the Scene IR document to this path"
    )

    analyze_parser = subparsers.add_parser(
        "analyze", help="run every check and write the report"
    )
    _add_common(analyze_parser)
    _add_report_options(analyze_parser)
    analyze_parser.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="NAME",
        help="print one analyzer's detail output; repeatable",
    )

    for name, description in (
        ("flow", "measure traversal structure only"),
        ("pacing", "measure the zone intensity sequence only"),
        ("encounter", "measure combat-space affordances only"),
        ("fairness", "measure competitive symmetry only"),
    ):
        section = subparsers.add_parser(name, help=description)
        _add_common(section)
        section.add_argument("--json", action="store_true", help="emit JSON instead of text")

    capture = subparsers.add_parser("capture", help="render views of a scene")
    _add_common(capture)
    capture.add_argument("--output", metavar="DIR", help="where to write the images")
    capture.add_argument(
        "--view",
        action="append",
        default=[],
        metavar="NAME",
        help="view to render; repeatable, defaults to the configured set",
    )

    improve_parser = subparsers.add_parser(
        "improve", help="analyze, correct, re-analyze, and report the difference"
    )
    _add_common(improve_parser)
    _add_report_options(improve_parser)
    improve_parser.add_argument(
        "--iterations", type=int, metavar="N", help="maximum correction passes"
    )
    improve_parser.add_argument(
        "--write-scene",
        metavar="PATH",
        help="write the corrected scene; Godot scenes are patched in place of a copy",
    )
    improve_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and report corrections without writing any scene file",
    )
    return parser


def _add_common(parser: argparse.ArgumentParser, scene: bool = True) -> None:
    if scene:
        parser.add_argument("scene", help="scene file (.tscn, .json, .blend)")
        parser.add_argument(
            "--engine",
            metavar="NAME",
            help="force an adapter instead of detecting one from the file extension",
        )
    parser.add_argument("--config", metavar="PATH", help="Mapwright config YAML")
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help=f"genre profile ({', '.join(available_profiles())})",
    )


def _add_report_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report-dir", metavar="DIR", help="where to write level_report.md and .json"
    )
    parser.add_argument(
        "--no-report", action="store_true", help="print to the terminal without writing files"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON to stdout")


def resolve_config(args: argparse.Namespace) -> MapwrightConfig:
    """Load configuration from the flags, a config file, or the defaults."""
    path = getattr(args, "config", None)
    if path is None:
        path = _discover_config()
    config = load_config(path) if path else MapwrightConfig()
    profile = getattr(args, "profile", None)
    if profile:
        config = config.with_profile(profile)
    report_dir = getattr(args, "report_dir", None)
    if report_dir:
        config = replace(config, report_dir=report_dir)
    iterations = getattr(args, "iterations", None)
    if iterations:
        config = replace(config, max_iterations=iterations)
    return config


def _discover_config() -> str | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return str(candidate)
    return None


def _load_scene(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> SceneIR:
    return registry.load_scene(args.scene, config, engine=getattr(args, "engine", None))


def _run_doctor(args: argparse.Namespace, config: MapwrightConfig) -> int:
    report = run_doctor(config, getattr(args, "config", None) or _discover_config())
    print(format_doctor(report))
    return 0 if report.healthy else 1


def _run_inspect(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> int:
    scene = _load_scene(args, config, registry)
    context = AnalysisContext(scene=scene, config=config)
    review = review_structural(context, (), applicable_categories(context))
    print(format_structural(review))
    if args.export:
        written = scene.write_json(args.export)
        print(f"\nScene IR written to {written}")
    return 0


def _run_analyze(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> int:
    scene = _load_scene(args, config, registry)
    analysis = analyze(scene, config)
    report = build_report(analysis)
    if args.json:
        print(json.dumps(report.data, indent=2, ensure_ascii=False))
    else:
        print(_terminal_summary(analysis))
        for name in args.section:
            print()
            print(analysis.format_section(name))
    if not args.no_report:
        markdown_path, json_path = report.write(config.report_dir)
        if not args.json:
            print(f"\nReport written to {markdown_path} and {json_path}")
    return 1 if analysis.scorecard.overall < 50 else 0


def _run_section(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> int:
    scene = _load_scene(args, config, registry)
    analysis = analyze(scene, config)
    name = SECTION_COMMANDS[args.command]
    report = analysis.report(name)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(analysis.format_section(name))
    return 0


def _run_capture(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> int:
    engine = getattr(args, "engine", None) or registry.detect(args.scene).engine
    capturer = registry.capturer_for(engine)
    available, detail = capturer.available(config)
    if not available:
        print(f"mapwright: capture unavailable — {detail}", file=sys.stderr)
        return 2
    views = tuple(args.view) or config.capture_views
    result = capturer.capture(
        args.scene, args.output or config.capture_output_dir, views, config
    )
    for image in result.images:
        print(f"rendered {image}")
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    if result.missing:
        print(f"missing views: {', '.join(result.missing)}", file=sys.stderr)
        return 1
    return 0


def _run_improve(
    args: argparse.Namespace, config: MapwrightConfig, registry: AdapterRegistry
) -> int:
    scene = _load_scene(args, config, registry)
    result = improve(scene, config)
    report = build_report(
        result.final,
        plan=result.iterations[-1].plan if result.iterations else None,
        improvement=result,
    )
    if args.json:
        print(json.dumps(report.data, indent=2, ensure_ascii=False))
    else:
        print(_terminal_summary(result.final))
        print()
        print(report.data["improvement"]["comparison"])

    if args.write_scene and not args.dry_run:
        engine = getattr(args, "engine", None) or registry.detect(args.scene).engine
        exporter = registry.exporter_for(engine)
        export = exporter.export_scene(result.scene, args.write_scene, original=args.scene)
        print(f"\n{export.summary}")
        for note in export.notes:
            print(f"note: {note}", file=sys.stderr)
    if not args.no_report:
        markdown_path, json_path = report.write(config.report_dir)
        if not args.json:
            print(f"Report written to {markdown_path} and {json_path}")
    return 0


def _terminal_summary(analysis) -> str:
    """Return the compact terminal view: verdict, scores, and findings."""
    from mapwright.review.report import _verdict

    lines = [
        f"Mapwright analysis: {analysis.scene.scene} "
        f"({analysis.scene.source_engine}, profile {analysis.config.profile})",
        "",
        analysis.scorecard.format_table(),
        "",
        _verdict(analysis),
    ]
    if analysis.issues:
        lines.extend(["", "Findings, most severe first:", ""])
        for issue in analysis.issues:
            marker = " (review note)" if issue.advisory else ""
            zone = f" [{issue.zone}]" if issue.zone else ""
            lines.append(f"{issue.severity.value:8s} {issue.code}{zone}{marker}")
            lines.append(f"         {issue.evidence}")
            lines.append(f"         -> {issue.recommendation}")
            lines.append("")
    return "\n".join(lines).rstrip()


if __name__ == "__main__":
    raise SystemExit(main())
