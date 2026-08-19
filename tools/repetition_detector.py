"""Analyze external asset repetition in Godot 4 text scenes."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


DEFAULT_THRESHOLD = 5.0

_TAG_PATTERN = re.compile(
    r"^\s*\[(?P<name>[a-z_]+)(?P<body>.*?)\]\s*(?:;.*)?$"
)


class TscnParseError(ValueError):
    """Raised when a scene is not a supported Godot 4 text scene."""


@dataclass(frozen=True)
class ExternalResource:
    """An ``ext_resource`` declaration from a TSCN file."""

    resource_id: str
    path: str
    resource_type: str

    @property
    def name(self) -> str:
        """Return a readable asset name derived from its resource path."""
        filename = PurePosixPath(self.path.replace("\\", "/")).name
        return PurePosixPath(filename).stem or filename or self.resource_id


@dataclass(frozen=True)
class AssetUsage:
    """Usage statistics for one external asset."""

    name: str
    path: str
    resource_type: str
    count: int
    percentage: float
    flagged: bool


@dataclass(frozen=True)
class RepetitionReport:
    """Complete repetition analysis for a scene."""

    scene_path: str
    threshold: float
    total_references: int
    assets: tuple[AssetUsage, ...]


def _decode_quoted_string(raw_value: str, context: str) -> str:
    try:
        return json.loads(f'"{raw_value}"')
    except json.JSONDecodeError as exc:
        raise TscnParseError(f"Invalid quoted string in {context}.") from exc


def _quoted_attribute(tag_body: str, name: str, context: str) -> str:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*\"((?:\\.|[^\"\\])*)\"",
        tag_body,
    )
    if match is None:
        raise TscnParseError(f"Missing '{name}' in {context}.")
    return _decode_quoted_string(match.group(1), context)


def _scene_format(tag_body: str) -> int:
    match = re.search(r"(?:^|\s)format\s*=\s*(\d+)(?:\s|$)", tag_body)
    if match is None:
        raise TscnParseError("Missing 'format' in gd_scene header.")
    return int(match.group(1))


def _skip_quoted_string(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index + 1
        else:
            index += 1
    raise TscnParseError("Unterminated quoted string in node data.")


def _find_ext_resource_references(text: str) -> list[str]:
    """Find ExtResource IDs while ignoring comments and ordinary strings."""
    references: list[str] = []
    index = 0

    while index < len(text):
        character = text[index]

        if character == ";":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue

        if character == '"':
            index = _skip_quoted_string(text, index)
            continue

        token = "ExtResource"
        if text.startswith(token, index):
            before_is_identifier = index > 0 and (
                text[index - 1].isalnum() or text[index - 1] == "_"
            )
            cursor = index + len(token)
            after_is_identifier = cursor < len(text) and (
                text[cursor].isalnum() or text[cursor] == "_"
            )
            if before_is_identifier or after_is_identifier:
                index += 1
                continue

            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] != "(":
                index += len(token)
                continue

            cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1

            if cursor >= len(text) or text[cursor] != '"':
                raise TscnParseError(
                    "ExtResource references must use a quoted Godot 4 resource ID."
                )

            end = _skip_quoted_string(text, cursor)
            resource_id = _decode_quoted_string(
                text[cursor + 1 : end - 1], "ExtResource reference"
            )
            cursor = end
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] != ")":
                raise TscnParseError("Malformed ExtResource reference in node data.")

            references.append(resource_id)
            index = cursor + 1
            continue

        index += 1

    return references


def parse_tscn(scene_text: str) -> tuple[dict[str, ExternalResource], list[str]]:
    """Parse external declarations and node references from Godot 4 TSCN text.

    References in sub-resources, connections, comments, and string literals are
    intentionally excluded. Each actual ``ExtResource`` occurrence in a node
    header or node property counts as one use.
    """
    resources: dict[str, ExternalResource] = {}
    node_chunks: list[str] = []
    current_node_lines: list[str] | None = None
    header_checked = False

    for line_number, line in enumerate(scene_text.lstrip("\ufeff").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            if current_node_lines is not None:
                current_node_lines.append(line)
            continue

        tag_match = _TAG_PATTERN.match(line)
        if tag_match is not None:
            tag_name = tag_match.group("name")
            tag_body = tag_match.group("body")

            if not header_checked:
                if tag_name != "gd_scene":
                    raise TscnParseError(
                        "The first TSCN entry must be a gd_scene header."
                    )
                scene_format = _scene_format(tag_body)
                if scene_format != 3:
                    raise TscnParseError(
                        f"Unsupported TSCN format {scene_format}; Godot 4 format 3 is required."
                    )
                header_checked = True
                continue

            if current_node_lines is not None:
                node_chunks.append("\n".join(current_node_lines))
                current_node_lines = None

            if tag_name == "ext_resource":
                context = f"ext_resource declaration on line {line_number}"
                resource_id = _quoted_attribute(tag_body, "id", context)
                if resource_id in resources:
                    raise TscnParseError(
                        f"Duplicate ext_resource id '{resource_id}' on line {line_number}."
                    )
                resources[resource_id] = ExternalResource(
                    resource_id=resource_id,
                    path=_quoted_attribute(tag_body, "path", context),
                    resource_type=_quoted_attribute(tag_body, "type", context),
                )
            elif tag_name == "node":
                current_node_lines = [line]
            continue

        if not header_checked:
            raise TscnParseError("The first TSCN entry must be a gd_scene header.")
        if current_node_lines is not None:
            current_node_lines.append(line)

    if current_node_lines is not None:
        node_chunks.append("\n".join(current_node_lines))
    if not header_checked:
        raise TscnParseError("TSCN scene is empty or has no gd_scene header.")

    references = [
        resource_id
        for node_chunk in node_chunks
        for resource_id in _find_ext_resource_references(node_chunk)
    ]
    unknown_ids = sorted(set(references).difference(resources))
    if unknown_ids:
        unknown = ", ".join(unknown_ids)
        raise TscnParseError(f"Node data references undefined ext_resource ID(s): {unknown}")

    return resources, references


def analyze_scene(path: str, threshold: float = DEFAULT_THRESHOLD) -> RepetitionReport:
    """Analyze a Godot 4 ``.tscn`` file and return per-asset usage statistics."""
    if not 0 <= threshold <= 100:
        raise ValueError("Repetition threshold must be between 0 and 100 percent.")

    scene_path = Path(path).expanduser()
    if scene_path.suffix.lower() != ".tscn":
        raise ValueError(f"Expected a .tscn scene file: {scene_path}")

    try:
        scene_text = scene_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Godot scene file not found: {scene_path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Godot scene is not a UTF-8 text scene: {scene_path}"
        ) from exc

    resources, references = parse_tscn(scene_text)
    counts = Counter(references)
    total = len(references)
    assets = []

    for resource in resources.values():
        count = counts[resource.resource_id]
        percentage = count / total * 100 if total else 0.0
        assets.append(
            AssetUsage(
                name=resource.name,
                path=resource.path,
                resource_type=resource.resource_type,
                count=count,
                percentage=percentage,
                flagged=percentage > threshold,
            )
        )

    assets.sort(key=lambda asset: (-asset.count, asset.name.casefold(), asset.path))
    return RepetitionReport(
        scene_path=str(scene_path),
        threshold=threshold,
        total_references=total,
        assets=tuple(assets),
    )


def format_report(report: RepetitionReport) -> str:
    """Format an analysis report as a terminal-friendly table."""
    headers = ("Asset", "Uses", "Percent", "Status", "Path")
    rows = [
        (
            asset.name,
            str(asset.count),
            f"{asset.percentage:.2f}%",
            "FLAGGED" if asset.flagged else "-",
            asset.path,
        )
        for asset in report.assets
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    divider = "  ".join("-" * width for width in widths)
    lines = [
        f"Mapwright repetition report: {report.scene_path}",
        f"Total asset references: {report.total_references} | Flag threshold: > {report.threshold:g}%",
        "",
        render_row(headers),
        divider,
    ]
    lines.extend(render_row(row) for row in rows)
    lines.append("")
    lines.append(f"Flagged assets: {sum(asset.flagged for asset in report.assets)}")
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Analyze external asset repetition in a Godot 4 .tscn scene."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Flag assets used above this percentage (default: {DEFAULT_THRESHOLD:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the repetition detector CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_scene(args.scene, args.threshold)
    except (FileNotFoundError, OSError, TscnParseError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
