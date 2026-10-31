"""All six up-axis choices must actually put that axis up.

USD's stage metadata only knows Y-up and Z-up, so the other four are expressed
as a ROTATION. That rotation used to sit on a synthetic `/Root` prim; Benn
asked for that wrapper to go ("I dont need this root parent, just keep the top
parent from the assembly"), so it now sits on the assembly's own root prims
instead -- which is exactly the kind of move that can silently invert or
transpose a transform and still look plausible.

The test does not trust the code's own arithmetic: it exports the same document
with every axis, recovers the rotation by comparing each against the +Z export
(whose root carries no rotation at all), and checks that the chosen MODEL axis
lands on the stage's up vector.

Run:  .venv/Scripts/python.exe spikes/up_axis.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pxr import Usd, UsdGeom, Gf
from stepusd.loadproc import load_cache
from stepusd.tessellate import tessellate, MeshParams
from stepusd.writer import write_usd
from stepusd import _testcache

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

AXES = ("+Z", "+Y", "-Z", "-Y", "+X", "-X")
VEC = {"+X": Gf.Vec3d(1, 0, 0), "-X": Gf.Vec3d(-1, 0, 0),
       "+Y": Gf.Vec3d(0, 1, 0), "-Y": Gf.Vec3d(0, -1, 0),
       "+Z": Gf.Vec3d(0, 0, 1), "-Z": Gf.Vec3d(0, 0, -1)}


def roots_of(stage):
    return [p for p in stage.GetPseudoRoot().GetChildren()
            if not p.GetName().startswith("_")]


def local(stage, prim):
    return UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())


def main():
    doc = load_cache(_testcache.biggest())
    p = MeshParams(deflection=2.0, angle_deg=40, min_size=3.2)
    for proto in doc.protos.values():
        proto.meshes = [tessellate(b, p) for b in proto.bodies]
    os.makedirs(OUT, exist_ok=True)

    base = None
    for ax in AXES:
        path = os.path.join(OUT, f"up_{ax.replace('+','p').replace('-','m')}.usda")
        if os.path.exists(path):
            os.remove(path)
        write_usd(doc, path, up_axis=ax)
        stage = Usd.Stage.Open(path)
        tops = roots_of(stage)
        assert tops, f"{ax}: no top-level prim at all"
        assert not any(t.GetName() == "Root" for t in tops), \
            "the synthetic /Root came back"
        dflt = stage.GetDefaultPrim()
        assert dflt and dflt.IsValid(), f"{ax}: no defaultPrim"

        m = local(stage, tops[0])
        if ax == "+Z":
            base = m
            up = UsdGeom.GetStageUpAxis(stage)
            print(f"  {ax}: stage up = {up}, top prim = '{tops[0].GetName()}', "
                  f"{len(tops)} root(s), default = '{dflt.GetName()}'")
            continue

        # recover the rotation the writer added: M_axis = M_node * R
        R = base.GetInverse() * m
        want = VEC["+Y"] if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y \
            else VEC["+Z"]
        got = R.TransformDir(VEC[ax])
        ok = Gf.Dot(got, want) > 0.999
        print(f"  {ax}: model {ax} -> "
              f"({got[0]:+.3f}, {got[1]:+.3f}, {got[2]:+.3f})   "
              f"stage up ({want[0]:+.0f}, {want[1]:+.0f}, {want[2]:+.0f})   "
              f"{'ok' if ok else 'WRONG'}")
        assert ok, f"{ax} does not end up pointing at the stage's up axis"

    # and the wrapper really is gone, with the assembly's own name on top
    stage = Usd.Stage.Open(os.path.join(OUT, "up_pZ.usda"))
    names = [t.GetName() for t in roots_of(stage)]
    print(f"\n  top-level prims: {names}")
    assert "Root" not in names
    print("\nPASSED: six axes land correctly, and no synthetic root remains")


if __name__ == "__main__":
    main()
