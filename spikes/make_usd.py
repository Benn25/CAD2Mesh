"""Build a realistic mechanical assembly, tessellate it, and emit USD two ways:
   A) USD native instancing (instanceable=True + internal reference to a class)
   B) plain internal reference, not instanceable
Then we import both into Blender and see which gives empties + SHARED mesh data."""
import sys, os, math
sys.path.insert(0, os.path.dirname(__file__))
from watertight_spike import tessellate_solid, check_watertight

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir, gp_Trsf, gp_Vec
from OCP.BRepTools import BRepTools
from pxr import Usd, UsdGeom, Gf, Vt, Sdf

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)


def trsf_to_matrix4d(t: gp_Trsf) -> Gf.Matrix4d:
    """OCCT column-vector 3x4 -> USD row-vector Matrix4d (translation in last ROW)."""
    return Gf.Matrix4d(
        t.Value(1,1), t.Value(2,1), t.Value(3,1), 0.0,
        t.Value(1,2), t.Value(2,2), t.Value(3,2), 0.0,
        t.Value(1,3), t.Value(2,3), t.Value(3,3), 0.0,
        t.Value(1,4), t.Value(2,4), t.Value(3,4), 1.0)


def make_plate():
    s = BRepPrimAPI_MakeBox(gp_Pnt(-40,-30,0), 80, 60, 8).Shape()
    for x,y in ((-30,-20),(30,-20),(-30,20),(30,20)):
        d = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x,y,-2), gp_Dir(0,0,1)), 3.2, 12).Shape()
        s = BRepAlgoAPI_Cut(s, d).Shape()
    return s

def make_screw():
    shaft = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,-10), gp_Dir(0,0,1)), 3.0, 14).Shape()
    head  = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,4), gp_Dir(0,0,1)), 5.5, 4).Shape()
    return BRepAlgoAPI_Fuse(shaft, head).Shape()

def make_bracket():
    a = BRepPrimAPI_MakeBox(gp_Pnt(-15,-10,0), 30, 20, 6).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(-15,-10,0), 6, 20, 25).Shape()
    return BRepAlgoAPI_Fuse(a, b).Shape()


def mesh_data(shape, deflection=0.08, angle_deg=14.0):
    BRepTools.Clean_s(shape)
    m = tessellate_solid(shape, deflection=deflection, angle_deg=angle_deg)
    c = check_watertight(m["points"], m["tris"])
    m["watertight"] = (c["boundary_or_nonmanifold"]==0 and c["winding_errors"]==0)
    m["check"] = c
    return m


def write_mesh(stage, path, m):
    mesh = UsdGeom.Mesh.Define(stage, path)
    pts = Vt.Vec3fArray([Gf.Vec3f(*p) for p in m["points"]])
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3]*len(m["tris"])))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([i for t in m["tris"] for i in t]))
    mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(*n) for n in m["normals"]]))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)   # sharp WITHOUT splitting verts
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)      # polygonal, do not subdivide
    xs=[p[0] for p in m["points"]]; ys=[p[1] for p in m["points"]]; zs=[p[2] for p in m["points"]]
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(min(xs),min(ys),min(zs)),
                                         Gf.Vec3f(max(xs),max(ys),max(zs))]))
    return mesh


def xform(stage, path, trsf=None):
    xf = UsdGeom.Xform.Define(stage, path)
    if trsf is not None:
        xf.AddTransformOp().Set(trsf_to_matrix4d(trsf))
    return xf

def T(x=0,y=0,z=0,rz_deg=0.0):
    t = gp_Trsf()
    if rz_deg:
        r = gp_Trsf(); r.SetRotation(gp_Ax2(gp_Pnt(0,0,0), gp_Dir(0,0,1)).Axis(), math.radians(rz_deg))
        t.Multiply(r)
    t.SetTranslationPart(gp_Vec(x,y,z)) if False else None
    tt = gp_Trsf(); tt.SetTranslation(gp_Vec(x,y,z))
    tt.Multiply(t)
    return tt


def build(filename, instanceable):
    stage = Usd.Stage.CreateNew(os.path.join(OUT, filename))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 0.001)          # STEP mm -> USD metres
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())

    # --- prototypes as class prims (abstract: not imaged directly) ---
    for name, data in protos.items():
        stage.CreateClassPrim(f"/_Prototypes/{name}")
        write_mesh(stage, f"/_Prototypes/{name}/Body", data)

    def place(parent, name, proto, trsf):
        p = f"{parent}/{name}"
        xform(stage, p, trsf)
        prim = stage.GetPrimAtPath(p)
        prim.GetReferences().AddInternalReference(f"/_Prototypes/{proto}")
        if instanceable:
            prim.SetInstanceable(True)
        return p

    # Plate: unique part, one occurrence
    place("/Root", "Plate_1", "Plate", T(0,0,0))

    # Sub-assembly defined once as a class, instanced twice
    stage.CreateClassPrim("/_Prototypes/BracketAsm")
    xform(stage, "/_Prototypes/BracketAsm/Bracket_1", T(0,0,8))
    bp = stage.GetPrimAtPath("/_Prototypes/BracketAsm/Bracket_1")
    bp.GetReferences().AddInternalReference("/_Prototypes/Bracket")
    for i,(sx,sy) in enumerate(((-8,0),(8,0)), start=1):
        sp = f"/_Prototypes/BracketAsm/Screw_{i}"
        xform(stage, sp, T(sx,sy,14))
        stage.GetPrimAtPath(sp).GetReferences().AddInternalReference("/_Prototypes/Screw")
        if instanceable:
            stage.GetPrimAtPath(sp).SetInstanceable(True)

    for i,(bx,by,rz) in enumerate(((-25,0,0),(25,0,180)), start=1):
        p = f"/Root/BracketAsm_{i}"
        xform(stage, p, T(bx,by,0,rz))
        prim = stage.GetPrimAtPath(p)
        prim.GetReferences().AddInternalReference("/_Prototypes/BracketAsm")
        if instanceable:
            prim.SetInstanceable(True)

    stage.GetRootLayer().Save()
    return os.path.join(OUT, filename)


print("tessellating prototypes...")
protos = {"Plate": mesh_data(make_plate()),
          "Screw": mesh_data(make_screw()),
          "Bracket": mesh_data(make_bracket())}
for n,m in protos.items():
    c=m["check"]
    print(f"  {n:<9} pts={c['V']:>5} tris={c['F']:>5} "
          f"{'WATERTIGHT' if m['watertight'] else '*** LEAK ***'}")

a = build("assembly_instanceable.usda", True)
b = build("assembly_plainref.usda", False)
print("\nwrote:\n ", a, "\n ", b)
