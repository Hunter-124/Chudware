"""Secrets & supply-chain regression tests.

Each test fails if the corresponding vulnerability is reintroduced:
a file-backed allowlist back beside the account store, unpinned requirements,
the cf_provision.py token file mode check removed, a personal --emails default
restored, or live secrets pasted into DEPLOYMENT.md.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY = REPO_ROOT / "Webservice" / "gateway"
DEPLOY = REPO_ROOT / "Webservice" / "deploy"
CF_PROVISION = DEPLOY / "cf_provision.py"


def _load_cf_provision():
    spec = importlib.util.spec_from_file_location("cf_provision", CF_PROVISION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_users_yaml_allowlist_remains() -> None:
    """Authorization lives in the account store. A users.yaml anywhere under
    the gateway would be a second, file-backed source of truth — exactly the
    thing self-serve auth replaced, and the one that used to ship real emails
    in the tree."""
    stragglers = sorted(p.name for p in GATEWAY.rglob("users.yaml"))
    assert stragglers == [], f"allowlist file(s) back on disk: {stragglers}"


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


def test_info_file_is_owner_only_in_worktree() -> None:
    """The live-credentials file on disk must already be chmod 600."""
    info = REPO_ROOT / "Webservice" / "Cloudflare-info.txt"
    if not info.exists():
        pytest.skip("Cloudflare-info.txt not present on this machine")
    assert stat.S_IMODE(info.stat().st_mode) & 0o077 == 0, (
        "Cloudflare-info.txt is group/world-readable"
    )
    assert "LIVE CREDENTIALS" in info.read_text()[:600], "warning header missing"
