"""Put test windows somewhere they do not interrupt Benn.

His main screen is the BenQ, so the tests belong on the Dell if it is awake and
on the HP otherwise. But those are not the only states the machine is in:

    Dell or HP attached   put the window there
    only the main screen  use it, but do NOT take the keyboard focus
    driving via Parsec    one screen, at a lower resolution -- same as above
    no screens at all     leave the window entirely alone

**Call `place()` BEFORE `show()`.** `WA_ShowWithoutActivating` is read when the
window is first shown, so setting it afterwards changes nothing -- the window
has already grabbed the focus by then, which is the whole thing being avoided.
The geometry is set the same way in both cases, so a late call still positions
correctly; it just cannot keep the focus.

`keyboard=True` says the test DRIVES the window with synthetic key events
(`QTest.keyClick`). Those go through Qt's shortcut map, which will not match a
shortcut whose window is inactive -- so such a test must take the focus, and
saying so at the call site beats it failing mysteriously. Only
`spikes/blender_keys.py` needs it.

**Parsec is deliberately NOT detected, because it cannot be.** Benn said focus
does not matter in a Parsec session, which would license taking it -- but
`parsecd.exe` runs CONTINUOUSLY as the host whether or not anyone is connected,
so its presence proves nothing: measured True with him sitting at the desk,
which would have switched the focus protection off exactly when it was needed.
`SM_REMOTESESSION` is no help either, since Parsec drives the real local
display rather than opening an RDP session. So the conservative behaviour is
used for both: never take the focus on the main screen. That is required at the
desk and merely unnecessary over Parsec, which is the right way round to be
wrong.
"""

#: preference order, matched against QScreen name / model / manufacturer
PREFERRED = ("dell", "hp")

#: a screen smaller than this in either direction is treated as absent -- that
#: is what a monitor which has been switched off can look like
MIN_USABLE = 200


def pick_screen(app=None):
    """(screen, is_the_main_one) for a test window, or (None, False)."""
    if app is None:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
    if app is None:
        return None, False
    main = app.primaryScreen()
    usable = [s for s in app.screens()
              if s.availableGeometry().width() > MIN_USABLE
              and s.availableGeometry().height() > MIN_USABLE]
    if not usable:
        return None, False
    for want in PREFERRED:
        for s in usable:
            tag = f"{s.name()} {s.model()} {s.manufacturer()}".lower()
            if want in tag and s is not main:
                return s, False
    # nothing preferred is awake: any second screen still beats covering his
    # work, and failing that the main one is all there is
    other = next((s for s in usable if s is not main), None)
    if other is not None:
        return other, False
    return (main if main in usable else usable[0]), True


def place(win, app=None, margin=40, keyboard=False):
    """Position `win` for testing. Returns the QScreen used, or None.

    See the module docstring: call this BEFORE `show()`.
    """
    s, is_main = pick_screen(app)
    if s is None:
        return None
    g = s.availableGeometry()
    w = max(320, min(win.width(), g.width() - 2 * margin))
    h = max(240, min(win.height(), g.height() - 2 * margin))
    win.setGeometry(g.x() + margin, g.y() + margin, w, h)
    handle = win.windowHandle()
    if handle is not None:                 # only exists once shown
        handle.setScreen(s)
    # Keep out of the way only when it would actually be in the way: on the one
    # screen he might be working on.
    if is_main and not keyboard:
        from PySide6.QtCore import Qt
        win.setAttribute(Qt.WA_ShowWithoutActivating, True)
    return s


def take_focus(win, app=None, timeout=5.0):
    """Make `win` the active window, and say whether it worked.

    A test that injects keys with `QTest.keyClick` depends on this: Qt resolves
    a shortcut through the shortcut map, which will not match one whose window
    is inactive, so the key is delivered and simply does nothing. The symptom
    is the FEATURE looking broken -- "Z did not toggle isolate" -- rather than
    anything pointing at focus, so the test should check the precondition
    instead of assuming it.

    Showing a window is not enough on its own: Windows may decline to activate
    a window that was positioned onto another screen just before being shown.
    """
    import time
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop
    if app is None:
        app = QApplication.instance()
    win.raise_()
    win.activateWindow()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        if win.isActiveWindow():
            return True
        time.sleep(0.02)
    return win.isActiveWindow()


def describe(app=None):
    """One line saying where the window went and why, for the test's output."""
    s, is_main = pick_screen(app)
    if s is None:
        return "no usable screen: leaving the window where Qt put it"
    if not is_main:
        return f"test window on the secondary screen '{s.name()}'"
    g = s.availableGeometry()
    return (f"test window on the ONLY screen '{s.name()}' "
            f"({g.width()}x{g.height()}), without taking focus")
