"""Secrets & supply-chain regression tests.

Each test fails if the corresponding vulnerability is reintroduced:
a file-backed allowlist back beside the account store, unpinned requirements,
the cf_provision.py token file mode check removed, a personal operator email
hardcoded into cf_provision.py, or live secrets pasted into DEPLOYMENT.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import stat
import subprocess
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


def test_no_personal_operator_email_is_hardcoded() -> None:
    """cf_provision.py talks to a live Cloudflare account. A personal address
    baked into it once became the default Access allowlist; the /admin* Access
    application it fed is gone, but the file must still name nobody."""
    assert "haedmonds" not in CF_PROVISION.read_text(), "personal email default restored"


# SHA-256 digests of the R2 credential pair leaked in 84963165. The literals
# themselves must never be committed again, not even inside a negative test.
_LEAKED_SECRET_DIGESTS = frozenset({
    "225da8b6711121ba8763f28bf86be249d68f0f1f3fd27d9a122fcb7acbdb147b",
    "2b6cc2f2e5626110fa49f602d59058811ca6c1678e9ea381d0dbb1c454bac863",
})


def test_deployment_md_contains_no_live_secrets() -> None:
    text = (DEPLOY / "DEPLOYMENT.md").read_text()
    assert "cfat_" not in text
    for token in re.findall(r"\b[0-9a-f]{32,64}\b", text):
        assert hashlib.sha256(token.encode()).hexdigest() not in _LEAKED_SECRET_DIGESTS


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
