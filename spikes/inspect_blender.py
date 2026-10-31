import bpy, sys, bmesh, collections
argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
path = argv[0]

bpy.ops.wm.read_factory_settings(use_empty=True)

props = bpy.ops.wm.usd_import.get_rna_type().properties
opts = sorted(p.identifier for p in props if p.identifier not in ("rna_type",))
print("USD_IMPORT_OPTIONS:", ", ".join(opts))

bpy.ops.wm.usd_import(filepath=path)

print(f"\n=== {path} ===")
print(f"blender {bpy.app.version_string}")

objs = list(bpy.data.objects)
print(f"\nOBJECTS ({len(objs)}):")
for o in sorted(objs, key=lambda x: x.name):
    par = o.parent.name if o.parent else "-"
    extra = ""
    if o.type == 'MESH':
        extra = f" data='{o.data.name}' users={o.data.users}"
    if o.type == 'EMPTY' and o.instance_type != 'NONE':
        extra = f" instance_type={o.instance_type} coll={getattr(o.instance_collection,'name',None)}"
    print(f"  {o.type:<6} {o.name:<26} parent={par:<22}{extra}")

print(f"\nMESH DATABLOCKS ({len(bpy.data.meshes)}):")
for me in bpy.data.meshes:
    bm = bmesh.new(); bm.from_mesh(me)
    nonman = sum(1 for e in bm.edges if len(e.link_faces) != 2)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) < 2)
    loose = sum(1 for v in bm.verts if not v.link_edges)
    bm.free()
    try:    sharp = sum(1 for e in me.edges if e.use_edge_sharp)
    except Exception: sharp = "n/a"
    custom = getattr(me, "has_custom_normals", "n/a")
    V,E,F = len(me.vertices), len(me.edges), len(me.polygons)
    verdict = "WATERTIGHT" if nonman == 0 else f"*** {nonman} BAD EDGES ***"
    print(f"  '{me.name}' users={me.users} V={V} E={E} F={F} euler={V-E+F}")
    print(f"      nonmanifold/open={nonman} (boundary={boundary}) loose_verts={loose} "
          f"sharp_edges={sharp} custom_normals={custom}  -> {verdict}")

print(f"\nCOLLECTIONS ({len(bpy.data.collections)}): {[c.name for c in bpy.data.collections]}")
uniq = len(bpy.data.meshes); meshobjs = sum(1 for o in objs if o.type=='MESH')
print(f"\nSUMMARY: {meshobjs} mesh objects sharing {uniq} unique mesh datablocks "
      f"-> instancing {'PRESERVED' if meshobjs > uniq else 'LOST (all data duplicated)'}")
