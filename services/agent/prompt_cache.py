"""Pre-composed system prompt: one ordering definition, cached on disk.

Why this module exists
----------------------
The system prompt a fresh ``general_agent`` turn receives is expensive to build
(``tool_schemas.render_tools_block`` renders every allowed tool's schema) and it
is almost entirely **request-independent**: the role card, the capability
assembly, the session-preference block, the harness/role-catalog blocks, the
project-folder listing, the autolearn standing guidance, the tools slot and the
skills catalog are all functions of *settings and files on disk*, not of what
the user typed. Only three slots depend on the request — project-memory recall,
skill activation, and Rules injection — and those stay live in ``run_turn``.

Two things that used to be broken and are the reason for this module:

1. **The first request paid for composition.** A fresh chat's first provider
   call was preceded by a full render of the tools slot. That work is now done
   ahead of time (``warm()``) and reused.

2. **The GUI could not honestly show what a fresh chat costs.** The context
   meter read an em-dash over the window until a provider call measured
   something, and the only other assembled-prompt surface,
   ``prompt_preview.preview_main_chat_system``, is a *mirror* of the live path
   that injects a SAMPLE ledger/stage a real turn never sends — so its number
   was never the real one. ``baseline()`` reports the tokens of the bytes a real
   turn actually sends, broken down per segment, so the meter's tooltip can say
   *what* is filling the context rather than only how much.

Single source of truth
----------------------
``static_segments()`` is the ONLY place the static block order is written down.
``agent_turn.run_turn`` iterates it (splicing the request-dependent project
memory block at :data:`MEMORY_SLOT`) instead of appending those blocks itself,
and ``mission_dispatch.run_linear_role_turn`` takes ``role_system`` /
``allowed`` / ``tools_text`` from here instead of composing them again.
``test_prompt_cache.py`` pins byte-equality between this composition and the
system prompt a live turn actually hands its provider — a mirror that drifts is
the defect class this module removes, so it is asserted rather than hoped for.

Invalidation
------------
Two mechanisms, deliberately belt-and-braces:

* ``invalidate()`` is called by the settings/prompt/rule/tool-option write RPCs.
  This is the *fast* path — it makes a Settings change visible immediately.
* :func:`fingerprint` is the *correctness backstop*: a stat signature over every
  input that can change the composed bytes. Any writer we failed to hook still
  cannot serve a stale prompt, because the fingerprint will not match. A cache
  entry whose fingerprint differs is discarded, never used.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services import paths

#: Name of the segment slot AFTER which request-dependent project-memory recall
#: is spliced by ``run_turn``. Named rather than positional so the ordering can
#: be re-arranged here without silently moving the memory block.
MEMORY_SLOT = "role_catalog"

#: Cache schema version. Bump when the meaning of a stored field changes; an
#: entry written by an older version is discarded rather than misread.
CACHE_VERSION = 3

#: Directories whose ``.py`` / ``.md`` / ``.yaml`` contents can change the
#: composed bytes. Walked to build the fingerprint. Kept explicit: a recursive
#: walk of the whole repo would make the check cost more than the compose.
_CODE_DIRS = ("services/agent", "services/mcp")
_CODE_SUFFIXES = (".py", ".md", ".yaml", ".yml")

#: The fingerprint stats a few hundred files. Cheap (single-digit ms) but the
#: live turn path consults the cache on every turn, so revalidate at most this
#: often in-process. A settings write calls invalidate() and does not wait.
_REVALIDATE_AFTER_S = 2.0

_lock = threading.RLock()
_mem: dict[str, "StaticPrompt"] = {}
_fp_cache: tuple[float, str] | None = None


# --------------------------------------------------------------------------- model

@dataclass
class Segment:
    """One labelled block of the static system prompt.

    ``label`` is a stable id (used by :data:`MEMORY_SLOT` and by the GUI meter
    tooltip); ``title`` is the human string the tooltip shows.
    """

    label: str
    title: str
    text: str
    #: Separator used when this segment is appended to the accumulated prompt.
    #: ``run_turn`` joins every block with a blank line EXCEPT the bare
    #: "Allowed tools:" fallback, which uses a single newline — reproduced here
    #: so the composed bytes match the live path exactly.
    sep: str = "\n\n"

    @property
    def tokens(self) -> int:
        return _count_tokens(self.text)


@dataclass
class StaticPrompt:
    """The request-independent portion of a turn's system prompt."""

    role: str
    role_system: str
    allowed: list[str]
    tools_text: str
    segments: list[Segment]
    fingerprint: str
    project_root: str = ""
    capability: str = ""
    warnings: list[str] = field(default_factory=list)

    # -- composition ------------------------------------------------------
    def render(self, *, memory_block: str = "") -> str:
        """Assemble the full system prompt, splicing *memory_block* at its slot."""
        return render_segments(self.segments, memory_block=memory_block)

    # -- reporting --------------------------------------------------------
    def breakdown(self) -> list[dict[str, Any]]:
        """Per-segment token report, largest first, for the meter tooltip."""
        rows = [
            {"label": s.label, "title": s.title,
             "tokens": s.tokens, "chars": len(s.text)}
            for s in self.segments if s.text
        ]
        rows.sort(key=lambda r: -int(r["tokens"]))
        return rows

    @property
    def total_tokens(self) -> int:
        return _count_tokens(self.render())

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "role": self.role,
            "role_system": self.role_system,
            "allowed": list(self.allowed),
            "tools_text": self.tools_text,
            # `sep` MUST be persisted: the "Allowed tools:" fallback uses a
            # single newline, so dropping it made a round-tripped entry render
            # a blank line there instead — a silent byte change introduced by
            # the cache itself, caught by test_disk_round_trip_preserves_the_bytes.
            "segments": [{"label": s.label, "title": s.title, "text": s.text,
                          "sep": s.sep}
                         for s in self.segments],
            "fingerprint": self.fingerprint,
            "project_root": self.project_root,
            "capability": self.capability,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, d: dict) -> "StaticPrompt | None":
        if not isinstance(d, dict) or int(d.get("version") or 0) != CACHE_VERSION:
            return None
        try:
            return cls(
                role=str(d["role"]),
                role_system=str(d.get("role_system") or ""),
                allowed=[str(x) for x in (d.get("allowed") or [])],
                tools_text=str(d.get("tools_text") or ""),
                segments=[Segment(str(s["label"]),
                                  str(s.get("title") or s["label"]),
                                  str(s.get("text") or ""),
                                  str(s.get("sep") or "\n\n"))
                          for s in (d.get("segments") or [])
                          if isinstance(s, dict) and s.get("label")],
                fingerprint=str(d.get("fingerprint") or ""),
                project_root=str(d.get("project_root") or ""),
                capability=str(d.get("capability") or ""),
                warnings=[str(x) for x in (d.get("warnings") or [])],
            )
        except Exception:  # noqa: BLE001 — a malformed entry is a cache miss
            return None


# --------------------------------------------------------------------------- helpers

def _count_tokens(text: str) -> int:
    """Repo-wide 4-chars/token heuristic (shared with ``tiers._count_tokens``)."""
    try:
        from services.agent.tiers import _count_tokens as _ct  # noqa: PLC0415
        return _ct(text)
    except Exception:  # noqa: BLE001
        return (len(text) + 3) // 4


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cache_dir() -> Path:
    """On-disk cache location, created on first use."""
    d = paths.ROOT / "prompt_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stat_sig(p: Path, h: Any) -> None:
    try:
        st = p.stat()
    except OSError:
        return
    h.update(str(p).encode("utf-8", "replace"))
    h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())


def fingerprint(*, project_root: str | None = None) -> str:
    """Stat signature over every input that can change the composed bytes.

    Covers: config.json (settings — tool options, families, harness prefs, the
    autolearn toggle), the authored role cards and their user overrides, the
    Rules spec and its context packs, the skills trees, the project folder
    listing, and the ``services/agent`` + ``services/mcp`` source that supplies
    ``_SYSTEM_BASE``, the harness/catalog blocks and every tool schema.

    Throttled: recomputing on every turn would stat a few hundred files for no
    reason, so a result is reused for :data:`_REVALIDATE_AFTER_S`. A settings
    write calls :func:`invalidate`, which clears the throttle immediately.
    """
    global _fp_cache
    now = time.monotonic()
    with _lock:
        if _fp_cache is not None and (now - _fp_cache[0]) < _REVALIDATE_AFTER_S:
            cached = _fp_cache[1]
        else:
            cached = None
    if cached is not None:
        return _mix(cached, project_root)

    h = hashlib.sha256()
    h.update(f"v{CACHE_VERSION}\n".encode())
    _stat_sig(paths.CONFIG, h)
    root = _repo_root()
    for rel in _CODE_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in {"__pycache__", ".pytest_cache"} and not d.startswith(".")
            )
            for fn in sorted(filenames):
                if fn.endswith(_CODE_SUFFIXES):
                    _stat_sig(Path(dirpath) / fn, h)
    # User-editable overrides that live OUTSIDE the repo.
    for sub in ("prompts", "skills", "context_packs", "triggers.yaml"):
        p = paths.ROOT / sub
        if p.is_file():
            _stat_sig(p, h)
        elif p.is_dir():
            try:
                for fn in sorted(os.listdir(p)):
                    _stat_sig(p / fn, h)
            except OSError:
                pass
    sig = h.hexdigest()[:32]
    with _lock:
        _fp_cache = (now, sig)
    return _mix(sig, project_root)


def _mix(sig: str, project_root: str | None) -> str:
    """Fold the project-folder listing signature into *sig*.

    The composed prompt contains ``project_context_block(root)`` — a listing of
    the CAD files in the active project folder — so adding or removing a file
    there genuinely changes the bytes and must invalidate.
    """
    if not project_root:
        return sig
    h = hashlib.sha256(sig.encode())
    h.update(str(project_root).encode("utf-8", "replace"))
    try:
        p = Path(project_root)
        for entry in sorted(os.listdir(p))[:400]:
            h.update(entry.encode("utf-8", "replace"))
            try:
                h.update(str((p / entry).stat().st_mtime_ns).encode())
            except OSError:
                pass
    except OSError:
        pass
    return h.hexdigest()[:32]


def _key(role: str, capability: str, project_root: str) -> str:
    raw = f"{role}|{capability}|{project_root}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


# --------------------------------------------------------------------------- compose

def static_segments(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    role_system: str = "",
    tools_text: str = "",
    allowed: list[str] | None = None,
    include_catalog: bool = True,
    include_skills: bool | None = None,
) -> tuple[list[Segment], list[str]]:
    """THE ordering definition for the static system prompt.

    Returns ``(segments, warnings)``. Every block here is request-independent;
    ``run_turn`` splices request-dependent project memory after the segment
    named :data:`MEMORY_SLOT` and appends skill activation / Rules advisories
    afterwards.

    *include_catalog* mirrors ``run_turn``'s once-per-session behaviour: the
    harness + role-catalog blocks are static within a session and are sent on
    its FIRST turn only.

    *include_skills* controls the skills CATALOG block, defaulting to
    *include_catalog*. ``run_turn`` passes ``False`` and emits it itself,
    because there the catalog is interleaved with request-dependent skill
    ACTIVATION and is gated on catalog-due alone (a subagent still gets it).
    Callers that want the whole static prompt — the cache, the meter baseline —
    leave it defaulted.
    """
    segs: list[Segment] = []
    warn: list[str] = []

    def _add(label: str, title: str, text: str, sep: str = "\n\n") -> None:
        """Append a guarded block.

        Text is stored RAW (not ``rstrip``-ed) because the live path rstrips the
        accumulated left side, not the incoming block — so a trailing newline on
        the FINAL block survives there and must survive here too.
        """
        if text and text.strip():
            segs.append(Segment(label, title, text, sep))

    from services.agent import agent_turn as _at  # noqa: PLC0415

    # Role card and harness protocol are always present as the first two
    # segments, verbatim: `run_turn` opens with `(system_prompt or "") + "\n\n"
    # + _SYSTEM_BASE`, so an empty role card still contributes its separator.
    segs.append(Segment("role", "Role card", role_system or ""))
    segs.append(Segment("base", "Harness protocol", _at._SYSTEM_BASE))

    if include_catalog:
        try:
            from services.agent.role_catalog import (  # noqa: PLC0415
                format_role_catalog_block,
                harness_context_block,
            )
            _add("harness", "Harness context", harness_context_block())
            _add(MEMORY_SLOT, "Role catalog", format_role_catalog_block())
        except Exception as exc:  # noqa: BLE001
            warn.append(f"role_catalog: {type(exc).__name__}: {exc}")

    # The memory slot must exist even when the catalog blocks are suppressed,
    # otherwise a non-first turn would silently drop the memory block instead
    # of splicing it. An empty placeholder carries the slot without bytes.
    if not any(s.label == MEMORY_SLOT for s in segs):
        segs.append(Segment(MEMORY_SLOT, "Role catalog", ""))

    try:
        from services.agent.project_context import project_context_block  # noqa: PLC0415
        _add("project", "Project files", project_context_block(project_root))
    except Exception as exc:  # noqa: BLE001
        warn.append(f"project_context: {type(exc).__name__}: {exc}")

    try:
        from services.agent.autolearn import standing_guidance  # noqa: PLC0415
        _add("autolearn", "Standing guidance", standing_guidance())
    except Exception as exc:  # noqa: BLE001
        warn.append(f"autolearn: {type(exc).__name__}: {exc}")

    if tools_text:
        _add("tools", "Tool schemas", tools_text)
    elif allowed:
        # Single newline, not a blank line: matches run_turn's
        # `system += "\nAllowed tools: " + ...` fallback byte for byte.
        _add("tools", "Tool schemas",
             "Allowed tools: " + ", ".join(sorted(allowed)), "\n")

    want_skills = include_catalog if include_skills is None else include_skills
    if want_skills:
        try:
            from services.agent import skills as _skills  # noqa: PLC0415
            _add("skills", "Skills catalog", _skills.catalog_block())
        except Exception as exc:  # noqa: BLE001
            warn.append(f"skills: {type(exc).__name__}: {exc}")

    return segs, warn


def render_segments(segments: list[Segment], *, memory_block: str = "") -> str:
    """Join *segments* into a system prompt, splicing *memory_block* at its slot.

    The join rules are the live path's, reproduced deliberately rather than
    tidied, because ``run_turn`` calls this and the composed bytes must not
    change: the role card and the harness protocol are joined VERBATIM
    (``run_turn`` does not ``rstrip`` there, so a role card ending in a newline
    really does contribute a third blank line), every later block is appended to
    an ``rstrip``-ed left side, and the bare "Allowed tools:" fallback uses a
    single newline via its segment ``sep``.
    """
    out = ""
    for i, seg in enumerate(segments):
        if seg.text:
            if i == 0:
                out = seg.text
            elif seg.label == "base":
                out = out + seg.sep + seg.text
            else:
                out = out.rstrip() + seg.sep + seg.text
        if seg.label == MEMORY_SLOT and memory_block:
            out = (out.rstrip() + "\n\n" + memory_block) if out else memory_block
    return out


def compose(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    include_tools: bool = True,
) -> StaticPrompt:
    """Compose the static prompt from scratch (no cache read).

    Runs the same calls the live path runs, in the live path's order: the role
    card resolution + capability assembly + session preferences that
    ``run_linear_role_turn`` performs, then :func:`static_segments`.
    """
    warn: list[str] = []
    from services.agent import agent_turn as _at  # noqa: PLC0415
    from services.agent.roles import load_role  # noqa: PLC0415

    card = load_role(role)
    try:
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            effective_role_system_prompt,
            linear_role_allowed_tools,
        )
        base_role_system = effective_role_system_prompt(card)
        allowed = linear_role_allowed_tools(card)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"effective_role_policy: {type(exc).__name__}: {exc}")
        base_role_system = card.system_prompt
        allowed = list(card.concrete_tools)
    role_system = base_role_system
    cap_letter = ""
    try:
        from services.agent.prompt_tiers import (  # noqa: PLC0415
            assemble_system_for_capability,
            letter_from_capability,
        )
        cap_letter = str(letter_from_capability(capability) or "")
        role_system = assemble_system_for_capability(
            base_role_system, cap_letter)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"prompt_tiers: {type(exc).__name__}: {exc}")
        role_system = base_role_system

    # Session preferences BEFORE the tools render: a preference that drops a
    # tool must also drop its schema, or the model is shown a tool every call
    # to which will be refused.
    try:
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            _apply_session_preferences,
        )
        allowed, role_system = _apply_session_preferences(role, allowed, role_system)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"session_preferences: {type(exc).__name__}: {exc}")

    tools_text = ""
    if include_tools:
        try:
            from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
            tools_text, _meta = render_tools_block(
                allowed, resolver=_at.resolver_for(None))
        except Exception as exc:  # noqa: BLE001
            warn.append(f"render_tools_block: {type(exc).__name__}: {exc}")
            tools_text = ""

    segs, seg_warn = static_segments(
        role=role, capability=capability, project_root=project_root,
        role_system=role_system, tools_text=tools_text, allowed=allowed,
    )
    return StaticPrompt(
        role=role,
        role_system=role_system,
        allowed=allowed,
        tools_text=tools_text,
        segments=segs,
        fingerprint=fingerprint(project_root=project_root),
        project_root=str(project_root or ""),
        capability=cap_letter,
        warnings=warn + seg_warn,
    )


# --------------------------------------------------------------------------- cache

def _disk_path(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def _load_disk(key: str, want_fp: str) -> StaticPrompt | None:
    p = _disk_path(key)
    try:
        sp = StaticPrompt.from_json(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — missing/corrupt entry is a miss
        return None
    if sp is None or sp.fingerprint != want_fp:
        return None
    return sp


def _store_disk(key: str, sp: StaticPrompt) -> None:
    """Atomic write so a concurrent reader never sees a half-written entry."""
    try:
        p = _disk_path(key)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sp.to_json(), ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 — the cache is an optimisation, never fatal
        pass


def get(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    include_tools: bool = True,
    refresh: bool = False,
) -> StaticPrompt:
    """Return the static prompt, composing only when the cache cannot serve it.

    Never raises: a cache failure degrades to a live compose.
    """
    cap = ""
    try:
        from services.agent.prompt_tiers import letter_from_capability  # noqa: PLC0415
        cap = str(letter_from_capability(capability) or "")
    except Exception:  # noqa: BLE001
        cap = ""
    root = str(project_root or "")
    key = _key(role, cap, root)
    want = fingerprint(project_root=project_root)

    if not refresh:
        with _lock:
            hit = _mem.get(key)
        if hit is not None and hit.fingerprint == want:
            return hit
        disk = _load_disk(key, want)
        if disk is not None:
            with _lock:
                _mem[key] = disk
            return disk

    sp = compose(role=role, capability=capability, project_root=project_root,
                 include_tools=include_tools)
    with _lock:
        _mem[key] = sp
    _store_disk(key, sp)
    return sp


def invalidate() -> None:
    """Drop every cached entry and the fingerprint throttle.

    Called by the settings / prompt / rule / tool-option write RPCs so a change
    is visible on the next turn instead of up to
    :data:`_REVALIDATE_AFTER_S` later. The fingerprint remains the correctness
    backstop for any writer not hooked here.
    """
    global _fp_cache
    with _lock:
        _mem.clear()
        _fp_cache = None
    try:
        for p in cache_dir().glob("*.json"):
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def warm(*, project_root: str | None = None, background: bool = True) -> Any:
    """Compose ahead of the user's first message.

    The point of the whole module: ``render_tools_block`` is the expensive step
    and it used to run inside the first turn, so the first provider call waited
    on it. Returns the thread when *background*, else the ``StaticPrompt``.
    """
    if not background:
        return get(project_root=project_root)

    def _run() -> None:
        try:
            get(project_root=project_root)
        except Exception:  # noqa: BLE001 — warming never breaks the worker
            pass

    t = threading.Thread(target=_run, name="prompt-cache-warm", daemon=True)
    t.start()
    return t


def baseline(*, project_root: str | None = None,
             role: str = "general_agent") -> dict[str, Any]:
    """What a fresh chat costs, for the GUI context meter and its tooltip.

    ``tokens`` is the estimated input cost of the system prompt a real turn
    sends — the bytes from :meth:`StaticPrompt.render`, not a mirror of them.
    ``segments`` is the per-block breakdown the tooltip renders so the user can
    see *what* is filling the window (tool schemas dominate) rather than only
    that something is.
    """
    try:
        sp = get(role=role, project_root=project_root)
        return {
            "ok": True,
            "tokens": sp.total_tokens,
            "chars": len(sp.render()),
            "role": sp.role,
            "fingerprint": sp.fingerprint,
            "project_root": sp.project_root,
            "allowed_tool_count": len(sp.allowed),
            "segments": sp.breakdown(),
            "warnings": sp.warnings,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tokens": 0, "chars": 0, "segments": [],
                "error": f"{type(exc).__name__}: {exc}"}
