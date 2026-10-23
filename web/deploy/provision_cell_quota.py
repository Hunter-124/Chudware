#!/usr/bin/python3 -IS
"""Inspect, or explicitly provision, offline XFS/ext4 cell project quotas.

Run as real root with a reviewed root-owned plan; never read gateway cell.json
as quota authority. Each plan is the complete owner-budget inventory. Every
workspace lists both its workspace root and its cell-state root. All roots for
one owner on one filesystem share a project; fixed limits across filesystems
must sum to at most that owner's budget.

Root paths must already exist, have their reviewed device/inode identities,
and be empty or wholly consistent with the existing project. This tool never
recursively assigns a nonempty uncharged tree. Drain/migrate such data offline,
retain warm CAD saves, then provision the safe destination before admitting it.
No mount, quotaon, sudo grant, service restart, or online migration is performed.
"""
from __future__ import annotations

import argparse
import copy
import errno
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "cell"))
import storage_quota as quota

INVENTORY_FILE = ".quota-inventory.json"


def _inventory(policy):
    return {root["path"]: (owner["owner_key"], root["workspace_key"], root["kind"], group["project_id"])
            for owner in policy["owners"] for group in owner["groups"] for root in group["roots"]}


def _old_policy(proof_root):
    try:
        old = quota.read_authority(proof_root + "/" + quota.PROOF_FILE)
        schema = quota.PROOF_SCHEMA
    except FileNotFoundError:
        try:
            # Root's approved inventory survives an interrupted apply. This
            # PLAN is never accepted as an admission/enforcement attestation.
            old = quota.read_authority(proof_root + "/" + INVENTORY_FILE)
            schema = quota.PLAN_SCHEMA
        except FileNotFoundError:
            return None
    return quota.validate_policy(old, schema)


def _preserve_existing_inventory(old, plan):
    if old is None:
        return
    if old["service_uid"] != plan["service_uid"]:
        raise quota.QuotaRefused("cannot change the provisioned service identity online")
    previous, current = _inventory(old), _inventory(plan)
    if any(current.get(path) != identity for path, identity in previous.items()):
        raise quota.QuotaRefused("complete plan cannot omit/reassign an existing quota root; migrate/retire offline")


def _empty(fd):
    with os.scandir(fd) as entries:
        return next(entries, None) is None


def _publish(fd, proof, filename=quota.PROOF_FILE):
    payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(payload) > quota.MAX_JSON:
        raise quota.QuotaRefused("quota proof exceeds bounded policy size")
    name = ".quota-" + secrets.token_hex(16)
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o644, dir_fd=fd)
    try:
        os.fchmod(target, 0o644)
        remaining = memoryview(payload)
        while remaining:
            count = os.write(target, remaining)
            if count <= 0:
                raise OSError("short quota proof write")
            remaining = remaining[count:]
        os.fsync(target)
        os.close(target)
        target = None
        os.replace(name, filename, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if target is not None:
            os.close(target)
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


def provision(plan, proof_root=quota.DEFAULT_PROOF_ROOT, *, apply=False):
    """Root supervisor API; caller supplies an explicitly reviewed plan object.

    The CLI additionally pins a root-owned plan file and its entire ancestor
    chain. Inspection does not alter inode flags, quotas, or proof contents.
    Apply invalidates the old proof before the first mutation, then publishes
    only after all actual kernel hard limits and root trees match.
    """
    quota.initial_user_namespace()
    if os.geteuid() != 0 or os.getuid() != 0:
        raise quota.QuotaRefused("real root authority/CAP_SYS_ADMIN required; no general gateway sudo is installed")
    proof_root = quota.canonical(proof_root)
    plan = copy.deepcopy(quota.validate_policy(plan, quota.PLAN_SCHEMA))
    proof_fd = quota.open_nofollow(proof_root, root_authority=True)
    root_fds = []
    try:
        old = _old_policy(proof_root)
        _preserve_existing_inventory(old, plan)
        old_roots = {root["path"]: root for owner in old["owners"]
                     for group in owner["groups"] for root in group["roots"]} if old else {}
        old_projects = {(owner["owner_key"], group["project_id"],
                         group["roots"][0].get("mount", {}).get("filesystem_uuid"))
                        for owner in old["owners"] for group in owner["groups"]} if old else set()
        rows = quota.mounts()
        groups = []
        filesystem_capacity = {}
        for owner in plan["owners"]:
            for group in owner["groups"]:
                fds = []
                mount = None
                new_roots = []
                for root in group["roots"]:
                    fd = quota.open_nofollow(root["path"])
                    root_fds.append(fd)
                    fds.append((fd, root))
                    quota.check_root(fd, root, plan["service_uid"])
                    evidence = quota.mount_evidence(fd, root["path"], rows)
                    if mount is not None and evidence != mount:
                        raise quota.QuotaRefused("one owner project cannot cross mount aliases")
                    mount = evidence
                    root["mount"] = evidence
                    actual, inherit = quota.inode_attributes(fd)
                    if actual == group["project_id"] and inherit:
                        previous = old_roots.get(root["path"], {})
                        sockets = (root["kind"] == "state" and
                                   (previous.get("device"), previous.get("inode")) == (root["device"], root["inode"]))
                        quota.check_tree(fd, actual, plan["service_uid"], group["hard_inodes"],
                                         runtime_sockets=sockets)
                    elif actual == 0 and not inherit and _empty(fd):
                        new_roots.append(fd)
                    else:
                        raise quota.QuotaRefused("nonempty unsafe migration or a root already charged to another project")
                fd = fds[0][0]
                group["kernel_state"] = quota.kernel_state(fd)
                try:
                    current = quota.kernel_limits(fd, group["project_id"])
                except OSError as exc:
                    # XFS reports ENOENT for a never-allocated dquot. Active
                    # enforcement was independently read above; ESRCH is not
                    # treated as a missing/new quota or as a usable fallback.
                    if exc.errno != errno.ENOENT:
                        raise
                    current = {"hard_bytes": 0, "hard_inodes": 0, "used_bytes": 0, "used_inodes": 0}
                known_project = (owner["owner_key"], group["project_id"], mount["filesystem_uuid"]) in old_projects
                if not known_project and (current["hard_bytes"] or current["hard_inodes"]
                                          or current["used_bytes"] or current["used_inodes"]):
                    # A project in use without our root-controlled inventory
                    # might belong to another application/owner on this volume.
                    raise quota.QuotaRefused("project already allocated outside trusted inventory; use an unused project")
                if current["used_bytes"] > group["hard_bytes"] or current["used_inodes"] > group["hard_inodes"]:
                    raise quota.QuotaRefused("hard quota is underprovisioned for existing charged saves/state")
                capacity = os.fstatvfs(fd)
                if group["hard_bytes"] % capacity.f_frsize:
                    raise quota.QuotaRefused("hard quota must align to the filesystem allocation block")
                minimum_inodes = len(fds) + sum(8 for _, root in fds if root["kind"] == "state")
                if (group["hard_inodes"] < minimum_inodes
                        or group["hard_bytes"] < minimum_inodes * capacity.f_frsize):
                    raise quota.QuotaRefused("hard quota is underprovisioned for writable root/state initialization")
                device = os.fstat(fd).st_dev
                reserve = filesystem_capacity.setdefault(device, {
                    "available_bytes": capacity.f_bavail * capacity.f_frsize,
                    "available_inodes": capacity.f_favail,
                    "required_bytes": 0, "required_inodes": 0,
                })
                reserve["required_bytes"] += group["hard_bytes"] - current["used_bytes"]
                reserve["required_inodes"] += group["hard_inodes"] - current["used_inodes"]
                groups.append((group, fds, new_roots, current))
        for reserve in filesystem_capacity.values():
            if (reserve["required_bytes"] > reserve["available_bytes"]
                    or reserve["required_inodes"] > reserve["available_inodes"]):
                raise quota.QuotaRefused("filesystem is underprovisioned for the complete reserved owner budgets")
        report = {"schema": "chudware-cell-quota-inspection-v1", "mode": "apply" if apply else "inspect",
                  "mutated": False, "service_uid": plan["service_uid"],
                  "owners": len(plan["owners"]), "groups": len(groups),
                  "roots": len(root_fds), "capacity": list(filesystem_capacity.values()),
                  "projects": [{"project_id": group["project_id"], "requested_hard_bytes": group["hard_bytes"],
                                "requested_hard_inodes": group["hard_inodes"], "kernel": current,
                                "roots_requiring_inheritance": len(new_roots)}
                               for group, _, new_roots, current in groups]}
        if not apply:
            return report
        # No still-valid old permission-fallback attestation may survive a
        # partial quota mutation. Running cells are never stopped by this tool.
        _publish(proof_fd, plan, INVENTORY_FILE)
        try:
            os.unlink(quota.PROOF_FILE, dir_fd=proof_fd)
            os.fsync(proof_fd)
        except FileNotFoundError:
            pass
        for group, fds, new_roots, _ in groups:
            fd = fds[0][0]
            quota.set_limits(fd, group["project_id"], group["hard_bytes"], group["hard_inodes"])
            for root_fd in new_roots:
                # Recheck immediately before assigning an empty root. A writer
                # racing the ioctl is caught by the post-assignment tree walk.
                if not _empty(root_fd):
                    raise quota.QuotaRefused("root became nonempty before project assignment; no proof published")
                quota.set_project(root_fd, group["project_id"])
            group["kernel_state"] = quota.kernel_state(fd)
            actual = quota.kernel_limits(fd, group["project_id"])
            if (actual["hard_bytes"], actual["hard_inodes"]) != (group["hard_bytes"], group["hard_inodes"]):
                raise quota.QuotaRefused("actual kernel hard limits differ; no proof published")
            group["kernel_limits"] = actual
            for root_fd, root in fds:
                quota.check_root(root_fd, root, plan["service_uid"])
                if quota.mount_evidence(root_fd, root["path"]) != root["mount"]:
                    raise quota.QuotaRefused("quota mount changed during provisioning; no proof published")
                previous = old_roots.get(root["path"], {})
                sockets = (root["kind"] == "state" and
                           (previous.get("device"), previous.get("inode")) == (root["device"], root["inode"]))
                quota.check_tree(root_fd, group["project_id"], plan["service_uid"], group["hard_inodes"],
                                 runtime_sockets=sockets)
                # Bind proof to the still-current path, not a replaced detached
                # inode which happened to remain pinned throughout root work.
                current_fd = quota.open_nofollow(root["path"])
                try:
                    quota.check_root(current_fd, root, plan["service_uid"])
                    if quota.mount_id(current_fd) != quota.mount_id(root_fd):
                        raise quota.QuotaRefused("quota root path replaced during provisioning")
                finally:
                    os.close(current_fd)
        proof = {"schema": quota.PROOF_SCHEMA, "boot_id": quota.boot_id(),
                 "service_uid": plan["service_uid"], "owners": plan["owners"]}
        quota.validate_policy(proof, quota.PROOF_SCHEMA)
        _publish(proof_fd, proof)
        os.unlink(INVENTORY_FILE, dir_fd=proof_fd)
        os.fsync(proof_fd)
        report["mutated"] = True
        report["proof"] = proof_root + "/" + quota.PROOF_FILE
        report["projects"] = [{"project_id": group["project_id"], "kernel": group["kernel_limits"]}
                              for group, _, _, _ in groups]
        return report
    finally:
        for fd in root_fds:
            os.close(fd)
        os.close(proof_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="reviewed root-owned complete inventory JSON (never cell.json)")
    parser.add_argument("--proof-root", default=quota.DEFAULT_PROOF_ROOT,
                        help="existing root-owned, service-readable protected directory")
    parser.add_argument("--apply", action="store_true", help="explicitly set kernel limits/inheritance and publish root proof")
    args = parser.parse_args(argv)
    try:
        plan = quota.read_authority(args.plan)
        result = provision(plan, args.proof_root, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RecursionError) as exc:
        # Errors name the failing boundary, never include plan/file contents.
        print(json.dumps({"schema": "chudware-cell-quota-inspection-v1", "mode": "apply" if args.apply else "inspect",
                          "ok": False, "error": str(exc), "errno": getattr(exc, "errno", None)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
