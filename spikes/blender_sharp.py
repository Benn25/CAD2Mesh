import bpy, sys, bmesh
path = sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=path, merge_parent_xform=False)
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
sharp = crease = edges = 0
for o in meshes:
    me = o.data
    edges += len(me.edges)
    a = me.attributes.get("sharp_edge")
    if a is not None:
        sharp += sum(1 for v in a.data if v.value)
    else:
        sharp += sum(1 for e in me.edges if e.use_edge_sharp)
    c = me.attributes.get("crease_edge")
    if c is not None:
        crease += sum(1 for v in c.data if v.value > 0.0)
print(f"RESULT meshes={len(meshes)} edges={edges} sharp={sharp} creased={crease}")
print("RESULT attrs on first mesh:", sorted(meshes[0].data.attributes.keys()))
