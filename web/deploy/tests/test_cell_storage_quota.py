"""Behavioral admission tests plus an explicitly provisioned real-kernel smoke.

The kernel tests NEVER mount a filesystem or gain privilege. They consume a
reviewed root-owned disposable plan and the proof root provisioned for it with
provision_cell_quota.py (CHUDWARE_TEST_QUOTA_PLAN, CHUDWARE_TEST_QUOTA_PROOF_ROOT).
Pytest runs as the plan's service_uid. Missing explicit setup is an
unavailable prerequisite, never a behavioral substitute for enforcement.
"""
import copy
import errno
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys

import pytest

CELL = Path(__file__).resolve().parents[1] / "cell"
sys.path.insert(0, str(CELL))
import cell_runtime as runtime
import storage_quota as quota


def policy():
    key = "user:quota-test-a"
    ident = quota.cell_id(key)
    roots = [{"workspace_key": key, "kind": kind, "path": path, "device": 7, "inode": inode}
             for kind, path, inode in (
                 ("workspace", "/srv/quota/work/a", 101),
                 ("state", "/srv/quota/cells/" + ident + "/state", 102))]
    return {"schema": quota.PLAN_SCHEMA, "service_uid": 1000, "owners": [
        {"owner_key": key, "budget_bytes": 32 * 1024 * 1024, "budget_inodes": 1000,
         "groups": [{"project_id": 11001, "hard_bytes": 32 * 1024 * 1024,
                     "hard_inodes": 1000, "roots": roots}]}]}


def test_owner_budget_includes_workspace_and_state():
    value = policy()
    assert quota.validate_policy(value, quota.PLAN_SCHEMA) is value
    for kind in ("workspace", "state"):
        missing = copy.deepcopy(value)
        missing["owners"][0]["groups"][0]["roots"] = [
            root for root in missing["owners"][0]["groups"][0]["roots"] if root["kind"] != kind]
        with pytest.raises(quota.QuotaRefused, match="charging is incomplete"):
            quota.validate_policy(missing, quota.PLAN_SCHEMA)


def test_workspaces_of_one_owner_share_the_owner_project():
    value = policy()
    group = value["owners"][0]["groups"][0]
    other = "user:quota-test-b"
    group["roots"] += [
        {"workspace_key": other, "kind": "workspace", "path": "/srv/quota/work/b", "device": 7, "inode": 104},
        {"workspace_key": other, "kind": "state", "path": "/srv/quota/cells/" + quota.cell_id(other) + "/state",
         "device": 7, "inode": 105}]
    assert quota.validate_policy(value, quota.PLAN_SCHEMA) is value
    duplicate = copy.deepcopy(value)
    duplicate["owners"][0]["budget_bytes"] *= 2
    duplicate["owners"][0]["budget_inodes"] *= 2
    duplicate["owners"][0]["groups"][0]["roots"] = group["roots"][:2]
    duplicate["owners"][0]["groups"].append({**group, "project_id": 11002, "roots": group["roots"][2:]})
    with pytest.raises(quota.QuotaRefused, match="share one project"):
        quota.validate_policy(duplicate, quota.PLAN_SCHEMA)


def test_split_filesystems_cannot_multiply_owner_budget():
    value = policy()
    owner = value["owners"][0]
    workspace, state = owner["groups"][0]["roots"]
    workspace["device"] = 8
    owner["groups"][0]["roots"] = [state]
    owner["groups"].append({"project_id": 11001, "hard_bytes": 16 * 1024 * 1024,
                            "hard_inodes": 500, "roots": [workspace]})
    with pytest.raises(quota.QuotaRefused, match="exceed the bounded owner budget"):
        quota.validate_policy(value, quota.PLAN_SCHEMA)
    owner["groups"][0]["hard_bytes"] = 16 * 1024 * 1024
    owner["groups"][0]["hard_inodes"] = 500
    assert quota.validate_policy(value, quota.PLAN_SCHEMA) is value


@pytest.mark.parametrize("field,value", [("service_uid", 0), ("service_uid", True),
                                         ("service_uid", -1), ("service_uid", 2 ** 32)])
def test_rejects_invalid_service_identity(field, value):
    data = policy()
    data[field] = value
    with pytest.raises(quota.QuotaRefused):
        quota.validate_policy(data, quota.PLAN_SCHEMA)


@pytest.mark.parametrize("field,value", [("project_id", 0), ("project_id", -1), ("project_id", True),
                                         ("hard_bytes", 0), ("hard_bytes", 1025),
                                         ("hard_inodes", 0), ("hard_inodes", 1000001)])
def test_rejects_unbounded_or_unrepresentable_project_limits(field, value):
    data = policy()
    data["owners"][0]["groups"][0][field] = value
    with pytest.raises(quota.QuotaRefused):
        quota.validate_policy(data, quota.PLAN_SCHEMA)


def test_state_root_and_root_aliases_are_bound_to_exact_workspace():
    data = policy()
    roots = data["owners"][0]["groups"][0]["roots"]
    roots[1]["path"] = "/srv/quota/cells/" + "a" * 32 + "/state"
    with pytest.raises(quota.QuotaRefused, match="exact workspace cell identity"):
        quota.validate_policy(data, quota.PLAN_SCHEMA)
    data = policy()
    roots = data["owners"][0]["groups"][0]["roots"]
    roots[1]["inode"] = roots[0]["inode"]
    with pytest.raises(quota.QuotaRefused, match="aliased quota root"):
        quota.validate_policy(data, quota.PLAN_SCHEMA)
    roots[1]["inode"] = 102
    roots[0]["path"] = roots[1]["path"].rsplit("/", 1)[0]
    with pytest.raises(quota.QuotaRefused, match="overlapping quota roots"):
        quota.validate_policy(data, quota.PLAN_SCHEMA)


@pytest.mark.parametrize("key", ["team:org/team", "user:a.b", "user:", "operator"])
def test_only_operator_workspace_keys_are_charged(key):
    data = policy()
    for root in data["owners"][0]["groups"][0]["roots"]:
        root["workspace_key"] = key
    with pytest.raises(quota.QuotaRefused, match="workspace identity"):
        quota.validate_policy(data, quota.PLAN_SCHEMA)


def test_pins_paths_without_following_symlinked_ancestors(tmp_path):
    directory = tmp_path / "actual"
    directory.mkdir()
    (directory / "work").mkdir()
    (tmp_path / "redirect").symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        quota.open_nofollow(str(tmp_path / "redirect/work"))
    fd = quota.open_nofollow(str(directory / "work"))
    try:
        before = os.fstat(fd)
        (directory / "work").rename(directory / "detached")
        (directory / "work").mkdir()
        assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (before.st_dev, before.st_ino)
        assert os.stat(directory / "work").st_ino != before.st_ino
    finally:
        os.close(fd)


def test_gateway_private_json_never_becomes_root_quota_authority(tmp_path):
    directory = tmp_path / "proof"
    directory.mkdir(mode=0o700)
    path = directory / quota.PROOF_FILE
    value = policy()
    value["schema"] = quota.PROOF_SCHEMA
    value["boot_id"] = quota.boot_id()
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(quota.QuotaRefused):
        quota.read_authority(str(path))


def test_missing_independent_quota_evidence_never_exposes_writable_roots(tmp_path):
    pins = []
    work = tmp_path / "work"
    work.mkdir()
    cell = tmp_path / quota.cell_id("user:quota-test-a")
    cell.mkdir(mode=0o700)
    (cell / "state").mkdir(mode=0o700)
    cfg = {"quota_proof_root": str(tmp_path / "not-root-authority")}
    with pytest.raises((OSError, quota.QuotaRefused)):
        runtime.quota_roots(cell.name, cfg, str(cell), str(work), pins)
    assert pins == []
    assert list((cell / "state").iterdir()) == []


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EINVAL, errno.ESRCH, errno.ENOENT, errno.EIO])
def test_unavailable_kernel_inspection_is_not_permission_fallback(code):
    def unavailable():
        raise OSError(code, "controlled inspection failure")
    with pytest.raises(OSError) as exc:
        quota._permission_attestation(unavailable, {"hard_bytes": 4096, "hard_inodes": 10})
    assert exc.value.errno == code


@pytest.fixture
def real_kernel_layout():
    plan_path = os.environ.get("CHUDWARE_TEST_QUOTA_PLAN")
    proof_root = os.environ.get("CHUDWARE_TEST_QUOTA_PROOF_ROOT")
    if not plan_path and not proof_root:
        pytest.skip("real kernel quota prerequisite unavailable: root must provision a disposable XFS/ext4 plan")
    assert plan_path and proof_root, "both explicit real-kernel quota setup paths are required"
    plan = quota.validate_policy(quota.read_authority(plan_path), quota.PLAN_SCHEMA)
    assert plan.get("purpose") == "disposable-kernel-quota-smoke", "never exhaust real workspace storage"
    assert os.getuid() == plan["service_uid"] != 0, "kernel smoke must run as the unprivileged cell service uid"
    assert len(plan["owners"]) >= 2, "two independently charged owners required"
    assert all(len(owner["groups"]) == 1 for owner in plan["owners"][:2]), "single scratch volume per owner required"
    assert all(owner["groups"][0]["hard_bytes"] <= 1024 ** 3 for owner in plan["owners"][:2]), "bounded smoke budgets required"
    return plan, proof_root


def _create_write(fd, name, payload):
    file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(file_fd, payload[offset:])
            assert written > 0
            offset += written
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _admit_owner(owner, proof_root):
    group = owner["groups"][0]
    workspace = next(root for root in group["roots"] if root["kind"] == "workspace")
    key = workspace["workspace_key"]
    state = next(root for root in group["roots"] if root["workspace_key"] == key and root["kind"] == "state")
    work_fd, state_fd = quota.admit(quota.cell_id(key), workspace["path"], state["path"], proof_root)
    return group, key, workspace, state, work_fd, state_fd


def test_real_kernel_owner_exhaustion_isolates_sibling_and_preserves_saves(real_kernel_layout):
    plan, proof_root = real_kernel_layout
    first = _admit_owner(plan["owners"][0], proof_root)
    second = _admit_owner(plan["owners"][1], proof_root)
    group, key, workspace, state, work_fd, state_fd = first
    sibling_work, sibling_state = second[-2:]
    suffix = secrets.token_hex(8)
    saved, fill, state_write, sibling_write = [name + "-" + suffix for name in
                                             ("durable-save", "quota-fill", "state-refused", "sibling-save")]
    marker = b"opaque durable save before quota exhaustion\n"
    try:
        _create_write(work_fd, saved, marker)
        fill_fd = os.open(fill, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                          0o600, dir_fd=work_fd)
        exhausted = None
        written = 0
        try:
            for block_size in (1024 * 1024, os.fstatvfs(work_fd).f_frsize):
                block = b"q" * block_size
                while written <= group["hard_bytes"] + block_size:
                    try:
                        count = os.write(fill_fd, block)
                        assert count > 0
                        written += count
                        os.fsync(fill_fd)
                    except OSError as exc:
                        assert exc.errno in {errno.EDQUOT, errno.ENOSPC}, exc
                        exhausted = exc.errno
                        break
        finally:
            os.close(fill_fd)
        assert exhausted in {errno.EDQUOT, errno.ENOSPC}, "actual kernel hard limit did not stop owner writes"
        with pytest.raises(OSError) as exc:
            _create_write(state_fd, state_write, b"s" * 4096)
        assert exc.value.errno in {errno.EDQUOT, errno.ENOSPC}
        _create_write(sibling_work, sibling_write, marker)
        os.unlink(fill, dir_fd=work_fd)
        # Re-admission and a fresh fd reopen demonstrate persistence, not a
        # behavioral substitute for EDQUOT.
        reopened = quota.admit(quota.cell_id(key), workspace["path"], state["path"], proof_root)
        try:
            saved_fd = os.open(saved, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=reopened[0])
            try:
                assert os.read(saved_fd, 4096) == marker
            finally:
                os.close(saved_fd)
        finally:
            for fd in reopened:
                os.close(fd)
    finally:
        for fd, names in ((work_fd, (saved, fill)), (state_fd, (state_write,)),
                          (sibling_work, (sibling_write,))):
            for name in names:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
        for fd in (work_fd, state_fd, sibling_work, sibling_state):
            os.close(fd)


def test_real_kernel_admission_pins_inodes_and_survives_state_runtime_socket(real_kernel_layout):
    plan, proof_root = real_kernel_layout
    group, key, workspace, state, work_fd, state_fd = _admit_owner(plan["owners"][0], proof_root)
    suffix = secrets.token_hex(8)
    name = "run"
    created = False
    sock = socket.socket(socket.AF_UNIX)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=state_fd)
            created = True
        except FileExistsError:
            pass
        socket_path = "/proc/self/fd/" + str(state_fd) + "/run/q-" + suffix
        sock.bind(socket_path)
        actual_work, actual_state = quota.admit(quota.cell_id(key), workspace["path"], state["path"], proof_root)
        try:
            assert os.fstat(actual_work).st_ino == workspace["inode"]
            assert os.fstat(actual_state).st_ino == state["inode"]
            assert quota.inode_attributes(actual_work) == (group["project_id"], True)
            assert quota.inode_attributes(actual_state) == (group["project_id"], True)
        finally:
            os.close(actual_work)
            os.close(actual_state)
    finally:
        sock.close()
        if "socket_path" in locals():
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass
        if created:
            os.rmdir(name, dir_fd=state_fd)
        os.close(work_fd)
        os.close(state_fd)


def test_real_kernel_proof_rejects_replaced_state_inode(real_kernel_layout):
    plan, proof_root = real_kernel_layout
    group, key, workspace, state, work_fd, state_fd = _admit_owner(plan["owners"][0], proof_root)
    path = Path(state["path"])
    detached = path.with_name("state-detached-" + secrets.token_hex(8))
    replaced = False
    try:
        path.rename(detached)
        path.mkdir(mode=0o700)
        replaced = True
        with pytest.raises(quota.QuotaRefused, match="identity/ownership changed"):
            quota.admit(quota.cell_id(key), workspace["path"], state["path"], proof_root)
    finally:
        if replaced:
            path.rmdir()
        if detached.exists():
            detached.rename(path)
        os.close(work_fd)
        os.close(state_fd)
    restored = quota.admit(quota.cell_id(key), workspace["path"], state["path"], proof_root)
    for fd in restored:
        os.close(fd)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap absent")
def test_real_kernel_nested_user_namespace_cannot_escape_project_quota(real_kernel_layout):
    """Inheritance checks alone are insufficient outside the cell's user namespace."""
    plan, proof_root = real_kernel_layout
    group, key, workspace, state, work_fd, state_fd = _admit_owner(plan["owners"][0], proof_root)
    suffix = secrets.token_hex(8)
    directory = "namespace-quota-" + suffix
    state_name = "namespace-full-" + suffix
    sibling_name = "namespace-sibling-" + suffix
    pins = []
    sibling_fds = ()
    try:
        # Use the cell's namespace/capability/rootfs recipe, explicitly
        # mapping namespace UID/GID 0 to the unprivileged fixture owner. Only
        # admitted descriptors are writable; no host storage device is passed.
        arguments = runtime.rootfs(str(CELL.parent), pins)
        pins += [work_fd, state_fd]
        arguments += ["--uid", "0", "--gid", "0",
                      "--bind", "/proc/self/fd/" + str(work_fd), "/work",
                      "--bind", "/proc/self/fd/" + str(state_fd), "/actor"]
        program = r'''
import errno, fcntl, json, os, struct, sys
for name in os.listdir("/proc/self/fd"):
    descriptor = int(name)
    if descriptor > 2:
        try:
            os.close(descriptor)
        except OSError:
            pass
assert os.getuid() == 0
with open("/proc/self/uid_map", encoding="ascii") as stream:
    mapping = stream.read(1024).split()
assert mapping == ["0", sys.argv[5], "1"], mapping
project = int(sys.argv[1])
hard_bytes = int(sys.argv[2])
directory, state_name = sys.argv[3:5]
get_attributes = 0x801C581F
set_attributes = 0x401C5820
inherit = 0x200
refusals = []
for root in ("/work", "/actor"):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        original = bytearray(28)
        fcntl.ioctl(descriptor, get_attributes, original, True)
        fields = list(struct.unpack("=IIIII8s", original))
        assert fields[3] == project and fields[0] & inherit, (root, fields)
        # Test each escape separately; clearing inheritance must not merely be
        # rejected because the same ioctl also requested a different project.
        for mutation in ("inheritance", "project"):
            changed = fields.copy()
            if mutation == "inheritance":
                changed[0] &= ~inherit
            else:
                changed[3] = 0
            try:
                fcntl.ioctl(descriptor, set_attributes, struct.pack("=IIIII8s", *changed))
            except OSError as exc:
                assert exc.errno in (errno.EINVAL, errno.EPERM, errno.EACCES), (root, mutation, exc)
                refusals.append({"root": root, "mutation": mutation, "errno": exc.errno})
            else:
                raise AssertionError("nested actor escaped " + mutation + " on " + root)
            actual = bytearray(28)
            fcntl.ioctl(descriptor, get_attributes, actual, True)
            current = struct.unpack("=IIIII8s", actual)
            assert current[3] == project and current[0] & inherit, (root, current)
    finally:
        os.close(descriptor)
os.mkdir("/work/" + directory, 0o700)
child = os.open("/work/" + directory, os.O_RDONLY | os.O_DIRECTORY)
try:
    attributes = bytearray(28)
    fcntl.ioctl(child, get_attributes, attributes, True)
    fields = struct.unpack("=IIIII8s", attributes)
    assert fields[3] == project and fields[0] & inherit, fields
finally:
    os.close(child)
fill = os.open("/work/" + directory + "/fill", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
written = 0
exhausted = None
try:
    attributes = bytearray(28)
    fcntl.ioctl(fill, get_attributes, attributes, True)
    assert struct.unpack("=IIIII8s", attributes)[3] == project
    for block_size in (1024 * 1024, os.fstatvfs(fill).f_frsize):
        block = b"q" * block_size
        while written <= hard_bytes + block_size:
            try:
                count = os.write(fill, block)
                assert count > 0
                written += count
                os.fsync(fill)
            except OSError as exc:
                assert exc.errno in (errno.EDQUOT, errno.ENOSPC), exc
                exhausted = exc.errno
                break
finally:
    os.close(fill)
assert exhausted in (errno.EDQUOT, errno.ENOSPC), "nested actor escaped the actual kernel byte cap"
assert written <= hard_bytes, (written, hard_bytes)
state_error = None
try:
    with open("/actor/" + state_name, "xb") as stream:
        stream.write(b"s" * 4096)
except OSError as exc:
    assert exc.errno in (errno.EDQUOT, errno.ENOSPC), exc
    state_error = exc.errno
assert state_error is not None, "cell-state tree escaped the exhausted owner project"
print(json.dumps({"project": project, "refusals": refusals, "written": written,
                  "exhaustion_errno": exhausted, "state_errno": state_error}))
'''
        arguments += ["--", "/usr/bin/python3", "-I", "-S", "-c", program,
                      str(group["project_id"]), str(group["hard_bytes"]), directory, state_name,
                      str(plan["service_uid"])]
        result = subprocess.run(arguments, pass_fds=tuple(pins), capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        observed = json.loads(result.stdout)
        assert len(observed["refusals"]) == 4
        assert observed["project"] == group["project_id"]
        assert observed["exhaustion_errno"] in {errno.EDQUOT, errno.ENOSPC}
        assert observed["state_errno"] in {errno.EDQUOT, errno.ENOSPC}
        assert quota.inode_attributes(work_fd) == (group["project_id"], True)
        assert quota.inode_attributes(state_fd) == (group["project_id"], True)
        sibling = _admit_owner(plan["owners"][1], proof_root)
        sibling_fds = sibling[-2:]
        _create_write(sibling_fds[0], sibling_name, b"sibling remains writable after nested actor exhaustion")
    finally:
        try:
            child_tree = Path(workspace["path"]) / directory
            if child_tree.exists():
                shutil.rmtree(child_tree)
            try:
                os.unlink(state_name, dir_fd=state_fd)
            except FileNotFoundError:
                pass
            if sibling_fds:
                try:
                    os.unlink(sibling_name, dir_fd=sibling_fds[0])
                except FileNotFoundError:
                    pass
        finally:
            for descriptor in set((*pins, work_fd, state_fd, *sibling_fds)):
                os.close(descriptor)
