"""Print-only delivery boundary; firmware maintenance is never a print job.

Only inspectable UTF-8 text G-code is accepted. Binary G-code requires a decoder
before it can pass this boundary; a filename or a manifest hash cannot certify it.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

TEXT_GCODE_SUFFIXES = frozenset({".gcode", ".gco", ".g"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_COMMAND = re.compile(r"^\s*(?:N\s*\d+\s*)?M\s*(\d+)(?:\.\d+)?", re.IGNORECASE)
_MACRO = re.compile(r"^\s*(?:N\s*\d+\s*)?([A-Z_]+)\b", re.IGNORECASE)
# Marlin / RepRap SD writers, persistent settings changes, SPI image backup /
# restore, firmware update, and RepRapFirmware config / web-file upload commands.
_MAINTENANCE = frozenset({"28", "29", "500", "502", "559", "560", "993", "994", "997"})
_MACRO_MAINTENANCE = frozenset({"SAVE_CONFIG", "FIRMWARE_RESTART", "UPDATE_FIRMWARE"})


class UnsafePrintFile(ValueError):
    """A file cannot be delivered through a print-only interface."""


def validate_output_path(path: str | Path) -> Path:
    """Reject final aliases before resolving the containing directory.

    Existing directories are allowed for work directories. Parent directory
    aliases are normal on some platforms, so only the final entry is checked.
    """
    path = Path(path).expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePrintFile(f"Output path is a symbolic link: {path}")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise UnsafePrintFile(f"Output path has multiple hard links: {path}")
        elif not stat.S_ISDIR(info.st_mode):
            raise UnsafePrintFile(f"Output path is not a regular file or directory: {path}")
    return path.parent.resolve() / path.name


@contextmanager
def atomic_print_output(path: Path) -> Iterator[BinaryIO]:
    """Publish a print artifact without opening its destination for writing."""
    path = validate_output_path(path)
    if path.is_dir():
        raise UnsafePrintFile(f"Output path is a directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".chudware-print-", suffix=".part", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        validate_output_path(path)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def validate_print_filename(name: str) -> None:
    if PurePosixPath(name).suffix.lower() not in TEXT_GCODE_SUFFIXES:
        raise UnsafePrintFile(
            "Print delivery accepts only text .gcode, .gco or .g files; "
            "firmware images and binary G-code are not print jobs.")


def validate_print_line(raw: str, line_number: int) -> None:
    if _CONTROL.search(raw):
        raise UnsafePrintFile(f"Line {line_number}: binary/control bytes are not text G-code.")
    # Parenthesized and semicolon comments are not executable commands. Keep
    # display text (M117/M118) out of the command test as well.
    for line in raw.splitlines():
        code = re.sub(r"\([^)]*\)", "", line.split(";", 1)[0]).lstrip("\ufeff")
        command = _COMMAND.match(code)
        macro = _MACRO.match(code)
        number = (command[1].lstrip("0") or "0") if command else None
        if number in _MAINTENANCE:
            name = f"M{number}"
        elif macro and macro[1].upper() in _MACRO_MAINTENANCE:
            name = macro[1].upper()
        else:
            continue
        raise UnsafePrintFile(
            f"Line {line_number}: {name} is a firmware, persistent-settings or media-write "
            "command, not a print operation. Remove it and regenerate the print job; "
            "print-readiness overrides cannot authorize firmware maintenance.")


def validate_print_bytes(data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsafePrintFile("Binary/non-UTF-8 payloads cannot be delivered as text G-code.") from exc
    for line_number, raw in enumerate(text.split("\n"), 1):
        validate_print_line(raw, line_number)
