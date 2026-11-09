"""Settings → AI policy over OMP's settings, and Chudware's settings layer."""
import pytest
import yaml

from services import paths
from services.agent import omp_settings
from services.agent.omp_bridge import BridgeError


def row(key, tab, group, visible=True, **fields):
    """One row as the extension's ``config_list`` / ``config_get`` reports it."""
    return {"key": key, "tab": tab, "group": group, "label": key, "description": "", "type": "boolean",
            "options": [], "current": "false", "default": "false", "source": "default",
            "visible": visible, **fields}


@pytest.fixture
def layer(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    return tmp_path / "omp" / "cad-settings.yml"


def test_policy_exposes_omp_groups_by_chudware_tab_and_never_what_chudware_owns():
    def tab(key, omp_tab, group, backend="off"):
        return omp_settings.classify(row(key, omp_tab, group), backend)

    assert tab("retry.maxRetries", "model", "Retry & Fallback") == "models"
    assert tab("tier.anthropic", "model", "Sampling") == "models"
    assert tab("compaction.thresholdPercent", "context", "Compaction") == "context"
    assert tab("lsp.enabled", "files", "LSP") == "coding"
    assert tab("eval.py", "shell", "Eval & Runtimes") == "coding"
    assert tab("task.maxConcurrency", "tasks", "Subagents") == "agents"
    assert tab("providers.cacheRetention", "providers", "Protocol") == "providers"
    # Only what Chudware shows from a group; the rest of OMP's Sampling and Vision rows stay out.
    assert tab("temperature", "model", "Sampling") is None
    assert tab("images.urls.enabled", "model", "Vision") is None
    # Chudware's own controls and per-session pins, the composer's thinking level, terminal rendering.
    for key, omp_tab, group in (
        ("compaction.enabled", "context", "Compaction"), ("compaction.dropUseless", "context", "Compaction"),
        ("todo.eager", "tools", "Todos"), ("task.maxRuntimeMs", "tasks", "Subagents"),
        ("defaultThinkingLevel", "model", "Thinking"), ("hideThinkingBlock", "model", "Thinking"),
        ("bash.enabled", "shell", "Bash"), ("speech.voice", "providers", "Services"),
    ):
        assert tab(key, omp_tab, group) is None, key
    # OMP's Appearance, Interaction and Plugins have no Chudware tab.
    assert tab("theme.dark", "appearance", "Theme") is None
    assert tab("steeringMode", "interaction", "Input") is None
    assert tab("memory.backend", "memory", "General", "mnemopi") is None


@pytest.mark.parametrize(("backend", "group"), [
    ("mnemopi", "Mnemopi"), ("hindsight", "Hindsight"), ("sharpshooter", "Sharpshooter"),
])
def test_memory_tab_shows_only_the_group_of_the_selected_backend(backend, group):
    groups = {"Mnemopi", "Hindsight", "Sharpshooter"}
    for other in groups:
        shown = omp_settings.classify(row("some.setting", "memory", other), backend)
        assert shown == ("memory" if other == group else None)
    assert {"tab": "memory", "groups": [group]} in omp_settings.sources(backend)
    # Chudware pins Mnemopi's scoping per session.
    assert omp_settings.classify(row("mnemopi.scoping", "memory", "Mnemopi"), "mnemopi") is None


@pytest.mark.parametrize("backend", ["off", "local"])
def test_backends_without_an_omp_group_read_no_memory_rows(backend):
    assert all(source["tab"] != "memory" for source in omp_settings.sources(backend))


def test_present_shapes_entries_for_the_gui():
    rows = [
        row("retry.maxRetries", "model", "Retry & Fallback", type="number", current="10", default="10"),
        row("retry.usageReservePct", "model", "Retry & Fallback", visible=False),
        row("lsp.enabled", "files", "LSP", default="true", source="overlay"),
        row("theme.dark", "appearance", "Theme"),
        row("mnemopi.autoRecall", "memory", "Mnemopi", visible=False),
    ]
    entries = omp_settings.present(rows, "mnemopi")
    assert [(entry["key"], entry["tab"], entry["scope"]) for entry in entries] == [
        ("retry.maxRetries", "models", "omp"),
        ("lsp.enabled", "coding", "chudware"),
        # OMP's own condition names the backend of the runtime that answers, not Chudware's choice.
        ("mnemopi.autoRecall", "memory", "omp"),
    ]
    assert "visible" not in entries[0] and entries[0]["group"] == "Retry & Fallback"
    # A Chudware-owned key's default is Chudware's, not OMP's.
    assert entries[1]["default"] == "false" and entries[1]["source"] == "overlay"


def test_layer_is_created_with_chudware_defaults(layer):
    assert omp_settings.layer_path() == layer
    assert omp_settings.ensure_layer() == str(layer)
    assert yaml.safe_load(layer.read_text(encoding="utf-8")) == {
        "lsp": {"enabled": False, "diagnosticsOnWrite": False}, "task": {"maxEffort": "high"}}
    assert omp_settings.layer_value("lsp.enabled") is False


def test_ensure_layer_keeps_choices_and_other_keys_and_repairs_a_corrupt_file(layer):
    layer.parent.mkdir(parents=True)
    layer.write_text("task:\n  maxEffort: xhigh\nlsp:\n  lazy: false\n", encoding="utf-8")
    omp_settings.ensure_layer()
    assert yaml.safe_load(layer.read_text(encoding="utf-8")) == {
        "lsp": {"enabled": False, "diagnosticsOnWrite": False, "lazy": False}, "task": {"maxEffort": "xhigh"}}
    layer.write_text("{ unbalanced", encoding="utf-8")
    omp_settings.ensure_layer()
    assert omp_settings.layer_value("task.maxEffort") == "high"


def test_layer_lives_under_the_actor_state_root_in_a_cell(monkeypatch, tmp_path):
    monkeypatch.setattr(omp_settings.tenancy, "in_cell", lambda: True)
    monkeypatch.setattr(omp_settings.tenancy, "actor_state_root", lambda: tmp_path / "actor")
    assert omp_settings.layer_path() == tmp_path / "actor" / "omp" / "cad-settings.yml"
    assert omp_settings.ensure_layer() == str(tmp_path / "actor" / "omp" / "cad-settings.yml")


class Runtime:
    """The extension of one OMP runtime: the user's OMP config plus Chudware's layer."""

    def __init__(self, rows):
        self.rows = rows
        self.user = {}
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        key = params.get("key") or params["keys"][0]
        if key not in self.rows:
            raise BridgeError("unsupported")
        if command == "config_parse":
            if params["value"] not in ("true", "false"):
                raise BridgeError("invalid_request")
            return {"value": params["value"] == "true"}
        if command == "config_set":
            self.user[key] = params["value"]
        entry = dict(self.rows[key])
        entry["current"] = self.user.get(key, entry["current"])
        layered = omp_settings.layer_value(key)
        if layered is not None:
            entry.update(current=str(layered).lower(), source="overlay")
        return entry if command == "config_set" else {"settings": [entry]}


@pytest.fixture
def runtime():
    return Runtime({
        "lsp.enabled": row("lsp.enabled", "files", "LSP", current="true", default="true"),
        "retry.maxRetries": row("retry.maxRetries", "model", "Retry & Fallback", type="number",
                                current="10", default="10"),
        "compaction.enabled": row("compaction.enabled", "context", "Compaction"),
        "theme.dark": row("theme.dark", "appearance", "Theme"),
    })


def test_a_chudware_setting_is_written_to_the_layer_and_not_to_the_users_omp_config(layer, runtime):
    entry = omp_settings.apply(runtime, "lsp.enabled", "true", "off")
    assert (entry["current"], entry["source"], entry["scope"], entry["tab"]) == ("true", "overlay", "chudware", "coding")
    assert omp_settings.layer_value("lsp.enabled") is True
    assert omp_settings.layer_value("task.maxEffort") == "high"
    assert runtime.user == {} and all(command != "config_set" for command, _ in runtime.calls)

    entry = omp_settings.apply(runtime, "lsp.enabled", "default", "off")
    assert entry["current"] == "false" and omp_settings.layer_value("lsp.enabled") is False


def test_an_omp_setting_is_written_through_omp_and_leaves_the_layer_alone(layer, runtime):
    entry = omp_settings.apply(runtime, "retry.maxRetries", "5", "off")
    assert (entry["current"], entry["scope"], entry["tab"]) == ("5", "omp", "models")
    assert ("config_set", {"key": "retry.maxRetries", "value": "5"}) in runtime.calls
    assert not layer.exists()
    omp_settings.apply(runtime, "retry.maxRetries", "default", "off")
    assert ("config_set", {"key": "retry.maxRetries", "value": "default"}) in runtime.calls


def test_settings_chudware_does_not_expose_are_refused_before_anything_is_written(layer, runtime):
    for key in ("compaction.enabled", "theme.dark", "mnemopi.llmApiKey", "no.such.setting"):
        with pytest.raises(omp_settings.Refused):
            omp_settings.apply(runtime, key, "true", "off")
    with pytest.raises(omp_settings.Refused):
        omp_settings.apply(runtime, "lsp.enabled", "sideways", "off")
    assert not layer.exists()
    assert all(command in {"config_get", "config_parse"} for command, _ in runtime.calls)


def test_the_reply_is_the_value_the_runtime_uses_even_when_it_ignores_the_layer(layer, runtime, monkeypatch):
    monkeypatch.setattr(omp_settings, "_LIVE_SECONDS", 0.05)
    runtime.rows["lsp.enabled"]["source"] = "env"
    original = runtime.__call__

    def ignoring(command, params):
        reply = original(command, params)
        for entry in reply.get("settings", []):
            entry.update(current="true", source="env")
        return reply

    entry = omp_settings.apply(ignoring, "lsp.enabled", "false", "off")
    assert (entry["current"], entry["source"]) == ("true", "env")
    assert omp_settings.layer_value("lsp.enabled") is False
