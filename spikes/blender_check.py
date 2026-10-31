import bpy, sys, bmesh
path = sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=path, merge_parent_xform=False)
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
custom = sum(1 for o in meshes if o.data.has_custom_normals)
smooth = sum(1 for o in meshes
             for p in o.data.polygons if p.use_smooth)
flat = sum(1 for o in meshes for p in o.data.polygons if not p.use_smooth)
print(f"RESULT meshes={len(meshes)} custom_normals={custom} "
      f"smooth_polys={smooth} flat_polys={flat}")
me = meshes[0].data
ln = [tuple(round(c, 4) for c in l.normal) for l in me.loops[:6]]
print("RESULT sample loop normals:", ln)
# do loop normals differ from the face normal? that is the whole point of
# faceVarying normals: smooth across a curved face, sharp at a real CAD edge
p0 = me.polygons[0]
fn = tuple(round(c, 4) for c in p0.normal)
lns = [tuple(round(c, 4) for c in me.loops[i].normal) for i in p0.loop_indices]
print("RESULT face0 normal:", fn, " its loop normals:", lns)
tot_open = tot_nm = 0
for o in meshes:
    bm = bmesh.new(); bm.from_mesh(o.data)
    tot_open += sum(1 for e in bm.edges if len(e.link_faces) == 1)
    tot_nm += sum(1 for e in bm.edges if len(e.link_faces) > 2)
    bm.free()
print(f"RESULT open_edges_total={tot_open} nonmanifold_total={tot_nm}")
