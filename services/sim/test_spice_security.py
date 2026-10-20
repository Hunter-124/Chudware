"""Pure serializer security fixtures: never launch ngspice or a daemon."""
from copy import deepcopy

import pytest

from services.installer.ngspice_backend import emit_deck
from services.sim.spice import netlist_to_spice_circuit, write_spice


def circuit():
    return {"nodes": ["n", "0"], "models": {}, "params": {}, "devices": [
        {"kind": "R", "name": "R1", "nodes": ["n", "0"],
         "value": 1000.0, "model": None, "params": {}}]}


PAYLOAD = "R1 n 0 1k\n.control\necho AUDIT_FIXTURE_ONLY\n.endc\n*"


@pytest.mark.parametrize("field,payload", [
    ("name", PAYLOAD), ("name", ".control"), ("name", "shell"),
    ("nodes", ["n\n.control", "0"]), ("model", "D1\r.shell"),
    ("params", {"temp\n.control": 1}),
    ("params", {"temp": "1\n.control"}),
    ("params", {"temp": float("nan")}),
])
def test_device_data_cannot_become_directives(field, payload):
    data = circuit()
    data["devices"][0][field] = payload
    with pytest.raises(ValueError):
        emit_deck(data, {"analyses": [{"type": "op"}]})


@pytest.mark.parametrize("analysis", [
    {"type": "tf", "output": "V(n)\n.control\nshell echo fixture\n.endc"},
    {"type": "tf", "source": "V1 shell", "output": "V(n)"},
    {"type": "dc", "source": "V1\n.control"},
    {"type": "tran", "tstop": "1\n.control"},
    {"type": "ac", "sweep": "dec\n.control"},
    {"type": "ac", "points": "10\n.control"},
    {"type": "shell"},
])
def test_analysis_strings_cannot_become_directives(analysis):
    with pytest.raises(ValueError):
        emit_deck(circuit(), {"analyses": [analysis]})


def test_schematic_reference_refused_at_deck_boundary():
    data, _ = netlist_to_spice_circuit({"components": [
        {"ref": PAYLOAD, "libid": "sim:r", "value": "1k",
         "pins": {"1": "n", "2": "GND"},
         "sim": {"model": "R", "pin_order": ["1", "2"]}}], "nets": []})
    with pytest.raises(ValueError):
        emit_deck(data, {"analyses": [{"type": "op"}]})


@pytest.mark.parametrize("directive", [".control", ".include other.lib", ".lib other.lib section", ".shell echo fixture"])
def test_vendor_library_cannot_supply_commands_or_nested_includes(tmp_path, directive):
    library = tmp_path / "vendor.lib"
    library.write_text(".subckt DEMO n\n" + directive + "\n.ends\n")
    data = circuit()
    data["includes"] = [str(library)]
    with pytest.raises(ValueError):
        emit_deck(data, {"analyses": [{"type": "op"}]})


@pytest.mark.parametrize("field,payload", [("name", "X1\n.control"), ("nodes", ["n\n.control"]), ("subckt", ".control")])
def test_vendor_instance_tokens_refused(field, payload):
    data = circuit()
    device = {"name": "X1", "nodes": ["n", "0"], "subckt": "DEMO"}
    device[field] = payload
    data["xdevices"] = [device]
    with pytest.raises(ValueError):
        emit_deck(data, {"analyses": [{"type": "op"}]})


def test_legitimate_circuit_and_library_preserve_data(tmp_path):
    data = circuit()
    before = deepcopy(data)
    deck = emit_deck(data, {"analyses": [{"type": "op"}, {"type": "tf", "output": "V(n,0)", "source": "V1"}]})
    assert "Rcw_device_0 cw_node_0 0 1k" in deck
    assert ".op\n.tf V(cw_node_0,0) Vcw_device_1" in deck
    assert data == before
    library = tmp_path / "vendor model.lib"
    library.write_text(".subckt DEMO n g\nRin n g 10meg\n.ends DEMO\n")
    data["includes"] = [str(library)]
    data["xdevices"] = [{"name": "X1", "nodes": ["n", "0"], "subckt": "DEMO"}]
    deck = emit_deck(data, {"analyses": [{"type": "op"}]})
    assert ".subckt DEMO n g\nRin n g 10meg\n.ends DEMO" in deck
    assert "Xcw_device_1 cw_node_0 0 DEMO" in deck
    assert ".include" not in deck


def test_study_options_and_probes_are_data_only():
    for study in ({"probes": ["V(n)\n.control"]}, {"options": {"shell": 1}},
                  {"options": {"reltol": "1\n.control"}}):
        with pytest.raises(ValueError):
            write_spice(circuit(), study)


@pytest.mark.parametrize("expression", [".control", "V(n)\nshell echo fixture", "shell(1)", "'quoted'", "V(n);shell", "1\x00"])
def test_behavioral_expression_is_data_only(expression):
    data = circuit()
    data["devices"][0].update(kind="B", name="B1", params={"expr": expression})
    with pytest.raises(ValueError):
        emit_deck(data, {"analyses": [{"type": "op"}]})


def test_legitimate_behavioral_expression_preserved():
    data = circuit()
    data["devices"][0].update(kind="B", name="B1", params={"expr": "2*V(n)+sin(time)"})
    assert "Bcw_device_0 cw_node_0 0 V=2*V(cw_node_0)+sin(time)" in emit_deck(data, {"analyses": [{"type": "op"}]})


def test_external_solver_does_not_inherit_credentials_or_startup_commands(monkeypatch):
    from types import SimpleNamespace
    from services.installer import ngspice_backend as ng
    observed = {}
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(ng, "find_ngspice", lambda: "/fixture/ngspice")

    def run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ng.subprocess, "run", run)
    ng._run_ngspice("* fixture\n.end\n", want_raw=False)
    assert "-n" in observed["argv"]
    assert "OPENAI_API_KEY" not in observed["env"]
    assert set(observed["env"]) <= {"PATH", "SystemRoot", "HOME", "SPICE_ASCIIRAWFILE"}
    assert observed["cwd"] == observed["env"]["HOME"]


def test_power_and_hierarchical_node_labels_encoded():
    data = circuit()
    data["nodes"] = ["+3V3", "/sheet/OUT", "0"]
    data["devices"][0]["nodes"] = ["+3V3", "/sheet/OUT"]
    deck = emit_deck(data, {"analyses": [{"type": "tf", "output": "V(/sheet/OUT)", "source": "V1"}]})
    assert "Rcw_device_0 cw_node_0 cw_node_1 1k" in deck
    assert ".tf V(cw_node_1) Vcw_device_1" in deck
    assert "+3V3" not in deck and "/sheet/OUT" not in deck


@pytest.mark.parametrize("node", ["N$2", "Net-(R1-Pad1)", "/VCC", "net with spaces", "cw_node_0"])
def test_eda_labels_never_enter_solver_language(node):
    data = circuit()
    data["nodes"] = [node, "other", "0"]
    data["devices"][0].update(name="R$display", nodes=[node, "other"])
    deck = emit_deck(data, {"analyses": [{"type": "tf", "source": "V$display", "output": f"V({node})"}]})
    assert "Rcw_device_0 cw_node_0 cw_node_1 1k" in deck
    assert ".tf V(cw_node_0) Vcw_device_1" in deck
    assert "R$display" not in deck and "V$display" not in deck
    assert data["devices"][0]["name"] == "R$display"


def test_encoded_eda_labels_restore_original_probe_results():
    from services.installer.ngspice_backend import ngspice_available, simulate_ngspice
    if not ngspice_available():
        pytest.skip("ngspice is not installed")
    data = {"nodes": ["/VCC", "N$2", "0"], "devices": [
        {"kind": "V", "name": "V$source", "nodes": ["/VCC", "0"], "value": 5, "model": None, "params": {}},
        {"kind": "R", "name": "R[bias]", "nodes": ["/VCC", "N$2"], "value": 1000, "model": None, "params": {}},
        {"kind": "R", "name": "R$load", "nodes": ["N$2", "0"], "value": 1000, "model": None, "params": {}},
    ]}
    result = simulate_ngspice(data, {"analyses": [{"type": "op"}],
                                   "probes": ["V(/VCC)", "V(N$2)", "I(V$source)"]})
    assert result["op"] == pytest.approx({"V(/VCC)": 5, "V(N$2)": 2.5, "I(V$source)": -0.0025})
