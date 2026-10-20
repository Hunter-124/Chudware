#!/usr/bin/env python3
"""Environment-scoped encrypted backups and isolated, staged recovery.

Archive/restore/retention and storage selection use only the standard library.
Database commands use the provisioned gateway interpreter and explicit DB URL.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

SCHEMA = "chudware-backup-v1"
LABELS = {"data", "workspace", "bucket", "cells", "university"}
CREDENTIAL_DIRS = {
    "secrets", "credentials", ".secrets", ".credentials", ".ssh", ".aws",
    ".azure", ".gnupg", ".pki", ".docker", ".kube", "gcloud",
    "credstore", "credstore.encrypted", "browser_profiles", ".mozilla",
    "chromium", "google-chrome", "google-chrome-unstable", "firefox",
}
CREDENTIAL_FILES = {
    "rclone.conf", "auth.json", "credentials.json", ".credentials.json",
    "credentials", "tokens.json", "application_default_credentials.json",
    "cloudflare-info.txt", ".netrc", ".pgpass",
}
CREDENTIAL_ENV = (
    "CREDENTIALS_DIRECTORY", "CHUDWARE_ENTITLEMENT_KEY_PATH", "RCLONE_CONFIG",
    "CHUDWARE_WEB_GATEWAY_ENV", "CHUDWARE_WEBD_ENV", "CHUDWARE_BACKUP_AGE_IDENTITY",
)
ARTIFACT = re.compile(
    r"(?P<family>pre-deploy|nightly-database|nightly-state)-(?P<environment>prod|staging)-"
    r"(?P<stamp>\d{8}T\d{6}Z)-[0-9a-f]{32}\.(?P<kind>dump|tar)\.age"
)


class BackupError(ValueError):
    pass


def _path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or any(part in {".", ".."} for part in str(raw).split("/")):
        raise BackupError("an absolute, traversal-free path is required")
    if any(ord(char) < 32 for char in str(path)):
        raise BackupError("control characters in path")
    return path


def _open_directory(path: Path) -> int:
    """Pin every component, including ancestors; never follow a link."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directory(path: Path) -> os.stat_result:
    fd = _open_directory(path)
    try:
        info = os.fstat(fd)
        if not info.st_mode & 0o444 or not info.st_mode & 0o111:
            raise BackupError(f"source directory has no read/traverse authority: {path}")
        return info
    finally:
        os.close(fd)


def _credential(relative: PurePosixPath) -> bool:
    if any(part.lower() in CREDENTIAL_DIRS for part in relative.parts):
        return True
    name = relative.name.lower()
    parts = tuple(part.lower() for part in relative.parts)
    actor_children = [
        index + 5 for index in range(len(parts) - 1)
        if parts[index:index + 2] == ("state", "actors")
        and len(parts) > index + 5 and parts[index + 3] == "data"
        and re.fullmatch(r"[0-9a-f]{32}", parts[index + 4])
    ]
    if any(parts[index].startswith(("chudware-byok-", "chudware-subscription-", "chudware-pool-")) for index in actor_children):
        # Application-created per-provider temporary SDK homes are credential authority.
        return True
    actor_config = any(len(parts) == index + 1 for index in actor_children)
    desktop_config = len(parts) > 1 and parts[-2] in {"chudware", "billet", "ai-cad"}
    if name.startswith("config.json") and (actor_config or desktop_config):
        # Only established credential-store locations, not arbitrary model config.json.
        return True
    return (name in CREDENTIAL_FILES or name.startswith(".env")
            or name.endswith(".env") or ".env." in name or ".env.bak-" in name
            or name.endswith((".pem", ".key", ".p12", ".pfx")))


def _mounts() -> dict[Path, str]:
    result = {}
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            # mountinfo encodes whitespace/backslashes with octal escapes.
            raw = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            result[Path(raw)] = fields[0]
    return result


def validate_roots(roots: dict[str, Path], environment: str) -> dict:
    if environment not in {"prod", "staging"} or not {"data", "workspace", "bucket", "cells"} <= roots.keys():
        raise BackupError("a configured environment and all four storage roots are required")
    if not roots.keys() <= LABELS:
        raise BackupError("unknown storage root label")
    mounts = _mounts()
    entries = []
    for label, raw in sorted(roots.items()):
        path = _path(raw)
        if path in {Path("/"), Path.home(), Path("/home"), Path("/var"), Path("/var/lib"), Path("/tmp")}:
            raise BackupError(f"unsafe broad backup root: {label}")
        if any(path.is_relative_to(Path(base)) for base in ("/etc", "/proc", "/sys", "/dev", "/run")) or _credential(PurePosixPath(path)):
            raise BackupError(f"unsafe credential/system backup root: {label}")
        info = _directory(path)
        boundary = max((mount for mount in mounts if path.is_relative_to(mount)), key=lambda item: len(item.parts))
        unconfigured = [mount for mount in mounts if mount != path and mount.is_relative_to(path) and mount not in roots.values()]
        if unconfigured:
            raise BackupError(f"unconfigured nested mount under {label}; enumerate its storage root explicitly")
        entries.append({"label": label, "source": str(path), "device": info.st_dev,
                        "inode": info.st_ino, "mount_id": mounts[boundary], "mount_path": str(boundary)})
    return {"schema": SCHEMA, "environment": environment,
            "created_at": datetime.now(timezone.utc).isoformat(), "roots": entries,
            "mount_policy": "explicit roots only; nested roots archived separately; no links",
            "credential_inventory": {"directories": sorted(CREDENTIAL_DIRS), "files": sorted(CREDENTIAL_FILES),
                                     "patterns": ["*.env", "*.env.*", ".env*", "*.pem", "*.key", "*.p12", "*.pfx",
                                                  "state/actors/*/data/<owner-hash>/config.json*",
                                                  "{Chudware,Billet,AI-CAD}/config.json*",
                                                  "state/actors/*/data/<owner-hash>/chudware-{byok,subscription,pool}-*/**"],
                                     "configured_paths": [os.environ[key] for key in CREDENTIAL_ENV if os.environ.get(key)]},
            "volatile_exclusions": ["data/logs", "data/tmp", "data/backups", "cells/**/run", "*.sock", "*.socket"]}


def assert_identity(environment: str, data: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    home = Path(os.environ.get("HOME", ""))
    if environment == "staging":
        if user != "chudware-staging" or home != Path("/var/lib/chudware-staging") or data != home / "chudware-web-staging":
            raise BackupError("staging backup requires its dedicated identity, home and data root")
    elif environment != "prod" or user == "chudware-staging" or home == Path("/var/lib/chudware-staging"):
        raise BackupError("staging authority cannot back up production")


def configured_roots(environment: str) -> dict[str, Path]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gateway.storage_config import configured_storage

    if os.environ.get("CHUDWARE_DEPLOY_ENV", "prod") != environment:
        raise BackupError("gateway and backup environments differ")
    # The same pure selector drives Settings, but no authentication configuration
    # is constructed. Raw paths are checked before resolution can hide a link.
    storage = configured_storage(environment)
    roots = storage.roots
    for raw in storage.raw_paths.values():
        _directory(_path(raw))
    assert_identity(environment, roots["data"])
    if environment == "staging":
        if any(not path.is_relative_to(roots["data"]) for path in roots.values()):
            raise BackupError("staging backup roots must remain inside its dedicated data root")
    return roots


def _info(name: str, source: os.stat_result | None = None, directory: bool = False) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.mode = (stat.S_IMODE(source.st_mode) & 0o777) if source else (0o700 if directory else 0o600)
    if source:
        item.uid, item.gid, item.mtime = source.st_uid, source.st_gid, int(source.st_mtime)
        item.size = 0 if directory else source.st_size
    return item


def archive_state(stream, roots: dict[str, Path], environment: str, destination: Path | None = None) -> None:
    manifest = validate_roots(roots, environment)
    mounts = _mounts()
    credentials = [_path(os.environ[key]) for key in CREDENTIAL_ENV if os.environ.get(key)]
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
        item = _info("backup-manifest.json")
        item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))
        archive.addfile(_info("roots", directory=True))
        for label, root in sorted(roots.items()):
            prefix = "roots/" + label
            root_fd = _open_directory(root)
            try:
                root_info = os.fstat(root_fd)
                expected = next(entry for entry in manifest["roots"] if entry["label"] == label)
                if (root_info.st_dev, root_info.st_ino) != (expected["device"], expected["inode"]):
                    raise BackupError("source root changed during backup")
                archive.addfile(_info(prefix, root_info, directory=True))

                def walk(fd: int, relative: PurePosixPath) -> None:
                    with os.scandir(fd) as listing:
                        names = sorted(entry.name for entry in listing)
                    for name in names:
                        rel = relative / name
                        full = root / str(rel)
                        if any(ord(char) < 32 for char in name) or "\\" in name:
                            raise BackupError("unsafe source member path")
                        if (_credential(rel) or any(full == path or full.is_relative_to(path) for path in credentials)
                                or (label == "data" and rel.parts[0] in {"logs", "tmp", "backups"})
                                or (label == "cells" and "run" in rel.parts)
                                or (destination is not None and (full == destination or full.is_relative_to(destination)))
                                or any(full == path and path != root for path in roots.values())):
                            continue
                        source = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISSOCK(source.st_mode) and name.endswith((".sock", ".socket")):
                            continue
                        if full in mounts or source.st_dev != root_info.st_dev:
                            raise BackupError("unconfigured mount boundary appeared during backup")
                        member = prefix + "/" + str(rel)
                        if stat.S_ISDIR(source.st_mode):
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                            try:
                                pinned = os.fstat(child)
                                if (pinned.st_dev, pinned.st_ino) != (source.st_dev, source.st_ino) or not pinned.st_mode & 0o444 or not pinned.st_mode & 0o111:
                                    raise BackupError("source directory changed or is unreadable")
                                archive.addfile(_info(member, pinned, directory=True))
                                walk(child, rel)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(source.st_mode) and source.st_nlink == 1:
                            if not source.st_mode & 0o444:
                                raise BackupError("source file has no read authority")
                            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                            with os.fdopen(file_fd, "rb") as contents:
                                pinned = os.fstat(contents.fileno())
                                if (pinned.st_dev, pinned.st_ino, pinned.st_size, pinned.st_mtime_ns, pinned.st_ctime_ns) != (source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns, source.st_ctime_ns):
                                    raise BackupError("source file changed during backup")
                                archive.addfile(_info(member, pinned), contents)
                                after = os.fstat(contents.fileno())
                                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (pinned.st_size, pinned.st_mtime_ns, pinned.st_ctime_ns):
                                    raise BackupError("source file changed during backup")
                        else:
                            raise BackupError("backup source contains a link or unsupported file type")
                walk(root_fd, PurePosixPath())
            finally:
                os.close(root_fd)


def private_destination(path: Path) -> Path:
    path = _path(path)
    parent = _directory(path.parent)
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise BackupError("backup parent must be identity-owned and not group/world writable")
    if not path.exists():
        path.mkdir(mode=0o700)
    fd = _open_directory(path)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BackupError("backup destination must be owned by this identity and mode 0700")
    finally:
        os.close(fd)
    return path


def recipient(data: Path) -> str:
    value = os.environ.get("CHUDWARE_BACKUP_AGE_RECIPIENT", "").strip()
    if not value:
        path = data / "secrets" / "backup_age_recipient"
        _directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackupError("age recipient must be a regular file")
            value = "".join(stream.read(4097).split())
    if not value or len(value) > 4096:
        raise BackupError("configure CHUDWARE_BACKUP_AGE_RECIPIENT or secrets/backup_age_recipient")
    return value


def _artifact(destination: Path, family: str, environment: str) -> Path:
    kind = "tar" if family == "nightly-state" else "dump"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return destination / f"{family}-{environment}-{stamp}-{uuid.uuid4().hex}.{kind}.age"


@contextmanager
def encrypted_output(final: Path, age_recipient: str):
    partial = final.with_name(final.name + ".partial")
    process = None
    created = False
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(fd, "wb") as output:
            process = subprocess.Popen(["age", "-r", age_recipient], stdin=subprocess.PIPE, stdout=output)
            try:
                yield process.stdin
            finally:
                process.stdin.close()
            if process.wait() != 0:
                raise BackupError("age encryption failed")
            output.flush()
            os.fsync(output.fileno())
        os.link(partial, final)  # Never replace another run's finished artifact.
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        if created:
            partial.unlink(missing_ok=True)


def _database_fields(environment: str) -> dict[str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gateway import db
    from psycopg.conninfo import conninfo_to_dict
    fields = conninfo_to_dict(db.dsn())
    if fields.get("dbname") != "chudware_" + environment:
        raise BackupError("backup database does not match its configured environment")
    return fields


@contextmanager
def pg_credentials(fields: dict[str, str], directory: Path):
    from psycopg.conninfo import make_conninfo
    fields = fields.copy()
    password = fields.pop("password", "")
    # libpq can otherwise inherit a different database/password/service via env.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    with tempfile.TemporaryDirectory(prefix=".pgpass-", dir=directory) as temporary:
        path = Path(temporary) / "pgpass"
        def field(value: str) -> str:
            if "\n" in value or "\r" in value:
                raise BackupError("invalid newline in PostgreSQL credential")
            return value.replace("\\", "\\\\").replace(":", "\\:")
        values = (fields.get("host", "localhost"), fields.get("port", "5432"), fields["dbname"], fields.get("user", pwd.getpwuid(os.getuid()).pw_name), password)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(":".join(field(value) for value in values) + "\n")
        env["PGPASSFILE"] = str(path)
        # Do not let connection service files supply foreign or ambient authority.
        if any(key in fields for key in ("service", "passfile")):
            raise BackupError("backup requires an explicit PostgreSQL credential, not a service/passfile reference")
        yield make_conninfo(**fields), env


def dump_database(destination: Path, environment: str, age_recipient: str, family: str) -> Path:
    fields = _database_fields(environment)
    final = _artifact(destination, family, environment)
    with pg_credentials(fields, destination) as (connection, env):
        with encrypted_output(final, age_recipient) as encrypted:
            producer = subprocess.Popen(["pg_dump", "--no-password", "--format=custom", connection], stdout=encrypted, env=env)
            try:
                if producer.wait() != 0:
                    raise BackupError("pg_dump failed; no backup was committed")
            finally:
                if producer.poll() is None:
                    producer.terminate()
                    producer.wait()
    return final


def prune(destination: Path, environment: str, keep: int, days: int, families: set[str]) -> list[Path]:
    if keep < 1 or days < 1:
        raise BackupError("backup count and lifetime must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    grouped = {family: [] for family in families}
    for path in destination.iterdir():
        match = ARTIFACT.fullmatch(path.name)
        info = path.lstat()
        if (not match or match["environment"] != environment or match["family"] not in families
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or match["kind"] != ("tar" if match["family"] == "nightly-state" else "dump")):
            continue
        try:
            stamp = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        grouped[match["family"]].append((stamp, path, info))
    removed = []
    for entries in grouped.values():
        for index, (stamp, path, info) in enumerate(sorted(entries, key=lambda item: (item[0], item[2].st_mtime_ns, item[1].name), reverse=True)):
            if index >= keep or stamp < cutoff:
                current = path.lstat()
                if (current.st_dev, current.st_ino, current.st_uid, current.st_nlink) != (info.st_dev, info.st_ino, os.getuid(), 1):
                    raise BackupError("retention artifact changed during collection")
                path.unlink()
                removed.append(path)
    return removed


def _member_path(name: str) -> PurePosixPath:
    if not name or name.startswith("/") or "\\" in name or any(ord(char) < 32 for char in name):
        raise BackupError("unsafe archive member path")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupError("archive path traversal")
    return PurePosixPath(*parts)


def unpack_state(stream, stage: Path, environment: str) -> dict:
    seen = set()
    directories = set()
    manifest = None
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for item in archive:
            path = _member_path(item.name)
            name = str(path)
            if name in seen or not (item.isdir() or item.isreg()) or item.mode & ~0o777:
                raise BackupError("duplicate, linked, special or privileged archive member")
            seen.add(name)
            if manifest is None:
                if name != "backup-manifest.json" or not item.isreg() or item.size > 1024 * 1024:
                    raise BackupError("archive must begin with a bounded backup manifest")
                raw = archive.extractfile(item).read()
                manifest = json.loads(raw)
                if not isinstance(manifest, dict) or not isinstance(manifest.get("roots"), list):
                    raise BackupError("archive manifest must declare storage roots")
                entries = manifest["roots"]
                if any(not isinstance(entry, dict) or not isinstance(entry.get("label"), str)
                       or not isinstance(entry.get("source"), str) for entry in entries):
                    raise BackupError("archive storage root entry invalid")
                labels = [entry["label"] for entry in entries]
                if (manifest.get("schema") != SCHEMA or manifest.get("environment") != environment
                        or len(labels) != len(entries) or len(labels) != len(set(labels))
                        or not {"data", "workspace", "bucket", "cells"} <= set(labels) or not set(labels) <= LABELS):
                    raise BackupError("archive manifest environment or roots invalid")
                (stage / name).write_bytes(raw)
                continue
            if name == "roots":
                if not item.isdir():
                    raise BackupError("roots must be a directory")
            elif len(path.parts) < 2 or path.parts[0] != "roots" or path.parts[1] not in labels or _credential(PurePosixPath(*path.parts[2:])):
                raise BackupError("archive member is outside its declared storage or is a credential")
            if len(path.parts) > 1 and str(path.parent) not in directories:
                raise BackupError("archive member parent must be a preceding directory")
            target = stage / name
            if item.isdir():
                target.mkdir(mode=0o700)
                directories.add(name)
            else:
                with target.open("xb") as output, archive.extractfile(item) as contents:
                    shutil.copyfileobj(contents, output)
                target.chmod(item.mode & 0o777)
                os.utime(target, (item.mtime, item.mtime), follow_symlinks=False)
    if manifest is None or any("roots/" + label not in directories for label in labels):
        raise BackupError("archive storage root missing")
    return manifest


def restore_state(artifact: Path, identity: Path, target: Path, environment: str, replace: bool = False) -> None:
    target = _path(target)
    parent = _directory(target.parent)
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise BackupError("restore parent must be operator-owned and not group/world writable")
    if target == Path.home() or target == Path("/"):
        raise BackupError("restore requires a new, isolated target directory")
    if target.exists() or target.is_symlink():
        _directory(target)
        if not replace:
            raise BackupError("restore target already exists; preserve it or explicitly choose --replace")
    with tempfile.TemporaryDirectory(prefix=".chudware-restore-", dir=target.parent) as temporary:
        stage = Path(temporary) / "recovered"
        stage.mkdir(mode=0o700)
        process = subprocess.Popen(["age", "--decrypt", "--identity", str(identity), str(artifact)], stdout=subprocess.PIPE)
        try:
            manifest = unpack_state(process.stdout, stage, environment)
            for entry in manifest["roots"]:
                source = _path(entry["source"])
                if target.is_relative_to(source) or source.is_relative_to(target):
                    raise BackupError("restore target overlaps an original storage root; choose an isolated target")
            # Consume the authenticated stream to EOF before any placement.
            while process.stdout.read(1024 * 1024):
                pass
            if process.wait() != 0:
                raise BackupError("age decryption failed; restore target was not changed")
            if target.exists():
                if not replace:
                    raise BackupError("restore target appeared during recovery")
                _directory(target)
                displaced = Path(temporary) / "previous"
                target.rename(displaced)
                try:
                    stage.rename(target)
                except BaseException:
                    displaced.rename(target)
                    raise
            else:
                stage.rename(target)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()


def restore_database(artifact: Path, identity: Path, target_db: str, directory: Path) -> None:
    from psycopg.conninfo import conninfo_to_dict
    raw = os.environ.get("CHUDWARE_RESTORE_DB_URL", "")
    fields = conninfo_to_dict(raw) if raw else {}
    if (not target_db or target_db in {"chudware_prod", "chudware_staging"} or fields.get("dbname") != target_db
            or fields.get("user") in {"chudware_prod", "chudware_staging"} or not fields.get("user")):
        raise BackupError("restore requires an explicit isolated database and non-service login in CHUDWARE_RESTORE_DB_URL")
    with pg_credentials(fields, directory) as (connection, env):
        # No --clean/--create: conflicts must fail, not erase existing objects.
        producer = subprocess.Popen(["age", "--decrypt", "--identity", str(identity), str(artifact)], stdout=subprocess.PIPE)
        consumer = None
        try:
            consumer = subprocess.Popen(["pg_restore", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--dbname", connection], stdin=producer.stdout, env=env)
            producer.stdout.close()
            consumer_rc = consumer.wait()
            producer_rc = producer.wait()
            if consumer_rc or producer_rc:
                raise BackupError("isolated database restore failed")
        finally:
            producer.stdout.close()
            for process in (consumer, producer):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("nightly", "database"):
        command = commands.add_parser(name)
        command.add_argument("--environment", choices=("prod", "staging"), required=True)
        command.add_argument("--destination", type=Path, required=True)
        if name == "database":
            command.add_argument("--data-root", type=Path, required=True)
    restore = commands.add_parser("restore-state")
    restore.add_argument("--environment", choices=("prod", "staging"), required=True)
    restore.add_argument("--artifact", type=Path, required=True)
    restore.add_argument("--identity", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--replace", action="store_true", help="explicitly replace the entire isolated target")
    database = commands.add_parser("restore-database")
    database.add_argument("--artifact", type=Path, required=True)
    database.add_argument("--identity", type=Path, required=True)
    database.add_argument("--target-db", required=True)
    database.add_argument("--temporary-root", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "restore-state":
            restore_state(args.artifact, args.identity, args.target, args.environment, args.replace)
            return 0
        if args.command == "restore-database":
            directory = private_destination(args.temporary_root)
            restore_database(args.artifact, args.identity, args.target_db, directory)
            return 0
        if os.environ.get("CHUDWARE_DEPLOY_ENV", "prod") != args.environment:
            raise BackupError("backup command and configured environments differ")
        keep = int(os.environ.get("CHUDWARE_WEB_BACKUP_KEEP", "7"))
        days = int(os.environ.get("CHUDWARE_BACKUP_LIFECYCLE_DAYS", "30"))
        if keep < 1 or days < 1:
            raise BackupError("backup count and lifetime must be positive")
        if args.command == "nightly":
            roots = configured_roots(args.environment)
            validate_roots(roots, args.environment)  # Refuse missing roots before any dump.
            data = roots["data"]
        else:
            data = _path(args.data_root)
            _directory(data)
            assert_identity(args.environment, data)
        value = recipient(data)
        destination = private_destination(args.destination)
        family = "pre-deploy" if args.command == "database" else "nightly-database"
        artifacts = [dump_database(destination, args.environment, value, family)]
        families = {family}
        prune(destination, args.environment, keep, days, families)
        if args.command == "nightly":
            final = _artifact(destination, "nightly-state", args.environment)
            with encrypted_output(final, value) as output:
                archive_state(output, roots, args.environment, destination)
            artifacts.append(final)
            families.add("nightly-state")
        prune(destination, args.environment, keep, days, families)
        for path in artifacts:
            print(path)
        return 0
    except (BackupError, OSError, ValueError, RuntimeError, ImportError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
