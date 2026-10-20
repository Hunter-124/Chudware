"""The update.sh <-> deploy-prod.yml exit-code contract.

On 2026-08-31 a single dirty file in the canonical checkout on livingroom-pc made
``update.sh`` refuse before it had changed anything. It exited 1, which
deploy-prod.yml reads as "the deploy may have mutated production", so the workflow
ran ``rollback.sh`` against a production it had never touched. The rollback then
failed on its own unrelated grounds (the retained release had an empty
``bin/chud``), and every push for the next four days filed the same alarm:
"ROLLBACK ITSELF FAILED ... Production needs hands-on recovery now". Nothing was
wrong with production.

Exit 4 means "refused, production untouched". These tests execute the updater
and rollback against isolated filesystems rather than asserting workflow text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
UPDATE_SH = REPO / "Webservice" / "deploy" / "update.sh"


def test_a_precondition_refusal_exits_4(tmp_path):
    # Fully isolated: a private lock file and a gateway env that does not exist.
    # The refusal happens before the first mutation, so this runs no git write, no
    # build, and no systemctl.
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        CHUDWARE_DEPLOY_LOCK=str(tmp_path / "deploy.lock"),
        CHUDWARE_WEB_GATEWAY_ENV=str(tmp_path / "absent" / "gateway.env"),
    )
    proc = subprocess.run(
        ["bash", str(UPDATE_SH), "prod", "--continuous", "--no-pull"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=120,
    )
    assert proc.returncode == 4, (
        "a precondition refusal must exit 4, not "
        f"{proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _release(root: Path, name: str, *, torn: bool = False) -> Path:
    """A retained release tree that is structurally complete but not signed.

    Validation rejects it long before the manifest digests matter, which is
    exactly what these tests need: the point under test is WHICH candidate the
    selector reaches, not whether a real release verifies.
    """
    d = root / name
    (d / "bin").mkdir(parents=True)
    (d / "app").mkdir(parents=True)
    (d / "agent" / "prompts").mkdir(parents=True)
    for rel in (
        "bin/chudware-hostd",
        "bin/libchudware.so",
        "bin/chud",
        "CHUDCODE_ARTIFACT.lock",
        "CHUDCODE-NOTICE",
        "CHUDCODE-THIRD-PARTY-NOTICES.txt",
        "app/index.html",
        "app/Chudware.js",
        "app/Chudware.wasm",
    ):
        (d / rel).write_text("x")
    (d / "chudware-hostd.service").write_text("[Unit]\n")
    (d / "deploy-manifest").write_text("joint_sha256=deadbeef\n")
    if torn:
        # The shape that wedged production: the file is present but empty, so
        # `test -s` fails and this tree can never be restored.
        (d / "bin" / "chud").write_text("")
    return d


def _rollback(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "Webservice" / "deploy" / "rollback.sh"), *args],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            CHUDWARE_WEB_HOME=str(tmp_path / "chudware-web"),
        ),
        cwd=str(REPO),
        timeout=120,
    )


def test_default_payload_selection_walks_past_a_torn_release(tmp_path):
    releases = tmp_path / "chudware-web" / "releases"
    releases.mkdir(parents=True)
    _release(releases, "20260901T120000Z-live-1")
    _release(releases, "20260831T120000Z-torn-1", torn=True)
    _release(releases, "20260830T120000Z-good-1")
    (tmp_path / "chudware-web" / "current").symlink_to(
        releases / "20260901T120000Z-live-1"
    )

    proc = _rollback(tmp_path)
    out = proc.stdout + proc.stderr
    # The torn tree is reported and skipped; selection reaches the older one and
    # only then stops on that release's own (unsigned, synthetic) evidence.
    assert proc.returncode == 2, out
    assert "20260830T120000Z-good-1" in out, out


def test_an_operator_named_release_still_fails_loudly(tmp_path):
    releases = tmp_path / "chudware-web" / "releases"
    releases.mkdir(parents=True)
    _release(releases, "20260901T120000Z-live-1")
    _release(releases, "20260831T120000Z-torn-1", torn=True)
    _release(releases, "20260830T120000Z-good-1")
    (tmp_path / "chudware-web" / "current").symlink_to(
        releases / "20260901T120000Z-live-1"
    )

    proc = _rollback(tmp_path, "--to", "20260831T120000Z-torn-1")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    # It must NOT quietly restore a different release than the one requested.
    assert "20260830T120000Z-good-1" not in out, out


def test_no_restorable_candidate_is_exit_2_not_a_broken_rollback(tmp_path):
    releases = tmp_path / "chudware-web" / "releases"
    releases.mkdir(parents=True)
    _release(releases, "20260901T120000Z-live-1")
    _release(releases, "20260831T120000Z-torn-1", torn=True)
    (tmp_path / "chudware-web" / "current").symlink_to(
        releases / "20260901T120000Z-live-1"
    )

    proc = _rollback(tmp_path)
    out = proc.stdout + proc.stderr
    # Exit 2 is the workflow's "COULD NOT ROLL BACK: nothing to fall back to",
    # which is the truth here — not "ROLLBACK ITSELF FAILED".
    assert proc.returncode == 2, out


