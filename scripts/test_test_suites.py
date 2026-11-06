from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check, test_suites


def put(root: Path, name: str, content: str = '') -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def write_manifest(root: Path, **changes) -> None:
    data = {
        'version': 1,
        'suites': {'agent': 'Agent runtime', 'ecad': 'Electronics'},
        'core': {},
        'host': {},
        'pytest': {},
    }
    data.update(changes)
    put(root, 'scripts/test_suites.json', json.dumps(data))


def test_longest_prefix_owns_files_and_respects_path_boundaries(tmp_path):
    put(tmp_path, 'services/mcp/test_general.py')
    put(tmp_path, 'services/mcp/test_ecad.py')
    put(tmp_path, 'services/mcp_extra/test_other.py')
    write_manifest(tmp_path, pytest={
        'agent': ['services/mcp'],
        'ecad': ['services/mcp/test_ecad.py'],
    })
    manifest = test_suites.load(tmp_path)
    assert test_suites.pytest_suite('services/mcp/test_general.py', manifest) == 'agent'
    assert test_suites.pytest_suite('services/mcp/test_ecad.py', manifest) == 'ecad'
    assert test_suites.pytest_suite('services/mcp_extra/test_other.py', manifest) is None
    assert test_suites.pytest_files(manifest, 'agent', tmp_path) == [
        'services/mcp/test_general.py',
    ]
    assert test_suites.pytest_files(manifest, 'ecad', tmp_path) == [
        'services/mcp/test_ecad.py',
    ]


def test_inventory_detects_unowned_files_and_ignores_environment_dirs(tmp_path):
    put(tmp_path, 'services/agent/test_owned.py')
    put(tmp_path, 'tools/nested/check_test.py')
    put(tmp_path, 'web/.venv-local/test_hidden.py')
    put(tmp_path, 'web/node_modules/test_hidden.py')
    put(tmp_path, 'web/__pycache__/test_hidden.py')
    put(tmp_path, 'web/hidden.py')
    write_manifest(tmp_path, pytest={'agent': ['services/agent']})
    manifest = test_suites.load(tmp_path)
    files = test_suites.test_files(tmp_path)
    assert files == ['services/agent/test_owned.py', 'tools/nested/check_test.py']
    assert [path for path in files if test_suites.pytest_suite(path, manifest) is None] == [
        'tools/nested/check_test.py',
    ]


def test_check_audit_reports_unowned_files(tmp_path, monkeypatch, capsys):
    put(tmp_path, 'services/agent/test_owned.py')
    put(tmp_path, 'tools/test_unowned.py')
    files = test_suites.test_files(tmp_path)
    write_manifest(tmp_path, pytest={'agent': ['services/agent']})
    manifest = test_suites.load(tmp_path)
    monkeypatch.setattr(check.test_suites, 'load', lambda: manifest)
    monkeypatch.setattr(check.test_suites, 'test_files', lambda: files)
    assert check.audit_test_suites() == 1
    assert 'tools/test_unowned.py' in capsys.readouterr().out


@pytest.mark.parametrize('field', ['core', 'host', 'pytest'])
def test_load_rejects_unknown_suite(tmp_path, field):
    write_manifest(tmp_path, **{field: {'missing': []}})
    with pytest.raises(ValueError, match=r'unknown suite .missing. in ' + field):
        test_suites.load(tmp_path)


@pytest.mark.parametrize('tree', ['core', 'host'])
def test_load_rejects_duplicate_native_tests(tmp_path, tree):
    write_manifest(tmp_path, **{tree: {'agent': ['same_test'], 'ecad': ['same_test']}})
    with pytest.raises(ValueError, match=r"native test 'same_test'.*" + tree):
        test_suites.load(tmp_path)


def test_native_names_can_be_reused_across_trees(tmp_path):
    write_manifest(tmp_path, core={'agent': ['same_test']}, host={'ecad': ['same_test']})
    manifest = test_suites.load(tmp_path)
    assert manifest.native['core']['agent'] == ['same_test']
    assert manifest.native['host']['ecad'] == ['same_test']


def test_load_rejects_missing_pytest_path(tmp_path):
    write_manifest(tmp_path, pytest={'agent': ['services/absent']})
    with pytest.raises(ValueError, match=r"pytest path 'services/absent' does not exist"):
        test_suites.load(tmp_path)


@pytest.mark.parametrize('same_suite', [True, False])
def test_load_rejects_duplicate_pytest_paths(tmp_path, same_suite):
    put(tmp_path, 'services/agent/test_one.py')
    path = 'services/agent'
    mapping = {'agent': [path, path]} if same_suite else {'agent': [path], 'ecad': [path]}
    write_manifest(tmp_path, pytest=mapping)
    with pytest.raises(ValueError, match=r"pytest path 'services/agent' is listed twice"):
        test_suites.load(tmp_path)


@pytest.mark.parametrize(('path', 'expected'), [
    ('services/agent/test_a.py', 'services/agent'),
    ('services/agent/advisor/test_b.py', 'services/agent'),
    ('services/test_a.py', 'services/test_a.py'),
    ('tools/nested/test_a.py', 'tools'),
    ('benchmarks/artifact_validity/test_a.py', 'benchmarks/artifact_validity'),
    ('web/gateway/tests/test_a.py', 'web'),
])
def test_gate_units(path, expected):
    assert test_suites.gate_unit(path) == expected
