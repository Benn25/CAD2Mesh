"""Step through a body's tessellation face by face.

A debug view, and it exists because the mesher is otherwise a black box: it
reports counters at the end and nothing about how it got there. This reveals
the finished mesh one B-rep face at a time and says, for each, which PATH the
mesher chose for it -- the ruled grid, the iso-line patch, the sphere cap, or
plain OCCT -- which is the thing that actually explains what a face looks like.
"""
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView)

from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.BRepAdaptor import BRepAdaptor_Surface

from .. import tessellate as tess


def _submesh(m, upto_face):
    """A copy of `m` holding only the polygons whose B-rep face index <= upto.

    Points are kept whole -- unused ones cost nothing and keeping the indexing
    means no remapping can go wrong. Normals are faceVarying, so they have to be
    sliced by CORNER, not by polygon.
    """
    out = tess.Mesh()
    out.points = m.points
    starts = []
    n = 0
    for f in m.faces:
        starts.append(n)
        n += len(f)
    for i, (f, src) in enumerate(zip(m.faces, m.face_source)):
        if src > upto_face:
            continue
        out.faces.append(f)
        out.face_source.append(src)
        out.normals.extend(m.normals[starts[i]:starts[i] + len(f)])
    out.n_quads = sum(1 for f in out.faces if len(f) == 4)
    return out


class StepDialog(QDialog):
    def __init__(self, parent, body, params, label, apply_mesh):
        super().__init__(parent)
        self.setWindowTitle(f"Step through - {label}")
        self.resize(760, 520)
        self._apply = apply_mesh

        tess.LAST_FACE_PATHS.clear()
        self.mesh = tess.tessellate(body, params)
        paths = dict(tess.LAST_FACE_PATHS)

        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, faces)
        det = replace(params, fillet_first=True)

        per = {}
        for f, src in zip(self.mesh.faces, self.mesh.face_source):
            d = per.setdefault(src, [0, 0])
            d[0] += 1
            if len(f) == 4:
                d[1] += 1

        self.rows = []
        # face indices are 1-BASED here: _tessellate_once walks
        # `range(1, faces.Extent() + 1)` and writes that into face_source and
        # LAST_FACE_PATHS. Using 0-based shifted every path by one face, which
        # showed up as a PLANE apparently taking the ruled-grid path.
        for fi in range(1, faces.Extent() + 1):
            face = TopoDS.Face_s(faces.FindKey(fi))
            st = str(BRepAdaptor_Surface(face).GetType()).split("_")[-1]
            polys, quads = per.get(fi, [0, 0])
            fil = ""
            got = tess._fillet_of(face)
            if got is not None:
                r, along, across = got
                if tess.is_priority_fillet(face, det):
                    fil = f"fillet r={r:.2f} x{along / max(across, 1e-9):.0f}"
                else:
                    fil = f"r={r:.2f}"
            self.rows.append((fi, st, paths.get(fi, "plain OCCT"),
                              polys, quads, fil))

        lay = QVBoxLayout(self)
        self.head = QLabel("")
        self.head.setStyleSheet("font-weight:bold")
        lay.addWidget(self.head)

        self.table = QTableWidget(len(self.rows), 6)
        self.table.setHorizontalHeaderLabels(
            ["face", "surface", "path taken", "polys", "quads", "blend"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for r, (fi, st, path, polys, quads, fil) in enumerate(self.rows):
            for c, v in enumerate((str(fi), st, path, f"{polys:,}",
                                   f"{quads:,}", fil)):
                self.table.setItem(r, c, QTableWidgetItem(v))
        h = self.table.horizontalHeader()
        h.setStretchLastSection(False)
        for c in range(5):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(
            lambda r, *_: self._goto(r) if r >= 0 else None)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn in (("|< first", lambda: self._goto(0)),
                         ("< prev", lambda: self._step(-1)),
                         ("next >", lambda: self._step(1)),
                         ("+10", lambda: self._step(10)),
                         ("show all >|",
                          lambda: self._goto(len(self.rows) - 1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

        self.i = 0
        self._goto(0)

    def _step(self, d):
        self._goto(max(0, min(len(self.rows) - 1, self.i + d)))

    def _goto(self, i):
        if not self.rows:
            return
        self.i = max(0, min(len(self.rows) - 1, i))
        fi, st, path, polys, quads, fil = self.rows[self.i]
        shown = sum(r[3] for r in self.rows[:self.i + 1])
        self.head.setText(
            f"face {fi} of {len(self.rows)}   {st}   -> {path}   "
            f"({polys:,} polys, {quads:,} quads{', ' + fil if fil else ''})   "
            f"|   {shown:,} of {len(self.mesh.faces):,} polygons drawn")
        if self.table.currentRow() != self.self_row():
            self.table.blockSignals(True)
            self.table.selectRow(self.i)
            self.table.blockSignals(False)
        self._apply(_submesh(self.mesh, fi))

    def self_row(self):
        return self.i

    def done(self, r):
        self._apply(self.mesh)          # put the whole body back
        super().done(r)
