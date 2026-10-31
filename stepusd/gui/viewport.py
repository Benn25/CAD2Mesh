"""VTK viewport.

Two things matter here:

1. What you see is the EXACT triangle set that gets exported. No display-time
   smoothing, no recomputed normals -- the normals drawn are the ones written
   to USD.
2. Occurrences of the same prototype SHARE a vtkPolyDataMapper, so a 500-screw
   assembly holds one screw mesh in memory, mirroring the export.

Note on vertex duplication: VTK shades from per-POINT normals, while our meshes
carry per-face-CORNER (faceVarying) normals. So the *display* copy unwelds each
triangle corner to reproduce the exact shading Blender will show. The exported
mesh stays welded and watertight -- this duplication is display-only and never
reaches the writer.
"""
from __future__ import annotations
import os
import numpy as np

from vtkmodules.vtkCommonCore import vtkPoints, vtkIdTypeArray, vtkFloatArray
from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
from vtkmodules.vtkCommonMath import vtkMatrix4x4
from vtkmodules.vtkRenderingCore import (
    vtkActor, vtkPolyDataMapper, vtkRenderer, vtkLightKit)
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
from vtkmodules.util.numpy_support import numpy_to_vtk

from PySide6.QtWidgets import QWidget, QVBoxLayout
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
import vtkmodules.vtkRenderingOpenGL2   # noqa: F401  (registers the GL backend)
import vtkmodules.vtkRenderingFreeType   # noqa: F401

from ..scene import to_matrix16

#: colour for the current selection -- deliberately unlike anything the
#: palettes produce (they are muted; warnings are amber/red)
SELECTION_COLOR = (0.16, 0.86, 0.94)
#: The ACTIVE body -- the last one clicked -- in a multi-body selection, the way
#: Blender distinguishes it. Same hue, much lighter, so it reads as "one of the
#: selected, but the one in charge" rather than as a different state.
ACTIVE_COLOR = (0.80, 0.99, 1.00)


def mesh_to_polydata(mesh, cell_rgb=None) -> vtkPolyData:
    """Build the display copy: corners unwelded so faceVarying normals render
    exactly. See module docstring -- the exported mesh stays welded.

    Handles triangles AND quads: the ruled grids on cylinders/cones come out as
    quads, and VTK renders them directly, so the wireframe shows the real
    exported topology rather than a triangulated stand-in.
    """
    faces = mesh.faces
    nf = len(faces)
    pd = vtkPolyData()
    if nf == 0:
        return pd

    pts = np.asarray(mesh.points, dtype=np.float64)
    counts = np.fromiter((len(f) for f in faces), dtype=np.int64, count=nf)
    idx = np.fromiter((i for f in faces for i in f), dtype=np.int64,
                      count=int(counts.sum()))
    corners = pts[idx]
    nrm = np.asarray(mesh.normals, dtype=np.float32)

    vpts = vtkPoints()
    vpts.SetData(numpy_to_vtk(np.ascontiguousarray(corners), deep=True))
    pd.SetPoints(vpts)

    ncorner = int(counts.sum())
    conn = np.arange(ncorner, dtype=np.int64)
    offs = np.zeros(nf + 1, dtype=np.int64)
    np.cumsum(counts, out=offs[1:])
    cells = vtkCellArray()
    ca_off = vtkIdTypeArray(); ca_off.DeepCopy(numpy_to_vtk(offs, deep=True,
                                                            array_type=12))
    ca_con = vtkIdTypeArray(); ca_con.DeepCopy(numpy_to_vtk(conn, deep=True,
                                                            array_type=12))
    cells.SetData(ca_off, ca_con)
    pd.SetPolys(cells)

    if len(nrm) == ncorner:
        vn = numpy_to_vtk(np.ascontiguousarray(nrm), deep=True)
        vn.SetName("Normals")
        pd.GetPointData().SetNormals(vn)

    if cell_rgb is not None and len(cell_rgb) == nf:
        # One colour per face: a part with several materials keeps ONE welded
        # mesh and varies colour per face, it is never split.
        ca = numpy_to_vtk(np.ascontiguousarray(cell_rgb, dtype=np.uint8), deep=True)
        ca.SetName("FaceColor")
        pd.GetCellData().SetScalars(ca)
    return pd


class Viewport(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.interactor = QVTKRenderWindowInteractor(self)
        lay.addWidget(self.interactor)

        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.16, 0.17, 0.19)
        self.renderer.SetBackground2(0.09, 0.10, 0.11)
        self.renderer.GradientBackgroundOn()
        self.interactor.GetRenderWindow().AddRenderer(self.renderer)
        self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

        # Three-point lighting. A bare headlight leaves faces angled away from
        # the camera almost black -- with dark CAD colours (steel blue is only
        # 0.06,0.22,0.46) that reads as a modelling error when it isn't one.
        self.lights = vtkLightKit()
        # a brighter key and a strong fill, so the highlight sweeps across a
        # curved face instead of sitting in one spot
        try:
            self.lights.SetKeyLightIntensity(1.15)
            self.lights.SetKeyToFillRatio(2.2)
            self.lights.SetKeyToHeadRatio(2.8)
            self.lights.SetKeyToBackRatio(2.5)
        except Exception:
            pass
        self.lights.SetKeyToFillRatio(2.4)
        self.lights.SetKeyToHeadRatio(2.8)
        self.lights.SetKeyToBackRatio(3.2)
        self.lights.AddLightsToRenderer(self.renderer)

        self._axes = vtkAxesActor()
        self._marker = vtkOrientationMarkerWidget()
        self._marker.SetOrientationMarker(self._axes)
        self._marker.SetInteractor(self.interactor)
        self._marker.SetViewport(0.0, 0.0, 0.16, 0.22)

        self._mappers: dict = {}      # (proto_key, body_i) -> vtkPolyDataMapper
        self._actors: list = []       # (occurrence, body_index, actor)
        self._edges = False
        self._selected: set = set()
        self._active = None
        self._cycle_stack: list = []      # what is under the cursor, by depth
        self._cycle_pos = None
        self._cycle_i = 0
        # built here rather than in enable_picking: the depth-stack query is
        # useful (and testable) whether or not click-picking has been wired up
        from vtkmodules.vtkRenderingCore import vtkPropPicker, vtkCellPicker
        self._picker = vtkPropPicker()
        # A CELL picker as well: the loop-cut tool needs the polygon under the
        # cursor and the point on it, not just which body was hit.
        self._cellpicker = vtkCellPicker()
        self._cellpicker.SetTolerance(0.001)
        self._cell_cb = None
        self._hover_cb = None
        self._rmb_cb = None
        self._iso_actor = None
        self._cuts_actor = None
        self._hot_actor = None
        self._hot_ghost = None
        self._snap_actor = None
        self._ghost_actor = None
        self._overlay = None
        self._wheel_cb = None
        self._mod_cb = None
        self._mod_timer = None
        self._shift = False
        self._cursors: dict = {}
        self._cursor_kind = None
        self._hidden: set = set()     # {(occurrence.path, body_index)}
        self._dimmed = True
        self._pick_cb = None
        self._dbl_cb = None
        self._menu_cb = None
        self._middle_cb = None
        self._file_cb = None
        self._color_for = None
        self._press = None
        self._moved = False

    def watch_modifiers(self, callback):
        """Report Shift going down or up, even with no mouse movement.

        Polled rather than driven by key events on purpose: the interactor only
        receives keys once it has FOCUS, and the whole point here is to answer
        a modifier held while merely hovering, before anything has been
        clicked. `queryKeyboardModifiers` reads the physical state, so it is
        right whoever owns the focus.
        """
        from PySide6.QtCore import QTimer
        self._mod_cb = callback
        if callback is None:
            if self._mod_timer is not None:
                self._mod_timer.stop()
            self._shift = False
            return
        if self._mod_timer is None:
            self._mod_timer = QTimer(self)
            self._mod_timer.setInterval(60)
            self._mod_timer.timeout.connect(self._poll_modifiers)
        self._mod_timer.start()

    def _poll_modifiers(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt as _Qt
        held = bool(QApplication.queryKeyboardModifiers() & _Qt.ShiftModifier)
        if held != self._shift:
            self._shift = held
            if self._mod_cb is not None:
                self._mod_cb(held)

    def enable_file_drop(self, callback):
        """Accept a STEP file dragged in from the file manager."""
        self._file_cb = callback
        self.setAcceptDrops(True)
        self.interactor.setAcceptDrops(True)
        self.interactor.installEventFilter(self)

    def _handle_file_event(self, ev):
        """Returns True when the event was a STEP-file drag we consumed."""
        from PySide6.QtCore import QEvent, QTimer as _QTimer
        t = ev.type()
        if t in (QEvent.DragEnter, QEvent.DragMove):
            if self._step_path(ev):
                ev.acceptProposedAction()
                return True
        elif t == QEvent.Drop:
            path = self._step_path(ev)
            if path and self._file_cb is not None:
                ev.acceptProposedAction()
                # leave the drag handler before starting a long load
                _QTimer.singleShot(0, lambda p=path: self._file_cb(p))
                return True
        return False

    @staticmethod
    def _step_path(ev):
        md = getattr(ev, "mimeData", None)
        md = md() if callable(md) else None
        if md is None or not md.hasUrls():
            return None
        for u in md.urls():
            p = u.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in (".step", ".stp"):
                return p
        return None

    def start(self):
        self.interactor.Initialize()
        self._marker.EnabledOn()
        self._marker.InteractiveOff()

    # ---- scene construction -------------------------------------------
    def clear(self):
        self.renderer.RemoveAllViewProps()
        self._mappers.clear()
        self._actors.clear()

    def build(self, meshes_by_proto: dict, occurrences: list, color_for,
              face_rgb_for=None):
        """meshes_by_proto: {proto_key: [Mesh,...]}.
        color_for(occurrence, body_index) -> (r, g, b).
        face_rgb_for(proto_key, body_index) -> (ntri,3) uint8 or None."""
        self.clear()
        for key, meshes in meshes_by_proto.items():
            for i, m in enumerate(meshes):
                rgb = face_rgb_for(key, i) if face_rgb_for else None
                mp = vtkPolyDataMapper()
                mp.SetInputData(mesh_to_polydata(m, rgb))
                mp.SetScalarModeToUseCellData()
                mp.ScalarVisibilityOff()
                self._mappers[(key, i)] = mp

        for occ in occurrences:
            mat = vtkMatrix4x4()
            mat.DeepCopy(to_matrix16(occ.world))
            n_bodies = len(meshes_by_proto.get(occ.proto_key, []))
            for i in range(n_bodies):
                a = vtkActor()
                a.SetMapper(self._mappers[(occ.proto_key, i)])
                a.SetUserMatrix(mat)
                p = a.GetProperty()
                p.SetColor(*color_for(occ, i))
                self._color_for = color_for
                # Shiny, matcap-ish: a tight bright highlight rides over the
                # surface and makes a normal defect obvious -- a crease or a
                # sliver shows as a kink in the highlight long before it shows
                # in flat diffuse shading. That is the whole point of the
                # viewport, so it is worth the extra specular.
                p.SetInterpolationToPhong()
                p.SetAmbient(0.16); p.SetDiffuse(0.62)
                p.SetSpecular(0.85); p.SetSpecularPower(55)
                p.SetSpecularColor(1.0, 1.0, 1.0)
                p.SetEdgeColor(0.02, 0.02, 0.03)
                p.SetLineWidth(1.0)
                p.SetEdgeVisibility(self._edges)
                a.SetVisibility(0 if (occ.path, i) in self._hidden else 1)
                self.renderer.AddActor(a)
                self._actors.append((occ, i, a))

    def set_face_color_mode(self, enabled_for):
        """enabled_for(proto_key, body_index) -> bool. When True that body is
        drawn from its per-triangle colours instead of one flat actor colour."""
        for (key, bi), mp in self._mappers.items():
            mp.SetScalarVisibility(bool(enabled_for(key, bi)))
        self.render()

    def update_meshes(self, meshes_by_proto: dict, face_rgb_for=None):
        """Swap geometry on the existing mappers. Body counts don't change between
        tessellations, so actors stay valid -- this is what keeps slider drags
        responsive on assemblies with thousands of occurrences."""
        for key, meshes in meshes_by_proto.items():
            for i, m in enumerate(meshes):
                mp = self._mappers.get((key, i))
                if mp is not None:
                    rgb = face_rgb_for(key, i) if face_rgb_for else None
                    mp.SetInputData(mesh_to_polydata(m, rgb))
        self.render()

    # ---- display options ----------------------------------------------
    def set_edges(self, on: bool):
        self._edges = on
        for _, _, a in self._actors:
            a.GetProperty().SetEdgeVisibility(on)
        self.render()

    def set_colors(self, color_for):
        self._color_for = color_for
        self._apply_colors()

    def _apply_colors(self):
        if self._color_for is None:
            return
        for occ, bi, a in self._actors:
            p = a.GetProperty()
            if (occ.path, bi) in self._selected:
                if (occ.path, bi) == self._active:
                    p.SetColor(*ACTIVE_COLOR)
                    p.SetAmbient(0.50)
                else:
                    p.SetColor(*SELECTION_COLOR)
                    p.SetAmbient(0.38)
            else:
                p.SetColor(*self._color_for(occ, bi))
                p.SetAmbient(0.18)
        self.render()

    def highlight(self, keys: set, dim: bool = True, force: bool = False,
                  active=None):
        """keys: {(occurrence.path, body_index)}. Selected bodies take
        SELECTION_COLOR, the ACTIVE one a lighter shade; with `dim` the rest
        also fade back."""
        keys = set(keys)
        if (not force and keys == self._selected and dim == self._dimmed
                and active == self._active):
            return          # re-applying identical state is what made it flash
        self._selected = keys
        self._active = active
        self._dimmed = dim
        for occ, bi, a in self._actors:
            p = a.GetProperty()
            sel = (occ.path, bi) in self._selected
            if dim:
                p.SetOpacity(1.0 if (not self._selected or sel) else 0.12)
            else:
                p.SetOpacity(1.0)
        self._apply_colors()

    def frame(self, keys: set, fill: float = 0.7):
        """Move the camera so those bodies fill roughly `fill` of the view."""
        b = None
        for occ, bi, a in self._actors:
            if keys and (occ.path, bi) not in keys:
                continue
            ab = a.GetBounds()
            if ab[1] < ab[0]:
                continue
            b = list(ab) if b is None else [
                min(b[0], ab[0]), max(b[1], ab[1]),
                min(b[2], ab[2]), max(b[3], ab[3]),
                min(b[4], ab[4]), max(b[5], ab[5])]
        if b is None:
            return
        pad = max(1e-6, max(b[1] - b[0], b[3] - b[2], b[5] - b[4]) * 1e-3)
        for i in (0, 2, 4):
            b[i] -= pad
            b[i + 1] += pad
        self.renderer.ResetCamera(b)
        # ResetCamera fits the bounding SPHERE, so the box itself lands near 60%;
        # nudge in to land around the requested fraction.
        self.renderer.GetActiveCamera().Zoom(max(0.2, fill / 0.58))
        self.renderer.ResetCameraClippingRange()
        self.render()

    def set_hidden(self, hidden: set):
        """hidden: {(occurrence.path, body_index)} to switch off."""
        self._hidden = set(hidden)
        for occ, bi, a in self._actors:
            a.SetVisibility(0 if (occ.path, bi) in self._hidden else 1)
        self.render()

    DRAG_SLOP = 4      # pixels; beyond this a click counts as an orbit, not a pick

    def set_extra_mouse(self, menu_callback=None, middle_callback=None):
        """menu_callback(point) on a right CLICK, middle_callback() on a middle
        CLICK. Both ignore drags, which VTK uses for pan and zoom."""
        self._menu_cb = menu_callback
        self._middle_cb = middle_callback

    def enable_picking(self, callback, double_callback=None):
        """callback((path, body_index) or None) on left CLICK in the view; None
        means empty space, i.e. deselect.

        Filtered at the QT level, not through VTK observers: the trackball style
        calls GrabFocus() on button-down, after which VTK routes events only to
        the style and a LeftButtonReleaseEvent observer never fires at all.
        """
        self._pick_cb = callback
        self._dbl_cb = double_callback
        self.interactor.installEventFilter(self)

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent, Qt as _Qt
        if obj is self.interactor and self._file_cb is not None:
            if self._handle_file_event(ev):
                return True
        if obj is self.interactor and self._pick_cb is not None:
            t = ev.type()
            if t == QEvent.MouseButtonPress and ev.button() == _Qt.LeftButton:
                self._press = ev.position().toPoint()
                self._moved = False
            elif t == QEvent.MouseMove and self._press is not None:
                p = ev.position().toPoint()
                if (abs(p.x() - self._press.x()) > self.DRAG_SLOP
                        or abs(p.y() - self._press.y()) > self.DRAG_SLOP):
                    self._moved = True        # an orbit: keep the selection
            elif (t == QEvent.MouseMove and self._press is None
                    and self._hover_cb is not None):
                # only while the loop-cut tool is armed, and never mid-drag
                x, y = self._qt_to_vtk(ev.position().toPoint())
                got = self.pick_cell(x, y)
                self._hover_cb(got)
            elif t == QEvent.MouseButtonRelease and ev.button() == _Qt.LeftButton:
                if self._press is not None and not self._moved:
                    add = bool(ev.modifiers() & _Qt.ShiftModifier)
                    self._click(ev.position().toPoint(), False, add)
                self._press = None
            elif t == QEvent.MouseButtonDblClick and ev.button() == _Qt.LeftButton:
                self._click(ev.position().toPoint(), True)
                self._press = None
            elif (t == QEvent.Wheel and self._wheel_cb is not None
                    and (ev.modifiers() & _Qt.ControlModifier)):
                d = ev.angleDelta().y()
                if d:
                    self._wheel_cb(1 if d > 0 else -1,
                                   bool(ev.modifiers() & _Qt.ShiftModifier))
                return True                 # ours: do not also zoom
            elif t == QEvent.MouseButtonPress and ev.button() in (
                    _Qt.RightButton, _Qt.MiddleButton):
                self._press = ev.position().toPoint()
                self._moved = False
            elif t == QEvent.MouseButtonRelease and ev.button() == _Qt.RightButton:
                if self._press is not None and not self._moved:
                    if self._rmb_cb is not None:
                        # cut tool: remove a cut, or swap U/V
                        self._rmb_cb(bool(ev.modifiers() & _Qt.ShiftModifier))
                    elif self._menu_cb is not None:
                        self._menu_cb(ev.position().toPoint())
                self._press = None
            elif t == QEvent.MouseButtonRelease and ev.button() == _Qt.MiddleButton:
                if (self._press is not None and not self._moved
                        and self._middle_cb is not None):
                    self._middle_cb()
                self._press = None
        return False        # never consume: VTK must still get its drag

    def _qt_to_vtk(self, qp):
        dpr = self.interactor.devicePixelRatioF()
        w, h = self.interactor.GetRenderWindow().GetSize()
        x = int(round(qp.x() * dpr))
        y = int(round(h - 1 - qp.y() * dpr))     # Qt is y-down, VTK is y-up
        return max(0, min(w - 1, x)), max(0, min(h - 1, y))

    CYCLE_SLOP = 4          # px: clicking "in the same place" for cycling

    def show_iso_preview(self, points):
        """Draw a bright polyline where a loop cut would land.

        Benn: "I need a preview of where it will be cut, this is important to
        visualize before clicking!" -- so this follows the cursor rather than
        appearing after the fact. Drawn with depth testing OFF so it is visible
        against the surface it lies exactly on, which would otherwise z-fight.
        """
        from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor
        # accept either one polyline or a LIST of them, since a propagated cut
        # crosses several faces and each stretch is its own curve
        strips = points if points and isinstance(points[0], (list, tuple))             and points[0] and isinstance(points[0][0], (list, tuple)) else [points]
        strips = [t for t in strips if t and len(t) >= 2]
        if not strips:
            self.clear_iso_preview()
            return
        self._iso_actor = self._line_actor(self._iso_actor, strips,
                                           (1.0, 0.85, 0.1), 3.0)
        self._show_ghost(strips, (1.0, 0.85, 0.1))
        self.render()

    def _line_actor(self, existing, strips, rgb, width):
        """Build or update a polyline actor from a list of point strips."""
        from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor
        strips = [t for t in strips if t and len(t) >= 2]
        if not strips:
            if existing is not None:
                existing.SetVisibility(False)
            return existing
        pts = vtkPoints()
        lines = vtkCellArray()
        for strip in strips:
            base = pts.GetNumberOfPoints()
            for q in strip:
                pts.InsertNextPoint(*q)
            lines.InsertNextCell(len(strip))
            for i in range(len(strip)):
                lines.InsertCellPoint(base + i)
        pd = vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(lines)
        if existing is None:
            mp = vtkPolyDataMapper()
            existing = vtkActor()
            existing.SetMapper(mp)
            existing.SetPickable(False)
            mp.SetResolveCoincidentTopologyToPolygonOffset()
            pr = existing.GetProperty()
            pr.SetLighting(False)
            self.renderer.AddActor(existing)
        existing.GetMapper().SetInputData(pd)
        pr = existing.GetProperty()
        pr.SetColor(*rgb)
        pr.SetLineWidth(width)
        existing.SetVisibility(True)
        return existing

    def _overlay_renderer(self):
        """A renderer drawn AFTER the scene, sharing the camera.

        Layer 1 has no depth relationship with the model, so anything in it is
        visible through the part. That is what lets a preview line be followed
        round the far side, which is the only way to see that a cut really
        closes into a ring.
        """
        if self._overlay is None:
            from vtkmodules.vtkRenderingCore import vtkRenderer
            r = vtkRenderer()
            r.SetLayer(1)
            r.SetActiveCamera(self.renderer.GetActiveCamera())
            r.InteractiveOff()
            rw = self.interactor.GetRenderWindow()
            rw.SetNumberOfLayers(2)
            rw.AddRenderer(r)
            self._overlay = r
        return self._overlay

    def _show_ghost(self, strips, rgb):
        """The see-through half of a line: same curve, faint, drawn on top."""
        self._ghost_of(strips, rgb, "_ghost_actor", 2.0, 0.35)

    def _ghost_of(self, strips, rgb, attr, width, opacity):
        ren = self._overlay_renderer()
        before = self.renderer
        try:
            self.renderer = ren            # _line_actor adds to self.renderer
            setattr(self, attr, self._line_actor(getattr(self, attr), strips,
                                                 rgb, width))
        finally:
            self.renderer = before
        a = getattr(self, attr)
        if a is not None:
            a.GetProperty().SetOpacity(opacity)

    def clear_ghost(self):
        if self._ghost_actor is not None:
            self._ghost_actor.SetVisibility(False)
            self.render()

    def show_cut_lines(self, strips, hot=()):
        """Draw the cuts that already exist: `strips` red, `hot` glowing.

        The two are separate sets rather than a flag, because what glows is
        what a click would REMOVE -- which is a portion of a chain, not the
        whole of it. Benn: "when we press shift, just make glow the lines that
        will disappear, and not the full bunch."
        """
        self._cuts_actor = self._line_actor(self._cuts_actor, strips,
                                            (0.85, 0.15, 0.15), 2.5)
        self._hot_actor = self._line_actor(self._hot_actor, hot,
                                           (1.0, 0.45, 0.2), 5.0)
        # the doomed stretches are ghosted through the part as well, so a cut
        # that wraps round the far side can be judged before it is dropped
        self._ghost_of(hot, (1.0, 0.45, 0.2), "_hot_ghost", 3.0, 0.4)
        self.render()

    def clear_cut_lines(self):
        for a in (self._cuts_actor, self._hot_actor, self._hot_ghost):
            if a is not None:
                a.SetVisibility(False)
        self.render()

    def show_snap_marker(self, point, size):
        """A small 3D cross where the cut has snapped, CAD-style.

        Drawn in the overlay layer so it cannot be swallowed by the surface it
        sits exactly on."""
        if point is None:
            self.clear_snap_marker()
            return
        x, y, z = point
        h = size * 0.5
        strips = [[(x - h, y, z), (x + h, y, z)],
                  [(x, y - h, z), (x, y + h, z)],
                  [(x, y, z - h), (x, y, z + h)]]
        ren = self._overlay_renderer()
        before = self.renderer
        try:
            self.renderer = ren
            self._snap_actor = self._line_actor(self._snap_actor, strips,
                                                (0.3, 1.0, 0.5), 2.5)
        finally:
            self.renderer = before
        self.render()

    def clear_snap_marker(self):
        if self._snap_actor is not None:
            self._snap_actor.SetVisibility(False)
            self.render()

    # ------------------------------------------------------------------ cursor
    #: what a click would do, drawn onto the cursor itself
    CUT_CURSOR, DROP_CURSOR, SNAP_CURSOR = "cut", "drop", "snap"

    def set_action_cursor(self, kind, count=0):
        """Say what a click will do, on the cursor, where the eye already is.

        Benn: "maybe we should investigate a cursor change or a icon with the
        cursor to indicate the user what will happen when he clicks". The
        status bar carries the same information and is routinely missed -- it
        is at the other end of the window from the thing being pointed at.
        """
        key = (kind, count)
        if key == self._cursor_kind:
            return
        self._cursor_kind = key
        if kind is None:
            self.interactor.unsetCursor()
            return
        cur = self._cursors.get(key)
        if cur is None:
            cur = self._cursors[key] = self._make_cursor(kind, count)
        self.interactor.setCursor(cur)

    def _make_cursor(self, kind, count):
        from PySide6.QtGui import QCursor, QPixmap, QPainter, QPen, QColor, QFont
        from PySide6.QtCore import Qt as _Qt
        n = 32
        pm = QPixmap(n, n)
        pm.fill(_Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = {self.CUT_CURSOR: QColor(255, 217, 25),
             self.DROP_CURSOR: QColor(255, 115, 51),
             self.SNAP_CURSOR: QColor(77, 255, 128)}[kind]
        # a black under-stroke, or the glyph vanishes on a light part
        for pen, w in ((QColor(0, 0, 0, 190), 3.0), (c, 1.6)):
            p.setPen(QPen(pen, w, _Qt.SolidLine, _Qt.RoundCap))
            p.drawLine(11, 2, 11, 20)          # the crosshair
            p.drawLine(2, 11, 20, 11)
            if kind == self.DROP_CURSOR:       # an X: this click removes
                p.drawLine(19, 19, 29, 29)
                p.drawLine(29, 19, 19, 29)
            elif kind == self.SNAP_CURSOR:     # a box: locked onto a point
                p.drawRect(19, 19, 9, 9)
        if kind == self.DROP_CURSOR and count > 1:
            f = QFont()
            f.setPixelSize(11)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QPen(QColor(0, 0, 0, 200), 1))
            p.drawText(23, 15, str(min(count, 99)))
            p.setPen(QPen(c, 1))
            p.drawText(22, 14, str(min(count, 99)))
        p.end()
        return QCursor(pm, 11, 11)

    def clear_iso_preview(self):
        if self._iso_actor is not None:
            self._iso_actor.SetVisibility(False)
        if self._ghost_actor is not None:
            self._ghost_actor.SetVisibility(False)
        if self._iso_actor is not None or self._ghost_actor is not None:
            self.render()

    def pick_cell(self, x, y):
        """(body key, cell index, world point) under the cursor, or None.

        The cell index is the POLYGON index in that body's mesh, which is what
        maps to a B-rep face through `face_source`.
        """
        self._cellpicker.Pick(x, y, 0, self.renderer)
        prop = self._cellpicker.GetActor()
        cid = self._cellpicker.GetCellId()
        if prop is None or cid < 0:
            return None
        for occ, bi, a in self._actors:
            if a is prop:
                p = self._cellpicker.GetPickPosition()
                return (occ.path, bi), cid, (p[0], p[1], p[2])
        return None

    def _click(self, qp, double: bool, add: bool = False):
        x, y = self._qt_to_vtk(qp)
        if self._cell_cb is not None and not double:
            got = self.pick_cell(x, y)
            if got is not None:
                self._cell_cb(*got, shift=add)
            return
        if double:
            hit = self._pick_at(x, y)
            self._cycle_stack, self._cycle_pos = [], None
            if self._dbl_cb is not None:
                self._dbl_cb(hit)
            return
        # Blender's behaviour: clicking repeatedly in the same spot walks DOWN
        # through everything under the cursor in depth order, then wraps back to
        # the front. The stack is rebuilt whenever the cursor moves, so a click
        # somewhere else always selects the nearest part.
        same = (self._cycle_pos is not None
                and abs(x - self._cycle_pos[0]) <= self.CYCLE_SLOP
                and abs(y - self._cycle_pos[1]) <= self.CYCLE_SLOP)
        if not same:
            self._cycle_stack = self._pick_stack(x, y)
            self._cycle_i = 0
        else:
            self._cycle_i += 1
            if self._cycle_i >= len(self._cycle_stack):
                self._cycle_i = 0       # wrap to the front, as Blender does
        self._cycle_pos = (x, y)
        hit = (self._cycle_stack[self._cycle_i]
               if self._cycle_stack else None)
        self._pick_cb(hit, add)

    def _pick_stack(self, x, y, limit: int = 64):
        """Everything under (x, y), nearest first.

        vtkPropPicker only ever reports the front-most prop, so the stack is
        built by picking repeatedly and making each hit un-pickable in turn --
        the order that falls out IS depth order. Everything is restored
        afterwards, including on the way out of an exception.
        """
        out, toggled = [], []
        try:
            for _ in range(limit):
                self._picker.Pick(x, y, 0, self.renderer)
                prop = self._picker.GetViewProp()
                if prop is None:
                    break
                key = None
                for occ, bi, a in self._actors:
                    if a is prop:
                        key = (occ.path, bi)
                        break
                prop.PickableOff()
                toggled.append(prop)
                if key is None:
                    break
                out.append(key)
        finally:
            for prop in toggled:
                prop.PickableOn()
        return out

    def _pick_at(self, x, y):
        self._picker.Pick(x, y, 0, self.renderer)
        prop = self._picker.GetViewProp()
        if prop is None:
            return None
        for occ, bi, a in self._actors:
            if a is prop:
                return (occ.path, bi)
        return None

    def fit(self):
        self.renderer.ResetCamera()
        self.render()

    def set_view(self, name: str = "iso"):
        """CAD-style standard views. STEP is Z-up, so view-up is +Z, not VTK's
        default +Y -- otherwise the first thing you see is a flat plan view."""
        d = {"iso":   ((1.0, -1.0, 0.75), (0.0, 0.0, 1.0)),
             "front": ((0.0, -1.0, 0.0),  (0.0, 0.0, 1.0)),
             "right": ((1.0, 0.0, 0.0),   (0.0, 0.0, 1.0)),
             "top":   ((0.0, 0.0, 1.0),   (0.0, 1.0, 0.0))}[name]
        pos, up = d
        cam = self.renderer.GetActiveCamera()
        cam.SetFocalPoint(0.0, 0.0, 0.0)
        cam.SetPosition(*pos)
        cam.SetViewUp(*up)
        self.renderer.ResetCamera()
        self.render()

    def render(self):
        self.interactor.GetRenderWindow().Render()
