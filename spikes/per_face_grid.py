"""Per-face comparison: does the axis-aligned grid help or hurt THIS face?

Benn's report is that long concave small fillets look better with the grid OFF
while other faces need it on. Measure both candidates face by face and look for
what separates them.
"""
import sys, os, glob, math, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"H:\Working\programming\Converter step-FBX")
os.chdir(r"H:\Working\programming\Converter step-FBX")
from dataclasses import replace
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_SurfaceType as ST
from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

NAME = {ST.GeomAbs_Cylinder: "cyl", ST.GeomAbs_Cone: "cone",
        ST.GeomAbs_Torus: "torus", ST.GeomAbs_Sphere: "sphere"}
TOL = math.radians(8.0)


def axis_of(sa, st):
    return {ST.GeomAbs_Cylinder: lambda: sa.Cylinder().Axis(),
            ST.GeomAbs_Cone: lambda: sa.Cone().Axis(),
            ST.GeomAbs_Torus: lambda: sa.Torus().Axis(),
            ST.GeomAbs_Sphere: lambda: sa.Sphere().Position().Axis()}[st]()


def stats(m, fi, ax, org):
    """(iso% , worst aspect, median aspect, polys) for one face."""
    n = al = 0
    asp = []
    for pi, poly in enumerate(m.faces):
        if m.face_source[pi] != fi:
            continue
        k = len(poly)
        lens = []
        for e in range(k):
            p, q = m.points[poly[e]], m.points[poly[(e + 1) % k]]
            ex, ey, ez = q[0] - p[0], q[1] - p[1], q[2] - p[2]
            el = math.sqrt(ex * ex + ey * ey + ez * ez)
            lens.append(el)
            if el < 1e-12:
                continue
            mx = (p[0] + q[0]) * .5 - org[0]
            my = (p[1] + q[1]) * .5 - org[1]
            mz = (p[2] + q[2]) * .5 - org[2]
            t = mx * ax[0] + my * ax[1] + mz * ax[2]
            rx, ry, rz = mx - t * ax[0], my - t * ax[1], mz - t * ax[2]
            zx = ax[1] * rz - ax[2] * ry
            zy = ax[2] * rx - ax[0] * rz
            zz = ax[0] * ry - ax[1] * rx
            zl = math.sqrt(zx * zx + zy * zy + zz * zz)
            if zl < 1e-12:
                continue
            c = max(0.0, min(1.0, abs((ex * zx + ey * zy + ez * zz) / (el * zl))))
            a = math.acos(c)
            n += 1
            if a <= TOL or a >= math.pi / 2 - TOL:
                al += 1
        lens = [x for x in lens if x > 1e-12]
        if len(lens) >= 3:
            asp.append(max(lens) / min(lens))
    if not asp:
        return None
    asp.sort()
    return (100.0 * al / max(n, 1), asp[-1], asp[len(asp) // 2], len(asp))


defl = float(sys.argv[1]) if len(sys.argv) > 1 else 0.15
ang = float(sys.argv[2]) if len(sys.argv) > 2 else 33.0
base = MeshParams(deflection=defl, angle_deg=ang, grid_aspect=8.0,
                  quads=False, align_edges=False, lattice=True, grid_trim=True)

V = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
     and os.path.exists(os.path.join(x, "scene.json")) and "handle" not in x][0]
body = [(p, x) for p in load_cache(V).protos.values() for x in p.bodies][137][1]
on = tessellate(body, base)
off = tessellate(body, replace(base, structured=False))
BRepTools.Clean_s(body)
from OCP.BRepMesh import BRepMesh_IncrementalMesh
BRepMesh_IncrementalMesh(body, base.to_occt())
fm = TopTools_IndexedMapOfShape()
TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
rows = []
for fi in range(1, fm.Extent() + 1):
    face = TopoDS.Face_s(fm.FindKey(fi))
    sa = BRepAdaptor_Surface(face)
    st = sa.GetType()
    if st not in NAME:
        continue
    ax_ = axis_of(sa, st)
    d, o = ax_.Direction(), ax_.Location()
    A = (d.X(), d.Y(), d.Z())
    O = (o.X(), o.Y(), o.Z())
    a = stats(on, fi, A, O)
    b = stats(off, fi, A, O)
    if a is None or b is None:
        continue
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    rows.append((a[0] - b[0], fi, NAME[st], a, b, u1 - u0, v1 - v0))
rows.sort()
print(f"body138 @ {defl}/{ang}, quads off  --  grid ON vs OFF, per face")
print(f"{'d-iso':>6} {'face':>4} {'type':<6} "
      f"{'ON iso/worstAsp/medAsp/n':>30} {'OFF iso/worstAsp/medAsp/n':>30}  du     dv")
for d_, fi, t, a, b, du, dv in rows[:10]:
    print(f"{d_:>6.1f} {fi:>4} {t:<6} "
          f"{a[0]:8.1f}{a[1]:9.1f}{a[2]:8.2f}{a[3]:6d}      "
          f"{b[0]:8.1f}{b[1]:9.1f}{b[2]:8.2f}{b[3]:6d}  {du:5.2f} {dv:6.2f}")
print("  ... best for the grid ...")
for d_, fi, t, a, b, du, dv in rows[-4:]:
    print(f"{d_:>6.1f} {fi:>4} {t:<6} "
          f"{a[0]:8.1f}{a[1]:9.1f}{a[2]:8.2f}{a[3]:6d}      "
          f"{b[0]:8.1f}{b[1]:9.1f}{b[2]:8.2f}{b[3]:6d}  {du:5.2f} {dv:6.2f}")
