#!/usr/bin/env python3
"""Machine-checkable SolidWorks-parity audit for Chudware.

A "typechecker" for the parity standard in docs/SW_PARITY_SPEC.md. Every capability row there
carries a status (`done` / `partial` / `missing` / `beyond`) and an Evidence cell naming the code
that backs it. Prose drifts — a row can stay `done` after its feature type is removed, a ribbon
command deleted, or an RPC method retired. This script re-derives the artifact corpus FROM THE
LIVE CODE and checks the spec's evidence pointers against it.

It is deliberately a SUBSET check. An artifact existing never proves a SolidWorks acceptance
condition ("Newton/LM solver with analytic Jacobians", "removes 3-4 DOF"); those stay human
judgment. What IS checkable is that the artifacts a row names actually exist:

  * compiler — a key of ``FEATURE_TYPES`` (core/doc/validate.cpp) that the compiler really
               dispatches (a ``type``/``ftype`` comparison literal in core/doc/document.cpp or
               core/doc/assembly.cpp), or an apply-time macro (``expand_<type>(`` in
               core/doc/session.cpp). Registry-only types (validated but never dispatched) are
               reported separately and are NOT compiler evidence.
  * op       — a document op kind in ``kDocOpKinds`` (core/doc/op_registry.cpp) or an ECAD op
               kind in ``kEcadOpKinds`` (core/ecad/op_registry.cpp).
  * rpc      — a libchudware dispatch literal (``m == "<method>"`` in core/api/libchudware.cpp);
               its ``kModeling`` profiler list is cross-checked, not treated as independent dispatch;
               or a key of a Python services-worker table that services/worker_main.py
               registers (its ``_METHODS`` literal plus every ``_register(...)`` module table,
               following ``.update(...)`` merges, ``**`` splats, subscript assignments and
               name-list comprehensions).
  * ribbon   — a cmd_id row in host/src/ui/ribbon_def.cpp.
  * pm_form  — an actual form row in host/src/ui/pm_forms.cpp, not just a lookup call.

Two ways a row names an artifact:

  * EXPLICIT pointer — a kind keyword immediately followed by backticked ids or bare
    snake_case ids (also single-word pm_form ids), slash-separated: ``compiler `extrude```,
    ``op sketch_trim``, ``rpc `doc_push_pull```, ``ribbon sm_hem/sm_jog``, ``pm_form boolean``.
    In every row except ``missing`` ones, each id MUST exist in its corpus; absence is DRIFT.
  * implicit token — any multi-segment snake_case identifier in the capability or evidence text
    that happens to be a harvested artifact id. It corroborates a claim but can never be drift
    (prose may name things that are not artifacts).
Repo-relative source paths in evidence cells are also checked for file presence in
non-missing rows. A present source file alone does not increase artifact coverage.

A ``done``/``beyond`` row is `verified` when it names at least one existing artifact and
`unverifiable` when it names none — prose evidence is counted, never treated as failure.
``missing`` rows are not checked: they may legitimately name absent or present artifacts.

Usage:
  python scripts/parity_audit.py                  # text scorecard, exit 0 (clean) / 1 (drift)
  python scripts/parity_audit.py --report md      # markdown report (docs / PR comment)
  python scripts/parity_audit.py --area Sketcher  # restrict to one area (substring match)
  python scripts/parity_audit.py --list-evidence  # dump the harvested code-evidence corpus

Exit code: 0 = no drift, 1 = drift found, 2 = bad invocation, unreadable spec, a missing evidence
source, an unresolvable RPC table, or an empty harvest. Python standard library only.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(REPO, "docs", "SW_PARITY_SPEC.md")

# Live sources of truth that back the spec's evidence cells. Every one is mandatory.
VALIDATE = os.path.join(REPO, "core", "doc", "validate.cpp")
DOCUMENT = os.path.join(REPO, "core", "doc", "document.cpp")
ASSEMBLY = os.path.join(REPO, "core", "doc", "assembly.cpp")
SESSION = os.path.join(REPO, "core", "doc", "session.cpp")
DOC_OPS = os.path.join(REPO, "core", "doc", "op_registry.cpp")
ECAD_OPS = os.path.join(REPO, "core", "ecad", "op_registry.cpp")
LIBCHUDWARE = os.path.join(REPO, "core", "api", "libchudware.cpp")
WORKER_MAIN = os.path.join(REPO, "services", "worker_main.py")
RIBBON = os.path.join(REPO, "host", "src", "ui", "ribbon_def.cpp")
PM_FORMS = os.path.join(REPO, "host", "src", "ui", "pm_forms.cpp")

STATUSES = ("done", "partial", "missing", "beyond")
# Statuses that make an affirmative, falsifiable code claim and are scored for coverage.
CLAIM_STATUSES = ("done", "beyond")
# Explicit pointers in these rows must resolve. `missing` rows only describe absence.
POINTER_CHECKED_STATUSES = ("done", "beyond", "partial")
KINDS = ("compiler", "op", "rpc", "ribbon", "pm_form")


class SourceError(RuntimeError):
    """A mandatory evidence source is missing, unparseable, or yielded nothing."""


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO)


# Preserve C++ strings/character literals while excluding commented-out artifacts.
# Keep newlines so table-boundary patterns retain their source-line semantics.
CPP_COMMENT_RE = re.compile(
    r'R"(?P<delimiter>[^\s()\\]{0,16})\(.*?\)(?P=delimiter)"'
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r'|(?P<comment>//[^\n]*|/\*.*?\*/)', re.S)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if path.endswith(".cpp"):
            text = CPP_COMMENT_RE.sub(
                lambda m: "\n" * m["comment"].count("\n") if m["comment"] is not None else m[0],
                text)
        return text
    except OSError as exc:
        raise SourceError(f"evidence source unreadable: {_rel(path)} ({exc.strerror})") from exc


def _nonempty(label: str, items: set[str], source: str) -> set[str]:
    if not items:
        raise SourceError(f"harvested 0 {label} from {source} — source format changed?")
    return items


def _c_string_array(path: str, name: str) -> set[str]:
    """String literals of a ``const char* const <name>[] = { ... };`` table."""
    text = _read(path)
    m = re.search(r'\b' + re.escape(name) + r'\s*\[\s*\]\s*=\s*\{(.*?)\n\};', text, re.S)
    if not m:
        raise SourceError(f"{name} table not found in {_rel(path)}")
    return _nonempty(name, set(re.findall(r'"([a-z][a-z0-9_]*)"', m.group(1))), _rel(path))


# ====================================================================================================
# 1. Harvest the code-evidence corpus.
# ====================================================================================================

# --- feature types: FEATURE_TYPES keys, split by whether the compiler really dispatches them -------
FEATURE_TYPES_RE = re.compile(r'\bFEATURE_TYPES\s*=\s*\{(.*?)\n\};', re.S)
FEATURE_KEY_RE = re.compile(r'^\s*\{"([a-z][a-z0-9_]*)",\s*\{', re.M)
FEATURE_DISPATCH_RE = re.compile(r'\b(?:type|ftype)\s*==\s*"([a-z][a-z0-9_]*)"')


def harvest_feature_types() -> tuple[set[str], set[str], set[str]]:
    """(compiled, macros, registry_only) feature types.

    compiled      — FEATURE_TYPES keys with a dispatch literal in document.cpp / assembly.cpp.
    macros        — FEATURE_TYPES keys expanded at apply time by ``expand_<type>(`` in session.cpp
                    (they never reach the compiler as themselves).
    registry_only — validated keys with neither: accepted by validation, backed by no handler.
    """
    m = FEATURE_TYPES_RE.search(_read(VALIDATE))
    if not m:
        raise SourceError(f"FEATURE_TYPES map not found in {_rel(VALIDATE)}")
    keys = _nonempty("FEATURE_TYPES keys", set(FEATURE_KEY_RE.findall(m.group(1))), _rel(VALIDATE))
    dispatched = _nonempty("document dispatch literals",
                           set(FEATURE_DISPATCH_RE.findall(_read(DOCUMENT))), _rel(DOCUMENT))
    dispatched |= _nonempty("assembly dispatch literals",
                            set(FEATURE_DISPATCH_RE.findall(_read(ASSEMBLY))), _rel(ASSEMBLY))
    session = _read(SESSION)
    macros = {"standard_part"} & keys if re.search(
        r'if\s*\(\s*ftype_early\s*==\s*"standard_part"\s*\).*?\bexpand_standard_part\s*\(',
        session, re.S) else set()
    _nonempty("apply-time feature macros", macros, _rel(SESSION))
    compiled = (keys & dispatched) - macros
    _nonempty("dispatched feature compilers", compiled, f"{_rel(DOCUMENT)} / {_rel(ASSEMBLY)}")
    return compiled, macros, keys - compiled - macros


# --- document + ECAD op kinds -------------------------------------------------------------------------

def harvest_ops() -> tuple[set[str], set[str]]:
    return _c_string_array(DOC_OPS, "kDocOpKinds"), _c_string_array(ECAD_OPS, "kEcadOpKinds")


# --- libchudware RPC dispatch -------------------------------------------------------------------------
# kModeling is a profiler list, not a dispatch table: stale profiler entries must not
# manufacture RPC evidence. Require the list and corroborate each entry against dispatch.
NATIVE_RPC_RE = re.compile(r'\bm\s*==\s*"([a-z][a-z0-9_]*)"')
MODELING_RE = re.compile(r'\bkModeling\s*=\s*\{\{(.*?)\}\};', re.S)


def harvest_native_rpc() -> tuple[set[str], set[str]]:
    text = _read(LIBCHUDWARE)
    methods = _nonempty("libchudware dispatch literals",
                        set(NATIVE_RPC_RE.findall(text)), _rel(LIBCHUDWARE))
    modeling = MODELING_RE.search(text)
    if not modeling:
        raise SourceError(f"kModeling table not found in {_rel(LIBCHUDWARE)}")
    names = _nonempty("kModeling entries", set(re.findall(
        r'"([a-z][a-z0-9_]*)"', modeling.group(1))), _rel(LIBCHUDWARE))
    # The profiler list includes legacy names: only its dispatched intersection is evidence.
    return methods, _nonempty("dispatched kModeling entries", names & methods, _rel(LIBCHUDWARE))


# --- Python services-worker RPC tables (static, via ast; nothing is imported) ------------------------

def _module_file(module: str) -> str | None:
    base = os.path.join(REPO, *module.split("."))
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _module_statements(body: list[ast.stmt]):
    """Module-level statements, descending into try/if/with blocks but not defs or loops."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.For, ast.AsyncFor, ast.While)):
            continue
        yield node
        for field in ("body", "orelse", "finalbody"):
            sub = getattr(node, field, None)
            if isinstance(sub, list):
                yield from _module_statements(sub)
        for handler in getattr(node, "handlers", ()):
            yield from _module_statements(handler.body)


def _assigned_value(stmt: ast.stmt, name: str) -> ast.expr | None:
    if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
        return stmt.value
    if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name and stmt.value is not None):
        return stmt.value
    return None


class _PyModule:
    """One parsed module: its module-level statements and from-import aliases."""

    _cache: dict[str, "_PyModule"] = {}

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            tree = ast.parse(_read(path), path)
        except SyntaxError as exc:
            raise SourceError(f"cannot parse {_rel(path)}: {exc}") from exc
        self.stmts = list(_module_statements(tree.body))
        self.imports: dict[str, tuple[str, str]] = {}
        for node in self.stmts:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                pkg = os.path.dirname(path)
                for _ in range(node.level - 1):
                    pkg = os.path.dirname(pkg)
                module = _rel(pkg).replace(os.sep, ".")
                if node.module:
                    module += "." + node.module
            else:
                module = node.module or ""
            for alias in node.names:
                self.imports[alias.asname or alias.name] = (module, alias.name)

    @classmethod
    def load(cls, path: str) -> "_PyModule":
        if path not in cls._cache:
            cls._cache[path] = cls(path)
        return cls._cache[path]

    def defines(self, name: str) -> bool:
        return any(_assigned_value(s, name) is not None for s in self.stmts)

    def fail(self, msg: str) -> SourceError:
        return SourceError(f"{_rel(self.path)}: {msg}")


def _py_strings(mod: _PyModule, node: ast.expr) -> set[str]:
    """String members of a literal tuple/list/set (or a module-level name bound to one)."""
    if isinstance(node, ast.Name):
        for stmt in mod.stmts:
            value = _assigned_value(stmt, node.id)
            if value is not None:
                return _py_strings(mod, value)
        raise mod.fail(f"cannot resolve name list {node.id}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 \
            and node.func.id in ("tuple", "list", "set", "frozenset"):
        return _py_strings(mod, node.args[0])
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return {e.value for e in node.elts}
    raise mod.fail(f"unsupported RPC name list: {ast.unparse(node)}")


def _py_table_value(mod: _PyModule, node: ast.expr, into: str, seen: set) -> set[str]:
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for k, v in zip(node.keys, node.values):
            if k is None:  # **splat
                keys |= _py_table_value(mod, v, into, seen)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                raise mod.fail(f"non-literal RPC key in {into}: {ast.unparse(k)}")
        return keys
    if isinstance(node, ast.DictComp) and len(node.generators) == 1:
        gen = node.generators[0]
        if isinstance(node.key, ast.Name) and isinstance(gen.target, ast.Name) \
                and node.key.id == gen.target.id and not gen.ifs:
            return _py_strings(mod, gen.iter)
    if isinstance(node, ast.Name):
        if mod.defines(node.id):
            return _py_table_keys(mod.path, node.id, seen)
        if node.id in mod.imports:
            module, attr = mod.imports[node.id]
            path = _module_file(module)
            if path is None:
                raise mod.fail(f"{node.id} is imported from missing module {module}")
            return _py_table_keys(path, attr, seen)
    raise mod.fail(f"cannot resolve RPC table expression merged into {into}: {ast.unparse(node)}")


def _py_table_keys(path: str, name: str, seen: set | None = None) -> set[str]:
    """Keys of module-level dispatch table ``name`` in ``path``, following its merges."""
    seen = set() if seen is None else set(seen)
    if (path, name) in seen:
        raise SourceError(f"cyclic RPC table reference: {_rel(path)}.{name}")
    seen.add((path, name))
    mod = _PyModule.load(path)
    keys: set[str] = set()
    defined = False
    for stmt in mod.stmts:
        value = _assigned_value(stmt, name)
        if value is not None:
            defined = True
            keys |= _py_table_value(mod, value, name, seen)
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if (isinstance(func, ast.Attribute) and func.attr == "update"
                    and isinstance(func.value, ast.Name) and func.value.id == name):
                for arg in stmt.value.args:
                    keys |= _py_table_value(mod, arg, name, seen)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == name and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    keys.add(target.slice.value)
    if not defined:
        raise mod.fail(f"no module-level RPC table {name}")
    return keys


def _call_arg(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def harvest_python_rpc() -> dict[str, set[str]]:
    """Per-table keys of every RPC table services/worker_main.py registers."""
    main = _PyModule.load(WORKER_MAIN)
    tables = {"worker_main._METHODS": _py_table_keys(WORKER_MAIN, "_METHODS")}
    for stmt in main.stmts:
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_register"):
            continue
        module = _call_arg(call, 1, "import_path")
        attr = _call_arg(call, 2, "attr")
        if not (isinstance(module, ast.Constant) and isinstance(module.value, str)
                and isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            raise main.fail(f"unsupported _register call: {ast.unparse(call)}")
        path = _module_file(module.value)
        if path is None:
            raise main.fail(f"_register names missing module {module.value}")
        tables[f"{module.value}.{attr.value}"] = _py_table_keys(path, attr.value)
    if len(tables) == 1:
        raise main.fail("no _register(...) RPC tables found — registration format changed?")
    for label, keys in tables.items():
        _nonempty(f"RPC methods in {label}", keys, _rel(WORKER_MAIN))
    return tables


# --- ribbon commands: rows in ribbon_def.cpp --------------------------------------------------------
# Row shape: { "cmd_id", "Label", "icon", "Tab", "Group", big, sub_of, stretch }
RIBBON_ROW_RE = re.compile(r'^\s*\{\s*"([a-zA-Z0-9_]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,', re.M)


def harvest_ribbon() -> set[str]:
    """Every cmd_id present as a row in the ribbon table (visible OR stretch-reserved)."""
    return _nonempty("ribbon rows", set(RIBBON_ROW_RE.findall(_read(RIBBON))), _rel(RIBBON))


# --- PM forms: actual form-type keys in pm_forms.cpp -------------------------------------------------
# kForms rows look like:  { "box", "Box", "extrude_boss", { ...fields... }},
PM_FORM_ROW_RE = re.compile(r'^\s*\{\s*"([a-zA-Z0-9_]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*\{', re.M)


def harvest_pm_forms() -> set[str]:
    """Every PropertyManager form-type defined by the host (lookup calls alone are not forms)."""
    text = _read(PM_FORMS)
    forms = set(PM_FORM_ROW_RE.findall(text))
    return _nonempty("PM forms", forms, _rel(PM_FORMS))


class Evidence:
    """The harvested code corpus, with helpers to classify + look up a token."""

    def __init__(self) -> None:
        self.compilers, self.macros, self.registry_only = harvest_feature_types()
        self.doc_ops, self.ecad_ops = harvest_ops()
        self.native_rpc, self.modeling_rpc = harvest_native_rpc()
        self.python_rpc = harvest_python_rpc()
        self.ribbon = harvest_ribbon()
        self.pm_forms = harvest_pm_forms()
        rpc: set[str] = set(self.native_rpc)
        for keys in self.python_rpc.values():
            rpc |= keys
        self.by_kind: dict[str, set[str]] = {
            "compiler": self.compilers | self.macros,
            "op": self.doc_ops | self.ecad_ops,
            "rpc": rpc,
            "ribbon": self.ribbon,
            "pm_form": self.pm_forms,
        }

    def classify(self, tok: str) -> str | None:
        """The artifact kind a token belongs to, or None if it is not a harvested artifact id."""
        for kind in KINDS:
            if tok in self.by_kind[kind]:
                return kind
        return None


# ====================================================================================================
# 2. Parse the spec into capability rows.
# ====================================================================================================

# An area header: "## 1. Sketcher (2D) — 80%".  We strip the leading number and trailing "— NN%".
AREA_RE = re.compile(r'^##\s+\d+\.\s+(.*?)\s*(?:—.*)?$')
# A capability table row: | Capability | acceptance | status | evidence |
ROW_RE = re.compile(r'^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$')
# Status cell may be wrapped in ** ** for emphasis (the partial/missing rows are).
STATUS_RE = re.compile(r'\*{0,2}(done|partial|missing|beyond)\*{0,2}', re.I)


class Row:
    """One capability row from a spec area table."""

    __slots__ = ("area", "capability", "acceptance", "status", "evidence", "line")

    def __init__(self, area, capability, acceptance, status, evidence, line):
        self.area = area
        self.capability = capability
        self.acceptance = acceptance
        self.status = status
        self.evidence = evidence
        self.line = line


def parse_spec() -> list[Row]:
    text = _read(SPEC)
    rows: list[Row] = []
    area = None
    for n, line in enumerate(text.splitlines(), 1):
        am = AREA_RE.match(line)
        if am:
            area = am.group(1).strip()
            continue
        if area is None or not line.lstrip().startswith("|"):
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        cap, acc, status_cell, evid = m.groups()
        # Skip the table header row and the |---|---| separator.
        if cap.lower() in ("capability", ":---", "---") or set(cap) <= {"-", ":", " "}:
            continue
        sm = STATUS_RE.search(status_cell)
        if not sm:
            continue  # not a real capability row (e.g. the scorecard summary table)
        rows.append(Row(area, cap, acc, sm.group(1).lower(), evid, n))
    if not rows:
        raise SourceError(f"parsed 0 capability rows from {_rel(SPEC)} — spec format changed?")
    return rows


# ====================================================================================================
# 3. Match a row's named artifacts to the harvested evidence, and decide drift.
# ====================================================================================================

# Bare snake_case tokens are unambiguous; single-word ids require backticks except
# the existing pm_form notation. Do not parse "op exists" / "compiler resolution" prose.
_ID = r'(?:`[a-z][a-z0-9_]*`|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)'
POINTER_RE = re.compile(r'\b(' + "|".join(KINDS) + r')\s+(' + _ID + r'(?:\s*/\s*' + _ID + r')*)'
                        r'|\b(pm_form)\s+([a-z][a-z0-9_]*(?:\s*/\s*[a-z][a-z0-9_]*)*)')

# Strip source filenames (`sketch_mode.cpp`, `document.cpp`) BEFORE tokenising — a filename stem
# is not an artifact id. Also strip C++ scope-qualified names (`SketchTool::Line`).
FILENAME_RE = re.compile(r'\b[A-Za-z0-9_./]+\.(?:cpp|py|h|hpp|cc|md|json)\b')
CPP_SCOPED_RE = re.compile(r'\b[A-Za-z_]\w*::[A-Za-z_]\w*\b')
# A bare snake_case identifier (>=2 segments) — the shape of every implicit artifact id.
IDENT_RE = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b')


def explicit_pointers(row: Row) -> list[tuple[str, str]]:
    """(kind, id) for every explicit pointer in the evidence cell."""
    out: list[tuple[str, str]] = []
    for kind, ids, form_kind, form_ids in POINTER_RE.findall(row.evidence):
        out.extend((kind or form_kind, tok.strip("`"))
                   for tok in re.split(r'\s*/\s*', ids or form_ids))
    return out


def _implicit_tokens(row: Row) -> set[str]:
    text = f"{row.capability} {row.evidence}"
    text = FILENAME_RE.sub(" ", text)
    text = CPP_SCOPED_RE.sub(" ", text)
    return set(IDENT_RE.findall(text.lower()))


def audit_row(row: Row, ev: Evidence) -> dict:
    """Classify a single spec row against the code evidence.

    Returns a dict: {result, found:[...], drift:[...]}.
      result = "verified"      — names at least one artifact that exists (acceptance unchecked)
               "unverifiable"  — a done/beyond row naming no harvested artifact (counted, not drift)
               "drift"         — an explicit pointer in a non-missing row resolves to nothing
               "n/a"           — partial/missing row with no drift
    """
    found: set[tuple[str, str]] = set()
    drift: list[tuple[str, str]] = []
    for kind, tok in explicit_pointers(row):
        if tok in ev.by_kind[kind]:
            found.add((tok, kind))
        elif row.status in POINTER_CHECKED_STATUSES:
            drift.append((tok, kind))
    if row.status in POINTER_CHECKED_STATUSES:
        for path in sorted(set(FILENAME_RE.findall(row.evidence))):
            if "/" in path and not os.path.isfile(os.path.join(REPO, path)):
                drift.append((path, "source"))
    for tok in _implicit_tokens(row):
        kind = ev.classify(tok)
        if kind:
            found.add((tok, kind))

    if drift:
        result = "drift"
    elif row.status not in CLAIM_STATUSES:
        result = "n/a"
    elif found:
        result = "verified"
    else:
        result = "unverifiable"
    return {"result": result, "found": sorted(found), "drift": drift}


# ====================================================================================================
# 4. Scorecard + reporting.
# ====================================================================================================

class AreaScore:
    __slots__ = ("area", "done", "partial", "missing", "beyond", "verified",
                 "unverifiable", "drift", "drift_rows")

    def __init__(self, area: str) -> None:
        self.area = area
        self.done = self.partial = self.missing = self.beyond = 0
        self.verified = self.unverifiable = self.drift = 0
        self.drift_rows: list[tuple[Row, dict]] = []

    @property
    def total(self) -> int:
        return self.done + self.partial + self.missing + self.beyond

    @property
    def parity(self) -> float:
        """Same formula the spec uses: (done + beyond + 0.5*partial) / total."""
        if not self.total:
            return 0.0
        return (self.done + self.beyond + 0.5 * self.partial) / self.total

    @property
    def claims(self) -> int:
        """done + beyond rows = the rows that make a falsifiable code claim."""
        return self.done + self.beyond

    @property
    def coverage(self) -> float:
        """Fraction of claim rows that name at least one existing artifact."""
        if not self.claims:
            return 1.0
        return self.verified / self.claims


def score(rows: list[Row], ev: Evidence) -> dict[str, AreaScore]:
    scores: dict[str, AreaScore] = {}
    for row in rows:
        sc = scores.setdefault(row.area, AreaScore(row.area))
        setattr(sc, row.status, getattr(sc, row.status) + 1)
        res = audit_row(row, ev)
        if res["result"] == "verified":
            sc.verified += 1
        elif res["result"] == "unverifiable":
            sc.unverifiable += 1
        elif res["result"] == "drift":
            sc.drift += 1
            sc.drift_rows.append((row, res))
    return scores


def _total(scores: dict[str, AreaScore]) -> AreaScore:
    tot = AreaScore("TOTAL")
    for sc in scores.values():
        for f in ("done", "partial", "missing", "beyond", "verified", "unverifiable", "drift"):
            setattr(tot, f, getattr(tot, f) + getattr(sc, f))
        tot.drift_rows.extend(sc.drift_rows)
    return tot


def _bar(frac: float, width: int = 14) -> str:
    fill = int(round(frac * width))
    return "#" * fill + "-" * (width - fill)


def report_text(scores: dict[str, AreaScore], total_rows: int) -> None:
    print("SolidWorks parity audit — evidence pointers checked against live code\n")
    header = (f"{'Area':<28}{'parity':>8}{'claims':>8}{'verifd':>8}"
              f"{'unver':>7}{'DRIFT':>7}  coverage")
    print(header)
    print("-" * len(header))
    for area in sorted(scores):
        sc = scores[area]
        flag = "  <-- DRIFT" if sc.drift else ""
        print(f"{sc.area[:27]:<28}{sc.parity*100:>7.0f}%{sc.claims:>8}{sc.verified:>8}"
              f"{sc.unverifiable:>7}{sc.drift:>7}  {_bar(sc.coverage)} {sc.coverage*100:>3.0f}%{flag}")
    tot = _total(scores)
    print("-" * len(header))
    print(f"{'TOTAL':<28}{tot.parity*100:>7.0f}%{tot.claims:>8}{tot.verified:>8}"
          f"{tot.unverifiable:>7}{tot.drift:>7}  {_bar(tot.coverage)} {tot.coverage*100:>3.0f}%")

    print(f"\n{total_rows} capability rows parsed; {tot.claims} make a done/beyond code claim "
          f"({tot.verified} name an existing artifact, {tot.unverifiable} prose-only). "
          "An existing artifact does not prove the acceptance condition.")

    if tot.drift_rows:
        print(f"\nDRIFT — {len(tot.drift_rows)} row(s) carry an explicit pointer absent from code:")
        for row, res in tot.drift_rows:
            miss = ", ".join(f"{k} `{t}`" for t, k in res["drift"])
            print(f"  [{row.area}] {row.capability}")
            print(f"      SW_PARITY_SPEC.md:{row.line}  status={row.status}  missing: {miss}")
    else:
        print("\nNo drift: every explicit evidence pointer resolved to live code.")


def report_md(scores: dict[str, AreaScore], total_rows: int) -> None:
    print("# SolidWorks parity audit\n")
    print("Evidence-pointer check for `docs/SW_PARITY_SPEC.md`. *Coverage* = fraction of "
          "`done`/`beyond` rows naming at least one existing artifact (compiler / op / RPC / "
          "ribbon command / PM form). It does not prove the SolidWorks acceptance condition.\n")
    print("| Area | Parity % | Claims | Verified | Unverifiable | Drift | Coverage |")
    print("|------|---------:|-------:|---------:|-------------:|------:|---------:|")
    for area in sorted(scores):
        sc = scores[area]
        print(f"| {sc.area} | {sc.parity*100:.0f}% | {sc.claims} | {sc.verified} | "
              f"{sc.unverifiable} | {sc.drift} | {sc.coverage*100:.0f}% |")
    tot = _total(scores)
    print(f"| **TOTAL** | **{tot.parity*100:.0f}%** | **{tot.claims}** | **{tot.verified}** | "
          f"**{tot.unverifiable}** | **{tot.drift}** | **{tot.coverage*100:.0f}%** |")
    print(f"\n_{total_rows} capability rows parsed._\n")
    if tot.drift_rows:
        print(f"\n## Drift ({len(tot.drift_rows)})\n")
        print("A row carries an explicit evidence pointer whose artifact is **absent** from the "
              "code tree:\n")
        for row, res in tot.drift_rows:
            miss = ", ".join(f"{k} `{t}`" for t, k in res["drift"])
            print(f"- **{row.area} — {row.capability}** "
                  f"(`SW_PARITY_SPEC.md:{row.line}`, status `{row.status}`): missing {miss}")
    else:
        print("\n## Drift (0)\n\nNo drift — every explicit evidence pointer resolved to live code.")


def report_evidence(ev: Evidence) -> None:
    print("Harvested code-evidence corpus:\n")
    sections = [
        (f"compilers (FEATURE_TYPES dispatched in {_rel(DOCUMENT)} / {_rel(ASSEMBLY)})",
         ev.compilers),
        (f"feature macros (expand_<type> in {_rel(SESSION)})", ev.macros),
        ("registry-only feature types (validated, no handler; NOT compiler evidence)",
         ev.registry_only),
        (f"document ops (kDocOpKinds, {_rel(DOC_OPS)})", ev.doc_ops),
        (f"ECAD ops (kEcadOpKinds, {_rel(ECAD_OPS)})", ev.ecad_ops),
        (f"native RPC (dispatch literals, {_rel(LIBCHUDWARE)})", ev.native_rpc),
        (f"modeling RPC (kModeling corroborated by dispatch, {_rel(LIBCHUDWARE)})",
         ev.modeling_rpc),
    ]
    sections += [(f"python RPC ({label})", keys) for label, keys in ev.python_rpc.items()]
    sections += [(f"ribbon commands ({_rel(RIBBON)})", ev.ribbon),
                 (f"PM forms ({_rel(PM_FORMS)})", ev.pm_forms)]
    for label, items in sections:
        print(f"## {label} — {len(items)}")
        for it in sorted(items):
            print(f"  {it}")
        print()


# ====================================================================================================
# 5. main
# ====================================================================================================

def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Chudware SolidWorks-parity drift auditor")
    ap.add_argument("--report", default="text", choices=["text", "md"],
                    help="output format (default text)")
    ap.add_argument("--area", default=None,
                    help="restrict to areas whose name contains this substring (case-insensitive)")
    ap.add_argument("--list-evidence", action="store_true",
                    help="dump the harvested code-evidence corpus and exit")
    args = ap.parse_args(argv)

    try:
        ev = Evidence()
        if args.list_evidence:
            report_evidence(ev)
            return 0
        rows = parse_spec()
    except SourceError as exc:
        print(f"parity_audit: FATAL — {exc}", file=sys.stderr)
        return 2

    if args.area:
        needle = args.area.lower()
        rows = [r for r in rows if needle in r.area.lower()]
        if not rows:
            print(f"parity_audit: no area matched '{args.area}'", file=sys.stderr)
            return 2

    scores = score(rows, ev)
    if args.report == "md":
        report_md(scores, len(rows))
    else:
        report_text(scores, len(rows))

    drift = sum(sc.drift for sc in scores.values())
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
