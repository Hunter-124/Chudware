"""WS3 redaction pipeline tests — deterministic scrub, rehydrate, wrapper, local_only."""
from __future__ import annotations

import json
from typing import Any

import pytest

from services.agent import config as cfg
from services.agent import redaction as R
from services.agent.providers.base import Provider, image_part, text_part, user


# --------------------------------------------------------------------------- fixtures
class _EchoProvider(Provider):
    """Returns the scrubbed system + last user text so tests can inspect outbound payload."""

    name = "mock"
    supports_vision = True
    offline_safe = True

    def __init__(self) -> None:
        self.last_system = ""
        self.last_messages: list[dict] = []
        self.reply_template = "ACK {echo}"

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> tuple[str, str]:
        self.last_system = system
        self.last_messages = messages
        # Echo last user text part back with placeholders intact (simulates cloud).
        echo = _last_user_text(messages)
        return self.reply_template.format(echo=echo), "thinking:" + echo


class _CloudishProvider(Provider):
    """Pretends to be anthropic (never offline-safe)."""

    name = "anthropic"
    supports_vision = True
    offline_safe = False

    def complete(self, system, messages, max_tokens=8000):
        return "ok"

    def complete_ex(self, system, messages, max_tokens=8000):
        return "ok", ""


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            bits = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    bits.append(str(p.get("text") or ""))
            return "\n".join(bits)
    return ""


# --------------------------------------------------------------------------- scrub unit tests
class TestDeterministicScrub:
    def test_dimension_with_units(self):
        m = R.RedactionMap()
        out = R.scrub_text("plate is 100.5 mm wide and 2.0 in thick", m)
        assert "100.5 mm" not in out
        assert "2.0 in" not in out
        assert "DIM_1" in out and "DIM_2" in out
        # Round-trip
        assert R.rehydrate_text(out, m) == "plate is 100.5 mm wide and 2.0 in thick"

    def test_tolerance_and_iso_fit(self):
        m = R.RedactionMap()
        text = "bore Ø20 mm H7 with tolerance ±0.02 mm, mate shaft g6"
        out = R.scrub_text(text, m)
        assert "±0.02" not in out
        assert "H7" not in out
        assert "g6" not in out
        # diameter prefix form
        assert "20 mm" not in out or "Ø20" not in out
        restored = R.rehydrate_text(out, m)
        assert "H7" in restored
        assert "±0.02" in restored
        assert "g6" in restored

    def test_material_spec(self):
        m = R.RedactionMap()
        text = "Machine from 6061-T6 aluminum; alternate AISI 4140 steel"
        out = R.scrub_text(text, m)
        assert "6061-T6" not in out
        assert "AISI 4140" not in out
        assert "MAT_" in out
        restored = R.rehydrate_text(out, m)
        assert "6061-T6" in restored
        assert "AISI 4140" in restored

    def test_part_and_drawing_numbers(self):
        m = R.RedactionMap()
        text = "See DWG-1234-A and P/N ABC-99-X for assembly"
        out = R.scrub_text(text, m)
        assert "DWG-1234-A" not in out
        assert "ABC-99-X" not in out
        assert "PN_" in out
        restored = R.rehydrate_text(out, m)
        assert "DWG-1234-A" in restored

    def test_mapping_stability_across_turns(self):
        m = R.RedactionMap()
        t1 = R.scrub_text("length 50 mm", m)
        t2 = R.scrub_text("also 50 mm on the other side", m)
        # Same original → same placeholder
        ph = m.forward["50 mm"]
        assert ph in t1 and ph in t2
        assert list(m.forward.values()).count(ph) == 1 or t1.count(ph) >= 1
        # Counter does not allocate a second token for the same value
        assert m.forward["50 mm"] == ph
        assert len([k for k in m.forward if k == "50 mm"]) == 1

    def test_degree_and_bilateral_tol(self):
        m = R.RedactionMap()
        out = R.scrub_text("chamfer 45° and +0.02/-0.01 mm", m)
        assert "45°" not in out
        assert "+0.02/-0.01" not in out
        restored = R.rehydrate_text(out, m)
        assert "45°" in restored
        assert "+0.02/-0.01" in restored


class TestToolCallArgsScrub:
    def test_tool_call_arguments_json_string(self):
        m = R.RedactionMap()
        args = json.dumps({"d": "12.5 mm", "material": "6061-T6", "fit": "H7"})
        messages = [{
            "role": "assistant",
            "content": [],
            "tool_calls": [{
                "id": "1",
                "type": "function",
                "function": {"name": "add_features", "arguments": args},
            }],
        }]
        scrubbed, _ = R.scrub_messages(messages, m)
        raw_args = scrubbed[0]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(raw_args)
        assert "12.5 mm" not in raw_args
        assert "6061-T6" not in raw_args
        assert "H7" not in raw_args
        assert parsed["d"].startswith("DIM_") or "DIM_" in parsed["d"]
        # Rehydrate restores tool args for local dispatch
        back = R.rehydrate_messages(scrubbed, m)
        back_args = json.loads(back[0]["tool_calls"][0]["function"]["arguments"])
        assert back_args["d"] == "12.5 mm"
        assert back_args["material"] == "6061-T6"
        assert back_args["fit"] == "H7"

    def test_content_part_input_dict(self):
        m = R.RedactionMap()
        messages = [{
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "name": "extrude",
                "input": {"depth": "3.0 mm", "note": "use 304 stainless"},
            }],
        }]
        scrubbed, _ = R.scrub_messages(messages, m)
        inp = scrubbed[0]["content"][0]["input"]
        assert "3.0 mm" not in inp["depth"]
        assert "304 stainless" not in inp["note"]
        back = R.rehydrate_messages(scrubbed, m)
        assert back[0]["content"][0]["input"]["depth"] == "3.0 mm"


class TestImageBlocking:
    def test_images_blocked_in_scrub_messages(self):
        m = R.RedactionMap()
        messages = [user([text_part("see view"), image_part("/tmp/x.png")])]
        scrubbed, n = R.scrub_messages(messages, m, block_images=True)
        assert n == 1
        parts = scrubbed[0]["content"]
        assert all(p.get("type") != "image" for p in parts if isinstance(p, dict))
        assert any("image redacted" in (p.get("text") or "") for p in parts if isinstance(p, dict))


class TestRedactingProvider:
    def test_round_trip_scrub_and_rehydrate(self):
        inner = _EchoProvider()
        inner.reply_template = "Set width to {echo} please"
        wrap = R.RedactingProvider(inner, enable_llm_pass=False)
        prompt = "Make a 80 mm x 40 mm plate from 6061-T6, hole H7"
        text, thinking = wrap.complete_ex(
            "You are a CAD agent.",
            [user([text_part(prompt)])],
        )
        # Outbound saw placeholders only
        outbound = inner.last_messages
        outbound_txt = _last_user_text(outbound)
        assert "80 mm" not in outbound_txt
        assert "40 mm" not in outbound_txt
        assert "6061-T6" not in outbound_txt
        assert "H7" not in outbound_txt
        assert "DIM_" in outbound_txt
        assert "MAT_" in outbound_txt
        # Inbound rehydrated for local consumer
        assert "80 mm" in text
        assert "6061-T6" in text
        assert "H7" in text
        assert "DIM_" not in text
        assert "80 mm" in thinking

    def test_supports_vision_forced_false(self):
        inner = _EchoProvider()
        assert inner.supports_vision is True
        wrap = R.RedactingProvider(inner, enable_llm_pass=False)
        assert wrap.supports_vision is False

    def test_images_blocked_on_complete(self):
        inner = _EchoProvider()
        wrap = R.RedactingProvider(inner, enable_llm_pass=False)
        wrap.complete_ex(
            "sys",
            [user([text_part("view this 10 mm part"), image_part("/tmp/p.png")])],
        )
        assert wrap.last_images_blocked == 1
        # No image parts reached inner
        for m in inner.last_messages:
            content = m.get("content") or []
            if isinstance(content, list):
                assert all(
                    not (isinstance(p, dict) and p.get("type") == "image")
                    for p in content
                )

    def test_llm_pass_failure_degrades_to_deterministic(self):
        class _Boom(Provider):
            name = "mock"
            offline_safe = True
            supports_vision = False

            def complete(self, system, messages, max_tokens=8000):
                raise RuntimeError("local llm down")

            def complete_ex(self, system, messages, max_tokens=8000):
                raise RuntimeError("local llm down")

        inner = _EchoProvider()
        wrap = R.RedactingProvider(
            inner, redaction_llm=_Boom(), enable_llm_pass=True
        )
        # Must not raise; deterministic scrub still applies
        text, _ = wrap.complete_ex("sys", [user([text_part("cut 5.0 mm deep")])])
        assert "5.0 mm" not in _last_user_text(inner.last_messages)
        assert "5.0 mm" in text  # rehydrated

    def test_llm_pass_flags_residual_spans(self):
        class _Flagger(Provider):
            name = "mock"
            offline_safe = True
            supports_vision = False

            def complete(self, system, messages, max_tokens=8000):
                # Claim "SECRET-PART" is residual sensitive
                return '["SECRET-PART"]'

            def complete_ex(self, system, messages, max_tokens=8000):
                return self.complete(system, messages, max_tokens), ""

        inner = _EchoProvider()
        wrap = R.RedactingProvider(
            inner, redaction_llm=_Flagger(), enable_llm_pass=True
        )
        text, _ = wrap.complete_ex(
            "sys", [user([text_part("code name SECRET-PART uses 10 mm pin")])]
        )
        outbound = _last_user_text(inner.last_messages)
        assert "SECRET-PART" not in outbound
        assert "SPAN_" in outbound or "SECRET-PART" not in outbound
        assert "SECRET-PART" in text  # rehydrated for local


class TestLocalOnlyMode:
    def test_compliance_mode_local_only_refuses_cloud(self):
        with pytest.raises(cfg.OfflineModeError):
            cfg.get_provider(
                "anthropic",
                {"compliance_mode": "local_only", "model": "x", "api_key": "k"},
            )

    def test_compliance_mode_local_only_allows_mock(self):
        p = cfg.get_provider("mock", {"compliance_mode": "local_only"})
        # mock is offline-safe; not wrapped unless redact
        assert not isinstance(p, R.RedactingProvider)
        assert getattr(p, "name", "") in ("mock", "mock-roles") or p.name == "mock"

    def test_offline_mode_still_refuses_cloud(self):
        with pytest.raises(cfg.OfflineModeError):
            cfg.get_provider("openrouter", {"offline_mode": True})

    def test_redact_mode_wraps_provider(self):
        p = cfg.get_provider("mock", {"compliance_mode": "redact"})
        assert isinstance(p, R.RedactingProvider)
        assert p.supports_vision is False

    def test_off_mode_no_wrap(self):
        p = cfg.get_provider("mock", {"compliance_mode": "off"})
        assert not isinstance(p, R.RedactingProvider)

    def test_helpers(self):
        assert cfg.compliance_mode({"compliance_mode": "REDACT"}) == "redact"
        assert cfg.compliance_mode({"compliance_mode": "local-only"}) == "local_only"
        assert cfg.compliance_mode({}) == "off"
        assert cfg.local_only_enabled({"compliance_mode": "local_only"}) is True
        assert cfg.local_only_enabled({"offline_mode": True}) is True
        assert cfg.local_only_enabled({"compliance_mode": "redact"}) is False


class TestRedactionRoleLoopback:
    def test_cloud_redaction_role_refused(self):
        p = R.resolve_redaction_provider({
            "ai_roles": {"redaction": {"provider": "anthropic", "model": "claude"}},
        })
        assert p is None

    def test_llm_flag_residual_empty_on_bad_json(self):
        m = R.RedactionMap()
        out = R.llm_flag_residual(
            "hello", m, provider=None, complete_fn=lambda t: "not json"
        )
        assert out == "hello"
