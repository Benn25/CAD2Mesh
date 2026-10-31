"""Where the app is allowed to WRITE.

The cache and the presets used to live next to the code, which is fine for a
checkout and fatal for the way this actually gets shared: a folder on a network
share, read-only to everyone but the person who put it there. Loading a STEP
writes a cache, and editing presets writes a JSON, so on a read-only copy the
first is a hard failure and the second silently loses the edit.

Rule: if the app's own folder is writable, behave exactly as before -- a
developer checkout keeps its `.cache/` and `quality_presets.json` where they
have always been. Only when it is NOT writable does state move to the user's
own profile, where each colleague gets their own.
"""
import os

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_data_dir = None


def app_dir() -> str:
    """The folder holding stepusd/ -- read-only on a shared deployment."""
    return _APP


def _writable(d: str) -> bool:
    probe = os.path.join(d, ".write_probe")
    try:
        os.makedirs(d, exist_ok=True)
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir() -> str:
    """Where cache and settings go. Probed once, then remembered."""
    global _data_dir
    if _data_dir is not None:
        return _data_dir
    if _writable(_APP):
        _data_dir = _APP
    else:
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
        d = os.path.join(base, "StepUSD")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = _APP          # nothing else to try; callers handle the failure
        _data_dir = d
    return _data_dir


def cache_dir() -> str:
    return os.path.join(data_dir(), ".cache")


def settings_path(name: str) -> str:
    return os.path.join(data_dir(), name)
