"""Launch the STEP -> USD converter GUI.

Self-healing launcher:

* The dependencies (PySide6, VTK, OCP, USD) live in the project's .venv, so
  running this with the system Python -- which is what a double-click or
  `py run_gui.py` does -- would die instantly with ModuleNotFoundError. If we
  are not on an interpreter that has them, re-exec with the venv one.
* Prefer pythonw.exe so no console window appears. That means sys.stdout and
  sys.stderr are None, so they are redirected to error.log first: a stray
  print() would otherwise crash the app.
* Any unhandled exception is shown in a dialog and written to error.log, since
  with no console there is nowhere else for it to go.
"""
import os
import sys
import subprocess
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = "STEPUSD_RELAUNCHED"
LOG = os.path.join(HERE, "error.log")


def _ensure_streams():
    """pythonw.exe gives None for stdout/stderr; print() would then raise."""
    if sys.stdout is None or sys.stderr is None:
        # Discard by default so no empty error.log is littered on every launch;
        # report() writes the log explicitly when there is something to say.
        f = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def venv_python(windowed: bool = False):
    names = ("pythonw.exe", "python.exe") if windowed else ("python.exe",)
    for nm in names:
        p = os.path.join(HERE, ".venv", "Scripts", nm)
        if os.path.exists(p):
            return p
    p = os.path.join(HERE, ".venv", "bin", "python")
    return p if os.path.exists(p) else None


def have_deps() -> bool:
    try:
        import PySide6  # noqa: F401
        import vtkmodules  # noqa: F401
        import OCP  # noqa: F401
        from pxr import Usd  # noqa: F401
        return True
    except Exception:
        return False


def report(msg: str, title: str = "STEP to USD") -> int:
    """Show a message. Falls back to console/log when Qt is unavailable."""
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n\n")
    except OSError:
        pass
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        app.setStyle("Fusion")      # match the app; see stepusd/gui/main.run
        QMessageBox.critical(None, title, msg)
        return 1
    except Exception:
        pass
    print("\n" + "=" * 68 + "\n" + msg + "\n" + "=" * 68)
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
    return 1


def _excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    report(f"Unexpected error:\n\n{text}", "STEP to USD - crash")


def main() -> int:
    _ensure_streams()

    if have_deps():
        sys.excepthook = _excepthook
        from stepusd.gui.main import run
        return run()

    if os.environ.get(GUARD):
        return report(
            "The virtual environment is missing dependencies.\n\n"
            "Rebuild it from the project folder:\n"
            "    .venv\\Scripts\\python.exe -m pip install "
            "cadquery-ocp usd-core numpy PySide6")

    vp = venv_python(windowed=True)
    if not vp:
        return report(
            f"No virtual environment found at {os.path.join(HERE, '.venv')}\n\n"
            f"This app must run on the project's .venv, not the system Python.\n"
            f"Create it with:\n"
            f"    py -3.12 -m venv .venv\n"
            f"    .venv\\Scripts\\python.exe -m pip install "
            f"cadquery-ocp usd-core numpy PySide6")

    env = dict(os.environ, **{GUARD: "1"})
    # Launch DETACHED and return at once. subprocess.call() would sit here
    # waiting, which is what leaves an empty console window hanging around after
    # a double-click (and closing it then does not stop the app).
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    subprocess.Popen([vp, os.path.abspath(__file__)] + sys.argv[1:],
                     env=env, creationflags=flags, close_fds=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
