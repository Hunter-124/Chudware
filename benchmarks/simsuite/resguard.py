"""Resource guard for simsuite benches and sim tests.

Stdlib-only and cross-platform (Linux/Windows/macOS; uses psutil when present).
Computes a memory/CPU budget as a *fraction of this machine's resources* — never
absolute numbers — so the same suite runs safely on a 32 GB desktop, a 16 GB
laptop, or a big CI box, and can be tuned per-host with env vars:

    SIMSUITE_MEM_FRAC   fraction of total RAM the run may use   (default 0.5)
    SIMSUITE_MEM_CAP_GB absolute ceiling in GB, whichever is lower (default: none)
    SIMSUITE_HEADROOM_GB min RAM left for the OS/desktop          (default 6)
    SIMSUITE_CPU_FRAC   fraction of logical cores for solver pools (default 0.75)
    SIMSUITE_RLIMIT     POSIX RLIMIT_AS hard backstop, 1/0         (default 1)

Enforcement is layered:
  1. Solver thread pools are capped up front (OMP/BLAS env, set before the
     engine loads) so parallel solvers scale to the box.
  2. A watchdog thread polls the RSS of this process + all descendants
     (ccx/foam/ngspice children included). At 85% of budget it warns and sets
     a flag (`should_abort()`); at 100% it kills child processes and raises
     KeyboardInterrupt in the main thread so the runner records a module
     error and the OS stays alive.
  3. On POSIX, RLIMIT_AS (budget + slack) backstops native allocations the
     watchdog can't interrupt mid-solve. Children inherit it.

Bench modules can also size cases to the box:
    budget = resguard.install()          # idempotent; returns Budget
    if not resguard.fits(est_peak_gb=6): return skip_case(...)
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import NamedTuple

_GB = 1024 ** 3


class Budget(NamedTuple):
    mem_bytes: int      # hard memory budget for the whole process tree
    soft_bytes: int     # warn/flag threshold (85%)
    total_bytes: int    # machine RAM
    workers: int        # solver thread budget


# ---------------------------------------------------------------- memory info

def total_ram() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().total)
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong)
                for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                          "ullAvailExtendedVirtual")
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullTotalPhys)
    if sys.platform == "darwin":
        import subprocess
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
    return 8 * _GB  # conservative unknown-platform fallback


def available_ram() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    return total_ram() // 2  # pessimistic fallback


def _rss_tree() -> int:
    """RSS of this process plus all descendants, in bytes."""
    try:
        import psutil
        me = psutil.Process()
        rss = me.memory_info().rss
        for ch in me.children(recursive=True):
            try:
                rss += ch.memory_info().rss
            except Exception:
                pass
        return rss
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        # walk /proc once, building the descendant set of our pid
        procs: dict[int, tuple[int, int]] = {}  # pid -> (ppid, rss_bytes)
        for ent in os.listdir("/proc"):
            if not ent.isdigit():
                continue
            try:
                with open(f"/proc/{ent}/status") as fh:
                    ppid = rss = 0
                    for line in fh:
                        if line.startswith("PPid:"):
                            ppid = int(line.split()[1])
                        elif line.startswith("VmRSS:"):
                            rss = int(line.split()[1]) * 1024
                procs[int(ent)] = (ppid, rss)
            except Exception:
                continue
        mine = {os.getpid()}
        grew = True
        while grew:
            grew = False
            for pid, (ppid, _) in procs.items():
                if ppid in mine and pid not in mine:
                    mine.add(pid)
                    grew = True
        return sum(procs[p][1] for p in mine if p in procs)
    return 0  # unknown platform without psutil: watchdog degrades to no-op


# ---------------------------------------------------------------- budget calc

def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def compute_budget() -> Budget:
    total = total_ram()
    frac = _env_f("SIMSUITE_MEM_FRAC", 0.5)
    headroom = int(_env_f("SIMSUITE_HEADROOM_GB", 6.0) * _GB)
    budget = int(total * frac)
    cap_gb = _env_f("SIMSUITE_MEM_CAP_GB", 0.0)
    if cap_gb > 0:
        budget = min(budget, int(cap_gb * _GB))
    # never budget past what is actually free right now minus OS headroom —
    # concurrent guarded processes therefore self-limit collectively — but
    # keep a small floor so a briefly-loaded box doesn't starve tiny runs
    # into false aborts
    floor = min(3 * _GB, int(total * 0.4))
    budget = max(min(budget, max(_GB, available_ram() - headroom)), floor)
    cores = os.cpu_count() or 4
    workers = max(1, int(cores * _env_f("SIMSUITE_CPU_FRAC", 0.75)))
    return Budget(mem_bytes=budget, soft_bytes=int(budget * 0.85),
                  total_bytes=total, workers=workers)


# ---------------------------------------------------------------- enforcement

_state = {"watchdog": None, "budget": None, "soft_hit": False}


def should_abort() -> bool:
    return bool(_state["soft_hit"])


def check() -> None:
    """Raise between cases once the soft threshold has been crossed."""
    if _state["soft_hit"]:
        b = _state["budget"]
        raise RuntimeError(
            f"resguard: process tree crossed {b.soft_bytes / _GB:.1f} GB "
            f"(budget {b.mem_bytes / _GB:.1f} GB) — aborting remaining cases")


def fits(est_peak_gb: float) -> bool:
    """Would a case with this estimated peak fit the remaining budget?"""
    b: Budget = _state["budget"] or compute_budget()
    return _rss_tree() + est_peak_gb * _GB <= b.mem_bytes


class _Watchdog(threading.Thread):
    def __init__(self, budget: Budget, interval: float = 0.5):
        super().__init__(name="simsuite-resguard", daemon=True)
        self.budget = budget
        self.interval = interval

    def run(self) -> None:
        import _thread
        while True:
            time.sleep(self.interval)
            rss = _rss_tree()
            if not rss:
                return  # platform gives us nothing to measure
            if rss > self.budget.mem_bytes:
                sys.stderr.write(
                    f"[resguard] HARD LIMIT: tree RSS {rss / _GB:.1f} GB > "
                    f"budget {self.budget.mem_bytes / _GB:.1f} GB — "
                    f"killing children + interrupting run\n")
                self._kill_children()
                _state["soft_hit"] = True
                _thread.interrupt_main()
            elif rss > self.budget.soft_bytes and not _state["soft_hit"]:
                _state["soft_hit"] = True
                sys.stderr.write(
                    f"[resguard] soft limit: tree RSS {rss / _GB:.1f} GB > "
                    f"{self.budget.soft_bytes / _GB:.1f} GB — finishing current "
                    f"case, then aborting\n")

    @staticmethod
    def _kill_children() -> None:
        try:
            import psutil
            for ch in psutil.Process().children(recursive=True):
                try:
                    ch.kill()
                except Exception:
                    pass
            return
        except Exception:
            pass
        if hasattr(os, "killpg"):
            # last resort on POSIX without psutil: children share our group
            # only if callers didn't detach; harmless if none exist.
            import signal
            try:
                os.killpg(os.getpgid(0), signal.SIGTERM)
            except Exception:
                pass


def _apply_thread_env(workers: int) -> None:
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                "CHUDWARE_SIM_THREADS"):
        os.environ.setdefault(var, str(workers))


def _apply_rlimit(budget: Budget) -> None:
    if os.environ.get("SIMSUITE_RLIMIT", "1") != "1" or os.name != "posix":
        return
    try:
        import resource
        # generous slack over the RSS budget: RLIMIT_AS counts virtual address
        # space, which legitimately exceeds RSS (arenas, mapped libs)
        limit = budget.mem_bytes + 8 * _GB
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if soft == resource.RLIM_INFINITY or soft > limit:
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except Exception:
        pass


def install() -> Budget:
    """Cap solver threads, start the watchdog, arm the POSIX backstop.

    Idempotent — safe to call from every bench module and conftest.
    Must run before the engine/solver libraries spin up their pools.
    """
    if _state["watchdog"] is not None:
        return _state["budget"]
    budget = compute_budget()
    _state["budget"] = budget
    _apply_thread_env(budget.workers)
    _apply_rlimit(budget)
    wd = _Watchdog(budget)
    _state["watchdog"] = wd
    wd.start()
    sys.stderr.write(
        f"[resguard] budget: {budget.mem_bytes / _GB:.1f} GB RAM "
        f"(of {budget.total_bytes / _GB:.0f} GB), {budget.workers} solver threads\n")
    return budget
