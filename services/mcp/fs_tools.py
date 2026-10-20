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
office_tools.py / plm_tools.py). A ``not_found`` also carries ``did_you_mean`` (near-miss
suggestions, incl. the ``X`` vs ``X+`` native-container trap) and folds the top suggestion
into ``hint``. CAD/geometry files (.step+, .step, .stl, …) are never dumped: fs_read returns
their ``model_card`` preview (summary + a labelled render) and points at import_part +
describe_part/measure_part/inspect_part; ``browse_models`` is the thumbnail explorer.

CONTEXT-SAFE READING (the point of fs_read): a raw file dump overflows a small model's
context. fs_read never returns unbounded text. It emits LINE-NUMBERED output, honours an
offset/limit window, caps total lines, per-line columns, and the page's JSON-escaped bytes
(``READ_PAGE_BYTES``, under the host-tool result cap), and appends a truncation footer
stating the file's real length and how to page further. Raw pages carry no footer; they
report ``byte_offset``/``bytes``/``total_bytes`` and ``next_byte_offset``, so consecutive
raw pages join to the exact text. A path may also
carry a trailing ``:selector`` (design ported from the oh-my-pi read tool):
    ``:50``            from line 50 to the window cap
    ``:50-200``        inclusive range
    ``:50+150``        150 lines starting at 50
    ``:5-16,960-973``  multiple ranges
    ``:raw``           verbatim, no line numbers
Documents (.pdf/.docx/.pptx/.xlsx/.xlsm/.csv/.doc) route through the FROZEN
``office_tools.extract_office`` reader and are then paginated identically, so a 400-page PDF
never floods context either.

Text scans consume at most 64 MiB in finite chunks while retaining only one response
page; files changing during the scan refuse. Directory traversals and search input/output
have explicit work budgets. Regex compilation/search runs in a disposable readonly actor
child with a two-second per-tool deadline, not in the serving worker's GIL. Shell pipes
are drained under a byte budget and the command tree is stopped at budget/deadline refusal.

SCOPING (the "limited cases" the base tooling is deliberately narrowed for):
  * A fixed, always-on credential-directory block applies to EVERY tool below (read and
    write alike) — ~/.ssh, ~/.aws, ~/.gnupg, cloud CLI credential stores, .netrc, etc. are
    refused outright regardless of any allow-list or env override; see ``_sensitive_block``.
  * ``CHUDWARE_FS_ROOT`` is the MANDATORY workspace jail for every tool below, read and
    write alike (3aaee658 hardening): paths outside that root are refused. The DAG/mission
    runner sets this to the active project directory before dispatching a role so a role
    acting "like an engineer" stays inside the project it was asked to work in, not the
    whole filesystem. Unset/invalid root fails closed (``workspace_root_unavailable``).
  * A write refused because it lands inside a FIRMWARE project (fw_new_project scaffolds
    under firmware_root(), which is never inside a workspace) is still refused — the jail
    does not widen — but the envelope is ``use_fw_sources`` and names
    ``fw_sources(project_id, write={...})``, the tool that can author those sources.
  * The one read-only jail carve-out: READ-class tools (fs_read/fs_list/fs_glob/fs_grep)
    may read files inside a REGISTERED firmware project root, because fw_sources'
    contract is "write through fw_sources, read it back with fs_read" and those
    tool-managed roots deliberately live outside every workspace. Writes there stay
    refused, and the credential denylist still applies.
"""
from __future__ import annotations

import bisect
import codecs
import contextvars
import difflib
import fnmatch
import heapq
import itertools
import json
import os
import re
import select
import signal
import subprocess
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from services.agent.request_context import in_cell, root_value, sandbox_argv

# --------------------------------------------------------------------------- scoping

# Directories/files that are NEVER reachable through these tools.  The check is
# applied to the fully resolved target and to every descendant discovered by a
# recursive operation, so a symlink or an innocuous parent cannot hide a secret.
_SENSITIVE_DIR_NAMES = (
    ".ssh", ".aws", ".gnupg", ".gcloud", ".azure", ".kube", ".docker",
    "keyrings", "credentials",
)
_SENSITIVE_FILE_NAMES = (
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", "id_rsa", "id_ed25519",
)


def _is_sensitive_path(resolved: Path) -> bool:
    """Check the credential denylist on a fully resolved path, without a workspace jail."""
    return resolved.name in _SENSITIVE_FILE_NAMES or any(
        part in _SENSITIVE_DIR_NAMES for part in resolved.parts
    )


def _workspace_root() -> Optional[Path]:
    """Return the canonical mandatory filesystem jail, or ``None`` to fail closed."""
    raw = root_value("CHUDWARE_FS_ROOT").strip()
    if not raw:
        return None
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return root if root.is_dir() else None


def _firmware_project_redirect(raw_path: str) -> Optional[str]:
    """Hint text when *raw_path* belongs to the separate firmware project store."""
    try:
        from services.mcp.firmware_tools import project_for_path  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    hit = project_for_path(raw_path)
    if hit is None:
        return None
    pid, _root, rel = hit
    target = rel or "src/main.c"
    return (f'{raw_path} is inside firmware project {pid} — fs_write cannot reach it, but '
            f'fw_sources(project_id="{pid}", write={{"{target}": "<file content>"}}) '
            "writes it AND registers it so the next fw_build compiles it")


def _inside_firmware_project(resolved: Path) -> bool:
    """True when *resolved* lives inside a registered firmware project root."""
    try:
        from services.mcp.firmware_tools import project_for_path  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    try:
        return project_for_path(str(resolved)) is not None
    except Exception:  # noqa: BLE001
        return False


def _exists_or_unreachable(resolved: Path) -> bool:
    """``resolved.exists()``, but a parent we may not stat through is "gone", not a crash.

    ``Path.exists()`` only swallows ENOENT/ENOTDIR/EBADF/ELOOP; every other errno
    propagates. A directory the caller cannot traverse (mode 0o000, a foreign-uid
    tree, an autofs mount that refuses) therefore turned a routine miss into a
    ``PermissionError`` escaping the tool boundary, where the whole fs_* contract is
    "answer an envelope, never raise". Unreachable is indistinguishable from absent
    from this side of the syscall, and both answers are ``not_found`` — the caller
    cannot open it either way.
    """
    try:
        return resolved.exists()
    except OSError:
        return False


def _canonical_target(raw_path: str, *, allow_missing: bool = False,
                      read: bool = False) -> tuple[Optional[Path], Optional[dict]]:
    """Resolve *raw_path* once and enforce the mandatory root and secret denylist.

    Every tool class is root-jailed (3aaee658 hardening). ``read=True``
    (fs_read/fs_list/fs_glob/fs_grep) additionally admits paths inside a
    REGISTERED firmware project root — fw_sources' contract is "write through
    fw_sources, read it back with fs_read" and those tool-managed roots live
    outside every workspace by design. The credential denylist always applies.
    """
    root = _workspace_root()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        if root is None:
            return None, _err("workspace_root_unavailable",
                              "CHUDWARE_FS_ROOT must name an existing directory")
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return None, _err("invalid_path", str(exc))
    if not allow_missing and not _exists_or_unreachable(resolved):
        return None, _err_not_found(str(candidate))
    inside_root = root is not None and (resolved == root or root in resolved.parents)
    if not inside_root and not (read and _inside_firmware_project(resolved)):
        if root is None:
            return None, _err("workspace_root_unavailable",
                              "CHUDWARE_FS_ROOT must name an existing directory")
        firmware = _firmware_project_redirect(raw_path)
        if firmware:
            return None, _err("use_fw_sources", firmware)
        return None, _err("outside_workspace_root",
                          f"{raw_path} resolves outside the active project root ({root})")
    if _is_sensitive_path(resolved):
        return None, _err("blocked_sensitive_path",
                          f"{raw_path} resolves into a credential/key location")
    return resolved, None


def _sensitive_block(raw_path: str) -> Optional[dict]:
    """Compatibility wrapper for callers that only need the policy result."""
    _target, blocked = _canonical_target(raw_path, allow_missing=True)
    return blocked


def _root_block(raw_path: str) -> Optional[dict]:
    """Compatibility wrapper for write and shell callers."""
    _target, blocked = _canonical_target(raw_path, allow_missing=True)
    return blocked

# --------------------------------------------------------------------------- envelope

def _err(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _ok(**fields: Any) -> dict:
    return {"ok": True, **fields}


# --------------------------------------------------------------------------- near-miss recovery
# A misspelled path must never be a dead end: not_found carries did_you_mean suggestions
# AND folds the top one into hint, so a model that only reads hint still recovers.
NEAR_MISS_MAX_ENTRIES = 5000        # dir scan cap — a huge dir degrades to no suggestions
NEAR_MISS_CUTOFF = 0.6              # difflib similarity floor
NEAR_MISS_MAX_SUGGESTIONS = 5


def _dir_entry_names(directory: Path) -> list[str]:
    """Best-effort list of entry NAMES in *directory*. Never raises — an unreadable or
    huge directory degrades to fewer/zero names instead of an exception."""
    names: list[str] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) >= NEAR_MISS_MAX_ENTRIES:
                    break
    except Exception:  # noqa: BLE001
        pass
    return names


def _near_misses(raw_path: str) -> list[str]:
    """Existing paths near a MISSING *raw_path*, most-likely-first.

    Probes Chudware's own dual-format trap first — the exact ``+``-suffixed sibling
    (``X`` → ``X+`` native part container) or the de-suffixed original (``X+`` → ``X``)
    — then difflib close matches over the parent's entries. When the parent itself is
    missing, walks up to the nearest existing ancestor and close-matches the first
    missing component instead, so a mistyped folder name is diagnosed too. Never raises,
    never lists entries from a _sensitive_block location, and caps work (NEAR_MISS_*)."""
    suggestions: list[str] = []
    try:
        if _sensitive_block(raw_path) is not None:
            return suggestions
        target = Path(raw_path).expanduser()
        seen: set[str] = set()

        def _add(candidate: Path) -> None:
            text = str(candidate)
            if text not in seen and _sensitive_block(text) is None:
                seen.add(text)
                suggestions.append(text)

        # 1) The native-container +/- trap, probed exactly (our own most likely typo).
        probe = target.parent / (
            target.name[:-1] if target.name.endswith("+") else target.name + "+")
        try:
            if probe.exists():
                _add(probe)
        except Exception:  # noqa: BLE001
            pass

        # 2) Close matches over the parent — or, when the parent chain is broken, over
        #    the nearest existing ancestor for the first missing component.
        probe_dir, probe_name = target.parent, target.name
        try:
            while not probe_dir.is_dir():
                parent = probe_dir.parent
                if parent == probe_dir:  # filesystem root reached, nothing exists
                    return suggestions
                probe_name = probe_dir.name
                probe_dir = parent
        except Exception:  # noqa: BLE001
            return suggestions
        if probe_dir != target.parent and _sensitive_block(str(probe_dir)) is not None:
            return suggestions  # listing this ancestor would leak a credential location
        entries = _dir_entry_names(probe_dir)
        if entries:
            by_lower: dict[str, str] = {}
            for entry in entries:
                by_lower.setdefault(entry.lower(), entry)
            close = difflib.get_close_matches(
                probe_name.lower(), list(by_lower),
                n=NEAR_MISS_MAX_SUGGESTIONS, cutoff=NEAR_MISS_CUTOFF)
            for key in close:
                _add(probe_dir / by_lower[key])
    except Exception:  # noqa: BLE001
        pass
    return suggestions[:NEAR_MISS_MAX_SUGGESTIONS]


def _err_not_found(raw_path: str, what: str = "path") -> dict:
    """not_found envelope with near-miss recovery: a did_you_mean list of existing
    neighbours, with the top suggestion folded into hint so a model that only reads
    hint still recovers."""
    hint = f"no such {what}: {raw_path}"
    near = _near_misses(raw_path)
    if not near:
        return _err("not_found", hint)
    hint += f"; did you mean '{near[0]}'?"
    if len(near) > 1:
        hint += f" (also: {', '.join(repr(n) for n in near[1:])})"
    return {"ok": False, "reason": "not_found", "hint": hint, "did_you_mean": near}


# --------------------------------------------------------------------------- limits
# Defaults chosen so a single read/search/shell call cannot flood a small-model context.
DEFAULT_MAX_LINES = 400          # lines returned by fs_read when no explicit window
HARD_MAX_LINES = 4000            # ceiling the caller cannot exceed in one call
MAX_LINE_COLUMNS = 2000          # per-line character cap (wide/minified lines are clipped)
#: Budget for the text of one fs_read page, in UTF-8 bytes of its JSON-escaped form (the
#: form the text takes inside the tool result). The online agent's host-tool envelope caps
#: a result's text at 46 000 bytes (``chudcode_tools._RESULT_TEXT_BYTES``) and shortens
#: anything larger, which cut c250's raw JSON read mid-string. A page of this size plus the
#: other result fields stays under that cap, so fs_read pages and is never cut.
READ_PAGE_BYTES = 32 * 1024
MAX_LIST_ENTRIES = 1000          # fs_list entry cap
MAX_GLOB_RESULTS = 1000          # fs_glob result cap
MAX_GREP_MATCHES = 500           # fs_grep match cap
MAX_GREP_FILES = 5000            # fs_grep files-scanned cap
GREP_MAX_FILE_BYTES = 8 * 1024 * 1024   # skip files larger than this in fs_grep
READ_MAX_INPUT_BYTES = 64 * 1024 * 1024
IO_CHUNK_BYTES = 64 * 1024
FS_MAX_SCAN_ENTRIES = 20000
FS_MAX_SCAN_DEPTH = 64
FS_SCAN_DEADLINE_S = 10.0
FS_RESPONSE_BYTES = 32 * 1024
GREP_MAX_INPUT_BYTES = 64 * 1024 * 1024
GREP_SEARCH_DEADLINE_S = 2.0
GREP_MAX_PATTERN_BYTES = 4096
SHELL_DEFAULT_TIMEOUT_S = 60
SHELL_MAX_TIMEOUT_S = 24 * 60 * 60
SHELL_MAX_OUTPUT_BYTES = 96 * 1024

DOC_EXTS = {
    "pptx", "ppt", "odp", "xlsx", "xlsm", "xls", "ods",
    "csv", "pdf", "doc", "docx", "odt",
}

# CAD/geometry files fs_read must NOT try to dump as text — the right reader is
# import_part(path=...) which returns a doc_id for describe_part / measure_part /
# inspect_part. Binary containers ALWAYS redirect; plain-text exchange formats redirect
# by default but honour an explicit raw=True escape (the text is genuinely readable).
CAD_BINARY_EXTS = {
    "step+",   # Chudware native part container (ZIP)
    "draw+",   # Chudware standalone editable drawing container (ZIP)
    "cam+",    # Chudware native CAM container (ZIP)
    "stl", "3mf", "glb", "3dm", "sldprt", "sldasm", "f3d", "ipt", "iam", "dwg",
}
CAD_TEXT_EXTS = {
    "step", "stp", "iges", "igs", "brep", "obj", "ply", "gltf", "dxf",
}
CAD_EXTS = CAD_BINARY_EXTS | CAD_TEXT_EXTS


def _cad_redirect(path: str, kind: str) -> dict:
    """fs_read of a CAD file: its card, a labelled thumbnail, and the route to the reader.

    fs_read serves text/documents; CAD geometry is edited and measured through
    import_part (returns a doc_id) and the describe/measure/inspect readers. The card
    (model_card: name, kind, features, size, bbox) is read straight from the file and a
    native part's thumbnail is the engine's product shot (model_thumbs, shared with the
    GUI's PLM thumbnail cache), so a glance never costs an import. Only an unreadable file
    is a failure; a preview is a successful read."""
    name = Path(path).name
    if kind == "dxf":
        hint = (f"{name} is a 2D CAD drawing — open it with import_part(path=\"{path}\") "
                "(imports as wires and returns a doc_id), then read it with describe_part / "
                "measure_part / inspect_part; DXF is plain text, so if you truly need the "
                "raw file text pass raw=True to fs_read")
    elif kind in CAD_TEXT_EXTS:
        hint = (f"{name} is a CAD/geometry exchange file — open it with "
                f"import_part(path=\"{path}\") which returns a doc_id, then read the model "
                "with describe_part / measure_part / inspect_part; this format is plain "
                "text, so if you truly need the raw file text pass raw=True to fs_read")
    else:
        hint = (f"{name} is a binary CAD/geometry container — fs_read shows its preview "
                f"card, not its bytes; open it with import_part(path=\"{path}\") which "
                "returns a doc_id, then read the model with describe_part / measure_part / "
                "inspect_part")
    from .model_card import model_card

    card = model_card(path)
    if card.get("ok") is False or card.get("error"):
        return {**_err("use_import_part", hint), "card": card}
    out = _ok(path=path, kind="cad_preview", is_document=False, open_with="import_part",
              card=card, hint=hint)
    preview = _preview_image(name, card)
    if preview:
        out["preview"] = preview
    return out


#: Wall-clock budget for rendering missing thumbnails in one call; the rest are marked
#: pending (the renders already done are cached, so a repeat call continues).
PREVIEW_RENDER_BUDGET_S = 45.0


_ARCHIVE_DIRS = frozenset({"archive", "archives", "archived", "backup", "backups", "old",
                           "deprecated", "depricated", "trash", "recovered", "autosave",
                           "autosaves"})


def _archived(rel: Path) -> bool:
    """True when a folder on *rel*'s path reads as retired copies (archive/, backup/, …)."""
    return any(part.lower() in _ARCHIVE_DIRS or "archive" in part.lower()
               or "backup" in part.lower() for part in rel.parts[:-1])


def _image_ref(path: str) -> dict:
    """The reference the Chudcode adapter turns into an image block for the model."""
    return {"kind": "image", "path": path, "mime": "image/png"}


def _preview_image(label: str, card: dict) -> Optional[dict]:
    try:
        from .model_thumbs import build_tiles, single_tile, sheet_dir

        tiles, _ = build_tiles([(label, card)], render=True,
                               deadline=time.monotonic() + PREVIEW_RENDER_BUDGET_S)
        if not tiles[0].get("thumb"):
            return None
        return _image_ref(single_tile(tiles[0], sheet_dir()))
    except Exception:  # noqa: BLE001 - the card alone is still a useful preview
        return None


#: Cards are cheap (cached, read from the file) but a directory of large assemblies
#: still costs; a listing summarises this many CAD files and names the rest.
MAX_LIST_CARDS = 60


def _card_summary(path: str) -> Optional[str]:
    try:
        from .model_card import summary_line

        return summary_line(path)
    except Exception:  # noqa: BLE001 - a listing never fails on one bad file
        return None


def _reader_for(name: str) -> str:
    """The tool that can actually READ *name*, or "" when fs_read is the right reader.

    Listings carry this so a model never has to discover the redirect the expensive way:
    the live desktop runs show fs_read being called on a ``.step+`` straight out of an
    fs_list result, burning a call and surfacing a red failure card for what is only a
    routing hint.
    """
    kind = _kind_for(name)
    if kind in CAD_EXTS:
        return "import_part"
    if kind in DOC_EXTS:
        return "fs_read"
    return ""

# Destructive shell shapes refused outright (ported/trimmed from the oh-my-pi bash tool's
# CRITICAL_BASH_PATTERNS). Intentionally tight — the cost of a false negative is a wiped host.
_CRITICAL_SHELL_PATTERNS = [
    re.compile(
        r"\brm\s+(?=(?:--?[a-z-]+\s+)*(?:-[a-z]*[rf][a-z]*|--(?:recursive|force))\s+)"
        r"(?:--?[a-z-]+\s+)*(?:--\s+)?(?:[^\s;|&<>]+\s+)*"
        r"(?:/(?:\*|(?:bin|boot|dev|etc|home|lib|lib64|media|mnt|opt|proc|root|run|"
        r"sbin|srv|sys|tmp|usr|var)(?:/\*?)?)?|~(?:/\*?)?|"
        r"\$HOME(?:/\*?)?|\$\{HOME\}(?:/\*?)?)(?=$|[\s;|&<>])",
        re.I,
    ),
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


def _json_cost(text: str) -> int:
    """UTF-8 bytes of *text* as the body of a JSON string (quotes excluded)."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _fit_prefix(text: str, budget: int) -> int:
    """Length of the longest prefix of *text* whose JSON cost fits *budget*."""
    low, high = 0, min(len(text), max(budget, 0))
    while low < high:
        mid = (low + high + 1) // 2
        if _json_cost(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def _line_starts(lines: list[str], final_eol: bool) -> tuple[list[int], int]:
    """UTF-8 byte offset of each line's start, and the byte length of the whole text."""
    starts: list[int] = []
    pos = 0
    last = len(lines) - 1
    for index, line in enumerate(lines):
        starts.append(pos)
        pos += len(line.encode("utf-8")) + (1 if index < last or final_eol else 0)
    return starts, pos


class _ByteOffsetOutOfRange(ValueError):
    """A raw read asked for a byte_offset past the end of the text."""


def _window_lines(
    lines: list[str],
    ranges: Optional[list[tuple[int, Optional[int]]]],
    offset: int,
    limit: int,
    raw: bool,
    final_eol: bool = False,
    byte_offset: Optional[int] = None,
) -> dict:
    """Select + number the requested lines under the byte/line caps. Returns the
    payload fields for fs_read: text, offset, total_lines, shown_lines, truncated,
    next_offset, plus byte_offset, bytes, total_bytes, next_byte_offset for raw reads.

    *lines* are the physical lines split on ``\\n`` (terminator removed). Raw output
    re-attaches each selected line's own ``\\n`` — every line but the last has one, the last
    only when *final_eol* — so a raw slice is the source text verbatim (CRLF keeps its
    ``\\r`` in the line body; a file without a final newline stays without one).

    A page holds at most ``READ_PAGE_BYTES`` of JSON-escaped text. It ends at a line
    boundary, except when the first line alone is larger than a page: then the page ends
    inside that line, ``next_offset`` is absent and ``next_byte_offset`` continues it.
    Raw text carries no footer, so the texts of consecutive raw pages join to the file.
    *byte_offset* (raw only, caller-validated) starts the page at that UTF-8 byte of the
    text, moved back to a character boundary."""
    total = len(lines)
    if limit > 0:
        max_lines = min(limit, HARD_MAX_LINES)
    else:
        max_lines = HARD_MAX_LINES if byte_offset is not None else DEFAULT_MAX_LINES
    starts, total_bytes = _line_starts(lines, final_eol) if raw else ([], 0)
    skip_bytes = 0
    skip_chars = 0

    # Build the ordered list of 1-indexed line numbers to show.
    if byte_offset is not None:
        if byte_offset > total_bytes:
            raise _ByteOffsetOutOfRange(
                f"byte_offset={byte_offset} is past the end. The text has {total_bytes} "
                "bytes. Use a byte_offset from 0 to that size."
            )
        wanted: list[int] = []
        if byte_offset < total_bytes:
            first = bisect.bisect_right(starts, byte_offset)
            encoded = lines[first - 1].encode("utf-8")
            skip_bytes = byte_offset - starts[first - 1]
            while 0 < skip_bytes < len(encoded) and encoded[skip_bytes] & 0xC0 == 0x80:
                skip_bytes -= 1
            skip_chars = len(encoded[:skip_bytes].decode("utf-8"))
            wanted = list(itertools.islice(range(first, total + 1), max_lines + 1))
    elif ranges:
        wanted = list(itertools.islice(
            (number for start, end in ranges
             for number in range(max(start, 1), (total if end is None else min(end, total)) + 1)),
            max_lines + 1))
    else:
        start = max(offset, 1)
        wanted = list(itertools.islice(range(start, total + 1), max_lines + 1))

    truncated = False
    if len(wanted) > max_lines:
        wanted = wanted[:max_lines]
        truncated = True

    out: list[str] = []
    used = 0
    cut = False
    for n in wanted:
        body = lines[n - 1]
        if raw:
            if not out and skip_chars:
                body = body[skip_chars:]
            piece = body + ("\n" if n < total or final_eol else "")
            cost = _json_cost(piece)
        else:
            piece = f"{n:>6}\t{_clip_line(body)}"
            cost = _json_cost(piece) + 2  # the escaped "\n" that joins numbered lines
        if used + cost > READ_PAGE_BYTES:
            truncated = True
            if not out:  # one line larger than a page: end the page inside it
                out.append(piece[:_fit_prefix(piece, READ_PAGE_BYTES)])
                cut = True
            break
        out.append(piece)
        used += cost
    shown = len(out)

    last_shown = wanted[shown - 1] if shown else (offset if offset > 0 else 0)
    next_offset = (
        last_shown + 1 if (truncated and not cut and last_shown < total) else None
    )
    text = "".join(out) if raw else "\n".join(out)
    if not raw and truncated and next_offset is not None:
        text += (
            f"\n… [truncated — showed {shown} of {total} lines; "
            f"continue with offset={next_offset} or path selector ':{next_offset}-']"
        )
    win: dict[str, Any] = {
        "text": text,
        "offset": wanted[0] if shown else max(offset, 1),
        "total_lines": total,
        "shown_lines": shown,
        "truncated": truncated,
        **({"next_offset": next_offset} if next_offset is not None else {}),
    }
    if not raw:
        return win
    page_start = starts[wanted[0] - 1] + skip_bytes if shown else total_bytes
    page_end = (
        starts[last_shown - 1] + (skip_bytes if shown == 1 else 0)
        + len(out[-1].encode("utf-8"))
        if shown else page_start
    )
    win.update(byte_offset=page_start, bytes=len(text.encode("utf-8")),
               total_bytes=total_bytes)
    if truncated and page_end < total_bytes:
        win["next_byte_offset"] = page_end
        win["hint"] = (
            f"This page holds {win['bytes']} of {total_bytes} bytes. To read on, call "
            f"fs_read with raw=true and byte_offset={page_end}. The texts of consecutive "
            "pages join with no gap and no overlap."
        )
    return win


def _kind_for(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


# --------------------------------------------------------------------------- read cores

def _has_content_lines(lines: list[str], kind: str) -> bool:
    """True when the extracted text carries at least one real content line. The extractors
    prefix generated headers ('# Page N' / '# Slide N' / '# sheet-name'), so a document
    whose extraction is ONLY those headers (scanned/image-only, no text layer) is
    effectively empty."""
    header_kinds = {"pptx", "pdf", "xlsx", "xlsm"}
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if kind in header_kinds and s.startswith("# "):
            continue
        return True
    return False


def _read_document(path: str, ranges, offset: int, limit: int, raw: bool,
                   byte_offset: Optional[int] = None) -> dict:
    from services.mcp.office_tools import extract_office  # noqa: PLC0415

    res = extract_office(path)
    if not res.get("ok"):
        return _err(res.get("reason", "extract_failed"), res.get("hint", ""))
    kind = res.get("kind", _kind_for(path))
    text = res.get("text", "") or ""
    lines = text.split("\n")
    try:
        win = _window_lines(lines, ranges, offset, limit, raw, byte_offset=byte_offset)
    except _ByteOffsetOutOfRange as exc:
        return _err("byte_offset_out_of_range", str(exc))
    if not _has_content_lines(lines, kind):
        win["hint"] = ("extracted text is empty — the document may be scanned/image-only "
                       "or have no text layer; OCR it first to get usable text")
    return _ok(path=path, kind=kind, is_document=True, **win)


def _open_read_file(path: Path):
    """Open only a regular, admitted file; never block on a FIFO or follow a new alias."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name == "posix":
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:-1]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(path.name, flags, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("only regular files can be read")
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _file_unchanged(before, after) -> bool:
    return (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) == (
        after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev)


def _read_text(path: str, ranges, offset: int, limit: int, raw: bool,
               byte_offset: Optional[int] = None) -> dict:
    p = Path(path)
    max_lines = min(limit, HARD_MAX_LINES) if limit > 0 else (
        HARD_MAX_LINES if byte_offset is not None else DEFAULT_MAX_LINES)
    out: list[str] = []
    starts: list[int] = []
    numbers: list[int] = []
    used = wanted = total = position = 0
    line_start = line_length = 0
    prefix = ""
    page_closed = cut = False
    first_page_start = 0
    last_eol = False
    range_index = 0

    def selected(number: int) -> bool:
        nonlocal range_index
        if byte_offset is not None:
            return position > byte_offset
        if not ranges:
            return number >= max(offset, 1)
        while range_index < len(ranges):
            start, end = ranges[range_index]
            if end is not None and number > end:
                range_index += 1
                continue
            return number >= start
        return False

    def consume(piece: str, eol: bool) -> None:
        nonlocal position, prefix, line_length, total, line_start, last_eol
        nonlocal wanted, used, page_closed, cut, first_page_start
        encoded = piece.encode("utf-8")
        piece_start = position
        position += len(encoded)
        line_length += len(piece)
        # Retain only a possible page prefix, not a whole physical line.
        if not page_closed:
            keep = piece
            if byte_offset is not None and piece_start < byte_offset:
                skip = min(byte_offset - piece_start, len(encoded))
                while 0 < skip < len(encoded) and encoded[skip] & 0xC0 == 0x80:
                    skip -= 1
                keep = encoded[skip:].decode("utf-8")
                if not prefix:
                    first_page_start = piece_start + skip
            elif byte_offset is not None and not prefix:
                first_page_start = piece_start
            prefix += keep[:max(0, READ_PAGE_BYTES + 4 - len(prefix))]
        if not eol:
            last_eol = False
            return
        total += 1
        last_eol = piece.endswith("\n")
        if selected(total):
            wanted += 1
            if len(out) >= max_lines:
                page_closed = True
            if not page_closed:
                if raw:
                    formatted = prefix
                else:
                    body = prefix[:-1] if last_eol and line_length == len(prefix) else prefix
                    length = line_length - int(last_eol)
                    body = body[:MAX_LINE_COLUMNS]
                    if length > MAX_LINE_COLUMNS:
                        body += f"… [+{length - MAX_LINE_COLUMNS} chars]"
                    formatted = f"{total:>6}\t{body}"
                cost = _json_cost(formatted) + (0 if raw else 2)
                incomplete = raw and line_length > len(prefix) and byte_offset is None
                if byte_offset is not None:
                    start_at = max(line_start, first_page_start)
                    incomplete = len(formatted.encode("utf-8")) < position - start_at
                if used + cost > READ_PAGE_BYTES or incomplete:
                    page_closed = True
                    if not out:
                        formatted = formatted[:_fit_prefix(formatted, READ_PAGE_BYTES)]
                        cut = True
                    else:
                        formatted = ""
                if formatted or (raw and not line_length):
                    out.append(formatted)
                    numbers.append(total)
                    starts.append(max(line_start, first_page_start) if byte_offset is not None else line_start)
                    used += _json_cost(formatted) + (0 if raw else 2)
        prefix = ""
        line_length = 0
        line_start = position

    try:
        with _open_read_file(p) as fh:
            before = os.fstat(fh.fileno())
            if before.st_size > READ_MAX_INPUT_BYTES:
                return _err("read_budget_exceeded",
                            f"Text scan exceeds {READ_MAX_INPUT_BYTES} input bytes; narrow the file.")
            remaining = before.st_size
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            sniff = b""
            while remaining:
                data = fh.read(min(IO_CHUNK_BYTES, remaining))
                if not data:
                    return _err("file_changed", "File shrank while being read; retry a stable file.")
                remaining -= len(data)
                if len(sniff) < 8192:
                    sniff += data[:8192 - len(sniff)]
                    if b"\x00" in sniff:
                        return _err("binary_file", f"{p.name} looks binary; fs_read serves text only")
                text = decoder.decode(data, final=not remaining)
                at = 0
                while at < len(text):
                    end = text.find("\n", at)
                    stop = end + 1 if end >= 0 else len(text)
                    consume(text[at:stop], end >= 0)
                    at = stop
            if line_length:
                consume("", True)
            if not _file_unchanged(before, os.fstat(fh.fileno())):
                return _err("file_changed", "File changed while being read; retry a stable file.")
    except (OSError, ValueError) as exc:
        return _err("read_failed", str(exc))
    if byte_offset is not None and byte_offset > position:
        return _err("byte_offset_out_of_range",
                    f"byte_offset={byte_offset} is past the end. The text has {position} bytes.")
    shown = len(out)
    truncated = wanted > shown or cut
    win = dict(text="".join(out) if raw else "\n".join(out),
               offset=numbers[0] if shown else max(offset, 1), total_lines=total,
               shown_lines=shown, truncated=truncated)
    if truncated and not cut and shown and numbers[-1] < total:
        win["next_offset"] = numbers[-1] + 1
        if not raw:
            win["text"] += (
                f"\n… [truncated — showed {shown} of {total} lines; continue with "
                f"offset={win['next_offset']} or path selector ':{win['next_offset']}-']")
    if raw:
        page_start = starts[0] if shown else position
        page_end = starts[-1] + len(out[-1].encode("utf-8")) if shown else page_start
        win.update(byte_offset=page_start, bytes=len(win["text"].encode("utf-8")),
                   total_bytes=position)
        if truncated and page_end < position:
            win["next_byte_offset"] = page_end
            win["hint"] = f"Continue with raw=true and byte_offset={page_end}; join page texts."
    return _ok(path=str(p), kind=_kind_for(path) or "text", is_document=False, **win)


class _FSBudgetError(RuntimeError):
    def __init__(self, reason: str, hint: str):
        super().__init__(hint)
        self.reason = reason
        self.hint = hint


def _bounded_response(result: dict) -> dict:
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > FS_RESPONSE_BYTES:
        return _err("response_budget_exceeded", "Response exceeds 32 KiB; narrow path or lower the limit.")
    return result


def _iter_scoped_paths(base: Path, pattern: str, *, recursive: bool = False,
                       deadline: Optional[float] = None, canonical: bool = True):
    """Lazy, bounded glob traversal; no directory/path list is materialized."""
    if len(pattern.encode("utf-8")) > GREP_MAX_PATTERN_BYTES:
        raise _FSBudgetError("pattern_budget_exceeded", "Glob pattern exceeds 4096 bytes.")
    if not pattern or pattern.startswith(("/", "\\")) or ".." in Path(pattern).parts:
        raise _FSBudgetError("invalid_pattern", "Use a relative glob without parent traversal.")
    parts = tuple(part for part in pattern.replace(os.sep, "/").split("/") if part and part != ".")
    if len(parts) > FS_MAX_SCAN_DEPTH:
        raise _FSBudgetError("pattern_budget_exceeded", "Glob pattern has too many components.")
    directories_only = pattern.endswith(("/", os.sep))
    if not parts:
        yield base
        return
    if recursive:
        parts = ("**", *parts)
    deadline = deadline if deadline is not None else time.monotonic() + FS_SCAN_DEADLINE_S
    seen = 0

    def closure(states):
        expanded = set(states)
        for index in range(len(parts)):
            if index in expanded and parts[index] == "**":
                expanded.add(index + 1)
        return expanded

    def walk(directory: Path, states, depth: int):
        nonlocal seen
        if depth > FS_MAX_SCAN_DEPTH:
            raise _FSBudgetError("scan_budget_exceeded", "Directory depth exceeds the scan budget.")
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise _FSBudgetError("scan_deadline_exceeded", "Directory scan deadline expired; narrow path.")
                seen += 1
                if seen > FS_MAX_SCAN_ENTRIES:
                    raise _FSBudgetError("scan_budget_exceeded", "Directory entry budget exhausted; narrow path.")
                next_states = set()
                for index in states:
                    if index < len(parts):
                        if parts[index] == "**":
                            next_states.add(index)
                        elif fnmatch.fnmatch(entry.name, parts[index]):
                            next_states.add(index + 1)
                next_states = closure(next_states)
                if not next_states:
                    continue
                candidate = Path(entry.path)
                resolved, blocked = _canonical_target(str(candidate), read=True)
                if blocked or resolved is None:
                    continue
                if len(parts) in next_states and (not directories_only or resolved.is_dir()):
                    yield resolved if canonical else candidate
                if entry.is_dir(follow_symlinks=False) and any(index < len(parts) for index in next_states):
                    yield from walk(resolved, next_states, depth + 1)

    yield from walk(base, closure({0}), 0)


def _list_dir(path: str, show_hidden: bool) -> dict:
    p = Path(path)
    try:
        candidates = _iter_scoped_paths(p, "*", canonical=False)
        entries = heapq.nsmallest(
            MAX_LIST_ENTRIES + 1,
            (entry for entry in candidates if show_hidden or not entry.name.startswith(".")),
            key=lambda entry: (entry.is_file(), entry.name.lower()))
    except _FSBudgetError as exc:
        return _err(exc.reason, exc.hint)
    except (OSError, ValueError) as exc:
        return _err("list_failed", str(exc))
    rows = []
    carded = 0
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
        row = {"name": e.name + ("/" if is_dir else ""), "dir": is_dir, "size": size}
        if not is_dir:
            reader = _reader_for(e.name)
            if reader == "import_part":
                row["open_with"] = "import_part"
                if carded < MAX_LIST_CARDS:
                    summary = _card_summary(str(e))
                    if summary:
                        row["card"] = summary
                        carded += 1
        rows.append(row)
    geometry = sorted({r["name"] for r in rows if r.get("open_with") == "import_part"})
    out = _ok(path=str(p), entries=rows, count=len(rows), truncated=len(entries) > MAX_LIST_ENTRIES)
    if geometry:
        out["note"] = (
            f"{len(geometry)} CAD/geometry file(s) here; each row's card summarises it. "
            "browse_models(path) shows them as labelled thumbnail sheets; to edit or "
            'measure one, open it with import_part(path="...") for a doc_id'
        )
    return _bounded_response(out)


# =========================================================================== registration

def register_fs_tools(mcp) -> None:
    """Register the ``files`` family: fs_read / fs_write / fs_edit / fs_list / fs_glob / fs_grep."""

    @mcp.tool()
    def fs_read(path: str, offset: int = 0, limit: int = 0, raw: bool = False,
                byte_offset: Optional[int] = None) -> dict:
        """Read a text file, directory, or document with CONTEXT-SAFE, line-numbered output.

        Never dumps unbounded text. Each call returns one page of at most about 32 KB of
        text. Text input is streamed under a 64 MiB scan budget; larger or changing files
        refuse with read_budget_exceeded/file_changed instead of an incomplete success.
        A successful page says the file's real length and how to page on.

        path        : file/dir path; may carry a trailing selector — ':50' (from line 50),
                      ':50-200' (inclusive), ':50+150' (150 lines from 50), ':5-16,960-973'
                      (multi-range), ':raw' (verbatim, no line numbers). A directory lists
                      its entries. A document (.pdf/.docx/.doc/.odt/.pptx/.ppt/.odp/.xlsx/
                      .xlsm/.xls/.ods/.csv) is extracted to text (via office_extract) and
                      paginated the same way. A CAD/geometry
                      file (.draw+/.step+/.step/.stp/.iges/.stl/.3mf/.obj/…) is NOT dumped —
                      you get its preview card (kind, features, bbox) and a labelled render,
                      both read from the file; open it with import_part(path=...) and read
                      the model via describe_part / measure_part / inspect_part (plain-text
                      formats honour raw=True as an explicit escape).
        offset      : 1-indexed start line when no selector is given (default: from the top).
        limit       : max lines to return (default 400, hard cap 4000; 4000 when
                      byte_offset is set).
        raw         : return the text verbatim: no line numbers, no column clip, no footer.
        byte_offset : start a raw page at this UTF-8 byte of the text (0 = start). Sets
                      raw=True. Do not use it with offset or a line selector.

        Returns {ok, path, kind, text, offset, total_lines, shown_lines, truncated,
        next_offset?}. A raw read also returns byte_offset, bytes and total_bytes; when
        truncated, it also returns next_byte_offset and hint. A page ends at a line end,
        but a line longer than a page is split: then only next_byte_offset continues it.

        To read a whole file (for example, to json.loads it), call with raw=True and
        byte_offset=0. While truncated is true, call again with
        byte_offset=next_byte_offset. Join the text of all pages. Do not parse one page
        of a larger file.

        A result that is still too large for the tool budget is shortened. Each cut string
        then ends with a "…[cut: …]" marker, and _truncated.full_result names a JSON file
        that holds the full result.
        """
        if byte_offset is not None:
            if byte_offset < 0:
                return _err("invalid_byte_offset",
                            f"byte_offset={byte_offset} is negative. Use 0 or more.")
            if offset:
                return _err("conflicting_offsets",
                            "Use offset (lines) or byte_offset (bytes), not both.")
        clean, sel = _split_path_selector(path)
        p, blocked = _canonical_target(clean, read=True)
        if blocked:
            return blocked
        assert p is not None
        ranges = _parse_ranges(sel) if sel else None
        if ranges and byte_offset is not None:
            return _err("conflicting_offsets",
                        "Use a line selector or byte_offset, not both. "
                        "Remove the line range from the path.")
        if sel and sel.lower().find("raw") >= 0:
            raw = True
        if p.is_dir():
            return _list_dir(str(p), show_hidden=raw)
        if byte_offset is not None:
            raw = True
        kind = _kind_for(str(p))
        if kind in CAD_EXTS and (kind in CAD_BINARY_EXTS or not raw):
            return _cad_redirect(str(p), kind)
        if kind in DOC_EXTS:
            return _read_document(str(p), ranges, offset, limit, raw, byte_offset)
        return _read_text(str(p), ranges, offset, limit, raw, byte_offset)

    @mcp.tool()
    def fs_list(path: str = ".", show_hidden: bool = False) -> dict:
        """List a directory's entries (name, dir flag, size). Capped at 1000 entries.

        A CAD/geometry file (.step+/.step/.stl/.dxf/…) is tagged ``open_with:
        "import_part"`` and summarised in ``note`` — those are NOT readable with fs_read;
        call import_part(path=...) for a doc_id and read the model with describe_part /
        measure_part / inspect_part. Risk: read.
        """
        p, blocked = _canonical_target(path, read=True)
        if blocked:
            return blocked
        assert p is not None
        if not p.is_dir():
            return _err("not_a_directory", f"{path} is not a directory")
        return _list_dir(str(p), show_hidden)

    @mcp.tool()
    def fs_glob(pattern: str, path: str = ".", limit: int = 0) -> dict:
        """Find paths matching a glob (e.g. 'src/**/*.py'). Returns matches newest-first.
        Capped at 1000.

        Any CAD/geometry match is also listed under ``open_with_import_part`` — read those
        with import_part(path=...), never fs_read. Risk: read.
        """
        base, blocked = _canonical_target(path, read=True)
        if blocked:
            return blocked
        assert base is not None
        cap = min(limit if limit > 0 else MAX_GLOB_RESULTS, MAX_GLOB_RESULTS)
        if not base.is_dir():
            return _err("not_a_directory", f"{path} is not a directory")
        try:
            hits = heapq.nsmallest(
                cap + 1, _iter_scoped_paths(base, pattern),
                key=lambda entry: (-entry.stat().st_mtime_ns, str(entry)))
        except _FSBudgetError as exc:
            return _err(exc.reason, exc.hint)
        except (OSError, ValueError) as exc:
            return _err("glob_failed", str(exc))
        truncated = len(hits) > cap
        shown = [str(hit) for hit in hits[:cap]]
        out = _ok(pattern=pattern, matches=shown, count=len(shown), truncated=truncated)
        geometry = [h for h in shown if _reader_for(h) == "import_part"]
        if geometry:
            out["open_with_import_part"] = geometry
            out["note"] = ("these are CAD/geometry files — fs_read cannot read them; call "
                           'import_part(path="...") for a doc_id, then describe_part / '
                           "measure_part / inspect_part")
        return _bounded_response(out)

    @mcp.tool()
    def browse_models(path: str = ".", pattern: str = "**/*", sheets: bool = True,
                      limit: int = 24) -> dict:
        """Browse the project's CAD files like a thumbnail explorer: labelled contact sheets of real renders.

        Returns ``sheets`` — images of up to 12 numbered tiles each (4x3): the engine's
        product shot of every .step+ part/assembly, trimmed to the part, captioned with
        file name, kind, size (mm), material, features/components/mates, volume and
        revision, with a red badge on failing features — and ``models``, the same tiles as
        text (``n`` = tile number, file, one-line summary). Everything comes from the files
        (no import); thumbnails are cached in <project>/.plm/thumbs (shared with the GUI),
        and missing ones render within a time budget — ``pending`` tiles fill in on the
        next call. STEP/STL/other formats get a captioned "no preview" tile.
        ``pattern`` globs under ``path`` (hidden folders skipped); ``limit`` = tiles
        (max 48, the rest are only named); ``sheets=False`` returns text only.
        Open one with import_part(path=...) to edit or measure it. Risk: read.
        """
        from .model_card import model_card
        from .model_thumbs import build_tiles, contact_sheets, sheet_dir

        base, blocked = _canonical_target(path, read=True)
        if blocked:
            return blocked
        assert base is not None
        if not base.is_dir():
            return _err("not_a_directory", f"{path} is not a directory")
        cap = max(1, min(int(limit or 24), 48))
        count = 0

        def models():
            nonlocal count
            for match in _iter_scoped_paths(base, pattern, canonical=False):
                rel = match.relative_to(base)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if match.is_file() and _kind_for(match.name) in CAD_EXTS:
                    count += 1
                    yield match

        try:
            # Retain only the tiles and the bounded "not shown" window.
            found = heapq.nsmallest(
                cap + 200, models(),
                key=lambda entry: (_archived(entry.relative_to(base)),
                                   len(entry.relative_to(base).parts),
                                   str(entry.relative_to(base)).lower()))
        except _FSBudgetError as exc:
            return _err(exc.reason, exc.hint)
        except (OSError, ValueError) as exc:
            return _err("glob_failed", str(exc))
        shown = found[:cap]
        entries = [(str(m.relative_to(base)), model_card(m)) for m in shown]
        tiles, renders = build_tiles(
            entries, render=bool(sheets),
            deadline=time.monotonic() + PREVIEW_RENDER_BUDGET_S,
        )
        models = []
        for tile in tiles:
            card = tile["card"]
            # With sheets, each tile's caption already shows kind/size/material/contents/
            # volume/revision: the text row is only the key back to the file. Without
            # sheets the one-line summary is the whole preview.
            row: dict = {"n": tile["n"], "file": tile["label"]}
            if not sheets:
                row["summary"] = card["summary"]
            for key in ("failing_features", "geometry"):
                if card.get(key) is not None:
                    row[key] = card[key]
            if sheets and not tile.get("thumb") and tile.get("reason"):
                row["preview"] = f"{tile['status']}: {tile['reason']}"
            models.append(row)
        out = _ok(root=str(base), count=count, shown=len(models), models=models)
        if sheets and tiles:
            out["sheets"] = [_image_ref(p) for p in contact_sheets(tiles, sheet_dir())]
            out["renders"] = renders
        if count > cap:
            out["not_shown"] = [str(m.relative_to(base)) for m in found[cap:cap + 200]]
            out["note"] = (f"{count - cap} more model(s) not tiled; narrow path/pattern "
                           "or raise limit (max 48)")
        if renders.get("pending"):
            out["note"] = (out.get("note", "") + " " if out.get("note") else "") + (
                f"{renders['pending']} thumbnail(s) not rendered yet (time budget) — call "
                "again to fill them in; finished renders are cached")
        return _bounded_response(out)

    @mcp.tool()
    def fs_grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
                max_matches: int = 0) -> dict:
        """Regex-search files under *path* (optionally filtered by *glob*, e.g. '*.py').
        Returns {file, line, text} matches, capped at 500. Compilation/search runs in a
        killable readonly child with a two-second per-tool deadline. Input scans (64 MiB,
        5000 files), traversal and 32 KiB responses refuse explicitly when exhausted.
        Files initially over 8 MiB are skipped. Risk: read."""
        base, blocked = _canonical_target(path, read=True)
        if blocked:
            return blocked
        assert base is not None
        if len(pattern.encode("utf-8")) > GREP_MAX_PATTERN_BYTES:
            return _err("pattern_budget_exceeded", "Regex pattern exceeds 4096 bytes.")
        cap = min(max_matches if max_matches > 0 else MAX_GREP_MATCHES, MAX_GREP_MATCHES)
        deadline = time.monotonic() + GREP_SEARCH_DEADLINE_S
        try:
            argv = sandbox_argv(
                [sys.executable, "-I", "-S", "-B", str(Path(__file__).with_name("fs_grep_worker.py"))],
                "ro")
            env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
            if os.name == "nt" and "SYSTEMROOT" in os.environ:
                env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            if in_cell():
                env["CHUDWARE_CELL"] = "1"
                for key in ("CHUDWARE_ACTOR_SANDBOX", "CHUDWARE_ACTOR_HEX"):
                    if key in os.environ:
                        env[key] = os.environ[key]
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, cwd=str(base if base.is_dir() else base.parent), bufsize=0,
                start_new_session=os.name == "posix",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
            output, refusal = _capture_bounded_process(
                proc, deadline, FS_RESPONSE_BYTES,
                producer=lambda stream: _produce_grep_input(
                    stream, base, pattern, glob, ignore_case, cap, deadline))
        except (OSError, ValueError) as exc:
            return _err("search_worker_failed", str(exc))
        if refusal:
            reason = "search_deadline_exceeded" if refusal == "deadline_exceeded" else refusal
            return _err(reason, "Search was refused; narrow path/pattern or simplify the regex.")
        if proc.returncode != 0:
            return _err("search_worker_failed", "Isolated regex worker exited without a search result.")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            return _err("search_worker_failed", "Isolated regex worker returned an invalid result.")
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return _err("search_worker_failed", "Isolated regex worker returned an invalid result.")
        return result

    @mcp.tool()
    def fs_write(path: str, content: str, make_dirs: bool = True) -> dict:
        """Create or OVERWRITE a text file with *content*. Parent dirs are created by
        default. Returns {ok, path, bytes}. Risk: write."""
        p, blocked = _canonical_target(path, allow_missing=True)
        if blocked:
            return blocked
        assert p is not None
        try:
            if make_dirs and p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p, blocked = _canonical_target(str(p), allow_missing=True)
            if blocked:
                return blocked
            assert p is not None
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
        p, blocked = _canonical_target(path)
        if blocked:
            return blocked
        assert p is not None
        if p.is_dir():
            return _err("not_found",
                        f"{path} is a directory, not a file — fs_edit edits files; "
                        "use fs_list to browse it")
        if not p.is_file():
            return _err_not_found(path, what="file")
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


def _kill_shell_process_tree(proc: subprocess.Popen) -> None:
    """Stop the isolated command tree, including children of an exited shell."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                check=False,
            )
        finally:
            if proc.poll() is None:
                proc.kill()


def _capture_bounded_process(proc: subprocess.Popen, deadline: float, output_cap: int,
                             producer=None) -> tuple[bytes, Optional[str]]:
    """Drain finite pipe chunks; deadlines never depend on a child's Python GIL."""
    output = bytearray()
    done = threading.Event()
    stop = threading.Event()
    failure: list[str] = []
    assert proc.stdout is not None

    def collect():
        try:
            while not stop.is_set():
                if os.name == "posix" and not select.select([proc.stdout], [], [], 0.05)[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), min(IO_CHUNK_BYTES, output_cap + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > output_cap:
                    failure.append("output_budget_exceeded")
                    break
        except (OSError, ValueError):
            if not stop.is_set():
                failure.append("process_io_failed")
        finally:
            done.set()

    def produce():
        try:
            producer(proc.stdin)
        except BrokenPipeError:
            pass  # A grep worker exits early at the caller's requested match cap.
        except (OSError, ValueError, _FSBudgetError):
            failure.append("process_io_failed")
            done.set()
        finally:
            if proc.stdin is not None:
                proc.stdin.close()

    reader = threading.Thread(target=collect, name="fs-process-output", daemon=True)
    producer_context = contextvars.copy_context()
    writer = threading.Thread(
        target=lambda: producer_context.run(produce), name="fs-grep-input", daemon=True
    ) if producer else None
    refusal = None
    reader_started = writer_started = False
    try:
        reader.start()
        reader_started = True
        if writer is not None:
            writer.start()
            writer_started = True
        if not done.wait(max(0, deadline - time.monotonic())):
            refusal = "deadline_exceeded"
        elif failure:
            refusal = failure[0]
        else:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                refusal = "deadline_exceeded"
    except RuntimeError:
        refusal = "process_io_failed"
    finally:
        stop.set()
        cleanup_deadline = time.monotonic() + 5.0
        if refusal or proc.poll() is None:
            try:
                _kill_shell_process_tree(proc)
            except (OSError, subprocess.TimeoutExpired):
                refusal = "cleanup_failed"
        try:
            proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            refusal = "cleanup_failed"
        if reader_started:
            reader.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        if writer_started:
            writer.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        elif proc.stdin is not None:
            proc.stdin.close()
        if reader.is_alive() or (writer is not None and writer.is_alive()):
            refusal = "cleanup_failed"
        proc.stdout.close()
    return bytes(output[:output_cap]), refusal


def _produce_grep_input(stream, base: Path, pattern: str, glob: str,
                        ignore_case: bool, cap: int, deadline: float) -> None:
    """Only the parent admits/opens files. The child receives scoped data, not handles to paths."""
    def send(data: bytes):
        view = memoryview(data)
        while view:
            if time.monotonic() >= deadline:
                raise _FSBudgetError("search_deadline_exceeded", "Search deadline expired.")
            written = stream.write(view[:IO_CHUNK_BYTES])
            if not written:
                raise BrokenPipeError("search worker closed input")
            view = view[written:]

    def header(value: dict):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(data) > FS_RESPONSE_BYTES:
            raise _FSBudgetError("pattern_budget_exceeded", "Search header exceeds 32 KiB.")
        send(data)

    try:
        header(dict(pattern=pattern, ignore_case=ignore_case, cap=cap,
                    output_cap=FS_RESPONSE_BYTES, line_columns=MAX_LINE_COLUMNS,
                    file_cap=GREP_MAX_FILE_BYTES))
        files = iter((base,)) if base.is_file() else _iter_scoped_paths(
            base, glob or "*", recursive=True, deadline=deadline)
        scanned = input_bytes = 0
        for candidate in files:
            if not candidate.is_file():
                continue
            fp, blocked = _canonical_target(str(candidate), read=True)
            if blocked or fp is None:
                continue
            scanned += 1
            if scanned > MAX_GREP_FILES:
                raise _FSBudgetError("search_budget_exceeded", "File scan budget exhausted; narrow path.")
            try:
                with _open_read_file(fp) as fh:
                    before = os.fstat(fh.fileno())
                    if before.st_size > GREP_MAX_FILE_BYTES:
                        header({"skip": True})
                        continue
                    if input_bytes + before.st_size > GREP_MAX_INPUT_BYTES:
                        raise _FSBudgetError("search_budget_exceeded", "Search input byte budget exhausted.")
                    data = bytearray()
                    remaining = before.st_size
                    while remaining:
                        if time.monotonic() >= deadline:
                            raise _FSBudgetError("search_deadline_exceeded", "Search deadline expired.")
                        chunk = fh.read(min(IO_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise _FSBudgetError("file_changed", "Search input shrank during the read.")
                        data.extend(chunk)
                        remaining -= len(chunk)
                    if not _file_unchanged(before, os.fstat(fh.fileno())):
                        raise _FSBudgetError("file_changed", "Search input changed during the read.")
                    input_bytes += len(data)
            except (OSError, ValueError):
                header({"skip": True})
                continue
            header({"file": str(fp), "bytes": len(data)})
            send(data)
        header({"end": True})
    except _FSBudgetError as exc:
        # The child discards accumulated matches instead of reporting a partial success.
        data = json.dumps({"error": _err(exc.reason, exc.hint)}).encode("utf-8") + b"\n"
        stream.write(data)


def register_shell_tools(mcp) -> None:
    """Register the ``shell`` family: run_shell (kept separate so it toggles independently)."""

    @mcp.tool()
    def run_shell(command: str, cwd: str = "", timeout_s: int = SHELL_DEFAULT_TIMEOUT_S) -> dict:
        """Run a shell command and capture combined stdout+stderr (byte-capped).

        Refuses obviously destructive commands (rm -rf /, mkfs, dd of=/dev/*, fork bombs,
        shutdown/reboot, …). When CHUDWARE_FS_ROOT is set, cwd defaults to and is jailed to
        that root (a command that ``cd``s or uses absolute paths can still reach outside it
        — this is a starting-directory jail, not a full sandbox; the destructive-pattern
        guard above is the other half of the safety story, not a substitute for one).
        timeout_s is honored exactly from 1 to 86400 seconds (default 60; zero uses
        the default). Out-of-range requests refuse before execution, never clamp.
        At timeout or more than 96 KiB of output, the isolated POSIX process group /
        Windows process tree is killed and reaped. Captured output is bounded while it
        is produced, decoded as UTF-8 with replacement, and retained on a typed refusal
        (shell_deadline_exceeded/output_budget_exceeded); never a partial ok:true.
        Returns {ok, exit_code, output, truncated, timed_out, reason?, hint?}. Risk: write.
        """
        if not command.strip():
            return _err("empty_command", "command is empty")
        for rx in _CRITICAL_SHELL_PATTERNS:
            if rx.search(command):
                return _err("blocked",
                            "command matches a destructive-pattern guard and was refused")
        try:
            timeout = int(timeout_s) if timeout_s else SHELL_DEFAULT_TIMEOUT_S
        except (TypeError, ValueError, OverflowError):
            return _err("invalid_timeout", "timeout_s must be an integer number of seconds")
        if not 1 <= timeout <= SHELL_MAX_TIMEOUT_S:
            return _err("invalid_timeout",
                        f"timeout_s must be between 1 and {SHELL_MAX_TIMEOUT_S} seconds")
        root = _workspace_root()
        if root is None:
            return _err("workspace_root_unavailable", "CHUDWARE_FS_ROOT must name an existing directory")
        directory = Path(cwd).expanduser() if cwd else root
        if not directory.is_absolute():
            directory = root / directory
        workdir = str(directory.resolve())
        if workdir and not Path(workdir).is_dir():
            return _err("bad_cwd", f"cwd is not a directory: {workdir}")
        if workdir:
            blocked = _sensitive_block(workdir) or _root_block(workdir)
            if blocked:
                return blocked
        try:
            argv = sandbox_argv(["/bin/sh", "-c", command]) if in_cell() else command
            proc = subprocess.Popen(
                argv, shell=not in_cell(), cwd=workdir,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                start_new_session=os.name == "posix",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            output, refusal = _capture_bounded_process(
                proc, time.monotonic() + timeout, SHELL_MAX_OUTPUT_BYTES)
        except Exception as exc:  # noqa: BLE001
            return _err("exec_failed", str(exc))
        timed_out = refusal == "deadline_exceeded"
        truncated = refusal == "output_budget_exceeded"
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text += "\n… [output budget exceeded; command stopped]"
        result = _ok(exit_code=124 if timed_out else proc.returncode,
                     timed_out=timed_out, truncated=truncated, output=text)
        if refusal:
            result.update(_err(
                "shell_deadline_exceeded" if timed_out else refusal,
                "Command was stopped at its deadline or I/O budget; output is incomplete."))
        return result

