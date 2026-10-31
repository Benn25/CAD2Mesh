"""Start menu and desktop shortcuts, so the app can be started without digging
through the drive for run_gui.bat.

Same constraints as winassoc.py: **no .exe** (colleagues are wary of the
SmartScreen warning on unsigned executables) and nothing that changes how the
app runs in development. A shortcut is only a pointer, so it points straight at
the venv's `pythonw.exe` with `run_gui.py` -- exactly what run_gui.bat does,
minus the console flash.

**The shortcut carries the app's AppUserModelID.** `run()` sets that ID on the
running process so the taskbar shows our icon rather than Python's. A shortcut
WITHOUT the same ID is a different "app" as far as the taskbar is concerned:
pin it, launch it, and you get TWO buttons -- the pin and the running window.
`WScript.Shell` cannot set the ID, so the shortcut is built through
IShellLink + IPropertyStore in a few lines of C# compiled by PowerShell's
Add-Type. No new dependency, nothing downloaded.

Per-user (the user's own Desktop and Start menu), no admin rights. The paths
are absolute, so a MOVED project needs the shortcuts re-created; `create()` is
idempotent and simply overwrites them.

    python -m stepusd.shortcuts            create both
    python -m stepusd.shortcuts remove     delete both
    python -m stepusd.shortcuts status
"""
import base64
import os
import subprocess
import sys

from .winassoc import APP_NAME, icon_path, launcher

# run() sets this on the process; the shortcut must carry the same string
APP_ID = "cmax.stepusd.converter.1"
DESCRIPTION = "Convert STEP to USD for Blender, with a live tessellation preview"

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CSIDL_PROGRAMS = 0x02            # %APPDATA%\Microsoft\Windows\Start Menu\Programs
_CSIDL_DESKTOPDIRECTORY = 0x10    # follows OneDrive / folder redirection


def _folder(csidl):
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) != 0:
        return None
    return buf.value or None


def locations():
    """{"Start menu": path, "Desktop": path} for the two .lnk files."""
    out = {}
    for label, csidl in (("Start menu", _CSIDL_PROGRAMS),
                         ("Desktop", _CSIDL_DESKTOPDIRECTORY)):
        d = _folder(csidl)
        if d:
            out[label] = os.path.join(d, APP_NAME + ".lnk")
    return out


# The C# is ASCII only; every value arrives through environment variables, so
# a path with spaces or Japanese in it never has to survive shell quoting.
_PS = r'''
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class CShellLink {}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
 Guid("000214F9-0000-0000-C000-000000000046")]
interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int c, IntPtr d, uint fl);
    void GetIDList(out IntPtr p);
    void SetIDList(IntPtr p);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetHotkey(out short k);
    void SetHotkey(short k);
    void GetShowCmd(out int c);
    void SetShowCmd(int c);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c, out int i);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string s, int i);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string s, uint r);
    void Resolve(IntPtr h, uint f);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string s);
}

[StructLayout(LayoutKind.Sequential, Pack = 4)]
struct PropKey { public Guid fmtid; public uint pid; }

// PROPVARIANT is 24 bytes on x64; declare the full size so the callee never
// reads past the end of what we hand it.
[StructLayout(LayoutKind.Explicit, Size = 24)]
struct PropVar {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
 Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
interface IPropertyStore {
    void GetCount(out uint c);
    void GetAt(uint i, out PropKey k);
    void GetValue(ref PropKey k, out PropVar v);
    void SetValue(ref PropKey k, ref PropVar v);
    void Commit();
}

public static class StepUsdLink {
    public static void Make(string lnk, string exe, string args, string dir,
                            string icon, string desc, string appId) {
        IShellLinkW l = (IShellLinkW)new CShellLink();
        l.SetPath(exe);
        l.SetArguments(args);
        l.SetWorkingDirectory(dir);
        l.SetDescription(desc);
        if (!String.IsNullOrEmpty(icon)) l.SetIconLocation(icon, 0);

        IPropertyStore ps = (IPropertyStore)l;
        PropKey key = new PropKey();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");  // System.AppUserModel.ID
        key.pid = 5;
        PropVar v = new PropVar();
        v.vt = 31;                                                     // VT_LPWSTR
        v.p = Marshal.StringToCoTaskMemUni(appId);
        try { ps.SetValue(ref key, ref v); ps.Commit(); }
        finally { Marshal.FreeCoTaskMem(v.p); }

        ((IPersistFile)l).Save(lnk, true);
    }
}
"@
foreach ($lnk in $env:SU_LNKS.Split("|")) {
    [StepUsdLink]::Make($lnk, $env:SU_EXE, $env:SU_ARGS, $env:SU_DIR,
                        $env:SU_ICON, $env:SU_DESC, $env:SU_ID)
    Write-Output "ok $lnk"
}
'''


def create(where=("Start menu", "Desktop")):
    """Create (or overwrite) the shortcuts. Returns {label: path}."""
    if os.name != "nt":
        raise OSError("Windows only")
    exe, script = launcher()
    if not exe:
        raise OSError(f"no runnable launcher under {_APP}")
    locs = {k: v for k, v in locations().items() if k in where}
    if not locs:
        raise OSError("could not find the Desktop / Start menu folders")
    for p in locs.values():
        os.makedirs(os.path.dirname(p), exist_ok=True)

    env = dict(os.environ,
               SU_LNKS="|".join(locs.values()), SU_EXE=exe,
               SU_ARGS=f'"{script}"', SU_DIR=_APP, SU_ICON=icon_path() or "",
               SU_DESC=DESCRIPTION, SU_ID=APP_ID)
    enc = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-EncodedCommand", enc],
        env=env, capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    missing = [p for p in locs.values() if not os.path.exists(p)]
    if r.returncode != 0 or missing:
        raise OSError((r.stderr or r.stdout or "powershell failed").strip())
    return locs


def remove():
    """Delete whichever shortcuts exist. Returns the paths removed."""
    gone = []
    for p in locations().values():
        if os.path.exists(p):
            os.remove(p)
            gone.append(p)
    return gone


def status():
    """{label: path} for the shortcuts that currently exist."""
    return {k: p for k, p in locations().items() if os.path.exists(p)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "create"
    if cmd == "create":
        for k, p in create().items():
            print(f"{k:<11} {p}")
    elif cmd == "remove":
        for p in remove() or ["(nothing to remove)"]:
            print(p)
    elif cmd == "status":
        s = status()
        for k, p in locations().items():
            print(f"{k:<11} {'present' if k in s else 'absent '}  {p}")
    else:
        sys.exit("usage: python -m stepusd.shortcuts [create|remove|status]")
