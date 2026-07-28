"""Snap-compaction tests: OMP snap_render integration, snap store, re-snap chain.

The bridge (``services.agent.omp_bridge.call``) is faked everywhere except the
env-gated live test at the bottom (CHUDWARE_SNAP_LIVE=1). The snap store root
is redirected to tmp_path via ``snap_store.default_dir``.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from services.agent import compaction, snap_spec, snap_store

# 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

VISION_PROVIDER = {"api": "anthropic", "model": "claude-test", "vision": True}


def _frame(n: int = 1) -> dict:
    return {
        "data": _PNG_B64,
        "mimeType": "image/png",
        "cols": 80,
        "rows": 40,
        "chars": 100 * n,
    }


def _msgs(n: int, *, tag: str = "") -> list[dict]:
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"{tag}message {i} " + "x" * 120})
    return out


class FakeBridge:
    """Records snap_render calls; returns a deterministic contract response.

    Emulates the real bridge's OG-source semantics: the returned ``text`` is the
    cumulative source (priorText unfolded with the new text appended).
    """

    def __init__(self, n_frames: int = 2, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.n_frames = n_frames
        self.fail = fail

    def __call__(self, method: str, params: dict | None = None, timeout: float = 120):
        if self.fail is not None:
            raise self.fail
        assert method == "snap_render"
        params = dict(params or {})
        self.calls.append(params)
        prior = str(params.get("priorText") or "")
        new = str(params.get("text") or "")
        full = "\n".join(p for p in (prior, new) if p)
        frames = [_frame(i) for i in range(self.n_frames)] if full else []
        return {
            "frames": frames,
            "text": full,
            "textHead": full[:50],
            "textTail": full[-50:],
            "totalChars": len(full),
            "truncatedChars": 0,
            "checksum": "deadbeef",
            "summary": "fake",
            "shortSummary": "fake",
        }


@pytest.fixture()
def snap_env(tmp_path, monkeypatch):
    """Omp backend on, snap frames on, store redirected to tmp_path."""
    monkeypatch.setattr(compaction, "_compaction_backend", lambda cfg=None: "omp")
    monkeypatch.setattr(
        "services.agent.config.compaction_snap_frames", lambda cfg=None: True
    )
    monkeypatch.setattr(
        "services.agent.config.compaction_snap_max_frames", lambda cfg=None: 80
    )
    monkeypatch.setattr(snap_store, "default_dir", lambda cfg=None: tmp_path)
    return tmp_path


def _install_bridge(monkeypatch, fake: FakeBridge):
    monkeypatch.setattr("services.agent.omp_bridge.call", fake)


# --------------------------------------------------------------------------- stamp


def test_stamp_line_roundtrip():
    snap = {"snap_id": "0123456789abcdef", "api": "anthropic", "model": "claude-x"}
    line = compaction._snap_stamp_line(snap)
    m = compaction._SNAP_STAMP_RE.search(line)
    assert m, line
    assert m.group(1) == "0123456789abcdef"
    assert m.group(2) == "anthropic"
    assert m.group(3).strip() == "claude-x"


def test_snap_anchor_prefers_prior_archive():
    prior = {"snap": {"snap_id": "a" * 16, "api": "anthropic", "model": "m"}}
    msgs = [{"role": "user", "content": "[snap:" + "b" * 16 + " api=openai model=n]\n..."}]
    anchor = compaction._snap_anchor(prior, msgs)
    assert anchor["snap_id"] == "a" * 16


def test_snap_anchor_finds_newest_stamped_message():
    msgs = [
        {"role": "user", "content": "[snap:" + "a" * 16 + " api=openai model=x]\nold"},
        {"role": "assistant", "content": "plain"},
        {"role": "user", "content": "[snap:" + "b" * 16 + " api=anthropic model=y]\nnew"},
    ]
    anchor = compaction._snap_anchor(None, msgs)
    assert anchor["snap_id"] == "b" * 16
    assert anchor["api"] == "anthropic"
    assert compaction._snap_anchor(None, [{"role": "user", "content": "no stamp"}]) is None


# --------------------------------------------------------------------------- snap round-trip


def test_snap_roundtrip_vision_provider(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=3)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER, session_id="s1"
    )
    snap = out["archive"].get("snap")
    assert snap, out["archive"]
    assert snap["api"] == "anthropic"
    assert snap["model"] == "claude-test"
    assert snap["frame_count"] == 3
    assert snap["session_id"] == "s1"
    assert "(+3 snap frames)" in out["short_summary"]
    # Bridge got the discarded transcript as text, empty prior on a fresh chain.
    assert len(fake.calls) == 1
    assert fake.calls[0]["priorText"] == ""
    assert "message 0" in fake.calls[0]["text"]
    assert fake.calls[0]["api"] == "anthropic"
    # Store round-trip: OG source + 3 decoded PNGs validated.
    meta = snap_store.get(snap["snap_id"], root=snap_env)
    assert meta is not None
    assert len(meta["frames"]) == 3
    for fr in meta["frames"]:
        assert fr["path"].read_bytes() == base64.b64decode(_PNG_B64)
    assert "message 0" in snap_store.source_of(snap["snap_id"], root=snap_env)
    # Reinject with the same vision provider: text part + 3 image parts, stamp first.
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    assert len(msgs) == 1
    parts = msgs[0]["content"]
    assert isinstance(parts, list)
    assert parts[0]["type"] == "text"
    assert parts[0]["text"].startswith(f"[snap:{snap['snap_id']} api=anthropic model=claude-test]")
    images = [p for p in parts if p.get("type") == "image"]
    assert len(images) == 3
    assert all(p["mime"] == "image/png" for p in images)


def test_chain_appends_prior_source(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=1)
    _install_bridge(monkeypatch, fake)
    out1 = compaction.snapcompact_archive(
        _msgs(8, tag="r1 "), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    snap1 = out1["archive"]["snap"]
    # Rebuild the conversation the way production does: reinjected archive + kept + new.
    convo = (
        compaction.reinject_archive(out1, provider=VISION_PROVIDER)
        + out1["kept_messages"]
        + _msgs(6, tag="r2 ")
    )
    out2 = compaction.snapcompact_archive(
        convo, keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    snap2 = out2["archive"].get("snap")
    assert snap2 and snap2["snap_id"] != snap1["snap_id"]
    assert len(fake.calls) == 2
    # Second render unfolded onto round 1's stored OG source.
    assert fake.calls[1]["priorText"] == fake.calls[0]["text"]
    assert "r1 message 0" in fake.calls[1]["priorText"]
    meta2 = snap_store.get(snap2["snap_id"], root=snap_env)
    assert meta2["parent_id"] == snap1["snap_id"]
    # OG chain: round-2 source carries both rounds.
    src2 = snap_store.source_of(snap2["snap_id"], root=snap_env)
    assert "r1 message 0" in src2 and "r2 message 0" in src2


def test_provider_switch_resnaps_at_reinject(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=2)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    snap = out["archive"]["snap"]
    assert len(fake.calls) == 1
    switched = {"api": "openai", "model": "gpt-test", "vision": True}
    msgs = compaction.reinject_archive(out, provider=switched)
    parts = msgs[0]["content"]
    images = [p for p in parts if p.get("type") == "image"]
    assert len(images) == 2
    # A second render happened: OG source re-rendered in the new provider's shape.
    assert len(fake.calls) == 2
    assert fake.calls[1]["text"] == ""
    assert fake.calls[1]["api"] == "openai"
    assert fake.calls[1]["modelId"] == "gpt-test"
    assert fake.calls[1]["priorText"] == fake.calls[0]["text"]
    # The re-snap is chained to the original snap.
    src = fake.calls[1]["priorText"]
    new_id = snap_store.snap_id_for(src, snap["snap_id"], "gpt-test", "openai")
    meta = snap_store.get(new_id, root=snap_env)
    assert meta is not None and meta["parent_id"] == snap["snap_id"]


def test_no_vision_provider_never_calls_bridge(snap_env, monkeypatch):
    fake = FakeBridge()
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50,
        provider={"api": "anthropic", "model": "claude-test", "vision": False},
    )
    assert "snap" not in out["archive"]
    assert fake.calls == []
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    assert isinstance(msgs[0]["content"], str)


def test_local_backend_never_calls_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(compaction, "_compaction_backend", lambda cfg=None: "local")
    monkeypatch.setattr(snap_store, "default_dir", lambda cfg=None: tmp_path)
    fake = FakeBridge()
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    assert "snap" not in out["archive"]
    assert fake.calls == []


def test_bridge_failure_degrades_to_text(snap_env, monkeypatch):
    from services.agent.omp_bridge import BridgeUnavailable

    fake = FakeBridge(fail=BridgeUnavailable("no bun"))
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    assert "snap" not in out["archive"]
    assert "snap render unavailable" in out["archive"]["warning"]
    # Text-only reinject still carries the archive.
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    assert isinstance(msgs[0]["content"], str)
    assert "message 0" in msgs[0]["content"]


def test_zero_frames_snap_still_stores_source(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=0)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    snap = out["archive"].get("snap")
    assert snap and snap["frame_count"] == 0
    assert snap_store.source_of(snap["snap_id"], root=snap_env)
    # No frames → vision reinject falls back to plain (stamped) text.
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    assert isinstance(msgs[0]["content"], str)
    assert msgs[0]["content"].startswith(f"[snap:{snap['snap_id']} ")


def test_budget_cap_drops_middle_frames(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=10)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    monkeypatch.setattr(snap_spec, "FAMILY_IMAGE_BUDGETS", {"anthropic": 4})
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    parts = msgs[0]["content"]
    images = [p for p in parts if p.get("type") == "image"]
    assert len(images) == 4
    assert "6 middle snap frame(s) omitted" in parts[0]["text"]


def test_budget_frames_edge_keeping():
    frames = [{"i": i} for i in range(10)]
    keep = compaction._budget_frames(frames, 4, 10**9)
    assert [f["i"] for f in keep] == [0, 1, 8, 9]


def test_missing_store_falls_back_to_text(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=2)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    import shutil

    shutil.rmtree(snap_env / out["archive"]["snap"]["snap_id"])
    msgs = compaction.reinject_archive(out, provider=VISION_PROVIDER)
    assert isinstance(msgs[0]["content"], str)
    assert "message 0" in msgs[0]["content"]


def test_snap_dir_gc(snap_env, monkeypatch):
    fake = FakeBridge(n_frames=1)
    _install_bridge(monkeypatch, fake)
    out = compaction.snapcompact_archive(
        _msgs(8), keep_recent_tokens=50, provider=VISION_PROVIDER
    )
    snap_id = out["archive"]["snap"]["snap_id"]
    freed = snap_store.gc(root=snap_env, max_bytes=1)
    assert freed > 0
    assert snap_store.get(snap_id, root=snap_env) is None


# --------------------------------------------------------------------------- live bridge


@pytest.mark.skipif(
    os.environ.get("CHUDWARE_SNAP_LIVE") != "1",
    reason="live OMP bridge smoke; set CHUDWARE_SNAP_LIVE=1",
)
def test_live_bridge_snap_render(snap_env):
    from services.agent import omp_bridge

    big = "\n".join(f"line {i}: " + "lorem ipsum dolor sit amet " * 8 for i in range(900))
    assert len(big) > 28_000
    out = compaction.snapcompact_archive(
        [{"role": "user", "content": big}] + _msgs(4),
        keep_recent_tokens=50,
        provider=VISION_PROVIDER,
    )
    snap = out["archive"].get("snap")
    assert snap, out["archive"].get("warning")
    assert snap["frame_count"] >= 1
    meta = snap_store.get(snap["snap_id"], root=snap_env)
    assert meta is not None
    src = snap_store.source_of(snap["snap_id"], root=snap_env)
    assert "line 0" in src and "line 899" in src
    omp_bridge.shutdown()
