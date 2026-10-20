"""Windows local byte pipes using ipc_private.h's owner + SYSTEM policy.

No TCP emulation: this module only creates real, remote-rejecting Windows pipes.
Connect/read/write use overlapped events and finite monotonic deadlines.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from functools import lru_cache
import math
import os
import secrets
import threading
import time

INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_NO_DATA = 232
ERROR_IO_PENDING = 997
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", w.LPVOID), ("attributes", w.DWORD)]


class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                ("offset", w.DWORD), ("offset_high", w.DWORD), ("event", w.HANDLE)]


class _Api:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows named pipes require Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        definitions = (
            (self.kernel, "GetCurrentProcess", [], w.HANDLE),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (self.kernel, "LocalFree", [w.LPVOID], w.LPVOID),
            (self.kernel, "CreateEventW", [w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            (self.kernel, "ResetEvent", [w.HANDLE], w.BOOL),
            (self.kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            (self.kernel, "CancelIoEx", [w.HANDLE, ctypes.POINTER(_Overlapped)], w.BOOL),
            (self.kernel, "GetOverlappedResult", [w.HANDLE, ctypes.POINTER(_Overlapped),
                ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL),
            (self.kernel, "CreateNamedPipeW", [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(_SecurityAttributes)], w.HANDLE),
            (self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (self.kernel, "WaitNamedPipeW", [w.LPCWSTR, w.DWORD], w.BOOL),
            (self.kernel, "ConnectNamedPipe", [w.HANDLE, ctypes.POINTER(_Overlapped)], w.BOOL),
            (self.kernel, "SetNamedPipeHandleState", [w.HANDLE, ctypes.POINTER(w.DWORD),
                w.LPVOID, w.LPVOID], w.BOOL),
            (self.kernel, "DisconnectNamedPipe", [w.HANDLE], w.BOOL),
            (self.kernel, "ReadFile", [w.HANDLE, w.LPVOID, w.DWORD,
                ctypes.POINTER(w.DWORD), ctypes.POINTER(_Overlapped)], w.BOOL),
            (self.kernel, "WriteFile", [w.HANDLE, w.LPVOID, w.DWORD,
                ctypes.POINTER(w.DWORD), ctypes.POINTER(_Overlapped)], w.BOOL),
            (self.kernel, "GetNamedPipeServerProcessId", [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
            (self.kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
            (self.advapi, "GetTokenInformation", [w.HANDLE, ctypes.c_int, w.LPVOID,
                w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.advapi, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
            (self.advapi, "EqualSid", [w.LPVOID, w.LPVOID], w.BOOL),
            (self.advapi, "IsWellKnownSid", [w.LPVOID, ctypes.c_int], w.BOOL),
            (self.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), w.LPVOID], w.BOOL),
        )
        for library, name, arguments, result in definitions:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def token_user(self, process):
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = w.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError("Windows owner identity unavailable")
            data = ctypes.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return data
        finally:
            self.kernel.CloseHandle(token)

    def verify_server(self, handle) -> None:
        pid = w.ULONG()
        if not self.kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ours = self.token_user(self.kernel.GetCurrentProcess())
            theirs = self.token_user(process)
            own_sid = _TokenUser.from_buffer(ours).sid
            server_sid = _TokenUser.from_buffer(theirs).sid
            if not (self.advapi.EqualSid(own_sid, server_sid) or
                    self.advapi.IsWellKnownSid(server_sid, 22)):
                raise PermissionError("Windows pipe server identity mismatch")
        finally:
            self.kernel.CloseHandle(process)


@lru_cache(maxsize=1)
def _api() -> _Api:
    return _Api()


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    """Process TokenUser SID, shared by daemon discovery and private control pipes."""
    api = _api()
    user = api.token_user(api.kernel.GetCurrentProcess())
    text = w.LPWSTR()
    if not api.advapi.ConvertSidToStringSidW(_TokenUser.from_buffer(user).sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        api.kernel.LocalFree(ctypes.cast(text, w.LPVOID))


class WindowsPipe:
    """Single-I/O-owner byte pipe; shutdown cancels before buffers/handles retire."""
    def __init__(self, handle) -> None:
        self._api = _api()
        self._handle = handle
        self._event = self._api.kernel.CreateEventW(None, True, False, None)
        if not self._event:
            error = ctypes.get_last_error()
            self._api.kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._timeout = 2.0
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._stopped = False

    def settimeout(self, timeout: float) -> None:
        self._timeout = max(0.001, timeout)

    def _io(self, operation, buffer=None, size=0, *, timeout=None) -> int:
        kernel = self._api.kernel
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        with self._io_lock:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Windows pipe I/O timed out")
                count = w.DWORD()
                overlapped = _Overlapped(event=self._event)
                with self._lock:
                    if self._stopped or self._handle is None:
                        raise OSError("Windows pipe closed")
                    handle = self._handle
                    if not kernel.ResetEvent(self._event):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if operation == "connect":
                        completed = kernel.ConnectNamedPipe(handle, ctypes.byref(overlapped))
                    else:
                        function = kernel.ReadFile if operation == "read" else kernel.WriteFile
                        completed = function(handle, buffer, size, ctypes.byref(count),
                                             ctypes.byref(overlapped))
                    error = 0 if completed else ctypes.get_last_error()
                    if operation == "connect" and error == ERROR_NO_DATA:
                        # The previous client disconnected before accept saw it.
                        if not kernel.DisconnectNamedPipe(handle):
                            raise ctypes.WinError(ctypes.get_last_error())
                        continue
                if operation == "connect" and error == ERROR_PIPE_CONNECTED:
                    return 0
                if error == ERROR_IO_PENDING:
                    remaining = max(0, deadline - time.monotonic())
                    wait = kernel.WaitForSingleObject(
                        self._event, min(0x7fffffff, math.ceil(remaining * 1000)))
                    if wait != 0:
                        wait_error = ctypes.get_last_error() if wait == 0xffffffff else 0
                        # Cancellation is asynchronous. Drain completion while the
                        # OVERLAPPED, event and caller's buffer are still alive.
                        kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                        kernel.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                                   ctypes.byref(count), True)
                        if wait_error:
                            raise ctypes.WinError(wait_error)
                        raise TimeoutError("Windows pipe I/O timed out")
                    completed = kernel.GetOverlappedResult(
                        handle, ctypes.byref(overlapped), ctypes.byref(count), False)
                    error = 0 if completed else ctypes.get_last_error()
                if completed:
                    return count.value
                if operation == "read" and error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA):
                    return 0
                raise ctypes.WinError(error)

    def accept(self, timeout: float = 0.2) -> None:
        self._io("connect", timeout=timeout)

    def recv(self, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        count = self._io("read", buffer, size)
        return buffer.raw[:count]

    def sendall(self, data: bytes) -> None:
        deadline = time.monotonic() + self._timeout
        buffer = ctypes.create_string_buffer(data)
        offset = 0
        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Windows pipe write timed out")
            count = self._io("write", ctypes.byref(buffer, offset), len(data) - offset, timeout=remaining)
            if count == 0:
                raise OSError("Windows pipe closed")
            offset += count

    def disconnect(self) -> None:
        with self._io_lock, self._lock:
            if self._handle is not None and not self._stopped:
                self._api.kernel.DisconnectNamedPipe(self._handle)

    def shutdown(self, _how=None) -> None:
        with self._lock:
            self._stopped = True
            if self._handle is not None:
                self._api.kernel.CancelIoEx(self._handle, None)

    def close(self) -> None:
        self.shutdown()
        # The I/O owner drains cancellation before we release kernel resources.
        with self._io_lock, self._lock:
            if self._handle is not None:
                self._api.kernel.CloseHandle(self._handle)
                self._handle = None
            if self._event is not None:
                self._api.kernel.CloseHandle(self._event)
                self._event = None

    @classmethod
    def connect(cls, endpoint: str, timeout: float = 2.0):
        if not endpoint.startswith("\\\\.\\pipe\\chudware-control-") or "\\" in endpoint[9:]:
            raise ValueError("invalid local control pipe endpoint")
        kernel = _api().kernel
        deadline = time.monotonic() + timeout
        while True:
            handle = kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3,
                                        0x40000000 | 0x00100000 | 0x00010000, None)
            if handle != INVALID_HANDLE:
                pipe = cls(handle)
                try:
                    pipe._api.verify_server(handle)
                    mode = w.DWORD(0)  # PIPE_WAIT, byte read mode; I/O is overlapped
                    if not kernel.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                except Exception:
                    pipe.close()
                    raise
                pipe.settimeout(max(0.001, deadline - time.monotonic()))
                return pipe
            error = ctypes.get_last_error()
            if error != ERROR_PIPE_BUSY:
                raise ctypes.WinError(error)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Windows pipe connect timed out")
            kernel.WaitNamedPipeW(endpoint, min(0x7fffffff, max(1, math.ceil(1000 * remaining))))


def create_private_pipes(count: int) -> tuple[str, list[WindowsPipe]]:
    """Precreate the entire bounded pool: a live first instance retains exclusivity."""
    api = _api()
    sid = current_user_sid()
    endpoint = "\\\\.\\pipe\\chudware-control-" + sid + "-" + secrets.token_hex(32)
    # Identical protected owner+SYSTEM policy to native PrivateSecurity.
    descriptor = w.LPVOID()
    if not api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    pipes = []
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        for index in range(count):
            handle = api.kernel.CreateNamedPipeW(endpoint,
                0x00000003 | 0x40000000 | (0x00080000 if index == 0 else 0),
                0x00000008, count, 65536, 65536, 0, ctypes.byref(attributes))
            if handle == INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            pipes.append(WindowsPipe(handle))
        return endpoint, pipes
    except Exception:
        for pipe in pipes:
            pipe.close()
        raise
    finally:
        api.kernel.LocalFree(descriptor)
