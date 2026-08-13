from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parent


def test_environment_timers_are_daily_persistent_and_target_matching_services():
    prod = (DEPLOY / "chudware-security-retention.timer").read_text(encoding="utf-8")
    staging = (DEPLOY / "chudware-security-retention-staging.timer").read_text(encoding="utf-8")
    for timer in (prod, staging):
        assert "OnCalendar=*-*-* 04:15:00" in timer
        assert "Persistent=true" in timer
        assert "WantedBy=timers.target" in timer
    assert "Unit=chudware-security-retention.service" in prod
    assert "staging" not in prod
    assert "Unit=chudware-security-retention-staging.service" in staging


def test_retention_services_use_distinct_environment_and_database_contexts():
    prod = (DEPLOY / "chudware-security-retention.service").read_text(encoding="utf-8")
    staging = (DEPLOY / "chudware-security-retention-staging.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=%h/chudware-web-data/gateway.env" in prod
    assert "chudware-web-staging" not in prod
    assert "EnvironmentFile=%h/chudware-web-staging/gateway.env" in staging
    assert "Environment=CHUDWARE_WEB_DATA=%h/chudware-web-staging" in staging
    assert "Environment=CHUDWARE_WEB_DATA_ROOT=%h/chudware-web-staging" in staging
    assert "Environment=CHUDWARE_DEPLOY_ENV=staging" in staging
    assert "%h/Repositories/CAD/Webservice/gateway/.venv/bin/python" in prod
    assert "%h/Repositories/CAD/Webservice/deploy/security_retention.py" in staging


def test_setup_installs_and_enables_only_selected_environment_timer():
    setup = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
    assert 'RETENTION_TIMER="chudware-security-retention.timer"' in setup
    assert 'RETENTION_TIMER="chudware-security-retention-staging.timer"' in setup
    assert 'for u in "${GATEWAY_UNIT}"' in setup
    assert '"${RETENTION_UNIT}" "${RETENTION_TIMER}"' in setup
    assert "chudware-entitlement-staging.cred" in setup
    assert "encrypted entitlement credential is missing" in setup
    assert setup.index('install -m 644 "${src}" "${dst}"') < setup.index("systemctl --user daemon-reload")
    assert setup.index("systemctl --user daemon-reload") < setup.index('systemctl --user enable "${GATEWAY_UNIT}"')
    assert "disable --now chudware-security-retention.timer chudware-security-retention-staging.timer" in setup

def test_update_installs_current_units_before_reload_restart_and_retention_is_nonblocking():
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")
    migration = update.index("  run_migrations\n")
    install_units = update.index("  install_current_units\n", migration)
    enable = update.index("  enable_security_retention\n", install_units)
    restart = update.index("  systemctl --user restart", enable)
    function = update[update.index("install_current_units()") : update.index("enable_security_retention()")]
    assert 'install -m 644 "$REPO/Webservice/deploy/$RETENTION_UNIT"' in function
    assert 'install -m 644 "$REPO/Webservice/deploy/$RETENTION_TIMER"' in function
    assert function.index("install -m 644") < function.index("systemctl --user daemon-reload")
    assert migration < install_units < enable < restart
    retention = update[update.index("enable_security_retention()") : update.index("assert_entitlement_credential()")]
    assert 'enable --now "$RETENTION_UNIT" "$RETENTION_TIMER"' in retention
    assert "WARN:" in retention
    assert "exit 1" not in retention


def test_update_selects_staging_units_and_fails_closed_before_service_changes():
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")
    assert 'RETENTION_UNIT="chudware-security-retention.service"' in update
    assert 'RETENTION_UNIT="chudware-security-retention-staging.service"' in update
    assert 'RETENTION_TIMER="chudware-security-retention.timer"' in update
    assert 'RETENTION_TIMER="chudware-security-retention-staging.timer"' in update
    assert "disable --now chudware-security-retention.timer chudware-security-retention-staging.timer" in update
    assert "chudware-entitlement-staging.cred" in update
    credential = update.index("  assert_entitlement_credential\n")
    install_units = update.index("  install_current_units\n", credential)
    restart = update.index("  systemctl --user restart", install_units)
    assert credential < install_units < restart


def test_unit_installation_is_idempotent_copy_not_append():
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")
    function = update[update.index("install_current_units()") : update.index("enable_security_retention()")]
    assert function.count("install -m 644") == 3
    assert ">>" not in function
    assert "cp -n" not in function
