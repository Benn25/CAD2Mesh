"""What colour data does a STEP file actually contain?

Benn: "what is the data used for 'material colors'? because everything has
always the same color... What I need for the color here, is the color that will
be used in blender as the BSDF color when you export a fbx from fusion 360."

Answering that needs two questions kept apart, because the fix is different:

  1. **Is there any colour in the file at all?** STEP carries it as
     `STYLED_ITEM` -> `PRESENTATION_STYLE_ASSIGNMENT` -> ... -> `COLOUR_RGB`
     (or a `DRAUGHTING_PRE_DEFINED_COLOUR` by name). Those are plain text in
     the file, so they can be counted without OCCT and without any trust in
     our own reader.
  2. **Does OUR reader find it?** XDE exposes it through `XCAFDoc_ColorTool`,
     which we query for all three types (Surf, Gen, Curv) at both the shape and
     the face level.

If (1) is zero the file has no colour and no reader can invent one -- the
export settings that produced it are the thing to change. If (1) is non-zero
and (2) is zero, that is our bug.

Run:  .venv/Scripts/python.exe spikes/color_probe.py <file.step> [...]
      .venv/Scripts/python.exe spikes/color_probe.py          # every STEP here
"""
import collections
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: the entity types that can carry a colour in an AP203/214/242 part file
ENTITIES = ("COLOUR_RGB", "DRAUGHTING_PRE_DEFINED_COLOUR", "STYLED_ITEM",
            "PRESENTATION_STYLE_ASSIGNMENT", "SURFACE_STYLE_FILL_AREA",
            "FILL_AREA_STYLE_COLOUR", "MECHANICAL_DESIGN_GEOMETRIC_"
            "PRESENTATION_REPRESENTATION")


def raw_scan(path):
    """Count colour entities in the TEXT, so it does not depend on our reader.

    Scanned as ONE string, not line by line: a STEP entity may be wrapped
    across lines, and reading `knob (failing).step` a line at a time found only
    the first of its two colours -- which made the file look as though it held
    a single grey when the part is actually painted SolidWorks blue.
    """
    txt = open(path, "r", encoding="utf-8", errors="replace").read()
    up = txt.upper()
    counts = collections.Counter()
    for e in ENTITIES:
        n = up.count(e)
        if n:
            counts[e] = n
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", txt)
    schema = m.group(1) if m else ""
    rgb = []
    for m in re.finditer(
            r"COLOUR_RGB\s*\(\s*'([^']*)'\s*,\s*"
            r"([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)",
            txt, re.I | re.S):
        try:
            rgb.append((m.group(1),
                        tuple(round(float(g), 4) for g in m.groups()[1:])))
        except ValueError:
            pass
    return schema, counts, rgb


def via_xde(path):
    """What OUR reader gets: colours per prototype and per FACE."""
    from stepusd.reader import StepDocument
    doc = StepDocument(path)
    proto_colors = collections.Counter()
    face_colors = collections.Counter()
    bodies = 0
    for proto in doc.protos.values():
        proto_colors[proto.color] += 1
        for fc in getattr(proto, "body_face_colors", []) or []:
            bodies += 1
            for c in fc:
                face_colors[c] += 1
    return doc, proto_colors, face_colors, bodies


def report(path):
    print(f"\n=== {os.path.basename(path)}")
    schema, counts, rgb = raw_scan(path)
    print(f"  schema            : {schema or '(not found)'}")
    total = sum(counts.values())
    if not total:
        print("  colour entities   : NONE -- the file carries no colour at all")
    else:
        for e in ENTITIES:
            if counts[e]:
                print(f"  {e[:34]:<34}: {counts[e]}")
    if rgb:
        uniq = sorted(set(rgb))
        print(f"  distinct COLOUR_RGB values: {len(uniq)}")
        for name, c in uniq[:10]:
            hexv = "".join("%02X" % round(max(0, min(1, v)) * 255) for v in c)
            # STEP stores sRGB, which is what a CAD colour picker shows
            print(f"      #{hexv}  {c}  '{name}'")
    try:
        doc, pc, fc, bodies = via_xde(path)
    except Exception as e:
        print(f"  our reader        : FAILED {type(e).__name__}: {e}")
        return
    named = {k: v for k, v in pc.items() if k is not None}
    print(f"  our reader        : {len(doc.protos)} prototypes, "
          f"{bodies} bodies")
    print(f"     prototypes with a colour : {sum(named.values())} "
          f"of {sum(pc.values())}")
    fnamed = {k: v for k, v in fc.items() if k is not None}
    print(f"     faces with a colour      : {sum(fnamed.values())} "
          f"of {sum(fc.values())}")
    if named:
        for c, n in list(named.items())[:6]:
            print(f"        proto rgb {tuple(round(v,4) for v in c)} x{n}")
    if fnamed:
        for c, n in list(fnamed.items())[:6]:
            print(f"        face  rgb {tuple(round(v,4) for v in c)} x{n}")

    if total and not (named or fnamed):
        print("  >>> the file HAS colour and our reader missed it: our bug")
    elif not total:
        print("  >>> nothing to read. Re-export as AP214/AP242 with colours "
              "on, or the viewport can only ever show one default colour.")
    else:
        print("  >>> colour found and read")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        args = sorted(glob.glob("*.step") + glob.glob("*.STEP") +
                      glob.glob("*.stp"))
    if not args:
        print("no STEP files given or found")
        return
    for a in args:
        report(a)


if __name__ == "__main__":
    main()
