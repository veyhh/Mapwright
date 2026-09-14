"""Render Godot scene views by driving the bundled ``godot/capture.gd`` script.

Godot's ``--headless`` mode selects a dummy renderer and writes blank images,
so capture always runs against a real rendering driver with a 1x1 off-screen
host window, and falls back to Xvfb on a display-less Linux machine. Every
failure here is reported as a note: a missing engine must never stop an
otherwise complete analysis run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from mapwright.adapters.base import CaptureAdapter, CaptureResult
from mapwright.core.config import MapwrightConfig


#: The GDScript entry point shipped with Mapwright.
CAPTURE_SCRIPT = Path(__file__).resolve().parents[3] / "godot" / "capture.gd"
#: Seconds to wait for Godot before giving up on a capture run.
CAPTURE_TIMEOUT = 300.0
#: Flags that force a real renderer; see SKILL.md section 4B.
RENDERING_FLAGS = (
    "--rendering-method",
    "gl_compatibility",
    "--rendering-driver",
    "opengl3",
    "--audio-driver",
    "Dummy",
)
XVFB_ARGUMENTS = ("xvfb-run", "-a", "-s", "-screen 0 1280x1024x24")
_STDERR_NOTE_LIMIT = 600


class GodotCaptureAdapter(CaptureAdapter):
    """Renders top-down and isometric views of a scene with the Godot editor binary."""

    engine = "godot"

    def available(self, config: MapwrightConfig) -> tuple[bool, str]:
        """Return whether a Godot executable, project, and script are all present."""
        executable = _executable(config)
        if executable is None:
            configured = config.engine.executable_path
            return False, (
                f"Godot executable '{configured}' was not found."
                if configured
                else "No Godot executable is configured (engine.executable_path)."
            )
        project = _project(config)
        if project is None:
            configured = config.engine.project_path
            return False, (
                f"Godot project directory '{configured}' does not exist."
                if configured
                else "No Godot project directory is configured (engine.project_path)."
            )
        if not CAPTURE_SCRIPT.is_file():
            return False, f"Capture script is missing: {CAPTURE_SCRIPT}"
        if not (project / "project.godot").is_file():
            return False, f"'{project}' contains no project.godot."
        return True, f"Godot capture ready: {executable}"

    def capture(
        self,
        scene_path: str | Path,
        output_dir: str | Path,
        views: Sequence[str],
        config: MapwrightConfig,
    ) -> CaptureResult:
        """Render the requested views and report which PNG files landed on disk."""
        destination = Path(output_dir).expanduser()
        requested = tuple(views)
        ready, reason = self.available(config)
        if not ready:
            return CaptureResult(
                output_dir=destination, missing=requested, notes=(reason,)
            )

        executable = _executable(config)
        project = _project(config)
        destination.mkdir(parents=True, exist_ok=True)
        command, notes = _build_command(
            str(executable),
            str(project),
            Path(scene_path).expanduser().resolve(),
            destination,
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=CAPTURE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            notes.append(f"Godot capture timed out after {CAPTURE_TIMEOUT:g} seconds.")
        except OSError as exc:
            notes.append(f"Could not run Godot capture: {exc}")
        else:
            if completed.returncode != 0:
                notes.append(
                    f"Godot exited with code {completed.returncode}. "
                    f"{_tail(completed.stderr) or 'No error output.'}"
                )

        images = tuple(
            path for view in requested if (path := destination / f"{view}.png").is_file()
        )
        missing = tuple(
            view for view in requested if not (destination / f"{view}.png").is_file()
        )
        return CaptureResult(
            output_dir=destination,
            images=images,
            missing=missing,
            notes=tuple(notes),
        )


def _build_command(
    executable: str, project: str, scene: Path, destination: Path
) -> tuple[list[str], list[str]]:
    """Assemble the platform-specific Godot invocation and any caveats about it."""
    notes: list[str] = []
    prefix: list[str] = []
    display: list[str] = []
    if sys.platform.startswith("win"):
        display = [
            "--display-driver",
            "windows",
            "--resolution",
            "1x1",
            "--position=-10000,-10000",
        ]
    elif sys.platform.startswith("linux"):
        display = ["--display-driver", "x11"]
        if not os.environ.get("DISPLAY"):
            if shutil.which(XVFB_ARGUMENTS[0]):
                prefix = list(XVFB_ARGUMENTS)
            else:
                notes.append(
                    "DISPLAY is unset and xvfb-run is not installed; Godot has no "
                    "rendering context and the capture will probably fail."
                )
    command = [
        *prefix,
        executable,
        "--path",
        project,
        *display,
        *RENDERING_FLAGS,
        "--script",
        str(CAPTURE_SCRIPT),
        "--",
        str(scene),
        str(destination.resolve()),
    ]
    return command, notes


def _executable(config: MapwrightConfig) -> Path | None:
    configured = config.engine.executable_path
    if not configured:
        return None
    resolved = shutil.which(configured)
    if resolved is not None:
        return Path(resolved)
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_file() else None


def _project(config: MapwrightConfig) -> Path | None:
    configured = config.engine.project_path
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_dir() else None


def _tail(output: str | None) -> str:
    if not output:
        return ""
    text = " ".join(output.split())
    return text if len(text) <= _STDERR_NOTE_LIMIT else f"...{text[-_STDERR_NOTE_LIMIT:]}"
