"""Mapwright: an engine-agnostic level design director for coding agents.

Mapwright plans, measures, explains, and iteratively improves playable spaces.
Adapters convert an engine's scene into the Scene IR; every analyzer reads only
the IR, so the same spatial, flow, pacing, encounter, and fairness checks run
against Godot, Blender, or a plain JSON level.
"""

from mapwright.core.config import MapwrightConfig, load_config
from mapwright.core.context import AnalysisContext
from mapwright.core.issues import Category, Issue, Severity
from mapwright.core.scene_ir import SceneIR

__version__ = "0.2.0"

__all__ = [
    "AnalysisContext",
    "Category",
    "Issue",
    "MapwrightConfig",
    "SceneIR",
    "Severity",
    "load_config",
    "__version__",
]
