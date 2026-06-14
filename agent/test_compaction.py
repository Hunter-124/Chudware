"""Auto-compaction: when a provider runs out of context, the loop should shrink the history
and retry instead of dying (the local-model "request exceeds context size" failure)."""
from __future__ import annotations

from agent import loop
from agent.loop import (_apply_compaction, _is_context_overflow, _keep_recent,
                        _strip_images)
from agent.providers.base import Provider, assistant, image_part, text_part, user

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
