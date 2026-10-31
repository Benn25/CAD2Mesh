"""Export only selected bodies: right bodies, right places, nothing else.

Uses the handle (real hierarchy, instancing). Meshes are cheap fakes -- one
triangle per body, offset by body index -- because what is under test is the
WRITER's filtering and placement, not the mesher.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pxr import Usd, UsdGeom
from stepusd.loadproc import load_cache
from stepusd.scene import flatten
from stepusd.tessellate import Mesh
from stepusd.writer import write_usd

doc = load_cache(".cache/handle")
for proto in doc.protos.values():
    ms = []
    for i in range(len(proto.bodies)):
        m = Mesh()
        m.points = [(i, 0, 0), (i + 1, 0, 0), (i, 1, 0)]
        m.faces = [(0, 1, 2)]
        m.normals = [(0, 0, 1)] * 3
        ms.append(m)
    proto.meshes = ms

occs = flatten(doc)
multi = [o for o in occs if len(doc.protos[o.proto_key].bodies) > 1]
shared = {}
for o in occs:
    shared.setdefault(o.proto_key, []).append(o)
# Two copies of one part that sit at DIFFERENT paths. (Copies with the same
# name under the same parent share a path, hence a selection key: selecting one
# selects all of them, in the viewport and here alike.)
inst = next(v for v in shared.values()
            if len({o.path for o in v}) > 1)
a = inst[0]
b = next(o for o in inst if o.path != a.path)
# a PARTIAL multi-body occurrence, plus two copies of an instanced part
only = {(multi[0].path, 1), (a.path, 0), (b.path, 0)}
from collections import Counter
npath = Counter(o.path for o in occs)
want_n = sum(npath[op] for op, _ in only)
print("selected:", sorted(only))

out = "spikes/out"
full_p, sel_p = f"{out}/sel_full.usda", f"{out}/sel_only.usda"
write_usd(doc, full_p)
write_usd(doc, sel_p, only=only)


def world_meshes(p):
    st = Usd.Stage.Open(p)
    cache = UsdGeom.XformCache()
    res = {}
    for prim in st.Traverse():
        if prim.IsA(UsdGeom.Mesh) and not str(prim.GetPath()).startswith("/_"):
            m = cache.GetLocalToWorldTransform(prim)
            pts = UsdGeom.Mesh(prim).GetPointsAttr().Get()
            key = str(prim.GetPath())
            res[key] = [tuple(round(c, 6) for c in m.Transform(p)) for p in pts]
    return st, res


st_f, full = world_meshes(full_p)
st_s, sel = world_meshes(sel_p)
fails = 0
print(f"full export: {len(full)} meshes   selected export: {len(sel)} meshes")
if len(sel) != want_n:
    print(f"FAIL: wanted {want_n} meshes"); fails += 1
# every selected mesh must sit exactly where the full export puts it
for k, pts in sel.items():
    if full.get(k) != pts:
        print("FAIL: moved or renamed", k); fails += 1
# a partial multi-body occurrence keeps ONLY the chosen body
names = sorted(k.rsplit("/", 1)[1] for k in sel)
print("bodies:", names)
if sum(1 for n in names if n == "Body_2") != npath[multi[0].path]:
    print("FAIL: partial occurrence did not keep exactly Body_2"); fails += 1
# instancing survives: both copies reference ONE prototype
refs = set()
for prim in st_s.Traverse():
    for spec in prim.GetPrimStack():
        for r in spec.referenceList.prependedItems:
            refs.add(str(r.primPath))
print("prototypes referenced:", len(refs))
if len(refs) != 2 or a.proto_key == multi[0].proto_key:
    print("FAIL: expected 2 prototypes (one partial, one shared)"); fails += 1
# full export unchanged by the new code path: count matches every body placed
want = sum(len(doc.protos[o.proto_key].bodies) for o in occs)
if len(full) != want:
    print(f"FAIL: full export has {len(full)} meshes, want {want}"); fails += 1
print("PASSED" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
