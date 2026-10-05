"""Firmware compile-loop MCP tools (firmware_tools.py).

Defends the contracts an agent authoring firmware actually depends on:

* SOURCE AUTHORING (the capability hole): a firmware project is scaffolded under
  firmware_root(), outside any fs_* workspace jail, so fs_write cannot reach it and
  ``fw_sources(add=...)`` only ever made an EMPTY stub — firmware source code could not be
  written at all. ``fw_sources(write={relpath: content})`` is the sanctioned path, and the
  test below drives the FULL loop with tool calls only and asserts the exact TEXT is at the
  path the build compiles (plus a real cross-toolchain build when one is installed).
* Consumer file-I/O boundaries and invalid simulation inputs are refused before launch.
* A genuinely unknown argument is still a hard refusal (no catch-all **kwargs).
"""
from __future__ import annotations

import json
import os

import pytest

import services.mcp.core_bridge as cb
import services.mcp.tool_registry as reg
from services.mcp.firmware_tools import register_firmware_tools
from services.mcp.fs_tools import register_fs_tools

pytestmark = pytest.mark.skipif(
    not cb.available(), reason="libchudware not built — the firmware RPCs are unreachable")


class _FakeMCP:
    """Captures @mcp.tool()-decorated fns by name (what FastMCP does under the hood)."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """fw_* + fs_* tools wired against a PRIVATE firmware root.

    CHUDWARE_ENGINE_DAEMON=0 is mandatory: in daemon mode the RPCs run in the shared
    chudware-hostd process, which never sees this test's CHUDWARE_FIRMWARE_DIR and would
    scaffold projects into the developer's real firmware root.
    """
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    monkeypatch.setenv("CHUDWARE_FIRMWARE_DIR", str(tmp_path / "fwroot"))
    for key in ("CHUDWARE_FS_ROOT", "CHUDWARE_FS_ALLOW_OUTSIDE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    m = _FakeMCP()
    register_firmware_tools(reg.view(m, "ecad"))
    register_fs_tools(reg.view(m, "files"))
    return m.tools


def _call(_tools, _name, **kw):
    out = _tools[_name](**kw)
    return json.loads(out) if isinstance(out, str) else out


MAIN_C = """/* authored by fw_sources(write=...) — CHUDWARE_AUTHORED_MARKER */
#include <stdint.h>

extern void blink_once(void);

int main(void)
{
    for (;;) {
        blink_once();
    }
    return 0;
}
"""

BLINK_C = """#include <stdint.h>

#define GPIO_ODR (*(volatile uint32_t *)0x40020014u)

void blink_once(void)
{
    GPIO_ODR ^= (1u << 5);
}
"""


# ------------------------------------------------------------------ item 1: authoring

def test_authoring_loop_puts_real_source_where_the_build_compiles_it(tools):
    """The whole point: tool calls only, end at a file whose exact TEXT is on disk."""
    created = _call(tools, "fw_new_project", name="authoring", mcu_family="stm32f4")
    pid = created["project_id"]
    root = created["root_dir"]                    # surfaced so the agent can read it back
    assert os.path.isdir(root)

    _call(tools, "fw_sources", project_id=pid,
          write={"src/main.c": MAIN_C, "src/blink.c": BLINK_C})

    # READ BACK from the path the build compiles — not the tool's own ok/echo.
    main_abs = os.path.join(root, "src", "main.c")
    with open(os.path.join(root, "src", "blink.c"), encoding="utf-8") as fh:
        assert fh.read() == BLINK_C
    with open(main_abs, encoding="utf-8") as fh:
        assert fh.read() == MAIN_C
    # ...and through the tool an agent would use — reads are jailed in general
    # (3aaee658), but a REGISTERED firmware project root is the one read carve-out:
    # fw_sources' contract is "write through fw_sources, read it back with fs_read".
    got = _call(tools, "fs_read", path=main_abs, raw=True)
    assert got["ok"]
    assert got["text"] == MAIN_C

    build = _call(tools, "fw_build", project_id=pid)
    if build.get("returncode") == -1:
        pytest.skip(f"cross-toolchain absent: {build.get('stderr', '')[:120]}")
    # The link only resolves blink_once() if the authored file was really compiled.
    assert build["success"] is True, build.get("stderr")
    assert build["elf_path"]


def test_overwrite_replaces_content_and_stays_registered_once(tools):
    pid = _call(tools, "fw_new_project", mcu_family="stm32f4")["project_id"]
    first = _call(tools, "fw_sources", project_id=pid, write={"src/main.c": "int main(void){return 0;}\n"})
    second = _call(tools, "fw_sources", project_id=pid, write={"src/main.c": MAIN_C})
    assert first["src_paths"] == second["src_paths"]
    assert second["src_paths"].count("src/main.c") == 1
    with open(second["written"][0]["abs_path"], encoding="utf-8") as fh:
        assert fh.read() == MAIN_C


def test_add_dict_entries_are_a_write_synonym(tools):
    pid = _call(tools, "fw_new_project", mcu_family="stm32f4")["project_id"]
    out = _call(tools, "fw_sources", project_id=pid,
                add=[{"path": "src/drv.c", "content": "void drv(void){}\n"}])
    assert "src/drv.c" in out["src_paths"]
    with open(out["written"][0]["abs_path"], encoding="utf-8") as fh:
        assert fh.read() == "void drv(void){}\n"


def test_source_add_creates_a_stub_and_remove_deregisters_without_deleting_it(tools):
    created = _call(tools, "fw_new_project", mcu_family="stm32f4")
    pid, root = created["project_id"], created["root_dir"]
    listed = _call(tools, "fw_sources", project_id=pid)
    original = set(listed["src_paths"])

    added = _call(tools, "fw_sources", project_id=pid, add=["src/stub.c"])
    assert set(added["src_paths"]) == original | {"src/stub.c"}
    stub = os.path.join(root, "src", "stub.c")
    with open(stub, encoding="utf-8") as fh:
        assert fh.read() == ""

    removed = _call(tools, "fw_sources", project_id=pid, remove=["src/stub.c"])
    assert set(removed["src_paths"]) == original
    assert os.path.isfile(stub)


def test_write_refuses_escaping_and_unknown_projects(tools):
    pid = _call(tools, "fw_new_project", mcu_family="stm32f4")["project_id"]
    esc = _call(tools, "fw_sources", project_id=pid, write={"../../evil.c": "x"})
    assert esc["code"] == "invalid_request"

    abs_outside = _call(tools, "fw_sources", project_id=pid, write={"/etc/evil.c": "x"})
    assert abs_outside["code"] == "invalid_request"

    unknown = _call(tools, "fw_sources", project_id="nope", write={"src/main.c": "x"})
    assert unknown["code"] == "unknown_project"

    not_text = _call(tools, "fw_sources", project_id=pid, write={"src/main.c": 17})
    assert not_text["code"] == "invalid_request"


def test_absolute_path_inside_the_project_writes_source(tools):
    created = _call(tools, "fw_new_project", mcu_family="stm32f4")
    pid, root = created["project_id"], created["root_dir"]
    _call(tools, "fw_sources", project_id=pid,
          write={os.path.join(root, "src", "main.c"): MAIN_C})
    with open(os.path.join(root, "src", "main.c"), encoding="utf-8") as fh:
        assert fh.read() == MAIN_C


def test_fs_write_cannot_modify_a_firmware_project(tools, tmp_path, monkeypatch):
    """Firmware reads are allowed, but writes must still respect the workspace jail."""
    created = _call(tools, "fw_new_project", mcu_family="stm32f4")
    root = created["root_dir"]
    main_abs = os.path.join(root, "src", "main.c")
    with open(main_abs, encoding="utf-8") as fh:
        original = fh.read()
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(workspace))

    refused = _call(tools, "fs_write", path=os.path.join(root, "src", "main.c"), content=MAIN_C)
    assert refused["ok"] is False
    assert refused["reason"] == "use_fw_sources"
    with open(main_abs, encoding="utf-8") as fh:
        assert fh.read() == original

    # Unrelated cross-root writes keep the ORIGINAL refusal — the jail did not widen.
    other = _call(tools, "fs_write", path=str(tmp_path / "elsewhere.txt"), content="x")
    assert other["reason"] == "outside_workspace_root"
    assert not (tmp_path / "elsewhere.txt").exists()


# ------------------------------------------------------------------ simulation boundaries

@pytest.fixture
def simulation_calls(tools, monkeypatch):
    """Observe whether boundary validation or trace availability permits an RPC."""
    seen = []

    def fake(method, params=None):
        seen.append(method)
        if method == "fw_sim_run":
            return {"trace_path": fake.trace}
        return {}

    fake.trace = ""
    monkeypatch.setattr(cb, "call", fake)
    return tools, seen, fake


def test_scenario_file_failures_and_ambiguous_sources_do_not_launch(simulation_calls, tmp_path):
    tools, seen, _ = simulation_calls
    missing = tmp_path / "missing.json"
    out = _call(tools, "fw_sim_run", project_id="p", scenario_path=str(missing))
    assert out["code"] == "invalid_request"
    path = tmp_path / "bad-encoding.json"
    path.write_bytes(b"\xff")
    out = _call(tools, "fw_sim_run", project_id="p", scenario_path=str(path))
    assert out["code"] == "invalid_request"
    out = _call(tools, "fw_sim_run", project_id="p",
                scenario="fixture:user_button", scenario_path=str(path))
    assert out["code"] == "invalid_request"
    out = _call(tools, "fw_sim_run", project_id="p", backend="pretend")
    assert out["code"] == "invalid_request"
    assert not seen


@pytest.mark.parametrize("text", ["", "{broken", "[]", '"fixture:user_button"'])
def test_saved_file_must_be_a_json_object_not_a_silent_empty_run(simulation_calls, tmp_path, text):
    tools, seen, _ = simulation_calls
    path = tmp_path / "scenario.json"
    path.write_text(text, encoding="utf-8")
    out = _call(tools, "fw_sim_run", project_id="p", scenario_path=str(path))
    assert out["code"] == "invalid_request"
    assert not seen


def test_power_profile_is_available_when_a_trace_exists(simulation_calls):
    tools, seen, fake = simulation_calls
    fake.trace = "/tmp/run.trace.json"
    out = _call(tools, "fw_sim_run", project_id="p", power_profile=True)
    assert "power_profile" in out
    assert "fw_power_profile" in seen


def test_power_profile_is_not_fabricated_without_a_trace(simulation_calls):
    tools, seen, fake = simulation_calls
    fake.trace = ""
    out = _call(tools, "fw_sim_run", project_id="p", power_profile=True)
    assert "power_profile" not in out
    assert "fw_power_profile" not in seen

@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("run_s", -1.0),
        ("run_s", float("nan")),
        ("timeout_s", float("inf")),
    ],
)
def test_simulation_rejects_invalid_time_bounds_before_rpc(simulation_calls, argument, value):
    tools, seen, _ = simulation_calls
    out = _call(tools, "fw_sim_run", project_id="p", **{argument: value})
    assert out["code"] == "invalid_request"
    assert not seen


def test_simulation_rejects_bad_images_and_assertions_before_rpc(simulation_calls, tmp_path):
    tools, seen, _ = simulation_calls
    bad_elf = tmp_path / "bad.elf"
    bad_elf.write_bytes(b"not an elf")
    out = _call(tools, "fw_sim_run", project_id="p", elf_path=str(bad_elf))
    assert out["code"] == "invalid_request"
    assert not seen

    out = _call(tools, "fw_sim_run", project_id="p", assertions=[""])
    assert out["code"] == "invalid_request"
    assert not seen


def test_sensor_tool_refuses_ambiguous_nonfinite_and_invalid_grids(simulation_calls):
    tools, seen, _ = simulation_calls
    profile = {"t_s": [0.0, 1.0], "altitude_m": [10.0, 11.0]}
    sensors = [{"device": "bmp280"}]

    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sim_result=profile,
        sensor_map=sensors)
    assert out["code"] == "invalid_request"
    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sensor_map=sensors, dt_s=0.0)
    assert out["code"] == "invalid_request"
    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sensor_map=sensors,
        duration_s=-1.0)
    assert out["code"] == "invalid_request"
    out = _call(
        tools, "fw_sensor_scenario",
        profile='{"t_s":[0, NaN], "altitude_m":[10,11]}',
        sensor_map=sensors)
    assert out["code"] == "invalid_request"
    assert not seen


def test_direct_emulator_tools_refuse_invalid_setup_before_rpc(simulation_calls, tmp_path):
    tools, seen, _ = simulation_calls
    elf = tmp_path / "app.elf"
    header = bytearray(52)
    header[:7] = b"\x7fELF\x01\x01\x01"
    header[16:18] = (2).to_bytes(2, "little")   # ET_EXEC
    header[18:20] = (83).to_bytes(2, "little")  # EM_AVR
    elf.write_bytes(header)
    resc = tmp_path / "app.resc"
    resc.write_text("mach create\n", encoding="utf-8")

    out = _call(
        tools, "fw_emulate_simavr", elf_path=str(elf), mcu="atmega328p", run_s=-1.0)
    assert out["code"] == "invalid_request"
    out = _call(
        tools, "fw_emulate_renode", resc_path=str(resc), mi_commands=["-exec-run"])
    assert out["code"] == "invalid_request"
    assert not seen


# ------------------------------------------------------------------ no catch-all

@pytest.mark.parametrize("tool_name", ["fw_status", "fw_sources", "fw_sim_run"])
def test_genuinely_unknown_arguments_are_still_refused(tools, tool_name):
    with pytest.raises(TypeError):
        tools[tool_name](project_id="p", definitely_not_a_real_arg=1)
