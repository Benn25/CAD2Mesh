"""Drive the GUI headlessly: load a STEP, tessellate, screenshot, change the
sliders, re-tessellate, screenshot again. Verifies the live loop really works."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer
from stepusd.gui.main import MainWindow
from stepusd.reader import StepDocument
from _screen import place

STEP = sys.argv[1] if len(sys.argv) > 1 else "spikes/out/test_assembly.step"
OUT = "spikes/out"


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


def wait_for_tess(app, w, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w.meshes and not (w._worker and w._worker.isRunning()):
            pump(app, 0.3)
            return True
        time.sleep(0.01)
    return False


def shot(w, name):
    from vtkmodules.vtkIOImage import vtkPNGWriter
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter
    rw = w.viewport.interactor.GetRenderWindow()
    rw.Render()
    f = vtkWindowToImageFilter()
    f.SetInput(rw)
    f.SetScale(1)
    f.ReadFrontBufferOff()
    f.Update()
    p = os.path.join(OUT, name)
    wr = vtkPNGWriter(); wr.SetFileName(p); wr.SetInputConnection(f.GetOutputPort())
    wr.Write()
    print(f"  wrote {p}")
    return p


app = QApplication(sys.argv[:1])
app.setStyle("Fusion")
w = MainWindow()
w.resize(1500, 940)
place(w)
w.show()
w.viewport.start()
pump(app, 0.6)

print(f"loading {STEP}")
doc = StepDocument(STEP)
w._loaded(doc)
assert wait_for_tess(app, w), "tessellation timed out"
print(f"  prototypes={w.l_protos.text()} occurrences={w.l_occ.text()}")
print(f"  triangles: {w.l_tris.text()}")
print(f"  watertight: {w.l_water.text()}   time: {w.l_time.text()}")
w.viewport.fit()
pump(app, 0.4)
shot(w, "gui_coarse.png")

# --- now drive the sliders like a user would ---
print("\nrefining: deflection 0.01mm, angle 5deg")
w.s_defl.set_value(0.01)
w.s_angle.set_value(5.0)
w._retessellate()
assert wait_for_tess(app, w), "refine timed out"
print(f"  triangles: {w.l_tris.text()}")
print(f"  watertight: {w.l_water.text()}   time: {w.l_time.text()}")
pump(app, 0.4)
shot(w, "gui_fine.png")

print("\ncoarsening: deflection 1.0mm, angle 40deg")
w.s_defl.set_value(1.0)
w.s_angle.set_value(40.0)
w._retessellate()
assert wait_for_tess(app, w), "coarsen timed out"
print(f"  triangles: {w.l_tris.text()}")
print(f"  watertight: {w.l_water.text()}")
pump(app, 0.4)
shot(w, "gui_verycoarse.png")

# instance colouring
w.cb_color.setCurrentIndex(1)
pump(app, 0.4)
shot(w, "gui_instance_colors.png")

# full-window grab (UI + viewport)
g = app.primaryScreen().grabWindow(w.winId())
gp = os.path.join(OUT, "gui_full.png")
g.save(gp)
print(f"  wrote {gp}")

print("\nGUI smoke test PASSED")
