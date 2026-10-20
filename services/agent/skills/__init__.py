"""skills.py — Copilot skills discovery, catalog, activation, and skill:// resolver.

Contract: contracts/skills.md v1.

Skills are folders of instructions + resources the copilot (and humans) can
invoke, modeled on Anthropic Agent Skills / omp progressive disclosure:
metadata always in context (~1 line per skill), full body injected on
activation only.

Locations (precedence, first wins by name):
  1. project  ``.chudware/skills/<name>/SKILL.md``
  2. user     ``~/.local/share/Chudware/skills/<name>/SKILL.md``
  3. builtin  ``services/agent/skills/builtin/<name>/SKILL.md``

Activation:
  (a) explicit ``/skill:<name>`` or ``/<name>`` slash command
  (b) auto-inject when ``inject: auto`` and a trigger fires (keyword in user
      prompt, tool about to run, mission phase) — once per session until
      :func:`reset_session` clears that session's injected set.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional
from services.agent.request_context import root_value

# --------------------------------------------------------------------------- constants


_SKILL_FILENAME = "SKILL.md"
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
_SLASH_SKILL_RE = re.compile(
    r"(?m)^\s*/(?:skill:)?([a-z0-9][a-z0-9_-]*)(?:\s+(.*))?$",
    re.IGNORECASE,
)
_MID_PROMPT_SKILL_RE = re.compile(
    r"(?:^|\s)/skill:([a-z0-9][a-z0-9_-]*)(?=\s|$)",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Slash commands owned by other subsystems: never resolved as a skill even if a
# skill of that name exists (see parse_rule_slash below).
RESERVED_SLASH_COMMANDS: frozenset[str] = frozenset({"rule"})

# Process-global session state: session_id -> set of skill names already injected.
_session_injected: dict[str, set[str]] = {}
_session_lock = threading.Lock()
# Character budgets use the conservative tokenizer-independent estimate of
# four UTF-8 text characters per token: 12k chars/skill (~3k tokens), 24k
# chars/turn (~6k tokens). The aggregate covers activated bodies and reports.
SKILL_BODY_CHAR_LIMIT = 12_000
SKILL_INJECTION_CHAR_LIMIT = 24_000
SKILL_CATALOG_CHAR_LIMIT = 4_000
_OMISSION_REPORT_RESERVE = 1_024
# Catalogs are isolated by their fully resolved project/user/builtin roots.
_catalog_cache: dict[tuple[Path, Path, Path], "_Catalog"] = {}
_catalog_lock = threading.Lock()


# --------------------------------------------------------------------------- paths


def user_skills_dir() -> Path:
    """``~/.local/share/Chudware/skills`` (Windows: %APPDATA%/Chudware/skills).

    ``CHUDWARE_SKILLS_USER_DIR`` overrides (tests).
    """
    override = os.environ.get("CHUDWARE_SKILLS_USER_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "Chudware" / "skills"


def project_skills_dir(project_root: Path | str | None = None) -> Path:
    """``<project>/.chudware/skills``. ``CHUDWARE_PROJECT_DIR`` or *project_root* or cwd."""
    override = os.environ.get("CHUDWARE_SKILLS_PROJECT_DIR")
    if override:
        return Path(override)
    if project_root is not None:
        return Path(project_root) / ".chudware" / "skills"
    env = root_value("CHUDWARE_PROJECT_DIR")
    root = Path(env) if env else Path.cwd()
    return root / ".chudware" / "skills"


def builtin_skills_dir() -> Path:
    override = os.environ.get("CHUDWARE_SKILLS_BUILTIN_DIR")
    if override:
        return Path(override)
    return _BUILTIN_DIR

def _resolved_path(path: Path) -> Path:
    """Return a stable cache-key path without requiring the target to exist."""
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def _resolved_roots(
    project_root: Path | str | None = None,
) -> tuple[Path, Path, Path]:
    return (
        _resolved_path(project_skills_dir(project_root)),
        _resolved_path(user_skills_dir()),
        _resolved_path(builtin_skills_dir()),
    )


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class SkillTriggers:
    keywords: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "keywords": list(self.keywords),
            "tools": list(self.tools),
            "phases": list(self.phases),
        }


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    source: str  # "project" | "user" | "builtin"
    path: Path  # SKILL.md path
    base_dir: Path
    inject: str = "manual"  # "auto" | "manual"
    triggers: SkillTriggers = field(default_factory=SkillTriggers)
    roles: tuple[str, ...] = ()
    body: str = ""  # markdown body (frontmatter stripped)
    raw: str = ""  # full file text

    def meta_line(self) -> str:
        """One-line catalog entry for system context (~100 tokens/skill)."""
        return f"- {self.name}: {self.description}"

    def to_list_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "inject": self.inject,
            "triggers": self.triggers.as_dict(),
            "roles": list(self.roles),
        }

@dataclass(frozen=True)
class SkillLoadDiagnostic:
    """A discovered skill/root that could not be loaded safely."""

    name: str | None
    source: str
    path: Path
    kind: str
    message: str

    def to_list_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": "Unavailable: skill failed to load",
            "source": self.source,
            "inject": "unavailable",
            "triggers": SkillTriggers().as_dict(),
            "roles": [],
            "available": False,
            "path": str(self.path),
            "load_error": {
                "kind": self.kind,
                "message": self.message,
            },
        }


@dataclass(frozen=True)
class _ScanRecord:
    skill: Skill | None
    diagnostic: SkillLoadDiagnostic | None
    reserved_names: frozenset[str]


@dataclass(frozen=True)
class _Catalog:
    skills: tuple[Skill, ...]
    diagnostics: tuple[SkillLoadDiagnostic, ...]
    reserved_names: frozenset[str]


# --------------------------------------------------------------------------- frontmatter


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML subset for skill frontmatter (no PyYAML required).

    Supports: scalars, inline lists ``[a, b]``, block lists ``- item``, and one
    level of nested mapping (``triggers:`` with indented keys).
    """
    result: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)

    def _scalar(s: str) -> Any:
        s = s.strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [_scalar(p) for p in inner.split(",")]
        low = s.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if low in ("null", "none", "~"):
            return None
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s

    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # top-level key: value
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        if rest == "" or rest is None:
            # nested block or list
            i += 1
            nested: dict[str, Any] = {}
            items: list[Any] = []
            mode: Optional[str] = None
            while i < n:
                sub = lines[i]
                if not sub.strip() or sub.lstrip().startswith("#"):
                    i += 1
                    continue
                # still indented?
                if not (sub.startswith(" ") or sub.startswith("\t")):
                    break
                sm = re.match(r"^\s+-\s+(.*)$", sub)
                if sm:
                    mode = "list"
                    items.append(_scalar(sm.group(1)))
                    i += 1
                    continue
                km = re.match(r"^\s+([A-Za-z0-9_-]+)\s*:\s*(.*)$", sub)
                if km:
                    mode = "map"
                    sk, sv = km.group(1), km.group(2)
                    if sv == "":
                        # nested list under this key
                        i += 1
                        sub_items: list[Any] = []
                        while i < n:
                            ss = lines[i]
                            if not ss.strip():
                                i += 1
                                continue
                            lm = re.match(r"^\s+-\s+(.*)$", ss)
                            if not lm:
                                break
                            sub_items.append(_scalar(lm.group(1)))
                            i += 1
                        nested[sk] = sub_items
                    else:
                        nested[sk] = _scalar(sv)
                        i += 1
                    continue
                break
            if mode == "list":
                result[key] = items
            elif mode == "map":
                result[key] = nested
            else:
                result[key] = ""
            continue
        result[key] = _scalar(rest)
        i += 1
    return result


def parse_skill_md(text: str, *, path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body)``. Body has frontmatter stripped.

    The bundled mini-parser is only a dependency fallback. Once PyYAML imports,
    its syntax/type errors are authoritative and must reach the load diagnostic.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        data = _parse_simple_yaml(fm_text)
    else:
        data = yaml.safe_load(fm_text) or {}
        if not isinstance(data, dict):
            raise ValueError("skill frontmatter must be a YAML mapping")
    return data, body


def _triggers_from_fm(raw: Any) -> SkillTriggers:
    if not isinstance(raw, dict):
        return SkillTriggers()
    def _list(key: str) -> tuple[str, ...]:
        v = raw.get(key) or []
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (list, tuple)):
            return tuple(str(x) for x in v if str(x).strip())
        return ()
    return SkillTriggers(keywords=_list("keywords"), tools=_list("tools"), phases=_list("phases"))


def _load_diagnostic(
    skill_dir: Path,
    source: str,
    kind: str,
    message: str,
) -> _ScanRecord:
    candidate = skill_dir.name if _NAME_RE.match(skill_dir.name) else None
    return _ScanRecord(
        skill=None,
        diagnostic=SkillLoadDiagnostic(
            name=candidate,
            source=source,
            path=skill_dir / _SKILL_FILENAME,
            kind=kind,
            message=message,
        ),
        reserved_names=frozenset({candidate} if candidate else ()),
    )


def _load_skill_from_dir(skill_dir: Path, source: str) -> _ScanRecord | None:
    """Load one skill folder, preserving any failed candidate as a diagnostic."""
    skill_md = skill_dir / _SKILL_FILENAME
    try:
        file_mode = skill_md.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _load_diagnostic(
            skill_dir, source, "read_error", f"{type(exc).__name__}: {exc}",
        )
    if not stat.S_ISREG(file_mode):
        return None
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return _load_diagnostic(
            skill_dir, source, "read_error", f"{type(exc).__name__}: {exc}",
        )
    try:
        fm, body = parse_skill_md(raw, path=skill_md)
    except Exception as exc:  # PyYAML and frontmatter type failures are data.
        return _load_diagnostic(
            skill_dir, source, "yaml_error", f"{type(exc).__name__}: {exc}",
        )
    name = str(fm.get("name") or skill_dir.name).strip()
    reserved_names = {
        candidate
        for candidate in (skill_dir.name, name)
        if _NAME_RE.match(candidate)
    }
    if not name or not _NAME_RE.match(name):
        record = _load_diagnostic(
            skill_dir, source, "invalid_name", f"invalid skill name: {name!r}",
        )
        return _ScanRecord(
            skill=None,
            diagnostic=record.diagnostic,
            reserved_names=frozenset(reserved_names),
        )
    description = str(fm.get("description") or "").strip()
    if not description:
        return _ScanRecord(
            skill=None,
            diagnostic=SkillLoadDiagnostic(
                name=name,
                source=source,
                path=skill_md,
                kind="validation_error",
                message="frontmatter description is required",
            ),
            reserved_names=frozenset(reserved_names),
        )
    inject = str(fm.get("inject") or "manual").strip().lower()
    if inject not in ("auto", "manual"):
        inject = "manual"
    roles_raw = fm.get("roles") or []
    if isinstance(roles_raw, str):
        roles: tuple[str, ...] = (roles_raw,)
    elif isinstance(roles_raw, (list, tuple)):
        roles = tuple(str(r) for r in roles_raw)
    else:
        roles = ()
    skill = Skill(
        name=name,
        description=description,
        source=source,
        path=skill_md,
        base_dir=skill_dir,
        inject=inject,
        triggers=_triggers_from_fm(fm.get("triggers")),
        roles=roles,
        body=body.lstrip("\n"),
        raw=raw,
    )
    return _ScanRecord(
        skill=skill,
        diagnostic=None,
        reserved_names=frozenset(reserved_names),
    )


def _scan_root(
    root: Path,
    source: str,
) -> tuple[list[_ScanRecord], list[SkillLoadDiagnostic]]:
    """Scan ``root/<name>/SKILL.md`` and report unreadable roots/entries."""
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    except OSError as exc:
        return [], [
            SkillLoadDiagnostic(
                name=None,
                source=source,
                path=root,
                kind="scan_error",
                message=f"{type(exc).__name__}: {exc}",
            )
        ]
    records: list[_ScanRecord] = []
    diagnostics: list[SkillLoadDiagnostic] = []
    for entry in entries:
        try:
            if not stat.S_ISDIR(entry.stat().st_mode):
                continue
        except OSError as exc:
            diagnostics.append(
                SkillLoadDiagnostic(
                    name=entry.name if _NAME_RE.match(entry.name) else None,
                    source=source,
                    path=entry,
                    kind="scan_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        record = _load_skill_from_dir(entry, source)
        if record is not None:
            records.append(record)
    return records, diagnostics


# --------------------------------------------------------------------------- discovery


def _discover_catalog(
    *,
    project_root: Path | str | None = None,
    force: bool = False,
) -> _Catalog:
    roots = _resolved_roots(project_root)
    with _catalog_lock:
        cached = _catalog_cache.get(roots)
        if cached is not None and not force:
            return cached
        by_name: dict[str, Skill] = {}
        diagnostics: list[SkillLoadDiagnostic] = []
        reserved: set[str] = set()
        for root, source in zip(roots, ("project", "user", "builtin")):
            records, root_diagnostics = _scan_root(root, source)
            diagnostics.extend(root_diagnostics)
            for record in records:
                # A higher-precedence discovered candidate owns all of its valid
                # folder/frontmatter names even when loading that candidate failed.
                if record.reserved_names & reserved:
                    continue
                reserved.update(record.reserved_names)
                if record.skill is not None:
                    by_name.setdefault(record.skill.name, record.skill)
                elif record.diagnostic is not None:
                    diagnostics.append(record.diagnostic)
        catalog = _Catalog(
            skills=tuple(sorted(by_name.values(), key=lambda s: s.name.lower())),
            diagnostics=tuple(diagnostics),
            reserved_names=frozenset(reserved),
        )
        _catalog_cache[roots] = catalog
        return catalog


def discover_skills(
    *,
    project_root: Path | str | None = None,
    force: bool = False,
) -> list[Skill]:
    """Discover skills with project > user > builtin precedence."""
    return list(_discover_catalog(project_root=project_root, force=force).skills)


def clear_cache(
    *,
    project_root: Path | str | None = None,
    user_root: Path | str | None = None,
    builtin_root: Path | str | None = None,
) -> None:
    """Invalidate all catalogs, or only catalogs containing supplied roots."""
    with _catalog_lock:
        if project_root is None and user_root is None and builtin_root is None:
            _catalog_cache.clear()
            return
        project_path = (
            _resolved_path(project_skills_dir(project_root))
            if project_root is not None else None
        )
        user_path = _resolved_path(Path(user_root)) if user_root is not None else None
        builtin_path = (
            _resolved_path(Path(builtin_root)) if builtin_root is not None else None
        )
        stale = [
            key for key in _catalog_cache
            if (project_path is None or key[0] == project_path)
            and (user_path is None or key[1] == user_path)
            and (builtin_path is None or key[2] == builtin_path)
        ]
        for key in stale:
            _catalog_cache.pop(key, None)


def get_skill(name: str, *, project_root: Path | str | None = None) -> Optional[Skill]:
    name = (name or "").strip()
    if not name:
        return None
    for s in discover_skills(project_root=project_root):
        if s.name == name:
            return s
    return None


def list_skills(*, project_root: Path | str | None = None) -> list[dict[str, Any]]:
    """Return available skills plus structured diagnostics for failed candidates."""
    catalog = _discover_catalog(project_root=project_root)
    entries = [skill.to_list_entry() for skill in catalog.skills]
    entries.extend(diagnostic.to_list_entry() for diagnostic in catalog.diagnostics)
    return entries


def _skill_load_diagnostic(
    name: str,
    *,
    project_root: Path | str | None = None,
) -> SkillLoadDiagnostic | None:
    for diagnostic in _discover_catalog(project_root=project_root).diagnostics:
        if diagnostic.name == name:
            return diagnostic
    return None


# --------------------------------------------------------------------------- catalog / system context


def catalog_block(*, project_root: Path | str | None = None) -> str:
    """Metadata catalog for system context — ~1 line per skill."""
    skills = discover_skills(project_root=project_root)
    if not skills:
        return ""
    lines = [
        "[SKILLS] Available specialized skill packs. Metadata only — fetch a full "
        "body with cad_skill(name=\"<name>\") the moment its topic comes up:",
    ]
    for s in skills:
        lines.append(s.meta_line())
    lines.append(
        "Fetch with cad_skill(name=\"<name>\"), or cad_skill(name=\"<name>\", "
        "path=\"<file>\") for a file inside the pack. NEVER read a skill:// URL: "
        "that scheme resolves against the runtime's own unrelated skill registry "
        "and fails. Auto-inject packs also arrive once per session when their "
        "trigger tools are first used."
    )
    block = "\n".join(lines)
    return _truncate_semantically(
        block,
        SKILL_CATALOG_CHAR_LIMIT,
        "\n[SKILLS CATALOG TRUNCATED: additional entries omitted.]",
    )


def _truncate_semantically(text: str, max_chars: int, marker: str) -> str:
    """Truncate at a paragraph, then line boundary, with a visible marker."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if len(marker) >= max_chars:
        return marker[:max_chars]
    available = max_chars - len(marker)
    boundary = text.rfind("\n\n", 0, available + 1)
    if boundary <= 0:
        boundary = text.rfind("\n", 0, available + 1)
    if boundary <= 0:
        boundary = available
    return text[:boundary].rstrip() + marker


def format_skill_body(
    skill: Skill,
    *,
    args: str = "",
    max_chars: int = SKILL_BODY_CHAR_LIMIT,
) -> str:
    """Format one skill within its measured character budget.

    Character counts include the skill header, user arguments, truncation marker,
    and source footer. Paragraph and line boundaries are preferred over hard cuts.
    """
    header = f"[SKILL: {skill.name} ({skill.source})]"
    footer = f'\n\n(Source: cad_skill(name="{skill.name}"))'
    payload = skill.body.rstrip()
    if args.strip():
        payload += f"\n\nUser args: {args.strip()}"
    marker = (
        "\n\n[TRUNCATED: skill injection capped at "
        f"{max_chars} characters (~{max_chars // 4} tokens at 4 chars/token).]"
    )
    payload_limit = max(0, max_chars - len(header) - 1 - len(footer))
    payload = _truncate_semantically(payload, payload_limit, marker)
    return f"{header}\n{payload}{footer}"


def _format_load_diagnostic(diagnostic: SkillLoadDiagnostic) -> str:
    name = diagnostic.name or "<root>"
    text = "\n".join(
        [
            f"[SKILL LOAD ERROR: {name} ({diagnostic.source})]",
            f"kind: {diagnostic.kind}",
            f"path: {diagnostic.path}",
            f"message: {diagnostic.message}",
            "The higher-precedence skill name remains reserved; no lower-precedence "
            "skill was substituted.",
        ]
    )
    return _truncate_semantically(
        text,
        SKILL_BODY_CHAR_LIMIT,
        "\n[TRUNCATED: skill load diagnostic capped.]",
    )


# --------------------------------------------------------------------------- session activation state


def _session_key(session_id: str | None) -> str:
    return session_id or "_default"


def reset_session(session_id: str | None = None) -> None:
    """Clear one session's injected set, or every session's when *session_id* is None."""
    with _session_lock:
        if session_id is None:
            _session_injected.clear()
        else:
            _session_injected.pop(_session_key(session_id), None)


def already_injected(name: str, session_id: str | None = None) -> bool:
    with _session_lock:
        return name in _session_injected.get(_session_key(session_id), set())


def _mark_injected(name: str, session_id: str | None = None) -> None:
    with _session_lock:
        key = _session_key(session_id)
        _session_injected.setdefault(key, set()).add(name)


# --------------------------------------------------------------------------- triggers / activation


@lru_cache(maxsize=1024)
def _keyword_pattern(kw: str) -> re.Pattern[str]:
    """Compiled word-boundary matcher for one trigger keyword.

    Matching stays LITERAL per token — no stemming. That is deliberate: the
    single-word nouns in the builtin lists ("part", "shell", "hole") turn into
    false-positive machines in the plural ("archive old parts", "spare parts"),
    which is exactly what test_bare_part_keyword_does_not_match_plural_parts
    pins. Authors that need both forms list both (see edit-assembly's
    "mate, mates").

    The one liberty taken is whitespace: a run of spaces in the keyword matches
    any whitespace run, so a multi-word phrase trigger ("will this hold",
    "route the board", "check the part") still fires when the user's prompt
    wrapped or double-spaced between those words.
    """
    words = kw.lower().split()
    if not words:
        return re.compile(r"(?!x)x")  # never matches
    return re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")


def match_keyword_triggers(
    text: str,
    *,
    session_id: str | None = None,
    project_root: Path | str | None = None,
    role: str | None = None,
) -> list[Skill]:
    """Return auto skills whose keyword triggers fire on *text* (once per session)."""
    if not text:
        return []
    hay = text.lower()
    hits: list[Skill] = []
    for skill in discover_skills(project_root=project_root):
        if skill.inject != "auto":
            continue
        if skill.roles and role and role not in skill.roles:
            continue
        if already_injected(skill.name, session_id):
            continue
        for kw in skill.triggers.keywords:
            if kw and _keyword_pattern(kw).search(hay):
                hits.append(skill)
                break
    return hits


def match_tool_triggers(
    tool_name: str,
    *,
    session_id: str | None = None,
    project_root: Path | str | None = None,
    role: str | None = None,
) -> list[Skill]:
    """Return auto skills whose tool triggers match *tool_name* (once per session)."""
    if not tool_name:
        return []
    t = tool_name.strip()
    hits: list[Skill] = []
    for skill in discover_skills(project_root=project_root):
        if skill.inject != "auto":
            continue
        if skill.roles and role and role not in skill.roles:
            continue
        if already_injected(skill.name, session_id):
            continue
        if t in skill.triggers.tools:
            hits.append(skill)
    return hits


def match_phase_triggers(
    phase: str,
    *,
    session_id: str | None = None,
    project_root: Path | str | None = None,
    role: str | None = None,
) -> list[Skill]:
    """Return auto skills whose phase triggers match *phase* (once per session)."""
    if not phase:
        return []
    p = phase.strip().lower()
    hits: list[Skill] = []
    for skill in discover_skills(project_root=project_root):
        if skill.inject != "auto":
            continue
        if skill.roles and role and role not in skill.roles:
            continue
        if already_injected(skill.name, session_id):
            continue
        phases = {x.lower() for x in skill.triggers.phases}
        if p in phases:
            hits.append(skill)
    return hits


def activate_skill(
    name: str,
    *,
    session_id: str | None = None,
    project_root: Path | str | None = None,
    args: str = "",
    force: bool = False,
    role: str | None = None,
) -> Optional[str]:
    """Activate one skill within the per-body budget.

    A reserved candidate that failed to load returns its structured diagnostic;
    ``force`` only controls the once-per-session guard for valid skills.
    """
    skill = get_skill(name, project_root=project_root)
    if skill is None:
        diagnostic = _skill_load_diagnostic(name, project_root=project_root)
        if diagnostic is not None:
            return _format_load_diagnostic(diagnostic)
        return None
    if skill.roles and (not role or role not in skill.roles):
        return None
    if not force and already_injected(skill.name, session_id):
        return None
    text = format_skill_body(skill, args=args)
    if not text:
        return None
    _mark_injected(skill.name, session_id)
    return text


def activate_for_prompt(
    user_text: str,
    *,
    session_id: str | None = None,
    project_root: Path | str | None = None,
    phase: str | None = None,
    tools_about_to_run: Iterable[str] | None = None,
    role: str | None = None,
    _aggregate_char_limit: int = SKILL_INJECTION_CHAR_LIMIT,
) -> list[str]:
    """Collect budgeted bodies in explicit > keyword > phase > tool priority.

    A skill is marked injected only after its formatted body fits the aggregate
    block. Candidates that do not fit remain eligible on a later turn.
    """
    candidates: list[
        tuple[str, Skill | None, str, SkillLoadDiagnostic | None]
    ] = []
    seen: set[str] = set()

    # 1) Explicit slash commands take priority and always bypass once-per-session.
    cleaned, slash_hits = resolve_slash(user_text, project_root=project_root)
    for name, args in slash_hits:
        if name in seen:
            continue
        skill = get_skill(name, project_root=project_root)
        if skill is not None:
            if not skill.roles or (role and role in skill.roles):
                candidates.append((name, skill, args, None))
                seen.add(name)
        else:
            diagnostic = _skill_load_diagnostic(name, project_root=project_root)
            if diagnostic is not None:
                candidates.append((name, None, "", diagnostic))
                seen.add(name)

    # 2) Auto keyword triggers on the cleaned user text.
    for skill in match_keyword_triggers(
        cleaned, session_id=session_id, project_root=project_root, role=role,
    ):
        if skill.name not in seen:
            candidates.append((skill.name, skill, "", None))
            seen.add(skill.name)

    # 3) Phase triggers.
    if phase:
        for skill in match_phase_triggers(
            phase, session_id=session_id, project_root=project_root, role=role,
        ):
            if skill.name not in seen:
                candidates.append((skill.name, skill, "", None))
                seen.add(skill.name)

    # 4) Tool-about-to-run triggers.
    if tools_about_to_run:
        for tool in tools_about_to_run:
            for skill in match_tool_triggers(
                tool, session_id=session_id, project_root=project_root, role=role,
            ):
                if skill.name not in seen:
                    candidates.append((skill.name, skill, "", None))
                    seen.add(skill.name)

    formatted: list[tuple[str, Skill | None, str]] = []
    for name, skill, args, diagnostic in candidates:
        if skill is not None:
            text = format_skill_body(skill, args=args)
        else:
            assert diagnostic is not None
            text = _format_load_diagnostic(diagnostic)
        formatted.append((name, skill, text))

    full_size = sum(len(text) for _, _, text in formatted)
    full_size += max(0, len(formatted) - 1) * 2
    aggregate_limit = max(0, _aggregate_char_limit)
    if full_size <= aggregate_limit:
        for _, skill, _ in formatted:
            if skill is not None:
                _mark_injected(skill.name, session_id)
        return [text for _, _, text in formatted]

    # Reserve bounded space for an omission report before admitting candidates.
    report_reserve = min(_OMISSION_REPORT_RESERVE, aggregate_limit)
    content_limit = aggregate_limit - report_reserve
    bodies: list[str] = []
    injected_skills: list[Skill] = []
    omitted: list[str] = []
    used = 0
    for name, skill, text in formatted:
        separator = 2 if bodies else 0
        if used + separator + len(text) <= content_limit:
            bodies.append(text)
            used += separator + len(text)
            if skill is not None:
                injected_skills.append(skill)
        else:
            omitted.append(name)

    if omitted:
        report = "\n".join(
            [
                "[SKILLS OMITTED: injection character budget reached]",
                *(f"- {name}" for name in omitted),
                "Omitted skills were not marked injected and remain eligible later.",
            ]
        )
        report = _truncate_semantically(
            report,
            max(0, report_reserve - (2 if bodies else 0)),
            "\n[TRUNCATED: additional omitted skill names.]",
        )
        if report:
            bodies.append(report)
    for skill in injected_skills:
        _mark_injected(skill.name, session_id)
    return bodies


def session_system_block(
    session_id: str | None = None,
    *,
    project_root: Path | str | None = None,
    user_text: str = "",
    phase: str | None = None,
    tools_about_to_run: Iterable[str] | None = None,
    role: str | None = None,
) -> str:
    """Return catalog and activated bodies within the aggregate character cap."""
    parts: list[str] = []
    cat = catalog_block(project_root=project_root)
    if cat:
        parts.append(cat)
    if user_text or phase or tools_about_to_run:
        activation_budget = SKILL_INJECTION_CHAR_LIMIT - len(cat)
        if cat:
            activation_budget -= 2  # separator inserted by the final join
        bodies = activate_for_prompt(
            user_text or "",
            session_id=session_id,
            project_root=project_root,
            phase=phase,
            tools_about_to_run=tools_about_to_run,
            role=role,
            _aggregate_char_limit=max(0, activation_budget),
        )
        parts.extend(bodies)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- slash commands


def resolve_slash(
    text: str,
    *,
    project_root: Path | str | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Parse ``/skill:<name>`` and ``/<name>`` from *text*.

    Returns ``(cleaned_text, [(name, args), ...])``. Only known skill names are
    consumed; unknown ``/foo`` tokens are left in the text so other slash
    commands (if any) keep working. ``/skill:unknown`` is still stripped but
    yields no hit (caller can report).
    """
    if not text:
        return "", []
    catalog = _discover_catalog(project_root=project_root)
    known = (
        {skill.name for skill in catalog.skills}
        | {d.name for d in catalog.diagnostics if d.name}
    ) - RESERVED_SLASH_COMMANDS
    hits: list[tuple[str, str]] = []
    lines_out: list[str] = []

    for line in text.splitlines():
        m = _SLASH_SKILL_RE.match(line)
        if m:
            name = m.group(1).lower()
            args = (m.group(2) or "").strip()
            # /skill:name always treated as skill invocation (even if unknown).
            explicit = line.lstrip().lower().startswith("/skill:")
            if explicit or name in known:
                if name in known:
                    hits.append((name, args))
                # strip the command line either way for /skill:
                if explicit or name in known:
                    continue
        lines_out.append(line)

    cleaned = "\n".join(lines_out)
    # Mid-prompt /skill:name tokens
    def _mid_sub(m: re.Match[str]) -> str:
        name = m.group(1).lower()
        if name in known:
            hits.append((name, ""))
            return " "
        return m.group(0)

    cleaned = _MID_PROMPT_SKILL_RE.sub(_mid_sub, cleaned)
    # Collapse leftover whitespace from mid-prompt removals.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, hits


# --------------------------------------------------------------------------- /rule slash command
#
# `/rule <trigger words> -> <context to inject>` authors a Rule (aicad.trigger/1).
# Rules are NOT skills: they live in the trigger table, so `rule` is reserved here
# and a skill of that name can never shadow the command. Parsing lives beside the
# skill slash parser; the write lives in worker_rpc._rule_create (single writer:
# advisor.triggers.save_trigger / save_pack).

RULE_SLASH_ARROW = "->"
RULE_SLASH_USAGE = "/rule <trigger words> -> <context to inject>"

_RULE_SLASH_RE = re.compile(r"^\s*/rule\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)
_RULE_ID_STRIP_RE = re.compile(r"[^a-z0-9]+")


def rule_slug(text: str) -> str:
    """Slug *text* into an ``aicad.trigger/1`` id (``[A-Za-z][A-Za-z0-9_-]{0,63}``)."""
    slug = _RULE_ID_STRIP_RE.sub("_", str(text or "").lower()).strip("_")
    while slug and not slug[0].isalpha():
        slug = slug[1:].lstrip("_")
    return slug[:48].rstrip("_")


def parse_rule_slash(text: str) -> Optional[dict[str, Any]]:
    """Parse a ``/rule`` composer command.

    Returns ``None`` when *text* is not a ``/rule`` command at all. Otherwise a
    dict: ``{ok: True, id, keyword, instruction, purpose}`` or
    ``{ok: False, reason, usage}``.

    A rule with no trigger words can never fire and a rule with nothing to
    inject fires to no effect, so both halves are REQUIRED here rather than
    written out as a dead row.
    """
    m = _RULE_SLASH_RE.match(text or "")
    if not m:
        return None
    body = (m.group(1) or "").strip()
    bad = {"ok": False, "usage": RULE_SLASH_USAGE}
    if not body:
        return {**bad, "reason": "a rule needs trigger words and context to inject"}
    if RULE_SLASH_ARROW not in body:
        return {**bad, "reason": f"missing {RULE_SLASH_ARROW!r} separator"}
    keyword, _, instruction = body.partition(RULE_SLASH_ARROW)
    keyword = " ".join(keyword.split())
    instruction = instruction.strip()
    if not keyword:
        return {**bad, "reason": "no trigger words before the arrow — the rule could never fire"}
    if not instruction:
        return {**bad, "reason": "nothing to inject after the arrow"}
    rid = rule_slug(keyword)
    if not rid:
        return {**bad, "reason": f"cannot derive a rule id from {keyword!r}"}
    short = instruction if len(instruction) <= 72 else instruction[:69] + "..."
    return {
        "ok": True,
        "id": rid,
        "keyword": keyword,
        "instruction": instruction,
        "purpose": f"When {keyword!r} appears: {short}",
    }


# --------------------------------------------------------------------------- skill:// resolver


class SkillResolveError(ValueError):
    """skill:// resolution failed (unknown skill, traversal, missing file)."""


def _validate_relative(rel: str) -> str:
    if not rel or rel in (".",):
        return ""
    if rel.startswith("/") or re.match(r"^[A-Za-z]:[/\\]", rel):
        raise SkillResolveError("Absolute paths are not allowed in skill:// URLs")
    # Normalize and reject .. traversal
    parts: list[str] = []
    for p in rel.replace("\\", "/").split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise SkillResolveError("Path traversal (..) is not allowed in skill:// URLs")
        parts.append(p)
    return "/".join(parts)


def resolve_skill_uri(
    uri: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve ``skill://<name>`` or ``skill://<name>/<path>``.

    Returns ``{ok, name, path, content, content_type}`` or raises
    :class:`SkillResolveError`. Traversal-guarded: resolved path must stay
    inside the skill's base_dir.
    """
    raw = (uri or "").strip()
    if raw.startswith("skill://"):
        raw = raw[len("skill://"):]
    raw = raw.lstrip("/")
    if not raw:
        raise SkillResolveError("skill:// URL requires a skill name: skill://<name>")
    # name is first segment; rest is relative path
    if "/" in raw:
        name, rel = raw.split("/", 1)
    else:
        name, rel = raw, ""
    name = name.strip()
    if not name or not _NAME_RE.match(name):
        raise SkillResolveError(f"Invalid skill name: {name!r}")
    skill = get_skill(name, project_root=project_root)
    if skill is None:
        avail = ", ".join(s.name for s in discover_skills(project_root=project_root)) or "none"
        raise SkillResolveError(f"Unknown skill: {name}\nAvailable: {avail}")

    rel_clean = _validate_relative(rel)
    base = skill.base_dir.resolve()
    if rel_clean:
        target = (base / rel_clean).resolve()
    else:
        target = skill.path.resolve()

    # Traversal guard: target must be under base
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise SkillResolveError("Path traversal is not allowed") from exc

    if not target.exists():
        raise SkillResolveError(f"File not found: skill://{name}" + (f"/{rel_clean}" if rel_clean else ""))
    if target.is_dir():
        # Directory listing (names only)
        try:
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        except OSError as exc:
            raise SkillResolveError(f"Cannot list directory: {exc}") from exc
        content = "\n".join(entries)
        return {
            "ok": True,
            "name": name,
            "path": str(target),
            "content": content,
            "content_type": "text/plain",
            "is_directory": True,
        }
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillResolveError(f"Cannot read file: {exc}") from exc
    ctype = "text/markdown" if target.suffix.lower() == ".md" else "text/plain"
    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "content": content,
        "content_type": ctype,
        "is_directory": False,
    }


def skill_reference_text(
    name: str,
    path: str | None = None,
    *,
    project_root: Path | str | None = None,
) -> str:
    """Model-facing text for one skill pack: its SKILL.md, or one file inside it.

    This is what the ``cad_skill`` host tool returns, and it is the ONLY fetch
    path that works on the online (Chudcode) lane: there the ``skill://`` scheme
    is resolved by the runtime's own ``read`` builtin against its own unrelated
    skill registry, which answers "Unknown skill" for every CAD pack. Never
    raises — an unknown name comes back as the catalog of real names, because a
    model that asked for the wrong pack needs the right one, not a tool error.
    """
    ref = (name or "").strip()
    if ref.startswith("skill://"):
        ref = ref[len("skill://"):]
    ref = ref.strip("/")
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    if not ref:
        avail = ", ".join(s.name for s in discover_skills(project_root=project_root))
        return (
            "[SKILL NOT LOADED]\ncad_skill needs a pack name.\n"
            f"Available: {avail or 'none'}"
        )
    try:
        resolved = resolve_skill_uri(
            f"{ref}/{rel}" if rel else ref, project_root=project_root
        )
    except SkillResolveError as exc:
        return f"[SKILL NOT LOADED]\n{exc}"
    if not rel:
        skill = get_skill(ref, project_root=project_root)
        if skill is not None:
            return format_skill_body(skill)
    listing = " (directory listing)" if resolved.get("is_directory") else ""
    header = f"[SKILL FILE: {ref}/{rel}{listing}]"
    body = str(resolved.get("content") or "").rstrip()
    return _truncate_semantically(
        f"{header}\n{body}",
        SKILL_BODY_CHAR_LIMIT,
        f"\n\n[TRUNCATED: cad_skill capped at {SKILL_BODY_CHAR_LIMIT} characters "
        "— request one file at a time with path=.]",
    )


# --------------------------------------------------------------------------- user-dir CRUD (RPC backing)


def save_user_skill(name: str, body: str) -> dict[str, Any]:
    """Write/overwrite a skill in the user dir only. *body* is full SKILL.md text."""
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        return {"ok": False, "reason": f"invalid skill name: {name!r}"}
    if not isinstance(body, str) or not body.strip():
        return {"ok": False, "reason": "body required"}
    # Ensure frontmatter has name
    fm, md_body = parse_skill_md(body)
    if not fm.get("name"):
        # inject name into frontmatter
        if body.lstrip().startswith("---"):
            body = re.sub(
                r"\A---\s*\n",
                f"---\nname: {name}\n",
                body,
                count=1,
            )
        else:
            body = f"---\nname: {name}\ndescription: (user skill)\n---\n\n{body}"
        fm, md_body = parse_skill_md(body)
    if not str(fm.get("description") or "").strip():
        return {"ok": False, "reason": "frontmatter description is required"}
    # Force name match
    if str(fm.get("name")).strip() != name:
        return {"ok": False, "reason": f"frontmatter name {fm.get('name')!r} must match {name!r}"}
    dest_dir = user_skills_dir() / name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / _SKILL_FILENAME).write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"write failed: {exc}"}
    clear_cache(user_root=user_skills_dir())
    return {"ok": True, "name": name, "path": str(dest_dir / _SKILL_FILENAME)}


def delete_user_skill(name: str) -> dict[str, Any]:
    """Delete a skill from the user dir only (never project/builtin)."""
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        return {"ok": False, "reason": f"invalid skill name: {name!r}"}
    dest_dir = user_skills_dir() / name
    skill_md = dest_dir / _SKILL_FILENAME
    if not skill_md.is_file():
        return {"ok": False, "reason": f"user skill not found: {name!r}"}
    try:
        skill_md.unlink()
        # Remove empty dir; leave other resources if any were added manually.
        try:
            # Remove remaining files we own carefully — only SKILL.md required.
            if dest_dir.is_dir() and not any(dest_dir.iterdir()):
                dest_dir.rmdir()
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "reason": f"delete failed: {exc}"}
    clear_cache(user_root=user_skills_dir())
    return {"ok": True, "name": name}


def get_skill_payload(name: str, *, project_root: Path | str | None = None) -> dict[str, Any]:
    """RPC shape for skills_get."""
    skill = get_skill(name, project_root=project_root)
    if skill is None:
        return {"ok": False, "reason": f"skill not found: {name!r}"}
    files: list[str] = []
    try:
        for p in sorted(skill.base_dir.rglob("*")):
            if p.is_file():
                try:
                    rel = str(p.relative_to(skill.base_dir)).replace("\\", "/")
                except ValueError:
                    continue
                # skip traversal-ish
                if ".." in rel.split("/"):
                    continue
                files.append(rel)
    except OSError:
        files = [_SKILL_FILENAME]
    return {
        "ok": True,
        "name": skill.name,
        "description": skill.description,
        "source": skill.source,
        "inject": skill.inject,
        "triggers": skill.triggers.as_dict(),
        "body": skill.raw,
        "files": files,
    }
