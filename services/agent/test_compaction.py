"""Auto-compaction + OMP-port engines.

1. Legacy loop overflow helpers (``_apply_compaction`` / ``_is_context_overflow``).
2. Pure engines in ``services.agent.compaction`` (serialize / snapcompact / shake).
"""
from __future__ import annotations

from agent import loop
from agent.loop import (_apply_compaction, _is_context_overflow, _keep_recent,
                        _strip_images)
from agent.providers.base import Provider, assistant, image_part, text_part, user

from services.agent import compaction as compact
from services.agent import config as agent_config

LM_STUDIO_ERR = ("Error code: 400 - {'error': 'request (4349 tokens) exceeds the available "
                 "context size (4096 tokens), try increasing it'}")


def test_detection() -> None:
    assert _is_context_overflow(RuntimeError(LM_STUDIO_ERR)), "must catch LM Studio overflow"
    assert _is_context_overflow(Exception("This model's maximum context length is 8192 tokens"))
    assert _is_context_overflow(Exception("prompt is too long: 250000 tokens > 200000"))
    assert not _is_context_overflow(Exception("401 invalid api key")), "non-context 400 -> no"
    print("[detect] overflow errors recognized; unrelated errors ignored")


def _roles(messages):
    return [m["role"] for m in messages]


def _valid_alternation(messages) -> bool:
    rs = _roles(messages)
    if not rs or rs[0] != "user":
        return False
    return all(a != b for a, b in zip(rs, rs[1:]))


def test_compaction_helpers() -> None:
    def fresh():
        return [
            user([text_part("original request")]),
            assistant([text_part("code v1")]),
            user([text_part("feedback 1"), image_part("a.png")]),
            assistant([text_part("code v2")]),
            user([text_part("feedback 2"), image_part("b.png")]),
            assistant([text_part("code v3")]),
            user([text_part("feedback 3"), image_part("c.png")]),
        ]

    m = fresh()
    assert _strip_images(m, keep_last=1) == 2, "should drop all but the last image"
    imgs = [p for msg in m for p in msg["content"] if p.get("type") == "image"]
    assert len(imgs) == 1 and imgs[0]["path"] == "c.png", "the newest render is kept"
    assert _valid_alternation(m), "stripping images keeps roles valid"

    m = fresh()
    _keep_recent(m, keep_pairs=2)
    assert _roles(m) == ["user", "assistant", "user", "assistant", "user"], _roles(m)
    assert m[0]["content"][0]["text"] == "original request", "original request is preserved"
    assert _valid_alternation(m)

    m = fresh()
    _apply_compaction(m, level=3)  # aggressive: no images, fewer turns
    assert not [p for msg in m for p in msg["content"] if p.get("type") == "image"]
    assert _valid_alternation(m), "deep compaction still yields a valid conversation"
    print("[helpers] image-strip + keep-recent shrink history, preserve alternation")


class _OverflowThenOK(Provider):
    """Raises a context-overflow error until the (proxy) payload fits the budget."""
    name = "overflow-mock"
    offline_safe = True  # in-process test double; makes no network calls

    def __init__(self, replies, budget, image_cost=8000):
        self._replies = replies
        self._i = 0
        self.budget = budget
        self.image_cost = image_cost
        self.calls = 0
        self.overflows = 0

    def _size(self, system, messages) -> int:
        n = len(system)
        for m in messages:
            for p in m["content"]:
                n += len(p.get("text", "")) if p.get("type") == "text" else self.image_cost
        return n

    def complete_ex(self, system, messages, max_tokens=8000):
        self.calls += 1
        if self._size(system, messages) > self.budget:
            self.overflows += 1
            raise RuntimeError(LM_STUDIO_ERR)
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply, ""


_REPLIES = [
    '```python\nresult = Box(20, 20, 20)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
    '```json\n{"done": true, "notes": "matches"}\n```',
]


def test_autocompact_recovers() -> None:
    # Budget passes the (image-free) first call but not the image-bearing verify turn until
    # images are dropped -> forces escalation, then succeeds.
    prov = _OverflowThenOK(_REPLIES, budget=5000, image_cost=8000)
    run = loop.run("a 20mm cube", prov, name="agent_compact")
    assert run.success, "loop should recover via compaction and finish"
    assert prov.overflows >= 1, "the provider should have reported at least one overflow"
    print("[recover] success after", prov.overflows, "overflow(s); calls:", prov.calls)


def test_gives_up_when_impossible() -> None:
    # Nothing fits this budget; _generate must give up after the cap and surface the error.
    prov = _OverflowThenOK(_REPLIES, budget=10, image_cost=8000)
    try:
        loop.run("a cube", prov, name="agent_compact_fail")
    except Exception as e:  # noqa: BLE001
        assert _is_context_overflow(e), f"should surface the overflow, got: {e}"
        print("[giveup] surfaced overflow after bounded retries:", prov.overflows, "tries")
        return
    raise AssertionError("expected a context-overflow error to propagate")


def main() -> None:
    test_detection()
    test_compaction_helpers()
    test_autocompact_recovers()
    test_gives_up_when_impossible()
    print("\nAUTO-COMPACTION TESTS PASSED")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# OMP-port engines (services.agent.compaction) — pure / offline
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
    assert "artifact://" in auto["regions"][0].get("placeholder", "")

    # skill:// read protected even aggressive
    skill_chat = [
        _assistant_call("s1", "read", {"path": "skill://edit-part"}),
        _tool("s1", "SKILLDOC" * 2000, name="read"),
        _user("go"),
    ]
    for i in range(50):
        skill_chat.append(_user("pad " + "p" * 100))
    sk = compact.shake_messages(skill_chat, aggressive=True)
    assert sk["placeholders"] == 0
    assert "SKILLDOC" in str(sk["messages"][1].get("content"))


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


def test_strategy_config_defaults() -> None:
    assert agent_config.get_compaction_strategy({}) == "off"
    assert agent_config.get_compaction_strategy(
        {"agent": {"compaction_strategy": "snapcompact"}}
    ) == "snapcompact"
    assert agent_config.get_compaction_strategy(
        {"agent": {"compaction_strategy": "nope"}}
    ) == "off"
    cc = agent_config.get_compaction_config({})
    assert cc["supersede_reads"] is True
    assert cc["drop_useless"] is True
    assert cc["keep_recent_tokens"] == 20_000
    assert agent_config.prewalk_enabled({}) is False
    assert agent_config.get_prewalk_target({}) == "@smol"
    assert agent_config.handoff_save_to_disk({}) is False
    assert agent_config.prewalk_enabled({"agent": {"prewalk": True}}) is True
    assert agent_config.get_prewalk_target(
        {"agent": {"prewalk_target": "@fast"}}
    ) == "@fast"
    assert agent_config.handoff_save_to_disk(
        {"agent": {"handoff_save_to_disk": True}}
    ) is True
