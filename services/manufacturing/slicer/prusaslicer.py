"""Headless subprocess boundary to stock PrusaSlicer and installed profiles.

No GUI entrypoint or shared user datadir is used. Commands have bounded capture
and a wall-clock deadline, with process-group termination on POSIX. Discovery
never installs software and identifies binaries by content, flatpaks by commit.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from .contract import SliceRefusal
from .sidecar import _communicate_bounded, _ProcessLimitError
from .vendor_bundle import _META, write_ini

_APP = 'com.prusa3d.PrusaSlicer'
_DOWNLOAD = 'https://github.com/prusa3d/PrusaSlicer/releases/tag/version_2.9.6'


@dataclass(frozen=True)
class SlicerInstall:
    kind: str
    argv_prefix: tuple[str, ...]
    version: str
    profiles_dir: Path | None
    source: str
    identity: str

    def to_dict(self) -> dict:
        result = asdict(self)
        result['argv_prefix'] = list(self.argv_prefix)
        result['profiles_dir'] = str(self.profiles_dir) if self.profiles_dir else None
        result['usable'] = True
        return result


@dataclass
class CliOutcome:
    ok: bool
    returncode: int
    stdout_tail: str
    stderr_tail: str
    warnings: list[str]
    cmd: list[str]


@dataclass
class SliceRun:
    ok: bool
    gcode_path: Path | None
    project_3mf: Path | None
    config_path: Path | None
    cmd: list[str]
    warnings: list[str]
    stdout_tail: str
    stderr_tail: str
    duration_s: float
    refusal: SliceRefusal | None


def _env(environ=None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    names = ('HOME', 'PATH', 'XDG_RUNTIME_DIR', 'XDG_DATA_DIRS', 'XDG_DATA_HOME',
             'DBUS_SESSION_BUS_ADDRESS', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
    result = {name: source[name] for name in names if name in source}
    result['LANG'] = 'C.UTF-8'
    return result


def _probe(cmd, environ) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=15, env=_env(environ), check=False)
        return result.returncode, result.stdout.decode('utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return -1, ''


def _version(prefix, environ):
    code, text = _probe([*prefix, '--help'], environ)
    match = re.search(r'PrusaSlicer[- ](\d+\.\d+\.\d+)', text.splitlines()[0] if text else '')
    return match.group(1) if code == 0 and match else None


def install_hint() -> str:
    if platform.system() == 'Linux':
        return ('Install stock PrusaSlicer: flatpak remote-add --user --if-not-exists flathub '
                'https://dl.flathub.org/repo/flathub.flatpakrepo && flatpak install --user '
                '--noninteractive -y flathub com.prusa3d.PrusaSlicer; or run python3 '
                'scripts/provision_slicer.py --install.')
    expected = ('/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer' if platform.system() == 'Darwin' else
                r'%ProgramFiles%\Prusa3D\PrusaSlicer\prusa-slicer-console.exe')
    return f'Install PrusaSlicer 2.9.6 from {_DOWNLOAD} at {expected}.'


def discover(environ=None) -> SlicerInstall | SliceRefusal:
    environ = os.environ if environ is None else environ
    override = environ.get('CHUDWARE_SLICER_PROFILES')
    def profiles(path):
        p = Path(override).expanduser() if override else path
        return p if p and p.is_dir() else None
    def binary(path, profile_path, source):
        path = Path(path).expanduser()
        if not path.is_file():
            return None
        version = _version((str(path),), environ)
        if not version:
            return None
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return SlicerInstall('binary', (str(path.resolve()),), version, profiles(profile_path), source, digest.hexdigest())
    explicit = environ.get('CHUDWARE_SLICER_BIN')
    if explicit:
        found = binary(explicit, None, 'env')
        if found:
            return found
        return SliceRefusal('slicer_not_installed', install_hint(), f'Usable binary at CHUDWARE_SLICER_BIN={explicit}')
    flatpak = shutil.which('flatpak', path=environ.get('PATH', os.defpath))
    if flatpak:
        for scope in ('user', 'system'):
            rc, location = _probe([flatpak, 'info', f'--{scope}', '--show-location', _APP], environ)
            if rc != 0 or not location.strip():
                continue
            prefix = (flatpak, 'run', f'--{scope}', '--command=prusa-slicer', _APP)
            version = _version(prefix, environ)
            if not version:
                continue
            rc, commit = _probe([flatpak, 'info', f'--{scope}', '--show-commit', _APP], environ)
            if rc != 0 or not commit.strip():
                continue
            root = Path(location.strip())
            return SlicerInstall('flatpak', prefix, version,
                                 profiles(root / 'files/share/PrusaSlicer/profiles'),
                                 f'flatpak_{scope}', commit.strip())
    system = platform.system()
    if system == 'Darwin':
        root = Path('/Applications/PrusaSlicer.app/Contents')
        paths = [(root / 'MacOS/PrusaSlicer', root / 'Resources/profiles')]
    elif system == 'Windows':
        root = Path(environ.get('ProgramFiles', r'C:\Program Files')) / 'Prusa3D/PrusaSlicer'
        paths = [(root / 'prusa-slicer-console.exe', root / 'resources/profiles')]
    else:
        paths = [(Path('/usr/bin/prusa-slicer'), Path('/usr/share/PrusaSlicer/profiles'))]
    for path, profile_path in paths:
        found = binary(path, profile_path, 'system_path')
        if found:
            return found
    return SliceRefusal('slicer_not_installed', install_hint(), 'Stock PrusaSlicer binary or flatpak')


def run_cli(install: SlicerInstall, args: list[str], *, work_dir: Path,
            grant_dirs: list[Path], timeout_s=600) -> CliOutcome:
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    datadir = work_dir / 'datadir'
    datadir.mkdir(exist_ok=True)
    prefix = list(install.argv_prefix)
    if install.kind == 'flatpak':
        grants = sorted({str(Path(p).resolve()) for p in [work_dir, *grant_dirs]})
        # run options must precede the application ID.
        prefix[-1:-1] = [f'--filesystem={p}' for p in grants]
    cmd = [*prefix, '--datadir', str(datadir), *args]
    try:
        if os.name == 'nt':
            # Windows anonymous pipes cannot be selected by DefaultSelector.
            # Capture to files to avoid unbounded in-memory communicate buffers.
            with (work_dir / 'stdout.log').open('wb') as out, (work_dir / 'stderr.log').open('wb') as err:
                process = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=work_dir, env=_env(),
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                deadline = time.monotonic() + timeout_s
                failure = ''
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        failure = 'slicer_timeout'
                    elif out.tell() > 8 * 1024 * 1024 or err.tell() > 8 * 1024 * 1024:
                        failure = 'slicer_output_limit'
                    if failure:
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                                       capture_output=True, timeout=10, check=False)
                        process.wait(timeout=10)
                        return CliOutcome(False, -1, '', failure, [], cmd)
                    time.sleep(0.05)
                if out.tell() > 8 * 1024 * 1024 or err.tell() > 8 * 1024 * 1024:
                    return CliOutcome(False, -1, '', 'slicer_output_limit', [], cmd)
            stdout = (work_dir / 'stdout.log').read_bytes()
            stderr = (work_dir / 'stderr.log').read_bytes()
        else:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=work_dir, env=_env(), start_new_session=True)
            stdout, stderr = _communicate_bounded(process, timeout_s=float(timeout_s), limit=8 * 1024 * 1024)
        out = stdout.decode('utf-8', errors='replace')
        err = stderr.decode('utf-8', errors='replace')
        warnings = []
        warning_block = []
        for line in (out + '\n' + err).splitlines():
            # Arachne logs recoverable geometry-kernel noise as "warning".
            # Suppress only known messages in the internal boost-log format;
            # preserve the original bytes in stdout_tail/stderr_tail.
            if (re.match(r'^\[\d{4}-\d{2}-\d{2}[^]]*\]\s+\[0x[0-9a-f]+\]\s+\[\w+\]', line, re.I)
                    and re.search(r'Voronoi|polygons will be rotated|rotated back and forth', line, re.I)):
                continue
            if re.match(r'\d+\s*=>', line):
                if warning_block:
                    warnings.append('; '.join(warning_block))
                    warning_block = []
                continue
            if warning_block and line.strip():
                warning_block.append(line.strip())
            elif re.search(r'warning|print stability|low bed adhesion|loose extrusion|collapsing overhang|(?:need|require|recommend|enabl).*supports', line, re.I):
                warning_block.append(line.strip())
        if warning_block:
            warnings.append('; '.join(warning_block))
        warnings = list(dict.fromkeys(warnings))
        return CliOutcome(process.returncode == 0, process.returncode, out[-16000:], err[-16000:], warnings, cmd)
    except _ProcessLimitError as exc:
        return CliOutcome(False, -1, '', f'{exc.code}: {exc}', [], cmd)
    except (OSError, ValueError) as exc:
        return CliOutcome(False, -1, '', str(exc), [], cmd)


@lru_cache(maxsize=8)
def _option_keys(install: SlicerInstall) -> frozenset[str]:
    code, text = _probe([*install.argv_prefix, '--help-fff'], os.environ)
    if code:
        return frozenset()
    return frozenset(m.replace('-', '_') for m in re.findall(r'--([a-z][a-z0-9-]+)', text))


def validate_overrides(install: SlicerInstall, resolved_config: dict[str, str],
                       overrides: dict[str, str]) -> SliceRefusal | None:
    """Validate overrides BEFORE merging, against this preset and CLI keys."""
    known = set(resolved_config) | set(_option_keys(install))
    unknown = sorted(set(overrides) - known)
    if not unknown:
        return None
    suggestions = '; '.join(
        f'{key} (close matches: {", ".join(difflib.get_close_matches(key, sorted(known), n=3)) or "none"})'
        for key in unknown)
    return SliceRefusal('unknown_config_key', 'Unknown PrusaSlicer configuration keys: ' + suggestions)


def slice_mesh(install: SlicerInstall, *, mesh_paths: list[Path], config: dict[str, str],
               output_gcode: Path, work_dir: Path, center_xy: tuple[float, float], copies: int = 1,
               export_3mf: Path | None = None, timeout_s=600) -> SliceRun:
    started = time.monotonic()
    work_dir = Path(work_dir).resolve()
    output_gcode = Path(output_gcode).resolve()
    mesh_paths = [Path(p).resolve() for p in mesh_paths]
    config_path = work_dir / 'config.ini'
    def refused(code, message):
        return SliceRun(False, None, None, None, [], [], '', '', time.monotonic() - started,
                        SliceRefusal(code, message))
    if not mesh_paths or copies < 1 or int(copies) != copies:
        return refused('invalid_slice_input', 'Provide mesh paths and a positive integer copies count.')
    if any(not p.is_file() for p in mesh_paths):
        return refused('mesh_not_found', 'Every input mesh must exist.')
    known = set(_option_keys(install))
    # Some profile-only options are not exposed as CLI flags. Installed bundles
    # provide the authoritative additional key set, never the caller's typo.
    if install.profiles_dir:
        from .vendor_bundle import ProfileCatalog
        catalog = ProfileCatalog(install.profiles_dir)
        for values in catalog._sections.values():
            known.update(values)
    unknown = sorted(k for k in config if k not in known and k not in _META and not k.startswith('compatible_'))
    if unknown:
        suggestions = '; '.join(f'{k} (close matches: {", ".join(difflib.get_close_matches(k, known, n=3)) or "none"})' for k in unknown)
        return refused('unknown_config_key', 'Unknown PrusaSlicer configuration keys: ' + suggestions)
    output_gcode.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Force text G-code: verification cannot read the binary container.
        write_ini({**config, 'binary_gcode': '0'}, config_path)
    except ValueError as exc:
        return refused('invalid_config', str(exc))
    transforms = ['--ensure-on-bed', '--center', f'{center_xy[0]:g},{center_xy[1]:g}']
    # 2.9.6 has no --arrange. --duplicate arranges copies. Multi-mesh
    # components retain their supplied relative coordinates: bare --merge's
    # auto-layout crashes in the headless flatpak, and separate actions would
    # silently overwrite the one requested output.
    if len(mesh_paths) > 1:
        transforms += ['--dont-arrange', '--merge']
    if copies > 1:
        transforms += ['--duplicate', str(copies)]
    common = ['--load', str(config_path), '--threads', '2', '--loglevel', '2', *transforms]
    grants = [p.parent for p in mesh_paths] + [output_gcode.parent]
    outcome = run_cli(install, [*common, '--export-gcode', '--output', str(output_gcode),
                                *(str(p) for p in mesh_paths)], work_dir=work_dir,
                      grant_dirs=grants, timeout_s=timeout_s)
    project = None
    warnings = outcome.warnings.copy()
    if outcome.ok and output_gcode.is_file() and export_3mf is not None:
        project = Path(export_3mf).resolve()
        project.parent.mkdir(parents=True, exist_ok=True)
        remaining = max(0.01, timeout_s - (time.monotonic() - started))
        second = run_cli(install, [*common, '--export-3mf', '--output', str(project),
                                  *(str(p) for p in mesh_paths)], work_dir=work_dir,
                         grant_dirs=[*grants, project.parent], timeout_s=remaining)
        warnings.extend(second.warnings)
        if not second.ok or not project.is_file():
            return SliceRun(False, output_gcode, None, config_path, second.cmd, warnings,
                            second.stdout_tail, second.stderr_tail, time.monotonic() - started,
                            SliceRefusal('project_export_failed', 'PrusaSlicer did not export the review project.', detail=second.stderr_tail))
    ok = outcome.ok and output_gcode.is_file() and output_gcode.stat().st_size > 0
    return SliceRun(ok, output_gcode if ok else None, project, config_path, outcome.cmd,
                    list(dict.fromkeys(warnings)), outcome.stdout_tail, outcome.stderr_tail,
                    round(time.monotonic() - started, 3), None if ok else
                    SliceRefusal('slicing_failed', f'PrusaSlicer did not produce G-code (exit {outcome.returncode}).',
                                 detail=outcome.stderr_tail or outcome.stdout_tail or f'CLI exited {outcome.returncode} without diagnostics.'))
