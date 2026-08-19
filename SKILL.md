---
name: mapwright
description: Plan, build, and iteratively validate 3D levels in Godot 4.x. Use for level design, environment layout, prop placement, scene dressing, gameplay-space composition, or revising a Godot 3D scene without random asset scattering.
---

# Mapwright level design pipeline

Use this workflow for every Godot 4.x level-design task. Placement alone is not completion. Validate the scene numerically and visually, correct material issues, and preserve the intended gameplay flow.

## 1. Establish inputs

1. Locate the Mapwright root, Godot project root, target `.tscn`, asset pack, capture directory, and Godot executable. Read `mapwright.config.yaml` when present; discover values that are safely discoverable before asking.
2. Confirm Godot 4.x and a target scene inside the project.
3. Inspect the asset pack and existing scene conventions before editing. Do not invent resource paths or scatter placeholders unless blockout geometry is requested.
4. Preserve user-authored content unless replacement is explicitly authorized.

## 2. Plan zones and intent

Before editing, write a compact plan covering:

1. Gameplay or experiential purpose.
2. Entry, exit, critical route, optional routes, and player flow.
3. Named zones with one purpose and visual identity each.
4. Intended primary landmarks and reveal sightlines.
5. Intended density per zone and required negative space.
6. Asset families per zone and assets that should remain rare.

Place an asset only when it supports a zone, route, landmark, boundary, gameplay need, or readable environmental story.

## 3. Place assets systematically

Work from large-scale structure to detail:

1. Block out boundaries, traversable space, and major elevation changes.
2. Establish routes, negative space, and landmark sightlines.
3. Place major structures and landmarks.
4. Place gameplay obstacles, cover, route markers, and boundaries.
5. Add secondary dressing only after the larger composition reads correctly.
6. Use deliberate variation without accidental intersections, repeated rows, identical rotations, implausible clearances, or filler scattering.
7. Save a valid Godot 4 text scene.

When validation finds a problem, **do not default to deleting objects**. First reposition, redistribute, rotate, rescale, or—when repetition count itself is the issue—replace instances with suitable alternatives. Deletion is a last resort for an asset with no remaining design purpose and must be reported. Do not reduce prop count merely to make one metric pass; prior Mapwright runs showed that deletion can leave density and spatial balance unchanged or worse.

## 4. Run the complete validation loop

Run all six validators in the order below, then run visual capture. This order moves from cheap/global inventory checks to geometry-dependent checks: repetition establishes usage, spacing and density establish layout, landmark establishes candidates, navigation consumes measured footprints, and sightline consumes both landmark candidates and navigable viewpoints.

Use consistent thresholds across correction cycles unless the design requirement justifies a documented change.

```bash
python "${CLAUDE_SKILL_DIR}/tools/repetition_detector.py" "<scene.tscn>" --threshold 5
python "${CLAUDE_SKILL_DIR}/tools/spacing_analyzer.py" "<scene.tscn>" --threshold 1.5
python "${CLAUDE_SKILL_DIR}/tools/density_analyzer.py" "<scene.tscn>" --cell-size 3 --imbalance-threshold 55
python "${CLAUDE_SKILL_DIR}/tools/landmark_analyzer.py" "<scene.tscn>" --candidate-ratio 1.15 --hierarchy-factor 1.5
python "${CLAUDE_SKILL_DIR}/tools/navigation_analyzer.py" "<scene.tscn>" --minimum-path-width 1 --cell-size 0.25 --minimum-region-area 1
python "${CLAUDE_SKILL_DIR}/tools/sightline_analyzer.py" "<scene.tscn>" --eye-height 1.6 --target-height-ratio 0.6 --test-point-count 5
```

### 4A. Interpret numerical evidence

1. **Repetition:** Review flagged counts and percentages. Decide whether repetition is structural or unjustified. Prefer redistribution or appropriate asset substitution over deletion.
2. **Spacing:** Inspect every `TOO_CLOSE` pair against asset size and visual evidence. Move one or both objects while preserving zone purpose.
3. **Density:** Compare the heatmap, empty-cell ratio, variance, and imbalance score with the zone plan. Redistribute props between sparse and crowded cells; do not assume reducing the total count improves distribution.
4. **Landmark:** Review candidates, competing candidates, and missing hierarchy against the intended landmark plan.
5. **Navigation:** Treat confirmed disconnected significant regions as actionable unless intentionally inaccessible. Verify routes and prop approaches.
6. **Sightline:** Review each landmark's clear-ray count and blockers, especially landmarks never visible from any test point.

Landmark and sightline warnings are **review notes, not unconditional correction commands**. These validators measure approximate size and AABB visibility, not semantic importance, central placement, uniqueness, color, lighting, or silhouette. A well may be the intended primary landmark even when trees score larger. Change the scene only when the warning conflicts with the zone plan and capture evidence; otherwise document the intentional exception.

A navigation `NARROW / GRID-SENSITIVE BOTTLENECK` warning is also **not proof of disconnection**. Rerun with a finer grid, inspect the ASCII map and capture, and manually evaluate the relevant passage. Treat `DISCONNECTED NAVIGABLE REGIONS` as stronger evidence. Never claim a route is blocked solely from the grid-sensitive warning.

### 4B. Capture and inspect visually

Do not use Godot `--headless` for 3D capture because it selects a dummy renderer. Use a real rendering driver with an off-screen host window, or Xvfb on display-less Linux.

Windows/OpenGL Compatibility:

```powershell
godot --path "<godot-project>" `
  --display-driver windows `
  --rendering-method gl_compatibility `
  --rendering-driver opengl3 `
  --audio-driver Dummy `
  --resolution 1x1 `
  --position=-10000,-10000 `
  --script "${CLAUDE_SKILL_DIR}/godot/capture.gd" -- `
  "<scene.tscn>" "<capture-output>/iteration-N"
```

Linux CI:

```bash
xvfb-run -a -s "-screen 0 1280x1024x24" \
  godot --path "<godot-project>" \
  --display-driver x11 \
  --rendering-method gl_compatibility \
  --rendering-driver opengl3 \
  --audio-driver Dummy \
  --script "${CLAUDE_SKILL_DIR}/godot/capture.gd" -- \
  "<scene.tscn>" "<capture-output>/iteration-N"
```

Confirm that `top_down.png`, `iso_ne.png`, and `iso_sw.png` exist and are non-blank, then open all three. Check:

1. Intentional versus accidental empty regions.
2. Over-clustering and one-sided visual mass.
3. Intersections, ground embedding, and tight clearances.
4. Rows, grids, equal gaps, repeated rotations, and silhouettes.
5. Landmark hierarchy and visibility from intended approaches.
6. Entry, exit, paths, optional areas, and negative-space readability.
7. Occlusion or stacking visible only from one angle.

Record each issue with its zone and the view that exposes it. Do not use unsupported judgments such as “looks off.”

### 4C. Correct and repeat

Combine all six reports with the three captures and the zone plan.

1. Classify each finding as actionable, intentional/justified, or review-only.
2. Fix actionable findings with repositioning and redistribution first.
3. After any scene change, rerun **all six validators and all three captures**. Never validate only the metric being targeted; a spacing fix can create density, navigation, or sightline regressions.
4. Allow at most **three correction passes** after initial validation: no more than four complete validation cycles.
5. If material issues remain after pass three, stop, preserve the best verified scene, and report the unresolved trade-offs instead of looping or deleting content to force a pass.

## 5. Report completion

Provide:

1. Zone and placement summary.
2. Before/after table for all six validators using the same thresholds.
3. Final capture paths and view-by-view assessment.
4. Correction-pass count and a concise list of moved, replaced, added, or deleted objects. Explicitly state when nothing was deleted.
5. Intentional validator exceptions and environment/rendering limitations.

Never claim visual validation unless all three PNGs from the final cycle were generated, opened, and inspected.
