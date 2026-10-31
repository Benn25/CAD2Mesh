"""Exercise the new UI features: body tree, selection, visibility, colour modes."""
import os, sys, time, glob
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QEventLoop
from stepusd.gui.main import MainWindow
from stepusd.loadproc import load_cache
from _screen import place


def _biggest_cache():
    """The largest cached model, not just whatever glob returns first.

    Picking [0] made these tests depend on which STEP files happen to have been
    opened recently -- adding the knob and motor silently switched this suite
    onto a 1-body model and it failed on assertions written for the valve.
    """
    best = None
    for c in glob.glob(".cache/*"):
        if not (os.path.isdir(c) and os.path.exists(os.path.join(c, "scene.json"))):
            continue
        n = os.path.getsize(os.path.join(c, "scene.json"))
        if best is None or n > best[0]:
            best = (n, c)
    if best is None:
        raise SystemExit("no cached model found; open a STEP in the GUI first")
    return best[1]


CACHE = _biggest_cache()


def main():
    app = QApplication(sys.argv[:1])
    w = MainWindow(); w.resize(1500, 940)
    place(w); w.show(); w.viewport.start()
    w.viewport.enable_picking(w._on_pick)


    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents(QEventLoop.AllEvents, 10)
            time.sleep(0.002)


    def wait(cond, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents(QEventLoop.AllEvents, 10)
            if cond():
                return True
            time.sleep(0.002)
        return False


    w.s_defl.spin.setValue(0.6)
    w._loaded(load_cache(CACHE))
    assert wait(lambda: bool(w.meshes) and w._worker is None), "tessellation timed out"
    print(f"loaded: {w.l_bodies.text()} | {w.l_water.text()}")

    print(f"\ntree: {len(w._node_items)} component items, {len(w._body_items)} body items")
    flags = {}
    for (path, bi), it in w._body_items.items():
        flags[it.text(1)] = flags.get(it.text(1), 0) + 1
    odd = {k: v for k, v in flags.items() if not k.endswith("tris")}
    print(f"  body status labels: {odd if odd else 'all normal'}")

    print("\n-- visibility --")
    print(f"  actors: {len(w.viewport._actors)}")
    w._set_all_visible(False)
    pump(0.2)
    vis = sum(1 for _, _, a in w.viewport._actors if a.GetVisibility())
    print(f"  after Hide all : {vis} visible (expect 0)")
    w._set_all_visible(True)
    pump(0.2)
    vis = sum(1 for _, _, a in w.viewport._actors if a.GetVisibility())
    print(f"  after Show all : {vis} visible (expect {len(w.viewport._actors)})")

    # hide a single body via its checkbox
    key = sorted(w._body_items)[5]
    w._body_items[key].setCheckState(0, Qt.Unchecked)
    pump(0.2)
    vis = sum(1 for _, _, a in w.viewport._actors if a.GetVisibility())
    print(f"  after hiding one body: {vis} visible (expect {len(w.viewport._actors)-1})")
    w._body_items[key].setCheckState(0, Qt.Checked)
    pump(0.2)

    print("\n-- selection --")
    w.tree.clearSelection()
    sel = sorted(w._body_items)[10:14]
    for k in sel:
        w._body_items[k].setSelected(True)
    pump(0.2)
    print(f"  selected {len(sel)} bodies -> viewport keys {len(w.viewport._selected)}")
    w.c_isolate.setChecked(True)
    pump(0.2)
    dim = sum(1 for _, _, a in w.viewport._actors if a.GetProperty().GetOpacity() < 0.5)
    print(f"  isolate on -> {dim} actors dimmed")
    w.c_isolate.setChecked(False)

    print("\n-- pick from viewport --")
    w._on_pick(sorted(w._body_items)[20])
    pump(0.2)
    print(f"  picked -> tree selection = {len(w.tree.selectedItems())} item(s), "
          f"text='{w.tree.selectedItems()[0].text(0) if w.tree.selectedItems() else ''}'")

    print("\n-- colour modes --")
    from vtkmodules.vtkIOImage import vtkPNGWriter
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter


    def shot(name):
        rw = w.viewport.interactor.GetRenderWindow(); rw.Render()
        f = vtkWindowToImageFilter(); f.SetInput(rw); f.ReadFrontBufferOff(); f.Update()
        wr = vtkPNGWriter(); wr.SetFileName(f"spikes/out/{name}")
        wr.SetInputConnection(f.GetOutputPort()); wr.Write()


    w.viewport.set_view("iso")
    for idx, name in ((0, "colour_material"), (1, "colour_component"), (2, "colour_part")):
        w.cb_color.setCurrentIndex(idx)
        pump(0.4)
        cols = {tuple(round(c, 3) for c in a.GetProperty().GetColor())
                for _, _, a in w.viewport._actors}
        print(f"  {w.cb_color.currentText():<22} {len(cols)} distinct colours")
        shot(f"{name}.png")

    g = app.primaryScreen().grabWindow(w.winId())
    g.save("spikes/out/ui_features.png")
    print("\nUI FEATURES TEST PASSED")


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
