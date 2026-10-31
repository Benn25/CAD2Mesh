"""Colour schemes for the viewport.

Deliberately muted: mid-saturation, mid-lightness, hues spaced by the golden
ratio so neighbouring groups never collide. No fully saturated primaries.
"""
from __future__ import annotations
import colorsys

GOLDEN = 0.618033988749895
SHADES = 12        # distinct lightness steps within one component

# problems, so they read at a glance without being alarming
OPEN_SHELL_COLOR = (0.90, 0.58, 0.22)     # amber: never was a closed solid
NONMANIFOLD_COLOR = (0.85, 0.31, 0.31)    # red: closed but >2 faces on an edge
DEFAULT_COLOR = (0.72, 0.74, 0.77)        # neutral grey when nothing is known


def _hue(i: int, offset: float = 0.08) -> float:
    return (offset + i * GOLDEN) % 1.0


def component_body_colors(occurrences, body_counts: dict) -> dict:
    """One hue per component, shades of it per body inside that component.

    Every body under the same component shares a hue, and its lightness walks a
    narrow ramp so individual bodies stay distinguishable without the group
    losing its identity. `body_counts` maps proto_key -> number of bodies.

    Returns {(occurrence.path, body_index): (r, g, b)}.
    """
    groups: dict = {}
    for occ in occurrences:
        parent = occ.path.rsplit("/", 1)[0] if "/" in occ.path else occ.path
        groups.setdefault(parent, []).append(occ)

    out: dict = {}
    for gi, parent in enumerate(sorted(groups)):
        h = _hue(gi)
        members = []
        for occ in groups[parent]:
            for bi in range(body_counts.get(occ.proto_key, 1)):
                members.append((occ.path, bi))
        # A dozen shades is plenty to tell neighbouring bodies apart; a unique
        # shade per body just makes the steps invisible on a 164-body part.
        steps = min(SHADES, max(len(members), 1))
        for k, key in enumerate(members):
            t = (k % steps) / max(steps - 1, 1)
            light = 0.28 + 0.62 * t
            sat = 0.34 if len(members) > 1 else 0.30
            out[key] = colorsys.hls_to_rgb(h, light, sat)
    return out


def prototype_colors(protos) -> dict:
    """One colour per unique part, so repeated hardware shares a colour."""
    out = {}
    for i, key in enumerate(protos):
        out[key] = colorsys.hls_to_rgb(_hue(i), 0.56, 0.36)
    return out


#: The surface kinds shown in the "By surface type" legend, in legend order.
#: These are DISPLAY names, not OCCT's: Benn asked for "free surface" for the
#: ones that are just bent 3D NURBS, because that is the distinction that
#: matters to him -- analytic (exact, predictable) against free-form (whatever
#: the CAD system fitted). B-spline and Bezier are the same thing to a user, so
#: they share one entry.
SURFACE_TYPE_COLORS = {
    "Plane":         (0.62, 0.64, 0.67),
    "Cylinder":      (0.25, 0.55, 0.95),
    "Cone":          (0.15, 0.78, 0.72),
    "Sphere":        (0.55, 0.40, 0.90),
    "Torus":         (0.20, 0.75, 0.35),
    "Free surface":  (0.95, 0.55, 0.15),
    "Revolved":      (0.90, 0.35, 0.35),
    "Extruded":      (0.85, 0.45, 0.65),
    "Offset":        (0.60, 0.50, 0.35),
    "Other":         (0.45, 0.45, 0.48),
}
UNKNOWN_SURFACE_COLOR = (0.35, 0.35, 0.38)

#: OCCT's GeomAbs names -> the label above.
SURFACE_TYPE_LABEL = {
    "Plane": "Plane",
    "Cylinder": "Cylinder",
    "Cone": "Cone",
    "Sphere": "Sphere",
    "Torus": "Torus",
    "BSplineSurface": "Free surface",
    "BezierSurface": "Free surface",
    "SurfaceOfRevolution": "Revolved",
    "SurfaceOfExtrusion": "Extruded",
    "OffsetSurface": "Offset",
    "OtherSurface": "Other",
}


def surface_type_label(kind: str) -> str:
    return SURFACE_TYPE_LABEL.get(kind, "Other")


def surface_type_color(kind: str):
    """Colour for an OCCT surface-type name (or a display label)."""
    if kind in SURFACE_TYPE_COLORS:
        return SURFACE_TYPE_COLORS[kind]
    return SURFACE_TYPE_COLORS.get(surface_type_label(kind),
                                   UNKNOWN_SURFACE_COLOR)
