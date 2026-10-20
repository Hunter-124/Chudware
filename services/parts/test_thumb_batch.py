"""Tests for the batch thumbnail RPC (``parts_thumbnails``) and the shared single-entry
preview path in :mod:`services.parts.gui_rpc`.

The GUI used to issue one ``parts_thumbnail`` per card down a single FIFO dispatch
thread, so a grid of uncached products filled in one image at a time. The batch method
fans the genuine misses out across a per-call pool while every disk-cache hit is
answered synchronously up front. What must hold, and is guarded here:

  * one output entry per input, in input order, whatever happens to any single item;
  * a cached batch NEVER touches the fetcher (a warm grid is a millisecond operation);
  * the 32-entry cap silently truncates instead of erroring;
  * the wall budget is honoured even when a fetcher hangs — the call cannot hang with it;
  * a per-item failure leaves the envelope ok:true;
  * a definitive miss leaves a negative marker so the next call short-circuits;
  * singular and plural give the same answer for the same input (they share one impl).

Every test redirects the on-disk thumb cache into tmp_path with the same
``_thumb_cache_dir`` monkeypatch the existing thumbnail tests use
(test_parts_worker_rpc.py::TestPartsThumbnail), so a stale shared cache can neither mask
a break nor leak state between tests. No test here reaches the network or the native
core: ``_fetch_image_bytes`` and ``_render_local_part_png`` are always stubbed.
"""
from __future__ import annotations

import base64
import threading
import time

import pytest

def _png(color=0):
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color]))) + chunk(b"IEND", b""))


PNG = _png()


def _noop_emit(_):
    pass


@pytest.fixture
def gr(tmp_path, monkeypatch):
    """gui_rpc with an isolated cache dir and both slow paths (network fetch, engine
    render) stubbed out to a definitive failure. Individual tests re-stub what they need."""
    from services.parts import gui_rpc

    monkeypatch.setattr(gui_rpc, "_thumb_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(gui_rpc, "_fetch_image_bytes", lambda url, pid: None)
    monkeypatch.setattr(gui_rpc, "_render_local_part_png", lambda pid, png: None)
    # A fresh trim window per test: the sweep is a module global and a neighbouring test
    # must not be able to suppress (or trigger) it here.
    monkeypatch.setattr(gui_rpc, "_thumb_last_trim", 0.0, raising=False)
    return gui_rpc


@pytest.fixture
def raw(tmp_path, monkeypatch):
    """gui_rpc with ONLY the cache dir redirected — the REAL ``_fetch_image_bytes`` is
    left in place. Anything asserting on the fetcher's own behaviour must use this, not
    ``gr``: ``gr`` replaces that function outright, so a test built on it would be
    asserting against the stub."""
    from services.parts import gui_rpc

    monkeypatch.setattr(gui_rpc, "_thumb_cache_dir", lambda: tmp_path)
    return gui_rpc


def _batch(gr, items):
    return gr._parts_thumbnails({"items": items}, _noop_emit)


def _item(pid, url="https://cdn.example.com/i.png"):
    return {"item_id": pid, "image_url": url}


class TestRegistration:
    def test_both_methods_registered(self, gr):
        for name in ("parts_thumbnail", "parts_thumbnails"):
            assert name in gr.GUI_WORKER_METHODS, f"{name} missing from the parts dispatch table"
            assert callable(gr.GUI_WORKER_METHODS[name])


class TestBatchShape:
    def test_one_entry_per_input_in_order(self, gr, monkeypatch):
        monkeypatch.setattr(gr, "_fetch_image_bytes", lambda url, pid: PNG)
        ids = [f"catalog:mcmaster::p{n}" for n in range(5)]
        r = _batch(gr, [_item(i) for i in ids])
        assert r["ok"] is True
        assert [e["item_id"] for e in r["images"]] == ids
        assert all(e["ok"] is True for e in r["images"])

    def test_every_entry_carries_the_full_contract_keys(self, gr):
        r = _batch(gr, [_item("catalog:x::p1")])
        e = r["images"][0]
        assert set(("item_id", "ok", "image_b64", "reason")) <= set(e)
        assert isinstance(e["ok"], bool)

    def test_empty_batch_is_ok_with_no_images(self, gr):
        assert _batch(gr, []) == {"ok": True, "images": []}

    def test_blank_item_id_is_a_per_entry_error(self, gr):
        r = _batch(gr, [_item(""), _item("catalog:x::p1")])
        assert r["ok"] is True
        assert r["images"][0]["ok"] is False
        assert "item_id" in r["images"][0]["reason"]

    def test_non_list_items_is_a_caller_error(self, gr):
        assert gr._parts_thumbnails({"items": "nope"}, _noop_emit)["ok"] is False


class TestCap:
    def test_batch_is_capped_at_32_entries(self, gr, monkeypatch):
        calls = []
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: calls.append(pid) or PNG)
        r = _batch(gr, [_item(f"catalog:x::p{n}") for n in range(50)])
        assert r["ok"] is True, "an over-long batch must truncate, never error"
        assert len(r["images"]) == 32
        assert len(calls) == 32, "the excess must be ignored, not fetched"
        assert r["images"][-1]["item_id"] == "catalog:x::p31"


class TestCacheHitsAreFree:
    def test_cached_entries_never_touch_the_fetcher(self, gr, tmp_path, monkeypatch):
        ids = [f"catalog:x::p{n}" for n in range(6)]
        for pid in ids:
            gr._thumb_cache_paths(pid)[0].write_bytes(PNG)
        calls = []
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: calls.append(pid) or PNG)
        monkeypatch.setattr(gr, "_render_local_part_png",
                            lambda pid, png: pytest.fail("cache hit reached the renderer"))
        t0 = time.monotonic()
        r = _batch(gr, [_item(i) for i in ids])
        assert calls == [], "a warm batch must resolve from disk alone"
        assert all(e["ok"] and base64.b64decode(e["image_b64"]) == PNG for e in r["images"])
        assert time.monotonic() - t0 < 1.0, "a fully cached batch must be ~instant"

    def test_mostly_cached_batch_only_fetches_the_misses(self, gr, monkeypatch):
        warm = [f"catalog:x::warm{n}" for n in range(8)]
        cold = ["catalog:x::cold"]
        for pid in warm:
            gr._thumb_cache_paths(pid)[0].write_bytes(PNG)
        calls = []
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: calls.append(pid) or PNG)
        r = _batch(gr, [_item(i) for i in warm + cold])
        assert calls == cold
        assert all(e["ok"] for e in r["images"])


class TestBudget:
    def test_slow_fetcher_cannot_outlast_the_wall_budget(self, gr, monkeypatch):
        monkeypatch.setenv("CHUDWARE_THUMB_BATCH_BUDGET_S", "0.4")
        release = threading.Event()

        def _hang(url, pid):
            release.wait(20.0)      # released by the test below; never a real 20 s hang
            return PNG

        monkeypatch.setattr(gr, "_fetch_image_bytes", _hang)
        try:
            t0 = time.monotonic()
            r = _batch(gr, [_item(f"catalog:x::p{n}") for n in range(4)])
            elapsed = time.monotonic() - t0
        finally:
            release.set()           # let the abandoned stragglers exit promptly
        assert elapsed < 5.0, f"batch blew its 0.4 s budget: {elapsed:.2f}s"
        assert r["ok"] is True, "a timeout is a per-entry outcome, not an envelope failure"
        assert len(r["images"]) == 4
        assert all(e["ok"] is False and e["reason"] == "timeout" for e in r["images"])

    def test_timeout_leaves_no_negative_marker(self, gr, tmp_path, monkeypatch):
        """A timeout says nothing about the part — caching it as a miss would blind the GUI
        to that image for a whole day. Snapshot the dir the instant the batch returns: the
        abandoned straggler is free to cache its own (definitive) answer later."""
        monkeypatch.setenv("CHUDWARE_THUMB_BATCH_BUDGET_S", "0.3")
        release = threading.Event()
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: (release.wait(20.0), None)[1])
        try:
            r = _batch(gr, [_item("catalog:x::slow")])
            markers = list(tmp_path.glob("*.miss"))
        finally:
            release.set()
        assert r["images"][0]["reason"] == "timeout"
        assert not markers, "a timed-out entry must not be cached as a miss"


class TestPerEntryFailure:
    def test_one_raising_entry_does_not_fail_the_envelope(self, gr, monkeypatch):
        def _boom(url, pid):
            if pid.endswith("bad"):
                raise RuntimeError("CDN exploded")
            return PNG

        monkeypatch.setattr(gr, "_fetch_image_bytes", _boom)
        r = _batch(gr, [_item("catalog:x::good"), _item("catalog:x::bad"),
                        _item("catalog:x::good2")])
        assert r["ok"] is True
        oks = [e["ok"] for e in r["images"]]
        assert oks == [True, False, True]
        assert r["images"][1]["reason"], "a failed entry must explain itself"

    def test_unfetchable_entry_reports_no_preview(self, gr):
        r = _batch(gr, [_item("catalog:x::nothing")])
        assert r["images"][0]["ok"] is False
        assert "no preview" in r["images"][0]["reason"]


class TestNegativeCache:
    def test_marker_short_circuits_the_second_call(self, gr, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(gr, "_fetch_image_bytes", lambda url, pid: calls.append(pid))
        pid = "catalog:x::ghost"
        first = _batch(gr, [_item(pid)])
        assert first["images"][0]["ok"] is False
        assert len(calls) == 1
        assert list(tmp_path.glob("*.miss")), "a definitive miss must leave a marker"

        second = _batch(gr, [_item(pid)])
        assert len(calls) == 1, "the negative cache did not short-circuit the retry"
        assert second["images"][0]["ok"] is False
        assert "cached" in second["images"][0]["reason"]

    def test_expired_marker_lets_the_part_be_retried(self, gr, monkeypatch):
        calls = []
        monkeypatch.setattr(gr, "_fetch_image_bytes", lambda url, pid: calls.append(pid))
        pid = "catalog:x::stale"
        _batch(gr, [_item(pid)])
        monkeypatch.setenv("CHUDWARE_THUMB_MISS_TTL_S", "1")
        miss = gr._thumb_cache_paths(pid)[1]
        import os as _os
        old = time.time() - 60
        _os.utime(miss, (old, old))
        _batch(gr, [_item(pid)])
        assert len(calls) == 2, "an expired marker must not suppress the retry forever"
        assert not miss.exists() or miss.stat().st_mtime > old

    def test_a_successful_fetch_clears_a_stale_marker(self, gr, monkeypatch):
        pid = "catalog:x::recovers"
        _batch(gr, [_item(pid)])                       # miss → marker
        miss = gr._thumb_cache_paths(pid)[1]
        assert miss.exists()
        monkeypatch.setenv("CHUDWARE_THUMB_MISS_TTL_S", "1")
        import os as _os
        _os.utime(miss, (time.time() - 60,) * 2)
        monkeypatch.setattr(gr, "_fetch_image_bytes", lambda url, pid_: PNG)
        r = _batch(gr, [_item(pid)])
        assert r["images"][0]["ok"] is True
        assert not miss.exists(), "the marker must be cleared once the image lands"


class TestSingularPluralAgree:
    def test_same_input_same_answer_on_success(self, gr, monkeypatch):
        monkeypatch.setattr(gr, "_fetch_image_bytes", lambda url, pid: PNG)
        pid, url = "catalog:x::same", "https://cdn.example.com/a.png"
        one = gr._parts_thumbnail({"item_id": pid, "image_url": url}, _noop_emit)
        many = _batch(gr, [{"item_id": pid, "image_url": url}])["images"][0]
        assert one["ok"] is True and many["ok"] is True
        assert one["image_b64"] == many["image_b64"]

    def test_same_input_same_answer_on_failure(self, gr):
        pid = "catalog:x::hopeless"
        one = gr._parts_thumbnail({"item_id": pid, "image_url": ""}, _noop_emit)
        many = _batch(gr, [{"item_id": pid, "image_url": ""}])["images"][0]
        assert one["ok"] is False and many["ok"] is False
        # First call marked the miss, so the second sees the cached form of the same answer.
        assert "no preview" in one["reason"] and "no preview" in many["reason"]

    def test_singular_still_rejects_a_missing_item_id(self, gr):
        r = gr._parts_thumbnail({"item_id": ""}, _noop_emit)
        assert r["ok"] is False and "item_id" in r["reason"]

    def test_singular_serves_the_disk_cache(self, gr, monkeypatch):
        pid = "catalog:x::warm"
        gr._thumb_cache_paths(pid)[0].write_bytes(PNG)
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, p: pytest.fail("cache hit reached the fetcher"))
        r = gr._parts_thumbnail({"item_id": pid, "image_url": "https://x/y.png"}, _noop_emit)
        assert base64.b64decode(r["image_b64"]) == PNG


class TestRenderSerialisation:
    def test_renders_never_overlap_while_fetches_do(self, gr, monkeypatch):
        """The native core is driven as a four-call sequence per render and is unlocked in
        the in-process mode — two renders must never be in flight at once. Network fetches
        share nothing and must stay parallel."""
        monkeypatch.setattr(gr, "_resolve_core_family", lambda pid: ("shcs", "M5"))
        live = {"render": 0, "render_peak": 0, "fetch": 0, "fetch_peak": 0}
        lk = threading.Lock()

        def _track(kind, hold):
            with lk:
                live[kind] += 1
                live[kind + "_peak"] = max(live[kind + "_peak"], live[kind])
            time.sleep(hold)
            with lk:
                live[kind] -= 1

        monkeypatch.setattr(gr, "_render_local_part_png",
                            lambda pid, png: (_track("render", 0.05), None)[1])
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: (_track("fetch", 0.05), PNG)[1])

        items = [_item(f"local:r{n}", "") for n in range(4)] + \
                [_item(f"catalog:x::f{n}") for n in range(4)]
        r = _batch(gr, items)
        assert r["ok"] is True
        assert live["render_peak"] == 1, \
            f"{live['render_peak']} concurrent renders — the core is not thread-safe"
        assert live["fetch_peak"] > 1, "network fetches were needlessly serialised"

    def test_fanout_is_actually_concurrent(self, gr, monkeypatch):
        monkeypatch.setattr(gr, "_fetch_image_bytes",
                            lambda url, pid: (time.sleep(0.15), PNG)[1])
        t0 = time.monotonic()
        r = _batch(gr, [_item(f"catalog:x::p{n}") for n in range(8)])
        elapsed = time.monotonic() - t0
        assert all(e["ok"] for e in r["images"])
        assert elapsed < 0.15 * 8 * 0.6, \
            f"8 x 150ms fetches took {elapsed:.2f}s — the fan-out is serial"


class TestFetchTuning:
    """These exercise the REAL ``_fetch_image_bytes`` (fixture ``raw``, not ``gr``)."""

    @staticmethod
    def _recording_cef(monkeypatch):
        """Install a CEF client that records the urls it is asked to download."""
        seen: list[str] = []

        class _Client:
            def download(self, url, dest, profile=""):
                seen.append(url)
                return {"ok": False}

        monkeypatch.setattr("services.browser.cef_client.client", lambda: _Client())
        return seen

    @staticmethod
    def _route(monkeypatch, table):
        """Serve bounded public HTTP response envelopes without network access."""
        from services.net import safe_http
        asked: list[str] = []

        def _get(url, **kwargs):
            asked.append(url)
            status, body = table.get(url, (599, b""))
            return safe_http.Response(status, {}, url, body)

        monkeypatch.setattr(safe_http, "get", _get)
        return asked


    def test_fetch_timeout_knob_is_applied(self, raw, monkeypatch):
        monkeypatch.setenv("CHUDWARE_THUMB_FETCH_TIMEOUT_S", "1.5")
        assert raw._thumb_fetch_timeout_s() == pytest.approx(1.5)
        monkeypatch.delenv("CHUDWARE_THUMB_FETCH_TIMEOUT_S")
        assert raw._thumb_fetch_timeout_s() == pytest.approx(4.0), \
            "the default must stay at 4 s, not the old 8 s"

    def test_public_client_is_called_with_the_tuned_idle_timeout(self, raw, monkeypatch):
        from services.net import safe_http
        monkeypatch.setenv("CHUDWARE_THUMB_FETCH_TIMEOUT_S", "2.25")
        seen: list[float] = []

        def _capture(url, **kwargs):
            seen.append(kwargs["idle_timeout"])
            raise OSError("connection refused")

        monkeypatch.setattr(safe_http, "get", _capture)
        self._recording_cef(monkeypatch)
        raw._fetch_image_bytes("https://cdn.example.com/a.png", "catalog:x::p")
        assert seen == [pytest.approx(2.25)]

    def test_worker_count_is_clamped(self, raw, monkeypatch):
        monkeypatch.setenv("CHUDWARE_THUMB_WORKERS", "999")
        assert raw._thumb_workers() == 16
        monkeypatch.setenv("CHUDWARE_THUMB_WORKERS", "0")
        assert raw._thumb_workers() == 8, "garbage must fall back to the default, not 0 threads"
        monkeypatch.setenv("CHUDWARE_THUMB_WORKERS", "nonsense")
        assert raw._thumb_workers() == 8

    def test_404_with_no_image_body_skips_the_cef_fallback(self, raw, monkeypatch):
        """A missing image will not appear via CEF either — burning a browser download
        (and several seconds of the GUI's patience) on it is pure latency."""
        url = "https://cdn.example.com/gone.png"
        self._route(monkeypatch, {url: (404, b"<html>Not Found</html>")})
        seen = self._recording_cef(monkeypatch)
        assert raw._fetch_image_bytes(url, "catalog:x::p") is None
        assert seen == [], "a bodyless/HTML 404 must not fall through to the CEF download"

    def test_404_with_a_png_body_is_a_success(self, raw, monkeypatch):
        """MEASURED: Google's s2 favicon service answers 404 for domains it has no icon
        for and still ships a valid 726-byte globe PNG. Discarding that body is what left
        thk.com / 3dcontentcentral.com / traceparts.com on lettered tiles."""
        url = "https://cdn.example.com/odd.png"
        self._route(monkeypatch, {url: (404, PNG)})
        seen = self._recording_cef(monkeypatch)
        assert raw._fetch_image_bytes(url, "catalog:x::p") == PNG
        assert seen == [], "we already had the bytes — CEF must not run"

    def test_404_with_an_html_body_is_not_an_image(self, raw, monkeypatch):
        url = "https://cdn.example.com/page.png"
        self._route(monkeypatch, {url: (404, b"<!doctype html><title>404</title>")})
        assert raw._fetch_image_bytes(url, "catalog:x::p") is None

    @pytest.mark.parametrize("body", [
        b"\xff\xd8\xff\xe0" + b"jfif-payload",
        b"GIF89a" + b"gif-payload",
        b"RIFF\x10\x00\x00\x00WEBPVP8 payload",
    ])
    def test_invalid_image_magics_are_refused(self, raw, monkeypatch, body):
        url = "https://cdn.example.com/x.img"
        self._route(monkeypatch, {url: (404, body)})
        assert raw._fetch_image_bytes(url, "catalog:x::p") is None

    def test_error_body_salvage_reaches_the_gui_as_ok_true(self, raw, monkeypatch):
        """End to end through the batch RPC: the salvaged body must arrive as a real image,
        not as a 'no preview available' card. Uses ``raw`` — ``gr`` stubs the fetcher, so
        the same test written against it would prove nothing."""
        url = "https://www.google.com/s2/favicons?domain=thk.com&sz=64"
        alt = raw._gstatic_favicon_url(url)
        self._route(monkeypatch, {url: (404, PNG), alt: (404, b"<html/>")})
        r = raw._parts_thumbnails({"items": [{"item_id": "vendorlogo:thk",
                                              "image_url": url}]}, _noop_emit)
        assert r["images"][0]["ok"] is True, "a 404-with-a-PNG-body must not be a lettered tile"
        assert base64.b64decode(r["images"][0]["image_b64"]) == PNG
        cached = raw._parts_thumbnail({"item_id": "vendorlogo:thk", "image_url": url},
                                      _noop_emit)
        assert base64.b64decode(cached["image_b64"]) == PNG, "the salvaged icon must cache"

    def test_s2_404_retries_gstatic_and_prefers_its_real_icon(self, raw, monkeypatch):
        """MEASURED: traceparts.com 404s on s2 (generic globe) but 200s on gstatic's
        faviconV2 with a real 1075-byte JPEG. The real icon must win."""
        s2 = "https://www.google.com/s2/favicons?domain=traceparts.com&sz=64"
        alt = raw._gstatic_favicon_url(s2)
        assert alt.startswith("https://t3.gstatic.com/faviconV2?")
        assert "url=https://traceparts.com" in alt and "size=64" in alt
        real_icon = _png(255)
        asked = self._route(monkeypatch, {s2: (404, PNG), alt: (200, real_icon)})
        assert raw._fetch_image_bytes(s2, "vendorlogo:traceparts") == real_icon
        assert asked == [s2, alt], "exactly one alternate attempt, in order"

    def test_s2_404_keeps_the_globe_when_gstatic_also_fails(self, raw, monkeypatch):
        """Any icon beats a lettered tile, so the globe stands as the last resort."""
        s2 = "https://www.google.com/s2/favicons?domain=thk.com&sz=64"
        alt = raw._gstatic_favicon_url(s2)
        globe = PNG + b"generic-globe"
        asked = self._route(monkeypatch, {s2: (404, globe), alt: (404, globe)})
        assert raw._fetch_image_bytes(s2, "vendorlogo:thk") == globe
        assert asked == [s2, alt]

    def test_s2_200_never_makes_the_alternate_request(self, raw, monkeypatch):
        s2 = "https://www.google.com/s2/favicons?domain=mcmaster.com&sz=64"
        asked = self._route(monkeypatch, {s2: (200, PNG)})
        assert raw._fetch_image_bytes(s2, "vendorlogo:mcmaster") == PNG
        assert asked == [s2], "a working favicon must cost exactly one request"

    def test_gstatic_url_only_derived_for_s2_favicons(self, raw):
        assert raw._gstatic_favicon_url("https://cdn.example.com/a.png") == ""
        assert raw._gstatic_favicon_url("https://www.google.com/search?q=x") == ""
        assert raw._gstatic_favicon_url("https://evil.com/s2/favicons?domain=x.com") == ""
        assert raw._gstatic_favicon_url("https://www.google.com/s2/favicons?sz=64") == "", \
            "no domain to ask about → no alternate request"
        assert "size=64" in raw._gstatic_favicon_url(
            "https://www.google.com/s2/favicons?domain=thk.com"), "size must default to 64"

    def test_non_favicon_404_makes_no_alternate_request(self, raw, monkeypatch):
        url = "https://cdn.example.com/missing.png"
        asked = self._route(monkeypatch, {url: (404, b"")})
        self._recording_cef(monkeypatch)
        assert raw._fetch_image_bytes(url, "catalog:x::p") is None
        assert asked == [url]

    def test_http_failure_never_falls_through_to_unrestricted_cef(self, raw, monkeypatch):
        url = "https://cdn.example.com/hot.png"
        self._route(monkeypatch, {url: (403, b"Forbidden")})
        seen = self._recording_cef(monkeypatch)
        assert raw._fetch_image_bytes(url, "catalog:x::p") is None
        assert seen == []

    def test_hotlink_headers_are_preserved(self, raw, monkeypatch):
        """The Referer/User-Agent hotlink defeat is what makes vendor CDNs serve at all."""
        from services.net import safe_http
        captured: list = []

        def _capture(url, **kwargs):
            captured.append(kwargs["headers"])
            raise OSError("nope")

        monkeypatch.setattr(safe_http, "get", _capture)
        raw._fetch_image_bytes("https://cdn.example.com/a.png",
                               "catalog:mcmaster::https://mcmaster.com/91290A115/")
        assert captured[0]["User-Agent"].startswith("Mozilla/5.0")
        assert captured[0]["Referer"] == "https://mcmaster.com/91290A115/"
