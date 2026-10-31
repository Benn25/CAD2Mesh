import bpy, sys, bmesh
argv = sys.argv[sys.argv.index("--")+1:]
path, scene_inst, merge = argv[0], argv[1]=="1", argv[2]=="1"
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=path, support_scene_instancing=scene_inst,
                      merge_parent_xform=merge)
print(f"\n### file={path.split(chr(92))[-1].split('/')[-1]} "
      f"support_scene_instancing={scene_inst} merge_parent_xform={merge}")
objs=list(bpy.data.objects)
for o in sorted(objs,key=lambda x:x.name):
    par=o.parent.name if o.parent else "-"
    ex=f" data='{o.data.name}' users={o.data.users}" if o.type=='MESH' else ""
    if o.type=='EMPTY' and o.instance_type!='NONE':
        ex=f" INSTANCE_COLL={getattr(o.instance_collection,'name',None)}"
    print(f"  {o.type:<6} {o.name:<24} parent={par:<20}{ex}")
bad=0
for me in bpy.data.meshes:
    bm=bmesh.new(); bm.from_mesh(me)
    nm=sum(1 for e in bm.edges if len(e.link_faces)!=2); bm.free(); bad+=nm
    print(f"   mesh '{me.name}' users={me.users} V={len(me.vertices)} F={len(me.polygons)} nonmanifold={nm}")
mo=sum(1 for o in objs if o.type=='MESH'); uq=len(bpy.data.meshes)
print(f"   => {mo} mesh objects / {uq} datablocks | watertight={'YES' if bad==0 else 'NO'} "
      f"| instancing={'SHARED' if mo>uq else 'DUPLICATED'}")
