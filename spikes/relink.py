import bpy, sys, bmesh, collections, re
path = sys.argv[sys.argv.index("--")+1]
bpy.ops.wm.read_factory_settings(use_empty=True)
_opts = {x.identifier for x in bpy.ops.wm.usd_import.get_rna_type().properties}
kw = dict(filepath=path, support_scene_instancing=False, merge_parent_xform=False)
if 'attr_import_mode' in _opts: kw['attr_import_mode'] = 'ALL'        # Blender 4.x
if 'property_import_mode' in _opts: kw['property_import_mode'] = 'ALL' # Blender 5.x
bpy.ops.wm.usd_import(**kw)
print("\n--- what metadata survived? (first mesh object) ---")
mo=[o for o in bpy.data.objects if o.type=='MESH']
o=mo[0]
print("  object custom props:", dict(o.items()))
print("  mesh   custom props:", dict(o.data.items()))
print("  mesh attributes    :", [a.name for a in o.data.attributes])

KEYS = ("cad_prototype", "userProperties:cad_prototype",
        "primvars:cad_prototype", "cad:prototype")

def proto_of(ob):
    for src in (ob.data, ob):
        for k in KEYS:
            if k in src.keys():
                return src[k]
    # No tag found. Do NOT fall back to the mesh name: every body is named
    # "Body", so that would merge the whole assembly onto one datablock.
    raise SystemExit(
        f"ERROR: no cad_prototype tag on '{ob.name}'. Re-export with a writer "
        f"that tags prototypes, or import with attr/property_import_mode='ALL'. "
        f"Found keys: mesh={list(ob.data.keys())} object={list(ob.keys())}")

print("\n--- relinking duplicates by prototype ---")
groups=collections.defaultdict(list)
for ob in mo: groups[proto_of(ob)].append(ob)
before=len(bpy.data.meshes)
for proto, obs in sorted(groups.items()):
    keep=obs[0].data
    for ob in obs[1:]: ob.data=keep
    print(f"  {proto:<28} {len(obs)} objects -> 1 datablock '{keep.name}'")
for me in list(bpy.data.meshes):
    if me.users==0: bpy.data.meshes.remove(me)
print(f"\n  datablocks {before} -> {len(bpy.data.meshes)}")

print("\n--- final scene ---")
bad=0
for ob in sorted(bpy.data.objects,key=lambda x:x.name):
    par=ob.parent.name if ob.parent else "-"
    ex=f" data='{ob.data.name}' users={ob.data.users}" if ob.type=='MESH' else ""
    print(f"  {ob.type:<6} {ob.name:<22} parent={par:<20}{ex}")
for me in bpy.data.meshes:
    bm=bmesh.new(); bm.from_mesh(me)
    nm=sum(1 for e in bm.edges if len(e.link_faces)!=2); bm.free(); bad+=nm
    print(f"   mesh '{me.name}' users={me.users} V={len(me.vertices)} F={len(me.polygons)} "
          f"nonmanifold={nm} custom_normals={me.has_custom_normals}")
n_mo=len(mo); n_db=len(bpy.data.meshes)
print(f"\nRESULT: {n_mo} mesh objects / {n_db} datablocks | "
      f"watertight={'YES' if bad==0 else 'NO'} | "
      f"instancing={'SHARED (linked duplicates)' if n_mo>n_db else 'duplicated'}")
