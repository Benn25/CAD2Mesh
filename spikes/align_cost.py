"""How many extra boundary vertices would it take to make every rung straight?

For each banded face that _ruled_rows accepts, the interior rows sit at uniform
u. A rung is straight only if the boundary ring also has a node at that u. Count
how many are missing, and which neighbouring faces would have to accept them.
"""
import sys, os, glob, math, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"H:\Working\programming\Converter step-FBX")
os.chdir(r"H:\Working\programming\Converter step-FBX")
from stepusd.loadproc import load_cache
from stepusd.tessellate import MeshParams, _ruled_rows
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import (TopTools_IndexedMapOfShape,
                          TopTools_IndexedDataMapOfShapeListOfShape)
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType as ST

NAME = {ST.GeomAbs_Cylinder: "cyl", ST.GeomAbs_Cone: "cone",
        ST.GeomAbs_Torus: "torus", ST.GeomAbs_Sphere: "sphere",
        ST.GeomAbs_Plane: "plane", ST.GeomAbs_BSplineSurface: "bspl"}

defl = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2
ang = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
params = MeshParams(deflection=defl, angle_deg=ang, grid_aspect=8.0)

V = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
     and os.path.exists(os.path.join(x, "scene.json")) and "handle" not in x][0]
doc = load_cache(V)
bodies = [(p, x) for p in doc.protos.values() for x in p.bodies]

tot_rows = tot_missing = tot_have = 0
neigh_kinds = collections.Counter()
per_face = []
for bi, (proto, body) in enumerate(bodies):
    if bi != 137:
        continue
    BRepTools.Clean_s(body)
    BRepMesh_IncrementalMesh(body, params.to_occt())
    fm = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(body, TopAbs_ShapeEnum.TopAbs_EDGE,
                                   TopAbs_ShapeEnum.TopAbs_FACE, anc)
    for fi in range(1, fm.Extent() + 1):
        face = TopoDS.Face_s(fm.FindKey(fi))
        st = BRepAdaptor_Surface(face).GetType()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        rows = _ruled_rows(face, tri, loc, params)
        if not rows:
            continue
        # interior rows = those whose points are mostly new (item is None)
        interior_us = set()
        for rv, row in rows:
            news = [u for u, item, *_ in row if item is None]
            if len(news) >= max(2, len(row) - 2):
                interior_us.update(news)
        if not interior_us:
            continue
        # boundary ring u values
        ring_us = []
        for rv, row in rows:
            news = [u for u, item, *_ in row if item is None]
            if len(news) < max(2, len(row) - 2):
                ring_us.append(sorted(u for u, item, *_ in row))
        if len(ring_us) != 2:
            continue
        u_all = sorted(interior_us)
        span = u_all[-1] - u_all[0] if len(u_all) > 1 else 1.0
        tol = (span / max(len(u_all) - 1, 1)) * 0.15
        missing = have = 0
        for ring in ring_us:
            for u in u_all:
                near = min((abs(u - w) for w in ring), default=1e9)
                if near <= tol:
                    have += 1
                else:
                    missing += 1
        tot_rows += len(u_all) * 2
        tot_missing += missing
        tot_have += have
        per_face.append((missing, fi, NAME.get(st, "?"), len(u_all), have))
        # which neighbours would have to accept the new vertices
        ex = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            ex.Next()
            if BRep_Tool.IsClosed_s(e, face) or BRep_Tool.Degenerated_s(e):
                continue
            for sh in anc.FindFromKey(e):
                nf = TopoDS.Face_s(sh)
                if nf.IsSame(face):
                    continue
                neigh_kinds[NAME.get(BRepAdaptor_Surface(nf).GetType(), "?")] += 1

per_face.sort(reverse=True)
print(f"body138 @ deflection {defl}, angle {ang}")
print(f"  structured bands examined: {len(per_face)}")
print(f"  rung endpoints wanted on a ring: {tot_rows}")
print(f"    already have a boundary vertex : {tot_have} "
      f"({100*tot_have/max(tot_rows,1):.0f}%)")
print(f"    would need one INSERTED        : {tot_missing} "
      f"({100*tot_missing/max(tot_rows,1):.0f}%)")
print(f"  neighbour faces that must accept them, by kind: {dict(neigh_kinds)}")
print("\n  worst faces (missing, face, type, interior rows, aligned):")
for row in per_face[:8]:
    print(f"    {row}")
