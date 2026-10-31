"""Render the same body with OCCT's Delaunay tessellation vs the ruled grid,
so the edge structure on cylinders/cones can be compared directly."""
import os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from stepusd.gui.viewport import mesh_to_polydata
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType as ST
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS

from vtkmodules.vtkRenderingCore import (vtkActor, vtkPolyDataMapper, vtkRenderer,
                                         vtkRenderWindow, vtkLightKit,
                                         vtkWindowToImageFilter)
from vtkmodules.vtkIOImage import vtkPNGWriter
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401

d = [x for x in glob.glob(".cache/*")
     if os.path.isdir(x) and os.path.exists(os.path.join(x, "scene.json"))][0]
proto = list(load_cache(d).protos.values())[0]

# pick the body with the most cylindrical/conical faces
best, best_n = None, -1
for i, b in enumerate(proto.bodies):
    n = 0
    e = TopExp_Explorer(b, TopAbs_ShapeEnum.TopAbs_FACE)
    while e.More():
        t = BRepAdaptor_Surface(TopoDS.Face_s(e.Current())).GetType()
        if t in (ST.GeomAbs_Cylinder, ST.GeomAbs_Cone):
            n += 1
        e.Next()
    if n > best_n:
        best, best_n = i, n
print(f"body {best} has {best_n} cylindrical/conical faces")

DEFL = float(sys.argv[1]) if len(sys.argv) > 1 else 0.4
ANG = 25.0
ASPECT = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
for label, structured, quads in (("occt", False, False), ("grid", True, True)):
    m = tessellate(proto.bodies[best],
                   MeshParams(deflection=DEFL, angle_deg=ANG, structured=structured,
                              grid_aspect=ASPECT, quads=quads))
    print(f"  {label}: faces={len(m.faces):,} quads={m.n_quads:,} tris={len(m.tris):,} "
          f"structured={m.structured_faces} watertight={m.watertight}")
    ren = vtkRenderer()
    ren.SetBackground(0.16, 0.17, 0.19)
    ren.SetBackground2(0.09, 0.10, 0.11)
    ren.GradientBackgroundOn()
    lk = vtkLightKit(); lk.AddLightsToRenderer(ren)
    mp = vtkPolyDataMapper(); mp.SetInputData(mesh_to_polydata(m))
    a = vtkActor(); a.SetMapper(mp)
    p = a.GetProperty()
    p.SetColor(0.62, 0.66, 0.72)
    p.SetAmbient(0.20); p.SetDiffuse(0.80)
    p.SetEdgeVisibility(True); p.SetEdgeColor(0.02, 0.02, 0.03); p.SetLineWidth(1.2)
    ren.AddActor(a)
    rw = vtkRenderWindow(); rw.SetOffScreenRendering(True)
    rw.SetSize(1100, 900); rw.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(0, 0, 0); cam.SetPosition(1, -1, 0.55); cam.SetViewUp(0, 0, 1)
    ren.ResetCamera()
    cam.Zoom(1.9)
    rw.Render()
    f = vtkWindowToImageFilter(); f.SetInput(rw); f.ReadFrontBufferOff(); f.Update()
    w = vtkPNGWriter(); w.SetFileName(f"spikes/out/tess_{label}.png")
    w.SetInputConnection(f.GetOutputPort()); w.Write()
    print(f"    wrote spikes/out/tess_{label}.png")
