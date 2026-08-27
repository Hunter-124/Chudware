"""Regression tests for the OMP vendoring mechanism (`scripts/vendor_omp.py`).

Focus: the invariants that are silent when broken. Re-vendoring is rare (it happens
when we pull upstream fixes), so a regression here would sit undetected until a
provider stopped working, and the symptom ("Cannot find module '@oh-my-pi/pi-ai'")
points nowhere near the vendoring step.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
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
    (root / "packages" / "catalog" / "src").mkdir(parents=True)
    (root / "packages" / "catalog" / "src" / "models.json").write_text(
        json.dumps(
            {
                "future-provider": {},
                "openai": {},
                "openrouter": {},
            }
        )
        + "\n"
    )
    (root / "packages" / "catalog" / "package.json").write_text(
        '{"name":"@oh-my-pi/pi-catalog"}\n'
    )
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

    vendor_omp.vendor(
        str(source), sha="deadbeef", packages=["ai", "catalog"], run_install=False
    )
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


def _generated_cpp_ids(repo_root: Path) -> tuple[str, ...]:
    path = repo_root / "core" / "agent" / "omp_bridge_provider_ids.inc"
    lines = path.read_text().splitlines()
    return tuple(json.loads(line.strip().removesuffix(",")) for line in lines[1:])


def test_revendor_generates_bridge_routes_from_catalog(vendored):
    repo_root = vendored.parent
    generated = runpy.run_path(
        str(repo_root / "services" / "agent" / "_omp_bridge_provider_ids.py")
    )["OMP_BRIDGE_PROVIDER_IDS"]
    expected = frozenset({"anthropic-sub", "future-provider", "openrouter"})

    assert generated == expected
    assert _generated_cpp_ids(repo_root) == tuple(sorted(expected))
    assert "openai" not in generated


def test_provider_route_order_is_deterministic():
    catalog = {
        "z-provider": {},
        "openai": {},
        "a-provider": {},
    }
    assert vendor_omp.omp_bridge_provider_ids(catalog) == (
        "a-provider",
        "anthropic-sub",
        "z-provider",
    )


def test_verify_accepts_generated_provider_routes(vendored):
    assert vendor_omp.verify() == 0


def test_verify_flags_missing_generated_python_routes(vendored, capsys):
    generated = vendored.parent / "services" / "agent" / "_omp_bridge_provider_ids.py"
    generated.unlink()

    assert vendor_omp.verify() == 1
    assert "missing generated provider routes" in capsys.readouterr().err


def test_verify_flags_changed_generated_cpp_routes(vendored, capsys):
    generated = vendored.parent / "core" / "agent" / "omp_bridge_provider_ids.inc"
    generated.write_text(generated.read_text() + '        "tampered",\n')

    assert vendor_omp.verify() == 1
    assert "changed generated provider routes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        [],
        {},
        {"": {}},
        {1: {}},
    ],
)
def test_provider_route_derivation_rejects_malformed_catalog(catalog):
    with pytest.raises(ValueError):
        vendor_omp.omp_bridge_provider_ids(catalog)


@pytest.mark.parametrize("catalog_contents", [None, "{not json"])
def test_vendor_fails_when_requested_catalog_is_missing_or_malformed(
    tmp_path, monkeypatch, catalog_contents
):
    source = _fake_upstream(tmp_path / "src")
    models = source / "packages" / "catalog" / "src" / "models.json"
    if catalog_contents is None:
        models.unlink()
    else:
        models.write_text(catalog_contents)
    vendor_dir = tmp_path / "vendor-omp"
    monkeypatch.setattr(vendor_omp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vendor_omp, "VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(vendor_omp, "LOCK_PATH", vendor_dir / "OMP_VENDOR.lock")

    with pytest.raises(SystemExit, match="cannot generate OMP bridge provider routes"):
        vendor_omp.vendor(
            str(source), sha="deadbeef", packages=["catalog"], run_install=False
        )


def test_vendor_without_catalog_package_skips_route_generation(tmp_path, monkeypatch):
    source = _fake_upstream(tmp_path / "src")
    vendor_dir = tmp_path / "vendor-omp"
    monkeypatch.setattr(vendor_omp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vendor_omp, "VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(vendor_omp, "LOCK_PATH", vendor_dir / "OMP_VENDOR.lock")

    vendor_omp.vendor(str(source), sha="deadbeef", packages=["ai"], run_install=False)

    py_path, cpp_path = vendor_omp._provider_route_output_paths()
    assert not py_path.exists()
    assert not cpp_path.exists()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@pytest.fixture
def vendored_dirty(tmp_path, monkeypatch):
    """Vendor from a REAL git checkout carrying local patches.

    Mirrors production: our fork's fixes live as uncommitted work over a pinned upstream
    sha, as one modified tracked file plus one untracked file.
    """
    source = _fake_upstream(tmp_path / "src")
    _git(source, "init", "-q", ".")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "upstream")
    # A local patch over a tracked file, and a brand-new local file.
    (source / "packages" / "ai" / "src" / "index.ts").write_text("export const x = 2;\n")
    (source / "packages" / "ai" / "src" / "extra.ts").write_text("export const y = 3;\n")

    vendor_dir = tmp_path / "vendor-omp"
    vendor_dir.mkdir()
    monkeypatch.setattr(vendor_omp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vendor_omp, "VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(vendor_omp, "LOCK_PATH", vendor_dir / "OMP_VENDOR.lock")
    vendor_omp.vendor(
        str(source), sha=None, packages=["ai", "catalog"], run_install=False
    )
    return vendor_dir


def test_local_patches_are_captured_into_the_repo(vendored_dirty):
    """The whole point: the patch series must survive losing the source checkout.

    Before this, the lock recorded only the NAMES of locally-modified files, so the
    vendored bytes were reproducible solely from a gitignored scratch clone's uncommitted
    working tree. One `git clean` there and the provider stack's local fixes could not be
    rebuilt, while `--verify` kept passing.
    """
    lock = json.loads((vendored_dirty / "OMP_VENDOR.lock").read_text())
    captured = lock["local_patches"]
    patches = sorted(k for k in captured if k != "README.md")
    assert len(patches) == 2, f"expected both local changes captured, got {patches}"
    assert "README.md" in captured, "the series must carry reapplication instructions"

    for name in captured:
        p = vendored_dirty / vendor_omp.LOCAL_PATCH_DIRNAME / name
        assert p.exists(), f"lock references {name} but the file is absent"
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest == captured[name], f"{name} hash does not match the lock"

    # The diffs must carry the actual local content, not just a filename.
    bodies = "\n".join(
        (vendored_dirty / vendor_omp.LOCAL_PATCH_DIRNAME / n).read_text() for n in patches
    )
    assert "export const x = 2;" in bodies, "modified tracked file's diff not captured"
    assert "export const y = 3;" in bodies, "untracked local file not captured"


def test_captured_patches_are_not_content_pinned_as_upstream(vendored_dirty):
    """`files` pins upstream bytes; the patch series is ours and is pinned separately."""
    lock = json.loads((vendored_dirty / "OMP_VENDOR.lock").read_text())
    assert not [
        rel for rel in lock["files"] if rel.startswith(vendor_omp.LOCAL_PATCH_DIRNAME + "/")
    ]
    assert lock["local_patches"], "but they must still be pinned under local_patches"


def test_verify_flags_a_deleted_local_patch(vendored_dirty, monkeypatch, capsys):
    """A vanished patch file is drift, not a clean tree.

    Neuter check: without the local_patches loop in verify(), deleting the only record of
    how to reproduce the vendored tree would still report "vendor OK".
    """
    assert vendor_omp.verify() == 0, "baseline must be clean before neutering it"
    lock = json.loads((vendored_dirty / "OMP_VENDOR.lock").read_text())
    victim = next(k for k in lock["local_patches"] if k != "README.md")
    (vendored_dirty / vendor_omp.LOCAL_PATCH_DIRNAME / victim).unlink()

    assert vendor_omp.verify() == 1, "deleting a captured patch must fail verification"
    assert "missing local patch" in capsys.readouterr().err


def test_verify_flags_an_edited_local_patch(vendored_dirty, capsys):
    """Silent edits to the series break reproducibility just as badly as deletion."""
    lock = json.loads((vendored_dirty / "OMP_VENDOR.lock").read_text())
    victim = next(k for k in lock["local_patches"] if k != "README.md")
    p = vendored_dirty / vendor_omp.LOCAL_PATCH_DIRNAME / victim
    p.write_text(p.read_text() + "\n# tampered\n")

    assert vendor_omp.verify() == 1
    assert "changed local patch" in capsys.readouterr().err
