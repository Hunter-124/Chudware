"""Windows process identities and descendant snapshots, without probing unrelated tokens."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from functools import lru_cache
import os


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("executable", w.WCHAR * 260)]


@lru_cache(maxsize=1)
def _kernel():
    if os.name != "nt":
        raise OSError("native Windows process APIs required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateToolhelp32Snapshot": (w.HANDLE, [w.DWORD, w.DWORD]),
        "Process32FirstW": (w.BOOL, [w.HANDLE, ctypes.POINTER(_ProcessEntry)]),
        "Process32NextW": (w.BOOL, [w.HANDLE, ctypes.POINTER(_ProcessEntry)]),
        "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
        "GetProcessTimes": (w.BOOL, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4),
        "WaitForSingleObject": (w.DWORD, [w.HANDLE, w.DWORD]),
        "CloseHandle": (w.BOOL, [w.HANDLE]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    return kernel


def process_start(pid: int) -> int | None:
    """Exact FILETIME creation identity, or None after process exit; access errors propagate."""
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # limited query + synchronize
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return None
        raise ctypes.WinError(error)
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return None
        if wait != 258:  # WAIT_TIMEOUT: still running
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def process_tree(root: int, maximum: int) -> dict[int, tuple[int, int]]:
    """Return discoverable descendants as PID -> (parent, creation FILETIME).

    Toolhelp supplies the parent graph without opening protected system processes.
    Query identity only for this tree, fail closed on inaccessible descendants, and
    keep traversing the captured graph if an intermediate parent exits during sampling.
    This does not attest escaped desktop SDK tools; the caller retains that refusal.
    """
    kernel = _kernel()
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    entry = _ProcessEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if len(parents) >= maximum:
                raise RuntimeError("process_census_oversized")
            parents[entry.pid] = entry.parent
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES is the only successful terminal result
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)
    if root not in parents:
        raise RuntimeError("process_census_incomplete")
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    records: dict[int, tuple[int, int]] = {}
    visited: set[int] = set()
    pending = [root]
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        pending.extend(children.get(pid, ()))
        start = process_start(pid)
        if start is not None:
            records[pid] = (parents[pid], start)
    if root not in records:
        raise RuntimeError("process_census_incomplete")
    return records
