"""Does the test window land somewhere sensible in every monitor state?

Benn's machine is not always the three-monitor desk. It can be:

    all three awake       -> Dell
    Dell off              -> HP
    both off              -> the BenQ, WITHOUT taking the keyboard focus
    driven through Parsec -> the one (smaller) screen, focus is free
    nothing at all        -> leave the window alone

The real screen list cannot be changed from a test, so `pick_screen` is driven
against a stand-in that reports whatever state is being checked. The one thing
this cannot fake is the actual monitors, so it also runs once for real.

Run:  .venv/Scripts/python.exe spikes/screen_choice.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QRect, Qt
import _screen


class FakeScreen:
    def __init__(self, name, x=0, w=2560, h=1440):
        self._n, self._g = name, QRect(x, 0, w, h)

    def name(self):
        return self._n

    def model(self):
        return self._n

    def manufacturer(self):
        return self._n

    def availableGeometry(self):
        return self._g

    def geometry(self):
        return self._g


class FakeApp:
    def __init__(self, screens, primary=0):
        self._s = screens
        self._p = screens[primary] if screens else None

    def screens(self):
        return self._s

    def primaryScreen(self):
        return self._p


BENQ = FakeScreen("BenQ PD3200U", 0)
HP = FakeScreen("HP V28 4K", -3840)
DELL = FakeScreen("DELL U2711", 3840)
OFF = FakeScreen("DELL U2711", 3840, 0, 0)        # a monitor switched off
PARSEC_BENQ = FakeScreen("BenQ PD3200U", 0, 1920, 1080)   # lower res

CASES = [
    ("all three awake", [BENQ, HP, DELL], "DELL U2711", False),
    ("Dell off", [BENQ, HP, OFF], "HP V28 4K", False),
    ("Dell and HP off", [BENQ, OFF], "BenQ PD3200U", True),
    ("main only", [BENQ], "BenQ PD3200U", True),
    ("Parsec: one smaller screen", [PARSEC_BENQ], "BenQ PD3200U", True),
    ("everything off", [OFF], None, False),
    ("no screens at all", [], None, False),
]


def main():
    app = QApplication(sys.argv[:1])
    ok = True
    for label, screens, want, want_main in CASES:
        got, is_main = _screen.pick_screen(FakeApp(screens))
        name = got.name() if got is not None else None
        mark = "ok " if (name == want and (got is None or is_main == want_main)) \
            else "BAD"
        if mark == "BAD":
            ok = False
        print(f"  {mark} {label:<28} -> {name!r}"
              f"{' (main, keeps focus off)' if is_main else ''}")
        assert name == want, f"{label}: wanted {want!r}, got {name!r}"
        if got is not None:
            assert is_main == want_main, f"{label}: is_main {is_main}"

    # focus suppression must be ON for the one-screen case and OFF otherwise,
    # since a test that drives QTest keys cannot work in an inactive window
    for label, screens, want_attr in (
            ("secondary present", [BENQ, HP, DELL], False),
            ("main only", [BENQ], True)):
        w = QWidget()
        w.resize(1400, 900)
        _screen.place(w, app=FakeApp(screens))
        got = w.testAttribute(Qt.WA_ShowWithoutActivating)
        print(f"  {'ok ' if got == want_attr else 'BAD'} "
              f"{label:<28} -> ShowWithoutActivating={got}")
        assert got == want_attr, (label, got, want_attr)
        g = w.geometry()
        scr = _screen.pick_screen(FakeApp(screens))[0]
        assert scr.availableGeometry().contains(g.topLeft()), \
            f"{label}: window placed outside its screen"

    # keyboard=True must ALWAYS keep the focus, or QTest.keyClick cannot work
    w = QWidget()
    w.resize(1400, 900)
    _screen.place(w, app=FakeApp([BENQ]), keyboard=True)
    assert not w.testAttribute(Qt.WA_ShowWithoutActivating), \
        "a keyboard-driven test must be allowed to take the focus"
    print("  ok  keyboard=True on the main screen  -> keeps the focus")

    print(f"  right now: {_screen.describe(app)}")
    print("\nPASSED: every monitor state picks a sensible screen"
          if ok else "\nFAILED")


if __name__ == "__main__":
    main()
