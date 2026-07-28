"""tool_guidance.files — curated options for the fs_* file tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "fs_read": {
        "prompt_note": (
            "Windowed by design: after a fs_grep hit, jump straight there with "
            "a 'path:50-200' selector instead of paging from the top; binary "
            "files are refused, directories list entries."
        ),
    },
    "fs_list": {
        "prompt_note": (
            "Directories only (a file path errors) — orient here first, then "
            "fs_glob/fs_grep to go deeper; dirs sort before files."
        ),
    },
    "fs_glob": {
        "prompt_note": (
            "Matches come back newest-first, so recently generated outputs "
            "surface at the top; '**' recurses — anchor it under a "
            "subdirectory to stay under the 1000-result cap."
        ),
        # limit=0 falls back to the internal 1000-path cap — a '**' sweep on
        # a big tree dumps ~1000 paths into context; 200 newest-first matches
        # is plenty to orient (the model can raise it when it needs more).
        "param_defaults": {"limit": 200},
    },
    "fs_grep": {
        "prompt_note": (
            "Python regex, line-oriented; skips files >8 MiB and stops after "
            "5000 files, so narrow big trees with path+glob; follow hits up "
            "with fs_read's line selector."
        ),
        # max_matches=0 falls back to the internal 500-match cap — 500 clipped
        # lines is a multi-thousand-token dump; 100 hits is enough to pick a
        # window and jump to it with fs_read's line selector.
        "param_defaults": {"max_matches": 100},
    },
    "fs_write": {
        "prompt_note": (
            "Full overwrite, no append — never use it to patch an existing "
            "file; that is fs_edit's job. Parent dirs are created for you."
        ),
    },
    "fs_edit": {
        "prompt_note": (
            "Copy 'old' verbatim (indentation included) from an fs_read window; "
            "an 'ambiguous' error means widen the surrounding context, not "
            "retry the same string."
        ),
    },
}
