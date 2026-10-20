from __future__ import annotations

import importlib.util
import importlib.metadata
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_runtime_audit import check_dependencies
import mcp_runtime_audit as audit


def test_file_install_satisfies_pin_but_wrong_version_does_not(tmp_path, monkeypatch):
    metadata = tmp_path / "chudcode_rpc-0.2.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: chudcode-rpc\nVersion: 0.2.0\n")
    (metadata / "direct_url.json").write_text(json.dumps({
        "url": "file:///home/hunter/Repositories/omp/python/chudcode-rpc",
        "dir_info": {"editable": True},
    }))
    distributions = importlib.metadata.distributions
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: distributions(path=[str(tmp_path)]))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("chudcode-rpc==0.2.0 # private editable package\n")
    assert check_dependencies(requirements)["declared_missing"] == []
    (metadata / "METADATA").write_text("Name: chudcode-rpc\nVersion: 0.1.0\n")
    assert check_dependencies(requirements)["declared_missing"] == [
        {"pkg": "chudcode-rpc", "req_line": "chudcode-rpc==0.2.0 # private editable package"},
    ]


def test_missing_requirements_respect_environment_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        '--no-index\n--only-binary=:all:\n'
        'absent-package>=1; python_version >= "3.0" # applicable\n'
        'not-for-this-runtime==1; python_version < "3.0"\n'
    )
    result = check_dependencies(requirements)
    assert result["declared_missing"] == [
        {"pkg": "absent-package", "req_line": 'absent-package>=1; python_version >= "3.0" # applicable'},
    ]
    assert result["requirements_count"] == 1


@pytest.mark.parametrize("configured", [None, "/private/caller/libchudware.so"])
def test_import_preserves_engine_and_output_configuration(monkeypatch, configured):
    for name in ("CHUDWARE_LIBCHUDWARE", "CHUDWARE_QUIET"):
        if configured is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, configured)
    spec = importlib.util.spec_from_file_location("_audit_import_probe", audit.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import os

    assert os.environ.get("CHUDWARE_LIBCHUDWARE") == configured
    assert os.environ.get("CHUDWARE_QUIET") == configured


@pytest.mark.parametrize("explicit", [False, True])
def test_audit_resolves_existing_library_without_mutating_caller_config(
        tmp_path, monkeypatch, explicit):
    import os
    from services.mcp import core_bridge

    name = core_bridge._lib_name()
    full_build = (tmp_path / "build" / "windows-occt" if sys.platform == "win32"
                  else tmp_path / "core" / "build-occt-linux")
    full_engine = full_build / "bin" / name
    full_engine.parent.mkdir(parents=True)
    full_engine.touch()
    stale_engine = tmp_path / "core" / "build-linux" / "bin" / name
    stale_engine.parent.mkdir(parents=True)
    stale_engine.touch()
    monkeypatch.setattr(core_bridge, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("CHUDWARE_DIST", str(tmp_path / "dist"))
    if explicit:
        monkeypatch.setenv("CHUDWARE_LIBCHUDWARE", str(stale_engine))
        expected = stale_engine
    else:
        monkeypatch.delenv("CHUDWARE_LIBCHUDWARE", raising=False)
        expected = full_engine
    configured = os.environ.get("CHUDWARE_LIBCHUDWARE")
    # An empty catalog audits resolution and reporting without providers, devices,
    # or RPCs; path selection still uses the real canonical core resolver.
    monkeypatch.setattr(audit, "FAMILY_REGISTRY", [])
    monkeypatch.setattr(audit, "check_dependencies", lambda: {
        "installed_pkgs": set(), "req_pkgs": set(), "declared_missing": []})

    result = audit.run_audit()

    assert result["libchudware"] == str(expected)
    assert result["total_tools"] == 0
    assert os.environ.get("CHUDWARE_LIBCHUDWARE") == configured
