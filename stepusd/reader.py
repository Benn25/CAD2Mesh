"""Read a STEP file via OCCT's XDE layer, preserving assembly hierarchy,
instancing (prototype + transform references), names and colors."""
from __future__ import annotations
from dataclasses import dataclass, field

from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_LabelSequence, TDF_Label, TDF_Tool
from OCP.Quantity import Quantity_Color
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
import re

#: STEP entity names OCCT hands back for assembly occurrences, e.g. "NAUO223".
#: They are not names, they are entity ids.
_AUTO_NAME = re.compile(r"^(NAUO|PRODUCT|SHAPE|SR|PD)[-_ ]?\d+$", re.I)


def _text_score(t: str) -> int:
    """Plausibility of a decoded name. Japanese CAD names are kana/kanji/ASCII;
    Cyrillic or Hangul means we picked the wrong code page."""
    good = bad = 0
    for c in t:
        o = ord(c)
        if c.isascii():
            good += 0 if o < 0x20 else 1
        elif 0x3040 <= o <= 0x30FF:
            good += 3                       # kana: the strongest signal
        elif 0x4E00 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF:
            good += 2                       # kanji / fullwidth
        elif 0x0400 <= o <= 0x04FF or 0xAC00 <= o <= 0xD7AF or o == 0xFFFD:
            bad += 3                        # cyrillic / hangul / replacement
        else:
            bad += 1
    return good - 3 * bad


def fix_mojibake(text):
    """Recover names that were written as raw code-page bytes.

    SolidWorks writes Japanese STEP names as raw Shift-JIS bytes rather than the
    ISO-10303 escapes. OCCT widens most of those bytes straight to characters
    but decodes any run that happens to be valid UTF-8, so what reaches us is a
    MIX: mostly U+0080..U+00FF with the occasional real code point spliced in.

    Only strings containing that raw-byte range are touched -- a name that is
    already proper Japanese has no such characters and is returned untouched,
    which is what stops this from mangling correct input. Code pages are tried
    in priority order and the first clean, plausible decode wins; comparing
    scores across code pages does not work, because gb18030 decodes almost any
    byte string into confident-looking nonsense.
    """
    if not text or not any(0x80 <= ord(c) <= 0xFF for c in text):
        return text
    raw = bytearray()
    for ch in text:
        o = ord(ch)
        if o < 0x100:
            raw.append(o)
        else:
            raw.extend(ch.encode("utf-8", "replace"))
    raw = bytes(raw)
    for enc in ("cp932", "utf-8", "cp949", "gb18030"):
        try:
            cand = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "�" in cand:
            continue
        if _text_score(cand) > 0:
            return cand
    return text


@dataclass
class Proto:
    """A unique part definition. Tessellated ONCE regardless of occurrences."""
    key: str
    name: str
    shape: object
    color: tuple | None = None
    bodies: list = field(default_factory=list)   # TopoDS_Solid/Shell per body
    solid_bodies: int = 0
    open_bodies: int = 0                          # not closed solids -> flagged
    body_face_colors: list = field(default_factory=list)  # per body: [rgb|None] per face
    body_colors: list = field(default_factory=list)       # per body: dominant rgb|None
    body_names: list = field(default_factory=list)        # per body: user override|None
    occurrences: int = 0
    meshes: list = field(default_factory=list)    # filled by the converter


@dataclass
class Node:
    name: str
    matrix: object                    # gp_Trsf, relative to parent
    proto: Proto | None = None        # leaf part
    children: list = field(default_factory=list)

    @property
    def is_assembly(self): return self.proto is None


class StepDocument:
    def __init__(self, path: str):
        self.path = path
        self.protos: dict[str, Proto] = {}
        self.roots: list[Node] = []
        self._read()

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _entry(label) -> str:
        s = TCollection_AsciiString(); TDF_Tool.Entry_s(label, s); return s.ToCString()

    @staticmethod
    def _name(label) -> str | None:
        n = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), n):
            v = fix_mojibake(n.Get().ToExtString())
            # OCCT names component labels "=>[0:1:1:2]" -- not a real name
            if v and not v.startswith("=>") and not _AUTO_NAME.match(v):
                return v
        return None

    def _shape_color(self, sh):
        """Colour attached to any shape (a face, a solid, ...), or None."""
        if sh is None:
            return None
        c = Quantity_Color()
        for t in (XCAFDoc_ColorType.XCAFDoc_ColorSurf,
                  XCAFDoc_ColorType.XCAFDoc_ColorGen,
                  XCAFDoc_ColorType.XCAFDoc_ColorCurv):
            try:
                if self._ct.GetColor(sh, t, c):
                    return (c.Red(), c.Green(), c.Blue())
            except Exception:
                return None
        return None

    def face_colors(self, body) -> list:
        """Colour per face, indexed exactly as tessellate() indexes faces
        (TopExp.MapShapes order), so triangle -> face -> colour lines up.

        A single part legitimately carries several materials on different faces;
        they must end up as material SLOTS on one welded mesh, never as separate
        meshes.
        """
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(body, TopAbs_ShapeEnum.TopAbs_FACE, m)
        return [self._shape_color(m.FindKey(i)) for i in range(1, m.Extent() + 1)]

    def _color(self, label):
        c = Quantity_Color()
        try: sh = self._st.GetShape_s(label)
        except Exception: return None
        if sh is None: return None
        for t in (XCAFDoc_ColorType.XCAFDoc_ColorSurf,
                  XCAFDoc_ColorType.XCAFDoc_ColorGen,
                  XCAFDoc_ColorType.XCAFDoc_ColorCurv):
            if self._ct.GetColor(sh, t, c):
                return (c.Red(), c.Green(), c.Blue())
        return None

    @staticmethod
    def _bodies(shape):
        """Split a part shape into bodies.

        Prefer real solids. But SolidWorks (and others) often export a solid
        model as a SHELL_BASED_SURFACE_MODEL: the shells are genuinely closed,
        they are just never wrapped in MANIFOLD_SOLID_BREP, so OCCT hands us
        TopoDS_Shell. Treating those as 'not solid' would wrongly disclaim the
        watertight guarantee on most real-world files, so promote every CLOSED
        shell to a real solid and flag only the ones that are actually open.
        """
        solids, shells = [], []
        e = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
        while e.More():
            solids.append(TopoDS.Solid_s(e.Current())); e.Next()
        if solids:
            return solids, len(solids), 0

        e = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
        while e.More():
            shells.append(TopoDS.Shell_s(e.Current())); e.Next()
        if not shells:
            return [shape], 0, 1

        out, n_solid, n_open = [], 0, 0
        for sh in shells:
            if sh.Closed():
                mk = BRepBuilderAPI_MakeSolid(sh)
                if mk.IsDone():
                    out.append(mk.Solid()); n_solid += 1
                    continue
            out.append(sh); n_open += 1        # genuinely open -> cannot be sealed
        return out, n_solid, n_open

    # ---- reading -------------------------------------------------------
    def _read(self):
        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
        app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)

        r = STEPCAFControl_Reader()
        r.SetNameMode(True); r.SetColorMode(True); r.SetLayerMode(True)
        status = r.ReadFile(self.path)
        if status != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise RuntimeError(f"could not read STEP file: {self.path} (status {status})")
        if not r.Transfer(doc):
            raise RuntimeError(f"STEP transfer failed: {self.path}")

        self._doc = doc
        self._st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        self._ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

        free = TDF_LabelSequence(); self._st.GetFreeShapes(free)
        for i in range(1, free.Length() + 1):
            self.roots.append(self._walk(free.Value(i), f"Root_{i}"))

    def _walk(self, label, fallback: str) -> Node:
        st = self._st
        target, trsf = label, None
        if st.IsReference_s(label):
            ref = TDF_Label(); st.GetReferredShape_s(label, ref)
            trsf = st.GetLocation_s(label).Transformation()
            target = ref
        # Prefer the referenced product's name. The occurrence label carries the
        # STEP entity name, which for SolidWorks exports is "NAUO223" and tells
        # the user nothing.
        name = self._name(target) or self._name(label) or fallback

        if trsf is None:
            from OCP.gp import gp_Trsf
            trsf = gp_Trsf()

        if st.IsAssembly_s(target):
            node = Node(name=name, matrix=trsf)
            seq = TDF_LabelSequence(); st.GetComponents_s(target, seq)
            for i in range(1, seq.Length() + 1):
                node.children.append(self._walk(seq.Value(i), f"Part_{i}"))
            return node

        key = self._entry(target)
        proto = self.protos.get(key)
        if proto is None:
            shape = st.GetShape_s(target)
            bodies, nsolid, nopen = self._bodies(shape)
            proto = Proto(key=key, name=self._name(target) or name, shape=shape,
                          color=self._color(target), bodies=bodies,
                          solid_bodies=nsolid, open_bodies=nopen)
            proto.body_face_colors = [self.face_colors(b) for b in bodies]
            proto.body_colors = [_dominant(fc) for fc in proto.body_face_colors]
            self.protos[key] = proto
        proto.occurrences += 1
        return Node(name=name, matrix=trsf, proto=proto)

    # ---- summary -------------------------------------------------------
    def stats(self):
        occ = sum(p.occurrences for p in self.protos.values())
        return dict(prototypes=len(self.protos), occurrences=occ,
                    instanced=occ - len(self.protos),
                    open_bodies=sum(p.open_bodies for p in self.protos.values()))


def _dominant(face_colors):
    """Most common non-null face colour, used when one flat colour is wanted."""
    counts = {}
    for c in face_colors:
        if c:
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]
