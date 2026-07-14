"""Tests for the firmware compile-loop MCP handlers (T-C2): fw_new_project / fw_build / fw_sources."""

from __future__ import annotations

import pytest

from engine.ecad import schema
from engine.ecad import session as ecad_session
from engine.firmware import mcp_tools as fw
from engine.firmware import project as fw_project


@pytest.fixture(autouse=True)
def _isolated_firmware_root(tmp_path, monkeypatch):
    """Point every project at a throwaway dir so load()/save() round-trip in-test."""
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path / "fw"))


def _open_board(*net_names: str, mcu_value: str = "STM32F103") -> str:
    """Register a board doc in the ECAD session registry and return its doc_id (a board_doc_ref)."""
    board = schema.new_board("t_c2_board")
    for nm in net_names:
        board["nets"].append({"name": nm, "class": "default"})
    board["footprints"].append({
        "id": "fp1", "fpid": "MCU:LQFP48", "ref": "U1", "value": mcu_value,
        "pos": [10.0, 10.0], "angle": 0.0, "side": "top", "fields": {}, "pads": [],
    })
    return ecad_session.open_session(board).doc_id


# --------------------------------------------------------------------------- fw_new_project


def test_fw_new_project_explicit_mcu():
    res = fw.fw_new_project({"name": "blink", "mcu_family": "cortex-m4"})
    assert "project_id" in res
    assert res["project"]["mcu_family"] == "cortex-m4"
    assert res["project"]["schema"] == "aicad.firmware.project/1"
    # compile_commands.json guarantee (contract §6) — project loads back
    loaded = fw_project.load(res["project_id"])
    assert loaded.mcu_family == "cortex-m4"


def test_fw_new_project_auto_selects_mcu_from_board_bom():
    ref = _open_board("PA5", "MISO", mcu_value="STM32F103")
    res = fw.fw_new_project({"name": "auto", "board_doc_ref": ref})  # no mcu_family
    assert res["project"]["mcu_family"] == "stm32f1"
    assert res["project"]["board_doc_ref"] == ref


def test_fw_new_project_seeds_pinmap_from_board():
    ref = _open_board("PA5", "MISO")
    res = fw.fw_new_project({"board_doc_ref": ref, "mcu_family": "cortex-m4"})
    assert "pinmap_path" in res
    from pathlib import Path
    header = Path(res["pinmap_path"]).read_text(encoding="utf-8")
    assert "#define PIN_PA5" in header
    assert "#define PIN_MISO" in header


def test_fw_new_project_errors_without_mcu_or_board():
    res = fw.fw_new_project({"name": "orphan"})   # no mcu, no board
    assert res.get("code") == "invalid_request"
    assert "mcu" in res["message"]


# --------------------------------------------------------------------------- fw_build


def test_fw_build_returns_build_result_shape():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    res = fw.fw_build({"project_id": proj["project_id"]})
    # DoD: fw_build returns {success, elf_path, stderr, warnings} — assert the full BuildResult shape.
    for key in ("success", "elf_path", "map_path", "stderr", "warnings", "returncode", "cmd", "ms"):
        assert key in res, f"BuildResult missing '{key}'"
    assert isinstance(res["warnings"], list)
    # T-C5: don't assume the cross-toolchain is absent — assert on the real invariant instead
    # (the env-assuming-assertion trap: this used to hard-assert success is False, which broke
    # the moment arm-none-eabi-gcc was actually installed and the build genuinely succeeded).
    if res["success"]:
        assert res["elf_path"] and res["elf_path"].endswith((".elf", ".bin", ".hex"))
        assert res["returncode"] == 0
    else:
        assert res["returncode"] != 0        # -1 (cmake absent) or non-zero (cross-compile fails)
        assert res["elf_path"] is None


def test_fw_build_unknown_project_is_error_envelope():
    res = fw.fw_build({"project_id": "deadbeefcafe"})
    assert res.get("code") == "unknown_project"


def test_fw_build_requires_project_id():
    res = fw.fw_build({})
    assert res.get("code") == "invalid_request"


# --------------------------------------------------------------------------- fw_sources


def test_fw_sources_lists_template_sources():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    res = fw.fw_sources({"project_id": proj["project_id"]})
    assert "src_paths" in res
    # cmake_arm template ships src/main.c + src/startup.c
    assert any(p.endswith("main.c") for p in res["src_paths"])


def test_fw_sources_add_and_remove():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    added = fw.fw_sources({"project_id": pid, "add": ["src/blink.c"]})
    assert "src/blink.c" in added["src_paths"]
    # add materialises a stub so a later build sees it
    loaded = fw_project.load(pid)
    assert (loaded.root_dir / "src/blink.c").is_file()

    removed = fw.fw_sources({"project_id": pid, "remove": ["src/blink.c"]})
    assert "src/blink.c" not in removed["src_paths"]


def test_fw_sources_requires_project_id():
    res = fw.fw_sources({})
    assert res.get("code") == "invalid_request"


# --------------------------------------------------------------------------- fw_sim_run


def test_fw_sim_run_requires_project_id():
    res = fw.fw_sim_run({})
    assert res.get("code") == "invalid_request"


def test_fw_sim_run_unknown_project_is_error_envelope():
    res = fw.fw_sim_run({"project_id": "deadbeefcafe"})
    assert res.get("code") == "unknown_project"


def test_fw_sim_run_no_elf_graceful():
    """No ELF in build dir → SimResult(passed=False) not an error envelope."""
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    # Project was created but never built — no .elf exists.
    res = fw.fw_sim_run({"project_id": proj["project_id"]})
    # Graceful SimResult shape, not an error envelope
    assert "passed" in res, "expected SimResult dict, not error envelope"
    assert res["passed"] is False
    assert "no_elf" in res.get("uart_log", "")
    # assertion_results is empty (nothing ran)
    assert res.get("assertion_results") == []


def test_fw_sim_run_emulator_absent_graceful(tmp_path):
    """Neither renode nor qemu → SimResult(passed=False), not a raise."""
    from unittest.mock import patch

    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    # Materialise a fake ELF so the handler passes the no-elf check.
    import engine.firmware.project as fw_proj
    loaded = fw_proj.load(pid)
    fake_elf = loaded.root_dir / "build" / "firmware.elf"
    fake_elf.parent.mkdir(parents=True, exist_ok=True)
    fake_elf.write_bytes(b"\x7fELF")  # minimal sentinel

    with patch("engine.firmware.emulator.shutil.which", return_value=None):
        res = fw.fw_sim_run({"project_id": pid, "elf_path": str(fake_elf),
                             "assertions": ["uart:BOOT_OK"]})

    assert "passed" in res
    assert res["passed"] is False
    assert "emulator_not_found" in res.get("uart_log", "")


def test_fw_sim_run_happy_path_mocked(tmp_path):
    """Mock run_simulation returns passing SimResult — handler passes it through."""
    from unittest.mock import patch
    from engine.firmware.emulator import SimResult, AssertionResult

    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    import engine.firmware.project as fw_proj
    loaded = fw_proj.load(pid)
    fake_elf = loaded.root_dir / "build" / "firmware.elf"
    fake_elf.parent.mkdir(parents=True, exist_ok=True)
    fake_elf.write_bytes(b"\x7fELF")

    mock_result = SimResult(
        passed=True,
        uart_log="BOOT_OK\nDONE\n",
        gpio_states={"LED": 1},
        assertion_results=[AssertionResult("uart:BOOT_OK", True, "found in uart_log")],
        ms=42.0,
    )

    with patch("engine.firmware.mcp_tools.run_simulation", return_value=mock_result):
        res = fw.fw_sim_run({
            "project_id": pid,
            "elf_path": str(fake_elf),
            "assertions": ["uart:BOOT_OK"],
        })

    assert res["passed"] is True
    assert res["uart_log"] == "BOOT_OK\nDONE\n"
    assert res["gpio_states"] == {"LED": 1}
    assert len(res["assertion_results"]) == 1
    assert res["assertion_results"][0]["passed"] is True
    assert "ms" in res


def test_fw_sim_run_failed_assertions_mocked(tmp_path):
    """Assertion fails → handler returns SimResult with passed=False."""
    from unittest.mock import patch
    from engine.firmware.emulator import SimResult, AssertionResult

    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    import engine.firmware.project as fw_proj
    loaded = fw_proj.load(pid)
    fake_elf = loaded.root_dir / "build" / "firmware.elf"
    fake_elf.parent.mkdir(parents=True, exist_ok=True)
    fake_elf.write_bytes(b"\x7fELF")

    mock_result = SimResult(
        passed=False,
        uart_log="BOOT_FAILED\n",
        assertion_results=[AssertionResult("uart:BOOT_OK", False, "'BOOT_OK' not found")],
        ms=10.0,
    )

    with patch("engine.firmware.mcp_tools.run_simulation", return_value=mock_result):
        res = fw.fw_sim_run({
            "project_id": pid,
            "elf_path": str(fake_elf),
            "assertions": ["uart:BOOT_OK"],
        })

    assert res["passed"] is False
    assert res["assertion_results"][0]["passed"] is False


def test_fw_sim_run_simresult_shape(tmp_path):
    """Verify SimResult dict shape matches contract §4 exactly."""
    from unittest.mock import patch
    from engine.firmware.emulator import SimResult

    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    import engine.firmware.project as fw_proj
    loaded = fw_proj.load(pid)
    fake_elf = loaded.root_dir / "build" / "firmware.elf"
    fake_elf.parent.mkdir(parents=True, exist_ok=True)
    fake_elf.write_bytes(b"\x7fELF")

    with patch("engine.firmware.mcp_tools.run_simulation",
               return_value=SimResult(passed=True, uart_log="ok", ms=1.0)):
        res = fw.fw_sim_run({"project_id": pid, "elf_path": str(fake_elf)})

    for field in ("passed", "uart_log", "gpio_states", "assertion_results", "ms"):
        assert field in res, f"SimResult dict missing contract field '{field}'"


# --------------------------------------------------------------------------- fw_status (T-FIN4)


class TestFwStatus:
    """Shape tests for the provisioning doctor — must pass with ANY tool availability,
    so every probe path is mocked (shutil.which / subprocess.run in provision's namespace)."""

    @staticmethod
    def _status(which_map=None, version_text="ver 1.2.3"):
        from unittest.mock import patch, MagicMock
        from engine.firmware import provision

        which_map = which_map or {}

        def fake_which(alias):
            return which_map.get(alias)

        proc = MagicMock(stdout=version_text, stderr="")
        with patch.object(provision.shutil, "which", side_effect=fake_which), \
             patch.object(provision.subprocess, "run", return_value=proc):
            return provision.fw_status()

    def test_shape_all_missing(self):
        res = self._status({})
        assert res["ok"] is True                       # never fails
        for key in ("tools", "families", "banners", "missing"):
            assert key in res
        for name in ("cmake", "ninja", "arm-none-eabi-gcc", "renode",
                     "qemu-system-arm", "probe-rs", "esptool"):
            t = res["tools"][name]
            assert t["found"] is False
            assert t["path"] is None and t["version"] is None
            assert t["install_hint"]                    # actionable hint always present
            assert isinstance(t["unlocks"], list)
            assert name in res["missing"]
        for family in ("cortex-m", "esp32", "avr"):
            fam = res["families"][family]
            for cap in ("can_build", "can_emulate", "can_flash"):
                assert fam[cap] is False
            assert isinstance(fam["missing"], list)
        assert res["banners"]                           # fix-it guidance emitted

    def test_cortex_m_fully_provisioned(self):
        res = self._status({
            "cmake": "/usr/bin/cmake",
            "arm-none-eabi-gcc": "/usr/bin/arm-none-eabi-gcc",
            "renode": "/usr/bin/renode",
            "probe-rs": "/usr/bin/probe-rs",
        })
        fam = res["families"]["cortex-m"]
        assert fam == {"can_build": True, "can_emulate": True,
                       "can_flash": True, "missing": []}
        assert res["tools"]["cmake"]["version"] == "1.2.3"
        assert res["tools"]["cmake"]["path"] == "/usr/bin/cmake"
        # esp32 remains honestly unprovisioned
        assert res["families"]["esp32"]["can_build"] is False

    def test_qemu_fallback_unlocks_emulation(self):
        res = self._status({"qemu-system-arm": "/usr/bin/qemu-system-arm"})
        assert res["families"]["cortex-m"]["can_emulate"] is True
        assert res["families"]["cortex-m"]["can_build"] is False

    def test_esptool_alias_resolution(self):
        # esptool.py alias (pip install) must satisfy the "esptool" descriptor
        res = self._status({"esptool.py": "/usr/local/bin/esptool.py"})
        t = res["tools"]["esptool"]
        assert t["found"] is True and t["path"].endswith("esptool.py")
        assert res["families"]["esp32"]["can_flash"] is True
        assert "esptool" not in res["missing"]

    def test_version_probe_failure_degrades_not_raises(self):
        from unittest.mock import patch
        from engine.firmware import provision

        with patch.object(provision.shutil, "which", return_value="/usr/bin/cmake"), \
             patch.object(provision.subprocess, "run",
                          side_effect=OSError("boom")):
            res = provision.fw_status()
        assert res["ok"] is True
        assert res["tools"]["cmake"]["found"] is True
        assert res["tools"]["cmake"]["version"] is None

    def test_mcp_wrapper_returns_json_shape(self):
        """The services/mcp fw_status tool returns the same shape as JSON."""
        import json as _json
        import sys
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo / "services" / "mcp"))
        try:
            import firmware_tools
        finally:
            sys.path.pop(0)

        captured = {}

        class StubMCP:
            def tool(self):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco

        firmware_tools.register_firmware_tools(StubMCP())
        assert "fw_status" in captured

        from engine.firmware import provision
        proc = MagicMock(stdout="v9.9", stderr="")
        with patch.object(provision.shutil, "which", return_value=None), \
             patch.object(provision.subprocess, "run", return_value=proc):
            payload = _json.loads(captured["fw_status"]())
        assert payload["ok"] is True
        assert "families" in payload and "tools" in payload
