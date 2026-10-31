"""Generate a test STEP assembly with real XDE instancing, names and colours.

Structure:
    TopAssembly
      Plate          x1
      BracketAsm     x2   (sub-assembly, instanced)
        Bracket      x1
        Screw        x2   (instanced within the sub-assembly)
  -> 7 occurrences from 3 unique prototypes.
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
from OCP.TopLoc import TopLoc_Location
from OCP.Quantity import Quantity_Color, Quantity_NameOfColor as NOC
from OCP.gp import gp_Trsf, gp_Vec, gp_Ax1, gp_Ax2, gp_Pnt, gp_Dir
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)


def T(x=0.0, y=0.0, z=0.0, rz_deg=0.0):
    """Translation with an optional rotation about Z. rz is KEYWORD-ish on
    purpose: passing it positionally as z was a real bug once."""
    rot = gp_Trsf()
    if rz_deg:
        rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(rz_deg))
    out = gp_Trsf()
    out.SetTranslation(gp_Vec(x, y, z))
    out.Multiply(rot)
    return out


def make_plate():
    s = BRepPrimAPI_MakeBox(gp_Pnt(-40, -30, 0), 80, 60, 8).Shape()
    for x, y in ((-30, -20), (30, -20), (-30, 20), (30, 20)):
        d = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -2), gp_Dir(0, 0, 1)), 3.2, 12).Shape()
        s = BRepAlgoAPI_Cut(s, d).Shape()
    return s


def make_screw():
    shaft = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -10), gp_Dir(0, 0, 1)), 3.0, 14).Shape()
    head = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 4), gp_Dir(0, 0, 1)), 5.5, 4).Shape()
    return BRepAlgoAPI_Fuse(shaft, head).Shape()


def make_bracket():
    a = BRepPrimAPI_MakeBox(gp_Pnt(-15, -10, 0), 30, 20, 6).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(-15, -10, 0), 6, 20, 25).Shape()
    return BRepAlgoAPI_Fuse(a, b).Shape()


def build(path=None):
    path = path or os.path.join(OUT, "test_assembly.step")
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    def part(shape, name, color):
        l = st.AddShape(shape, False)
        TDataStd_Name.Set_s(l, TCollection_ExtendedString(name))
        ct.SetColor(l, Quantity_Color(color), XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        return l

    l_plate = part(make_plate(), "Plate", NOC.Quantity_NOC_STEELBLUE)
    l_screw = part(make_screw(), "Screw", NOC.Quantity_NOC_RED)
    l_brack = part(make_bracket(), "Bracket", NOC.Quantity_NOC_GREEN)

    l_sub = st.NewShape()
    TDataStd_Name.Set_s(l_sub, TCollection_ExtendedString("BracketAsm"))
    st.AddComponent(l_sub, l_brack, TopLoc_Location(T(0, 0, 8)))
    st.AddComponent(l_sub, l_screw, TopLoc_Location(T(-8, 0, 14)))
    st.AddComponent(l_sub, l_screw, TopLoc_Location(T(8, 0, 14)))

    l_top = st.NewShape()
    TDataStd_Name.Set_s(l_top, TCollection_ExtendedString("TopAssembly"))
    st.AddComponent(l_top, l_plate, TopLoc_Location(T(0, 0, 0)))
    st.AddComponent(l_top, l_sub, TopLoc_Location(T(-25, 0, 8)))
    st.AddComponent(l_top, l_sub, TopLoc_Location(T(25, 0, 8, rz_deg=180)))
    st.UpdateAssemblies()

    w = STEPCAFControl_Writer()
    w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    w.Write(path)
    return path


if __name__ == "__main__":
    p = build()
    print(f"wrote {p} ({os.path.getsize(p):,} bytes)")
