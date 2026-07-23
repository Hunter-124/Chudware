"""fs_tools.py — general-purpose filesystem + shell MCP tools (coding-harness surface).

The CAD/Chudware AI harness historically exposed only CAD-geometry ops and document-scoped
office_* readers. A tool-calling agent (services/agent/agent_turn.py run_turn — the DAG /
mission / subagent surface — plus external MCP clients like Claude Code) needs the *standard*
coding-harness capabilities to do project work: read/organize files on disk, search, and run
commands. These tools provide exactly that.

Families (server.py wires each via _reg.view(mcp, family)):
  * ``files`` — fs_read / fs_write / fs_edit / fs_list / fs_glob / fs_grep
  * ``shell`` — run_shell   (kept a SEPARATE family so it can be toggled off independently)

Envelope: ``{ok:true, …}`` | ``{ok:false, reason, hint}`` — never a traceback (mirrors
office_tools.py / plm_tools.py).

CONTEXT-SAFE READING (the point of fs_read): a raw file dump overflows a small model's
context. fs_read never returns unbounded text. It emits LINE-NUMBERED output, honours an
offset/limit window, caps total lines, per-line columns, and total bytes, and appends a
truncation footer stating the file's real length and how to page further. A path may also
carry a trailing ``:selector`` (design ported from the oh-my-pi read tool):
    ``:50``            from line 50 to the window cap
    ``:50-200``        inclusive range
    ``:50+150``        150 lines starting at 50
    ``:5-16,960-973``  multiple ranges
    ``:raw``           verbatim, no line numbers
Documents (.pdf/.docx/.pptx/.xlsx/.xlsm/.csv/.doc) route through the FROZEN
``office_tools.extract_office`` reader and are then paginated identically, so a 400-page PDF
never floods context either.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- envelope

def _err(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _ok(**fields: Any) -> dict:
    return {"ok": True, **fields}


# --------------------------------------------------------------------------- limits
# Defaults chosen so a single read/search/shell call cannot flood a small-model context.
DEFAULT_MAX_LINES = 400          # lines returned by fs_read when no explicit window
HARD_MAX_LINES = 4000            # ceiling the caller cannot exceed in one call
MAX_LINE_COLUMNS = 2000          # per-line character cap (wide/minified lines are clipped)
DEFAULT_MAX_BYTES = 96 * 1024    # byte ceiling on returned text
MAX_LIST_ENTRIES = 1000          # fs_list entry cap
MAX_GLOB_RESULTS = 1000          # fs_glob result cap
MAX_GREP_MATCHES = 500           # fs_grep match cap
MAX_GREP_FILES = 5000            # fs_grep files-scanned cap
GREP_MAX_FILE_BYTES = 8 * 1024 * 1024   # skip files larger than this in fs_grep
SHELL_DEFAULT_TIMEOUT_S = 60
SHELL_MAX_TIMEOUT_S = 900
SHELL_MAX_OUTPUT_BYTES = 96 * 1024

DOC_EXTS = {"pptx", "xlsx", "xlsm", "csv", "pdf", "doc", "docx"}

# Destructive shell shapes refused outright (ported/trimmed from the oh-my-pi bash tool's
# CRITICAL_BASH_PATTERNS). Intentionally tight — the cost of a false negative is a wiped host.
_CRITICAL_SHELL_PATTERNS = [
    re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:/|~|\$HOME|\*)(?:\s|$)", re.I),  # rm -rf / ~ * $HOME
    re.compile(r"\bsudo\s+rm\b", re.I),
    re.compile(r"\bmkfs(?:\.\w+)?\b", re.I),
    re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/(?:sd|nvme|hd)", re.I),
    re.compile(r"\bchmod\s+-R\s+[0-7]+\s+/(?:\s|$)", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;", re.I),  # fork bomb :(){ :|:& };:
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.I),
]


# --------------------------------------------------------------------------- selector grammar
# Ported from oh-my-pi path-utils.ts parseLineRanges. A range chunk is one of
# `N`, `N-M`, `N-`, `N+K`, `N..M` (`..` is a forgiving alias for `-`). Multiple
# chunks are comma-separated. Lines are 1-indexed.
_RANGE_CHUNK_RE = re.compile(r"^L?(\d+)(?:(\.\.|[-+])L?(\d*))?$", re.I)
_SEL_TAIL_RE = re.compile(
    r"^(?:raw|conflicts|\d+(?:(?:\.\.|[-+])\d*)?(?:,\d+(?:(?:\.\.|[-+])\d*)?)*)$", re.I
)


def _split_path_selector(raw: str) -> tuple[str, Optional[str]]:
    """Peel a trailing ``:selector`` off *raw* when the tail parses as a selector.

    Handles compound selectors (``path:5-6:raw``) by peeling the whole trailing RUN of
    colon-separated segments that each look like a selector (a range-list, ``raw``, or
    ``conflicts``). Conservative: a non-empty path must remain, so ordinary paths (and the
    rare POSIX path containing a colon) are untouched.
    """
    parts = raw.split(":")
    if len(parts) < 2:
        return raw, None
    i = len(parts)
    while i > 1 and _SEL_TAIL_RE.match(parts[i - 1]):
        i -= 1
    if i == len(parts):  # nothing selector-like at the tail
        return raw, None
    path = ":".join(parts[:i])
    if not path:
        return raw, None
    return path, ":".join(parts[i:])


def _parse_range_list(chunk: str) -> Optional[list[tuple[int, Optional[int]]]]:
    """Parse ONE comma-separated range-list segment (``5-16,960-973``) into ascending,
    merged (start, end|None) ranges. Returns None when it names no valid range."""
    ranges: list[tuple[int, Optional[int]]] = []
    for chunk_part in chunk.split(","):
        low = chunk_part.strip().lower()
        if low in ("raw", "conflicts", ""):
            continue
        m = _RANGE_CHUNK_RE.match(chunk_part.strip())
        if not m:
            return None
        start = int(m.group(1))
        if start < 1:
            return None
        sep = m.group(2)
        rhs = m.group(3)
        end: Optional[int]
        if sep == "+":
            if not rhs or int(rhs) < 1:
                return None
            end = start + int(rhs) - 1
        elif sep in ("-", ".."):
            end = int(rhs) if rhs else None
            if end is not None and end < start:
                return None
        else:
            end = None  # bare N → open-ended
        ranges.append((start, end))
    if not ranges:
        return None
    ranges.sort(key=lambda r: r[0])
    merged: list[tuple[int, Optional[int]]] = [ranges[0]]
    for start, end in ranges[1:]:
        ls, le = merged[-1]
        if le is None:
            continue  # open-ended absorbs the rest
        if start <= le + 1:
            merged[-1] = (ls, None if end is None else max(le, end))
        else:
            merged.append((start, end))
    return merged


def _parse_ranges(sel: str) -> Optional[list[tuple[int, Optional[int]]]]:
    """Parse a selector (possibly ``:``-compound like ``5-6:raw``) into ranges. Returns
    None for pure ``raw``/``conflicts`` / invalid selectors. Ported from oh-my-pi
    selectorLineRanges — the first segment that names ranges wins."""
    for seg in sel.split(":"):
        if seg.strip().lower() in ("raw", "conflicts", ""):
            continue
        ranges = _parse_range_list(seg)
        if ranges:
            return ranges
    return None


# --------------------------------------------------------------------------- text formatting

def _clip_line(text: str) -> str:
    if len(text) > MAX_LINE_COLUMNS:
        return text[:MAX_LINE_COLUMNS] + f"… [+{len(text) - MAX_LINE_COLUMNS} chars]"
    return text


def _window_lines(
    lines: list[str],
    ranges: Optional[list[tuple[int, Optional[int]]]],
    offset: int,
    limit: int,
    raw: bool,
) -> dict:
    """Select + number the requested lines under the byte/line caps. Returns the
    payload fields for fs_read: text, total_lines, shown_lines, truncated, next_offset."""
    total = len(lines)
    max_lines = min(limit if limit > 0 else DEFAULT_MAX_LINES, HARD_MAX_LINES)

    # Build the ordered list of 1-indexed line numbers to show.
    if ranges:
        wanted: list[int] = []
        for start, end in ranges:
            stop = total if end is None else min(end, total)
            wanted.extend(range(start, stop + 1))
        wanted = [n for n in wanted if 1 <= n <= total]
    else:
        start = max(offset, 1)
        wanted = list(range(start, total + 1))

    truncated = False
    if len(wanted) > max_lines:
        wanted = wanted[:max_lines]
        truncated = True

    out: list[str] = []
    used = 0
    shown = 0
    for n in wanted:
        body = lines[n - 1]
        rendered = _clip_line(body) if not raw else body
        piece = rendered if raw else f"{n:>6}\t{rendered}"
        used += len(piece) + 1
        if used > DEFAULT_MAX_BYTES and shown > 0:
            truncated = True
            break
        out.append(piece)
        shown += 1

    last_shown = wanted[shown - 1] if shown else (offset if offset > 0 else 0)
    next_offset = last_shown + 1 if (truncated and last_shown < total) else None
    text = "\n".join(out)
    if truncated and next_offset is not None:
        text += (
            f"\n… [truncated — showed {shown} of {total} lines; "
            f"continue with offset={next_offset} or path selector ':{next_offset}-']"
        )
    return {
        "text": text,
        "total_lines": total,
        "shown_lines": shown,
        "truncated": truncated,
        **({"next_offset": next_offset} if next_offset is not None else {}),
    }


def _kind_for(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


# --------------------------------------------------------------------------- read cores

def _read_document(path: str, ranges, offset: int, limit: int, raw: bool) -> dict:
    from services.mcp.office_tools import extract_office  # noqa: PLC0415

    res = extract_office(path)
    if not res.get("ok"):
        return _err(res.get("reason", "extract_failed"), res.get("hint", ""))
    text = res.get("text", "") or ""
    lines = text.split("\n")
    win = _window_lines(lines, ranges, offset, limit, raw)
    return _ok(path=path, kind=res.get("kind", _kind_for(path)), is_document=True, **win)


def _read_text(path: str, ranges, offset: int, limit: int, raw: bool) -> dict:
    p = Path(path)
    try:
        data = p.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return _err("read_failed", str(exc))
    # Binary sniff: NUL byte in the first 8 KiB → refuse to dump as text.
    if b"\x00" in data[:8192]:
        return _err("binary_file",
                    f"{p.name} looks binary ({len(data)} bytes); fs_read serves text/documents only")
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline shouldn't count as a phantom blank line
    win = _window_lines(lines, ranges, offset, limit, raw)
    return _ok(path=str(p), kind=_kind_for(path) or "text", is_document=False, **win)


def _list_dir(path: str, show_hidden: bool) -> dict:
    p = Path(path)
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except Exception as exc:  # noqa: BLE001
        return _err("list_failed", str(exc))
    rows = []
    for e in entries:
        if not show_hidden and e.name.startswith("."):
            continue
        if len(rows) >= MAX_LIST_ENTRIES:
            break
        try:
            is_dir = e.is_dir()
            size = e.stat().st_size if not is_dir else 0
        except OSError:
            is_dir, size = False, 0
        rows.append({"name": e.name + ("/" if is_dir else ""), "dir": is_dir, "size": size})
    return _ok(path=str(p), entries=rows, count=len(rows))


# =========================================================================== registration

def register_fs_tools(mcp) -> None:
    """Register the ``files`` family: fs_read / fs_write / fs_edit / fs_list / fs_glob / fs_grep."""

    @mcp.tool()
    def fs_read(path: str, offset: int = 0, limit: int = 0, raw: bool = False) -> dict:
        """Read a text file, directory, or document with CONTEXT-SAFE, line-numbered output.

        Never dumps unbounded text. Returns at most a windowed slice with a truncation
        footer telling you the file's real length and how to page on.

        path   : file/dir path; may carry a trailing selector — ':50' (from line 50),
                 ':50-200' (inclusive), ':50+150' (150 lines from 50), ':5-16,960-973'
                 (multi-range), ':raw' (verbatim, no line numbers). A directory lists its
                 entries. A document (.pdf/.docx/.pptx/.xlsx/.xlsm/.csv/.doc) is extracted
                 to text (via office_extract) and paginated the same way.
        offset : 1-indexed start line when no selector is given (default: from the top).
        limit  : max lines to return (default 400, hard cap 4000).
        raw    : omit line numbers / column clipping.

        Returns {ok, path, kind, text, total_lines, shown_lines, truncated, next_offset?}.
        """
        clean, sel = _split_path_selector(path)
        ranges = _parse_ranges(sel) if sel else None
        if sel and sel.lower().find("raw") >= 0:
            raw = True
        p = Path(clean)
        if not p.exists():
            return _err("not_found", f"no such path: {clean}")
        if p.is_dir():
            return _list_dir(clean, show_hidden=raw)
        if _kind_for(clean) in DOC_EXTS:
            return _read_document(clean, ranges, offset, limit, raw)
        return _read_text(clean, ranges, offset, limit, raw)

    @mcp.tool()
    def fs_list(path: str = ".", show_hidden: bool = False) -> dict:
        """List a directory's entries (name, dir flag, size). Capped at 1000 entries.
        Risk: read."""
        p = Path(path)
        if not p.exists():
            return _err("not_found", f"no such path: {path}")
        if not p.is_dir():
            return _err("not_a_directory", f"{path} is not a directory")
        return _list_dir(path, show_hidden)

    @mcp.tool()
    def fs_glob(pattern: str, path: str = ".", limit: int = 0) -> dict:
        """Find paths matching a glob (e.g. 'src/**/*.py'). Returns matches newest-first.
        Capped at 1000. Risk: read."""
        base = Path(path)
        if not base.exists():
            return _err("not_found", f"no such path: {path}")
        cap = min(limit if limit > 0 else MAX_GLOB_RESULTS, MAX_GLOB_RESULTS)
        try:
            hits = [str(m) for m in base.glob(pattern)]
        except Exception as exc:  # noqa: BLE001
            return _err("glob_failed", str(exc))
        try:
            hits.sort(key=lambda m: Path(m).stat().st_mtime, reverse=True)
        except OSError:
            hits.sort()
        truncated = len(hits) > cap
        return _ok(pattern=pattern, matches=hits[:cap], count=min(len(hits), cap),
                   truncated=truncated)

    @mcp.tool()
    def fs_grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
                max_matches: int = 0) -> dict:
        """Regex-search files under *path* (optionally filtered by *glob*, e.g. '*.py').
        Returns {file, line, text} matches, capped at 500. Risk: read."""
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as exc:
            return _err("bad_pattern", str(exc))
        base = Path(path)
        if not base.exists():
            return _err("not_found", f"no such path: {path}")
        cap = min(max_matches if max_matches > 0 else MAX_GREP_MATCHES, MAX_GREP_MATCHES)
        files: list[Path] = [base] if base.is_file() else \
            [p for p in base.rglob(glob or "*") if p.is_file()]
        matches: list[dict] = []
        scanned = 0
        truncated = False
        for fp in files:
            if scanned >= MAX_GREP_FILES:
                truncated = True
                break
            scanned += 1
            try:
                if fp.stat().st_size > GREP_MAX_FILE_BYTES:
                    continue
                with fp.open("r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append({"file": str(fp), "line": i,
                                            "text": _clip_line(line.rstrip("\n"))})
                            if len(matches) >= cap:
                                truncated = True
                                break
            except (OSError, UnicodeError):
                continue
            if len(matches) >= cap:
                break
        return _ok(pattern=pattern, matches=matches, count=len(matches),
                   files_scanned=scanned, truncated=truncated)

    @mcp.tool()
    def fs_write(path: str, content: str, make_dirs: bool = True) -> dict:
        """Create or OVERWRITE a text file with *content*. Parent dirs are created by
        default. Returns {ok, path, bytes}. Risk: write."""
        p = Path(path)
        try:
            if make_dirs and p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            p.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            return _err("write_failed", str(exc))
        return _ok(path=str(p), bytes=len(data))

    @mcp.tool()
    def fs_edit(path: str, old: str, new: str, replace_all: bool = False) -> dict:
        """Replace text in an existing file. By default *old* MUST occur exactly once
        (a safe, unambiguous edit); set replace_all=True to replace every occurrence.
        Returns {ok, path, replacements}. Risk: write."""
        p = Path(path)
        if not p.is_file():
            return _err("not_found", f"no such file: {path}")
        if old == "":
            return _err("empty_old", "`old` must be a non-empty string to locate the edit")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return _err("read_failed", str(exc))
        occurrences = text.count(old)
        if occurrences == 0:
            return _err("not_found_in_file", "`old` string does not appear in the file")
        if occurrences > 1 and not replace_all:
            return _err("ambiguous",
                        f"`old` appears {occurrences}× — pass replace_all=True or give a "
                        "longer unique snippet")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            p.write_text(updated, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return _err("write_failed", str(exc))
        return _ok(path=str(p), replacements=(occurrences if replace_all else 1))


def register_shell_tools(mcp) -> None:
    """Register the ``shell`` family: run_shell (kept separate so it toggles independently)."""

    @mcp.tool()
    def run_shell(command: str, cwd: str = "", timeout_s: int = SHELL_DEFAULT_TIMEOUT_S) -> dict:
        """Run a shell command and capture combined stdout+stderr (byte-capped).

        Refuses obviously destructive commands (rm -rf /, mkfs, dd of=/dev/*, fork bombs,
        shutdown/reboot, …). Output over ~96 KB is truncated. Returns
        {ok, exit_code, output, truncated, timed_out}. Risk: write.
        """
        if not command.strip():
            return _err("empty_command", "command is empty")
        for rx in _CRITICAL_SHELL_PATTERNS:
            if rx.search(command):
                return _err("blocked",
                            "command matches a destructive-pattern guard and was refused")
        timeout = max(1, min(int(timeout_s) if timeout_s else SHELL_DEFAULT_TIMEOUT_S,
                             SHELL_MAX_TIMEOUT_S))
        workdir = cwd or None
        if workdir and not Path(workdir).is_dir():
            return _err("bad_cwd", f"cwd is not a directory: {workdir}")
        try:
            proc = subprocess.run(
                command, shell=True, cwd=workdir, capture_output=True,
                timeout=timeout, text=True, errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            return _ok(exit_code=124, timed_out=True, truncated=len(partial) > SHELL_MAX_OUTPUT_BYTES,
                       output=partial[:SHELL_MAX_OUTPUT_BYTES])
        except Exception as exc:  # noqa: BLE001
            return _err("exec_failed", str(exc))
        output = (proc.stdout or "") + (proc.stderr or "")
        truncated = len(output) > SHELL_MAX_OUTPUT_BYTES
        if truncated:
            output = output[:SHELL_MAX_OUTPUT_BYTES] + "\n… [output truncated]"
        return _ok(exit_code=proc.returncode, timed_out=False, truncated=truncated,
                   output=output)

