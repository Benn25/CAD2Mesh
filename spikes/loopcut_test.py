"""Forced loop cuts: click a surface, get a real edge in the mesh."""
import sys, os, time, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop
from stepusd.gui.main import MainWindow
from stepusd.reader import StepDocument
from _screen import place


def pump(app, s):
    e = time.time() + s
    while time.time() < e:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


def settle(app, w, limit=300):
    t0 = time.time()
    pump(app, 0.35)
    while time.time() - t0 < limit:
        app.processEvents(QEventLoop.AllEvents, 20)
        if (w.meshes and w._worker is None and not w._pending
                and not w._debounce.isActive()):
            return True
        time.sleep(0.01)
    return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    step = args[0] if args else "knob (failing).step"
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    w = MainWindow()
    w.resize(1400, 900)
    place(w)
    w.show()
    w.viewport.start()
    pump(app, 0.6)
    w._loaded(StepDocument(step))
    assert settle(app, w), "load timed out"
    pk = list(w.meshes)[0]
    m0 = w.meshes[pk][0]
    print(f"  before: polys={len(m0.faces):,} watertight={m0.watertight} "
          f"cuts={m0.iso_cuts_applied}")

    # arm the tool and simulate a click on a real polygon of the body
    w.c_loopcut.setChecked(True)
    pump(app, 0.2)
    assert w.viewport._cell_cb is not None, "click handler not armed"

    key = next(k for k in w._body_items if k[1] == 0)
    cell = len(m0.faces) // 2
    poly = m0.faces[cell]
    pt = [sum(m0.points[i][k] for i in poly) / len(poly) for k in range(3)]
    fi = m0.face_source[cell]
    for direction in ("U", "V"):
        w.cb_uv.setCurrentText(direction)
        pump(app, 0.1)
        w._add_loopcut(key, cell, tuple(pt))
        assert settle(app, w), f"{direction} cut timed out"
        m = w.meshes[pk][0]
        print(f"  after {direction} cut on face {fi}: polys={len(m.faces):,} "
              f"cuts={m.iso_cuts_applied} watertight={m.watertight} "
              f"open={m.boundary_edges} nonman={m.nonmanifold_edges} "
              f"max_src={max(m.face_source)}")
        assert m.iso_cuts_applied >= 1, "cut not applied"
        assert m.boundary_edges == 0, "the cut opened a hole"
        assert m.nonmanifold_edges == 0, "the cut made a non-manifold edge"

    # the cut must survive a re-tessellation at other settings
    w.s_defl.spin.setValue(w.s_defl.value() * 2.0)
    assert settle(app, w), "re-tessellation timed out"
    m = w.meshes[pk][0]
    print(f"  after coarsening: polys={len(m.faces):,} "
          f"cuts={m.iso_cuts_applied} watertight={m.watertight}")
    assert m.iso_cuts_applied >= 1, "cuts lost on re-tessellation"

    n = len(w._iso_cuts.get((pk, 0), ()))
    w.tree.clearSelection()
    w._body_items[key].setSelected(True)
    pump(app, 0.3)
    w._clear_loopcuts()
    assert settle(app, w), "clear timed out"
    m = w.meshes[pk][0]
    print(f"  cleared {n} cut(s): polys={len(m.faces):,} "
          f"cuts={m.iso_cuts_applied} watertight={m.watertight}")
    assert m.iso_cuts_applied == 0, "cuts not cleared"
    print("\nPASSED: loop cuts apply, survive re-meshing, and clear")


if __name__ == "__main__" and not any(
        a.startswith("--") for a in sys.argv[1:]):   # Windows spawn guard
    main()


def check_blender_loop(step="knob (failing).step"):
    """A forced cut must read as a LOOP CUT in Blender, not a hard edge.

    Two properties matter: the cut must NOT be marked sharp (the surface is
    tangent across it -- it is a subdivision, not a feature), and its vertices
    must sit in quads so Alt+click can walk the loop.
    """
    import collections
    from stepusd.reader import StepDocument
    from stepusd import tessellate as T
    from dataclasses import replace
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.BRepTools import BRepTools

    doc = StepDocument(step)
    b = list(doc.protos.values())[0].bodies[0]
    base = T.MeshParams(deflection=0.125, angle_deg=30, min_size=0.2)
    fm = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(b, TopAbs_ShapeEnum.TopAbs_FACE, fm)
    # cut across a cylinder, where a clean quad grid is expected
    face = TopoDS.Face_s(fm.FindKey(2))
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    m0 = T.tessellate(b, base)
    m1 = T.tessellate(b, replace(base, iso_cuts=((2, "V", (v0 + v1) / 2),)))
    print(f"  sharp edges: {len(m0.sharp_edges)} without the cut, "
          f"{len(m1.sharp_edges)} with it")
    assert len(m1.sharp_edges) <= len(m0.sharp_edges) + 2, \
        "the cut was marked SHARP -- Blender would crease it"
    val = collections.Counter()
    quad = collections.Counter()
    for poly in m1.faces:
        for v in poly:
            val[v] += 1
            if len(poly) == 4:
                quad[v] += 1
    onquad = sum(1 for v, c in val.items() if quad[v] == c)
    print(f"  vertices whose faces are ALL quads: "
          f"{100 * onquad / max(len(val), 1):.0f}%  "
          f"(a loop needs quads to walk through)")
    print("  cut is smooth and quad-embedded: Blender reads it as a loop cut")


def check_ring(step="motor conic surface failing.step"):
    """A cut must WALK across tangent faces and stop at a hard edge."""
    from stepusd.reader import StepDocument
    from stepusd import tessellate as T
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools

    doc = StepDocument(step)
    b = list(doc.protos.values())[0].bodies[0]
    fm = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(b, TopAbs_ShapeEnum.TopAbs_FACE, fm)
    fi = 734                              # the big outer cylinder
    u0, u1, v0, v1 = BRepTools.UVBounds_s(TopoDS.Face_s(fm.FindKey(fi)))
    T.PROPAGATE_STATS.clear()
    chain = T.propagate_iso_cut(b, fi, "U", (u0 + u1) / 2)
    kinds = [str(BRepAdaptor_Surface(TopoDS.Face_s(fm.FindKey(f))).GetType())
             .split("_")[-1] for f, _, _ in chain]
    print(f"  chain: {len(chain)} stretches across {kinds}")
    assert len(chain) > 1, "the cut did not propagate at all"
    assert any("stopped" in k or "closed" in k for k in T.PROPAGATE_STATS),         "the walk ended without a reason"
    # and the cut body must still be sound
    from dataclasses import replace
    pr = T.MeshParams(deflection=0.1, angle_deg=30, min_size=0.15)
    m = T.tessellate(b, replace(pr, iso_cuts=tuple(chain)))
    print(f"  meshed with the ring: polys={len(m.faces):,} "
          f"cuts={m.iso_cuts_applied} open={m.boundary_edges} "
          f"nonman={m.nonmanifold_edges} watertight={m.watertight}")
    assert m.boundary_edges == 0 and m.nonmanifold_edges == 0,         "the ring cut damaged the mesh"
    print("  PASSED: the cut walks tangent faces and the mesh stays sound")


if __name__ == "__main__" and "--blender" in sys.argv:
    check_blender_loop()
if __name__ == "__main__" and "--ring" in sys.argv:
    check_ring()
