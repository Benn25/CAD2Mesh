"""Re-mesh a body with its analytic surfaces dropped -- in a CHILD PROCESS.

Converting a solid with `BRepBuilderAPI_NurbsConvert` is what fixes OCCT's
under-meshing of trimmed conical faces (see CLAUDE.md). It is also, on at least
one real body, a way to SEGFAULT: handle `CenterLED_sw_001_DECO_B` converts
cleanly, passes a volume check, and then kills the process inside `BRepMesh`.
Nothing about the converted shape predicts it -- face count, per-face areas, UV
bounds and total volume are all healthy, and `BRepCheck_Analyzer` crashes on it
too, so it cannot be validated in-process either.

A segfault cannot be caught, so the work is done somewhere it is allowed to die.
The parent writes the solid to a BRep file, runs this module as a child, and
reads back a pickled Mesh; if the child dies, the parent simply keeps the
original mesh and the body stays flagged. That is the same reasoning as
`loadproc.py`, which already runs the STEP read out of process.
"""
import os
import pickle
import subprocess
import sys
import tempfile


def _child_main(brep_path: str, params_path: str, out_path: str) -> int:
    from OCP.BRepTools import BRepTools
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Shape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from . import tessellate as T

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, brep_path, BRep_Builder()):
        return 2
    with open(params_path, "rb") as f:
        params = pickle.load(f)
    conv = BRepBuilderAPI_NurbsConvert(shape, True).Shape()
    if conv is None or conv.IsNull():
        return 3
    # the volume guard lives here too: a conversion that moved the part is not
    # the user's part, and there is no point shipping it back
    v0, v1 = T._solid_volume(shape), T._solid_volume(conv)
    if v0 > 0 and abs(v1 - v0) > v0 * 0.01:
        return 4
    mesh = T._tessellate_once(conv, params)
    with open(out_path, "wb") as f:
        pickle.dump(mesh, f, protocol=pickle.HIGHEST_PROTOCOL)
    return 0


def attempt(shape, params, timeout: float = 120.0):
    """A NURBS-converted mesh of `shape`, or None if the child could not."""
    from OCP.BRepTools import BRepTools

    tmp = tempfile.mkdtemp(prefix="stepusd_nurbs_")
    brep = os.path.join(tmp, "in.brep")
    par = os.path.join(tmp, "params.pkl")
    out = os.path.join(tmp, "out.pkl")
    try:
        if not BRepTools.Write_s(shape, brep):
            return None
        with open(par, "wb") as f:
            pickle.dump(params, f, protocol=pickle.HIGHEST_PROTOCOL)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run(
            [sys.executable, "-m", "stepusd.nurbsfix", brep, par, out],
            cwd=root, env=env, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.exists(out):
            return None            # crashed, timed out, or refused: no fix
        with open(out, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1], sys.argv[2], sys.argv[3]))
