"""The rule the whole rewrite exists to enforce: core never knows an engine.

v0.1 could not support a second engine because every validator parsed Godot
itself. If an engine name ever reappears above the adapter layer, that coupling
is back and these tests are the thing that notices.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "mapwright"

#: Layers that must stay engine-independent.
ENGINE_FREE_LAYERS = ("core", "validators", "design", "review", "ecosystem")

#: Names that only an adapter may mention.
ENGINE_NAMES = ("godot", "unity", "unreal", "blender", "bpy", "tscn", "umap")

#: The one place engine knowledge is allowed, plus the CLI and doctor, which
#: select adapters by name without knowing what they do.
ALLOWED_PREFIXES = ("mapwright/adapters/",)


def engine_free_modules() -> list[Path]:
    return sorted(
        path
        for layer in ENGINE_FREE_LAYERS
        for path in (PACKAGE / layer).rglob("*.py")
    )


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "module", engine_free_modules(), ids=lambda path: str(path.relative_to(PACKAGE))
)
def test_engine_free_layers_import_no_adapter(module: Path):
    offenders = sorted(
        name
        for name in imported_modules(module)
        if "adapters" in name or any(engine in name.lower() for engine in ENGINE_NAMES)
    )
    assert not offenders, (
        f"{module.relative_to(PACKAGE)} imports engine code: {', '.join(offenders)}"
    )


@pytest.mark.parametrize(
    "module", engine_free_modules(), ids=lambda path: str(path.relative_to(PACKAGE))
)
def test_engine_free_layers_never_handle_an_engine_format(module: Path):
    """No layer above the adapters may recognise an engine's file format.

    Naming an engine is not itself coupling — ``core/config.py`` still accepts
    v0.1's ``godot_project_path`` key so old config files keep loading, and
    that is data, not behaviour. Parsing ``.tscn`` would be coupling, so the
    test looks for format literals rather than for the word.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    literals = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = sorted(
        literal
        for literal in literals
        if literal in (".tscn", ".umap", ".unity", ".blend", "gd_scene", "ext_resource")
    )
    assert not offenders, (
        f"{module.relative_to(PACKAGE)} handles engine formats: {', '.join(offenders)}"
    )


def test_adapters_are_the_only_layer_touching_engines():
    touching = sorted(
        str(path.relative_to(PACKAGE.parent))
        for path in PACKAGE.rglob("*.py")
        if any(engine in path.read_text(encoding="utf-8").lower() for engine in ("bpy", "tscn"))
    )
    for path in touching:
        assert path.startswith(ALLOWED_PREFIXES) or path in (
            "mapwright/cli.py",
            "mapwright/doctor.py",
        ), f"{path} handles engine formats but is not an adapter"


def test_every_public_module_has_a_docstring():
    missing = [
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if path.name != "__init__.py"
        and not ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not missing, f"modules without a docstring: {', '.join(missing)}"
