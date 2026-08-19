# Mapwright

![Godot 4.x](docs/badges/godot-4x.svg) ![Python 3.10+](docs/badges/python-310.svg) ![MIT License](docs/badges/license-mit.svg) ![6 validators](docs/badges/validators-6.svg)

Mapwright is a **Claude Code level-design skill and a suite of six deterministic validators for Godot 4.x**. It does not claim to make AI “design levels.” It gives a coding agent a repeatable way to evaluate and improve its own spatial composition using measurable checks and three rendered views. The result is a narrow, testable workflow—not a replacement for design judgment or playtesting.

> **Mapwright v0.1 is under active development.** It provides a working, tested pipeline within a deliberately limited scope.

## v0.1 scope

### What it does

- Statically analyzes Godot 4.x `.tscn` scenes.
- Guides Claude Code through zone planning, systematic placement, six numerical checks, three-view visual review, and iterative correction.
- Produces repeatable terminal reports when given the same scene and thresholds.
- Treats deletion as a last resort: repositioning, redistribution, rotation, rescaling, or appropriate asset replacement come first.
- Captures `top_down`, `iso_ne`, and `iso_sw` PNG views through Godot 4.x.

### What it does not do

- It does not support Godot 3.x, Unity, or Unreal.
- It is not packaged as a skill for ChatGPT or coding agents other than Claude Code.
- It does not design gameplay, encounters, combat, quests, pacing, AI behavior, or economies. Its target is spatial composition quality.
- It does not run a Godot physics simulation, bake a real NavigationMesh, or replace playtesting.
- It does not prove aesthetic quality with a single score. Validator warnings are evidence for agent or human review.
- It does not cover every asset pipeline. v0.1 has been tested with the single example `.tscn` asset pack in this repository; imported meshes without serialized dimensions may remain unresolved.

## The pipeline

```text
┌─────────────────────┐
│ 1. Plan             │  Define purpose, zones, routes, landmarks,
│    zones + intent   │  negative space, and density targets.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 2. Block out        │  Establish boundaries, traversable space,
│    space + routes   │  major elevation, and sightline intent.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 3. Place            │  Add structures, landmarks, gameplay props,
│    systematically   │  and secondary dressing—in that order.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 4. Validate         │  Run all 6 validators, then inspect
│    numbers + views  │  top-down and 2 isometric captures.
└──────────┬──────────┘
           │
     actionable issue?
       ┌───┴────┐
      yes       no ───────────────► Report the verified result
       │
       ▼
┌─────────────────────┐
│ 5. Correct          │  Reposition and redistribute first;
│    without erasing  │  do not optimize one metric in isolation.
└──────────┬──────────┘
           │
           └──────────────► Return to step 4 (maximum 3 correction passes)
```

Validators run in this order:

`repetition → spacing → density → landmark → navigation → sightline → visual capture`

Landmark and sightline warnings are review notes, not unconditional correction commands: size, position, uniqueness, color, and semantic importance are not equivalent. A navigation `GRID-SENSITIVE` warning is not proof of disconnection either; verify it with a finer grid, captures, and manual review.

## Six deterministic validators

| Validator | What it measures | Honest limitation |
|---|---|---|
| 🔁 **Repetition** | Counts scene `ExtResource` references used by nodes, calculates each asset's share of total usage, and flags values above the configured threshold. | It cannot judge the spatial rhythm or design intent of repetition. Percentage thresholds are sensitive in small scenes, and visually similar assets are not grouped into families. |
| 📏 **Spacing** | Resolves prop nodes' global `Transform3D` positions and compares their center-to-center 3D Euclidean distances. Cameras, lights, markers, and ground/path support nodes are filtered out. | It measures centers rather than mesh surfaces or collision shapes, so objects with very different dimensions can produce misleading results. |
| 🔲 **Density** | Divides the supported ground geometry's XZ bounds into equal-area cells, counts prop origins, and reports a heatmap, empty-cell ratio, variance, standard deviation, and imbalance score. | Results depend on cell size and count origins rather than footprints. Intentional plazas or negative space may correctly appear empty. |
| 🗼 **Landmark** | Derives a world-AABB diagonal from `dimensions` metadata, `custom_aabb`, or supported primitive mesh/CSG sizes, then estimates relative size and hierarchy against the scene median. | This is a heuristic. It does not measure position, uniqueness, color, lighting, silhouette, or narrative importance, and cannot reliably infer dimensions absent from imported mesh serialization. |
| 🧭 **Navigation** | Inflates prop AABB footprints by the required passage radius, builds an XZ occupancy grid, and uses flood-fill to measure significant connected free-space regions and prop approaches. | This is a coarse, grid-sensitive 2D estimate. It ignores height, slopes, stairs, jumping, crouching, doors, dynamic obstacles, collision layers, and real NavigationMesh behavior; rotated AABBs are conservative. |
| 👁️ **Sightline** | Casts 3D line segments from deterministic spawn/entry/coverage points to a configured height inside landmark AABBs, testing intersections against other world AABBs. | This is not a physics raycast. Conservative AABBs ignore transparency, gaps in foliage or meshes, animation, and semantic knowledge of the main route. |

## Case study: `courtyard_level`

This is not a deliberately broken fixture. It began as a realistic first-pass layout for a small courtyard and went through three Mapwright correction passes. The same thresholds were rerun against both scene snapshots.

### Before and after

<table>
  <thead>
    <tr>
      <th width="50%">Before</th>
      <th width="50%">After</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><img src="captures/six_validator_rework/before/top_down.png" width="100%" alt="Courtyard before, top-down view"></td>
      <td><img src="captures/six_validator_rework/after/top_down.png" width="100%" alt="Courtyard after, top-down view"></td>
    </tr>
    <tr>
      <td>One spacing conflict, clustered density, competing size landmarks, and blocked west-side sightlines.</td>
      <td>Spacing cleared, density improved, size hierarchy clarified, and the measured landmark reached 5/5 viewpoints.</td>
    </tr>
  </tbody>
</table>

Additional views: [before · north-east](captures/six_validator_rework/before/iso_ne.png), [before · south-west](captures/six_validator_rework/before/iso_sw.png), [after · north-east](captures/six_validator_rework/after/iso_ne.png), and [after · south-west](captures/six_validator_rework/after/iso_sw.png). The exact analyzed snapshots are stored under [`before`](captures/six_validator_rework/before/courtyard_level.tscn) and [`after`](captures/six_validator_rework/after/courtyard_level.tscn).

### All six metrics

| Validator | Before | After | Observed change |
|---|---|---|---|
| Repetition | 25 references, 0 flagged; every asset at `4.00%` | 25 references, 0 flagged; every asset at `4.00%` | Unchanged; asset variety was preserved. |
| Spacing | 1 `TOO_CLOSE` pair: ExitSign–Pine at `1.170` units | 0 `TOO_CLOSE` pairs | The spacing signal was resolved. |
| Density | `56.51/100` — **WARNING**; 27/48 empty cells (`56.25%`); densest-cell share `12%` | `50.89/100` — below threshold; 24/48 empty cells (`50.00%`); densest-cell share `8%` | Distribution improved, but half the cells remain empty. The composition is not homogeneous, nor does it need to be. |
| Landmark | Pine `5.802` + Oak `5.433`; **COMPETING CANDIDATES** | Oak `6.248` as the sole candidate; Pine `4.931`; **CLEAR SIZE HIERARCHY** | Size hierarchy became clearer. The central well's positional and semantic role is not measured by this validator. |
| Navigation | 1 significant region; largest region `100%`; 25/25 approaches; **GRID-SENSITIVE**; free area `208.56` | 1 significant region + 1 tiny pocket; largest region `99.70%`; 25/25 approaches; **GRID-SENSITIVE**; free area `206.50` | The main space remained connected. The grid-sensitive signal remains a manual review note. |
| Sightline | Pine 4/5, Oak 4/5; west-side rays blocked by NorthBrokenWall/Oak | Oak 5/5; no blocked rays | The sole measured size landmark is visible from every test point. Pine is no longer a final sightline target because it is no longer a candidate. |

The correction deliberately avoided deletion:

- **5 objects moved:** `PathLantern`, `ExitSign`, `FlowerBed`, `GardenRockC`, `NorthBrokenWall`
- **2 objects rescaled:** `Oak` (`1.15×`) and `Pine` (`0.85×`)
- **0 objects deleted; 0 assets added or replaced**

This does not mean the final level is perfect. Density remains sparse, the navigation warning remains, and a semantic landmark such as the well is not captured by a size-only validator. The narrower demonstrated claim is that the pipeline can reduce measurable issues without deleting content while keeping unresolved trade-offs visible.

## Installation

### Requirements

- Python **3.10+**
- Godot **4.x**
- A working graphics driver and rendering context for captures

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

[`requirements.txt`](requirements.txt) contains only `PyYAML` in v0.1. For local paths, copy [`mapwright.config.example.yaml`](mapwright.config.example.yaml) to `mapwright.config.yaml` and edit its values.

### Connect Mapwright to Claude Code

Copy the entire Mapwright directory—or link it with a symlink/junction—into a Claude Code skill location:

```text
# Project-specific
<godot-project>/.claude/skills/mapwright/SKILL.md

# Personal, available across projects
~/.claude/skills/mapwright/SKILL.md
```

Claude Code can discover the skill automatically when a request matches its description. Invoke it explicitly with:

```text
/mapwright
```

See the official [Claude Code Skills documentation](https://code.claude.com/docs/en/slash-commands) for skill discovery and invocation behavior. If you create a top-level `skills` directory for the first time during an active session, Claude Code may need to be restarted.

Do not copy only `SKILL.md`: the workflow resolves its validators and capture script through `${CLAUDE_SKILL_DIR}`, so the complete Mapwright directory must remain together.

## CLI usage

Run commands from the Mapwright root. Add `--help` to any command for every available option.

### Repetition

```bash
python -m tools.repetition_detector examples/courtyard_level.tscn --threshold 5
```

Reports asset counts and usage percentages; flags values above 5%.

### Spacing

```bash
python -m tools.spacing_analyzer examples/courtyard_level.tscn --threshold 1.5
```

Reports prop centers closer than 1.5 Godot units.

### Density

```bash
python -m tools.density_analyzer examples/courtyard_level.tscn --cell-size 3 --imbalance-threshold 55
```

Prints a ground-based ASCII heatmap and regional imbalance metrics.

### Landmark

```bash
python -m tools.landmark_analyzer examples/courtyard_level.tscn --candidate-ratio 1.15 --hierarchy-factor 1.5
```

Estimates relative size hierarchy and competing landmark candidates.

### Navigation

```bash
python -m tools.navigation_analyzer examples/courtyard_level.tscn --minimum-path-width 1 --cell-size 0.25 --minimum-region-area 1
```

Prints an occupancy map, connected regions, and the number of approachable props.

### Sightline

```bash
python -m tools.sightline_analyzer examples/courtyard_level.tscn --eye-height 1.6 --target-height-ratio 0.6 --test-point-count 5
```

Reports clear and blocked rays from automatically selected test points to landmark AABBs. Repeat `--point X,Y,Z` to provide exact viewpoints.

## Three-view capture

Godot 4's `--headless` display driver uses a dummy renderer, so Mapwright does **not** use `--headless` for 3D capture. The script does not require a visible application window, but it does require a real display server and rendering context. It exits with an error instead of producing black PNGs when a dummy/headless environment is detected.

### Windows · OpenGL Compatibility

```powershell
godot --path . --display-driver windows --rendering-method gl_compatibility --rendering-driver opengl3 --audio-driver Dummy --resolution 1x1 --position=-10000,-10000 --script godot/capture.gd -- examples/courtyard_level.tscn captures/courtyard
```

### Display-less Linux/CI · Xvfb

```bash
xvfb-run -a -s "-screen 0 1280x1024x24" godot --path . --display-driver x11 --rendering-method gl_compatibility --rendering-driver opengl3 --audio-driver Dummy --script godot/capture.gd -- examples/courtyard_level.tscn captures/courtyard
```

Both commands produce `top_down.png`, `iso_ne.png`, and `iso_sw.png`.

- GPU acceleration requires a suitable driver and, in containers, potentially access to devices such as `/dev/dri`.
- GPU-less Linux runners can use an external software OpenGL implementation such as Mesa `llvmpipe`, but it is slower; Godot has no built-in software renderer.
- Projects that depend on Forward+ features may not render correctly through Compatibility without suitable Vulkan, D3D12, or Metal support.
- Capture may fail in Windows service/session 0, minimal containers, or runners that cannot create a graphics context. Use Xvfb, GPU passthrough, or a graphics-enabled runner as appropriate.

See Godot's [command-line tutorial](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html) and [`NavigationMesh` reference](https://docs.godotengine.org/en/stable/classes/class_navigationmesh.html) for the upstream behaviors behind these constraints.

## Tests

```bash
python -B -m unittest discover -s tests -v
```

The v0.1 tree contains **12 tests** covering the config loader and all six validators.

## Repository structure

```text
Mapwright/
├── SKILL.md                       # Claude Code level-design pipeline
├── README.md
├── LICENSE                        # MIT
├── requirements.txt               # Python dependencies
├── mapwright.config.example.yaml  # Local path and capture configuration
├── tools/                         # Config loader + six validator CLIs/modules
├── godot/capture.gd               # Three-view Godot 4 capture script
├── docs/badges/                   # Repository-local status badges
├── examples/                      # Example levels and test asset pack
├── captures/                      # Generated PNGs; verified case study is retained
└── tests/                         # Python unit tests
```

## Known limitations and roadmap

Mapwright v0.1 does not provide ChatGPT skill packaging or Unity/Unreal support. Those may be evaluated later, but they are not promised roadmap items.

Released under the MIT License. See [`LICENSE`](LICENSE).
