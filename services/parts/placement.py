"""Resolve verified catalog objects into project-local placement references."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path


def _workspace_root() -> Path:
    from services.agent.request_context import cell_path
    return cell_path('/work')


def _verify_stream(stream, artifact: dict, output=None) -> None:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        size += len(chunk)
        if size > artifact['size_bytes']:
            raise ValueError('catalog artifact size or SHA-256 mismatch')
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if size != artifact['size_bytes'] or digest.hexdigest() != artifact['sha256']:
        raise ValueError('catalog artifact size or SHA-256 mismatch')


def placement_record(record: dict) -> dict:
    """Give native placement a confined, content-addressed cell reference.

    Only manifest artifacts acquired and verified by the catalog are eligible;
    recipes and desktop placements retain their existing routing.
    """
    from services.agent.request_context import in_cell
    artifact = record.get('artifact')
    if not in_cell() or not artifact:
        return record
    from services.parts.parts_library import CAD_EXTS, _MAX_ARTIFACT, _cache_path
    from services.mcp.fs_tools import _open_read_file

    if (not isinstance(artifact, dict)
            or not isinstance(artifact.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', artifact['sha256'])
            or type(artifact.get('size_bytes')) is not int
            or not 0 < artifact['size_bytes'] <= _MAX_ARTIFACT
            or artifact.get('format') not in set(CAD_EXTS.values())):
        raise ValueError('invalid catalog artifact metadata')
    gref = record.get('geometry_ref') or {}
    source = Path(str(gref.get('path') or ''))
    if gref.get('kind') != 'import_file' or source != _cache_path(artifact):
        raise ValueError('catalog artifact was not acquired from the verified cache')

    # Walk project directories by descriptor, never following symlinks, and
    # publish via a no-replace hard link so parallel placements cannot clobber
    # either a user file or another verified object.
    components = ('.chudware', 'catalog', artifact['sha256'])
    filename = 'model.' + artifact['format']
    directory = None
    temporary = None
    try:
        directory = os.open(_workspace_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in components:
            try:
                os.mkdir(component, 0o755, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child

        name = '.object-' + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=directory)
        temporary = name
        with os.fdopen(fd, 'wb') as output, _open_read_file(source) as input_file:
            _verify_stream(input_file, artifact, output)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o444)
        try:
            os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
        except FileExistsError:
            pass
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as existing:
            st = os.fstat(existing.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o222:
                raise ValueError('catalog destination is not an immutable regular file')
            _verify_stream(existing, artifact)
    except OSError as exc:
        # Never return the actor-private source path in an error envelope.
        raise ValueError('cannot materialize verified catalog artifact in project') from exc
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(directory)
    resolved = dict(record)
    resolved['geometry_ref'] = dict(gref, path='/'.join((*components, filename)))
    return resolved
