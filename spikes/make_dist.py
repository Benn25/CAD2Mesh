"""Build a trimmed, runnable copy of the app for sharing.

The dev venv is 1.3 GB, and almost none of that is reachable from this program:
PySide6 ships the whole of Qt (an embedded Chromium, Qt3D, QML, Designer,
Multimedia) while the app imports QtCore, QtGui and QtWidgets only. Nothing here
is a code change -- it is simply not copying files that are never loaded.

    .venv/Scripts/python.exe spikes/make_dist.py <target-dir> [--force]
"""
import os
import shutil
import sys
import fnmatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv")

# Qt modules the app never imports. Checked with:
#   grep -rho "from PySide6\.[A-Za-z]*" stepusd/ run_gui.py
# which reports QtCore, QtGui, QtWidgets (and QtTest, for the spikes only).
QT_DROP = [
    "*WebEngine*", "*Qt3D*", "*Quick*", "*Qml*", "*Multimedia*", "*Designer*",
    "*Charts*", "*DataVisualization*", "*Pdf*", "*Bluetooth*", "*Nfc*",
    "*SerialPort*", "*Sql*", "*Sensors*", "*Positioning*", "*RemoteObjects*",
    "*Scxml*", "*SpatialAudio*", "*TextToSpeech*", "*WebSockets*",
    "*WebChannel*", "*Location*", "*Help*", "*UiTools*",
    "avcodec*", "avformat*", "avutil*", "swresample*", "swscale*",
]
QT_DROP_DIRS = ["qml", "translations", "typesystems", "glue", "include",
                "scripts", "examples", "support"]
# Whole packages that only the test scripts need
DROP_PKGS = ["pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack"]

# DO NOT trim vtk.libs. It looks like the obvious next 77 MB -- viskores (VTK-m
# compute filters), charts, volume rendering, the exotic IO formats -- and none
# of it is reachable from our code. But OCP links against VTK itself:
# cadquery-ocp bundles OCCT's VTK integration, so removing those DLLs breaks
# `import OCP` with a bare "DLL load failed", nowhere near the code that
# actually needed them. Measured: parking 65 DLLs saved 77 MB and made the app
# refuse to start. Anything further needs a real dependency-closure walk of the
# PE imports, not a guess at which names look unused.


def folder_mb(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    dst = os.path.abspath(args[0])
    if os.path.exists(dst):
        if not force:
            print(f"{dst} already exists; pass --force to replace it")
            return 1
        marker = os.path.join(dst, ".venv", "Scripts")
        if not os.path.isdir(marker):
            print(f"{dst} does not look like a build of this app "
                  f"(no .venv/Scripts) -- refusing to delete it")
            return 1
        print(f"replacing {dst}")
        shutil.rmtree(dst)
    before = folder_mb(VENV)
    print(f"source venv: {before:,.0f} MB  -> {dst}")

    def ignore(dirpath, names):
        skip = set()
        for n in names:
            full = os.path.join(dirpath, n)
            if n == "__pycache__" or n.endswith((".pyc", ".pdb", ".lib", ".exp")):
                skip.add(n)
                continue
            if os.path.isdir(full) and n in DROP_PKGS:
                skip.add(n)
                continue
            if "PySide6" in dirpath or n.startswith("PySide6"):
                if os.path.isdir(full) and n in QT_DROP_DIRS:
                    skip.add(n)
                    continue
                if any(fnmatch.fnmatch(n, p) for p in QT_DROP):
                    skip.add(n)
        return skip

    shutil.copytree(VENV, os.path.join(dst, ".venv"), ignore=ignore,
                    symlinks=True)
    for item in ("stepusd", "run_gui.py", "run_gui.bat", "CLAUDE.md"):
        src = os.path.join(ROOT, item)
        if not os.path.exists(src):
            continue
        d = os.path.join(dst, item)
        if os.path.isdir(src):
            shutil.copytree(src, d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, d)
    after = folder_mb(dst)
    print(f"trimmed:     {after:,.0f} MB   "
          f"({100 * (1 - after / before):.0f}% smaller)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
