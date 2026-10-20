"""tool_guidance.files — curated options for the fs_* file tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "fs_read": {
        "prompt_note": (
            "Windowed by design: after a fs_grep hit, jump straight there with "
            "a 'path:50-200' selector instead of paging from the top; binary "
            "files are refused, directories list entries; a CAD file returns "
            "its card plus a labelled render (preview image), not its bytes. "
            "Text scans are chunked and bounded to 64 MiB; larger/changing inputs "
            "refuse with read_budget_exceeded/file_changed, not a partial success. "
            "To parse a large supported file (json.loads), read raw=true from "
            "byte_offset=0, follow next_byte_offset while truncated is true, and "
            "join the page texts; do not parse a single truncated page."
        ),
    },
    "fs_list": {
        "prompt_note": (
            "Directories only (a file path errors) — orient here first, then "
            "fs_glob/fs_grep to go deeper; dirs sort before files; CAD rows "
            "carry a one-line card summary."
        ),
    },
    "browse_models": {
        "prompt_note": (
            "Labelled contact sheets (12 numbered renders each) of a project's "
            "CAD files, read from the files without importing: find the right part "
            "here before import_part, never unzip/jq. Tile numbers key the text "
            "rows; narrow path/pattern (e.g. 'parts/*') for more than 24 models."
        ),
    },
    "fs_glob": {
        "prompt_note": (
            "Matches come back newest-first, so recently generated outputs "
            "surface at the top; '**' recurses — anchor it under a "
            "subdirectory to stay under the scan and 32 KiB response budgets. "
            "Budget/deadline refusals are not empty or complete search results."
        ),
        # limit=0 falls back to the internal 1000-path cap — a '**' sweep on
        # a big tree dumps ~1000 paths into context; 200 newest-first matches
        # is plenty to orient (the model can raise it when it needs more).
        "param_defaults": {"limit": 200},
    },
    "fs_grep": {
        "prompt_note": (
            "Python regex, line-oriented, in a killable readonly child with a "
            "two-second per-tool deadline. Skips files initially >8 MiB; bounds "
            "input to 64 MiB, 5000 files and responses to 32 KiB. A deadline or "
            "budget exhaustion is a typed refusal, not partial success; simplify "
            "the regex or narrow path+glob/lower max_matches. Follow hits up "
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
