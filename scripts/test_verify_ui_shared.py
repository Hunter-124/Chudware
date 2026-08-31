"""Focused tests for scripts/verify-ui-shared.py (the chud-ui-shared-v1 contract).

Covers the acceptance points: the verifier passes from any CWD against the real
repo, and each kind of deliberate drift fails with exactly the failure that
names it — nothing collateral, nothing swallowed.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-ui-shared.py"

spec = importlib.util.spec_from_file_location("verify_ui_shared", SCRIPT)
vus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vus)


# ---- fixture: a minimal drift-able copy of the verified surfaces ---------------------------

@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """Copy exactly the files the verifier reads into an isolated root."""
    for rel in (
        "assets/ui-shared-manifest.json",
        "assets/fonts/OFL.txt",
        vus.THEME_HEADER,
        vus.KIT_HEADER,
        vus.ANIM_HEADER,
        vus.ICONS_HEADER,
        vus.ICON_ATLAS,
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dst)
    import json
    manifest = json.loads((REPO_ROOT / "assets/ui-shared-manifest.json").read_text())
    for font in manifest["fonts"]["files"]:
        shutil.copyfile(REPO_ROOT / vus.FONTS_DIR / font["file"],
                        tmp_path / vus.FONTS_DIR / font["file"])
    # Existence-only surfaces: content is product-owned, only presence is contractual.
    for rel in vus.ICON_TEXTURES + vus.ICON_GENERATORS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return tmp_path


# ---- green paths ---------------------------------------------------------------------------

def test_repo_has_no_drift():
    assert vus.collect_failures(REPO_ROOT) == []


def test_passes_from_any_cwd(tmp_path: Path):
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "UI shared contract verified: chud-ui-shared-v1" in result.stdout


def test_copied_tree_is_green(tree: Path):
    assert vus.collect_failures(tree) == []


# ---- deliberate drift fails exactly --------------------------------------------------------

def test_manifest_byte_drift_fails_byte_identity(tree: Path):
    manifest = tree / "assets/ui-shared-manifest.json"
    # Whitespace-only change: semantically identical JSON, still a byte-identity breach.
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("manifest.sha256")
    assert vus.EXPECTED_MANIFEST_SHA256 in failures[0]


def test_font_byte_drift_names_that_font_only(tree: Path):
    victim = tree / vus.FONTS_DIR / "Rubik-Regular.ttf"
    victim.write_bytes(victim.read_bytes() + b"\0")
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("fonts.Rubik-Regular.ttf.sha256")


def test_missing_font_fails(tree: Path):
    (tree / vus.FONTS_DIR / "JetBrainsMono-Medium.ttf").unlink()
    failures = vus.collect_failures(tree)
    assert failures == [f for f in failures if "JetBrainsMono-Medium.ttf" in f]
    assert len(failures) == 1


def test_symlinked_font_rejected(tree: Path):
    victim = tree / vus.FONTS_DIR / "Rubik-Bold.ttf"
    victim.unlink()
    victim.symlink_to(REPO_ROOT / vus.FONTS_DIR / "Rubik-Bold.ttf")
    failures = vus.collect_failures(tree)
    assert failures == ['fonts.Rubik-Bold.ttf.storage: expected "regular vendored file", '
                        'got "symbolic link"']


def test_threshold_drift_fails_exactly(tree: Path):
    header = tree / vus.THEME_HEADER
    header.write_text(header.read_text().replace(
        "kUsageCritFrac = 0.90f", "kUsageCritFrac = 0.85f"))
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("theme.kUsageCritFrac: expected 0.9, got 0.85")


def test_tone_enum_drift_fails(tree: Path):
    kit = tree / vus.KIT_HEADER
    text = kit.read_text()
    assert "Info," in text
    kit.write_text(text.replace("Info,", "", 1))
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("kit.Tone:")


def test_renamed_palette_role_fails(tree: Path):
    header = tree / vus.THEME_HEADER
    header.write_text(header.read_text().replace("float statusOk[4]", "float statusOkay[4]"))
    failures = vus.collect_failures(tree)
    assert failures == ['theme.Palette.statusOk: expected "float[4] Palette field", '
                        'got "missing"']


def test_animation_token_drift_fails(tree: Path):
    anim = tree / vus.ANIM_HEADER
    anim.write_text(anim.read_text().replace("kBreathe = 2.40f", "kBreathe = 3.00f"))
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("anim.kBreathe: expected 2.4, got 3")


def test_missing_ofl_license_fails(tree: Path):
    (tree / vus.FONT_LICENSE).unlink()
    failures = vus.collect_failures(tree)
    # One "missing file" failure plus one per license marker the empty read can't contain.
    assert any(f.startswith(f"{vus.FONT_LICENSE}:") for f in failures)
    for marker in vus.LICENSE_MARKERS:
        assert any(marker in f for f in failures)


def test_atlas_foreign_cell_size_fails(tree: Path):
    import json
    atlas_path = tree / vus.ICON_ATLAS
    atlas = json.loads(atlas_path.read_text())
    atlas["cell"]["rogue@48"] = {"x": 0, "y": 0, "w": 48, "h": 48}
    atlas_path.write_text(json.dumps(atlas))
    failures = vus.collect_failures(tree)
    assert len(failures) == 1
    assert failures[0].startswith("iconPipeline.atlas.cell.rogue@48")


def test_cli_reports_drift_and_exits_nonzero(tree: Path):
    manifest = tree / "assets/ui-shared-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tree)],
                            cwd=tree, capture_output=True, text=True)
    assert result.returncode == 1
    assert "UI shared drift: manifest.sha256" in result.stderr
