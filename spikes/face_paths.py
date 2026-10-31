"""What path did each face of a body actually take? Read LAST_FACE_PATHS.

Re-calling _ruled_rows from a script is a good way to fool yourself: it is easy
to forget the swap retry or _grid_wins. This reports what production chose.

    .venv/Scripts/python.exe spikes/face_paths.py [body] [deflection] [angle]
"""
import os, sys, glob, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams, LAST_FACE_PATHS
from OCP.BRepTools import BRepTools
from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.BRepAdaptor import BRepAdaptor_Surface

BI = int(sys.argv[1]) if len(sys.argv) > 1 else 157
defl = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
ang = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
V = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
     and os.path.exists(os.path.join(x, "scene.json")) and "handle" not in x][0]
body = [(p, x) for p in load_cache(V).protos.values() for x in p.bodies][BI][1]
params = MeshParams(deflection=defl, angle_deg=ang, grid_aspect=8.0)
m = tessellate(body, params)
paths = dict(LAST_FACE_PATHS)
per = collections.Counter(m.face_source)
quads = collections.Counter()
for i, f in enumerate(m.face_source):
    if len(m.faces[i]) == 4:
        quads[f] += 1
fm = TopTools_IndexedMapOfShape()
TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
rows = []
for fi in range(1, fm.Extent() + 1):
    face = TopoDS.Face_s(fm.FindKey(fi))
    st = str(BRepAdaptor_Surface(face).GetType()).split("_")[-1]
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    rows.append((per[fi], fi, st, quads[fi], paths.get(fi, "plain OCCT"),
                 u1 - u0, v1 - v0))
rows.sort(reverse=True)
print(f"body{BI} @ {defl}/{ang}: {len(m.faces):,} polys, {m.n_quads:,} quads")
print(f"{'polys':>6} {'face':>4} {'type':<9}{'quads':>6}  {'du':>7}{'dv':>7}  path")
for n, fi, t, q, path, du, dv in rows[:16]:
    print(f"{n:>6} {fi:>4} {t:<9}{q:>6}  {du:7.2f}{dv:7.2f}  {path}")
tally = collections.Counter()
for n, fi, t, q, path, du, dv in rows:
    tally[path] += n
print("  polygons by path:", dict(tally))
