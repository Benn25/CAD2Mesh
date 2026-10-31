"""Regenerate the README screenshots from the public sample model.

    .venv/Scripts/python.exe docs/make_screenshots.py

Drives the real MainWindow (on a side monitor if there is one, see
spikes/_screen.py) and writes PNGs into docs/.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "spikes"))

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication
from stepusd.gui.main import MainWindow
from stepusd.reader import StepDocument
from _screen import place

STEP = os.path.join(ROOT, "OSTCXBEAC1E--3DModel-STEP-510211.STEP")
OUT = os.path.join(ROOT, "docs")


def pump(app, s):
    end = time.time() + s
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


def wait(app, w, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w.meshes and not (w._worker and w._worker.isRunning()):
            pump(app, 0.5)
            return
        time.sleep(0.01)
    raise SystemExit("tessellation timed out")


def view_shot(w, name):
    from vtkmodules.vtkIOImage import vtkPNGWriter
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter
    rw = w.viewport.interactor.GetRenderWindow()
    rw.Render()
    f = vtkWindowToImageFilter()
    f.SetInput(rw)
    f.ReadFrontBufferOff()
    f.Update()
    wr = vtkPNGWriter()
    wr.SetFileName(os.path.join(OUT, name))
    wr.SetInputConnection(f.GetOutputPort())
    wr.Write()
    print("wrote", name)


def window_shot(app, w, name):
    pump(app, 0.5)
    w.screen().grabWindow(w.winId()).save(os.path.join(OUT, name))
    print("wrote", name)


def camera(w, azimuth=0, elevation=0, zoom=1.0):
    w.viewport.set_view("iso")
    cam = w.viewport.renderer.GetActiveCamera()
    cam.Azimuth(azimuth)
    cam.Elevation(elevation)
    cam.OrthogonalizeViewUp()
    w.viewport.renderer.ResetCamera()
    cam.Zoom(zoom)
    w.viewport.renderer.ResetCameraClippingRange()


app = QApplication(sys.argv[:1])
app.setStyle("Fusion")
w = MainWindow()
w.resize(1600, 960)
place(w)
w.show()
w.viewport.start()
pump(app, 0.8)

w._loaded(StepDocument(STEP))
wait(app, w)
w.tree.expandAll()

# 1. the whole window, wireframe on, one part selected
w.c_edges.setChecked(True)
camera(w, azimuth=180, zoom=1.2)
items = list(w._body_items.values())
if items:
    it = items[len(items) // 2]
    it.setSelected(True)
    w.tree.setCurrentItem(it)
pump(app, 0.6)
window_shot(app, w, "window.png")
w.tree.clearSelection()

# 2. by surface type: what each face IS
w.cb_color.setCurrentIndex(4)
pump(app, 0.6)
camera(w, azimuth=180, zoom=1.2)
view_shot(w, "surface_types.png")

# 3. close-up of the mesh flow
w.cb_color.setCurrentIndex(0)
camera(w, azimuth=180, elevation=10, zoom=2.6)
pump(app, 0.4)
view_shot(w, "mesh_closeup.png")

# 4. by component
w.cb_color.setCurrentIndex(1)
w.c_edges.setChecked(False)
camera(w, azimuth=140, elevation=15, zoom=1.2)
pump(app, 0.4)
view_shot(w, "components.png")
print("done")
