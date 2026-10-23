"""OMP-port compaction engines (agent.compaction) — pure / offline.

(The legacy loop.py overflow-helper tests that used to live here were removed
with the loop itself.)
"""

from __future__ import annotations

from services.agent import compaction as compact


# ---------------------------------------------------------------------------
# OMP-port engines (agent.compaction) — pure / offline
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _assistant_call(call_id: str, name: str, arguments: dict, intent: str = "") -> dict:
    block: dict = {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": arguments,
    }
    if intent:
        block["intent"] = intent
    return {"role": "assistant", "content": [block]}


def _tool(call_id: str, content: str, name: str = "tool", **extra) -> dict:
    m = {
        "role": "tool",
        "tool_call_id": call_id,
        "tool_name": name,
        "content": content,
    }
    m.update(extra)
    return m


def test_serialize_transcript_caps_and_scopes() -> None:
    msgs = [
        _user("build a bracket"),
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "plan it"},
                {"type": "text", "text": "ok"},
                {
                    "type": "toolCall",
                    "id": "c1",
                    "name": "write",
                    "arguments": {"path": "p.py", "content": "X" * 2000},
                    "intent": "save file",
                },
            ],
        },
        _tool("c1", "Y" * 5000, name="write"),
    ]
    text = compact.serialize_transcript(msgs)
    assert "¶user:build a bracket" in text
    assert "¶think:plan it" in text
    assert "¶ai:ok" in text
    assert "¶call:write(" in text
    assert "//save file" in text
    assert "<out>" in text and "</out>" in text
    assert "elided" in text
    out = text.split("<out>")[1].split("</out>")[0]
    assert len(out) < 2500


def test_serialize_drop_useless() -> None:
    msgs = [
        _user("x"),
        _assistant_call("u1", "read", {"path": "a"}, intent="peek"),
        _tool("u1", "noise", name="read", useless=True),
        _assistant_call("u2", "read", {"path": "b"}),
        _tool("u2", "keep-me", name="read"),
    ]
    dropped = compact.serialize_transcript(msgs, drop_useless=True)
    assert "noise" not in dropped
    assert "keep-me" in dropped
    kept = compact.serialize_transcript(msgs, drop_useless=False)
    assert "noise" in kept


def test_snapcompact_edge_preservation_and_middle() -> None:
    # TEXT_EDGE_CHARS=8000 → need >16000 archived chars. Fat turns + tiny keep.
    fat = "Q" * 500
    msgs: list[dict] = []
    for i in range(50):
        msgs.append(_user(f"U{i} {fat}"))
        msgs.append(_assistant_text(f"A{i} {fat}"))
    result = compact.snapcompact_archive(
        msgs, keep_recent_tokens=50, supersede_reads=False, drop_useless=True
    )
    assert result["cut_index"] > 0
    assert result["cut_index"] < len(msgs)
    arch = result["archive"]
    discarded = msgs[: result["cut_index"]]
    ser = compact.serialize_transcript(discarded, drop_useless=True)
    assert len(ser) > 2 * compact.TEXT_EDGE_CHARS, len(ser)
    assert arch["text_head"]
    assert arch["text_tail"]
    assert arch["truncated_chars"] > 0
    assert "middle history omitted" in arch["text"]
    assert arch["text"].startswith(arch["text_head"])
    assert arch["text"].endswith(arch["text_tail"])
    assert "Archived" in result["short_summary"]
    assert "as text" in result["short_summary"]
    assert "HISTORY" in result["summary"]
    assert result["kept_messages"] == msgs[result["cut_index"] :]


def test_snapcompact_never_cuts_mid_tool_result() -> None:
    msgs = [
        _user("start"),
        _assistant_call("t1", "measure_part", {}),
        _tool("t1", "geom " * 500, name="measure_part"),
        _user("more"),
        _assistant_text("done"),
    ]
    cut = compact.find_cut_index(msgs, keep_recent_tokens=1)
    assert cut == 0 or not compact._is_tool_result(msgs[cut])  # noqa: SLF001
    r = compact.snapcompact_archive(msgs, keep_recent_tokens=5)
    if r["kept_messages"]:
        assert r["kept_messages"][0].get("role") != "tool" or r["cut_index"] == 0


def test_snapcompact_cumulative_fold() -> None:
    msgs = [_user("old " + "Z" * 100), _assistant_text("reply")]
    first = compact.snapcompact_archive(
        msgs + [_user("mid"), _assistant_text("m")],
        keep_recent_tokens=1,
        supersede_reads=False,
    )
    second = compact.snapcompact_archive(
        [_user("new work"), _assistant_text("ok")] * 10,
        keep_recent_tokens=50,
        prior_archive=first["archive"],
        supersede_reads=False,
    )
    prior_head = first["archive"]["text_head"] or first["archive"]["text"]
    assert prior_head[:20] in second["archive"]["text"] or prior_head[:20] in (
        second["archive"]["text_head"] or ""
    )


def test_snapcompact_supersede_reads() -> None:
    msgs = [
        _assistant_call("r1", "read", {"path": "part.step+"}),
        _tool("r1", "OLD_BODY_UNIQUE", name="read"),
        _assistant_call("r2", "read", {"path": "part.step+"}),
        _tool("r2", "NEW_BODY_UNIQUE", name="read"),
        _user("continue " + "c" * 200),
        _assistant_text("yes " + "y" * 200),
    ]
    applied = compact.apply_supersede_reads(msgs)
    assert compact.SUPERSEDED_NOTICE in str(applied[1].get("content"))
    assert "NEW_BODY_UNIQUE" in str(applied[3].get("content"))
    assert "OLD_BODY_UNIQUE" not in str(applied[1].get("content"))

    # Serialize after supersede must not contain the older body.
    ser = compact.serialize_transcript(applied, drop_useless=False)
    assert "OLD_BODY_UNIQUE" not in ser
    assert "NEW_BODY_UNIQUE" in ser or compact.SUPERSEDED_NOTICE in ser


def test_shake_eligibility_and_placeholder() -> None:
    chat: list[dict] = []
    for i in range(3):
        chat.append(_user(f"u{i}"))
        chat.append(_assistant_text(f"a{i}"))
    chat.append(_assistant_call("t1", "measure_part", {}))
    chat.append(_tool("t1", "M" * 20_000, name="measure_part"))
    # Need > protectTokens (16k) AFTER the tool result so it is eligible.
    for i in range(120):
        chat.append(_user("recent " + "x" * 300))
        chat.append(_assistant_text("ok " + "y" * 300))

    auto = compact.shake_messages(chat, aggressive=False)
    assert auto["placeholders"] >= 1, auto["notice"]
    assert auto["saved_tokens"] >= 4_000
    assert any("shaken" in str(m.get("content")) for m in auto["messages"])
    assert auto["regions"]
    region = auto["regions"][0]
    assert "artifact://" not in region.get("placeholder", "")
    assert region["original_text"] == "M" * 20_000

    # A fetched skill body is protected even aggressive, under either fetch
    # verb: cad_skill (CAD's own packs) and the runtime's read skill://.
    for call, result_name, args in (
        ("s1", "cad_skill", {"name": "edit-part"}),
        ("s1", "read", {"path": "skill://edit-part"}),
    ):
        skill_chat = [
            _assistant_call(call, result_name, args),
            _tool(call, "SKILLDOC" * 2000, name=result_name),
            _user("go"),
        ]
        for i in range(50):
            skill_chat.append(_user("pad " + "p" * 100))
        sk = compact.shake_messages(skill_chat, aggressive=True)
        assert sk["placeholders"] == 0, result_name
        assert "SKILLDOC" in str(sk["messages"][1].get("content")), result_name


def test_shake_aggressive_and_min_savings_noop() -> None:
    tiny = [
        _user("hi"),
        _assistant_call("t", "x", {}),
        _tool("t", "small-result", name="x"),
    ]
    noop = compact.shake_messages(tiny, aggressive=False)
    assert noop["placeholders"] == 0
    assert noop["saved_tokens"] == 0
    assert "minSavings" in noop["notice"]

    agg = compact.shake_messages(tiny, aggressive=True)
    assert agg["placeholders"] == 1
    assert "shaken" in str(agg["messages"][2].get("content"))


def test_shake_images_mode() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "path": "shot.png"},
            ],
        }
    ]
    out = compact.shake_messages(msgs, mode="images")
    parts = out["messages"][0]["content"]
    assert all(p.get("type") != "image" for p in parts)
    assert out["placeholders"] >= 1


def test_shake_large_fence() -> None:
    fence = "```python\n" + ("print(1)\n" * 500) + "```"
    msgs = [_user("intro\n" + fence + "\noutro")]
    out = compact.shake_messages(msgs, aggressive=True)
    assert out["placeholders"] >= 1
    body = out["messages"][0]["content"]
    assert "shaken" in body
    assert "print(1)" not in body or body.count("print(1)") < 10


def test_reinject_archive() -> None:
    msgs = [_user("alpha " * 50), _assistant_text("beta " * 50)] * 20
    r = compact.snapcompact_archive(msgs, keep_recent_tokens=100, supersede_reads=False)
    injected = compact.reinject_archive(r)
    assert len(injected) == 1
    assert injected[0]["role"] == "user"
    assert "HISTORY" in injected[0]["content"]
    injected2 = compact.reinject_archive(r["archive"])
    assert injected2[0]["role"] == "user"




# ---------------------------------------------------------------------------
# revert-safety: compaction must never mutate persisted history
# ---------------------------------------------------------------------------


def test_rpc_wrappers_never_mutate_caller_messages() -> None:
    """Revert depends on the chat transcript surviving compaction untouched.
    compact_session / shake_session deep-copy before handing messages to the
    engines (shake elides in place), so the caller's list AND nested dicts must
    be byte-identical afterwards, and the rebuilt transcript must be new objects."""
    import copy as _copy

    from services.agent import session_cmds as sc

    fat = "Z" * 6000
    msgs = [
        _user("build a bracket " + fat),
        _assistant_call(
            "c1", "add_features", {"ops": [{"op": "box"}]}, intent="make box"
        ),
        _tool("c1", "W" * 5000, name="add_features"),
        _assistant_text("done " + fat),
    ] * 4
    before = _copy.deepcopy(msgs)

    out = sc.compact_session(
        msgs, "snapcompact", keep_recent_tokens=200, standalone=True
    )
    assert msgs == before  # caller's history untouched
    assert out["kept_messages"] is not msgs

    out2 = sc.shake_session(msgs, mode="elide", aggressive=True, standalone=True)
    assert msgs == before  # in-place elision hit the copy, never the caller
    assert out2["messages"] is not msgs
