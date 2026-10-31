import sys, os, glob, math
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"H:\Working\programming\Converter step-FBX")
os.chdir(r"H:\Working\programming\Converter step-FBX")
from dataclasses import replace
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from stepusd.gui.viewport import mesh_to_polydata
from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType as ST
from vtkmodules.vtkRenderingCore import (vtkActor, vtkPolyDataMapper, vtkRenderer,
                                         vtkRenderWindow, vtkLightKit,
                                         vtkWindowToImageFilter)
from vtkmodules.vtkIOImage import vtkPNGWriter
import vtkmodules.vtkRenderingOpenGL2  # noqa

V = [x for x in glob.glob(".cache/*") if os.path.isdir(x)
     and os.path.exists(os.path.join(x, "scene.json")) and "handle" not in x][0]
body = [(p, x) for p in load_cache(V).protos.values() for x in p.bodies][137][1]
PR = MeshParams(deflection=0.125, angle_deg=30, grid_aspect=8.0)

fm = TopTools_IndexedMapOfShape()
TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, fm)
target = None
for fi in range(1, fm.Extent() + 1):
    sa = BRepAdaptor_Surface(TopoDS.Face_s(fm.FindKey(fi)))
    if sa.GetType() == ST.GeomAbs_Sphere and sa.Sphere().Radius() > 5:
        s = sa.Sphere()
        target = (fi, s.Location(), s.Radius())
        break
fi, C, R = target
print(f"  framing sphere face {fi}, R={R:.1f}")

for label, pr in (("APEX_off", replace(PR, structured=False)),
                  ("APEX_on", PR)):
    m = tessellate(body, pr)
    # centroid of that face's polygons, to aim at
    pts = [m.points[v] for i, poly in enumerate(m.faces)
           if m.face_source[i] == fi for v in poly]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    cz = sum(p[2] for p in pts) / len(pts)
    d = (cx - C.X(), cy - C.Y(), cz - C.Z())
    n = math.sqrt(sum(c * c for c in d)) or 1.0
    d = tuple(c / n for c in d)
    print(f"  {label}: polys={len(m.faces):,} face polys={len(pts)//3}")
    ren = vtkRenderer()
    ren.SetBackground(0.80, 0.82, 0.84)
    ren.SetBackground2(0.66, 0.68, 0.71)
    ren.GradientBackgroundOn()
    lk = vtkLightKit()
    lk.SetKeyLightIntensity(1.15)
    lk.AddLightsToRenderer(ren)
    mp = vtkPolyDataMapper()
    mp.SetInputData(mesh_to_polydata(m))
    a = vtkActor()
    a.SetMapper(mp)
    p2 = a.GetProperty()
    p2.SetColor(0.72, 0.74, 0.77)
    p2.SetInterpolationToPhong()
    p2.SetAmbient(0.16)
    p2.SetDiffuse(0.62)
    p2.SetSpecular(0.85)
    p2.SetSpecularPower(55)
    p2.SetEdgeVisibility(True)
    p2.SetEdgeColor(0.05, 0.05, 0.06)
    p2.SetLineWidth(1.2)
    ren.AddActor(a)
    rw = vtkRenderWindow()
    rw.SetOffScreenRendering(True)
    rw.SetSize(760, 700)
    rw.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(cx, cy, cz)
    dist = R * 2.2
    cam.SetPosition(cx + d[0] * dist, cy + d[1] * dist, cz + d[2] * dist)
    cam.SetViewUp(0, 0, 1)
    ren.ResetCameraClippingRange()
    rw.Render()
    f = vtkWindowToImageFilter()
    f.SetInput(rw)
    f.ReadFrontBufferOff()
    f.Update()
    w = vtkPNGWriter()
    w.SetFileName(f"spikes/out/{label}.png")
    w.SetInputConnection(f.GetOutputPort())
    w.Write()
    print(f"    wrote spikes/out/{label}.png")
