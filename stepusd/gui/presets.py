"""User-editable tessellation quality presets.

A preset is an ABSOLUTE deflection and angle, not a multiplier: "Fine" meaning
0.05 mm / 8 deg is something you can reason about, whereas "0.35x the global
setting" is not. Presets are saved next to the project so they persist between
sessions.
"""
from __future__ import annotations
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QMessageBox, QColorDialog)

DEFAULTS = [
    {"name": "Draft", "grid": True, "iso": False, "deflection": 1.0, "angle": 30.0, "quads": True,
     "color": [0.78, 0.45, 0.36]},
    {"name": "Standard", "grid": True, "iso": False, "deflection": 0.2, "angle": 20.0, "quads": True,
     "color": [0.45, 0.60, 0.75]},
    {"name": "Fine", "grid": True, "iso": False, "deflection": 0.05, "angle": 10.0, "quads": True,
     "color": [0.47, 0.70, 0.52]},
    {"name": "Ultra", "grid": True, "iso": False, "deflection": 0.015, "angle": 5.0, "quads": True,
     "color": [0.66, 0.53, 0.76]},
]

#: bodies with no preset, in "by tessellation quality" colouring
NO_PRESET_COLOR = (0.62, 0.64, 0.67)


def _path() -> str:
    """Where presets are SAVED -- the user's own copy on a shared deployment."""
    from .. import paths
    return paths.settings_path("quality_presets.json")


def _shipped() -> str:
    """The copy that came with the app, used as the starting point."""
    from .. import paths
    return os.path.join(paths.app_dir(), "quality_presets.json")


def load() -> list:
    # the user's own file first, then whatever shipped with the app
    for src in (_path(), _shipped()):
        got = _read(src)
        if got:
            return got
    return [dict(d) for d in DEFAULTS]


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for d in data:
            if {"name", "deflection", "angle"} <= set(d):
                out.append({"name": str(d["name"]),
                            "deflection": float(d["deflection"]),
                            "angle": float(d["angle"]),
                            "quads": bool(d.get("quads", True)),
                            "grid": bool(d.get("grid", True)),
                            "iso": bool(d.get("iso", False)),
                            "color": [float(c) for c in d.get(
                                "color", [0.6, 0.6, 0.6])][:3]})
        return out or None
    except Exception:
        return None


def save(presets) -> None:
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


class PresetDialog(QDialog):
    """Edit the preset list: name, max surface deviation, max normal deviation."""

    def __init__(self, presets, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tessellation quality presets")
        self.resize(640, 340)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Assign these to parts from the right-click menu.\n"
            "Anything without a preset uses the global sliders."))

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Max surface deviation (mm)", "Max normal deviation (°)",
             "Quads", "Axis grid", "Iso lines", "Colour"])
        self.table.cellDoubleClicked.connect(self._pick_color)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        for p in presets:
            self._add_row(p)

        row = QHBoxLayout()
        b_add = QPushButton("Add")
        b_add.clicked.connect(
            lambda: self._add_row({"name": "New", "deflection": 0.1,
                                   "angle": 15.0, "quads": True,
                                   "grid": True, "iso": False,
                                   "color": [0.6, 0.6, 0.6]}))
        b_del = QPushButton("Remove")
        b_del.clicked.connect(self._remove_row)
        b_reset = QPushButton("Restore defaults")
        b_reset.clicked.connect(self._restore)
        row.addWidget(b_add)
        row.addWidget(b_del)
        row.addWidget(b_reset)
        row.addStretch(1)
        b_ok = QPushButton("OK")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self._accept)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        row.addWidget(b_ok)
        row.addWidget(b_cancel)
        lay.addLayout(row)

    def _add_row(self, p):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(p["name"])))
        self.table.setItem(r, 1, QTableWidgetItem(f"{p['deflection']:g}"))
        self.table.setItem(r, 2, QTableWidgetItem(f"{p['angle']:g}"))
        q = QTableWidgetItem()
        q.setFlags((q.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        q.setCheckState(Qt.Checked if p.get("quads", True) else Qt.Unchecked)
        self.table.setItem(r, 3, q)
        g = QTableWidgetItem()
        g.setFlags((g.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        g.setCheckState(Qt.Checked if p.get("grid", True) else Qt.Unchecked)
        g.setToolTip("Rebuild cylinders and cones as an axis-aligned grid")
        self.table.setItem(r, 4, g)
        iso = QTableWidgetItem()
        iso.setFlags((iso.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        iso.setCheckState(Qt.Checked if p.get("iso", True) else Qt.Unchecked)
        iso.setToolTip("Re-mesh TRIMMED cylinders and cones so their edges "
                       "follow the iso-lines")
        self.table.setItem(r, 5, iso)
        c = QTableWidgetItem("double-click")
        c.setFlags(c.flags() & ~Qt.ItemIsEditable)
        rgb = p.get("color", [0.6, 0.6, 0.6])
        c.setData(Qt.UserRole, list(rgb))
        c.setBackground(QBrush(QColor.fromRgbF(*rgb)))
        self.table.setItem(r, 6, c)

    def _pick_color(self, row, col):
        """Colour is what the "by tessellation quality" view draws with."""
        if col != 6:
            return
        it = self.table.item(row, 6)
        cur = it.data(Qt.UserRole) or [0.6, 0.6, 0.6]
        col_ = QColorDialog.getColor(QColor.fromRgbF(*cur), self,
                                     "Colour for this preset")
        if not col_.isValid():
            return
        rgb = [col_.redF(), col_.greenF(), col_.blueF()]
        it.setData(Qt.UserRole, rgb)
        it.setBackground(QBrush(col_))

    def _remove_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _restore(self):
        self.table.setRowCount(0)
        for p in DEFAULTS:
            self._add_row(p)

    def _accept(self):
        try:
            self._result = self.values()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid preset", str(e))
            return
        self.accept()

    def values(self) -> list:
        out, seen = [], set()
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            if not name:
                raise ValueError(f"row {r + 1}: the name cannot be empty")
            if name in seen:
                raise ValueError(f"'{name}' is used twice")
            seen.add(name)
            try:
                d = float(self.table.item(r, 1).text())
                a = float(self.table.item(r, 2).text())
            except (AttributeError, ValueError):
                raise ValueError(f"'{name}': deviation and angle must be numbers")
            if d <= 0 or not (0.1 <= a <= 60.0):
                raise ValueError(
                    f"'{name}': deviation must be > 0 and angle between 0.1 and 60")
            qi = self.table.item(r, 3)
            gi = self.table.item(r, 4)
            ii = self.table.item(r, 5)
            ci = self.table.item(r, 6)
            out.append({"name": name, "deflection": d, "angle": a,
                        "quads": qi is None or qi.checkState() == Qt.Checked,
                        "grid": gi is None or gi.checkState() == Qt.Checked,
                        "iso": ii is not None and ii.checkState() == Qt.Checked,
                        "color": (ci.data(Qt.UserRole) if ci else None)
                                 or [0.6, 0.6, 0.6]})
        if not out:
            raise ValueError("keep at least one preset")
        return out
