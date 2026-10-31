"""The Blender-style keys, the active body, and depth-cycling picking."""
import sys, os, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, Qt, QItemSelectionModel
from PySide6.QtTest import QTest
from stepusd import _testcache as _tc
from stepusd.gui.main import MainWindow
from stepusd.loadproc import load_cache
from stepusd.gui.viewport import SELECTION_COLOR, ACTIVE_COLOR
from _screen import place, take_focus


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


def main():
    V = _tc.biggest(exclude_handle=True)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    w = MainWindow()
    w.resize(1400, 900)
    place(w, keyboard=True)   # it drives the window with QTest keys
    w.show()
    w.viewport.start()
    pump(app, 0.6)
    # This test injects keys, and Qt will not match a shortcut whose window is
    # inactive -- the key arrives and silently does nothing, so the failure
    # reads as "Z did not toggle isolate" and points nowhere near the cause.
    # Assert the precondition instead of assuming it.
    assert take_focus(w, app),         "could not activate the window: QTest keys cannot reach the shortcuts"

    w._loaded(load_cache(V))
    assert settle(app, w) is not None
    total = len(w._body_items)
    rows = sorted(w._body_items, key=lambda k: k[1])
    # the default is 1.6x the deviation, so check the RULE, not a number that
    # only holds for one particular model
    from stepusd.gui.main import _nice
    want_min = _nice(w.s_defl.value() * 1.6)
    print(f"{total} bodies;  deviation {w.s_defl.value():g} -> "
          f"min edge {w.s_min.value():g} mm (want {want_min:g})")
    assert abs(w.s_min.value() - want_min) < 1e-9, (w.s_min.value(), want_min)

    # --- active body -------------------------------------------------------
    w.tree.clearSelection()
    for k in rows[:4]:
        w._body_items[k].setSelected(True)
    # NoUpdate: setting the current row must not collapse the
    # selection, which is what a real ctrl-click does too
    w.tree.setCurrentItem(w._body_items[rows[2]], 0,
                          QItemSelectionModel.NoUpdate)
    pump(app, 0.4)
    act = w._active_key()
    print(f"active body = {act[1]}  (selected {len(w._selected_keys())})")
    assert act == rows[2], (act, rows[2])
    assert w.viewport._active == act
    # and it must actually be drawn differently
    cols = {}
    for occ, bi, a in w.viewport._actors:
        if (occ.path, bi) in w._selected_keys():
            cols[(occ.path, bi)] = tuple(round(c, 3)
                                         for c in a.GetProperty().GetColor())
    assert cols[act] == tuple(round(c, 3) for c in ACTIVE_COLOR), cols[act]
    others = {v for k, v in cols.items() if k != act}
    assert others == {tuple(round(c, 3) for c in SELECTION_COLOR)}, others
    print("active drawn in ACTIVE_COLOR, the rest in SELECTION_COLOR")

    # --- propagate quality -------------------------------------------------
    src = (w._occ_by_path[act[0]].proto_key, act[1])
    w._quality[src] = "Fine"
    w._propagate_quality()
    pump(app, 0.4)
    got = [(w._occ_by_path[p].proto_key, b) for p, b in w._selected_keys()]
    assert all(w._quality.get(k) == "Fine" for k in got), \
        {k: w._quality.get(k) for k in got}
    print(f"propagated 'Fine' from the active part to {len(got)} parts")
    w._quality.clear()

    # --- H / Alt+H ---------------------------------------------------------
    w.tree.clearSelection()
    for k in rows[:3]:
        w._body_items[k].setSelected(True)
    pump(app, 0.3)
    QTest.keyClick(w, Qt.Key_H)
    pump(app, 0.4)
    print(f"H -> {len(w._hidden)} hidden (want 3)")
    assert len(w._hidden) == 3, len(w._hidden)
    QTest.keyClick(w, Qt.Key_H, Qt.AltModifier)
    pump(app, 0.4)
    print(f"Alt+H -> {len(w._hidden)} hidden (want 0)")
    assert not w._hidden, len(w._hidden)

    # --- Z toggles the ghosting -------------------------------------------
    before = w.c_isolate.isChecked()
    QTest.keyClick(w, Qt.Key_Z)
    pump(app, 0.3)
    assert w.c_isolate.isChecked() != before
    QTest.keyClick(w, Qt.Key_Z)
    pump(app, 0.3)
    assert w.c_isolate.isChecked() == before
    print("Z toggles isolate (ghost the rest)")

    # --- depth cycling -----------------------------------------------------
    vp = w.viewport
    hits = []
    for occ, bi, a in vp._actors:
        hits.append((occ.path, bi))
    # find a pixel with several bodies behind it
    best, bx, by = [], 0, 0
    sz = vp.interactor.GetRenderWindow().GetSize()
    for x in range(sz[0] // 4, 3 * sz[0] // 4, 37):
        for y in range(sz[1] // 4, 3 * sz[1] // 4, 37):
            st = vp._pick_stack(x, y)
            if len(st) > len(best):
                best, bx, by = st, x, y
    print(f"deepest pixel has {len(best)} bodies stacked")
    assert len(best) >= 2, "no overlapping bodies found to cycle through"
    assert len(set(best)) == len(best), "the stack repeats a body"
    # clicking the same spot walks down the stack and wraps
    seen = []
    vp._cycle_stack, vp._cycle_pos = [], None
    for _ in range(len(best) + 1):
        vp._cycle_pos = (bx, by) if seen else None
        if seen:
            vp._cycle_i += 1
            if vp._cycle_i >= len(vp._cycle_stack):
                vp._cycle_i = 0
        else:
            vp._cycle_stack = vp._pick_stack(bx, by)
            vp._cycle_i = 0
        seen.append(vp._cycle_stack[vp._cycle_i])
    assert seen[:len(best)] == best, (seen, best)
    assert seen[-1] == best[0], "did not wrap back to the front"
    print(f"clicking cycles {len(best)} deep and wraps to the front")

    # --- the active row must follow a VIEWPORT pick, both ways -------------
    w.tree.clearSelection()
    for k in rows[:4]:
        w._body_items[k].setSelected(True)
    w.tree.setCurrentItem(w._body_items[rows[2]], 0,
                          QItemSelectionModel.NoUpdate)
    pump(app, 0.3)
    assert w._active_key() == rows[2]
    w._on_pick(rows[7])                       # a plain click in the 3D view
    pump(app, 0.3)
    print(f"VP pick -> active {w._active_key()[1]} (want {rows[7][1]})")
    assert w._active_key() == rows[7], w._active_key()
    w._on_pick(rows[9], add=True)             # shift-click adds and activates
    pump(app, 0.3)
    print(f"VP shift-pick -> active {w._active_key()[1]} (want {rows[9][1]})")
    assert w._active_key() == rows[9], w._active_key()
    assert w.viewport._active == rows[9]
    it = w._body_items[rows[9]]
    assert it.font(0).bold(), "active row not bold in the outliner"
    col = it.foreground(0).color()
    want = tuple(int(c * 255) for c in ACTIVE_COLOR)
    assert (col.red(), col.green(), col.blue()) == want, col.getRgb()
    print("active row is bold and ACTIVE_COLOR in the outliner")
    # and the OTHER selected rows carry the selection colour, not the default
    other = [k for k in w._selected_keys() if k != rows[9]]
    oc = w._body_items[other[0]].foreground(0).color()
    wsel = tuple(int(c * 255) for c in SELECTION_COLOR)
    assert (oc.red(), oc.green(), oc.blue()) == wsel, oc.getRgb()
    assert not w._body_items[other[0]].font(0).bold()
    print("other selected rows carry SELECTION_COLOR, not bold")
    # deselecting must put a row back to the plain colour
    w.tree.clearSelection()
    pump(app, 0.3)
    pc = w._body_items[rows[9]].foreground(0).color()
    assert (pc.red(), pc.green(), pc.blue()) == (220, 220, 225), pc.getRgb()
    print("deselected rows return to the plain colour")

    # --- isolating filters the outliner ------------------------------------
    w.tree.clearSelection()
    for k in rows[:3]:
        w._body_items[k].setSelected(True)
    pump(app, 0.3)
    w._toggle_isolate(True)
    pump(app, 0.5)
    vis = [k for k, i in w._body_items.items() if not i.isHidden()]
    print(f"isolated 3 -> outliner lists {len(vis)} of {total} bodies")
    assert len(vis) == 3, len(vis)
    w._toggle_isolate(False)
    pump(app, 0.5)
    vis = [k for k, i in w._body_items.items() if not i.isHidden()]
    assert len(vis) == total, len(vis)
    print(f"un-isolated -> outliner lists all {len(vis)} again")

    print("\nPASSED: active body, propagate, H/Alt+H/Z, depth cycling")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
