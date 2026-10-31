"""Which bodies a quality change re-meshes, in each of the three modes.

Benn's rules:
  isolated                 -> only the isolated parts, everything else ignored
  not isolated, selection  -> only the SELECTED parts that have no set quality
  not isolated, no select  -> every part that has no set quality
"""
import sys, os, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop
from stepusd.gui import worker as wk
from stepusd import _testcache as _tc
from stepusd.gui.main import MainWindow
from stepusd.loadproc import load_cache
from _screen import place

CALLS = []
_t = wk.tessellate


def counting(body, params):
    CALLS.append(params.deflection)
    return _t(body, params)


wk.tessellate = counting
# force the serial path so the meshing happens here and can be counted
MainWindow._ensure_pool = lambda self: None


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


def settle(app, w, limit=600):
    t0 = time.time()
    pump(app, 0.35)
    while time.time() - t0 < limit:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w._worker is None and not w._pending and not w._debounce.isActive():
            return time.time() - t0
        time.sleep(0.01)
    return None


def meshed(app, w, value):
    """Change the deviation to an uncached value; return how many bodies ran."""
    CALLS.clear()
    w.s_defl.spin.setValue(value)
    assert settle(app, w) is not None, "run timed out"
    return len(CALLS)


def select(w, keys):
    w.tree.clearSelection()
    for k in keys:
        w._body_items[k].setSelected(True)


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
    assert settle(app, w) is not None
    total = len(w._body_items)
    rows = sorted(w._body_items, key=lambda k: k[1])
    print(f"{total} bodies;  _base frozen at load = {w._base is not None}")
    fails = []

    # --- 3. nothing isolated, nothing selected: everything with no quality ---
    w.tree.clearSelection()
    pump(app, 0.3)
    n = meshed(app, w, 0.31)
    print(f"no isolation, no selection      -> {n:3d} meshed (want {total})")
    if n != total:
        fails.append("global change did not reach every body")

    # --- 2. a selection, nothing isolated: only those, and only unset ones ---
    select(w, rows[:5])
    pump(app, 0.3)
    n = meshed(app, w, 0.32)
    print(f"5 selected, none isolated       -> {n:3d} meshed (want 5)")
    if n != 5:
        fails.append(f"selection change meshed {n}, wanted 5")

    # a selected body that HAS a quality must be left alone
    w._quality[(w._occ_by_path[rows[0][0]].proto_key, rows[0][1])] = "__set__"
    select(w, rows[:5])
    pump(app, 0.3)
    n = meshed(app, w, 0.33)
    print(f"same 5, one has a set quality   -> {n:3d} meshed (want 4)")
    if n != 4:
        fails.append(f"a set-quality body was disturbed ({n} meshed, wanted 4)")
    w._quality.clear()

    # --- 1. isolated: only the isolated parts ---
    select(w, rows[:3])
    pump(app, 0.3)
    w._toggle_isolate(True)
    pump(app, 0.4)
    vis = total - len(w._hidden)
    # deselect, to prove isolation drives the panel without a selection
    w.tree.clearSelection()
    pump(app, 0.3)
    n = meshed(app, w, 0.34)
    print(f"3 isolated, nothing selected    -> {n:3d} meshed (want {vis})")
    if n != vis:
        fails.append(f"isolated change meshed {n}, wanted {vis}")

    w._toggle_isolate(False)
    pump(app, 0.4)

    # --- 3b. a REAL preset must survive a global change ---
    # (the stub name above only exercises _tuning_targets; a preset that does
    #  not resolve falls back to _base and would re-mesh anyway)
    w._custom.clear()
    w._quality.clear()
    for k in rows[:10]:
        w._quality[(w._occ_by_path[k[0]].proto_key, k[1])] = "Standard"
    w.tree.clearSelection()
    pump(app, 0.3)
    meshed(app, w, 0.35)             # settle the preset bodies onto Standard
    n = meshed(app, w, 0.36)
    print(f"global change, 10 on a preset   -> {n:3d} meshed "
          f"(want {total - 10})")
    if n != total - 10:
        fails.append(f"preset bodies disturbed by a global change ({n} meshed)")

    assert not fails, "\n  ".join(["FAILED:"] + fails)
    print("\nPASSED: all three targeting rules hold")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
