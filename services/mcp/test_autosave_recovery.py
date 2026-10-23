"""Tests for autosave, crash recovery & consent-tied diagnostics.

Self-contained: imports go through ``services.mcp.*`` (not ``services.mcp.*``)
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
  python -m pytest services/mcp/test_autosave_recovery.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

import services.mcp.cad_common as cad_tools  # autosave internals moved
import services.mcp.crash_bundle as crash_bundle


@pytest.fixture(autouse=True)
def _legacy_engine_mode(monkeypatch):
    """This module tests the LEGACY in-process autosave/recovery path. Daemon
    mode is the default since the Phase-8 flip (hostd owns autosave), so opt
    out explicitly for every test here."""
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")


# ---------------------------------------------------------------------------
# 1. _autosave_dir() path resolution
# ---------------------------------------------------------------------------

class TestAutosaveDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        """CHUDWARE_AUTOSAVE_DIR env var takes precedence over services.paths."""
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)
        d = cad_tools._autosave_dir()
        assert d == tmp_path

    def test_no_appdata_literal_dir_created_on_linux(self, monkeypatch):
        """On Linux without APPDATA set, must not create a literal backslash path."""
        if os.name == "nt":
            pytest.skip("Windows-only failure mode not applicable on NT")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("CHUDWARE_AUTOSAVE_DIR", raising=False)
        d = cad_tools._autosave_dir()
        assert "\\" not in str(d), f"Path contains backslash on Linux: {d}"
        assert str(d).startswith("/"), f"Expected absolute path, got: {d}"

    def test_dir_is_created(self, monkeypatch, tmp_path):
        target = str(tmp_path / "autosave_subdir")
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", target)
        d = cad_tools._autosave_dir()
        assert d.exists() and d.is_dir()


# ---------------------------------------------------------------------------
# 2. Kill-9 recovery scenario (worker-level, no live engine needed)
# ---------------------------------------------------------------------------

class TestKill9Recovery:
    """Simulate a prior session's autosaves and prove recover_autosaved() works.

    Does NOT require libchudware.so: exercises autosave metadata / sidecar path
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
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(autosave_dir))

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
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(autosave_dir))

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
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(autosave_dir))

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
# 2b. Autosave DEBRIS: a big pile must not poison the rest of the turn, and
#     retention must not silently eat a part nobody saved.
#
# Measured on the real workstation pile (~/.local/share/Chudware/mcp_autosave,
# 2,000 snapshots / 512 sessions / 270 docs in the largest session) BEFORE the fix:
#   recover_autosaved(scope="recent") opened 270 docs, scope="all" opened 1,803;
#   the next doc-scoped call then either silently adopted whichever snapshot sorted
#   last, or refused with doc_ambiguous naming 108 (recent) / 619 (all) candidates.
# Everything below synthesises its own pile in tmp_path — the real data dir is
# never read or written by these tests.
# ---------------------------------------------------------------------------

def _write_snapshot(d, session, doc_id, name, saved_at):
    """One autosave snapshot + sidecar, mtime pinned to `saved_at`."""
    stem = f"{session}-{doc_id}"
    (d / f"{stem}.step+").write_bytes(b"MOCK")
    (d / f"{stem}.json").write_text(json.dumps(
        {"session": session, "doc_id": doc_id, "name": name, "saved_at": saved_at}))
    os.utime(d / f"{stem}.step+", (saved_at, saved_at))
    return d / f"{stem}.step+"


class TestAutosaveDebris:
    SESSIONS = 512          # distinct sessions on disk
    RECENT_DOCS = 270       # docs in the most-recent prior session
    NAMES = ["bracket", "plate", "AI part", "bracket", "housing"]

    def _pile(self, tmp_path, monkeypatch):
        """Synthesise the measured pile shape. Returns (dir, recent_session)."""
        d = tmp_path / "debris"
        d.mkdir()
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(d))
        monkeypatch.setattr(cad_tools, "_SESSION_ID", "cur000000000")
        monkeypatch.setattr(cad_tools, "_DOC_ALIASES", {})
        monkeypatch.setattr(cad_tools, "_CUR", {"doc_id": None})
        # Age-prune must not interfere: keep the whole pile inside the age window,
        # and disable the count cap so recovery sees every snapshot.
        monkeypatch.setattr(cad_tools, "_AUTOSAVE_MAX_AGE_DAYS", 3650)
        monkeypatch.setattr(cad_tools, "_AUTOSAVE_MAX_FILES", 0)
        now = time.time()
        for s in range(self.SESSIONS - 1):
            for i in range(3):
                _write_snapshot(d, f"{s:012x}", f"doc-cpp{i:08x}", self.NAMES[i % 5],
                                now - 86400 - s * 10 - i)
        recent = "ffffffffffff"
        for i in range(self.RECENT_DOCS):
            _write_snapshot(d, recent, f"doc-cpp{i:08x}", self.NAMES[i % 5],
                            now - 100 + i * 0.001)
        return d, recent

    @staticmethod
    def _fake_engine(monkeypatch):
        """A doc_open/doc_list engine stub. Returns the live-doc map it fills."""
        opened: dict = {}
        n = [0]

        def call(op, args=None):
            args = args or {}
            if op == "doc_open":
                n[0] += 1
                p = pathlib.Path(args["path"])
                meta = json.loads((p.parent / f"{p.name[:-6]}.json").read_text())
                nid = f"live-{n[0]:04d}"
                opened[nid] = {"doc_id": nid, "name": meta["name"]}
                return {"doc_id": nid, "name": meta["name"]}
            if op == "doc_list":
                return {"docs": list(opened.values())}
            return {}

        monkeypatch.setattr(cad_tools.cb, "call", call)
        return opened

    def test_debris_pile_does_not_make_next_call_ambiguous(self, monkeypatch, tmp_path):
        """1,803 snapshots on disk, and the following doc-scoped call still resolves.

        The number that matters is how many docs land in the engine: 270 (scope=recent)
        / 1,803 (scope=all) before, _RECOVER_MAX_DOCS after. _doc(None) must then return
        the NEWEST recovered snapshot's live id — a specific value, not "some doc"."""
        d, recent = self._pile(tmp_path, monkeypatch)
        opened = self._fake_engine(monkeypatch)
        total = len(list(d.glob("*.step+")))
        assert total == 1803, f"pile shape changed: {total}"

        for scope, on_disk in (("recent", self.RECENT_DOCS), ("all", total)):
            opened.clear()
            cad_tools._CUR["doc_id"] = None
            rec = cad_tools.recover_autosaved(scope=scope)
            cap = cad_tools._RECOVER_MAX_DOCS
            assert len(rec) == cap, f"scope={scope}: opened {len(rec)}, expected {cap}"
            assert len(opened) == cap, f"scope={scope}: engine holds {len(opened)} docs"
            assert cad_tools.recover_skipped() == on_disk - cap

            # The newest snapshot of the recent session is doc-cpp<269> -> "housing".
            newest = rec[0]
            assert newest["original_doc_id"] == f"doc-cpp{self.RECENT_DOCS - 1:08x}", \
                f"scope={scope}: newest recovered is {newest}"
            assert newest["name"] == "housing"
            # ...and an omitted doc_id resolves to exactly that doc, not an arbitrary one.
            assert cad_tools._doc(None) == newest["doc_id"]

    def test_recovered_candidates_named_in_refusal(self, monkeypatch, tmp_path):
        """An unresolvable handle after recovery lists the recovered docs by name/id."""
        self._pile(tmp_path, monkeypatch)
        self._fake_engine(monkeypatch)
        rec = cad_tools.recover_autosaved(scope="recent")
        with pytest.raises(cad_tools.DocRefError) as ei:
            cad_tools._doc("the part")
        err = ei.value
        assert err.detail["recovered_documents"], "refusal named no recovered candidate"
        assert len(err.detail["recovered_documents"]) == len(rec)
        assert err.detail["recovery_skipped"] == self.RECENT_DOCS - len(rec)
        assert rec[0]["doc_id"] in str(err), "newest recovered doc not offered in the message"
        assert "recover_parts" in str(err)


class TestAutosaveRetention:
    """The count cap evicts WHOLE sessions, so it can never half-restore a part.

    A snapshot IS potentially-unsaved user work (no dirty flag exists on disk), so the
    safety rule is structural rather than a guess: a session survives entirely or not at
    all, and this process's own session is never touched.
    """
    def _sessions(self, tmp_path, monkeypatch, n_sessions=3, per=3):
        d = tmp_path / "ret"
        d.mkdir()
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(d))
        monkeypatch.setattr(cad_tools, "_SESSION_ID", "cur000000000")
        now = time.time()
        for s in range(n_sessions):
            for i in range(per):
                # SAME doc_ids in every session: engine doc_ids reset per process, so
                # this is the real on-disk shape — and the trap the old per-doc_id
                # protection fell into.
                _write_snapshot(d, f"{s:012x}", f"doc-cpp{i:08x}", f"part-s{s}-d{i}",
                                now - (n_sessions - s) * 60 + i)
        return d

    def test_count_cap_evicts_whole_sessions_only(self, monkeypatch, tmp_path):
        """After a prune, no surviving session is missing any of its snapshots.

        The old cap protected "the newest snapshot of each doc_id" — but doc_ids repeat
        across sessions, so it kept 3 files from the newest session and one stray from an
        older one, leaving that older session half-recoverable. Now: 3 sessions x 3 docs,
        cap 4, keep_sessions 1 -> the two older sessions go whole and session 2 survives
        intact (3 files), which is ABOVE the cap on purpose.
        """
        d = self._sessions(tmp_path, monkeypatch, n_sessions=3, per=3)
        assert len(list(d.glob("*.step+"))) == 9
        cad_tools._prune_autosaves(max_age_days=3650, max_files=4, keep_sessions=1)

        by_session: dict = {}
        for p in d.glob("*.step+"):
            by_session.setdefault(cad_tools._autosave_session_key(p), []).append(p)
        assert set(by_session) == {f"{2:012x}"}, f"surviving sessions: {sorted(by_session)}"
        assert len(by_session[f"{2:012x}"]) == 3, "a surviving session lost snapshots"
        # Sidecars go with their snapshot; a snapshot with no sidecar recovers nameless.
        assert len(list(d.glob("*.json"))) == 3

    def test_retention_never_discards_unsaved_work(self, monkeypatch, tmp_path):
        """This process's own snapshots survive even when they are the OLDEST on disk.

        Our session's .step+ files ARE the live unsaved work of the running turn; there is
        no on-disk copy of it anywhere else. Making them the oldest snapshots isolates the
        own-session rule from the newest-N window (which would otherwise protect them
        incidentally), and cap=1 is the harshest cap there is: it goes unmet rather than
        touching them.
        """
        d = self._sessions(tmp_path, monkeypatch, n_sessions=2, per=2)
        old = time.time() - 100000  # older than every other session in the pile
        mine = [_write_snapshot(d, "cur000000000", f"doc-cpp{i:08x}", f"live-{i}", old + i)
                for i in range(3)]
        cad_tools._prune_autosaves(max_age_days=3650, max_files=1, keep_sessions=1)
        for p in mine:
            assert p.exists(), f"prune deleted live unsaved work: {p.name}"
        # keep_sessions=1 protects only the newest session (000000000001); the other
        # prior session goes WHOLE, and the cap (1) is left unmet at 5 surviving files.
        assert not list(d.glob(f"{0:012x}-*.step+")), "older session should have gone whole"
        assert len(list(d.glob(f"{1:012x}-*.step+"))) == 2, "newest session must stay intact"
        assert len(list(d.glob("*.step+"))) == 5

    def test_prune_plan_is_all_or_nothing_per_session(self, monkeypatch, tmp_path):
        """The planner itself never returns a partial session, at any cap."""
        d = self._sessions(tmp_path, monkeypatch, n_sessions=6, per=4)
        entries = [(p.stat().st_mtime, p) for p in d.glob("*.step+")]
        for cap in range(0, 25):
            doomed = set(cad_tools._autosave_prune_plan(
                entries, cap, keep_sessions=1, own_session="cur000000000"))
            per_session: dict = {}
            for _mt, p in entries:
                k = cad_tools._autosave_session_key(p)
                per_session.setdefault(k, []).append(p in doomed)
            for k, flags in per_session.items():
                assert len(set(flags)) == 1, \
                    f"cap={cap}: session {k} partially pruned ({flags})"



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
        monkeypatch.setenv("CHUDWARE_CRASH_DIR", str(d))
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
