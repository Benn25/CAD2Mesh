"""How close are the PRIORITY FILLETS to a regular ladder?

Judges only the faces the fillet detector flags, and judges them on the things
that actually show as a broken normal: are the cells quads, are they uniform in
size, are they free of slivers, and is the vertex valence that of a grid rather
than a fan. Deliberately NOT _iso_fraction -- that scores a fillet of small
aligned quads with long slivers top and bottom very well, and it looks terrible.
"""
import sys, os, glob, math, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dataclasses import replace
from collections import Counter
from stepusd import _testcache as _tc
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams, is_priority_fillet
from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape


def score(doc, params, limit=None):
    n_poly = n_quad = n_sliver = 0
    areas, valences = [], []
    seen = 0
    for p in doc.protos.values():
        for b in p.bodies:
            if limit is not None and seen >= limit:
                break
            seen += 1
            faces = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(b, TopAbs_ShapeEnum.TopAbs_FACE, faces)
            # detect with the switch ON regardless of what we MESH with, so
            # both arms score the same set of faces
            det = replace(params, fillet_first=True)
            want = set()
            for fi in range(1, faces.Extent() + 1):
                if is_priority_fillet(TopoDS.Face_s(faces.FindKey(fi)), det):
                    want.add(fi - 1)
            if not want:
                continue
            m = tessellate(b, params)
            val = Counter()
            for poly, src in zip(m.faces, m.face_source):
                if src not in want:
                    continue
                n_poly += 1
                if len(poly) == 4:
                    n_quad += 1
                for v in poly:
                    val[v] += 1
                k = len(poly)
                L = [math.dist(m.points[poly[i]], m.points[poly[(i + 1) % k]])
                     for i in range(k)]
                L = [x for x in L if x > 1e-12]
                if len(L) >= 3:
                    if max(L) / min(L) > 4.0:
                        n_sliver += 1
                    areas.append(sum(L) / len(L))
            if val:
                valences.append(max(val.values()))
    if not n_poly:
        return None
    med = statistics.median(areas) if areas else 1.0
    spread = (statistics.pstdev(areas) / med) if len(areas) > 1 and med else 0.0
    return dict(polys=n_poly,
                quads=100.0 * n_quad / n_poly,
                slivers=100.0 * n_sliver / n_poly,
                size_spread=spread,
                worst_valence=max(valences) if valences else 0)


def main():
    defl = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2
    ang = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    V = _tc.biggest(exclude_handle=True)
    base = MeshParams(deflection=defl, angle_deg=ang)
    for label, cache in (("valve", V), ("handle", ".cache/handle")):
        doc = load_cache(cache)
        print(f"\n{label}  (priority fillet faces only, {defl}/{ang:.0f})")
        for name, pr in (("OFF", replace(base, fillet_first=False)),
                         ("ON ", replace(base, fillet_first=True))):
            r = score(doc, pr)
            if r is None:
                print(f"  fillet_first {name}: no fillet faces")
                continue
            print(f"  fillet_first {name}: {r['polys']:7,} polys  "
                  f"quads {r['quads']:5.1f}%  slivers {r['slivers']:5.2f}%  "
                  f"size spread {r['size_spread']:.3f}  "
                  f"worst valence {r['worst_valence']}", flush=True)


if __name__ == "__main__":       # Windows spawn re-imports this module
    main()
