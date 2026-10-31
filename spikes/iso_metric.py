"""Measure how ISO-ALIGNED the mesh is on curved faces.

The complaint this answers is "why are the lines not all parallel". On a
cylinder, cone or torus every edge should run either along a constant-u line
(across the band, the loop cut) or along a constant-v line (around it). Anything
else is a diagonal, and diagonals are what make a fillet look anarchic.

For each polygon edge on such a face we take the local azimuthal direction --
the direction of increasing u, which is axis x radial -- and classify the edge:

  * |dot(edge, azimuth)| ~ 0  -> constant-u   (the loop cut)
  * |dot(edge, azimuth)| ~ 1  -> constant-v   (the ring)
  * anything between          -> diagonal

Reported per surface type, as the percentage of edges that are one or the other.

    .venv/Scripts/python.exe spikes/iso_metric.py [deflection] [angle] [aspect]
"""
import os
import sys
import glob
import math
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import replace                                    # noqa: E402
from stepusd.loadproc import load_cache                            # noqa: E402
from stepusd.tessellate import tessellate, MeshParams              # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface                    # noqa: E402
from OCP.GeomAbs import GeomAbs_SurfaceType as ST                  # noqa: E402
from OCP.TopExp import TopExp                                      # noqa: E402
from OCP.TopAbs import TopAbs_ShapeEnum                            # noqa: E402
from OCP.TopoDS import TopoDS                                      # noqa: E402
from OCP.TopTools import TopTools_IndexedMapOfShape                # noqa: E402

KINDS = {ST.GeomAbs_Cylinder: "cylinder", ST.GeomAbs_Cone: "cone",
         ST.GeomAbs_Torus: "torus", ST.GeomAbs_Sphere: "sphere"}
#: an edge counts as aligned if it is within this of exactly along or across
TOL = math.radians(8.0)


def _axis(sa, st):
    if st == ST.GeomAbs_Cylinder:
        return sa.Cylinder().Axis()
    if st == ST.GeomAbs_Cone:
        return sa.Cone().Axis()
    if st == ST.GeomAbs_Torus:
        return sa.Torus().Axis()
    return sa.Sphere().Position().Axis()


def measure(body, params, out):
    m = tessellate(body, params)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fmap)
    kind, axis = {}, {}
    for fi in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(fi))
        sa = BRepAdaptor_Surface(face)
        st = sa.GetType()
        if st not in KINDS:
            continue
        kind[fi] = KINDS[st]
        ax = _axis(sa, st)
        d, p = ax.Direction(), ax.Location()
        axis[fi] = ((d.X(), d.Y(), d.Z()), (p.X(), p.Y(), p.Z()))
    pts = m.points
    for pi, poly in enumerate(m.faces):
        fi = m.face_source[pi]
        k = kind.get(fi)
        if k is None:
            continue
        (ax, ay, az), (ox, oy, oz) = axis[fi]
        n = len(poly)
        for e in range(n):
            p = pts[poly[e]]
            q = pts[poly[(e + 1) % n]]
            ex, ey, ez = q[0] - p[0], q[1] - p[1], q[2] - p[2]
            el = math.sqrt(ex * ex + ey * ey + ez * ez)
            if el < 1e-12:
                continue
            mx, my, mz = (p[0] + q[0]) * .5 - ox, (p[1] + q[1]) * .5 - oy, \
                         (p[2] + q[2]) * .5 - oz
            t = mx * ax + my * ay + mz * az
            rx, ry, rz = mx - t * ax, my - t * ay, mz - t * az
            # azimuth = axis x radial
            zx, zy, zz = ay * rz - az * ry, az * rx - ax * rz, ax * ry - ay * rx
            zl = math.sqrt(zx * zx + zy * zy + zz * zz)
            if zl < 1e-12:
                continue
            c = abs((ex * zx + ey * zy + ez * zz) / (el * zl))
            c = max(0.0, min(1.0, c))
            ang = math.acos(c)                 # 0 = along the ring, pi/2 = across
            out[k]["n"] += 1
            if ang <= TOL:
                out[k]["ring"] += 1
            elif ang >= math.pi / 2 - TOL:
                out[k]["cut"] += 1


def main():
    defl = float(sys.argv[1]) if len(sys.argv) > 1 else 0.25
    ang = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    asp = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    base = MeshParams(deflection=defl, angle_deg=ang, grid_aspect=asp)
    variants = (("plain OCCT", replace(base, structured=False, lattice=False,
                                       grid_trim=False)),
                ("ours", base))
    cache = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
             and os.path.exists(os.path.join(x, "scene.json"))]
    valve = [x for x in cache if "handle" not in x][0]
    for name, cdir in (("valve", valve), ("handle", ".cache/handle")):
        doc = load_cache(cdir)
        print(f"\n{name}  (deflection {defl}, angle {ang}, aspect {asp})")
        for label, pr in variants:
            out = collections.defaultdict(lambda: collections.Counter())
            for proto in doc.protos.values():
                for b in proto.bodies:
                    measure(b, pr, out)
            bits = []
            for k in ("cylinder", "cone", "torus", "sphere"):
                c = out.get(k)
                if not c or not c["n"]:
                    continue
                pct = 100.0 * (c["ring"] + c["cut"]) / c["n"]
                bits.append(f"{k} {pct:5.1f}%")
            print(f"    {label:<11} iso-aligned edges: " + "  ".join(bits))


if __name__ == "__main__":
    main()
