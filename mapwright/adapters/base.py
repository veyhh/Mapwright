"""Adapter contracts and engine detection.

An adapter is the only place in Mapwright that may know about a game engine.
Importers turn an engine's scene format into Scene IR, exporters write
corrections back, and capture adapters render views. Everything above this
layer sees only the IR.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from mapwright.core.config import MapwrightConfig
from mapwright.core.scene_ir import SceneIR


class AdapterError(ValueError):
    """Raised when an adapter cannot handle the scene it was given."""


class UnsupportedEngineError(AdapterError):
    """Raised when no adapter claims a scene, or a stub adapter is invoked."""


@dataclass(frozen=True)
class ExportResult:
    """What an exporter changed on disk."""

    path: Path
    changed_objects: tuple[str, ...]
    written: bool
    notes: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """Return a one-line description of the write."""
        if not self.written:
            return f"No changes written to {self.path}"
        return (
            f"Wrote {len(self.changed_objects)} object change(s) to {self.path}"
        )


@dataclass(frozen=True)
class CaptureResult:
    """The views an engine produced, and any that failed."""

    output_dir: Path
    images: tuple[Path, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Return whether every requested view was rendered."""
        return bool(self.images) and not self.missing


class SceneImporter(ABC):
    """Reads an engine scene file into Scene IR."""

    engine: str = "generic"
    extensions: tuple[str, ...] = ()

    def can_import(self, path: str | Path) -> bool:
        """Return whether this adapter recognises a scene file by extension."""
        return Path(path).suffix.lower() in self.extensions

    @abstractmethod
    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Read a scene file and return its engine-independent description."""


class SceneExporter(ABC):
    """Writes Scene IR changes back into an engine scene file."""

    engine: str = "generic"

    @abstractmethod
    def export_scene(
        self,
        scene: SceneIR,
        destination: str | Path,
        original: str | Path | None = None,
    ) -> ExportResult:
        """Write a scene, preserving anything Mapwright does not understand."""


class CaptureAdapter(ABC):
    """Renders views of a scene for visual review."""

    engine: str = "generic"

    @abstractmethod
    def capture(
        self,
        scene_path: str | Path,
        output_dir: str | Path,
        views: Sequence[str],
        config: MapwrightConfig,
    ) -> CaptureResult:
        """Render the requested views and return where they landed."""

    def available(self, config: MapwrightConfig) -> tuple[bool, str]:
        """Return whether capture can run now, and why not when it cannot."""
        return True, "available"


@dataclass
class AdapterRegistry:
    """Maps engines and file extensions to the adapters that serve them."""

    importers: list[SceneImporter] = field(default_factory=list)
    exporters: dict[str, SceneExporter] = field(default_factory=dict)
    capturers: dict[str, CaptureAdapter] = field(default_factory=dict)

    def register_importer(self, importer: SceneImporter) -> None:
        """Add an importer to the detection order."""
        self.importers.append(importer)

    def register_exporter(self, exporter: SceneExporter) -> None:
        """Add an exporter for one engine."""
        self.exporters[exporter.engine] = exporter

    def register_capturer(self, capturer: CaptureAdapter) -> None:
        """Add a capture adapter for one engine."""
        self.capturers[capturer.engine] = capturer

    @property
    def engines(self) -> tuple[str, ...]:
        """Return every engine with an importer, in registration order."""
        return tuple(dict.fromkeys(importer.engine for importer in self.importers))

    def detect(self, path: str | Path) -> SceneImporter:
        """Return the importer that claims a scene file by extension."""
        for importer in self.importers:
            if importer.can_import(path):
                return importer
        known = ", ".join(
            sorted({extension for item in self.importers for extension in item.extensions})
        )
        raise UnsupportedEngineError(
            f"No Mapwright adapter handles '{Path(path).suffix or Path(path).name}'. "
            f"Supported scene files: {known}."
        )

    def importer_for(self, engine: str) -> SceneImporter:
        """Return one engine's importer by name."""
        for importer in self.importers:
            if importer.engine == engine:
                return importer
        raise UnsupportedEngineError(
            f"No importer registered for engine '{engine}'. "
            f"Available: {', '.join(self.engines)}."
        )

    def exporter_for(self, engine: str) -> SceneExporter:
        """Return one engine's exporter by name."""
        exporter = self.exporters.get(engine)
        if exporter is None:
            raise UnsupportedEngineError(
                f"Engine '{engine}' cannot write scene changes yet. "
                f"Available: {', '.join(sorted(self.exporters))}."
            )
        return exporter

    def capturer_for(self, engine: str) -> CaptureAdapter:
        """Return one engine's capture adapter by name."""
        capturer = self.capturers.get(engine)
        if capturer is None:
            raise UnsupportedEngineError(
                f"Engine '{engine}' has no capture adapter. "
                f"Available: {', '.join(sorted(self.capturers)) or 'none'}."
            )
        return capturer

    def load_scene(
        self,
        path: str | Path,
        config: MapwrightConfig,
        engine: str | None = None,
    ) -> SceneIR:
        """Import a scene, detecting the engine from the file when not given."""
        importer = self.importer_for(engine) if engine else self.detect(path)
        return importer.import_scene(path, config)


def default_registry() -> AdapterRegistry:
    """Return the registry with every built-in adapter installed."""
    from mapwright.adapters.blender.capture import BlenderCaptureAdapter
    from mapwright.adapters.generic.exporter import GenericExporter
    from mapwright.adapters.generic.importer import GenericImporter
    from mapwright.adapters.godot.capture import GodotCaptureAdapter
    from mapwright.adapters.godot.exporter import GodotExporter
    from mapwright.adapters.godot.importer import GodotImporter

    registry = AdapterRegistry()
    registry.register_importer(GenericImporter())
    registry.register_importer(GodotImporter())
    registry.register_exporter(GenericExporter())
    registry.register_exporter(GodotExporter())
    registry.register_capturer(GodotCaptureAdapter())
    registry.register_capturer(BlenderCaptureAdapter())
    return registry
