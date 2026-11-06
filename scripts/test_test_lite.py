from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from scripts import test_lite as lite


def put(root: Path, name: str, content: str = '') -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


@pytest.fixture(autouse=True)
def isolated_test_environment(monkeypatch):
    monkeypatch.setattr(lite.testbox_guard, 'isolated', lambda: True)
    for name in ('CMAKE_BUILD_PARALLEL_LEVEL', 'CHUDWARE_LIBCHUDWARE', 'CHUDWARE_HOSTD_BIN'):
        monkeypatch.delenv(name, raising=False)
    # Serial by default, so command-shape tests do not depend on this host's CPU count
    # or on pytest-xdist being installed; the parallel tests opt in explicitly.
    monkeypatch.setenv('CTEST_PARALLEL_LEVEL', '1')


def suite_manifest(root: Path, pytest_paths: dict[str, list[str]] | None = None) -> None:
    if pytest_paths is None:
        pytest_paths = {'infra': ['scripts']}
        if (root / 'services').is_dir():
            pytest_paths['agent'] = ['services']
    manifest = {
        'version': 1,
        'suites': {'sketch': 'Sketch tests', 'agent': 'Agent tests', 'infra': 'Infrastructure tests'},
        'core': {'sketch': ['sketch_2d', 'sketch_3d'], 'infra': ['parts', 'parts_extra']},
        'host': {'sketch': ['widget']},
        'pytest': pytest_paths,
    }
    put(root, 'scripts/test_suites.json', json.dumps(manifest))


def configured_core(root: Path, build_dir: str = 'core/build-linux') -> None:
    put(root, f'{build_dir}/build.ninja')
    put(root, f'{build_dir}/CTestTestfile.cmake')


def native_tree(root: Path, area: str, targets: dict[str, list[str]], registrations: dict[str, str],
                build: str | None = None) -> None:
    build = build or ('core/build-occt-linux' if area == 'core' else 'host/build-linux')
    entries = []
    for target, sources in targets.items():
        for source in sources:
            entries.append({'directory': str(root / build), 'file': str(root / source),
                            'output': str(root / build / f'CMakeFiles/{target}.dir/file.o')})
    put(root, f'{build}/build.ninja')
    put(root, f'{build}/compile_commands.json', json.dumps(entries))
    put(root, f'{build}/CTestTestfile.cmake', ''.join(
        f'add_test([=[{name}]=] "{root / build}/bin/{target}")\n'
        for name, target in registrations.items()))


def suite_root(root: Path, build_dirs: tuple[str, ...] = (
        'core/build-occt-linux', 'core/build-linux', 'host/build-linux')) -> None:
    for path in ('scripts/test_infra.py', 'scripts/test_sketch.py',
                 'services/alpha/test_one.py', 'services/alpha/test_two.py',
                 'services/beta/test_three.py'):
        put(root, path, 'def test_example(): pass\n')
    suite_manifest(root, {
        'sketch': ['scripts/test_sketch.py', 'services/alpha', 'services/beta'],
        'infra': ['scripts'],
    })
    for build_dir in build_dirs:
        put(root, f'{build_dir}/build.ninja')
        put(root, f'{build_dir}/CTestTestfile.cmake')


def test_python_imports_and_changed_tests(tmp_path):
    put(tmp_path, 'services/mcp/recon_tools.py')
    put(tmp_path, 'services/mcp/other.py')
    put(tmp_path, 'services/mcp/test_dotted.py', 'from services.mcp.recon_tools import thing\ndef test_thing(): pass\n')
    put(tmp_path, 'services/mcp/test_alias.py', 'from services.mcp import (other,\n recon_tools as recon)\ndef test_alias(): pass\n')
    put(tmp_path, 'services/mcp/test_relative.py', 'from . import recon_tools\nclass TestRelative:\n def test_it(self): pass\n')
    put(tmp_path, 'services/mcp/test_unrelated.py', 'from services.mcp import other\ndef test_other(): pass\n')
    put(tmp_path, 'services/mcp/test_roundtrip.py', 'from services.mcp import recon_tools\nasync def main(): pass\n')
    put(tmp_path, 'services/mcp/test_mcp_freshness.py', 'def test_fresh(): pass\n')
    put(tmp_path, 'scripts/test_lite.py', 'from __future__ import annotations\n')
    put(tmp_path, 'scripts/test_test_lite.py', 'from scripts import test_lite as lite\ndef test_runner(): pass\n')
    configured_core(tmp_path)
    result = lite.select(tmp_path, ['services/mcp/recon_tools.py', 'scripts/test_lite.py',
                                   'services/mcp/test_unrelated.py'])
    assert set(result.python_tests) == {
        'services/mcp/test_dotted.py', 'services/mcp/test_alias.py',
        'services/mcp/test_relative.py', 'services/mcp/test_mcp_freshness.py',
        'services/mcp/test_unrelated.py', 'scripts/test_test_lite.py',
    }
    assert result.check
    assert not result.unmatched
    assert 'services/mcp/test_roundtrip.py' not in result.python_tests
    assert 'services/mcp/test_roundtrip.py' not in lite.select(
        tmp_path, ['services/mcp/test_roundtrip.py']).python_tests
    assert not lite.imports_module('from services.mcp import recon_tools_extra\n',
                                   'services.mcp.recon_tools', 'services.mcp')

def test_missing_freshness_test_is_not_a_green(tmp_path):
    put(tmp_path, 'services/mcp/recon_tools.py')
    with pytest.raises(ValueError, match='freshness test is missing'):
        lite.select(tmp_path, ['services/mcp/recon_tools.py'])


def test_nested_module_can_use_parent_package_tests(tmp_path):
    put(tmp_path, 'services/parts/adapters/bolts.py')
    put(tmp_path, 'services/parts/test_bolts.py', 'from services.parts.adapters.bolts import adapter\ndef test_bolts(): pass\n')
    put(tmp_path, 'services/parts/tests/test_more.py', 'from services.parts.adapters import bolts\ndef test_more(): pass\n')
    configured_core(tmp_path)
    result = lite.select(tmp_path, ['services/parts/adapters/bolts.py'])
    assert set(result.python_tests) == {'services/parts/test_bolts.py', 'services/parts/tests/test_more.py'}
    assert not result.unmatched



def test_native_target_and_ctest_selection(tmp_path):
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/tests/test_other.cpp', '#include "other/other.h"\n')
    put(tmp_path, 'host/src/ui/widget.cpp', '#include "ui/widget.h"\n')
    put(tmp_path, 'host/src/ui/widget.h')
    put(tmp_path, 'host/src/ui/widget.inl')
    put(tmp_path, 'host/tests/test_widget.cpp', '#include "ui/widget.h"\n#include "ui/widget.inl"\n')
    put(tmp_path, 'host/tests/test_other.cpp', '#include "ui/other.h"\n')
    native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp'],
                                  'test_other': ['core/tests/test_other.cpp']},
                {'parts': 'test_parts', 'parts_extra': 'test_parts', 'other': 'test_other'})
    native_tree(tmp_path, 'host', {'test_widget': ['host/tests/test_widget.cpp', 'host/src/ui/widget.cpp'],
                                  'test_other': ['host/tests/test_other.cpp']},
                {'widget': 'test_widget', 'widget_alt': 'test_widget', 'other': 'test_other'})
    result = lite.select(tmp_path, ['core/parts/generate.cpp', 'host/src/ui/widget.cpp'])
    assert result.native == [lite.NativeSelection('core/build-occt-linux', ['test_parts'], ['parts', 'parts_extra']),
                             lite.NativeSelection('host/build-linux', ['test_widget'], ['widget', 'widget_alt'])]
    assert result.check
    assert not result.unmatched
    stages = lite.commands(result)
    assert stages[1][1] == ['ninja', '-C', 'core/build-occt-linux', 'test_parts']
    assert stages[2][1][-1] == '^(parts|parts_extra)$'
    assert '--no-tests=error' in stages[2][1]
    assert lite.select(tmp_path, ['host/src/ui/widget.h']).native[0].targets == ['test_widget']
    assert lite.select(tmp_path, ['host/src/ui/widget.inl']).native[0].targets == ['test_widget']
    assert lite.select(tmp_path, ['core/tests/test_parts.cpp']).native[0].tests == ['parts', 'parts_extra']

def test_runtime_rebuilds_and_fresh_test_paths(tmp_path, monkeypatch):
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp']}, {'parts': 'test_parts'})
    put(tmp_path, 'core/build-linux/build.ninja')
    put(tmp_path, 'services/mcp/recon_tools.py')
    put(tmp_path, 'services/mcp/test_mcp_freshness.py', 'def test_fresh(): pass\n')
    put(tmp_path, 'services/mcp/test_recon.py',
        'from services.mcp import recon_tools\ndef test_recon(): pass\n')
    suite_manifest(tmp_path)
    changed = ['core/parts/generate.cpp', 'services/mcp/recon_tools.py']
    result = lite.select(tmp_path, changed)
    assert result.runtime_builds == ['core/build-occt-linux', 'core/build-linux']
    stages = lite.commands(result)
    core = next(command for label, command, _ in stages if label == 'core build')
    assert core == ['ninja', '-C', 'core/build-occt-linux', 'test_parts', 'chudware',
                    'chudware-hostd', 'chudware-shape-build-worker']
    slim = next(command for label, command, _ in stages if label == 'core slim build')
    assert slim == ['ninja', '-C', 'core/build-linux', 'chudware',
                    'chudware-hostd', 'chudware-shape-build-worker']
    assert not lite.select(tmp_path, ['core/parts/generate.cpp']).runtime_builds
    assert lite.select(tmp_path, ['services/mcp/recon_tools.py']).runtime_builds == ['core/build-occt-linux']
    seen = []
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda root, now: set())
    monkeypatch.setattr(lite, 'running_runtime_users', lambda root, build: [])
    monkeypatch.setattr(lite, 'run_command',
                        lambda cmd, root, env, timeout, allow: (seen.append((cmd, env.copy())) or ('PASS', 0.1, '')))
    assert lite.main(['--files', *changed]) == 0
    assert seen[-1][1]['CHUDWARE_LIBCHUDWARE'] == str(tmp_path / 'core/build-occt-linux/bin/libchudware.so')
    assert seen[-1][1]['CHUDWARE_HOSTD_BIN'] == str(tmp_path / 'core/build-occt-linux/bin/chudware-hostd')
    assert not any(cmd[:2] == ['./build.sh', 'stage'] for cmd, _ in seen)

def test_runtime_guard_detects_executable_and_mapped_engine(tmp_path):
    proc = tmp_path / 'fake-proc'
    build = 'core/build-occt-linux'
    hostd = put(tmp_path, f'{build}/bin/chudware-hostd')
    library = put(tmp_path, f'{build}/bin/libchudware.so')
    worker = put(tmp_path, f'{build}/bin/chudware-shape-build-worker')
    other = put(tmp_path, 'external/chudware')
    noise = put(tmp_path, 'external/python')
    put(proc, '10/comm', 'chudware-hostd\n')
    (proc / '10/exe').symlink_to(hostd)
    put(proc, '11/comm', 'chudware\n')
    (proc / '11/exe').symlink_to(other)
    put(proc, '11/maps', f'1000-2000 r-xp 0 00:00 1 {library} (deleted)\n')
    put(proc, '12/comm', 'python\n')
    (proc / '12/exe').symlink_to(noise)
    put(proc, '12/maps', f'1000-2000 r-xp 0 00:00 1 {library}\n')
    put(proc, '13/comm', 'chudware-shape-\n')
    (proc / '13/exe').symlink_to(worker)
    assert lite.running_runtime_users(tmp_path, build, proc) == [
        f'chudware pid 11: libchudware mapped from {build}',
        f'chudware-hostd pid 10: executable in {build}',
        f'chudware-shape-build-worker pid 13: executable in {build}',
    ]
    assert lite.running_runtime_users(tmp_path, 'core/build-linux', proc) == []


def test_busy_runtime_skips_dependent_suites_and_stamp(tmp_path, monkeypatch, capsys):
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp']}, {'parts': 'test_parts'})
    put(tmp_path, 'core/build-linux/build.ninja')
    put(tmp_path, 'services/mcp/recon_tools.py')
    put(tmp_path, 'services/mcp/test_mcp_freshness.py', 'def test_fresh(): pass\n')
    suite_manifest(tmp_path)
    changed = ['core/parts/generate.cpp', 'services/mcp/recon_tools.py']
    seen = []
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda root, now: set())
    monkeypatch.setattr(lite, 'running_runtime_users', lambda root, build: ['chudware-hostd pid 10'])
    monkeypatch.setattr(lite, 'run_command',
                        lambda cmd, root, env, timeout, allow: (seen.append(cmd) or ('PASS', 0.1, '')))
    assert lite.main(['--files', *changed]) == 1
    output = capsys.readouterr().out
    assert 'WARNING: skipping core/build-occt-linux' in output
    assert 'chudware-hostd pid 10' in output and 'SKIPPED' in output
    assert seen == [[sys.executable, 'scripts/check.py']]
    assert not (tmp_path / lite.STAMP).exists()
    # Even a seemingly static native test target may pull in a shared engine.
    assert lite.main(['--files', 'core/parts/generate.cpp']) == 1
    assert seen == [[sys.executable, 'scripts/check.py']]



def test_host_in_process_test_uses_fresh_runtime(tmp_path):
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp']}, {'parts': 'test_parts'})
    put(tmp_path, 'host/src/ui/widget.cpp')
    put(tmp_path, 'host/tests/test_widget.cpp')
    put(tmp_path, 'services/worker/cpp_core.cpp')
    native_tree(tmp_path, 'host', {'test_widget': ['host/tests/test_widget.cpp', 'host/src/ui/widget.cpp',
                                                  'services/worker/cpp_core.cpp']},
                {'widget': 'test_widget'})
    selection = lite.select(tmp_path, ['core/parts/generate.cpp', 'host/src/ui/widget.cpp'])
    assert selection.native[-1].uses_engine
    assert selection.runtime_builds == ['core/build-occt-linux']
    commands = lite.commands(selection)
    core_build = next(command for label, command, _ in commands if label == 'core build')
    assert core_build[-3:] == ['chudware', 'chudware-hostd', 'chudware-shape-build-worker']
    assert all(command[:2] != ['./build.sh', 'stage'] for _, command, _ in commands)


def test_no_tests_exit_is_nonfatal_only_for_pytest(tmp_path, monkeypatch, capsys):
    command = [sys.executable, '-c', 'raise SystemExit(5)']
    assert lite.run_command(command, tmp_path, os.environ.copy(), 3, True)[0] == 'NO TESTS'
    assert lite.run_command(command, tmp_path, os.environ.copy(), 3)[0] == 'FAIL (5)'
    put(tmp_path, 'scripts/test_a.py', 'def test_a(): pass\n')
    put(tmp_path, 'scripts/test_b.py', 'def test_b(): pass\n')
    suite_manifest(tmp_path)
    configured_core(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda root, now: set())
    monkeypatch.setattr(lite, 'running_runtime_users', lambda root, build: [])
    results = iter([('PASS', 0.1, ''), ('NO TESTS', 0.1, ''), ('PASS', 0.2, '')])
    monkeypatch.setattr(lite, 'run_command', lambda *args: next(results))
    assert lite.main(['--files', 'scripts/test_a.py', 'scripts/test_b.py']) == 0
    assert 'NO TESTS' in capsys.readouterr().out
    assert (tmp_path / lite.STAMP).exists()


def test_missing_native_build_is_not_a_green(tmp_path):
    put(tmp_path, 'core/parts/generate.cpp')
    with pytest.raises(ValueError, match='configured Ninja tree'):
        lite.select(tmp_path, ['core/parts/generate.cpp'])


def test_ordering_uses_recorded_times_then_size(tmp_path):
    for name, size in [('test_a.py', 2000), ('test_b.py', 5000), ('test_c.py', 8000)]:
        put(tmp_path, 'scripts/' + name, 'def test_case(): pass\n#' + 'x' * size)
    configured_core(tmp_path)
    changed = ['scripts/test_a.py', 'scripts/test_b.py', 'scripts/test_c.py']
    assert lite.select(tmp_path, changed, {'scripts/test_c.py': 0.5}).python_tests == [
        'scripts/test_c.py', 'scripts/test_a.py', 'scripts/test_b.py']


def repo_git(root: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def test_status_since_stamp_and_scoped_pending(tmp_path):
    repo_git(tmp_path, 'init', '-q')
    repo_git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    repo_git(tmp_path, 'config', 'user.name', 'test')
    old = put(tmp_path, 'services/old.py', 'old')
    changed = put(tmp_path, 'services/changed.py', 'old')
    repo_git(tmp_path, 'add', '.')
    repo_git(tmp_path, 'commit', '-qm', 'initial')
    old.write_text('modified', encoding='utf-8')
    changed.write_text('modified', encoding='utf-8')
    new = put(tmp_path, 'services/new with space.py', 'new')
    ignored_build = put(tmp_path, 'host/build-x/out.cpp', 'build')
    now = time.time_ns()
    os.utime(old, ns=(now - 25 * 3600 * 10**9,) * 2)
    assert lite.changed_files(tmp_path, None, None, now) == ['services/changed.py', 'services/new with space.py']
    assert 'services/old.py' in lite.changed_files(tmp_path, 'HEAD', None, now)
    assert 'host/build-x/out.cpp' not in lite.changed_files(tmp_path, 'HEAD', None, now)
    assert lite.changed_files(tmp_path, None, ['services/old.py'], now) == ['services/old.py']
    with pytest.raises(ValueError, match='build directory'):
        lite.changed_files(tmp_path, None, [str(ignored_build)], now)
    lite.save_json(tmp_path / lite.STAMP, {'pending': ['services/new with space.py']}, now)
    assert lite.changed_files(tmp_path, None, None, now + 10**9) == ['services/new with space.py']
    os.utime(changed, ns=(now + 1, now + 1))
    assert lite.changed_files(tmp_path, None, None, now + 10**9) == [
        'services/changed.py', 'services/new with space.py']
    assert new.is_file()


def test_budget_failure_does_not_update_stamp_and_reports_remaining(tmp_path, monkeypatch, capsys):
    put(tmp_path, 'scripts/test_a.py', 'def test_a(): pass\n')
    put(tmp_path, 'scripts/test_b.py', 'def test_b(): pass\n')
    suite_manifest(tmp_path)
    configured_core(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda root, now: set())
    monkeypatch.setattr(lite, 'running_runtime_users', lambda root, build: [])
    monkeypatch.setattr(lite, 'run_command', lambda *args: ('BUDGET', 0.1, 'timed out'))
    assert lite.main(['--files', 'scripts/test_a.py', 'scripts/test_b.py']) == 1
    output = capsys.readouterr().out
    assert 'BUDGET' in output and 'SKIPPED' in output
    assert not (tmp_path / lite.STAMP).exists()


def test_scoped_success_keeps_other_dirty_files_pending(tmp_path, monkeypatch):
    put(tmp_path, 'scripts/test_a.py', 'def test_a(): pass\n')
    put(tmp_path, 'scripts/test_b.py', 'def test_b(): pass\n')
    suite_manifest(tmp_path)
    configured_core(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda root, now: {'scripts/test_a.py', 'scripts/test_b.py'})
    monkeypatch.setattr(lite, 'running_runtime_users', lambda root, build: [])
    monkeypatch.setattr(lite, 'run_command', lambda *args: ('PASS', 0.1, ''))
    assert lite.main(['--files', 'scripts/test_a.py']) == 0
    assert json.loads((tmp_path / lite.STAMP).read_text())['pending'] == ['scripts/test_b.py']


def test_named_suite_union_uses_unit_groups_without_budget_or_stamp(tmp_path, monkeypatch, capsys):
    suite_root(tmp_path)
    stamp = put(tmp_path, str(lite.STAMP), '{"pending": ["keep.py"]}\n')
    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'changed_files', lambda *args: pytest.fail('suite run scanned changes'))
    monkeypatch.setattr(lite, 'running_runtime_users', lambda *args: [])
    calls = []
    environments = []

    def run(command, root, env, timeout, allow_no_tests):
        assert root == tmp_path
        calls.append((command, timeout, allow_no_tests))
        environments.append(env.copy())
        return 'PASS', 0.1, ''

    monkeypatch.setattr(lite, 'run_command', run)
    assert lite.main(['--suite', 'sketch,infra', '--suite', 'sketch', '--budget', '0']) == 0
    assert 'unbudgeted suite run' in capsys.readouterr().out
    for index, build_dir in enumerate(('core/build-occt-linux', 'core/build-linux', 'host/build-linux')):
        targets = ['tests-sketch', 'tests-infra']
        if build_dir == 'core/build-linux':
            targets += ['chudware', 'chudware-hostd', 'chudware-shape-build-worker']
        assert calls[2 * index] == (['ninja', '-C', build_dir, *targets], None, False)
        assert calls[2 * index + 1] == (['ctest', '--test-dir', build_dir, '-L',
                                       '^(sketch|infra)$', '--output-on-failure'], None, False)
    pytest_prefix = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q']
    assert calls[6:] == [
        (pytest_prefix + ['scripts/test_infra.py', 'scripts/test_sketch.py'], None, True),
        (pytest_prefix + ['services/alpha/test_one.py', 'services/alpha/test_two.py'], None, True),
        (pytest_prefix + ['services/beta/test_three.py'], None, True),
    ]
    assert environments[-1]['CHUDWARE_LIBCHUDWARE'] == str(tmp_path / 'core/build-linux/bin/libchudware.so')
    assert environments[-1]['CHUDWARE_HOSTD_BIN'] == str(tmp_path / 'core/build-linux/bin/chudware-hostd')
    assert (stamp.read_bytes(), stamp.stat().st_mtime_ns) == before


def test_named_suite_dry_run_prints_only_its_commands(tmp_path, monkeypatch, capsys):
    suite_root(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'run_command', lambda *args: pytest.fail('dry run executed a command'))
    assert lite.main(['--suite', 'sketch', '--dry-run']) == 0
    output = capsys.readouterr().out
    assert 'Selected suites: sketch' in output
    for build_dir in ('core/build-occt-linux', 'core/build-linux', 'host/build-linux'):
        assert f'ninja -C {build_dir} tests-sketch' in output
        assert f"ctest --test-dir {build_dir} -L '^(sketch)$' --output-on-failure" in output
    assert ('ninja -C core/build-linux tests-sketch chudware chudware-hostd '
            'chudware-shape-build-worker') in output
    assert 'services/alpha/test_one.py services/alpha/test_two.py' in output
    assert 'scripts/test_sketch.py' in output
    assert 'scripts/test_infra.py' not in output
    assert not (tmp_path / lite.STAMP).exists()


def test_suite_listing_and_unknown_names(tmp_path, monkeypatch, capsys):
    suite_root(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'changed_files', lambda *args: pytest.fail('listing scanned changes'))
    assert lite.main(['--list-suites']) == 0
    output = capsys.readouterr().out
    assert 'sketch: Sketch tests (core 2 tests, host 1 tests, pytest 4 files)' in output
    assert 'agent: Agent tests (core 0 tests, host 0 tests, pytest 0 files)' in output
    assert 'infra: Infrastructure tests (core 2 tests, host 0 tests, pytest 1 files)' in output
    with pytest.raises(SystemExit) as exc:
        lite.main(['--suite', 'sketch,unknown', '--dry-run'])
    assert exc.value.code == 2
    assert 'unknown suite(s): unknown' in capsys.readouterr().err


def test_change_plan_identifies_each_test_suite(tmp_path, capsys):
    suite_root(tmp_path)
    manifest = lite.test_suites.load(tmp_path)
    selection = lite.Selection(
        ['core/tests/example.cpp'], ['services/alpha/test_one.py', 'scripts/test_infra.py'],
        [lite.NativeSelection('core/build-linux', ['test_sketch'], ['sketch_2d']),
         lite.NativeSelection('host/build-linux', ['test_widget'], ['widget'])], [], False, [])
    lite.print_plan(selection, [], manifest)
    output = capsys.readouterr().out
    assert 'core/sketch_2d: sketch' in output
    assert 'host/widget: sketch' in output
    assert 'services/alpha/test_one.py: sketch' in output
    assert 'scripts/test_infra.py: infra' in output


def test_suite_builds_honor_parallelism_setting(tmp_path, monkeypatch):
    suite_root(tmp_path, ('core/build-linux',))
    monkeypatch.setenv('CMAKE_BUILD_PARALLEL_LEVEL', '3')
    selection = lite.select_suites(tmp_path, lite.test_suites.load(tmp_path), ('sketch',))
    assert lite.commands(selection)[0][1] == [
        'ninja', '-C', 'core/build-linux', '-j', '3', 'tests-sketch',
        'chudware', 'chudware-hostd', 'chudware-shape-build-worker']


def test_suite_keeps_runtime_guard_for_each_core_tree(tmp_path, monkeypatch, capsys):
    suite_root(tmp_path, ('core/build-linux', 'host/build-linux'))
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'running_runtime_users', lambda *args: ['chudware pid 1'])
    calls = []
    monkeypatch.setattr(lite, 'run_command',
                        lambda command, *args: (calls.append(command) or ('PASS', 0.1, '')))
    assert lite.main(['--suite', 'sketch']) == 1
    assert calls == [['ninja', '-C', 'host/build-linux', 'tests-sketch']]
    assert 'core build was skipped' in capsys.readouterr().out
    assert not (tmp_path / lite.STAMP).exists()

@pytest.mark.parametrize('args', [[], ['--suite', 'sketch'], ['--files', 'missing.py']])
def test_runner_refuses_host_before_discovery(tmp_path, monkeypatch, capsys, args):
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite.testbox_guard, 'isolated', lambda: False)
    monkeypatch.setattr(lite.test_suites, 'load', lambda *args: pytest.fail('loaded manifest on host'))
    assert lite.main(args) == 3
    assert capsys.readouterr().err == lite.testbox_guard.MESSAGE + '\n'


def test_host_can_list_suites_and_preview_without_running_tests(tmp_path, monkeypatch, capsys):
    suite_root(tmp_path)
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite.testbox_guard, 'isolated', lambda: False)
    monkeypatch.setattr(lite, 'run_command', lambda *args: pytest.fail('host ran a command'))
    assert lite.main(['--list-suites']) == 0
    assert 'sketch: Sketch tests' in capsys.readouterr().out
    assert lite.main(['--suite', 'sketch', '--dry-run']) == 0
    assert 'tests-sketch' in capsys.readouterr().out


@pytest.mark.parametrize('args', [['--suite', 'sketch'], ['--files', 'scripts/test_sketch.py']])
def test_python_selection_builds_runtime_and_honors_explicit_paths(tmp_path, monkeypatch, args):
    suite_root(tmp_path, ('core/build-linux',))
    monkeypatch.setattr(lite, 'ROOT', tmp_path)
    monkeypatch.setattr(lite, 'default_files', lambda *args: set())
    monkeypatch.setattr(lite, 'running_runtime_users', lambda *args: [])
    monkeypatch.setenv('CHUDWARE_LIBCHUDWARE', '/custom/libchudware.so')
    monkeypatch.setenv('CHUDWARE_HOSTD_BIN', '/custom/chudware-hostd')
    calls = []

    def run(command, root, env, timeout, allow_no_tests):
        calls.append((command, env.copy()))
        return 'PASS', 0.1, ''

    monkeypatch.setattr(lite, 'run_command', run)
    assert lite.main(args) == 0
    assert calls[0][0][-3:] == ['chudware', 'chudware-hostd', 'chudware-shape-build-worker']
    assert calls[-1][1]['CHUDWARE_LIBCHUDWARE'] == '/custom/libchudware.so'
    assert calls[-1][1]['CHUDWARE_HOSTD_BIN'] == '/custom/chudware-hostd'


def test_python_selection_requires_a_configured_core_tree(tmp_path):
    put(tmp_path, 'scripts/test_one.py', 'def test_one(): pass\n')
    suite_manifest(tmp_path)
    with pytest.raises(ValueError, match='Python tests need a configured core Ninja tree'):
        lite.select(tmp_path, ['scripts/test_one.py'])
    with pytest.raises(ValueError, match='Python tests need a configured core Ninja tree'):
        lite.select_suites(tmp_path, lite.test_suites.load(tmp_path), ('infra',))


def test_native_only_suite_does_not_build_python_runtime(tmp_path):
    suite_root(tmp_path, ('core/build-linux',))
    selection = lite.select_suites(tmp_path, lite.test_suites.load(tmp_path), ('agent',))
    assert not selection.python_tests
    assert not selection.runtime_builds
    assert lite.commands(selection)[0][1] == ['ninja', '-C', 'core/build-linux', 'tests-agent']


def test_change_selection_uses_the_only_configured_core_tree(tmp_path):
    # A testbox slot configures core/build-linux alone; nothing may name the OCCT tree.
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp']}, {'parts': 'test_parts'},
                build='core/build-linux')
    put(tmp_path, 'services/mcp/recon_tools.py')
    put(tmp_path, 'services/mcp/test_mcp_freshness.py', 'def test_fresh(): pass\n')
    suite_manifest(tmp_path)
    selection = lite.select(tmp_path, ['core/parts/generate.cpp', 'services/mcp/recon_tools.py'])
    assert selection.native == [lite.NativeSelection('core/build-linux', ['test_parts'], ['parts'])]
    assert selection.runtime_builds == ['core/build-linux']
    stages = lite.commands(selection)
    assert [(label, command[:3]) for label, command, _ in stages if command[0] != sys.executable] == [
        ('core build', ['ninja', '-C', 'core/build-linux']),
        ('core ctest', ['ctest', '--test-dir', 'core/build-linux'])]
    assert stages[1][1][-3:] == ['chudware', 'chudware-hostd', 'chudware-shape-build-worker']
    assert not any('core/build-occt-linux' in part for _, command, _ in stages for part in command)
    # Python-only changes in the same slot still find the runtime tree.
    assert lite.select(tmp_path, ['services/mcp/recon_tools.py']).runtime_builds == ['core/build-linux']


def test_change_selection_keeps_occt_tree_first_on_developer_checkouts(tmp_path):
    put(tmp_path, 'core/parts/generate.cpp', '#include "parts/parts.h"\n')
    put(tmp_path, 'core/parts/parts.h')
    put(tmp_path, 'core/tests/test_parts.cpp', '#include "parts/parts.h"\n')
    for build in ('core/build-occt-linux', 'core/build-linux'):
        native_tree(tmp_path, 'core', {'test_parts': ['core/tests/test_parts.cpp']}, {'parts': 'test_parts'},
                    build=build)
    selection = lite.select(tmp_path, ['core/parts/generate.cpp'])
    assert [item.build_dir for item in selection.native] == ['core/build-occt-linux']


@pytest.mark.parametrize(('jobs', 'xdist', 'files', 'workers'), [
    ('6', True, 9, 6),
    ('6', True, 3, 3),      # never more workers than files
    ('2', True, 9, 2),
    ('1', True, 9, 0),      # a single job runs serially
    ('6', False, 9, 0),     # no xdist in the runner: unchanged serial command
    ('6', True, 1, 0),      # a lone file cannot be spread by loadfile
])
def test_pytest_workers_only_when_xdist_can_help(monkeypatch, jobs, xdist, files, workers):
    monkeypatch.setenv('CTEST_PARALLEL_LEVEL', jobs)
    monkeypatch.setattr(lite, 'has_xdist', lambda: xdist)
    paths = [f'scripts/test_{index}.py' for index in range(files)]
    expected = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q']
    if workers:
        expected += ['-n', str(workers), '--dist', 'loadfile']
    assert lite.pytest_command(paths) == [*expected, *paths]
    assert lite.pytest_command(paths, fail_fast=True) == [*expected, '-x', *paths]


@pytest.mark.parametrize('level', ['', 'many', '0', '-3'])
def test_parallel_jobs_fall_back_to_the_cpu_count(monkeypatch, level):
    monkeypatch.setenv('CTEST_PARALLEL_LEVEL', level)
    monkeypatch.setattr(lite.os, 'sched_getaffinity', lambda pid: set(range(5)))
    assert lite.parallel_jobs() == 5
    monkeypatch.setenv('CTEST_PARALLEL_LEVEL', '7')
    assert lite.parallel_jobs() == 7


def test_suite_units_run_in_parallel_but_change_runs_stay_per_file(tmp_path, monkeypatch):
    suite_root(tmp_path, ('core/build-linux',))
    monkeypatch.setenv('CTEST_PARALLEL_LEVEL', '4')
    monkeypatch.setattr(lite, 'has_xdist', lambda: True)
    selection = lite.select_suites(tmp_path, lite.test_suites.load(tmp_path), ('sketch',))
    pytests = {label: command for label, command, _ in lite.commands(selection) if label.startswith('pytest')}
    prefix = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q']
    assert pytests['pytest services/alpha'] == [
        *prefix, '-n', '2', '--dist', 'loadfile', 'services/alpha/test_one.py', 'services/alpha/test_two.py']
    # A unit of one file stays in-process.
    assert pytests['pytest services/beta'] == [*prefix, 'services/beta/test_three.py']
    changes = lite.Selection([], ['services/alpha/test_one.py'], [], ['core/build-linux'], False, [])
    assert [command for label, command, _ in lite.commands(changes) if label.startswith('pytest')] == [
        [*prefix, '-x', 'services/alpha/test_one.py']]




def test_unbudgeted_command_waits_without_timeout(tmp_path, monkeypatch):
    class Process:
        def wait(self, timeout):
            assert timeout is None
            return 0

    monkeypatch.setattr(lite.subprocess, 'Popen', lambda *args, **kwargs: Process())
    assert lite.run_command(['unused'], tmp_path, {}, None)[0] == 'PASS'



def test_actual_process_budget_stops_own_command(tmp_path):
    # A private daemon can create a separate session outside pytest's group.
    code = (
        'import pathlib, subprocess, sys, time; '
        'p = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(30)\"], start_new_session=True); '
        'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)'
    )
    pidfile = tmp_path / 'child.pid'
    result, elapsed, _ = lite.run_command([sys.executable, '-c', code, str(pidfile)],
                                          tmp_path, os.environ.copy(), 0.5)
    assert result == 'BUDGET'
    assert elapsed < 6
    try:
        pidfd = os.pidfd_open(int(pidfile.read_text()))
    except ProcessLookupError:
        return
    try:
        assert select.select([pidfd], [], [], 2)[0]
    finally:
        os.close(pidfd)
