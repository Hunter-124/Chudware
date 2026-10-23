#!/usr/bin/env python3
"""oracle_import_audit — no authored Python may import the deleted engine package.

The Python->C++ migration is complete: ``engine_oracle/`` and its repo-root
``engine`` symlink are deleted, so any

    from engine... import ...   /   import engine...
    from engine_oracle... ...   /   import engine_oracle...

is a broken import. This gate FAILS on every such import in the authored Python roots
``services/``, ``scripts/`` and ``tools/`` whose target module is NOT covered by
``scripts/oracle_import_allowlist.txt`` (one dotted module path prefix per line; ``#``
comments allowed; intentionally EMPTY). Imports inside ``try:`` fallbacks count too:
the module they reach for no longer exists. Retarget to ``core_bridge`` / the C++ RPC
surface / the ``services.*`` re-home instead of allowlisting.

Skipped directories: ``__pycache__``, dot-dirs (``.venv``, ``.pytest_cache``), vendored
trees (``vendor``, ``third_party``, ``node_modules``) and ``services/dist`` (staged
runtime output from ``./build.sh stage``). A missing scan root or a scan that finds no
Python files is an invocation error (exit 2), never a clean pass.

Usage:
    python scripts/oracle_import_audit.py     # text report, exit 0 (clean) / 1 (violations)

Exit code: 0 = clean, 1 = disallowed imports found, 2 = invocation error. Stdlib only
(AST parse — no imports of the scanned code are executed).
"""
from __future__ import annotations

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOTS = ("services", "scripts", "tools")
ALLOWLIST = os.path.join(REPO, "scripts", "oracle_import_allowlist.txt")

SKIP_DIR_NAMES = frozenset({"__pycache__", "vendor", "third_party", "node_modules"})
SKIP_REL_DIRS = frozenset({os.path.join("services", "dist")})

# The two spellings of the deleted engine package.
ORACLE_ROOTS = ("engine", "engine_oracle")


def load_allowlist(path: str) -> list[str]:
    """Dotted module prefixes; blank lines and ``#`` comments are ignored.

    Both spellings canonicalize to ``engine.`` so one entry covers ``engine`` and
    ``engine_oracle`` imports alike.
    """
    entries: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                entries.append(_canonical(line))
    return entries


def _canonical(module: str) -> str:
    """Normalize ``engine_oracle[.x]`` -> ``engine[.x]``."""
    if module == "engine_oracle":
        return "engine"
    if module.startswith("engine_oracle."):
        return "engine." + module[len("engine_oracle."):]
    return module


def _allowed(target: str, entries: list[str]) -> bool:
    for entry in entries:
        if target == entry:
            return True
        # A bare-root entry (`engine`) matches ONLY the bare import, never submodules —
        # otherwise a single entry would allowlist the whole legacy package.
        if entry not in ORACLE_ROOTS and target.startswith(entry + "."):
            return True
    return False


def oracle_targets(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, canonical target module) for every engine/engine_oracle import in *tree*.

    ``from engine.doc.canonical import canonical_dump`` yields ``engine.doc.canonical``
    (the module) AND ``engine.doc.canonical.canonical_dump`` (the from-name as a possible
    submodule); a from-import passes when EITHER is allowlisted. ``import engine.paths``
    yields ``engine.paths``; a bare ``import engine`` yields ``engine``.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ORACLE_ROOTS or alias.name.startswith(ORACLE_ROOTS[0] + ".") \
                        or alias.name.startswith(ORACLE_ROOTS[1] + "."):
                    out.append((node.lineno, _canonical(alias.name)))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (from .engine import …) can never reference the
            # top-level legacy package — skip them.
            if node.level:
                continue
            mod = node.module or ""
            if mod in ORACLE_ROOTS or mod.startswith(ORACLE_ROOTS[0] + ".") \
                    or mod.startswith(ORACLE_ROOTS[1] + "."):
                base = _canonical(mod)
                out.append((node.lineno, base))
                for alias in node.names:
                    if alias.name != "*":
                        out.append((node.lineno, f"{base}.{alias.name}"))
    return out


def iter_python_files(root_rel: str):
    """Yield (abs path, repo-relative path) for every authored ``.py`` under *root_rel*."""
    for root, dirs, files in os.walk(os.path.join(REPO, root_rel)):
        rel_root = os.path.relpath(root, REPO)
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d not in SKIP_DIR_NAMES
            and os.path.join(rel_root, d) not in SKIP_REL_DIRS)
        for fname in sorted(files):
            if fname.endswith(".py"):
                yield os.path.join(root, fname), os.path.join(rel_root, fname)


def main() -> int:
    entries = load_allowlist(ALLOWLIST)
    violations: list[str] = []
    counts: dict[str, int] = {}
    for root_rel in SCAN_ROOTS:
        if not os.path.isdir(os.path.join(REPO, root_rel)):
            print(f"oracle_import_audit: scan root {root_rel}/ does not exist — "
                  "update SCAN_ROOTS rather than scanning nothing", file=sys.stderr)
            return 2
        counts[root_rel] = 0
        for path, rel in iter_python_files(root_rel):
            counts[root_rel] += 1
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
            except SyntaxError as exc:
                violations.append(f"{rel}: {exc.lineno}: SyntaxError: {exc.msg}")
                continue
            hits = oracle_targets(tree)
            # A line passes if ANY of its candidate targets is allowlisted (from-imports
            # yield the module plus each from-name as candidates).
            by_line: dict[int, list[str]] = {}
            for lineno, target in hits:
                by_line.setdefault(lineno, []).append(target)
            for lineno, targets in sorted(by_line.items()):
                if not any(_allowed(t, entries) for t in targets):
                    violations.append(
                        f"{rel}:{lineno}: imports {targets[0]} "
                        f"(not in scripts/oracle_import_allowlist.txt)")
    empty = [r for r, n in counts.items() if n == 0]
    if empty:
        print(f"oracle_import_audit: no Python files found under {', '.join(empty)} — "
              "a vacuous scan is not a pass", file=sys.stderr)
        return 2
    scope = ", ".join(f"{r}/ {n}" for r, n in counts.items())
    if violations:
        print(f"oracle_import_audit: {len(violations)} import(s) of the deleted engine "
              f"package ({scope} files scanned):")
        for v in violations:
            print(f"  {v}")
        print("engine/engine_oracle no longer exist. Retarget each import to core_bridge, "
              "the C++ RPC surface, or its services.* re-home.")
        return 1
    print(f"oracle_import_audit: clean — {scope} .py files, "
          f"{len(entries)} allowlist prefix(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
