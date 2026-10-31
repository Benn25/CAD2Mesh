"""Generate a STEP where a SINGLE body carries SEVERAL face colours.

Benn's own export has no colour data at all, so this fixture is what proves the
per-face material path: one solid, different colours on different faces, which
must survive as separate material slots on one welded mesh (NOT as separate
meshes -- splitting by material is exactly the Fusion behaviour he rejects).
"""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TDataStd import TDataStd_Name
from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType as ST
from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
from OCP.TopLoc import TopLoc_Location

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)

# a block with a through hole and a boss: planar, cylindrical faces to colour
block = BRepPrimAPI_MakeBox(gp_Pnt(-30, -20, 0), 60, 40, 14).Shape()
boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 14), gp_Dir(0, 0, 1)), 9, 10).Shape()
solid = BRepAlgoAPI_Fuse(block, boss).Shape()
hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -5), gp_Dir(0, 0, 1)), 5, 40).Shape()
solid = BRepAlgoAPI_Cut(solid, hole).Shape()

app = XCAFApp_Application.GetApplication_s()
doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

lab = st.AddShape(solid, False)
TDataStd_Name.Set_s(lab, TCollection_ExtendedString("MultiMaterialPart"))

# overall colour, then per-face overrides
ct.SetColor(lab, Quantity_Color(0.55, 0.57, 0.60, Quantity_TypeOfColor.Quantity_TOC_RGB),
            XCAFDoc_ColorType.XCAFDoc_ColorSurf)

PALETTE = [(0.80, 0.30, 0.25), (0.25, 0.55, 0.75), (0.35, 0.65, 0.40),
           (0.85, 0.65, 0.20), (0.55, 0.40, 0.70)]

exp = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
n = 0
assigned = []
while exp.More():
    f = TopoDS.Face_s(exp.Current())
    exp.Next()
    kind = BRepAdaptor_Surface(f).GetType()
    # colour the cylindrical faces and a couple of planes distinctly
    if kind == ST.GeomAbs_Cylinder or n % 3 == 0:
        col = PALETTE[len(assigned) % len(PALETTE)]
        sub = st.AddSubShape(lab, f)
        if not sub.IsNull():
            ct.SetColor(sub, Quantity_Color(*col, Quantity_TypeOfColor.Quantity_TOC_RGB),
                        XCAFDoc_ColorType.XCAFDoc_ColorSurf)
            assigned.append(col)
    n += 1

st.UpdateAssemblies()
path = os.path.join(OUT, "multicolor.step")
w = STEPCAFControl_Writer()
w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
w.Write(path)
print(f"wrote {path} ({os.path.getsize(path):,} bytes)")
print(f"  {n} faces, {len(assigned)} given their own colour, "
      f"{len(set(assigned))} distinct")
