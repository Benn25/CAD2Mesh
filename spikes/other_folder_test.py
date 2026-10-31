"""Reproduce Benn's 'could not read STEP / loader exited with code 1'.

The trigger is opening a STEP that lives OUTSIDE the project folder: the file
dialog leaves the process working directory somewhere else, and the loader
child (`python -m stepusd.loadproc`) can then no longer import the package.
This test deliberately runs with a foreign cwd and a cold cache.
"""
import os, sys, time, shutil, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STEP = (sys.argv[1] if len(sys.argv) > 1 else
        r"D:\WORKING\2026\2603728_Aishin sweet sweet powder\from client"
        r"\Y形三方ボールバルブ\8-7 DBXA-80S1A-JIS5K（／1R分岐）.STEP")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop
from stepusd.gui.main import MainWindow
from stepusd.gui.worker import ProcessLoader
from _screen import place

print(f"STEP: {STEP}")
print(f"exists: {os.path.exists(STEP)}")

# cold cache for this file, so the child process really runs
probe_dir = None
app = QApplication(sys.argv[:1])
w = MainWindow(); w.resize(1400, 900)
place(w); w.show(); w.viewport.start()

pl = ProcessLoader(STEP)
probe_dir = pl.cache_dir
if os.path.isdir(probe_dir):
    shutil.rmtree(probe_dir, ignore_errors=True)
    print(f"cleared cache {probe_dir}")

# THE TRIGGER: work from a foreign directory, exactly as the file dialog leaves us
os.chdir(os.path.dirname(STEP))
print(f"cwd is now: {os.getcwd()}")

errors = []
w._load_failed = lambda msg: errors.append(msg)      # capture instead of dialog

t0 = time.time()
w.open_step_path(STEP)
end = time.time() + 300
while time.time() < end:
    app.processEvents(QEventLoop.AllEvents, 10)
    if w.doc is not None or errors:
        break
    time.sleep(0.002)

if errors:
    print("\nFAILED:")
    for e in errors:
        print("   " + e.replace("\n", "\n   "))
    sys.exit(1)

print(f"\nloaded in {time.time()-t0:.1f}s")
print(f"  prototypes={w.l_protos.text()}  bodies={w.l_bodies.text()}")

end = time.time() + 300
while time.time() < end:
    app.processEvents(QEventLoop.AllEvents, 10)
    if w.meshes and w._worker is None:
        break
    time.sleep(0.002)
print(f"  {w.l_tris.text()} | {w.l_water.text()} | grid faces {w.l_grid.text()}")
print("\nOTHER-FOLDER TEST PASSED")
