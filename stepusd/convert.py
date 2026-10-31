"""STEP -> USD conversion pipeline."""
from __future__ import annotations
import colorsys
from .reader import StepDocument
from .tessellate import tessellate, MeshParams
from .writer import write_usd


def instance_palette(doc) -> dict:
    """Distinct hue per prototype, so repeated parts share a colour on screen."""
    out = {}
    for i, k in enumerate(doc.protos):
        h = (i * 0.618033988749895) % 1.0          # golden-ratio hue spacing
        out[k] = colorsys.hsv_to_rgb(h, 0.55, 0.95)
    return out


def convert(step_path: str, usd_path: str, params: MeshParams,
            color_mode: str = "cad", progress=None):
    doc = StepDocument(step_path)
    n = len(doc.protos)
    for i, proto in enumerate(doc.protos.values(), 1):
        if progress:
            progress(i, n, proto.name)
        proto.meshes = [tessellate(b, params) for b in proto.bodies]
    colors = instance_palette(doc) if color_mode == "instance" else None
    write_usd(doc, usd_path, instance_colors=colors)
    return doc
