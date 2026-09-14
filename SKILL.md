---
name: mapwright
description: Engine-agnostic level design director. Use when planning, building, evaluating, or improving a playable 3D space in any engine — analysing a level's flow, pacing, encounters, or competitive fairness; diagnosing why a map feels boring, unfair, confusing, or cramped; checking whether an arena is balanced for two teams or whether combat works in a corridor; making a level more readable; finding a scene's level-design problems; or iteratively improving a map. Works on Godot 4.x `.tscn` scenes and on engine-independent Scene IR JSON with no engine installed.
---

# Mapwright level design director

Mapwright measures a playable space, explains what it found, proposes specific
corrections, and measures again. Placement is not completion, and neither is a
passing metric: the goal is a space that reads and plays, with every claim
backed by a number you can check.

Work this loop:

```
UNDERSTAND -> PLAN -> BUILD -> MEASURE -> REVIEW -> CORRECT -> REBUILD
```

## 1. Understand the space

Run `mapwright inspect` first. It reports what Mapwright can see and, more
importantly, what it cannot measure.

```bash
mapwright inspect <scene>            # .tscn, .json, or .blend
mapwright inspect <scene> --export ir.json
```

Read the coverage block before anything else. A check listed as *not
applicable* is not a pass — it means the scene never declared what that check
needs. Objects listed without usable extents are excluded from every spatial
measurement, so a clean spatial report over half-measured geometry means
little.

If `mapwright doctor` reports a blocked required check, fix that first.

## 2. Declare intent, not just geometry

Mapwright cannot infer what a space is *for*. Flow, pacing, encounter, and
fairness analysis all depend on declarations the scene has to carry:

| Declaration | Unlocks | Without it |
|---|---|---|
| `zones` with `type` and `pacing` | pacing, encounter | pacing reports `no_zone_intent`; encounter has nothing to analyse |
| `entry_points`, `objectives`, `exits` | critical path, route diversity | topology is sampled from walkable space and every finding becomes advisory |
| `spawn_points` with `team` | fairness | fairness stands down |

In a Godot scene, name `Marker3D`/`Node3D` nodes with `spawn`, `entry`,
`exit`, or `objective` tokens and the importer picks them up. In Scene IR
JSON, declare them directly. Adding four markers and four zones changes
Mapwright from a prop checker into a level-design tool — do it before
concluding a level is fine.

Choose a genre profile too. The same geometry is a defect in one genre and the
point in another: a forced corridor is bad flow in an exploration map and
correct in horror.

```bash
mapwright analyze <scene> --profile horror    # fps, stealth, platformer, moba, exploration
```

## 3. Plan zones before placing anything

For new work, write a compact plan first:

1. Gameplay or experiential purpose.
2. Entry, exit, critical route, optional routes.
3. Named zones with one purpose and one intensity each.
4. Intended landmarks and the approaches that should reveal them.
5. Intended density per zone, and the negative space to protect.
6. Asset families per zone, and which assets stay rare.

Place an asset only when it serves a zone, route, landmark, boundary,
gameplay need, or readable environmental story. Work from large-scale
structure to detail: boundaries and traversable space, then routes and
sightlines, then structures and landmarks, then cover and obstacles, then
secondary dressing.

## 4. Measure

```bash
mapwright analyze <scene>
mapwright analyze <scene> --section density --section flow   # detail for one analyzer
mapwright flow <scene>        # or: pacing, encounter, fairness
```

`analyze` writes `reports/level_report.md` and `reports/level_report.json`,
and prints the score table with every finding, most severe first.

What each system measures, and what it cannot:

| System | Measures | Cannot judge |
|---|---|---|
| Spatial | asset share, centre distances, grid density, size hierarchy | whether repetition is rhythm or laziness |
| Navigation | connected walkable space, clearance, reachability | anything the engine's own navmesh would add |
| Flow | critical path, loops, dead ends, chokepoints, route diversity, traversal concentration | whether a forced route is a mistake or a set piece |
| Pacing | declared intensity sequence, runs, relief, abrupt transitions | whether the curve suits the story |
| Encounter | entrances, flanks, high ground, cover spread, retreat, exposure | how enemies actually behave |
| Fairness | per-team distance, travel time, cover, chokes, high ground | skill, meta, or spawn timing |
| Visual | mass balance, silhouette variety, clutter, emptiness | what a rendered frame looks like |

Findings marked *review note* (`advisory`) measure a proxy, not the design
property itself. Size is not importance; a geometric mass check is not a look
at the image; a graph sampled from open space is not the designer's intended
topology. Weigh them, do not obey them.

## 5. Review the evidence, then correct

Classify every finding as actionable, intentional, or review-only. When it is
intentional, say so and why — an unexplained ignored finding is
indistinguishable from an unnoticed one.

Correct by **repositioning, redistributing, rotating, rescaling, or
substituting**. Do not delete placed content to make a metric pass: v0.1
showed that deleting props leaves density and composition no better and the
level poorer. Deletion is a last resort for an asset with no remaining design
purpose, and must be reported when it happens.

```bash
mapwright improve <scene> --dry-run                        # propose only
mapwright improve <scene> --write-scene corrected.tscn     # apply and write
```

`improve` applies only the corrections it can verify, re-analyses, and keeps a
pass only when the overall score actually rises. Structural findings — a
missing second route, a single-entrance arena, an absent relief beat — are
reported as **needs a design decision** rather than faked. Those are yours.

## 6. Capture and look

Numbers miss what a frame shows. Render the views and open them.

```bash
mapwright capture <scene> --output captures/iteration-1
```

Do not use Godot `--headless` for 3D capture: it selects a dummy renderer and
writes blank images. Mapwright's Godot adapter already uses a real driver with
an off-screen window, and `xvfb-run` on a display-less Linux host. Confirm
`top_down.png`, `iso_ne.png`, and `iso_sw.png` exist and are not blank, then
open all three and check:

1. Intentional versus accidental empty regions.
2. Over-clustering and one-sided visual mass.
3. Intersections, ground embedding, tight clearances.
4. Rows, grids, equal gaps, repeated rotations, silhouettes.
5. Landmark hierarchy and visibility from intended approaches.
6. Entry, exit, optional areas, and negative-space readability.
7. Occlusion visible from only one angle.

Record each observation as a structured issue with its zone and the view that
exposes it, using Mapwright's own visual issue codes (`visual_mass_imbalance`,
`landmark_weakness`, `excessive_repetition`, `poor_silhouette`, `clutter`,
`empty_zone`, `contrast_problem`, `composition_bias`, `sightline_block`,
`navigation_readability`). Never write "looks off". Feed them back in with
`mapwright.review.visual.ingest_observations` so they join the same report.

Never claim visual validation unless the final cycle's images were generated
and actually inspected.

## 7. Iterate, then stop

After any scene change, re-run the **whole** analysis, not the metric you
targeted: a spacing fix routinely creates a density, navigation, or sightline
regression. Allow at most three correction passes. If material issues remain,
stop, keep the best verified scene, and report the unresolved trade-offs
rather than looping or deleting content to force a pass.

## 8. Report

Provide:

1. Zone and placement summary.
2. Before/after score table, same profile and thresholds both times.
3. Final capture paths and a view-by-view assessment.
4. Correction-pass count, and what was moved, replaced, added, or deleted.
   State explicitly when nothing was deleted.
5. Findings deliberately not acted on, with the reason.
6. Environment limits that affected the run — an unavailable renderer, objects
   without extents, a topology that had to be inferred.

## Working without an engine

Mapwright's analysers never import an engine. A level written as Scene IR
JSON gets the full pipeline with nothing installed:

```bash
mapwright analyze level.json
```

Engine scenes are converted by adapters (`.tscn` → Godot, `.json` → generic,
`.blend` → Blender). Unity and Unreal have documented import contracts but no
implementation in v0.2; export those levels to Scene IR JSON instead. See
`examples/outpost_level.json` for the schema in practice.

## Legacy v0.1 tools

The v0.1 Godot-only scripts under `tools/` still run unchanged and produce
identical numbers — `tests/test_backward_compatibility.py` pins them together.
They are deprecated: prefer `mapwright analyze`, which covers the same six
checks plus flow, pacing, encounter, fairness, scoring, and correction.
