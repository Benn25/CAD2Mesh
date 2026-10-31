"""Esc must abandon a run WITHOUT wedging the tool.

Benn's report: "once I stop a tessellation process with esc, its not possible
to tessellate anymore". The cause was not the worker thread -- that always
cleared -- but the process POOL: Future.cancel() cannot stop a task already
running, so the abandoned bodies kept every worker busy and the next run queued
behind all of them. On a fine setting that is minutes, which reads as a dead
tool. So the two things asserted here are that a run still completes after a
stop, and that the time to recover does NOT scale with how expensive the
cancelled run was.
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


def settle(app, w, limit=600):
    """Wait out the 180ms debounce AND the worker, as the app really behaves."""
    t0 = time.time()
    pump(app, 0.35)
    while time.time() - t0 < limit:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w._worker is None and not w._pending and not w._debounce.isActive():
            return time.time() - t0
        time.sleep(0.01)
    return None


def main():
    V = _tc.biggest(exclude_handle=True)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    w = MainWindow()
    w.resize(1400, 900)
    place(w)
    w.show()
    w.viewport.start()
    pump(app, 0.6)
    w._loaded(load_cache(V))
    assert settle(app, w) is not None, "first tessellation timed out"
    w.c_fine.setChecked(True)

    w.s_defl.spin.setValue(0.60)          # uncached
    base = settle(app, w)
    assert base is not None, "baseline run timed out"
    print(f"baseline: uncached 0.60 on an idle pool = {base:.1f}s")

    times = []
    for i, fine in enumerate((0.05, 0.02, 0.008)):
        w.s_defl.spin.setValue(fine)
        pump(app, 2.5)
        assert w._worker is not None and w._worker.isRunning(), "nothing to stop"
        w._stop_tessellation()
        t_esc = time.time()
        while w._worker is not None and time.time() - t_esc < 180:
            app.processEvents(QEventLoop.AllEvents, 20)
            time.sleep(0.01)
        assert w._worker is None, "worker never cleared after Esc"
        assert not w.progress.isVisible(), "progress bar stuck visible"

        nxt = 0.61 + i * 0.01             # uncached, same cost as the baseline
        w.s_defl.spin.setValue(nxt)
        t = settle(app, w)
        assert t is not None, f"re-tessellation after cancelling {fine} never ran"
        times.append(t)
        print(f"  cancelled a {fine} run -> next {nxt:.2f} run "
              f"{t:.1f}s ({t / base:.1f}x baseline)")

    # the point: recovery is a constant pool re-spawn, not the abandoned work
    spread = max(times) / min(times)
    print(f"spread across cancelled runs of very different cost: {spread:.2f}x")
    assert spread < 1.5, f"recovery still scales with the cancelled run: {times}"
    assert max(times) < base * 3, f"recovery too slow: {times} vs base {base:.1f}"
    print("\nPASSED: Esc frees the pool; recovery is flat and bounded")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
