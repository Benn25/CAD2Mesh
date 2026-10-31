"""A curved wall with a hexagonal boss, for the "cut through a bolt" case.

Benn's motor screenshot: three cuts across a big blue wall, each lined up with
a vertex of a hexagonal bolt boss, and the wall does not get cut. A hexagon on
a cylinder gives all three configurations at once, which is what makes it a
useful fixture:

    through two opposite vertices  the cut CROSSES the opening
    at the topmost vertex          the cut TOUCHES it at one point
    along a flat                   the cut lies ALONG an edge

Run:  .venv/Scripts/python.exe spikes/make_bossed_step.py
Makes spikes/out/bossed.step
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_Transform)
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.gp import gp_Pnt, gp_Vec, gp_Trsf, gp_Ax1, gp_Dir
from OCP.STEPControl import STEPControl_Writer, STEPControl_StepModelType
from OCP.Interface import Interface_Static

R = 40.0          # wall radius
H = 60.0          # wall height
HEX_R = 9.0       # hexagon circumradius
HEX_Z = 30.0      # its centre height
STAND = 10.0      # how far the boss stands off the wall
NBOSS = 3         # bosses round the wall


def hex_boss(angle):
    """A hexagonal prism sticking radially out of the wall.

    A vertex is put at the TOP (rotated 90 degrees) so that one cut can be
    aligned with an extreme vertex -- the touching case -- and another through
    two opposite vertices.
    """
    poly = BRepBuilderAPI_MakePolygon()
    for i in range(6):
        a = math.pi / 2 + 2 * math.pi * i / 6
        poly.Add(gp_Pnt(R - STAND * 0.5, HEX_R * math.cos(a),
                        HEX_Z + HEX_R * math.sin(a)))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    solid = BRepPrimAPI_MakePrism(face, gp_Vec(STAND * 1.5, 0, 0)).Shape()
    tr = gp_Trsf()
    tr.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle)
    return BRepBuilderAPI_Transform(solid, tr, True).Shape()


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(out, exist_ok=True)
    shape = BRepPrimAPI_MakeCylinder(R, H).Shape()
    for i in range(NBOSS):
        shape = BRepAlgoAPI_Fuse(
            shape, hex_boss(2 * math.pi * i / NBOSS)).Shape()
    path = os.path.join(out, "bossed.step")
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    w.Write(path)
    print(f"wrote {path}")
    print(f"  {NBOSS} hex bosses, centre z={HEX_Z}, circumradius {HEX_R}")
    print(f"  top vertex   z = {HEX_Z + HEX_R}   (a cut TOUCHES it)")
    print(f"  side vertices z = {HEX_Z + HEX_R * math.sin(math.pi/6):.4f} "
          f"and {HEX_Z - HEX_R * math.sin(math.pi/6):.4f}  (cut CROSSES)")
    return path


if __name__ == "__main__":
    main()
