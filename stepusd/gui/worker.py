"""Background tessellation.

CRITICAL: tessellation MUTATES the OCCT shape (BRepTools.Clean_s wipes the
triangulation, BRepMesh_IncrementalMesh writes a new one). Two workers meshing
the same TopoDS_Shape concurrently corrupts memory and kills the process with
no Python traceback. So exactly ONE worker may run at a time -- the scheduling
in MainWindow enforces that, and cancellation here is fine-grained (checked per
BODY, not per prototype) so a cancelled worker gets out of the way quickly even
on a single 164-body part.
"""
from __future__ import annotations
import collections
import hashlib
import os
import sys
import time
from PySide6.QtCore import (QThread, QObject, QProcess, QProcessEnvironment, QTimer, Signal)

from ..reader import StepDocument
from .. import loadproc
from dataclasses import replace, fields
from ..tessellate import tessellate, MeshParams


def _signature(p: MeshParams):
    """Hashable identity of a parameter set, for the mesh cache.

    Derived from ALL fields, not a hand-written list. The hand-written version
    went stale three times over -- grid_trim, align_edges and auto_grid were
    each missing -- and a field missing here does not cause a subtle error, it
    makes the corresponding GUI checkbox appear completely DEAD: every body hits
    a cache entry computed with the old setting, so nothing on screen changes.
    If you add a field to MeshParams, this now picks it up for free.

    `parallel` is excluded deliberately: it selects TBB threading inside OCCT
    and cannot change the geometry, and the parallel path deliberately flips it.
    """
    out = []
    for f in fields(p):
        if f.name == "parallel":
            continue
        v = getattr(p, f.name)
        out.append((f.name, round(v, 9) if isinstance(v, float) else v))
    return tuple(out)


class LoadWorker(QThread):
    """Kept for small files / fallback. NOTE: OCP holds the GIL for the whole
    STEP read, so this does NOT keep the UI responsive -- measured 3 heartbeat
    ticks where ~3600 were expected. Large files go through loadproc instead."""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.progress.emit("reading STEP...")
            self.done.emit(StepDocument(self.path))
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class TessellateWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(object, float)     # {proto_key: [Mesh, ...]}, seconds
    failed = Signal(str)

    def __init__(self, protos: list, params_for, generation: int, cache=None,
                 pool=None, proto_index=None):
        """params_for(proto, body_index) -> MeshParams, so quality is per BODY.
        That matters: a part can hold 164 bodies (SolidWorks surface-model
        exports do), and keying quality by part would move all of them at once.
        `cache` maps (proto.key, body index, params signature) -> mesh."""
        super().__init__()
        self.protos = protos
        self.params_for = params_for
        self.generation = generation
        self.cache = cache if cache is not None else {}
        self.pool = pool                    # ProcessPoolExecutor or None
        self.proto_index = proto_index or {}
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def total_bodies(self) -> int:
        return sum(len(p.bodies) for p in self.protos) or 1

    def run(self):
        t0 = time.time()
        if self.pool is not None and self.proto_index:
            try:
                self._run_parallel(t0)
                return
            except Exception:
                pass        # a broken pool must not lose the user's work
        self._run_serial(t0)

    def _plan(self):
        """[(proto, body index, params, cache key)] for every body."""
        out = []
        for proto in self.protos:
            for bi in range(len(proto.bodies)):
                p = self.params_for(proto, bi)
                out.append((proto, bi, p, (proto.key, bi, _signature(p))))
        return out

    def _run_parallel(self, t0):
        """Fan the bodies out over worker processes.

        Python threads cannot help -- OCP holds the GIL through BRepMesh -- so
        this is the only way to use more than one core. Measured 4x on 8
        workers, with byte-identical meshes.
        """
        from .. import parallel as par
        total = self.total_bodies()
        done = 0
        got = {}
        pending = []
        for proto, bi, p, key in self._plan():
            hit = self.cache.get(key)
            if hit is not None:
                got[(proto.key, bi)] = hit
                done += 1
                continue
            pi = self.proto_index.get(proto.key)
            if pi is None:
                raise RuntimeError("prototype not in the cache index")
            sub = replace(p, parallel=False)   # no nested TBB oversubscription
            pending.append((proto, bi, key,
                            self.pool.submit(par.tess_one, (pi, bi, sub))))
        if done:
            self.progress.emit(min(done, total), total, "cached")
        # Collect AS THEY FINISH, not in submission order. Waiting on futures in
        # order means one slow body blocks every result queued behind it, so the
        # progress bar sits still and then jumps -- which is what Benn saw as
        # "stuck at 0% for several seconds". The work took the same time either
        # way; only the reporting was wrong.
        from concurrent.futures import as_completed
        by_fut = {fut: (proto, bi, key) for proto, bi, key, fut in pending}
        for fut in as_completed(by_fut):
            if self._cancel:
                for f in by_fut:
                    f.cancel()
                return
            proto, bi, key = by_fut[fut]
            try:
                _pi, _bi, mesh = fut.result()
            except Exception:
                if self._cancel:
                    return
                raise
            got[(proto.key, bi)] = mesh
            self.cache[key] = mesh
            done += 1
            if done % 4 == 0 or done == total:
                self.progress.emit(min(done, total), total, proto.name)
        result = {}
        for proto in self.protos:
            result[proto.key] = [got[(proto.key, i)]
                                 for i in range(len(proto.bodies))]
        if not self._cancel:
            self.done.emit(result, time.time() - t0)

    def _run_serial(self, t0):
        result = {}
        total = self.total_bodies()
        done = 0
        try:
            for proto in self.protos:
                if self._cancel:
                    return
                meshes = []
                for bi, body in enumerate(proto.bodies):
                    if self._cancel:
                        return          # abandon promptly, mid-part
                    p = self.params_for(proto, bi)
                    key = (proto.key, bi, _signature(p))
                    hit = self.cache.get(key)
                    if hit is None:
                        hit = tessellate(body, p)
                        self.cache[key] = hit
                    meshes.append(hit)
                    done += 1
                    if done % 8 == 0 or done == total:
                        self.progress.emit(done, total, proto.name)
                result[proto.key] = meshes
            if not self._cancel:
                self.done.emit(result, time.time() - t0)
        except Exception as e:                       # noqa: BLE001
            if not self._cancel:
                self.failed.emit(f"{type(e).__name__}: {e}")


class ProcessLoader(QObject):
    """Load a STEP file in a child process so the UI stays alive.

    Also caches: the expensive STEP parse is keyed on (path, mtime, size), so
    re-opening the same file skips the child entirely and costs about a second.
    """
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    #: project root (the directory that CONTAINS the stepusd package)
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def __init__(self, step_path: str, parent=None):
        super().__init__(parent)
        self.step_path = os.path.abspath(step_path)
        self.proc: QProcess | None = None
        self._buf = ""
        self._tail = collections.deque(maxlen=12)
        self._reported = False
        st = os.stat(self.step_path)
        key = hashlib.sha1(
            f"{self.step_path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:16]
        # NOT self.ROOT: on a shared deployment the app folder is read-only,
        # and the cache is the one thing every load must be able to write.
        from .. import paths
        self.cache_dir = os.path.join(paths.cache_dir(), key)

    def _cache_ready(self) -> bool:
        return all(os.path.exists(os.path.join(self.cache_dir, f))
                   for f in (loadproc.SHAPES, loadproc.SCENE))

    def start(self):
        if self._cache_ready():
            self.progress.emit("loading cached geometry...")
            QTimer.singleShot(0, self._finish_from_cache)
            return
        self.progress.emit("reading STEP in a background process...")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(
            lambda e: self._fail(f"loader process error: {e}"))
        # The child runs `-m stepusd.loadproc`, so it must be able to import the
        # package. NEVER rely on the inherited working directory: opening a file
        # from another folder leaves the process cwd somewhere else entirely and
        # the child dies with ModuleNotFoundError. Pin both cwd and PYTHONPATH.
        self.proc.setWorkingDirectory(self.ROOT)
        env = QProcessEnvironment.systemEnvironment()
        existing = env.value("PYTHONPATH", "")
        env.insert("PYTHONPATH",
                   self.ROOT + (os.pathsep + existing if existing else ""))
        env.insert("PYTHONIOENCODING", "utf-8")   # child prints paths; console may be cp932
        self.proc.setProcessEnvironment(env)
        self.proc.start(sys.executable,
                        ["-m", "stepusd.loadproc", self.step_path, self.cache_dir])

    def cancel(self):
        # Mark it reported BEFORE killing. QProcess reports a deliberate kill as
        # errorOccurred(Crashed) and then finished() with a non-zero code, so
        # quitting the window during a load put a "loader process error:
        # ProcessError.Crashed" dialog on screen -- alarming, and about nothing.
        self._reported = True
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)

    def _fail(self, msg: str):
        if self._reported:
            return
        self._reported = True
        self.failed.emit(msg)

    def _on_output(self):
        self._buf += bytes(self.proc.readAllStandardOutput()).decode(
            "utf-8", "replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                self.progress.emit(line[9:])
                continue
            if line.startswith("ERROR "):
                self._fail(line[6:])
                continue
            # Anything else is a traceback / interpreter message. Keep it: it is
            # the only useful thing to show when the child dies unexpectedly.
            self._tail.append(line)

    def _on_finished(self, code, _status):
        if code != 0:
            detail = "\n".join(self._tail) if self._tail else ""
            self._fail(f"The STEP loader failed (exit code {code})."
                       + (f"\n\n{detail}" if detail else ""))
            return
        self._finish_from_cache()

    def _finish_from_cache(self):
        try:
            self.done.emit(loadproc.load_cache(self.cache_dir))
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")
