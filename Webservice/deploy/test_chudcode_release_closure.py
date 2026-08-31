from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEPLOY = Path(__file__).resolve().parent
NOTICE_NAMES = ("CHUDCODE-NOTICE", "CHUDCODE-THIRD-PARTY-NOTICES.txt")


def test_web_release_hashes_and_records_both_chudcode_notices() -> None:
    install = (DEPLOY / "install_webd.sh").read_text(encoding="utf-8")
    assert 'stage-notices \\\n    --output-dir "$STAGE_ROOT"' in install
    assert "schema=5" in install
    assert 'CHUD_NOTICE_SHA="$(sha256sum "$STAGE_ROOT/CHUDCODE-NOTICE"' in install
    assert 'CHUD_THIRD_PARTY_NOTICES_SHA="$(sha256sum "$STAGE_ROOT/CHUDCODE-THIRD-PARTY-NOTICES.txt"' in install
    assert 'chudcode_notice_sha256=$CHUD_NOTICE_SHA' in install
    assert 'chudcode_third_party_notices_sha256=$CHUD_THIRD_PARTY_NOTICES_SHA' in install
    joint = install[install.index('JOINT_SHA='):install.index('BUILT_AT_UTC=')]
    assert "$CHUD_NOTICE_SHA" in joint
    assert "$CHUD_THIRD_PARTY_NOTICES_SHA" in joint


def test_web_rollback_fails_closed_on_missing_or_tampered_notices() -> None:
    rollback = (DEPLOY / "rollback.sh").read_text(encoding="utf-8")
    for name in NOTICE_NAMES:
        assert name in rollback
    assert "EXPECTED_NOTICE_SHA" in rollback
    assert "EXPECTED_THIRD_PARTY_NOTICES_SHA" in rollback
    assert "Chudcode notice no longer matches its deploy manifest" in rollback
    assert "Chudcode third-party notices no longer match its deploy manifest" in rollback
    assert '--lock "$TARGET_DIR/CHUDCODE_ARTIFACT.lock" check-lock' in rollback


def test_desktop_packagers_stage_only_current_chudcode_notices() -> None:
    paths = (
        REPO / "packaging/linux/build_appimage.sh",
        REPO / "packaging/macos/build_app.sh",
        REPO / "packaging/windows/build_sidecars.ps1",
    )
    for path in paths:
        body = path.read_text(encoding="utf-8")
        assert "stage-notices" in body, path
        assert "OMP-NOTICE" not in body, path
    linux = paths[0].read_text(encoding="utf-8")
    assert linux.index('stage_chudcode "$APPDIR/usr/bin/chud"') < linux.rindex(
        'prepare_signing_metadata "$APPDIR"'
    )
    macos = paths[1].read_text(encoding="utf-8")
    assert macos.index('stage_chudcode "$MACOS/chud"') < macos.rindex(
        'prepare_signing_metadata "$APP"'
    )
