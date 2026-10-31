"""One body, several materials: slots on ONE mesh, assigned per face.

Benn: "how happen when an object has several materials? like a surface glass
and the rest steel? fusion 360 convert this in several material slots and
attribute the materials on the mesh accordingly. this is what I need."

The half that is easy to get wrong is not the slots, it is keeping ONE mesh.
Splitting a part per material is what Fusion does to the geometry and is
exactly what must not happen here -- a welded watertight solid has to stay one
welded watertight solid, with the materials expressed as `GeomSubset`s over its
faces.

So this asserts four things in Blender, not one:

  * the body arrives as a SINGLE object, not one per material;
  * it has one material slot per distinct colour in the STEP;
  * the slot colours are the STEP's colours;
  * every polygon is assigned to a slot, and the assignment SPLITS the mesh
    up the way the colours do -- not all polygons on slot 0.

Run:  .venv/Scripts/python.exe spikes/multi_material.py
"""
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stepusd.reader import StepDocument
from stepusd.tessellate import tessellate, MeshParams
from stepusd.writer import write_usd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
STEP = os.path.join(OUT, "multicolor.step")
BLENDER = next(iter(sorted(glob.glob(
    r"C:\no install prog\blender\stable\blender-4.5*\blender.exe"))), None)

PROBE = '''
import bpy, sys, json, collections
path, report = sys.argv[sys.argv.index("--") + 1:][0:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=path, merge_parent_xform=False)
out = []
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    per = collections.Counter(p.material_index for p in o.data.polygons)
    slots = []
    for m in o.data.materials:
        rgb = None
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    rgb = [round(v, 4) for v in
                           n.inputs["Base Color"].default_value[:3]]
        slots.append({"name": m.name if m else None, "base": rgb})
    out.append({"object": o.name, "polys": len(o.data.polygons),
                "slots": slots, "per_slot": dict(per)})
json.dump(out, open(report, "w"))
'''


def to_srgb_hex(lin):
    def f(u):
        return 12.92 * u if u <= 0.0031308 else 1.055 * (u ** (1 / 2.4)) - 0.055
    return "#" + "".join("%02X" % round(max(0, min(1, f(v))) * 255)
                         for v in lin)


def main():
    if not os.path.exists(STEP):
        print("building the fixture...")
        subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "make_multicolor_step.py")],
                       check=True, capture_output=True)
    doc = StepDocument(STEP)
    proto = list(doc.protos.values())[0]
    face_cols = proto.body_face_colors[0]
    distinct = {tuple(c) for c in face_cols if c}
    bare = sum(1 for c in face_cols if not c)
    print(f"STEP: 1 body, {len(face_cols)} faces, "
          f"{len(distinct)} distinct face colours, {bare} with none of their own")
    for c in sorted(distinct):
        n = sum(1 for x in face_cols if x and tuple(x) == c)
        print(f"    {to_srgb_hex(c)}  on {n} face(s)")
    assert len(distinct) >= 2, "the fixture must carry several colours"

    # A face with NO colour of its own inherits the PART's colour, and that is
    # a slot too -- which is right, and is Benn's "the rest steel" case: paint
    # the glass faces, leave the rest, and two slots come out. So the expected
    # slot count is the distinct face colours plus the part colour, and only
    # when some face actually needs it.
    expect = set(distinct)
    if bare and proto.color:
        expect.add(tuple(proto.color))

    p = MeshParams(deflection=0.4, angle_deg=25, min_size=0.64)
    proto.meshes = [tessellate(b, p) for b in proto.bodies]
    m = proto.meshes[0]
    print(f"mesh: {len(m.faces)} polygons, watertight={m.watertight} "
          f"open={m.boundary_edges} nonman={m.nonmanifold_edges}")
    assert m.watertight, "the fixture body must be watertight to begin with"

    usd = os.path.join(OUT, "multi_material.usda")
    if os.path.exists(usd):
        os.remove(usd)
    write_usd(doc, usd)

    if not BLENDER:
        print("no Blender found; stopping after the export")
        return
    probe = os.path.join(OUT, "_multi_probe.py")
    report = os.path.join(OUT, "_multi_report.json")
    open(probe, "w", encoding="utf-8").write(PROBE)
    if os.path.exists(report):
        os.remove(report)
    subprocess.run([BLENDER, "-b", "--python", probe, "--", usd, report],
                   capture_output=True, text=True, timeout=900,
                   encoding="utf-8", errors="replace")
    assert os.path.exists(report), "the Blender probe wrote nothing"
    got = json.load(open(report))

    print(f"\nin Blender: {len(got)} mesh object(s)")
    assert len(got) == 1, \
        f"the body was SPLIT into {len(got)} objects -- it must stay one mesh"
    o = got[0]
    print(f"  '{o['object']}': {o['polys']} polygons, "
          f"{len(o['slots'])} material slot(s)")
    for i, sl in enumerate(o["slots"]):
        n = o["per_slot"].get(str(i), o["per_slot"].get(i, 0))
        hexv = to_srgb_hex(sl["base"]) if sl["base"] else "?"
        print(f"    slot {i}: {sl['name']:<16} {hexv}  "
              f"{n} polygon(s)")

    assert len(o["slots"]) == len(expect), \
        f"expected {len(expect)} slots, got {len(o['slots'])}"
    used = {int(k) for k, v in o["per_slot"].items() if v}
    assert len(used) == len(expect), \
        f"only {len(used)} of {len(expect)} slot(s) actually carry polygons"
    assert sum(o["per_slot"].values()) == o["polys"], "polygons unassigned"
    want = {to_srgb_hex(c) for c in expect}
    have = {to_srgb_hex(s["base"]) for s in o["slots"] if s["base"]}
    assert want == have, f"slot colours {have} != expected {want}"
    # and the split must be REAL: not everything piled onto one slot
    biggest = max(o["per_slot"].values())
    assert biggest < o["polys"], "every polygon landed on a single slot"
    print(f"\n  every face colour became its own slot, and the "
          f"{bare} uncoloured face(s) inherited the part colour")
    print("\nPASSED: one welded mesh, one slot per colour, faces assigned")


if __name__ == "__main__":
    main()
