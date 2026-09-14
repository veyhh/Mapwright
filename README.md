# Mapwright

![engine-agnostic](docs/badges/engine-agnostic.svg) ![Python 3.10+](docs/badges/python-310.svg) ![MIT License](docs/badges/license-mit.svg) ![11 analyzers](docs/badges/analyzers-11.svg) ![112 tests](docs/badges/tests-112.svg)

**Engine-agnostic level design director for AI coding agents.** Mapwright plans, builds, evaluates, and iteratively improves playable spaces using measurable spatial, gameplay, flow, and visual checks.

It is not a map generator. It is the reasoning, validation, and correction layer around whatever builds the map: it reads a level, measures how it plays, explains what it found in terms a designer can argue with, proposes the specific edit that would fix it, applies the ones it can verify, and measures again.

```text
UNDERSTAND → PLAN → BUILD → MEASURE → REVIEW → CORRECT → REBUILD
```

> **v0.2 is a rewrite.** v0.1 was a suite of six validators that parsed Godot `.tscn` files directly. v0.2 parses once into an engine-independent **Scene IR**; every analyzer reads only that. The v0.1 command line still works and still produces identical numbers — see [Backward compatibility](#backward-compatibility).

## What it does

```bash
$ mapwright analyze examples/outpost_level.json
```

```text
Mapwright analysis: outpost (generic, profile generic)

Spatial Quality      26
Flow                 75
Navigation           100
Pacing               84
Encounter            82
Fairness             n/a (Fairness analysis needs spawn points for two or more teams.)
Visual Readability   83

Overall score:       75 / 100

REVIEW — score 75/100, 30 warning(s)

WARNING  chokepoint
         courtyard -> tunnel is the only route between two halves of the level
         (3 place(s) on one side, 4 on the other). It is 4.00 m wide and carries
         50% of routed journeys.
         -> Add a second connection between the courtyard side and the tunnel
            side so the split is a choice rather than a single point of failure.

WARNING  missing_relief [arena]
         arena peaks at intense and nothing follows it.
         -> Add a relief beat after arena — a calm or low zone, or a relief-typed
            space such as a safe room or an exit approach — so the peak has an
            aftermath.
```

Then let it correct what it can:

```bash
$ mapwright improve examples/outpost_level.json
```

```text
Spatial Quality      26 -> 54 (+28)
Flow                 75 -> 78 (+3)
Navigation           100 -> 97 (-3)
Pacing               84 -> 84 (+0)
Encounter            82 -> 82 (+0)
Fairness             n/a -> n/a
Visual Readability   83 -> 88 (+5)

Overall score:       75 -> 81 (+6)
```

Three passes, 11 corrections applied, 9 rejected after measurement, 33 findings down to 23. Every applied change was verified to raise the score on its own; the ones that traded one finding for another were reported and discarded. The navigation dip is real and reported rather than hidden — moving props to relieve crowding cost a little clearance elsewhere.

The findings it could not fix itself are stated as design work, not faked:

```text
Needs a design decision
- no_flank_route — arena: Add a route to arena that shares no segment with the
  main approach — a parallel corridor, a rooftop, a service tunnel — so the
  fight can be opened from a second direction.
```

## Quick start

```bash
pip install -e .

mapwright doctor                          # what works in this environment
mapwright inspect  <scene>                # what Mapwright sees, and what it cannot measure
mapwright analyze  <scene>                # every check, plus reports/level_report.{md,json}
mapwright flow     <scene>                # or: pacing, encounter, fairness
mapwright improve  <scene> --write-scene out.tscn
mapwright capture  <scene> --output captures/
```

Scene type is detected from the extension (`.tscn` → Godot, `.json` → Scene IR, `.blend` → Blender) and can be forced with `--engine`. Genre is selected with `--profile`.

## Architecture

The one rule the codebase enforces: **core never knows an engine.**

```text
        .tscn        .json        .blend        .unity / .umap
          │            │            │                 │
          ▼            ▼            ▼                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │  adapters/   the only layer allowed to import an engine   │
   └──────────────────────────────┬───────────────────────────┘
                                  ▼
                        ┌───────────────────┐
                        │     Scene IR      │   flat, world-space,
                        │  JSON, Y-up, m    │   deterministic
                        └─────────┬─────────┘
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │  core/      geometry · occupancy grid · clearance ·       │
   │             route graph · zones · scoring                 │
   ├──────────────────────────────────────────────────────────┤
   │  design/    flow · pacing · encounters · composition ·    │
   │             readability · correction                      │
   ├──────────────────────────────────────────────────────────┤
   │  validators/  thresholds → explainable findings            │
   ├──────────────────────────────────────────────────────────┤
   │  review/    structural · visual · Markdown + JSON report  │
   └──────────────────────────────────────────────────────────┘
```

`design/` measures without opinion; `validators/` applies thresholds and raises findings. That split is why one set of checks serves every genre: only the numbers move.

## Scene IR

Every adapter produces this, and only this. Right-handed, Y-up, metres, Euler rotations in intrinsic Y-X-Z order, world-space transforms with no hierarchy.

```json
{
  "scene": "courtyard",
  "units": "meters",
  "coordinate_system": "Y_UP",
  "objects": [
    {
      "id": "tree_01",
      "name": "Oak Tree",
      "type": "prop",
      "asset": "oak_tree",
      "position": [3.2, 0.0, -5.4],
      "rotation": [0.0, 1.57, 0.0],
      "scale": [1.0, 1.0, 1.0],
      "bounds": { "min": [-1.0, 0.0, -1.0], "max": [1.0, 5.0, 1.0] },
      "tags": ["foliage", "landmark_candidate"]
    }
  ],
  "zones": [
    { "name": "courtyard", "type": "exploration", "pacing": "medium",
      "bounds": { "min": [-12, 0, -15], "max": [0, 6, 15] } }
  ],
  "entry_points": [], "spawn_points": [], "objectives": [], "exits": [], "landmarks": []
}
```

Geometry alone cannot say what a space is *for*, so zones carry intent and markers carry gameplay structure. Declaring them is what turns Mapwright from a prop checker into a level-design tool: without zones there is no pacing to judge, and without spawns there is no fairness question to ask. `Z_UP` documents are converted on load. See [`examples/outpost_level.json`](examples/outpost_level.json).

## What it measures

| System | Measures | Honest limitation |
|---|---|---|
| 🔁 **Repetition** | each asset's share of total placements | cannot tell rhythm from laziness |
| 📏 **Spacing** | centre distances, plus surface gaps narrower than the player | measures boxes, not silhouettes |
| 🔲 **Density** | equal-area grid distribution, imbalance score, heatmap | depends on cell size; counts origins |
| 🗼 **Landmark** | size hierarchy from intrinsic dimensions | size is not importance, uniqueness, or lighting |
| 🧭 **Navigation** | connected walkable space, clearance, reachability | an occupancy grid, not the engine's navmesh |
| 🔀 **Flow** | critical path, loops, dead ends, chokepoints, route diversity, traversal concentration | cannot tell a mistake from a set piece |
| 🎚 **Pacing** | declared intensity sequence, runs, relief, abrupt transitions | cannot judge whether the curve suits the story |
| ⚔️ **Encounter** | entrances, flanks, high ground, cover spread, retreat, exposure | says nothing about enemy behaviour |
| ⚖️ **Fairness** | per-team distance, travel time, cover, chokes, flanks | not skill, meta, or spawn timing |
| 👁 **Sightline** | landmark visibility along real rays | AABB visibility, not rendered occlusion |
| 🎨 **Visual** | mass balance, silhouette variety, clutter, emptiness | geometric proxies; it has not seen an image |

Every finding states the measurement, why it matters, and what would resolve it:

```text
WARNING
62% of critical traversal passes through corridor_03.
This creates excessive route concentration.
Recommendation:
Create an alternate path between courtyard and objective zone.
```

Findings marked *review note* measure a proxy rather than the property itself, and are weighted accordingly. Mapwright says so instead of presenting a heuristic as a fact.

## Genre profiles

The same geometry is a defect in one genre and the point in another. Profiles shift thresholds **and** per-finding severity.

| Profile | Emphasis | Example effect |
|---|---|---|
| `fps` | encounters, flanks, high ground | `frontal_only_engagement` → ERROR |
| `horror` | confinement, pacing | `dead_end`, `low_route_diversity` → INFO; `missing_relief` → ERROR |
| `stealth` | route choice, cover | route diversity 3, `poor_cover_distribution` → ERROR |
| `platformer` | reachability, silhouette | `disconnected_regions` → INFO (islands are the level) |
| `moba` | symmetry above all | travel-time tolerance 5%, `travel_time_imbalance` → CRITICAL |
| `exploration` | loops, landmarks | `landmark_never_visible` → ERROR |

## Fairness

Give two teams spawn points and Mapwright compares their approaches:

```text
TEAM A -> CENTRE OBJECTIVE  3.9s over 17.5m, 3 cover, 4 high ground, 1.0m choke, 3 flank route(s)
TEAM B -> CENTRE OBJECTIVE  4.4s over 19.8m, 2 cover, 4 high ground, 1.0m choke, 2 flank route(s)

TEAM A -> CENTRE OBJECTIVE 3.9s / TEAM B -> CENTRE OBJECTIVE 4.4s / ASYMMETRY 13.0% (limit 10%)
WARNING Travel-time imbalance exceeds configured threshold.
```

## Corrections

Corrections are **non-destructive**: nothing is deleted. v0.1 established that deleting props to satisfy a metric leaves density and composition no better and the level poorer, so Mapwright repositions, redistributes, rotates, rescales, or proposes a substitution instead.

They are also **honest**. A correction that needs new geometry, a new route, or a designer's judgement is reported as design work rather than faked. Asset substitution is always proposed, never applied: Mapwright can size-match a candidate but cannot tell whether a barrel reads as cover in that corner.

The improvement loop applies and measures **one change at a time**. A batch is planned against a scene that no longer exists by the time the last edit lands — separating one crowded pair moves a prop into the space the next correction assumed was free. One analysis per candidate buys a monotone loop: every kept change provably raised the score, and every rejected one is reported with the number that rejected it.

## Scoring

Each category starts at 100 and loses points per *kind* of finding, weighted by the profile. Repeated instances of one code are damped (`1 + ln n`) rather than summed: ten crowded pairs is one problem of some size, and a linear sum pins any busy level at zero, where a score can no longer show whether a correction helped.

A category that cannot be assessed is excluded from the overall, not scored zero. A single-player level has no teams to treat unequally; scoring it 0 for fairness would be a statement about the level rather than a measurement of it.

## Adapters

| Engine | Import | Export | Capture |
|---|---|---|---|
| **Scene IR JSON** | ✅ | ✅ | — |
| **Godot 4.x** | ✅ `.tscn` | ✅ surgical text patch | ✅ (needs a Godot executable) |
| **Blender** | ✅ `.blend` (headless) | — | ✅ (needs a Blender executable) |
| **Unity** | 📋 documented contract | — | — |
| **Unreal** | 📋 documented contract | — | — |

The Godot exporter rewrites only the transform lines of objects that moved, converting world positions back to local space. It never regenerates the file: a scene holds sub-resources, materials, signals, and user content Mapwright does not model, and losing any of it would be unacceptable.

Unity and Unreal are **deliberately unimplemented**. A `.unity` scene is a GUID-referenced YAML graph whose prefab bounds live elsewhere, and a `.umap` is a binary asset needing the editor; guessing at those bounds would feed every validator numbers that look authoritative and are not. Both modules document exactly what an implementation must satisfy. Until then, export to Scene IR JSON and the whole pipeline runs.

### Roadmap

- **v0.3** — Unity importer against the documented contract; engine-mode navigation through Godot's `NavigationServer`; correction of vertical space (ramps, ledges, jump gaps) rather than XZ only.
- **Later** — Unreal via an editor plugin; Asset Director and Motion Director wired to real services behind the existing `AssetProvider` / `MotionProvider` seams.

## Honest limitations

- **Approximate navigation.** Clearance and connectivity come from a top-down occupancy grid with an exact Euclidean distance transform, not a baked navmesh. Vertical traversal, jumps, and ledges are not modelled; `navigation_mode: engine` is a declared seam, not a shipped feature.
- **Capture is untested in CI.** Neither Godot nor Blender is installed in this repository's test environment, so the capture adapters are exercised only through their availability and failure paths. `mapwright doctor` reports honestly which are usable where you are.
- **Visual review has not seen an image.** It measures mass, silhouette, clutter, and emptiness geometrically and marks every such finding as a review note. Render the views, look at them, and feed real observations back with `review.visual.ingest_observations`.
- **Derived topology is a guess.** A scene with no declared markers or zones gets a route graph sampled from open space. It is marked `derived`, its findings are advisory, and the reports say so.
- **Objects without extents are invisible to spatial checks.** Imported meshes with no serialized dimensions are kept in the IR, excluded from measurement, and listed in every report so a clean spatial section over half-measured geometry cannot be mistaken for a clean level.
- **Shear is not representable.** Scene IR stores position, Euler rotation, and scale; a sheared transform is decomposed and the shear discarded.

## Backward compatibility

The v0.1 tools in `tools/` are **unchanged and still work**:

```bash
python tools/density_analyzer.py examples/courtyard_level.tscn --cell-size 3
```

`tests/test_backward_compatibility.py` pins the two implementations together on both example scenes: given matching thresholds, the ported validators reproduce v0.1's numbers exactly — same analyzed props, same positions, same size scores and landmark candidates, same density statistics (imbalance 50.887625 on the courtyard), same occupancy counts (3304 free cells, 2408 blocked). Nothing about v0.1's behaviour was changed by the port.

Two deliberate differences, both documented above: v0.1 raised an exception on a scene it could not fully measure, where v0.2 reports what it can and says what it could not; and v0.2's default minimum path width comes from the configured player radius rather than a fixed `1.0`.

`tools/` is deprecated and will be removed in v0.3. Prefer `mapwright analyze`.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q        # 112 tests
```

The suite covers Scene IR round-tripping and validation, the exact distance transform against brute force, route-graph topology, adapter detection and surgical export, every analyzer, scoring, correction application, v0.1 parity, and the full analyze → correct → re-analyze loop.

## License

MIT — see [LICENSE](LICENSE).
