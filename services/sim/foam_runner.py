"""OpenFOAM invocation layer — the *only* module that knows native-vs-WSL.

The OpenFOAM CFD bridge (``services.sim.openfoam``) builds a case directory, runs a
sequence of OpenFOAM utilities (blockMesh, snappyHexMesh, simpleFoam, …) and parses the
results.  None of that cares *how* the executables are launched — that is this module's job.

Three runners are provided:

* :class:`NativeRunner` — a native-Windows OpenFOAM build (blueCFD-Core, an ESI/MinGW port
  that keeps the classic ``simpleFoam`` solver names).  This is the user-preferred target:
  no WSL hop, no path translation.  Detected from a vendored ``third_party/openfoam``, the
  ``CHUDWARE_OPENFOAM`` env var, or a ``C:\\Program Files\\blueCFD-Core*`` install.
* :class:`LinuxRunner` — OpenFOAM on a Linux/posix host.  Detected from (in order) an
  already-sourced environment (``simpleFoam`` on PATH), the ``CHUDWARE_OPENFOAM`` env var
  (an install root containing ``etc/bashrc`` OR a flat ``bin/`` layout like the
  conda-forge package), and the usual system/user install roots (``/usr/lib/openfoam``,
  ``/opt``, ``~/OpenFOAM``, ``~/openfoam*``, ``~/micromamba/envs/*``, ``~/.conda/envs/*``,
  ``~/miniconda3/envs/*``).  No root/sudo needed — a user-space tarball or conda env works.
* :class:`WslRunner` — OpenFOAM installed inside a WSL2 distro (``apt install openfoam2412``).
  Used as a verified fallback on Windows when no native build is present.  Case directories
  on the Windows filesystem are reached from Linux under ``/mnt/<drive>/…`` (via ``wslpath``).

LICENSE POSTURE (contracts/license_debt.md): OpenFOAM is GPL — every invocation in this
module is a **separate subprocess** (``subprocess.Popen`` of the stock executables); nothing
is linked, imported, vendored, or bundled.  Keep it that way.

Both expose the same small surface so case-generation / post-processing share 100% of the
logic:

    runner = get_runner()                 # NativeRunner | WslRunner | None
    if runner and runner.available():
        runner.run(case_dir, "blockMesh", [], on_line=print)
        runner.run(case_dir, "simpleFoam", ["-parallel"], on_line=print, np=8)

``run`` streams the executable's combined stdout/stderr line-by-line to ``on_line`` (so the
solver's residual history can drive the live progress bar) and raises
:class:`FoamError` on a non-zero exit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional

OnLine = Optional[Callable[[str], None]]


class FoamError(RuntimeError):
    """An OpenFOAM utility exited non-zero (or the runner could not launch it)."""

    def __init__(self, app: str, code: int, tail: str = ""):
        self.app = app
        self.code = code
        self.tail = tail
        super().__init__(f"OpenFOAM '{app}' failed (exit {code})" + (f": {tail}" if tail else ""))


# --------------------------------------------------------------------------- ABC


class FoamRunner(ABC):
    """Abstract OpenFOAM launcher.  Subclasses implement *how* to spawn a utility."""

    kind: str = "?"          # "native" | "wsl"
    version_str: str = ""    # populated by available()

    @abstractmethod
    def available(self) -> bool:
        """True if a working OpenFOAM is reachable through this runner (cached)."""

    @abstractmethod
    def run(self, case_dir: str, app: str, args: Optional[List[str]] = None,
            on_line: OnLine = None, np: int = 1, timeout: Optional[float] = None) -> None:
        """Run OpenFOAM utility ``app`` with ``args`` inside ``case_dir``.

        Streams combined stdout/stderr line by line to ``on_line``.  When ``np`` > 1 and the
        app supports it, the run is launched in parallel (``mpirun -np N app -parallel``).
        Raises :class:`FoamError` on non-zero exit.
        """

    def version(self) -> str:
        self.available()
        return self.version_str

    # -- shared helper: pump a Popen's output to on_line, keep a tail for errors --
    @staticmethod
    def _pump(proc: "subprocess.Popen[str]", app: str, on_line: OnLine,
              timeout: Optional[float]) -> None:
        tail: List[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:  # progress callbacks must never kill the run
                        pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise FoamError(app, -1, "timed out")
        t.join(timeout=5)
        if proc.returncode != 0:
            raise FoamError(app, proc.returncode or -1, "\n".join(tail[-12:]))


# --------------------------------------------------------------------------- WSL


def _win_drives_prefix(win_path: str) -> str:
    """Translate ``C:\\a\\b`` -> ``/mnt/c/a/b`` (fallback if wslpath is unavailable)."""
    p = win_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:]}"
    return p


class WslRunner(FoamRunner):
    """Run OpenFOAM inside a WSL2 distro.  Case dirs live on the Windows FS (``/mnt/...``)."""

    kind = "wsl"

    # Probed once, then cached.  bashrc differs by install (ESI vs Foundation vs version).
    _BASHRC_GLOBS = (
        "/usr/lib/openfoam/openfoam*/etc/bashrc",   # ESI apt (openfoam2412-default)
        "/opt/openfoam*/etc/bashrc",                # ESI tarball / older
        "/opt/OpenFOAM-*/etc/bashrc",               # Foundation tarball
    )

    def __init__(self, distro: Optional[str] = None):
        self.distro = distro            # None => default distro
        self._checked = False
        self._ok = False
        self._bashrc = ""

    # -- low-level: build a `wsl ... bash -lc` argv --
    def _wsl_argv(self, bash_cmd: str) -> List[str]:
        argv = ["wsl"]
        if self.distro:
            argv += ["-d", self.distro]
        argv += ["-e", "bash", "-lc", bash_cmd]
        return argv

    def _sourced(self, body: str) -> str:
        """Wrap a shell body so the OpenFOAM environment is sourced first."""
        return f"source {self._bashrc} >/dev/null 2>&1; {body}"

    def available(self) -> bool:
        if self._checked:
            return self._ok
        self._checked = True
        if shutil.which("wsl") is None:
            return False
        # locate a bashrc
        find = ("for g in /usr/lib/openfoam/openfoam*/etc/bashrc "
                "/opt/openfoam*/etc/bashrc /opt/OpenFOAM-*/etc/bashrc; do "
                "[ -f \"$g\" ] && { echo \"$g\"; break; }; done")
        try:
            r = subprocess.run(self._wsl_argv(find), capture_output=True, text=True, timeout=30)
        except Exception:
            return False
        bashrc = (r.stdout or "").strip().splitlines()
        if not bashrc:
            return False
        self._bashrc = bashrc[0]
        # confirm simpleFoam resolves once sourced, capture version
        probe = self._sourced("which simpleFoam >/dev/null 2>&1 && echo $WM_PROJECT_VERSION")
        try:
            r = subprocess.run(self._wsl_argv(probe), capture_output=True, text=True, timeout=30)
        except Exception:
            return False
        out = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and out:
            self._ok = True
            self.version_str = f"OpenFOAM {out[-1]} (WSL)"
        return self._ok

    def to_case_path(self, win_path: str) -> str:
        """Windows path -> WSL path via ``wslpath`` (fallback to /mnt heuristic)."""
        try:
            r = subprocess.run(self._wsl_argv(f"wslpath -u '{win_path}'"),
                               capture_output=True, text=True, timeout=20)
            p = (r.stdout or "").strip()
            if p:
                return p
        except Exception:
            pass
        return _win_drives_prefix(win_path)

    def run(self, case_dir: str, app: str, args: Optional[List[str]] = None,
            on_line: OnLine = None, np: int = 1, timeout: Optional[float] = None) -> None:
        if not self.available():
            raise FoamError(app, -1, "no OpenFOAM in WSL")
        args = args or []
        wsl_case = self.to_case_path(case_dir)
        argstr = " ".join(args)
        if np > 1:
            cmd = f"cd '{wsl_case}' && mpirun -np {np} {app} -parallel {argstr}"
        else:
            cmd = f"cd '{wsl_case}' && {app} {argstr}"
        proc = subprocess.Popen(
            self._wsl_argv(self._sourced(cmd)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._pump(proc, app, on_line, timeout)


# --------------------------------------------------------------------------- linux


class LinuxRunner(FoamRunner):
    """Run OpenFOAM natively on a Linux/posix host — strictly as subprocesses.

    Detection (no root needed, cached after the first probe):

      1. Already-sourced environment: ``simpleFoam`` resolvable on PATH.
      2. ``CHUDWARE_OPENFOAM`` env var: an install root — either a classic tree with
         ``etc/bashrc`` (openfoam.com tarball) or a flat prefix whose ``bin/`` holds the
         executables (conda-forge package, which also ships ``etc/bashrc``).
      3. Well-known roots, system and user-space::

            /usr/lib/openfoam/openfoam*   /opt/openfoam*   /opt/OpenFOAM-*
            ~/OpenFOAM/OpenFOAM-*         ~/openfoam*      ~/.local/openfoam*
            ~/.local/share/mamba/envs/*   ~/micromamba/envs/*   ~/.local/share/micromamba/envs/*
            ~/.conda/envs/*               ~/miniconda3/envs/*   ~/mambaforge/envs/*

    Every utility is launched as ``bash -c "source <bashrc>; cd <case> && app"`` (or
    directly when the environment is pre-sourced).  Nothing is linked or imported.
    """

    kind = "linux"

    def __init__(self, root: Optional[str] = None):
        self._root = root           # explicit install root (tests / CHUDWARE_OPENFOAM)
        self._checked = False
        self._ok = False
        self._bashrc = ""           # "" => rely on the already-sourced PATH

    @staticmethod
    def _bashrc_candidates(root: Optional[str] = None) -> List[Path]:
        if root:
            r = Path(root).expanduser()
            return [r / "etc" / "bashrc", *sorted(r.glob("OpenFOAM-*/etc/bashrc"))]
        home = Path.home()
        cands: List[Path] = []
        env = os.environ.get("CHUDWARE_OPENFOAM")
        if env:
            cands += LinuxRunner._bashrc_candidates(env)
        globs = (
            "/usr/lib/openfoam/openfoam*/etc/bashrc",
            "/opt/openfoam*/etc/bashrc",
            "/opt/OpenFOAM-*/etc/bashrc",
        )
        for g in globs:
            cands += sorted(Path("/").glob(g.lstrip("/")))
        for rel in ("OpenFOAM/OpenFOAM-*", "openfoam*", ".local/openfoam*",
                    ".local/share/mamba/envs/*", "micromamba/envs/*",
                    ".local/share/micromamba/envs/*", ".conda/envs/*",
                    "miniconda3/envs/*", "mambaforge/envs/*"):
            for d in sorted(home.glob(rel)):
                b = d / "etc" / "bashrc"
                if b.is_file():
                    cands.append(b)
        return [c for c in cands if c.is_file()]

    def _probe(self, bashrc: str) -> Optional[str]:
        """Return the version string if ``simpleFoam`` resolves with this bashrc ('' = PATH)."""
        pre = f"source '{bashrc}' >/dev/null 2>&1; " if bashrc else ""
        cmd = pre + "command -v simpleFoam >/dev/null 2>&1 && echo ${WM_PROJECT_VERSION:-unknown}"
        try:
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
        except Exception:
            return None
        out = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and out:
            return out[-1]
        return None

    def available(self) -> bool:
        if self._checked:
            return self._ok
        self._checked = True
        if os.name != "posix" or shutil.which("bash") is None:
            return False
        # 1. pre-sourced environment (only when no explicit root was requested)
        if self._root is None and shutil.which("simpleFoam") is not None:
            ver = self._probe("")
            if ver is not None:
                self._ok, self._bashrc = True, ""
                self.version_str = f"OpenFOAM {ver} (linux)"
                return True
        # 2./3. bashrc-based installs
        for b in self._bashrc_candidates(self._root):
            ver = self._probe(str(b))
            if ver is not None:
                self._ok, self._bashrc = True, str(b)
                self.version_str = f"OpenFOAM {ver} (linux, {b})"
                return True
        return False

    def to_case_path(self, path: str) -> str:
        return path                 # native posix: no translation

    def run(self, case_dir: str, app: str, args: Optional[List[str]] = None,
            on_line: OnLine = None, np: int = 1, timeout: Optional[float] = None) -> None:
        if not self.available():
            raise FoamError(app, -1, "no OpenFOAM on this Linux host")
        args = args or []
        argstr = " ".join(args)
        body = (f"cd '{case_dir}' && mpirun -np {np} {app} -parallel {argstr}" if np > 1
                else f"cd '{case_dir}' && {app} {argstr}")
        if self._bashrc:
            body = f"source '{self._bashrc}' >/dev/null 2>&1; {body}"
        proc = subprocess.Popen(
            ["bash", "-c", body],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._pump(proc, app, on_line, timeout)


# --------------------------------------------------------------------------- native


class NativeRunner(FoamRunner):
    """Run a native-Windows OpenFOAM build (blueCFD-Core, ESI/MinGW port).

    blueCFD ships its executables under ``<root>/OpenFOAM-<ver>/platforms/<arch>/bin`` and a
    MinGW runtime under ``<root>/mingw64/bin``.  We replicate the environment its ``setvars``
    script would set (``WM_PROJECT_DIR`` + PATH) in the child process, so we can call the
    ``.exe`` directly with ``cwd=case_dir`` instead of launching its interactive shell.
    """

    kind = "native"

    def __init__(self, root: Optional[str] = None):
        self._root = root
        self._checked = False
        self._ok = False
        self._bin_dirs: List[str] = []
        self._proj_dir = ""

    @staticmethod
    def _candidate_roots() -> List[Path]:
        roots: List[Path] = []
        here = Path(__file__).resolve()
        # repo third_party/openfoam (vendored)
        for up in here.parents:
            cand = up / "third_party" / "openfoam"
            if cand.exists():
                roots.append(cand)
                break
        env = os.environ.get("CHUDWARE_OPENFOAM")
        if env:
            roots.append(Path(env))
        for base in (r"C:\Program Files", r"C:\\"):
            b = Path(base)
            if b.exists():
                roots += sorted(b.glob("blueCFD-Core*"))
        return roots

    def _discover(self) -> bool:
        roots = [Path(self._root)] if self._root else self._candidate_roots()
        for root in roots:
            if not root.exists():
                continue
            # blueCFD nests an OpenFOAM-<ver> dir; vendored layouts may be flatter.
            proj_dirs = sorted(root.glob("OpenFOAM-*")) or [root]
            for proj in proj_dirs:
                exes = list(proj.glob("platforms/*/bin/simpleFoam.exe"))
                if not exes:
                    exes = list(proj.glob("**/bin/simpleFoam.exe"))
                if exes:
                    bindir = exes[0].parent
                    mingw = list(root.glob("**/mingw64/bin")) + list(root.glob("**/usr/bin"))
                    self._proj_dir = str(proj)
                    self._bin_dirs = [str(bindir)] + [str(m) for m in mingw]
                    return True
        return False

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["WM_PROJECT_DIR"] = self._proj_dir
        env["PATH"] = os.pathsep.join(self._bin_dirs + [env.get("PATH", "")])
        return env

    def available(self) -> bool:
        if self._checked:
            return self._ok
        self._checked = True
        if os.name != "nt":
            return False
        if not self._discover():
            return False
        self._ok = True
        ver = Path(self._proj_dir).name.replace("OpenFOAM-", "")
        self.version_str = f"OpenFOAM {ver} (native)"
        return True

    def to_case_path(self, win_path: str) -> str:
        return win_path        # native: no translation

    def run(self, case_dir: str, app: str, args: Optional[List[str]] = None,
            on_line: OnLine = None, np: int = 1, timeout: Optional[float] = None) -> None:
        if not self.available():
            raise FoamError(app, -1, "no native OpenFOAM")
        args = args or []
        exe = app if app.lower().endswith(".exe") else app + ".exe"
        if np > 1:
            argv = ["mpirun", "-np", str(np), exe, "-parallel", *args]
        else:
            argv = [exe, *args]
        proc = subprocess.Popen(
            argv, cwd=case_dir, env=self._child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._pump(proc, app, on_line, timeout)


# --------------------------------------------------------------------------- selection

_CACHED: Optional[FoamRunner] = None
_CACHE_SET = False


def get_runner(prefer: str = "native") -> Optional[FoamRunner]:
    """Return the best available runner, or ``None`` if no OpenFOAM.

    Windows: native blueCFD preferred, WSL fallback.  Linux/posix: :class:`LinuxRunner`.
    Cached after the first call.  ``prefer="wsl"`` flips the Windows order (tests / overrides).
    """
    global _CACHED, _CACHE_SET
    if _CACHE_SET:
        return _CACHED
    if os.name == "posix":
        order: List[FoamRunner] = [LinuxRunner()]
    else:
        native = NativeRunner()
        wsl = WslRunner()
        order = [native, wsl] if prefer != "wsl" else [wsl, native]
    chosen: Optional[FoamRunner] = None
    for r in order:
        try:
            if r.available():
                chosen = r
                break
        except Exception:
            continue
    _CACHED = chosen
    _CACHE_SET = True
    return chosen


def capabilities() -> dict:
    """Capability summary for the ``sim_caps`` RPC (host gates the solver selector on this)."""
    r = get_runner()
    return {
        "lbm": True,
        "openfoam": r is not None,
        "openfoam_kind": r.kind if r else None,
        "openfoam_version": r.version() if r else "",
    }


def reset_cache() -> None:
    """Drop the cached runner (tests / after provisioning a new install)."""
    global _CACHED, _CACHE_SET
    _CACHED = None
    _CACHE_SET = False
