"""Read the shared native and Python test-suite manifest.

Pytest ownership uses the most specific listed file or directory, so a broad
package owner can be overridden for tests of a particular product area.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


# This module's public test_files() function is an inventory API, not a test.
__test__ = False

ROOT = Path(__file__).resolve().parent.parent
PYTEST_ROOTS = (
    'services', 'tools', 'scripts', 'assets', 'packaging',
    'benchmarks/artifact_validity', 'web',
)


@dataclass(frozen=True)
class Manifest:
    suites: dict[str, str]
    native: dict[str, dict[str, list[str]]]
    pytest: dict[str, list[str]]


def load(root: Path = ROOT) -> Manifest:
    """Read and validate the manifest relative to *root*."""
    manifest_path = root / 'scripts/test_suites.json'
    with manifest_path.open(encoding='utf-8') as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError(f'{manifest_path}: expected a version 1 manifest')

    def object_field(name: str) -> dict:
        value = data.get(name)
        if not isinstance(value, dict):
            raise ValueError(f'{manifest_path}: {name} must be an object')
        return value

    suites = object_field('suites')
    if any(not isinstance(name, str) or not isinstance(description, str)
           for name, description in suites.items()):
        raise ValueError(f'{manifest_path}: suite names and descriptions must be strings')

    def suite_entries(name: str) -> dict[str, list[str]]:
        entries = object_field(name)
        for suite, values in entries.items():
            if suite not in suites:
                raise ValueError(f'{manifest_path}: unknown suite {suite!r} in {name}')
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f'{manifest_path}: {name}.{suite} must be a list of strings')
        return entries

    native = {}
    for tree in ('core', 'host'):
        native[tree] = suite_entries(tree)
        seen: dict[str, str] = {}
        for suite, tests in native[tree].items():
            for test in tests:
                if test in seen:
                    raise ValueError(
                        f'{manifest_path}: native test {test!r} is listed in {tree} '
                        f'suites {seen[test]!r} and {suite!r}'
                    )
                seen[test] = suite

    pytest = suite_entries('pytest')
    seen_paths: dict[str, str] = {}
    for suite, paths in pytest.items():
        for path in paths:
            if path in seen_paths:
                raise ValueError(
                    f'{manifest_path}: pytest path {path!r} is listed twice '
                    f'in suites {seen_paths[path]!r} and {suite!r}'
                )
            if not (root / path).exists():
                raise ValueError(f'{manifest_path}: pytest path {path!r} does not exist')
            seen_paths[path] = suite

    return Manifest(suites=suites, native=native, pytest=pytest)


def test_files(root: Path = ROOT) -> list[str]:
    """Find Python test filenames under the gate's source roots."""
    result = []
    for source in PYTEST_ROOTS:
        for directory, dirs, files in os.walk(root / source):
            dirs[:] = [name for name in dirs
                       if name != '__pycache__' and name != 'node_modules'
                       and not name.startswith('.venv')]
            for name in files:
                if name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py')):
                    result.append((Path(directory) / name).relative_to(root).as_posix())
    return sorted(result)


def pytest_suite(path: str, manifest: Manifest) -> str | None:
    """Return the owner with the longest matching file/directory path."""
    owner = None
    longest = -1
    for suite, prefixes in manifest.pytest.items():
        for prefix in prefixes:
            if (path == prefix or path.startswith(prefix + '/')) and len(prefix) > longest:
                owner = suite
                longest = len(prefix)
    return owner


def pytest_files(manifest: Manifest, suite: str, root: Path = ROOT) -> list[str]:
    return [path for path in test_files(root) if pytest_suite(path, manifest) == suite]


def gate_unit(path: str) -> str:
    """Return the original gate sweep's independent pytest invocation unit."""
    parts = path.split('/')
    if parts[0] == 'services':
        return '/'.join(parts[:2])
    for source in PYTEST_ROOTS:
        if path == source or path.startswith(source + '/'):
            return source
    raise ValueError(f'not a Python test root: {path!r}')
