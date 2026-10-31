"""The app must work from a READ-ONLY folder, the way a share deployment is.

Loading a STEP writes a geometry cache and editing presets writes a JSON; both
used to land next to the code, so a read-only copy failed at the first load and
silently lost preset edits. State now falls back to the user's profile, while a
writable checkout keeps everything exactly where it was.
"""
import sys, os, importlib, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    from stepusd import paths

    # --- 1. a writable checkout is unchanged -----------------------------
    assert paths.data_dir() == paths.app_dir(), (paths.data_dir(),
                                                 paths.app_dir())
    assert paths.cache_dir() == os.path.join(paths.app_dir(), ".cache")
    print(f"writable checkout: cache stays at {paths.cache_dir()}")
    from stepusd.gui import presets
    assert presets._path() == os.path.join(paths.app_dir(),
                                           "quality_presets.json")
    print(f"writable checkout: presets stay at {presets._path()}")

    # --- 2. read-only: everything moves to the user profile --------------
    paths._data_dir = None
    real_writable = paths._writable
    paths._writable = lambda d: False        # pretend the app dir is read-only
    try:
        d = paths.data_dir()
        assert d != paths.app_dir(), d
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        assert d.startswith(base), (d, base)
        assert os.path.isdir(d), d
        print(f"read-only app dir: state moves to {d}")
        importlib.reload(presets)
        assert presets._path().startswith(d), presets._path()
        # the SHIPPED presets are still the starting point
        assert presets._shipped().startswith(paths.app_dir())
        got = presets.load()
        assert got and all({"name", "deflection", "angle"} <= set(g)
                           for g in got), got
        print(f"read-only app dir: still loads {len(got)} presets "
              f"(from the shipped file)")
        # and a save lands in the writable place, not the app dir
        before = set(os.listdir(paths.app_dir()))
        presets.save(got)
        assert os.path.exists(presets._path()), presets._path()
        after = set(os.listdir(paths.app_dir()))
        assert before == after, after - before
        print("read-only app dir: saving presets does not touch the app folder")
        os.remove(presets._path())
    finally:
        paths._writable = real_writable
        paths._data_dir = None
        importlib.reload(presets)

    # --- 3. back to normal ------------------------------------------------
    assert paths.data_dir() == paths.app_dir()
    assert presets._path() == os.path.join(paths.app_dir(),
                                           "quality_presets.json")
    print("restored: writable checkout behaves exactly as before")
    print("\nPASSED: read-only deployment writes nothing into the app folder")


if __name__ == "__main__":
    main()
