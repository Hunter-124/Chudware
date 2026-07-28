"""Tests for the weak-local -> live-fanout merge fix (Recall slice).

Proves the suppression bug fix in ``PartsHub.search``: previously ANY
non-empty local result short-circuited the whole call before the live vendor
fan-out ever ran, even when the local hit was junk (the proven
"stepper nema 17" -> 11 ball bearings, 0 web results case). ``search`` now
gates on ``PartsIndex.term_coverage`` (Contract, owned by the Ranking slice)
plus a local-count-vs-limit shortfall check, and merges local + live instead
of returning early.

Uses a fully stubbed ``PartsIndex`` double and a fake live provider — no real
corpus, no network, no dependency on the concurrently-edited index.py/query_norm.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.parts.hub import PartsHub  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeIndex:
    """Stand-in for PartsIndex: deterministic search() + term_coverage()."""

    def __init__(self, results: list[dict], coverage: tuple[int, int]) -> None:
        self._results = results
        self._coverage = coverage
        self.search_calls: list[str] = []

    def search(self, query, *, domain=None, limit=20):  # noqa: ARG002
        self.search_calls.append(query)
        return list(self._results[:limit])

    def term_coverage(self, query):  # noqa: ARG002
        return self._coverage

    def get(self, part_id):  # noqa: ARG002
        return None


class FakeLiveProvider:
    """A live-only CatalogProvider stub (never enumerable -> _is_live_only True)."""

    def __init__(self, site: str, hits: list[dict] | None = None,
                 domain: str = "catalog") -> None:
        self.site = site
        self.provider_id = f"fake_live_{site}"
        self.domain = domain
        self._hits = hits if hits is not None else []
        self.calls: list[str] = []

    def search(self, query, **kw):  # noqa: ARG002
        self.calls.append(query)
        return list(self._hits)

    def count(self):
        raise NotImplementedError

    def iter_records(self):
        raise NotImplementedError


def _make_hub(fake_index: FakeIndex, providers: list[object] = ()) -> PartsHub:
    hub = PartsHub()
    hub._index = fake_index
    hub._index_built = True
    # Skip real index build/revalidate machinery entirely — the fake index
    # needs none of it, and providers registered below must not trigger a
    # real rebuild attempt against adapters that don't exist here.
    hub._ensure_index = lambda: None  # type: ignore[method-assign]
    hub._maybe_revalidate = lambda: None  # type: ignore[method-assign]
    for p in providers:
        hub._registry.register(p)
    return hub


def _bearing_row(part_id="bearing:ISO 15:6203", designation="6203") -> dict:
    return {
        "part_id": part_id, "domain": "bearing", "title": "Deep-Groove Ball Bearing",
        "designation": designation, "standard": "ISO 15", "params": {"bore": 17.0},
        "geometry_ref": None, "sourcing": [], "provenance": "test", "license": "test",
    }


def _live_stepper_hit(site="automationdirect") -> dict:
    return {
        "part_id": f"catalog:{site}::nema17", "domain": "catalog",
        "title": "NEMA 17 Stepper Motor", "source": "live",
        "params": {}, "geometry_ref": {"kind": "import_file", "path": "x.step"},
        "sourcing": [{"unit_price": 12.5, "product_url": "https://x/nema17"}],
        "provenance": "test", "license": "test",
    }


# ---------------------------------------------------------------------------
# (a) weak local coverage triggers live fan-out; merged result has live rows
# ---------------------------------------------------------------------------

class TestWeakLocalTriggersLiveMerge:
    def test_partial_term_coverage_merges_live_results(self):
        """'stepper nema 17' -> junk bearing hits locally (matched only on the
        lone '17' token) but coverage=1/3. Live fan-out must run and its real
        results must land in the merged output."""
        junk_local = [_bearing_row(part_id=f"bearing:ISO 15:620{i}",
                                    designation=f"620{i}") for i in range(3)]
        index = FakeIndex(junk_local, coverage=(1, 3))
        live = FakeLiveProvider("automationdirect", hits=[_live_stepper_hit()])
        hub = _make_hub(index, providers=[live])

        out = hub.search("stepper nema 17", limit=20)

        assert live.calls == ["stepper nema 17"], "live provider was never invoked"
        pids = [r.get("part_id") for r in out]
        assert "catalog:automationdirect::nema17" in pids
        # Junk local hits are still present (merge, not replace) but no longer
        # exclusive of live results.
        assert any(pid.startswith("bearing:") for pid in pids)

    def test_no_local_results_triggers_live(self):
        index = FakeIndex([], coverage=(0, 2))
        live = FakeLiveProvider("mcmaster", hits=[_live_stepper_hit("mcmaster")])
        hub = _make_hub(index, providers=[live])

        out = hub.search("o-ring 2-014", limit=20)

        assert live.calls, "live provider must run when local has zero hits"
        assert any(r.get("part_id") == "catalog:mcmaster::nema17" for r in out)


# ---------------------------------------------------------------------------
# (b) full coverage + plentiful local results -> zero live calls
# ---------------------------------------------------------------------------

class TestStrongLocalSkipsLive:
    def test_full_coverage_plentiful_results_never_calls_live(self):
        plentiful = [_bearing_row(part_id=f"bearing:ISO 15:{i}", designation=str(i))
                     for i in range(20)]
        index = FakeIndex(plentiful, coverage=(2, 2))
        live = FakeLiveProvider("mcmaster")
        hub = _make_hub(index, providers=[live])

        out = hub.search("bearing 6203", limit=20)

        assert live.calls == [], f"live provider must not run on strong local: {live.calls}"
        assert len(out) == 20

    def test_full_coverage_sparse_results_does_not_probe_live(self):
        """Full term coverage with only 1 local hit is PRECISE, not weak.

        An exact designation ("608zz", "as568-014 viton") legitimately matches
        a handful of parts. Treating a small full-coverage answer as weak spent
        an entire vendor fan-out re-deriving the row already sitting at rank 1
        (measured: 147.8 ms -> 0.4 ms once the shortfall rule was confined to
        incomplete coverage), so the hit count alone must never trigger live.
        """
        index = FakeIndex([_bearing_row()], coverage=(2, 2))
        live = FakeLiveProvider("digikey", hits=[_live_stepper_hit("digikey")])
        hub = _make_hub(index, providers=[live])

        out = hub.search("bearing 6203", limit=20)

        assert not live.calls, "full-coverage local hits must not pay for a fan-out"
        assert [r.get("part_id") for r in out] == [_bearing_row()["part_id"]]


# ---------------------------------------------------------------------------
# (c) include_live=False -> zero provider calls even with weak coverage
# ---------------------------------------------------------------------------

class TestIncludeLiveFalseIsNetworkFree:
    def test_weak_coverage_include_live_false_makes_zero_calls(self):
        junk_local = [_bearing_row()]
        index = FakeIndex(junk_local, coverage=(1, 3))
        live = FakeLiveProvider("automationdirect", hits=[_live_stepper_hit()])
        hub = _make_hub(index, providers=[live])

        out = hub.search("stepper nema 17", limit=20, include_live=False)

        assert live.calls == [], "include_live=False must never touch a live provider"
        assert out == junk_local

    def test_empty_local_include_live_false_returns_empty(self):
        index = FakeIndex([], coverage=(0, 2))
        live = FakeLiveProvider("mcmaster", hits=[_live_stepper_hit()])
        hub = _make_hub(index, providers=[live])

        out = hub.search("o-ring 2-014", include_live=False)

        assert out == []
        assert live.calls == []


# ---------------------------------------------------------------------------
# (d) cross-boundary dedupe keeps the richer record
# ---------------------------------------------------------------------------

class TestCrossBoundaryDedupe:
    def test_richer_live_record_wins_over_sparse_local(self):
        sparse_local = {
            "part_id": "catalog:local::nema17", "domain": "catalog",
            "title": "NEMA 17 Stepper Motor", "designation": "NEMA17",
            "params": {}, "geometry_ref": None, "sourcing": [],
            "provenance": "test", "license": "test",
        }
        rich_live = {
            "part_id": "catalog:automationdirect::nema17", "domain": "catalog",
            "title": "NEMA 17 Stepper Motor", "designation": "NEMA17",
            "source": "live", "params": {},
            "geometry_ref": {"kind": "import_file", "path": "nema17.step"},
            "sourcing": [{"unit_price": 14.0, "product_url": "https://x/nema17",
                          "cad_format": "step"}],
            "provenance": "test", "license": "test",
        }
        index = FakeIndex([sparse_local], coverage=(1, 2))
        live = FakeLiveProvider("automationdirect", hits=[rich_live])
        hub = _make_hub(index, providers=[live])

        out = hub.search("nema17 stepper", limit=20)

        matches = [r for r in out if r.get("designation") == "NEMA17"]
        assert len(matches) == 1, f"expected exactly one deduped NEMA17 row, got {matches}"
        assert matches[0]["part_id"] == "catalog:automationdirect::nema17", \
            "the richer (CAD+price) live record must win over the sparse local one"

    def test_richer_local_wins_over_sparse_live(self):
        rich_local = {
            "part_id": "bearing:ISO 15:6203", "domain": "bearing",
            "title": "Deep-Groove Ball Bearing", "designation": "6203",
            "standard": "ISO 15", "params": {"bore": 17.0},
            "geometry_ref": {"kind": "core_family", "family": "bearing_dgbb"},
            "sourcing": [{"unit_price": 3.2, "product_url": "https://x/6203"}],
            "provenance": "test", "license": "test",
        }
        sparse_live = {
            "part_id": "catalog:mcmaster::6203", "domain": "bearing",
            "title": "Deep-Groove Ball Bearing", "designation": "6203",
            "source": "live", "params": {}, "geometry_ref": None, "sourcing": [],
            "provenance": "test", "license": "test",
        }
        index = FakeIndex([rich_local], coverage=(1, 2))
        live = FakeLiveProvider("mcmaster", hits=[sparse_live])
        hub = _make_hub(index, providers=[live])

        out = hub.search("bearing 6203 nema", limit=20)

        matches = [r for r in out if r.get("designation") == "6203"]
        assert len(matches) == 1
        assert matches[0]["part_id"] == "bearing:ISO 15:6203"


# ---------------------------------------------------------------------------
# (e) notice stubs survive the merge
# ---------------------------------------------------------------------------

class TestNoticeStubsSurviveMerge:
    def test_timeout_notice_appended_after_merge(self):
        # Give the live provider hits AND simulate a second, timed-out site by
        # directly checking _search_live's own notice-append path through the
        # public search() surface: monkeypatch _search_live to splice a notice
        # in, matching how the real fan-out appends timeout/login stubs.
        junk_local = [_bearing_row()]
        index = FakeIndex(junk_local, coverage=(1, 3))
        hub = _make_hub(index, providers=[])

        notice = {"site": "slowsite", "reason": "timeout", "hint": "exceeded budget"}
        real_hit = _live_stepper_hit()
        hub._search_live = lambda *a, **kw: [real_hit, notice]  # type: ignore[method-assign]

        out = hub.search("stepper nema 17", limit=20)

        assert notice in out, f"notice stub dropped from merged result: {out}"
        assert out[-1] == notice, "notices must stay appended at the end"
        assert any(r.get("part_id") == real_hit["part_id"] for r in out)

    def test_notice_only_live_result_still_appended_when_local_present(self):
        junk_local = [_bearing_row()]
        index = FakeIndex(junk_local, coverage=(0, 1))
        hub = _make_hub(index, providers=[])
        notice = {"site": "slowsite", "reason": "timeout", "hint": "exceeded budget"}
        hub._search_live = lambda *a, **kw: [notice]  # type: ignore[method-assign]

        out = hub.search("zzz_unmatched_query", limit=20)

        assert notice in out
        assert out[-1] == notice
        assert any(r["part_id"] == junk_local[0]["part_id"] for r in out)
