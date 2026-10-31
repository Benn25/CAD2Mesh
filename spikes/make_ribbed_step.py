"""A cylinder with radial ribs, for the "cut along a rib corner" case.

Benn's screenshot: a large curved wall with grey ribs standing off it, and the
cut he wants runs exactly through the ribs' corner vertices -- which means the
isocurve lies ALONG each rib's base edge for the rib's width, and crosses open
surface between them. That configuration is what `_iso_interior_runs` and the
coincident-versus-tangent snap test exist for, and none of the real files to
hand contain one.

Run:  .venv/Scripts/python.exe spikes/make_ribbed_step.py
Makes spikes/out/ribbed.step
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeBox
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Trsf, gp_Ax1, gp_Pnt, gp_Dir, gp_Vec
from OCP.STEPControl import STEPControl_Writer, STEPControl_StepModelType
from OCP.Interface import Interface_Static

R = 40.0          # cylinder radius
H = 60.0          # cylinder height
NRIB = 8          # ribs round the wall
RIB_W = 6.0       # rib width (tangential)
RIB_T = 8.0       # how far it stands off the wall
RIB_H = 18.0      # rib height
RIB_Z = 22.0      # the rib's BASE height -- the isocurve we care about


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(out, exist_ok=True)

    shape = BRepPrimAPI_MakeCylinder(R, H).Shape()

    for i in range(NRIB):
        ang = 2 * math.pi * i / NRIB
        # a box straddling the wall, so fusing leaves edges ON the cylinder at
        # z = RIB_Z and z = RIB_Z + RIB_H
        box = BRepPrimAPI_MakeBox(
            gp_Pnt(R - RIB_T * 0.5, -RIB_W / 2, RIB_Z),
            RIB_T, RIB_W, RIB_H).Shape()
        tr = gp_Trsf()
        tr.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), ang)
        box = BRepBuilderAPI_Transform(box, tr, True).Shape()
        shape = BRepAlgoAPI_Fuse(shape, box).Shape()

    path = os.path.join(out, "ribbed.step")
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    w.Write(path)
    print(f"wrote {path}")
    print(f"  {NRIB} ribs, base edges at z = {RIB_Z}")
    return path


if __name__ == "__main__":
    main()
