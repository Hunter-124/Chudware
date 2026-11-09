"""IP-18 regen_diagnose MCP seam: the callable backend fires on a rebuild-failure doc, returns a
fix-preview card, and degrades honestly when its model step cannot run. The default model step
is a tool-free agent-runtime completion (``OmpSessionHost.complete_text``), exercised here
through an injected process-free RPC client; no binary is started and no provider is contacted.
Offline/local-only binds an offline-safe provider. No core (structural verify)."""
import json
import random
from types import SimpleNamespace

import pytest

from services.agent import config as agent_config
from services.agent import regen_diagnosis as RD
from services.agent import regen_verify as RV
from services.agent import role_models
from services.agent.omp_host import (
    OmpSessionHost,
    _set_default_omp_host_for_tests,
)
from services.agent.providers.base import Provider, ProviderUnreachable
from services.agent.providers.mock_provider import MockProvider
from services.mcp import regen_tools as RT
from tools.flywheel.bench.corpus import regen_corpus as RC


class _HelperBridge:
    """Process-free implementation of the OMP extension's helper auth seam."""

    def __init__(self, runtime_dir, dispatch):
        self.operations = []
        self.keyless_providers = []
        self.stopped = False

    def start(self):
        return self

    def info(self):
        return {"transport": "unix", "endpoint": "/private/test.sock", "token": "fixture"}

    def publish_tools(self, definitions):
        assert not definitions

    def publish_keyless_providers(self, providers):
        self.keyless_providers = list(providers)

    def call(self, operation, arguments, *, timeout, cancel_event=None):
        self.operations.append((operation, arguments))
        assert operation == "auth_state"
        assert arguments["provider"] == "fixture"
        return {"provider": "fixture", "configured": True, "oauth": False}

    def stop(self):
        self.stopped = True


class _HelperClient:
    """The RpcClient surface ``complete_text`` drives, without a process."""

    protocol_version = 2

    def __init__(self, script, **kwargs):
        self.kwargs = kwargs
        self.script = script
        self.listeners = []
        self.prompts = []
        self.models = []
        self.model = script.get("model")
        self.stopped = False

    def start(self):
        self.bridge = self._omp_bridge
        return self

    def on_notification(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def set_model(self, provider, model):
        self.model = {"provider": provider, "id": model}
        self.models.append((provider, model))

    def get_state(self):
        return {"model": self.model}

    def prompt_and_wait(self, message, timeout=None):
        self.prompts.append(message)
        error = self.script.get("error")
        message_end = {"role": "assistant", "stopReason": "error" if error else "stop"}
        if error:
            message_end["errorMessage"] = error
        for listener in list(self.listeners):
            listener({"type": "message_end", "message": message_end})
        return SimpleNamespace(assistant_text=self.script.get("reply"))

    def stop(self):
        self.stopped = True


@pytest.fixture
def runtime(monkeypatch):
    """The process-global agent host with a process-free client and the runtime's own model."""
    clients = []
    script = {"model": {"provider": "fixture", "id": "fixture-model"}}

    def factory(**kwargs):
        clients.append(_HelperClient(script, **kwargs))
        return clients[-1]

    host = OmpSessionHost(client_factory=factory)
    monkeypatch.setattr("services.agent.omp_bridge.ExtensionBridge", _HelperBridge)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: None)
    monkeypatch.setattr(agent_config, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(role_models, "resolve_role_model",
                        lambda *a, **k: SimpleNamespace(provider="", model=""))
    previous = _set_default_omp_host_for_tests(host)
    try:
        yield host, clients, script
    finally:
        _set_default_omp_host_for_tests(previous)
        host.shutdown()


def _corrupt(mclass, seed=1):
    corrupted, _ = RC.mutate(RC.load_seed(), mclass, random.Random(seed))
    return {"revision": 0, "tree": corrupted, "owner_map": {}}


def _oracle_reply(document):
    ops = []
    for f in RD.detect_failures(document):
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
        elif f.kind == "bad_selector":
            ops.append({"op": "set_selector_kind", "feature": f.feature,
                        "old_kind": f.options["bad_kind"], "kind": "face"})
    return json.dumps({"diagnosis": "repair", "fix_ops": ops})


def test_seam_returns_card_and_logs_ip18(monkeypatch):
    monkeypatch.setattr(RV, "make_live_verifier", lambda: (None, None))
    doc = _corrupt("suppressed_parent", seed=2)
    events = []
    out = RT.diagnose_failed_document(
        doc, provider=MockProvider([_oracle_reply(doc)]),
        on_event=events.append, consent_fn=lambda: "meta")
    assert out["ok"] is True
    assert out["artifact_id"]
    card = out["card"]
    assert card["kind"] == "fix_preview_card" and card["fix_ops"]
    assert card["verifier"]["detail"]["mode"] == "fixture"
    assert card["verifier"]["detail"]["anomaly_gone"] is True
    assert card["verifier"]["score"] is None
    # IP-18 correction trace emitted
    assert any(e.get("kind") == "ai_artifact" and e["insertion_point"] == "IP-18" for e in events)


def test_seam_degrades_without_provider(runtime):
    _, clients, script = runtime
    script["model"] = None
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, provider=None)
    assert out["ok"] is False
    assert isinstance(out["reason"], str) and out["reason"]
    assert "card" not in out
    assert any(f["kind"] == "bad_selector" for f in out["failures"])
    assert len(clients) == 1 and clients[0].stopped and not clients[0].prompts
    assert clients[0].bridge.stopped and not clients[0].bridge.operations


def test_default_helper_model_failure_retains_detected_failures(runtime):
    _, clients, script = runtime
    script["error"] = "helper provider failed"
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc)
    assert out["ok"] is False and out["reason"]
    assert "card" not in out
    assert any(f["kind"] == "bad_selector" for f in out["failures"])
    assert len(clients) == 1 and clients[0].stopped and len(clients[0].prompts) == 1
    assert clients[0].bridge.stopped
    assert ("auth_state", {"provider": "fixture"}) in clients[0].bridge.operations


def test_unreachable_provider_degrades_instead_of_raising():
    """A configured provider that cannot be reached must not crash the diagnosis.

    Regression: openai_provider raised a bare RuntimeError for a refused connection, so
    a user with LM Studio configured but not running got an exception out of
    regen_diagnose instead of the failure list the tool promises in its docstring.
    """
    class _Down:
        def complete(self, *a, **k):
            raise ProviderUnreachable("Could not reach the model server at http://localhost:1234/v1")

        def complete_ex(self, *a, **k):
            raise ProviderUnreachable("Could not reach the model server at http://localhost:1234/v1")

    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, provider=_Down())
    assert out["ok"] is False
    assert "localhost:1234" in out["reason"]          # names the endpoint that is down
    assert any(f["kind"] == "bad_selector" for f in out["failures"])


def test_tool_registration_and_json_contract():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    fn = captured["regen_diagnose"]
    doc = _corrupt("suppressed_parent", seed=4)
    # inject an oracle provider by monkeypatching the resolver for this call
    orig = RT._resolve_provider
    RT._resolve_provider = lambda cancel=None: MockProvider([_oracle_reply(doc)])
    try:
        raw = fn(json.dumps(doc))
    finally:
        RT._resolve_provider = orig
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["card"]["kind"] == "fix_preview_card"


def test_tool_reports_bad_json():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    result = json.loads(captured["regen_diagnose"]("{not valid json"))
    assert result["ok"] is False and result["error"]


def test_default_helper_returns_logged_fix_card(runtime):
    host, clients, script = runtime
    doc = _corrupt("suppressed_parent", seed=2)
    script["reply"] = _oracle_reply(doc)
    events = []
    out = RT.diagnose_failed_document(
        doc, on_event=events.append, consent_fn=lambda: "meta", verify=False)
    assert out["ok"] is True and out["card"]["fix_ops"]
    card = out["card"]
    assert card["fix_ops"] == json.loads(script["reply"])["fix_ops"]
    assert card["n_applied"] > 0 and not RD.detect_failures(card["_fixed_document"])
    assert any(f["kind"] == "suppressed_parent" for f in card["failures"])
    assert out["artifact_id"] and out["artifact_id"] == card["artifact_id"]
    assert any(event.get("kind") == "ai_artifact" and
               event.get("artifact_id") == out["artifact_id"] and
               event.get("insertion_point") == "IP-18" for event in events)
    assert len(clients) == 1 and clients[0].stopped
    assert clients[0].kwargs["tools"] == [] and clients[0].kwargs["no_session"]
    assert clients[0].bridge.stopped
    assert ("auth_state", {"provider": "fixture"}) in clients[0].bridge.operations
    prompt = clients[0].prompts[0]
    payload = json.loads(prompt[prompt.index("{"):])
    assert any(f["kind"] == "suppressed_parent" for f in payload["failures"])
    assert {f["id"] for f in payload["features"]} == {
        f["id"] for f in doc["tree"]["features"]}
    assert not host._sessions and not host._helper_clients


def test_explicit_offline_helper_binds_only_offline_safe_provider(monkeypatch):
    from services.agent.providers.omp_provider import helper_provider

    bound = MockProvider(["offline repair"])
    monkeypatch.setattr(agent_config, "load_config", lambda: {"offline_mode": True})
    monkeypatch.setattr(agent_config, "get_provider", lambda **kwargs: bound)
    monkeypatch.setattr(agent_config, "provider_offline_safe", lambda provider: provider is bound)
    assert helper_provider("fixer").complete("", []) == "offline repair"

    monkeypatch.setattr(agent_config, "provider_offline_safe", lambda provider: False)
    with pytest.raises(agent_config.OfflineModeError):
        helper_provider("fixer")


def test_cancelled_default_helper_retains_detected_failures_without_prompting(runtime):
    _, clients, _ = runtime
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, cancel=lambda: True)
    assert not out["ok"] and out["reason"]
    assert any(f["kind"] == "bad_selector" for f in out["failures"])
    assert not clients


def test_incomplete_selected_model_fails_without_runtime_default_substitution(runtime, monkeypatch):
    _, clients, _ = runtime
    monkeypatch.setattr(agent_config, "load_config", lambda: {"provider": "codex"})
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc)
    assert not out["ok"] and out["reason"]
    assert not clients


def _failed_candidate():
    return {"revision": 7, "tree": {"schema": "aicad.part/1", "name": "Wrist",
            "features": [
                {"id": "f25", "type": "box", "name": "Base",
                 "params": {"length": 10, "width": 10, "height": 10}, "refs": {}},
                {"id": "f61", "type": "fillet", "name": "Ramp apron end cove",
                 "params": {"radius": {"value": 1, "expr": None}},
                 "refs": {"edges": [
                     {"of": "edge", "surf": "line", "near": [5, 5, 0], "count": 1},
                     {"kind": "edge", "centroid": [1000, 1000, 1000]},
                 ]}}],
            "equations": {"globals": []}},
            "rebuild": {"feature_status": [{"id": "f61", "ok": False,
                         "error": "fillet: unresolved edge selection"}]}}


def test_failed_add_features_carries_structural_findings_and_calls_no_model(monkeypatch):
    from services.mcp import cad_doc_tools as CT
    from services.mcp import core_bridge as cb
    from services.mcp import preflight_tools
    import copy

    class Tools:
        def __init__(self):
            self.functions = {}

        def tool(self, *args, **kwargs):
            def register(fn):
                self.functions[fn.__name__] = fn
                return fn
            return register

    tools = Tools()
    CT.register_cad_doc_tools(tools)
    candidate = _failed_candidate()
    rollback = {"revision": 6, "tree": copy.deepcopy(candidate["tree"])}
    rollback["tree"]["features"].pop()
    original = copy.deepcopy(rollback)
    calls = []
    model_calls = []

    def engine(method, params):
        calls.append(method)
        if method == "doc_checkpoint":
            return {"checkpoint": "before"}
        if method == "doc_apply_ops":
            return {**copy.deepcopy(rollback), "rejected_tree": candidate["tree"],
                    "rebuild": candidate["rebuild"], "id_map": {"$0": "f61"}}
        if method == "doc_revert":
            pytest.fail("native atomic rejection must not mutate the restored document")
        return {}

    def no_model(*args, **kwargs):
        model_calls.append(args)
        raise AssertionError("a failed add_features must not reach a model")

    monkeypatch.setattr(cb, "call", engine)
    monkeypatch.setattr(CT, "_doc", lambda doc_id: doc_id)
    monkeypatch.setattr(CT, "_face_hist", lambda doc_id: {})
    monkeypatch.setattr(CT, "_publish_current", lambda doc_id: None)
    monkeypatch.setattr(CT, "_solve_sync_or_handoff", lambda name, fn, **kwargs: fn())
    monkeypatch.setattr(preflight_tools, "run_auto_preflight", lambda *args: {})
    monkeypatch.setattr(RT, "_resolve_provider", no_model)
    monkeypatch.setattr(RT, "diagnose_failed_document", no_model)
    monkeypatch.setattr(RD, "diagnose", no_model)
    from services.agent.providers import omp_provider
    monkeypatch.setattr(omp_provider, "helper_provider", no_model)
    result = json.loads(tools.functions["add_features"](
        doc_id="live", ops=[{"op": "add_feature", "feature": candidate["tree"]["features"][-1]}]))
    assert result["ok"] is False and result["rolled_back"] is True
    assert rollback == original
    assert "doc_revert" not in calls
    assert not model_calls
    assert "regen_diagnosis" not in result
    findings = result["structural_findings"]
    failure = findings["failures"][-1]
    assert failure["feature"] == "f61"
    assert failure["detail"] == candidate["rebuild"]["feature_status"][0]["error"]
    assert failure["options"]["name"] == "Ramp apron end cove"
    assert "regen_diagnose" in findings["hint"]


def test_native_failure_survives_unavailable_model_and_verifier(monkeypatch):
    doc = _failed_candidate()
    monkeypatch.setattr(RT, "_resolve_provider", lambda *args: (_ for _ in ()).throw(
        ProviderUnreachable("no model")))
    out = RT.diagnose_failed_document(doc)
    assert out["ok"] is False and out["failures"][-1]["feature"] == "f61"
    monkeypatch.setattr(RV, "make_live_verifier", lambda: (None, None))
    score, detail = RT._verify(doc, SimpleNamespace(n_applied=1, fixed_document=doc), None)
    assert score is None and detail["mode"] == "fixture"
    # Structural detection cannot establish that the native fillet rebuilds.
    assert detail["anomaly_gone"] is True

@pytest.mark.parametrize(
    ("rebuilt", "applied", "expected"),
    [(True, 1, None), (False, 1, 0.0), (True, 0, 0.0)],
)
def test_rebuild_without_reference_never_proves_geometry(monkeypatch, rebuilt, applied, expected):
    doc = _failed_candidate()
    closed = []

    def opener(document):
        assert document is doc
        return {"doc_id": "scratch", "rebuilt": rebuilt, "error": None}

    opener.close = closed.append
    monkeypatch.setattr(RV, "make_live_verifier", lambda: (opener, None))
    score, detail = RT._verify(doc, SimpleNamespace(n_applied=applied, fixed_document=doc), None)
    assert score == expected
    assert detail["mode"] == "live-rebuild-only" and detail["rebuilt"] is rebuilt
    assert closed == ["scratch"]


@pytest.mark.parametrize(
    ("match", "expected_score", "expected_verdict"),
    [(100.0, 1.0, "pass"), (99.5, 1.0, "pass"),
     (99.49, 0.0, "fail"), (None, None, "error")],
)
def test_reference_comparison_controls_verifier_score(
        monkeypatch, match, expected_score, expected_verdict):
    doc = _failed_candidate()
    reference = {"tree": {"features": []}}
    opened = []
    closed = []
    compared = []

    def opener(document):
        opened.append(document)
        return {"doc_id": f"scratch-{len(opened)}", "rebuilt": True}

    def compare(fixed_id, reference_id):
        compared.append((fixed_id, reference_id))
        return match

    opener.close = closed.append
    monkeypatch.setattr(RV, "make_live_verifier", lambda: (opener, compare))
    score, detail = RT._verify(
        doc, SimpleNamespace(n_applied=1, fixed_document=doc), reference)
    assert score == expected_score and detail["verdict"] == expected_verdict
    assert opened == [doc, reference]
    assert compared == [("scratch-1", "scratch-2")]
    assert closed == ["scratch-1", "scratch-2"]


def test_matching_reference_cannot_credit_deleted_candidate_feature(monkeypatch):
    candidate = {"tree": {"features": [{"id": "f1"}, {"id": "f2"}]}}
    fixed = {"tree": {"features": [{"id": "f1"}]}}
    reference = {"tree": {"features": [{"id": "f1"}]}}
    opened = []
    closed = []
    comparisons = []

    def opener(document):
        opened.append(document)
        return {"doc_id": f"scratch-{len(opened)}", "rebuilt": True}

    def compare(fixed_id, reference_id):
        comparisons.append((fixed_id, reference_id))
        return 100.0

    opener.close = closed.append
    monkeypatch.setattr(RV, "make_live_verifier", lambda: (opener, compare))
    score, detail = RT._verify(
        candidate, SimpleNamespace(n_applied=1, fixed_document=fixed), reference)
    assert opened == [fixed, reference]
    assert comparisons == [("scratch-1", "scratch-2")]
    assert closed == ["scratch-1", "scratch-2"]
    assert score == 0.0 and detail["verdict"] == "fail"
    assert detail["removed_features"] == ["f2"]


@pytest.mark.parametrize("outcome", ["success", "native_failure", "validation_failure"])
def test_live_verifier_supported_ops_sparse_ids_and_cleanup(monkeypatch, outcome):
    import copy
    from services.mcp import core_bridge as cb
    doc = _failed_candidate()
    doc["tree"]["features"][0]["readback_only"] = True
    doc["tree"]["features"][1]["refs"]["edges"][0]["owner_feature"] = "f25"
    original = copy.deepcopy(doc)
    calls = []

    def engine(method, params):
        calls.append((method, params))
        if method == "doc_new":
            assert params["internal"] is True
            return {"doc_id": "scratch"}
        if method == "doc_apply_ops":
            assert params["doc_id"] == "scratch"
            assert params["with_tess"] is False
            features = [op["feature"] for op in params["ops"] if op["op"] == "add_feature"]
            assert set(features[0]) == {"type", "name", "params", "refs"}
            assert features[1]["refs"]["edges"][0]["owner_feature"] == "$0"
            if outcome == "validation_failure":
                raise cb.CoreError("invalid candidate", code="invalid_op")
            return {"id_map": {"$0": "f1", "$1": "f2"}, "rebuild": {"feature_status": [
                {"id": "f2", "ok": outcome == "success", "error": "native fillet error"}]}}
        if method == "doc_close":
            assert params == {"doc_id": "scratch"}
            return {"closed": True}
        pytest.fail(f"unsupported verifier method {method}")

    monkeypatch.setattr(cb, "available", lambda: True)
    monkeypatch.setattr(cb, "call", engine)
    opener, _compare = RV.make_live_verifier()
    result = opener(doc)
    if outcome == "success":
        opener.close(result["doc_id"])
    else:
        assert result["rebuilt"] is False and result["doc_id"] is None
        if outcome == "native_failure":
            assert result["error"] == "f61: native fillet error"
    assert calls[-1][0] == "doc_close"
    assert sum(method == "doc_close" for method, _ in calls) == 1
    assert doc == original


def test_live_verifier_compare_cleanup(monkeypatch):
    closed = []
    opened = []

    def opener(document):
        doc_id = f"scratch-{len(opened)}"
        opened.append(doc_id)
        return {"doc_id": doc_id, "rebuilt": True}

    opener.close = closed.append
    RV.verify_live(_failed_candidate(), True, _failed_candidate(),
                   opener=opener, compare_fn=lambda *args: None)
    assert closed == opened and len(closed) == 2


def test_live_rejected_candidate_and_verifier_smoke(monkeypatch):
    """Provider-independent runtime repro; only test-owned hidden documents are touched."""
    import copy
    import math

    from services.mcp import core_bridge as cb
    if not cb.available():
        pytest.skip("native engine unavailable")
    doc_id = cb.call("doc_new", {"kind": "part", "name": "regen_smoke", "internal": True})["doc_id"]
    try:
        initial = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{"op": "add_feature", "feature": {
            "type": "box", "name": "Block", "params": {"length": 10, "width": 10, "height": 10}}}]})
        assert all(s["ok"] for s in initial["rebuild"]["feature_status"])
        before = cb.call("doc_tree", {"doc_id": doc_id})
        mass_before = cb.call("eval_mass", {"doc_id": doc_id})["volume_mm3"]
        # The first edge exists; the second is a genuinely dangling pick.
        # An unsupported selection must not turn into a partial successful fillet.
        failed = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{"op": "add_feature", "feature": {
            "type": "fillet", "name": "Rejected cove", "params": {"radius": {"value": 1, "expr": None}},
            "refs": {"edges": [
                {"of": "edge", "surf": "line", "count": 1, "near": [5, 5, 0]},
                {"kind": "edge", "centroid": [1000, 1000, 1000]},
            ]}}}], "with_tess": False})
        statuses = failed["rebuild"]["feature_status"]
        native = next(status for status in statuses if not status["ok"])
        assert native["error"] and "rejected_tree" in failed
        candidate = {"tree": failed["rejected_tree"], "rebuild": failed["rebuild"]}
        assert candidate["tree"]["features"][-1]["id"] == native["id"]
        assert candidate["tree"]["features"][-1]["name"] == "Rejected cove"
        assert cb.call("doc_tree", {"doc_id": doc_id}) == before
        assert cb.call("eval_mass", {"doc_id": doc_id})["volume_mm3"] == pytest.approx(mass_before)
        monkeypatch.setattr(RT, "_resolve_provider", lambda *args: (_ for _ in ()).throw(
            ProviderUnreachable("smoke deliberately has no provider")))
        diagnostic = RT.diagnose_failed_document(candidate)
        assert any(f["feature"] == native["id"] and f["detail"] == native["error"]
                   for f in diagnostic["failures"])
        opener, compare = RV.make_live_verifier()
        temporary_before = {row["doc_id"] for row in cb.call("doc_list", {})["docs"]}
        reopened = opener(candidate)
        try:
            assert reopened["rebuilt"] is False
            assert native["id"] in reopened["error"] and native["error"] in reopened["error"]
        finally:
            if reopened.get("doc_id"):
                opener.close(reopened["doc_id"])
        # Repair the bad pick explicitly to the opposite real corner; retain both
        # selected edges and verify their rebuilt geometry rather than dropping one.
        corrected = copy.deepcopy(candidate)
        corrected["tree"]["features"][-1]["refs"]["edges"][1] = {
            "of": "edge", "surf": "line", "count": 1, "near": [-5, -5, 0],
        }
        repaired = opener(corrected)
        assert repaired["rebuilt"] is True, repaired
        try:
            repaired_id = repaired["doc_id"]
            repaired_tree = cb.call("doc_tree", {"doc_id": repaired_id})["tree"]
            fillet_id = next(f["id"] for f in repaired_tree["features"]
                             if f["name"] == "Rejected cove")
            rebuilt = cb.call("doc_dry_run_ops", {"doc_id": repaired_id, "ops": []})
            fillet = next(s for s in rebuilt["rebuild"]["feature_status"] if s["id"] == fillet_id)
            report = fillet["data"]["fillet"]
            assert fillet["ok"] and report["selected_edges"] == report["covered_edges"] == 2
            repaired_mass = cb.call("eval_mass", {"doc_id": repaired_id})["volume_mm3"]
            assert repaired_mass == pytest.approx(mass_before - 2 * (1 - math.pi / 4) * 10,
                                                  rel=1e-6)
        finally:
            opener.close(repaired["doc_id"])
        # Independently build the intended two-corner round on the unchanged
        # source, then compare the repaired candidate against that real part.
        reference_result = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{
            "op": "add_feature", "feature": {
                "type": "fillet", "name": "Reference rounds", "params": {"radius": 1},
                "refs": {"edges": [
                    {"of": "edge", "surf": "line", "count": 1, "near": [5, 5, 0]},
                    {"of": "edge", "surf": "line", "count": 1, "near": [-5, -5, 0]},
                ]},
            },
        }]})
        assert all(s["ok"] for s in reference_result["rebuild"]["feature_status"])
        reference_id = reference_result["id_map"]["$0"]
        reference_status = next(s for s in reference_result["rebuild"]["feature_status"]
                                if s["id"] == reference_id)
        reference_report = reference_status["data"]["fillet"]
        assert reference_report["selected_edges"] == reference_report["covered_edges"] == 2
        assert cb.call("eval_mass", {"doc_id": doc_id})["volume_mm3"] == pytest.approx(repaired_mass)
        reference = cb.call("doc_tree", {"doc_id": doc_id})
        score, passed, detail = RV.verify_live(
            corrected, True, reference, opener=opener, compare_fn=compare)
        assert score == 1 and passed and detail["rebuilt"] is True
        score, passed, detail = RV.verify_live(
            before, True, before, opener=opener, compare_fn=compare)
        assert score == 1 and passed and detail["rebuilt"] is True
        assert cb.call("doc_tree", {"doc_id": doc_id}) == reference
        assert {row["doc_id"] for row in cb.call("doc_list", {})["docs"]} == temporary_before
    finally:
        cb.call("doc_close", {"doc_id": doc_id})
