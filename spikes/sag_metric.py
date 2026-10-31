"""Measure CHORD sag: how far each edge's midpoint sits from the true surface.

Benn's correction, and it is the right one: checking vertices proves nothing.
Every vertex is on the surface by construction, so a vertex test always passes.
The error that shows up as a visible valley is in the EDGE -- the chord between
two vertices cuts through the solid, and the deeper it cuts the more obvious the
crease. A long edge across a curved direction can sag far past the requested max
surface deviation while both of its endpoints are perfect.

For each polygon edge on an analytic face this reports the distance from the
edge midpoint to the surface, against the deflection that was asked for.

    .venv/Scripts/python.exe spikes/sag_metric.py [deflection] [angle] [min_size]
"""
import os
import sys
import glob
import math
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stepusd.loadproc import load_cache                       # noqa: E402
from stepusd.tessellate import tessellate, MeshParams         # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface               # noqa: E402
from OCP.GeomAbs import GeomAbs_SurfaceType as ST             # noqa: E402
from OCP.TopExp import TopExp                                 # noqa: E402
from OCP.TopAbs import TopAbs_ShapeEnum                       # noqa: E402
from OCP.TopoDS import TopoDS                                 # noqa: E402
from OCP.TopTools import TopTools_IndexedMapOfShape           # noqa: E402

KIND = {ST.GeomAbs_Cylinder: "cylinder", ST.GeomAbs_Cone: "cone",
        ST.GeomAbs_Torus: "torus", ST.GeomAbs_Sphere: "sphere"}


def _frame(ax):
    d, o = ax.Direction(), ax.Location()
    return (d.X(), d.Y(), d.Z()), (o.X(), o.Y(), o.Z())


def _axial(p, axis, org):
    wx, wy, wz = p[0] - org[0], p[1] - org[1], p[2] - org[2]
    t = wx * axis[0] + wy * axis[1] + wz * axis[2]
    rx, ry, rz = wx - t * axis[0], wy - t * axis[1], wz - t * axis[2]
    return t, math.sqrt(rx * rx + ry * ry + rz * rz)


def _probe(face):
    """-> (kind, fn(point) -> signed distance to the surface) or None."""
    sa = BRepAdaptor_Surface(face)
    st = sa.GetType()
    if st == ST.GeomAbs_Sphere:
        s = sa.Sphere()
        c, r = s.Location(), s.Radius()
        cc = (c.X(), c.Y(), c.Z())
        return "sphere", lambda p: math.dist(p, cc) - r
    if st == ST.GeomAbs_Cylinder:
        cy = sa.Cylinder()
        axis, org = _frame(cy.Axis())
        r = cy.Radius()
        return "cylinder", lambda p: _axial(p, axis, org)[1] - r
    if st == ST.GeomAbs_Cone:
        co = sa.Cone()
        axis, org = _frame(co.Axis())
        r0, half = co.RefRadius(), co.SemiAngle()

        def f(p, axis=axis, org=org, r0=r0, half=half):
            t, r = _axial(p, axis, org)
            return (r - (r0 + t * math.tan(half))) * math.cos(half)
        return "cone", f
    if st == ST.GeomAbs_Torus:
        to = sa.Torus()
        axis, org = _frame(to.Axis())
        R, r = to.MajorRadius(), to.MinorRadius()

        def g(p, axis=axis, org=org, R=R, r=r):
            t, rad = _axial(p, axis, org)
            return math.hypot(rad - R, t) - r
        return "torus", g
    return None


def main():
    defl = float(sys.argv[1]) if len(sys.argv) > 1 else 0.15
    ang = float(sys.argv[2]) if len(sys.argv) > 2 else 28.0
    mn = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15
    params = MeshParams(deflection=defl, angle_deg=ang, min_size=mn,
                        grid_aspect=8.0)
    cache = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
             and os.path.exists(os.path.join(x, "scene.json"))]
    valve = [x for x in cache if "handle" not in x][0]
    for name, cdir in (("valve", valve), ("handle", ".cache/handle")):
        doc = load_cache(cdir)
        stat = collections.defaultdict(lambda: [0, 0, 0.0])   # n, over, worst
        for proto in doc.protos.values():
            for body in proto.bodies:
                m = tessellate(body, params)
                fm = TopTools_IndexedMapOfShape()
                TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
                probe = {}
                for fi in range(1, fm.Extent() + 1):
                    got = _probe(TopoDS.Face_s(fm.FindKey(fi)))
                    if got:
                        probe[fi] = got
                if not probe:
                    continue
                seen = set()
                for pi, poly in enumerate(m.faces):
                    got = probe.get(m.face_source[pi])
                    if got is None:
                        continue
                    kind, fn = got
                    k = len(poly)
                    for e in range(k):
                        a, b = poly[e], poly[(e + 1) % k]
                        key = (a, b) if a < b else (b, a)
                        if key in seen:
                            continue
                        seen.add(key)
                        pa, pb = m.points[a], m.points[b]
                        mid = ((pa[0] + pb[0]) * .5, (pa[1] + pb[1]) * .5,
                               (pa[2] + pb[2]) * .5)
                        sag = abs(fn(mid))
                        s = stat[kind]
                        s[0] += 1
                        if sag > defl:
                            s[1] += 1
                        s[2] = max(s[2], sag)
        print(f"\n{name}  (asked for max surface deviation {defl} mm)")
        for kind in ("cylinder", "cone", "torus", "sphere"):
            n, over, worst = stat.get(kind, (0, 0, 0.0))
            if not n:
                continue
            print(f"    {kind:<9} edges {n:>7}   over tolerance {over:>6} "
                  f"({100.0 * over / n:5.1f}%)   worst sag {worst:8.4f} mm "
                  f"= {worst / defl:6.1f}x")


if __name__ == "__main__":
    main()
