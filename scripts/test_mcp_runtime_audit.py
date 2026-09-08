from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_runtime_audit import check_dependencies


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
        'absent-package>=1; python_version >= "3.0" # applicable\n'
        'not-for-this-runtime==1; python_version < "3.0"\n'
    )
    result = check_dependencies(requirements)
    assert result["declared_missing"] == [
        {"pkg": "absent-package", "req_line": 'absent-package>=1; python_version >= "3.0" # applicable'},
    ]
    assert result["requirements_count"] == 1
