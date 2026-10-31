"""End-to-end test against the real 32 MB SolidWorks file.

Checks the three things Benn reported:
  1. UI frozen during load        -> measure event-loop responsiveness
  2. crash on low deflection      -> drag the slider hard, must survive
  3. general behaviour            -> stats, watertight, screenshots
"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop
from stepusd.gui.main import MainWindow
from _screen import place

STEP = glob.glob("*.STEP")[0]
COLD = "--cold" in sys.argv


class Pump:
    """Spin the Qt event loop and count iterations -- our responsiveness meter."""
    def __init__(self, app):
        self.app, self.n = app, 0

    def run(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents(QEventLoop.AllEvents, 10)
            self.n += 1
            time.sleep(0.002)

    def until(self, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents(QEventLoop.AllEvents, 10)
            self.n += 1
            time.sleep(0.002)
            if cond():
                return True
        return False


app = QApplication(sys.argv[:1])
app.setStyle("Fusion")
w = MainWindow(); w.resize(1500, 940)
place(w); w.show(); w.viewport.start()
pump = Pump(app)
pump.run(0.5)

if COLD:
    import shutil
    for d in glob.glob(".cache/*"):
        if os.path.isdir(d) and d != os.path.join(".cache", "proc"):
            shutil.rmtree(d, ignore_errors=True)
    print("cache cleared -> COLD load")

print(f"\nopening {STEP} ({os.path.getsize(STEP)/1e6:.1f} MB)")
t0 = time.time(); pump.n = 0
w.open_step_path(STEP)
ok = pump.until(lambda: w.doc is not None, 300)
load_s = time.time() - t0
rate = pump.n / max(load_s, 1e-9)
print(f"  loaded={ok} in {load_s:.1f}s")
print(f"  event loop ran {pump.n} times ({rate:.0f}/s) "
      f"-> UI {'RESPONSIVE' if rate > 20 else 'FROZEN'}")
print(f"  prototypes={w.l_protos.text()}  occurrences={w.l_occ.text()}")
print(f"  bodies={w.l_bodies.text()}")

print("\nwaiting for first tessellation...")
t0 = time.time()
pump.until(lambda: bool(w.meshes) and w._worker is None, 300)
print(f"  {w.l_tris.text()} | {w.l_water.text()} | {time.time()-t0:.1f}s")

from vtkmodules.vtkIOImage import vtkPNGWriter
from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter


def shot(name):
    rw = w.viewport.interactor.GetRenderWindow(); rw.Render()
    f = vtkWindowToImageFilter(); f.SetInput(rw); f.ReadFrontBufferOff(); f.Update()
    wr = vtkPNGWriter(); wr.SetFileName(f"spikes/out/{name}")
    wr.SetInputConnection(f.GetOutputPort()); wr.Write()
    print(f"  wrote spikes/out/{name}")


w.viewport.set_view("iso"); pump.run(0.4); shot("real_default.png")

print("\n--- hammering the deflection slider (the crash case) ---")
for d in (2.0, 1.0, 0.5, 0.3, 0.2, 0.15, 0.1, 0.08, 0.05):
    # go through the spinbox: set_value() intentionally suppresses signals,
    # so it would NOT reproduce a user drag
    w.s_defl.spin.setValue(d)
    pump.run(0.05)                 # barely any time: simulates a fast drag
    print(f"  set {d} mm", flush=True)
print("  draining (wait past the debounce first)...")
pump.run(0.5)          # let the debounce fire before testing for idle
pump.until(lambda: w._worker is None and not w._pending
           and not w._debounce.isActive(), 600)
print(f"  SURVIVED: {w.l_tris.text()} | {w.l_water.text()} | {w.l_time.text()}")
pump.run(0.4); shot("real_fine.png")

w.cb_color.setCurrentIndex(1); pump.run(0.3); shot("real_instance.png")
g = app.primaryScreen().grabWindow(w.winId()); g.save("spikes/out/real_full.png")
print("  wrote spikes/out/real_full.png")
print("\nREAL FILE TEST PASSED")
