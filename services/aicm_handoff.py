"""Python consumer side of the engine's AICM handoff (core/util/aicm_handoff.cpp).

The engine hands a published mesh/field to its callers as one of:

* ``aicm-shm://Local%5Cchudware_<key>`` (or a literal backslash) -- a Windows
  named file mapping;
* ``aicm-shm:///<name>`` -- a POSIX shared-memory object. Linux names it
  ``/chudware_p<pid>_<key>``; macOS caps shm names at 31 characters, so there it is
  ``/cwp<pid>_<key>`` or, for a longer key, ``/cwp<pid>_<16-hex digest>``. Only
  ``shm_open`` reaches it on every POSIX system (macOS has no ``/dev/shm``);
* a plain filesystem path -- the engine's temp-file fallback.

Every shm object is an 8-byte little-endian payload size followed by the payload.
The publisher owns the object's lifetime: a reader never unlinks it.
"""
from __future__ import annotations

import os
from urllib.parse import unquote

SHM_URI_PREFIX = "aicm-shm://"


def _split_payload(raw, what: str) -> bytes:
    if len(raw) < 8:
        raise ValueError(f"short AICM handoff object {what!r}")
    n = int.from_bytes(raw[:8], "little")
    if 8 + n > len(raw):
        raise ValueError(f"AICM handoff object {what!r} declares {n} bytes, holds {len(raw) - 8}")
    return bytes(raw[8:8 + n])


def _read_windows_mapping(name: str) -> bytes:
    import ctypes
    from ctypes import wintypes
    import mmap
    import sys

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    # mmap(tagname=...) creates a mapping if absent. Open and hold the publisher's
    # existing object first, so a missing handoff cannot become a fake empty one.
    borrowed = kernel32.OpenFileMappingW(0x0004, False, name)  # FILE_MAP_READ
    if not borrowed:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with mmap.mmap(-1, 8, tagname=name, access=mmap.ACCESS_READ) as header:
            n = int.from_bytes(header[:8], "little")
        if n > sys.maxsize - 8:
            raise ValueError(f"AICM handoff object {name!r} declares {n} bytes, too large to map")
        with mmap.mmap(-1, 8 + n, tagname=name, access=mmap.ACCESS_READ) as mapping:
            return _split_payload(mapping, name)
    finally:
        if not kernel32.CloseHandle(borrowed):
            raise ctypes.WinError(ctypes.get_last_error())


def _read_posix_shm(name: str) -> bytes:
    import ctypes
    import mmap

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        shm_open = libc.shm_open
    except AttributeError:
        # Older glibc exports shm_open from librt rather than libc.
        from ctypes.util import find_library

        librt = find_library("rt")
        if not librt:
            raise RuntimeError("POSIX shm_open is unavailable") from None
        shm_open = ctypes.CDLL(librt, use_errno=True).shm_open
    shm_open.argtypes = (ctypes.c_char_p, ctypes.c_int, ctypes.c_uint)
    shm_open.restype = ctypes.c_int
    name = "/" + name.lstrip("/")
    # Never attach with multiprocessing.SharedMemory: on Python < 3.13 even a
    # temporary register/unregister can steal a publisher's shared tracker entry.
    fd = shm_open(os.fsencode(name), os.O_RDONLY, 0)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), name)
    try:
        size = os.fstat(fd).st_size
        if size < 8:
            raise ValueError(f"short AICM handoff object {name!r}")
        with mmap.mmap(fd, size, access=mmap.ACCESS_READ) as mapping:
            return _split_payload(mapping, name)
    finally:
        os.close(fd)


def read_handoff(handle: str) -> bytes:
    """AICM payload bytes for an engine handoff URI or fallback file path."""
    if not handle:
        raise ValueError("empty AICM handoff")
    if handle.startswith(SHM_URI_PREFIX):
        name = unquote(handle[len(SHM_URI_PREFIX):], errors="strict")
        if not name.strip("/") or "\0" in name:
            raise ValueError(f"malformed AICM handoff URI {handle!r}")
        if os.name == "nt":
            return _read_windows_mapping(name)
        return _read_posix_shm(name)
    with open(handle, "rb") as f:
        return f.read()
