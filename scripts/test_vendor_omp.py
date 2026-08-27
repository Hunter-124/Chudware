"""Regression tests for the OMP vendoring mechanism (`scripts/vendor_omp.py`).

Focus: the invariants that are silent when broken. Re-vendoring is rare (it happens
when we pull upstream fixes), so a regression here would sit undetected until a
provider stopped working, and the symptom ("Cannot find module '@oh-my-pi/pi-ai'")
points nowhere near the vendoring step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vendor_omp  # noqa: E402


def _fake_upstream(root: Path) -> Path:
    """A minimal tree that satisfies vendor_omp's structural expectations.

    Deliberately not a git checkout: `_resolve_source` falls back to the passed sha for
    a plain directory and `_local_modifications` reports nothing, which keeps the test
    hermetic and fast (no network, no git object churn).
    """
    (root / "packages" / "ai" / "src").mkdir(parents=True)
    (root / "packages" / "ai" / "src" / "index.ts").write_text("export const x = 1;\n")
    (root / "packages" / "ai" / "package.json").write_text('{"name":"@oh-my-pi/pi-ai"}\n')
    # A pruned dir must not survive vendoring.
    (root / "packages" / "ai" / "test").mkdir()
    (root / "packages" / "ai" / "test" / "spec.test.ts").write_text("// test\n")
    (root / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "bun@1.3.14",
                "workspaces": {"catalog": {"@oh-my-pi/pi-natives": "17.1.5"}},
            }
        )
        + "\n"
    )
    (root / "bunfig.toml").write_text("[install]\n")
    (root / "tsconfig.base.json").write_text("{}\n")
    (root / "tsconfig.json").write_text("{}\n")
    return root


@pytest.fixture
def vendored(tmp_path, monkeypatch):
    """Vendor a fake upstream into a temp vendor dir pre-seeded with our own artifacts."""
    source = _fake_upstream(tmp_path / "src")
    vendor_dir = tmp_path / "vendor-omp"

    # Pre-existing state that a real re-vendor must not destroy.
    (vendor_dir / "bridge" / "src").mkdir(parents=True)
    (vendor_dir / "bridge" / "src" / "bridge.ts").write_text("// OUR bridge\n")
    (vendor_dir / "bun.lock").write_text('{"lockfileVersion":1,"ours":true}\n')

    # vendor() reports its output path relative to REPO_ROOT, and _git_ignored resolves
    # vendor-relative paths against it, so all three constants must move together.
    monkeypatch.setattr(vendor_omp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vendor_omp, "VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(vendor_omp, "LOCK_PATH", vendor_dir / "OMP_VENDOR.lock")

    vendor_omp.vendor(str(source), sha="deadbeef", packages=["ai"], run_install=False)
    return vendor_dir


def test_revendor_preserves_bun_lock(vendored):
    """The C++ core materializes node_modules from this committed lockfile.

    `core/agent/omp_bridge_client.cpp` installs with --frozen-lockfile from
    vendor/omp/bun.lock so providers work with no manual `bun install`. Upstream's own
    lock describes a much larger workspace than our generated root package.json, so it
    cannot be copied from source — it has to survive the clobber. When it did not, every
    re-vendor silently deleted it and provider auth broke at the next clean build.
    """
    lock = vendored / "bun.lock"
    assert lock.is_file(), "re-vendoring deleted bun.lock; the bridge can no longer install"
    assert json.loads(lock.read_text())["ours"] is True, "bun.lock was overwritten, not preserved"


def test_revendor_preserves_our_bridge_package(vendored):
    """bridge/ is our code, not upstream's, and is the seam consuming OMP APIs."""
    bridge = vendored / "bridge" / "src" / "bridge.ts"
    assert bridge.is_file()
    assert bridge.read_text() == "// OUR bridge\n"


def test_preserved_artifacts_are_not_content_pinned(vendored):
    """The lock pins upstream bytes only.

    Pinning our own preserved files would make `--verify` fail the build the moment we
    edited the bridge or bun re-resolved the lockfile.
    """
    manifest = json.loads((vendored / "OMP_VENDOR.lock").read_text())["files"]
    assert "bun.lock" not in manifest
    assert not [rel for rel in manifest if rel.startswith("bridge/")]
    assert "packages/ai/src/index.ts" in manifest, "upstream sources must still be pinned"


def test_revendor_prunes_non_runtime_dirs(vendored):
    """Vendoring trims test dirs; if that stops working the vendored tree balloons."""
    assert not (vendored / "packages" / "ai" / "test").exists()
