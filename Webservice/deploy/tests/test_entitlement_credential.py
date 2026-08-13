from pathlib import Path

import pytest


DEPLOY = Path(__file__).resolve().parents[1]
GATEWAY = DEPLOY.parent / "gateway"


@pytest.mark.parametrize(
    "name,encrypted",
    [
        ("chudware-gateway.service", "chudware-entitlement.cred"),
        ("chudware-gateway-staging.service", "chudware-entitlement-staging.cred"),
    ],
)
def test_gateway_units_use_private_systemd_credential(name: str, encrypted: str) -> None:
    unit = (DEPLOY / name).read_text(encoding="utf-8")
    assert f"LoadCredentialEncrypted=entitlement.key:%h/.config/credstore.encrypted/{encrypted}" in unit
    assert "Environment=CHUDWARE_ENTITLEMENT_KEY_PATH=%d/entitlement.key" in unit
    assert "Environment=CHUDWARE_ENTITLEMENT_KEY=" not in unit


def test_gateway_configuration_contains_no_raw_entitlement_secret_path() -> None:
    config = (GATEWAY / "config.example.env").read_text(encoding="utf-8")
    setup = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
    assert "CHUDWARE_ENTITLEMENT_KEY=" not in config
    env_skeleton = setup.split('cat >"${ENV_FILE}"', 1)[1].split("EOF", 1)[0]
    assert "CHUDWARE_ENTITLEMENT_KEY" not in env_skeleton


def test_provisioner_encrypts_exactly_32_bytes_without_persisting_plaintext() -> None:
    script = (DEPLOY / "provision_entitlement_credential.sh").read_text(encoding="utf-8")
    assert '"$(wc -c <"$tmp")" -eq 32' in script
    assert "systemd-creds" in script
    assert "--with-key=tpm2" in script
    assert "--with-key=host+tpm2-absent" in script
    assert "gateway.env" not in script


def test_update_and_hardening_fail_closed_on_missing_or_loose_credential() -> None:
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")
    harden = (DEPLOY / "harden_host.sh").read_text(encoding="utf-8")
    assert "assert_entitlement_credential" in update
    assert "encrypted entitlement credential is missing" in update
    assert '[[ "$mode" = 600 ]]' in update
    assert "the systemd unit owns it" in update
    assert "LoadCredentialEncrypted=entitlement.key:" in harden
    assert "CHUDWARE_ENTITLEMENT_KEY_PATH=%d/entitlement.key" in harden
