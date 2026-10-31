"""The outliner's three columns must fit the dock: no horizontal scroll bar.

Checks the bar the user actually sees (isVisible on the tree's horizontal
scroll bar) at several dock widths, not just the column arithmetic.
"""
import sys, os, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, Qt
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
    caches = [".cache/handle"] + [
        x for x in glob.glob(".cache/*")
        if os.path.isdir(x) and os.path.exists(os.path.join(x, "scene.json"))
        and "handle" not in x][:1]

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bad = []
    for cache in caches:
        w = MainWindow()
        w.resize(1500, 940)
        place(w)
        w.show()
        w.viewport.start()
        pump(app, 0.6)
        w._loaded(load_cache(cache))
        assert wait_for_tess(app, w), "tessellation timed out"
        w.tree.expandAll()
        pump(app, 0.5)

        dock = w.tree.parent()
        while dock is not None and not dock.inherits("QDockWidget"):
            dock = dock.parent()
        print(f"\n{cache}   {w.tree.topLevelItemCount()} top-level items")
        for width in (320, 360, 420, 520, 700):
            w.resizeDocks([dock], [width], Qt.Horizontal)
            pump(app, 0.35)
            sb = w.tree.horizontalScrollBar()
            cols = [w.tree.columnWidth(i) for i in range(3)]
            vp = w.tree.viewport().width()
            flag = "SCROLLBAR" if sb.isVisible() else "ok"
            print(f"  dock {width:4d}px  viewport {vp:4d}  "
                  f"cols {cols[0]:4d}/{cols[1]:3d}/{cols[2]:3d} "
                  f"= {sum(cols):4d}   {flag}")
            if sb.isVisible() and width >= 360:
                bad.append((cache, width))
        # A wide status on a LATE row must widen the column, not be elided:
        # the valve's open shells are bodies 161-163, so sizing the column from
        # the first n rows would cut exactly the text that matters.
        w.resizeDocks([dock], [420], Qt.Horizontal)
        pump(app, 0.3)
        before = w.tree.columnWidth(1)
        rows = sorted(w._body_items.items(), key=lambda kv: kv[0][1])
        last = rows[-1][1]
        last.setText(1, "non-manifold")
        pump(app, 0.4)
        fm = w.tree.fontMetrics()
        needed = fm.horizontalAdvance("non-manifold")
        after = w.tree.columnWidth(1)
        print(f"  wide status on the LAST row: needs {needed}px, "
              f"column {before} -> {after}"
              f"   {'ELIDED' if after < needed else 'fits'}")
        if after < needed:
            bad.append((cache, "elided late row"))
        sb = w.tree.horizontalScrollBar()
        if sb.isVisible():
            bad.append((cache, "scrollbar after widening"))

        w.close()
        pump(app, 0.3)

    assert not bad, f"horizontal scroll bar at usable widths: {bad}"
    print("\nPASSED: no horizontal scroll bar at any usable dock width")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
