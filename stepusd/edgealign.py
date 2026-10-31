"""Add vertices to shared edges so a band's rungs land on real boundary nodes.

The problem this solves, in Benn's words: the short cuts across a fillet do not
line up with the vertices on the fillet's boundary, so every rung jogs sideways
where it meets the rim. It is very visible, and it is the thing that forces a
manual repair in Blender.

The cause is that OCCT discretises every edge INDEPENDENTLY. The two rings of a
band therefore do not correspond -- measured on valve body 138, only 3 of 23
full-revolution bands have matching rings, the usual case being the same count
rotated about half a cell out of phase. No strip between two such rings can be
all-quad, and no choice of interior columns can align to both of them.

The fix is Benn's: do not move vertices, ADD them. A vertex placed on the edge's
own curve is geometrically free -- the boundary polyline gets CLOSER to the true
curve, never further, so deviation only improves. What makes it delicate is that
the edge is SHARED: adding the vertex on the band's side only leaves a
T-junction, which is a hole. So the extra parameters are planned per EDGE, and
every face that uses that edge rebuilds its boundary from the same list.

Two properties make that safe:

  * the 3D point is evaluated on the EDGE's curve, not on either surface, so
    both faces produce bit-identical coordinates and weld together;
  * the parameter list is a property of the edge, so both faces get the same
    number of boundary nodes in the same order, and the topological weld can
    still pair them up.

Planning only: applying the plan lives in tessellate.py.
"""
from __future__ import annotations

import math

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

#: surfaces whose faces are meshed as a band and therefore want aligned rungs
_BANDED = frozenset((
    GeomAbs_SurfaceType.GeomAbs_Cylinder,
    GeomAbs_SurfaceType.GeomAbs_Cone,
    GeomAbs_SurfaceType.GeomAbs_Torus,
    GeomAbs_SurfaceType.GeomAbs_Sphere,
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution,
))


def pcurve(edge, face):
    """(2d curve, first, last) of `edge` on `face`, or None.

    OCP quirk: CurveOnSurface_s takes First/Last as ordinary floats and hands
    back only the curve, so the range comes from BRep_Tool.Range_s instead.
    """
    try:
        c2d = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
    except Exception:
        return None
    if c2d is None:
        return None
    try:
        t0, t1 = BRep_Tool.Range_s(edge, face)
    except Exception:
        try:
            t0, t1 = BRep_Tool.Range_s(edge)
        except Exception:
            return None
    if not (t1 > t0):
        return None
    return c2d, t0, t1


def param_at_u(c2d, t0, t1, target, samples: int = 96, iters: int = 40,
               coord: int = 0):
    """Edge parameter where the pcurve reaches coordinate `coord` == target.

    `coord` is 0 for u and 1 for v: a band read transposed wants the other one.

    Bisection on a sampled bracket rather than a solve: u(t) is monotonic along
    a ring edge but not necessarily linear, and this needs no derivatives and
    cannot diverge.
    """
    try:
        us = [(t0 + (t1 - t0) * k / samples) for k in range(samples + 1)]
        vals = [(c2d.Value(t).X() if coord == 0 else c2d.Value(t).Y())
                for t in us]
    except Exception:
        return None
    lo = hi = None
    for k in range(samples):
        a, b = vals[k], vals[k + 1]
        if (a - target) * (b - target) <= 0 and a != b:
            lo, hi = us[k], us[k + 1]
            break
    if lo is None:
        return None
    try:
        for _ in range(iters):
            mid = 0.5 * (lo + hi)
            def _c(t):
                q = c2d.Value(t)
                return q.X() if coord == 0 else q.Y()
            if (_c(lo) - target) * (_c(mid) - target) <= 0:
                hi = mid
            else:
                lo = mid
    except Exception:
        return None
    return 0.5 * (lo + hi)


def sag_params(curve, have, deflection, max_extra=400):
    """Parameters to insert so no chord along the edge bows too far from it.

    Benn's observation, and the fix he proposed. A vertex test proves nothing --
    every vertex is on the curve by construction. What creases the surface is
    the CHORD between two vertices cutting through the solid, and OCCT's own
    edge discretisation leaves some badly out: measured 21.6 mm of sag on one
    handle cylinder against a 0.15 mm tolerance, 144x over, with our code
    entirely disabled.

    So walk each existing segment, measure the gap between the chord's midpoint
    and the curve, and bisect while it is too large. Points land ON the curve,
    so this only ever moves the polyline CLOSER to the true edge. `max_extra`
    stops a pathological curve from exploding the mesh.
    """
    if deflection <= 0 or len(have) < 2:
        return []
    try:
        pts = [curve.Value(t) for t in have]
    except Exception:
        return []
    out = []
    # Carry the endpoints down the recursion instead of re-evaluating them, and
    # skip any chord shorter than 2*deflection outright: a chord can never bow
    # further than half its own length, so those cannot be in violation. On
    # these models that removes the large majority of segments before a single
    # curve evaluation is spent on them.
    lo = 2.0 * deflection
    stack = [(have[i], have[i + 1], pts[i], pts[i + 1])
             for i in range(len(have) - 1)]
    while stack and len(out) < max_extra:
        a, b, pa, pb = stack.pop()
        if pa.Distance(pb) <= lo:
            continue
        mt = 0.5 * (a + b)
        try:
            pm = curve.Value(mt)
        except Exception:
            continue
        gap = math.sqrt(((pa.X() + pb.X()) * 0.5 - pm.X()) ** 2
                        + ((pa.Y() + pb.Y()) * 0.5 - pm.Y()) ** 2
                        + ((pa.Z() + pb.Z()) * 0.5 - pm.Z()) ** 2)
        if gap <= deflection:
            continue
        out.append(mt)
        stack.append((a, mt, pa, pm))
        stack.append((mt, b, pm, pb))
    return sorted(out)


def _edge_nodes(edge, faces_of_edge, tri_of):
    """Existing node parameters along an edge, from any face that carries it."""
    for face in faces_of_edge:
        got = tri_of(face)
        if got is None:
            continue
        tri, loc = got
        poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if poly is None or not poly.HasParameters():
            continue
        prm = poly.Parameters()
        ts = [prm.Value(k) for k in range(1, poly.NbNodes() + 1)]
        if len(ts) >= 2:
            return sorted(ts)
    return []


def _end_edges(face, tri, loc, umin, umax, tol, swap):
    """Edges closing the two ENDS of a band -- those at constant u."""
    out = []
    seen = set()
    ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        key = edge.TShape()
        if key in seen:
            continue
        seen.add(key)
        if BRep_Tool.IsClosed_s(edge, face) or BRep_Tool.Degenerated_s(edge):
            continue
        poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if poly is None:
            continue
        nodes = poly.Nodes()
        us = []
        for k in range(1, poly.NbNodes() + 1):
            q = tri.UVNode(nodes.Value(k))
            us.append(q.Y() if swap else q.X())
        if not us:
            continue
        if max(us) - min(us) <= tol:
            out.append(edge)
    return out


def _ring_edges(face, tri, loc, vmin, vmax, tol):
    """Edges of `face` whose nodes all sit on one of the band's two rings."""
    out = []
    ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    seen = set()
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        key = edge.TShape()
        if key in seen:
            continue
        seen.add(key)
        if BRep_Tool.IsClosed_s(edge, face) or BRep_Tool.Degenerated_s(edge):
            continue
        poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if poly is None:
            continue
        nodes = poly.Nodes()
        vs = [tri.UVNode(nodes.Value(k)).Y()
              for k in range(1, poly.NbNodes() + 1)]
        if not vs:
            continue
        if max(vs) - min(vs) <= tol:
            out.append((edge, poly))
    return out


def plan(shape, faces, params, ruled_rows, tri_of, face_filter=None):
    """Extra edge parameters wanted by the banded faces of `shape`.

    `ruled_rows` and `tri_of` are injected to keep this module free of a circular
    import with tessellate.

    `face_filter(face) -> bool` restricts which faces are allowed to ASK for
    vertices. With it set to the priority-fillet test, only small long blends
    dictate their rungs and every other face simply conforms -- which is the
    point: applied to every band this cost 2.4x the tessellation time for a
    result Benn judged worse as often as better.

    Returns {edge TShape: sorted list of parameters}. Parameters already covered
    by an existing boundary node are not included -- this only ever ADDS.
    """
    want: dict = {}

    # Pass 1: refine any edge whose own chords bow further from it than the
    # requested deviation. This is independent of the bands -- it fixes OCCT's
    # discretisation itself, and every face on the edge picks the extra points
    # up through the same machinery.
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE,
                                   TopAbs_ShapeEnum.TopAbs_FACE, anc)
    for ei in range(1, anc.Extent() + 1):
        edge = TopoDS.Edge_s(anc.FindKey(ei))
        if BRep_Tool.Degenerated_s(edge):
            continue
        adj = [TopoDS.Face_s(f) for f in anc.FindFromIndex(ei)]
        if face_filter is not None and not any(face_filter(f) for f in adj):
            continue        # nothing here is a priority face: leave it alone
        have = _edge_nodes(edge, adj, tri_of)
        if len(have) < 2:
            continue
        try:
            curve = BRepAdaptor_Curve(edge)
        except Exception:
            continue
        extra = sag_params(curve, have, params.deflection)
        if extra:
            want.setdefault(edge.TShape(), []).extend(extra)

    # Which edges a PLANE would have to absorb. Alignment vertices are only
    # free where the neighbour can spread them out; a plane is typically two
    # triangles, so 25 vertices along one of its edges shred it -- either into a
    # fan, or, once re-woven, into a mesh far denser than the flat face needs.
    # Benn hit this on body 49 and rightly called it something that should never
    # happen. Curved neighbours absorb them fine, so restrict to those.
    on_plane = set()
    for ei in range(1, anc.Extent() + 1):
        for f in anc.FindFromIndex(ei):
            if BRepAdaptor_Surface(TopoDS.Face_s(f)).GetType() ==                     GeomAbs_SurfaceType.GeomAbs_Plane:
                on_plane.add(TopoDS.Edge_s(anc.FindKey(ei)).TShape())
                break

    for fi in range(1, faces.Extent() + 1):
        face = TopoDS.Face_s(faces.FindKey(fi))
        if BRepAdaptor_Surface(face).GetType() not in _BANDED:
            continue
        if face_filter is not None and not face_filter(face):
            continue
        got = tri_of(face)
        if got is None:
            continue
        tri, loc = got
        report: dict = {}
        rows = None
        for sw in (False, True):
            report = {}
            rows = ruled_rows(face, tri, loc, params, report=report, swap=sw)
            if rows is not None:
                break
        cols = report.get("columns")
        if rows is None or not cols:
            continue
        v0, v1 = report.get("v_range", (0.0, 0.0))
        dv = v1 - v0
        if dv <= 0:
            continue
        # A partial band that wants interior rows has nowhere to put them
        # unless its END edges carry nodes at those levels. Ask for them: the
        # end edges are shared, so this is the only way to subdivide across a
        # fillet without leaving a T-junction. Valve body 60's four B-spline
        # fillets are exactly this case -- clamped to a single span across their
        # width, which sags far outside tolerance and gets rejected, leaving
        # OCCT's unstructured mesh and no quads at all.
        swap = bool(report.get("swap"))
        levels = report.get("row_levels") or []
        if levels and not report.get("has_side_rows"):
            u0r, u1r = report.get("u_range", (0.0, 0.0))
            span_u = u1r - u0r
            coord = 0 if swap else 1          # which pcurve coordinate is "v"
            for edge in _end_edges(face, tri, loc, u0r, u1r,
                                   max(span_u, 1e-30) * 1e-3, swap):
                if edge.TShape() in on_plane:
                    continue
                pc = pcurve(edge, face)
                if pc is None:
                    continue
                c2d, t0, t1 = pc
                near = max(abs(t1 - t0), 1e-30) * 1e-4
                slot = want.setdefault(edge.TShape(), [])
                for lv in levels:
                    t = param_at_u(c2d, t0, t1, lv, coord=coord)
                    if t is None or t <= t0 + near or t >= t1 - near:
                        continue
                    if any(abs(t - h) <= near for h in slot):
                        continue
                    slot.append(t)

        for edge, poly in _ring_edges(face, tri, loc, v0, v1, dv * 1e-3):
            if edge.TShape() in on_plane:
                continue
            pc = pcurve(edge, face)
            if pc is None:
                continue
            c2d, t0, t1 = pc
            have = []
            if poly.HasParameters():
                pr = poly.Parameters()
                have = [pr.Value(k) for k in range(1, poly.NbNodes() + 1)]
            if not have:
                continue
            near = max(abs(t1 - t0), 1e-30) * 1e-4
            key = edge.TShape()
            slot = want.setdefault(key, [])
            for u in cols:
                t = param_at_u(c2d, t0, t1, u)
                if t is None:
                    continue
                if t <= t0 + near or t >= t1 - near:
                    continue                      # an endpoint already exists
                if any(abs(t - h) <= near for h in have):
                    continue                      # a node is already there
                if any(abs(t - h) <= near for h in slot):
                    continue
                slot.append(t)
    return {k: sorted(v) for k, v in want.items() if v}
