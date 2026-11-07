#!/usr/bin/env python3
"""Run Chudware tests in an isolated container, never on the live checkout.

  scripts/testbox.py --suite <name>[,<name>]   test the area you changed (the normal case)
  scripts/testbox.py --list-suites             suite names and what they cover
  scripts/testbox.py                           tests selected from your changed files
  scripts/testbox.py --gate                    full gate: releases and cross-area hunts only
  scripts/testbox.py -- CMD ...                any command inside the container

Other arguments go to scripts/test_lite.py (e.g. --files, --since). Details and the
storage layout: CONTRIBUTING.md "Running tests".
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
CACHE = Path.home() / '.cache/chudware-testbox'
CONFIG = Path.home() / '.config/chudware/testbox.json'
IMAGE_INPUTS = ('tools/testbox/Containerfile', '.github/scripts/install-fedora-deps.sh', 'requirements.txt',
                'web/gateway/requirements.txt')
GIB = 1024 ** 3
MIN_FREE_GIB = 30
IGNORE = b'''**
!.github/
!.github/scripts/
!.github/scripts/install-fedora-deps.sh
!requirements.txt
!web/
!web/gateway/
!web/gateway/requirements.txt
!tools/
!tools/testbox/
!tools/testbox/Containerfile
'''

# The shell is inside the container. Its HOME, runtime directory, network and
# build trees are private to this slot; none are the developer's live state.
BOOTSTRAP = r'''set -eu
mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$GATE_TMPDIR"
chmod 700 "$HOME" "$XDG_RUNTIME_DIR"
# Tests that look for the repository's service venv run against the image venv
# instead of skipping; the slot's snapshot never carries the developer's own.
[ -e .venv-mcp ] || ln -sfn /opt/chudware-venv .venv-mcp
mode=$1
shift
# One engine tree per slot, shared by every mode (and by gate.sh): compiling the
# full engine twice under two names would double the slot's first build.
case "$mode" in
  suite|changes) core=core/build-linux ;;
  *) core= ;;
esac
# Configure every time (like the host tree below): a slot whose cache was left with
# CHUDWARE_WITH_OCCT=OFF by a custom command would otherwise lose the OCCT tests.
if [ -n "$core" ]; then
    cmake -S core -B "$core" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_WITH_OCCT=ON
fi
case "$mode" in
  suite|changes|gate)
    cef=OFF
    for sdk in extern/cef/cef_binary_*; do
        if [ -d "$sdk" ]; then cef=ON; break; fi
    done
    # gate.sh does not accept extra host CMake arguments, but its configure
    # retains this cache setting. Snapshots normally omit the untracked SDK.
    cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release "-DCHUDWARE_ENABLE_CEF=$cef"
    ;;
esac
exec "$@"
'''

REMOTE_LOCK = r'''set -eu
command -v flock >/dev/null || { echo 'testbox: remote flock is required' >&2; exit 1; }
slots="$HOME/.cache/chudware-testbox/slots"
mkdir -p "$slots"
n=0
while :; do
    dir="$slots/$n"
    mkdir -p "$dir/src"
    exec 9>>"$dir/lock"
    if flock -n 9; then
        printf 'TESTBOX_SLOT %s\n%s\n' "$n" "$dir"
        # Keep the lease through log collection. If the SSH session disappears,
        # wait for this run's container before making the slot available again.
        trap '' HUP
        if IFS= read -r container; then
            cat >/dev/null
            podman wait "$container" >/dev/null 2>&1 || :
        fi
        exit 0
    fi
    exec 9>&-
    n=$((n + 1))
done
'''


def positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a positive integer') from exc
    if result < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str], str]:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''The run snapshots the working tree (uncommitted changes included) into a cached
slot with its own build trees, offloads to the first reachable host in
~/.config/chudware/testbox.json ({"hosts": ["ssh-alias"]}) or falls back to local
Podman, and exits 124 when --timeout expires.''')
    parser.add_argument('--host', default='auto', metavar='auto|local|SSH-ALIAS',
                        help='execution host (default: auto)')
    parser.add_argument('--jobs', type=positive, metavar='N', help='parallel build/test jobs')
    parser.add_argument('--gate', action='store_true', help='run bash scripts/gate.sh instead of test_lite')
    parser.add_argument('--timeout', type=positive, metavar='MINUTES',
                        help='kill the run after this many minutes (default: 120, or 240 with --gate)')
    if '--' in argv:
        index = argv.index('--')
        opts, remaining = parser.parse_known_args(argv[:index])
        command = argv[index + 1:]
        if remaining or opts.gate or not command:
            parser.error('-- must be followed by a command and cannot be combined with --gate or test_lite arguments')
        return opts, command, 'custom'
    opts, remaining = parser.parse_known_args(argv)
    if opts.gate:
        if remaining:
            parser.error('--gate does not accept test_lite arguments')
        return opts, ['bash', 'scripts/gate.sh'], 'gate'
    mode = 'list' if '--list-suites' in remaining else (
        'suite' if any(arg == '--suite' or arg.startswith('--suite=') for arg in remaining) else 'changes')
    return opts, ['/opt/chudware-venv/bin/python', 'scripts/test_lite.py', *remaining], mode


def valid_host(host: str) -> bool:
    # An alias must not be mistaken for an ssh option or an rsync remote path.
    return bool(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host))


def configured_hosts() -> list[str]:
    override = os.environ.get('CHUDWARE_TESTBOX_HOSTS')
    if override is not None:
        hosts = override.split()
    elif CONFIG.exists():
        with CONFIG.open(encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get('hosts'), list):
            raise ValueError(f'{CONFIG}: expected an object with a hosts list')
        hosts = data['hosts']
    else:
        hosts = []
    if any(not isinstance(host, str) or not valid_host(host) or host in ('auto', 'local') for host in hosts):
        raise ValueError('testbox hosts must be valid SSH aliases (not auto or local)')
    return hosts


def remote_free_space(host: str) -> int | None:
    # Measure the filesystem holding the slots and the private image store (follows a
    # symlinked ~/.cache/chudware-testbox onto a larger data disk).
    probe = ('command -v podman rsync && mkdir -p "$HOME/.cache/chudware-testbox" && '
             'df -Pk "$HOME/.cache/chudware-testbox/" | awk \'NR==2 {print $4}\'')
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', host, probe],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) < 3:
        return None
    try:
        available_kib = int(lines[-1])
    except ValueError:
        return None
    return available_kib * 1024 if available_kib >= 0 else None


def low_space_message(host: str, available: int) -> str:
    return f'{host} skipped: {available // GIB} GiB free, need {MIN_FREE_GIB}'


def choose_host(requested: str) -> str:
    if requested == 'local':
        print('testbox: using local Podman (--host local)', flush=True)
        return 'local'
    if requested != 'auto':
        if not valid_host(requested):
            raise ValueError(f'invalid SSH alias: {requested!r}')
        free = remote_free_space(requested)
        if free is None:
            raise RuntimeError(f'{requested} unavailable or missing Podman/rsync')
        if free < MIN_FREE_GIB * GIB:
            raise RuntimeError(low_space_message(requested, free))
        print(f'testbox: using {requested} (explicit --host, {free // GIB} GiB free)', flush=True)
        return requested
    for host in configured_hosts():
        try:
            free = remote_free_space(host)
        except FileNotFoundError:
            print('testbox: ssh is not installed; no remote can be probed', flush=True)
            break
        if free is None:
            print(f'testbox: {host} unavailable or missing Podman/rsync', flush=True)
        elif free < MIN_FREE_GIB * GIB:
            print(f'testbox: {low_space_message(host, free)}', flush=True)
        else:
            print(f'testbox: using {host} (Podman, rsync, {free // GIB} GiB free)', flush=True)
            return host
    print('testbox: using local Podman (no configured remote is usable)', flush=True)
    return 'local'


class Machine:
    def __init__(self, host: str):
        self.host = host
        self.control_dir: str | None = None
        self.control: str | None = None

    def __enter__(self) -> Machine:
        if self.host != 'local':
            CACHE.mkdir(parents=True, exist_ok=True)
            self.control_dir = tempfile.mkdtemp(prefix='ssh-', dir=CACHE)
            self.control = str(Path(self.control_dir) / 'control')
            try:
                subprocess.run(['ssh', '-M', '-N', '-f', '-o', 'BatchMode=yes',
                                '-o', 'ConnectTimeout=4', '-o', f'ControlPath={self.control}',
                                self.host], check=True)
            except BaseException:
                shutil.rmtree(self.control_dir, ignore_errors=True)
                raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.control:
            subprocess.run(['ssh', '-S', self.control, '-O', 'exit', self.host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if self.control_dir:
            shutil.rmtree(self.control_dir, ignore_errors=True)

    def ssh(self, command: str) -> list[str]:
        assert self.control is not None
        return ['ssh', '-S', self.control, '-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
                '-o', 'ProxyCommand=false', '-T', self.host, command]

    def command(self, args: list[str], cwd: str | None = None) -> tuple[list[str], str | None]:
        if self.host == 'local':
            return args, cwd
        command = shlex.join(args)
        if cwd is not None:
            command = f'cd {shlex.quote(cwd)} && {command}'
        return self.ssh(command), None

    def run(self, args: list[str], *, cwd: str | None = None, data: bytes | None = None,
            capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        command, local_cwd = self.command(args, cwd)
        return subprocess.run(command, cwd=local_cwd, input=data,
                              stdout=subprocess.PIPE if capture else None, check=check)

    def destination(self, path: str) -> str:
        return path if self.host == 'local' else f'{self.host}:{path}'

    def rsync(self, args: list[str], *, data: bytes | None = None) -> None:
        command = ['rsync', '-a', '--protect-args', '--modify-window=-1']
        if self.control:
            remote_shell = ['ssh', '-S', self.control, '-o', 'ControlMaster=no',
                            '-o', 'BatchMode=yes', '-o', 'ProxyCommand=false']
            command += ['-e', shlex.join(remote_shell)]
        subprocess.run([*command, *args], input=data, check=True)

    def write_file(self, path: str, data: bytes) -> None:
        # Files are outside src and private to the locked slot; rename atomically.
        script = 'umask 077; cat > "$1.tmp" && mv -f -- "$1.tmp" "$1"'
        self.run(['sh', '-c', script, 'testbox', path], data=data)


class Slot:
    def __init__(self, machine: Machine):
        self.machine = machine
        self.number = -1
        self.path = ''
        self.lock = None
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> Slot:
        if self.machine.host == 'local':
            slots = CACHE / 'slots'
            slots.mkdir(parents=True, exist_ok=True)
            number = 0
            while True:
                directory = slots / str(number)
                directory.mkdir(exist_ok=True)
                lock = (directory / 'lock').open('a')
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close()
                    number += 1
                    continue
                self.number, self.path, self.lock = number, str(directory), lock
                (directory / 'src').mkdir(exist_ok=True)
                break
        else:
            self.process = subprocess.Popen(
                self.machine.ssh('sh -c ' + shlex.quote(REMOTE_LOCK)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
            assert self.process.stdout is not None
            header = self.process.stdout.readline().decode('utf-8', errors='replace').strip()
            if not re.fullmatch(r'TESTBOX_SLOT [0-9]+', header):
                remainder = self.process.stdout.read().decode('utf-8', errors='replace')
                self.process.wait()
                raise RuntimeError(f'failed to acquire remote slot: {header}\n{remainder}'.strip())
            self.number = int(header.split()[1])
            self.path = self.process.stdout.readline().decode('utf-8', errors='strict').rstrip('\n')
            if not self.path.startswith('/') or not self.path.endswith(f'/.cache/chudware-testbox/slots/{self.number}'):
                self.__exit__()
                raise RuntimeError('remote returned an invalid slot path')
        print(f'testbox: slot {self.number}: {self.path}/src', flush=True)
        return self

    def __exit__(self, *_: object) -> None:
        if self.process is not None:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            self.process.wait()
        if self.lock is not None:
            self.lock.close()

    def start(self, command: list[str], name: str) -> subprocess.Popen:
        if self.process is not None:
            assert self.process.stdin is not None
            self.process.stdin.write((name + '\n').encode())
            self.process.stdin.flush()
            command = self.machine.ssh(shlex.join(command))
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)


def managed(path: bytes) -> bool:
    parts = path.split(b'/')
    if not path or any(part in (b'', b'.', b'..') for part in parts) or parts[0] == b'.git':
        return False
    # Never manage a slot's own generated build trees or testbox logs.
    return not any(part == b'.cache' or part == b'build' or part.startswith(b'build-')
                   for part in parts[:-1]) and not path.startswith(b'extern/cef/')


def file_list() -> set[bytes]:
    result = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'],
                            cwd=ROOT, stdout=subprocess.PIPE, check=True)
    paths = set()
    for name in result.stdout.split(b'\0'):
        if not managed(name):
            continue
        path = os.fsencode(ROOT) + b'/' + name
        # gitlinks are directories, not a recursive request to copy ignored SDKs.
        if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
            paths.add(name)
    return paths


def nul_list(paths: set[bytes]) -> bytes:
    return b''.join(path + b'\0' for path in sorted(paths))


def git_directory(option: str) -> Path:
    result = subprocess.run(['git', 'rev-parse', '--path-format=absolute', option], cwd=ROOT,
                            stdout=subprocess.PIPE, check=True)
    return Path(os.fsdecode(result.stdout.rstrip(b'\n')))


def snapshot(machine: Machine, slot: Slot) -> None:
    source = str(ROOT) + '/'
    dest = machine.destination(slot.path + '/src/')
    manifest = slot.path + '/synced-files'
    read_manifest = 'if [ -f "$1" ]; then cat "$1"; fi'
    old_data = machine.run(['sh', '-c', read_manifest, 'testbox', manifest], capture=True).stdout
    old = {name for name in old_data.split(b'\0') if managed(name)}
    current = file_list()
    # Record the union first, so an interrupted transfer is cleaned next time.
    machine.write_file(manifest, nul_list(old | current))
    stale = old - current
    if stale:
        # Only previously synced paths are deleted (a path newly ignored in the
        # working tree included). Removed remotely rather than by rsync from an
        # empty source, which reports every absent parent directory as vanished.
        machine.run(['sh', '-c', 'cd "$1" && xargs -0 rm -f --', 'testbox', slot.path + '/src'],
                    data=nul_list(stale))
    machine.rsync(['--from0', '--files-from=-', source, dest], data=nul_list(current))

    # Materialize linked-worktree metadata into a self-contained .git directory.
    # Locks are transient state of the live checkout and must not block the copy.
    common = git_directory('--git-common-dir')
    gitdir = git_directory('--absolute-git-dir')
    git_dest = machine.destination(slot.path + '/src/.git/')
    machine.rsync(['--delete', '--delete-excluded', '--exclude=*.lock', str(common) + '/', git_dest])
    if gitdir != common:
        machine.rsync(['--exclude=*.lock', '--exclude=commondir', '--exclude=gitdir',
                       '--exclude=locked', str(gitdir) + '/', git_dest])
    machine.write_file(manifest, nul_list(current))


def podman(slot: Slot, *args: str) -> list[str]:
    """Podman bound to testbox-private storage beside the slots.

    Images, layers and build temp files then live on the same filesystem as the slot
    build trees (which may be a symlinked data disk), never in the account's default
    container store, and never mix with the machine's other containers.
    """
    root = slot.path.rsplit('/slots/', 1)[0]
    return ['env', f'TMPDIR={root}/tmp', 'podman', '--root', f'{root}/storage',
            '--runroot', f'{root}/run', *args]


def image(machine: Machine, slot: Slot) -> str:
    src = slot.path + '/src'
    # Hash the actual synced inputs, not a possibly changing live worktree.
    inputs = machine.run(['cat', *IMAGE_INPUTS], cwd=src, capture=True).stdout
    digest = hashlib.sha256(inputs).hexdigest()
    tag = 'chudware-testbox:' + digest[:12]
    root = slot.path.rsplit('/slots/', 1)[0]
    machine.run(['mkdir', '-p', f'{root}/tmp', f'{root}/storage', f'{root}/run', f'{root}/ccache'])
    result = machine.run(podman(slot, 'image', 'exists', tag), check=False)
    if result.returncode == 1:
        if machine.host == 'local':
            free = shutil.disk_usage(root).free
            if free < MIN_FREE_GIB * GIB:
                raise RuntimeError(low_space_message('local', free))
        # The repository .dockerignore deliberately excludes .github. Supply a
        # tiny testbox-specific context filter without modifying the snapshot.
        ignore = slot.path + '/containerignore'
        machine.write_file(ignore, IGNORE)
        # The private store is not labelled like ~/.local/share/containers, so SELinux
        # would deny RUN steps their libc mappings; runs already use label=disable.
        machine.run(podman(slot, 'build', '--layers', '--security-opt', 'label=disable',
                           '--ignorefile', ignore,
                           '-f', 'tools/testbox/Containerfile', '-t', tag, '.'), cwd=src)
    elif result.returncode != 0:
        raise RuntimeError(f'podman image exists failed (exit {result.returncode})')
    return tag


def log_directory(host: str) -> Path:
    label = re.sub(r'[^A-Za-z0-9_.-]', '_', host)
    parent = ROOT / '.cache/testbox'
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path = parent / f'{label}-{stamp}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def stream(process: subprocess.Popen, log: Path) -> int:
    assert process.stdout is not None
    with log.open('wb') as out:
        while True:
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    code = process.wait()
    return code if code >= 0 else 128 - code


def run_box(machine: Machine, slot: Slot, tag: str, jobs: int, cpus: int,
            command: list[str], mode: str, timeout: int) -> int:
    logs = log_directory(machine.host)
    output = slot.path + '/src/.cache/testbox-out'
    clean = 'rm -rf -- "$1" && mkdir -p -- "$1"'
    machine.run(['sh', '-c', clean, 'testbox', output])
    # The unique name permits stopping only this run if the caller interrupts it.
    name = f'chudware-testbox-{os.getpid()}-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")}'
    # Native workers need more than podman's 64 MB /dev/shm (SIGBUS otherwise), and the
    # SCAD/hostd sandboxes run bubblewrap, which must mount /proc inside the box.
    box = podman(slot, 'run', '--rm', '--init', '--userns=keep-id', '--name', name,
                 '--shm-size=2g', '--security-opt', 'unmask=ALL', '--security-opt', 'label=disable',
                 '-v', slot.path + '/src:/work:Z', '-w', '/work',
                 # One compiler cache per machine, shared by all slots (ccache locks).
                 '-v', slot.path.rsplit('/slots/', 1)[0] + '/ccache:/ccache:Z',
                 '-e', 'HOME=/tmp/chudware-home', '-e', 'XDG_RUNTIME_DIR=/tmp/chudware-runtime',
                 '-e', f'CHUDWARE_INSTANCE_KEY=testbox-{slot.number}',
                 '-e', 'GATE_TMP=/work/.cache/testbox-out', '-e', 'GATE_TMPDIR=/work/.cache/testbox-out',
                 '-e', f'CMAKE_BUILD_PARALLEL_LEVEL={jobs}', '-e', f'CTEST_PARALLEL_LEVEL={jobs}',
                 '--cpus', str(cpus), '--timeout', str(timeout * 60))
    box += [tag, 'nice', '-n', '10', 'bash', '-c', BOOTSTRAP, 'testbox', mode, *command]
    print(f'testbox: image {tag}; jobs {jobs}; timeout {timeout} min; logs {logs}', flush=True)
    started = time.monotonic()
    process = slot.start(box, name)
    try:
        code = stream(process, logs / 'run.log')
        if code != 0 and time.monotonic() - started >= timeout * 60 - 5:
            print(f'\ntestbox: run killed after the {timeout}-minute limit (--timeout)',
                  file=sys.stderr, flush=True)
            code = 124
    except BaseException as exc:
        print('\ntestbox: stopping the interrupted container', file=sys.stderr, flush=True)
        try:
            machine.run(podman(slot, 'stop', '--time', '10', name), check=False)
        finally:
            # Keep draining so a full SSH/Podman output pipe cannot prevent exit.
            if process.stdout is not None:
                while os.read(process.stdout.fileno(), 65536):
                    pass
            process.wait()
        if not isinstance(exc, KeyboardInterrupt):
            raise
        code = 130
    try:
        machine.rsync([machine.destination(output + '/'), str(logs) + '/'])
    except subprocess.CalledProcessError as exc:
        print(f'testbox: could not collect stage logs: {exc}', file=sys.stderr)
        if code == 0:
            code = exc.returncode or 1
    return code


def main(argv: list[str] | None = None) -> int:
    opts, command, mode = arguments(sys.argv[1:] if argv is None else argv)
    if mode == 'list':
        # Listing reads the manifest only; no snapshot or container is needed.
        return subprocess.call([sys.executable, str(ROOT / 'scripts' / 'test_lite.py'), '--list-suites'])
    try:
        host = choose_host(opts.host)
        with Machine(host) as machine, Slot(machine) as slot:
            snapshot(machine, slot)
            tag = image(machine, slot)
            result = machine.run(['nproc'], capture=True)
            cpus = positive(result.stdout.decode('ascii').strip())
            limit = max(1, cpus - 2)
            timeout = opts.timeout or (240 if mode == 'gate' else 120)
            return run_box(machine, slot, tag, opts.jobs or limit, limit, command, mode, timeout)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'testbox: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
