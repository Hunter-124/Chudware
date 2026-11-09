"""Claude lane of the provider broker: allowlist, paid-feature stripping, USD accounting, caps."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from demo.broker import MICROS, ClaudeLedger, ProviderBroker, Usage

from .support import FakeClock, make_cfg

OWNER = "sk-ant-owner-key"
SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
# Sonnet 5.5: 1000 x $2 + 2000 x $2.5 (5 min write) + 1000 x $4 (1 h write) + 20000 x $0.10 + 500 x $10
USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 3000,
         "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000},
         "output_tokens": 500}
SONNET_COST = 18_000
# The advance charge for ask() on Sonnet: 100 counted tokens x $4 (1 h write) + 1000 x $10.
SONNET_HOLD = 10_400


def run(coro):
    return asyncio.run(coro)


class Claude:
    """A scripted Claude API (plus an OpenRouter model list for the shared /v1/models)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []      # /v1/messages only
        self.counts: list[httpx.Request] = []        # /v1/messages/count_tokens
        self.respond = lambda request: httpx.Response(200, json={"id": "msg_1", "usage": USAGE})
        self.count = lambda request: httpx.Response(200, json={"input_tokens": 100})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json={"data": [{"id": ULTRA}]})
        assert request.url.host == "api.anthropic.com" and request.method == "POST"
        if request.url.path == "/v1/messages/count_tokens":
            self.counts.append(request)
            return self.count(request)
        assert request.url.path == "/v1/messages"
        self.requests.append(request)
        return self.respond(request)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def make(tmp_path, clock=None, **overrides):
    clock = clock or FakeClock()
    upstream = Claude()
    cfg = make_cfg(tmp_path, **{"anthropic_key": OWNER, **overrides})
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    broker = ProviderBroker(cfg, client=client, claude_path=tmp_path / "claude.json", clock=clock)
    return broker, upstream, clock, client


def ask(model=SONNET, **extra):
    return {"model": model, "max_tokens": 1000, "messages": [{"role": "user", "content": "hello"}], **extra}


async def post(lane, payload, path="/v1/messages", **kwargs):
    transport = httpx.ASGITransport(app=lane.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://lane") as c:
        return await c.post(path, json=payload, **kwargs)


def error_type(response) -> str:
    body = response.json()
    assert body["type"] == "error"
    return body["error"]["type"]


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-mythos-5", "claude-opus-5", "claude-sonnet-4-6",
                                   ULTRA, "", None, ["claude-opus-5-5"]])
def test_only_allowlisted_claude_models_reach_the_claude_api(tmp_path, model):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        for path in ("/v1/messages", "/v1/messages/count_tokens"):
            refused = await post(lane, ask(model), path)
            assert refused.status_code == 403 and error_type(refused) == "model_not_allowed"
            assert refused.headers["x-should-retry"] == "false"
        assert upstream.requests == upstream.counts == [] and lane.usage == Usage()
        assert broker.claude.day_micros == 0
        await client.aclose()
    run(main())


def test_claude_models_are_refused_on_the_free_route(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        refused = await post(lane, {"model": SONNET, "messages": []}, "/v1/chat/completions")
        assert refused.status_code == 403 and upstream.requests == upstream.counts == []
        await client.aclose()
    run(main())


def test_owner_key_replaces_client_credentials_and_only_plain_turn_fields_are_forwarded(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        tools = [{"name": "cad", "input_schema": {"type": "object"}},
                 {"type": "custom", "name": "probe", "input_schema": {"type": "object"}},
                 {"type": "web_search_20250305", "name": "web_search"},
                 {"type": "code_execution_20250825", "name": "code_execution"}]
        response = await post(lane, ask(
            tools=tools, mcp_servers=[{"url": "https://x"}], container="c", service_tier="priority",
            inference_geo="us", speed="fast", compaction={"type": "summarize"},
            fallbacks=[{"model": "claude-fable-5", "speed": "fast"}], max_tokens=999_999,
            context_management={"edits": [{"type": "compact_20260112"}, {"type": "clear_tool_uses_20250919"}]},
            thinking={"type": "enabled", "budget_tokens": 900_000}, cache_control={"type": "ephemeral"}),
            headers={
                "x-api-key": "visitor-key", "Authorization": "Bearer visitor-token",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "interleaved-thinking-2025-05-14, web-search-2025-03-05,mcp-client-2025-04-04,"
                                  "context-1m-2025-08-07,fast-mode-2026-02-01,compact-2026-09-04,"
                                  "server-side-fallback-2026-06-01,EFFORT-2025-11-24,effort-2025-11-24"})
        assert response.status_code == 200
        (sent,) = upstream.requests
        assert sent.url == "https://api.anthropic.com/v1/messages"
        assert sent.headers["x-api-key"] == OWNER and "authorization" not in sent.headers
        assert sent.headers["anthropic-version"] == "2023-06-01"
        assert sent.headers["anthropic-beta"] == "interleaved-thinking-2025-05-14,effort-2025-11-24"
        assert "visitor-key" not in repr(dict(sent.headers)) + sent.content.decode()
        body = upstream.bodies()[0]
        assert [t["name"] for t in body["tools"]] == ["cad", "probe"]
        for gone in ("mcp_servers", "container", "service_tier", "inference_geo", "speed", "compaction", "fallbacks"):
            assert gone not in body
        assert body["context_management"] == {"edits": [{"type": "clear_tool_uses_20250919"}]}
        assert body["cache_control"] == {"type": "ephemeral"}
        assert body["max_tokens"] == 32_000 and body["thinking"] == {"type": "adaptive"}
        # The pre-flight count carries only the prompt-shaping fields.
        assert set(json.loads(upstream.counts[0].content)) == {"model", "messages", "tools", "thinking"}
        await client.aclose()
    run(main())


def test_a_malformed_version_header_falls_back_to_the_stable_one(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        await post(broker.make_lane("a" * 24, Usage()), ask(), headers={"anthropic-version": "2023-06-01\r\nx: y"})
        assert upstream.requests[0].headers["anthropic-version"] == "2023-06-01"
        await client.aclose()
    run(main())


def test_reported_usage_is_charged_at_the_model_card_to_session_client_day_and_month(tmp_path):
    async def main():
        broker, upstream, clock, client = make(tmp_path)
        usage_path = tmp_path / "usage.json"
        lane = broker.make_lane("a" * 24, Usage(), usage_path, client="net-a")
        assert (await post(lane, ask())).status_code == 200
        assert lane.usage.claude_micros == SONNET_COST
        assert json.loads(usage_path.read_text())["claude_micros"] == SONNET_COST
        ledger = broker.claude
        assert (ledger.day_micros, ledger.month_micros, ledger.clients) == (SONNET_COST, SONNET_COST,
                                                                            {"net-a": SONNET_COST})
        # A restart reads the same totals back.
        again = ClaudeLedger(broker.cfg, tmp_path / "claude.json", clock)
        assert (again.day_micros, again.month_micros, again.clients) == (SONNET_COST, SONNET_COST,
                                                                         {"net-a": SONNET_COST})
        await client.aclose()
    run(main())


@pytest.mark.parametrize("content", [
    [{"type": "image", "source": {"type": "url", "url": "https://visitor.example/huge.png"}}],
    [{"type": "document", "source": {"type": "file", "file_id": "file_1"}}],
    [{"type": "tool_result", "tool_use_id": "t", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://visitor.example/x.png"}}]}],
    [{"type": "document", "source": {"type": "content", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://visitor.example/y.png"}}]}}],
])
def test_media_fetched_by_url_or_file_id_is_refused_before_counting(tmp_path, content):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        refused = await post(lane, ask(messages=[{"role": "user", "content": content}]))
        assert refused.status_code == 400 and error_type(refused) == "invalid_request"
        assert upstream.requests == upstream.counts == []
        inline = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]
        assert (await post(lane, ask(messages=[{"role": "user", "content": inline}]))).status_code == 200
        await client.aclose()
    run(main())


def test_server_side_iterations_are_billed_even_when_the_top_level_reports_zero(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.respond = lambda r: httpx.Response(200, json={"usage": {
            "input_tokens": 0, "output_tokens": 0,
            "iterations": [{"input_tokens": 50_000, "output_tokens": 1000}, {"input_tokens": 2000, "output_tokens": 10}]}})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        assert (await post(lane, ask())).status_code == 200
        assert lane.usage.claude_micros == 52_000 * 2 + 1010 * 10
        await client.aclose()
    run(main())


@pytest.mark.parametrize("fresh,expected", [(1000, 1000 * 0.10 + 1000 * 0.50),          # <= 100k prompt
                                             (120_000, 120_000 * 0.50 + 1000 * 2.50)])  # > 100k prompt
def test_haiku_is_billed_at_its_long_prompt_card_only_above_100k_tokens(tmp_path, fresh, expected):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.respond = lambda r: httpx.Response(200, json={"usage": {"input_tokens": fresh, "output_tokens": 1000}})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        assert (await post(lane, ask(HAIKU))).status_code == 200
        assert lane.usage.claude_micros == expected
        await client.aclose()
    run(main())


def sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"
                    for e in events)


def test_a_streamed_reply_is_settled_from_message_start_and_message_delta(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        start = {k: v for k, v in USAGE.items() if k != "output_tokens"} | {"output_tokens": 1}
        stream = sse({"type": "message_start", "message": {"id": "m", "usage": start}},
                     {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                     {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 500}},
                     {"type": "message_stop"})
        upstream.respond = lambda r: httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        response = await post(lane, ask(stream=True))
        assert response.status_code == 200 and response.content == stream
        assert lane.usage.claude_micros == SONNET_COST and broker.claude.day_micros == SONNET_COST
        await client.aclose()
    run(main())


def test_a_stream_cut_before_its_final_usage_keeps_the_advance_charge(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        stream = sse({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}})
        upstream.respond = lambda r: httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        await post(lane, ask(stream=True))
        assert lane.usage.claude_micros == SONNET_HOLD
        await client.aclose()
    run(main())


def test_a_read_timeout_keeps_the_advance_charge_but_a_refused_connection_does_not(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)
        upstream.respond = timeout
        assert (await post(lane, ask())).status_code == 502
        assert lane.usage.claude_micros == SONNET_HOLD             # upstream may still be billing

        def refused(request):
            raise httpx.ConnectError("refused", request=request)
        upstream.respond = refused
        assert (await post(lane, ask())).status_code == 502
        assert lane.usage.claude_micros == SONNET_HOLD             # never left: nothing more kept
        await client.aclose()
    run(main())


def test_the_advance_charge_counts_tokens_upstream_so_small_bodies_cannot_hide_big_prompts(tmp_path):
    async def main():
        # $5 session cap; the count says this tiny body is a 1M-token prompt (URL documents, images).
        broker, upstream, _, client = make(tmp_path, claude_session_usd=5)
        upstream.count = lambda r: httpx.Response(200, json={"input_tokens": 1_000_000})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        refused = await post(lane, ask(OPUS, max_tokens=1))        # 1M x $8 (1 h write) = $8 > $5
        assert refused.status_code == 429 and error_type(refused) == "demo_quota_exhausted"
        assert upstream.requests == [] and lane.usage.claude_micros == 0
        await client.aclose()
    run(main())


def test_a_failed_count_is_reported_and_nothing_is_charged_or_sent(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.count = lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={"type": "error"})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        busy = await post(lane, ask())
        assert busy.status_code == 429 and error_type(busy) == "demo_ai_rate_limited"
        assert busy.headers["retry-after"] == "7"
        assert upstream.requests == [] and lane.usage.claude_micros == 0 and broker.claude.day_micros == 0
        await client.aclose()
    run(main())


def test_session_cap_counts_the_request_being_admitted(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, claude_session_usd=0.03)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        assert (await post(lane, ask())).status_code == 200        # 0 + 0.0104 <= 0.03
        assert (await post(lane, ask())).status_code == 200        # 0.018 + 0.0104 <= 0.03
        refused = await post(lane, ask())                          # 0.036 + 0.0104 > 0.03
        assert refused.status_code == 429 and error_type(refused) == "demo_quota_exhausted"
        assert refused.headers["x-should-retry"] == "false"
        assert "session" in refused.json()["error"]["message"] and len(upstream.requests) == 2
        # The same visitor's next session (a new sid) starts with its own session allowance.
        assert (await post(broker.make_lane("b" * 24, Usage(), client="net-a"), ask())).status_code == 200
        await client.aclose()
    run(main())


def test_client_cap_spans_that_clients_sessions_and_spares_other_clients(tmp_path):
    async def main():
        broker, upstream, clock, client = make(tmp_path, claude_client_daily_usd=0.04)
        first, second, third = (broker.make_lane(c * 24, Usage(), client="net-a") for c in "abc")
        assert (await post(first, ask())).status_code == 200      # net-a 0.018
        assert (await post(second, ask())).status_code == 200     # 0.018 + 0.0104 <= 0.04: now 0.036
        refused = await post(third, ask())
        assert refused.status_code == 429 and error_type(refused) == "demo_quota_exhausted"
        assert "today" in refused.json()["error"]["message"]
        assert (await post(broker.make_lane("d" * 24, Usage(), client="net-b"), ask())).status_code == 200
        clock.advance(86400)                                      # next UTC day: a fresh client allowance
        assert (await post(third, ask())).status_code == 200
        await client.aclose()
    run(main())


def test_daily_and_monthly_caps_are_shared_and_reset_on_their_own_boundaries(tmp_path):
    async def main():
        broker, upstream, clock, client = make(tmp_path, claude_daily_usd=0.04, claude_monthly_usd=0.06)
        lanes = [broker.make_lane(c * 24, Usage(), client=f"net-{c}") for c in "abcdef"]
        assert (await post(lanes[0], ask())).status_code == 200
        assert (await post(lanes[1], ask())).status_code == 200   # 0.018 + 0.0104 <= 0.04
        day = await post(lanes[2], ask())                          # 0.036 + 0.0104 > 0.04
        assert day.status_code == 429 and error_type(day) == "demo_ai_daily_limit"
        clock.advance(86400)
        assert (await post(lanes[3], ask())).status_code == 200   # month 0.036 + 0.0104 <= 0.06
        month = await post(lanes[4], ask())                        # 0.054 + 0.0104 > 0.06
        assert month.status_code == 429 and "month" in month.json()["error"]["message"]
        clock.advance(31 * 86400)                                  # a new calendar month
        assert (await post(lanes[5], ask())).status_code == 200
        await client.aclose()
    run(main())


def test_a_correction_settled_after_midnight_does_not_lower_the_new_day(tmp_path):
    cfg = make_cfg(tmp_path, anthropic_key=OWNER)
    clock = FakeClock((19_675 + 1) * 86400 - 10)                 # ten seconds before UTC midnight
    ledger = ClaudeLedger(cfg, None, clock)
    held = ledger.period()
    ledger.charge("net-a", 5_000_000, held)                      # a $5 advance charge on day D
    clock.advance(20)
    ledger.charge("net-b", 1_000_000)                            # $1 really spent on day D+1
    ledger.charge("net-a", -4_990_000, held)                     # day D's request settles at $0.01
    assert ledger.day_micros == 1_000_000 and ledger.clients == {"net-b": 1_000_000}
    assert ledger.month_micros == 1_010_000                     # same calendar month: corrected


def test_upstream_rate_limit_is_retryable_and_refusals_are_not_billed(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        upstream.respond = lambda r: httpx.Response(429, headers={"retry-after": "12"},
                                                    json={"type": "error", "error": {"type": "rate_limit_error"}})
        busy = await post(lane, ask())
        assert busy.status_code == 429 and error_type(busy) == "demo_ai_rate_limited"
        assert busy.headers["retry-after"] == "12" and "x-should-retry" not in busy.headers
        for status, detail in ((401, b"invalid x-api-key"), (400, b"Your credit balance is too low")):
            upstream.respond = lambda r, s=status, d=detail: httpx.Response(s, content=d)
            refused = await post(lane, ask())
            assert refused.status_code == 503 and error_type(refused) == "ai_unavailable"
            assert "credit" not in refused.text and "x-api-key" not in refused.text
        assert lane.usage.claude_micros == 0 and broker.claude.day_micros == 0
        await client.aclose()
    run(main())


def test_token_counting_is_forwarded_but_never_charged(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.count = lambda r: httpx.Response(200, json={"input_tokens": 42})
        lane = broker.make_lane("a" * 24, Usage(), client="net-a")
        counted = await post(lane, ask(tools=[{"name": "cad", "input_schema": {}}], max_tokens=5),
                             "/v1/messages/count_tokens")
        assert counted.status_code == 200 and counted.json() == {"input_tokens": 42}
        assert upstream.requests == [] and "max_tokens" not in json.loads(upstream.counts[0].content)
        assert lane.usage.claude_micros == 0 and broker.claude.day_micros == 0
        await client.aclose()
    run(main())


def test_models_list_claude_first_with_its_api_family_and_card(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=broker.make_lane("a" * 24, Usage()).app),
                                     base_url="http://lane") as c:
            data = (await c.get("/v1/models")).json()["data"]
        assert [m["id"] for m in data] == [SONNET, OPUS, HAIKU, ULTRA]
        sonnet = data[0]
        assert sonnet["owned_by"] == "anthropic" and sonnet["name"] == "Claude Sonnet 5.5"
        assert float(sonnet["pricing"]["prompt"]) * MICROS == pytest.approx(2.0)
        assert float(sonnet["pricing"]["input_cache_read"]) * MICROS == pytest.approx(0.10)
        assert data[3]["owned_by"] == "nvidia" and "pricing" not in data[3]
        await client.aclose()
    run(main())


def test_without_a_claude_key_the_claude_route_is_unavailable(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, anthropic_key="")
        response = await post(broker.make_lane("a" * 24, Usage()), ask())
        assert response.status_code == 503 and error_type(response) == "ai_unavailable"
        assert upstream.requests == upstream.counts == [] and not broker.claude_available()
        await client.aclose()
    run(main())
