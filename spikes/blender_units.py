"""Everything must arrive in Blender at scale 1, at the right physical size.

Benn's report: "the main empty (the very top parent) have a scale of 0.001
while all the other parts and empties are properly at scale 1. I need
everything to be at scale 1, but still respect the real size of the object."

That 0.001 is Blender converting a stage whose unit is not its own. Writing the
geometry in METRES and declaring the stage as metres removes the conversion,
and the two halves of "still respect the real size" are:

  * the physical size must be IDENTICAL to the old export -- checked by writing
    the same document both ways and comparing what Blender measures;
  * the PLACEMENTS must scale with the geometry. The handle is the test that
    matters here: 230 occurrences six levels deep, so if the translations kept
    their millimetre values while the meshes shrank, the assembly would fly
    apart and the overall size would be ~1000x too big.

Run:  .venv/Scripts/python.exe spikes/blender_units.py
"""
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from stepusd.writer import write_usd
from stepusd import _testcache

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
PROBE = os.path.join(OUT, "_unit_probe.py")

BLENDER = next(iter(sorted(glob.glob(
    r"C:\no install prog\blender\stable\blender-4.5*\blender.exe"))), None)

_PROBE_SRC = '''
import bpy, sys, json, mathutils
path, report = sys.argv[sys.argv.index("--") + 1:][0:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.unit_settings.system = "METRIC"
bpy.context.scene.unit_settings.length_unit = "MILLIMETERS"
bpy.ops.wm.usd_import(filepath=path, merge_parent_xform=False)
off = [o.name for o in bpy.data.objects
       if any(abs(v - 1.0) > 1e-6 for v in o.scale)]
lo = mathutils.Vector((1e18,) * 3)
hi = mathutils.Vector((-1e18,) * 3)
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    for c in o.bound_box:
        w = o.matrix_world @ mathutils.Vector(c)
        lo = mathutils.Vector(map(min, lo, w))
        hi = mathutils.Vector(map(max, hi, w))
size = hi - lo
u = bpy.context.scene.unit_settings.scale_length
json.dump({"objects": len(bpy.data.objects),
           "off_scale": off,
           "mm": [round(v * u * 1000, 4) for v in size]},
          open(report, "w"))
'''


def in_blender(usd):
    report = os.path.join(OUT, "_unit_report.json")
    for f in (report,):
        if os.path.exists(f):
            os.remove(f)
    open(PROBE, "w", encoding="utf-8").write(_PROBE_SRC)
    subprocess.run([BLENDER, "-b", "--python", PROBE, "--", usd, report],
                   capture_output=True, text=True, timeout=900)
    assert os.path.exists(report), "the Blender probe wrote nothing"
    return json.load(open(report))


def main():
    if not BLENDER:
        print("no Blender found; skipping")
        return
    which = sys.argv[1] if len(sys.argv) > 1 else "handle"
    cache = ".cache/handle" if which == "handle" else _testcache.biggest()
    doc = load_cache(cache)
    n = sum(len(p.bodies) for p in doc.protos.values())
    print(f"{which}: {len(doc.protos)} prototypes, {n} bodies")

    p = MeshParams(deflection=1.0, angle_deg=35, min_size=1.6)
    for proto in doc.protos.values():
        proto.meshes = [tessellate(b, p) for b in proto.bodies]

    os.makedirs(OUT, exist_ok=True)
    results = {}
    for label, flag in (("millimetre stage (old)", False),
                        ("metre stage (new)", True)):
        path = os.path.join(OUT, f"units_{int(flag)}.usda")
        if os.path.exists(path):
            os.remove(path)
        write_usd(doc, path, scale_to_meters=flag)
        r = in_blender(path)
        results[flag] = r
        print(f"  {label:<22} objects={r['objects']:<5} "
              f"off scale 1: {len(r['off_scale']):<3} "
              f"size(mm)={r['mm']}")
        if r["off_scale"][:3]:
            print(f"      {r['off_scale'][:3]}")

    old, new = results[False], results[True]
    assert not new["off_scale"], \
        f"still not at scale 1: {new['off_scale'][:5]}"
    assert old["off_scale"], \
        "the old path was expected to show the 0.001 root Benn reported"
    # the physical size must not move -- this is what catches an unscaled
    # placement, which would blow the assembly up by ~1000x
    for a, b in zip(old["mm"], new["mm"]):
        assert abs(a - b) < max(1e-3, abs(a) * 1e-6), \
            f"the part CHANGED SIZE: {old['mm']} -> {new['mm']}"
    assert old["objects"] == new["objects"], "objects went missing"
    print(f"\n  same physical size to within 1e-6, "
          f"and {new['objects']} objects all at scale 1")
    print("\nPASSED: metre stage, scale 1 everywhere, size unchanged")


if __name__ == "__main__":
    main()
