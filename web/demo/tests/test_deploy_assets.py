"""The shipped deployment files hold their safety-relevant invariants."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
DEPLOY = DEMO / "deploy"


def test_example_env_has_no_secret_values_and_only_known_names():
    from demo.config import DemoConfig

    text = (DEMO / "config.example.env").read_text()
    for line in text.splitlines():
        stripped = line.lstrip("# ").strip()
        match = re.fullmatch(r"(CHUDWARE_DEMO_[A-Z_]+)=(.*)", stripped)
        if not line.startswith("#") and match:
            if match.group(1) in ("CHUDWARE_DEMO_OPENROUTER_KEY", "CHUDWARE_DEMO_NVIDIA_KEY",
                                  "CHUDWARE_DEMO_ANTHROPIC_KEY", "CHUDWARE_DEMO_COOKIE_KEY",
                                  "CHUDWARE_DEMO_ADMIN_KEY"):
                assert match.group(2) == "", line                  # secrets are never in the repository
    assert not re.search(r"sk-or-|sk-ant-|nvapi-|ghp_|BEGIN [A-Z ]*PRIVATE", text)
    # Every documented name is one the config actually reads (the keys are read verbatim).
    source = (DEMO / "config.py").read_text()
    for name in set(re.findall(r"CHUDWARE_DEMO_([A-Z_]+)=", text)):
        assert (re.search(rf"""["']{name}["']""", source)
                or f'"CHUDWARE_DEMO_{name}"' in source), name
    # The shipped defaults are what the code documents.
    cfg = DemoConfig.from_env({})
    assert cfg.ai_max_output_tokens == 16_384 and not cfg.nvidia_enabled


def test_user_unit_binds_loopback_only_and_reads_the_root_owned_env_file():
    unit = (DEPLOY / "chudware-demo.service").read_text()
    assert "--host 127.0.0.1" in unit and "--port 8300" in unit and "0.0.0.0" not in unit
    assert "--no-proxy-headers" in unit and "--factory" in unit
    assert "EnvironmentFile=/etc/chudware-demo/demo.env" in unit
    # newuidmap is setuid: NoNewPrivileges would break rootless podman.
    assert not re.search(r"^NoNewPrivileges=yes", unit, re.MULTILINE)
    assert "OPENROUTER" not in unit and "ANTHROPIC" not in unit and not re.search(r"sk-or-|sk-ant-", unit)


def test_egress_ruleset_blocks_lan_tailnet_and_link_local_for_the_demo_user_only():
    rules = (DEPLOY / "egress.nft").read_text()
    assert "table inet chudware_demo" in rules and "hook output" in rules
    assert 'meta skuid != "chudware-demo" return' in rules
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                "224.0.0.0/4", "fc00::/7", "fe80::/10"):
        assert net in rules, net
    assert rules.index("ip daddr 127.0.0.0/8 accept") < rules.index("192.168.0.0/16")
    assert rules.rstrip().splitlines()[-3].strip() == "accept"          # public destinations last
    assert "policy accept" in rules                                     # never touches other users
    unit = (DEPLOY / "chudware-demo-egress.service").read_text()
    assert "nft -f /etc/chudware-demo/egress.nft" in unit and "ExecStop" not in unit   # fail closed
    assert "user@__UID__.service" in unit


def test_installer_is_idempotent_by_construction_and_never_clobbers_the_env_file():
    script = (DEPLOY / "install.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in script
    assert '[[ ! -e $ENV_FILE ]]' in script                             # created only when absent
    assert "getent passwd" in script and "enable-linger" in script
    assert 'chmod 0600 "$ENV_FILE"' in script and 'chown "$DEMO_USER:$DEMO_USER" "$ENV_FILE"' in script
    assert "nologin" in script and "/etc/subuid" in script and "/etc/subgid" in script
    assert (script.index("systemctl restart chudware-demo-egress.service")
            < script.index("user_systemctl restart chudware-demo.service"))   # firewall first
    result = subprocess.run(["bash", "-n", str(DEPLOY / "install.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
