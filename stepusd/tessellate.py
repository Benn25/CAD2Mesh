"""Tessellate an OCCT solid into ONE welded, watertight mesh with faceVarying
normals (sharp at sharp CAD edges, smooth across curved faces, never unwelded).

Welding is done in two passes:

1. Exact coordinate match. Handles the common case, plus seams and poles where
   a face references the same 3D point twice.
2. TOPOLOGICAL weld along shared edges. Real-world CAD (anything translated,
   e.g. SolidWorks -> STEP) often has adjacent surfaces that do not meet
   exactly, so the two faces sharing an edge evaluate its nodes to positions up
   to ~0.1 mm apart -- far too big to weld by proximity without risking
   collapsing genuine thin features. But OCCT discretises each edge ONCE, so
   node k of the edge polygon on face A *is* node k on face B no matter what
   the coordinates say. Unioning by that correspondence seals the mesh exactly,
   with no tolerance to tune.

Without pass 2, a real 164-body assembly left one body with 8 open edges at
coarse settings; with it, every closed solid comes out watertight at every
setting.
"""
from __future__ import annotations
import math, collections
from dataclasses import dataclass, field, replace

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_Orientation, TopAbs_State
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.GeomAbs import GeomAbs_SurfaceType

from . import edgealign
from . import nurbsfix
from OCP.TopTools import (TopTools_IndexedMapOfShape,
                          TopTools_IndexedDataMapOfShapeListOfShape)

#: Surfaces we rebuild as a ruled grid. All analytic, so their u behaviour is
#: uniform and the boundary's u sampling stays valid across the interior. Torus
#: matters most in practice: fillets are tori, and OCCT tessellates their
#: interior far more coarsely than their edges, leaving huge triangles down the
#: middle of every fillet.
#: diagnostics for the lattice path; see spikes/ for how it is read
LATTICE_STATS = collections.Counter()

#: Surfaces the structured paths will try. NOT just the analytic ones: a
#: SolidWorks fillet is very often exported as a NURBS patch rather than a
#: torus, and gating on analytic types meant those faces never even reached the
#: grid -- valve body 60 is FOUR B-spline faces and came out at 5184 polygons
#: and ZERO quads, while Fusion draws the same fillets as clean stripes. Fusion
#: is not doing anything clever there; it just meshes the UV of whatever surface
#: it has. Everything downstream is already parameter-space generic
#: (_rows_for_curvature and _cols_for_curvature SAMPLE the surface rather than
#: assuming a formula), so opening this up costs nothing in special cases.
_SAMPLED_BLENDS = frozenset((
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution,
))

BANDED_SURFACES = frozenset((
    GeomAbs_SurfaceType.GeomAbs_Cylinder,
    GeomAbs_SurfaceType.GeomAbs_Cone,
    GeomAbs_SurfaceType.GeomAbs_Torus,
    GeomAbs_SurfaceType.GeomAbs_Sphere,
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution,
))


@dataclass
class MeshParams:
    deflection: float = 0.1      # max surface deviation (linear)
    angle_deg: float = 20.0      # max normal deviation
    min_size: float = -1.0       # min element size (-1 = auto)
    relative: bool = False       # scale deflection per-part size
    parallel: bool = True
    structured: bool = True      # ruled grid on cylinders/cones (see _ruled_rows)
    grid_aspect: float = 1.6     # subdivide along the axis until cells are at
                                 # most this many times longer than wide
    quads: bool = True           # emit quads on the ruled grids instead of
                                 # splitting every cell into two triangles
    # Both OFF by default. Measured at 0.2/25 on both models the two of them
    # move the iso-aligned percentage by under 1% either way -- cylinders not at
    # all -- while costing time and, per Benn, looking WORSE in most places:
    # they only ever run on faces _ruled_rows declined, and their Delaunay rim
    # is visibly messier than what OCCT would have left there. Left in place and
    # switchable, but earn their keep before turning either back on.
    lattice: bool = False
    grid_trim: bool = False  # grid-first remesh (see _grid_trim)
    #: Add vertices to shared edges so a band's rungs land on real boundary
    #: nodes instead of jogging at the rim (see stepusd/edgealign.py). OFF by
    #: default: measured at 0.2/30 it buys cylinder 84.0 -> 85.5% and cone
    #: 69.0 -> 74.9% iso-aligned for 9% more polygons and **2.4x the time**
    #: (valve 34s -> 81s), and Benn's repeated verdict looking at real meshes is
    #: that it makes things worse more often than better. Kept and switchable.
    align_edges: bool = False
    #: Let SMALL LONG FILLETS dictate their own rungs, and make their
    #: neighbours conform. Benn's framing, and the priority is his: a blend is
    #: where an imperfection shows as a broken normal, so it should be meshed
    #: as close to a regular ladder as possible and everything else should
    #: adapt to it. This is `align_edges` restricted to the faces that actually
    #: need it -- applied to every band it cost 2.4x the time for a mixed
    #: result, which is why that switch is off.
    #:
    #: MEASURED AND TURNED OFF. Scoring only the flagged fillet faces at 0.2/25
    #: and 0.5/35 on both models: the valve is unchanged (quads 76.7 -> 77.1%,
    #: slivers 28.2 -> 27.7%) for +70% tessellation time, and the handle trades
    #: slivers 42.1 -> 37.3% for a WORSE size spread and a worst vertex valence
    #: of 21 -> 113, i.e. a fan -- the exact defect this was meant to remove.
    #: The reason is structural: augmenting an edge makes the rungs LAND on
    #: boundary nodes but leaves the column spacing as OCCT sampled it, so a
    #: denser uneven ring is still uneven. A regular ladder needs the long edges
    #: RE-SAMPLED uniformly, which replaces their nodes instead of adding to
    #: them and so forces every face on the edge to be rebuilt. Kept switchable;
    #: `_fillet_of` / `is_priority_fillet` are good building blocks for that.
    fillet_first: bool = False
    #: The apex-outward cone fan (`_cone_fan`). On by default -- it is what
    #: makes drill bottoms clean -- but it can emit a patch that only turns out
    #: to be non-manifold after the GLOBAL weld, which no per-face check can
    #: see. Benn's knob: 24 non-manifold edges from ONE small cone, which sent
    #: the whole body to the plain fallback and cost it every other structured
    #: face. It is therefore the FIRST thing the cascade drops.
    cone_fan: bool = True
    #: User-forced loop cuts: a tuple of (face index, "U"|"V", parameter).
    #: Each becomes a real B-rep EDGE along that isocurve before meshing, so
    #: the tessellation is obliged to run a line of vertices along it exactly
    #: as it would along any other edge. Benn's tool for pinning down a bad
    #: face. A tuple, not a list, so `_signature` stays hashable and the mesh
    #: cache notices when a cut is added or removed.
    iso_cuts: tuple = ()
    fillet_max_r: float = 8.0      # mm; above this a blend has rows to spare
    fillet_min_aspect: float = 3.0  # length / width before it counts as "long"
    #: choose the axis-aligned grid per FACE by measuring, instead of forcing it
    #: on every banded face (see _grid_wins)
    auto_grid: bool = True
    #: how that choice is weighted: 1.0 = judge only on how closely the mesh
    #: follows the surface's UV grid, 0.0 = judge only on how regular the faces
    #: are in shape and size. Benn's call, and he is right that the ideal
    #: balance is not universal -- it depends on the part.
    grid_bias: float = 0.5         # re-mesh TRIMMED cylinders/cones on a u-rail
                                 # lattice so their edges follow the iso-lines.
                                 # Every rebuilt patch is proved sound before
                                 # use (_patch_is_sound) and otherwise falls
                                 # back to OCCT, so this cannot leak.

    def to_occt(self) -> IMeshTools_Parameters:
        p = IMeshTools_Parameters()
        p.Deflection = self.deflection
        p.Angle = math.radians(self.angle_deg)
        p.MinSize = self.min_size
        p.Relative = self.relative
        p.InParallel = self.parallel
        p.AllowQualityDecrease = True   # REQUIRED: else coarsening is ignored
        # REQUIRED for fillets. By default OCCT only enforces the surface
        # deflection on some surfaces, and tori (i.e. every fillet) overshoot:
        # measured 0.497 mm against a requested 0.2 mm, showing up as a few
        # huge triangles down the middle of an otherwise fine fillet. With this
        # on, nothing exceeds the requested deflection.
        p.EnableControlSurfaceDeflectionAllSurfaces = True
        return p


@dataclass
class Mesh:
    """A welded polygon mesh. `faces` holds triangles AND quads: the ruled
    grids on cylinders/cones come out as quads, which is both what Blender
    users want to edit and half as many primitives."""
    points: list = field(default_factory=list)     # welded positions
    faces: list = field(default_factory=list)      # tuples of 3 or 4 indices
    normals: list = field(default_factory=list)    # faceVarying, one per corner
    watertight: bool = False
    open_edges: int = 0
    winding_errors: int = 0
    euler: int = 0
    dropped_degenerate: int = 0
    topo_welds: int = 0        # vertices merged by pass 2
    removed_flaps: int = 0     # zero-volume back-to-back triangle pairs
    structured_faces: int = 0  # faces re-triangulated as an axis-aligned grid
    repaired_faces: int = 0    # faces OCCT under-meshed, re-triangulated by us
    coarsened: int = 0         # body re-meshed coarser to dodge an OCCT defect
    cone_fan_fallbacks: int = 0  # body re-meshed with the cone fan switched off
    seam_split: int = 0        # a full-revolution face was cut along a generatrix
    iso_cuts_applied: int = 0  # user-forced loop cuts honoured on this body
    #: forced cuts thrown away because they cost the body its watertightness
    iso_cuts_dropped: int = 0
    #: forced cuts that the splitter accepted but which changed nothing
    iso_cuts_ineffective: int = 0
    nurbs_retry: int = 0       # body re-meshed with its analytic tags dropped
    #: Faces OCCT under-meshed that we could NOT honestly repair -- the surface
    #: is missing area and any seal over it is invented. This is the one defect
    #: that used to be completely silent: a folded repair patch is topologically
    #: sealed, so `watertight` stayed True while the solid was visibly wrong.
    surface_defects: int = 0
    #: WHICH faces those are (1-based B-rep indices), so a fix can be surgical
    defect_faces: list = field(default_factory=list)
    #: Surface type per B-rep face, 1-BASED to match `face_source` (index 0 is
    #: a placeholder). Free to collect -- the mesher already asks every face for
    #: its type -- and it is what the "by surface type" colour mode reads.
    face_kinds: list = field(default_factory=list)
    lattice_faces: int = 0     # trimmed faces rebuilt on a u-rail lattice
    lattice_fallbacks: int = 0
    grid_fallbacks: int = 0
    #: welded index pairs on edges where the surface is NOT G1
    sharp_edges: list = field(default_factory=list)
    align_fallbacks: int = 0 # body re-meshed without the lattice to stay sealed
    filled_holes: int = 0      # holes sealed by the last-resort fill
    face_source: list = field(default_factory=list)  # B-rep face index per polygon
    n_quads: int = 0
    nonmanifold_edges: int = 0 # edges used by >2 triangles (distinct from holes)
    boundary_edges: int = 0    # edges used by <2 triangles == an actual HOLE

    @property
    def genus(self):
        return (2 - self.euler) // 2

    @property
    def tris(self):
        """Triangulated view, for code and checks that want triangles only."""
        out = []
        for f in self.faces:
            if len(f) == 3:
                out.append(f)
            else:
                out.append((f[0], f[1], f[2]))
                out.append((f[0], f[2], f[3]))
        return out

    @property
    def tri_face(self):
        """B-rep face index per TRIANGLE (mirrors `tris`)."""
        out = []
        for f, src in zip(self.faces, self.face_source):
            out.extend([src] * (1 if len(f) == 3 else 2))
        return out


class _UnionFind:
    __slots__ = ("p",)

    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.p[max(ra, rb)] = min(ra, rb)
        return True


def check_manifold(mesh: Mesh) -> Mesh:
    """Closed 2-manifold <=> every undirected edge used twice AND every
    directed edge used once (consistent winding)."""
    und, dirc = collections.Counter(), collections.Counter()
    for f in mesh.faces:
        n = len(f)
        for k in range(n):
            u, v = f[k], f[(k + 1) % n]
            und[(u, v) if u < v else (v, u)] += 1
            dirc[(u, v)] += 1
    mesh.boundary_edges = sum(1 for n in und.values() if n < 2)
    mesh.nonmanifold_edges = sum(1 for n in und.values() if n > 2)
    mesh.open_edges = sum(1 for n in und.values() if n != 2)
    mesh.winding_errors = sum(1 for n in dirc.values() if n != 1)
    mesh.euler = len(mesh.points) - len(und) + len(mesh.faces)
    mesh.watertight = (mesh.open_edges == 0 and mesh.winding_errors == 0)
    return mesh


def _iso_curve_points(face, direction, param, n=120, inside_only=True):
    """Sample an isocurve, keeping only what is really inside the trimmed face.

    Returns [(u, v, gp_Pnt), ...] in parameter order.
    """
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.gp import gp_Pnt2d
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    surf = BRep_Tool.Surface_s(face)
    cls = BRepTopAdaptor_FClass2d(face, 1e-7) if inside_only else None
    a, b = (v0, v1) if direction == "U" else (u0, u1)
    out = []
    for i in range(n + 1):
        t = a + (b - a) * i / n
        uv = (param, t) if direction == "U" else (t, param)
        if cls is not None and cls.Perform(gp_Pnt2d(*uv)) != TopAbs_State.TopAbs_IN:
            continue
        out.append((uv[0], uv[1], surf.Value(*uv)))
    return out


def _iso_exit(face, direction, param, at_end, n=120, refine=40):
    """Where the isocurve LEAVES the trimmed face, to full precision.

    Sampling alone is not good enough: at 120 samples over a 191 mm face the
    last inside point sits 1.6 mm short of the boundary, which is both too far
    to identify the edge it crosses and too inaccurate to line the cut up with
    the neighbouring face. So the crossing is bisected.

    Returns (u, v, gp_Pnt) or None.
    """
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.gp import gp_Pnt2d
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    surf = BRep_Tool.Surface_s(face)
    cls = BRepTopAdaptor_FClass2d(face, 1e-7)
    a, b = (v0, v1) if direction == "U" else (u0, u1)

    def at(t):
        return (param, t) if direction == "U" else (t, param)

    def inside(t):
        return cls.Perform(gp_Pnt2d(*at(t))) == TopAbs_State.TopAbs_IN

    ts = [a + (b - a) * i / n for i in range(n + 1)]
    if at_end:
        ts.reverse()
    first_in = next((i for i, t in enumerate(ts) if inside(t)), None)
    if first_in is None:
        return None
    # walk outward from the inside run to the first sample that is OUT
    lo = ts[first_in]
    hi = None
    for t in ts[:first_in][::-1]:
        hi = t
        break
    if hi is None:
        uv = at(lo)                      # the curve reaches the parameter end
        return uv[0], uv[1], surf.Value(*uv)
    for _ in range(refine):
        mid = 0.5 * (lo + hi)
        if inside(mid):
            lo = mid
        else:
            hi = mid
    uv = at(lo)
    return uv[0], uv[1], surf.Value(*uv)


def _nearest_edge(face, pnt, skip=None):
    """The face's boundary edge closest to `pnt`, and that distance."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    vtx = BRepBuilderAPI_MakeVertex(pnt).Vertex()
    best, bestd = None, 1e30
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge_s(exp.Current())
        exp.Next()
        if BRep_Tool.Degenerated_s(e):
            continue
        if skip is not None and e.IsSame(skip):
            continue
        try:
            d = BRepExtrema_DistShapeShape(vtx, e).Value()
        except Exception:
            continue
        if d < bestd:
            best, bestd = e, d
    return best, bestd


#: names for the two kinds of point a cut can snap to, in report order
SNAP_AXIS, SNAP_VERTEX = "axis tangent", "corner"


def _axis_tangent_params(curve, a, b, samples=96, tol=2e-3):
    """Parameters where a curve's tangent runs along a world axis.

    Benn wants a cut to be placeable exactly at the top of a fillet, or on the
    horizontal centreline of a bore -- and those are not corners, they are the
    points where the edge stops climbing in one of the three directions.

    The tangent is a UNIT vector, so "parallel to X" is just "its Y and Z are
    zero"; that perpendicular magnitude is minimised by a ternary search from
    every sampled local minimum, which finds all of them on a full circle (four
    per axis pair) rather than only the best one.
    """
    from OCP.gp import gp_Pnt, gp_Vec
    import math as _m

    def perp(t, k):
        p, v = gp_Pnt(), gp_Vec()
        try:
            curve.D1(t, p, v)
        except Exception:
            return None
        d = (v.X(), v.Y(), v.Z())
        n = _m.sqrt(sum(c * c for c in d))
        if n < 1e-12:
            return None
        rest = [d[i] / n for i in range(3) if i != k]
        return _m.hypot(*rest)

    out = []
    for k in range(3):
        f = [perp(a + (b - a) * i / samples, k) for i in range(samples + 1)]
        for i in range(1, samples):
            if f[i] is None or f[i - 1] is None or f[i + 1] is None:
                continue
            if not (f[i] <= f[i - 1] and f[i] <= f[i + 1]):
                continue
            lo = a + (b - a) * (i - 1) / samples
            hi = a + (b - a) * (i + 1) / samples
            for _ in range(60):            # ternary search on a unimodal dip
                m1 = lo + (hi - lo) / 3.0
                m2 = hi - (hi - lo) / 3.0
                y1, y2 = perp(m1, k), perp(m2, k)
                if y1 is None or y2 is None:
                    break
                if y1 < y2:
                    hi = m2
                else:
                    lo = m1
            t = 0.5 * (lo + hi)
            g = perp(t, k)
            if g is not None and g < tol:
                out.append(t)
    return out


def face_snap_points(shape, face_index):
    """Special points on one face that a loop cut may snap to.

    Two kinds, which are the two Benn asked for: a point on a CURVED edge whose
    tangent is parallel to a world axis, and a VERTEX of the face. Returns
    [(x, y, z, u, v, kind, u_grazes, v_grazes)] with the uv already projected
    onto this face, since what the cut actually needs is the parameter, not the
    point. The two flags say that the corresponding isocurve is TANGENT to an
    edge here and so must not be cut along -- see `grazes`.

    A straight edge is skipped for the first kind on purpose: if it is
    axis-parallel then every one of its points is, so there is no distinguished
    place along it to snap to, and offering one would be arbitrary.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_CurveType
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.gp import gp_Pnt, gp_Vec
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
    if face_index < 1 or face_index > faces.Extent():
        return []
    face = TopoDS.Face_s(faces.FindKey(face_index))
    surf = BRep_Tool.Surface_s(face)
    sas = ShapeAnalysis_Surface(surf)
    out, seen = [], []

    def edge_runs_along(edge, which, param, span, samples=11):
        """Does this whole EDGE lie on the isocurve, rather than touch it?

        The distinction Benn drew, and it is the difference between a cut that
        works and one that opens the body:

          * a circular edge TANGENT to the isocurve shares its direction at one
            point and leaves immediately -- the cut would be pinched there;
          * a rib's base edge lying ALONG the isocurve stays on it for its whole
            length -- there is simply nothing to cut on that stretch, and the
            place is exactly where a modeller wants the loop.

        Asked directly: walk the edge and see whether the parameter that the
        cut holds constant stays constant. Touching at a point fails on the
        very next sample; lying along it never fails.
        """
        try:
            c = BRepAdaptor_Curve(edge)
            a, b = c.FirstParameter(), c.LastParameter()
            if not (b > a):
                return False
            tol = max(span * 1e-4, 1e-9)
            for i in range(samples):
                q = sas.ValueOfUV(c.Value(a + (b - a) * i / (samples - 1)),
                                  1e-4)
                here = q.X() if which == "U" else q.Y()
                if abs(here - param) > tol:
                    return False
            return True
        except Exception:
            return False

    def grazes(u, v, edges):
        """Which isocurve through (u, v) merely TOUCHES an edge here.

        A cut tangent to a boundary meets it at a single point instead of
        crossing it, which splits the face at a point and leaves the mesher a
        hole -- measured on the knob, a plane whose v-isocurve is tangent to a
        circular edge: open=1 nonmanifold=2 where every other parameter on the
        same face is clean. Returns (u-isocurve grazes, v-isocurve grazes);
        note the U-DIRECTION cut holds u constant and therefore runs along dv.

        An edge that runs ALONG the isocurve is NOT a graze -- see
        `edge_runs_along`. `apply_iso_cuts` skips those stretches instead.
        """
        bad_u = bad_v = False
        try:
            p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
            surf.D1(u, v, p, du, dv)
            ub0, ub1, vb0, vb1 = BRepTools.UVBounds_s(face)
        except Exception:
            return False, False
        for run, which, param, span in ((dv, "U", u, ub1 - ub0),
                                        (du, "V", v, vb1 - vb0)):
            n = math.sqrt(run.X() ** 2 + run.Y() ** 2 + run.Z() ** 2)
            if n < 1e-12:
                continue
            r = (run.X() / n, run.Y() / n, run.Z() / n)
            for edge, et in edges:
                if abs(sum(a * b for a, b in zip(r, et))) <= 0.999:
                    continue                    # it crosses: nothing to fear
                if edge_runs_along(edge, which, param, span):
                    continue                    # coincident, which is allowed
                if which == "U":
                    bad_u = True
                else:
                    bad_v = True
        return bad_u, bad_v

    def add(p, kind, edges=()):
        for q in seen:                      # one point, not three coincident
            if q.Distance(p) < 1e-7:
                return
        seen.append(p)
        try:
            uv = sas.ValueOfUV(p, 1e-4)
        except Exception:
            return
        bad_u, bad_v = grazes(uv.X(), uv.Y(), edges)
        out.append((p.X(), p.Y(), p.Z(), uv.X(), uv.Y(), kind, bad_u, bad_v))

    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_ShapeEnum.TopAbs_EDGE, edges)

    def edge_dirs(P, tol=1e-6):
        """(edge, unit tangent) for every edge of this face passing through P."""
        got = []
        for i in range(1, edges.Extent() + 1):
            e = TopoDS.Edge_s(edges.FindKey(i))
            try:
                if BRep_Tool.Degenerated_s(e):
                    continue
                c = BRepAdaptor_Curve(e)
                a, b = c.FirstParameter(), c.LastParameter()
                best = None
                for k in range(129):
                    tt = a + (b - a) * k / 128
                    d = c.Value(tt).Distance(P)
                    if best is None or d < best[0]:
                        best = (d, tt)
                # refine, or a coarse sample misses a long edge's true nearest
                lo, hi = max(a, best[1] - (b - a) / 128), \
                    min(b, best[1] + (b - a) / 128)
                for _ in range(40):
                    m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
                    if c.Value(m1).Distance(P) < c.Value(m2).Distance(P):
                        hi = m2
                    else:
                        lo = m1
                tt = 0.5 * (lo + hi)
                if c.Value(tt).Distance(P) > tol:
                    continue
                pp, vv = gp_Pnt(), gp_Vec()
                c.D1(tt, pp, vv)
                n = math.sqrt(vv.X() ** 2 + vv.Y() ** 2 + vv.Z() ** 2)
                if n > 1e-12:
                    got.append((e, (vv.X() / n, vv.Y() / n, vv.Z() / n)))
            except Exception:
                continue
        return got

    verts = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_ShapeEnum.TopAbs_VERTEX, verts)
    for i in range(1, verts.Extent() + 1):
        try:
            P = BRep_Tool.Pnt_s(TopoDS.Vertex_s(verts.FindKey(i)))
            add(P, SNAP_VERTEX, edge_dirs(P))
        except Exception:
            pass

    for i in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(i))
        try:
            if BRep_Tool.Degenerated_s(edge):
                continue
            c = BRepAdaptor_Curve(edge)
            if c.GetType() == GeomAbs_CurveType.GeomAbs_Line:
                continue
            a, b = c.FirstParameter(), c.LastParameter()
            if not (b > a):
                continue
            for t in _axis_tangent_params(c, a, b):
                pp, vv = gp_Pnt(), gp_Vec()
                c.D1(t, pp, vv)
                n = math.sqrt(vv.X() ** 2 + vv.Y() ** 2 + vv.Z() ** 2)
                add(pp, SNAP_AXIS,
                    [(edge, (vv.X() / n, vv.Y() / n, vv.Z() / n))]
                    if n > 1e-12 else [])
        except Exception:
            continue
    return out


def propagate_iso_cut(shape, face_index, direction, param, max_steps=64):
    """Continue a loop cut across every TANGENT edge until it hits a hard one.

    Benn's ask: "prolongate the cut to any surface that is tangent or curvature
    continuity after the cut edge, and continue again on the next surface until
    you hit a hard edge... we should be able to have a full ring cut".

    Walking rules, and each one is load-bearing:

    * Only cross an edge that `_is_g1` calls tangent. That is exactly the same
      test that decides whether an edge is drawn sharp, so the cut stops where a
      modeller would say the shape stops being smooth.
    * The direction can FLIP on the next face. U on one surface is not U on its
      neighbour -- a cylinder's axis may be a torus's minor circle -- so the
      continuation picks whichever of the two isocurves through the entry point
      runs closest to the direction the cut arrived in.
    * Stop on returning to a (face, direction) already cut. That is what closes
      a full ring instead of walking it forever.

    Returns [(face index, "U"|"V", parameter), ...] including the seed.
    """
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE,
                                   TopAbs_ShapeEnum.TopAbs_FACE, anc)

    def tangent_at(pts, at_end):
        if len(pts) < 2:
            return None
        a, b = (pts[-1][2], pts[-2][2]) if at_end else (pts[0][2], pts[1][2])
        d = (a.X() - b.X(), a.Y() - b.Y(), a.Z() - b.Z())
        n = math.sqrt(sum(c * c for c in d))
        return tuple(c / n for c in d) if n > 1e-12 else None

    # How close two points must be to count as the same place. Scaled to the
    # part, so it means the same thing on a 3 mm screw and a 400 mm motor.
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        _bb = Bnd_Box()
        BRepBndLib.Add_s(shape, _bb, False)
        _x0, _y0, _z0, _x1, _y1, _z1 = _bb.Get()
        close_tol = max(1e-6, math.dist((_x0, _y0, _z0), (_x1, _y1, _z1)) * 1e-4)
    except Exception:
        close_tol = 1e-4

    # Where the seed curve BEGINS, i.e. the far tip from the end we walk first.
    # Coming back to it is what "the line went all the way round" means.
    seed_face = TopoDS.Face_s(faces.FindKey(face_index))
    seed_pts = _iso_curve_points(seed_face, direction, param)
    seed_tips = [seed_pts[-1][2], seed_pts[0][2]] if len(seed_pts) >= 2 else []

    out = [(face_index, direction, float(param))]
    # Keyed on the PARAMETER too, not just the face: a cut may legitimately
    # cross the same surface twice on two different isocurves, and blocking
    # that stopped rings early on shapes that come back round.
    def _key(f, d, pm):
        return (f, d, round(float(pm), 6))

    seen = {_key(face_index, direction, param)}
    global LAST_LOOP_CLOSED
    LAST_LOOP_CLOSED = False
    # walk BOTH ends of the seed curve. `max_steps` counts SURFACES in the
    # finished cut, not steps per end -- that is what the user is setting when
    # they ask how far the line should travel.
    for at_end in (True, False):
        if len(out) >= max_steps or LAST_LOOP_CLOSED:
            break                          # a closed ring needs no second half
        fi, dr, pm = face_index, direction, float(param)
        came_from = None
        entry = None
        for _step in range(max_steps):
            if fi < 1 or fi > faces.Extent():
                PROPAGATE_STATS["stopped: face out of range"] += 1
                break
            face = TopoDS.Face_s(faces.FindKey(fi))
            pts = _iso_curve_points(face, dr, pm)
            if len(pts) < 2:
                PROPAGATE_STATS["stopped: isocurve empty"] += 1
                break
            # Leave by the end AWAY from where the cut came in. Forcing the
            # walk to always exit at pts[-1] made it turn back on itself: the
            # nearest edge was then the one it had just crossed, which is
            # skipped, so the next-nearest was far away and the walk stopped
            # with "no edge at the tip" while the surface carried on happily.
            if entry is not None:
                d_end = entry.Distance(pts[-1][2])
                d_start = entry.Distance(pts[0][2])
                at_end = d_end >= d_start
            tang = tangent_at(pts, at_end)
            tip = _iso_exit(face, dr, pm, at_end)
            if tip is None:
                PROPAGATE_STATS["stopped: no exit point"] += 1
                break
            edge, dist = _nearest_edge(face, tip[2], skip=came_from)
            if edge is None or dist > 1e-4 * max(1.0, abs(tip[2].X()) +
                                                 abs(tip[2].Y()) +
                                                 abs(tip[2].Z())):
                PROPAGATE_STATS["stopped: no edge at the tip"] += 1
                break                      # the curve stops inside the face
            adj = [TopoDS.Face_s(f) for f in anc.FindFromKey(edge)]
            if len(adj) != 2:
                PROPAGATE_STATS[f"stopped: {len(adj)} adjacent faces"] += 1
                break                      # a boundary of the solid
            if not _is_g1(edge, adj):
                PROPAGATE_STATS["stopped: hard edge"] += 1
                break
            nxt = next((f for f in adj if not f.IsSame(face)), None)
            if nxt is None:
                PROPAGATE_STATS["stopped: no other face"] += 1
                break
            ni = faces.FindIndex(nxt)
            if not ni:
                PROPAGATE_STATS["stopped: next face not indexed"] += 1
                break
            # where does the cut enter the next face, and which way does it go?
            try:
                sas = ShapeAnalysis_Surface(BRep_Tool.Surface_s(nxt))
                uv = sas.ValueOfUV(tip[2], 1e-4)
            except Exception:
                PROPAGATE_STATS["stopped: projection failed"] += 1
                break
            best = None
            for cand in ("U", "V"):
                cp = uv.X() if cand == "U" else uv.Y()
                cpts = _iso_curve_points(nxt, cand, cp)
                if len(cpts) < 2:
                    continue
                for end in (True, False):
                    ct = tangent_at(cpts, not end)
                    if ct is None or tang is None:
                        continue
                    dot = sum(a * b for a, b in zip(ct, tang))
                    if best is None or dot > best[0]:
                        best = (dot, cand, cp)
            if best is None or best[0] < 0.2:
                PROPAGATE_STATS["stopped: no continuation"] += 1
                break
            _dot, dr, pm = best
            fi = ni
            # A FULL LOOP: the cut has come back to where it started.
            if seed_tips and any(tip[2].Distance(t) <= close_tol
                                 for t in seed_tips) and len(out) > 2:
                LAST_LOOP_CLOSED = True
                PROPAGATE_STATS["closed the ring"] += 1
                break
            if _key(fi, dr, pm) in seen:
                PROPAGATE_STATS["already cut there"] += 1
                break
            seen.add(_key(fi, dr, pm))
            out.append((fi, dr, float(pm)))
            if len(out) >= max_steps:
                PROPAGATE_STATS["stopped: reached the limit"] += 1
                break
            came_from = edge
            entry = tip[2]                 # where the cut enters the new face
    PROPAGATE_STATS[f"cuts={len(out)}"] += 1
    return out


PROPAGATE_STATS = collections.Counter()

#: True when the last propagation came back to its own start.
LAST_LOOP_CLOSED = False


def _iso_lies_on_an_edge(face, direction, param, samples=7):
    """Does any edge of this face run ALONG the isocurve at `param`?

    The cheap gate in front of `_iso_interior_runs`, and it needs to be cheap
    because it runs per cut on every re-mesh -- which is exactly when the user
    is dragging a slider. Measured before it existed: 35.7 ms per cut, so a
    body carrying an 8-stretch chain paid 285 ms of probing on top of an 88 ms
    tessellation.

    It reads each edge's PCURVE -- the edge already expressed in the face's own
    (u, v) -- so answering "does v stay at 22 along this edge" is a handful of
    2D evaluations with no projection and no classifier.
    """
    from OCP.Geom2d import Geom2d_Curve       # noqa: F401  (documents intent)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    which_u = str(direction).upper().startswith("U")
    span = (u1 - u0) if which_u else (v1 - v0)
    tol = max(abs(span) * 1e-4, 1e-9)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_ShapeEnum.TopAbs_EDGE, edges)
    for i in range(1, edges.Extent() + 1):
        e = TopoDS.Edge_s(edges.FindKey(i))
        try:
            if BRep_Tool.Degenerated_s(e):
                continue
            pc = BRep_Tool.CurveOnSurface_s(e, face, 0.0, 0.0)
            if pc is None:
                continue
            a, b = pc.FirstParameter(), pc.LastParameter()
            if not (b > a):
                continue
            on = True
            for k in range(samples):
                p = pc.Value(a + (b - a) * k / (samples - 1))
                here = p.X() if which_u else p.Y()
                if abs(here - param) > tol:
                    on = False
                    break
            if on:
                return True
        except Exception:
            continue
    return False


def _iso_interior_runs(face, direction, param, samples=400):
    """Where along an isocurve the face is really THERE, as parameter spans.

    Benn's case: a cut placed on a rib's corner runs along that rib's base edge
    for its width, then carries on across open surface to the next one. On the
    coincident stretch there is nothing to cut -- the edge already exists -- and
    handing the splitter a curve that lies on top of a boundary is what makes
    the operation degenerate.

    `BRepTopAdaptor_FClass2d` answers both questions at once: outside the trim
    reads OUT, lying on an existing edge reads ON, and only genuine interior
    reads IN. Returns [(t0, t1), ...] for the IN runs, with each end BISECTED
    onto the transition -- a cut that stops short of the boundary leaves a
    dangling edge that divides nothing, which is the trap `_clean_isoline`
    already documents.

    Also returns whether a sustained ON run was seen, i.e. whether the curve
    lies along an edge at all; the caller keeps its old single-edge behaviour
    when it does not, so nothing that worked before goes through this path.
    """
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.gp import gp_Pnt2d
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    a, b = (v0, v1) if str(direction).upper().startswith("U") else (u0, u1)
    if not (b > a):
        return [], False
    cls = BRepTopAdaptor_FClass2d(face, 1e-7)

    def at(t):
        uv = (param, t) if str(direction).upper().startswith("U") else (t, param)
        return cls.Perform(gp_Pnt2d(*uv))

    IN, ON = TopAbs_State.TopAbs_IN, TopAbs_State.TopAbs_ON
    ts = [a + (b - a) * i / samples for i in range(samples + 1)]
    st = [at(t) for t in ts]

    # a single stray ON is noise on a boundary crossing; a RUN of them is the
    # curve lying along an edge
    on_run = longest = 0
    for x in st:
        on_run = on_run + 1 if x == ON else 0
        longest = max(longest, on_run)
    coincident = longest >= 3
    if not coincident:
        # the runs are only ever consumed on the coincident path, and the
        # bisection below is ~640 more classifier calls
        return [], False

    def edge_between(lo, hi):
        """Bisect for the point where inside-ness changes between lo and hi."""
        for _ in range(40):
            m = 0.5 * (lo + hi)
            if at(m) == IN:
                lo = m
            else:
                hi = m
        return lo

    runs, i = [], 0
    while i <= samples:
        if st[i] != IN:
            i += 1
            continue
        j = i
        while j + 1 <= samples and st[j + 1] == IN:
            j += 1
        t0 = ts[i] if i == 0 else edge_between(ts[i], ts[i - 1])
        t1 = ts[j] if j == samples else edge_between(ts[j], ts[j + 1])
        if t1 > t0:
            runs.append((t0, t1))
        i = j + 1
    return runs, coincident


def _anchored_cut_edges(face, iso, runs, tol):
    """The interior stretches of an isocurve, ANCHORED to existing vertices.

    This is the whole trick, and it took a measurement to find. A cut whose
    curve touches an existing edge is refused by `BRepFeat_SplitShape`, and it
    does not refuse locally -- the ENTIRE cut on that face is lost, which is
    what Benn saw as the prevention "bleeding" far past the bolt it was aligned
    to. Handing over only the interior stretches does not help either: measured
    on `spikes/out/ribbed.step`, eight interior arcs split 43 faces into 43,
    i.e. nothing, whether added as edges, as wires, or (44 GB of RAM later) as
    a compound.

    What OCCT actually wants is TOPOLOGICAL connection, not geometric
    coincidence. An arc whose ends are fresh vertices lying on an existing edge
    is floating as far as the splitter is concerned; the same arc built on the
    existing `TopoDS_Vertex` objects closes a real loop and splits the face --
    43 -> 44 on the same fixture, from that change alone.

    Returns the edge list, or None if any stretch cannot be anchored -- in
    which case the caller keeps the plain full-span edge rather than shipping
    something unproven.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.GeomAPI import GeomAPI_ProjectPointOnCurve
    vm = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_ShapeEnum.TopAbs_VERTEX, vm)
    verts = [TopoDS.Vertex_s(vm.FindKey(i)) for i in range(1, vm.Extent() + 1)]
    if not verts:
        return None
    pts = [BRep_Tool.Pnt_s(v) for v in verts]
    out = []
    for t0, t1 in runs:
        ends = []
        for t in (t0, t1):
            p = iso.Value(t)
            k = min(range(len(pts)), key=lambda i: pts[i].Distance(p))
            if pts[k].Distance(p) > tol:
                return None                # nothing to anchor to: give up
            # the bisection only ever converges to the classifier's own
            # tolerance, so move the end ONTO the vertex before building
            ends.append((verts[k], GeomAPI_ProjectPointOnCurve(
                pts[k], iso).LowerDistanceParameter()))
        (va, pa), (vb, pb) = ends
        if pb < pa:
            (va, pa), (vb, pb) = (vb, pb), (va, pa)
        if not (pb > pa):
            return None
        try:
            out.append(BRepBuilderAPI_MakeEdge(iso, va, vb, pa, pb).Edge())
        except Exception:
            return None
    return out or None


#: how many of the last requested cuts changed nothing at all
LAST_CUTS_INEFFECTIVE = 0


def apply_iso_cuts(shape, cuts):
    """Turn each requested isocurve into a real edge on its face.

    `cuts` is ((face index, "U"|"V", parameter), ...). This is the same
    `BRepFeat_SplitShape` machinery as `split_seam_faces`, with the curve chosen
    by the USER instead of by the seam heuristic -- a forced loop cut, so a
    suspect face can be pinned down and its flow controlled by hand.

    Returns (new shape, {new face index: ORIGINAL face index}) or None. The map
    is not optional: `face_source` carries face indices to the material binding,
    so without it a cut would silently shift every colour after it.
    """
    from OCP.BRepFeat import BRepFeat_SplitShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    if not cuts:
        return None
    CUT_STATS["attempt"] += 1
    try:
        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
        sp = BRepFeat_SplitShape(shape)
        made = 0
        aimed = {}          # {face index: face} the user asked to be cut
        for fi, direction, param in cuts:
            if fi < 1 or fi > faces.Extent():
                CUT_STATS["face out of range"] += 1
                continue
            face = TopoDS.Face_s(faces.FindKey(fi))
            aimed[fi] = face
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            surf = BRep_Tool.Surface_s(face)
            if str(direction).upper().startswith("U"):
                iso, a, b = surf.UIso(param), v0, v1
            else:
                iso, a, b = surf.VIso(param), u0, u1
            # The full span is the proven path and stays the default. It is
            # only replaced where it is known to FAIL: when the curve lies
            # along an existing edge, which loses the whole cut on this face.
            runs, coincident = ((), False)
            if _iso_lies_on_an_edge(face, direction, param):
                runs, coincident = _iso_interior_runs(face, direction, param)
            pieces = None
            if coincident and runs:
                try:
                    from OCP.Bnd import Bnd_Box
                    from OCP.BRepBndLib import BRepBndLib
                    bb = Bnd_Box()
                    BRepBndLib.Add_s(face, bb, False)
                    x0, y0, z0, x1, y1, z1 = bb.Get()
                    tol = max(1e-6, math.dist((x0, y0, z0),
                                              (x1, y1, z1)) * 1e-5)
                except Exception:
                    tol = 1e-3
                pieces = _anchored_cut_edges(face, iso, runs, tol)
            if pieces:
                for e in pieces:
                    sp.Add(e, face)
                    made += 1
                CUT_STATS["anchored around an existing edge"] += 1
                CUT_STATS[f"stretches={len(pieces)}"] += 1
            else:
                if coincident:
                    CUT_STATS["coincident but could not anchor"] += 1
                sp.Add(BRepBuilderAPI_MakeEdge(iso, a, b).Edge(), face)
                made += 1
        if not made:
            return None
        sp.Build()
        if not sp.IsDone():
            CUT_STATS["build failed"] += 1
            return None
        out = sp.Shape()
        if out is None or out.IsNull():
            CUT_STATS["null"] += 1
            return None
        v0_, v1_ = _solid_volume(shape), _solid_volume(out)
        if v0_ > 0 and abs(v1_ - v0_) > v0_ * 0.01:
            CUT_STATS["volume moved"] += 1
            return None
        newmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(out, TopAbs_ShapeEnum.TopAbs_FACE, newmap)
        back = {}
        for oi in range(1, faces.Extent() + 1):
            of = faces.FindKey(oi)
            mod = sp.Modified(of)
            if mod.Extent():
                for nf in mod:
                    ni = newmap.FindIndex(nf)
                    if ni:
                        back[ni] = oi
            else:
                ni = newmap.FindIndex(of)
                if ni:
                    back[ni] = oi
        # Did each cut actually CUT? A split that quietly changes nothing is
        # the worst outcome for the user: the red line is drawn, the mesh is
        # unchanged, and nothing says why. Benn hit exactly that and had to
        # work it out from the picture. `Modified` is the direct question.
        # `Modified` is non-empty even for a face that was left alone, so the
        # question has to be whether the face became MORE THAN ONE face --
        # measured: a cut placed on a face's own boundary edge reports one
        # modified face and no change to the mesh.
        # Counted per FACE, not per cut (`aimed` is keyed on the face
        # index -- `TopoDS_Shape.HashCode` no longer exists in OCCT 7.9, and
        # calling it threw, which silently lost every cut): two cuts on one
        # face both look effective if either of them split it.
        dud = 0
        for face in aimed.values():
            try:
                if sp.Modified(face).Extent() <= 1:
                    dud += 1
            except Exception:
                pass
        if dud:
            CUT_STATS["had no effect"] += dud
        global LAST_CUTS_INEFFECTIVE
        LAST_CUTS_INEFFECTIVE = dud
        CUT_STATS["ok"] += 1
        CUT_STATS[f"cuts={made}"] += 1
        return out, back
    except Exception:
        CUT_STATS["raised"] += 1
        return None


def _remap_faces(m, back):
    """Renumber a mesh's face references back to the ORIGINAL B-rep faces."""
    if not back:
        return
    m.face_source = [back.get(fi, fi) for fi in m.face_source]
    m.defect_faces = [back.get(fi, fi) for fi in m.defect_faces]
    if m.face_kinds:
        kinds = [""] * (max(back.values(), default=0) + 1)
        for new_i, old_i in back.items():
            if new_i < len(m.face_kinds) and old_i < len(kinds):
                kinds[old_i] = m.face_kinds[new_i]
        m.face_kinds = kinds


CUT_STATS = collections.Counter()


def tessellate(shape, params: MeshParams, weld_decimals: int = 7) -> Mesh:
    """Mesh one shape, applying any user-forced loop cuts first.

    The cuts are applied ONCE, up front, and everything below -- the structured
    paths, the fallback cascade, the defect detection -- then works on the cut
    shape as if the user had modelled it that way. Face references are mapped
    back at the end so nothing downstream can tell the difference.
    """
    if params.iso_cuts:
        got = apply_iso_cuts(shape, params.iso_cuts)
        if got is not None:
            cut, back = got
            m = _tessellate_cascade(cut, replace(params, iso_cuts=()),
                                    weld_decimals)
            _remap_faces(m, back)
            m.iso_cuts_applied = len(params.iso_cuts)
            m.iso_cuts_ineffective = LAST_CUTS_INEFFECTIVE
            # A forced cut is a nicety; watertightness is not. A cut that
            # merely GRAZES a boundary edge -- tangent to it rather than
            # crossing it -- splits the face at a single point and leaves the
            # mesher a hole, and no per-face check upstream can see that. So
            # the cuts are the LAST thing the cascade will drop, exactly as
            # `cone_fan` is the first.
            # A closed solid must not gain holes; NOTHING may gain a
            # non-manifold edge. The two are separated because an open shell
            # legitimately has boundary edges, so "has holes" says nothing
            # about it -- but both candidates are counted the same way, so the
            # comparison below is still the right one once it is entered.
            if m.nonmanifold_edges or (_is_closed(shape) and m.boundary_edges):
                plain = _tessellate_cascade(shape,
                                            replace(params, iso_cuts=()),
                                            weld_decimals)
                if (plain.boundary_edges, plain.nonmanifold_edges) < \
                        (m.boundary_edges, m.nonmanifold_edges):
                    plain.iso_cuts_dropped = len(params.iso_cuts)
                    return plain
            return m
    return _tessellate_cascade(shape, params, weld_decimals)


def _tessellate_cascade(shape, params: MeshParams,
                        weld_decimals: int = 7) -> Mesh:
    """One shape (a solid/shell) -> one welded mesh.

    If a structured rebuild leaves a closed solid unsealed, the body is re-meshed
    with less of it: first without the iso-line lattice, then without the ruled
    grid as well. Per-face validation catches most problems, but a rebuilt face
    can still interact badly with a neighbour in ways that show up only at
    particular deflection/angle combinations -- and chasing those one at a time
    is a losing game. This makes the guarantee structural: no structured pass can
    leave a solid worse than the plain OCCT path, at the cost of occasionally
    meshing one body more than once.

    The order matters. The lattice is dropped first because it is the more
    speculative of the two and the grid is what gives fillets their parallel
    lines, so the grid is worth keeping if it alone is sound.
    """
    m = _tessellate_once(shape, params, weld_decimals)
    # A non-manifold edge counts as a reason to retry even though `watertight`
    # ignores it (that flag is open edges and winding only). A structured
    # rebuild can leave a solid sealed but with one edge shared by four
    # triangles, and where the plain path has no such edge the retry is a
    # straight win -- valve body 64 at 0.15/20 and 0.08/12 was exactly this.
    if not _is_closed(shape) or (m.watertight and not m.nonmanifold_edges
                                 and not m.surface_defects):
        return m
    attempts = []
    # Drop the CONE FAN first, before anything else. It is the newest structured
    # path and the only one whose failure mode is invisible until the global
    # weld: on Benn's knob a single small cone produced 24 non-manifold edges,
    # and the old cascade answered that by dropping EVERY structured path, so a
    # body that should have been 1,693 polygons at 79.4% quads came back as 706
    # at 66.3% with half its faces slivers -- which is the anarchic mesh he
    # photographed. Dropping just the fan gives nonman=0 AND keeps the ruled
    # grids, the sphere caps and the quads.
    if params.cone_fan and (m.nonmanifold_edges or m.winding_errors):
        attempts.append(("cone_fan_fallbacks", replace(params, cone_fan=False)))
    # Gentlest step first: drop the added edge vertices but keep the grid. They
    # are the newest thing here and the most likely to be at fault, and losing
    # them only costs rung alignment, not structure.
    if (params.align_edges or params.fillet_first) and params.structured:
        attempts.append(("align_fallbacks",
                         replace(params, align_edges=False,
                                 fillet_first=False)))
    if params.lattice and m.lattice_faces:
        attempts.append(("lattice_fallbacks", replace(params, lattice=False)))
    # Note: NOT gated on m.structured_faces. `structured` also switches on the
    # diagonal flip pass (_flip_toward_axis), which rewires interior edges on
    # every banded face even when no face took the ruled grid. Valve body 64 at
    # 0.15/20 came back with structured_faces=0, winding_errors=2 and one
    # non-manifold edge, and was clean with structured off -- gating on the
    # counter meant no retry was ever queued for it.
    if params.structured:
        attempts.append(("grid_fallbacks",
                         replace(params, lattice=False, structured=False)))
    # A surface defect is OCCT's own under-meshing, so no path of ours can fix
    # it -- but it is deflection-DEPENDENT, and coarser usually works. Measured
    # on Benn's knob, face 4: every deflection from 0.3 down meshes 254 mm2 of
    # an 823 mm2 face, and every deflection from 0.4 up is correct. A slightly
    # coarser body that is RIGHT beats a fine one with a hole in it, so retry
    # progressively coarser rather than shipping the defect.
    # Coarsening the whole body to dodge the defect was tried and REMOVED. It
    # never wins now that the NURBS retry exists -- it was measured worse on its
    # own (the knob went -3.7% -> +17.1% volume error) and it cost three extra
    # full tessellations of the body every time, which is what Benn saw as
    # "loading takes a VERY long time": the failing motor ran SIX passes at
    # 39.1s against 6.1s for the version that meshes cleanly.
    #
    # The one that actually WORKS: take the analytic tag off the surface
    # and let OCCT mesh it as a NURBS instead. Benn's third idea, and the only
    # one of the three that survived measurement. His first -- splitting the
    # seam face in the B-rep -- is what ShapeUpgrade_ShapeDivideClosed does, and
    # it is unreliable: it made the knob WORSE (+19.7% volume) at one split and
    # tore the motor open at two (2473 boundary edges, -32%).
    #
    #     knob    -3.71% -> -0.34%   surface_defects 1 -> 0
    #     motor   -3.05% -> -0.07%
    #
    # It is safe because it changes the PARAMETERISATION, not the shape: volume
    # moves +0.0009% on the knob and -0.03% on the motor, area by 0.0001% and
    # 0.07%, the solid stays BRepCheck-valid, and the face COUNT is unchanged so
    # face_source still lines up with the B-rep faces (colours, GeomSubsets).
    # It costs polygons -- roughly 2-3x on the affected body -- which is why it
    # is a fallback for a detected defect and not the default.

    # Take the BEST result, not the first perfect one. Requiring a fallback to
    # be fully watertight looks safer but is not: two handle side panels carry
    # winding errors on every path, so `watertight` was never true for any of
    # them, and a fallback with ZERO holes was rejected in favour of an original
    # with fifteen. Holes are the cardinal rule, so rank on them first.
    # When a surface defect is present we can afford the REAL test: how far the
    # mesh's volume is from the solid's. It costs one BRepGProp call and only
    # happens on the rare defective body, and it is the only way to tell a
    # coarser mesh that FIXED the defect from one that merely traded it for a
    # badly under-resolved shape. Measured on Benn's knob: the original is
    # -3.7% out and the coarsened retry +17.1%, so the original must win --
    # ranking on the defect counter alone had picked the 17% one.
    want_vol = _solid_volume(shape) if m.surface_defects else 0.0

    def _vol_err(x):
        if want_vol <= 0:
            return 0.0
        return abs(_mesh_volume(x) - want_vol) / want_vol

    def _rank(x):
        # Holes first -- the cardinal rule -- then non-manifold edges. Volume
        # accuracy ranks BELOW both: putting it above traded a clean mesh for a
        # non-manifold one to gain a fraction of a per cent, and the handle
        # picked up new non-manifold bodies at 0.25/18 and 0.15/20 doing it.
        return (x.boundary_edges, x.nonmanifold_edges, round(_vol_err(x), 4),
                x.surface_defects, x.winding_errors)

    # A detected surface defect on a full-revolution face has a KNOWN fix that
    # keeps the analytic surface: cut the face along one interior generatrix so
    # it is no longer periodic. Unlike the NURBS conversion this IMPROVES the
    # topology -- the halves are still cones, so the ruled grid and the quads
    # keep working -- so it is tried first and taken as soon as it comes back
    # clean.
    if m.surface_defects and m.defect_faces:
        got_split = split_seam_faces(shape, m.defect_faces)
        if got_split is not None:
            cutshape, face_back = got_split
            # The cut shape has to go through the SAME fallbacks as anything
            # else. Comparing it against the original with every other path
            # unchanged threw it away on Benn's knob: both carried the cone
            # fan's 24 non-manifold edges, so they tied on that and the plain
            # fallback then beat them both -- discarding the one candidate whose
            # geometry was actually right.
            attempts.insert(0, ("seam_split", params, cutshape, face_back))
            attempts.insert(1, ("seam_split",
                                replace(params, cone_fan=False), cutshape,
                                face_back))

    best, best_field = m, None

    # The NURBS retry (nurbsfix.py) is DISABLED. It fixes the geometry -- the
    # knob went -3.71% -> -0.34% volume error -- and it wrecks the topology,
    # because converting the solid strips the analytic tag off EVERY face, so
    # the ruled grid, the cone fan and quad pairing all stop applying and what
    # comes back is raw Delaunay:
    #
    #     knob   analytic  quads 80.5%  slivers 18.2%
    #     knob   converted quads 21.5%  slivers 28.8%
    #     motor  analytic  quads 80.5%  ->  converted 39.6%, polys 29k -> 53k
    #
    # Benn's verdict on the result was "I cant work with this", and he is right:
    # a correct volume is worthless if the surface is a mess of slivers. This is
    # the same mistake as measuring vertices instead of chords -- the acceptance
    # test (volume) did not measure the thing that matters (topology).
    #
    # Kept in the tree because the DIAGNOSIS is sound and the child-process
    # isolation is reusable: whatever eventually meshes these faces has to be
    # crash-isolated the same way. What it needs is to apply to the ONE bad
    # face, not the whole body -- see "Still to do".
    if False and m.surface_defects:
        got = nurbsfix.attempt(shape, params)
        if got is not None:
            if got.surface_defects and want_vol > 0 and _vol_err(got) <= 0.005:
                got.surface_defects = 0
            if _rank(got) < _rank(best):
                best, best_field = got, "nurbs_retry"
            if (not best.boundary_edges and not best.nonmanifold_edges
                    and not best.surface_defects and best.watertight):
                setattr(best, "nurbs_retry", 1)
                return best

    for attempt in attempts:
        field, sub = attempt[0], attempt[1]
        target = attempt[2] if len(attempt) > 2 else shape
        safe = _tessellate_once(target, sub, weld_decimals)
        # renumber back to the ORIGINAL faces, so face_source keeps meaning the
        # same thing downstream (materials, GeomSubsets, the step debugger)
        if len(attempt) > 3 and attempt[3]:
            back = attempt[3]
            safe.face_source = [back.get(fi, fi) for fi in safe.face_source]
            safe.defect_faces = [back.get(fi, fi) for fi in safe.defect_faces]
            # face_kinds is indexed by the CUT shape's faces; rebuild it against
            # the original numbering or the surface-type colours land on the
            # wrong faces after a split. Both halves share the original's type.
            if safe.face_kinds:
                kinds = [""] * (max(back.values(), default=0) + 1)
                for new_i, old_i in back.items():
                    if new_i < len(safe.face_kinds) and old_i < len(kinds):
                        kinds[old_i] = safe.face_kinds[new_i]
                safe.face_kinds = kinds
        # Disprove the precursor HERE, not at the end: a candidate whose volume
        # matches the solid has no defect, and leaving the flag set stopped the
        # loop breaking on it -- so the body paid for every remaining attempt
        # after the fix had already been found.
        if safe.surface_defects and want_vol > 0 and _vol_err(safe) <= 0.005:
            safe.surface_defects = 0
        if _rank(safe) < _rank(best):
            best, best_field = safe, field
        if not safe.boundary_edges and not safe.nonmanifold_edges                 and not safe.surface_defects and safe.watertight:
            break                       # cannot do better than clean
    if best_field is not None:
        setattr(best, best_field, 1)
    # `surface_defects` is a PRECURSOR -- a face whose triangulation did not
    # reach its own boundary. When the volume was measured (which only happens
    # on a defective body) and the winning mesh matches the solid, that direct
    # evidence outranks the precursor and the flag is disproven. Without this a
    # body the NURBS retry had just FIXED still showed a red SURFACE ERROR:
    # Benn's motor came back at -0.07% volume error still claiming a defect.
    if best.surface_defects and want_vol > 0 and _vol_err(best) <= 0.005:
        best.surface_defects = 0
    return best


def _tessellate_once(shape, params: MeshParams, weld_decimals: int = 7) -> Mesh:
    BRepTools.Clean_s(shape)                      # else a stale finer mesh wins
    BRepMesh_IncrementalMesh(shape, params.to_occt())

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
    LAST_FACE_PATHS.clear()

    # Which shared edges want extra vertices, decided ONCE for the whole body so
    # that every face using an edge builds the same boundary from it.
    edge_plan = {}
    if (params.align_edges or params.fillet_first) and params.structured:
        def _tri_of(f):
            l = TopLoc_Location()
            t = BRep_Tool.Triangulation_s(f, l)
            return None if t is None else (t, l)
        # With align_edges off, ONLY the priority fillets get to ask -- so the
        # cost lands on the faces where regularity actually shows, instead of
        # on every band in the model.
        keep = None
        if not params.align_edges:
            def keep(f):
                return is_priority_fillet(f, params)
        try:
            edge_plan = edgealign.plan(shape, faces, params, _ruled_rows,
                                       _tri_of, face_filter=keep)
        except Exception:
            edge_plan = {}
    aug_chains = {}           # (face index, edge TShape) -> global indices

    points: list = []
    per_face: dict = {}       # face index -> (local_node -> global index)
    raw_tris: list = []       # (g0,g1,g2)
    raw_norms: list = []      # 3 normals per triangle
    raw_face: list = []       # source face index per triangle
    n_structured = 0
    n_repaired = 0
    n_surface_defects = 0
    defect_faces = []
    face_kinds = [""]
    n_lattice = 0
    planar_faces = set()

    for fi in range(1, faces.Extent() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        flipped = (face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED)

        has_uv = tri.HasUVNodes()
        props = BRepLProp_SLProps(BRepAdaptor_Surface(face), 1, 1e-9) if has_uv else None

        # Positional welding is scoped to ONE face. Across faces we weld
        # topologically instead (below). Welding by position globally fuses
        # faces that merely happen to touch -- on features thinner than the
        # deflection that produces non-manifold edges shared by 4 triangles.
        # Within a face, coincident nodes are genuinely the same point: this is
        # what closes seams (cylinder/sphere) and poles.
        weld = {}
        l2g, nrm = {}, {}
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i).Transformed(trsf)
            key = (round(p.X(), weld_decimals), round(p.Y(), weld_decimals),
                   round(p.Z(), weld_decimals))
            gi = weld.get(key)
            if gi is None:
                gi = len(points)
                weld[key] = gi
                points.append([p.X(), p.Y(), p.Z()])
            l2g[i] = gi

            n = (0.0, 0.0, 1.0)
            if has_uv:
                uv = tri.UVNode(i)
                props.SetParameters(uv.X(), uv.Y())
                if props.IsNormalDefined():
                    d = props.Normal().Transformed(trsf)
                    n = (d.X(), d.Y(), d.Z())
            nrm[i] = (-n[0], -n[1], -n[2]) if flipped else n
        per_face[fi] = l2g

        uvx, pntx, aug, aug_inserts, chains = {}, {}, None, {}, {}
        if edge_plan:
            chains, aug_inserts, _ = _augment_boundary(
                face, tri, loc, trsf, edge_plan, l2g, nrm, uvx, pntx, weld,
                points, props, flipped, weld_decimals, tri.NbNodes() + 1)
            if chains:
                aug = {"chains": chains, "uv": uvx, "pnt": pntx}
                for _k, _ch in chains.items():
                    aug_chains[(fi, _k)] = [l2g[r] for r in _ch]

        rows = None
        alt_patch = None       # an iso-line patch that beat the ruled grid
        if params.structured and has_uv:
            stype = BRepAdaptor_Surface(face).GetType()
            if stype in BANDED_SURFACES:
                rows, rows_swapped = _best_rows(face, tri, loc, params, aug)
                if rows is None:
                    LAST_FACE_PATHS[fi] = "ruled declined"
                elif params.auto_grid and not _grid_wins(
                        face, tri, rows, params, uvx, swap=rows_swapped):
                    rows = None          # OCCT's own mesh is better here
                    LAST_FACE_PATHS[fi] = "ruled lost to plain"
                else:
                    LAST_FACE_PATHS[fi] = ("ruled/swap" if rows_swapped
                                           else "ruled")
                    # "Decide per face" has to judge ALL the candidates, not
                    # just each one against plain OCCT. If the ruled grid wins
                    # here, build the iso-line patch too and take whichever
                    # scores better -- otherwise the iso path could never be
                    # chosen on a face the ruled grid also happens to accept.
                    if (params.auto_grid
                            and (params.grid_trim
                                 or stype == GeomAbs_SurfaceType.GeomAbs_Sphere)):
                        ruled_score = LAST_RULED_SCORE[0]
                        LAST_GRID_SCORE[0] = -1.0
                        alt = _grid_trim(face, tri, loc, params)
                        if alt is not None and LAST_GRID_SCORE[0] > ruled_score:
                            alt_patch = alt
                            rows = None
                            AUTO_GRID_STATS["iso beat ruled"] += 1
                            LAST_FACE_PATHS[fi] = "grid_trim (beat ruled)"
        if rows is not None:
            n_structured += 1
            gsurf = BRep_Tool.Surface_s(face)
            mat = []
            for rv, row in rows:
                out_row = []
                for u, item, *rest in row:
                    if item is not None:
                        out_row.append((l2g[item], nrm[item], u))
                    else:
                        # a row may carry its own v per point: see _side_rows,
                        # where a band whose two end edges are out of phase gets
                        # rows that drift gently from one end's level to the
                        # other's instead of being rejected outright
                        v = rest[0] if rest else rv
                        # rows may be in transposed parameters (see _ruled_rows)
                        eu, ev = (v, u) if rows_swapped else (u, v)
                        pt = gsurf.Value(eu, ev).Transformed(trsf)
                        # go through the SAME per-face weld map as the boundary
                        # nodes: on a closed cylinder u=umin and u=umax are the
                        # same 3D point (the seam), and skipping this leaves the
                        # interior rows split open along it.
                        pkey = (round(pt.X(), weld_decimals),
                                round(pt.Y(), weld_decimals),
                                round(pt.Z(), weld_decimals))
                        gi = weld.get(pkey)
                        if gi is None:
                            gi = len(points)
                            weld[pkey] = gi
                            points.append([pt.X(), pt.Y(), pt.Z()])
                        nn = (0.0, 0.0, 1.0)
                        if props is not None:
                            props.SetParameters(eu, ev)
                            if props.IsNormalDefined():
                                dd = props.Normal().Transformed(trsf)
                                nn = (dd.X(), dd.Y(), dd.Z())
                        if flipped:
                            nn = (-nn[0], -nn[1], -nn[2])
                        out_row.append((gi, nn, u))
                mat.append(out_row)
            for r in range(len(mat) - 1):
                for poly in _ladder(mat[r], mat[r + 1], params.quads):
                    # Transposing (u, v) is a REFLECTION, so a face read the
                    # swapped way comes out wound backwards. Body 158's
                    # structured attempt had 122 winding errors from exactly
                    # this, which made the cascade throw the whole body back to
                    # plain OCCT -- every structured face on it, silently lost.
                    if flipped != rows_swapped:
                        poly = tuple(reversed(poly))
                    ids = tuple(e[0] for e in poly)
                    nms = tuple(e[1] for e in poly)
                    # Two rows may share an end vertex, where the two sides of a
                    # stripe carry different node counts and one had to repeat.
                    # Collapse the duplicate: the cell is a triangle there.
                    if len(set(ids)) != len(ids):
                        keep = [k for k in range(len(ids))
                                if ids[k] != ids[(k - 1) % len(ids)]]
                        if len(keep) < 3:
                            continue
                        ids = tuple(ids[k] for k in keep)
                        nms = tuple(nms[k] for k in keep)
                    raw_tris.append(ids)
                    raw_norms.append(nms)
                    raw_face.append(fi)
        else:
            local = []
            for t in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(t).Get()
                if flipped:
                    a, c = c, a
                local.append((a, b, c))
            # OCCT sometimes fails to mesh a sliver face properly: it emits a
            # triangulation that does not even reach all of the face's own
            # boundary nodes (seen: 13 boundary nodes, 1 triangle). Those nodes
            # are welded to the neighbouring faces, so the neighbours end up
            # with edges used only once -- a real hole in a valid closed solid.
            # Re-triangulate such a face from its boundary loop instead.
            if has_uv:
                before = REPAIR_STATS["undermeshed"]
                repaired = _repair_undermeshed(face, tri, loc, local, flipped)
                if repaired is not None:
                    local = repaired
                    n_repaired += 1
                elif REPAIR_STATS["undermeshed"] > before:
                    # under-meshed AND unrepairable: the face is missing real
                    # surface and nothing we invent here would be honest
                    n_surface_defects += 1
                    defect_faces.append(fi)
            # A trimmed cylinder/cone cannot be re-gridded (its boundary does
            # not follow the iso-lines) but its interior diagonals can still be
            # rewired to run along the axis. This keeps every vertex, so the
            # boundary and the watertight guarantee are untouched.
            stype = BRepAdaptor_Surface(face).GetType() if has_uv else None
            # Remember the surface type per face. Benn's suggestion, and it is
            # the right place: the mesher already asks every face for its type,
            # so the "by surface type" colour mode costs nothing extra.
            while len(face_kinds) <= fi:
                face_kinds.append("")
            try:
                face_kinds[fi] = str(
                    BRepAdaptor_Surface(face).GetType()).split("_")[-1]
            except Exception:
                face_kinds[fi] = ""

            lattice = alt_patch
            # Includes TORUS: a fillet is a torus, and a trimmed one gets no
            # ruled band, so without this it keeps OCCT's unstructured mesh --
            # the "anarchic fillet". Constant-u on a torus runs across the
            # fillet, constant-v along it, so the lattice gives exactly the
            # parallel lines a fillet should have.
            if params.structured and stype in BANDED_SURFACES:
                # Grid-first, then the older recover-by-flipping lattice for the
                # faces it declines. They return the same (refs, polys,
                # constraints) shape, so everything downstream is shared.
                # The two are INDEPENDENT: grid_trim used to be gated behind
                # `lattice`, so switching it on alone did nothing at all.
                # Spheres ALWAYS try the grid, whatever the checkbox says.
                # OCCT fans a spherical cap from its pole, and on this model
                # that produces slivers up to 1230:1 -- which is what Benn sees
                # as "deep peaks and valleys": the vertices are on the sphere to
                # 7e-4 mm, so it is not the geometry, it is shading across
                # needle triangles. Routing spheres through _grid_trim takes the
                # worst aspect to 111 with 10% FEWER polygons. The checkbox stays
                # meaningful for every other surface, where Benn measured the
                # grid path as worse.
                cone_fan_tried = False
                if stype == GeomAbs_SurfaceType.GeomAbs_Sphere:
                    # apex-outward rings first: it is the only path that keeps
                    # triangles near-equilateral all the way to the middle
                    lattice = _sphere_cap(face, tri, loc, params)
                    if lattice is not None:
                        LAST_FACE_PATHS[fi] = "sphere_cap"
                # A cone that reaches its apex -- the bottom of a drilled hole
                # -- is exactly a radial fan of rings, and OCCT makes a mess of
                # it. Like the sphere cap this does not wait for the iso-lines
                # checkbox: there is no case where OCCT's version is better.
                if stype == GeomAbs_SurfaceType.GeomAbs_Cone and params.cone_fan:
                    got_fan = _cone_fan(face, tri, loc, params)
                    if got_fan is not None:   # only on success: a decline must
                        lattice = got_fan     # not discard an existing alt_patch
                        cone_fan_tried = True
                # grid_trim stays OPT-IN. It now scores itself against OCCT's
                # mesh before returning, and even so, switching it on for every
                # banded face measurably hurt: valve cylinders 90.6 -> 82.2% and
                # cones 81.0 -> 72.5% iso-aligned, and the handle picked up two
                # more non-manifold bodies. Benn has said three times that this
                # path makes things worse, and the measurement agrees with him.
                # It is kept for the case it is genuinely needed -- a TRIMMED
                # stripe, which nothing else can mesh -- and for spheres, where
                # OCCT's pole fan is worse than anything.
                if lattice is None and (
                        params.grid_trim
                        or stype == GeomAbs_SurfaceType.GeomAbs_Sphere):
                    lattice = _grid_trim(face, tri, loc, params)
                if lattice is None and params.lattice:
                    lattice = _lattice_remesh(face, tri, loc, params)
            if lattice is not None:
                LAST_FACE_PATHS.setdefault(
                    fi, "cone_fan" if cone_fan_tried else "grid_trim/lattice")
                # Trimmed cylinder/cone rebuilt on a u-rail lattice: its edges
                # follow the iso-lines even though its boundary does not.
                refs, polys, constraints = lattice
                gsurf2 = BRep_Tool.Surface_s(face)
                res = []
                for r in refs:
                    if isinstance(r, int):
                        res.append((l2g[r], nrm[r]))
                    else:
                        u, v = r
                        pt = gsurf2.Value(u, v).Transformed(trsf)
                        pk = (round(pt.X(), weld_decimals),
                              round(pt.Y(), weld_decimals),
                              round(pt.Z(), weld_decimals))
                        gi = weld.get(pk)
                        if gi is None:
                            gi = len(points)
                            weld[pk] = gi
                            points.append([pt.X(), pt.Y(), pt.Z()])
                        nn = (0.0, 0.0, 1.0)
                        if props is not None:
                            props.SetParameters(u, v)
                            if props.IsNormalDefined():
                                dd = props.Normal().Transformed(trsf)
                                nn = (dd.X(), dd.Y(), dd.Z())
                        if flipped:
                            nn = (-nn[0], -nn[1], -nn[2])
                        res.append((gi, nn))
                mapped, norms_m = [], []
                for poly in polys:
                    ids = tuple(res[i][0] for i in poly)
                    nms = tuple(res[i][1] for i in poly)
                    if flipped:
                        ids = tuple(reversed(ids))
                        nms = tuple(reversed(nms))
                    mapped.append(ids)
                    norms_m.append(nms)
                # Re-prove the patch on the WELDED indices. Soundness in UV is
                # not enough: on a seam face the two sides are distinct in UV
                # but weld to the SAME 3D vertex, so a patch that is perfect in
                # parameter space can still be degenerate in space.
                gmap = {i: res[i][0] for i in range(len(res))}
                gcons = {(gmap[a], gmap[b]) for a, b in constraints
                         if gmap[a] != gmap[b]}
                # Two BOUNDARY refs welding to one vertex means the face has a
                # seam: PolygonOnTriangulation hands back a single node run for
                # both traversals of it, so the loop we built doubles back on
                # itself. That is fine in UV and wrong in space, and it only
                # shows up once the neighbours are welded on.
                bcount = sum(1 for r in refs if isinstance(r, int))
                seamish = len({gmap[i] for i in range(bcount)}) != bcount
                if not seamish and _patch_is_sound(mapped, gcons):
                    n_lattice += 1
                    raw_tris.extend(mapped)
                    raw_norms.extend(norms_m)
                    raw_face.extend([fi] * len(mapped))
                    LATTICE_STATS["ok"] += 1
                    exp_done = True
                else:
                    LATTICE_STATS["seam" if seamish
                                  else "unsound after weld"] += 1
                    exp_done = False
            else:
                exp_done = False
            if exp_done:
                continue
            # Under `structured` so the fallback cascade can switch it off:
            # the fill is a local rebuild and, like every other rebuild here, it
            # must be droppable if a body comes out unsound.
            if (params.structured and has_uv
                    and stype == GeomAbs_SurfaceType.GeomAbs_Sphere):
                fixed = _sphere_apex_fix(
                    face, tri, loc, params, local, l2g, nrm, weld, points,
                    props, trsf, flipped, weld_decimals,
                    tri.NbNodes() + 1 + len(uvx) + 10, uvx=uvx)
                if fixed is not None:
                    local = fixed
            if aug_inserts:
                woven = None
                if has_uv and chains:
                    woven = _reweave_face(
                        face, tri, loc, params, chains, uvx, flipped,
                        l2g=l2g, nrm=nrm, weld=weld, points=points,
                        props=props, trsf=trsf, weld_decimals=weld_decimals,
                        next_id=tri.NbNodes() + 1 + len(uvx) + 10)
                if woven is not None:
                    local = woven
                else:
                    uvall = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
                             for i in range(1, tri.NbNodes() + 1)} if has_uv else {}
                    uvall.update(uvx)
                    local = _split_boundary_triangles(local, aug_inserts, uvall)
            if params.structured and stype in BANDED_SURFACES:
                uvs = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
                       for i in range(1, tri.NbNodes() + 1)}
                uvs.update(uvx)
                local = _flip_toward_axis(local, uvs,
                                          sa=BRepAdaptor_Surface(face),
                                          max_sag=params.deflection)
            if stype == GeomAbs_SurfaceType.GeomAbs_Plane:
                planar_faces.add(fi)
            for poly in local:
                raw_tris.append(tuple(l2g[v] for v in poly))
                raw_norms.append(tuple(nrm[v] for v in poly))
                raw_face.append(fi)

    m = Mesh()
    if not raw_tris:
        return check_manifold(m)

    # ---- pass 2: topological weld along shared edges --------------------
    uf = _UnionFind(len(points))
    merged = 0
    sharp_pairs = []          # (global index, global index) before relabelling
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE,
                                   TopAbs_ShapeEnum.TopAbs_FACE, e2f)
    for ei in range(1, e2f.Extent() + 1):
        edge = TopoDS.Edge_s(e2f.FindKey(ei))
        chains = []
        for f in e2f.FindFromIndex(ei):
            face = TopoDS.Face_s(f)
            fi = faces.FindIndex(face)
            l2g = per_face.get(fi)
            if l2g is None:
                continue
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is None:
                continue
            # Where we added vertices to this edge, the face's boundary run is
            # the augmented one; pairing the raw polygon runs would miss them
            # and leave the added vertices unwelded, i.e. a hole.
            got = aug_chains.get((fi, edge.TShape()))
            if got is not None:
                chains.append(list(got))
                continue
            poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if poly is None:
                continue
            nodes = poly.Nodes()
            chains.append([l2g[nodes.Value(k)]
                           for k in range(1, poly.NbNodes() + 1)])
        if len(chains) == 2 and not BRep_Tool.Degenerated_s(edge):
            fl = [TopoDS.Face_s(f) for f in e2f.FindFromIndex(ei)]
            if len(fl) == 2 and not _is_g1(edge, fl):
                run = chains[0]
                for k in range(len(run) - 1):
                    if run[k] != run[k + 1]:
                        sharp_pairs.append((run[k], run[k + 1]))
        if BRep_Tool.Degenerated_s(edge):
            # A pole/apex: the whole edge collapses to ONE 3D point, and it has
            # a single adjacent face so the pairwise rule below never sees it.
            # If the surface evaluates its nodes to slightly different points
            # (sloppy translated geometry) the apex splits and leaves a hole --
            # every node here is the same point by definition, so union them.
            flat = [n for ch in chains for n in ch]
            for k in range(1, len(flat)):
                if uf.union(flat[0], flat[k]):
                    merged += 1
            continue
        if len(chains) < 2:
            continue
        base = chains[0]
        for other in chains[1:]:
            if len(other) != len(base):
                continue                     # cannot correspond; leave alone
            # node order may run either way along the edge; pick the pairing
            # whose endpoints are closer
            fwd = _pair_cost(points, base, other)
            rev = _pair_cost(points, base, other[::-1])
            seq = other if fwd <= rev else other[::-1]
            for a, b in zip(base, seq):
                if uf.union(a, b):
                    merged += 1

    # ---- relabel ---------------------------------------------------------
    if merged:
        groups: dict = collections.defaultdict(list)
        for i in range(len(points)):
            groups[uf.find(i)].append(i)
        remap = {}
        newpts = []
        for root, members in groups.items():
            idx = len(newpts)
            if len(members) == 1:
                newpts.append(points[members[0]])
            else:
                # average: the members disagree by less than the deflection,
                # so splitting the difference is within tolerance either way
                n = float(len(members))
                newpts.append([sum(points[k][j] for k in members) / n
                               for j in range(3)])
            for k in members:
                remap[k] = idx
        points = newpts
    else:
        remap = None

    tmp_t, tmp_n, tmp_f = [], [], []
    for poly, ns, fi in zip(raw_tris, raw_norms, raw_face):
        if remap is not None:
            poly = tuple(remap[i] for i in poly)
        # drop repeated corners (seam / pole collapse); a quad that loses one
        # corner is still a valid triangle
        clean, cn = [], []
        for idx, nv in zip(poly, ns):
            if not clean or idx != clean[-1]:
                clean.append(idx)
                cn.append(nv)
        if len(clean) > 2 and clean[0] == clean[-1]:
            clean.pop()
            cn.pop()
        if len(clean) < 3:
            m.dropped_degenerate += 1
            continue
        if len(set(clean)) < len(clean):
            # A quad whose opposite corners welded together is a zero-area
            # bowtie. Its directed edges already cancel in pairs, so dropping
            # the whole polygon keeps every edge balanced -- whereas keeping it
            # produces a non-manifold edge and two winding errors. Consecutive
            # repeats are handled above; this catches the non-adjacent case,
            # which only a quad can have.
            m.dropped_degenerate += 1
            continue
        tmp_t.append(tuple(clean))
        tmp_n.append(tuple(cn))
        tmp_f.append(fi)

    keep = _drop_flaps(tmp_t)
    for t, ns, fi, k in zip(tmp_t, tmp_n, tmp_f, keep):
        if k:
            m.faces.append(t)
            m.normals.extend(ns)
            m.face_source.append(fi)
            if len(t) == 4:
                m.n_quads += 1
        else:
            m.removed_flaps += 1

    # Drop vertices no triangle references. Re-gridding a face discards OCCT's
    # interior nodes, which would otherwise linger as loose vertices in Blender.
    used = sorted({i for f in m.faces for i in f})
    if len(used) != len(points):
        remap2 = {old_i: new_i for new_i, old_i in enumerate(used)}
        points = [points[i] for i in used]
        m.faces = [tuple(remap2[i] for i in f) for f in m.faces]

    if sharp_pairs:
        seen = set()
        for a, b in sharp_pairs:
            if remap is not None:
                a, b = remap[a], remap[b]
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key not in seen:
                seen.add(key)
                m.sharp_edges.append(key)
    m.points = [tuple(p) for p in points]
    if params.quads and planar_faces:
        # Pair planar triangles into quads only NOW, on the welded indices.
        # Doing it per-face beforehand was unsafe: welding can merge two corners
        # of an already-formed quad, turning it into a zero-area bowtie that
        # shows up as a non-manifold edge and two winding errors.
        _pair_planar_quads(m, planar_faces, math.radians(params.angle_deg))
    check_manifold(m)
    if m.boundary_edges and _is_closed(shape):
        # Last-resort safety net. OCCT occasionally leaves a hole in an
        # otherwise valid closed solid -- a sliver face it failed to mesh, or a
        # gap in a face's interior -- and the failure modes vary with the
        # deflection, so chasing each one individually is a losing game. The
        # input is a CLOSED solid, so any remaining boundary loop is by
        # definition a defect and can be capped. Only ever applied to closed
        # solids: a genuine open shell must stay open and be reported.
        m.filled_holes = _fill_holes(m)

    m.topo_welds = merged
    m.structured_faces = n_structured + n_lattice
    m.lattice_faces = n_lattice
    m.repaired_faces = n_repaired
    m.surface_defects = n_surface_defects
    m.defect_faces = defect_faces
    m.face_kinds = face_kinds
    return check_manifold(m)


def _pair_cost(points, a, b) -> float:
    """Squared distance between the two endpoint pairs -- enough to decide
    whether the second chain runs the same way along the edge."""
    tot = 0.0
    for i in (0, -1):
        pa, pb = points[a[i]], points[b[i]]
        tot += ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
                + (pa[2] - pb[2]) ** 2)
    return tot


def _drop_flaps(polys) -> list:
    """Find zero-volume flaps: two triangles on the SAME three vertices with
    OPPOSITE winding, back to back enclosing no volume.

    They appear where a sliver face is thinner than the deflection, so its two
    sides tessellate onto identical triangles. Both members must go: the shared
    edges then drop from 4 uses to 2 and the mesh becomes manifold again.
    Removing them changes neither the enclosed volume nor the silhouette --
    measured areas on a real part were 0.0002-0.001 mm^2.
    """
    def rot(t):
        i = t.index(min(t))
        n = len(t)
        return tuple(t[(i + k) % n] for k in range(n))

    pending: dict = {}
    keep = [True] * len(polys)
    for i, t in enumerate(polys):
        rev = rot(tuple(reversed(t)))
        bucket = pending.get(rev)
        if bucket:
            keep[bucket.pop()] = False
            keep[i] = False
            continue
        pending.setdefault(rot(t), []).append(i)
    return keep


FILLET_STATS = collections.Counter()


def _fillet_of(face, samples: int = 5):
    """(radius, along, across) if this face is a BLEND, else None.

    Looks at the SURFACE, never at the boundary. A fillet is a surface with one
    small, roughly constant principal radius and a much larger extent across it,
    and that is invariant to trimming -- so a long edge split into several
    segments, or a dent eaten out of one side by another feature, changes
    nothing here. Boundary-shape heuristics fail on both.

    For a torus or cylinder the radius is exact and free. Everything else is
    sampled, and a face whose radius wanders by more than 3x is not a blend.
    """
    try:
        sa = BRepAdaptor_Surface(face)
        st = sa.GetType()
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        if not (u1 > u0 and v1 > v0):
            return None
        um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
        eu, ev = (u1 - u0) * 1e-3, (v1 - v0) * 1e-3
        here = sa.Value(um, vm)
        su = here.Distance(sa.Value(um + eu, vm)) / eu
        sv = here.Distance(sa.Value(um, vm + ev)) / ev
        lu, lv = su * (u1 - u0), sv * (v1 - v0)
        if st == GeomAbs_SurfaceType.GeomAbs_Torus:
            r = sa.Torus().MinorRadius()
        elif st == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            r = sa.Cylinder().Radius()
        elif st in _SAMPLED_BLENDS:
            rs = []
            for i in range(samples):
                for j in range(samples):
                    u = u0 + (u1 - u0) * (i + 0.5) / samples
                    v = v0 + (v1 - v0) * (j + 0.5) / samples
                    try:
                        pr = BRepLProp_SLProps(sa, u, v, 2, 1e-7)
                        if not pr.IsCurvatureDefined():
                            continue
                        k = max(abs(pr.MaxCurvature()), abs(pr.MinCurvature()))
                        if k > 1e-9:
                            rs.append(1.0 / k)
                    except Exception:
                        pass
            if len(rs) < samples:
                return None
            rs.sort()
            med = rs[len(rs) // 2]
            lo, hi = rs[len(rs) // 10], rs[-1 - len(rs) // 10]
            if med <= 0 or hi > med * 3.0 or lo < med / 3.0:
                return None
            r = med
        else:
            return None
    except Exception:
        return None
    if r <= 0:
        return None
    return r, max(lu, lv), min(lu, lv)


def is_priority_fillet(face, params) -> bool:
    """A small-radius, long blend -- the faces Benn most needs to be perfect.

    Small radius is not just an aesthetic preference: at 30 degrees a 1 mm
    fillet gets about six segments across the blend, few enough that the eye
    reads every row, so an uneven one shows as a kink in the highlight. A big
    fillet gets enough rows for irregularity to average out.
    """
    if not params.fillet_first:
        return False
    got = _fillet_of(face)
    if got is None:
        return False
    r, along, across = got
    ok = (r <= params.fillet_max_r and across > 0
          and along >= across * params.fillet_min_aspect)
    FILLET_STATS["fillet" if ok else "not fillet"] += 1
    return ok


def _band_closed(sa, umin, umax, vmin, vmax, swap, tol=1e-6, samples=5):
    """Does the band join back onto itself along its long direction?

    NOT `du >= 2*pi`: that assumes the parameter IS an angle. An analytic
    cylinder is parameterised 0..2pi so the old test worked there, but a
    B-spline runs 0..1, so a fillet wrapping the whole way round never
    registered as closed -- it was treated as a partial band, clamped to a
    single span across its width, and the resulting sag got it rejected. Valve
    body 60's four fillets are exactly that. Ask the geometry instead.
    """
    try:
        for k in range(samples):
            t = vmin + (vmax - vmin) * (k + 0.5) / samples
            a = sa.Value(t, umin) if swap else sa.Value(umin, t)
            b = sa.Value(t, umax) if swap else sa.Value(umax, t)
            if a.Distance(b) > tol:
                return False
    except Exception:
        return False
    return True


def _side_rows(side, us, vs, umin, umax, tu, tv, max_slant=3.0):
    """Pair up the nodes on the two END edges of a trimmed band.

    A fillet that stops short of a full revolution is still a rectangle in UV:
    two rings, and two edges at constant u closing the ends. Those end edges
    already carry nodes at intermediate v, put there by OCCT to meet the
    deflection -- so a grid row placed at one of THOSE v levels ends on vertices
    that already exist and that the neighbouring face already shares. No
    T-junction, no crack, and the band gets its horizontal lines.

    The two ends are discretised INDEPENDENTLY, so their levels usually do not
    coincide. Measured on valve face 40 at deflection 0.2: at 20 degrees the two
    ends land on identical levels and the face meshes at 97.9% iso-aligned; at
    25 and 30 degrees both ends still have the same COUNT but are offset in
    phase (0.1142 vs 0.212), and requiring exact agreement threw the face back
    to OCCT's unstructured mesh -- 0 quads, ~45% aligned. That is the whole of
    Benn's "sweet spot between 16 and 20": outside it, this test failed.

    So when the counts match, pair by index and let the row DRIFT from the left
    end's level to the right end's. Both lists are sorted, so paired rows can
    never cross, and the drift is capped at `max_slant` row spacings -- beyond
    that the band really is skewed and we decline. Each row still starts and
    ends on a real boundary node, which is what keeps the face watertight.

    Paired rows can never CROSS, whatever the drift: both level lists are
    sorted, so the gap between consecutive rows is
    (lv[k+1]-lv[k])(1-t) + (rv[k+1]-rv[k])t, positive for every t. `max_slant`
    is therefore a quality bound, not a safety one -- it keeps rows recognisably
    iso-parallel and lets a genuinely skewed band fall through to another path.

    Returns [(v_left, v_right, left node, right node), ...] ordered by v, or
    None if the two ends cannot be corresponded.
    """
    left, right = [], []
    for i in side:
        (left if abs(us[i] - umin) <= tu else right).append(i)
    if not left or not right:
        return None
    left.sort(key=lambda i: vs[i])
    right.sort(key=lambda i: vs[i])

    # The two long sides rarely carry the SAME number of nodes -- OCCT
    # discretises them independently. Benn's framing is the right target: a
    # stripe should have a very similar count down both sides so it can be a
    # regular ladder of quads, and near-equal is the normal case (body 158's
    # face 3 is 47 against 45). Demanding equality threw those away entirely.
    # Walk both by NORMALISED position instead, letting the shorter side repeat
    # a node where it must; the repeat collapses to a triangle at that end,
    # which is exactly how one would close it by hand.
    if len(left) != len(right):
        n = max(len(left), len(right))
        if min(len(left), len(right)) < n * 0.5:
            return None          # too lopsided to be one stripe
        li_, ri_ = [], []
        for k in range(n):
            li_.append(left[min(len(left) - 1, int(k * len(left) / n))])
            ri_.append(right[min(len(right) - 1, int(k * len(right) / n))])
        left, right = li_, ri_
    lv = [vs[i] for i in left]
    rv = [vs[i] for i in right]
    lo = min(lv[0], rv[0])
    hi = max(lv[-1], rv[-1])
    spacing = (hi - lo) / max(len(lv), 1)
    limit = max(spacing * max_slant, tv)
    out = []
    for k, (li, ri) in enumerate(zip(left, right)):
        if abs(lv[k] - rv[k]) > limit:
            return None                 # genuinely skewed, not just out of phase
        out.append((lv[k], rv[k], li, ri))
    return out


RULED_STATS = collections.Counter()


def _ruled_rows(face, tri, loc, params, rel_tol: float = 1e-3,
                abs_tol: float = 3e-4, snap_frac: float = 0.5,
                max_rows: int = 400, report: dict | None = None, aug=None,
                swap: bool = False):
    """Rebuild a cylindrical/conical face as a ruled GRID whose edges run along
    the axis, subdivided so cells are not ribbon-thin.

    On a cylinder or cone a line of constant u is a straight generatrix parallel
    to the axis, so a grid in (u,v) gives the axis-aligned edges Fusion produces.
    OCCT instead Delaunay-triangulates the (u,v) rectangle, which meets every
    tolerance but has no structure.

    Only the BOUNDARY nodes are shared with neighbouring faces, so OCCT's
    interior nodes are ours to throw away and replace with our own lattice.
    That matters: cones almost always carry interior nodes, and requiring a face
    to have none left them entirely unstructured (measured 8% of cone triangles
    with an axis-parallel edge, versus 77% for cylinders).

    Discarding interior nodes costs no accuracy here: both surfaces are ruled
    along v, so a straight span between the two rings lies exactly on the
    surface. Deflection is governed by the u sampling, which comes from the
    boundary rings and is preserved untouched.

    Returns [(v, [(u, local_node_or_None), ...]), ...] -- None entries are new
    interior points to evaluate. Returns None if the face is not a simple band.
    """
    n = tri.NbNodes()
    if n < 4:
        RULED_STATS["too few nodes"] += 1
        return None

    def _pnt(r):
        return aug["pnt"][r] if (aug and r in aug["pnt"]) else tri.Node(r)

    # Boundary nodes = those on the face's edges, EXCLUDING seams. A seam edge
    # is shared by this face with itself, not with a neighbour, so its nodes are
    # ours to move like any interior node. Cones in particular get extra nodes
    # along the seam at intermediate v; treating those as boundary made every
    # cone fail the "simple band" test and left them unstructured.
    bnodes = set()
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        exp.Next()
        if BRep_Tool.IsClosed_s(edge, face):
            continue                    # seam: internal to this face
        chain = aug["chains"].get(edge.TShape()) if aug else None
        if chain is not None:
            # this edge carries vertices we added, so its boundary run is the
            # augmented one, not what OCCT triangulated
            bnodes.update(chain)
            continue
        poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if poly is None:
            RULED_STATS["no edge polygon"] += 1
            return None
        nodes = poly.Nodes()
        for kk in range(1, poly.NbNodes() + 1):
            bnodes.add(nodes.Value(kk))
    if len(bnodes) < 4:
        RULED_STATS["few boundary nodes"] += 1
        return None

    # Keyed by REF, not position: a ref is normally a triangulation node index,
    # but with edge alignment on it can also be a vertex we added to a shared
    # edge, which has no slot in the triangulation. See _augment_boundary.
    # `swap` transposes the parameter roles. A band does not always run along
    # u: valve body 60's fillets are B-spline patches whose rings are the
    # CONSTANT-U edges (60 and 58 nodes) while v=vmin and v=vmax hold 2 nodes
    # each -- the corners. Read the other way round this is an ordinary band,
    # and everything below works unchanged.
    if swap:
        us = {i: tri.UVNode(i).Y() for i in range(1, n + 1)}
        vs = {i: tri.UVNode(i).X() for i in range(1, n + 1)}
    else:
        us = {i: tri.UVNode(i).X() for i in range(1, n + 1)}
        vs = {i: tri.UVNode(i).Y() for i in range(1, n + 1)}
    if aug:
        for r, (au, av) in aug["uv"].items():
            us[r] = av if swap else au
            vs[r] = au if swap else av
    bv = [vs[i] for i in bnodes]
    bu = [us[i] for i in bnodes]
    vmin, vmax = min(bv), max(bv)
    umin, umax = min(bu), max(bu)
    du, dv = umax - umin, vmax - vmin
    if du <= 0 or dv <= 0:
        RULED_STATS["degenerate uv"] += 1
        return None
    # This tolerance absorbs OCCT's own noise, not just float error: the nodes
    # of a boundary ring come from the edge's curve-on-surface, so their v is
    # only good to ~1e-4 -- one fillet ring measured 4.7124 AND 4.7125 on what
    # is a single circle. At 1e-6 that reads as "the boundary wanders", and
    # every ring-shaped fillet was rejected.
    #
    # But the noise is ~1e-4 ABSOLUTE, not relative, so it cannot be scaled by
    # dv alone: one cylinder here has dv = 12.09, where a relative 1e-3 is 1.2e-2
    # -- loose enough to pull genuinely intermediate nodes into a ring, where
    # sorting them by u scrambles the boundary order and tears the face open
    # (5 holes on two handle bodies). Hence the absolute cap.
    tv = min(dv * rel_tol, abs_tol)

    tu = du * rel_tol
    bot, top, side = [], [], []
    for i in bnodes:
        v = vs[i]
        if abs(v - vmin) <= tv:
            bot.append(i)
        elif abs(v - vmax) <= tv:
            top.append(i)
        elif abs(us[i] - umin) <= tu or abs(us[i] - umax) <= tu:
            side.append(i)              # on an end edge: a trimmed band
        else:
            RULED_STATS["boundary wanders"] += 1
            return None

    # A POLE is a legitimate ring of one point. A spherical cap is bounded by a
    # circle and a degenerate edge at the pole, whose nodes are distinct in UV
    # (u runs 0..2pi at v = -pi/2) but are ONE point in space. Rejecting it sent
    # every cap to OCCT, which fans it into the mess Benn photographed. Collapse
    # such a ring to a single entry and _ladder fans to it on its own -- with a
    # row of one, its walk degenerates into exactly the triangle fan a pole
    # wants, and the latitude rings above it stay regular.
    def _degenerate(ring):
        if len(ring) < 2:
            return True
        p0 = _pnt(ring[0])
        return all(p0.Distance(_pnt(i)) <= 1e-7 for i in ring[1:])

    bot_pole = bool(bot) and _degenerate(bot)
    top_pole = bool(top) and _degenerate(top)
    if bot_pole and top_pole:
        RULED_STATS["both rings degenerate"] += 1
        return None
    if bot_pole:
        bot = bot[:1]
    if top_pole:
        top = top[:1]
    if (len(bot) < 2 and not bot_pole) or (len(top) < 2 and not top_pole):
        RULED_STATS["no two rings"] += 1
        return None
    side_rows = _side_rows(side, us, vs, umin, umax, tu, tv) if side else []
    if side_rows is None:
        RULED_STATS["ends do not correspond"] += 1
        return None
    bot.sort(key=lambda kk: us[kk])
    top.sort(key=lambda kk: us[kk])

    widths = []
    for ring in (bot, top):
        for kk in range(len(ring) - 1):
            widths.append(_pnt(ring[kk]).Distance(_pnt(ring[kk + 1])))
    widths = sorted(w for w in widths if w > 0)
    if not widths:
        RULED_STATS["no widths"] += 1
        return None
    cell = widths[len(widths) // 2]

    # Rows needed for SHAPE: on a cylinder or cone the v-isocurve is straight so
    # one span is exact, but on a torus (every fillet) it is an arc and must be
    # subdivided to meet the deflection and angle limits, exactly like OCCT does
    # along the boundary. Without this the grid would cut the corner.
    sa = BRepAdaptor_Surface(face)
    _mid_u = (umin + umax) * 0.5
    if swap:
        n_shape = _cols_for_curvature(sa, _mid_u, vmin, vmax,
                                      params.deflection,
                                      math.radians(params.angle_deg))
    else:
        n_shape = _rows_for_curvature(sa, _mid_u, vmin, vmax,
                                      params.deflection,
                                      math.radians(params.angle_deg))
    # Rows needed for SHAPE of the cells: keep them from being ribbon-thin.
    n_aspect = int(round(dv / max(cell * params.grid_aspect, 1e-12)))
    nrows = int(max(1, min(max_rows, max(n_shape, n_aspect))))

    # Subdividing along the axis is only safe on a FULL revolution. On a partial
    # arc the u=umin/u=umax lines are real boundary edges shared with the
    # neighbouring faces, and inserting row vertices on them creates T-junctions
    # the neighbour does not have -- measured 26-44 leaking bodies before this
    # guard. On a closed face those lines are the seam, internal to the face.
    closed_u = _band_closed(sa, umin, umax, vmin, vmax, swap)

    # If the two rings were discretised differently -- a different count, or the
    # same count rotated out of phase -- then NO ladder between them can be all
    # quad, and the band comes out as a screw of diagonals. Measured on body
    # 138: only 3 of 23 full bands match, and the usual case is the same count
    # offset by about half a cell (52 vs 52, 0.0696 rad). We cannot re-sample
    # the rings, they are shared with the neighbouring faces -- but forcing two
    # UNIFORM interior rows puts evenly spaced, axis-parallel lines across the
    # middle and confines the mismatch to the two rim strips. It costs two rows
    # of geometry, which is the right trade: regular topology matters more here
    # than landing exactly on the requested deflection.
    ring_u_bot = sorted(us[i] for i in bot)
    ring_u_top = sorted(us[i] for i in top)
    rings_match = (not bot_pole and not top_pole
                   and len(ring_u_bot) == len(ring_u_top)
                   and all(abs(a - c) <= tu
                           for a, c in zip(ring_u_bot, ring_u_top)))
    if closed_u and not rings_match:
        nrows = max(nrows, 3)

    # Size the rows by MEASURED sag, not by the curvature estimate alone.
    # _spans_along assumes splitting the parameter range evenly splits the arc
    # evenly, which holds for an analytic surface but not for a B-spline: its
    # 0..1 parameter is distributed however the fitter left it, so one span can
    # be far longer than its neighbours and bow past tolerance while the
    # estimate says the count is fine. Valve body 60 came out at 3 rows and
    # 0.33 mm of sag against 0.15 asked, so the whole face was thrown away.
    if closed_u and not side_rows and params.deflection > 0:
        u_probe = [umin + du * (k + 0.5) / 4 for k in range(4)]
        # Probe the CELL DIAGONAL, not the row span. The diagonal is the longest
        # edge the ladder emits and it bows in both directions at once; sizing
        # rows off the row span alone left body 60 at 0.33 mm against 0.15 asked
        # even though every row span was inside tolerance.
        _ncol = max(1, max(len(bot), len(top)) - 1)
        _ustep = du / _ncol
        for _ in range(8):
            worst = 0.0
            try:
                for k in range(nrows):
                    va = vmin + dv * k / nrows
                    vb = vmin + dv * (k + 1) / nrows
                    vm = 0.5 * (va + vb)
                    for uu in u_probe:
                        ub = uu + _ustep
                        umid = uu + _ustep * 0.5
                        pa = sa.Value(va, uu) if swap else sa.Value(uu, va)
                        pb = sa.Value(vb, ub) if swap else sa.Value(ub, vb)
                        pm = sa.Value(vm, umid) if swap else sa.Value(umid, vm)
                        d = math.sqrt(
                            ((pa.X() + pb.X()) * 0.5 - pm.X()) ** 2
                            + ((pa.Y() + pb.Y()) * 0.5 - pm.Y()) ** 2
                            + ((pa.Z() + pb.Z()) * 0.5 - pm.Z()) ** 2)
                        if d > worst:
                            worst = d
            except Exception:
                break
            if worst <= params.deflection or nrows >= max_rows:
                break
            nrows = min(max_rows,
                        max(nrows + 1,
                            int(nrows * math.sqrt(worst / params.deflection)) + 1))

    wanted_rows = nrows
    if nrows > 1 and not closed_u:
        # A partial band may not invent row vertices on its end edges: they are
        # shared, and a vertex on one side only is a T-junction. Unless the ends
        # already carry nodes (side_rows), we are stuck with a single span --
        # which on a curved fillet sags badly. The way out is to have those
        # vertices ADDED to the end edges up front, which is what
        # `report["wanted_rows"]` tells edgealign to ask for.
        if not side_rows:
            nrows = 1

    bot_row = [(us[i], i) for i in bot]
    top_row = [(us[i], i) for i in top]

    # A trimmed band drives its rows off the end edges rather than off nrows:
    # those levels are the only ones we may use without adding vertices to a
    # shared boundary. Every boundary node ends up in exactly one row, which is
    # what keeps the patch watertight.
    # Interior rows sample u UNIFORMLY. They used to take the UNION of the two
    # rings' u values, and that is what made fillets look erratic: wherever OCCT
    # discretised the two rings differently the union puts two lines a hair
    # apart, so the band gets a sliver row, and a ladder between rows whose u
    # lists differ has to emit triangles instead of quads. Sampling evenly and
    # letting the ladder to each ring absorb the mismatch gives parallel,
    # evenly spaced lines with quads everywhere between the interior rows --
    # only the two rim strips carry triangles. Density follows the denser ring
    # so nothing is lost along u.
    n_cols = max(1, min(4000, max(len(bot), len(top)) - 1))
    uniform = [umin + du * k / n_cols for k in range(n_cols + 1)]

    # Snap a column onto a boundary node when one is already close. A rung
    # across a fillet only looks straight if the ring HAS a vertex where the
    # column meets it; otherwise the cut jogs sideways at the rim, which is very
    # visible on a fillet and is the thing Benn ends up repairing by hand in
    # Blender. We cannot INSERT a vertex on the ring here -- the ring is shared
    # with the neighbouring face, and adding it on one side only is a
    # T-junction, which is a hole -- but where the ring already has a node
    # within a fraction of a cell, moving the column onto it is free and
    # straightens that rung. Snapping to the DENSER ring gets the most of them.
    #
    # snap_frac swept on the valve at 0.2/25 (iso off): 0.0 -> cyl 74.2%, cone
    # 65.1%, 63k quads; 0.4 -> 80.9/73.5, 80k; 0.5 -> 82.9/77.8, 85k quads and
    # FEWER faces; 0.6 -> 83.1/77.9 but quads fall again. 0.5 it is.
    #
    # This only ever fixes one side. Straightening both needs the vertex
    # inserted on the ring and handed to the neighbour as well.
    dense = ring_u_bot if len(ring_u_bot) >= len(ring_u_top) else ring_u_top
    if dense and n_cols > 0:
        snap = du / n_cols * snap_frac
        snapped = []
        for u in uniform:
            best = min(dense, key=lambda w: abs(w - u))
            snapped.append(best if abs(best - u) <= snap else u)
        uniform = sorted(set(snapped))
    if report is not None:
        # the edge-alignment planner needs to know exactly where this face wants
        # its rungs, so it can ask the ring edges for a vertex at each of them
        report["columns"] = list(uniform)
        report["u_range"] = (umin, umax)
        report["v_range"] = (vmin, vmax)
        report["closed_u"] = closed_u
        report["swap"] = swap
        report["wanted_rows"] = wanted_rows
        report["has_side_rows"] = bool(side_rows)
        report["row_levels"] = [vmin + (vmax - vmin) * k / wanted_rows
                                for k in range(1, wanted_rows)]

    if side_rows:
        inner = [u for u in uniform if umin + tu < u < umax - tu]
        span = max(umax - umin, 1e-30)
        rows = [(vmin, bot_row)]
        for vl, vr, li, ri in side_rows:
            pts = [(umin, li)]
            for u in inner:
                t = (u - umin) / span
                pts.append((u, None, vl + (vr - vl) * t))
            pts.append((umax, ri))
            rows.append((0.5 * (vl + vr), pts))
        rows.append((vmax, top_row))
        RULED_STATS["ok trimmed band"] += 1
        return rows

    if nrows <= 1:
        RULED_STATS["ok single span"] += 1
        return [(vmin, bot_row), (vmax, top_row)]

    u_all = uniform
    rows = [(vmin, bot_row)]
    for kk in range(1, nrows):
        v = vmin + dv * kk / nrows
        rows.append((v, [(u, None) for u in u_all]))
    rows.append((vmax, top_row))
    RULED_STATS["ok multi row"] += 1
    return rows


def _rows_for_curvature(sa, u, vmin, vmax, deflection, angle_rad, samples=24):
    """How many spans the v-direction needs to stay within tolerance.

    Measured, not derived: walk the v-isocurve at mid-u and look at how far it
    bows away from its chord and how much its direction turns. A cylinder or
    cone gives 0 for both (its v-isocurve is a straight generatrix) so one span
    is exact; a torus gives the arc's sagitta and sweep, which is what forces a
    fillet to be subdivided across its width.
    """
    return _spans_along(lambda t: sa.Value(u, t), vmin, vmax,
                        deflection, angle_rad, samples)


def _cols_for_curvature(sa, v, umin, umax, deflection, angle_rad, samples=24):
    """The same measurement along u, for the other side of a grid."""
    return _spans_along(lambda t: sa.Value(t, v), umin, umax,
                        deflection, angle_rad, samples)


def _spans_along(evalf, t0, t1, deflection, angle_rad, samples=24):
    """Spans needed along one isocurve to hold both tolerances."""
    dv = t1 - t0
    if dv <= 0:
        return 1
    try:
        pts = [evalf(t0 + dv * k / samples) for k in range(samples + 1)]
    except Exception:
        return 1
    p0, p1 = pts[0], pts[-1]
    chord = p0.Distance(p1)
    if chord <= 1e-12:
        return 1
    # bow: max distance from the chord line
    ax, ay, az = p0.X(), p0.Y(), p0.Z()
    dx, dy, dz = p1.X() - ax, p1.Y() - ay, p1.Z() - az
    inv = 1.0 / (dx * dx + dy * dy + dz * dz)
    bow = 0.0
    for q in pts[1:-1]:
        wx, wy, wz = q.X() - ax, q.Y() - ay, q.Z() - az
        t = (wx * dx + wy * dy + wz * dz) * inv
        cx, cy, cz = wx - t * dx, wy - t * dy, wz - t * dz
        bow = max(bow, math.sqrt(cx * cx + cy * cy + cz * cz))
    # total turning of the polyline
    turn = 0.0
    for k in range(1, samples):
        ux, uy, uz = (pts[k].X() - pts[k-1].X(), pts[k].Y() - pts[k-1].Y(),
                      pts[k].Z() - pts[k-1].Z())
        vx, vy, vz = (pts[k+1].X() - pts[k].X(), pts[k+1].Y() - pts[k].Y(),
                      pts[k+1].Z() - pts[k].Z())
        nu = math.sqrt(ux*ux + uy*uy + uz*uz)
        nv = math.sqrt(vx*vx + vy*vy + vz*vz)
        if nu < 1e-15 or nv < 1e-15:
            continue
        c = max(-1.0, min(1.0, (ux*vx + uy*vy + uz*vz) / (nu * nv)))
        turn += math.acos(c)

    n = 1
    if bow > deflection > 0:
        # sagitta falls off as 1/n^2
        n = max(n, int(math.ceil(math.sqrt(bow / deflection))))
    if angle_rad > 0 and turn > angle_rad:
        n = max(n, int(math.ceil(turn / angle_rad)))
    return n


def _ladder(row_a, row_b, quads: bool = True, tol: float = 1e-9):
    """Triangle strip between two rows of (index, normal, u).

    Advance by the PARAMETER u, not by index position: that is what puts an
    edge along the axis wherever the two rows share a u sample. Advancing by
    normalised index instead makes every edge diagonal and the whole cylinder
    appears to twist.

    Pairing the two rings by index was tried and removed. It does produce all
    quads when the counts match, but the rings are typically rotated out of
    phase by half a cell, and half a cell on a 40 mm radius is ~2.8 mm of shear
    -- on a 1 mm tall band that is a 70 degree tilt. Uniformly skewed quads are
    not better than triangles here. Two rings that disagree cannot be bridged
    cleanly at all; the fix is to stop them disagreeing, which means owning the
    edge discretisation instead of taking OCCT's.
    """
    na, nb = len(row_a), len(row_b)

    out = []
    i = j = 0
    span = max(abs(row_a[-1][2] - row_a[0][2]), 1e-30)
    eps = span * tol
    while i < na - 1 or j < nb - 1:
        if j >= nb - 1:
            out.append((row_a[i], row_a[i + 1], row_b[j]))
            i += 1
            continue
        if i >= na - 1:
            out.append((row_a[i], row_b[j + 1], row_b[j]))
            j += 1
            continue
        ua, ub = row_a[i + 1][2], row_b[j + 1][2]
        if quads and abs(ua - ub) <= eps:
            # both rows advance to the same u: emit ONE quad whose two side
            # edges are exactly axial, instead of splitting it with a diagonal
            out.append((row_a[i], row_a[i + 1], row_b[j + 1], row_b[j]))
            i += 1
            j += 1
        elif ua <= ub:
            out.append((row_a[i], row_a[i + 1], row_b[j]))
            i += 1
        else:
            out.append((row_a[i], row_b[j + 1], row_b[j]))
            j += 1
    return out


def _flip_toward_axis(tris, uv, max_passes: int = 4, sa=None, max_sag=None):
    """Flip interior diagonals so edges run along the axis (constant u in UV).

    Used on trimmed cylindrical/conical faces, whose boundary does not follow
    the iso-lines so a ruled grid cannot be built. Flipping changes only
    CONNECTIVITY -- never the vertex set -- so the shared boundary with
    neighbouring faces, and the watertight guarantee, are untouched.

    An edge is "axial" when its endpoints share a u parameter, so an edge's cost
    is |du|. A flip is taken when it lowers that cost and both resulting
    triangles keep the original orientation; flipping across a concave quad
    would fold the surface over itself.

    **Untouched vertices are not enough.** This decides purely in UV, and a
    diagonal that looks better there can be a chord that cuts straight across
    the curvature -- both endpoints still exactly on the surface, the edge
    between them sunk into the solid. That is a visible crease, and on spheres
    it was severe: with flipping on, 21.5% of sphere edges were outside the
    requested deviation, worst case 2.74 mm against 0.15 asked, versus 0.0% and
    0.10 mm with it off. Torus went 3.1% / 1.72 mm versus 0.0% / 0.14 mm. Benn
    saw it as valleys in the shading long before any test caught it, because
    the tests only ever checked vertices.

    So pass `sa` (the face's surface) and `max_sag` (the deflection): a flip is
    refused when the edge it creates bows further from the surface than the user
    asked for. Without them the old UV-only behaviour is kept.
    """
    if len(tris) < 2:
        return tris

    def area2(p, q, r):
        return ((q[0] - p[0]) * (r[1] - p[1])
                - (q[1] - p[1]) * (r[0] - p[0]))

    def directed(t, x, y):
        return (t[0] == x and t[1] == y) or (t[1] == x and t[2] == y) \
            or (t[2] == x and t[0] == y)

    tris = [tuple(t) for t in tris]
    for _ in range(max_passes):
        edges = {}
        for ti, (a, b, c) in enumerate(tris):
            for x, y in ((a, b), (b, c), (c, a)):
                edges.setdefault((x, y) if x < y else (y, x), []).append(ti)
        changed = False
        touched = set()
        for (e0, e1), owners in edges.items():
            if len(owners) != 2:
                continue                      # boundary or non-manifold: leave
            t1, t2 = owners
            if t1 in touched or t2 in touched:
                continue
            A, B = tris[t1], tris[t2]
            # orient the shared edge as P->Q in A, so B carries Q->P
            if directed(A, e0, e1):
                P, Q = e0, e1
            elif directed(A, e1, e0):
                P, Q = e1, e0
            else:
                continue
            if not directed(B, Q, P):
                continue                      # inconsistent winding: leave it
            b_ = next((v for v in A if v not in (P, Q)), None)
            d_ = next((v for v in B if v not in (P, Q)), None)
            if b_ is None or d_ is None or b_ == d_:
                continue
            if abs(uv[b_][0] - uv[d_][0]) >= abs(uv[P][0] - uv[Q][0]) - 1e-15:
                continue                      # no more axial than what we have
            sgn = area2(uv[P], uv[Q], uv[b_])
            if sgn == 0.0:
                continue
            n1 = area2(uv[P], uv[d_], uv[b_])
            n2 = area2(uv[Q], uv[b_], uv[d_])
            if n1 == 0.0 or n2 == 0.0:
                continue
            if (n1 > 0) != (sgn > 0) or (n2 > 0) != (sgn > 0):
                continue                      # concave quad: the flip would fold
            # Surface test LAST: it costs three evaluations, so let the cheap
            # UV rejects above throw out most candidates first.
            if sa is not None and max_sag is not None:
                ub, vb = uv[b_]
                ud, vd = uv[d_]
                try:
                    pb = sa.Value(ub, vb)
                    pd = sa.Value(ud, vd)
                    pm = sa.Value((ub + ud) * 0.5, (vb + vd) * 0.5)
                except Exception:
                    continue
                if math.sqrt(((pb.X() + pd.X()) * 0.5 - pm.X()) ** 2
                             + ((pb.Y() + pd.Y()) * 0.5 - pm.Y()) ** 2
                             + ((pb.Z() + pd.Z()) * 0.5 - pm.Z()) ** 2) > max_sag:
                    continue                  # the new edge would sink into the solid
            tris[t1] = (P, d_, b_)
            tris[t2] = (Q, b_, d_)
            touched.add(t1)
            touched.add(t2)
            changed = True
        if not changed:
            break
    return tris


def _is_g1(edge, faces, tol_deg=1.0, samples=5):
    """Is the surface tangent-continuous across this edge?

    Compare the two faces' surface normals at points along the shared edge,
    taking each face's orientation into account. A fillet meeting its
    neighbours is G1 and must NOT be marked sharp; a chamfer or a plain
    intersection is not, and must be.

    Measured on the valve, the distribution is cleanly bimodal: 48.6% of shared
    edges sit under 0.5 degrees, 51.4% sit above 2, and NOTHING falls in
    between. So the threshold is not a tuning knob -- anywhere in that gap gives
    the same answer, and 1 degree is comfortably inside it.
    """
    try:
        t0, t1 = BRep_Tool.Range_s(edge)
    except Exception:
        return True
    if not (t1 > t0):
        return True
    lim = math.cos(math.radians(tol_deg))
    props = []
    for f in faces:
        c2d = BRep_Tool.CurveOnSurface_s(edge, f, 0.0, 0.0)
        if c2d is None:
            return True                  # cannot tell: leave it smooth
        props.append((c2d, BRepAdaptor_Surface(f),
                      f.Orientation() == TopAbs_Orientation.TopAbs_REVERSED))
    for k in range(1, samples):
        t = t0 + (t1 - t0) * k / samples
        ns = []
        for c2d, sa, rev in props:
            uv = c2d.Value(t)
            sl = BRepLProp_SLProps(sa, uv.X(), uv.Y(), 1, 1e-9)
            if not sl.IsNormalDefined():
                return True
            d = sl.Normal()
            v = (d.X(), d.Y(), d.Z())
            ns.append((-v[0], -v[1], -v[2]) if rev else v)
        if ns[0][0] * ns[1][0] + ns[0][1] * ns[1][1] + ns[0][2] * ns[1][2] < lim:
            return False
    return True


def _seam_partners(tri, decimals=7):
    """node -> the other triangulation node at the SAME 3D point.

    A seam face carries two UV nodes for every point on the seam (one at u=0,
    one at u=2*pi) with identical coordinates. OCCT hands back only ONE node run
    for the seam edge, but the wire traverses it TWICE -- once up each side --
    so a loop built from that single run doubles back on itself, the constraint
    set is wrong, and the patch is rejected as unsound. That was the biggest
    single exit from _grid_trim, and it is why body 158's big sphere (a full
    revolution, so it has a seam) could not be rebuilt at all.

    The indexed BRep_Tool.PolygonOnTriangulation overload returns its results by
    reference and is unusable from Python, so pair the duplicates by position
    instead and let the caller pick the run that keeps the loop continuous.
    """
    by = {}
    for i in range(1, tri.NbNodes() + 1):
        q = tri.Node(i)
        k = (round(q.X(), decimals), round(q.Y(), decimals), round(q.Z(), decimals))
        by.setdefault(k, []).append(i)
    out = {}
    for v in by.values():
        if len(v) == 2:
            out[v[0]] = v[1]
            out[v[1]] = v[0]
    return out


def _face_loops(face, tri, loc, aug_chains=None):
    """Ordered boundary loops of a face, as lists of local node indices.

    `aug_chains` maps an edge TShape to the refs along it INCLUDING any vertices
    we added, in the same order PolygonOnTriangulation would have given them.
    """
    loops = []
    partners = None
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_WIRE)
    while exp.More():
        wire = TopoDS.Wire_s(exp.Current())
        exp.Next()
        seq = []
        wx = BRepTools_WireExplorer(wire, face)
        ok = True
        while wx.More():
            edge = wx.Current()
            wx.Next()
            seam = BRep_Tool.IsClosed_s(edge, face)
            ids = aug_chains.get(edge.TShape()) if aug_chains else None
            if ids is not None:
                ids = list(ids)
            else:
                poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
                if poly is None:
                    ok = False
                    break
                nd = poly.Nodes()
                ids = [nd.Value(k) for k in range(1, poly.NbNodes() + 1)]
            if edge.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
                ids.reverse()
            if seam and seq:
                # Second traversal of a seam runs up the OTHER side, on the
                # duplicate nodes. Pick whichever run continues the loop.
                if partners is None:
                    partners = _seam_partners(tri)
                alt = [partners.get(i, i) for i in ids]
                if alt != ids:
                    here = (0 if seq[-1] == ids[0] else 1)                         + (0 if ids[-1] in partners else 0)
                    if seq[-1] != ids[0] and seq[-1] == alt[0]:
                        ids = alt
                    elif seq[-1] not in (ids[0], alt[0]):
                        # neither joins directly: take the run whose start is
                        # nearest the current end, in UV
                        try:
                            pu = tri.UVNode(seq[-1])
                            da = abs(tri.UVNode(ids[0]).X() - pu.X())
                            db = abs(tri.UVNode(alt[0]).X() - pu.X())
                            if db < da:
                                ids = alt
                        except Exception:
                            pass
            if seq and ids and seq[-1] == ids[0]:
                ids = ids[1:]
            seq.extend(ids)
        if not ok or len(seq) < 3:
            return None
        if seq[0] == seq[-1]:
            seq.pop()
        # drop consecutive repeats
        clean = [seq[0]]
        for v in seq[1:]:
            if v != clean[-1]:
                clean.append(v)
        if len(clean) >= 3:
            loops.append(clean)
    return loops or None


REPAIR_STATS = collections.Counter()


def _face_area(face) -> float:
    """The B-rep face's true area, for checking a mesh against."""
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, g, 1e-5)
        return abs(g.Mass())
    except Exception:
        return 0.0


SPLIT_STATS = collections.Counter()


def _clean_isoline(face, nu: int = 240, nv: int = 40):
    """A constant-u parameter where the face spans its WHOLE v range unbroken.

    Benn's rule, and the reason it is the right one: the cut has to be a curve
    that lies entirely inside the trimmed face, or the split leaves a dangling
    edge that divides nothing. On a 5-lobed knob only 10 of 180 sampled
    generatrices qualify, in 5 groups -- and the MIDDLE of a group is the
    robust place to cut, not its edge.

    Returns the parameter, or None if the face has no such isoline.
    """
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.gp import gp_Pnt2d
    try:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        cls = BRepTopAdaptor_FClass2d(face, 1e-7)
    except Exception:
        return None
    good = []
    for i in range(nu):
        u = u0 + (u1 - u0) * (i + 0.5) / nu
        ok = True
        for j in range(nv):
            v = v0 + (v1 - v0) * (j + 0.5) / nv
            if cls.Perform(gp_Pnt2d(u, v)) != TopAbs_State.TopAbs_IN:
                ok = False
                break
        if ok:
            good.append(u)
    if not good:
        return None
    runs, cur = [], []
    step = (u1 - u0) / nu
    for u in good:
        if cur and u - cur[-1] > step * 1.5:
            runs.append(cur)
            cur = []
        cur.append(u)
    if cur:
        runs.append(cur)
    runs.sort(key=len, reverse=True)          # the widest clean band
    best = runs[0]
    return best[len(best) // 2]


def _is_full_revolution(face) -> bool:
    """A face whose u range closes the whole way round, so it carries a SEAM."""
    try:
        u0, u1, _v0, _v1 = BRepTools.UVBounds_s(face)
    except Exception:
        return False
    if (u1 - u0) < 2.0 * math.pi - 1e-6:
        return False
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge_s(exp.Current())
        exp.Next()
        if BRep_Tool.IsClosed_s(e, face):
            return True
    return False


def split_seam_faces(shape, face_indices, tol: float = 0.01):
    """Cut the given full-revolution faces along one clean generatrix each.

    Benn's fix, and it is the one that works. OCCT under-meshes a trimmed
    full-revolution cone badly, and every structured path of ours declines it,
    because the face carries a SEAM: its UV boundary loop is not a simple
    polygon. Cutting it along one interior generatrix removes the periodicity
    and the halves are still CONES -- which matters, because that is what the
    NURBS conversion destroyed. Measured on Benn's knob, one cut:

        volume error       -2.90% -> -0.12%
        worst face error   478.1% ->   0.9%
        quads               79.4% ->  84.2%
        slivers             19.1% ->  14.5%
        polygons            1,693 ->  1,623

    Better on every axis at once, which is exactly what the NURBS route was
    not. One cut is enough; more only add polygons.

    Returns (new shape, {new face index: ORIGINAL face index}), or None.

    The map matters and is not bookkeeping: `face_source` carries B-rep face
    indices all the way to `writer._bind_materials`, which reads
    `face_colors[fi - 1]` from the ORIGINAL shape. Splitting a face shifts every
    index after it, so without remapping, a coloured model would silently get
    every material after the cut assigned to the wrong face.
    """
    from OCP.BRepFeat import BRepFeat_SplitShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    SPLIT_STATS["attempt"] += 1
    try:
        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
        sp = BRepFeat_SplitShape(shape)
        cut = 0
        for fi in face_indices:
            if fi < 1 or fi > faces.Extent():
                continue
            face = TopoDS.Face_s(faces.FindKey(fi))
            if not _is_full_revolution(face):
                SPLIT_STATS["not full revolution"] += 1
                continue
            u = _clean_isoline(face)
            if u is None:
                SPLIT_STATS["no clean isoline"] += 1
                continue
            _u0, _u1, v0, v1 = BRepTools.UVBounds_s(face)
            iso = BRep_Tool.Surface_s(face).UIso(u)
            sp.Add(BRepBuilderAPI_MakeEdge(iso, v0, v1).Edge(), face)
            cut += 1
        if not cut:
            return None
        sp.Build()
        if not sp.IsDone():
            SPLIT_STATS["build failed"] += 1
            return None
        out = sp.Shape()
        if out is None or out.IsNull():
            SPLIT_STATS["null result"] += 1
            return None
        # The split must not move the part. This is a topological cut, so the
        # volume is the one thing that cannot legitimately change.
        v_before, v_after = _solid_volume(shape), _solid_volume(out)
        if v_before > 0 and abs(v_after - v_before) > v_before * tol:
            SPLIT_STATS["volume moved"] += 1
            return None
        # Map every NEW face back to the original it came from.
        newmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(out, TopAbs_ShapeEnum.TopAbs_FACE, newmap)
        back = {}
        for oi in range(1, faces.Extent() + 1):
            of = faces.FindKey(oi)
            mod = sp.Modified(of)
            if mod.Extent():
                # TopTools_ListOfShape is directly iterable in OCP; there is no
                # TopTools_ListIteratorOfListOfShape binding to reach for.
                for nf in mod:
                    ni = newmap.FindIndex(nf)
                    if ni:
                        back[ni] = oi
            else:
                ni = newmap.FindIndex(of)
                if ni:
                    back[ni] = oi
        SPLIT_STATS["ok"] += 1
        SPLIT_STATS[f"cuts={cut}"] += 1
        return out, back
    except Exception as _e:
        SPLIT_STATS["raised"] += 1
        SPLIT_STATS[f"raised: {type(_e).__name__}"] += 1
        import os as _os
        if _os.environ.get("STEPUSD_DEBUG_SPLIT"):
            import traceback as _tb
            _tb.print_exc()
        return None


def _solid_volume(shape) -> float:
    """The B-rep solid's true volume, for checking a mesh against."""
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        g = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, g, 1e-6, True)
        return abs(g.Mass())
    except Exception:
        return 0.0


def _mesh_volume(m) -> float:
    """Signed-tetrahedron volume of a closed mesh."""
    v = 0.0
    pts = m.points
    for poly in m.faces:
        a = pts[poly[0]]
        for k in range(1, len(poly) - 1):
            b, c = pts[poly[k]], pts[poly[k + 1]]
            v += (a[0] * (b[1] * c[2] - b[2] * c[1])
                  - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return abs(v) / 6.0


def _repair_undermeshed(face, tri, loc, local, flipped):
    """Re-triangulate a face whose triangulation misses its own boundary nodes.

    Returns replacement triangles (local node indices) or None to keep what
    OCCT produced. Only single-loop faces are repaired: a face with holes needs
    a constrained triangulation, and guessing there would be worse than leaving
    the (rare) problem visible.
    """
    used = {v for t in local for v in t}
    loops = _face_loops(face, tri, loc)
    if not loops:
        return None
    boundary = {v for lp in loops for v in lp}
    if boundary <= used:
        return None                     # nothing missing: OCCT did its job
    # Past here the face IS under-meshed: OCCT's triangulation does not even
    # reach the face's own boundary. Whatever happens next, that is a defect.
    REPAIR_STATS["undermeshed"] += 1
    if len(loops) != 1:
        REPAIR_STATS["rejected: holes"] += 1
        return None
    loop = loops[0]
    uv = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
          for i in range(1, tri.NbNodes() + 1)}
    # Orient the loop counter-clockwise in UV, which is OCCT's convention for a
    # FORWARD face. Wire order alone is not enough: a loop that comes back
    # clockwise makes every repaired triangle face the wrong way, which shows up
    # as winding errors rather than holes.
    sgn = 0.0
    for k in range(len(loop)):
        x1, y1 = uv[loop[k]]
        x2, y2 = uv[loop[(k + 1) % len(loop)]]
        sgn += x1 * y2 - x2 * y1
    if sgn < 0:
        loop = loop[::-1]
    tris = _ear_clip(loop, uv)
    if not tris:
        # Slivers can defeat ear clipping (every candidate ear is degenerate in
        # UV). A fan still uses every boundary vertex exactly once, so each
        # boundary edge ends up used twice overall and the hole is sealed. The
        # face has negligible area, so the shape cost is nil.
        tris = [(loop[0], loop[k], loop[k + 1]) for k in range(1, len(loop) - 1)]
    if not tris:
        return None

    # PROVE the repair before accepting it. This patch REPLACES the whole face,
    # so its area must be the face's area -- and when it is not, the repair has
    # invented folded, self-overlapping geometry.
    #
    # This is the sneakiest failure this program has had. OCCT under-meshes some
    # trimmed conical faces badly (Benn's knob, face 4: 254 of 823 mm2, a
    # visible VOID with 104 open edges). Ear clipping a seam face's UV loop --
    # which is not a simple polygon, because the loop crosses the parameter
    # seam -- then "sealed" it with 3800 mm2 of overlapping triangles: 4.6x the
    # true area. The body came back watertight=True with no error of any kind,
    # so the tool silently shipped a corrupt solid. An honest hole is far better
    # than a sealed lie: refuse, and let the open edges be reported.
    area = 0.0
    for a, b, c in tris:
        try:
            pa, pb, pc = (tri.Node(a), tri.Node(b), tri.Node(c))
        except Exception:
            return None
        ux, uy, uz = pb.X() - pa.X(), pb.Y() - pa.Y(), pb.Z() - pa.Z()
        vx, vy, vz = pc.X() - pa.X(), pc.Y() - pa.Y(), pc.Z() - pa.Z()
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        area += 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)
    want = _face_area(face)
    if want > 0 and (area > want * 1.25 or area < want * 0.75):
        REPAIR_STATS["rejected: area wrong"] += 1
        return None
    REPAIR_STATS["ok"] += 1
    if flipped:
        tris = [(a, c, b) for a, b, c in tris]
    return tris


AUTO_GRID_STATS = collections.Counter()

#: Scores from the last per-face comparison, so the caller can judge all THREE
#: candidates against each other rather than only each against plain OCCT.
#: (ruled, plain) from _grid_wins; grid_trim's own from _grid_trim.
LAST_RULED_SCORE = [0.0, 0.0]
LAST_GRID_SCORE = [0.0]

#: Debug aid: face index -> the path actually taken, for the LAST body meshed.
#: Diagnosing by re-calling _ruled_rows from a script is a good way to fool
#: yourself (it is easy to forget the swap retry, or _grid_wins). Read this
#: instead. Cleared at the start of every _tessellate_once.
LAST_FACE_PATHS = {}


def _max_chord_sag(polys_uv, sa, limit=None, swap=False):
    """Largest gap between an edge's chord and the surface it should follow.

    Checking VERTICES proves nothing: every vertex is on the surface by
    construction, so a vertex test always passes. The error that shows up as a
    visible valley lives in the CHORD -- the straight edge between two vertices
    cuts through the solid, and a long edge across a curved direction can sag
    far past the requested deviation while both endpoints are perfect. Benn
    spotted this from screenshots after a vertex-based check said the mesh was
    fine; measured afterwards, our structured rebuilds were putting 19.8% of
    sphere edges out of tolerance, worst case 17x the requested deflection,
    where plain OCCT was at 0.0%.

    Measured as the distance between the midpoint of the chord and the surface
    point at the midpoint of the PARAMETERS, which is what a deflection
    tolerance actually bounds. Stops early once `limit` is exceeded.
    """
    worst = 0.0
    seen = set()
    for poly in polys_uv:
        k = len(poly)
        for i in range(k):
            a, b = poly[i], poly[(i + 1) % k]
            key = (a, b) if a <= b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            if swap:
                a, b = (a[1], a[0]), (b[1], b[0])
            try:
                pa = sa.Value(a[0], a[1])
                pb = sa.Value(b[0], b[1])
                pm = sa.Value((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
            except Exception:
                return float("inf")
            d = math.sqrt(((pa.X() + pb.X()) * 0.5 - pm.X()) ** 2
                          + ((pa.Y() + pb.Y()) * 0.5 - pm.Y()) ** 2
                          + ((pa.Z() + pb.Z()) * 0.5 - pm.Z()) ** 2)
            if d > worst:
                worst = d
                if limit is not None and worst > limit:
                    return worst
    return worst


def _uv_scales(face):
    """(su, sv): how much arc length one unit of u and of v is worth."""
    try:
        sa = BRepAdaptor_Surface(face)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
        eu = max((u1 - u0) * 1e-3, 1e-9)
        ev = max((v1 - v0) * 1e-3, 1e-9)
        here = sa.Value(um, vm)
        su = here.Distance(sa.Value(um + eu, vm)) / eu
        sv = here.Distance(sa.Value(um, vm + ev)) / ev
    except Exception:
        return None
    if su <= 0 or sv <= 0:
        return None
    return su, sv


def _rows_candidate(rows, params, swap, su, sv):
    """(polys in uv, iso fraction) for what `rows` would emit."""
    probe = []
    for rv, row in rows:
        probe.append([(None, None, u, (rest[0] if rest else rv))
                      for u, item, *rest in row])
    uvp, scaled = [], []
    for r in range(len(probe) - 1):
        for poly in _ladder(probe[r], probe[r + 1], params.quads):
            uvp.append([(e[2], e[3]) for e in poly])
            a, b = (sv, su) if swap else (su, sv)
            scaled.append([(e[2] * a, e[3] * b) for e in poly])
    if not uvp:
        return None
    return uvp, _iso_fraction(scaled)


def _best_rows(face, tri, loc, params, aug):
    """Build the band and, if that reading is no good, try it transposed.

    Taking the first orientation that returns anything is not enough: a band
    read across its short direction still "works". Body 31's face 2 gives 53
    rows of two points each -- one span across the whole fillet -- which sags
    3.47 mm against 0.2 asked, while the transposed reading is 7 rows of 366
    polygons at 0.164 mm.

    But do NOT rank the two on iso-alignment: a coarse degenerate reading scores
    very well on it (few edges, all of them axial) and wins against the right
    answer. That cost the valve 90.6% -> 81.8% on cylinders when tried. The
    natural reading wins unless it is genuinely UNACCEPTABLE -- i.e. it bows
    outside the requested deflection -- which is the one thing that actually
    disqualifies a candidate.
    """
    sa = BRepAdaptor_Surface(face)
    sc = _uv_scales(face)
    fallback = None
    for swap in (False, True):
        rows = _ruled_rows(face, tri, loc, params, aug=aug, swap=swap)
        if rows is None:
            continue
        if fallback is None:
            fallback = (rows, swap)
        if sc is None or params.deflection <= 0:
            return rows, swap
        got = _rows_candidate(rows, params, swap, sc[0], sc[1])
        if got is None:
            continue
        if _max_chord_sag(got[0], sa, limit=params.deflection,
                          swap=swap) <= params.deflection:
            return rows, swap
    if fallback is None:
        return None, False
    return fallback


def _regularity(polys):
    """0..1: how uniform the polygons are in SHAPE and in SIZE.

    Benn's criterion, and it is the right one: following the surface's UV grid
    and having consistent face sizes matter equally, so the per-face choice
    weights them 50/50 rather than ranking on alignment alone. Alignment by
    itself picks badly -- a fillet meshed as small aligned quads down the middle
    with long slivers top and bottom scores WELL on it (the slivers run along
    the iso-lines) and looks terrible.

    Shape is the share of polygons elongated past 4:1; size is how far the
    middle 90% of areas spread around the median.
    """
    if not polys:
        return 0.0
    areas, asp = [], []
    for poly in polys:
        k = len(poly)
        lens = []
        a2 = 0.0
        for i in range(k):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % k]
            lens.append(math.hypot(x2 - x1, y2 - y1))
            a2 += x1 * y2 - x2 * y1
        lens = [d for d in lens if d > 1e-15]
        if len(lens) < 3:
            continue
        asp.append(max(lens) / min(lens))
        areas.append(abs(a2) * 0.5)
    if not areas:
        return 0.0
    areas.sort()
    med = areas[len(areas) // 2] or 1e-30
    lo = areas[int(0.05 * (len(areas) - 1))]
    hi = areas[int(0.95 * (len(areas) - 1))]
    spread = min(1.0, (hi - lo) / (med * 4.0))
    bad = sum(1 for r in asp if r > 4.0) / len(asp)
    return max(0.0, 1.0 - 0.5 * spread - 0.5 * bad)


def _shape_penalty(polys, limit=4.0):
    """Share of polygons that are badly elongated, in the same scaled UV.

    Iso-alignment alone is not enough to choose between two candidates. Benn's
    case on body 138: with the iso path on, a concave fillet came out as small
    aligned quads down the middle and "stupidly long triangles" top and bottom
    -- which scores WELL on alignment, because those long triangles run along
    the iso-lines, and looks terrible. With the iso path off the same face is
    perfect. So the score has to see element shape too.
    """
    if not polys:
        return 1.0
    bad = 0
    for poly in polys:
        k = len(poly)
        lens = []
        for i in range(k):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % k]
            d = math.hypot(bx - ax, by - ay)
            if d > 1e-15:
                lens.append(d)
        if len(lens) >= 3 and max(lens) / min(lens) > limit:
            bad += 1
    return bad / len(polys)


def _iso_fraction(polys, tol_deg: float = 8.0) -> float:
    """Share of polygon edges running along a u or v iso-line.

    Vertices arrive already scaled to arc length, so the angle measured here is
    the real one on the surface rather than a parameter-space artefact.
    """
    tol = math.radians(tol_deg)
    hi = math.pi / 2 - tol
    n = ok = 0
    for poly in polys:
        k = len(poly)
        for i in range(k):
            (ua, va), (ub, vb) = poly[i], poly[(i + 1) % k]
            du, dv = ub - ua, vb - va
            if abs(du) < 1e-15 and abs(dv) < 1e-15:
                continue
            a = math.atan2(abs(dv), abs(du))
            n += 1
            if a <= tol or a >= hi:
                ok += 1
    return ok / n if n else 0.0


def _grid_wins(face, tri, rows, params, uvx, margin: float = 0.02,
               swap: bool = False):
    """Would the ruled grid actually beat OCCT's own mesh on THIS face?

    The axis-aligned grid is not a per-part choice, it is a per-FACE one, and
    forcing it everywhere makes some faces worse. Measured on valve body 138 at
    0.15/33, iso-aligned edges, grid ON vs OFF:

        face 56-59  cyl   dv 9.00    66.0%  143 polys  vs  100.0%   47 polys
        face 29-30  cyl   dv 6.00    66.7%  156        vs  100.0%   52
        face 11-14  torus dv 1.32    36.5%   73        vs   61.6%  289
        face 2      cone  dv 0.71    96.4%  513        vs   36.1%  169
        face 24     cyl   dv 1.00    96.0%  516        vs   38.4%  172
        face 40     torus dv 1.57    97.8% 1023        vs   53.9% 1277

    A tall plain cylinder is already perfect in OCCT's hands -- 100% aligned
    with a third of the polygons -- and the grid, forced to insert interior rows
    because the rings disagree, only adds diagonals. Short bands and fillets are
    the opposite. Rather than guess a rule from dv (which is mm on a cylinder
    and radians on a torus, so not comparable), build BOTH candidates in
    parameter space, score them, and take the better. Nothing is materialised
    while probing, so a rejected candidate leaves no orphan vertices behind.

    `margin` tilts ties toward the grid, which also yields quads.
    """
    try:
        sa = BRepAdaptor_Surface(face)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
        eu = max((u1 - u0) * 1e-3, 1e-9)
        ev = max((v1 - v0) * 1e-3, 1e-9)
        here = sa.Value(um, vm)
        su = here.Distance(sa.Value(um + eu, vm)) / eu
        sv = here.Distance(sa.Value(um, vm + ev)) / ev
        if swap:
            su, sv = sv, su
    except Exception:
        return True
    if su <= 0 or sv <= 0:
        return True

    probe = []
    for rv, row in rows:
        pr = []
        for u, item, *rest in row:
            pr.append((None, None, u, rest[0] if rest else rv))
        probe.append(pr)
    a_polys = []
    a_uv = []
    for r in range(len(probe) - 1):
        for poly in _ladder(probe[r], probe[r + 1], params.quads):
            a_polys.append([(e[2] * su, e[3] * sv) for e in poly])
            a_uv.append([(e[2], e[3]) for e in poly])
    if not a_polys:
        return False

    # Hard gate, not part of the score: a rebuild that bows away from the
    # surface further than the user asked for is not a better mesh however
    # tidy its edges look. This is what was carving valleys into fillets and
    # spheres.
    if params.deflection > 0:
        if _max_chord_sag(a_uv, sa, limit=params.deflection,
                          swap=swap) > params.deflection:
            AUTO_GRID_STATS["plain (grid sags)"] += 1
            return False

    if swap:
        uvs = {i: (tri.UVNode(i).Y(), tri.UVNode(i).X())
               for i in range(1, tri.NbNodes() + 1)}
        uvs.update({k: (b, a) for k, (a, b) in uvx.items()})
    else:
        uvs = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
               for i in range(1, tri.NbNodes() + 1)}
        uvs.update(uvx)
    plain = [tri.Triangle(t).Get() for t in range(1, tri.NbTriangles() + 1)]
    if not plain:
        return True
    plain = _flip_toward_axis(plain, uvs, sa=sa,
                              max_sag=params.deflection)
    b_polys = []
    for t in plain:
        try:
            b_polys.append([(uvs[i][0] * su, uvs[i][1] * sv) for i in t])
        except KeyError:
            return True

    w = min(1.0, max(0.0, float(getattr(params, "grid_bias", 0.5))))
    a = w * _iso_fraction(a_polys) + (1.0 - w) * _regularity(a_polys)
    b = w * _iso_fraction(b_polys) + (1.0 - w) * _regularity(b_polys)
    LAST_RULED_SCORE[0] = a
    LAST_RULED_SCORE[1] = b
    AUTO_GRID_STATS["grid" if a >= b - margin else "plain"] += 1
    return a >= b - margin


def _augment_boundary(face, tri, loc, trsf, plan, l2g, nrm, uvx, pntx, weld,
                      points, props, flipped, weld_decimals, next_id):
    """Insert the planned extra vertices into this face's boundary.

    The 3D position comes from the EDGE's own curve, never from either adjacent
    surface. That is what makes this safe: both faces sharing the edge evaluate
    the same curve at the same parameter and get bit-identical coordinates, so
    the two copies weld into one vertex. Taking each surface's own evaluation
    instead would leave them ~1e-4 mm apart -- the model's own tolerance -- and
    that is a hole.

    Returns (chains, inserts, next_id):
      chains[edge TShape] = refs along the edge, in the edge's parameter order,
                            for the cross-face weld to pair up
      inserts[(a, b)]     = refs to be threaded between existing nodes a and b
    """
    chains, inserts = {}, {}
    seen = set()
    ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        key = edge.TShape()
        if key in seen:
            continue
        seen.add(key)
        ts = plan.get(key)
        if not ts:
            continue
        poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if poly is None or not poly.HasParameters():
            continue
        nodes, prm = poly.Nodes(), poly.Parameters()
        base = [(prm.Value(k), nodes.Value(k))
                for k in range(1, poly.NbNodes() + 1)]
        if len(base) < 2:
            continue
        rev = base[0][0] > base[-1][0]
        asc = base[::-1] if rev else base
        pc = edgealign.pcurve(edge, face)
        if pc is None:
            continue
        c2d = pc[0]
        curve = BRepAdaptor_Curve(edge)
        added = []
        for t in ts:
            p = curve.Value(t)
            pk = (round(p.X(), weld_decimals), round(p.Y(), weld_decimals),
                  round(p.Z(), weld_decimals))
            gi = weld.get(pk)
            if gi is None:
                gi = len(points)
                weld[pk] = gi
                points.append([p.X(), p.Y(), p.Z()])
            q = c2d.Value(t)
            nn = (0.0, 0.0, 1.0)
            if props is not None:
                props.SetParameters(q.X(), q.Y())
                if props.IsNormalDefined():
                    d = props.Normal().Transformed(trsf)
                    nn = (d.X(), d.Y(), d.Z())
            ref = next_id
            next_id += 1
            l2g[ref] = gi
            nrm[ref] = (-nn[0], -nn[1], -nn[2]) if flipped else nn
            uvx[ref] = (q.X(), q.Y())
            pntx[ref] = p
            added.append((t, ref))
        if not added:
            continue
        fresh = {r for _, r in added}
        merged = sorted(asc + added, key=lambda x: x[0])
        seq = [r for _, r in merged]
        chains[key] = seq[::-1] if rev else seq
        prev, run = None, []
        for _, r in merged:
            if r in fresh:
                run.append(r)
            else:
                if prev is not None and run:
                    inserts[(prev, r)] = list(run)
                run = []
                prev = r
    return chains, inserts, next_id


def _reweave_face(face, tri, loc, params, aug_chains, uvx, flipped,
                  l2g=None, nrm=None, weld=None, points=None,
                  props=None, trsf=None, weld_decimals=7,
                  next_id=None):
    """Re-triangulate a whole face around vertices added to its boundary.

    Splitting only the triangle that carries an inserted vertex FANS: a coarse
    planar face is two triangles, so 25 vertices added along one of its edges
    turn one triangle into 25 slivers radiating from the opposite corner. Benn
    hit exactly that on body 49 -- two planes and two cylinders, where the
    cylinders' rings asked for 47 extra vertices and the planes came out
    shredded. Flat faces are supposed to absorb these quietly.

    So rebuild the face instead: keep OCCT's interior nodes, take the augmented
    boundary, and re-triangulate the lot with constrained Delaunay. Returns
    local-index triangles in the same winding the plain path uses, or None to
    leave the caller's fallback in place.
    """
    try:
        import numpy as np
        from scipy.spatial import Delaunay
        from matplotlib.path import Path
    except Exception:
        return None
    loops = _face_loops(face, tri, loc, aug_chains)
    if not loops:
        return None
    uv = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
          for i in range(1, tri.NbNodes() + 1)}
    uv.update(uvx)
    bset = set()
    for lp in loops:
        for r in lp:
            if r not in uv:
                return None
            bset.add(r)
    if len(bset) < 3:
        return None
    interior = [i for i in range(1, tri.NbNodes() + 1) if i not in bset]
    refs = list(bset) + interior
    idx = {r: k for k, r in enumerate(refs)}
    us = [uv[r][0] for r in refs]
    vs = [uv[r][1] for r in refs]
    du = max(us) - min(us)
    dv = max(vs) - min(vs)
    if du <= 0 or dv <= 0:
        return None
    sa = BRepAdaptor_Surface(face)
    try:
        um = 0.5 * (min(us) + max(us))
        vm = 0.5 * (min(vs) + max(vs))
        eu, ev = du * 1e-3, dv * 1e-3
        here = sa.Value(um, vm)
        su = here.Distance(sa.Value(um + eu, vm)) / eu
        sv = here.Distance(sa.Value(um, vm + ev)) / ev
    except Exception:
        return None
    if su <= 0 or sv <= 0:
        return None
    base = [[uv[r][0] * su, uv[r][1] * sv] for r in refs]
    if len(base) < 3:
        return None
    pts = np.array(base)
    if len(pts) < 3:
        return None
    if len(loops) > 1:
        def _area(lp):
            a = 0.0
            for k in range(len(lp)):
                x1, y1 = uv[lp[k]]
                x2, y2 = uv[lp[(k + 1) % len(lp)]]
                a += x1 * y2 - x2 * y1
            return abs(a)
        loops = sorted(loops, key=_area, reverse=True)
    try:
        dl = Delaunay(pts)
    except Exception:
        return None
    constraints = []
    for lp in loops:
        for k in range(len(lp)):
            a, b = idx[lp[k]], idx[lp[(k + 1) % len(lp)]]
            if a != b:
                constraints.append((a, b))
    tris = _recover_constraints([tuple(int(v) for v in t) for t in dl.simplices],
                                pts, constraints)
    if tris is None:
        return None
    outer = Path([(uv[r][0] * su, uv[r][1] * sv) for r in loops[0]])
    holes = [Path([(uv[r][0] * su, uv[r][1] * sv) for r in lp])
             for lp in loops[1:]]
    simp = np.asarray(tris, dtype=int)
    cen = pts[simp].mean(axis=1)
    keep = outer.contains_points(cen)
    for h in holes:
        keep &= ~h.contains_points(cen)
    simp = simp[keep]
    if len(simp) == 0:
        return None
    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in constraints:
        if ((x, y) if x < y else (y, x)) not in have:
            return None
    out = []
    for a, b, c in simp:
        pa, pb, pc = pts[a], pts[b], pts[c]
        if ((pb[0] - pa[0]) * (pc[1] - pa[1])
                - (pb[1] - pa[1]) * (pc[0] - pa[0])) < 0:
            a, c = c, a
        t = (refs[a], refs[b], refs[c])
        out.append((t[2], t[1], t[0]) if flipped else t)
    return out


def _split_boundary_triangles(local, inserts, uv):
    """Thread inserted boundary vertices through the triangles that touch them.

    A vertex added to a shared edge must appear in EVERY triangle along that
    edge, on both faces, or it is a T-junction -- which reads as a hole. The
    triangle carrying the segment is re-triangulated with the new points on its
    rim; one triangle may receive insertions on more than one of its edges, so
    the whole rim is rebuilt and ear-clipped rather than fanned.
    """
    if not inserts:
        return local
    edge_tris = {}
    for idx, t in enumerate(local):
        n = len(t)
        for k in range(n):
            a, b = t[k], t[(k + 1) % n]
            edge_tris.setdefault((a, b) if a < b else (b, a), []).append(idx)
    per_tri = {}
    for (a, b), refs in inserts.items():
        lst = edge_tris.get((a, b) if a < b else (b, a))
        if not lst or len(lst) != 1:
            continue          # not a boundary segment of exactly one triangle
        per_tri.setdefault(lst[0], {})[(a, b)] = refs
    if not per_tri:
        return local

    def _area(seq):
        s = 0.0
        for k in range(len(seq)):
            x1, y1 = uv[seq[k]]
            x2, y2 = uv[seq[(k + 1) % len(seq)]]
            s += x1 * y2 - x2 * y1
        return s

    out = []
    for idx, t in enumerate(local):
        ins = per_tri.get(idx)
        if not ins or len(t) != 3:
            out.append(t)
            continue
        loop = []
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            loop.append(a)
            fwd = ins.get((a, b))
            if fwd is not None:
                loop.extend(fwd)
            else:
                back = ins.get((b, a))
                if back is not None:
                    loop.extend(reversed(back))
        if len(loop) < 4 or any(r not in uv for r in loop):
            out.append(t)
            continue
        cw = _area(loop) < 0
        pieces = _ear_clip(loop[::-1] if cw else loop, uv)
        if not pieces:
            out.append(t)      # could not re-triangulate: leave it alone, and
            continue           # the plan for this edge simply does not apply
        if cw:
            pieces = [(c, b_, a_) for a_, b_, c in pieces]
        out.extend(pieces)
    return out

def _ear_clip(loop, uv):
    """Ear-clipping triangulation of a simple polygon given in UV.

    No new vertices, so the boundary stays exactly as the neighbouring faces
    discretised it and the mesh stays watertight.
    """
    raw = [uv[i] for i in loop]
    n = len(loop)
    if n < 3:
        return []
    # Normalise the UV box to roughly unit size. A sliver face can be 1e-6 wide
    # in one parameter, which makes every cross product underflow to zero and
    # the ear test fail on a polygon that is perfectly triangulable.
    xs = [q[0] for q in raw]
    ys = [q[1] for q in raw]
    sx = (max(xs) - min(xs)) or 1.0
    sy = (max(ys) - min(ys)) or 1.0
    pts = [((q[0] - min(xs)) / sx, (q[1] - min(ys)) / sy) for q in raw]

    def area2(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))

    signed = 0.0
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % n]
        signed += x1 * y2 - x2 * y1
    idx = list(range(n))
    if signed < 0:                      # make it counter-clockwise
        idx.reverse()

    def inside(a, b, c, p):
        d1 = area2(a, b, p)
        d2 = area2(b, c, p)
        d3 = area2(c, a, p)
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (neg and pos)

    out = []
    guard = 0
    while len(idx) > 3 and guard < 4 * n:
        guard += 1
        clipped = False
        for k in range(len(idx)):
            i0 = idx[(k - 1) % len(idx)]
            i1 = idx[k]
            i2 = idx[(k + 1) % len(idx)]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if area2(a, b, c) <= 0:
                continue                # reflex or degenerate: not an ear
            if any(inside(a, b, c, pts[j]) for j in idx
                   if j not in (i0, i1, i2)):
                continue
            out.append((loop[i0], loop[i1], loop[i2]))
            idx.pop(k)
            clipped = True
            break
        if not clipped:
            return []                   # self-intersecting in UV: give up
    if len(idx) == 3:
        out.append((loop[idx[0]], loop[idx[1]], loop[idx[2]]))
    return out


def _is_closed(shape) -> bool:
    """True when the input is a closed solid, so any hole in its mesh is a
    defect rather than real geometry."""
    try:
        if shape.ShapeType() == TopAbs_ShapeEnum.TopAbs_SOLID:
            return True
        if shape.ShapeType() == TopAbs_ShapeEnum.TopAbs_SHELL:
            return bool(shape.Closed())
    except Exception:
        pass
    return False


def _plane_fill(mesh, seq, uv, origin, U, V, nrm):
    """Triangulate a planar hole with a GRID, not a fan.

    Ear clipping a roughly circular rim gives a fan of slivers meeting at one
    vertex -- the very thing the sphere apex fill exists to remove, so it has no
    business being the repair for a hole either. Lay a regular grid across the
    rim's own plane at the rim's own spacing, cull it to the inside, and let
    constrained Delaunay stitch grid to rim. Interior points are invented, which
    is free: nothing outside this hole can see them.

    Falls back to a single CENTRE vertex (an umbrella) when the hole is too
    small for even one grid point, and to ear clipping only if both fail.
    Returns triangles over mesh point indices, wound CCW in `uv`.
    """
    try:
        import numpy as np
        from scipy.spatial import Delaunay, cKDTree
        from matplotlib.path import Path
    except Exception:
        return _ear_clip(seq, uv)

    ring = [uv[i] for i in seq]
    n = len(seq)
    step = 0.0
    for k in range(n):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % n]
        step += math.hypot(x2 - x1, y2 - y1)
    step /= max(n, 1)
    if step <= 0:
        return _ear_clip(seq, uv)

    path = Path(ring)
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    nx_ = int((max(xs) - min(xs)) / step) + 1
    ny_ = int((max(ys) - min(ys)) / step) + 1
    inside = []
    if 1 < nx_ * ny_ <= 40000:
        cand = np.array([[min(xs) + step * (i + 0.5), min(ys) + step * (j + 0.5)]
                         for i in range(nx_) for j in range(ny_)])
        keep = path.contains_points(cand, radius=-0.45 * step)
        cand = cand[keep]
        if len(cand):
            dense = []
            for k in range(n):
                x1, y1 = ring[k]
                x2, y2 = ring[(k + 1) % n]
                m_ = max(1, int(math.hypot(x2 - x1, y2 - y1) / (0.3 * step)))
                for t2 in range(m_):
                    f = t2 / m_
                    dense.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
            d2, _ = cKDTree(np.asarray(dense)).query(cand, k=1)
            cand = cand[d2 > 0.55 * step]
        inside = [tuple(map(float, c)) for c in cand]

    if not inside:
        # too small for a grid: one centre vertex beats a fan from a corner,
        # and on a near-circular rim that IS the right answer
        cx2 = sum(p[0] for p in ring) / n
        cy2 = sum(p[1] for p in ring) / n
        if path.contains_point((cx2, cy2)):
            inside = [(cx2, cy2)]

    if not inside:
        return _ear_clip(seq, uv)

    refs = list(seq)
    pts = [list(p) for p in ring]
    for (a, b) in inside:
        p3 = (origin[0] + U[0] * a + V[0] * b,
              origin[1] + U[1] * a + V[1] * b,
              origin[2] + U[2] * a + V[2] * b)
        mesh.points.append(list(p3))
        refs.append(len(mesh.points) - 1)
        pts.append([a, b])

    arr = np.asarray(pts)
    try:
        dl = Delaunay(arr)
    except Exception:
        return _ear_clip(seq, uv)
    cons = [(k, (k + 1) % n) for k in range(n)]
    got = _recover_constraints([tuple(int(v) for v in t) for t in dl.simplices],
                               arr, cons)
    if got is None:
        return _ear_clip(seq, uv)
    simp = np.asarray(got, dtype=int)
    cen = arr[simp].mean(axis=1)
    simp = simp[path.contains_points(cen)]
    if len(simp) == 0:
        return _ear_clip(seq, uv)
    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in cons:
        if ((x, y) if x < y else (y, x)) not in have:
            return _ear_clip(seq, uv)
    out = []
    for a, b, c in simp:
        pa, pb, pc = arr[a], arr[b], arr[c]
        if ((pb[0] - pa[0]) * (pc[1] - pa[1])
                - (pb[1] - pa[1]) * (pc[0] - pa[0])) < 0:
            a, c = c, a
        out.append((refs[a], refs[b], refs[c]))
    return out


def fill_planar_holes(mesh, flat_tol: float = 0.02, max_loop: int = 4096):
    """Cap the holes in an open shell, but only where the rim is FLAT.

    For repairing a shell that was never a closed solid -- the STEP simply had
    no lid on it. A planar rim is the case where inventing a lid is honest: the
    geometry it stands for is unambiguous, and the result is what the part
    obviously should have been. A rim that twists through space is NOT filled,
    because any surface we put there would be invented shape, and a wrong solid
    is worse than an honest open shell.

    `flat_tol` is relative to the rim's own size: a loop counts as planar when
    no vertex is further from the best-fit plane than `flat_tol` times the
    loop's largest extent (2% by default).

    Winding comes from the rim itself: whatever direction the existing
    triangles traverse a boundary edge, the cap must traverse it the other way,
    or the mesh ends up sealed but inconsistent -- which reads as "not
    watertight" just as loudly as a hole.

    Returns (filled, skipped_not_flat).
    """
    directed = set()
    owner = {}                      # rim edge -> the face that carries it
    for fi, f in enumerate(mesh.faces):
        n = len(f)
        for k in range(n):
            e = (f[k], f[(k + 1) % n])
            directed.add(e)
            owner[e] = fi
    rim = {(u, v) for (u, v) in directed if (v, u) not in directed}
    if not rim:
        return 0, 0

    nxt = {}
    for u, v in rim:
        nxt.setdefault(u, []).append(v)

    loops = []
    unused = set(rim)
    while unused:
        u0, v0 = next(iter(unused))
        loop = [u0, v0]
        unused.discard((u0, v0))
        cur = v0
        ok = True
        while cur != u0:
            outs = [w for w in nxt.get(cur, []) if (cur, w) in unused]
            if not outs or len(loop) > max_loop:
                ok = False
                break
            w = outs[0]
            unused.discard((cur, w))
            cur = w
            if cur != u0:
                loop.append(cur)
        if ok and len(loop) >= 3:
            loops.append(loop)

    filled = skipped = 0
    for loop in loops:
        pts = [mesh.points[i] for i in loop]
        n = len(pts)
        cx = sum(p[0] for p in pts) / n
        cy = sum(p[1] for p in pts) / n
        cz = sum(p[2] for p in pts) / n
        # best-fit plane normal by the Newell method: robust for a polygon and
        # needs no eigen decomposition
        nx = ny = nz = 0.0
        for k in range(n):
            a = pts[k]
            b = pts[(k + 1) % n]
            nx += (a[1] - b[1]) * (a[2] + b[2])
            ny += (a[2] - b[2]) * (a[0] + b[0])
            nz += (a[0] - b[0]) * (a[1] + b[1])
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln <= 1e-20:
            skipped += 1
            continue
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        dev = 0.0
        ext = 0.0
        for p in pts:
            d = abs((p[0] - cx) * nx + (p[1] - cy) * ny + (p[2] - cz) * nz)
            dev = max(dev, d)
            ext = max(ext, math.dist(p, (cx, cy, cz)))
        if ext <= 0 or dev > flat_tol * ext:
            skipped += 1
            continue

        # project onto the plane and ear-clip there
        ax = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
        ux = (ny * ax[2] - nz * ax[1], nz * ax[0] - nx * ax[2],
              nx * ax[1] - ny * ax[0])
        ul = math.sqrt(sum(c * c for c in ux))
        if ul <= 0:
            skipped += 1
            continue
        U = tuple(c / ul for c in ux)
        V = (ny * U[2] - nz * U[1], nz * U[0] - nx * U[2], nx * U[1] - ny * U[0])
        uv = {}
        for i, p in zip(loop, pts):
            uv[i] = ((p[0] - cx) * U[0] + (p[1] - cy) * U[1] + (p[2] - cz) * U[2],
                     (p[0] - cx) * V[0] + (p[1] - cy) * V[1] + (p[2] - cz) * V[2])
        area = 0.0
        for k in range(n):
            x1, y1 = uv[loop[k]]
            x2, y2 = uv[loop[(k + 1) % n]]
            area += x1 * y2 - x2 * y1
        seq = loop if area > 0 else loop[::-1]
        nrm = (nx, ny, nz)
        tris = _plane_fill(mesh, seq, uv, (cx, cy, cz), U, V, nrm)
        if not tris:
            skipped += 1
            continue

        # The cap must run AGAINST the rim: whatever direction the existing
        # triangles traverse a boundary edge, the cap traverses the other way.
        # Decide it by looking at an actual edge rather than reasoning about
        # area signs -- that is what went wrong in the sphere apex fill.
        flip = False
        for t in tris:
            hit = False
            for k in range(3):
                e = (t[k], t[(k + 1) % 3])
                if e in rim:            # same direction as the existing mesh
                    flip = True
                    hit = True
                    break
                if (e[1], e[0]) in rim:
                    hit = True
                    break
            if hit:
                break
        # Take the material from the faces AROUND the hole, not from face 0 --
        # on a multi-material part the lid belongs with its own neighbours.
        votes = {}
        for k in range(n):
            fi = owner.get((loop[k], loop[(k + 1) % n]))
            if fi is None:
                fi = owner.get((loop[(k + 1) % n], loop[k]))
            if fi is not None and fi < len(mesh.face_source):
                sv = mesh.face_source[fi]
                votes[sv] = votes.get(sv, 0) + 1
        if votes:
            src = max(votes.items(), key=lambda kv: kv[1])[0]
        else:
            src = mesh.face_source[0] if mesh.face_source else 0
        for t in tris:
            tt = (t[0], t[2], t[1]) if flip else t
            # Take the normal from the FINAL winding, not from the Newell sign:
            # that sign follows the loop's direction, so half the caps would
            # point into the solid. The surrounding faces are wound outward, and
            # the cap is now wound to agree with them, so its own geometric
            # normal is the one that matches its neighbours.
            pa, pb, pc = (mesh.points[i] for i in tt)
            ux_ = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
            vx_ = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
            fx = ux_[1] * vx_[2] - ux_[2] * vx_[1]
            fy = ux_[2] * vx_[0] - ux_[0] * vx_[2]
            fz = ux_[0] * vx_[1] - ux_[1] * vx_[0]
            fl = math.sqrt(fx * fx + fy * fy + fz * fz)
            fn = (fx / fl, fy / fl, fz / fl) if fl > 1e-20 else nrm
            mesh.faces.append(tt)
            mesh.normals.extend((fn, fn, fn))
            mesh.face_source.append(src)
        filled += 1

    if filled:
        check_manifold(mesh)
        mesh.n_quads = sum(1 for f in mesh.faces if len(f) == 4)
    return filled, skipped


def _fill_holes(mesh, max_loop: int = 512) -> int:
    """Cap every boundary loop left in a mesh that should be closed.

    Boundary edges are those traversed in one direction only. Chaining them
    gives the hole's rim; a fan wound against that direction balances every rim
    edge to two uses. The holes seen in practice are a handful of edges around a
    sliver, so the shape cost is nil -- but the watertight guarantee is restored.
    """
    directed = {}
    for f in mesh.faces:
        n = len(f)
        for k in range(n):
            directed[(f[k], f[(k + 1) % n])] = True
    rim = {(u, v) for (u, v) in directed if (v, u) not in directed}
    if not rim:
        return 0

    nxt = {}
    for u, v in rim:
        nxt.setdefault(u, []).append(v)

    filled = 0
    unused = set(rim)
    while unused:
        u0, v0 = next(iter(unused))
        loop = [u0, v0]
        unused.discard((u0, v0))
        cur = v0
        while cur != u0 and len(loop) < max_loop:
            outs = [w for w in nxt.get(cur, []) if (cur, w) in unused]
            if not outs:
                break
            w = outs[0]
            unused.discard((cur, w))
            cur = w
            if cur != u0:
                loop.append(cur)
        if cur != u0 or len(loop) < 3:
            continue                      # not a clean loop: leave it visible
        pts = mesh.points
        # fan wound AGAINST the rim direction so each rim edge reaches two uses
        base = loop[0]
        tri_normals = []
        for k in range(1, len(loop) - 1):
            a, b, c = base, loop[k + 1], loop[k]
            ax, ay, az = pts[a]
            ux, uy, uz = (pts[b][0] - ax, pts[b][1] - ay, pts[b][2] - az)
            vx, vy, vz = (pts[c][0] - ax, pts[c][1] - ay, pts[c][2] - az)
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            n3 = (nx / ln, ny / ln, nz / ln)
            mesh.faces.append((a, b, c))
            mesh.normals.extend((n3, n3, n3))
            mesh.face_source.append(mesh.face_source[-1] if mesh.face_source else 1)
            tri_normals.append(n3)
        filled += 1
    if filled:
        check_manifold(mesh)
    return filled


def _pair_planar_quads(mesh, planar_faces, angle_rad=None):
    """Merge adjacent triangle pairs into quads, on WELDED indices.

    Planar faces are exactly representable by quads, so a cube should be six
    quads rather than twelve triangles. Convexity is tested in the plane of the
    pair, and the merge is rejected unless the resulting quad has four distinct
    vertices and each of its edges is used exactly twice in the whole mesh --
    which is what keeps the mesh manifold no matter what the welding did.

    CURVED faces are paired too, subject to a dihedral limit. Benn's case: a
    long concave fillet swept along a changing direction is a B-spline surface,
    so it gets neither the ruled grid nor the old planes-only pairing, and came
    out as an obvious ladder of triangles that should plainly have been quads.
    Within one B-rep face there are no sharp edges by definition, and the mesh
    already holds the angle tolerance, so two adjacent triangles there are as
    flat as the user asked the mesh to be. `angle_rad` caps how much bend a quad
    may span; None keeps the old planes-only behaviour.

    Note the pairs still must share a B-rep face -- never merge across one, that
    is where the real sharp edges live.
    """
    faces = mesh.faces
    norms = mesh.normals
    src = mesh.face_source
    pts = mesh.points

    # corner-normal offset of every polygon
    offs = []
    o = 0
    for f in faces:
        offs.append(o)
        o += len(f)

    edge_use = {}
    for f in faces:
        n = len(f)
        for k in range(n):
            a, b = f[k], f[(k + 1) % n]
            e = (a, b) if a < b else (b, a)
            edge_use[e] = edge_use.get(e, 0) + 1

    def _tri_normal(t):
        a, b, c = (pts[v] for v in t)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln < 1e-20:
            return None
        return (nx / ln, ny / ln, nz / ln)

    cos_lim = math.cos(angle_rad) if angle_rad is not None else None
    tri_of_edge = {}
    for ti, f in enumerate(faces):
        if len(f) != 3:
            continue
        if cos_lim is None and src[ti] not in planar_faces:
            continue
        for k in range(3):
            a, b = f[k], f[(k + 1) % 3]
            tri_of_edge.setdefault((a, b) if a < b else (b, a), []).append(ti)

    def directed(t, x, y):
        return (t[0] == x and t[1] == y) or (t[1] == x and t[2] == y) \
            or (t[2] == x and t[0] == y)

    cands = []
    for e, owners in tri_of_edge.items():
        if len(owners) != 2 or edge_use.get(e) != 2:
            continue
        t1, t2 = owners
        if src[t1] != src[t2]:
            continue                       # different B-rep faces: never merge
        A, B = faces[t1], faces[t2]
        e0, e1 = e
        if directed(A, e0, e1):
            P, Q = e0, e1
        elif directed(A, e1, e0):
            P, Q = e1, e0
        else:
            continue
        if not directed(B, Q, P):
            continue
        b_ = next((v for v in A if v not in (P, Q)), None)
        d_ = next((v for v in B if v not in (P, Q)), None)
        if b_ is None or d_ is None:
            continue
        quad = (P, d_, Q, b_)
        if len(set(quad)) != 4:
            continue                       # would be a bowtie
        if cos_lim is not None and src[t1] not in planar_faces:
            n1 = _tri_normal(faces[t1])
            n2 = _tri_normal(faces[t2])
            if n1 is None or n2 is None:
                continue
            if n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2] < cos_lim:
                continue                   # too much bend to be one quad
        # convexity, in the plane of the pair
        p0, p1, p2, p3 = (pts[v] for v in quad)
        ux, uy, uz = (p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2])
        vx, vy, vz = (p3[0]-p0[0], p3[1]-p0[1], p3[2]-p0[2])
        nx, ny, nz = (uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx)
        ln = math.sqrt(nx*nx+ny*ny+nz*nz)
        if ln < 1e-20:
            continue
        nx, ny, nz = nx/ln, ny/ln, nz/ln
        ok = True
        for k in range(4):
            a_ = pts[quad[k]]
            b_p = pts[quad[(k+1) % 4]]
            c_ = pts[quad[(k+2) % 4]]
            e1x, e1y, e1z = (b_p[0]-a_[0], b_p[1]-a_[1], b_p[2]-a_[2])
            e2x, e2y, e2z = (c_[0]-b_p[0], c_[1]-b_p[1], c_[2]-b_p[2])
            cx = e1y*e2z - e1z*e2y
            cy = e1z*e2x - e1x*e2z
            cz = e1x*e2y - e1y*e2x
            if cx*nx + cy*ny + cz*nz <= 0:
                ok = False
                break
        if not ok:
            continue
        dd = ((pts[P][0]-pts[Q][0])**2 + (pts[P][1]-pts[Q][1])**2
              + (pts[P][2]-pts[Q][2])**2)
        cands.append((dd, t1, t2, quad))

    if not cands:
        return
    cands.sort(reverse=True)
    used = set()
    merged = []
    for _, t1, t2, quad in cands:
        if t1 in used or t2 in used:
            continue
        used.add(t1); used.add(t2)
        merged.append((t1, t2, quad))
    if not merged:
        return

    replace = {}
    partner = {}
    for t1, t2, quad in merged:
        replace[t1] = quad
        replace[t2] = None
        partner[t1] = t2

    new_faces, new_norms, new_src = [], [], []
    for ti, f in enumerate(faces):
        if ti in replace:
            q = replace[ti]
            if q is None:
                continue
            # Keep the CORNER normals. Collapsing to one normal is only right
            # on a plane; on a fillet it would flat-shade the quad and undo the
            # smooth shading the faceVarying normals exist to provide.
            look = {}
            for tj in (ti, partner[ti]):
                fj = faces[tj]
                for kk, v in enumerate(fj):
                    look.setdefault(v, norms[offs[tj] + kk])
            fallback = norms[offs[ti]]
            new_faces.append(q)
            new_norms.extend(look.get(v, fallback) for v in q)
            new_src.append(src[ti])
        else:
            new_faces.append(f)
            new_norms.extend(norms[offs[ti]:offs[ti] + len(f)])
            new_src.append(src[ti])
    mesh.faces = new_faces
    mesh.normals = new_norms
    mesh.face_source = new_src
    mesh.n_quads = sum(1 for f in new_faces if len(f) == 4)


def _lattice_remesh(face, tri, loc, params):
    """Rebuild a TRIMMED cylinder/cone so its edges follow the u iso-lines.

    A trimmed face cannot use the ruled grid: its boundary wanders instead of
    lying on two iso-curves, so there are no clean rings to span. But only the
    BOUNDARY is shared with the neighbours -- the interior is ours. So:

      * keep every boundary node exactly as OCCT discretised it,
      * throw away OCCT's interior nodes,
      * lay a lattice of interior points on RAILS at the boundary nodes' own u
        values (so rails line up with the boundary), spaced in v by the same
        deflection and aspect rules as the ruled grid,
      * Delaunay-triangulate the lot in a UV space scaled so u is arc length,
      * drop triangles outside the face, and VERIFY every boundary edge survived
        -- if any is missing the triangulation is not boundary-conforming and we
        return None so the caller keeps OCCT's version.

    Returns (refs, polys): refs are local node indices (existing) or (u, v)
    pairs (new interior points); polys index into refs.
    """
    LATTICE_STATS["attempt"] += 1
    try:
        import numpy as np
        from scipy.spatial import Delaunay
        from matplotlib.path import Path
    except Exception:
        return None

    # A seam edge appears twice in the wire but PolygonOnTriangulation returns
    # only ONE of its two node runs, so the loop we would build is wrong and the
    # face would no longer join itself across the seam.
    # Seam faces are NOT skipped up front any more: the patch validator at the
    # end proves correctness, so a seam face that happens to work is a free win
    # and one that does not simply falls back to OCCT.
    loops = _face_loops(face, tri, loc)
    if not loops:
        LATTICE_STATS["no loops"] += 1
        return None
    n_nodes = tri.NbNodes()
    uv = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y()) for i in range(1, n_nodes + 1)}

    bnodes = []
    seen = set()
    for lp in loops:
        for v in lp:
            if v not in seen:
                seen.add(v)
                bnodes.append(v)
    if len(bnodes) < 4 or len(bnodes) > 20000:
        LATTICE_STATS["node count"] += 1
        return None

    us = [uv[i][0] for i in bnodes]
    vs = [uv[i][1] for i in bnodes]
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    du, dv = umax - umin, vmax - vmin
    if du <= 0 or dv <= 0:
        LATTICE_STATS["degenerate uv"] += 1
        return None

    # The OUTER wire must come first: TopExp_Explorer returns wires in no
    # particular order, and taking a hole as the outer boundary inverts the
    # inside/outside test, so triangles survive on the wrong side of an edge.
    # Largest |area| in UV is the outer loop of a valid face.
    if len(loops) > 1:
        def _area(lp):
            a = 0.0
            for k in range(len(lp)):
                x1, y1 = uv[lp[k]]
                x2, y2 = uv[lp[(k + 1) % len(lp)]]
                a += x1 * y2 - x2 * y1
            return abs(a)
        loops.sort(key=_area, reverse=True)

    # u -> arc length, so the lattice is isotropic and Delaunay behaves
    sa = BRepAdaptor_Surface(face)
    try:
        vm = 0.5 * (vmin + vmax)
        p0 = sa.Value(umin, vm)
        p1 = sa.Value(umin + du * 0.001, vm)
        su = p0.Distance(p1) / (du * 0.001)
    except Exception:
        LATTICE_STATS["surface eval"] += 1
        return None
    if su <= 0:
        LATTICE_STATS["scale"] += 1
        return None

    # rails: the boundary's own u values, plus extra rails so no gap is wider
    # than a cell
    n_v = max(1, _rows_for_curvature(sa, 0.5 * (umin + umax), vmin, vmax,
                                     params.deflection,
                                     math.radians(params.angle_deg)))
    cell_v = dv / n_v
    cell_u = max(cell_v * params.grid_aspect / max(su, 1e-12), du * 1e-6)
    # Merge rails by TOLERANCE, not by rounding: two boundary u values differing
    # by ~1e-12 survive a round() and become rails a hair apart, whose lattice
    # points QHull then merges -- which silently drops boundary edges and tears
    # the face off its neighbours.
    # Purely a numerical-coincidence guard, NOT a spacing rule: tying it to the
    # cell size collapses every rail when cell_u exceeds the u-range.
    rail_tol = du * 1e-7
    ordered = sorted(us)
    rails = [ordered[0]]
    for u in ordered[1:]:
        if u - rails[-1] > rail_tol:
            rails.append(u)
    filled = [rails[0]]
    for a, b in zip(rails, rails[1:]):
        gap = b - a
        if gap > cell_u * 1.5:
            k = int(gap / cell_u)
            for j in range(1, k):
                filled.append(a + gap * j / k)
        filled.append(b)
    rails = filled
    if len(rails) * (n_v + 1) > 200000:
        LATTICE_STATS["too big"] += 1
        return None

    # interior lattice points, strictly inside the face
    outer = Path([(uv[i][0] * su, uv[i][1]) for i in loops[0]])
    holes = [Path([(uv[i][0] * su, uv[i][1]) for i in lp]) for lp in loops[1:]]
    cand = []
    for u in rails:
        for k in range(1, n_v + 1):
            v = vmin + dv * k / (n_v + 1)
            cand.append((u * su, v))
    if not cand:
        LATTICE_STATS["no candidates"] += 1
        return None
    cand = np.asarray(cand)
    margin = -0.25 * min(cell_v, cell_u * su)
    inside = outer.contains_points(cand, radius=margin)
    for h in holes:
        inside &= ~h.contains_points(cand, radius=-margin)
    interior = cand[inside]

    bxy = np.array([[uv[i][0] * su, uv[i][1]] for i in bnodes])
    # Cull lattice points that land on (or almost on) a boundary node. The rails
    # sit at the boundary's own u values, so coincidences are common -- measured
    # 22 pairs closer than 1e-9 on one face. QHull merges points that close, and
    # the triangulation then references one representative, which silently drops
    # boundary edges and tears the face away from its neighbours.
    if len(interior):
        from scipy.spatial import cKDTree
        keep_tol = 0.35 * min(cell_v, cell_u * su)
        d, _ = cKDTree(bxy).query(interior, k=1)
        interior = interior[d > keep_tol]
    pts = np.vstack([bxy, interior]) if len(interior) else bxy
    if len(pts) < 3:
        LATTICE_STATS["too few points"] += 1
        return None
    try:
        dl = Delaunay(pts)
    except Exception:
        LATTICE_STATS["delaunay failed"] += 1
        return None

    # Recover the boundary as CONSTRAINED edges. Plain Delaunay is free to cut
    # across a concave boundary, and on this model it did so for 145 of 262
    # trimmed cylinders -- which used to make us discard the lattice entirely.
    idx_of = {node: k for k, node in enumerate(bnodes)}
    constraints = []
    for lp in loops:
        for k in range(len(lp)):
            x = idx_of[lp[k]]
            y = idx_of[lp[(k + 1) % len(lp)]]
            if x != y:
                constraints.append((x, y))
    tri_list = _recover_constraints([tuple(int(v) for v in t) for t in dl.simplices],
                                    pts, constraints)
    if tri_list is None:
        LATTICE_STATS["recovery failed"] += 1
        return None       # could not make it conform: keep OCCT's mesh

    # keep triangles whose centroid is inside the face
    import numpy as _np
    simp = _np.asarray(tri_list, dtype=int)
    cen = pts[simp].mean(axis=1)
    keep = outer.contains_points(cen)
    for h in holes:
        keep &= ~h.contains_points(cen)
    simp = simp[keep]
    if len(simp) == 0:
        LATTICE_STATS["all filtered out"] += 1
        return None

    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in constraints:
        if ((x, y) if x < y else (y, x)) not in have:
            LATTICE_STATS["constraint lost"] += 1
            return None

    refs = list(bnodes) + [(float(x / su), float(y)) for x, y in interior]
    # orient CCW in UV, matching OCCT's convention for a FORWARD face
    polys = []
    for a, b, c in simp:
        pa, pb, pc = pts[a], pts[b], pts[c]
        if ((pb[0] - pa[0]) * (pc[1] - pa[1])
                - (pb[1] - pa[1]) * (pc[0] - pa[0])) < 0:
            a, c = c, a
        polys.append((int(a), int(b), int(c)))

    uvmap = {}
    for k, r in enumerate(refs):
        uvmap[k] = uv[r] if isinstance(r, int) else r
    polys = _flip_toward_axis(polys, uvmap, sa=sa,
                              max_sag=params.deflection)
    if params.quads:
        polys = _pair_axial_quads(polys, uvmap)

    # Prove the patch before handing it back. A re-meshed face is only safe if
    # its own boundary is EXACTLY the face's wire edges, nothing is used more
    # than twice, and no directed edge repeats. Anything else tears the face
    # away from its neighbours somewhere. Rather than chase every way Delaunay
    # and constraint recovery can go wrong on degenerate input, verify and fall
    # back to OCCT's triangulation -- correctness first, structure second.
    if not _patch_is_sound(polys, constraints):
        LATTICE_STATS["patch unsound"] += 1
        return None
    return refs, polys, constraints


GRID_STATS = collections.Counter()


SPHERE_STATS = collections.Counter()


def _sphere_apex_fix(face, tri, loc, params, local, l2g, nrm, weld, points,
                     props, trsf, flipped, weld_decimals, next_id, uvx=None,
                     rings=4.0):
    """Cut out the fan around a sphere's apex and re-fill it with a grid.

    Benn's idea, and the neat thing about it is what it does NOT touch. OCCT
    fans a spherical patch from the parameter pole, and the damage is entirely
    local: past a couple of rings the mesh is fine, and there is a clean loop
    around the mess. So delete the triangles inside that loop, fill the hole
    with a regular grid, and project the new points onto the sphere -- a
    shrinkwrap, in Blender terms. Losing the vertex exactly at the apex is fine.

    Because only INTERIOR triangles are replaced, the face's own boundary is
    untouched, so nothing a neighbouring face can see changes and the watertight
    guarantee is unaffected. That is why this works where the from-scratch
    rebuilds (`_sphere_cap`, `_grid_trim`) keep failing on these patches: it
    never has to reproduce the trim curve.

    Returns a new triangle list, or None to leave `local` alone.
    """
    try:
        import numpy as np
        from scipy.spatial import Delaunay, cKDTree
        from matplotlib.path import Path
    except Exception:
        return None
    sa = BRepAdaptor_Surface(face)
    if sa.GetType() != GeomAbs_SurfaceType.GeomAbs_Sphere:
        return None
    sph = sa.Sphere()
    R = sph.Radius()
    if R <= 0:
        return None
    pos = sph.Position()
    C = pos.Location()
    EX = (pos.XDirection().X(), pos.XDirection().Y(), pos.XDirection().Z())
    EY = (pos.YDirection().X(), pos.YDirection().Y(), pos.YDirection().Z())
    EZ = (pos.Direction().X(), pos.Direction().Y(), pos.Direction().Z())
    CC = (C.X(), C.Y(), C.Z())

    used = set()
    for t in local:
        used.update(t)
    if len(used) < 12:
        return None

    def _dir(n):
        q = tri.Node(n)
        w = (q.X() - CC[0], q.Y() - CC[1], q.Z() - CC[2])
        d = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
        return None if d <= 0 else (w[0] / d, w[1] / d, w[2] / d)

    dirs = {}
    for n in used:
        d = _dir(n)
        if d is None:
            return None
        dirs[n] = d
    # apex from the AREA of the patch (all nodes), not from its boundary: a
    # boundary mean mis-centres any patch that is not a disc
    ax = [sum(dirs[n][i] for n in used) / len(used) for i in range(3)]
    an = math.sqrt(sum(c * c for c in ax))
    if an < 0.3:
        SPHERE_STATS["apex fix: no apex"] += 1
        return None
    AX = tuple(c / an for c in ax)

    # typical edge length on this face, as an angle
    seg = []
    for t in local:
        for k in range(len(t)):
            a, b = t[k], t[(k + 1) % len(t)]
            seg.append(tri.Node(a).Distance(tri.Node(b)))
    seg = sorted(x for x in seg if x > 0)
    if not seg:
        return None
    step = seg[len(seg) // 2] / R
    if step <= 0:
        return None

    def _theta(n):
        c = max(-1.0, min(1.0, sum(dirs[n][i] * AX[i] for i in range(3))))
        return math.acos(c)

    # How big a bite to take. `rings` is the ideal, but on a small patch that
    # can swallow the whole face, so try progressively smaller bites and take
    # the first that leaves a clean interior hole.
    ths = sorted(_theta(n) for n in used)
    cap = ths[int(0.35 * (len(ths) - 1))]

    # The face's REAL boundary is only its non-seam, non-degenerate edges. This
    # matters more than it sounds: a seam runs radially from the pole outward
    # and the degenerate edge IS the pole, so counting either as boundary makes
    # a perfectly clean cap look like one whose boundary reaches its own apex.
    # Measured on body 138's four big spheres: the real boundary sits at 55-78
    # degrees (a clean ring, which is what Benn sees), while the seam runs from
    # 1 degree outward. Getting this wrong is what made the cut-and-fill decline
    # every one of them.
    outer_nodes = set()
    outer_edges = set()
    ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge_s(ex.Current())
        ex.Next()
        if BRep_Tool.Degenerated_s(e) or BRep_Tool.IsClosed_s(e, face):
            continue
        poly = BRep_Tool.PolygonOnTriangulation_s(e, tri, loc)
        if poly is None:
            continue
        nd = poly.Nodes()
        ids = [nd.Value(k) for k in range(1, poly.NbNodes() + 1)]
        outer_nodes.update(ids)
        for k in range(len(ids) - 1):
            a, b = ids[k], ids[k + 1]
            outer_edges.add((a, b) if a < b else (b, a))

    # Canonical vertex per 3D POSITION: the seam carries two UV nodes for every
    # point on it and the pole several, and they weld later anyway. Working in
    # canonical ids is what lets the rim close into one loop across the seam.
    bypos = {}
    for n in used:
        q = tri.Node(n)
        key = (round(q.X(), 7), round(q.Y(), 7), round(q.Z(), 7))
        bypos.setdefault(key, []).append(n)
    canon = {}
    for group in bypos.values():
        rep = min(group)
        for n in group:
            canon[n] = rep
    outer_can = {canon[n] for n in outer_nodes if n in canon}
    outer_ecan = set()
    for a, b in outer_edges:
        if a in canon and b in canon:
            ca, cb = canon[a], canon[b]
            if ca != cb:
                outer_ecan.add((ca, cb) if ca < cb else (cb, ca))

    def _rim_loop(inner_can):
        """The hole's rim as one ordered loop of canonical ids, or None."""
        keep, drop = [], []
        for t in local:
            ct = tuple(canon.get(v, v) for v in t)
            (drop if any(v in inner_can for v in ct) else keep).append(t)
        if not drop or not keep:
            return None
        cnt = {}
        for t in keep:
            ct = [canon.get(v, v) for v in t]
            for k in range(len(ct)):
                a, b = ct[k], ct[(k + 1) % len(ct)]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                cnt[key] = cnt.get(key, 0) + 1
        rim = [e for e, c in cnt.items() if c == 1 and e not in outer_ecan]
        if len(rim) < 6:
            return None
        nxt = {}
        for a, b in rim:
            nxt.setdefault(a, []).append(b)
            nxt.setdefault(b, []).append(a)
        if any(len(v) != 2 for v in nxt.values()):
            return None
        start_v = rim[0][0]
        loop = [start_v]
        prev, cur = None, start_v
        while True:
            a, b = nxt[cur]
            step_v = a if a != prev else b
            if step_v == start_v:
                break
            loop.append(step_v)
            prev, cur = cur, step_v
            if len(loop) > len(rim) + 2:
                return None
        if len(loop) != len(rim):
            return None
        return keep, loop

    got = None
    for f in (rings, 3.0, 2.5, 2.0, 1.5):
        cut = min(step * f, cap)
        if cut <= 0 or cut > 1.2:
            continue
        inner_can = {canon[n] for n in used if _theta(n) < cut}
        if not inner_can or (inner_can & outer_can):
            continue
        if len({canon[n] for n in used}) - len(inner_can) < 8:
            continue
        got = _rim_loop(inner_can)
        if got is not None:
            break
    if got is None:
        SPHERE_STATS["apex fix: no clean rim"] += 1
        return None
    keep, loop = got

    # project the hole about the apex and fill it with a regular grid
    tmp = EX if abs(sum(AX[i] * EX[i] for i in range(3))) < 0.9 else EY
    ux = (AX[1] * tmp[2] - AX[2] * tmp[1],
          AX[2] * tmp[0] - AX[0] * tmp[2],
          AX[0] * tmp[1] - AX[1] * tmp[0])
    un = math.sqrt(sum(c * c for c in ux))
    if un <= 0:
        return None
    U = tuple(c / un for c in ux)
    Vv = (AX[1] * U[2] - AX[2] * U[1],
          AX[2] * U[0] - AX[0] * U[2],
          AX[0] * U[1] - AX[1] * U[0])

    def _proj(d):
        c = max(-1.0, min(1.0, sum(d[i] * AX[i] for i in range(3))))
        th = math.acos(c)
        x = sum(d[i] * U[i] for i in range(3))
        y = sum(d[i] * Vv[i] for i in range(3))
        ph = math.atan2(y, x)
        return (th * math.cos(ph), th * math.sin(ph))

    ring_xy = [_proj(dirs[n]) for n in loop]
    path = Path(ring_xy)
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    nx = int((max(xs) - min(xs)) / step) + 1
    ny = int((max(ys) - min(ys)) / step) + 1
    if nx * ny > 20000 or nx < 2 or ny < 2:
        SPHERE_STATS["apex fix: bad grid"] += 1
        return None
    cand = np.array([[min(xs) + step * (i + 0.5), min(ys) + step * (j + 0.5)]
                     for i in range(nx) for j in range(ny)])
    ins = path.contains_points(cand, radius=-0.45 * step)
    cand = cand[ins]
    if len(cand):
        dense = []
        for k in range(len(ring_xy)):
            x1, y1 = ring_xy[k]
            x2, y2 = ring_xy[(k + 1) % len(ring_xy)]
            m_ = max(1, int(math.hypot(x2 - x1, y2 - y1) / (0.3 * step)))
            for t2 in range(m_):
                f = t2 / m_
                dense.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
        d2, _ = cKDTree(np.asarray(dense)).query(cand, k=1)
        cand = cand[d2 > 0.55 * step]

    refs = list(loop)
    pts = [list(p) for p in ring_xy]
    nid = next_id
    gsurf = BRep_Tool.Surface_s(face)
    for xy in cand:
        th = math.hypot(xy[0], xy[1])
        ph = math.atan2(xy[1], xy[0])
        st_, ct = math.sin(th), math.cos(th)
        d = tuple(AX[i] * ct + (U[i] * math.cos(ph) + Vv[i] * math.sin(ph)) * st_
                  for i in range(3))
        z = max(-1.0, min(1.0, sum(d[i] * EZ[i] for i in range(3))))
        uu = math.atan2(sum(d[i] * EY[i] for i in range(3)),
                        sum(d[i] * EX[i] for i in range(3)))
        if uu < 0.0:
            uu += 2.0 * math.pi
        vv = math.asin(z)
        pt = gsurf.Value(uu, vv).Transformed(trsf)
        pk = (round(pt.X(), weld_decimals), round(pt.Y(), weld_decimals),
              round(pt.Z(), weld_decimals))
        gi = weld.get(pk)
        if gi is None:
            gi = len(points)
            weld[pk] = gi
            points.append([pt.X(), pt.Y(), pt.Z()])
        nn = (0.0, 0.0, 1.0)
        if props is not None:
            props.SetParameters(uu, vv)
            if props.IsNormalDefined():
                dd = props.Normal().Transformed(trsf)
                nn = (dd.X(), dd.Y(), dd.Z())
        l2g[nid] = gi
        nrm[nid] = (-nn[0], -nn[1], -nn[2]) if flipped else nn
        if uvx is not None:
            # the later passes (diagonal flip, quad pairing) look these up
            uvx[nid] = (uu, vv)
        refs.append(nid)
        pts.append([float(xy[0]), float(xy[1])])
        nid += 1

    arr = np.asarray(pts)
    if len(arr) < 3:
        return None
    try:
        dl = Delaunay(arr)
    except Exception:
        SPHERE_STATS["apex fix: delaunay failed"] += 1
        return None
    cons = [(k, (k + 1) % len(loop)) for k in range(len(loop))]
    got = _recover_constraints([tuple(int(v) for v in t) for t in dl.simplices],
                               arr, cons)
    if got is None:
        SPHERE_STATS["apex fix: recovery failed"] += 1
        return None
    simp = np.asarray(got, dtype=int)
    cen = arr[simp].mean(axis=1)
    simp = simp[path.contains_points(cen)]
    if len(simp) == 0:
        SPHERE_STATS["apex fix: empty fill"] += 1
        return None
    # the rim must survive, or the patch will not close onto what we kept
    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in cons:
        if ((x, y) if x < y else (y, x)) not in have:
            SPHERE_STATS["apex fix: rim lost"] += 1
            return None
    # Orientation must come from the KEPT triangles, not from the loop's own
    # area. The rim walk picks an arbitrary direction, and getting this wrong
    # does not tear the mesh -- it produces WINDING errors, which leaves the
    # body sealed but not `watertight`, so the fallback cascade throws the whole
    # body back to plain OCCT and the fan comes straight back. That is exactly
    # what happened here: 116 winding errors, the fix silently discarded, and
    # the apex still a fan on screen while the counters said "ok".
    #
    # The rule: whatever direction a kept triangle traverses a rim edge, the
    # fill must traverse it the other way, because the fill is on the other
    # side of that edge.
    kept_dir = set()
    for t in keep:
        ct = [canon.get(v, v) for v in t]
        for k in range(len(ct)):
            kept_dir.add((ct[k], ct[(k + 1) % len(ct)]))

    fill = []
    for a, b, c in simp:
        fill.append((refs[a], refs[b], refs[c]))
    flip_all = False
    for t in fill:
        done = False
        for k in range(3):
            e = (t[k], t[(k + 1) % 3])
            if e in kept_dir:            # same direction as a kept triangle
                flip_all = True          # -> the fill is wound backwards
                done = True
                break
            if (e[1], e[0]) in kept_dir:  # opposite: correct
                done = True
                break
        if done:
            break
    out = list(keep)
    for t in fill:
        out.append((t[0], t[2], t[1]) if flip_all else t)
    SPHERE_STATS["apex fix: ok"] += 1
    return out


CONE_STATS = collections.Counter()


def _cone_fan(face, tri, loc, params):
    """Mesh a full cone that reaches its APEX as a radial fan of rings.

    Benn's construction, and the target is the bottom of a drilled hole: a full
    revolution from the apex out to one circle, which OCCT hands back as an
    unstructured mess. The right answer is the obvious one -- concentric circles
    around the apex, joined by quads, with a fan of triangles closing the
    middle.

    The subtlety is how the ring resolution changes on the way out. Accuracy
    around a circle needs a count that grows with radius, but changing it at an
    arbitrary place leaves a ragged row. So the count only ever DOUBLES, and
    only at a ring where doubling is actually needed, which makes the transition
    exactly "every edge splits in two": one inner node feeds two outer segments.
    Every ring is a perfect circle, perpendicular to the axis, so every added
    vertex is exactly on the surface.

    Radial spacing is free: a cone is ruled along its generatrix, so a straight
    span from one ring to the next lies exactly on the surface at any spacing.
    It is chosen purely to keep cells square.

    The OUTER ring is OCCT's own boundary and is used verbatim -- it is shared
    with the neighbouring face, and moving it would open a hole. Everything
    inside is ours.

    Returns (refs, polys, constraints) like _grid_trim, or None to decline.
    """
    CONE_STATS["attempt"] += 1
    sa = BRepAdaptor_Surface(face)
    if sa.GetType() != GeomAbs_SurfaceType.GeomAbs_Cone:
        return None
    try:
        cone = sa.Cone()
        semi = cone.SemiAngle()
        ref_r = cone.RefRadius()
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    except Exception:
        return None
    if abs(math.sin(semi)) < 1e-9:
        CONE_STATS["degenerate angle"] += 1
        return None
    if (u1 - u0) < 2.0 * math.pi - 1e-6:
        CONE_STATS["not a full revolution"] += 1
        return None

    # the face's edges: one real outer circle, and a degenerate apex
    real, degen = [], 0
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge_s(exp.Current())
        exp.Next()
        if BRep_Tool.Degenerated_s(e):
            degen += 1
        elif not BRep_Tool.IsClosed_s(e, face):
            real.append(e)
    if degen < 1 or len(real) != 1:
        CONE_STATS["not an apex cap"] += 1
        return None

    poly = BRep_Tool.PolygonOnTriangulation_s(real[0], tri, loc)
    if poly is None:
        CONE_STATS["no boundary polygon"] += 1
        return None
    idx = [poly.Nodes().Value(i) for i in range(1, poly.Nodes().Length() + 1)]
    if not tri.HasUVNodes() or len(idx) < 4:
        CONE_STATS["no uv"] += 1
        return None
    uv = {}
    for r in idx:
        p2 = tri.UVNode(r)
        uv[r] = (p2.X(), p2.Y())
    vs = [uv[r][1] for r in idx]
    v_out = sum(vs) / len(vs)
    dv_all = max(vs) - min(vs)
    if dv_all > max(abs(v1 - v0), 1e-12) * 1e-3:
        CONE_STATS["rim not a circle"] += 1
        return None

    def radius(v):
        return abs(ref_r + v * math.sin(semi))

    R = radius(v_out)
    v_apex = -ref_r / math.sin(semi)
    if R <= 1e-9 or abs(v_out - v_apex) <= 1e-9:
        CONE_STATS["no radius"] += 1
        return None

    # The rim in angular order, dropping the seam duplicate. Dedupe by 3D
    # POSITION, not by u: a full revolution ends at u=2*pi, whose node is the
    # same point in space as the one at u=0 but is a different UV node, and
    # keeping both makes the patch validator reject the face as seam-degenerate
    # once the two weld together.
    order, seen_pos = [], set()
    for r in idx:
        q = tri.Node(r)
        k = (round(q.X(), 7), round(q.Y(), 7), round(q.Z(), 7))
        if k in seen_pos:
            continue
        seen_pos.add(k)
        order.append(r)
    N = len(order)
    if N < 6:
        CONE_STATS["rim too coarse"] += 1
        return None

    n_ang = max(3, int(math.ceil(360.0 / max(params.angle_deg, 1e-6))))

    def needed(r):
        """Segments a circle of radius r needs to meet BOTH tolerances."""
        n = n_ang
        d = params.deflection
        if d > 0 and r > d:
            n = max(n, int(math.ceil(math.pi / math.acos(max(-1.0,
                                     min(1.0, 1.0 - d / r))))))
        return max(3, n)

    # How many rings, decided ONCE from the generatrix length and the rim's own
    # spacing. Stepping inward by the local arc length instead is the obvious
    # thing and it is wrong: the step shrinks with the radius, so v approaches
    # the apex geometrically and never reaches it -- measured 54 rings of
    # invisible detail on a 2.1 mm drill point before this was bounded.
    L = abs(v_out - v_apex)
    arc_rim = 2.0 * math.pi * R / N
    bands = max(1, min(16, int(math.ceil(L / max(1.5 * arc_rim, 1e-12)))))
    sgn = 1.0 if v_apex > v_out else -1.0

    def count_at(r):
        """N halved as many times as the radius has earned.

        Only ever N, N/2, N/4... so a transition is exactly "every edge splits
        in two". It can never go below the angle limit: on a cone the normal
        sweeps the full turn however small the circle, so the count needed for
        normal deviation does not shrink as the apex is approached.
        """
        n = N
        while (n % 2 == 0 and n // 2 >= n_ang and needed(r) <= n // 2):
            n //= 2
        return n

    rings = [(v_out, N, None)]          # (v, count, None = use the rim nodes)
    for k in range(1, bands):
        v_k = v_out + sgn * L * k / bands
        rings.append((v_k, count_at(radius(v_k)), True))
    if len(rings) < 2:
        CONE_STATS["one band only"] += 1

    # ---- materialise -------------------------------------------------------
    refs, cols = [], []
    for k, (v_k, n_k, synth) in enumerate(rings):
        col = []
        if synth is None:
            for r in order:
                col.append(len(refs))
                refs.append(int(r))
        else:
            base = uv[order[0]][0]
            for j in range(n_k):
                col.append(len(refs))
                refs.append((base + 2.0 * math.pi * j / n_k, v_k))
        cols.append(col)
    apex = len(refs)
    refs.append((uv[order[0]][0], v_apex))

    polys = []
    for k in range(len(cols) - 1):
        a, b = cols[k], cols[k + 1]         # a = outer, b = inner
        if len(b) == len(a):
            for j in range(len(a)):
                j2 = (j + 1) % len(a)
                polys.append((b[j], b[j2], a[j2], a[j]))
        else:                               # 2:1 -- every edge splits in two
            m, na = len(b), len(a)
            for j in range(m):
                o0, o1, o2 = 2 * j, (2 * j + 1) % na, (2 * j + 2) % na
                polys.append((b[j], a[o1], a[o0]))
                polys.append((b[j], a[o2], a[o1]))
                polys.append((b[j], b[(j + 1) % m], a[o2]))
    last = cols[-1]
    for j in range(len(last)):
        polys.append((apex, last[(j + 1) % len(last)], last[j]))

    # Normalise the winding in PARAMETER space. Whether the apex is at greater
    # or smaller v depends on how the cone was authored, and getting it wrong
    # does not tear anything -- it produces winding errors, which sends the
    # whole BODY back to plain OCCT through the cascade while the counters still
    # say the fan was built. Same trap as the sphere apex fill.
    def _uv_of(i):
        r = refs[i]
        if isinstance(r, int):
            q = tri.UVNode(r)
            return q.X(), q.Y()
        return r

    area = 0.0
    for poly in polys:
        pts = [_uv_of(i) for i in poly]
        for t in range(len(pts)):
            x1, y1 = pts[t]
            x2, y2 = pts[(t + 1) % len(pts)]
            area += x1 * y2 - x2 * y1
    if area < 0:
        polys = [tuple(reversed(q)) for q in polys]

    # The rim is the patch's one boundary loop, and it has to be DECLARED:
    # _patch_is_sound compares the edges used exactly once against this set, so
    # returning no constraints makes the face's own boundary read as an error.
    rim_cons = [(cols[0][j], cols[0][(j + 1) % N]) for j in range(N)]
    CONE_STATS["ok"] += 1
    CONE_STATS[f"rings={len(cols)}"] += 1
    return refs, polys, rim_cons


def _sphere_cap(face, tri, loc, params):
    """Apex-outward cap, backing off to a coarser ring spacing if needed.

    Constraint recovery fails when interior points crowd a boundary that
    wanders close to them, and giving up throws away the whole construction for
    a face OCCT will then fan badly. Coarsening the rings thins the crowd; even
    a coarse concentric cap beats a fan from the parameter pole.
    """
    for c in (1.0, 1.5, 2.5, 4.0):
        got = _sphere_cap_once(face, tri, loc, params, coarsen=c)
        if got is not None:
            return got
    return None


def _sphere_cap_once(face, tri, loc, params, coarsen=1.0):
    """Mesh a spherical face by walking OUTWARD from its apex.

    Benn's construction, and it is the right one for a sphere. OCCT fans a cap
    from the PARAMETER pole -- a point usually nowhere near the middle of the
    patch -- so every triangle is a needle meeting at one vertex, which is the
    "deep peaks and valleys" in the shading. A plain UV grid is little better:
    its meridians crowd together as they approach the pole.

    Instead: find the apex (the middle of THIS patch, derived from its own
    boundary), lay concentric rings around it, and give each ring the number of
    points its own circumference deserves -- about 2*pi*sin(theta)*R/step -- so
    segments stay one step long everywhere and cells stay near-equilateral.
    That is the icosphere-like grading Benn asked for, without an icosphere's
    fixed subdivision, and it works on a cap sitting anywhere on the sphere.

    Ring points falling outside the trim curve are culled, and the whole set --
    boundary plus surviving ring points -- goes through one constrained
    Delaunay. The concentric structure comes from WHERE the points are, so it
    survives without having to emit cells explicitly and then fit a rim, which
    is what kept failing on these wandering trimmed boundaries.

    The step also never exceeds the boundary's own spacing: a shallow cap whose
    rim carries 40 nodes needs rings, or the apex fan gives 40 needles.

    Returns (refs, polys, constraints) like _grid_trim, or None to decline.
    """
    SPHERE_STATS["attempt"] += 1
    try:
        import numpy as np
        from scipy.spatial import Delaunay, cKDTree
        from matplotlib.path import Path
    except Exception:
        return None

    sa = BRepAdaptor_Surface(face)
    if sa.GetType() != GeomAbs_SurfaceType.GeomAbs_Sphere:
        return None
    sph = sa.Sphere()
    R = sph.Radius()
    if R <= 0:
        return None
    pos = sph.Position()
    C = pos.Location()
    EX = (pos.XDirection().X(), pos.XDirection().Y(), pos.XDirection().Z())
    EY = (pos.YDirection().X(), pos.YDirection().Y(), pos.YDirection().Z())
    EZ = (pos.Direction().X(), pos.Direction().Y(), pos.Direction().Z())
    CC = (C.X(), C.Y(), C.Z())

    loops = _face_loops(face, tri, loc)
    if not loops:
        SPHERE_STATS["no loops"] += 1
        return None
    nodes = [n for lp in loops for n in lp]
    if len(nodes) < 3 or len(nodes) > 20000:
        SPHERE_STATS["node count"] += 1
        return None

    def _dir_of(node):
        q = tri.Node(node)
        w = (q.X() - CC[0], q.Y() - CC[1], q.Z() - CC[2])
        n = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
        return None if n <= 0 else (w[0] / n, w[1] / n, w[2] / n)

    dmap = {}
    for n in nodes:
        d = _dir_of(n)
        if d is None:
            SPHERE_STATS["degenerate node"] += 1
            return None
        dmap[n] = d

    outer_nodes = loops[0]
    ax = [sum(dmap[n][i] for n in outer_nodes) / len(outer_nodes) for i in range(3)]
    an = math.sqrt(sum(c * c for c in ax))
    if an < 0.2:
        SPHERE_STATS["no apex"] += 1     # patch too big to have a sensible centre
        return None
    AX = tuple(c / an for c in ax)

    tmp = EX if abs(sum(AX[i] * EX[i] for i in range(3))) < 0.9 else EY
    ux = (AX[1] * tmp[2] - AX[2] * tmp[1],
          AX[2] * tmp[0] - AX[0] * tmp[2],
          AX[0] * tmp[1] - AX[1] * tmp[0])
    un = math.sqrt(sum(c * c for c in ux))
    if un <= 0:
        return None
    U = tuple(c / un for c in ux)
    V = (AX[1] * U[2] - AX[2] * U[1],
         AX[2] * U[0] - AX[0] * U[2],
         AX[0] * U[1] - AX[1] * U[0])

    def _polar(d):
        c = max(-1.0, min(1.0, sum(d[i] * AX[i] for i in range(3))))
        th = math.acos(c)
        x = sum(d[i] * U[i] for i in range(3))
        y = sum(d[i] * V[i] for i in range(3))
        return th, math.atan2(y, x)

    pol = {n: _polar(dmap[n]) for n in nodes}
    th_max = max(t for t, _ in pol.values())

    step = math.radians(max(params.angle_deg, 1.0))
    if 0 < params.deflection < 2.0 * R:
        step = min(step, 2.0 * math.acos(max(-1.0, 1.0 - params.deflection / R)))
    seg = []
    for lp in loops:
        for k in range(len(lp)):
            d = tri.Node(lp[k]).Distance(tri.Node(lp[(k + 1) % len(lp)]))
            if d > 0:
                seg.append(d)
    if seg:
        seg.sort()
        step = min(step, seg[len(seg) // 2] / R)
    step *= coarsen
    if params.min_size and params.min_size > 0:
        step = max(step, params.min_size / R)
    if step <= 1e-9 or th_max <= step:
        SPHERE_STATS["no room"] += 1
        return None
    # The azimuthal projection is only injective within a hemisphere. Body 158's
    # big sphere reaches 134 degrees from its apex -- it is a spherical BAND
    # with a hole, not a cap -- and its projected boundary self-intersects in 24
    # places, so the Delaunay below can only ever fail. Decline early and let
    # the band paths have it.
    if th_max > 1.45:
        SPHERE_STATS["not a cap"] += 1
        return None

    def _uv_of(th, ph):
        st_, ct = math.sin(th), math.cos(th)
        d = tuple(AX[i] * ct + (U[i] * math.cos(ph) + V[i] * math.sin(ph)) * st_
                  for i in range(3))
        z = max(-1.0, min(1.0, sum(d[i] * EZ[i] for i in range(3))))
        u = math.atan2(sum(d[i] * EY[i] for i in range(3)),
                       sum(d[i] * EX[i] for i in range(3)))
        return (u + 2.0 * math.pi if u < 0.0 else u), math.asin(z)

    # candidate interior points: the apex, then a ring every `step` outward,
    # each ring sampled so its own segments are also about `step` long
    cand = [(0.0, 0.0)]
    k = 1
    while step * k < th_max and k <= 400:
        th = step * k
        n_k = max(3, int(round(2.0 * math.pi * math.sin(th) / step)))
        if len(cand) + n_k > 60000:
            break
        for j in range(n_k):
            cand.append((th, 2.0 * math.pi * j / n_k))
        k += 1
    if len(cand) < 2:
        SPHERE_STATS["no room"] += 1
        return None

    def _proj(th, ph):
        return (th * math.cos(ph), th * math.sin(ph))

    bproj = {n: _proj(*pol[n]) for n in nodes}
    outer = Path([bproj[n] for n in loops[0]])
    holes = [Path([bproj[n] for n in lp]) for lp in loops[1:]]
    cxy = np.array([_proj(t, p_) for t, p_ in cand])
    keep = outer.contains_points(cxy, radius=-0.35 * step)
    for h in holes:
        keep &= ~h.contains_points(cxy, radius=0.35 * step)
    # and clear of the boundary polyline itself, or Delaunay cannot recover it
    dense = []
    for lp in loops:
        for i2 in range(len(lp)):
            x1, y1 = bproj[lp[i2]]
            x2, y2 = bproj[lp[(i2 + 1) % len(lp)]]
            m_ = max(1, int(math.hypot(x2 - x1, y2 - y1) / (0.3 * step)))
            for t2 in range(m_):
                f = t2 / m_
                dense.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
    if dense and len(cxy):
        dd, _ = cKDTree(np.asarray(dense)).query(cxy, k=1)
        keep &= dd > 0.5 * step
    inside = [cand[i] for i in range(len(cand)) if keep[i]]
    if not inside:
        SPHERE_STATS["no interior"] += 1
        return None

    refs = list(nodes) + [_uv_of(t, p_) for t, p_ in inside]
    pts = np.array([bproj[n] for n in nodes] + [_proj(t, p_) for t, p_ in inside])
    try:
        dl = Delaunay(pts)
    except Exception:
        SPHERE_STATS["delaunay failed"] += 1
        return None
    idx_of = {n: i for i, n in enumerate(nodes)}
    cons = []
    for lp in loops:
        for k2 in range(len(lp)):
            a, b = idx_of[lp[k2]], idx_of[lp[(k2 + 1) % len(lp)]]
            if a != b:
                cons.append((a, b))
    tris = _recover_constraints([tuple(int(v) for v in t) for t in dl.simplices],
                                pts, cons)
    if tris is None:
        SPHERE_STATS["recovery failed"] += 1
        return None
    simp = np.asarray(tris, dtype=int)
    cen = pts[simp].mean(axis=1)
    good = outer.contains_points(cen)
    for h in holes:
        good &= ~h.contains_points(cen)
    simp = simp[good]
    if len(simp) == 0:
        SPHERE_STATS["all filtered"] += 1
        return None
    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in cons:
        if ((x, y) if x < y else (y, x)) not in have:
            SPHERE_STATS["constraint lost"] += 1
            return None
    polys = []
    for a, b, c in simp:
        pa, pb, pc = pts[a], pts[b], pts[c]
        if ((pb[0] - pa[0]) * (pc[1] - pa[1])
                - (pb[1] - pa[1]) * (pc[0] - pa[0])) < 0:
            a, c = c, a
        polys.append((int(a), int(b), int(c)))
    if not _patch_is_sound(polys, cons):
        SPHERE_STATS["patch unsound"] += 1
        return None
    SPHERE_STATS["ok"] += 1
    SPHERE_STATS["ring points"] += len(inside)
    return refs, polys, cons


def _grid_trim(face, tri, loc, params, max_points: int = 120000):
    """Mesh the UNTRIMMED surface on a regular UV grid, then trim it to the face.

    This is the reverse of _lattice_remesh, and it is what Fusion 360 does.
    The lattice starts from OCCT's Delaunay mesh and tries to RECOVER structure
    by flipping edges; recovery fails on most trimmed faces (5 of 28 succeed on
    the valve's body 138), and a fillet that loses it falls back to an anarchic
    fan. Here the structure exists from the first step and only the rim has to
    be fitted, so regularity is a property of the construction rather than
    something that has to survive a repair.

      * cells lying entirely clear of the boundary are emitted as QUADS on the
        grid -- parallel to the iso-lines in both directions, by construction;
      * the rim between those cells and the face's real boundary is filled by
        constrained Delaunay, exactly as the lattice does;
      * the boundary polylines are reused VERBATIM, so neighbours still match.
        Everything a neighbour can see is untouched; only the interior is ours.

    Returns (refs, polys, constraints) like _lattice_remesh, or None to decline.
    """
    GRID_STATS["attempt"] += 1
    try:
        import numpy as np
        from scipy.spatial import Delaunay, cKDTree
        from matplotlib.path import Path
    except Exception:
        return None

    loops = _face_loops(face, tri, loc)
    if not loops:
        GRID_STATS["no loops"] += 1
        return None
    n_nodes = tri.NbNodes()
    uv = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y()) for i in range(1, n_nodes + 1)}
    bnodes, seen = [], set()
    for lp in loops:
        for x in lp:
            if x not in seen:
                seen.add(x)
                bnodes.append(x)
    if len(bnodes) < 4 or len(bnodes) > 20000:
        GRID_STATS["node count"] += 1
        return None

    us = [uv[i][0] for i in bnodes]
    vs = [uv[i][1] for i in bnodes]
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    du, dv = umax - umin, vmax - vmin
    if du <= 0 or dv <= 0:
        GRID_STATS["degenerate uv"] += 1
        return None

    # Outer wire first: TopExp_Explorer returns wires in no order, and taking a
    # hole as the outer boundary inverts every inside test.
    if len(loops) > 1:
        def _area(lp):
            a = 0.0
            for k in range(len(lp)):
                x1, y1 = uv[lp[k]]
                x2, y2 = uv[lp[(k + 1) % len(lp)]]
                a += x1 * y2 - x2 * y1
            return abs(a)
        loops.sort(key=_area, reverse=True)

    # u and v scaled to arc length, so "square cell" means square on the SURFACE
    sa = BRepAdaptor_Surface(face)
    try:
        um, vm = 0.5 * (umin + umax), 0.5 * (vmin + vmax)
        eu, ev = du * 1e-3, dv * 1e-3
        su = sa.Value(um, vm).Distance(sa.Value(um + eu, vm)) / eu
        sv = sa.Value(um, vm).Distance(sa.Value(um, vm + ev)) / ev
    except Exception:
        GRID_STATS["surface eval"] += 1
        return None
    if su <= 0 or sv <= 0:
        GRID_STATS["scale"] += 1
        return None

    # Grid resolution: curvature in each direction, then relaxed until neither
    # side of a cell is more than grid_aspect times the other. Sizing u and v
    # independently is what makes a cylinder come out as square-ish quads
    # instead of the tall ribbons the ruled band produces.
    ang = math.radians(params.angle_deg)
    n_v = max(1, _rows_for_curvature(sa, um, vmin, vmax, params.deflection, ang))
    n_u = max(1, _cols_for_curvature(sa, vm, umin, umax, params.deflection, ang))
    arc_u, arc_v = du * su, dv * sv
    asp = max(float(params.grid_aspect), 1.0)
    if arc_v > 0 and n_v > 0:
        n_u = max(n_u, int(math.ceil(arc_u / max(arc_v / n_v * asp, 1e-12))))
    if arc_u > 0 and n_u > 0:
        n_v = max(n_v, int(math.ceil(arc_v / max(arc_u / n_u * asp, 1e-12))))
    # Size by MEASURED sag, like _ruled_rows. The curvature estimate assumes an
    # even parameterisation, which a B-spline does not have, so the grid came
    # out too coarse and the whole patch was then thrown away by the sag gate --
    # "sags past deflection" was the single biggest exit on body 158's fillets.
    # Probe the CELL DIAGONAL: it is the longest edge emitted and it bows in
    # both directions at once.
    if params.deflection > 0:
        for _ in range(8):
            worst = 0.0
            try:
                for a in range(3):
                    for b in range(3):
                        ua = umin + du * (a + 0.5) / 3
                        va = vmin + dv * (b + 0.5) / 3
                        ub = ua + du / n_u
                        vb = va + dv / n_v
                        pa = sa.Value(ua, va)
                        pb = sa.Value(ub, vb)
                        pm = sa.Value(0.5 * (ua + ub), 0.5 * (va + vb))
                        d = math.sqrt(
                            ((pa.X() + pb.X()) * 0.5 - pm.X()) ** 2
                            + ((pa.Y() + pb.Y()) * 0.5 - pm.Y()) ** 2
                            + ((pa.Z() + pb.Z()) * 0.5 - pm.Z()) ** 2)
                        if d > worst:
                            worst = d
            except Exception:
                break
            if worst <= params.deflection or max(n_u, n_v) >= 2000:
                break
            g = math.sqrt(worst / params.deflection)
            n_u = min(2000, max(n_u + 1, int(n_u * g) + 1))
            n_v = min(2000, max(n_v + 1, int(n_v * g) + 1))

    # A stripe narrower than one cell gets every grid point culled and exits as
    # "no interior" -- 37 of 90 faces on body 31, and they are exactly the thin
    # fillets. Give the NARROW direction a few divisions regardless: that is
    # what puts lines down the length of a fillet instead of nothing.
    if arc_u > 0 and arc_v > 0:
        if arc_v < arc_u:
            n_v = max(n_v, 4)
        else:
            n_u = max(n_u, 4)

    n_u, n_v = min(n_u, 2000), min(n_v, 2000)
    if (n_u + 1) * (n_v + 1) > max_points:
        GRID_STATS["too big"] += 1
        return None

    gu = [umin + du * i / n_u for i in range(n_u + 1)]
    gv = [vmin + dv * j / n_v for j in range(n_v + 1)]
    cu, cv = du / n_u, dv / n_v            # cell size in PARAMETER space
    cell_arc = min(cu * su, cv * sv)

    outer = Path([(uv[i][0] * su, uv[i][1] * sv) for i in loops[0]])
    holes = [Path([(uv[i][0] * su, uv[i][1] * sv) for i in lp]) for lp in loops[1:]]

    cells_ij = [(i, j) for i in range(n_u + 1) for j in range(n_v + 1)]
    pts_grid = np.array([[gu[i] * su, gv[j] * sv] for i, j in cells_ij])
    margin = -0.25 * cell_arc
    ins = outer.contains_points(pts_grid, radius=margin)
    for h in holes:
        ins &= ~h.contains_points(pts_grid, radius=-margin)

    # Keep grid points clear of the boundary POLYLINE, not merely of its
    # nodes. A point sitting close to the middle of a long boundary segment is
    # what blocks constrained edge recovery -- the segment cannot be flipped
    # into the triangulation because that point is in the way -- and culling
    # only near-coincident NODES left 8 of 20 faces failing recovery. Sampling
    # the segments and culling against those samples fixes the cause rather than
    # the symptom, and costs at most a slightly wider Delaunay rim.
    bxy = np.array([[uv[i][0] * su, uv[i][1] * sv] for i in bnodes])
    dense = []
    step = 0.25 * cell_arc
    for lp in loops:
        for k in range(len(lp)):
            x1, y1 = uv[lp[k]][0] * su, uv[lp[k]][1] * sv
            nx = lp[(k + 1) % len(lp)]
            x2, y2 = uv[nx][0] * su, uv[nx][1] * sv
            seg = math.hypot(x2 - x1, y2 - y1)
            n_s = max(1, int(seg / step)) if step > 0 else 1
            for t in range(n_s):
                f = t / n_s
                dense.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
    if not dense:
        GRID_STATS["no boundary samples"] += 1
        return None
    dist, _ = cKDTree(np.asarray(dense)).query(pts_grid, k=1)
    ok = ins & (dist > 0.55 * cell_arc)

    usable = [ij for k, ij in enumerate(cells_ij) if ok[k]]
    if not usable:
        GRID_STATS["no interior"] += 1
        return None
    gidx = {ij: len(bnodes) + k for k, ij in enumerate(usable)}

    # Cells the boundary passes through cannot be plain quads. Marking the whole
    # bounding box of each boundary segment is deliberately conservative: it
    # widens the Delaunay rim by at most a cell and can only cost quads, never
    # correctness.
    blocked = set()
    for lp in loops:
        for k in range(len(lp)):
            u1, v1 = uv[lp[k]]
            u2, v2 = uv[lp[(k + 1) % len(lp)]]
            i0 = max(0, min(n_u - 1, int((min(u1, u2) - umin) / cu) - 1))
            i1 = max(0, min(n_u - 1, int((max(u1, u2) - umin) / cu) + 1))
            j0 = max(0, min(n_v - 1, int((min(v1, v2) - vmin) / cv) - 1))
            j1 = max(0, min(n_v - 1, int((max(v1, v2) - vmin) / cv) + 1))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    blocked.add((i, j))

    cells = []
    for i in range(n_u):
        for j in range(n_v):
            if (i, j) in blocked:
                continue
            if all((i + a, j + b) in gidx
                   for a, b in ((0, 0), (1, 0), (1, 1), (0, 1))):
                cells.append((i, j))
    cellset = set(cells)

    pts = np.vstack([bxy, np.array([[gu[i] * su, gv[j] * sv]
                                    for i, j in usable])])
    if len(pts) < 3:
        GRID_STATS["too few points"] += 1
        return None
    try:
        dl = Delaunay(pts)
    except Exception:
        GRID_STATS["delaunay failed"] += 1
        return None

    idx_of = {node: k for k, node in enumerate(bnodes)}
    constraints = []
    for lp in loops:
        for k in range(len(lp)):
            x, y = idx_of[lp[k]], idx_of[lp[(k + 1) % len(lp)]]
            if x != y:
                constraints.append((x, y))
    tri_list = _recover_constraints(
        [tuple(int(v) for v in t) for t in dl.simplices], pts, constraints)
    if tri_list is None:
        GRID_STATS["recovery failed"] += 1
        return None

    simp = np.asarray(tri_list, dtype=int)
    cen = pts[simp].mean(axis=1)
    keep = outer.contains_points(cen)
    for h in holes:
        keep &= ~h.contains_points(cen)
    # Drop the Delaunay triangles falling inside a cell we emit as a quad. Every
    # corner of such a cell is a Delaunay site, so its triangles are exactly the
    # two halves of the cell: nothing straddles the seam between the quad block
    # and the rim, and the two tilings meet exactly.
    if cellset:
        ci = np.floor((cen[:, 0] / su - umin) / cu).astype(int)
        cj = np.floor((cen[:, 1] / sv - vmin) / cv).astype(int)
        inq = np.array([(int(a), int(b)) in cellset for a, b in zip(ci, cj)])
        keep &= ~inq
    simp = simp[keep]

    have = set()
    for a, b, c in simp:
        for x, y in ((a, b), (b, c), (c, a)):
            have.add((x, y) if x < y else (y, x))
    for x, y in constraints:
        if ((x, y) if x < y else (y, x)) not in have:
            GRID_STATS["constraint lost"] += 1
            return None

    refs = list(bnodes) + [(float(gu[i]), float(gv[j])) for i, j in usable]
    polys = []
    for a, b, c in simp:
        pa, pb, pc = pts[a], pts[b], pts[c]
        if ((pb[0] - pa[0]) * (pc[1] - pa[1])
                - (pb[1] - pa[1]) * (pc[0] - pa[0])) < 0:
            a, c = c, a
        polys.append((int(a), int(b), int(c)))

    uvmap = {k: (uv[r] if isinstance(r, int) else r) for k, r in enumerate(refs)}
    polys = _flip_toward_axis(polys, uvmap, sa=sa,
                              max_sag=params.deflection)
    if params.quads:
        polys = _pair_axial_quads(polys, uvmap)
    for i, j in cells:
        polys.append((gidx[(i, j)], gidx[(i + 1, j)],
                      gidx[(i + 1, j + 1)], gidx[(i, j + 1)]))
    if not params.quads:
        split = []
        for poly in polys:
            if len(poly) == 4:
                a, b, c, d = poly
                split.append((a, b, c))
                split.append((a, c, d))
            else:
                split.append(poly)
        polys = split

    if not _patch_is_sound(polys, constraints):
        GRID_STATS["patch unsound"] += 1
        return None
    # Same chord test as the ruled grid: the rim Delaunay here is free to draw
    # a long edge straight across a curved patch, and on a spherical cap that
    # is exactly how a valley appears.
    if params.deflection > 0:
        uvp = [[uvmap[i] for i in poly] for poly in polys]
        if _max_chord_sag(uvp, sa, limit=params.deflection) > params.deflection:
            GRID_STATS["sags past deflection"] += 1
            return None
    # Self-policing, exactly like the ruled grid. Benn's verdict on this path
    # was that it made things worse more often than better, and he was right --
    # but it is also the ONLY path that can take a genuinely TRIMMED stripe (a
    # fillet whose long edge is cut by other geometry), which is what is left to
    # solve. So keep it, and let it run only where it measurably beats OCCT on
    # the same face, instead of being switched on or off for the whole model.
    if params.auto_grid:
        try:
            uvs = {i: (tri.UVNode(i).X(), tri.UVNode(i).Y())
                   for i in range(1, tri.NbNodes() + 1)}
            plain = _flip_toward_axis(
                [tri.Triangle(t).Get() for t in range(1, tri.NbTriangles() + 1)],
                uvs, sa=sa, max_sag=params.deflection)
            b_polys = [[(uvs[i][0] * su, uvs[i][1] * sv) for i in t]
                       for t in plain]
            a_polys = [[(uvmap[i][0] * su, uvmap[i][1] * sv) for i in poly]
                       for poly in polys]
            # Strict bar: this path only earns a face by beating OCCT clearly.
            # At a 2% margin it took too many and made the model worse overall
            # (valve cylinders 90.6 -> 82.2%); the faces it should win are the
            # trimmed stripes, where it wins by a mile or not at all.
            w = min(1.0, max(0.0, float(getattr(params, "grid_bias", 0.5))))
            sa_ = w * _iso_fraction(a_polys) + (1.0 - w) * _regularity(a_polys)
            sb_ = w * _iso_fraction(b_polys) + (1.0 - w) * _regularity(b_polys)
            LAST_GRID_SCORE[0] = sa_
            if b_polys and sa_ < sb_ + 0.05:
                GRID_STATS["lost to plain"] += 1
                return None
        except Exception:
            pass
    GRID_STATS["ok"] += 1
    GRID_STATS["quad cells"] += len(cells)
    return refs, polys, constraints


def _patch_is_sound(polys, constraints) -> bool:
    und, dirc = collections.Counter(), collections.Counter()
    for poly in polys:
        n = len(poly)
        if len(set(poly)) != n:
            return False                 # repeated corner: degenerate polygon
        for k in range(n):
            a, b = poly[k], poly[(k + 1) % n]
            und[(a, b) if a < b else (b, a)] += 1
            dirc[(a, b)] += 1
    if any(c > 2 for c in und.values()):
        return False                     # non-manifold inside the face
    if any(c > 1 for c in dirc.values()):
        return False                     # inconsistent winding
    want = {(x, y) if x < y else (y, x) for x, y in constraints}
    got = {e for e, c in und.items() if c == 1}
    return got == want


def _pair_axial_quads(tris, uv, tol_ratio: float = 0.02):
    """Merge triangle pairs whose shared edge is the DIAGONAL of an axis-aligned
    cell, so a lattice comes out as quads with two exactly-axial sides."""
    if len(tris) < 2:
        return [tuple(t) for t in tris]

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    def directed(t, x, y):
        return (t[0] == x and t[1] == y) or (t[1] == x and t[2] == y) \
            or (t[2] == x and t[0] == y)

    us = [p[0] for p in uv.values()]
    span = (max(us) - min(us)) or 1.0
    tol = span * tol_ratio

    edges = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            x, y = t[k], t[(k + 1) % 3]
            edges.setdefault((x, y) if x < y else (y, x), []).append(ti)

    cands = []
    for (e0, e1), owners in edges.items():
        if len(owners) != 2:
            continue
        t1, t2 = owners
        A, B = tris[t1], tris[t2]
        if directed(A, e0, e1):
            P, Q = e0, e1
        elif directed(A, e1, e0):
            P, Q = e1, e0
        else:
            continue
        if not directed(B, Q, P):
            continue
        b_ = next((v for v in A if v not in (P, Q)), None)
        d_ = next((v for v in B if v not in (P, Q)), None)
        if b_ is None or d_ is None or b_ == d_:
            continue
        quad = (P, d_, Q, b_)
        pts = [uv[v] for v in quad]
        s = [cross(pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]) for i in range(4)]
        if not (all(x > 0 for x in s) or all(x < 0 for x in s)):
            continue
        # the two sides that should be axial: quad[0]-quad[1] and quad[2]-quad[3]
        ax = (abs(uv[quad[0]][0] - uv[quad[3]][0]) < tol
              and abs(uv[quad[1]][0] - uv[quad[2]][0]) < tol)
        if not ax:
            continue
        cands.append((abs(uv[P][0] - uv[Q][0]), t1, t2, quad))

    cands.sort(reverse=True)
    used, out = set(), []
    for _, t1, t2, quad in cands:
        if t1 in used or t2 in used:
            continue
        used.add(t1); used.add(t2)
        out.append(quad)
    for ti, t in enumerate(tris):
        if ti not in used:
            out.append(tuple(t))
    return out


def _np():
    import numpy
    return numpy


def _all_crossing(edges, XY, p, q):
    """Every mesh edge properly crossing pq, computed in one numpy pass.

    This is the fallback inside constraint recovery, and it was the single
    hottest thing in the whole mesher: a pure-Python scan over every edge, run
    again for each flip. On valve body 157 it was 53.9 MILLION orientation
    tests, 36 of the body's 38 seconds -- and because one slow body holds up the
    whole pool, that is what parked the progress bar near the end. Same maths,
    done on arrays.
    """
    np = _np()
    keys = [e for e in edges if p != e[0] and p != e[1]
            and q != e[0] and q != e[1]]
    if not keys:
        return []
    idx = np.asarray(keys, dtype=int)
    A = XY[idx[:, 0]]
    B = XY[idx[:, 1]]
    P = XY[p]
    Q = XY[q]
    eps = 1e-18
    qx, qy = Q[0] - P[0], Q[1] - P[1]
    o1 = qx * (A[:, 1] - P[1]) - qy * (A[:, 0] - P[0])
    o2 = qx * (B[:, 1] - P[1]) - qy * (B[:, 0] - P[0])
    ex = B[:, 0] - A[:, 0]
    ey = B[:, 1] - A[:, 1]
    o3 = ex * (P[1] - A[:, 1]) - ey * (P[0] - A[:, 0])
    o4 = ex * (Q[1] - A[:, 1]) - ey * (Q[0] - A[:, 0])
    s1 = np.where(o1 > eps, 1, np.where(o1 < -eps, -1, 0))
    s2 = np.where(o2 > eps, 1, np.where(o2 < -eps, -1, 0))
    s3 = np.where(o3 > eps, 1, np.where(o3 < -eps, -1, 0))
    s4 = np.where(o4 > eps, 1, np.where(o4 < -eps, -1, 0))
    m = (s1 != 0) & (s2 != 0) & (s3 != 0) & (s4 != 0) & (s1 != s2) & (s3 != s4)
    hit = np.nonzero(m)[0]
    return [keys[i] for i in hit]


def _seg_properly_crosses(p, q, a, b) -> bool:
    """True when segment pq crosses ab in their interiors."""
    def o(u, v, w):
        d = ((v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0]))
        if d > 1e-18:
            return 1
        if d < -1e-18:
            return -1
        return 0
    o1, o2 = o(p, q, a), o(p, q, b)
    o3, o4 = o(a, b, p), o(a, b, q)
    return o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0 and o1 != o2 and o3 != o4


def _recover_constraints(tris, xy, constraints, max_flips: int = 4000):
    """Turn a plain Delaunay triangulation into a CONSTRAINED one.

    For each required edge that is missing, flip a triangulation edge that
    crosses it; each successful flip reduces the crossing count, so it
    terminates. This is what lets a re-meshed face still join its neighbours
    exactly. Returns None if some constraint cannot be recovered, and the caller
    then keeps OCCT's triangulation.

    Crossing edges are found from an adjacency walk, not by rescanning every
    edge: a full rescan per flip made this quadratic and the whole model took
    minutes instead of seconds.
    """
    def key(a, b):
        return (a, b) if a < b else (b, a)

    def directed(t, x, y):
        return (t[0] == x and t[1] == y) or (t[1] == x and t[2] == y)             or (t[2] == x and t[0] == y)

    def cross(o_, a_, b_):
        return ((a_[0] - o_[0]) * (b_[1] - o_[1])
                - (a_[1] - o_[1]) * (b_[0] - o_[0]))

    XY = _np().asarray(xy, dtype=float)
    tris = [tuple(t) for t in tris]
    edges = {}
    at_vertex = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            edges.setdefault(key(t[k], t[(k + 1) % 3]), []).append(ti)
        for v in t:
            at_vertex.setdefault(v, set()).add(ti)

    def crossing_edges(p, q):
        """Edges crossing pq, reached from the triangles around p.

        Falls back to a full scan when the walk finds nothing: the walk only
        steps THROUGH crossing edges, so a crossing that is reachable only past
        a vertex fan is invisible to it, and recovery would give up on a
        perfectly recoverable constraint.
        """
        out = []
        seen = set()
        stack = list(at_vertex.get(p, ()))
        while stack:
            ti = stack.pop()
            if ti in seen:
                continue
            seen.add(ti)
            t = tris[ti]
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                if a in (p, q) or b in (p, q):
                    continue
                if _seg_properly_crosses(xy[p], xy[q], xy[a], xy[b]):
                    e = key(a, b)
                    out.append(e)
                    for nb in edges.get(e, ()):
                        if nb not in seen:
                            stack.append(nb)
        if not out:
            out = _all_crossing(edges, XY, p, q)
        return out

    flips = 0
    for p, q in constraints:
        if key(p, q) in edges:
            continue
        while key(p, q) not in edges and flips < max_flips:
            progressed = False
            for e in crossing_edges(p, q):
                owners = edges.get(e)
                if not owners or len(owners) != 2:
                    continue
                t1, t2 = owners
                A, B = tris[t1], tris[t2]
                e0, e1 = e
                if directed(A, e0, e1):
                    P, Q = e0, e1
                elif directed(A, e1, e0):
                    P, Q = e1, e0
                else:
                    continue
                if not directed(B, Q, P):
                    continue
                b_ = next((v for v in A if v not in (P, Q)), None)
                d_ = next((v for v in B if v not in (P, Q)), None)
                if b_ is None or d_ is None or b_ == d_:
                    continue
                sgn = cross(xy[P], xy[Q], xy[b_])
                n1 = cross(xy[P], xy[d_], xy[b_])
                n2 = cross(xy[Q], xy[b_], xy[d_])
                if sgn == 0 or n1 == 0 or n2 == 0 or                    (n1 > 0) != (sgn > 0) or (n2 > 0) != (sgn > 0):
                    continue            # concave quad: try the next crossing
                for tri_i, old_t in ((t1, A), (t2, B)):
                    for k in range(3):
                        kk = key(old_t[k], old_t[(k + 1) % 3])
                        lst = edges.get(kk)
                        if lst and tri_i in lst:
                            lst.remove(tri_i)
                            if not lst:
                                del edges[kk]
                    for v in old_t:
                        at_vertex[v].discard(tri_i)
                tris[t1] = (P, d_, b_)
                tris[t2] = (Q, b_, d_)
                for tri_i in (t1, t2):
                    t = tris[tri_i]
                    for k in range(3):
                        edges.setdefault(key(t[k], t[(k + 1) % 3]), []).append(tri_i)
                    for v in t:
                        at_vertex.setdefault(v, set()).add(tri_i)
                flips += 1
                progressed = True
                break
            if not progressed:
                break
        if key(p, q) not in edges:
            return None
    return tris
