"""The public-demo model lane (free models and Claude): when it exists, what it lists, what it never accepts."""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.agent import demo_lane

LISTING = {"object": "list", "data": [
    {"id": "example/model-a:free", "name": "Model A (free)", "context_length": 65536,
     "top_provider": {"max_completion_tokens": 8192},
     "architecture": {"input_modalities": ["text", "image"]}},
    {"id": "example/model-b:free"},
]}

# The front door's Claude row, exactly as ProviderBroker._claude_entry shapes it.
CLAUDE_ROW = {
    "id": "claude-sonnet-5-5", "object": "model", "created": 0, "owned_by": "anthropic",
    "name": "Claude Sonnet 5.5", "context_length": 200000,
    "top_provider": {"context_length": 200000, "max_completion_tokens": 32000},
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    "supported_parameters": ["tools", "tool_choice", "max_tokens", "reasoning"],
    "pricing": {"prompt": "0.000002", "completion": "0.00001",
                "input_cache_read": "0.0000001", "input_cache_write": "0.0000025"},
}
MIXED_LISTING = {"object": "list", "data": [CLAUDE_ROW, *LISTING["data"]]}


@pytest.fixture
def lane(monkeypatch):
    """A stand-in for the front door's GET /v1/models behind the fixed loopback address."""
    state = {"status": 200, "body": json.dumps(LISTING).encode(), "hits": 0, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"] += 1
            state["paths"].append(self.path)
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(demo_lane, "LANE_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
    demo_lane.clear_cache()
    yield state
    server.shutdown()
    server.server_close()
    demo_lane.clear_cache()


@pytest.fixture
def short_dir():
    path = Path(tempfile.mkdtemp(prefix="cw-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


# --- availability -----------------------------------------------------------------------


def test_lane_exists_only_in_a_cell_of_a_demo_container_with_a_mounted_socket(short_dir, monkeypatch):
    path = short_dir / "provider.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    try:
        monkeypatch.setattr(demo_lane, "in_cell", lambda: True)
        assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "1"}, str(path)) is True
        assert demo_lane.enabled({}, str(path)) is False
        assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "0"}, str(path)) is False
        assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "true"}, str(path)) is False
        assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "1"}, str(short_dir / "absent.sock")) is False
        monkeypatch.setattr(demo_lane, "in_cell", lambda: False)
        assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "1"}, str(path)) is False
    finally:
        listener.close()


def test_a_regular_file_at_the_socket_path_is_not_a_lane(short_dir, monkeypatch):
    path = short_dir / "provider.sock"
    path.write_text("not a socket")
    monkeypatch.setattr(demo_lane, "in_cell", lambda: True)
    assert demo_lane.enabled({"CHUDWARE_WEB_DEMO": "1"}, str(path)) is False


def test_desktop_never_has_a_lane_whatever_the_environment(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.setenv("CHUDWARE_WEB_DEMO", "1")
    assert demo_lane.enabled() is False


# --- the model list ---------------------------------------------------------------------


def test_models_are_exactly_what_the_lane_lists_in_its_order(lane):
    rows = demo_lane.models()
    assert [row["id"] for row in rows] == ["example/model-a:free", "example/model-b:free"]
    first = rows[0]
    assert first["provider"] == "openrouter" and first["name"] == "Model A (free)"
    assert first["contextWindow"] == 65536 and first["maxTokens"] == 8192
    assert first["input"] == ["text", "image"] and first["cost"] == {
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    assert rows[1]["name"] == "example/model-b:free" and "contextWindow" not in rows[1]
    assert lane["paths"] == ["/v1/models"]


def test_no_model_id_is_built_into_the_lane_module():
    source = Path(demo_lane.__file__).read_text(encoding="utf-8")
    assert ":free" not in source and "claude" not in source.lower()


def test_model_list_is_cached_briefly_and_refresh_bypasses_it(lane):
    demo_lane.models()
    demo_lane.models()
    assert lane["hits"] == 1
    demo_lane.models(refresh=True)
    assert lane["hits"] == 2


def test_callers_cannot_mutate_the_cached_rows(lane):
    demo_lane.models()[0]["id"] = "tampered"
    assert demo_lane.models()[0]["id"] == "example/model-a:free"


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_a_failing_lane_is_an_error_not_an_empty_picker(lane, status):
    lane["status"] = status
    with pytest.raises(demo_lane.LaneError):
        demo_lane.models()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"data": "x"}', b'{"object": "list"}'])
def test_malformed_lane_answers_are_refused(lane, body):
    lane["body"] = body
    with pytest.raises(demo_lane.LaneError):
        demo_lane.models()


def test_oversized_lane_answer_is_refused(lane):
    lane["body"] = b'{"data": [' + b'{"id": "a"},' * 200_000 + b'{"id": "b"}]}'
    with pytest.raises(demo_lane.LaneError, match="too large"):
        demo_lane.models()


def test_unreachable_lane_is_an_error(monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(demo_lane, "LANE_BASE_URL", f"http://127.0.0.1:{port}/v1")
    demo_lane.clear_cache()
    with pytest.raises(demo_lane.LaneError, match="unavailable"):
        demo_lane.models(timeout=1.0)


def test_lane_is_dialled_directly_never_through_the_egress_proxy(lane, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert demo_lane.models()


def test_hostile_rows_are_dropped_and_the_list_is_bounded():
    rows = demo_lane.parse_models({"data": [
        {"id": "../etc/passwd"}, {"id": ""}, {"id": 7}, "text", None, {"id": "bad id with spaces"},
        {"id": "x" * 500}, {"id": "ok/model:free", "name": "N" * 1000, "context_length": -1,
                            "top_provider": {"max_completion_tokens": True}},
        {"id": "ok/model:free"},
        *({"id": f"m/{n}"} for n in range(100)),
    ]})
    assert rows[0]["id"] == "ok/model:free" and len(rows[0]["name"]) == 256
    assert "contextWindow" not in rows[0] and "maxTokens" not in rows[0]
    assert len({row["id"] for row in rows}) == len(rows) == 32


# --- OMP configuration ------------------------------------------------------------------


def test_omp_config_points_openrouter_at_the_loopback_lane_with_a_placeholder_key():
    rows = demo_lane.parse_models(LISTING)
    config = demo_lane.models_config(rows)
    provider = config["providers"]["openrouter"]
    assert provider["baseUrl"] == "http://127.0.0.1:18081/v1"
    assert provider["apiKey"] == demo_lane.PLACEHOLDER_KEY == "chudware-demo-lane"
    assert provider["api"] == "openai-completions"
    assert provider["auth"] == "apiKey"
    assert all("api" not in m and "baseUrl" not in m for m in provider["models"])
    first, second = provider["models"]
    assert first["id"] == "example/model-a:free"
    assert (first["contextWindow"], first["maxTokens"]) == (65536, 8192)
    assert first["input"] == ["text", "image"]
    # A model the lane lists without limits still gets BOTH numbers: OMP reports null for an
    # unknown model and the RPC client then cannot decode set_model's reply.
    assert (second["contextWindow"], second["maxTokens"]) == (128_000, 16_384)
    assert all(m["maxTokens"] <= m["contextWindow"] for m in (first, second))
    assert json.loads(json.dumps(config)) == config  # JSON is valid YAML for models.yml


def test_omp_config_without_a_model_list_still_reaches_only_the_lane():
    provider = demo_lane.models_config(None)["providers"]["openrouter"]
    assert "models" not in provider and provider["baseUrl"] == demo_lane.LANE_BASE_URL
    assert demo_lane.models_config([])["providers"]["openrouter"].get("models") is None


def test_a_claude_row_carries_its_api_family_and_per_million_token_cost():
    claude, free, _ = demo_lane.parse_models(MIXED_LISTING)
    assert claude["id"] == "claude-sonnet-5-5" and claude["api"] == "anthropic-messages"
    assert claude["provider"] == "openrouter" and claude["name"] == "Claude Sonnet 5.5"
    assert (claude["contextWindow"], claude["maxTokens"]) == (200000, 32000)
    assert claude["input"] == ["text", "image"]
    assert claude["cost"] == {"input": 2.0, "output": 10.0, "cacheRead": 0.1, "cacheWrite": 2.5}
    assert "api" not in free and free["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def test_omp_config_routes_a_claude_row_through_the_lane_root_and_keeps_free_rows_on_chat_completions():
    config = demo_lane.models_config(demo_lane.parse_models(MIXED_LISTING))
    provider = config["providers"]["openrouter"]
    assert provider["api"] == "openai-completions" and provider["baseUrl"] == "http://127.0.0.1:18081/v1"
    assert provider["auth"] == "apiKey" and provider["apiKey"] == demo_lane.PLACEHOLDER_KEY
    claude, free, _ = provider["models"]
    assert claude["api"] == "anthropic-messages" and claude["baseUrl"] == "http://127.0.0.1:18081"
    assert claude["baseUrl"] == demo_lane.LANE_ROOT
    assert claude["cost"] == {"input": 2.0, "output": 10.0, "cacheRead": 0.1, "cacheWrite": 2.5}
    assert (claude["contextWindow"], claude["maxTokens"]) == (200000, 32000)
    assert claude["input"] == ["text", "image"]
    assert "api" not in free and "baseUrl" not in free
    assert free["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    assert json.loads(json.dumps(config)) == config


@pytest.mark.parametrize("pricing", [
    None, [], "free", {"prompt": "abc", "completion": None, "input_cache_read": [], "input_cache_write": {}},
    {"prompt": "-0.000002", "completion": "-1", "input_cache_read": "NaN", "input_cache_write": "Infinity"},
    {"prompt": "1e999999999", "completion": "1e400", "input_cache_read": True, "input_cache_write": ""},
])
def test_garbage_pricing_falls_back_to_zero_per_field(pricing):
    row = demo_lane.parse_models({"data": [{**CLAUDE_ROW, "pricing": pricing}]})[0]
    assert row["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def test_one_bad_price_does_not_discard_the_good_ones():
    pricing = {"prompt": 0.000003, "completion": "oops", "input_cache_read": "0.0000003"}
    row = demo_lane.parse_models({"data": [{**CLAUDE_ROW, "pricing": pricing}]})[0]
    assert row["cost"] == {"input": 3.0, "output": 0, "cacheRead": 0.3, "cacheWrite": 0}


def test_the_only_endpoint_in_the_module_is_the_fixed_loopback_lane():
    source = Path(demo_lane.__file__).read_text(encoding="utf-8")
    assert "openrouter.ai" not in source and "https://" not in source
    assert demo_lane.LANE_ROOT == "http://127.0.0.1:18081"
    assert demo_lane.LANE_BASE_URL == "http://127.0.0.1:18081/v1"
    assert demo_lane.LANE_SOCKET == "/run/chudware-demo/provider.sock"


# --- model pinning ----------------------------------------------------------------------


def test_a_turn_naming_nothing_gets_the_lanes_first_model(lane):
    pinned = demo_lane.pin_model({"request": "hi"})
    assert (pinned["provider"], pinned["model"]) == ("openrouter", "example/model-a:free")


def test_a_listed_model_is_accepted_as_is(lane):
    params = {"provider": " openrouter ", "model": "example/model-b:free", "request": "hi"}
    assert demo_lane.pin_model(params) == {**params, "provider": "openrouter"}


@pytest.mark.parametrize("params", [
    {"provider": "openai", "model": "gpt-test"},
    {"provider": "openrouter", "model": "not/listed:free"},
    {"provider": "openrouter"},
    {"model": "example/model-a:free"},
    {"provider": "anthropic", "model": "example/model-a:free"},
])
def test_any_other_selection_is_refused(lane, params):
    with pytest.raises(ValueError, match=demo_lane.MODEL_REFUSAL):
        demo_lane.pin_model(params)


def test_pinning_needs_a_live_nonempty_lane(lane):
    lane["body"] = b'{"data": []}'
    with pytest.raises(demo_lane.LaneError, match="no demo models"):
        demo_lane.pin_model({})


# --- provider presentation --------------------------------------------------------------


def test_providers_panel_has_one_ready_row_with_nothing_to_sign_in_to():
    row = demo_lane.provider_descriptor(4)
    assert row["id"] == "openrouter" and row["name"] == "Demo models"
    assert row["authenticated"] is True and row["available"] is True and row["authKinds"] == []
    assert row["modelsAvailable"] == 4
    assert demo_lane.provider_descriptor(0)["available"] is False


def test_auth_state_is_ready_for_the_lane_and_signed_out_for_anything_else():
    ready = demo_lane.auth_state("openrouter")
    assert ready["state"] == "configured" and ready["authenticated"] is True
    assert ready["hasApiKey"] is False and ready["accounts"] == []
    other = demo_lane.auth_state("anthropic")
    assert other["state"] == "signed_out" and other["authenticated"] is False


# --- quota refusals ---------------------------------------------------------------------


def _error(code, message="Message from the front door."):
    return "429 " + json.dumps({"error": {"code": code, "message": message}})


@pytest.mark.parametrize("code", ["demo_ai_daily_limit", "demo_quota_exhausted"])
def test_quota_refusal_surfaces_the_lanes_own_message(code):
    assert demo_lane.quota_message(_error(code, "Today's free AI budget is spent.")) == (
        "Today's free AI budget is spent.")


@pytest.mark.parametrize("code", ["demo_ai_daily_limit", "demo_quota_exhausted"])
def test_quota_refusal_without_a_message_still_reads_as_a_sentence(code):
    for raw in (f'429 {{"error":{{"code":"{code}"}}}}', f"429 {code}", f'429 {{"error": {{"code": "{code}", broken'):
        message = demo_lane.quota_message(raw)
        assert message and code not in message and message.endswith(".")


@pytest.mark.parametrize("raw", [
    None, 429, "", "429 rate limited", _error("model_not_allowed"),
    '429 {"error":{"code":"upstream_rate_limited","message":"slow down"}}',
    "demo_quota_exhausted_extra", "my_demo_ai_daily_limit",
])
def test_other_errors_are_not_quota_refusals(raw):
    assert demo_lane.quota_message(raw) is None


def test_message_text_is_bounded():
    assert len(demo_lane.quota_message(_error("demo_quota_exhausted", "x" * 5000))) == 600


def test_omp_worded_refusal_yields_the_lanes_message_even_inside_a_retry_summary():
    plain = "429 This demo session has used all of its AI requests. (type=demo_quota_exhausted)"
    assert demo_lane.quota_message(plain) == "This demo session has used all of its AI requests."
    summary = ("Provider requested 1800000ms wait, exceeds retry.maxDelayMs (300000ms). "
               "Original error: " + plain)
    assert demo_lane.quota_message(summary) == "This demo session has used all of its AI requests."
    daily = "429 Today's free AI budget is spent. (type=demo_ai_daily_limit)"
    assert demo_lane.quota_message(daily) == "Today's free AI budget is spent."


def test_only_a_retry_start_frame_is_inspected():
    frame = {"type": "auto_retry_start", "errorMessage": _error("demo_ai_daily_limit", "Used up.")}
    assert demo_lane.frame_quota_message(frame) == "Used up."
    assert demo_lane.frame_quota_message({**frame, "type": "message_end"}) is None
    snake = {"type": "auto_retry_start", "error_message": _error("demo_quota_exhausted", "Done.")}
    assert demo_lane.frame_quota_message(snake) == "Done."
    assert demo_lane.frame_quota_message({"type": "auto_retry_start", "errorMessage": "503 busy"}) is None


def _claude_error(code, message="Message from the front door."):
    return "429 " + json.dumps({"type": "error", "error": {"type": code, "message": message}})


@pytest.mark.parametrize("code", ["demo_ai_daily_limit", "demo_quota_exhausted"])
def test_a_claude_shaped_refusal_surfaces_the_lanes_own_message(code):
    assert demo_lane.quota_message(_claude_error(code, "Today's AI budget is spent.")) == (
        "Today's AI budget is spent.")
    frame = {"type": "auto_retry_start", "errorMessage": _claude_error(code, "Used up.")}
    assert demo_lane.frame_quota_message(frame) == "Used up."


@pytest.mark.parametrize("code", ["demo_ai_daily_limit", "demo_quota_exhausted"])
def test_a_claude_shaped_refusal_without_a_message_still_reads_as_a_sentence(code):
    message = demo_lane.quota_message('429 {"type":"error","error":{"type":"%s"}}' % code)
    assert message and code not in message and message.endswith(".")


@pytest.mark.parametrize("code", ["demo_ai_rate_limited", "ai_unavailable", "model_not_allowed"])
def test_other_claude_refusals_are_not_quota_refusals(code):
    assert demo_lane.quota_message(_claude_error(code)) is None
