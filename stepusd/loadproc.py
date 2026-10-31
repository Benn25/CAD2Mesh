"""Read a STEP file in a SEPARATE PROCESS, then hand the result back as a
BRep cache + a JSON scene description.

Why a process and not a thread: OCP holds the GIL for the entire duration of
STEPCAFControl_Reader.ReadFile/Transfer. Measured on a 32 MB SolidWorks file,
a background QThread let the main thread run 3 times in 36 seconds where ~3600
were expected -- the UI is completely frozen. A subprocess sidesteps the GIL
entirely; the parent then reads the BRep cache, which takes ~0.4 s for the same
model instead of 36 s.

Run as:  python -m stepusd.loadproc <step_file> <out_dir>
Progress lines go to stdout as "PROGRESS <text>", the final line is "OK" or
"ERROR <message>".
"""
from __future__ import annotations
import json
import os
import sys

SHAPES = "shapes.brep"
SCENE = "scene.json"


def _say(msg: str):
    print(f"PROGRESS {msg}", flush=True)


def trsf_to_list(t) -> list:
    return [t.Value(i, j) for i in (1, 2, 3) for j in (1, 2, 3, 4)]


def list_to_trsf(v: list):
    from OCP.gp import gp_Trsf
    t = gp_Trsf()
    t.SetValues(v[0], v[1], v[2], v[3], v[4], v[5],
                v[6], v[7], v[8], v[9], v[10], v[11])
    return t


def export(step_path: str, out_dir: str) -> dict:
    """Read the STEP and write shapes.brep + scene.json into out_dir."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound
    from .reader import StepDocument

    os.makedirs(out_dir, exist_ok=True)
    _say("reading STEP (this is the slow part on large files)...")
    doc = StepDocument(step_path)
    _say("collecting bodies...")

    builder = BRep_Builder()
    root = TopoDS_Compound()
    builder.MakeCompound(root)

    protos = []
    for proto in doc.protos.values():
        sub = TopoDS_Compound()
        builder.MakeCompound(sub)
        for b in proto.bodies:
            builder.Add(sub, b)
        builder.Add(root, sub)
        protos.append(dict(key=proto.key, name=proto.name,
                           color=list(proto.color) if proto.color else None,
                           n_bodies=len(proto.bodies),
                           solid_bodies=proto.solid_bodies,
                           open_bodies=proto.open_bodies,
                           occurrences=proto.occurrences,
                           body_face_colors=[[list(c) if c else None for c in fc]
                                             for fc in proto.body_face_colors],
                           body_colors=[list(c) if c else None
                                        for c in proto.body_colors]))

    def enc(node):
        return dict(name=node.name, matrix=trsf_to_list(node.matrix),
                    proto=(node.proto.key if node.proto else None),
                    children=[enc(c) for c in node.children])

    scene = dict(protos=protos, roots=[enc(r) for r in doc.roots],
                 source=os.path.abspath(step_path))

    _say("writing cache...")
    BRepTools.Write_s(root, os.path.join(out_dir, SHAPES))
    with open(os.path.join(out_dir, SCENE), "w", encoding="utf-8") as f:
        json.dump(scene, f)
    return scene


def load_cache(out_dir: str):
    """Parent side: rebuild a StepDocument-shaped object from the cache."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape, TopoDS_Iterator
    from .reader import Proto, Node

    with open(os.path.join(out_dir, SCENE), encoding="utf-8") as f:
        scene = json.load(f)

    root = TopoDS_Shape()
    BRepTools.Read_s(root, os.path.join(out_dir, SHAPES), BRep_Builder())

    groups = []
    it = TopoDS_Iterator(root)
    while it.More():
        sub, bodies = it.Value(), []
        jt = TopoDS_Iterator(sub)
        while jt.More():
            bodies.append(jt.Value()); jt.Next()
        groups.append(bodies)
        it.Next()

    protos = {}
    for spec, bodies in zip(scene["protos"], groups):
        pr = Proto(
            key=spec["key"], name=spec["name"], shape=None,
            color=tuple(spec["color"]) if spec["color"] else None,
            bodies=bodies, solid_bodies=spec["solid_bodies"],
            open_bodies=spec["open_bodies"], occurrences=spec["occurrences"])
        pr.body_face_colors = [[tuple(c) if c else None for c in fc]
                               for fc in spec.get("body_face_colors", [])]
        pr.body_colors = [tuple(c) if c else None
                          for c in spec.get("body_colors", [])]
        protos[spec["key"]] = pr

    def dec(d):
        return Node(name=d["name"], matrix=list_to_trsf(d["matrix"]),
                    proto=protos.get(d["proto"]) if d["proto"] else None,
                    children=[dec(c) for c in d["children"]])

    class CachedDocument:
        def __init__(self):
            self.path = scene.get("source", "")
            #: where shapes.brep lives, so workers can re-read it per process
            self.cache_dir = out_dir
            self.protos = protos
            self.roots = [dec(r) for r in scene["roots"]]

        def stats(self):
            occ = sum(p.occurrences for p in self.protos.values())
            return dict(prototypes=len(self.protos), occurrences=occ,
                        instanced=occ - len(self.protos),
                        open_bodies=sum(p.open_bodies for p in self.protos.values()))

    return CachedDocument()


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print("ERROR usage: python -m stepusd.loadproc <step_file> <out_dir>",
              flush=True)
        return 2
    try:
        export(argv[0], argv[1])
        print("OK", flush=True)
        return 0
    except Exception as e:                           # noqa: BLE001
        print(f"ERROR {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
