"""Why is there a 'sweet spot' in max normal deviation?

Sweep the angle on ONE face and report what changes: the two rings' sample
counts, which path meshed it, and how iso-aligned the result is.
"""
import sys, os, glob, math, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"H:\Working\programming\Converter step-FBX")
os.chdir(r"H:\Working\programming\Converter step-FBX")
from stepusd.loadproc import load_cache
from stepusd.tessellate import (tessellate, MeshParams, _ruled_rows,
                                _grid_trim, _lattice_remesh)
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType as ST

TOL = math.radians(8.0)
TARGET = [int(x) for x in (sys.argv[1:] or ["40"])]

V = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
     and os.path.exists(os.path.join(x, "scene.json")) and "handle" not in x][0]
body = [(p, x) for p in load_cache(V).protos.values() for x in p.bodies][137][1]


def axis_of(sa, st):
    return {ST.GeomAbs_Cylinder: lambda: sa.Cylinder().Axis(),
            ST.GeomAbs_Cone: lambda: sa.Cone().Axis(),
            ST.GeomAbs_Torus: lambda: sa.Torus().Axis()}[st]()


def iso_pct(m, fi, ax, org):
    n = al = 0
    for pi, poly in enumerate(m.faces):
        if m.face_source[pi] != fi:
            continue
        k = len(poly)
        for e in range(k):
            p, q = m.points[poly[e]], m.points[poly[(e + 1) % k]]
            ex, ey, ez = q[0] - p[0], q[1] - p[1], q[2] - p[2]
            el = math.sqrt(ex * ex + ey * ey + ez * ez)
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
    return 100.0 * al / max(n, 1), n


print("face  ang | rings(bot,top) side | path       polys  quads  iso-aligned")
for fi in TARGET:
    for ang in (12, 14, 16, 18, 20, 22, 25, 28, 30, 35):
        params = MeshParams(deflection=0.2, angle_deg=ang, grid_aspect=8.0)
        m = tessellate(body, params)
        BRepTools.Clean_s(body)
        BRepMesh_IncrementalMesh(body, params.to_occt())
        fm = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
        face = TopoDS.Face_s(fm.FindKey(fi))
        sa = BRepAdaptor_Surface(face)
        st = sa.GetType()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        vmin, vmax = v1, v0
        counts, side = [], 0
        ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            ex.Next()
            if BRep_Tool.IsClosed_s(e, face) or BRep_Tool.Degenerated_s(e):
                continue
            po = BRep_Tool.PolygonOnTriangulation_s(e, tri, loc)
            if po is None:
                continue
            nd = po.Nodes()
            vs = [tri.UVNode(nd.Value(i)).Y() for i in range(1, po.NbNodes() + 1)]
            if max(vs) - min(vs) < (v1 - v0) * 1e-3:
                counts.append(po.NbNodes())
            else:
                side += po.NbNodes()
        r = _ruled_rows(face, tri, loc, params)
        g = None if r else _grid_trim(face, tri, loc, params)
        l = None if (r or g) else _lattice_remesh(face, tri, loc, params)
        path = "ruled" if r else ("grid_trim" if g else ("lattice" if l else "OCCT"))
        ax_ = axis_of(sa, st)
        d, o = ax_.Direction(), ax_.Location()
        pct, ne = iso_pct(m, fi, (d.X(), d.Y(), d.Z()), (o.X(), o.Y(), o.Z()))
        np_ = sum(1 for x in m.face_source if x == fi)
        nq = sum(1 for pi, x in enumerate(m.face_source)
                 if x == fi and len(m.faces[pi]) == 4)
        cs = ",".join(str(c) for c in sorted(counts))
        flag = "  <== MATCH" if len(counts) == 2 and counts[0] == counts[1] else ""
        print(f"{fi:>4} {ang:>4} | {cs:>12} {side:>4} | {path:<10} {np_:>5} "
              f"{nq:>6}  {pct:5.1f}%{flag}")
    print()
