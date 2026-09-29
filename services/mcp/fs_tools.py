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

import difflib
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

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
    raw = os.environ.get("CHUDWARE_FS_ROOT", "").strip()
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
DEFAULT_MAX_BYTES = 96 * 1024    # byte ceiling on returned text
MAX_LIST_ENTRIES = 1000          # fs_list entry cap
MAX_GLOB_RESULTS = 1000          # fs_glob result cap
MAX_GREP_MATCHES = 500           # fs_grep match cap
MAX_GREP_FILES = 5000            # fs_grep files-scanned cap
GREP_MAX_FILE_BYTES = 8 * 1024 * 1024   # skip files larger than this in fs_grep
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


def _read_document(path: str, ranges, offset: int, limit: int, raw: bool) -> dict:
    from services.mcp.office_tools import extract_office  # noqa: PLC0415

    res = extract_office(path)
    if not res.get("ok"):
        return _err(res.get("reason", "extract_failed"), res.get("hint", ""))
    kind = res.get("kind", _kind_for(path))
    text = res.get("text", "") or ""
    lines = text.split("\n")
    win = _window_lines(lines, ranges, offset, limit, raw)
    if not _has_content_lines(lines, kind):
        win["hint"] = ("extracted text is empty — the document may be scanned/image-only "
                       "or have no text layer; OCR it first to get usable text")
    return _ok(path=path, kind=kind, is_document=True, **win)


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
    out = _ok(path=str(p), entries=rows, count=len(rows))
    if geometry:
        out["note"] = (
            f"{len(geometry)} CAD/geometry file(s) here; each row's card summarises it. "
            "browse_models(path) shows them as labelled thumbnail sheets; to edit or "
            'measure one, open it with import_part(path="...") for a doc_id'
        )
    return out


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
                 entries. A document (.pdf/.docx/.doc/.odt/.pptx/.ppt/.odp/.xlsx/.xlsm/
                 .xls/.ods/.csv) is extracted to text (via office_extract) and paginated
                 the same way. A CAD/geometry
                 file (.draw+/.step+/.step/.stp/.iges/.stl/.3mf/.obj/…) is NOT dumped —
                 you get its preview card (kind, features, bbox) and a labelled render,
                 both read from the file; open it with import_part(path=...) and read the model via
                 describe_part / measure_part / inspect_part (plain-text formats honour
                 raw=True as an explicit escape).
        offset : 1-indexed start line when no selector is given (default: from the top).
        limit  : max lines to return (default 400, hard cap 4000).
        raw    : omit line numbers / column clipping.

        Returns {ok, path, kind, text, total_lines, shown_lines, truncated, next_offset?}.
        """
        clean, sel = _split_path_selector(path)
        p, blocked = _canonical_target(clean, read=True)
        if blocked:
            return blocked
        assert p is not None
        ranges = _parse_ranges(sel) if sel else None
        if sel and sel.lower().find("raw") >= 0:
            raw = True
        if p.is_dir():
            return _list_dir(str(p), show_hidden=raw)
        kind = _kind_for(str(p))
        if kind in CAD_EXTS and (kind in CAD_BINARY_EXTS or not raw):
            return _cad_redirect(str(p), kind)
        if kind in DOC_EXTS:
            return _read_document(str(p), ranges, offset, limit, raw)
        return _read_text(str(p), ranges, offset, limit, raw)

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
        hits: list[str] = []
        try:
            for match in base.glob(pattern):
                resolved, descendant_block = _canonical_target(str(match), read=True)
                if descendant_block or resolved is None:
                    continue
                hits.append(str(resolved))
        except Exception as exc:  # noqa: BLE001
            return _err("glob_failed", str(exc))
        try:
            hits.sort(key=lambda m: Path(m).stat().st_mtime, reverse=True)
        except OSError:
            hits.sort()
        truncated = len(hits) > cap
        shown = hits[:cap]
        out = _ok(pattern=pattern, matches=shown, count=len(shown), truncated=truncated)
        geometry = [h for h in shown if _reader_for(h) == "import_part"]
        if geometry:
            out["open_with_import_part"] = geometry
            out["note"] = ("these are CAD/geometry files — fs_read cannot read them; call "
                           'import_part(path="...") for a doc_id, then describe_part / '
                           "measure_part / inspect_part")
        return out

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
        found: list[Path] = []
        try:
            for match in base.glob(pattern):
                rel = match.relative_to(base)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if not match.is_file() or _kind_for(match.name) not in CAD_EXTS:
                    continue
                resolved, descendant_block = _canonical_target(str(match), read=True)
                if descendant_block or resolved is None:
                    continue
                found.append(match)
        except Exception as exc:  # noqa: BLE001
            return _err("glob_failed", str(exc))
        # Live models first: an archive/backup folder full of autosaves would otherwise
        # fill every tile before the parts anyone is working on (6-axis: 70 of 94 files).
        found.sort(key=lambda m: (_archived(m.relative_to(base)),
                                  len(m.relative_to(base).parts),
                                  str(m.relative_to(base)).lower()))
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
        out = _ok(root=str(base), count=len(found), shown=len(models), models=models)
        if sheets and tiles:
            out["sheets"] = [_image_ref(p) for p in contact_sheets(tiles, sheet_dir())]
            out["renders"] = renders
        if len(found) > cap:
            out["not_shown"] = [str(m.relative_to(base)) for m in found[cap:cap + 200]]
            out["note"] = (f"{len(found) - cap} more model(s) not tiled; narrow path/pattern "
                           "or raise limit (max 48)")
        if renders.get("pending"):
            out["note"] = (out.get("note", "") + " " if out.get("note") else "") + (
                f"{renders['pending']} thumbnail(s) not rendered yet (time budget) — call "
                "again to fill them in; finished renders are cached")
        return out

    @mcp.tool()
    def fs_grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
                max_matches: int = 0) -> dict:
        """Regex-search files under *path* (optionally filtered by *glob*, e.g. '*.py').
        Returns {file, line, text} matches, capped at 500. Risk: read."""
        base, blocked = _canonical_target(path, read=True)
        if blocked:
            return blocked
        assert base is not None
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as exc:
            return _err("bad_pattern", str(exc))
        cap = min(max_matches if max_matches > 0 else MAX_GREP_MATCHES, MAX_GREP_MATCHES)
        files: list[Path] = [base] if base.is_file() else []
        if base.is_dir():
            for candidate in base.rglob(glob or "*"):
                resolved, descendant_block = _canonical_target(str(candidate), read=True)
                if not descendant_block and resolved is not None and resolved.is_file():
                    files.append(resolved)
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
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()


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
        On timeout the isolated POSIX process group / Windows process tree is killed.
        Output over 96 KiB is truncated and decoded as UTF-8 with replacement,
        including partial timeout output. Returns
        {ok, exit_code, output, truncated, timed_out}. Risk: write.
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
        workdir = cwd or (str(root) if root else None)
        if workdir and not Path(workdir).is_dir():
            return _err("bad_cwd", f"cwd is not a directory: {workdir}")
        if workdir:
            blocked = _sensitive_block(workdir) or _root_block(workdir)
            if blocked:
                return blocked
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            timed_out = False
            cleanup_failed = False
            try:
                try:
                    output, _ = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_shell_process_tree(proc)
                    try:
                        output, _ = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        # An escaped descendant still owns the pipe. Never turn cleanup
                        # into an unbounded wait or report complete tree termination.
                        output = exc.output or b""
                        cleanup_failed = True
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
        except Exception as exc:  # noqa: BLE001
            return _err("exec_failed", str(exc))
        truncated = len(output) > SHELL_MAX_OUTPUT_BYTES
        text = output[:SHELL_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n… [output truncated]"
        result = _ok(exit_code=124 if timed_out else proc.returncode,
                     timed_out=timed_out, truncated=truncated, output=text)
        if cleanup_failed:
            result.update(_err("cleanup_failed",
                              "Command timed out; an escaped descendant kept output open "
                              "after process-tree termination"))
        return result

