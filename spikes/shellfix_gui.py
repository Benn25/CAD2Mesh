"""The outliner must STOP saying 'open shell' once a shell has been capped.

Checks the label the user actually reads, not the counter the repair returns --
those disagreed, because the Status column is written by _update_body_flags
while the repair only refreshed the Quality column.
"""
import sys, os, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop
from stepusd import _testcache as _tc
from stepusd.gui.main import MainWindow
from stepusd.loadproc import load_cache
from _screen import place


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


def wait_for_tess(app, w, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w.meshes and not (w._worker and w._worker.isRunning()):
            pump(app, 0.3)
            return True
        time.sleep(0.01)
    return False


def main():
    # this suite needs a model that HAS open shells to repair
    CACHE = sys.argv[1] if len(sys.argv) > 1 else _tc.with_open_shells()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    w = MainWindow()
    w.resize(1500, 940)
    place(w)
    w.show()
    w.viewport.start()
    pump(app, 0.6)

    w._loaded(load_cache(CACHE))
    assert wait_for_tess(app, w), "tessellation timed out"
    print(f"loaded: {w.l_tris.text()}   watertight readout: {w.l_water.text()}")


    def status_of(keys):
        """The Status column text for each (proto key, body)."""
        out = {}
        for (path, bi), item in w._body_items.items():
            occ = w._occ_by_path.get(path)
            if occ is not None and (occ.proto_key, bi) in keys:
                out[(occ.proto_key, bi)] = item.text(1)
        return out


    keys = w._open_shell_keys(selected_only=False)
    print(f"open shells before: {len(keys)}")
    before = status_of(keys)
    for k, v in sorted(before.items()):
        print(f"  {k}: {v!r}")
    assert keys, "no open shells to test with"
    assert all(v == "open shell" for v in before.values()), before

    w._fix_all_shells()
    pump(app, 0.5)

    after = status_of(keys)
    print(f"last-action bar: {w.l_last.text()!r}")
    print(f"watertight readout: {w.l_water.text()!r}")
    for k, v in sorted(after.items()):
        print(f"  {k}: {v!r}")

    # A screenshot of the capped shells: the cap must shade WITH the wall
    # around it, which no counter can tell you.
    from vtkmodules.vtkIOImage import vtkPNGWriter
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter
    w._hidden = {(path, bi) for (path, bi) in w._body_items
                 if (w._occ_by_path[path].proto_key, bi) not in keys}
    w._apply_visibility() if hasattr(w, "_apply_visibility") else None
    w.viewport.set_hidden(w._hidden) if hasattr(
        w.viewport, "set_hidden") else None
    w.c_edges.setChecked(False)
    pump(app, 0.4)
    w.viewport.fit()
    pump(app, 0.6)
    rw = w.viewport.interactor.GetRenderWindow()
    rw.Render()
    f = vtkWindowToImageFilter()
    f.SetInput(rw); f.SetScale(1); f.ReadFrontBufferOff(); f.Update()
    wr = vtkPNGWriter()
    wr.SetFileName("spikes/out/shellfix_capped.png")
    wr.SetInputConnection(f.GetOutputPort())
    wr.Write()
    print("  wrote spikes/out/shellfix_capped.png")

    w.c_edges.setChecked(True)
    pump(app, 0.5)
    rw.Render()
    f2 = vtkWindowToImageFilter()
    f2.SetInput(rw); f2.SetScale(1); f2.ReadFrontBufferOff(); f2.Update()
    wr2 = vtkPNGWriter()
    wr2.SetFileName("spikes/out/shellfix_capped_wire.png")
    wr2.SetInputConnection(f2.GetOutputPort())
    wr2.Write()
    print("  wrote spikes/out/shellfix_capped_wire.png")

    still = [k for k, v in after.items() if v == "open shell"]
    assert not still, f"outliner still says open shell for {still}"
    assert not w._open_shell_keys(selected_only=False), "meshes still open"
    assert w.l_water.text() == "all bodies sealed", w.l_water.text()
    print("\nPASSED: outliner, results panel and meshes all agree")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
