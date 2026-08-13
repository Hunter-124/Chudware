"""The /engine bridge must not let one account name another account's files.

The chudware-webd daemon is SHARED by every web user and resolves whatever
absolute path an RPC hands it (host/src/core/fs_backend.h: doc_open/doc_save/
export_file take the same abs paths /workspace/list returns). Per-account
containment therefore has to be enforced on the gateway side of the pipe.

Guards the audit finding "engine bridge is a shared daemon pipe with no
per-user isolation" (2026-07-28). If `path_violation` is weakened, the
cross-tenant cases below start returning None and these tests fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_bridge import path_violation  # noqa: E402

ALICE = PurePosixPath("/srv/ws/users/alice~40ua.edu")
BOB = PurePosixPath("/srv/ws/users/bob~40ua.edu")
ROOTS = [ALICE, PurePosixPath("/srv/bucket/users/alice~40ua.edu"), PurePosixPath("/tmp")]


def rpc(method: str, params: object, call_id: int = 1) -> str:
    return json.dumps({"id": call_id, "method": method, "params": params})


@pytest.mark.parametrize(
    "line",
    [
        # Bob's part, opened directly.
        rpc("doc_open", {"path": f"{BOB}/secret.step+"}),
        # Traversal out of Alice's own root back into Bob's.
        rpc("doc_open", {"path": f"{ALICE}/../bob~40ua.edu/secret.step+"}),
        # Writing into someone else's tree.
        rpc("doc_save", {"path": f"{BOB}/clobbered.step+"}),
        # Host files that are not any account's workspace.
        rpc("doc_open", {"path": "/etc/passwd"}),
        rpc("doc_open", {"path": "/home/liveuser/.ssh/id_ed25519"}),
        rpc("export_file", {"out_path": "/home/liveuser/chudware-web-data/gateway.env"}),
        # Nested inside a params sub-object / list — the walk must be recursive.
        rpc("batch", {"ops": [{"op": "open", "src": f"{BOB}/x.step+"}]}),
        rpc("agent_run_v2", {"ctx": {"project_dir": "/home/liveuser"}}),
        # A path-bearing key nested two levels deep.
        rpc("thing", {"a": {"b": {"target": "/var/log/messages"}}}),
        # Relative paths would be resolved against the shared daemon's cwd and
        # must not bypass containment, including values nested in arrays/maps.
        rpc("doc_open", {"path": "relative/part.step+"}),
        rpc("batch", {"ops": [{"options": {"output_path": "out/result.step+"}}]}),
    ],
)
def test_cross_tenant_paths_are_rejected(line: str) -> None:
    assert path_violation(line, ROOTS) is not None


@pytest.mark.parametrize(
    "line",
    [
        # Alice's own files, in both of her roots.
        rpc("doc_open", {"path": f"{ALICE}/part.step+"}),
        rpc("doc_open", {"path": f"{ALICE}/nested/deep/part.step+"}),
        rpc("doc_save", {"path": "/srv/bucket/users/alice~40ua.edu/out.step+"}),
        # Scratch space used for mesh/tess payloads.
        rpc("doc_render", {"aicm_path": "/tmp/mesh-1234.aicm"}),
        # Canonical absolute paths are required for every path-bearing field.
        # Non-path payloads must pass through untouched.
        rpc("hello", {"client": "gui"}),
        rpc("doc_rebuild", {"doc_id": "abc123"}),
        # A value that merely looks path-ish but is not under a path key.
        rpc("note", {"text": "/etc/passwd is a file"}),
        # Not JSON at all.
        "this is not json",
        # Traversal that lands back inside Alice's own root is fine.
        rpc("doc_open", {"path": f"{ALICE}/sub/../part.step+"}),
    ],
)
def test_legitimate_paths_are_allowed(line: str) -> None:
    assert path_violation(line, ROOTS) is None


def test_root_itself_is_allowed_but_its_prefix_sibling_is_not() -> None:
    """`/srv/ws/users/alice2` must not pass just because it shares a prefix."""
    assert path_violation(rpc("doc_open", {"path": str(ALICE)}), ROOTS) is None
    sibling = rpc("doc_open", {"path": "/srv/ws/users/alice~40ua.edu-evil/x.step+"})
    assert path_violation(sibling, ROOTS) is not None
