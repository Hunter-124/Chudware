#!/usr/bin/env python3
"""Fail if any tracked path cannot exist on Windows or macOS.

This gate exists because of a real outage, not a hypothetical one. On
2026-08-10 the first Windows CI run of this repository could not even start:
84 files under the design gym's run-report directory sat in directories
named after model ids — ``C1_open_xai:grok-4-5_s1``,
``C1_local_ollama:llama3.2:latest_s1``,
``C1_open_openrouter:deepseek/deepseek-chat-v3.1_s1``. NTFS reserves ``:`` for
alternate data streams and forbids it in a filename, so ``actions/checkout``
aborted with ``error: invalid path`` and exit 128 **before any step ran**. The
Windows leg of every workflow was permanently red, and no amount of reading the
workflow files would have explained why.

A path problem is invisible on the machine that creates it and total on the
machine that cannot. That asymmetry is what makes it worth a gate: the author
of the next ``ollama:llama3.2:latest`` directory will never see a failure
locally.

Checked, per tracked path:

  reserved-character  ``< > : " | ? *`` or a control byte, all illegal on NTFS
  trailing-dot-space  a component ending in ``.`` or space; Windows silently
                      strips these, so the file you commit is not the file that
                      is checked out
  reserved-name       CON, PRN, AUX, NUL, COM1-9, LPT1-9 — reserved by Windows
                      as device names with or without an extension
  case-collision      two paths differing only by case; they are distinct on
                      ext4 and the same file on default NTFS and APFS, so a
                      checkout silently loses one

Stdlib only and no repository imports, so it runs inside ``scripts/check.py``
on every platform in well under a second.
"""
from __future__ import annotations

import collections
import os
import re
import subprocess
import sys

# Windows reserves these outright. '/' is absent deliberately: it is the path
# separator here and legal as such.
RESERVED_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# Device names, with or without an extension, case-insensitively.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{stem}{n}" for stem in ("COM", "LPT") for n in range(1, 10)
}


def tracked_paths(repo: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split("\0") if p]


def tracked_symlinks(repo: str) -> list[str]:
    """Return every path committed with git mode 120000."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    found = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if meta.split(" ", 1)[0] == "120000":
            found.append(path)
    return found


def audit(paths: list[str], symlinks: list[str]) -> list[str]:
    problems: list[str] = []

    for path in paths:
        for component in path.split("/"):
            if RESERVED_CHARS.search(component):
                bad = "".join(sorted({c for c in component if RESERVED_CHARS.match(c)}))
                problems.append(
                    f"reserved-character  {path}\n"
                    f"    component {component!r} contains {bad!r}, which Windows forbids in a\n"
                    f"    filename. `git clone` fails with 'invalid path' before any file lands."
                )
            if component != component.rstrip(". "):
                problems.append(
                    f"trailing-dot-space  {path}\n"
                    f"    component {component!r} ends in a dot or space. Windows strips those,\n"
                    f"    so the checked-out name differs from the committed one."
                )
            stem = component.split(".", 1)[0].upper()
            if stem in RESERVED_NAMES:
                problems.append(
                    f"reserved-name       {path}\n"
                    f"    component {component!r} is the reserved Windows device name {stem}."
                )

    folded: dict[str, list[str]] = collections.defaultdict(list)
    for path in paths:
        folded[path.lower()].append(path)
    for lowered, group in sorted(folded.items()):
        if len(group) > 1:
            joined = "\n      ".join(sorted(group))
            problems.append(
                f"case-collision      {lowered}\n"
                f"    these differ only by case and are the SAME file on default NTFS and\n"
                f"    APFS, so a checkout keeps one and loses the rest:\n      {joined}"
            )

    for link in sorted(symlinks):
        problems.append(
            f"committed-symlink   {link}\n"
            f"    committed with git mode 120000. Windows needs core.symlinks=true AND either\n"
            f"    Developer Mode or an elevated account, none of which an unelevated CI runner\n"
            f"    can have, so git checks this out as a TEXT FILE containing the target path.\n"
            f"    Nothing errors: imports of it just fail, and the failure looks exactly like a\n"
            f"    missing dependency. Ship a real file or a real package instead."
        )

    return problems


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = tracked_paths(repo)
    symlinks = tracked_symlinks(repo)
    problems = audit(paths, symlinks)

    if problems:
        for problem in problems:
            print(f"portable_paths_audit: {problem}")
        print(
            f"\nportable_paths_audit: FAIL — {len(problems)} tracked path(s) do not survive a\n"
            f"Windows or macOS checkout. Rename or replace them. If a name is generated, fix the\n"
            f"generator too, or the next run recreates it (see\n"
            f"tools/flywheel/bench/gym/runner.py _path_component)."
        )
        return 1

    print(
        f"portable_paths_audit: clean — {len(paths)} tracked paths are portable "
        f"and no committed symlinks"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
