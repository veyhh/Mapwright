"""Write Scene IR position changes back into a Godot 4 ``.tscn`` file.

A level scene carries far more than Mapwright models: sub-resources, materials,
scripts, signals, groups, and hand-authored properties. Regenerating the file
would silently destroy all of it, so this exporter edits the original text in
place, touching only the transform lines of nodes that actually moved.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from mapwright.adapters.base import AdapterError, ExportResult, SceneExporter
from mapwright.adapters.godot.importer import (
    NUMBER,
    POSITION_PATTERN,
    TRANSFORM_PATTERN,
    GodotNode,
    Transform,
    parse_godot_scene,
)
from mapwright.core.geometry import Vec3
from mapwright.core.scene_ir import SceneIR


#: Positions closer than this are the same authored value, not an edit.
POSITION_EPSILON = 1e-9
#: Decimal places used when writing a coordinate back into the scene.
POSITION_DECIMALS = 6

_NUMBER_PATTERN = re.compile(NUMBER)


@dataclass(frozen=True)
class _Edit:
    """One node's replacement local origin, ready to be written."""

    object_id: str
    node: GodotNode
    local_origin: Vec3


class GodotExporter(SceneExporter):
    """Rewrites node transforms in an existing Godot 4 text scene."""

    engine = "godot"

    def export_scene(
        self,
        scene: SceneIR,
        destination: str | Path,
        original: str | Path | None = None,
    ) -> ExportResult:
        """Rewrite only the transforms of moved objects, preserving everything else."""
        source = _source_path(scene, original)
        target = Path(destination).expanduser()
        try:
            text = source.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AdapterError(f"Godot scene file not found: {source}") from exc
        except UnicodeDecodeError as exc:
            raise AdapterError(
                f"Godot scene is not a UTF-8 text scene: {source}"
            ) from exc
        except OSError as exc:
            raise AdapterError(f"Could not read Godot scene {source}: {exc}") from exc

        parsed = parse_godot_scene(text)
        edits, notes = _plan_edits(scene, parsed.nodes)
        lines = text.splitlines(keepends=True)
        # Later edits first: an inserted line would shift every index after it.
        for edit in sorted(edits, key=lambda item: item.node.header_index, reverse=True):
            lines = _apply_edit(lines, edit)

        if not edits and target.resolve() == source.resolve():
            return ExportResult(
                path=target, changed_objects=(), written=False, notes=notes
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(lines), encoding="utf-8")
        return ExportResult(
            path=target,
            changed_objects=tuple(edit.object_id for edit in edits),
            written=True,
            notes=notes,
        )


def _source_path(scene: SceneIR, original: str | Path | None) -> Path:
    candidate = original if original is not None else scene.source_path
    if candidate is None:
        raise AdapterError(
            "Godot export needs the original .tscn file; pass 'original' or import "
            "the scene from disk so it carries a source_path."
        )
    source = Path(candidate).expanduser()
    if not source.is_file():
        raise AdapterError(f"Godot scene file not found: {source}")
    return source


def _plan_edits(
    scene: SceneIR, nodes: tuple[GodotNode, ...]
) -> tuple[tuple[_Edit, ...], tuple[str, ...]]:
    """Decide which nodes move, recomposing world transforms as parents shift."""
    known = {node.node_path for node in nodes}
    wanted: dict[str, tuple[str, Vec3]] = {}
    notes: list[str] = []
    for obj in scene.objects:
        node_path = obj.source_value("node_path") or obj.id
        if node_path not in known:
            notes.append(
                f"Object '{obj.id}' has no matching node in the scene; not written."
            )
            continue
        wanted[node_path] = (obj.id, obj.position)

    edits: list[_Edit] = []
    worlds: dict[str, Transform] = {}
    for node in nodes:
        parent = worlds.get(node.parent_path, Transform.identity())
        world = node.local if node.top_level else parent.compose(node.local)
        target = wanted.get(node.node_path)
        if target is None or not _moved(world.origin, target[1]):
            worlds[node.node_path] = world
            continue
        object_id, position = target
        local_origin = (
            position if node.top_level else parent.inverse_point(position)
        )
        if local_origin is None:
            notes.append(
                f"Object '{object_id}' not moved: the transform of parent "
                f"'{node.parent_path}' is not invertible."
            )
            worlds[node.node_path] = world
            continue
        edits.append(_Edit(object_id=object_id, node=node, local_origin=local_origin))
        worlds[node.node_path] = Transform(world.basis, position)
    return tuple(edits), tuple(notes)


def _moved(current: Vec3, wanted: Vec3) -> bool:
    return any(
        not math.isclose(left, right, rel_tol=0.0, abs_tol=POSITION_EPSILON)
        for left, right in zip(current, wanted)
    )


def _apply_edit(lines: list[str], edit: _Edit) -> list[str]:
    """Rewrite one node block's origin, leaving every other character alone."""
    start = edit.node.header_index + 1
    end = edit.node.property_end
    region = "".join(lines[start:end])
    rewritten = _rewrite_position(region, edit.local_origin)
    if rewritten is None:
        rewritten = _rewrite_transform_origin(region, edit.local_origin)
    if rewritten is None:
        ending = _line_ending(lines[edit.node.header_index])
        inserted = f"position = {_format_vector(edit.local_origin)}{ending}"
        return lines[:start] + [inserted] + lines[start:]
    return lines[:start] + [rewritten] + lines[end:]


def _rewrite_position(region: str, origin: Vec3) -> str | None:
    match = POSITION_PATTERN.search(region)
    if match is None:
        return None
    return _splice(region, [match.span(index) for index in (1, 2, 3)], origin)


def _rewrite_transform_origin(region: str, origin: Vec3) -> str | None:
    match = TRANSFORM_PATTERN.search(region)
    if match is None:
        return None
    offset = match.start(1)
    numbers = list(_NUMBER_PATTERN.finditer(match.group(1)))
    if len(numbers) != 12:
        raise AdapterError(
            f"Expected 12 numeric values in Transform3D, found {len(numbers)}."
        )
    spans = [
        (offset + number.start(), offset + number.end()) for number in numbers[9:12]
    ]
    return _splice(region, spans, origin)


def _splice(region: str, spans: list[tuple[int, int]], origin: Vec3) -> str:
    result = region
    for (start, end), value in zip(reversed(spans), reversed(list(origin))):
        result = f"{result[:start]}{_format_float(value)}{result[end:]}"
    return result


def _format_vector(origin: Vec3) -> str:
    return f"Vector3({', '.join(_format_float(value) for value in origin)})"


def _format_float(value: float) -> str:
    """Format a coordinate compactly: fixed notation, no trailing zeros."""
    if not math.isfinite(value):
        raise AdapterError(f"Cannot write a non-finite coordinate: {value!r}")
    text = f"{value:.{POSITION_DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


def _line_ending(line: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return ending
    return "\n"
