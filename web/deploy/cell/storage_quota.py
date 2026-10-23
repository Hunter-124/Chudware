"""Linux project-quota evidence; no accounting, tmpfs, or privileged fallback.

Q_XGETQSTATV is the generic kernel quota-state interface, including on ext4.
Project limit reads need CAP_SYS_ADMIN; state is normally kernel-readable.
Only a permission denial may use root-controlled state/limit attestation.
Inode project/inherit, filesystem UUID, and exact mount/root identities are
always checked live. A trusted root must re-provision after reboot/remount
and whenever it changes quota policy; proof is not an ongoing privileged read.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import struct

PLAN_SCHEMA = "chudware-cell-quota-plan-v1"
PROOF_SCHEMA = "chudware-cell-quota-proof-v1"
PROOF_FILE = "quota-policy.json"
DEFAULT_PROOF_ROOT = "/etc/chudware/cell-quota.d"
MAX_JSON = 2 * 1024 * 1024
MAX_ROOTS = 4096
MAX_TREE_INODES = 1000000
FSGETXATTR = 0x801C581F
FSSETXATTR = 0x401C5820
FSGETUUID = 0x80111500  # FS_IOC_GETFSUUID, Linux 6.8+.
PROJINHERIT = 0x200
PROJECT_ENFORCED = 0x30  # FS_QUOTA_PDQ_ACCT | FS_QUOTA_PDQ_ENFD
Q_GETQUOTA = 0x800007
Q_SETQUOTA = 0x800008
Q_XGETQSTATV = (ord("X") << 8) + 8
HEX = re.compile(r"[0-9a-f]{32}\Z")


class QuotaRefused(ValueError):
    """Missing or mismatched hard-quota evidence, never a product bypass."""


class Dqblk(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "bhard", "bsoft", "space", "ihard", "isoft", "inodes", "btime", "itime"
    )] + [("valid", ctypes.c_uint32)]


class Qfile(ctypes.Structure):
    _fields_ = [("ino", ctypes.c_uint64), ("blocks", ctypes.c_uint64),
                ("extents", ctypes.c_uint32), ("pad", ctypes.c_uint32)]


class Qstat(ctypes.Structure):
    _fields_ = [("version", ctypes.c_int8), ("pad1", ctypes.c_uint8),
                ("flags", ctypes.c_uint16), ("incore", ctypes.c_uint32),
                ("user", Qfile), ("group", Qfile), ("project", Qfile),
                ("btime", ctypes.c_int32), ("itime", ctypes.c_int32),
                ("rtbtime", ctypes.c_int32), ("bwarn", ctypes.c_uint16),
                ("iwarn", ctypes.c_uint16), ("rtbwarn", ctypes.c_uint16),
                ("pad3", ctypes.c_uint16), ("pad4", ctypes.c_uint32),
                ("pad2", ctypes.c_uint64 * 7)]


def bounded_read(fd, cap):
    parts = bytearray()
    while len(parts) <= cap:
        chunk = os.read(fd, min(65536, cap + 1 - len(parts)))
        if not chunk:
            return bytes(parts)
        parts.extend(chunk)
    raise QuotaRefused("oversized quota evidence")


def kernel_text(path, cap=65536):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        return bounded_read(fd, cap).decode("ascii")
    finally:
        os.close(fd)


def initial_user_namespace():
    # A caller-owned uid 0 in a nested namespace is NOT root attestation.
    if kernel_text("/proc/self/uid_map", 1024).split() != ["0", "0", "4294967295"]:
        raise QuotaRefused("quota admission/provisioning requires the initial user namespace")
    # Do not let a caller-created mount namespace forge uid_map text and
    # reinterpret service-owned files as root-owned proof. NSFS identity and
    # the initial namespace's owner UID translated by the *caller's* kernel
    # user namespace cannot be manufactured by a proc bind or FUSE inode.
    namespace = os.open("/proc/self/ns/user", os.O_RDONLY | os.O_CLOEXEC)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        filesystem = ctypes.create_string_buffer(256)
        if libc.fstatfs(ctypes.c_int(namespace), ctypes.byref(filesystem)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        if (ctypes.c_long.from_buffer(filesystem).value != 0x6E736673
                or os.fstat(namespace).st_ino != 0xEFFFFFFD
                or fcntl.ioctl(namespace, 0xB703) != 0x10000000):  # NS_GET_NSTYPE / CLONE_NEWUSER
            raise QuotaRefused("root quota authority requires the kernel initial NSFS user namespace")
        owner = bytearray(4)
        fcntl.ioctl(namespace, 0xB704, owner, True)  # NS_GET_OWNER_UID, in caller's namespace
        if struct.unpack("=I", owner)[0] != 0:
            raise QuotaRefused("initial root UID is not mapped as root in the caller's namespace")
    finally:
        os.close(namespace)


def canonical(path):
    if (not isinstance(path, str) or not path.startswith("/") or path == "/"
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in {"", ".", ".."} for p in path.split("/")[1:])):
        raise QuotaRefused("canonical absolute quota path required")
    return path


def trusted(info):
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise QuotaRefused("quota authority and all ancestors must be root-owned and not writable by service")


def open_nofollow(path, *, directory=True, root_authority=False):
    canonical(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if root_authority:
            trusted(os.fstat(fd))
        parts = path.split("/")[1:]
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            if root_authority:
                trusted(info)
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise QuotaRefused("regular quota authority file required")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QuotaRefused("duplicate quota policy key")
        result[key] = value
    return result


def read_authority(path):
    initial_user_namespace()
    fd = open_nofollow(path, directory=False, root_authority=True)
    try:
        value = json.loads(bounded_read(fd, MAX_JSON), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise QuotaRefused("quota policy object required")
        return value
    finally:
        os.close(fd)


def integer(value, label, minimum=1, maximum=(1 << 63) - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise QuotaRefused("invalid " + label)
    return value


def cell_id(workspace_key):
    if (not isinstance(workspace_key, str) or len(workspace_key) > 512
            or not re.fullmatch(r"user:[A-Za-z0-9_-]+", workspace_key)):
        raise QuotaRefused("invalid quota workspace identity")
    return hashlib.sha256(workspace_key.encode()).hexdigest()[:32]


def validate_policy(policy, schema):
    if policy.get("schema") != schema:
        raise QuotaRefused("invalid quota policy schema")
    integer(policy.get("service_uid"), "service uid", maximum=(1 << 32) - 2)
    owners = policy.get("owners")
    if not isinstance(owners, list) or not 1 <= len(owners) <= 128:
        raise QuotaRefused("bounded nonempty quota owner list required")
    owner_keys, paths, identities, projects, workspace_owners = set(), set(), set(), set(), {}
    root_count = 0
    for owner in owners:
        if not isinstance(owner, dict):
            raise QuotaRefused("invalid quota owner")
        key = owner.get("owner_key")
        if (not isinstance(key, str) or not key or len(key) > 512 or key in owner_keys
                or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise QuotaRefused("invalid or duplicate budget owner")
        owner_keys.add(key)
        budget_bytes = integer(owner.get("budget_bytes"), "owner byte budget")
        budget_inodes = integer(owner.get("budget_inodes"), "owner inode budget", maximum=MAX_TREE_INODES)
        groups = owner.get("groups")
        if not isinstance(groups, list) or not 1 <= len(groups) <= 32:
            raise QuotaRefused("bounded nonempty quota group list required")
        total_bytes = total_inodes = 0
        owner_devices, workspaces = set(), {}
        for group in groups:
            if not isinstance(group, dict):
                raise QuotaRefused("invalid quota group")
            project = integer(group.get("project_id"), "nonzero project id", maximum=(1 << 31) - 1)
            hard_bytes = integer(group.get("hard_bytes"), "hard byte limit")
            if hard_bytes % 1024:
                raise QuotaRefused("hard byte limits must be exact multiples of 1024")
            total_bytes += hard_bytes
            total_inodes += integer(group.get("hard_inodes"), "hard inode limit", maximum=MAX_TREE_INODES)
            roots = group.get("roots")
            if not isinstance(roots, list) or not roots:
                raise QuotaRefused("nonempty quota roots required")
            root_count += len(roots)
            if root_count > MAX_ROOTS:
                raise QuotaRefused("too many quota roots")
            devices = set()
            for root in roots:
                if not isinstance(root, dict) or root.get("kind") not in {"workspace", "state"}:
                    raise QuotaRefused("invalid quota root kind")
                workspace = root.get("workspace_key")
                ident = cell_id(workspace)
                if workspace_owners.setdefault(workspace, key) != key:
                    raise QuotaRefused("workspace charged to multiple owners")
                kinds = workspaces.setdefault(workspace, set())
                if root["kind"] in kinds:
                    raise QuotaRefused("duplicate workspace root kind")
                kinds.add(root["kind"])
                path = canonical(root.get("path"))
                if root["kind"] == "state" and not path.endswith("/" + ident + "/state"):
                    raise QuotaRefused("state root must bind the exact workspace cell identity")
                device = integer(root.get("device"), "root device", minimum=0)
                inode = integer(root.get("inode"), "root inode")
                if path in paths or (device, inode) in identities:
                    raise QuotaRefused("duplicate or aliased quota root")
                paths.add(path)
                identities.add((device, inode))
                devices.add(device)
            if len(devices) != 1:
                raise QuotaRefused("a quota project group must use one filesystem")
            device = next(iter(devices))
            if device in owner_devices:
                raise QuotaRefused("all roots for one owner on a filesystem must share one project")
            owner_devices.add(device)
            if (device, project) in projects:
                raise QuotaRefused("project reused across owners")
            projects.add((device, project))
        if total_bytes > budget_bytes or total_inodes > budget_inodes:
            raise QuotaRefused("split filesystem limits exceed the bounded owner budget")
        for workspace, kinds in workspaces.items():
            if kinds != {"workspace", "state"}:
                raise QuotaRefused("workspace/state charging is incomplete")
    ordered = sorted(paths)
    if any(right.startswith(left + "/") for left, right in zip(ordered, ordered[1:])):
        raise QuotaRefused("overlapping quota roots are unsafe")
    return policy


def boot_id():
    value = kernel_text("/proc/sys/kernel/random/boot_id", 128).strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise QuotaRefused("kernel boot identity unavailable")
    return value


def mount_id(fd):
    lines = kernel_text("/proc/self/fdinfo/" + str(fd), 4096).splitlines()
    found = [line.split()[1] for line in lines if line.startswith("mnt_id:")]
    if len(found) != 1:
        raise QuotaRefused("kernel mount identity unavailable")
    return int(found[0])


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def mounts():
    rows = {}
    for line in kernel_text("/proc/self/mountinfo", MAX_JSON).splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        ident = int(left[0])
        rows[ident] = {"mount_id": ident, "device": left[2],
                       "root": _unescape_mount(left[3]), "mountpoint": _unescape_mount(left[4]),
                       "options": sorted(left[5].split(",")), "optional": sorted(left[6:]),
                       "fs_type": right[0], "source": _unescape_mount(right[1]),
                       "super_options": sorted(right[2].split(","))}
    return rows


def mount_evidence(fd, path, rows=None):
    rows = mounts() if rows is None else rows
    row = rows.get(mount_id(fd))
    info = os.fstat(fd)
    if (row is None or row["device"] != f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
            or row["fs_type"] not in {"xfs", "ext4"}):
        raise QuotaRefused("only kernel XFS/ext4 project-quota filesystems are supported; FUSE is gated")
    options = set(row["options"] + row["super_options"])
    if ("rw" not in row["options"] or not {"prjquota", "pquota"} & options
            or {"pqnoenforce", "noquota", "ro"} & options or row["root"] != "/"
            or any(item.startswith("rtdev=") for item in options)
            or any("idmap" in item for item in row["optional"])):
        raise QuotaRefused("enforcing full-filesystem project-quota mount required; bind/idmapped roots refused")
    mountpoint = row["mountpoint"].rstrip("/")
    if not (path == mountpoint or path.startswith(mountpoint + "/")):
        raise QuotaRefused("quota root escaped its mount")
    if any(other["mountpoint"] == path or other["mountpoint"].startswith(path + "/")
           for ident, other in rows.items() if ident != row["mount_id"]):
        raise QuotaRefused("nested or substituted mount beneath quota root")
    identity = bytearray(17)
    fcntl.ioctl(fd, FSGETUUID, identity, True)
    if identity[0] != 16 or not any(identity[1:]):
        raise QuotaRefused("kernel filesystem UUID unavailable")
    return {**row, "filesystem_uuid": bytes(identity[1:]).hex()}


def inode_attributes(fd):
    data = bytearray(28)
    fcntl.ioctl(fd, FSGETXATTR, data, True)
    flags, _, _, project, _, _ = struct.unpack("=IIIII8s", data)
    if flags & 0x101:  # FS_XFLAG_REALTIME | FS_XFLAG_RTINHERIT use a separate XFS budget.
        raise QuotaRefused("XFS realtime allocation is outside the supported project byte budget")
    return project, bool(flags & PROJINHERIT)


def set_project(fd, project):
    data = bytearray(28)
    fcntl.ioctl(fd, FSGETXATTR, data, True)
    fields = list(struct.unpack("=IIIII8s", data))
    fields[0] |= PROJINHERIT
    fields[3] = project
    fcntl.ioctl(fd, FSSETXATTR, struct.pack("=IIIII8s", *fields))
    if inode_attributes(fd) != (project, True):
        raise QuotaRefused("kernel did not retain project inheritance")


def quotactl(fd, command, project, data):
    if os.uname().machine not in {"x86_64", "aarch64", "riscv64"}:
        raise QuotaRefused("quotactl_fd syscall ABI is not qualified on this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(443), ctypes.c_int(fd),
                          ctypes.c_uint((command << 8) | 2), ctypes.c_uint(project), ctypes.byref(data))
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return data


def kernel_state(fd):
    state = Qstat()
    state.version = 1
    quotactl(fd, Q_XGETQSTATV, 0, state)
    if state.version != 1 or state.flags & PROJECT_ENFORCED != PROJECT_ENFORCED:
        raise QuotaRefused("kernel project-quota accounting AND enforcement must both be enabled")
    return {"project_flags": int(state.flags & PROJECT_ENFORCED)}


def kernel_limits(fd, project):
    limits = quotactl(fd, Q_GETQUOTA, project, Dqblk())
    if limits.valid & 5 != 5:
        raise QuotaRefused("kernel did not return both hard limits")
    return {"hard_bytes": int(limits.bhard) * 1024, "hard_inodes": int(limits.ihard),
            "used_bytes": int(limits.space), "used_inodes": int(limits.inodes)}


def set_limits(fd, project, hard_bytes, hard_inodes):
    limits = Dqblk()
    limits.bhard = hard_bytes // 1024
    limits.ihard = hard_inodes
    limits.valid = 5  # QIF_BLIMITS | QIF_ILIMITS; soft limits explicitly zero.
    quotactl(fd, Q_SETQUOTA, project, limits)


def check_root(fd, root, service_uid):
    info = os.fstat(fd)
    if ((info.st_dev, info.st_ino) != (root["device"], root["inode"])
            or info.st_uid != service_uid or info.st_mode & 0o022):
        raise QuotaRefused("quota root identity/ownership changed or root is writable by other users")
    if root["kind"] == "state" and stat.S_IMODE(info.st_mode) != 0o700:
        raise QuotaRefused("cell-state quota root must be private mode 0700")


def check_tree(fd, project, service_uid, inode_cap, *, runtime_sockets=False):
    """Refuse unsafe migration; never recursively change existing files.

    Previously admitted actor run directories can retain Unix sockets after a
    crash. Socket inodes inherit their checked parent's project at creation;
    unlike files they cannot be opened for FSGETXATTR. Only private state/run
    sockets in an already-attested tree are accepted, never initial migration,
    workspace sockets, symlinks, devices, or FIFOs.
    """
    root_mount = mount_id(fd)
    visited = 0

    def visit(current, depth, name=""):
        nonlocal visited
        if depth > 128:
            raise QuotaRefused("quota tree exceeds bounded inspection depth")
        info = os.fstat(current)
        visited += 1
        if visited > min(inode_cap, MAX_TREE_INODES):
            raise QuotaRefused("quota tree exceeds owner inode inspection budget")
        if (info.st_uid != service_uid or mount_id(current) != root_mount
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise QuotaRefused("quota tree ownership, hardlink, or mount escape")
        actual, inherit = inode_attributes(current)
        directory = stat.S_ISDIR(info.st_mode)
        if actual != project or (directory and not inherit):
            raise QuotaRefused("nonempty unsafe quota migration; drain and migrate offline before provisioning")
        if directory:
            with os.scandir(current) as entries:
                for entry in entries:
                    # O_NONBLOCK prevents opening a tenant-created FIFO/device from hanging root.
                    info = entry.stat(follow_symlinks=False)
                    kind = info.st_mode
                    if runtime_sockets and name == "run" and stat.S_ISSOCK(kind):
                        visited += 1
                        if (visited > min(inode_cap, MAX_TREE_INODES) or info.st_uid != service_uid
                                or info.st_dev != os.fstat(fd).st_dev or info.st_nlink != 1):
                            raise QuotaRefused("unsafe state runtime socket")
                        continue
                    if not (stat.S_ISDIR(kind) or stat.S_ISREG(kind)):
                        raise QuotaRefused("quota tree symlink or special-file escape")
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    if stat.S_ISDIR(kind):
                        flags |= os.O_DIRECTORY
                    child = os.open(entry.name, flags, dir_fd=current)
                    try:
                        opened = os.fstat(child)
                        if not (stat.S_ISDIR(opened.st_mode) or stat.S_ISREG(opened.st_mode)):
                            raise QuotaRefused("quota tree changed during inspection")
                        visit(child, depth + 1, entry.name)
                    finally:
                        os.close(child)
    visit(fd, 0)
    return visited


def _permission_attestation(call, expected):
    try:
        return call(), "kernel"
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EACCES}:
            raise
        if not isinstance(expected, dict):
            raise QuotaRefused("root-controlled quota limit attestation missing") from None
        return expected, "root-attestation"


def admit(cell, work, state, proof_root=DEFAULT_PROOF_ROOT):
    """Return pinned workspace/state fds only after real quota admission.

    The caller mounts these *same descriptors*. Reopening a checked path would
    let a service-writable parent substitute an uncharged inode before bwrap.
    """
    if not isinstance(cell, str) or not HEX.fullmatch(cell):
        raise QuotaRefused("invalid cell identity")
    proof = validate_policy(read_authority(canonical(proof_root) + "/" + PROOF_FILE), PROOF_SCHEMA)
    if proof.get("boot_id") != boot_id() or proof["service_uid"] != os.getuid():
        raise QuotaRefused("quota proof boot/service identity mismatch; root re-provisioning required")
    matches = [(owner, root["workspace_key"]) for owner in proof["owners"]
               for group in owner["groups"] for root in group["roots"]
               if root["kind"] == "workspace" and cell_id(root["workspace_key"]) == cell]
    if len(matches) != 1:
        raise QuotaRefused("root-provisioned workspace quota evidence missing")
    owner, workspace = matches[0]
    roots = {root["kind"]: root for group in owner["groups"] for root in group["roots"]
             if root["workspace_key"] == workspace}
    if roots["workspace"]["path"] != canonical(work) or roots["state"]["path"] != canonical(state):
        raise QuotaRefused("gateway metadata does not match root-provisioned workspace/state")
    pinned = {}
    rows = mounts()
    try:
        for group in owner["groups"]:
            group_fd = None
            for root in group["roots"]:
                if root["workspace_key"] != workspace:
                    continue
                fd = open_nofollow(root["path"])
                pinned[root["kind"]] = fd
                check_root(fd, root, proof["service_uid"])
                evidence = mount_evidence(fd, root["path"], rows)
                if evidence != root.get("mount"):
                    raise QuotaRefused("quota proof exact mount binding changed; root re-provisioning required")
                check_tree(fd, group["project_id"], proof["service_uid"], group["hard_inodes"],
                           runtime_sockets=root["kind"] == "state")
                group_fd = fd
            # Other workspaces of this owner may reside on a different filesystem;
            # their reserved group limit remains attested, not falsely kernel-read.
            if group_fd is None:
                continue
            state_evidence, _ = _permission_attestation(lambda: kernel_state(group_fd), group.get("kernel_state"))
            if state_evidence.get("project_flags") != PROJECT_ENFORCED:
                raise QuotaRefused("root kernel quota enforcement attestation missing")
            expected = group.get("kernel_limits")
            limits, _ = _permission_attestation(lambda: kernel_limits(group_fd, group["project_id"]), expected)
            if (limits.get("hard_bytes"), limits.get("hard_inodes")) != (group["hard_bytes"], group["hard_inodes"]):
                raise QuotaRefused("kernel/root-attested hard quota differs from owner budget")
        result = pinned.pop("workspace"), pinned.pop("state")
        return result
    finally:
        for fd in pinned.values():
            os.close(fd)
