#!/usr/bin/env python3
"""oracle_import_audit — thin-client ratchet for the engine_oracle strangler (T-RF20).

The end state of the Python->C++ migration is a THIN CLIENT: no engine logic left in the
Python services tree, and nothing under ``services/`` importing the legacy engine package
(``engine`` is a repo-root symlink to ``engine_oracle``). This gate FAILS on any

    from engine... import ...   /   import engine...
    from engine_oracle... ...   /   import engine_oracle...

in ``services/**/*.py`` whose target module is NOT covered by
``scripts/oracle_import_allowlist.txt`` (one dotted module path prefix per line; ``#``
comments allowed). The allowlist holds ONLY genuinely-transitional modules — engine
subsystems not yet ported to core/ or re-homed to services/. Each entry must say WHY it
is still allowed. When a subsystem ports, its entry is deleted in the same commit, so the
allowlist ratchets to zero as ``engine_oracle/`` is strangled out.

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
SERVICES = os.path.join(REPO, "services")
ALLOWLIST = os.path.join(REPO, "scripts", "oracle_import_allowlist.txt")

# The two spellings of the legacy engine package (repo-root `engine` symlinks engine_oracle).
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


def main() -> int:
    entries = load_allowlist(ALLOWLIST)
    violations: list[str] = []
    scanned = 0
    for root, dirs, files in os.walk(SERVICES):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, REPO)
            scanned += 1
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
    if violations:
        print(f"oracle_import_audit: {len(violations)} disallowed engine_oracle import(s) "
              f"in services/ ({scanned} files scanned):")
        for v in violations:
            print(f"  {v}")
        print("Flip the import to core_bridge / the services re-home, or — only if the "
              "engine module is genuinely not yet ported — add it to "
              "scripts/oracle_import_allowlist.txt with a reason.")
        return 1
    print(f"oracle_import_audit: clean — {scanned} services/*.py files, "
          f"{len(entries)} transitional allowlist prefix(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
