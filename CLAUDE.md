# STEP → USD converter

A desktop tool that converts STEP to USD for Blender, preserving assembly
hierarchy, instancing and **watertight solids**, with a live 3D preview for
judging tessellation quality by eye. Built because Fusion 360 exports USD but
gives no control over tessellation — and unwelds meshes at material boundaries
and sharp edges, which is disqualifying.

## Run it

```bash
run_gui.bat                              # double-click; no console window
.venv/Scripts/python.exe run_gui.py      # same thing, with a console
.venv/Scripts/python.exe run_gui.py <file.step>       # open a file straight away
.venv/Scripts/python.exe -m stepusd.cli <file.step>   # headless convert
.venv/Scripts/python.exe -m stepusd.winassoc register # Explorer "Open with"
.venv/Scripts/python.exe -m stepusd.shortcuts create    # Start menu + desktop
```

`run_gui.py` self-heals: launched on the wrong interpreter it re-execs on the
venv one, detached. **Bare `python` is the Microsoft Store alias and fails** —
always use `.venv/Scripts/python.exe`.

Environment: `.venv` with **Python 3.12** (3.14 is too new for the OCCT
bindings), `cadquery-ocp` 7.9.3 (`OCP.*`), `usd-core` 26.8, VTK 9.6, PySide6
6.11, scipy, numpy.

Blender (portable, not on PATH):
`C:\no install prog\blender\stable\blender-{4.5.14-lts…,5.1.2…,5.2.2-lts…}\blender.exe`

## The one rule

**A closed solid must never export as a non-watertight mesh.** This outranks
every other goal. Two features have been backed out for violating it. Any change
to `tessellate.py` must be re-verified with a deflection *sweep* on both real
models — failure modes vary with deflection, so a single setting proves nothing.

```bash
# the regression that matters
.venv/Scripts/python.exe - <<'PY'
import glob, os
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from OCP.TopAbs import TopAbs_ShapeEnum
ds=[x for x in glob.glob(".cache/*") if os.path.isdir(x)
    and os.path.exists(os.path.join(x,"scene.json")) and "handle" not in x][0]
for label, cache in (("valve", ds), ("handle", ".cache/handle")):
    doc = load_cache(cache)
    for defl in (0.6, 0.4, 0.2, 0.08):
        bad=opens=0
        for p in doc.protos.values():
            for b in p.bodies:
                m=tessellate(b, MeshParams(deflection=defl, angle_deg=20))
                if not m.watertight:
                    if b.ShapeType()==TopAbs_ShapeEnum.TopAbs_SOLID: bad+=1
                    else: opens+=1
        print(f"{label} defl={defl}: SOLID LEAKS={bad} open={opens}")
PY
```
Expected: **0 solid leaks everywhere**; valve reports 3 open shells (genuine,
they were never closed solids); handle reports 0.

**Sweep BOTH deflection AND angle.** A lattice bug once passed a deflection
sweep at angle 20 and failed at angle 18. Use pairs like
(0.6,20) (0.4,25) (0.25,18) (0.15,20) (0.08,12).

## The second rule: check CHORDS, not vertices

A vertex test proves nothing. Every vertex is on the surface by construction, so
"all vertices within tolerance" is always true and always meaningless. The error
that a user actually sees as a valley or a crease lives in the EDGE: the chord
between two vertices cuts through the solid, and a long edge across a curved
direction can sag far past the requested deviation while both endpoints are
perfect.

This was missed for a long time precisely because a vertex check was used to
"prove" the mesh was fine. Benn caught it from screenshots and was right.

```bash
.venv/Scripts/python.exe spikes/sag_metric.py 0.15 28 0.15
```

Reports, per surface type, how many polygon edges have their midpoint further
from the true surface than the deflection asked for. **Run it after any change
to tessellate.py**, alongside the watertight sweep. Expected now: 0.0% over on
cone, torus and sphere for both models, worst sag ~1.0x the deflection.

What it caught: `_flip_toward_axis` decides purely in UV, and a diagonal that
looks better there can be a chord straight across the curvature. With flipping
unguarded, **21.5% of sphere edges and 3.1% of torus edges were out of
tolerance, worst case 2.74 mm against 0.15 asked (18x)** -- while plain OCCT was
at 0.0%. It now takes `sa` and `max_sag` and refuses any flip whose new edge
bows too far; the surface test runs LAST, after the cheap UV rejects.
`_grid_wins` and `_grid_trim` carry the same gate on their candidates.

Known remaining, and they are OCCT's own -- present with all our paths disabled:
9 handle cylinder edges at 21.6 mm and 156 handle torus edges up to 8.45 mm.
(Valve at 0.15/28 now: cone/torus/sphere 0.0% over, worst sag 1.0x; cylinder
41 edges of 85,817 over, worst 3.0x -- the one category on the valve that is not
at 1.0x, and not yet explained.)
Edge-level refinement (`edgealign.sag_params`, which bisects an edge's polyline
while a chord bows too far) did NOT fix them, so they are surface chords inside
OCCT's triangulation rather than edge chords. Unexplained; low count.

## Layout

```
stepusd/
  reader.py      STEP → XDE → Proto/Node tree. Names, colours, mojibake repair.
  tessellate.py  the mesher. Welding, ruled grids, quads, repair. THE core file.
  scene.py       flatten() the Node tree into placed Occurrences
  writer.py      USD out: Xform hierarchy, prototypes+references, GeomSubsets
  loadproc.py    STEP parse in a SUBPROCESS → shapes.brep + scene.json cache
  nurbsfix.py    NURBS re-mesh of a defective body, in a CHILD process
  parallel.py    multi-core tessellation (process pool)
  cli.py         headless conversion
  gui/
    main.py      MainWindow: sliders, outliner, colour modes, quality, editing
    viewport.py  VTK view; picking, selection, framing, file drop
    treeedit.py  AssemblyTree widget + reparent/create/delete component
    presets.py   user-editable quality presets (JSON beside the project)
    palette.py   colour schemes
    worker.py    ProcessLoader + TessellateWorker (serial and parallel paths)
spikes/          test scripts and fixtures (see "Testing")
```

## Non-obvious things that will bite you

### OCCT / tessellation
- `BRepMesh` **ignores a request for a coarser mesh** unless you set
  `AllowQualityDecrease=True` (or `BRepTools.Clean_s` first). The deflection
  slider looks dead when dragged toward coarse otherwise.
- `EnableControlSurfaceDeflectionAllSurfaces=True` is **required**: by default
  tori (i.e. every fillet) overshoot the requested deflection — measured
  0.497 mm against a requested 0.2 mm, seen as huge triangles down a fillet.
- Welding is **restricted**, and this matters: positional welding is scoped to
  ONE face (that closes seams and poles); across faces it is **topological**,
  via `BRep_Tool.PolygonOnTriangulation` node correspondence. Global positional
  welding fuses unrelated faces on sub-deflection features and produces
  non-manifold edges.
- Degenerate edges (poles/apexes) have ONE adjacent face and need their nodes
  unioned explicitly.
- Zero-volume **flaps** (two triangles, same 3 vertices, opposite winding) come
  from slivers thinner than the deflection; both members must be dropped.
- OCCT sometimes **under-meshes a sliver face** (seen: 13 boundary nodes, 1
  triangle) leaving a hole in a valid closed solid. `_repair_undermeshed`
  re-triangulates by ear clipping; `_fill_holes` is the last-resort cap and runs
  **only for closed solids** — a genuine open shell must stay open.
- Quad pairing must run **after** the global weld, on final indices. Before it,
  welding can merge two corners of a formed quad into a bowtie.
- OCP binding quirks: XDE queries are static (`IsAssembly_s`, `GetComponents_s`);
  `XCAFDoc_ColorTool.GetColor` takes a **shape**, not a label; colour constants
  live on `Quantity_NameOfColor`; **`gp_Trsf` has no copy constructor** — use
  `a.Multiplied(b)`.
- `BRepGProp.VolumeProperties_s(shape, props)` with default args is inaccurate
  (was off 20% once). Pass `(shape, props, 1e-6, True)` before concluding a mesh
  is wrong.

### Fillets and the ruled grid

`_ruled_rows` rebuilds a banded face (cylinder, cone, **torus**, sphere) as a
grid whose lines follow the iso-lines. Torus matters most: a fillet IS a torus,
and until 2026-08-29 the grid and the lattice were gated to cylinder/cone only,
so every fillet kept OCCT's unstructured Delaunay -- the "anarchic fillet".

Two things it gets wrong if you touch them:

- **The ring tolerance is not float noise.** Boundary node v comes from the
  edge's curve-on-surface and is only good to ~1e-4: one fillet ring measured
  4.7124 AND 4.7125 on a single circle. At the old `rel_tol=1e-6` that read as
  "the boundary wanders" and EVERY ring-shaped fillet was rejected. But the
  noise is absolute, not relative, so it cannot be scaled by `dv` alone --
  one cylinder here has `dv = 12.09`, where a relative 1e-3 is 1.2e-2, loose
  enough to pull genuinely intermediate nodes into a ring; sorting those by u
  scrambles the boundary order and tears the face open. Hence
  `tv = min(dv * rel_tol, abs_tol)`. Note the absolute cap alone did NOT fix
  the two handle bodies -- their leak has another cause, still unfound, and is
  currently absorbed by the fallback cascade.
- **A trimmed band drives its rows off the END EDGES** (`_side_rows`), not off
  `nrows`. Those edges already carry nodes at intermediate v, so a row at one of
  those levels lands on vertices the neighbour already shares: no T-junction.
  Inventing row levels on a partial band instead is what the old
  `if nrows > 1 and not closed_u: nrows = 1` guard was protecting against.

`tessellate()` now cascades on failure: full -> no lattice -> no lattice and no
grid. Lattice is dropped first because the grid is what gives fillets their
parallel lines. `Mesh.grid_fallbacks` counts the second step.

### The "sweet spot" trap -- why quality used to depend on the angle slider

Benn found that valve body 138's partial torus fillet was excellent at a max
normal deviation of 16-20 degrees and "an anarchic mess" outside it. That was
never about geometry: it was `_ruled_rows` silently DECLINING and the face
falling back to OCCT's unstructured mesh. Face 40, deflection 0.2:

    angle   path     polys  quads  iso-aligned
    16-20   ruled      964    692   97.9%
    22-30   OCCT      3138..1812   0   43-52%

The reason was `_side_rows` requiring the band's two END edges to have the same
node LEVELS. OCCT discretises them independently, so they agree only by luck --
at 20 degrees they matched exactly, at 25 and 30 both ends still had the same
COUNT but were offset in phase (0.1142 vs 0.212). Requiring exact agreement threw
the whole face away.

Fix: when the counts match, pair by index and let the row DRIFT from the left
end's level to the right end's (a per-point v in the row, see the `*rest`
unpacking in the consumer). Paired rows can never cross -- both level lists are
sorted, so the gap between consecutive rows is
(lv[k+1]-lv[k])(1-t) + (rv[k+1]-rv[k])t, positive for every t -- so `max_slant`
is a QUALITY bound, not a safety one. Now every angle from 12 to 35 takes the
ruled path at 92.6-98.6% iso-aligned.

**The general lesson, and it applies to every guard in this file:** a structured
path that declines is not neutral, it is a cliff. The result drops from ~97% to
~45% aligned with no warning, and the user experiences it as "the tool breaks at
certain settings". When adding a guard, prefer degrading the result to rejecting
the face, and count the rejections -- `RULED_STATS`, `GRID_STATS`,
`LATTICE_STATS` all exist for this. Clear one and read it after a run.

### "Follow iso-lines on trimmed faces" is OFF by default

`lattice` and `grid_trim` both default to False, the GUI checkbox is unchecked,
and the shipped presets carry `iso: false`. Benn's verdict after using it: worse
in most places. The measurement agrees that it is not worth it -- at 0.2/25 on
both models, on vs off moves the iso-aligned percentage by **under 1% either
way**, and cylinders not at all:

    valve  iso ON   cyl 80.9%  cone 72.9%  torus 69.0%   325k faces
    valve  iso OFF  cyl 80.9%  cone 73.5%  torus 67.7%   334k faces

They only ever run on faces `_ruled_rows` declined, and their Delaunay rim is
visibly messier than what OCCT would have left -- which an aggregate percentage
hides but the eye does not. Turning them off also cut serial tessellation from
~50-68s to 8-25s on the valve. Left in place and switchable; make them earn
their keep before re-enabling. Everything good in the current output comes from
`_ruled_rows`, not from these.

### The fallback cascade: what it retries on, and what gates it

Two bugs found here, both of the same shape -- the retry was too narrowly gated:

- `watertight` is `open_edges == 0 and winding_errors == 0`; it does NOT count
  non-manifold edges. A structured rebuild can leave a solid sealed but with an
  edge shared by four triangles, and the cascade returned it happily. It now
  retries whenever `nonmanifold_edges` is non-zero too, and accepts a fallback
  only if that comes back with neither holes nor non-manifold edges. If nothing
  is clean it keeps the ORIGINAL: a fallback that opened a hole would be worse.
- The retry must NOT be gated on `m.structured_faces`. `params.structured` also
  switches on the diagonal flip pass (`_flip_toward_axis`), which rewires
  interior edges on every banded face even when no face took the ruled grid.
  Valve body 64 at 0.15/20 came back with `structured_faces=0`,
  `winding_errors=2` and one non-manifold edge, and was clean with structured
  off -- but no retry was ever queued for it.

With both fixed the valve is nonman=0 at every setting swept. The handle's
2-3 remaining are the known `飾り枠右/左_DECO_B` defect, where the plain path is
non-manifold too, so no fallback can help.

### Edge alignment: adding vertices to shared edges (`edgealign.py`)

`MeshParams.align_edges`, ON by default, GUI "Align cuts to boundary vertices".

A rung across a band only looks straight if the boundary ring HAS a vertex where
the rung lands. OCCT discretises every edge independently, so it usually does
not: of 2428 rung endpoints on valve body 138, only 25% had one. Snapping
columns onto nearby ring nodes lifted that to 66%, but snapping can only ever
fix ONE side of a band -- one set of columns cannot align to two independently
sampled rings. The rest need a vertex INSERTED.

Benn's framing and it is the right one: do not move vertices, ADD them. A vertex
placed on the edge's own curve is geometrically FREE -- the boundary polyline
gets closer to the true curve, never further, so deviation only improves.

Three things make it safe, and all three are load-bearing:

1. **The plan is per EDGE, not per face** (`edgealign.plan`), computed once per
   body before any face is meshed. Every face using that edge then builds the
   same boundary from the same parameter list.
2. **The 3D point comes from the EDGE's curve**, never from either adjacent
   surface. Both faces evaluate the same curve at the same parameter and get
   bit-identical coordinates, so the two copies weld. Using each surface's own
   evaluation would leave them ~1e-4 mm apart -- the model's own tolerance --
   and that is a hole.
3. **Every face on the edge must take the vertices.** A vertex added on one side
   only is a T-junction, which reads as a hole. Band faces get them through
   `_ruled_rows(aug=...)`; every other face has the triangle carrying that
   segment re-triangulated by `_split_boundary_triangles` (whole rim rebuilt and
   ear-clipped, since one triangle can receive insertions on several edges).
   The cross-face weld then pairs the AUGMENTED chains, not the raw
   `PolygonOnTriangulation` runs -- `aug_chains` in `_tessellate_once`. Miss that
   and the added vertices never weld.

`_ruled_rows` takes an optional `aug={"chains","uv","pnt"}`. Note that `us`/`vs`
inside it are now dicts keyed by REF, not lists indexed by `i-1`: a ref is
usually a triangulation node index but can also be an added vertex, which has no
slot in the triangulation.

Measured at 0.2/25, both models, HOLES=0 everywhere:

    valve   align OFF  cyl 82.9%  cone 77.7%  torus 68.1%   328k faces,  85k quads
    valve   align ON   cyl 89.2%  cone 79.1%  torus 72.4%   447k faces, 169k quads
    handle  align OFF  cyl 83.9%  cone 70.0%  torus 79.1%   230k faces,  54k quads
    handle  align ON   cyl 83.8%  cone 72.9%  torus 80.3%   234k faces,  57k quads

Face 40 (the fillet Benn photographed) is now 96.4-99.5% iso-aligned across
angles 12-35, with 82-92% of its polygons quads. The cost is polygons: roughly
+35% on the valve, +2% on the handle. Serial time about doubles; the process
pool absorbs it (164 bodies, 16 workers, 5.3s).

`align_edges=False` is the FIRST step of the fallback cascade -- it is the
newest thing here and the cheapest to give up, since losing it costs alignment
but not structure.

### The axis-aligned grid is a per-FACE choice, not a per-part one

`MeshParams.auto_grid`, ON by default, GUI "decide per face".

Forcing the ruled grid on every banded face makes some faces WORSE. Measured on
valve body 138 at 0.15/33, iso-aligned edges, grid ON vs OFF:

    face 56-59  cyl   dv 9.00    66.0%  143 polys  vs  100.0%   47 polys
    face 29-30  cyl   dv 6.00    66.7%  156        vs  100.0%   52
    face 11-14  torus dv 1.32    36.5%   73        vs   61.6%  289
    face 2      cone  dv 0.71    96.4%  513        vs   36.1%  169
    face 24     cyl   dv 1.00    96.0%  516        vs   38.4%  172
    face 40     torus dv 1.57    97.8% 1023        vs   53.9% 1277

A tall plain cylinder is already perfect in OCCT's hands -- 100% aligned with a
third of the polygons -- and the grid, forced to add interior rows because the
rings disagree, only introduces diagonals. Short bands and fillets are the
opposite. Do NOT try to derive a rule from dv: it is millimetres on a cylinder
and radians on a torus, so the numbers above are not comparable across rows.

`_grid_wins` instead builds BOTH candidates in parameter space -- the ladder the
grid would emit, and OCCT's triangles after the same diagonal flip pass -- scores
each with `_iso_fraction`, and keeps the better. Nothing is materialised while
probing, so a rejected candidate leaves no orphan vertices. UV is scaled to arc
length first, so the angles are real. Validated face by face: 56, 29 -> plain;
2, 24, 40, 54 -> grid; 11, 13 decline the grid on their own. `margin` tilts ties
toward the grid (it also yields quads); sweeping it 0.02 / 0.0 / -0.1 moves the
model-wide numbers by under 1.5%, so it is not a sensitive knob.
`AUTO_GRID_STATS` counts the decisions.

### What a quality change actually re-meshes: the three modes

Benn's rules, and they are now asserted by `spikes/tuning_scope.py`:

    isolated                  -> only the isolated parts; the rest are ignored
    not isolated, a selection -> only the SELECTED parts with NO set quality
    not isolated, no selection-> every part with no set quality

Measured on the valve (164 bodies), counting real `tessellate()` calls:

    no isolation, no selection      164 meshed
    5 selected, none isolated         5 meshed
    same 5, one has a set quality     4 meshed
    3 isolated, nothing selected      3 meshed
    global change, 10 on a preset   154 meshed

**The bug that made isolating pointless: `_base` was never initialised.**
`_params_for` falls back to `self._base or self._params()`, and `_params()`
reads the LIVE panel -- so while `_base` stayed None, every body with no
override followed the sliders. Tuning 3 isolated bodies re-meshed all 164, which
is exactly what Benn reported. `_base` is now frozen in `_loaded`, right after
`_autoscale_defaults`, so an un-overridden body has a STABLE signature and hits
the mesh cache. Nothing else was needed to make hidden parts "ignored" -- the
cache already does that once the signature stops moving.

**But freezing it broke the programmatic path, and the smoke test caught it.**
`LogSlider.set_value` deliberately does not emit `changed` (it is how the panel
is filled from a selection), so `set_value` + `_retessellate()` never reached
`_param_changed` and meshed at the OLD values: gui_smoke reported 750 triangles
at 0.01 mm, at 1.0 mm and everywhere between. `_retessellate` therefore refreshes
`_base` itself when `_tuning_targets()` is empty -- i.e. exactly when the panel
IS the global default.

Two smaller consequences:

- the selection branch of `_tuning_targets` now SKIPS bodies that carry a
  quality of their own. A part with a preset is not disturbed by editing the
  panel with it selected; clear its quality first if that is what you want.
- `_param_changed` still pops `_quality` for its targets, but that can now only
  bite an ISOLATED body, since the selection branch already dropped those. While
  isolated the panel is meant to drive everything on screen, and clearing the
  preset keeps the outliner honest -- `_params_for` reads `_custom` first, so
  leaving the preset name showing would misreport what the body is meshed at.

### Isolate and tune

Isolating a selection now points the quality panel at ONLY those bodies. On
entering, `_frozen` snapshots the current params and every other body is pinned
to it, so their mesh-cache signatures still match and the worker re-meshes
nothing but the parts being tuned -- the "only re-tessellate the isolated parts"
behaviour falls out of the existing cache rather than needing new plumbing.
Settled values are kept in `_custom` (per body) and survive un-isolating; the
outliner shows them as "custom". Assigning a preset supersedes a custom setting;
"use global quality" clears both.

### The mesh-cache signature must cover EVERY MeshParams field

`_signature` in `gui/worker.py` is now derived from `dataclasses.fields`, not a
hand-written list. The hand-written one went stale three times -- `grid_trim`,
`align_edges` and `auto_grid` were each missing -- and the symptom is not a
subtle error: the corresponding GUI checkbox appears completely DEAD, because
every body hits a cache entry computed with the old setting and nothing on
screen changes. Benn reported exactly that for "decide per face". `parallel` is
excluded on purpose (it selects TBB threading and cannot change geometry, and
the parallel path deliberately flips it).

### Spheres: mostly TRIMMED caps, still unsolved

Benn's verdict is that spheres are unusable, and he is right. State of play on
the valve: **111 sphere faces, 55 with a degenerate pole edge, 15 with a seam.**
`_ruled_rows` accepts only 24; **83 decline as "boundary wanders"** because they
are trimmed caps whose boundary does not lie on two latitude lines, and 4 have
both rings degenerate.

Pole caps ARE now accepted: a ring whose nodes are one point in space collapses
to a single entry, and `_ladder` fans to it on its own (with a row of one its
walk degenerates into exactly the triangle fan a pole wants). That fixes clean
caps but changed nothing on this model, because almost none of its caps are
clean.

**Do not trust `iso_metric.py` on spheres.** It scores them 67% with the grid
against 36% without, while Benn sees the opposite -- because a pole fan is made
of meridians, which the metric counts as perfectly iso-aligned. The metric is
blind to the thing that actually looks wrong (chaotic ring spacing and slivers
at the pole). Any sphere work needs a different measure, or eyes.

Fixing the trimmed caps needs a working trimmed-patch mesher, i.e. `_grid_trim`
with pole and seam handling -- which is the path Benn turned off for being worse.
That is the honest prerequisite; patching `_ruled_rows` further will not reach it.

### Quality is per SELECTION, not global

The quality panel edits the SELECTED bodies (`_custom`, per body). Bodies with
no override sit on `_base`, so their cache signatures do not move and the worker
re-meshes only what changed -- "re-tessellate just these parts" falls out of the
cache rather than needing new plumbing. Selecting a tuned body loads its values
back into the panel (`_load_panel_from_selection` / `_show_params`, guarded by
`_loading_panel` so filling the widgets does not count as an edit). With nothing
selected the panel moves `_base`, i.e. the global default. "Apply to EVERY part"
warns, then resets `_base` and clears both `_custom` and `_quality`.

Worker count is now 80% of hardware threads (25 of 32 on Benn's box), not
`min(16, cpu-1)`.

### Quads on CURVED faces, not just planes

`_pair_planar_quads` used to consider only triangles whose B-rep face is a
PLANE. Benn's case: a long concave fillet swept along a changing direction is a
B-spline surface, so it gets neither the ruled grid (not a banded surface) nor
the old planes-only pairing, and came out as an obvious ladder of triangles that
should plainly have been quads. It now pairs on any face, capped by a dihedral
limit taken from `params.angle_deg` -- within one B-rep face there are no sharp
edges by definition, so two adjacent triangles there are already as flat as the
mesh tolerance allows. Pairs must still share a B-rep face; never merge across
one, that is where the real sharp edges are.

Two things this must keep: the merged quad keeps its CORNER normals (collapsing
to one normal is only right on a plane; on a fillet it flat-shades the quad and
undoes the faceVarying normals), and every edge of the quad must still be used
exactly twice in the whole mesh.

Effect, both models, HOLES=0 throughout: quads went from roughly 20-40% of
polygons to **49-71%**, and face counts fell by a quarter to a third because two
triangles become one polygon.

### Spheres always go through _grid_trim

OCCT fans a spherical cap from its pole. On the valve that produces slivers up
to **1230:1**, which is what Benn saw as "deep peaks and valleys" -- the vertices
are on the sphere to 7e-4 mm, so it is not the geometry at all, it is shading
across needle triangles. Sphere faces therefore always attempt `_grid_trim`
regardless of the iso-lines checkbox: worst aspect drops to **111** with 10%
FEWER polygons. The checkbox still governs every other surface, where Benn
measured that path as worse.

Note `_grid_trim` used to be gated BEHIND `params.lattice`, so switching it on
by itself did nothing; the two are independent now.

Still not solved: of 111 sphere faces on the valve, `_ruled_rows` accepts 24 and
**83 decline as "boundary wanders"** -- they are trimmed caps whose boundary does
not lie on two latitude lines (one measured face had 13 boundary nodes spread
over 9 latitude levels, with 168 interior nodes). Pole caps ARE now accepted (a
ring whose nodes are one point in space collapses to a single entry and `_ladder`
fans to it), but almost none of this model's caps are clean. Benn wants UV-sphere
or icosphere topology; an icosphere cannot be used on a trimmed cap because the
boundary must be preserved exactly.

### Progress reporting and pool warm-up

Two separate causes of "the bar sits at 0% for several seconds":

- results were collected in SUBMISSION order (`for ... fut.result()`), so one
  slow body blocked every result queued behind it. Now `as_completed`, reporting
  every 4 bodies. The work took the same time; only the reporting was wrong.
- `ProcessPoolExecutor` spawns workers LAZILY, one per submit, so a cold pool
  paid every process spawn plus every worker's BRep re-read on the first
  tessellation -- measured **1.7s of a 6.4s** first run at 25 workers.
  `_warm_pool` now fires one trivial task per worker right after load.

### Fillets are often B-SPLINES, and three assumptions hid that

Benn's body 60 is FOUR B-spline faces -- the fillets round a boss, exported by
SolidWorks as NURBS patches rather than tori. It came out at 5184 polygons and
ZERO quads while Fusion drew clean stripes. Three separate wrong assumptions,
all now fixed, and each one is worth remembering because the same shape of
mistake could be anywhere in this file:

1. **`BANDED_SURFACES` was analytic-only.** The structured paths never even
   looked at a NURBS fillet. Everything downstream is already parameter-space
   generic (`_rows_for_curvature`/`_cols_for_curvature` SAMPLE the surface), so
   B-spline, Bezier, extrusion and revolution surfaces are now included.
2. **A band does not always run along u.** Body 60's rings are the CONSTANT-U
   edges (60 and 58 nodes) while v=vmin/vmax hold 2 nodes each -- the corners.
   `_ruled_rows` takes `swap=` and the caller tries both readings; `_grid_wins`,
   `_max_chord_sag` and the planner all take the flag too.
3. **`closed_u = du >= 2*pi` assumes the parameter IS an angle.** True for an
   analytic cylinder, false for a B-spline parameterised 0..1 -- so a fillet
   wrapping the whole way round read as a partial band and got clamped to one
   span across its width. Now `_band_closed`, which asks the geometry.

Also: row count is no longer taken from the curvature estimate alone. That
estimate assumes splitting the parameter range evenly splits the ARC evenly,
which a B-spline does not, so `_ruled_rows` now grows `nrows` until the measured
CELL DIAGONAL sag is inside the deflection. The diagonal, not the row span --
it is the longest edge the ladder emits and it bows in both directions at once.

**Body 60 is still not fixed.** The structured candidate is now built (5 rows,
71.3% iso-aligned against OCCT's 29.9%) but its chord sag is 0.30 mm against
0.15 asked, so the sag gate rejects it and OCCT's mesh stands. That rejection is
CORRECT -- shipping it would trade a visible crease for tidy edges. The open
question is why the sag stays high as rows increase, which points at the
u-direction (column) spacing rather than the rows: the columns come from the
ring node counts, and an interior row of the fillet is a different circle from
the two boundary rings. Start there.

### Alignment must not be dumped on a PLANE

Vertices added to a shared edge are only free if the neighbour can spread them
out. A plane is typically two triangles, so 25 vertices along one of its edges
turn one triangle into 25 slivers radiating from the opposite corner. Benn hit
this on body 49 (two planes, two cylinders; the cylinders asked for 47 extra
vertices) and unchecking "align cuts" fixed it, which is the giveaway.

Two things came out of it:

- `edgealign.plan` skips column and row requests on any edge that a PLANE
  touches. Curved neighbours absorb them fine. Measured on the valve at 0.2/35:
  planar polygons 34,308 -> 34,318 (i.e. untouched) and planar slivers
  unchanged, while cylinder iso-alignment still rises 64.9% -> 70.2%.
- a face that does receive boundary vertices is now RE-WOVEN (`_reweave_face`:
  keep OCCT's interior nodes, take the augmented boundary, constrained Delaunay
  the lot) rather than having the one carrying triangle ear-clipped, which fans.

Rejected: adding Steiner points to the re-weave so Delaunay has something to
work with. It does cut the sliver rate (3.5% -> 1.8%) but triples planar polygon
count (34k -> 127k), which is worse for the user than the problem it solves.

### The fallback cascade ranks, it does not demand perfection

`tessellate()` used to accept a fallback only if it came back fully watertight.
That is not the safe choice it looks like: two handle side panels carry winding
errors on EVERY path, so `watertight` was never true for any variant, and a
fallback with ZERO holes was rejected in favour of an original with fifteen. It
now ranks candidates on (holes, non-manifold, winding) and keeps the best, which
restores HOLES=0 on both models.

### Constraint recovery was the mesher's hot spot

`_recover_constraints` falls back to a full scan when its adjacency walk finds
no crossing edge, and that scan was pure-Python over every edge, re-run per
flip. On valve body 157: **53.9 million orientation tests, 36 of the body's 38
seconds** -- and because one slow body holds up the whole process pool, that is
what parked Benn's progress bar near 100%. `_all_crossing` does the same maths
in one numpy pass: body 157 went **28.4s -> 4.1s**, the whole valve 69.9s ->
41.1s. If the mesher ever feels slow again, profile a single slow BODY first;
the distribution is extremely skewed (top 5 bodies were 64% of all the work).

### Spheres: the apex-outward cap (`_sphere_cap`)

Benn's construction, and it is the right one. OCCT fans a cap from the
PARAMETER pole -- a point usually nowhere near the middle of the patch -- so
every triangle is a needle meeting at one vertex. That is the "deep peaks and
valleys" in the shading; the vertices are on the sphere to 7e-4 mm, so it was
never the geometry.

`_sphere_cap` finds the apex from the FACE's own boundary (mean of the boundary
directions), lays concentric rings around it, and gives each ring the number of
points its own circumference deserves -- about `2*pi*sin(theta)*R/step` -- so
segments stay one step long and cells stay near-equilateral. Icosphere-like
grading, no fixed subdivision, and it works on a cap sitting anywhere on the
sphere.

Three things that turned out to matter:

- **The step must not exceed the BOUNDARY's own spacing.** Sized on deflection
  and angle alone, `n_rings` came out 0 for every face on this model: the caps
  are shallow (R = 0.5-9 mm blends), so a single apex fan was all that fit --
  and a shallow cap whose rim carries 40 nodes then gives 40 needles. Matching
  the rim spacing is what actually produces rings.
- **Do not emit rings and then fit a rim.** The first version built ring cells
  explicitly and constrained-Delaunay'd the annulus between the last ring and
  the trim curve; that failed on 75 of 111 faces because these boundaries
  wander. Culling the ring points against the boundary and putting EVERYTHING
  through one constrained Delaunay works better -- the concentric structure
  comes from where the points are, not from emitting cells.
- Points are culled against the boundary POLYLINE, not just its nodes, exactly
  as in `_grid_trim`.

Measured on the valve at 0.15/25: sphere polygons with aspect > 4 fall from
**15.6% to 12.9%**, accepted on 88 of 171 attempts, HOLES=0. Honest state: the
remaining 75 still fail constraint recovery at every ring spacing tried, so they
fall back. That is structural in their projected boundary (the azimuthal
projection of a wandering trim curve), not crowding -- coarsening does not help.
Worst-case aspect is unchanged at 164. `SPHERE_STATS` counts every exit.

### "Align cuts to boundary vertices" is OFF by default

Measured at 0.2/30: cylinder 84.0 -> 85.5% and cone 69.0 -> 74.9% iso-aligned,
for 9% more polygons and **2.4x the tessellation time** (valve 34s -> 81s).
Benn's repeated verdict on real meshes is that it makes things worse more often
than better, and the measured gain does not argue hard against him. Kept and
switchable; `iso` (lattice/grid_trim) is off for the same reason. Note spheres
still route through `_sphere_cap`/`_grid_trim` regardless of that checkbox.

### Transposing (u, v) is a REFLECTION -- the bug that hid everything else

`_ruled_rows(swap=True)` reads a band whose rings are the constant-u edges. It
works, but swapping the two parameters MIRRORS the parameter space, so every
polygon it emits comes out wound backwards. The mesh then has winding errors
rather than holes, `watertight` is False, and the fallback cascade throws the
ENTIRE BODY back to plain OCCT -- including all the faces that had meshed
perfectly well.

Valve body 158: 122 winding errors, and every one of its 9 successfully
structured faces silently discarded. Every body containing a single swapped face
was losing all of its structured work. Fixed by reversing on
`flipped != rows_swapped` at emission; measured immediately after, at 0.4/25:

    valve   cyl 84.0 -> 90.6%   cone 69.0 -> 81.0%   torus 79.6 -> 81.5%
    handle  cyl        87.8%    cone        76.0%    torus        90.1%

**The lesson worth keeping:** a per-face failure is visible and local, but an
orientation bug is neither -- it is laundered through the body-level cascade
into "OCCT's mesh looks fine here". Whenever a body seems to ignore the
structured paths entirely, check `LAST_FACE_PATHS` (it is cleared per
`_tessellate_once`, so an empty dict after `tessellate()` means the LAST pass
was a fallback) and check `winding_errors` on the structured attempt before
concluding the faces were declined.

### Stripes whose two sides carry different node counts

Benn's target: a stripe should have a very similar count down both long sides so
it can be a regular ladder of quads. `_side_rows` used to demand they be
IDENTICAL, which threw away the normal case -- body 158's face 3 is 47 against
45. It now walks both by normalised position, letting the shorter side repeat a
node where it must; the repeat collapses to a triangle at that end (the emitter
drops duplicate corners), which is how one would close it by hand. Declines only
when the two sides differ by more than 2x.

### Not every "sphere" is a cap

`_sphere_cap` projects the patch azimuthally about its apex, and that projection
is only injective within a HEMISPHERE. Body 158's big sphere (R = 71) reaches
134 degrees from its apex and carries a hole -- it is a spherical BAND, not a
cap -- and its projected boundary self-intersects in 24 places, so the Delaunay
could only ever fail. It now declines above 1.45 rad instead of burning four
retries on it.

That face is still unsolved: a full-revolution band (so it has a seam) with an
inner wire. `_ruled_rows` calls it "boundary wanders" because of the hole, and
`_grid_trim` calls it "patch unsound". It is the right target for the untrim
approach, and it is what Benn keeps photographing.

### Seam edges are traversed TWICE by the wire

A seam face carries two UV nodes for every point on the seam (u=0 and u=2*pi)
with identical 3D coordinates, and the wire runs up both sides -- but
`BRep_Tool.PolygonOnTriangulation_s` hands back only ONE node run. A loop built
from that single run doubles back on itself, so the constraint set is wrong and
the patch is rejected. `_seam_partners` pairs the duplicates by position and
`_face_loops` picks whichever run keeps the loop continuous.

The indexed `PolygonOnTriangulation` overload that would give the second run
directly returns its results by reference and is unusable from Python -- hence
pairing by position.

Honest status: this is in and it does not regress anything, but it has NOT yet
converted the faces it was aimed at. `patch unsound` is still 12 of 41 exits on
body 158. Do not assume the seam story is finished.

### Choosing the band's orientation: rank on SAG, never on iso-alignment

`_best_rows` builds the band and, if that reading is no good, tries it
transposed. Two traps, both hit:

- Taking the first orientation that returns rows is not enough. A band read
  across its short direction still "works": body 31's face 2 gives 53 rows of
  two points each -- one span across the whole fillet -- sagging 3.47 mm against
  0.2 asked, while the transposed reading is 7 rows of 366 polygons at 0.164 mm.
- Ranking the two on iso-alignment is WORSE than taking the first. A coarse
  degenerate reading scores beautifully on that metric (few edges, all of them
  axial) and beats the right answer. Measured: valve cylinders 90.6% -> 81.8%.

So the natural reading wins unless it is genuinely UNACCEPTABLE -- it bows
outside the requested deflection. That is the one property that actually
disqualifies a candidate, and it is cheap to test. General lesson: iso-alignment
is a good tiebreak between two candidates of similar density, and a terrible
one between candidates of different density.

### grid_trim stays OPT-IN, and that was re-tested

Tried making `_grid_trim` run on every banded face, self-policing by scoring
itself against OCCT's mesh before returning. Measured worse: valve cylinders
90.6 -> 82.2%, cones 81.0 -> 72.5%, and the handle picked up two extra
non-manifold bodies. Reverted. Benn has now said three times that this path
makes things worse and the measurement has agreed every time; the scoring gate
is kept because it helps when the path IS switched on, and spheres still route
through it because OCCT's pole fan is worse than anything.

### The GUI style lives in `run()`, not in the launcher

`run_gui.py` re-execs on the venv interpreter and then calls
`stepusd.gui.main.run()`, which is where `QApplication` is actually created --
so that is where `app.setStyle("Fusion")` has to go. Putting it in run_gui's
`report()` only styles the CRASH DIALOG, which is exactly the mistake made once
already. Fusion rather than the native Windows style because it is what the
headless smoke test renders, so its screenshots match what Benn sees.

### `_grid_trim`: thin stripes, and the acceptance bar

Two things learned while trying (again) to make this path carry the trimmed
fillets:

- **A stripe narrower than one cell exits as "no interior"** -- every grid point
  gets culled and there is nothing to regularise. That was 37 of 90 faces on
  body 31 and they are precisely the thin fillets. The NARROW direction now gets
  at least 4 divisions regardless of what the curvature estimate says.
- **The acceptance bar has to be strict.** At a 2% margin over OCCT the path
  took too many faces and made the model worse (valve cylinders 90.6 -> 82.2%).
  At +10% it takes 168 of 1864 faces and costs nothing, but the gains are small
  and mixed -- body 31 gets 10% fewer polygons at equal alignment while body
  158's cones drop 78.1 -> 69.0%. So it stays OPT-IN. Spheres still route
  through it unconditionally.

### Min edge length defaults to a real value, not to OCCT's auto

Benn: "adding a manual limit to the shortest edge len instead of letting it auto
improves drastically the quality quite often." Measured at each model's own
defaults, `min_size` auto vs 0.1 mm:

    valve   slivers 26.34% -> 23.42%   worst aspect 282146 -> 163340
    handle  slivers 42.73% -> 40.77%   worst aspect 623375 -> 132365
            polygons 141,893 -> 136,212, and nonman 2 -> 0

Fewer slivers, FEWER polygons, and the handle's two known non-manifold edges go
away. It is set from the deviation (`_nice(defl * 1.6)`) rather than hard-wired
to a fixed number, so a 3 mm screw does not inherit a floor sized for a 300 mm valve
body. Benn's verdict after using it on real parts: "really making a gigantic
difference, and always in a good way", often going to 0.3 by hand. Measured on
the valve at 0.125/30, min_size 0.1 -> 0.2 improves everything at once --
slivers 24.77% -> 23.38%, polygons 239,065 -> 217,452, 14% faster, HOLES=0 --
so the factor is 1.6x the deviation, which lands the valve on 0.2 mm. 0.3 is
marginally better again (22.95%) but 0.35 turns back up, so the useful range
tops out around there. `cli.py`
applies the same rule (`--min-size` defaults to 1.6x the deviation, -1 still
means OCCT auto), so headless output matches the GUI.

Note the watertight sweep at the top of this file builds `MeshParams` directly,
where `min_size` is still -1 -- which is why the handle's three DECO_B bodies
still report there but come out clean at the GUI's default.

### Defaults come from the BIGGEST BODY, and "relative" means per cent

On load the max surface deviation is set from the largest single BODY's biggest
dimension, not the assembly's diagonal: the diagonal is dominated by how far
apart the parts sit, which says nothing about the detail inside them. The rule
is `_nice(biggest / 2500)`, calibrated so Benn's valve -- biggest body 318.2 mm
across -- lands on 0.125 mm; the handle gets 0.15. `_nice` snaps to a number a
person would have typed (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8). Max normal
deviation defaults to 30 degrees.

"Relative to part size" now reads the deviation as a PERCENTAGE of each body's
largest dimension, and the spin box switches its suffix between " mm" and " %".
`_as_mm` converts per body before the mesher ever sees it. OCCT's own `Relative`
flag is deliberately NOT used: it scales to each EDGE's length, which is not
what "relative to part size" means to someone looking at an assembly of big and
small parts.

Beware: `MainWindow` already had a `_body_sizes()` METHOD; the per-body cache
added here is `_body_size_cache`. Shadowing the method with a dict broke both
test suites with "'dict' object is not callable".

### The per-face decision scores SHAPE as well as alignment

`_shape_penalty` is subtracted from `_iso_fraction` in both `_grid_wins` and
`_grid_trim`'s self-check. Iso-alignment alone picks badly: Benn's body 138
concave fillet came out with the iso path on as small aligned quads down the
middle and long slivers top and bottom, which scores WELL on alignment (the
slivers run along the iso-lines) and looks terrible; with the iso path off the
same face is perfect. A candidate full of long slivers is not a better mesh
however neatly the slivers line up.

"Align cuts to boundary vertices" has been REMOVED from the UI and the presets.
It was measured at cylinder 84.0 -> 85.5% and cone 69.0 -> 74.9% for 9% more
polygons and 2.4x the time, and Benn's verdict from real meshes was that it made
things worse more often than better. `MeshParams.align_edges` and edgealign.py
remain, defaulting False, so the work is available if it is ever wanted.

### The apex cut-and-fill (`_sphere_apex_fix`) -- WORKING

Benn's idea. OCCT's fan damage is local, so delete the triangles inside a loop
around the apex, fill the hole with a regular grid, and project the new points
onto the sphere. Only INTERIOR triangles are replaced, so the face's boundary --
and the watertight guarantee -- cannot be affected, and it never has to
reproduce the trim curve. Gated on `params.structured` so the cascade can drop
it. Verify with `spikes/sphere_render.py`.

Two bugs stood between "implemented" and "works", and both were invisible in the
counters:

1. **A face's real boundary is its non-seam, non-degenerate edges ONLY.** A seam
   runs RADIALLY from the pole outward and the degenerate edge IS the pole, so
   counting either as boundary makes a clean cap look like one whose boundary
   reaches its own apex. Body 138's four big spheres measure:

       real boundary   36 nodes at theta 55-77 deg   <- a clean ring
       seam            12 nodes at theta  1-60 deg   <- radial, not a boundary
       degenerate       2 nodes                      <- the pole

   With that wrong, all four declined, and the "measurement" that they were not
   caps at all (theta 3-95 deg) was an artefact of the same mistake. Benn said
   from the screenshots that they were obviously clean caps and he was right.
   The rim is also found in CANONICAL ids -- one per 3D position -- or it never
   closes into a loop across the seam.

2. **The fill's winding must come from the KEPT triangles, not from the rim
   loop's own area sign.** The rim walk picks an arbitrary direction. Getting it
   wrong does not tear anything -- it produces WINDING errors, so the body stays
   sealed but not `watertight`, the cascade throws the WHOLE BODY back to plain
   OCCT, and the fan is on screen again while `SPHERE_STATS` still says "ok=4".
   Measured: 116 winding errors, fix silently discarded. Whatever direction a
   kept triangle traverses a rim edge, the fill must traverse it the other way.

**Check the mesh, not the counter.** The honest test is the VALENCE of the worst
sphere vertex: ~6-9 is a grid, 18+ is a fan. On body 138 it went 18 -> 9.
Model-wide the fix now fires on 61 valve faces and 93 handle faces; what still
declines is the tiny spherical trims on screws and nuts, where the apex is often
trimmed away anyway and following the UV is enough.

### The per-face weighting is a slider, not a constant

`MeshParams.grid_bias` (GUI: "UV vs regularity", default 50%) sets how
`_grid_wins` and `_grid_trim` judge a candidate: 1.0 scores only how closely the
mesh follows the surface's UV grid, 0.0 only how regular the faces are in shape
and size. Benn's call, and he is right that the ideal balance is not universal
-- it depends on the part, so it is exposed rather than baked in.

`_regularity` combines elongation (share of faces past 4:1) with SIZE
consistency (spread of face areas around the median). Alignment alone picks
badly: a fillet meshed as small aligned quads down the middle with long slivers
top and bottom scores well on alignment, because the slivers run along the
iso-lines, and looks terrible.

### The viewport material is deliberately shiny

Strong specular with a tight highlight, matcap-like, and a brighter key light.
The point of the viewport is spotting normal defects, and a crease or a sliver
shows as a kink in a moving highlight long before it shows in flat diffuse
shading.

### The per-face verdict judges all THREE candidates

`_grid_wins` used to compare the ruled grid against plain OCCT, and `_grid_trim`
compared itself against plain -- so the two structured paths were never compared
against EACH OTHER. If the ruled grid won, the iso-line patch was never even
built, and could never be chosen on a face the ruled grid also accepted. Both
scorers now publish what they measured (`LAST_RULED_SCORE`, `LAST_GRID_SCORE`)
and the caller takes the best of the three. Measured on the valve at 0.125/30
with iso on: 19 faces where the iso-line patch beats the ruled grid.

### Stopping a run: Esc, and it must KILL THE POOL

One slow body holds up the whole pool -- the bar sits near the end while
everything else waits on it -- so Esc abandons the run in progress and keeps
whatever already finished. There is no Stop button: Benn never found it, and
decided Esc alone was what he wanted.

**Cancelling the worker thread is not enough, and this was a real bug**
("once I stop a tessellation process with esc, its not possible to tessellate
anymore"). The bodies are meshed in a process pool, and `Future.cancel()` only
drops work the executor has not yet handed to a worker -- it cannot stop one
already running, and by then the executor has fed most of the batch into its
call queue. Measured on the valve at 0.02 mm: of 53 work items, cancelling
removed **3 and left 50 still churning**. The next run queued behind all of
them, so it took 1.6x as long as the same work on an idle pool -- and on a fine
setting that is minutes, which reads exactly like a dead tool. The user pressed
Stop precisely BECAUSE the run was too long, so the cost scaled with the thing
they were trying to escape.

`_discard_pool` therefore terminates the worker processes outright and starts a
fresh pool warming. They are mid-`BRepMesh` in C++, so there is no flag to poll;
killing the process is the only way to get the core back. Each worker holds its
own read-only copy of the BRep and writes nothing outside itself, so losing one
mid-body costs only that body.

**Order matters, and it is counter-intuitive.** Terminate FIRST and do not call
`shutdown()` at all. Calling `shutdown(wait=False, cancel_futures=True)` before
terminating makes the executor's manager thread drain the running tasks first,
which puts the cost straight back on the cancelled run's size -- measured 2.3x,
2.9x and 3.3x baseline after cancelling a 0.05 / 0.02 / 0.008 run, against a
flat 1.7x when the processes are simply killed. (Shutdown-first was tried
because terminate-first can emit a stray `OSError: handle is closed` from the
dying manager thread; dropping the `shutdown()` call entirely is both fast and
quiet.)

`_stop_tessellation` also clears `_pending` AND stops `_debounce`, or a slider
edit made just before Esc fires 180 ms later and starts the run again.

What is left is one constant pool re-spawn, about +2.8s, and it is usually
hidden: the rebuild is queued on the event loop immediately, so it warms while
the user is reading the screen. `spikes/stop_resume.py` asserts the property
that matters -- recovery time must not scale with the cancelled run's cost
(measured spread 1.00x across cancelled runs of 0.05, 0.02 and 0.008).

### GUI layout and controls, and why they are where they are

- The **Result** read-out lives under the tree on the LEFT. Stacked under the
  quality panel it made the right dock taller than the screen, and the Stop
  button that used to live there ended up below the fold -- which is why Benn
  never saw it. The right dock is now a `QScrollArea` with a 300px floor, so the
  window is free to be any size.
- **Esc** is the only way to stop a run; the Stop button has been REMOVED at
  Benn's request ("its useless, esc only is fine"). See "Stopping a run" above
  -- it has to kill the pool, not just the worker thread.
- **T** toggles the wireframe (`_install_shortcuts`).
- Max surface deviation steps by **0.05 mm**, not 1. Values below 0.05 need the
  "allow below 0.05 mm" box: a few thousandths can take minutes and millions of
  polygons before the user notices. `LogSlider.set_min` moves the floor and
  remaps the log scale with it.
- **Up axis** for export (Z / Y). SolidWorks models are often authored Y-up,
  which is why the valve arrives lying on its side in a Z-up scene. `write_usd`
  already took `up_axis`; it just was not exposed.
- **"Decide per face" is NOT indented under the axis-aligned grid** any more: it
  governs both structured paths, so presenting it as a sub-option of one of them
  was wrong.

### While ISOLATED, the panel drives every isolated part

`_tuning_targets`: if anything is hidden (i.e. a selection is isolated), the
quality panel applies to everything still on screen, selected or not -- that is
the point of isolating, and having to also select them was busywork. With
nothing isolated it falls back to the selection, which is where per-part tuning
belongs.

### Flat faces never enter the per-face check

`BANDED_SURFACES` does not contain `GeomAbs_Plane`, so no candidate is ever
built for a plane and none is scored: measured on body 138, 0 of its 14 plane
faces enter the comparison. They take OCCT's triangulation directly, which is
what Benn wants -- a plane is exact at any tolerance, so there is nothing to
choose between.

There are THREE candidates, not four: plain OCCT, the ruled grid, and the
iso-line patch. The grid and iso paths are alternative MESHERS for the same
face, not composable layers, so "both on" is not a distinct mesh.

### "By tessellation quality" colours custom values on a ramp

A body with a PRESET gets that preset's colour; a body with a CUSTOM quality now
gets a blue ramp instead of the neutral grey. Only `deflection` feeds it -- the
angle and the switches say nothing about how fine a part looks.

The ramp is relative to the custom values ACTUALLY IN USE
(`_custom_deflection_range`), not to an absolute scale, or a model tuned between
0.1 and 0.15 would come out all one colour. Deep blue is the finest value
present, light blue the coarsest; a single custom value lands mid-ramp.

The dark end is (0.16, 0.30, 0.62), deliberately well short of black: the
wireframe is drawn in black and has to stay readable on top of it.

Because the ramp rescales to the values in use, changing ONE body's quality can
change the colour of all of them -- `_param_changed` re-runs `_recolor()` when
that colour mode is active.

### Repairing an open shell: FLAT rims only

`fill_planar_holes(mesh)` caps the holes in a shell that was never a closed
solid, reached from the outliner's right-click menu ("Try to fix this part" /
"Try to fix every open shell"). It edits the cached mesh in place, so the repair
lasts until that body is re-tessellated.

**Only a planar rim is filled**, and that restraint is the point: a flat rim
stands for an unambiguous lid, so inventing it is honest. A rim that twists
through space is left alone, because any surface put there would be invented
shape -- and a wrong solid is worse than an honest open shell. A loop counts as
planar when no vertex is further from its best-fit plane (Newell normal) than 2%
of the loop's own extent, so the test scales with the hole.

Winding comes from the RIM, not from the loop's area sign: whatever direction
the existing triangles traverse a boundary edge, the cap must traverse the other
way. Getting that wrong seals the mesh but leaves it inconsistent, which reads
as "not watertight" just as loudly as a hole -- the same trap as the sphere apex
fill.

The cap is a GRID, not a fan. Ear clipping a roughly circular rim produces
exactly the pathology `_sphere_cap` exists to remove -- every triangle meeting at
one vertex -- so it has no business being the repair either. `_plane_fill` lays a
regular grid over the rim's plane at the RIM'S OWN median edge spacing, culls it
against the densified boundary polyline (not just its nodes, same lesson as
`_grid_trim`), and constrained-Delaunays grid to rim. Too small for even one grid
point falls back to a single CENTRE vertex -- an umbrella, which is the right
answer on a near-circular hole -- and to ear clipping only if both fail.

**The cap's normal comes from its FINAL winding, never from the Newell sign.**
That sign follows the loop's traversal direction, so half the caps would shade
inward. The cap is already wound to oppose the rim, and the surrounding faces are
wound outward, so its own geometric normal is by construction the one that
matches its neighbours.

Measured on the valve's three genuine open shells at 0.125/30: 72, 73 and 65
boundary edges -> 0, two flat caps each, all three watertight with 0
non-manifold edges, and closed solids are untouched (verified: 0 of 40 altered).
The grid replaced the fan's single high-valence hub with a worst cap valence of
**8** and a worst triangle aspect of **3.0**. The cap also takes its MATERIAL by
majority vote of the faces around the rim rather than from `face_source[0]`, so a
lid on a multi-material part lands in its neighbours' GeomSubset.

Verified through Blender on the whole valve exported with all three shells
capped (0.6/25): `open_edges_total=0` where it used to report three meshes,
non-manifold 0, custom split normals 164/164, flat-shaded polygons 0.

**Repairing must refresh the STATUS column, which is `_update_body_flags`.**
`_refresh_tree_labels` only rewrites the Quality column, so a capped body kept
announcing "open shell" in the outliner while the mesh, the Result panel and the
status bar all agreed it was closed. `spikes/shellfix_gui.py` now asserts on the
label text the user actually reads, not on the counter the repair returns.

### The outliner's columns must FIT, never scroll sideways

They were fixed widths -- 230 + 80 + a default -- against a dock whose minimum
is 320, so the three columns overran it by construction and parked a horizontal
scroll bar under the tree at every window size. Benn's verdict: horrible, and he
is right; scrolling sideways to read a status column is not a thing anyone
should do.

Now `Status` and `Quality` are `ResizeToContents` (their vocabulary is short and
known: "open shell", "non-manifold", "custom", a preset name) and
`Component / body` is `Stretch`, so it absorbs whatever is left and the three
always sum to exactly the viewport. `setStretchLastSection(False)` is required
or the LAST column takes the slack and column 0 stays fixed. Measured with the
tree fully expanded, both models, dock at 320/360/420/520/700 px: columns sum to
the viewport every time and the bar never appears. The trade-off is that no
column can be dragged any more -- Stretch and ResizeToContents are both
non-interactive -- which is the price of it always fitting.

**Do not "optimise" this with `setResizeContentsPrecision(n)`.** It sizes a
column from the first n rows only, and the valve's open shells are bodies
161-163, so a cap of 64 elides exactly the status worth reading. It buys nothing
either: measured on the handle (255 body rows), three full label rewrites cost
89 ms at precision 64 and 89 ms at -1, because Qt posts one layout request
rather than resizing per `setText`. `spikes/outliner_fit.py` asserts on it by
putting a wide status on the LAST row and checking the column grows (valve:
62 -> 79 px) instead of eliding.

Two things go with it: the indent is 14 px, not Qt's 20 -- the handle is 6 levels
deep, where the default eats 120 px of the name column before a character is
drawn -- and every name carries a tooltip, since `ElideMiddle` is what makes the
fit possible and the full name has to stay reachable on hover.

### The ACTIVE part, and Blender's keys

Benn works in Blender all day, so the interaction copies it rather than
inventing anything:

    H        hide the selection          Alt+H   show everything
    Z        toggle isolate (ghosting)   .       fit the selection (numpad too)
    T        wireframe                   Esc     abandon a run
    Ctrl+Z   undo a loop cut (loop cuts ONLY -- not tree edits or quality)

The **active** part is the last one clicked, as in Blender, and it is drawn in
`ACTIVE_COLOR` -- the same hue as the selection but much lighter, so it reads as
"one of the selected, the one in charge". `_active_key` derives it from Qt's
`currentItem`, which already means exactly that, so there is no second piece of
state to keep in sync.

**But the active row can change without the SELECTION changing** -- ctrl-clicking
a row that is already selected, or walking with the arrow keys -- so
`currentItemChanged` is wired to `_selection_changed` as well. Without it the
lighter highlight simply does not follow, and the outliner and viewport disagree
about which part is the master.

**Picking in the 3D view must set the CURRENT row, not just the selection.**
`_on_pick` and `_select_keys` only ever called `setSelected`, so the current row
never moved: the active part could not be set from the viewport at all, and once
the outliner had set one, nothing in the 3D view could change it. Both now call
`setCurrentItem(..., QItemSelectionModel.NoUpdate)` -- the flag matters, since
the default would collapse the selection to that one row.

The outliner mirrors the viewport: selected rows are drawn in `SELECTION_COLOR`,
the active one in `ACTIVE_COLOR` and bold, everything else plain. Only the NAME
column is repainted -- Status and Quality carry their own meaning and colour --
and it runs behind `_tree_guard`, because `setForeground` emits `itemChanged`
and would otherwise read as the user ticking a visibility box.

### While isolated, the outliner lists ONLY the isolated parts

`_filter_rows` hides every other row, and any component left with nothing
visible under it. It is tied to an explicit `_isolated` flag rather than to the
Isolate button's checked state: that state is only in step when the click came
from the button itself, so any other route (a shortcut, a direct call) filtered
nothing at all.

Deliberately NOT tied to `_hidden` in general -- a body hidden ad hoc with H
stays listed with its box unticked, so it can be brought back. Isolation is the
mode where the rest are meant to be out of the way entirely; "Show all" and
Alt+H both clear the flag.

It earns its keep through "propagate the active part's quality to the
selection": tune one part until it looks right, then push that onto the rest.
Both a preset and a tuned custom value propagate, whichever the active part has.

The number-pad dot is a DIFFERENT key event from the main-row one -- Qt tags it
with `KeypadModifier` and a plain "." shortcut misses it -- so both are bound.

### Clicking through overlapping parts

Clicking repeatedly in the same spot walks DOWN through everything under the
cursor in depth order and then wraps to the front, exactly as Blender does.
`vtkPropPicker` only ever reports the front-most prop, so `_pick_stack` picks
repeatedly, switching each hit to `PickableOff` as it goes -- the order that
falls out IS depth order -- and restores them all in a `finally`. Measured on
the valve: the deepest pixel has 10 bodies stacked, and clicking cycles all 10
and wraps.

The stack is rebuilt whenever the cursor moves more than `CYCLE_SLOP` (4 px), so
a click anywhere else always takes the nearest part. Note `_picker` is built in
the Viewport's constructor, not in `enable_picking`: the depth query is useful
(and testable) before click-picking is wired up.

### Up axis: all six

`write_usd` takes `+Z / -Z / +Y / -Y / +X / -X`. USD's stage metadata only knows
Y-up and Z-up, so the other four are expressed as a rotation on `/Root` that
carries the chosen model axis onto the stage's up; `+Z` and `+Y` keep the
metadata-only path they always had, which is the one verified through Blender.
`spikes/` checks each of the six by transforming the chosen axis through the
root matrix and confirming it lands on the stage up vector.

### The status bar has two slots

The transient line carries the visible-body count; `_say()` writes the LAST
ACTION to a permanent label to its right, so it stays readable instead of being
overwritten by the next count update.

### Threading / processes
- **OCP holds the GIL through the whole STEP read and through BRepMesh.**
  Threads are useless. Loading runs in a subprocess (`loadproc.py`);
  tessellation runs in a process pool (`parallel.py`).
- Exactly **one tessellation worker at a time** — workers mutate the shared
  OCCT shapes and two at once kills the process with no traceback.
- Keep the process pool **warm**; each worker pays ~0.5 s re-reading the BRep.
- Set `MeshParams.parallel=False` inside pool workers or OCCT's TBB threads
  oversubscribe.
- Any script creating a pool needs `if __name__ == "__main__":` — Windows spawn
  re-imports the main module.

### Qt
- **Never store a Python object in QTreeWidgetItem data** when drags are
  enabled: Qt serialises item data into MIME and PySide6 pickles it. A `Node`
  holds a `gp_Trsf` → `TypeError: cannot pickle`. Store an int id; resolve via
  `AssemblyTree.node_resolver`.
- **Never rebuild the tree inside `dropEvent`** — it destroys the items Qt is
  still using, reported as the misleading
  `AttributeError: Slot 'MainWindow::_selection_changed()' not found`.
  Resolve to Nodes, `ev.accept()`, then `QTimer.singleShot(0, ...)`.
- **Picking cannot use VTK observers**: the trackball style calls `GrabFocus()`
  on button-down, so a `LeftButtonReleaseEvent` observer never fires. Picking is
  a Qt `eventFilter` on the interactor widget.
- Body keys are path-based and paths come from names, so renaming/reparenting
  renumbers them. Hide state is therefore keyed on `(id(node), body_index)`.
- Quality is keyed **per body**, not per part: the valve is ONE prototype with
  164 bodies.

### Blender / USD
- **`merge_parent_xform` makes NO difference to our output, and the advice
  here used to say otherwise.** Benn noticed ("absolutely no change") and he is
  right: measured on the valve in Blender 4.5, on and off both give 222
  objects, 6 empties, 216 meshes and identical parenting. The option only
  collapses an Xform wrapping EXACTLY ONE mesh, and our occurrence Xforms carry
  one prim per body. There is nothing to tell the user about it.
- Blender's USD importer gives either collection instances **or** an empty
  hierarchy with duplicated mesh data, never both. We emit plain references
  (not `instanceable`) plus `cad_prototype` tags; `spikes/relink.py` collapses
  duplicates into linked duplicates if wanted.
- **USD prim names may be UTF-8** (23.11+) and Blender reads them; don't use
  `Tf.MakeValidIdentifier`, which is ASCII-only and turns 'ネジキャップ' into
  underscores. Blender **ignores** USD `displayName`.
- Multiple materials on one part stay ONE welded mesh with `GeomSubset` per
  colour. GeomSubset indices are **face** indices — use `m.face_source`.

## Benn's real files

- `8-7 DBXA-80S1A-JIS5K（／1R分岐）.STEP` ("the valve", 32 MB, SolidWorks AP203).
  **Flat** — 1 product, no assembly structure; exported as a
  SHELL_BASED_SURFACE_MODEL so OCCT yields shells (we promote the 161 closed
  ones to solids; 3 are genuinely open). **No colour data at all.**
  164 bodies. Cache: `.cache/<hash>/`.
- `HandleModel_ASSY_sw_005.STEP` ("the handle", 62 MB, SolidWorks AP203).
  Real hierarchy: 149 prototypes, 230 occurrences, 81 saved by instancing,
  6 levels deep, 243 solids. Japanese names. Also **no colour data**.
  Cache: `.cache/handle/`.

Both lack colours because of the SolidWorks export settings — AP214/AP242 and
"solid" rather than "surface" geometry would fix colours *and* give the valve
its hierarchy. Worth re-raising if colour work resumes.

## Units: write METRES, so nothing arrives scaled

Benn: "the main empty (the very top parent) have a scale of 0.001 while all the
other parts and empties are properly at scale 1. I need everything to be at
scale 1, but still respect the real size of the object... because doing so in
blender is a big pain in the ass."

The cause was ours. We wrote millimetre coordinates and declared
`metersPerUnit = 0.001`, and Blender turns a stage whose unit is not its own
into a SCALE on the root object. `write_usd(scale_to_meters=True)`, the default,
converts the geometry to metres and declares the stage as metres instead, so
there is nothing left to convert. Measured, scene set to millimetres:

    millimetre stage (old)  565 objects  1 off scale 1  125.0001 x 179.3225 x 167.0475 mm
    metre stage (new)       564 objects  0 off scale 1  125.0001 x 179.3225 x 167.0475 mm

**The placements must scale with the geometry.** `_m4` takes the factor and
applies it to the TRANSLATION only -- the rotation is unitless. Miss that and
every part keeps its millimetre offset while the meshes shrink, so the assembly
flies apart by ~1000x. That is what the size comparison above is really testing,
and the handle is the case that proves it: 230 occurrences, six levels deep.

Normals are directions and stay unit-length under a uniform scale, so they are
deliberately left alone.

**The file's own unit never has to be read.** OCCT normalises every STEP to
millimetres on import whatever the file declares (`xstep.cascade.unit`), so the
input is always mm and `meters_per_unit=0.001` describes it correctly.

## No synthetic /Root prim

Benn: "I dont need this root parent, just keep the top parent from the
assembly, it is always OK from a step file". So the assembly's own roots are the
top-level prims now -- the valve imports as `SKF_200S_くし刃タイプ`, the handle
as `HandleModel_ASSY_sw_005`, with nothing above them. Object counts fall by
exactly one (222 -> 221, 565 -> 564).

Two things moved with it:

- **defaultPrim** is the first assembly root. A stage can name only one, so with
  several roots the rest are still imported by Blender but a consumer that
  REFERENCES the file gets only the named one.
- **the up-axis rotation.** USD metadata only knows Y-up and Z-up, so -Z, -Y,
  +X and -X are a real rotation, which used to sit on /Root. It now sits on each
  assembly root as an op AFTER its own transform: USD composes xformOps in list
  order with row vectors, so `[transform, rotateXYZ]` means "place the node,
  THEN rotate the world" -- which is what a parent rotation did. Reversing the
  two rotates each part about its own origin and still looks plausible.

That path had NO test -- this file claimed one existed in `spikes/` and it did
not. `spikes/up_axis.py` now recovers the rotation by differencing each export
against the +Z one (whose root carries none) and checks the chosen model axis
lands on the stage's up vector; all six pass, and it asserts the wrapper never
comes back.

**The two changes needed each other.** With /Root gone but the millimetre stage
kept, Blender puts the 0.001 straight onto the user's own assembly root -- worse
than before, because now it is on the prim he actually works with.

## Colour: a face colour beats the shape colour

Benn: "everything has always the same color, so I dont think the thing is
looking at the correct color data... What I need is the color that will be used
in blender as the BSDF color when you export a fbx from fusion 360."

Half of that was a real bug. `knob (failing).step` carries TWO colours and names
them itself:

    #CBD2EF  'Opaque(203,210,239)'   on all 17 faces  <- SolidWorks part colour
    #A0A0A0  'Steel - Satin'         on the shape

The reader finds both (it queries ColorSurf, ColorGen and ColorCurv at shape and
face level). But `_bind_materials` only emits GeomSubsets when there are TWO OR
MORE distinct face colours, so a part painted a single colour fell through to
the shape-level colour and exported as the grey. A colour on a FACE is more
specific than one on the shape, so when every face shares one it now wins.
Verified in Blender: the Principled BSDF Base Color is `#CBD2EF`, exactly the
203/210/239 in the file.

**The linear/sRGB handling was never wrong, and it is worth not "fixing".** STEP
stores sRGB (0.7961); OCCT's `Quantity_Color` returns LINEAR (0.5972);
UsdPreviewSurface `diffuseColor` is defined as linear; Blender holds linear and
its colour picker displays `#CBD2EF`. The round trip is exact -- converting
anywhere in that chain would break it.

`spikes/color_probe.py` answers "is there colour in this file at all" for any
STEP, independently of our reader: schema, every colour entity counted, and each
`COLOUR_RGB` with its NAME and hex. Scan the file as ONE string -- reading it
line by line found only the first of the knob's two colours, because a STEP
entity may wrap across lines, and that made the file look as though it held a
single grey.

## Sharp edges: marked from G1, not guessed

`_is_g1(edge, faces)` compares the two faces' surface normals at points along
their shared edge, honouring each face's orientation. Tangent-continuous means
NOT sharp: a fillet meeting its neighbours must stay smooth, a chamfer or a
plain intersection must not.

**The threshold is not a tuning knob.** Measured across 2353 shared edges on the
valve, the distribution is cleanly bimodal:

    0 - 0.5 deg   48.6%      <- G1: fillets and tangent blends
    0.5 - 2 deg    0.0%      <- nothing at all lands here
    2 - 10 deg     0.6%
    10 - 30 deg    4.8%
    30 - 80 deg   14.8%
    80+ deg       31.3%

Anywhere in that empty gap gives the same answer; 1 degree is used.

`Mesh.sharp_edges` holds welded index pairs, collected during the topological
weld (where the per-edge node chains already exist) and carried through the
relabel.

**NO CREASES ARE WRITTEN, and that is deliberate.** Benn: "never mark anything
as crease. use only sharp when the continuity of the nurbs surfaces is not
tengency or curvature." A crease is a subdivision instruction baked into the
asset, and it is not ours to decide for whoever models downstream.

writer.py used to emit every sharp edge as a crease at sharpness 10, on the
belief that creases were how Blender learned to mark an edge sharp. **That
belief was wrong**, and it was worth measuring rather than assuming. On the
knob:

    with creases     298 sharp,  289 creased at value 1.0
    creases removed  298 sharp,    0 creased

Identical sharp count. The flags come from the **faceVarying split normals**,
which diverge exactly where the surface is not tangent-continuous -- so the
sharp marking was always free, and the creases were pure baggage. Confirmed at
scale on the valve at 0.6/25: 30,952 sharp edges across 169,147 edges, **0
creases**.

`_is_g1` is what decides where the normals split, so an edge is sharp only where
the surfaces are NOT tangent, and therefore not curvature-continuous either --
exactly Benn's rule. A forced loop cut is tangent across itself, so it is never
sharp: measured 345 -> 345 sharp edges with a cut added.

## Verified in Blender, end to end

```bash
"C:/no install prog/blender/stable/blender-4.5.13-lts.daeeeca98fb0/blender.exe" ^
    -b --python spikes/blender_check.py -- <exported.usdc>
```

Measured on the valve at 0.6/25 (164 bodies). (The import flag is
irrelevant -- see the note above.)

    meshes                        164
    custom split normals          164 / 164
    polygons                  174,642   (121,449 quads, 70%)
    flat-shaded polygons            0
    non-manifold edges              0
    meshes with open edges          3   -- Body_162/163/164 only

Those three are the shells that were never closed solids in the STEP, so every
actual SOLID arrives watertight. The loop normals genuinely vary within a face
(face normal (0.955,-0.073,0.287) against loop normals (0.924,0,0.383) and
(0.979,-0.161,0.125)), which is the faceVarying data doing its job: smooth
across a curved face, sharp at a real CAD edge, without splitting a single
vertex. `mesh.SetNormalsInterpolation(faceVarying)` in writer.py is what carries
it, and Blender reads it as custom split normals.

Note Blender 4.5 removed `Mesh.calc_normals_split()`; read `loop.normal`
directly and check `mesh.has_custom_normals`.

## Testing

```bash
.venv/Scripts/python.exe spikes/gui_smoke.py          # headless GUI + screenshots
.venv/Scripts/python.exe spikes/cut_gestures.py      # loop-cut clicks, glow, snap
.venv/Scripts/python.exe spikes/screen_choice.py     # which monitor a test uses
.venv/Scripts/python.exe spikes/open_with.py         # Explorer association
.venv/Scripts/python.exe spikes/export_selected.py   # export only the selection
.venv/Scripts/python.exe spikes/blender_units.py     # scale 1 + real size
.venv/Scripts/python.exe spikes/up_axis.py           # all six up axes
.venv/Scripts/python.exe spikes/color_probe.py <f>   # what colour a STEP holds
.venv/Scripts/python.exe spikes/ui_features_test.py   # tree, visibility, colours
.venv/Scripts/python.exe spikes/real_file_test.py     # the valve, end to end
.venv/Scripts/python.exe spikes/make_test_step.py     # regenerate the fixture
.venv/Scripts/python.exe spikes/make_multicolor_step.py  # multi-material fixture
```
Blender round-trip checks live in `C:\Users\benn\AppData\Local\Temp\*.py`
(handlecheck / quadcheck / matcheck) — re-create them if lost; they import a
`.usda` and report objects, hierarchy, polygon sizes, non-manifold edges,
material slots.

### Fillet-first: TRIED, MEASURED WORSE, left switchable

Benn's proposal: small-radius long fillets are where an imperfection shows as a
broken normal, so mesh those as close to a regular ladder as possible FIRST and
let everything else adapt. The priority is right and the reasoning behind
"small radius" is right too -- at 30 degrees a 1 mm fillet gets about six rows
across the blend, few enough that the eye reads every one of them, while a big
fillet has enough rows for irregularity to average out.

**The detector is good and worth keeping.** `_fillet_of` looks at the SURFACE,
never at the boundary: a blend is a surface with one small, roughly constant
principal radius and a much larger extent across it. Torus and cylinder give the
radius exactly and free; B-spline/Bezier/extrusion/revolution are sampled with
`BRepLProp_SLProps` and rejected if the radius wanders more than 3x. Being
surface-based, it is immune to both flaws Benn predicted for a boundary-shape
test: a long edge split into several segments, and a dent eaten out of one side,
change nothing. It flags 935 of 3916 valve faces and 2239 of 7442 handle faces.

**What was implemented:** `edgealign.plan` gained a `face_filter`, so with
`MeshParams.fillet_first` on, ONLY the flagged fillets ask for extra edge
vertices -- the targeted version of `align_edges`, which applied to every band
costs 2.4x the time for a mixed result.

**It does not work, and the reason is structural.** Scoring only the flagged
faces (`spikes/ladder_metric.py`, which judges quads, sliver share, cell-size
spread and worst valence -- NOT `_iso_fraction`, which rates a fillet of neat
quads flanked by long slivers highly):

    0.2/25   valve  OFF  76.7% quads  28.22% slivers  spread 5.32  valence  23
             valve  ON   77.1% quads  27.72% slivers  spread 5.17  valence  23
             handle OFF  75.7% quads  42.08% slivers  spread 3.39  valence  21
             handle ON   77.6% quads  37.27% slivers  spread 4.67  valence 113
    0.5/35   same shape: valve unchanged, handle valence 15 -> 85

The valve is noise for +70% tessellation time. The handle buys fewer slivers and
pays with a worse size spread and a worst vertex valence of 113 -- a FAN, which
is the exact defect the whole idea exists to remove. Augmenting an edge makes the
rungs LAND on boundary nodes, but the column spacing is still OCCT's, so a denser
uneven ring is still uneven; and the added vertices have to go somewhere, which
is where the fan comes from.

A regular ladder needs the fillet's long edges RE-SAMPLED uniformly
(`GCPnts_UniformAbscissa`), replacing their nodes rather than adding to them --
which forces every face on those edges to be rebuilt from scratch instead of
patched. That is the honest prerequisite, and it is the same conclusion the
edge-alignment section reached from the other direction.

`fillet_first` therefore defaults **False**. `_fillet_of` and
`is_priority_fillet` stay: they are the right building blocks for the re-sampling
version, and the step-through debugger uses them to label faces.

### Cones that reach their apex: `_cone_fan`

Benn's construction, and the target is the bottom of a drilled hole -- OCCT
hands those back as a fan of needle triangles, which he described as "very
dirty" (body 49 has fourteen of them, 118-degree drill points: full revolution,
radius 0 -> R, apex carried as a degenerate edge).

`_cone_fan` lays concentric circles around the apex and joins them with quads,
closing the middle with one fan. The rule that makes it clean is Benn's: the
ring count only ever DOUBLES outward, and only where doubling is earned, so a
transition is exactly "every edge splits in two" -- one inner node feeding two
outer segments. Every ring is a perfect circle perpendicular to the axis, so
every added vertex is exactly on the surface. Radial spacing is free: a cone is
ruled along its generatrix, so a straight span between rings lies exactly on the
surface at any spacing, and it is chosen purely to keep cells square.

The count can never fall below `360 / angle_deg`: on a cone the normal sweeps
the whole turn however small the circle, so the angle criterion does not relax
as the apex is approached. The rim is OCCT's own and is used verbatim -- it is
shared with the hole wall, and moving it would open a hole.

Three things that had to be got right, and each was invisible in the counters:

- **Do not step inward by the local arc length.** The step shrinks with the
  radius, so v approaches the apex geometrically and never arrives -- measured
  54 rings of invisible detail on a 2.1 mm drill point. The ring count is now
  decided once, from the generatrix length against the rim's own spacing.
- **Dedupe the rim by 3D POSITION, not by u.** A full revolution ends at
  u = 2*pi, whose node is the same point in space as the one at u = 0. Keeping
  both makes the patch validator reject the face as seam-degenerate once the two
  weld, and every cone silently fell back to OCCT.
- **The rim must be DECLARED as the constraint loop.** `_patch_is_sound`
  compares the edges used exactly once against the constraints it was given, so
  returning `[]` makes the face's own boundary read as an error. This is what
  kept `LATTICE_STATS` at "unsound after weld" for all 14 faces while
  `CONE_STATS` cheerfully said ok=14.

Winding is normalised by the signed area of the emitted polygons in parameter
space, because whether the apex sits at greater or smaller v depends on how the
cone was authored -- and getting it wrong produces winding errors, not holes,
which sends the whole BODY back to plain OCCT while the counters still say the
fan was built. Same trap as the sphere apex fill.

Measured on the valve at 0.125/30, `min_size` 0.1: 38 cones accepted, polygons
252,920 -> 239,065, HOLES=0, nonman=0, wind=0. The handle has no qualifying
cones, so it is untouched. On body 49 alone the effect is much larger (7,699 ->
2,341 polygons) and verified correct rather than assumed: mesh volume matches
the B-rep to 0.001% and every face's area matches to within 0.25%, in both arms.
Slivers model-wide move the wrong way very slightly (23.42% -> 24.77%).

Honest gap: the hole WALLS next to these cones also get much leaner (a cylinder
at 444 polygons -> 27, same area to 0.25%), and that side effect is not yet
explained. It is a visible improvement -- the wall goes from a needlessly dense
grid to clean full-length strips -- but the mechanism should be understood
before anyone leans on it.

### The step-through debugger

Right-click one body in the outliner -> "Step through tessellation...". It
reveals the body one B-rep face at a time and reports, per face, the surface
type, the PATH the mesher chose, polygon and quad counts, and the blend radius.
Benn's words: the mesher "is a freaking black box" -- it reports counters at the
end and nothing about how it got there.

It reads `LAST_FACE_PATHS`, so it explains rather than just displays. On body 138
it immediately shows the four B-spline fillets (r=3.00, aspect 6) and both tori
all coming back "ruled declined" -- the body 60 story, visible at a glance.

**Face indices are 1-BASED.** `_tessellate_once` walks
`range(1, faces.Extent() + 1)` and writes that into BOTH `face_source` and
`LAST_FACE_PATHS`. Indexing from 0 shifts every path by one face, which shows up
as a PLANE apparently taking the ruled-grid path -- which is impossible, since
`BANDED_SURFACES` has no plane in it, and is how the off-by-one was caught.

### The silent killer: OCCT under-meshing, and a repair that hid it

Benn's report, and it is the worst class of bug this program can have: "some
conical shapes dont work AT ALL... the surface completely collapsing, leaving a
void. as the vertex just collapse, the body is still often considered as
watertight, this is why it is so sneaky: there is no error detected!"

Reproduced on `knob (failing).step`, face 4 -- a full-revolution conical band,
12 real edges, 2 seam edges, no apex, heavily trimmed:

    true area                             823.6 mm2
    OCCT's own triangulation              254.0 mm2   (-69%, 104 open edges)
    after _repair_undermeshed            3800.1 mm2   (+361%, sealed, "watertight")

**It is OCCT's bug, not ours.** Plain OCCT with every one of our paths disabled
gives the identical -69%, and no meshing parameter avoids it
(`EnableControlSurfaceDeflectionAllSurfaces`, `InternalVerticesMode`,
`ForceFaceDeflection`, `AdjustMinSize`, MinSize, Angle -- all measured, all
still wrong). It is deflection-DEPENDENT with a hard cliff: correct at 0.4 and
coarser, broken at 0.3 and finer, which is why "changing the settings cant make
it work" within any sane range.

**What made it invisible was ours.** `_repair_undermeshed` ear-clips the face's
UV boundary loop, and on a SEAM face that loop is not a simple polygon -- it
crosses the parameter seam -- so the patch came out folded and self-overlapping:
4.6x the true area, but topologically sealed. A detectable hole (104 open edges)
became an undetectable corruption. The repair now PROVES itself: it replaces the
whole face, so its area must match `BRepGProp.SurfaceProperties_s` within 25%,
and it refuses otherwise. An honest hole beats a sealed lie.

**Detection is free, because the precursor was already computed.** A face whose
triangulation does not even reach its own boundary nodes is exactly what
`_repair_undermeshed` tests for. When that is true and no honest repair is
possible, `Mesh.surface_defects` counts it. Validated against Benn's own pair of
files, which differ only in whether the conical face is split:

    knob (failing)                 vol err  -3.71%   surface_defects=1
    motor conic surface failing    vol err  -3.05%   surface_defects=1
    motor cylindrical surf cut     vol err  -0.04%   surface_defects=0

A full per-face area audit was measured and REJECTED as a default: 20 s on the
handle's 7,442 faces, and a per-body volume audit is 19 s on 243 bodies, against
a 30-50 s tessellation. The free precursor gives the same answer.

### ...and the FIX: drop the analytic tag (`_nurbs_variant`)

Benn: "that's nice, but I need a triangulation of the surface! we have to find a
way." He proposed three routes and the third one wins.

**What works: `BRepBuilderAPI_NurbsConvert`.** Re-expressing the solid's
surfaces as NURBS routes the face through a different mesher path entirely:

    knob    vol err  -3.71% -> -0.34%   surface_defects 1 -> 0
    motor   vol err  -3.05% -> -0.07%   surface_defects 1 -> 0

It is safe because it changes the PARAMETERISATION, not the shape: volume moves
+0.0009% (knob) and -0.03% (motor), area 0.0001% and 0.07%, the solid stays
`BRepCheck` valid, and the face COUNT is unchanged so `face_source` still lines
up with the B-rep faces and colours/GeomSubsets survive. `_nurbs_variant`
refuses any conversion that moves the volume more than 1%, so a conversion that
did deform the part can never be shipped.

**What does NOT work: splitting the seam face in the B-rep.** This was Benn's
first idea and it is what `ShapeUpgrade_ShapeDivideClosed` does. Measured:

    knob   ShapeDivideClosed(1)  vol err +19.68%   -- WORSE than the defect
    knob   ShapeDivideClosed(2)  vol err  -1.33%   -- still wrong
    motor  ShapeDivideClosed(2)  vol err -32.47%, 2473 boundary edges -- torn open

`ShapeUpgrade_ShapeDivideAngle` -- the purpose-built "split a periodic surface
along generatrices at a maximum angle", which is exactly the manual cut Benn
made in CAD -- **SEGFAULTS** the process outright. Do not spend time on the
`BRepAlgoAPI_Splitter`-by-hand version either: it is the same operation, and
both OCCT wrappers for it fail.

**It can SEGFAULT, so it runs in a CHILD PROCESS (`nurbsfix.py`).** Handle body
`CenterLED_sw_001_DECO_B` converts cleanly, passes the volume guard (-0.85%),
and then kills the process inside `BRepMesh`. Nothing predicts it: face count,
per-face areas, UV bounds and total volume are all healthy on the converted
shape, and `BRepCheck_Analyzer` crashes on it too, so it cannot even be
validated in-process. A segfault cannot be caught, so the work is done where it
is allowed to die -- the parent writes the solid to a BRep file, runs
`stepusd.nurbsfix` as a child, and reads back a pickled Mesh. If the child dies,
times out, or refuses on the volume guard, the parent keeps the flagged
original. Same reasoning as `loadproc.py`.

That crash is why the sweep must be re-run after ANY change here: with the retry
last in the cascade it almost never ran and the sweep passed; moving it first
made it run on every defective body and the sweep segfaulted on the first
setting, having printed nothing at all.

**Order it FIRST and drop the coarsening.** The defect path used to queue three
coarsening retries plus the NURBS one, so a defective body ran SIX full
tessellations -- Benn noticed immediately ("the loading of the parts takes a
VERY long time"). The failing motor was 39.1s against 6.1s for the version that
meshes cleanly. With the known fix tried first and coarsening removed it is
**16.7s in 2 passes**, which is the floor: the original plus the fix.

The break also needs the precursor DISPROVEN PER CANDIDATE. `surface_defects` is
only a precursor; when a candidate's volume matches the solid it has no defect,
and leaving the flag set stopped the loop breaking on the very candidate that
had just fixed it (3 passes instead of 2).

**Do not auto-coarsen out of it.** The obvious fix -- retry the body coarser
until the defect goes -- was implemented and measured WORSE: the knob went from
-3.7% to **+17.1%** volume error, because dropping the whole body to 0.5 mm to
save one face wrecks everything else. The cascade now ranks candidates on real
volume error (affordable, since it only runs on the rare defective body) and
keeps the accurate-but-flagged mesh. Volume ranks BELOW holes and non-manifold
edges: ranking it above traded clean meshes for non-manifold ones and gave the
handle new non-manifold bodies at 0.25/18 and 0.15/20.

Measured on the handle, before -> after the NURBS retry:

    0.6/20   defects 2 -> 1      0.4/25   1 -> 0      0.25/18  1 -> 0
    0.15/20  defects 3 -> 0      0.08/12  0 -> 0

`open=0` at every setting throughout. The one that remains at 0.6/20 is
`CenterLED_sw_001_DECO_B`, whose NURBS conversion is the one that segfaults --
so it correctly keeps its flagged original instead of crashing.

`surface_defects` shows as a red "SURFACE ERROR" row in the outliner and in the
Result panel. An OPEN SHELL keeps its own label -- it is already visible and
already reported; this label is for the silent case, a defect with no holes.

### The NURBS retry was REVERTED: correct volume, ruined topology

It fixed the geometry -- knob -3.71% -> -0.34%, motor -3.05% -> -0.07% -- and
Benn's verdict on the result was "I cant work with this". He was right, and the
measurement says why:

    knob   analytic   quads 80.5%  slivers 18.2%
    knob   converted  quads 21.5%  slivers 28.8%
    motor  analytic   quads 80.5%  ->  converted 39.6%, polygons 29k -> 53k

`BRepBuilderAPI_NurbsConvert` strips the analytic tag off EVERY face, so the
ruled grid, the cone fan and quad pairing all stop applying and what comes back
is raw Delaunay. **The acceptance test was wrong, not just the result**: volume
does not measure topology, exactly as a vertex test does not measure chord sag.
Any future attempt here must be scored on quads/slivers as well as volume.

Converting only the ONE bad face was tried and does not work either:
`GeomConvert.SurfaceToBSplineSurface_s` refuses an infinite cone (trim it first),
and the conversion re-parameterises -- the knob's cone went from u = -pi..pi to
pi..3pi. Shifting the knots back aligns the domain, but `BRep_Builder.UpdateFace`
plus re-added wires still yields a ZERO-AREA face, because the pcurves are stored
per (edge, face) pair and do not survive the swap. `nurbsfix.py` is kept, gated
off behind `if False`, because the child-process isolation it carries is the
reusable part.

### THE FIX for full-revolution cones: cut one generatrix (`split_seam_faces`)

Benn: "full revolution cone are completely failing... dont hesitate to add cut
to the cones in the nurbs state, this is perfectly acceptable (and proven
working, even if the 2 resulting surfaces are still tagged as cones)."

That last clause is the whole point, and it is why this works where the NURBS
conversion did not: **the halves are still CONES**, so the ruled grid, the cone
fan and quad pairing keep applying. Measured on the knob, one cut:

    original   vol -2.90%   worst face err 478.1%   quads 79.4%   slivers 19.1%
    1 cut      vol -0.12%   worst face err   0.9%   quads 84.2%   slivers 14.5%

Better on EVERY axis at once, with fewer polygons (1,693 -> 1,624). And on the
motor it reproduces Benn's manual CAD cut almost exactly -- 28,477 polygons at
81.4% quads against his hand-split file's 28,478 at 81.3%, both at -0.04%.

`BRepFeat_SplitShape` is the tool (NOT `BRepAlgoAPI_Splitter` on the solid,
which would cut the whole part). It takes the solid, an edge, and the face that
edge lies on, and divides just that face while keeping the solid valid --
volume moved 0.000% on every case measured.

**Picking where to cut is Benn's "longest isoline" rule, and it is necessary.**
The cut curve must lie ENTIRELY inside the trimmed face or the split leaves a
dangling edge that divides nothing. `_clean_isoline` classifies a grid of (u,v)
with `BRepTopAdaptor_FClass2d` and keeps only generatrices that are inside for
their whole v range: on the 5-lobed knob just **10 of 180** qualify, in 5 groups,
and the MIDDLE of the widest group is the robust place to cut. Note the opposite
generatrix is NOT usable there (23% covered), so a plane through the axis would
not have worked -- one cut is enough anyway, since a single cut already removes
the periodicity.

Two things that had to be right:

- **The split must compete through the whole cascade.** Comparing the cut shape
  against the original with every other path unchanged threw it away on the
  knob: both still carried the cone fan's 24 non-manifold edges, tied on that,
  and the plain fallback then beat them both -- discarding the one candidate
  whose geometry was actually correct. It is inserted as the FIRST TWO attempts
  (with and without the cone fan) and ranked like anything else.
- **Face indices must be renumbered back.** `face_source` carries B-rep face
  indices to `writer._bind_materials`, which reads `face_colors[fi - 1]` from
  the ORIGINAL shape. Splitting shifts every index after the cut, so a coloured
  model would silently get every material after it assigned to the wrong face.
  `split_seam_faces` returns a {new -> original} map built from
  `BRepFeat_SplitShape.Modified`, and the cascade applies it. Verified: the knob
  reports faces 17/17 with max index 17, the motor 895/895 with max 895.

`TopTools_ListOfShape` is directly iterable in OCP; there is no
`TopTools_ListIteratorOfListOfShape` binding to reach for.

Measured cost: it only runs on a body that already flagged a defect, and the
handle triggers it on ZERO bodies at 0.15/28, so the model-wide cost is nil.

### `_cone_fan` must be the FIRST thing the cascade drops

The knob's anarchic top (Benn's third screenshot) was NOT the cone defect and
NOT the sphere. `_cone_fan` emitted a patch on one small cone that only turns
out to be non-manifold after the GLOBAL weld -- 24 non-manifold edges and 24
winding errors -- which no per-face check can see. The old cascade answered that
by dropping EVERY structured path, so the body arrived as:

    structured : 1,618 polys  80.5% quads  18.2% slivers  nonman=24  <- rejected
    plain      :   706 polys  66.3% quads  49.6% slivers  nonman=0   <- chosen

The fallback was the correct choice given the options; the mistake was having
only those two options. Dropping just the fan gives 1,693 polys, 79.4% quads,
19.1% slivers, nonman=0 -- keeping the ruled grids, the sphere caps and the
quads. `MeshParams.cone_fan` therefore exists purely so the cascade can drop it
alone, and it is tried before every other fallback.

The rim that triggers it: 25 polygon nodes, 25 distinct node ids, **24 distinct
POSITIONS** -- the seam closure, where the first and last nodes are different
ids at the same point. Every one of the 24 rim edges comes back non-manifold, so
the fan's rim handling on a seam-closing rim is still wrong. Unfixed; the
cascade contains it.

**General lesson, and it is the same one as the swap reflection:** a structured
path whose output is only invalid after welding poisons the WHOLE body, because
the cascade's granularity is the body. Every such path needs its own fallback
step, or one bad face silently costs a hundred good ones.

### Forced loop cuts (`apply_iso_cuts`) and "By surface type"

Two of Benn's tweaking tools, and the first one is nearly free because
`split_seam_faces` already did the hard part.

**Forced loop cut.** Arm "Add loop cut on click", pick U or V, hover a surface
to see exactly where the cut would land, and click to make it. The isocurve
becomes a real B-rep EDGE via `BRepFeat_SplitShape`, so the mesher is obliged to
run a line of vertices along it exactly as along any other edge.

The cuts ride on `MeshParams.iso_cuts` -- a TUPLE, so `_signature` stays
hashable and the mesh cache notices a cut being added or removed. `tessellate`
is now a thin wrapper that applies them once up front and hands the cut shape to
`_tessellate_cascade`, so every structured path, the fallback cascade and the
defect detection all see the cut geometry. Face references are mapped back
afterwards (`_remap_faces`) -- `face_source` reaches `writer._bind_materials`,
which indexes `face_colors[fi - 1]` on the ORIGINAL shape, so without the map a
cut would silently shift every material after it.

**The cut WALKS across tangent faces** (`propagate_iso_cut`, GUI "Continue
across tangent edges", on by default). Benn: "prolongate the cut to any surface
that is tangent or curvature continuity after the cut edge, and continue again
on the next surface until you hit a hard edge... we should be able to have a
full ring cut". Seeded on the motor's big outer cylinder it walks
**Cylinder -> Torus -> Cone -> Torus** and stops at a hard edge, and the meshed
result stays watertight with 0 holes and 0 non-manifold edges.

Three rules, each load-bearing:

- **Cross only edges `_is_g1` calls tangent** -- the very test that decides
  whether an edge is drawn sharp, so the cut stops exactly where a modeller
  would say the shape stops being smooth.
- **The direction can FLIP on the next face.** U on one surface is not U on its
  neighbour: a cylinder's axis is a torus's minor circle. The continuation tries
  both isocurve families through the entry point and takes whichever runs
  closest to the direction the cut arrived in.
- **Stop on revisiting a (face, direction)** -- that is what closes a full ring
  instead of walking forever.

**Leave a face by the end AWAY from where the cut came in.** Forcing the walk
to always exit at `pts[-1]` made it turn back on itself: the nearest edge was
then the one it had just crossed, which is skipped, so the next-nearest was far
away and the walk stopped with "no edge at the tip" while the surface carried on
happily. Benn spotted this from a screenshot -- the line stopped, but starting
from the neighbouring face carried on further, which is the giveaway. Fixing it
took the motor chain from 4 to **5** stretches (Cylinder -> Torus -> Cone ->
Torus -> Cylinder), both ends now stopping at a real hard edge.

**A surface may be crossed TWICE, on different isocurves.** The walk used to
key `seen` on (face, direction), so a ring that legitimately comes back across a
surface on another isocurve stopped there. It now keys on the PARAMETER too, and
stops instead on a real geometric test: the cut coming back within
`bbox diagonal * 1e-4` of its own start. That is what "the line went all the way
round" actually means, and it is announced -- "full loop detected, N tangential
surfaces" -- in the status bar, on HOVER as well as on the click, so the ring is
confirmed before committing to it. `LAST_LOOP_CLOSED` carries the verdict.
A closed ring also skips walking the second end, which would only retrace it.

**Sampling is not enough to find where the cut leaves a face.** At 120 samples
over a 191 mm face the last inside point sits 1.6 mm short of the boundary --
too far to identify which edge is crossed, and too inaccurate to line the cut up
on the next face. `_iso_exit` bisects the crossing instead. Before that fix
propagation stopped instantly on every seed with "no edge at the tip".

**The preview is not decoration.** Benn: "I need a preview of where it will be
cut, this is important to visualize before clicking!" `_iso_points` samples the
isocurve and CLIPS it with `BRepTopAdaptor_FClass2d`, so it never promises a cut
where the face has been trimmed away. Drawn with lighting off and polygon offset
so it does not z-fight with the surface it lies exactly on.

### Managing the cuts: they are objects, not a batch

Cuts are stored GROUPED BY CHAIN -- one click makes one chain, which may span
several faces -- because Benn needs to remove one bad line without losing the
rest: "it may take time prepare, this would be painful to redo everything just
because only one line is not perfect."

    existing cuts        drawn RED, always, while the tool is armed
    hovered cut          GLOWS orange and thicker

The gestures that act on them are in "The loop-cut gestures" below, which
SUPERSEDES the shift+click binding this section originally described.

Cuts are UNDOABLE: `_push_cut_undo` snapshots before every add, delete and
clear, and **Ctrl+Z** steps back through them (64 deep). Placing a cut takes
thought, and there was no way back from a mis-click.

The prospective (yellow) cut is drawn TWICE: solid in the scene, and again in an
overlay renderer on layer 1 at 35% opacity. Layer 1 has no depth relationship
with the model, so the far side of the line shows through the part -- which is
the only way to see that a ring really closes rather than just heading round the
back. `SetNumberOfLayers(2)`, sharing the main camera.

"Is the cursor on this cut" is answered in the surface's OWN parameters, not in
space: a cut IS an isocurve, so it reduces to comparing one parameter against
the hover's, within 2% of the face's range. Exact, and free.

`max_steps` counts SURFACES IN THE FINISHED CUT, not steps per end. It walks
both ends, so counting per end made a limit of 4 produce 5 stretches -- which is
not what someone setting "how far should this go" means.

Right-click while the tool is armed swaps U/V, and the preview redraws
immediately -- swapping with the cursor still would otherwise be invisible.

**Blender reads it as a loop cut, and that had to be checked.** The cut adds
ZERO sharp edges (measured 345 -> 345 on the knob): the two halves are tangent,
so `_is_g1` correctly calls it smooth and `writer.py` emits no crease. A creased
cut would have read as a hard feature edge instead of a subdivision. 64% of
vertices sit only in quads, which is what lets Alt+click walk the loop.

**"By surface type" colours faces by what they ARE**, with a legend, and the
labels are Benn's rather than OCCT's: B-spline and Bezier both show as **Free
surface**, because the distinction that matters to a user is analytic (exact,
predictable) against free-form (whatever the CAD system fitted). `Mesh.face_kinds`
carries the type per B-rep face, 1-based to match `face_source` -- free to
collect, since the mesher already asks every face for its type.

Two traps in that mode:

- `set_face_color_mode` only toggles `SetScalarVisibility`; it uploads nothing.
  A body with no material colours has no cell scalars, so switching mode left
  the knob one flat colour until `_recolor` re-ran `update_meshes` with the new
  per-cell array.
- `face_kinds` is indexed by the shape actually meshed, so a seam split or a
  loop cut shifts it. It is remapped alongside `face_source`, or the colours
  land on the wrong faces after a cut.

**A patching accident worth remembering.** Inserting the `face_kinds` capture at
8 spaces where the surrounding code sits at 12 closed the enclosing block early
and truncated the whole face-processing loop: the knob came back with 2 of 17
faces meshed and **-88.75% volume**, still reporting `open=0` and
`watertight=True`. Another instance of the counters being no defence -- only the
volume check caught it.

### The sweep is REPRODUCIBLE, so a changed number is a real change

Worth knowing before blaming noise, which is the tempting explanation when a
count moves and the code that moved it is not obvious. `MeshParams.parallel`
defaults True, i.e. OCCT's TBB threads are meshing each body, so it is fair to
wonder -- but measured, it is deterministic: three consecutive runs of the
216-body model at 0.25/18 gave `LEAKS=1 nonman=4 bodies=[(67, 4)]` every time,
the same body and the same edge count.

So when a sweep line disagrees with a number written down earlier, the
difference is either a code change or a STALE baseline -- never the dice. Check
what landed between the two runs before reaching for an explanation.

### Do not run a GUI suite while the sweep is running

The GUI spikes wait with fixed-duration `pump(app, 0.4)` calls, which is not a
condition -- it is a guess about how long Qt needs. Run one while the watertight
sweep is saturating every core and the guess is wrong: `blender_keys` reported
`H -> 0 hidden (want 3)` under load and passes every time on its own. It is a
test artefact, not a regression, but it will look exactly like one.

### A cut that touches an existing edge: the splitter wants VERTICES

Benn: "the cuts are weirdly ignored also on location where they should
actively cut... It is like the cutting prevention location is bleeding."

He was right about the bleeding, and it is worse than a bleed.
`BRepFeat_SplitShape` refuses a cut curve that touches an existing edge, and it
does not refuse LOCALLY -- the whole face's cut is lost. One bolt vertex in the
path costs the entire loop across that wall, which is why the surface stayed
unsubdivided near the bolt while other faces in the chain cut fine.

**Handing over only the interior stretches does NOT fix it.** Measured on
`spikes/out/ribbed.step` (a wall with 8 ribs whose base edges lie on the
isocurve), cutting at the rib height:

    8 interior arcs, Add(edge, face)      43 faces -> 43   nothing
    8 interior arcs, Add(wire, face)      43 faces -> 43   nothing
    8 interior arcs, Add(compound, face)  hangs, 44 GB of RAM before the kill
    the full span as one edge             43 faces -> 43   nothing

**What OCCT wants is TOPOLOGICAL connection, not geometric coincidence.** The
arcs' endpoints were 4e-6 mm from existing vertices -- close, and useless. Built
on the existing `TopoDS_Vertex` objects, the same eight arcs split the face:

    8 arcs anchored to the face's vertices   43 faces -> 44, edges 102 -> 110

and the mesh gets the ring it was asked for -- at 0.2 mm, polygons 226 -> 414,
ring vertices 51 -> 83, and the widest gap in the ring 36.4 -> 7.3 degrees.

`_anchored_cut_edges` does that, and returns None if any stretch cannot be
anchored, so an unproven case keeps the plain full-span edge rather than
shipping something unmeasured. The bisection in `_iso_interior_runs` only
converges to `FClass2d`'s own tolerance, so each end is re-projected ONTO the
vertex before the edge is built.

**The full span stays the default.** It is the proven path and it is only
replaced where it is known to fail, i.e. where the curve is coincident with an
edge. A cut on clear surface takes exactly the code it always did.

### Snapping: coincident is fine, TANGENT is not

The first version of the graze test refused both, and Benn caught it: he needs
to snap to a rib corner and cut along that line, which is the topologically
right place. The two are different and the difference is measurable:

  * a circular edge TANGENT to the isocurve shares its direction at ONE point
    and leaves immediately -- the cut is pinched there (knob: open=1
    nonmanifold=2, while all 19 other parameters on that face are clean);
  * a rib's base edge lying ALONG the isocurve stays on it for its whole
    length -- there is simply nothing to cut on that stretch.

`edge_runs_along` asks the edge directly: walk it and see whether the parameter
the cut holds constant stays constant. Touching fails on the very next sample;
lying along never fails. On the ribbed fixture that takes the refusals from 20
of 20 rib corners to 0 of 20, and the knob's tangent case is still refused.

### The coincidence probe needs a CHEAP gate in front of it

`_iso_interior_runs` classifies 400 points with `BRepTopAdaptor_FClass2d` and
then bisects every transition, and it runs per cut on every re-mesh -- which is
precisely when the user is dragging a slider. Measured at 35.7 ms per cut, so a
body carrying an 8-stretch chain paid 285 ms of probing on top of an 88 ms
tessellation.

Note where the time is NOT: constructing the classifier is 0.06 ms and each
`Perform` is 13 microseconds, so it is the sheer call count plus Python
overhead, not setup. Two changes:

- **Do not bisect unless the runs will be used.** They are only consumed on the
  coincident path, and the bisection is ~640 further classifier calls.
- **Gate on the PCURVES.** `_iso_lies_on_an_edge` reads each edge already
  expressed in the face's own (u, v) via `BRep_Tool.CurveOnSurface_s`, so
  "does v stay at 22 along this edge" is a handful of 2D evaluations with no
  projection and no classifier at all. Measured **0.05-0.20 ms**, against 35 ms
  for the full probe.

    tessellate, ribbed fixture   no cuts  89.7 ms
    8 clear cuts   584 ms -> 153 ms
    1 coincident cut               168 ms   (the only one that pays the probe)

### Report a cut that changed nothing

A split the tool accepts but which divides nothing is the worst outcome for the
user: the red line is drawn, the mesh is unchanged, and nothing says why -- Benn
had to work it out from a screenshot. `Mesh.iso_cuts_ineffective` counts it, the
outliner says "cut had no effect", and the status bar explains.

Two traps in writing that check:

- **`Modified` is non-empty even for a face that was left alone**, so the
  question has to be whether the face became MORE THAN ONE face. Measured: a
  cut placed on a face's own boundary edge reports one modified face and no
  change to the mesh at all.
- **`TopoDS_Shape.HashCode` no longer exists in OCCT 7.9.** Using it to dedupe
  the aimed faces threw inside the `try`, which silently lost EVERY cut --
  `applied=0` everywhere and no error. The faces are keyed on the index we
  already have instead.

### Test caches must be picked DETERMINISTICALLY

The spikes took `glob.glob(".cache/*")[0]` -- whatever the filesystem returned
first. Opening the knob and the motor added caches and silently switched the
whole suite onto a 1-body model, and tests written for the valve failed on
assertions unrelated to the change being tested. `stepusd/_testcache.py` picks
by size (`biggest`) or by what the test actually needs (`with_open_shells`).
Assertions should check the RULE, not a number that only holds for one model:
`blender_keys` asserted min edge == 0.2 mm and broke on a model whose deviation
default differs; it now checks `_nice(deflection * 1.6)`.

### The loop-cut gestures: removal is RIGHT-CLICK only

Benn's rule, and the reason is worth keeping: a left click must never be able
to destroy work. Placing a cut takes a while, so a mis-click that silently
deletes one is much worse than a mis-click that adds one.

    click                 cut here
    shift + click         cut here, snapped to a special point
    right-click           remove the whole cut under the cursor, else swap U/V
    shift + right-click   remove only the glowing stretches
    ctrl + wheel          how far a cut travels
    shift + ctrl + wheel  how many consecutive stretches a removal takes
    ctrl + Z              undo (loop cuts only -- not tree edits or quality)

**The glow must show what will actually go, not the whole chain.** Plain
right-click takes the chain, so the chain glows; shift takes a portion, so only
that portion glows. `show_cut_lines(strips, hot=...)` therefore takes TWO sets
rather than a flag, and the hot set is ghosted through the part as well, so a
cut wrapping round the far side can be judged before it is dropped.

**"Consecutive" is grown by 3D proximity, not by list index.**
`propagate_iso_cut` returns seed-then-one-way-then-the-other, so `chain[i]` and
`chain[i+1]` are usually NOT neighbours on the part -- index 1 and index 2 are
the seed's two opposite sides. `_removal_set` starts at the hovered stretch and
repeatedly adds whichever remaining stretch has the closest ENDPOINT, which is
the right measure because stretches meet end to end at the edges they cross.

**Shift has to be visible before the mouse moves again.** The interactor only
receives key events once it has FOCUS, and the whole point is to answer a
modifier held while merely hovering -- before anything has been clicked. So
`Viewport.watch_modifiers` polls `QApplication.queryKeyboardModifiers()` every
60 ms, which reads the physical state whoever owns the focus.

**The cursor says what the click will do.** Three glyphs drawn with QPainter --
a plain crosshair for cutting, a crosshair plus an X for removing, a crosshair
plus a box for a live snap -- and the removal cursor carries the COUNT when the
wheel has set it above one. The status bar says the same thing and is routinely
missed, because it is at the other end of the window from the thing being
pointed at. Every glyph is stroked black underneath first or it disappears on a
light part.

### Snapping a cut: measure in the PARAMETER, never in 3D

Benn asked for two kinds of point (`face_snap_points`): a point on a CURVED
edge whose tangent is parallel to a world axis, and a vertex of the face. On a
horizontal bore both come out exactly right -- the four cardinal points of each
rim, i.e. the top line, the bottom line and the two side lines. Verified on the
valve by re-deriving every point from the edge independently of the finder:
608 axis-tangent points, worst distance to their own edge 2.9e-14 mm and worst
tangent deviation 8.0e-13 (0.0000 degrees off axis); 1348 corners, all exact.

A straight edge is skipped for the first kind on purpose: if it is axis-parallel
then every one of its points is, so there is no distinguished place to snap to.

**The distance that matters is in the cut's own parameter, not in space.** What
a click produces is an ISOCURVE, so where the cursor sits along that curve
changes nothing -- only the parameter across it does. Comparing 3D distance
makes the top line of a long cylinder unsnappable from anywhere but its very
end, which is not how a CAD snap behaves. `_snap_for` therefore compares
parameters, converts the gap to millimetres through the surface's own
derivative (`_param_scale`, so a radian parameter and a length parameter mean
the same thing), and wraps it round the period on a periodic surface -- a seam
is not a wall.

**A snap must never offer a GRAZING parameter, and this leaked.** The knob's
face 1 is a plane whose v-isocurve is exactly TANGENT to a circular edge at one
of its axis-tangent points (measured |iso . edge tangent| = 1.000000). Cutting
there splits the face at a single touching point instead of crossing it, and
the mesher comes back `open=1 nonmanifold=2` -- while all 19 other parameters
swept across the same face are clean. `face_snap_points` now returns two extra
flags per candidate saying which isocurve grazes an edge there, and `_snap_for`
skips those. Note the U-DIRECTION cut holds u constant and so runs along dv;
getting that the wrong way round filters the wrong candidates.

### A forced cut is the LAST thing the cascade drops

The grazing case was found by accident and there is no reason to believe it is
the only way a hand-placed cut can open a body. Since the cut is baked into the
TOPOLOGY before `_tessellate_cascade` ever runs, no fallback inside the cascade
can undo it -- so `tessellate()` now checks afterwards: if the cut body has
holes or non-manifold edges, it re-meshes the UNCUT shape and keeps that
instead, recording `Mesh.iso_cuts_dropped`.

That costs one extra tessellation only in the rare bad case, and it makes the
guarantee structural rather than a list of known traps: **a forced cut is a
nicety, watertightness is not.** It is reported -- "cut refused" in the
outliner's status column and a line in the status bar -- because saying nothing
would leave the user staring at a cut drawn in the viewport and absent from the
mesh.

Symmetry worth noting: `cone_fan` is the FIRST thing the cascade drops and the
iso cuts are the last, and both exist for the same reason -- a path whose output
is only invalid after the global weld poisons the whole body.

`spikes/cut_gestures.py` asserts all of it: that a left click cannot remove, that
the glow count equals the removal count, that the wheel sets the reach without
shift and the removal span with it, that a snap takes the nearest candidate in
the cut parameter and the placed cut lands on it, that the three cursors differ,
and that the known grazing cut comes back sealed with `iso_cuts_dropped == 1`.

### Test windows open where they will not interrupt

Benn works on the BenQ; `spikes/_screen.py` puts every GUI spike on the Dell if
it is awake and the HP otherwise, so a suite that takes minutes does not sit on
top of his work. The desk is not always in that state, and each state wants
something different -- `spikes/screen_choice.py` asserts all seven:

    Dell or HP attached   put the window there
    only the main screen  use it, but do NOT take the keyboard focus
    driving via Parsec    one screen at a lower resolution -- same as above
    no usable screen      leave the window entirely alone

Four things that are easy to get wrong:

- `QWindow.setScreen` alone does NOT move a window -- it only says whose
  settings to inherit -- so `place()` sets the geometry too, in that screen's
  own coordinates.
- **`place()` must run BEFORE `show()`.** `WA_ShowWithoutActivating` is read
  when the window is first shown; setting it afterwards changes nothing,
  because the focus has already been taken. Every call site was moved.
- **A keyboard-driven test must ACQUIRE the focus, not merely be permitted
  it.** Qt's shortcut map will not match a shortcut whose window is inactive,
  so `QTest.keyClick` is delivered and silently does nothing. Only
  `blender_keys.py` drives the window that way; `keyboard=True` stops the
  attribute being set, but that alone was NOT enough -- positioning the window
  onto another screen before showing it left Windows declining to activate it,
  and the test reported `H -> 0 hidden (want 3)`, which reads as the hide
  feature being broken and points nowhere near focus. `take_focus()` calls
  `raise_()` + `activateWindow()` and then WAITS for `isActiveWindow()`; the
  test asserts it before touching a key, so the failure now names its own
  cause. That fixed the case where the window never activated at all.

  **It did not fully eliminate the flakiness, and the earlier note here
  claiming it did was wrong.** `blender_keys` failed once more afterwards --
  running third in a back-to-back sequence of GUI suites -- and the signature
  was DIFFERENT: `take_focus` returned True and H still did nothing. Run alone
  it passes every time, and re-running the same three-suite sequence passed
  too, so it did not reproduce and is NOT explained. The menu bar added for
  the Explorer association was the obvious new suspect and is exonerated by
  that repeat, but only by one pass. If it shows up again, look for Qt's
  "Ambiguous shortcut overload" warning first (a menu action sharing a key
  with a `QShortcut` silences BOTH), and do not filter stderr while doing it.
- A monitor that has been switched off can still appear in `app.screens()` with
  an empty geometry, so a screen under 200 px either way is treated as absent.

**Parsec is deliberately NOT detected, and the attempt is worth recording.**
Benn's rule is that focus does not matter in a Parsec session, which would
license taking it -- but `parsecd.exe` runs CONTINUOUSLY as the host whether or
not anyone is connected, so its presence proves nothing: `tasklist` reported it
True with him sitting at the desk, which would have switched the focus
protection off in exactly the case it exists for. `SM_REMOTESESSION` does not
help either, since Parsec drives the real local display rather than opening an
RDP session. Both cases therefore get the conservative behaviour: never take
the focus on the main screen. Required at the desk, merely unnecessary over
Parsec -- the right way round to be wrong.

## Still to do

1. **Grid-first meshing (`_grid_trim`) -- SHIPPED, ON with the iso-lines
   checkbox.** Benn supplied `Body138_from fusion360.obj/.stl` as the target:
   welded, watertight, manifold, 12,002 triangles, every cylinder/cone/torus/
   sphere following the UV exactly, including on partial faces.

   `_grid_trim` does what Fusion does and is the REVERSE of `_lattice_remesh`:
   it lays a regular UV grid over the untrimmed surface, emits every cell clear
   of the boundary as a quad, and fits only the rim with constrained Delaunay.
   Structure comes from construction, not from recovery -- which is the whole
   point, because recovery is what kept failing. Both return the same
   `(refs, polys, constraints)`, so the welding, validation and fallback
   machinery downstream is shared; the caller tries `_grid_trim` first and the
   lattice second.

   Measured at 0.25/18, both models, HOLES=0: grid_trim accepted on 244/778
   valve faces and 548/1097 handle faces. Body 138's torus faces went from
   7,065 polys / 0 quads to 3,446 / 2,535. Valve quads overall 5% (plain OCCT)
   -> 52%. Serial cost about +36% (50s -> 68s on the valve), absorbed by the
   process pool.

   **The one thing that made it work:** cull grid points against the boundary
   POLYLINE, not just against boundary NODES. A point near the middle of a long
   boundary segment blocks constrained edge recovery, and culling only
   near-coincident nodes left 8 of 20 faces failing recovery on body 138;
   sampling the segments and culling against those dropped it to 3 and took
   quad cells from 62 to 1,584.

   **What is left, in size order** (`GRID_STATS` counts every exit):
     - `no interior` 48% valve / 30% handle -- the face is narrower than a cell,
       so every grid point gets culled and there is nothing to regularise.
       Mostly small faces; check whether they carry real polygon count before
       spending effort here.
     - `patch unsound` ~20% on both -- the validator rejecting the patch. Likely
       dominated by SEAM faces and worth confirming: `_face_loops` gets a single
       node run for a seam edge that the wire traverses twice, so the loop
       doubles back and the constraint set is wrong.
     - **Spheres are still bad and Benn has noticed.** A spherical dimple here
       is a trimmed cap that crosses the seam AND contains the pole: the
       degenerate pole edge has 2 UV nodes at (0,-pi/2) and (2pi,-pi/2) that are
       ONE 3D point, and the seam contributes two more coincident runs. All
       three paths decline. Fusion draws these as concentric rings plus a fan at
       the pole, which is the correct answer and needs the parameter domain
       unwrapped periodically -- the same fix as `patch unsound`.

2. **Edge alignment is SHIPPED** (see the section above). What is left of it:
     - it only plans vertices for faces `_ruled_rows` accepts. Faces that fall
       to OCCT -- spheres with a pole, seam-crossing faces -- neither request
       nor benefit;
     - the two rings of a band still have their own node counts, so the rungs
       are aligned but the ring spacing is still OCCT's. Uniform re-sampling of
       whole edges (`GCPnts_UniformAbscissa`, measured segment ratio 1.0001
       against OCCT's up-to-11:1) is the next step and would also fix the old
       "uneven circular hole" defect. It is a bigger change than augmenting,
       because REPLACING an edge's nodes means every face on it must be rebuilt
       from scratch rather than patched;
     - `BRepMesh` ignores a `Poly_Polygon3D` attached to an edge beforehand --
       tested, it re-discretises anyway (912 nodes -> 71). There is no shortcut
       through OCCT here; the boundary has to be ours.

3. **Quads on trimmed fillets** — wanted, needs item 1.
4. Body-level colour override; per-body (not per-preset) settings if asked.
5. **The trimmed stripes are the last big thing.** Benn's target, and he is
   right that it is the one that matters: every NURBS surface should drive the
   flow of the mesh, as Fusion does. What is left is the genuinely TRIMMED
   stripe -- a fillet whose long edge is cut by other geometry, so its boundary
   is not two rings plus two ends (body 158 face 3: 19 nodes on one ring, 47 on
   the other, 30 wandering). No ruled band can take those. His proposal is the
   right architecture and is what `_grid_trim` already attempts: mesh the
   UNTRIMMED surface regularly, discard what falls outside, then stitch the
   boundary back on, adding vertices where a joint needs them. `_grid_trim` now
   takes 9 of 41 faces on body 158; the exits to attack are `patch unsound`
   (12) and `no interior` (14). Reference meshes for comparison:
   `Body138/Body60/Body158 from fusion360.stl`.
6. **Body 54's fillet, and bodies 60/45/31.** Benn reports the UV flow is
   ignored on parts of these -- one side of a small fillet carries much larger
   triangles than the rest. Body 60 is diagnosed (four B-spline fillets whose
   structured candidate is built but rejected by the sag gate, see above);
   54/45/31 are not yet looked at. His framing is worth keeping: the worry is
   not the polygons, it is that the UV is sometimes ignored entirely, which
   means the logic is not yet nailed down.
6. Export currently writes every body; hidden bodies are not excluded — decide
   whether hiding should affect export.
6. **Known pre-existing defect** (not lattice-related): THREE of the handle's
   prototypes -- `飾り枠左_DECO_B`, `SidePanel_Right_sw_003__DECO_B` and
   `SidePanel_Left_sw_002__DECO_B` -- come out with `open=0 wind=2 nonman=1` at
   some settings (e.g. 0.4/25, 0.3/30): two different B-rep faces both put a
   triangle on the same welded edge, at a feature thinner than the deflection.
   No holes, so the mesh is CLOSED; it is flagged in red in the outliner. Not
   repairable by dropping triangles (the four triangles are all distinct).

   **It is OCCT's own, and that has now been measured rather than assumed.**
   `飾り枠左_DECO_B` at 0.4/25 gives `open=0 wind=2 nonman=1` identically on all
   four paths -- everything on, `structured=False`, `structured+auto_grid=False`,
   and fully plain OCCT -- so no fallback in the cascade can help, and the
   structured paths are not implicated.

   Beware when reading the watertight sweep: `watertight` is
   `open_edges == 0 and winding_errors == 0`, so these three report as
   "SOLID LEAKS" in the script at the top of this file even though they have
   **no holes at all**. Sweeping the prescribed pairs on both models gives
   `open=0` everywhere; the only non-clean line is handle 0.4/25, and it is
   these three bodies' winding errors.

## Explorer "Open with", and why there is no .exe (`winassoc.py`)

Benn: "I would like the option to pick up our app when I right click on a step
file > open with... just, dont make a exe file, some people in my company are
uneasy with the windows scare screen on unsigned exe." Plus: nothing that would
impair development, so `run_gui.py` and `run_gui.bat` are untouched.

That rules out every packaging option at once, and the answer is that none was
needed. A Windows file association is just a COMMAND STRING in the registry, so
it can point straight at the venv's interpreter:

    "…\.venv\Scripts\pythonw.exe" "…\run_gui.py" "%1"

which is exactly what `run_gui.bat` already runs. Nothing is built, nothing is
downloaded, and SmartScreen has no new binary to object to.

**It adds an entry; it must NOT take the default.** Registering the ProgId under
`HKCU\Software\Classes\.step\OpenWithProgids` lists the app alongside
SolidWorks and whatever else owns the type. Claiming the default would be both
unwanted and futile: since Windows 10 the default handler is protected by a
`UserChoice` hash that Explorer verifies, so a script that writes it is ignored
by design. Windows' own "Choose another app -> Always" is the supported route,
and `spikes/open_with.py` asserts that we never claim it.

Everything lives under **HKEY_CURRENT_USER**: no admin rights, and per-user,
which is what a tool on a network share needs.

Three things that are easy to miss:

- **`FriendlyAppName` is required.** Without it Windows names the entry after
  the executable in the command -- which here is `pythonw.exe`, so the list
  offers "Python" rather than "STEP to USD".
- **`%1` must be quoted in the registry string.** Benn's own files are named
  things like `8-7 DBXA-80S1A-JIS5K(/1R分岐).STEP`: spaces, full-width
  parentheses and Japanese.
- **The registry holds ABSOLUTE paths, so moving the project silently breaks
  the entry.** `winassoc.status()` compares what is registered against what the
  running copy would register and says so, rather than leaving it to be found
  by a click that does nothing.

`SHChangeNotify(SHCNE_ASSOCCHANGED, ...)` makes Explorer re-read
associations, and it needs **`SHCNF_FLUSH`**: without it the notification is
queued and Explorer keeps serving its CACHED "Open with" menu. Benn reported the
entry missing when the keys were already perfect.

**Prove it with the shell's own API, not with the registry.** "The keys are
right" and "Explorer will offer it" are different claims, and only
`SHAssocEnumHandlers` -- what the shell itself calls to build that list --
answers the second. `winassoc.offered()` wraps it and `status()` reports the
verdict, which is how the missing entry was diagnosed: Windows was already
listing us as a RECOMMENDED handler for both .step and .stp with the display
name "STEP to USD", so nothing was wrong with the registration and the menu was
simply stale.

**The GUI grew a menu bar for this.** Registering is a once-per-machine action a
colleague has to be able to FIND, and a toolbar button for it would be in the
way forever after: Tools -> Add to Windows "Open with"..., with a matching
Remove that is greyed out when nothing is registered. `File` duplicates the
toolbar's actions on purpose, reusing the same `QAction` objects (so the
shortcut is not defined twice) -- which is why `_build_menubar` runs AFTER
`_build_toolbar`.

`run()` now opens a path given on the command line, queued with
`QTimer.singleShot` so the window is up before the loader starts -- otherwise a
big STEP looks like a failure to launch. Note `QKeySequence.Quit` is EMPTY on
Windows, so the Quit action spells out Ctrl+Q.

`spikes/open_with.py` covers the four links separately, because each can fail
alone: the registry keys and that removing them leaves nothing; the command
string and its quoting; `run_gui.py` handing argv through to `run()` (driven as
a real subprocess with `run()` stubbed, so it does not open a window on Benn's
screen); and the app actually loading the file it is handed. It snapshots and
restores whatever was registered before, so running it changes nothing on the
machine.

One test trap worth keeping: the passthrough check compares a raw path against
a **repr** of argv. String-matching those fails on the doubled backslashes and
looks exactly like the path having been lost -- parse it with
`ast.literal_eval` instead.

## Export selected (`write_usd(only=...)`)

Benn: "sometimes I just need 1 or 2 parts in a full assembly". File -> Export
selected... (Ctrl+Shift+S, also on the toolbar, greyed out with nothing
selected). `only` takes the outliner's own keys, `(occurrence path, 0-based
body)`, so the export is exactly what is highlighted.

The parts keep their REAL placement and their real parent components, and
anything with nothing selected below it is dropped. A prototype that is only
partly selected gets a filtered prototype per distinct body subset, so copies
that keep the same bodies still share one -- instancing survives. Body prims
keep their ORIGINAL numbers (`Body_2` stays `Body_2`), so names do not shift
between a full and a partial export.

**Namesakes share a selection key.** Keys are path-based and paths come from
names, so copies with the same name under the same parent have the same key:
on the handle, 102 of 230 occurrences share a path with another (33 groups,
e.g. a row of identical screws). Selecting one selects all of them -- in the
viewport already, and so in the export too. Consistent, but not what "export
THIS screw" means; fixing it means a unique key scheme across hide state,
quality and selection, not a change in the writer.

`spikes/export_selected.py` asserts it on the handle: selected bodies land at
exactly the world positions the full export gives them, a partial occurrence
keeps only its chosen body, and copies reference one prototype. Through
Blender 4.5: 10 meshes, 4 empties, 1 root, 0 objects off scale 1.

## Start menu and desktop shortcuts (`shortcuts.py`)

Benn: "find me a easy way to start this app... a shortcut in start menu and
desktop with a nice icon". `python -m stepusd.shortcuts [create|remove|status]`,
or Tools -> "Create Start menu and desktop shortcuts" for a colleague. Same
rules as the Explorer association: no .exe, per-user, no admin, and the target
is exactly what run_gui.bat runs (`pythonw.exe run_gui.py`), so development is
untouched. The icon is the existing `assets/appicon.ico` (16-256 px).

**The shortcut carries the app's AppUserModelID, and that is why it is built
through C#.** `run()` sets `APP_ID` on the process so the taskbar shows our icon
and not Python's. A shortcut without the SAME ID is a different app to the
taskbar: pin it, launch it, and you get two buttons. `WScript.Shell` cannot set
the property, so `shortcuts.py` compiles a few lines of IShellLink +
IPropertyStore through PowerShell's `Add-Type`; values go in through environment
variables so paths with spaces or Japanese never meet shell quoting. `APP_ID`
lives in shortcuts.py and `run()` imports it -- keep ONE copy of that string.
Verified by reading the ID back through `Shell.Application.ExtendedProperty`.

The desktop is found with `SHGetFolderPath(CSIDL_DESKTOPDIRECTORY)`, not by
joining `~/Desktop`: Benn's is redirected to OneDrive and named in Japanese.
The paths are absolute, so a moved project needs `create` re-run (it overwrites).

## Shipping it: trimming the venv does NOT solve sharing

`spikes/make_dist.py <dir> [--force]` builds a trimmed runnable copy, measured
**1,411 MB -> 846 MB (40% off) with no code change** -- it simply does not copy
files nothing loads. It works: the GUI launches from the trimmed tree, loads a
STEP through the real subprocess loader (verified fully detached under
`pythonw`, no console), tessellates and renders.

**But Benn's verdict is that it misses the point, and he is right.** The only
reason to slim it was easier sharing, and 846 MB is no more emailable than
1.4 GB -- both need a network share or a stick, so the trim crosses no
threshold that changes HOW it is shared. The built copy was deleted. The script
is kept because it is written and costs nothing, and 40% less to copy still
helps if a share deployment ever happens.

**A headless or web-form build is NOT an option.** Benn: "everyone using the app
will absolutely need the UI and the tweak. there is no 'fixed settings' that
will work for every project! every project will need its very specific settings,
part by part." That rules out both the CLI-only build and the browser form, and
it means whatever ships is the whole desktop app -- roughly a 1 GB folder.

So the realistic channel is a folder on a network share, and the delivery route
matters more than the file type: run from an intranet UNC path there is no
Mark-of-the-Web and no SmartScreen, while anything DOWNLOADED trips it whether
it is an .exe, a .bat or a .zip. The only fix for the download case is a code-
signing certificate.

### A shared copy is READ-ONLY, and that used to break it (`paths.py`)

The blocker nobody sees until it is deployed: the geometry cache and
`quality_presets.json` were written next to the code. On a share that is
read-only to everyone but its owner, the first is a hard failure -- every STEP
load writes a cache -- and the second silently swallows the edit, because
`presets.save` catches OSError and moves on.

`stepusd/paths.py` decides where the app may write, once: if its own folder is
writable it behaves EXACTLY as before, so a developer checkout keeps `.cache/`
and `quality_presets.json` where they have always been (which matters -- Benn's
checkout has a warm cache for the valve and the handle). Only a read-only app
folder moves state to `%LOCALAPPDATA%\StepUSD`, where each colleague gets their
own cache and their own presets. The presets that SHIPPED with the app are still
read as the starting point, so a shared deployment can carry Benn's tuned list
and each user can then diverge from it.

`spikes/readonly_deploy.py` asserts both halves, including that saving presets
under the read-only case writes nothing at all into the app folder.

**The first load of any file is SLOW and looks like a hang.** Benn hit this in
the trimmed copy and reported it as broken. It was not: his project has a warm
`.cache` for the valve and handle, so those load instantly, while a fresh copy
has to actually parse a 32 MB STEP behind an indeterminate progress bar. Any
colleague's first open of any file will look the same. Worth remembering before
diagnosing a "hang".

Quitting during that load used to raise a "loader process error:
ProcessError.Crashed" dialog: QProcess reports a deliberate `kill()` as
errorOccurred(Crashed) and then a non-zero exit, and `_fail` published both.
`ProcessLoader.cancel` now sets `_reported` BEFORE killing, so a cancel is
silent.

## Where the weight is, and what is real

`spikes/make_dist.py <dir>` builds a trimmed, runnable copy. Measured
**1,411 MB -> 846 MB (40% off) with no code change at all** -- it simply does
not copy files nothing ever loads. Verified by launching the GUI from the
trimmed tree: loads a STEP, tessellates, renders, reports watertight.

Where the weight is, and what is real:

    PySide6            632 MB   446 MB of it never imported
    vtk.libs + modules 312 MB   REQUIRED, see below
    OCP + ocp.libs     152 MB   required
    scipy              109 MB   required (spatial), monolithic
    matplotlib chain    64 MB   one function -- see below
    pxr (usd-core)      49 MB   required
    numpy              51 MB    required

PySide6 is the easy win: the app imports QtCore, QtGui and QtWidgets, and the
wheel ships an embedded Chromium (`Qt6WebEngineCore.dll` alone is 194 MB), Qt3D,
Quick/QML, Designer, Multimedia and 59 MB of translations.

**Do NOT trim `vtk.libs`.** It looks like the obvious next 77 MB -- viskores
(VTK-m compute filters), charts, volume rendering, exotic IO -- and none of it
is reachable from our code. But **OCP links against VTK**: cadquery-ocp bundles
OCCT's VTK integration, so removing those DLLs breaks `import OCP` with a bare
"DLL load failed" pointing nowhere near the cause. Measured: parking 65 DLLs
saved 77 MB and stopped the app starting. Going further needs a real PE
dependency-closure walk, not a guess at which names look unused.

**matplotlib is 64 MB for one function.** `Path.contains_points` (with a
`radius` margin) at six sites in `tessellate.py`, pulling in PIL, fontTools and
the rest. It is replaceable with a numpy point-in-polygon plus a cKDTree
distance test for the margin -- scipy is already a dependency -- but it is the
culling logic inside the mesher, so it needs the full watertight sweep and the
sag metric before it can be believed. Not done.

## OCCT 8.0 (beta): MEASURED, no gain, and it moves the collection types

Tried because the STEP read is the biggest single phase of a first open.
`cadquery-ocp` 8.0.1.0.0 (OCCT 8.0.1) against our 7.9.3.1.1, same four files,
same reader, in a throwaway venv:

    file      7.9.3 transfer   8.0.1 transfer
    knob             16 ms           17 ms
    motor           346 ms          414 ms     slower
    valve        38,121 ms       37,067 ms     1.03x -- noise
    handle        7,653 ms        7,818 ms

Topology identical on both (3,916 / 9,617 faces, 164 shells, 325 solids), so it
is a fair comparison. The valve's 3% is inside the spread that file shows on ONE
kernel: 35.1 / 35.2 / 38.1 s across runs the same afternoon.

**And it is not free to adopt.** OCCT 8.0 relocates the collection types:
`TopTools_IndexedMapOfShape` becomes
`OCP.collections.IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher`, and the
`TopTools_*` module keeps only `TopTools`, `LocationSet`, `ShapeMapHasher`,
`ShapeSet`. We use that class in **24 places** across `tessellate.py`,
`reader.py`, `gui/main.py` and `gui/stepper.py`. Mechanical, but it touches the
file the watertight guarantee lives in, and the port would owe the full sweep on
both models plus the sag metric afterwards.

So: a rename across the core file, on a beta kernel, for zero measured gain.
**Do not do this** unless something else forces it.

Note the trap if anyone re-tests: `TopTools_IndexedMapOfShape` raising
`ImportError` looks like the class was REMOVED. It was not, it moved -- read
`TopExp.MapShapes_s.__doc__`, which names the map type it now expects.

## The STEP read: where it goes, and why the valve is the outlier

Measured on OCCT 7.9.3, plain `STEPControl_Reader`:

    file     MB    entities    parse    transfer   faces   us/face  yields
    knob    0.1        758     11 ms       15 ms      17      897   1 solid
    motor   1.8     32,452    112 ms      338 ms     895      385   1 solid
    valve  30.2    266,613    803 ms   35,151 ms   3,916    8,976   164 SHELLS, 0 solids
    handle 59.9    571,511  1,517 ms    6,985 ms   9,617      726   243 solids

Parse is 15-18% and transfer the rest. The valve is the outlier by a mile: HALF
the handle's entities and 40% of its faces for FIVE TIMES the time. Cost is
plainly not proportional to geometry -- a 12x spread in us/face -- and the one
thing separating the outlier is that it arrives as `SHELL_BASED_SURFACE_MODEL`,
so OCCT gets 164 open shells to sew rather than ready-made solids.

**Hypothesis, not proven:** most of the valve's 38 s is sewing/healing. The
correlation is consistent (every file yielding solids is 385-897 us/face; the
only file yielding shells is 8,976) but the valve also carries unusually nasty
geometry. Two kernel versions agree to within noise, so it is intrinsic to the
FILE rather than to OCCT's version -- which points at the export settings, not
at anything installable. Re-exporting as AP214/AP242 with solid geometry is the
cheap test, and it is the same change that would give this file its colours and
its assembly hierarchy.

Attempts to settle it by turning OCCT's shape processing down were all no-ops
and all looked like clean negatives -- see the `Interface_Static` trap below.

**Parallelising the transfer is not available.** `STEPControl_Reader` exposes
`TransferEntity` / `TransferOne` / `TransferList`, which look like they would let
each worker parse the file (cheap) and transfer only its share (dear). Measured
on the handle, every candidate returns False and transfers nothing: shape
representations, manifold solids, closed shells, product definitions -- including
`StepBasic_ProductDefinition`, which is the very type that succeeds as the root.
`NbRootsForTransfer` and `xst-transferrable-roots` both report **1**. OCCT treats
the file as one indivisible unit with the assembly inside it, so parallelism
would mean splitting the FILE and rebuilding the assembly tree by hand. Not worth
it: we cache the transfer, so it is paid once per file ever.

### `Interface_Static` parameters: two traps, both of which fake a clean result

- **They do not exist until a reader has been constructed.** The STEP controller
  registers them, so reading or writing them before the first
  `STEPControl_Reader()` silently does nothing. Three "healing off" runs came
  back at exactly the baseline, which reads as a real negative and is not one.
- **String-valued parameters read 0 through `IVal`.** `read.surfacecurve.mode`
  is `'Default'`, `read.step.nonmanifold` is `'Off'`, `read.step.sequence` is
  `'FromSTEP'` -- and `IVal` returns 0 for all of them however they are set. So
  "I set it to 0 and read back 0" proves nothing. Read `CVal` back, and check the
  setter's return value: one of ours returned False and was ignored.

## Style notes for this project

Benn is a professional CAD/3D user (Japanese mechanical assemblies, Blender
downstream). He reads results closely, spots geometry problems from screenshots,
and prefers a plainly stated limitation over a hedge. Report measured numbers,
say plainly when something is not fixed, and never quietly weaken the watertight
guarantee to make another feature work.
