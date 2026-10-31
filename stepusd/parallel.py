"""Tessellate bodies across CPU cores.

Python threads are useless here: OCP holds the GIL for the whole of
BRepMesh, so N threads run strictly one at a time. Processes do work, and
TopoDS_Shape is not picklable, so each worker re-reads the BRep cache once and
is then addressed by (prototype index, body index).

Note on the GPU: there is no path. OCCT's mesher is sequential B-rep work --
curve/surface evaluation, edge discretisation, constrained Delaunay in UV --
with data-dependent branching throughout. Cores are the realistic win.
"""
from __future__ import annotations
import os

#: per-process, loaded once by the initializer
_BODIES = None


def _load_bodies(cache_dir: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape, TopoDS_Iterator
    root = TopoDS_Shape()
    BRepTools.Read_s(root, os.path.join(cache_dir, "shapes.brep"), BRep_Builder())
    groups = []
    it = TopoDS_Iterator(root)
    while it.More():
        sub, bodies = it.Value(), []
        jt = TopoDS_Iterator(sub)
        while jt.More():
            bodies.append(jt.Value())
            jt.Next()
        groups.append(bodies)
        it.Next()
    return groups


def init_worker(cache_dir: str):
    global _BODIES
    _BODIES = _load_bodies(cache_dir)


def tess_one(task):
    """task = (proto_index, body_index, MeshParams) -> (proto_index, body_index, Mesh)."""
    from .tessellate import tessellate
    pi, bi, params = task
    body = _BODIES[pi][bi]
    return pi, bi, tessellate(body, params)


def worker_count(requested: int | None = None) -> int:
    if requested:
        return max(1, requested)
    n = os.cpu_count() or 4
    # 80% of the hardware threads, per Benn's request: enough to use the machine
    # properly while leaving the UI and the OS room. On his 32-thread box that
    # is 25 workers. Each worker holds its own copy of the BRep model, so the
    # real ceiling is memory rather than cores -- 25 workers on a 60 MB model is
    # roughly 3 GB.
    return max(1, min(32, int(n * 0.8)))


def warm(_ignored=None):
    """No-op task used to force a pool worker to actually start.

    ProcessPoolExecutor spawns workers LAZILY -- one per submit until
    max_workers -- so with a cold pool the FIRST tessellation pays every
    process spawn plus every worker's BRep re-read at once. At 25 workers on a
    30 MB model that is the progress bar sitting at 0% for several seconds.
    Submitting one of these per worker straight after the document loads moves
    that cost off the user's first interaction.
    """
    return _BODIES is not None
