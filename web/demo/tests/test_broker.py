"""Provider broker: allowlist, no free-model quota, OpenRouter's daily allowance, NVIDIA routing,
key isolation, streaming, catalogue."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
from pathlib import Path

import httpx
import pytest

from demo.broker import ProviderBroker, UpstreamAllowance, Usage, sanitize_chat_payload, usage_total

from .support import FakeClock, make_cfg

OWNER_KEY = "sk-or-owner-key"
NVIDIA_KEY = "nvapi-owner-key"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
ULTRA_NVIDIA = "nvidia/nemotron-3-ultra-550b-a55b"
GEMMA = "google/gemma-4-31b-it:free"
USAGE_15 = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def run(coro):
    return asyncio.run(coro)


class Upstream:
    """A scripted OpenRouter."""

    def __init__(self) -> None:
        self.chat: list[httpx.Request] = []
        self.models_hits = 0
        self.models_status = 200
        self.models_data = [{"id": ULTRA, "created": 5, "context_length": 262144}, {"id": GEMMA},
                            {"id": "anthropic/claude-opus-4"}]
        self.respond = lambda request: httpx.Response(
            200, json={"id": "gen-1", "choices": [{"message": {"content": "ok"}}], "usage": USAGE_15})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.models_hits += 1
            return httpx.Response(self.models_status, json={"data": self.models_data})
        assert request.url.path.endswith("/chat/completions") and request.method == "POST"
        self.chat.append(request)
        return self.respond(request)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.chat]


def make(tmp_path, **overrides):
    clock = FakeClock()
    upstream = Upstream()
    cfg = make_cfg(tmp_path, **overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    broker = ProviderBroker(cfg, client=client, clock=clock)
    return broker, upstream, clock, client


def lane_client(lane) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=lane.app), base_url="http://lane")


def chat(model=ULTRA, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


async def post(lane, payload, **kwargs):
    async with lane_client(lane) as client:
        return await client.post("/v1/chat/completions", json=payload, **kwargs)


def error_code(response) -> str:
    return response.json()["error"]["code"]


@pytest.mark.parametrize("model", [
    "anthropic/claude-opus-4", "qwen/qwen3-coder", "google/gemma-4-31b-it:free:online",
    "GOOGLE/GEMMA-4-31B-IT:FREE", "", None, 123, ["nvidia/nemotron-3-ultra-550b-a55b:free"]])
def test_models_outside_the_allowlist_get_403_and_nothing_is_forwarded(tmp_path, model):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        payload = chat()
        payload["model"] = model
        response = await post(lane, payload)
        assert response.status_code == 403 and error_code(response) == "model_not_allowed"
        assert response.headers["x-should-retry"] == "false"
        no_model = await post(lane, {"messages": []})
        assert no_model.status_code == 403
        assert upstream.chat == [] and lane.usage == Usage()      # not even counted
        await client.aclose()
    run(main())


def test_client_credentials_are_stripped_and_the_owner_key_is_added(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        response = await post(lane, chat(), headers={
            "Authorization": "Bearer user-supplied-token", "X-Api-Key": "user-api-key",
            "Cookie": "__Host-cw_demo=abc", "HTTP-Referer": "https://evil.example", "X-Title": "evil"})
        assert response.status_code == 200
        sent = upstream.chat[0]
        assert sent.headers["authorization"] == f"Bearer {OWNER_KEY}"
        assert sent.url == "https://openrouter.ai/api/v1/chat/completions"
        assert "x-api-key" not in sent.headers and "cookie" not in sent.headers
        assert sent.headers["http-referer"] == "https://app.chudware.com" and sent.headers["x-title"] == "Chudware demo"
        blob = repr(dict(sent.headers)) + sent.content.decode()
        assert "user-supplied-token" not in blob and "user-api-key" not in blob
        await client.aclose()
    run(main())


def test_paid_routing_fields_are_stripped_and_output_is_capped(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        await post(lane, chat(models=["anthropic/claude-opus-4"], route="fallback",
                              provider={"order": ["x"]}, plugins=[{"id": "web"}], transforms=["middle-out"],
                              preset="p", web_search_options={}, max_tokens=999_999, temperature=0.2))
        await post(lane, chat(max_completion_tokens=10))
        await post(lane, chat())
        await post(lane, chat(stream=True, stream_options={"x": 1}))
        first, second, third, fourth = upstream.bodies()
        for gone in ("models", "route", "provider", "plugins", "transforms", "preset", "web_search_options"):
            assert gone not in first
        assert first["model"] == ULTRA and first["temperature"] == 0.2
        assert first["max_tokens"] == 16384
        assert second["max_completion_tokens"] == 10 and "max_tokens" not in second
        assert third["max_tokens"] == 16384
        assert fourth["stream_options"] == {"x": 1, "include_usage": True}
        await client.aclose()
    run(main())


def test_sanitize_rejects_malformed_payloads():
    assert sanitize_chat_payload({"messages": "x"}, 100) is None
    assert sanitize_chat_payload({"messages": [], "stream": "yes"}, 100) is None
    assert sanitize_chat_payload({"messages": [], "max_tokens": "5"}, 100) is None
    assert sanitize_chat_payload({"messages": [], "max_tokens": True}, 100) is None
    assert sanitize_chat_payload({"messages": [], "max_tokens": 0}, 100)["max_tokens"] == 100


def test_usage_total_reads_total_or_the_sum_of_parts():
    assert usage_total({"usage": {"total_tokens": 7}}) == 7
    assert usage_total({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == 7
    assert usage_total({"usage": {"total_tokens": "7"}}) is None
    assert usage_total({"usage": None}) is None and usage_total([]) is None


def test_free_models_carry_no_session_or_daily_quota(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        usage_path = tmp_path / "usage.json"
        lane = broker.make_lane("a" * 24, Usage(), usage_path)
        for _ in range(120):
            assert (await post(lane, chat())).status_code == 200
        assert len(upstream.chat) == 120
        # Still counted, for the logs only.
        assert json.loads(usage_path.read_text()) == {"requests": 120, "tokens": 1800, "claude_micros": 0}
        assert Usage.load(usage_path) == Usage(120, 1800)
        assert (usage_path.stat().st_mode & 0o777) == 0o600
        assert broker.free_ai_available() is True
        await client.aclose()
    run(main())


def test_openrouter_daily_exhaustion_is_shared_persisted_and_ends_at_utc_midnight(tmp_path):
    async def main():
        path = tmp_path / "budget.json"
        clock = FakeClock((19_675 + 1) * 86400 - 10)               # ten seconds before UTC midnight
        upstream = Upstream()
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        broker = ProviderBroker(make_cfg(tmp_path), client=client, budget_path=path, clock=clock)
        upstream.respond = lambda r: httpx.Response(429, json={"error": {
            "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock.", "code": 429}})
        a, b = broker.make_lane("a" * 24, Usage()), broker.make_lane("b" * 24, Usage())
        refused = await post(a, chat())
        assert refused.status_code == 429 and error_code(refused) == "demo_ai_daily_limit"
        assert refused.headers["x-should-retry"] == "false"
        assert refused.json()["error"]["message"].startswith("OpenRouter's free allowance for today is used up")
        assert broker.free_ai_available() is False
        upstream.respond = lambda r: httpx.Response(200, json={"usage": USAGE_15})
        again = await post(b, chat())                          # no more upstream calls today
        assert again.status_code == 429 and error_code(again) == "demo_ai_daily_limit"
        assert len(upstream.chat) == 1

        # A restart on the same day keeps it; after midnight it is gone.
        assert UpstreamAllowance(path, clock).available() is False
        clock.advance(11)
        assert UpstreamAllowance(path, clock).available() is True
        assert broker.free_ai_available() is True
        assert (await post(a, chat())).status_code == 200
        await client.aclose()
    run(main())


def test_upstream_per_minute_limit_is_not_reported_as_the_daily_limit(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.respond = lambda r: httpx.Response(429, json={"error": {
            "message": "Rate limit exceeded: free-models-per-min.", "code": 429}})
        lane = broker.make_lane("a" * 24, Usage())
        busy = await post(lane, chat())
        assert busy.status_code == 429 and error_code(busy) == "demo_ai_rate_limited"
        assert busy.headers["retry-after"] == "30"
        assert "x-should-retry" not in busy.headers                    # transient: retrying is right
        assert broker.free_ai_available() is True
        await client.aclose()
    run(main())


# --- NVIDIA ---------------------------------------------------------------------------


def test_with_an_nvidia_key_nemotron_is_listed_and_served_under_nvidias_id(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, nvidia_key=NVIDIA_KEY)
        lane = broker.make_lane("a" * 24, Usage())
        upstream.models_data[0]["name"] = "NVIDIA: Nemotron 3 Ultra (free)"
        async with lane_client(lane) as c:
            data = (await c.get("/v1/models")).json()["data"]
        listed = {m["id"]: m for m in data}
        assert ULTRA not in listed and listed[ULTRA_NVIDIA]["served_by"] == "nvidia"
        assert listed[ULTRA_NVIDIA]["name"] == "NVIDIA: Nemotron 3 Ultra"
        assert listed[ULTRA_NVIDIA]["context_length"] == 262144     # OpenRouter's metadata
        assert listed[GEMMA]["served_by"] == "openrouter"

        response = await post(lane, chat(ULTRA_NVIDIA), headers={"Authorization": "Bearer visitor"})
        assert response.status_code == 200
        sent = upstream.chat[-1]
        assert sent.url == "https://integrate.api.nvidia.com/v1/chat/completions"
        assert sent.headers["authorization"] == f"Bearer {NVIDIA_KEY}"
        assert "http-referer" not in sent.headers and "x-title" not in sent.headers
        assert json.loads(sent.content)["model"] == ULTRA_NVIDIA
        # The OpenRouter id of a model NVIDIA serves is not a second route to it.
        assert (await post(lane, chat(ULTRA))).status_code == 403
        assert (await post(lane, chat(GEMMA))).status_code == 200
        assert upstream.chat[-1].url == "https://openrouter.ai/api/v1/chat/completions"
        await client.aclose()
    run(main())


def test_nvidia_is_untouched_by_openrouters_daily_allowance_and_its_429_is_transient(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, nvidia_key=NVIDIA_KEY)
        lane = broker.make_lane("a" * 24, Usage())
        broker.allowance.mark_exhausted()
        assert broker.free_ai_available() is True
        assert (await post(lane, chat(ULTRA_NVIDIA))).status_code == 200
        assert error_code(await post(lane, chat(GEMMA))) == "demo_ai_daily_limit"
        upstream.respond = lambda r: httpx.Response(429, json={"error": "Too many requests per day"})
        busy = await post(lane, chat(ULTRA_NVIDIA))
        assert busy.status_code == 429 and error_code(busy) == "demo_ai_rate_limited"
        assert busy.headers["retry-after"] == "30" and "x-should-retry" not in busy.headers
        await client.aclose()
    run(main())


def test_an_nvidia_key_alone_serves_only_the_models_nvidia_has(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, openrouter_key="", nvidia_key=NVIDIA_KEY)
        lane = broker.make_lane("a" * 24, Usage())
        async with lane_client(lane) as c:
            ids = [m["id"] for m in (await c.get("/v1/models")).json()["data"]]
        assert ids == [ULTRA_NVIDIA, "nvidia/nemotron-3.5-super-vl-120b-a12b",
                       "mistralai/mistral-large-4-1t-a52b", "minimaxai/minimax-m3.1-flash"]
        assert (await post(lane, chat(GEMMA))).status_code == 403
        assert (await post(lane, chat(ULTRA_NVIDIA))).status_code == 200
        await client.aclose()
    run(main())


def test_nvidia_only_models_follow_the_mapped_ones_with_their_names(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, nvidia_key=NVIDIA_KEY,
                                           nvidia_models=("mistralai/mistral-large-4-1t-a52b",))
        lane = broker.make_lane("a" * 24, Usage())
        async with lane_client(lane) as c:
            data = (await c.get("/v1/models")).json()["data"]
        assert data[-1] == {"id": "mistralai/mistral-large-4-1t-a52b", "object": "model", "created": 0,
                            "owned_by": "mistralai", "served_by": "nvidia", "name": "Mistral Large 4"}
        assert (await post(lane, chat("mistralai/mistral-large-4-1t-a52b"))).status_code == 200
        sent = upstream.chat[-1]
        assert sent.url == "https://integrate.api.nvidia.com/v1/chat/completions"
        assert json.loads(sent.content)["model"] == "mistralai/mistral-large-4-1t-a52b"
        assert (await post(lane, chat("minimaxai/minimax-m3.1-flash"))).status_code == 403   # not configured
        await client.aclose()
    run(main())


def test_nvidia_only_models_need_the_nvidia_key(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        assert (await post(lane, chat("mistralai/mistral-large-4-1t-a52b"))).status_code == 403
        assert upstream.chat == []
        await client.aclose()
    run(main())


@pytest.mark.parametrize("status", [401, 402, 403])
def test_a_refused_owner_key_is_reported_as_ai_unavailable(tmp_path, status):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.respond = lambda r: httpx.Response(status, json={"error": {"message": "key detail"}})
        response = await post(broker.make_lane("a" * 24, Usage()), chat())
        assert response.status_code == 503 and error_code(response) == "ai_unavailable"
        assert "key detail" not in response.text
        await client.aclose()
    run(main())


def test_other_upstream_errors_pass_through_and_network_errors_are_502(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        upstream.respond = lambda r: httpx.Response(400, json={"error": {"message": "bad param"}})
        bad = await post(lane, chat())
        assert bad.status_code == 400 and bad.json()["error"]["message"] == "bad param"

        def boom(request):
            raise httpx.ConnectError("refused")
        upstream.respond = boom
        down = await post(lane, chat())
        assert down.status_code == 502 and error_code(down) == "upstream_unavailable"
        await client.aclose()
    run(main())


def test_missing_key_answers_503_and_touches_nothing(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, openrouter_key="")
        lane = broker.make_lane("a" * 24, Usage())
        chat_response = await post(lane, chat())
        assert chat_response.status_code == 503 and error_code(chat_response) == "ai_unavailable"
        assert chat_response.headers["x-should-retry"] == "false"
        async with lane_client(lane) as c:
            models = await c.get("/v1/models")
        assert models.status_code == 503 and error_code(models) == "ai_unavailable"
        assert upstream.chat == [] and upstream.models_hits == 0 and lane.usage == Usage()
        assert broker.free_ai_available() is False
        await client.aclose()
    run(main())


SSE = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
       b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}\n\n'
       b"data: [DONE]\n\n")


def test_streaming_is_passed_through_and_the_final_usage_chunk_is_charged(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.respond = lambda r: httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        lane = broker.make_lane("a" * 24, Usage())
        response = await post(lane, chat(stream=True))
        assert response.status_code == 200 and response.content == SSE
        assert response.headers["content-type"].startswith("text/event-stream")
        assert lane.usage == Usage(1, 120)
        assert upstream.bodies()[0]["stream_options"]["include_usage"] is True
        await client.aclose()
    run(main())


def test_a_stream_without_usage_is_charged_a_size_estimate(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        body = b'data: {"choices":[{"delta":{"content":"' + b"x" * 4000 + b'"}}]}\n\ndata: [DONE]\n\n'
        upstream.respond = lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        lane = broker.make_lane("a" * 24, Usage())
        await post(lane, chat(stream=True))
        assert lane.usage.tokens >= 1000
        await client.aclose()
    run(main())


def test_models_lists_only_allowlisted_models_that_openrouter_still_has(tmp_path):
    async def main():
        broker, upstream, clock, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        async with lane_client(lane) as c:
            first = (await c.get("/v1/models")).json()
            assert first["object"] == "list"
            assert [m["id"] for m in first["data"]] == [ULTRA, GEMMA]     # retired / paid ids vanish
            assert first["data"][0]["context_length"] == 262144
            await c.get("/v1/models")
            assert upstream.models_hits == 1                               # cached
            clock.advance(601)
            upstream.models_data = [{"id": GEMMA}]
            assert [m["id"] for m in (await c.get("/v1/models")).json()["data"]] == [GEMMA]
            assert upstream.models_hits == 2
            clock.advance(601)
            upstream.models_status = 500                                   # outage: keep the last list
            assert [m["id"] for m in (await c.get("/v1/models")).json()["data"]] == [GEMMA]
        await client.aclose()
    run(main())


def test_models_carry_openrouter_metadata_but_nothing_else(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.models_data = [{
            "id": ULTRA, "name": "NVIDIA: Nemotron Ultra (free)", "created": 7, "context_length": 262144,
            "top_provider": {"context_length": 262144, "max_completion_tokens": 65536, "is_moderated": True},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"],
                             "modality": "text+image->text", "tokenizer": "Other", "instruct_type": None},
            "supported_parameters": ["tools", "tool_choice", "max_tokens"],
            "pricing": {"prompt": "0", "completion": "0"}, "description": "long text"}]
        async with lane_client(broker.make_lane("a" * 24, Usage())) as c:
            (entry,) = (await c.get("/v1/models")).json()["data"]
        assert entry == {
            "id": ULTRA, "object": "model", "created": 7, "owned_by": "nvidia", "served_by": "openrouter",
            "name": "NVIDIA: Nemotron Ultra (free)", "context_length": 262144,
            "top_provider": {"context_length": 262144, "max_completion_tokens": 65536},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"],
                             "modality": "text+image->text", "tokenizer": "Other"},
            "supported_parameters": ["tools", "tool_choice", "max_tokens"]}
        await client.aclose()
    run(main())


def test_omp_model_discovery_probes_are_answered_locally_and_uncounted(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("a" * 24, Usage())
        async with lane_client(lane) as c:
            for path in ("/v1/images/models", "/v1/embeddings/models", "/v1/videos/models"):
                response = await c.get(path, headers={"Authorization": "Bearer chudware-demo-lane"})
                assert response.status_code == 404 and error_code(response) == "not_found", path
            for query in ("output_modalities=decisions", "output_modalities=rerank"):
                response = await c.get("/v1/models?" + query)
                assert response.status_code == 200 and response.json() == {"object": "list", "data": []}
        assert upstream.models_hits == 0 and upstream.chat == []       # nothing forwarded upstream
        assert lane.usage == Usage()                                   # nothing counted
        await client.aclose()
    run(main())


def test_models_falls_back_to_the_allowlist_when_the_catalogue_is_unreachable_at_first(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        upstream.models_status = 503
        async with lane_client(broker.make_lane("a" * 24, Usage())) as c:
            ids = [m["id"] for m in (await c.get("/v1/models")).json()["data"]]
        assert ids == list(broker.cfg.ai_models)
        await client.aclose()
    run(main())


def test_method_path_and_body_refusals(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path, ai_max_body_bytes=2000)
        lane = broker.make_lane("a" * 24, Usage())
        async with lane_client(lane) as c:
            wrong = await c.get("/v1/chat/completions")
            assert wrong.status_code == 405 and wrong.headers["allow"] == "POST"
            assert error_code(wrong) == "method_not_allowed"
            assert (await c.put("/v1/chat/completions", json=chat())).status_code == 405
            posted = await c.post("/v1/models")
            assert posted.status_code == 405 and posted.headers["allow"] == "GET, HEAD"
            for path in ("/", "/v1", "/v1/completions", "/v1/embeddings", "/v1/chat/completions/x", "/admin"):
                assert (await c.get(path)).status_code == 404
                assert (await c.post(path, json=chat())).status_code in (404, 405)
            assert (await c.post("/v1/chat/completions", content=b"{not json")).status_code == 400
            assert (await c.post("/v1/chat/completions", json=[chat()])).status_code == 400
            assert (await c.post("/v1/chat/completions", json={"model": ULTRA})).status_code == 400
            assert (await c.post("/v1/chat/completions", json=chat(stream="yes"))).status_code == 400
            big = await c.post("/v1/chat/completions", json=chat(pad="x" * 5000))
            assert big.status_code == 413 and error_code(big) == "request_too_large"
        assert upstream.chat == [] and lane.usage == Usage()
        await client.aclose()
    run(main())


def test_prompts_and_keys_are_never_logged_but_counts_are(tmp_path, caplog):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        lane = broker.make_lane("abcdef0123456789abcdef01", Usage())
        with caplog.at_level(logging.DEBUG):
            await post(lane, chat(messages=[{"role": "user", "content": "UNIQUE-PROMPT-TEXT-123"}]),
                       headers={"Authorization": "Bearer user-token-xyz"})
            await post(lane, chat(stream=True))
            await post(lane, {"model": "evil/model", "messages": [{"role": "user", "content": "UNIQUE-PROMPT-TEXT-123"}]})
        await client.aclose()
    run(main())
    text = caplog.text
    assert "UNIQUE-PROMPT-TEXT-123" not in text and OWNER_KEY not in text and "user-token-xyz" not in text
    assert "sid=abcdef01" in text and "req=1 " in text


def test_a_real_unix_socket_lane_round_trips_and_is_cleaned_up(tmp_path):
    async def main():
        broker, upstream, _, client = make(tmp_path)
        with tempfile.TemporaryDirectory(prefix="cwd-", dir="/tmp") as root:
            run_dir = Path(root) / "run"
            await broker.open_session("a" * 24, run_dir, Usage())
            sock = run_dir / "provider.sock"
            assert stat.S_IMODE(os.stat(run_dir).st_mode) == 0o711    # container user may traverse, never write
            assert stat.S_ISSOCK(os.stat(sock).st_mode) and stat.S_IMODE(os.stat(sock).st_mode) == 0o666
            transport = httpx.AsyncHTTPTransport(uds=str(sock))
            async with httpx.AsyncClient(transport=transport, base_url="http://provider") as c:
                models = await c.get("/v1/models")
                assert models.status_code == 200 and len(models.json()["data"]) == 2
                completion = await c.post("/v1/chat/completions", json=chat(), headers={"Authorization": "Bearer x"})
                assert completion.status_code == 200
            assert upstream.chat[0].headers["authorization"] == f"Bearer {OWNER_KEY}"
            assert broker.lanes["a" * 24].usage == Usage(1, 15)
            await broker.close_session("a" * 24)
            assert not sock.exists()
            # Re-opening after a front-door restart replaces a stale socket file.
            sock.touch()
            await broker.open_session("a" * 24, run_dir, Usage())
            assert stat.S_ISSOCK(os.stat(sock).st_mode)
            await broker.aclose()
        await client.aclose()
    run(main())
