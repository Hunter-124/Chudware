"""Execute deployment operational helpers with inert systemd state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]


def test_staging_retention_leaves_production_timers_active(tmp_path):
    source = (DEPLOY / "update.sh").read_text()
    functions = []
    for name in ("enable_security_retention", "enable_release_retention"):
        start = source.index(f"{name}() {{")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    state = tmp_path / "state.json"
    production = {"chudware-security-retention.timer": True, "chudware-release-retention.timer": True}
    state.write_text(json.dumps(production))
    binary = tmp_path / "bin"
    binary.mkdir()
    systemctl = binary / "systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"state=Path({str(state)!r}); values=json.loads(state.read_text())\n"
        "args=[a for a in sys.argv[1:] if not a.startswith('--')]\n"
        "action=args[0]\n"
        "for unit in args[1:]: values[unit]=(action=='enable')\n"
        "state.write_text(json.dumps(values))\n"
    )
    systemctl.chmod(0o700)
    program = "\n".join(functions) + """
ENVIRONMENT=staging
RETENTION_UNIT=chudware-security-retention-staging.service
RETENTION_TIMER=chudware-security-retention-staging.timer
RELEASE_RETENTION_UNIT=chudware-release-retention-staging.service
RELEASE_RETENTION_TIMER=chudware-release-retention-staging.timer
enable_security_retention
enable_release_retention
"""
    result = subprocess.run(["bash", "-eu", "-c", program],
                            env={"PATH": f"{binary}:{os.defpath}", "HOME": str(tmp_path)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(state.read_text())
    assert all(values[unit] for unit in production)
    assert values["chudware-security-retention-staging.timer"]
    assert values["chudware-release-retention-staging.timer"]
