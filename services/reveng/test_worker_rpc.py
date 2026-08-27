"""Unit tests for the Reverse Engineering worker-RPC transport (services.reveng.worker_rpc).

The bridge is what lets the GUI (RE tab + Parts Library "Reverse-engineer this
part" action) reach the reveng backend over the worker seam. It is a thin adapter
over the MCP tool registry, so these tests drive the public
``REVENG_WORKER_METHODS`` table exactly as the worker dispatcher does — no MCP
transport, session store redirected to a temp dir.
"""
from __future__ import annotations

import pytest

from services.reveng import worker_rpc as reveng_rpc

M = reveng_rpc.REVENG_WORKER_METHODS


@pytest.fixture()
def reveng_dir(tmp_path, monkeypatch):
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(d))
    return d


def _emit(*_a, **_k):  # the bridge never emits; a no-op sink
    return None


def _call(method: str, params: dict | None = None) -> dict:
    return M[method](params or {}, _emit)


# Every reveng RPC the desktop GUI dispatches (host/src/ui/reveng/*, parts_workspace.cpp).
# A name missing here is a button that round-trips to "unknown method" and sticks an
# error in the panel — the defect this transport exists to prevent.
_GUI_DISPATCHED = (
    "list_reveng_sessions", "get_reveng_session", "get_reveng_coverage",
    "get_reveng_deviation", "list_reveng_dims", "describe_reveng_result",
    "reveng_status", "reveng_start_session", "reveng_add_images",
    "reveng_add_video", "reveng_ingest_datasheet", "reveng_set_scale",
    "reveng_print_scale_mat", "reveng_reconstruct", "reveng_single_photo",
    "reveng_fit_primitives", "reveng_build_parametric", "reveng_attach_result",
    "reveng_research_source", "reveng_make_part_from_library", "import_scan_mesh",
    "export_reveng_bundle", "start_capture_session", "stop_capture_session",
    "reveng_start_from_product",
)


def test_methods_registered_on_worker():
    """The whole GUI surface lands in the aggregated worker table (GUI reachability)."""
    from services import worker_main as w  # the post-oracle stdio worker (T-RF20)
    for name in _GUI_DISPATCHED:
        assert name in M, f"{name} not exposed by REVENG_WORKER_METHODS"
        assert name in w._METHODS, f"{name} not registered on the worker"


def test_start_session_then_list_and_get(reveng_dir):
    r = _call("reveng_start_session", {"name": "bridge test"})
    assert r["ok"] and r["session_id"]
    sid = r["session_id"]

    lst = _call("list_reveng_sessions")
    assert lst["ok"]
    assert any(s["session_id"] == sid for s in lst["sessions"])

    got = _call("get_reveng_session", {"session_id": sid})
    assert got["ok"] and got["session"]["session_id"] == sid


def test_start_session_with_research_seed_populates_lane(reveng_dir):
    # A part number seed routes through the research/datasheet lane (RE-29).
    r = _call("reveng_start_session", {"research": "SKF 6205"})
    assert r["ok"]
    assert "research_result" in r  # the datasheet/web lane was invoked


def test_add_images_delivers_photos(reveng_dir, tmp_path):
    sid = _call("reveng_start_session", {"name": "photos"})["session_id"]
    photo = tmp_path / "front.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # minimal JFIF

    r = _call("reveng_add_images", {"session_id": sid, "paths": [str(photo)]})
    assert r["ok"] and r["added"] == 1
    assert r["session"]["artifacts"]["images"] == 1
    assert r["session"]["stages"]["capture"] == "done"


def test_start_from_product_threads_media(reveng_dir, monkeypatch):
    """§2b hand-off: harvested images+documents are downloaded into the session
    and threaded through add_images / ingest_datasheet."""
    from services.parts import product_media as pm
    from services.browser import session as bsession

    monkeypatch.setattr(pm, "harvest_product_media", lambda ref: {
        "ok": True, "source_url": "https://shop.example/widget",
        "images": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "documents": ["https://cdn.example/ds.pdf"],
    })

    def _download(url, site, dest, **_k):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4\n" if str(dest).endswith(".pdf")
                         else b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(bsession, "browser_download", _download)
    # datasheet ingest backend may be absent in CI — make it deterministic. The tool
    # resolves _ingest_pdf in its OWN module's globals, so patch it there.
    monkeypatch.setattr("services.mcp.reveng_session_tools._ingest_pdf",
                        lambda path, out_dir: {"ok": True, "tables": [], "crops": []})

    r = _call("reveng_start_from_product",
              {"url": "https://shop.example/widget", "title": "Widget", "mpn": "W-1"})
    assert r["ok"] and r["session_id"]
    assert r["images_added"] == 2
    assert r["documents_ingested"] == 1
    assert r["source_url"] == "https://shop.example/widget"
    assert r["activeWorkspace"] == "ReverseEng"


def test_start_from_product_empty_harvest_still_starts_session(reveng_dir, monkeypatch):
    from services.parts import product_media as pm
    monkeypatch.setattr(pm, "harvest_product_media",
                        lambda ref: {"ok": False, "reason": "captcha_or_2fa_detected"})

    r = _call("reveng_start_from_product", {"url": "https://shop.example/widget"})
    assert r["ok"] and r["session_id"]
    assert r["images_added"] == 0 and r["documents_ingested"] == 0
    # session really exists on disk
    got = _call("get_reveng_session", {"session_id": r["session_id"]})
    assert got["ok"]


# --- §2b harvest: partial results beat an abandoned call ------------------------
#
# reverse_engineer_product carries timeout_s=300 (tool_guidance/parts.py) and calls
# browser_download once per harvested media URL. A site whose 429/403 backoff is
# armed used to block the FIRST item for up to BACKOFF_MAX_S (900 s), so the harness
# killed the call and every already-downloaded item was thrown away.

def _media_harvest(monkeypatch, images, documents):
    """Point harvest_product_media at a fixed media list."""
    from services.parts import product_media as pm
    monkeypatch.setattr(pm, "harvest_product_media", lambda ref: {
        "ok": True, "source_url": "https://shop.example/widget",
        "images": images, "documents": documents,
    })


def _writing_download(cooldown_after: int | None = None, calls: list | None = None):
    """browser_download stub: writes a plausible body, and after ``cooldown_after``
    successful calls starts answering with the cooldown_active refusal."""
    def _download(url, site, dest, **_k):
        if calls is not None:
            calls.append(url)
        if cooldown_after is not None and len(calls or []) > cooldown_after:
            return {"ok": False, "reason": "cooldown_active", "retry_after_s": 612.0,
                    "policy_refusal": True, "executed": False, "hint": "cooling down"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4\n" if str(dest).endswith(".pdf")
                         else b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        return {"ok": True, "dest": str(dest)}
    return _download


def test_harvest_keeps_partial_results_when_the_site_starts_cooling_down(
        reveng_dir, monkeypatch):
    """Item #3 is refused with cooldown_active: the call still SUCCEEDS with the two
    images it got, reports how many were skipped and why, and never touches the
    remaining same-site URLs (documents included)."""
    from services.browser import session as bsession

    _media_harvest(monkeypatch,
                   images=[f"https://cdn.example/{n}.jpg" for n in "abcde"],
                   documents=["https://cdn.example/ds.pdf"])
    calls: list[str] = []
    monkeypatch.setattr(bsession, "browser_download",
                        _writing_download(cooldown_after=2, calls=calls))
    monkeypatch.setattr("services.mcp.reveng_session_tools._ingest_pdf",
                        lambda path, out_dir: {"ok": True, "tables": [], "crops": []})

    r = _call("reveng_start_from_product", {"url": "https://shop.example/widget"})

    assert r["ok"] is True and r["session_id"]
    assert r["images_added"] == 2                     # the partial harvest was KEPT
    assert r["documents_ingested"] == 0
    # 3 remaining images (c,d,e) + the 1 document, none of them attempted after the refusal
    assert r["media_skipped"] == 4
    assert r["media_skipped_reason"] == "cooldown_active"
    assert r["retry_after_s"] == 612.0
    assert len(calls) == 3, calls                     # stopped AT the refusal, not after
    assert "ds.pdf" not in " ".join(calls)
    # The hint must route the model somewhere, not just name the state.
    assert "reveng_add_images" in r["hint"]
    assert "reverse_engineer_product" in r["hint"]    # ...and say not to re-call it


def test_harvest_is_bounded_by_the_item_cap(reveng_dir, monkeypatch):
    """A 60-image gallery cannot be allowed to define this tool's worst case."""
    from services.browser import session as bsession

    cap = reveng_rpc._MAX_HARVEST_ITEMS
    _media_harvest(monkeypatch,
                   images=[f"https://cdn.example/i{n}.jpg" for n in range(60)],
                   documents=["https://cdn.example/ds.pdf"])
    calls: list[str] = []
    monkeypatch.setattr(bsession, "browser_download", _writing_download(calls=calls))

    r = _call("reveng_start_from_product", {"url": "https://shop.example/widget"})

    assert r["ok"] is True
    assert len(calls) == cap                          # images + documents share ONE budget
    assert r["images_added"] == cap
    assert r["media_skipped"] == 61 - cap
    assert r["media_skipped_reason"] == "harvest_item_cap"
    assert "retry_after_s" not in r                   # a cap is not a cooldown
    assert str(cap) in r["hint"]


def test_clean_harvest_reports_zero_skips_and_no_hint(reveng_dir, monkeypatch):
    """The happy path stays clean: no skip fields beyond the honest zero count."""
    from services.browser import session as bsession

    _media_harvest(monkeypatch, images=["https://cdn.example/a.jpg"],
                   documents=["https://cdn.example/ds.pdf"])
    monkeypatch.setattr(bsession, "browser_download", _writing_download(calls=[]))
    monkeypatch.setattr("services.mcp.reveng_session_tools._ingest_pdf",
                        lambda path, out_dir: {"ok": True, "tables": [], "crops": []})

    r = _call("reveng_start_from_product", {"url": "https://shop.example/widget"})

    assert r["ok"] is True and r["images_added"] == 1 and r["documents_ingested"] == 1
    assert r["media_skipped"] == 0
    assert "media_skipped_reason" not in r and "hint" not in r


def test_missing_session_is_structured_failure(reveng_dir):
    r = _call("reveng_add_images", {"session_id": "nope", "paths": []})
    assert r["ok"] is False and "not found" in r["reason"]
    assert "hint" in r

    g = _call("get_reveng_session", {"session_id": "nope"})
    assert g["ok"] is False and "not found" in g["reason"]


def test_missing_required_param_is_structured_failure(reveng_dir):
    """A GUI call that forgets an argument gets an envelope, never a TypeError."""
    r = _call("get_reveng_session")
    assert r["ok"] is False
    assert r["reason"] == "missing_param: session_id"


def test_unregistered_tool_degrades_honestly(monkeypatch):
    """A name in the bridge table with no backing tool must not raise."""
    monkeypatch.setattr(reveng_rpc, "_tool_fn", lambda name: None)
    r = _call("reveng_status")
    assert r["ok"] is False
    assert r["reason"].startswith("tool_not_registered")


def test_capture_sessions_share_the_recon_tools_registry(monkeypatch):
    """GUI-started and agent-started capture sessions must live in ONE registry, and
    the GUI transport must not trigger photogrammetry on stop (the RE tab adds the
    photos to the session itself via reveng_add_images)."""
    from services.vision import capture_server
    from services.vision import photogrammetry
    import services.mcp.recon_tools as recon_tools

    class _FakeSession:
        port = 8888
        upload_url = "http://localhost:8888/upload"
        qr = "http://localhost:8888/upload"
        shot_names = ["front", "back"]
        received = ["/tmp/a.jpg"]
        work_dir = "/tmp"

    monkeypatch.setattr(capture_server, "start_session", lambda port=0: _FakeSession())
    monkeypatch.setattr(capture_server, "stop_session", lambda s: None)

    def _boom(**_kw):
        raise AssertionError("photogrammetry must not run on the GUI stop path")
    monkeypatch.setattr(photogrammetry, "reconstruct", _boom)
    monkeypatch.setitem(recon_tools._SESSIONS, 8888, None)
    recon_tools._SESSIONS.pop(8888)

    started = _call("start_capture_session", {"port": 8888})
    assert started["ok"] and started["port"] == 8888
    # One registry: the GUI-started session is the one the agent transport sees.
    assert recon_tools._SESSIONS.get(8888) is not None

    stopped = _call("stop_capture_session", {"port": 8888})
    assert stopped["ok"] and stopped["photo_count"] == 1
    assert stopped["reconstruction"] is None and stopped["mesh_path"] is None
    assert 8888 not in recon_tools._SESSIONS  # registry drained through recon_tools

    # …and the honest failure for a port nobody started.
    gone = _call("stop_capture_session", {"port": 8888})
    assert gone["ok"] is False and "no capture session" in gone["reason"]
