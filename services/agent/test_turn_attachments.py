"""test_turn_attachments.py — options.attachments → vision-model image parts.

Proves the pasted-image contract end to end at the Python layers:

  * agent_turn.run_turn folds a valid ``kind: "image"`` attachment onto the
    CURRENT user message as a real provider ``{"type": "image"}`` part, with
    the right path and mime, alongside the text part.
  * It survives the slash-command rebuild: ``skills.resolve_slash`` stripping a
    leading command rebuilds that same user message, and the image must still
    ride it (the regression the shared helper exists for).
  * Every malformed shape — kind "file", missing path, non-dict, empty path,
    oversized file — is skipped silently and the turn still runs.
  * At most 8 images ride, and they are the FIRST 8.
  * attachments None/[] == byte-identical messages to before the feature.
  * The explicit offline role-turn wrapper forwards attachments unchanged.

Scripted provider only, no model/network/port.
Run: pytest services/agent/test_turn_attachments.py -q -p no:randomly
"""

from __future__ import annotations

import struct
import zlib

from services.agent import agent_turn
from services.agent import mission_dispatch


class _RecordingProvider:
    """Scripted provider that captures the messages list of its first call."""

    name = "scripted"

    def __init__(self, replies=("done.",)):
        self._replies = list(replies)
        self.seen_messages: list[dict] | None = None

    def complete(self, system, messages, max_tokens=8000):
        if self.seen_messages is None:
            self.seen_messages = [dict(m) for m in messages]
        return self._replies.pop(0) if self._replies else "done."


def _png(path, *, pixels=b"\x00\xff\x00\x00"):
    """Write a real 1x1 PNG (valid signature + IHDR/IDAT/IEND) at *path*."""

    def _chunk(tag, payload):
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\x00" + pixels))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(body)
    return str(path)


def _run(request, provider, **kw):
    return agent_turn.run_turn(
        request,
        provider,
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        **kw,
    )


def _request_msg(provider):
    """The user message carrying the REQUEST and its attachments.

    Deliberately not `seen_messages[-1]`. Since the Rules engine went live
    (2026-07-29) a matching rule appends its own `role: "user"` ADVISORY message
    before the first completion, so the request is no longer last. The request
    message is the first user-role one; the advisory follows it and carries no
    attachments (pinned by test_a_rule_advisory_never_contaminates_the_request).
    """
    users = [m for m in provider.seen_messages if m.get("role") == "user"]
    assert users, "no user message reached the provider"
    return users[0]


def _images(msg):
    return [p for p in msg["content"] if p.get("type") == "image"]


def _texts(msg):
    return [p["text"] for p in msg["content"] if p.get("type") == "text"]


def test_image_attachment_becomes_one_image_part(tmp_path):
    shot = _png(tmp_path / "shot.png")
    provider = _RecordingProvider()
    _run(
        "what is wrong with this drawing?",
        provider,
        attachments=[
            {"path": shot, "name": "shot.png", "kind": "image", "mime": "image/png"}
        ],
    )
    msg = _request_msg(provider)
    assert msg["role"] == "user"
    assert _texts(msg) == ["what is wrong with this drawing?"]
    assert _images(msg) == [{"type": "image", "path": shot, "mime": "image/png"}]


def test_mime_is_honoured_not_forced_to_png(tmp_path):
    shot = _png(tmp_path / "photo.jpg")
    provider = _RecordingProvider()
    _run(
        "look",
        provider,
        attachments=[
            {"path": shot, "name": "photo.jpg", "kind": "image", "mime": "image/jpeg"}
        ],
    )
    assert _images(_request_msg(provider))[0]["mime"] == "image/jpeg"


def test_slash_command_rebuild_keeps_the_image(monkeypatch, tmp_path):
    """resolve_slash strips a leading command and REBUILDS the user message.

    Forgetting that second construction site silently dropped the image for
    every prompt that began with a slash command. Force the strip so the
    rebuild path is exercised regardless of which skills happen to be
    installed on the machine running this test.
    """
    from services.agent import skills as _skills

    monkeypatch.setattr(
        _skills, "resolve_slash", lambda text: ("describe this image", ["fake-skill"])
    )
    monkeypatch.setattr(_skills, "catalog_block", lambda: "")
    monkeypatch.setattr(_skills, "activate_for_prompt", lambda *a, **k: [])

    shot = _png(tmp_path / "shot.png")
    provider = _RecordingProvider()
    _run(
        "/vision describe this image",
        provider,
        attachments=[
            {"path": shot, "name": "shot.png", "kind": "image", "mime": "image/png"}
        ],
    )
    msg = _request_msg(provider)
    # The slash command really was stripped (we are on the rebuild path) ...
    assert _texts(msg) == ["describe this image"]
    # ... and the image survived it.
    assert _images(msg) == [{"type": "image", "path": shot, "mime": "image/png"}]


def test_malformed_attachments_are_skipped_without_raising(tmp_path):
    good = _png(tmp_path / "good.png")
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text, already inlined by the composer")
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (21 * 1024 * 1024))

    provider = _RecordingProvider()
    _run(
        "check these",
        provider,
        attachments=[
            {"path": str(text_file), "name": "notes.txt", "kind": "file"},
            {"path": str(tmp_path / "gone.png"), "name": "gone.png", "kind": "image"},
            "not a dict",
            None,
            {"path": "", "name": "", "kind": "image"},
            {"name": "no path at all", "kind": "image"},
            {"path": 42, "kind": "image"},
            {"path": str(huge), "name": "huge.png", "kind": "image"},
            {"path": good, "name": "good.png", "kind": "image"},
        ],
    )
    msg = _request_msg(provider)
    assert _texts(msg) == ["check these"]
    assert [p["path"] for p in _images(msg)] == [good]


def test_directory_path_is_not_an_image(tmp_path):
    provider = _RecordingProvider()
    _run(
        "hi",
        provider,
        attachments=[{"path": str(tmp_path), "name": "dir", "kind": "image"}],
    )
    assert _images(_request_msg(provider)) == []


def test_at_most_eight_images_and_they_are_the_first_eight(tmp_path):
    paths = [_png(tmp_path / f"img{i:02d}.png") for i in range(12)]
    provider = _RecordingProvider()
    _run(
        "all of them",
        provider,
        attachments=[
            {"path": p, "name": f"img{i:02d}.png", "kind": "image"}
            for i, p in enumerate(paths)
        ],
    )
    got = [p["path"] for p in _images(_request_msg(provider))]
    assert got == paths[:8]


def test_default_mime_when_absent(tmp_path):
    shot = _png(tmp_path / "shot.png")
    provider = _RecordingProvider()
    _run(
        "look",
        provider,
        attachments=[{"path": shot, "name": "shot.png", "kind": "image"}],
    )
    assert _images(_request_msg(provider))[0]["mime"] == "image/png"


def test_no_attachments_is_byte_identical_to_before():
    a = _RecordingProvider()
    _run("summarize this part", a)
    b = _RecordingProvider()
    _run("summarize this part", b, attachments=None)
    c = _RecordingProvider()
    _run("summarize this part", c, attachments=[])
    assert a.seen_messages == b.seen_messages == c.seen_messages
    assert a.seen_messages == [
        {"role": "user", "content": [{"type": "text", "text": "summarize this part"}]}
    ]


def test_attachments_ride_alongside_history(tmp_path):
    shot = _png(tmp_path / "shot.png")
    provider = _RecordingProvider()
    _run(
        "and this one?",
        provider,
        history=[
            {"role": "user", "text": "model a 20 mm cube"},
            {"role": "assistant", "text": "Done."},
        ],
        attachments=[{"path": shot, "name": "shot.png", "kind": "image"}],
    )
    msgs = provider.seen_messages
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    # Only the CURRENT user message carries the image.
    assert _images(msgs[0]) == [] and _images(msgs[1]) == []
    assert [p["path"] for p in _images(msgs[2])] == [shot]


def test_run_linear_role_turn_forwards_attachments(monkeypatch):
    captured = {}

    def _fake_run_turn(request, provider, **kwargs):
        captured["attachments"] = kwargs.get("attachments")
        return agent_turn.TurnResult(
            success=True, stop_reason="confirmed", result={"calls": [], "tool_calls": 0}
        )

    monkeypatch.setattr(agent_turn, "run_turn", _fake_run_turn)
    atts = [{"path": "/tmp/a.png", "name": "a.png", "kind": "image"}]
    mission_dispatch.run_linear_role_turn(
        "look at this",
        "general_agent",
        object(),
        attachments=atts,
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert captured["attachments"] == atts

    mission_dispatch.run_linear_role_turn(
        "plain turn", "general_agent", object(), tool_dispatch=lambda t, a: {"ok": True}
    )
    assert captured["attachments"] is None


def test_a_rule_advisory_never_contaminates_the_request(tmp_path):
    """A fired rule must be its OWN message, not folded into the user's turn.

    "what is wrong with this drawing?" matches the drawing_standards rule, so this
    turn really does inject. The request message must still hold exactly the user's
    text and their one image, and the advisory must arrive as a separate user-role
    message with no attachments — otherwise an injected block would ride inside the
    user's own turn and be indistinguishable from something they wrote.
    """
    shot = _png(tmp_path / "shot.png")
    provider = _RecordingProvider()
    _run(
        "what is wrong with this drawing?",
        provider,
        attachments=[
            {"path": shot, "name": "shot.png", "kind": "image", "mime": "image/png"}
        ],
    )
    users = [m for m in provider.seen_messages if m.get("role") == "user"]
    req = users[0]
    assert _texts(req) == ["what is wrong with this drawing?"]
    assert len(_images(req)) == 1

    advisories = [m for m in users[1:] if any("[ADVISORY" in t for t in _texts(m))]
    assert advisories, "the drawing_standards rule should have injected here"
    for adv in advisories:
        assert _images(adv) == [], "an advisory must not carry attachments"
        assert "what is wrong with this drawing?" not in " ".join(_texts(adv))
