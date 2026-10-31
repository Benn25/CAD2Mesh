"""
Spike: prove that OCCT tessellation of a SOLID can be emitted as a single
welded, watertight, 2-manifold mesh -- while still shading sharp at sharp
CAD edges (via faceVarying normals, NOT by splitting vertices).

This is the anti-Fusion360 test.
"""
import math, collections
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_Orientation
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location
from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir, gp_Vec
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepLProp import BRepLProp_SLProps


def tessellate_solid(solid, deflection=0.1, angle_deg=20.0, min_size=-1.0,
                     relative=False, weld_decimals=7):
    """Tessellate one solid into ONE welded vertex pool + faceVarying normals."""
    p = IMeshTools_Parameters()
    p.Deflection = deflection
    p.Angle = math.radians(angle_deg)
    p.MinSize = min_size
    p.Relative = relative
    p.InParallel = True
    BRepMesh_IncrementalMesh(solid, p)

    points = []            # welded global positions
    weld = {}              # quantized coord -> global index
    tris = []              # (i0,i1,i2) into points
    fv_normals = []        # one normal per face-corner (3 per triangle)
    exact_hits = 0
    total_nodes = 0

    exp = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
    nfaces = 0
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        nfaces += 1
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            exp.Next(); continue
        trsf = loc.Transformation()
        reversed_face = (face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED)

        # exact surface normals at each node's UV (true CAD normals)
        surf = BRepAdaptor_Surface(face)
        has_uv = tri.HasUVNodes()
        props = BRepLProp_SLProps(surf, 1, 1e-9) if has_uv else None

        local_to_global = {}
        node_normals = {}
        for i in range(1, tri.NbNodes() + 1):
            total_nodes += 1
            pnt = tri.Node(i).Transformed(trsf)
            key = (round(pnt.X(), weld_decimals),
                   round(pnt.Y(), weld_decimals),
                   round(pnt.Z(), weld_decimals))
            gi = weld.get(key)
            if gi is None:
                gi = len(points)
                weld[key] = gi
                points.append((pnt.X(), pnt.Y(), pnt.Z()))
            else:
                exact_hits += 1
            local_to_global[i] = gi

            n = (0.0, 0.0, 1.0)
            if has_uv:
                uv = tri.UVNode(i)
                props.SetParameters(uv.X(), uv.Y())
                if props.IsNormalDefined():
                    d = props.Normal().Transformed(trsf)
                    n = (d.X(), d.Y(), d.Z())
            if reversed_face:
                n = (-n[0], -n[1], -n[2])
            node_normals[i] = n

        for t in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(t).Get()
            if reversed_face:
                a, c = c, a
            gi = (local_to_global[a], local_to_global[b], local_to_global[c])
            if gi[0] == gi[1] or gi[1] == gi[2] or gi[0] == gi[2]:
                continue  # degenerate (seam/apex collapse) -- drop
            tris.append(gi)
            fv_normals.extend([node_normals[a], node_normals[b], node_normals[c]])
        exp.Next()

    return dict(points=points, tris=tris, normals=fv_normals, nfaces=nfaces,
                raw_nodes=total_nodes, welded_away=exact_hits)


def check_watertight(points, tris):
    """A closed 2-manifold: every undirected edge used exactly twice, and
    every directed edge exactly once (consistent winding)."""
    undirected = collections.Counter()
    directed = collections.Counter()
    for a, b, c in tris:
        for u, v in ((a, b), (b, c), (c, a)):
            undirected[(min(u, v), max(u, v))] += 1
            directed[(u, v)] += 1
    bad_edges = {e: n for e, n in undirected.items() if n != 2}
    bad_wind = {e: n for e, n in directed.items() if n != 1}
    V, E, F = len(points), len(undirected), len(tris)
    return dict(V=V, E=E, F=F, euler=V - E + F,
                boundary_or_nonmanifold=len(bad_edges),
                winding_errors=len(bad_wind),
                sample_bad=list(bad_edges.items())[:5])


def report(name, solid, **kw):
    m = tessellate_solid(solid, **kw)
    c = check_watertight(m["points"], m["tris"])
    genus = (2 - c["euler"]) // 2
    status = "WATERTIGHT" if (c["boundary_or_nonmanifold"] == 0 and
                              c["winding_errors"] == 0) else "*** LEAKING ***"
    print(f"\n--- {name} ---")
    print(f"  B-rep faces      : {m['nfaces']}")
    print(f"  raw nodes        : {m['raw_nodes']}  ->  welded points: {c['V']}"
          f"   ({m['welded_away']} merged)")
    print(f"  triangles        : {c['F']}   edges: {c['E']}")
    print(f"  Euler V-E+F      : {c['euler']}  (genus {genus})")
    print(f"  non-manifold/open: {c['boundary_or_nonmanifold']}")
    print(f"  winding errors   : {c['winding_errors']}")
    print(f"  faceVarying norms: {len(m['normals'])} (= 3 x tris: "
          f"{len(m['normals']) == 3 * c['F']})")
    print(f"  VERDICT          : {status}")
    if c["sample_bad"]:
        print(f"  sample bad edges : {c['sample_bad']}")
    return m


if __name__ == "__main__":
    # 1. box with a cylindrical through-hole: planar + cylindrical faces, seam edge,
    #    genus 1 (a torus topologically) -- catches naive Euler assumptions too
    box = BRepPrimAPI_MakeBox(gp_Pnt(-25, -25, 0), 50, 50, 20).Shape()
    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -5), gp_Dir(0, 0, 1)), 8, 30).Shape()
    holed = BRepAlgoAPI_Cut(box, cyl).Shape()
    report("box with through-hole (genus 1)", holed, deflection=0.1, angle_deg=15)

    # 2. filleted box: planar + cylindrical + spherical corner patches, all smooth
    #    joins -- the classic case where exporters unweld and leak
    base = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, 0), 40, 40, 40).Shape()
    f = BRepFilletAPI_MakeFillet(base)
    e = TopExp_Explorer(base, TopAbs_ShapeEnum.TopAbs_EDGE)
    while e.More():
        f.Add(6.0, TopoDS.Edge_s(e.Current())); e.Next()
    report("filleted box (sphere+cyl+plane)", f.Shape(), deflection=0.05, angle_deg=12)

    # 3. same filleted box at very coarse settings -- does welding survive?
    report("filleted box @ COARSE", f.Shape(), deflection=2.0, angle_deg=45)
