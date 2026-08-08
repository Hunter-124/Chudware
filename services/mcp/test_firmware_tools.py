"""Firmware compile-loop MCP tools (firmware_tools.py).

Defends the contracts an agent authoring firmware actually depends on:

* SOURCE AUTHORING (the capability hole): a firmware project is scaffolded under
  firmware_root(), outside any fs_* workspace jail, so fs_write cannot reach it and
  ``fw_sources(add=...)`` only ever made an EMPTY stub — firmware source code could not be
  written at all. ``fw_sources(write={relpath: content})`` is the sanctioned path, and the
  test below drives the FULL loop with tool calls only and asserts the exact TEXT is at the
  path the build compiles (plus a real cross-toolchain build when one is installed).
* The argument dialect a live model actually types: ``fw_status(project_id=...)``,
  ``fw_sim_run(run_s=..., power_profile=True)`` — accepted or refused with a message that
  names the tool that does the job, never silently dropped.
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

    written = _call(tools, "fw_sources", project_id=pid,
                    write={"src/main.c": MAIN_C, "src/blink.c": BLINK_C})
    assert "src/blink.c" in written["src_paths"]
    assert {w["path"] for w in written["written"]} == {"src/main.c", "src/blink.c"}
    # A brand-new source is useless unless the build knows about it: the cmake_arm template
    # hard-codes src/main.c + src/startup.c, so fw_sources must extend add_executable().
    assert written["cmake_sources_added"] == ["src/blink.c"]

    # READ BACK from the path the build compiles — not the tool's own ok/echo.
    main_abs = os.path.join(root, "src", "main.c")
    assert next(w for w in written["written"] if w["path"] == "src/main.c")["abs_path"] == main_abs
    with open(main_abs, encoding="utf-8") as fh:
        assert fh.read() == MAIN_C
    # ...and through the tool an agent would use, which is NOT jailed for reads.
    got = _call(tools, "fs_read", path=main_abs, raw=True)
    assert got["ok"] and "CHUDWARE_AUTHORED_MARKER" in got["text"]
    assert "blink_once" in got["text"]
    assert "src/blink.c" in open(os.path.join(root, "CMakeLists.txt"), encoding="utf-8").read()

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


def test_list_add_remove_behaviour_is_unchanged(tools):
    """The legacy shapes must keep their exact old semantics (stub, sorted, no extras)."""
    pid = _call(tools, "fw_new_project", mcu_family="stm32f4")["project_id"]
    listed = _call(tools, "fw_sources", project_id=pid)
    assert listed == {"src_paths": ["src/main.c", "src/startup.c"]}

    added = _call(tools, "fw_sources", project_id=pid, add=["src/stub.c"])
    assert added == {"src_paths": ["src/main.c", "src/startup.c", "src/stub.c"]}
    root = _call(tools, "fw_sources", project_id=pid, write={"src/main.c": MAIN_C})["root_dir"]
    assert os.path.getsize(os.path.join(root, "src", "stub.c")) == 0   # still an empty stub

    removed = _call(tools, "fw_sources", project_id=pid, remove=["src/startup.c"])
    assert removed == {"src_paths": ["src/main.c", "src/stub.c"]}


def test_write_refuses_escaping_and_unknown_projects(tools):
    pid = _call(tools, "fw_new_project", mcu_family="stm32f4")["project_id"]
    esc = _call(tools, "fw_sources", project_id=pid, write={"../../evil.c": "x"})
    assert esc["code"] == "invalid_request" and "escapes" in esc["hint"]

    abs_outside = _call(tools, "fw_sources", project_id=pid, write={"/etc/evil.c": "x"})
    assert abs_outside["code"] == "invalid_request"

    unknown = _call(tools, "fw_sources", project_id="nope", write={"src/main.c": "x"})
    assert unknown["code"] == "unknown_project" and "fw_new_project" in unknown["hint"]

    not_text = _call(tools, "fw_sources", project_id=pid, write={"src/main.c": 17})
    assert not_text["code"] == "invalid_request" and "string" in not_text["hint"]


def test_absolute_path_inside_the_project_is_relativised(tools):
    created = _call(tools, "fw_new_project", mcu_family="stm32f4")
    pid, root = created["project_id"], created["root_dir"]
    out = _call(tools, "fw_sources", project_id=pid,
                write={os.path.join(root, "src", "main.c"): MAIN_C})
    assert out["written"][0]["path"] == "src/main.c"


def test_fs_write_into_a_firmware_project_names_fw_sources(tools, tmp_path, monkeypatch):
    """The other path must not be a dead end (and the jail must not widen)."""
    created = _call(tools, "fw_new_project", mcu_family="stm32f4")
    pid, root = created["project_id"], created["root_dir"]
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(workspace))

    refused = _call(tools, "fs_write", path=os.path.join(root, "src", "main.c"), content=MAIN_C)
    assert refused["ok"] is False
    assert refused["reason"] == "use_fw_sources"
    assert "fw_sources" in refused["hint"] and pid in refused["hint"]
    assert 'write={"src/main.c"' in refused["hint"]
    with open(os.path.join(root, "src", "main.c"), encoding="utf-8") as fh:
        assert "CHUDWARE_AUTHORED_MARKER" not in fh.read()   # jail held: nothing written

    # Unrelated cross-root writes keep the ORIGINAL refusal — the jail did not widen.
    other = _call(tools, "fs_write", path=str(tmp_path / "elsewhere.txt"), content="x")
    assert other["reason"] == "outside_workspace_root"
    assert not (tmp_path / "elsewhere.txt").exists()


# ------------------------------------------------------------------ item 2: fw_status

def test_fw_status_accepts_project_id_and_says_it_is_global(tools):
    bare = _call(tools, "fw_status")
    with_pid = _call(tools, "fw_status", project_id="x")
    note = with_pid.pop("note")
    assert with_pid == bare                      # payload otherwise identical
    assert "GLOBAL" in note and "ignored" in note
    assert "fw_sources(project_id=...)" in note


# ------------------------------------------------------------------ item 3: fw_sim_run

@pytest.fixture
def sim_spy(tools, monkeypatch):
    """Capture the params fw_sim_run hands the engine, without running an emulator."""
    seen = []

    def fake(method, params=None):
        seen.append((method, params or {}))
        if method == "fw_sim_run":
            return {"passed": False, "uart_log": "", "trace_path": fake.trace}
        if method == "fw_power_profile":
            return {"schema": "aicad.powerprofile/1", "rails": []}
        return {}

    fake.trace = ""
    monkeypatch.setattr(cb, "call", fake)
    return tools, seen, fake


def test_run_s_is_applied_as_the_real_duration_knob(sim_spy):
    tools, seen, _ = sim_spy
    out = _call(tools, "fw_sim_run", project_id="p", run_s=1.0)
    assert seen[0][1]["timeout_s"] == 6.0        # engine runs the target for timeout_s - 5
    assert any("run_s=1.0 applied as timeout_s=6" in n for n in out["arg_notes"])


def test_explicit_timeout_s_wins_over_run_s(sim_spy):
    tools, seen, _ = sim_spy
    out = _call(tools, "fw_sim_run", project_id="p", run_s=1.0, timeout_s=40.0)
    assert seen[0][1]["timeout_s"] == 40.0
    assert any("run_s=1.0 ignored" in n for n in out["arg_notes"])


def test_no_duration_argument_leaves_the_engine_default(sim_spy):
    tools, seen, _ = sim_spy
    out = _call(tools, "fw_sim_run", project_id="p")
    assert "timeout_s" not in seen[0][1]
    assert "arg_notes" not in out


def test_power_profile_names_fw_power_profile_and_chains_it(sim_spy):
    tools, seen, fake = sim_spy
    fake.trace = "/tmp/run.trace.json"
    out = _call(tools, "fw_sim_run", project_id="p", power_profile=True)
    note = " ".join(out["arg_notes"])
    assert "fw_power_profile" in note and "trace_path" in note
    assert out["power_profile"]["schema"] == "aicad.powerprofile/1"
    assert ("fw_power_profile", {"trace_path": "/tmp/run.trace.json",
                                 "project_id": "p"}) in seen


def test_power_profile_without_a_trace_still_explains_itself(sim_spy):
    tools, seen, fake = sim_spy
    fake.trace = ""
    out = _call(tools, "fw_sim_run", project_id="p", power_profile=True)
    note = " ".join(out["arg_notes"])
    assert "fw_power_profile" in note and "no trace_path" in note
    assert "power_profile" not in out
    assert [m for m, _ in seen] == ["fw_sim_run"]      # nothing fabricated

@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("run_s", -1.0),
        ("run_s", float("nan")),
        ("timeout_s", float("inf")),
    ],
)
def test_simulation_rejects_invalid_time_bounds_before_rpc(sim_spy, argument, value):
    tools, seen, _ = sim_spy
    out = _call(tools, "fw_sim_run", project_id="p", **{argument: value})
    assert out["code"] == "invalid_request"
    assert not seen


def test_simulation_rejects_bad_images_and_assertions_before_rpc(sim_spy, tmp_path):
    tools, seen, _ = sim_spy
    bad_elf = tmp_path / "bad.elf"
    bad_elf.write_bytes(b"not an elf")
    out = _call(tools, "fw_sim_run", project_id="p", elf_path=str(bad_elf))
    assert out["code"] == "invalid_request" and "not an ELF" in out["hint"]
    assert not seen

    out = _call(tools, "fw_sim_run", project_id="p", assertions=[""])
    assert out["code"] == "invalid_request" and "non-empty" in out["hint"]
    assert not seen


def test_sensor_tool_refuses_ambiguous_nonfinite_and_invalid_grids(sim_spy):
    tools, seen, _ = sim_spy
    profile = {"t_s": [0.0, 1.0], "altitude_m": [10.0, 11.0]}
    sensors = [{"device": "bmp280"}]

    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sim_result=profile,
        sensor_map=sensors)
    assert out["code"] == "invalid_request" and "not both" in out["hint"]
    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sensor_map=sensors, dt_s=0.0)
    assert out["code"] == "invalid_request" and "dt_s" in out["hint"]
    out = _call(
        tools, "fw_sensor_scenario", profile=profile, sensor_map=sensors,
        duration_s=-1.0)
    assert out["code"] == "invalid_request" and "duration_s" in out["hint"]
    out = _call(
        tools, "fw_sensor_scenario",
        profile='{"t_s":[0, NaN], "altitude_m":[10,11]}',
        sensor_map=sensors)
    assert out["code"] == "invalid_request" and "$.t_s[1]" in out["hint"]
    assert not seen


def test_direct_emulator_tools_validate_setup_and_forward_valid_runs(sim_spy, tmp_path):
    tools, seen, _ = sim_spy
    elf = tmp_path / "app.elf"
    header = bytearray(52)
    header[:7] = b"\x7fELF\x01\x01\x01"
    header[16:18] = (2).to_bytes(2, "little")   # ET_EXEC
    header[18:20] = (83).to_bytes(2, "little")  # EM_AVR
    elf.write_bytes(header)
    resc = tmp_path / "app.resc"
    resc.write_text("mach create\n", encoding="utf-8")

    out = _call(
        tools, "fw_emulate_simavr", elf_path=str(elf), mcu="atmega328p", run_s=0.5)
    assert out == {}
    assert seen[-1] == (
        "fw_emulate_simavr",
        {"elf_path": str(elf), "mcu": "atmega328p", "run_s": 0.5},
    )

    out = _call(
        tools, "fw_emulate_renode", resc_path=str(resc), run_s=1.0, gdb=True,
        gdb_port=4444, mi_commands=["-exec-step-instruction"])
    assert out == {}
    assert seen[-1] == (
        "fw_emulate_renode",
        {
            "resc_path": str(resc),
            "run_s": 1.0,
            "gdb": True,
            "gdb_port": 4444,
            "mi_commands": ["-exec-step-instruction"],
        },
    )

    seen.clear()
    out = _call(
        tools, "fw_emulate_simavr", elf_path=str(elf), mcu="atmega328p", run_s=-1.0)
    assert out["code"] == "invalid_request"
    out = _call(
        tools, "fw_emulate_renode", resc_path=str(resc), mi_commands=["-exec-run"])
    assert out["code"] == "invalid_request" and "gdb=True" in out["hint"]
    assert not seen


# ------------------------------------------------------------------ no catch-all

@pytest.mark.parametrize("tool_name", ["fw_status", "fw_sources", "fw_sim_run"])
def test_genuinely_unknown_arguments_are_still_refused(tools, tool_name):
    with pytest.raises(TypeError):
        tools[tool_name](project_id="p", definitely_not_a_real_arg=1)
