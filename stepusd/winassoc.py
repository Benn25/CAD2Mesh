"""Put this app in Explorer's "Open with" list for STEP files.

Benn's ask, and its two constraints shape the whole design:

* **No .exe.** Colleagues are wary of the SmartScreen warning on an unsigned
  executable, so there is nothing new to run: the association points straight
  at the venv's `pythonw.exe` with `run_gui.py`, which is exactly what
  `run_gui.bat` already does. Nothing is built, nothing is downloaded.
* **Do not disturb development.** No launcher changes; this is additive.

**It adds us to "Open with"; it does NOT steal the default.** That is both what
was asked for and the only thing that reliably works: since Windows 10, the
default handler is protected by a `UserChoice` hash that Explorer verifies, so
writing the default association from a script is ignored (and by design -- it
is anti-hijacking). Registering the ProgId under `OpenWithProgids` lists us
alongside SolidWorks and whatever else owns .step, and the user can still
promote us with Windows' own "Choose another app -> Always" if they want to.

Everything is written under **HKEY_CURRENT_USER**, so it needs no admin rights
and affects only the person who runs it -- which is right for a tool on a
shared folder.

The paths are absolute and baked into the registry, so MOVING the project
breaks the entry. `status()` reports that rather than leaving it to be
discovered by a click that does nothing.
"""
import os
import sys

PROGID = "CMAX.StepUSD.1"
EXTENSIONS = (".step", ".stp")      # registry keys are case-insensitive
APP_NAME = "STEP to USD"
TYPE_NAME = "STEP model"

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launcher():
    """(pythonw.exe, run_gui.py) to run, or (None, None) if not found."""
    for name in ("pythonw.exe", "python.exe"):
        p = os.path.join(_APP, ".venv", "Scripts", name)
        if os.path.exists(p):
            script = os.path.join(_APP, "run_gui.py")
            return (p, script) if os.path.exists(script) else (None, None)
    return None, None


def command_line():
    """The exact string Explorer will run, or None if the app is not runnable.

    `%1` is quoted because CAD paths routinely contain spaces -- and Benn's
    own files contain Japanese and full-width parentheses too.
    """
    exe, script = launcher()
    if not exe:
        return None
    return f'"{exe}" "{script}" "%1"'


def icon_path():
    p = os.path.join(_APP, "assets", "appicon.ico")
    return p if os.path.exists(p) else None


def register():
    """Add the "Open with" entry for the current user. Returns a report str."""
    if os.name != "nt":
        raise OSError("Windows only")
    import winreg
    cmd = command_line()
    if cmd is None:
        raise OSError(f"no runnable launcher under {_APP}")

    root = winreg.HKEY_CURRENT_USER
    base = rf"Software\Classes\{PROGID}"
    with winreg.CreateKey(root, base) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, TYPE_NAME)
        winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, TYPE_NAME)
    ico = icon_path()
    if ico:
        with winreg.CreateKey(root, base + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f"{ico},0")
    with winreg.CreateKey(root, base + r"\shell\open") as k:
        # what the "Open with" list shows: without this Windows names the entry
        # after the executable, which here is pythonw.exe -- i.e. "Python"
        winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f"Open with {APP_NAME}")
    with winreg.CreateKey(root, base + r"\shell\open\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)

    for ext in EXTENSIONS:
        with winreg.CreateKey(
                root, rf"Software\Classes\{ext}\OpenWithProgids") as k:
            # an EMPTY value under OpenWithProgids is the documented way to say
            # "this ProgId can open this type" without claiming the default
            winreg.SetValueEx(k, PROGID, 0, winreg.REG_NONE, b"")

    _notify_shell()
    return (f"registered for {', '.join(EXTENSIONS)}\n{cmd}")


def unregister():
    """Remove the entry again. Safe to call when nothing is registered."""
    if os.name != "nt":
        raise OSError("Windows only")
    import winreg
    root = winreg.HKEY_CURRENT_USER
    for ext in EXTENSIONS:
        try:
            with winreg.OpenKey(root,
                                rf"Software\Classes\{ext}\OpenWithProgids", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, PROGID)
        except OSError:
            pass
    # deepest first: a key with subkeys cannot be deleted
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell",
                r"\DefaultIcon", ""):
        try:
            winreg.DeleteKey(root, rf"Software\Classes\{PROGID}{sub}")
        except OSError:
            pass
    _notify_shell()
    return "removed"


def status():
    """(registered, message). Also catches an entry left behind by a MOVE."""
    if os.name != "nt":
        return False, "not Windows"
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Classes\{PROGID}\shell\open\command") as k:
            have = winreg.QueryValueEx(k, None)[0]
    except OSError:
        return False, "not registered"
    want = command_line()
    if want and have != want:
        return True, ("registered, but pointing at a DIFFERENT copy of the "
                      f"app:\n{have}\n\nre-register to point it here:\n{want}")
    shown, rec = is_offered()
    note = ("\nWindows lists it as a %s handler."
            % ("RECOMMENDED" if rec else "known")) if shown else (
        "\nWindows is NOT offering it yet -- Explorer caches that menu, so "
        "try 'Open with -> Choose another app', or restart Explorer.")
    return True, f"registered\n{have}{note}"


def offered():
    """What Windows would actually put in "Open with", via its own API.

    The registry being right and Explorer OFFERING the entry are two different
    claims, and only this one answers the second. `SHAssocEnumHandlers` is what
    the shell itself calls to build that list, so if we appear here the
    registration has done its job and anything still missing from the menu is
    Explorer's cached UI, not us.

    Returns [(display name, command, is_recommended)], newest info first, or []
    if the API cannot be reached.
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import POINTER, byref, c_void_p, c_ulong, c_long, c_wchar_p
    NEXT = ctypes.WINFUNCTYPE(c_long, c_void_p, c_ulong,
                              POINTER(c_void_p), POINTER(c_ulong))
    GETSTR = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_wchar_p))
    NOARG = ctypes.WINFUNCTYPE(c_long, c_void_p)
    RELEASE = ctypes.WINFUNCTYPE(c_ulong, c_void_p)

    def slot(iface, index, proto):
        vtbl = ctypes.cast(iface, POINTER(c_void_p))[0]
        return proto(ctypes.cast(vtbl, POINTER(c_void_p))[index])

    out = []
    try:
        ctypes.windll.ole32.CoInitialize(None)
        for ext in EXTENSIONS:
            e = c_void_p()
            if ctypes.windll.shell32.SHAssocEnumHandlers(
                    c_wchar_p(ext), 0, byref(e)) != 0 or not e:
                continue
            while True:
                h = c_void_p()
                got = c_ulong(0)
                if slot(e, 3, NEXT)(e, 1, byref(h), byref(got)) != 0 \
                        or not got.value:
                    break
                name, ui = c_wchar_p(), c_wchar_p()
                slot(h, 3, GETSTR)(h, byref(name))
                slot(h, 4, GETSTR)(h, byref(ui))
                rec = slot(h, 6, NOARG)(h) == 0
                out.append((ui.value, name.value, rec))
                slot(h, 2, RELEASE)(h)
            slot(e, 2, RELEASE)(e)
    except Exception:
        return []
    return out


def is_offered():
    """(shown by Windows, recommended) for THIS app."""
    exe = (launcher()[0] or "").lower()
    for ui, cmd, rec in offered():
        if ui == APP_NAME or (cmd or "").lower() == exe:
            return True, rec
    return False, False


def _notify_shell():
    """Tell Explorer to re-read associations, so it works without a re-login.

    `SHCNF_FLUSH` makes the call synchronous and forces the shell to act on it
    before returning. Without it the notification is queued and Explorer can
    keep serving its cached "Open with" menu -- which is exactly what Benn hit:
    the keys were correct, `SHAssocEnumHandlers` already listed us as a
    RECOMMENDED handler, and the menu still did not show it.
    """
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST, SHCNF_FLUSH = 0x0000, 0x1000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH, None, None)
    except Exception:
        pass


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    if what == "register":
        print(register())
    elif what == "unregister":
        print(unregister())
    else:
        print(status()[1])
