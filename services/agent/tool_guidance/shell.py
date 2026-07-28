"""tool_guidance.shell — curated options for the shell tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "run_shell": {
        "prompt_note": (
            "Use for builds/tests/scripts; prefer fs_read/fs_grep/fs_glob/"
            "fs_write for plain file work. Stateless per call — pass cwd each "
            "time; pipe noisy output through head; check exit_code/timed_out."
        ),
    },
}
