"""Emit the scene graph as USD: Xform hierarchy (-> Blender empties), one welded
mesh per body, prototypes written ONCE and referenced by every occurrence."""
from __future__ import annotations
import re
from pxr import Usd, UsdGeom, UsdShade, Gf, Vt, Sdf, Tf

PROTO_ROOT = "/_Prototypes"
MAT_ROOT = "/_Materials"


def _m4(t, k: float = 1.0) -> Gf.Matrix4d:
    """OCCT column-vector 3x4 -> USD row-vector Matrix4d (translation last ROW).

    `k` scales the TRANSLATION only. When the geometry is converted to metres
    the placements must come with it, or every part keeps its millimetre offset
    and the assembly explodes -- the rotation is unitless and must NOT be
    touched.
    """
    return Gf.Matrix4d(
        t.Value(1, 1), t.Value(2, 1), t.Value(3, 1), 0.0,
        t.Value(1, 2), t.Value(2, 2), t.Value(3, 2), 0.0,
        t.Value(1, 3), t.Value(2, 3), t.Value(3, 3), 0.0,
        t.Value(1, 4) * k, t.Value(2, 4) * k, t.Value(3, 4) * k, 1.0)


#: characters that may appear in a USD prim name. USD 23.11+ accepts UTF-8
#: identifiers, so Japanese part names survive as prim names -- and Blender
#: reads them straight into the outliner. Tf.MakeValidIdentifier still applies
#: the old ASCII-only rule and would turn 'ネジキャップ２' into '_____________'.
_UNSAFE = re.compile(r"\W", re.UNICODE)


def _ident(name: str) -> str:
    v = _UNSAFE.sub("_", (name or "unnamed").strip())
    if not v or v.strip("_") == "":
        v = Tf.MakeValidIdentifier(name or "unnamed") or "unnamed"
    if v[0].isdigit():
        v = "_" + v
    return v


class _Names:
    """Unique sibling prim names."""

    def __init__(self):
        self.seen = {}

    def take(self, parent: str, name: str) -> str:
        base = _ident(name)
        used = self.seen.setdefault(parent, set())
        n, i = base, 1
        while n in used:
            i += 1
            n = f"{base}_{i}"
        used.add(n)
        return n


def write_usd(doc, out_path: str, meters_per_unit: float = 0.001,
              up_axis: str = "Z", instance_colors: dict | None = None,
              scale_to_meters: bool = True, only=None):
    """Write the document as USD.

    `only`, if given, is a set of (occurrence path, 0-based body index) -- the
    same keys the GUI's outliner uses -- and the export keeps just those bodies,
    at their real placements and under their real parent components. Anything
    with nothing kept below it is left out entirely. A prototype whose bodies
    are only PARTLY kept gets its own filtered prototype, one per distinct
    subset, so instancing still holds between occurrences that keep the same
    bodies.

    `meters_per_unit` describes the INPUT: OCCT normalises every STEP to
    millimetres whatever the file declares, so 0.001 is right and the file's
    own unit never has to be read.

    `scale_to_meters` (default) converts the geometry to metres and declares
    the stage as metres, instead of leaving millimetre coordinates behind a
    metersPerUnit of 0.001. Benn's reason, and it is a real cost in his work:
    Blender turns a stage whose unit is not its own into a SCALE on the root
    empty, so the whole import arrives at 0.001 and fixing that by hand is
    "a big pain in the ass".

    Measured on the knob, imported into Blender 4.5 with the scene set to
    millimetres:

        mm data,     metersPerUnit 0.001   root scale 0.001,  1 object off-scale
        metre data,  metersPerUnit 1.0     root scale 1.0,    0 objects off-scale

    Both display as 40.0 x 57.918 x 55.967 mm -- identical to the STEP's own
    size -- so this changes the numbers in the file and NOT the physical size.
    """
    stage = Usd.Stage.CreateNew(out_path)
    # one factor, applied to points AND to placements, or the parts keep their
    # millimetre offsets while the geometry shrinks
    k = meters_per_unit if scale_to_meters else 1.0
    # USD's stage metadata only knows Y-up and Z-up, so the other four choices
    # are expressed as a rotation on the ASSEMBLY'S OWN ROOT prims that carries
    # the chosen model axis onto the stage's up (it used to live on a synthetic
    # /Root, which has been removed). +Z and +Y keep the metadata-only path they
    # always had, which is the one verified through Blender.
    axis = {"Z": "+Z", "Y": "+Y"}.get(up_axis, up_axis)
    stage_up = UsdGeom.Tokens.y if axis == "+Y" else UsdGeom.Tokens.z
    ROOT_ROT = {"+Z": None, "+Y": None,
                "-Z": (180.0, 0.0, 0.0),      # flip about X
                "-Y": (-90.0, 0.0, 0.0),      # -Y -> +Z
                "+X": (0.0, -90.0, 0.0),      # +X -> +Z
                "-X": (0.0, 90.0, 0.0)}       # -X -> +Z
    UsdGeom.SetStageUpAxis(stage, stage_up)
    UsdGeom.SetStageMetersPerUnit(
        stage, 1.0 if scale_to_meters else meters_per_unit)
    # NO synthetic /Root. Benn: "I dont need this root parent, just keep the
    # top parent from the assembly, it is always OK from a step file". The
    # assembly's own roots become the top-level prims, so Blender's outliner
    # starts at the part he modelled rather than at a wrapper.
    #
    # The up-axis rotation used to live on /Root; with it gone the rotation is
    # applied to each root node instead, as an op AFTER its own transform.
    # USD composes xformOps in list order with row vectors, so [transform,
    # rotateXYZ] means "place the node, THEN rotate the world" -- which is what
    # a parent rotation did. Reversing the two would rotate each node about its
    # own origin instead, which is not the same thing at all.
    rot = ROOT_ROT.get(axis)
    names = _Names()
    materials: dict = {}          # rgb -> UsdShade.Material

    # ---- which bodies each occurrence keeps --------------------------------
    keep = None
    if only is not None:
        keep = {}
        for op, bi in only:
            keep.setdefault(op, set()).add(bi)

    def kept_bodies(node, here):
        """Tuple of 0-based body indices this occurrence exports."""
        n = len(node.proto.meshes)
        if keep is None:
            return tuple(range(n))
        return tuple(sorted(b for b in keep.get(here, ()) if 0 <= b < n))

    def wanted(node, here):
        if keep is None:
            return True
        if node.proto is not None and kept_bodies(node, here):
            return True
        return any(wanted(ch, f"{here}/{ch.name}") for ch in node.children)

    # ---- prototypes, authored once as class prims -----------------------
    # Keyed on (proto key, kept bodies): a full export uses one variant per
    # prototype, exactly as before; a partial one makes a variant per subset.
    variants = {}

    def collect(node, here):
        if node.proto is not None:
            sub = kept_bodies(node, here)
            if sub:
                variants.setdefault((node.proto.key, sub), node.proto)
        for ch in node.children:
            ch_here = f"{here}/{ch.name}"
            if wanted(ch, ch_here):
                collect(ch, ch_here)

    for r in doc.roots:
        if wanted(r, r.name):
            collect(r, r.name)

    proto_path = {}
    pn = _Names()
    for (key, sub), proto in variants.items():
        pname = pn.take(PROTO_ROOT, proto.name)
        path = f"{PROTO_ROOT}/{pname}"
        stage.CreateClassPrim(path)
        proto_path[(key, sub)] = path
        color = (instance_colors or {}).get(key, proto.color)
        multi = len(proto.meshes) > 1
        for i in (b + 1 for b in sub):
            mesh = proto.meshes[i - 1]
            mpath = f"{path}/Body_{i}" if multi else f"{path}/Body"
            faces = (proto.body_face_colors[i - 1]
                     if getattr(proto, "body_face_colors", None)
                     and i - 1 < len(proto.body_face_colors) else None)
            _write_mesh(stage, mpath, mesh, proto, color,
                        None if instance_colors else faces, materials, k)

    # ---- occurrence hierarchy ------------------------------------------
    def emit(node, parent_path, here, world_rot=None):
        nm = names.take(parent_path, node.name)
        path = f"{parent_path}/{nm}"
        xf = UsdGeom.Xform.Define(stage, path)
        xf.AddTransformOp().Set(_m4(node.matrix, k))
        if world_rot is not None:
            xf.AddRotateXYZOp().Set(Gf.Vec3f(*world_rot))
        prim = stage.GetPrimAtPath(path)
        if node.proto is not None:
            sub = kept_bodies(node, here)
            if sub:
                prim.GetReferences().AddInternalReference(
                    proto_path[(node.proto.key, sub)])
        for ch in node.children:
            ch_here = f"{here}/{ch.name}"
            if wanted(ch, ch_here):
                emit(ch, path, ch_here)
        return path

    tops = []
    for r in doc.roots:
        if wanted(r, r.name):
            tops.append(emit(r, "", r.name, rot))
    # A stage needs ONE defaultPrim, so with several assembly roots the first
    # is named. Blender imports the whole stage either way; this only matters
    # to a consumer that references the file.
    if tops:
        first = stage.GetPrimAtPath(tops[0])
        if first and first.IsValid():
            stage.SetDefaultPrim(first)

    stage.GetRootLayer().Save()
    return out_path


def _material_for(stage, materials, rgb):
    """One UsdPreviewSurface per distinct colour, shared across the stage."""
    key = tuple(round(float(c), 4) for c in rgb)
    mat = materials.get(key)
    if mat is not None:
        return mat
    name = "Mat_%02X%02X%02X" % tuple(max(0, min(255, int(c * 255 + 0.5)))
                                      for c in key)
    base, n = name, 1
    while stage.GetPrimAtPath(f"{MAT_ROOT}/{name}"):
        n += 1
        name = f"{base}_{n}"
    mpath = f"{MAT_ROOT}/{name}"
    mat = UsdShade.Material.Define(stage, mpath)
    shader = UsdShade.Shader.Define(stage, f"{mpath}/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(*key))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.45)
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    materials[key] = mat
    return mat


def _bind_materials(stage, mesh, m, face_colors, fallback, materials):
    """Bind per-face colours as GeomSubsets on ONE mesh.

    A part with several materials keeps a single welded, watertight mesh and
    gets one material slot per colour. Splitting the mesh per material is what
    Fusion does and is exactly what must not happen here.
    """
    if not face_colors or not m.face_source or not any(face_colors):
        return 0
    groups: dict = {}
    nfaces = len(face_colors)
    # GeomSubset indices are FACE indices, so walk face_source (one entry per
    # emitted polygon), not the triangulated view.
    for ti, fi in enumerate(m.face_source):
        c = face_colors[fi - 1] if 0 < fi <= nfaces else None
        groups.setdefault(tuple(c) if c else tuple(fallback), []).append(ti)
    if len(groups) < 2:
        return 0

    prim = mesh.GetPrim()
    UsdShade.MaterialBindingAPI.Apply(prim)
    biggest = max(groups.items(), key=lambda kv: len(kv[1]))[0]
    UsdShade.MaterialBindingAPI(prim).Bind(
        _material_for(stage, materials, biggest))

    for rgb, tris in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        nm = "mat_%02X%02X%02X" % tuple(max(0, min(255, int(c * 255 + 0.5)))
                                        for c in rgb)
        sub = UsdGeom.Subset.CreateGeomSubset(
            mesh, nm, UsdGeom.Tokens.face, Vt.IntArray(tris),
            "materialBind", UsdGeom.Tokens.partition)
        sp = sub.GetPrim()
        UsdShade.MaterialBindingAPI.Apply(sp)
        UsdShade.MaterialBindingAPI(sp).Bind(
            _material_for(stage, materials, rgb))
    return len(groups)


def _write_mesh(stage, path, m, proto, color, face_colors=None,
                materials=None, k: float = 1.0):
    mesh = UsdGeom.Mesh.Define(stage, path)
    # `k` converts to metres. Normals are DIRECTIONS and stay unit-length under
    # a uniform scale, so they are deliberately left alone.
    pts = m.points if k == 1.0 else [(x * k, y * k, z * k)
                                     for x, y, z in m.points]
    mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in pts]))
    # triangles AND quads: cylindrical/conical grids are emitted as quads
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([len(f) for f in m.faces]))
    mesh.CreateFaceVertexIndicesAttr(
        Vt.IntArray([i for f in m.faces for i in f]))
    # faceVarying normals: sharp at sharp CAD edges WITHOUT splitting vertices
    mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(*n) for n in m.normals]))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    # NO CREASES, deliberately. Benn: "never mark anything as crease" -- a
    # crease is a subdivision instruction baked into the asset, and it is not
    # ours to decide for someone modelling downstream.
    #
    # This used to write every sharp edge as a crease at maximum sharpness, on
    # the belief that creases were what made Blender mark an edge sharp. That
    # belief was WRONG, and dropping them costs nothing: measured on the knob,
    # with the crease attributes removed entirely Blender still reports the same
    # **298 sharp edges** and now **0 creases** (it was 298 sharp AND 289
    # creases at value 1.0). The sharp flags come from the faceVarying split
    # normals, which diverge exactly where the surface is not tangent-continuous.
    #
    # `m.sharp_edges` is still computed -- `_is_g1` is what decides where the
    # normals split in the first place, so an edge is sharp only where the
    # surfaces are NOT tangent (and therefore not curvature-continuous either).
    # A forced loop cut is tangent across itself, so it is never sharp.
    if m.points:
        xs, ys, zs = zip(*m.points)
        mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(min(xs), min(ys), min(zs)),
                                             Gf.Vec3f(max(xs), max(ys), max(zs))]))
    # A colour on a FACE is more specific than one on the shape, so when every
    # face carries the same one it IS the part's colour and must win.
    #
    # Benn's report -- "everything has always the same color" -- was this.
    # `knob (failing).step` holds two: COLOUR_RGB('Steel - Satin') on the shape
    # and COLOUR_RGB('Opaque(203,210,239)') on all 17 faces, i.e. SolidWorks'
    # default part colour. `_bind_materials` needs TWO groups before it emits
    # GeomSubsets, so a part painted one colour fell through to the shape-level
    # grey and exported as #A0A0A0 instead of the #CBD2EF actually on screen
    # in the CAD system.
    if face_colors and all(face_colors):
        seen = {tuple(c) for c in face_colors}
        if len(seen) == 1:
            color = next(iter(seen))
    nslots = 0
    if materials is not None:
        nslots = _bind_materials(stage, mesh, m, face_colors,
                                 color or (0.72, 0.74, 0.77), materials)
    if nslots:
        # per-face displayColor as well, so viewers that ignore materials still
        # show the right thing
        fallback = color or (0.72, 0.74, 0.77)
        nfaces = len(face_colors)
        cols = [Gf.Vec3f(*(face_colors[f - 1] if 0 < f <= nfaces
                           and face_colors[f - 1] else fallback))
                for f in m.face_source]
        pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
            "displayColor", Sdf.ValueTypeNames.Color3fArray,
            UsdGeom.Tokens.uniform)
        pv.Set(Vt.Vec3fArray(cols))
    elif color:
        mesh.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*color)]))
        if materials is not None:
            prim = mesh.GetPrim()
            UsdShade.MaterialBindingAPI.Apply(prim)
            UsdShade.MaterialBindingAPI(prim).Bind(
                _material_for(stage, materials, color))
    p = mesh.GetPrim()
    # Instance identity, so a post-import script can relink duplicates in Blender.
    # Written under both namespaces: Blender surfaces userProperties:* as a clean
    # 'cad_prototype' custom property, primvars:* keeps the namespace. Which one
    # arrives varies by version, and both together cost nothing.
    for ns in ("userProperties", "primvars"):
        a = p.CreateAttribute(f"{ns}:cad_prototype", Sdf.ValueTypeNames.String)
        a.Set(proto.key)
    a = p.CreateAttribute("userProperties:cad_part", Sdf.ValueTypeNames.String)
    a.Set(proto.name)
    p.SetCustomDataByKey("cad:prototype", proto.key)
    p.SetCustomDataByKey("cad:watertight", bool(m.watertight))
