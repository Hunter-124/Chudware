"""Environment authority boundaries for qualified deployment entrypoints."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gateway.config import Settings

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def isolated_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CHUDWARE_WEBD_ENV", "CHUDWARE_CELLS_ROOT", "CHUDWARE_WORKSPACE_ROOT",
                "CHUDWARE_WEB_BUCKET_ROOT", "CHUDWARE_DEPLOY_ENV"):
        monkeypatch.delenv(key, raising=False)
    prod = tmp_path / "chudware-web"
    prod.mkdir()
    workspace = tmp_path / "production-workspace"
    workspace.mkdir()
    (workspace / "production-only.txt").write_text("private production marker")
    (prod / "webd.env").write_text(
        f"CHUDWARE_WORKSPACE_ROOT={workspace}\n"
    )
    return tmp_path, workspace


def test_staging_does_not_inherit_production_discovery(isolated_discovery):
    home, production = isolated_discovery
    prod = Settings(deploy_environment="prod", data_root=home / "prod-data")
    stage = Settings(deploy_environment="staging", data_root=home / "stage-data")
    assert prod.workspace_root == production
    assert (prod.workspace_root / "production-only.txt").read_text() == "private production marker"
    assert not (stage.workspace_root / "production-only.txt").exists()
    assert stage.workspace_root.is_relative_to(stage.data_root)
    assert stage.bucket_root.is_relative_to(stage.data_root)
    assert stage.cells_root != prod.cells_root
    assert stage.cells_root == home / "chudware-web-staging" / "cells"
    assert prod.cells_root == home / "chudware-web" / "cells"
    assert production not in stage.project_roots


@pytest.mark.parametrize("field", ["workspace_root", "bucket_root", "university_workspace_root"])
def test_staging_refuses_foreign_storage_roots(isolated_discovery, field):
    home, production = isolated_discovery
    with pytest.raises(ValueError, match="staging"):
        Settings(deploy_environment="staging", data_root=home / "stage-data", **{field: production})


def test_explicit_cell_root_is_honored(isolated_discovery, monkeypatch):
    home, _ = isolated_discovery
    monkeypatch.setenv("CHUDWARE_CELLS_ROOT", str(home / "isolated-cells"))
    stage = Settings(deploy_environment="staging", data_root=home / "stage-data")
    assert stage.cells_root == home / "isolated-cells"


def test_unknown_environment_cannot_default_to_production(isolated_discovery):
    with pytest.raises(ValueError, match="CHUDWARE_DEPLOY_ENV"):
        Settings(deploy_environment="stagin")


@pytest.mark.parametrize("script,args,exit_code", [
    ("install_webd.sh", [], 4), ("rollback.sh", ["--list"], 2),
    ("update.sh", ["staging", "--no-pull"], 4), ("setup.sh", ["staging"], 2),
])
def test_same_account_staging_operations_refuse_before_mutation(tmp_path, script, args, exit_code):
    data = tmp_path / "untouched"
    cp = subprocess.run(["bash", str(REPO / "Webservice/deploy" / script), *args],
                        env={"HOME": str(tmp_path), "PATH": os.defpath,
                             "CHUDWARE_DEPLOY_ENV": "staging", "CHUDWARE_WEB_DATA": str(data)},
                        text=True, capture_output=True, timeout=15)
    assert cp.returncode == exit_code, cp.stdout + cp.stderr
    assert not data.exists()


@pytest.mark.parametrize("credential", [
    "", "postgresql://chudware_prod:fixture@localhost/chudware_prod",
    "postgresql://chudware_prod:fixture@localhost/chudware_staging",
    "postgresql://chudware_staging:fixture@localhost/chudware_staging?dbname=chudware_prod",
])
def test_staging_cannot_connect_with_production_or_implicit_database_credentials(monkeypatch, credential):
    from gateway import db
    monkeypatch.setenv("CHUDWARE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("CHUDWARE_DB_URL", credential)
    with pytest.raises(db.DatabaseUnavailable, match="staging"):
        db.dsn()
