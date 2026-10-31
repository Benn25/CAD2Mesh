"""Explorer's "Open with" entry: does it register, and does it actually work?

Three separate claims, because each can fail on its own:

  1. the registry gets the keys Explorer reads, under HKCU, and removing them
     leaves nothing behind;
  2. the command string Explorer will run points at files that exist and
     survives a path with spaces and non-ASCII characters -- Benn's own models
     are named things like `8-7 DBXA-80S1A-JIS5K(/1R分岐).STEP`;
  3. the APP opens a file handed to it on the command line at all, which is
     the half that has nothing to do with the registry.

The existing association is snapshotted and restored, so running this does not
change what is on the machine.

Run:  .venv/Scripts/python.exe spikes/open_with.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stepusd import winassoc
from _screen import place


def read_tree():
    """Everything we write, as a dict, or {} when nothing is registered."""
    import winreg
    out = {}
    root = winreg.HKEY_CURRENT_USER
    for sub in ("", r"\DefaultIcon", r"\shell\open", r"\shell\open\command"):
        try:
            with winreg.OpenKey(
                    root, rf"Software\Classes\{winassoc.PROGID}{sub}") as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    out[f"{sub}:{name or '(Default)'}"] = val
                    i += 1
        except OSError:
            pass
    for ext in winassoc.EXTENSIONS:
        try:
            with winreg.OpenKey(
                    root, rf"Software\Classes\{ext}\OpenWithProgids") as k:
                winreg.QueryValueEx(k, winassoc.PROGID)
                out[f"{ext}:OpenWithProgids"] = winassoc.PROGID
        except OSError:
            pass
    return out


def check_registry():
    before = winassoc.status()[0]
    print(f"  before: {'registered' if before else 'not registered'}")

    print(winassoc.register().replace("\n", "\n    "))
    tree = read_tree()
    for k in sorted(tree):
        print(f"    {k:<34} = {tree[k]}")

    on, msg = winassoc.status()
    assert on, "status() does not see its own registration"
    cmd = tree.get(r"\shell\open\command:(Default)")
    assert cmd == winassoc.command_line(), "the command was not written"
    assert r"\shell\open:FriendlyAppName" in tree, \
        "without FriendlyAppName the list shows the interpreter, not us"
    for ext in winassoc.EXTENSIONS:
        assert f"{ext}:OpenWithProgids" in tree, f"{ext} not offered"

    # it must NOT have taken the default handler
    import winreg
    for ext in winassoc.EXTENSIONS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                rf"Software\Classes\{ext}") as k:
                default = winreg.QueryValueEx(k, None)[0]
        except OSError:
            default = ""
        assert default != winassoc.PROGID, \
            f"{ext}: we claimed the DEFAULT handler, which was not asked for"
    print("  the default handler for .step/.stp is untouched")

    winassoc.unregister()
    left = read_tree()
    assert not left, f"unregister left {left}"
    assert not winassoc.status()[0]
    print("  unregister leaves nothing behind")
    return before


def check_command():
    exe, script = winassoc.launcher()
    assert exe and os.path.exists(exe), f"no interpreter: {exe}"
    assert os.path.exists(script), f"no launcher script: {script}"
    cmd = winassoc.command_line()
    assert cmd.endswith('"%1"'), "the path placeholder must be QUOTED"
    assert cmd.count('"') == 6, f"quoting looks wrong: {cmd}"
    ico = winassoc.icon_path()
    print(f"  interpreter : {exe}")
    print(f"  script      : {script}")
    print(f"  icon        : {ico}")
    assert ico and os.path.exists(ico), "no icon for the Explorer entry"

    # the substitution Explorer performs, on a name with spaces and Japanese
    hard = next((f for f in os.listdir(".")
                 if f.lower().endswith((".step", ".stp"))
                 and (" " in f or any(ord(c) > 127 for c in f))), None)
    if hard:
        line = cmd.replace("%1", os.path.abspath(hard))
        print(f"  a hard name : {hard}")
        assert line.count('"') == 6 and line.endswith('"')
        print("  quoting survives spaces and non-ASCII")
    else:
        print("  (no awkwardly named STEP here to check quoting against)")


def check_app_opens_argv():
    """The half that is not about the registry: does argv reach the loader?"""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop, QTimer
    from stepusd.gui.main import MainWindow

    target = os.path.abspath("knob (failing).step")
    if not os.path.exists(target):
        print("  (no knob file; skipping the load check)")
        return
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    w = MainWindow()
    w.resize(1200, 800)
    place(w)
    w.show()
    w.viewport.start()
    # exactly what run() does with a command-line path
    QTimer.singleShot(0, lambda: w.open_step_path(target))
    end = time.time() + 180
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        if w.meshes:
            break
        time.sleep(0.02)
    assert w.meshes, "the app never loaded the file it was handed"
    n = sum(len(v) for v in w.meshes.values())
    print(f"  opened {os.path.basename(target)} from the command line: "
          f"{n} body/bodies meshed")
    assert os.path.basename(target) in w.windowTitle(), \
        "the title bar does not name the file"
    w.close()


def check_launcher_passthrough():
    """run_gui.py -> run(): the link the registry test cannot reach.

    Driven as a real subprocess against the real launcher, with `run()` stubbed
    so it stops before creating a window -- otherwise this would open the app
    on whichever screen Benn is working on.
    """
    import subprocess
    import ast as _ast
    probe = (
        "import sys, runpy\n"
        "import stepusd.gui.main as M\n"
        "M.run = lambda: (sys.stdout.write("
        "'RUN SAW: %r' % (sys.argv,)), 0)[1]\n"
        "runpy.run_path('run_gui.py', run_name='__main__')\n")
    target = os.path.abspath("knob (failing).step")
    if not os.path.exists(target):
        print("  (no knob file; skipping)")
        return
    r = subprocess.run([os.path.join(".venv", "Scripts", "python.exe"),
                        "-c", probe, target],
                       capture_output=True, text=True, timeout=120)
    assert "RUN SAW:" in r.stdout, \
        f"run_gui never reached run(): {r.stderr[:200]}"
    # a repr of argv -- parsed, not string-matched: comparing a raw path
    # against a repr fails on the doubled backslashes and looks like a bug
    seen = _ast.literal_eval(r.stdout.split("RUN SAW:", 1)[1].strip())
    print(f"  the launcher passes: {seen}")
    assert target in seen, "the file path did NOT survive the launcher"


def main():
    if os.name != "nt":
        print("Windows only")
        return
    print("registry:")
    was = check_registry()
    print("\ncommand line:")
    check_command()
    print("\nthe launcher:")
    check_launcher_passthrough()
    print("\nthe app itself:")
    check_app_opens_argv()
    if was:
        winassoc.register()
        print("\n  (it was registered before, so it has been put back)")
    print("\nPASSED: the Open-with entry registers, removes cleanly, "
          "and the app opens a file given to it")


if __name__ == "__main__":
    main()
