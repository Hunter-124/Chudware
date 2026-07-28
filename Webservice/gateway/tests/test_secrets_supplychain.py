"""Secrets & supply-chain regression tests.

Each test fails if the corresponding vulnerability is reintroduced:
real accounts back in the tracked users.yaml, unpinned requirements, the
cf_provision.py token file mode check removed, a personal --emails default
restored, or live secrets pasted into DEPLOYMENT.md.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY = REPO_ROOT / "Webservice" / "gateway"
DEPLOY = REPO_ROOT / "Webservice" / "deploy"
CF_PROVISION = DEPLOY / "cf_provision.py"


def _load_cf_provision():
    spec = importlib.util.spec_from_file_location("cf_provision", CF_PROVISION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_users_yaml_template_grants_nobody_real_access() -> None:
    """The tracked allowlist must be a safe template: no real emails, no admin."""
    data = yaml.safe_load((GATEWAY / "users.yaml").read_text())
    rows = data.get("users", []) if isinstance(data, dict) else []
    assert isinstance(rows, list)
    for row in rows:
        email = str(row.get("email", "")).lower()
        assert email.endswith("@example.com"), f"real email in tracked template: {email}"
        assert row.get("role") != "admin", "tracked template must not grant admin"


def test_requirements_fully_pinned() -> None:
    """Every direct dependency must be ==-pinned; >= permits CVE'd versions."""
    lines = [
        ln.split("#", 1)[0].strip()
        for ln in (GATEWAY / "requirements.txt").read_text().splitlines()
    ]
    deps = [ln for ln in lines if ln]
    assert deps, "requirements.txt is empty"
    for dep in deps:
        assert ">=" not in dep and "~=" not in dep, f"unpinned dependency: {dep}"
        assert "==" in dep, f"dependency not exactly pinned: {dep}"


def test_token_refuses_group_or_world_readable_info_file(tmp_path, monkeypatch) -> None:
    """cf_provision must refuse a chmod-644 Cloudflare-info.txt."""
    mod = _load_cf_provision()
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    fake_deploy = tmp_path / "deploy"
    fake_deploy.mkdir()
    monkeypatch.setattr(mod, "__file__", str(fake_deploy / "cf_provision.py"))
    info = tmp_path / "Cloudflare-info.txt"
    info.write_text("cfat_faketoken\n")
    info.chmod(0o644)
    with pytest.raises(SystemExit) as exc:
        mod.token()
    assert "chmod 600" in str(exc.value)


def test_token_reads_owner_only_info_file(tmp_path, monkeypatch) -> None:
    mod = _load_cf_provision()
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    fake_deploy = tmp_path / "deploy"
    fake_deploy.mkdir()
    monkeypatch.setattr(mod, "__file__", str(fake_deploy / "cf_provision.py"))
    info = tmp_path / "Cloudflare-info.txt"
    info.write_text("cfat_faketoken\n")
    info.chmod(0o600)
    assert mod.token() == "cfat_faketoken"


def test_emails_has_no_personal_default(monkeypatch) -> None:
    """--emails must be required; the hardcoded personal default must stay gone."""
    assert "haedmonds" not in CF_PROVISION.read_text(), "personal email default restored"
    mod = _load_cf_provision()
    monkeypatch.setattr(sys, "argv", ["cf_provision.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2  # argparse error: --emails required


def test_deployment_md_contains_no_live_secrets() -> None:
    text = (DEPLOY / "DEPLOYMENT.md").read_text()
    assert "cfat_" not in text
    assert "8f71299e969f2831f8ce9c6f159a19da" not in text  # R2 Access Key ID
    assert "673599c85567a3f265f84d6ed945b77cb5e6ccb5af3b015837582504914ed367" not in text


def test_gitignore_covers_secret_paths() -> None:
    def ignored(path: str) -> bool:
        r = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, check=False
        )
        return r.returncode == 0

    assert ignored("Webservice/Cloudflare-info.txt")
    assert ignored("Webservice/gateway/.env")
    assert ignored("Webservice/prod.env")
    assert not ignored("Webservice/gateway/config.example.env"), (
        "config.example.env must stay tracked"
    )
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "Webservice/gateway/users.yaml" in gitignore


def test_info_file_is_owner_only_in_worktree() -> None:
    """The live-credentials file on disk must already be chmod 600."""
    info = REPO_ROOT / "Webservice" / "Cloudflare-info.txt"
    if not info.exists():
        pytest.skip("Cloudflare-info.txt not present on this machine")
    assert stat.S_IMODE(info.stat().st_mode) & 0o077 == 0, (
        "Cloudflare-info.txt is group/world-readable"
    )
    assert "LIVE CREDENTIALS" in info.read_text()[:600], "warning header missing"


def test_gateway_config_still_loads_template(tmp_path, monkeypatch) -> None:
    """config.py seeds a missing runtime users file from the template — the seed
    must parse and grant nobody access (documented lockout, not a crash)."""
    monkeypatch.setenv("CHUDWARE_WEB_USERS_FILE", str(tmp_path / "rt" / "users.yaml"))
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(tmp_path / "data"))
    sys.path.insert(0, str(GATEWAY))
    try:
        import config as gw_config

        settings = gw_config.Settings()
        settings.ensure_data_dirs()
        seeded = yaml.safe_load(settings.users_file.read_text())
        rows = seeded.get("users", [])
        assert all(
            str(r.get("email", "")).endswith("@example.com") for r in rows
        ), "seeded runtime file grants real access"
    finally:
        sys.path.remove(str(GATEWAY))
        for mod in ("config",):
            sys.modules.pop(mod, None)
