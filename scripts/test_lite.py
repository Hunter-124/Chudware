#!/usr/bin/env python3
"""Run named suites or a budgeted, change-selected subset of the full Linux gate.

Native change selection uses the configured Ninja trees' compile database and CTest
files: the latter are CMake's expanded add_test registrations, including foreach loops.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

if __package__:
    from . import test_suites
else:
    import test_suites


ROOT = Path(__file__).resolve().parent.parent
STAMP = Path('.cache/test_lite.stamp')
TIMINGS = Path('.cache/test_lite_times.json')
CPP_SUFFIXES = {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx', '.inl', '.ipp', '.inc', '.tpp'}
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
TARGET = re.compile(r'(?:^|/)CMakeFiles/([^/]+)\.dir/')
REGISTRATION = re.compile(r'^add_test\(\[(=*)\[(.*?)\]\1\]\s+(.*)\)\s*$')
# A developer checkout may keep an OCCT tree beside the gate's tree; a testbox slot
# configures only core/build-linux (the same CHUDWARE_WITH_OCCT=ON engine).
CORE_TREES = ('core/build-occt-linux', 'core/build-linux')


@dataclass
class NativeSelection:
    build_dir: str
    targets: list[str]
    tests: list[str]
    uses_engine: bool = False


@dataclass
class Selection:
    changed: list[str]
    python_tests: list[str]
    native: list[NativeSelection]
    runtime_builds: list[str]
    check: bool
    unmatched: list[str]
    suites: tuple[str, ...] = ()


def build_path(path: str) -> bool:
    return any(part in {'build', 'CMakeFiles', '__pycache__', '.cache', '.pytest_cache'}
               or part.startswith('build-') for part in Path(path).parts)


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)


def status_files(root: Path) -> set[str]:
    entries = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split(b'\0')
    files: set[str] = set()
    i = 0
    while i < len(entries) and entries[i]:
        entry = entries[i]
        state = entry[:2].decode('ascii')
        path = os.fsdecode(entry[3:])
        # In -z mode a rename/copy has a second, old-path record.
        i += 2 if 'R' in state or 'C' in state else 1
        if any(c in state for c in 'MARCU?') and not build_path(path) and (root / path).is_file():
            files.add(path)
    return files


def pending_files(root: Path) -> set[str]:
    try:
        data = json.loads((root / STAMP).read_text(encoding='utf-8'))
        return set(data.get('pending', []))
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def default_files(root: Path, now_ns: int) -> set[str]:
    stamp = root / STAMP
    try:
        threshold = stamp.stat().st_mtime_ns
    except FileNotFoundError:
        threshold = now_ns - 24 * 60 * 60 * 1_000_000_000
    pending = pending_files(root)
    result = set()
    for path in status_files(root):
        try:
            if path in pending or (root / path).stat().st_mtime_ns > threshold:
                result.add(path)
        except FileNotFoundError:  # The file was removed while status was being read.
            pass
    return result


def changed_files(root: Path, since: str | None, files: list[str] | None,
                  now_ns: int) -> list[str]:
    if files is not None:
        result = set()
        for name in files:
            path = Path(name)
            absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
            try:
                relative = absolute.relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError(f'file is outside the repository: {name}') from exc
            if not absolute.is_file():
                raise ValueError(f'file does not exist: {name}')
            if build_path(relative):
                raise ValueError(f'file is in a build directory: {name}')
            result.add(relative)
        return sorted(result)
    if since is not None:
        revision = git(root, 'rev-parse', '--verify', '--end-of-options', f'{since}^{{commit}}').strip().decode('ascii')
        raw = git(root, 'diff', '--name-only', '--diff-filter=ACMR', '-z', revision, '--')
        raw += git(root, 'ls-files', '--others', '--exclude-standard', '-z')
        return sorted(path for path in map(os.fsdecode, raw.split(b'\0'))
                      if path and not build_path(path) and (root / path).is_file())
    return sorted(default_files(root, now_ns))


def is_test(path: Path) -> bool:
    # The runner's requested filename happens to start with test_, but is a CLI, not a suite.
    return (path.as_posix() != 'scripts/test_lite.py' and path.suffix == '.py'
            and (path.name.startswith('test_') or path.name.endswith('_test.py')))


def analyze_test(text: str, package: str) -> tuple[set[tuple[str, str]], bool]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Let pytest report malformed test files rather than silently ignoring them.
        return set(), True
    runnable = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            runnable = True
        elif isinstance(node, ast.ClassDef) and (
            node.name.startswith('Test') or any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test_')
                for item in node.body
            )
        ):
            runnable = True
        elif isinstance(node, ast.ImportFrom) and any(
            (alias.asname or alias.name).startswith(('test_', 'Test')) or alias.name == '*'
            for alias in node.names
        ):
            runnable = True
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id.startswith('test_') for target in targets):
                runnable = True
    result = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = node.module or ''
        if node.level:
            try:
                source = importlib.util.resolve_name('.' * node.level + source, package)
            except (ImportError, ValueError):
                continue
        result.update((source, alias.name) for alias in node.names)
    return result, runnable


def imports_module(text: str, module: str, package: str,
                   imported: set[tuple[str, str]] | None = None) -> bool:
    if module in text and re.search(r'(?<![\w.])' + re.escape(module) + r'(?!\w)', text):
        return True
    parent, _, name = module.rpartition('.')
    imported = analyze_test(text, package)[0] if imported is None else imported
    return (parent, name) in imported or (parent, '*') in imported


def python_selection(root: Path, changed: list[str]) -> tuple[set[str], set[str]]:
    selected: set[str] = set()
    matched: set[str] = set()
    directories: dict[Path, list[Path]] = {}
    contents: dict[Path, tuple[str, set[tuple[str, str]], bool]] = {}

    def test_contents(test: Path) -> tuple[str, set[tuple[str, str]], bool]:
        if test not in contents:
            text = test.read_text(encoding='utf-8', errors='replace')
            imported, runnable = analyze_test(text, '.'.join(test.relative_to(root).parent.parts))
            contents[test] = text, imported, runnable
        return contents[test]

    for name in changed:
        path = Path(name)
        if path.suffix != '.py':
            continue
        if is_test(path) and test_contents(root / path)[2]:
            selected.add(name)
            matched.add(name)
        module = '.'.join(path.with_suffix('').parts)
        if path.stem == '__init__':
            module = '.'.join(path.parent.parts)
        if not module:
            continue
        if path.parent not in directories:
            candidates = set((root / path.parent).rglob('*.py'))
            # Subpackages often keep tests in their parent package or its tests/ dir.
            parent = path.parent.parent
            if parent != Path('.'):
                candidates.update((root / parent).glob('*.py'))
                candidates.update((root / parent / 'tests').rglob('*.py'))
            directories[path.parent] = [
                test for test in candidates if not build_path(test.relative_to(root).as_posix())
                and is_test(test.relative_to(root))
            ]
        for test in directories[path.parent]:
            relative = test.relative_to(root)
            text, imported, runnable = test_contents(test)
            if runnable and imports_module(text, module, '.'.join(relative.parent.parts), imported):
                selected.add(relative.as_posix())
                matched.add(name)
        if len(path.parts) >= 3 and path.parts[:2] == ('services', 'mcp') and not is_test(path):
            freshness = 'services/mcp/test_mcp_freshness.py'
            if not (root / freshness).is_file():
                raise ValueError(f'required MCP freshness test is missing: {freshness}')
            selected.add(freshness)
            matched.add(name)
    return selected, matched


def compile_targets(root: Path, build_dir: str) -> tuple[dict[str, set[Path]], dict[str, set[str]]]:
    directory = root / build_dir
    try:
        entries = json.loads((directory / 'compile_commands.json').read_text(encoding='utf-8'))
        registrations = (directory / 'CTestTestfile.cmake').read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'{build_dir} needs a configured Ninja tree with compile_commands.json and CTestTestfile.cmake') from exc
    sources: dict[str, set[Path]] = {}
    for entry in entries:
        output = entry.get('output', '') or entry.get('command', '')
        match = TARGET.search(output)
        if match:
            source = Path(entry['file'])
            if not source.is_absolute():
                source = Path(entry['directory']) / source
            sources.setdefault(match.group(1), set()).add(source.resolve())
    tests: dict[str, set[str]] = {}
    for line in registrations.splitlines():
        match = REGISTRATION.match(line)
        if not match:
            continue
        for argument in shlex.split(match.group(3)):
            target = Path(argument).name
            if target in sources:
                tests.setdefault(target, set()).add(match.group(2))
    return sources, tests


def include_names(path: Path) -> set[str]:
    try:
        return {Path(item).name for item in INCLUDE.findall(path.read_text(encoding='utf-8', errors='replace'))}
    except OSError:
        return set()


def associated_headers(root: Path, name: str) -> set[str]:
    source = root / name
    names = {source.name}
    if source.suffix.lower() in {'.c', '.cc', '.cpp', '.cxx'}:
        siblings = [source.with_suffix(ext) for ext in ('.h', '.hh', '.hpp', '.hxx')]
        headers = [header for header in siblings if header.is_file()]
        if not headers:
            # Some translation units (e.g. parts/generate.cpp) expose their API
            # through a differently named header in the same package.
            headers = [source.parent / item for item in include_names(source)
                       if (source.parent / item).is_file()]
        names.update(header.name for header in headers)
    return names


def native_selection(root: Path, changed: list[str], area: str) -> tuple[NativeSelection | None, set[str]]:
    files = [name for name in changed if name.startswith(area + '/')
             and Path(name).suffix.lower() in CPP_SUFFIXES
             and (area != 'host' or name.startswith(('host/src/', 'host/tests/')))]
    if not files:
        return None, set()
    build_dir = core_test_tree(root) if area == 'core' else 'host/build-linux'
    sources, tests = compile_targets(root, build_dir)
    selected_targets: set[str] = set()
    matched: set[str] = set()
    includes: dict[Path, set[str]] = {}
    core_tests = (root / 'core/tests').resolve()
    for name in files:
        absolute = (root / name).resolve()
        names = associated_headers(root, name)
        for target in tests:
            paths = sources[target]
            if area == 'core':
                # Only registered executables with test sources participate.
                paths = {p for p in paths if p.is_relative_to(core_tests)}
            for source in paths:
                if source not in includes:
                    includes[source] = include_names(source)
            if any(p == absolute or names & includes[p] for p in paths):
                selected_targets.add(target)
                matched.add(name)
    selected_tests = {test for target in selected_targets for test in tests[target]}
    engine_source = (root / 'services/worker/cpp_core.cpp').resolve()
    uses_engine = area == 'host' and any(engine_source in sources[target] for target in selected_targets)
    return NativeSelection(build_dir, sorted(selected_targets), sorted(selected_tests), uses_engine), matched


def core_test_tree(root: Path) -> str:
    """Core tree whose compile database selects native tests: the first configured one."""
    for build_dir in CORE_TREES:
        if (root / build_dir / 'build.ninja').is_file():
            return build_dir
    return CORE_TREES[0]  # unconfigured: compile_targets reports the missing tree


def python_runtime_tree(root: Path) -> str:
    for build_dir in ('core/build-linux', 'core/build-occt-linux'):
        if ((root / build_dir / 'build.ninja').is_file()
                and (root / build_dir / 'CTestTestfile.cmake').is_file()):
            return build_dir
    raise ValueError('Python tests need a configured core Ninja tree for libchudware and chudware-hostd')


def select(root: Path, changed: list[str], timings: dict[str, float] | None = None) -> Selection:
    python_tests, matched = python_selection(root, changed)
    native: list[NativeSelection] = []
    for area in ('core', 'host'):
        item, native_matched = native_selection(root, changed, area)
        if item and item.targets:
            native.append(item)
        matched.update(native_matched)
    timings = timings or {}
    # File size is a first-run proxy; subsequent runs use measured per-file wall time.
    ordered = sorted(python_tests, key=lambda p: (timings.get(p, max(1.0, (root / p).stat().st_size / 1000)), p))
    core_changed = any(p.startswith('core/') and Path(p).suffix.lower() in CPP_SUFFIXES for p in changed)
    runtime_builds = []
    if core_changed and (ordered or any(item.uses_engine for item in native)):
        runtime_builds.extend([tree for tree in CORE_TREES if (root / tree).is_dir()] or CORE_TREES[:1])
    if ordered and not runtime_builds:
        runtime_builds.append(python_runtime_tree(root))
    check = any(p.startswith(('services/', 'host/')) for p in changed)
    unmatched = [p for p in changed if p not in matched and Path(p).suffix.lower() in CPP_SUFFIXES | {'.py'}]
    return Selection(changed, ordered, native, runtime_builds, check, unmatched)


def select_suites(root: Path, manifest: test_suites.Manifest, suites: tuple[str, ...]) -> Selection:
    tests = sorted({path for suite in suites for path in test_suites.pytest_files(manifest, suite, root)})
    targets = [f'tests-{suite}' for suite in suites]
    native = [
        NativeSelection(build_dir, list(targets), [])
        for build_dir in ('core/build-occt-linux', 'core/build-linux', 'host/build-linux')
        if (root / build_dir / 'build.ninja').is_file()
        and (root / build_dir / 'CTestTestfile.cmake').is_file()
    ]
    runtime_builds = [python_runtime_tree(root)] if tests else []
    return Selection([], tests, native, runtime_builds, False, [], suites)


def load_timings(root: Path) -> dict[str, float]:
    try:
        data = json.loads((root / TIMINGS).read_text(encoding='utf-8'))
        return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float)) and v >= 0}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def save_json(path: Path, data: object, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(data, out, sort_keys=True)
            out.write('\n')
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def running_runtime_users(root: Path, build_dir: str, proc: Path = Path('/proc')) -> list[str]:
    """Identify live application processes that could be disturbed by a relink."""
    tree = (root / build_dir).resolve()
    try:
        processes = list(proc.iterdir())
    except OSError as exc:
        return [f'cannot inspect {proc}: {exc}']
    users = []
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            executable = os.readlink(process / 'exe').removesuffix(' (deleted)')
        except OSError:
            executable = ''
        try:
            comm = (process / 'comm').read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            comm = ''
        name = Path(executable).name if executable else comm
        application_names = {'chudware', 'chudware-hostd', 'chudware-shape-build-worker', 'chudware-shape-'}
        if name not in application_names and comm not in application_names:
            continue
        label = f'{name or comm} pid {process.name}'
        if not executable:
            users.append(f'{label}: cannot inspect executable')
            continue
        if Path(executable).resolve().is_relative_to(tree):
            users.append(f'{label}: executable in {build_dir}')
            continue
        try:
            maps = (process / 'maps').read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            continue  # The process exited during inspection.
        except OSError:
            users.append(f'{label}: cannot inspect mapped libraries')
            continue
        for line in maps.splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) != 6:
                continue
            mapped = fields[5].removesuffix(' (deleted)')
            if not mapped.startswith('/'):
                continue
            library = Path(mapped)
            if (library.name == 'libchudware.so' or library.name.startswith('libchudware.so.')) \
                    and library.resolve().is_relative_to(tree):
                users.append(f'{label}: libchudware mapped from {build_dir}')
                break
    return sorted(users)


def has_xdist() -> bool:
    return importlib.util.find_spec('xdist') is not None


def parallel_jobs() -> int:
    """Job count for parallel test workers: CTEST_PARALLEL_LEVEL (testbox sets it), else nproc."""
    try:
        jobs = int(os.environ.get('CTEST_PARALLEL_LEVEL', ''))
    except ValueError:
        jobs = 0
    if jobs < 1:
        jobs = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
    return jobs


def pytest_command(files: list[str], fail_fast: bool = False) -> list[str]:
    """One pytest process for `files`, spread over xdist workers when the runner has xdist.

    `--dist loadfile` keeps each file on a single worker, so a file's module-scoped
    state is never split. Workers never exceed the file count (an idle worker only
    pays collection), and a lone file runs in-process.
    """
    command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q']
    workers = min(parallel_jobs(), len(files))
    if workers > 1 and has_xdist():
        command += ['-n', str(workers), '--dist', 'loadfile']
    if fail_fast:
        command.append('-x')
    return [*command, *files]


def commands(selection: Selection) -> list[tuple[str, list[str], str | None]]:
    result: list[tuple[str, list[str], str | None]] = []
    if selection.check:
        result.append(('check.py', [sys.executable, 'scripts/check.py'], None))
    native_by_dir = {item.build_dir: item for item in selection.native}
    for build_dir in ('core/build-occt-linux', 'core/build-linux', 'host/build-linux'):
        native = native_by_dir.get(build_dir)
        targets = list(native.targets) if native else []
        if build_dir in selection.runtime_builds:
            targets.extend(t for t in ('chudware', 'chudware-hostd', 'chudware-shape-build-worker')
                           if t not in targets)
        if not targets:
            continue
        area = 'host' if build_dir.startswith('host/') else 'core'
        # "Slim" only distinguishes core/build-linux from a developer's OCCT tree; a
        # slot with that tree alone is the full engine.
        slim = build_dir == 'core/build-linux' and any(
            other.startswith('core/') and other != build_dir
            for other in (*native_by_dir, *selection.runtime_builds))
        label = 'core slim build' if slim else f'{area} build'
        build = ['ninja', '-C', build_dir]
        if os.environ.get('CMAKE_BUILD_PARALLEL_LEVEL'):
            build.extend(['-j', os.environ['CMAKE_BUILD_PARALLEL_LEVEL']])
        result.append((label, [*build, *targets], None))
        if native and selection.suites:
            pattern = '^(' + '|'.join(re.escape(name) for name in selection.suites) + ')$'
            result.append((f'{area} ctest', ['ctest', '--test-dir', build_dir,
                                             '-L', pattern, '--output-on-failure'], None))
        elif native and native.tests:
            pattern = '^(' + '|'.join(re.escape(name) for name in native.tests) + ')$'
            result.append((f'{area} ctest', ['ctest', '--test-dir', build_dir,
                                             '--output-on-failure', '--no-tests=error', '-R', pattern], None))
    if selection.suites:
        units: dict[str, list[str]] = {}
        for path in selection.python_tests:
            units.setdefault(test_suites.gate_unit(path), []).append(path)
        for unit, files in units.items():
            result.append((f'pytest {unit}', pytest_command(files), unit))
    else:
        # One file per invocation: loadfile pins a file to one worker, so xdist has
        # nothing to spread here.
        for path in selection.python_tests:
            result.append((f'pytest {path}', pytest_command([path], fail_fast=True), path))
    return result


def process_start(pid: int) -> str | None:
    try:
        # Everything after the parenthesized comm starts with field 3 (state);
        # field 22 is the kernel's process start time, used to guard PID reuse.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def descendants(pid: int) -> dict[int, str]:
    found: dict[int, str] = {}
    todo = [pid]
    while todo:
        parent = todo.pop()
        for children in Path(f'/proc/{parent}/task').glob('*/children'):
            try:
                ids = children.read_text().split()
            except OSError:
                continue
            for value in ids:
                child = int(value)
                if child in found:
                    continue
                start = process_start(child)
                if start is not None:
                    found[child] = start
                    todo.append(child)
    return found


def run_command(command: list[str], root: Path, environment: dict[str, str], timeout: float | None,
                allow_no_tests: bool = False) -> tuple[str, float, str]:
    start = time.monotonic()
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as output:
        try:
            process = subprocess.Popen(command, cwd=root, env=environment, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return 'FAIL', time.monotonic() - start, str(exc)
        try:
            code = process.wait(timeout=None if timeout is None else max(0.001, timeout))
            result = 'PASS' if code == 0 else 'NO TESTS' if code == 5 and allow_no_tests else f'FAIL ({code})'
        except subprocess.TimeoutExpired:
            owned_children = descendants(process.pid)
            # SIGINT lets pytest run fixture teardown (including its private daemon).
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                owned_children.update(descendants(process.pid))
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            # Private test daemons use their own sessions, so a process-group
            # signal alone cannot stop them. Only signal descendants observed
            # before teardown and only if their kernel start time still matches.
            for pid, child_start in owned_children.items():
                if process_start(pid) == child_start:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            result = 'BUDGET'
        output.seek(0)
        return result, time.monotonic() - start, output.read()


def print_plan(selection: Selection, stages: list[tuple[str, list[str], str | None]],
               manifest: test_suites.Manifest) -> None:
    if selection.suites:
        print('Selected suites: ' + ', '.join(selection.suites))
    else:
        print('Changed files:')
        for name in selection.changed:
            print(f'  {name}')
        if not selection.changed:
            print('  (none)')
        print('Selected test suites:')
        found = False
        for item in selection.native:
            tree = item.build_dir.split('/', 1)[0]
            owners = {test: suite for suite, tests in manifest.native[tree].items() for test in tests}
            for test in item.tests:
                print(f'  {tree}/{test}: {owners.get(test, "(not in manifest)")}')
                found = True
        for path in selection.python_tests:
            owner = test_suites.pytest_suite(path, manifest)
            print(f'  {path}: {owner or "(not in manifest)"}')
            found = True
        if not found:
            print('  (none)')
    print('Selected commands:')
    for _, command, _ in stages:
        print('  ' + shlex.join(command))
    if not stages:
        print('  (none)')
    if selection.unmatched:
        print('No directly matched test (full gate still required):')
        for name in selection.unmatched:
            print(f'  {name}')


def list_suites(root: Path, manifest: test_suites.Manifest) -> None:
    counts = {suite: 0 for suite in manifest.suites}
    for path in test_suites.test_files(root):
        suite = test_suites.pytest_suite(path, manifest)
        if suite is not None:
            counts[suite] += 1
    for suite, description in manifest.suites.items():
        core = len(manifest.native['core'].get(suite, []))
        host = len(manifest.native['host'].get(suite, []))
        print(f'{suite}: {description} (core {core} tests, host {host} tests, pytest {counts[suite]} files)')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--since', metavar='GIT-REF')
    group.add_argument('--files', nargs='+', metavar='FILE')
    group.add_argument('--suite', action='append', metavar='NAME',
                       help='run named suites (repeatable or comma-separated); never cut short')
    parser.add_argument('--list-suites', action='store_true', help='list suite descriptions and test counts')
    parser.add_argument('--budget', type=float, default=300, metavar='SECONDS',
                        help='change-selection time budget; with --suite, the per-suite warm target '
                             'that triggers a split warning')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not (args.suite or args.list_suites) and not 0 < args.budget < float('inf'):
        parser.error('--budget must be a positive finite number of seconds')
    started_ns = time.time_ns()
    started = time.monotonic()
    try:
        manifest = test_suites.load(ROOT)
        names = [name.strip() for value in args.suite or [] for name in value.split(',')]
        unknown = [name for name in names if name not in manifest.suites]
        if unknown:
            parser.error('unknown suite(s): ' + ', '.join(name or '(empty)' for name in unknown)
                         + '; choose from: ' + ', '.join(manifest.suites))
        suites = tuple(dict.fromkeys(names))
        if args.list_suites:
            list_suites(ROOT, manifest)
            return 0
        if suites:
            selection = select_suites(ROOT, manifest, suites)
            timings: dict[str, float] = {}
        else:
            changed = changed_files(ROOT, args.since, args.files, started_ns)
            # A scoped green run must not hide other still-dirty files from the next default run.
            pending = default_files(ROOT, started_ns) - set(changed) if args.files is not None or args.since else set()
            timings = load_timings(ROOT)
            selection = select(ROOT, changed, timings)
        stages = commands(selection)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'test_lite: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(os.fsdecode(exc.stderr).strip(), file=sys.stderr)
        return 2
    print_plan(selection, stages, manifest)
    if args.dry_run:
        return 0
    records: list[tuple[str, str, float, str]] = []
    failed = False
    incomplete = False
    blocked: list[str] = []
    skipped_builds: set[str] = set()
    host_needs_runtime = bool(selection.suites) or any(item.uses_engine for item in selection.native)
    with tempfile.TemporaryDirectory(prefix='chudware-test-lite-') as sandbox:
        env = os.environ.copy()
        env.pop('CHUDWARE_HOSTD_SOCKET', None)
        env.pop('CHUDWARE_HOSTD_PIPE', None)
        env['CHUDWARE_INSTANCE_KEY'] = f'test-lite-{os.getpid()}-{time.time_ns()}'
        if selection.runtime_builds:
            # Keep test clients on the just-built pair without staging over the
            # user's running host or the dist daemon watched by that host.
            runtime = ROOT / selection.runtime_builds[0] / 'bin'
            if not env.get('CHUDWARE_LIBCHUDWARE'):
                env['CHUDWARE_LIBCHUDWARE'] = str(runtime / 'libchudware.so')
            if not env.get('CHUDWARE_HOSTD_BIN'):
                env['CHUDWARE_HOSTD_BIN'] = str(runtime / 'chudware-hostd')
        for key, directory in {'HOME': 'home', 'USERPROFILE': 'home', 'APPDATA': 'data',
                               'XDG_DATA_HOME': 'data', 'XDG_CONFIG_HOME': 'config',
                               'XDG_CACHE_HOME': 'cache', 'XDG_RUNTIME_DIR': 'run',
                               'CHUDWARE_CACHE_ROOT': 'agent-cache', 'CHUDWARE_SUBAGENTS_DIR': 'subagents',
                               'CHUDWARE_ECAD_LIB_DIR': 'ecad', 'CHUDWARE_HANDOFF_DIR': 'handoff',
                               'CHUDWARE_BOX_MIRROR_DIR': 'box-mirror', 'CHUDWARE_REVENG_DIR': 'reveng',
                               'CHUDWARE_MFG_STATE_DIR': 'manufacturing'}.items():
            value = Path(sandbox) / directory
            value.mkdir(exist_ok=True)
            env[key] = str(value)
        env.setdefault('OMP_NUM_THREADS', '4')
        env.setdefault('OPENBLAS_NUM_THREADS', '1')
        env.setdefault('MKL_NUM_THREADS', '1')
        for label, command, test in stages:
            remaining = None if selection.suites else args.budget - (time.monotonic() - started)
            reason = ''
            if failed or (remaining is not None and remaining <= 0):
                reason = 'earlier failure' if failed else 'budget exhausted'
                failed = True
            elif len(command) >= 3 and command[:2] == ['ninja', '-C'] and command[2].startswith('core/'):
                build_dir = command[2]
                if blocked:
                    reason = f'runtime rebuild already blocked: {blocked[0]}'
                else:
                    users = running_runtime_users(ROOT, build_dir)
                    if users:
                        reason = '; '.join(users)
                        blocked.append(build_dir)
                        print(f'WARNING: skipping {build_dir} build: {reason}', flush=True)
                if reason:
                    skipped_builds.add(build_dir)
            elif label == 'core ctest' and command[2] in skipped_builds:
                reason = 'core build was skipped'
            elif blocked and (test is not None or (label == 'host ctest' and host_needs_runtime)):
                reason = f'runtime rebuild skipped: {blocked[0]}'
            if reason:
                records.append((label, 'SKIPPED', 0.0, reason))
                incomplete = True
                continue
            print(f'Running {label}...', flush=True)
            result, elapsed, output = run_command(command, ROOT, env, remaining, test is not None)
            records.append((label, result, elapsed, ''))
            if result not in {'PASS', 'NO TESTS'}:
                failed = True
                print(f'--- {label}: {result} ---\n{output}', flush=True)
            elif not selection.suites and test is not None and result == 'PASS':
                timings[test] = elapsed
    print('\nSuite                                      Result       Seconds')
    for label, result, elapsed, reason in records:
        print(f'{label:<42} {result:<12} {elapsed:7.2f}' + (f'  {reason}' if reason else ''))
    budget = 'unbudgeted suite run' if selection.suites else f'budget {args.budget:g}s'
    total = time.monotonic() - started
    print(f'Total: {total:.2f}s ({budget})')
    if selection.suites and total > args.budget * len(selection.suites):
        # Warm suite runs target --budget seconds each; a cold build legitimately exceeds it.
        print(f'test_lite: WARNING suite run took {total:.0f}s, over the {args.budget:g}s-per-suite '
              'warm target; if the build was warm, split the slow suite in scripts/test_suites.json.')
    if failed or incomplete:
        print('Incomplete or failed; stamp not updated.')
        return 1
    if selection.suites:
        # Explicit suites do not cover all changed files; preserve the change-selection stamp.
        return 0
    try:
        if timings:
            save_json(ROOT / TIMINGS, timings)
        # Mark the scan start, not the finish: edits made during the run stay eligible.
        save_json(ROOT / STAMP, {'pending': sorted(pending)}, started_ns)
    except OSError as exc:
        print(f'test_lite: could not save state: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
