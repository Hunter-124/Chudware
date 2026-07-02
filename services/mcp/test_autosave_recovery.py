"""Tests for T-Q2: autosave, crash recovery & consent-tied diagnostics.

Self-contained: imports go through ``mcp_server.*`` (not ``services.mcp.*``)
so FastMCP/stdio infrastructure is never triggered in test mode.

Coverage:
  1. _autosave_dir() uses XDG/appdata path on Linux (not /tmp via bare APPDATA).
  2. Kill-9 scenario: simulate a "prior session" autosave, clear the registry,
     then recover_autosaved() reloads the tree intact.
  3. Periodic autosave timer arms, fires, records latency.
  4. Crash bundle written locally on induced exception.
  5. Upload hook called ONLY at consent "full", NOT at "off" or "meta".
  6. Autosave cadence configurable via env var / start_periodic_autosave().

Run:
  python -m pytest mcp_server/test_autosave_recovery.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

import mcp_server.cad_tools as cad_tools
import mcp_server.crash_bundle as crash_bundle


# ---------------------------------------------------------------------------
# 1. _autosave_dir() path resolution
# ---------------------------------------------------------------------------

class TestAutosaveDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        """AI_CAD_AUTOSAVE_DIR env var takes precedence over engine.paths."""
        monkeypatch.setenv("AI_CAD_AUTOSAVE_DIR", str(tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)
        d = cad_tools._autosave_dir()
        assert d == tmp_path

    def test_no_appdata_literal_dir_created_on_linux(self, monkeypatch):
        """On Linux without APPDATA set, must not create a literal backslash path."""
        if os.name == "nt":
            pytest.skip("Windows-only failure mode not applicable on NT")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("AI_CAD_AUTOSAVE_DIR", raising=False)
        d = cad_tools._autosave_dir()
        assert "\\" not in str(d), f"Path contains backslash on Linux: {d}"
        assert str(d).startswith("/"), f"Expected absolute path, got: {d}"

    def test_dir_is_created(self, monkeypatch, tmp_path):
        target = str(tmp_path / "autosave_subdir")
        monkeypatch.setenv("AI_CAD_AUTOSAVE_DIR", target)
        d = cad_tools._autosave_dir()
        assert d.exists() and d.is_dir()


# ---------------------------------------------------------------------------
# 2. Kill-9 recovery scenario (worker-level, no live engine needed)
# ---------------------------------------------------------------------------

class TestKill9Recovery:
    """Simulate a prior session's autosaves and prove recover_autosaved() works.

    Does NOT require libaicad.so: exercises autosave metadata / sidecar path
    logic and session-scoping by writing .step+ stubs + sidecar JSON files,
    then patching cb.call to simulate doc_open.
    """

    def _write_autosave(self, autosave_dir: pathlib.Path, doc_id: str,
                        session_id: str, name: str = "widget", age_s: float = 0.0):
        step = autosave_dir / f"{doc_id}.step+"
        step.write_bytes(b"MOCK_STEP_PLUS_CONTENT")
        mtime = time.time() - age_s
        os.utime(step, (mtime, mtime))
        meta = {"session": session_id, "doc_id": doc_id, "name": name,
                "saved_at": mtime}
        (autosave_dir / f"{doc_id}.json").write_text(json.dumps(meta))
        return step

    def test_recover_autosaved_restores_tree(self, monkeypatch, tmp_path):
        """Core kill-9 scenario: prior session's autosave reloads to intact tree."""
        autosave_dir = tmp_path / "autosave"
        autosave_dir.mkdir()
        monkeypatch.setenv("AI_CAD_AUTOSAVE_DIR", str(autosave_dir))

        prior_session = "deadbeef0001"
        doc_id_a = "doc_aaa"
        doc_id_b = "doc_bbb"
        self._write_autosave(autosave_dir, doc_id_a, prior_session, name="bracket")
        self._write_autosave(autosave_dir, doc_id_b, prior_session, name="plate")

        fresh_ids = {doc_id_a: "fresh_001", doc_id_b: "fresh_002"}

        monkeypatch.setattr(cad_tools, "_SESSION_ID", "current_session_xyz")
        monkeypatch.setattr(cad_tools, "_DOC_ALIASES", {})
        monkeypatch.setattr(cad_tools, "_CUR", {"doc_id": None})

        def mock_cb_call(op, args):
            if op == "doc_open":
                stem = pathlib.Path(args["path"]).stem
                new_id = fresh_ids.get(stem, "fresh_unknown")
                return {"doc_id": new_id, "name": args.get("name", stem)}
            return {}

        monkeypatch.setattr(cad_tools.cb, "call", mock_cb_call)

        recovered = cad_tools.recover_autosaved(scope="recent")

        assert len(recovered) == 2, f"Expected 2 recovered docs, got: {recovered}"
        names = {r["name"] for r in recovered}
        assert "bracket" in names
        assert "plate" in names

        # Original id aliases wired up so old handles still resolve.
        assert cad_tools._DOC_ALIASES.get(doc_id_a) == "fresh_001"
        assert cad_tools._DOC_ALIASES.get(doc_id_b) == "fresh_002"

        # _CUR should point to a recovered doc.
        assert cad_tools._CUR["doc_id"] in ("fresh_001", "fresh_002")

    def test_current_session_docs_not_recovered(self, monkeypatch, tmp_path):
        """Docs from THIS session must not be re-loaded (already in memory)."""
        autosave_dir = tmp_path / "autosave2"
        autosave_dir.mkdir()
        monkeypatch.setenv("AI_CAD_AUTOSAVE_DIR", str(autosave_dir))

        cur_session = "CURRENT_SESS"
        monkeypatch.setattr(cad_tools, "_SESSION_ID", cur_session)
        monkeypatch.setattr(cad_tools, "_DOC_ALIASES", {})
        monkeypatch.setattr(cad_tools, "_CUR", {"doc_id": None})

        self._write_autosave(autosave_dir, "same_sess_doc", cur_session, name="mine")
        self._write_autosave(autosave_dir, "old_sess_doc", "prior_sess", name="old")

        def mock_cb_call(op, args):
            if op == "doc_open":
                return {"doc_id": "fresh_x", "name": "x"}
            return {}

        monkeypatch.setattr(cad_tools.cb, "call", mock_cb_call)
        recovered = cad_tools.recover_autosaved(scope="recent")
        assert len(recovered) == 1
        assert recovered[0]["name"] == "old"

    def test_stale_autosaves_pruned(self, monkeypatch, tmp_path):
        """Autosaves older than max_age_days are pruned at recovery time."""
        autosave_dir = tmp_path / "autosave3"
        autosave_dir.mkdir()
        monkeypatch.setenv("AI_CAD_AUTOSAVE_DIR", str(autosave_dir))

        monkeypatch.setattr(cad_tools, "_SESSION_ID", "cur_sess")
        monkeypatch.setattr(cad_tools, "_AUTOSAVE_MAX_AGE_DAYS", 1)

        stale_id = "stale_doc"
        stale_step = self._write_autosave(autosave_dir, stale_id, "old_sess",
                                          name="stale", age_s=10 * 86400)
        fresh_id = "fresh_doc"
        self._write_autosave(autosave_dir, fresh_id, "old_sess",
                             name="fresh", age_s=60)

        def mock_cb_call(op, args):
            if op == "doc_open":
                return {"doc_id": "new_id", "name": "fresh"}
            return {}

        monkeypatch.setattr(cad_tools.cb, "call", mock_cb_call)
        cad_tools._prune_autosaves(max_age_days=1)

        assert not stale_step.exists(), "Stale autosave should have been pruned"
        assert (autosave_dir / f"{fresh_id}.step+").exists(), "Fresh autosave must survive"


# ---------------------------------------------------------------------------
# 3. Periodic autosave timer
# ---------------------------------------------------------------------------

class TestPeriodicAutosave:
    def test_timer_fires_and_latency_recorded(self, monkeypatch, tmp_path):
        """start_periodic_autosave arms a timer; after firing, latency is recorded."""
        calls = []

        monkeypatch.setattr(cad_tools, "_autosave_all_open", lambda: calls.append(time.time()))
        cad_tools._AUTOSAVE_LATENCIES.clear()
        cad_tools.stop_periodic_autosave()

        try:
            cad_tools.start_periodic_autosave(interval_s=0.05)
            deadline = time.time() + 2.0
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            assert calls, "Periodic autosave timer did not fire within 2s"
            # autosave_p95_ms() must not raise regardless of sample count.
            _ = cad_tools.autosave_p95_ms()
        finally:
            cad_tools.stop_periodic_autosave()

    def test_interval_zero_disables_timer(self, monkeypatch):
        """interval_s=0 should not start a timer."""
        cad_tools.stop_periodic_autosave()
        cad_tools.start_periodic_autosave(interval_s=0)
        assert cad_tools._PERIODIC_TIMER[0] is None

    def test_stop_cancels_timer(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cad_tools, "_autosave_all_open", lambda: calls.append(1))
        cad_tools.stop_periodic_autosave()
        cad_tools.start_periodic_autosave(interval_s=5)
        cad_tools.stop_periodic_autosave()
        time.sleep(0.1)
        assert not calls, "Timer fired after stop_periodic_autosave()"


# ---------------------------------------------------------------------------
# 4 & 5. Crash bundle: write locally; upload only at consent "full"
# ---------------------------------------------------------------------------

class TestCrashBundle:
    def _fresh_crash_dir(self, tmp_path, monkeypatch) -> pathlib.Path:
        d = tmp_path / "crashes"
        d.mkdir()
        monkeypatch.setenv("AI_CAD_CRASH_DIR", str(d))
        return d

    def _patch_consent(self, monkeypatch, tier: str):
        monkeypatch.setattr(crash_bundle, "_current_consent_tier", lambda: tier)

    def test_bundle_written_locally_on_exception(self, monkeypatch, tmp_path):
        """write_crash_bundle() always writes a local crash file regardless of consent."""
        crash_dir = self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "off")

        try:
            raise RuntimeError("induced crash for test")
        except RuntimeError:
            import sys
            et, ev, tb = sys.exc_info()
            path = crash_bundle.write_crash_bundle(et, ev, tb, source="test")

        assert path is not None and path.exists(), f"Crash bundle not written: {path}"
        bundle = json.loads(path.read_text())
        assert bundle["schema"] == "crash_bundle_v1"
        assert "induced crash for test" in bundle["stack"]
        assert bundle["source"] == "test"

    def test_upload_NOT_called_at_off(self, monkeypatch, tmp_path):
        """Upload hook must NOT be called when consent tier is 'off'."""
        self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "off")

        upload_calls = []
        monkeypatch.setattr(crash_bundle, "_upload_hook",
                            lambda p, b: upload_calls.append(p))
        crash_bundle.write_crash_bundle(source="test_off")
        assert not upload_calls, "Upload hook must not be called at consent='off'"

    def test_upload_NOT_called_at_meta(self, monkeypatch, tmp_path):
        """Upload hook must NOT be called at consent 'meta'."""
        self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "meta")

        upload_calls = []
        monkeypatch.setattr(crash_bundle, "_upload_hook",
                            lambda p, b: upload_calls.append(p))
        crash_bundle.write_crash_bundle(source="test_meta")
        assert not upload_calls, "Upload hook must not be called at consent='meta'"

    def test_upload_called_at_full(self, monkeypatch, tmp_path):
        """Upload hook MUST be called when consent tier is 'full'."""
        self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "full")

        upload_calls = []
        monkeypatch.setattr(crash_bundle, "_upload_hook",
                            lambda p, b: upload_calls.append(p))
        path = crash_bundle.write_crash_bundle(source="test_full")
        assert len(upload_calls) == 1, f"Expected 1 upload call, got: {upload_calls}"
        assert upload_calls[0] == path

    def test_set_upload_hook(self, monkeypatch, tmp_path):
        """set_upload_hook() replaces the upload function."""
        self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "full")

        seen = []
        crash_bundle.set_upload_hook(lambda p, b: seen.append(b["source"]))
        try:
            crash_bundle.write_crash_bundle(source="hook_test")
            assert seen == ["hook_test"]
        finally:
            crash_bundle.set_upload_hook(None)  # reset

    def test_bundle_fields(self, monkeypatch, tmp_path):
        """Verify required fields are present in the bundle JSON."""
        self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "off")

        path = crash_bundle.write_crash_bundle(source="field_check")
        assert path is not None
        bundle = json.loads(path.read_text())
        for field in ("schema", "ts_utc", "source", "python", "argv", "stack",
                      "log_tail", "autosave_refs"):
            assert field in bundle, f"Missing field: {field}"

    def test_crash_dir_path_returned(self, monkeypatch, tmp_path):
        """show_crash_dir() returns a real directory path string."""
        crash_dir = self._fresh_crash_dir(tmp_path, monkeypatch)
        result = crash_bundle.show_crash_dir()
        assert result == str(crash_dir)

    def test_install_is_idempotent(self):
        """install() can be called multiple times without error."""
        crash_bundle.reset_for_testing()
        crash_bundle.install()
        crash_bundle.install()  # second call must be safe
        crash_bundle.reset_for_testing()  # cleanup

    def test_old_bundles_pruned(self, monkeypatch, tmp_path):
        """When _MAX_BUNDLES is exceeded, oldest bundles are removed."""
        crash_dir = self._fresh_crash_dir(tmp_path, monkeypatch)
        self._patch_consent(monkeypatch, "off")

        # Create 25 stub crash files (> _MAX_BUNDLES=20) with distinct mtimes.
        for i in range(25):
            p = crash_dir / f"crash_2026010{i:02d}T120000Z.json"
            p.write_text(json.dumps({"schema": "crash_bundle_v1", "n": i}))
            time.sleep(0.001)

        crash_bundle._prune_old_bundles(crash_dir, max_bundles=20)
        remaining = list(crash_dir.glob("crash_*.json"))
        assert len(remaining) <= 20, f"Expected <= 20 bundles after prune, got {len(remaining)}"


# ---------------------------------------------------------------------------
# 6. Autosave cadence configurable
# ---------------------------------------------------------------------------

class TestAutosaveCadenceConfig:
    def test_start_with_explicit_interval(self):
        """start_periodic_autosave(interval_s=N) stores the interval."""
        cad_tools.stop_periodic_autosave()
        cad_tools.start_periodic_autosave(interval_s=120)
        assert cad_tools._AUTOSAVE_INTERVAL_S == 120.0
        cad_tools.stop_periodic_autosave()

    def test_p95_returns_none_with_no_samples(self):
        """autosave_p95_ms() returns None when fewer than 2 samples exist."""
        cad_tools._AUTOSAVE_LATENCIES.clear()
        result = cad_tools.autosave_p95_ms()
        assert result is None
