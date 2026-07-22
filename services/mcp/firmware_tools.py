"""Firmware compile-loop MCP tools — ``fw_new_project`` / ``fw_build`` / ``fw_sources`` (T-C2).

Thin FastMCP wrappers over the C++ core via :mod:`services.mcp.core_bridge` (flipped off
the deleted ``engine.firmware.mcp_tools`` in T-RF15), mirroring ``services/mcp/ecad_tools.py``
exactly: each ``@mcp.tool()`` returns a JSON string and calls the worker-RPC method.
Registration is deferred behind :func:`register_firmware_tools` so ``server.py`` wires us
in with a two-line block and the tools can be driven against a stub ``mcp`` in tests without the
whole stdio server.

The compile loop these expose: create a project whose MCU target and ``pinmap.h`` come from the
linked ECAD board → build with a real cross-toolchain → read parsed warnings/errors → fix → repeat.
"""

from __future__ import annotations

import json
from typing import Optional

from . import core_bridge as cb


def _dumps(payload) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", **extra) -> dict:
    """Structured error envelope (mega-24: fw_sensor_scenario used _err without defining it)."""
    out = {"ok": False, "error": str(reason), "code": str(reason)}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def register_firmware_tools(mcp) -> None:
    """Register every firmware compile-loop tool on the given FastMCP instance."""

    @mcp.tool()
    def fw_status() -> str:
        """Firmware provisioning doctor (T-FIN4) — call FIRST before any fw_* work.

        Detects each firmware CLI (cmake, ninja, arm-none-eabi-gcc, renode,
        qemu-system-arm, probe-rs, esptool, idf.py, arduino-cli) and returns
        ``{ok, tools, families, platforms, banners, missing}``:

        - ``tools[name]`` = ``{found, path, version, install_hint, unlocks}``
        - ``families`` = per-MCU-family rollup (``cortex-m`` / ``esp32`` / ``avr``)
          with ``can_build`` / ``can_emulate`` / ``can_flash`` booleans plus the
          ``missing`` tool list that would unlock each family.
        - ``platforms`` = T-FIN2 Renode platform capability matrix (``stm32f4`` /
          ``stm32f1`` / ``nrf52`` / ``rp2040`` / ``esp32`` / ``cortex-m`` →
          ``{file, emulation, uart, gpio, gpio_observe, adc, i2c, spi, timers,
          notes}``). ESP32 is build+flash only (``emulation="none"``).
        - ``banners`` = human-readable fix-it lines; ``missing`` = absent core tools.

        Never fails (mirrors reveng_status / em_status): an unprovisioned machine
        still returns ``ok=true`` with everything ``found=false`` and install hints.
        Offline-safe — reads PATH + `--version` only, never the network.
        """
        try:
            return _dumps(cb.call("fw_status", {}))
        except Exception as exc:  # noqa: BLE001 — the doctor itself never errors out
            return _dumps({"ok": True, "tools": {}, "families": {}, "banners": [
                f"fw_status probe degraded: {exc}"], "missing": []})

    @mcp.tool()
    def fw_new_project(name: str = "", mcu_family: str = "", toolchain: str = "cmake",
                       board_doc_ref: str = "") -> str:
        """Create an embedded-firmware project (CMake + arm-none-eabi baseline) and return its id.

        Pass ``board_doc_ref`` (an open ECAD board doc_id or a ``.ecad+``/``.json`` path) to link the
        firmware to a PCB: the MCU family is then auto-selected from the board's BOM when
        ``mcu_family`` is left empty, and a ``build/include/pinmap.h`` header of
        ``#define PIN_<NET> <n>`` is generated from the board's net names. Returns
        ``{project_id, project, pinmap_path?}``.
        """
        params: dict = {"toolchain": toolchain or "cmake"}
        if name:
            params["name"] = name
        if mcu_family:
            params["mcu_family"] = mcu_family
        if board_doc_ref:
            params["board_doc_ref"] = board_doc_ref
        return _dumps(cb.call("fw_new_project", params))

    @mcp.tool()
    def fw_build(project_id: str) -> str:
        """Build a firmware project with its toolchain and return the BuildResult:
        ``{success, elf_path, map_path, stderr, warnings, returncode, cmd, ms}``. A missing
        cross-toolchain is reported as ``success=false, returncode=-1`` (not an error) so the agent
        can surface an install hint rather than crash the loop.
        """
        return _dumps(cb.call("fw_build", {"project_id": project_id}))

    @mcp.tool()
    def fw_sources(project_id: str, add: Optional[list[str]] = None,
                   remove: Optional[list[str]] = None) -> str:
        """List the project's source files, or add/remove them (paths relative to the project root).
        Adding a new path creates an empty stub so the next build sees it. Returns ``{src_paths}``.
        """
        params: dict = {"project_id": project_id}
        if add:
            params["add"] = add
        if remove:
            params["remove"] = remove
        return _dumps(cb.call("fw_sources", params))

    @mcp.tool()
    def fw_sim_run(project_id: str, elf_path: str = "", scenario: str = "",
                   assertions: Optional[list[str]] = None) -> str:
        """Run the firmware emulation loop via Renode or QEMU and return SimResult.

        SimResult fields: ``{passed, uart_log, gpio_states, assertion_results, ms,
        gpio_events, trace_path}``. ``passed`` is True iff every assertion matched.
        Assertions:
        - ``"uart:<text>"`` — UART/semihosting output contains ``<text>``.
        - ``"gpio:<PIN>=<level>"`` — GPIO pin is at the given logic level at end of run.
          Renode observes pins directly (state-change hooks + ODR snapshot, e.g. PA5 on
          stm32f4_discovery); on the QEMU fallback the firmware must report events via the
          ``AICAD_GPIO_TRACE(pin, level, t_ms)`` macro from the project's ``aicad_trace.h``.
        - ``"gpio:<PIN>=<level>@><T>ms"`` (also ``@<`` / ``@``) — timeline assertion:
          a time-stamped event shows the pin at that level after/before/at-or-after T ms.
        - ``"toggles:<PIN>>=<N>"`` (also <=, ==, >, <) — pin level changed N times (T-FIN3).
        - ``"period:<PIN>=<P>ms±<TOL>ms"`` — mean rising-edge period within P±TOL (T-FIN3).
        - ``"uart_within:<text>@<T>ms"`` — text in a time-stamped UART event at or before
          T ms (timestamps come from the scenario timeline's AICAD_T_MARK marks; T-FIN3).
        - plain string — treated as uart match.

        ``scenario`` (T-FIN3, schema ``aicad.fwscenario/1``, contracts/firmware_rpc.md §9)
        binds declarative sensor/GPIO stimulus to the emulated peripherals. Pass a JSON
        object string or a fixture reference: ``"fixture:i2c_temp_tmp103"`` (I2C TMP103
        temp sensor with programmed temperature), ``"fixture:spi_imu_stub"``,
        ``"fixture:quadrature_encoder"`` (PA0/PA1, 24 steps CW), ``"fixture:adc_thermistor"``
        (NTC heat-up curve via the Python-peripheral ADC stub), ``"fixture:user_button"``.
        Track kinds: gpio_edges / button / quadrature / i2c_sensor / i2c_registers /
        spi_stub / uart_rx / adc_waveform (sine|ramp|samples — STM32 families only; no
        ADC model ships upstream, other families refuse honestly). Scenarios run on the
        Renode path ONLY; an invalid or platform-unsupported scenario refuses honestly
        (``invalid_scenario`` envelope, or ``scenario_error`` / ``scenario_unsupported``
        in ``uart_log``) — never a silent drop.

        ``gpio_events`` is the time-ordered pin timeline; ``trace_path`` points at a JSON
        trace artifact (schema ``aicad.firmware.trace/1``, contracts/firmware_rpc.md §7)
        with interleaved pin + UART events, written next to the ELF.

        Supported targets (T-FIN2 platform pack): stm32f4 (uart+gpio+i2c+spi+timers),
        stm32f1 (uart+gpio+i2c+timers), nrf52 (uart+i2c+spi+timers; NO gpio assertions),
        rp2040 (bare-metal uart only), cortex-m generic (no peripherals; QEMU semihosting
        path). ESP32 has NO emulation — build+flash only. When an assertion needs a
        peripheral the platform lacks, the run refuses honestly: ``passed=False`` with a
        ``platform_capability`` explanation per assertion (see fw_status ``platforms``).

        T-FIN7: when the project is linked to an ECAD board (``board_doc_ref``) and a
        trace was written, the result also carries ``power_profile_path`` — an
        ``aicad.powerprofile/1`` artifact (``<elf>.power.json``, contract §11): duty-
        cycle-weighted per-rail current profile (CPU run/sleep residency + per-pin GPIO
        duty x the board's T-FIN8 ``eng.supply.rails`` currents). Bookkeeping, not
        SPICE. Use fw_power_profile to derive one from an existing trace.

        Falls back gracefully when neither Renode nor QEMU is installed (``passed=False``,
        ``uart_log`` explains what's missing). Run ``fw_build`` first to produce the ELF.
        """
        p: dict = {"project_id": project_id}
        if elf_path:
            p["elf_path"] = elf_path
        if scenario:
            p["scenario"] = scenario
        if assertions:
            p["assertions"] = assertions
        return _dumps(cb.call("fw_sim_run", p))

    @mcp.tool()
    def fw_verify_board(board_doc_ref: str, project_id: str = "",
                        sch_doc_ref: str = "", mcu_family: str = "",
                        trace_path: str = "", emulate: bool = False) -> str:
        """Firmware↔board co-verification (T-FIN6): check the firmware BACK against the
        PCB before fab and return ERC/DRC-shaped findings.

        STATIC checks (always run): every firmware-referenced ``PIN_<NET>`` exists as a
        board net and lands on the MCU footprint; boot-strap/reset pins (BOOT0/NRST,
        ESP32 GPIO0/2/12/15/EN...) not repurposed as signals; firmware never drives a net
        that already has another push-pull driver or an input-only pin (needs
        ``sch_doc_ref`` for netlist pin types — skipped honestly without it); every MCU
        supply rail has a decoupling capacitor (rails from the T-FIN8 eng metadata +
        power nets on the MCU's pads).

        DYNAMIC check: pass ``trace_path`` (an ``aicad.firmware.trace/1`` artifact from a
        previous fw_sim_run, ``<elf>.trace.json``) OR ``emulate=true`` (runs the project's
        built ELF through Renode/QEMU live) to diff OBSERVED pin activity against the
        netlist — e.g. the firmware toggling a pin whose net is driven by a peripheral.

        Firmware pin references come from the project's sources (``project_id``);
        ``pinmap.h`` itself is excluded. ``board_doc_ref``/``sch_doc_ref`` accept an open
        ECAD doc_id or a ``.ecad+``/``.json`` path.

        Rule ids: fw_pin_unknown_net, fw_pin_not_on_mcu, fw_strap_repurposed,
        fw_direction_clash, fw_drive_input_only, fw_missing_decouple,
        fw_trace_drive_conflict, fw_trace_unmapped_pin. Each finding is
        ``{rule, severity, net, pins, xy, message, fix}`` (the ERC violation shape plus a
        fix hint). Returns ``{ok, mcu_family, findings, summary, fw_pins, dynamic}``.
        Advisory — findings never block; missing emulators degrade gracefully
        (``dynamic.reason``).
        """
        p: dict = {"board_doc_ref": board_doc_ref}
        if project_id:
            p["project_id"] = project_id
        if sch_doc_ref:
            p["sch_doc_ref"] = sch_doc_ref
        if mcu_family:
            p["mcu_family"] = mcu_family
        if trace_path:
            p["trace_path"] = trace_path
        if emulate:
            p["emulate"] = True
        return _dumps(cb.call("fw_verify_board", p))

    @mcp.tool()
    def fw_power_profile(trace_path: str, project_id: str = "",
                         board_doc_ref: str = "", mcu_family: str = "",
                         window_ms: float = 0.0) -> str:
        """Derive an ``aicad.powerprofile/1`` power/duty-cycle profile (T-FIN7,
        contracts/firmware_rpc.md §11) from an EXISTING emulation trace — no live
        emulation needed.

        ``trace_path`` is the ``aicad.firmware.trace/1`` artifact a previous fw_sim_run
        returned (``<elf>.trace.json``). The profile combines:
        - CPU run/sleep residency — integrated from optional firmware-side
          ``AICAD_CPU_TRACE(state, t_ms)`` markers (aicad_trace.h); no markers →
          honestly assumed 100 % active (``source: "assumed_active"``).
        - Per-pin GPIO/PWM duty cycles — time-stamped events integrated over the run
          window (unstamped Renode hook events → duty null + toggle count; no events →
          the final-snapshot level).
        - Per-rail currents — each board component's T-FIN8 ``eng.supply.rails``
          active/sleep currents (contracts/ecad/boardphys.md) weighted by its activity
          (MCU → CPU residency; loads on nets mapped to observed pins → pin duty; else
          assumed always active). Unknown currents stay null and are listed in
          ``coverage`` — never an invented 0.

        Returns ``{schema, model, trace_ref, window_ms, cpu, pins, rails:[{name,
        voltage_v_nom, i_avg_a, i_peak_a, residency, schedule, provenance}], coverage}``.
        BOOKKEEPING, not SPICE — ``model`` says so. Each rail's ``schedule`` is the
        direct ``{"duty", "period_s"}`` heat-load schedule run_fea_thermal transient
        studies accept (duty-cycle heat loads), and the rails feed the system power
        budget without re-parsing raw traces.

        ``board_doc_ref`` (an open ECAD doc_id or ``.ecad+``/``.json`` path) beats the
        project's linked board; with neither, the activity half still returns
        (``rails: []`` + coverage note). ``window_ms`` overrides the run window when the
        trace has no time-stamped events.
        """
        p: dict = {"trace_path": trace_path}
        if project_id:
            p["project_id"] = project_id
        if board_doc_ref:
            p["board_doc_ref"] = board_doc_ref
        if mcu_family:
            p["mcu_family"] = mcu_family
        if window_ms:
            p["window_ms"] = window_ms
        return _dumps(cb.call("fw_power_profile", p))

    # -----------------------------------------------------------------------
    # Hardware path (T-C4): probe-rs flash/debug + esptool ESP32 flash
    # -----------------------------------------------------------------------

    @mcp.tool()
    def fw_flash(elf_path: str, chip: str = "", port: str = "",
                 baud: int = 460800, flash_offset: str = "0x0") -> str:
        """Flash an ELF/bin to a target board and return FlashResult.

        Uses probe-rs (MIT/Apache-2.0) when ``port`` is absent or the image is an ``.elf``;
        falls back to esptool.py (GPL-2.0+, subprocess-only) for ESP32 when ``port`` is
        provided and the image is a ``.bin``.

        FlashResult fields: ``{success, output, returncode, cmd, ms}``.
        ``success=False`` with a descriptive ``output`` when the tool binary is absent —
        never an error envelope. Install hint: ``cargo install probe-rs-tools`` (probe-rs)
        or ``pip install esptool`` (ESP32).

        ``chip`` is the probe-rs chip identifier (e.g. ``"STM32F407VGTx"``,
        ``"nRF52840_xxAA"``). See ``probe-rs chip list`` for the full catalogue.

        [~] Live flash requires a connected DAP probe (probe-rs) or USB serial (esptool).
        """
        p: dict = {"elf_path": elf_path}
        if chip:
            p["chip"] = chip
        if port:
            p["port"] = port
        if baud != 460800:
            p["baud"] = baud
        if flash_offset != "0x0":
            p["flash_offset"] = flash_offset
        return _dumps(cb.call("fw_flash", p))

    @mcp.tool()
    def fw_debug_start(chip: str) -> str:
        """Reset and start the target via probe-rs; returns FlashResult.

        Brings the board out of halt or error state so normal execution resumes.
        Uses ``probe-rs reset --chip <chip>``.

        [~] Live run requires a connected DAP probe.
        """
        return _dumps(cb.call("fw_debug_start", {"chip": chip}))

    @mcp.tool()
    def fw_debug_halt(chip: str) -> str:
        """Reset the target into halt via probe-rs; returns FlashResult.

        Stops execution so memory/registers can be inspected with fw_debug_read_mem.
        Uses ``probe-rs reset --chip <chip> --halt``.

        [~] Live run requires a connected DAP probe.
        """
        return _dumps(cb.call("fw_debug_halt", {"chip": chip}))

    @mcp.tool()
    def fw_debug_read_mem(chip: str, address: str, length: int) -> str:
        """Read *length* bytes from *address* on a halted target via probe-rs.

        Returns ``{success, data_hex, address, length, ms}``. ``data_hex`` is the raw
        bytes in lowercase hex. When probe-rs is absent or the read fails, ``success=False``
        with ``reason`` explaining the failure.

        ``address`` accepts decimal or hex strings (e.g. ``"0x20000000"``).
        Call fw_debug_halt first to halt the core.

        [~] Live run requires a connected DAP probe and a halted target.
        """
        return _dumps(cb.call("fw_debug_read_mem", {"chip": chip, "address": address, "length": length}))

    @mcp.tool()
    def fw_sensor_scenario(profile: str = "", sensor_map: Optional[list] = None,
                           duration_s: float = 0.0, dt_s: float = 0.01,
                           name: str = "", sim_result: str = "") -> str:
        """Build an ``aicad.fwscenario/1`` from a flight/CFD/kinematic profile (Wave-3 S1).

        Bridge from SIMULATED REALITY into the emulated MCU: physics-accurate sensor
        models (MPU6050 IMU, BMP280 baro, LIS3MDL mag, NEO-6M GNSS, pitot/airspeed)
        convert a time-series profile into time-varying ``i2c_register_stream`` tracks
        (and GNSS ``uart_rx`` NMEA) that ``fw_sim_run`` can play back via Renode's
        ``Mocks.DummyI2CSlave``.

        Args:
          profile: JSON object string of a flight profile / trajectory, e.g.
            ``{"t_s":[...], "altitude_m":[...], "yaw_rad":[...], "vel_ned_m_s":[...],
            "omega_body_rad_s":[...]}``. Also accepts CFD summaries
            ``{"t_s":[...], "airspeed_m_s":[...], "altitude_m":[...]}``.
          sensor_map: list of ``{device, bus?, address?, kind?, uart?}``. Devices:
            ``mpu6050``/``imu``, ``bmp280``/``baro``, ``lis3mdl``/``mag``,
            ``neo6m``/``gnss``/``gps``, ``pitot``/``airspeed``.
          duration_s: optional run length (0 → use profile span).
          dt_s: sample-and-hold grid step seconds (default 0.01).
          name: optional scenario display name.
          sim_result: alternate JSON string for the profile (same schema as profile).

        Returns JSON ``{scenario, n_tracks, devices, dt_s, duration_s, ms}``. The
        ``scenario`` object is a valid ``aicad.fwscenario/1`` ready for
        ``fw_sim_run(scenario=...)``. Building the scenario is pure Python (no Renode
        required); *running* it still needs Renode — when Renode is absent,
        ``fw_sim_run`` honest-skips with an explanation in ``uart_log`` (Renode 1.16.1
        is installed on the flight-computer benchmark host).

        Example (climb+turn into IMU+baro)::

            sc = fw_sensor_scenario(
                profile='{"t_s":[0,1,2], "altitude_m":[100,105,110],
                          "yaw_rad":[0,0.25,0.5], "vel_ned_m_s":[[25,0,-5],[25,0,-5],[25,0,-5]],
                          "omega_body_rad_s":[[0,0,0.25],[0,0,0.25],[0,0,0.25]]}',
                sensor_map=[{"device":"mpu6050","address":0x68},
                            {"device":"bmp280","address":0x76}],
                dt_s=0.05)
            fw_sim_run(project_id, scenario=sc["scenario"], assertions=[...])
        """
        p: dict = {"dt_s": float(dt_s)}
        if duration_s and float(duration_s) > 0:
            p["duration_s"] = float(duration_s)
        if name:
            p["name"] = name
        if sensor_map:
            p["sensor_map"] = sensor_map
        # profile / sim_result may arrive as JSON strings from the MCP wire
        def _parse(blob: str):
            if not blob:
                return None
            if isinstance(blob, (dict, list)):
                return blob
            try:
                return json.loads(blob)
            except (TypeError, json.JSONDecodeError) as exc:
                return _err("invalid_request", f"profile is not valid JSON: {exc}")

        if profile:
            parsed = _parse(profile)
            if isinstance(parsed, dict) and parsed.get("code"):
                return _dumps(parsed)
            p["profile"] = parsed
        if sim_result:
            parsed = _parse(sim_result)
            if isinstance(parsed, dict) and parsed.get("code"):
                return _dumps(parsed)
            p["sim_result"] = parsed
        return _dumps(cb.call("fw_sensor_scenario", p))
