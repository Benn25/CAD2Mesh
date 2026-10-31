"""Pick a cached model for the test scripts, deterministically.

The spikes used to take `glob.glob(".cache/*")[0]` -- whatever the filesystem
returned first. Opening a couple of new STEP files silently switched the whole
suite onto a 1-body model, and tests written for the valve failed on assertions
that had nothing to do with the change being tested. Size-ordered instead, so
adding models never moves the target.
"""
import glob
import os


def _entries():
    out = []
    for c in glob.glob(".cache/*"):
        j = os.path.join(c, "scene.json")
        if os.path.isdir(c) and os.path.exists(j):
            out.append((os.path.getsize(j), c))
    return sorted(out, reverse=True)


def biggest(exclude_handle: bool = False) -> str:
    for _n, c in _entries():
        if exclude_handle and "handle" in c:
            continue
        return c
    raise SystemExit("no cached model found; open a STEP in the GUI first")


def with_open_shells() -> str:
    """A cached model that actually has open shells, for the repair tests."""
    from .loadproc import load_cache
    for _n, c in _entries():
        try:
            doc = load_cache(c)
        except Exception:
            continue
        if sum(p.open_bodies for p in doc.protos.values()):
            return c
    raise SystemExit("no cached model with open shells found")
