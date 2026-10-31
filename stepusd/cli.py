from __future__ import annotations
import argparse, os, sys, time
from .tessellate import MeshParams
from .convert import convert


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="step2usd",
        description="Convert STEP to USD, preserving assembly hierarchy, "
                    "instancing and watertight solids.")
    ap.add_argument("step")
    ap.add_argument("-o", "--out", help="output .usda (default: alongside input)")
    ap.add_argument("--deflection", type=float, default=0.1,
                    help="max surface deviation, model units (default 0.1)")
    ap.add_argument("--angle", type=float, default=20.0,
                    help="max normal deviation in degrees (default 20)")
    ap.add_argument("--min-size", type=float, default=None,
                    help="min element size in mm; -1 = OCCT auto. Default is "
                         "1.6 x the deviation, which measured fewer slivers "
                         "AND fewer polygons than auto on both real models")
    ap.add_argument("--relative", action="store_true",
                    help="scale deflection to each part's size "
                         "(for assemblies mixing tiny and huge parts)")
    ap.add_argument("--no-quads", action="store_true",
                    help="split every grid cell into triangles instead of quads")
    ap.add_argument("--no-grid", action="store_true",
                    help="disable the axis-aligned ruled grid on cylinders/cones")
    ap.add_argument("--grid-aspect", type=float, default=8.0,
                    help="subdivide cylinders/cones along the axis until cells "
                         "are at most this many times longer than wide "
                         "(default 8; lower = squarer cells, more triangles)")
    ap.add_argument("--color", choices=("cad", "instance"), default="cad",
                    help="'instance' gives each unique part a distinct hue")
    a = ap.parse_args(argv)

    out = a.out or os.path.splitext(a.step)[0] + ".usda"
    # A real floor on element size beats OCCT's auto: measured on the valve,
    # slivers 26.34% -> 23.42%; on the handle 42.73% -> 40.77% with 4% FEWER
    # polygons and its two non-manifold edges gone. The GUI derives the same
    # value at load; keep the headless path in step with it.
    min_size = a.min_size if a.min_size is not None else a.deflection * 1.6
    params = MeshParams(deflection=a.deflection, angle_deg=a.angle,
                        min_size=min_size, relative=a.relative,
                        structured=not a.no_grid, grid_aspect=a.grid_aspect,
                        quads=not a.no_quads)
    t0 = time.time()

    def prog(i, n, name):
        print(f"\r  tessellating {i}/{n}  {name[:40]:<42}", end="", flush=True)

    doc = convert(a.step, out, params, color_mode=a.color, progress=prog)
    print("\r" + " " * 62 + "\r", end="")

    s = doc.stats()
    authored = sum(len(m.tris) for p in doc.protos.values() for m in p.meshes)
    placed = sum(len(m.tris) * p.occurrences
                 for p in doc.protos.values() for m in p.meshes)
    leaks = [(p.name, m) for p in doc.protos.values() for m in p.meshes
             if not m.watertight]
    opens = [p.name for p in doc.protos.values() if p.open_bodies]

    holes = sum(1 for _, m in leaks if m.boundary_edges)
    nonman = sum(1 for _, m in leaks if m.nonmanifold_edges)
    flaps = sum(m.removed_flaps for p in doc.protos.values() for m in p.meshes)
    if not leaks:
        verdict = "ALL BODIES SEALED"
    else:
        verdict = ", ".join(
            ([f"{holes} with holes"] if holes else [])
            + ([f"{nonman} non-manifold"] if nonman else []))
    print(f"\n{os.path.basename(a.step)} -> {out}")
    print(f"  prototypes      : {s['prototypes']}")
    print(f"  occurrences     : {s['occurrences']}  "
          f"({s['instanced']} saved by instancing)")
    ngrid = sum(m.structured_faces for p in doc.protos.values() for m in p.meshes)
    print(f"  triangles       : {authored:,} authored, {placed:,} as placed")
    nq = sum(m.n_quads for p in doc.protos.values() for m in p.meshes)
    nf = sum(len(m.faces) for p in doc.protos.values() for m in p.meshes)
    if nq:
        print(f"  quads           : {nq:,} of {nf:,} faces")
    if ngrid:
        print(f"  grid faces      : {ngrid} cylindrical/conical faces rebuilt "
              f"as axis-aligned grids")
    print(f"  watertight      : {verdict}")
    for nm, m in leaks[:10]:
        print(f"      ! {nm}: {m.boundary_edges} boundary(hole) edges, "
              f"{m.nonmanifold_edges} non-manifold edges, "
              f"{m.winding_errors} winding errors")
    if flaps:
        print(f"  repaired        : {flaps} zero-volume triangle flap(s)")
    if opens:
        print(f"  NOT SOLIDS      : {len(opens)} part(s) arrived as open shells "
              f"rather than closed solids, so they cannot be sealed: "
              f"{', '.join(opens[:6])}")
    print(f"  elapsed         : {time.time() - t0:.2f}s")
    print("\nBlender import: untick 'Merge Parent Xform', "
          "or the component empties collapse into their mesh.")
    return 0 if not leaks else 1


if __name__ == "__main__":
    sys.exit(main())
