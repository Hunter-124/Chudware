"""Firmware compile-loop MCP tools — ``fw_new_project`` / ``fw_build`` / ``fw_sources``.

Thin FastMCP wrappers over the C++ core via :mod:`services.mcp.core_bridge` (flipped off
the deleted ``engine.firmware.mcp_tools``), mirroring ``services/mcp/ecad_tools.py``
exactly: each ``@mcp.tool()`` returns a JSON string and calls the worker-RPC method.
Registration is deferred behind :func:`register_firmware_tools` so ``server.py`` wires us
in with a two-line block and the tools can be driven against a stub ``mcp`` in tests without the
whole stdio server.

The compile loop these expose: create a project whose MCU target and ``pinmap.h`` come from the
linked ECAD board → author its sources with ``fw_sources(write={...})`` (the project root sits
outside the fs_* workspace jail, so fs_write cannot reach it) → build with a real cross-toolchain
→ read parsed warnings/errors → fix → repeat.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
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


def _nonfinite_path(value, path: str = "$") -> str:
    """Return the first nested path containing NaN/Inf, or ``""`` when finite."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if not math.isfinite(float(value)):
                return path
        except (OverflowError, TypeError, ValueError):
            return path
    if isinstance(value, dict):
        for key, child in value.items():
            found = _nonfinite_path(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _nonfinite_path(child, f"{path}[{index}]")
            if found:
                return found
    return ""


def _finite_number(value, *, positive: bool = False, nonnegative: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    if positive:
        return number > 0.0
    if nonnegative:
        return number >= 0.0
    return True


def _file_problem(raw_path, label: str, *, elf: bool = False,
                  expected_machine: Optional[int] = None) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return f"{label} must be a non-empty path"
    try:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return f"{label} does not name a regular file: {path}"
    except (OSError, ValueError) as exc:
        return f"{label} is not a usable path: {exc}"
    if elf:
        try:
            with path.open("rb") as stream:
                header = stream.read(20)
            if len(header) != 20 or header[:4] != b"\x7fELF":
                return f"{label} is not an ELF image: {path}"
            if header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1:
                return f"{label} has an invalid ELF identification header: {path}"
            byteorder = "little" if header[5] == 1 else "big"
            image_type = int.from_bytes(header[16:18], byteorder)
            machine = int.from_bytes(header[18:20], byteorder)
            if image_type not in (2, 3) or machine == 0:
                return f"{label} is not an executable ELF image: {path}"
            if expected_machine is not None and machine != expected_machine:
                return (
                    f"{label} targets ELF machine {machine}, "
                    f"expected {expected_machine}: {path}"
                )
            if path.stat().st_size < (52 if header[4] == 1 else 64):
                return f"{label} has a truncated ELF header: {path}"
        except OSError as exc:
            return f"{label} cannot be read: {exc}"
    return ""


# --------------------------------------------------------------------- project roots
# A firmware project lives OUTSIDE the fs_tools workspace jail: the engine puts it under
# firmware_root() ($CHUDWARE_FIRMWARE_DIR, else <tmp|APPDATA>/Chudware/firmware). So
# fs_write(path="<project>/src/main.c") is refused as outside_workspace_root and, before
# this, firmware source code could not be authored AT ALL — fw_sources could only create
# empty stubs. fw_sources(write={relpath: content}) is the sanctioned authoring path;
# fs_tools imports known_project_roots()/project_for_path() so its refusal NAMES that tool
# instead of dead-ending. The jail itself is untouched.
_PROJECT_ROOTS: dict[str, Path] = {}

# Extensions the cmake_arm template's add_executable() can compile (mirrors kSrcExts in
# core/firmware/project.cpp).
_COMPILABLE_EXTS = frozenset({".c", ".cpp", ".cc", ".cxx", ".s"})



def _firmware_root() -> Path:
    """Mirror of ``firmware_root()`` in core/firmware/project.cpp (fallback resolver)."""
    raw = os.environ.get("CHUDWARE_FIRMWARE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path(tempfile.gettempdir())
    return base / "Chudware" / "firmware"


def project_root(project_id: str) -> Optional[Path]:
    """Absolute root directory of *project_id*, or None when it cannot be resolved.

    Authoritative source is the engine's own ``fw_projects`` listing (it reports
    ``root_dir`` per project, so a relocated or env-overridden root is still correct);
    the firmware_root() mirror above is only the fallback.
    """
    if not project_id:
        return None
    cached = _PROJECT_ROOTS.get(project_id)
    if cached is not None and cached.is_dir():
        return cached
    try:
        listing = cb.call("fw_projects", {}) or {}
        for proj in listing.get("projects") or []:
            pid, root = proj.get("project_id"), proj.get("root_dir")
            if pid and root:
                _PROJECT_ROOTS[pid] = Path(root)
    except Exception:  # noqa: BLE001 — resolution falls back to the on-disk mirror
        pass
    found = _PROJECT_ROOTS.get(project_id)
    if found is not None and found.is_dir():
        return found
    guess = _firmware_root() / project_id
    if (guess / "project.json").is_file():
        _PROJECT_ROOTS[project_id] = guess
        return guess
    return None


def known_project_roots() -> dict[str, Path]:
    """``{project_id: root}`` for every firmware project THIS process has resolved
    (via fw_new_project or fw_sources). Read by services/mcp/fs_tools.py so a write
    refused inside a firmware project can name fw_sources(write=...)."""
    return dict(_PROJECT_ROOTS)


def project_for_path(raw_path) -> Optional[tuple[str, Path, str]]:
    """``(project_id, root, relpath)`` when *raw_path* lands inside a known firmware
    project root, else None. ``relpath`` is '' for the root itself."""
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=False)
    except Exception:  # noqa: BLE001
        return None
    for pid, root in _PROJECT_ROOTS.items():
        try:
            root_res = root.resolve(strict=False)
        except Exception:  # noqa: BLE001
            continue
        if resolved == root_res:
            return (pid, root, "")
        if root_res in resolved.parents:
            return (pid, root, resolved.relative_to(root_res).as_posix())
    return None


def _safe_relpath(rel, root: Path) -> tuple[Optional[str], str]:
    """Normalise one project-relative source path. Returns ``(relpath, error)``; an
    absolute path inside *root* is relativised, anything escaping the project refuses."""
    text = str(rel).strip().replace("\\", "/")
    if not text:
        return (None, "empty source path")
    p = Path(text)
    if p.is_absolute() or (len(text) > 1 and text[1] == ":"):
        try:
            text = p.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
        except Exception:  # noqa: BLE001
            return (None, f"{rel} is an absolute path outside the project root ({root})")
    parts = [seg for seg in text.split("/") if seg not in ("", ".")]
    if not parts:
        return (None, f"{rel} does not name a file")
    if ".." in parts:
        return (None, f"{rel} escapes the project root ({root})")
    return ("/".join(parts), "")


_ADD_EXECUTABLE_RE = re.compile(r"add_executable\s*\(([^)]*)\)", re.I)


def _ensure_cmake_sources(root: Path, rel_paths: list[str]) -> list[str]:
    """Append newly authored compilable sources to the project's ``add_executable()``
    list so the next fw_build actually COMPILES them.

    The cmake_arm template hard-codes ``src/main.c`` + ``src/startup.c``
    (core/firmware/templates_embedded.cpp), so a file registered only in project.json
    src_paths was never handed to the compiler. Idempotent; never fatal — a project with
    a non-cmake or hand-edited CMakeLists is left alone and reports nothing added.
    """
    cml = root / "CMakeLists.txt"
    try:
        text = cml.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    m = _ADD_EXECUTABLE_RE.search(text)
    if not m:
        return []
    block = m.group(1)
    listed = set(block.split())
    added = [r for r in rel_paths
             if Path(r).suffix.lower() in _COMPILABLE_EXTS and r not in listed]
    if not added:
        return []
    indent = "    "
    new_block = block.rstrip() + "\n" + "".join(f"{indent}{r}\n" for r in added)
    updated = text[:m.start(1)] + new_block + text[m.end(1):]
    try:
        cml.write_text(updated, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    return added


def register_firmware_tools(mcp) -> None:
    """Register every firmware compile-loop tool on the given FastMCP instance."""

    @mcp.tool()
    def fw_status(project_id: str = "") -> str:
        """Firmware provisioning doctor — call FIRST before any fw_* work.

        Detects each firmware CLI (cmake, ninja, arm-none-eabi-gcc, renode,
        qemu-system-arm, probe-rs, esptool, idf.py, arduino-cli) and returns
        ``{ok, tools, families, platforms, banners, missing}``:

        - ``tools[name]`` = ``{found, path, version, install_hint, unlocks}``
        - ``families`` = per-MCU-family rollup (``cortex-m`` / ``esp32`` / ``avr``)
          with ``can_build`` / ``can_emulate`` / ``can_flash`` booleans plus the
          ``missing`` tool list that would unlock each family.
        - ``platforms`` = Renode platform capability matrix (``stm32f4`` /
          ``stm32f1`` / ``nrf52`` / ``rp2040`` / ``esp32`` / ``cortex-m`` →
          ``{file, emulation, uart, gpio, gpio_observe, adc, i2c, spi, can, timers,
          timed_bus, electrical_bus, notes}``). ESP32 is build+flash only
          (``emulation="none"``). Only implemented STM32F4 controllers on Renode
          support the timed/electrical bus models; a functional peripheral capability
          alone does not promise electrical fidelity on other families.
        - ``banners`` = human-readable fix-it lines; ``missing`` = absent core tools.

        This doctor is GLOBAL: it reports the machine's toolchain provisioning, not any
        one project. ``project_id`` is accepted (every sibling fw_* tool takes one, so
        passing it here is the natural mistake) but IGNORED — the answer is identical
        with or without it, and a ``note`` says so. For per-project state call
        ``fw_sources(project_id=...)`` (its source list) or ``fw_build(project_id=...)``.

        Never fails (mirrors reveng_status / em_status): an unprovisioned machine
        still returns ``ok=true`` with everything ``found=false`` and install hints.
        Offline-safe — reads PATH + `--version` only, never the network.
        """
        try:
            payload = cb.call("fw_status", {})
        except Exception as exc:  # noqa: BLE001 — the doctor itself never errors out
            payload = {"ok": True, "tools": {}, "families": {}, "banners": [
                f"fw_status probe degraded: {exc}"], "missing": []}
        if project_id:
            # Accept-and-ignore beats a hard unknown_argument refusal (which cost the
            # agent a whole call), but the drop must be VISIBLE and must not pretend
            # per-project provisioning exists.
            payload = dict(payload)
            payload["note"] = (
                f"project_id={project_id!r} ignored: fw_status reports GLOBAL firmware "
                "provisioning (detected CLIs + per-family capability), not per-project "
                "state. For this project call fw_sources(project_id=...) for its source "
                "list or fw_build(project_id=...) to compile it."
            )
        return _dumps(payload)

    @mcp.tool()
    def fw_new_project(name: str = "", mcu_family: str = "", toolchain: str = "cmake",
                       board_doc_ref: str = "") -> str:
        """Create an embedded-firmware project (CMake + arm-none-eabi baseline) and return its id.

        Pass ``board_doc_ref`` (an open ECAD board doc_id or a ``.ecad+``/``.json`` path) to link the
        firmware to a PCB: the MCU family is then auto-selected from the board's BOM when
        ``mcu_family`` is left empty, and a ``build/include/pinmap.h`` header of
        ``#define PIN_<NET> <n>`` is generated from the board's net names. Returns
        ``{project_id, project, root_dir, pinmap_path?}``.

        The project directory lives outside the fs_* workspace jail, so author its code with
        ``fw_sources(project_id, write={"src/main.c": "<code>"})`` — NOT fs_write.
        """
        params: dict = {"toolchain": toolchain or "cmake"}
        if name:
            params["name"] = name
        if mcu_family:
            params["mcu_family"] = mcu_family
        if board_doc_ref:
            params["board_doc_ref"] = board_doc_ref
        result = cb.call("fw_new_project", params)
        pid = (result or {}).get("project_id")
        if pid:
            root = project_root(pid)
            if root is not None:
                result = dict(result)
                result["root_dir"] = str(root)
                result["authoring"] = (
                    'write source code with fw_sources(project_id="%s", '
                    'write={"src/main.c": "<code>"}) — this directory is outside the '
                    "fs_write workspace jail" % pid
                )
        return _dumps(result)

    @mcp.tool()
    def fw_build(project_id: str) -> str:
        """Build a firmware project with its toolchain and return the BuildResult:
        ``{success, elf_path, map_path, stderr, warnings, returncode, cmd, ms}``. A missing
        cross-toolchain is reported as ``success=false, returncode=-1`` (not an error) so the agent
        can surface an install hint rather than crash the loop.
        """
        return _dumps(cb.call("fw_build", {"project_id": project_id}))

    @mcp.tool()
    def fw_sources(project_id: str, add: Optional[list] = None,
                   remove: Optional[list[str]] = None,
                   write: Optional[dict] = None) -> str:
        """List, add/remove, or WRITE the project's source files (paths relative to the
        project root). Returns ``{src_paths}`` plus ``{written, cmake_sources_added}`` when
        content was authored.

        This is the ONLY way to get firmware source code onto disk: a project root lives
        outside the fs_* workspace jail (fs_write refuses it as ``outside_workspace_root``),
        and ``add`` alone only materialises an EMPTY stub.

        - ``write={"src/main.c": "int main(void){...}"}`` — create-or-overwrite each file
          with that exact text, register it in the project, and (for compilable
          ``.c/.cpp/.cc/.cxx/.s`` paths) append it to the CMakeLists ``add_executable()``
          list so the next ``fw_build`` actually compiles it. Read it back with
          ``fs_read`` — the file is at ``<root_dir>/<relpath>``.
        - ``add=["src/driver.c"]`` — register + empty stub (unchanged).
          ``add=[{"path": "src/driver.c", "content": "..."}]`` is accepted as a synonym for
          ``write``.
        - ``remove=["src/startup.c"]`` — deregister (the file itself is left on disk).

        Paths must stay inside the project (``..`` refuses); an absolute path under the
        project root is relativised for you.
        """
        plain_add: list[str] = []
        content_map: dict = {}
        for entry in (add or []):
            if isinstance(entry, dict):
                rel = entry.get("path") or entry.get("relpath") or entry.get("file") or ""
                if not rel:
                    return _dumps(_err(
                        "invalid_request",
                        "an add[] object needs a 'path' key, e.g. "
                        '{"path": "src/driver.c", "content": "..."}'))
                content_map[rel] = entry.get("content", entry.get("text", ""))
            else:
                plain_add.append(str(entry))
        for rel, text in (write or {}).items():
            content_map[rel] = text

        if not content_map:
            # Unchanged legacy path: list / add-stub / remove, byte-identical.
            params: dict = {"project_id": project_id}
            if plain_add:
                params["add"] = plain_add
            if remove:
                params["remove"] = remove
            return _dumps(cb.call("fw_sources", params))

        root = project_root(project_id)
        if root is None:
            return _dumps(_err(
                "unknown_project",
                f"no firmware project {project_id!r} on disk — call fw_new_project first "
                "and pass the project_id it returns"))

        normalised: dict = {}
        for rel, text in content_map.items():
            safe, problem = _safe_relpath(rel, root)
            if safe is None:
                return _dumps(_err("invalid_request", problem))
            if not isinstance(text, str):
                return _dumps(_err(
                    "invalid_request",
                    f"content for {safe} must be a string, got {type(text).__name__}"))
            normalised[safe] = text

        params = {"project_id": project_id,
                  "add": plain_add + [r for r in normalised if r not in plain_add]}
        if remove:
            params["remove"] = remove
        result = cb.call("fw_sources", params)
        if not isinstance(result, dict) or "src_paths" not in result:
            return _dumps(result)

        written = []
        for rel, text in normalised.items():
            abs_path = root / rel
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                data = text.encode("utf-8")
                abs_path.write_bytes(data)
            except OSError as exc:
                return _dumps(_err("write_failed", f"{abs_path}: {exc}",
                                   src_paths=result.get("src_paths")))
            written.append({"path": rel, "abs_path": str(abs_path), "bytes": len(data)})

        result = dict(result)
        result["root_dir"] = str(root)
        result["written"] = written
        added = _ensure_cmake_sources(root, list(normalised))
        if added:
            result["cmake_sources_added"] = added
        return _dumps(result)

    @mcp.tool()
    def fw_sim_run(project_id: str, elf_path: str = "", scenario: str = "",
                   assertions: Optional[list[str]] = None, run_s: float = 0.0,
                   timeout_s: float = 0.0, power_profile: bool = False,
                   backend: str = "auto", scenario_path: str = "") -> str:
        """Run the canonical firmware simulation used by Firmware > Emulate.

        Humans select a project in Emulate, edit/load/save the same scenario JSON,
        choose backend, duration/host timeout and assertions, then Run emulation.
        The UI exposes Engineering evidence and Copy native result JSON; loaded files
        become inline JSON. Agents can instead pass ``scenario_path``.
        ``project_id`` is required; explicit ``elf_path`` wins over build discovery.
        Run ``fw_build`` first or supply an executable ELF for the selected architecture.
        ``backend`` is auto / renode / qemu / simavr. Forced choices refuse when
        unavailable or incompatible, never silently fall back. Scenarios require Renode.
        SimResult preserves ``{passed, uart_log, gpio_states, assertion_results, ms,
        gpio_events, trace_path}`` and adds ``bus_events`` / ``bus_summary`` / ``uart_events``.
        ``passed`` concerns the requested assertions, not engineering certification.
        ``ms`` is host wall elapsed time; bus event ``t_ns`` is nanosecond-quantized
        virtual time, not a guarantee of subnanosecond physical accuracy.
        Assertions:
        - ``"uart:<text>"`` — UART/semihosting output contains ``<text>``.
        - ``"gpio:<PIN>=<level>"`` — GPIO pin is at the given logic level at end of run.
          Renode observes pins directly (state-change hooks + ODR snapshot, e.g. PA5 on
          stm32f4_discovery); on the QEMU fallback the firmware must report events via the
          ``CHUDWARE_GPIO_TRACE(pin, level, t_ms)`` macro from the project's ``chudware_trace.h``.
        - ``"gpio:<PIN>=<level>@><T>ms"`` (also ``@<`` / ``@``) — timeline assertion:
          a time-stamped event shows the pin at that level after/before/at-or-after T ms.
        - ``"toggles:<PIN>>=<N>"`` (also <=, ==, >, <) — pin level changed N times.
        - ``"period:<PIN>=<P>ms±<TOL>ms"`` — mean rising-edge period within P±TOL.
        - ``"uart_within:<text>@<T>ms"`` — text in an actually time-stamped UART event
          at or before T ms; inspect timing evidence, not host log arrival time.
        - plain string — treated as uart match.

        ``scenario`` (schema ``aicad.fwscenario/1``, contracts/firmware_rpc.md §9)
        binds declarative sensor/GPIO stimulus to the emulated peripherals. Pass a JSON
        object string or a fixture reference: ``"fixture:i2c_temp_tmp103"`` (I2C TMP103
        temp sensor with programmed temperature), ``"fixture:spi_imu_stub"``,
        ``"fixture:quadrature_encoder"`` (PA0/PA1, 24 steps CW), ``"fixture:adc_thermistor"``
        (NTC heat-up curve via the Python-peripheral ADC stub), ``"fixture:user_button"``.
        Alternatively pass ``scenario_path`` to read a saved UTF-8 scenario JSON file
        (the same file humans load/save in Emulate); do not supply both inputs.
        Track kinds include gpio_edges / button / quadrature / i2c_sensor /
        i2c_registers / i2c_register_stream / spi_stub / spi_peer / uart_rx /
        can_frames / adc_waveform. ``spi_peer`` supplies ``response_hex`` or register
        mappings plus actual ``cs_gpio`` / ``cs_pin``. ``can_frames`` events supply
        ``t_ms,id,payload_hex`` and optional ``extended,remote``. A CAN track's optional
        ``bitrate_hz`` reconfigures its named peer even with a link, using sample point
        0.875, SJW 1 and track ``ack_enabled`` (default true); it can override link peer
        settings rather than rejecting a mismatch. Omit redundant track timing to retain
        link peer settings; inspect emitted effective peer timing, not just resolved_links.
        Native validation is authoritative: malformed, incomplete or platform-unsupported
        scenarios refuse with native diagnostics; inspect code/message/detail or the
        recorded scenario/platform errors rather than treating empty UART as success.

        Add optional ``bus_links`` to ``aicad.fwscenario/1`` and ``link_id`` to tracks.
        Each link names ``id``, ``bus`` (actual controller), ``protocol`` (i2c / uart /
        spi / can), explicit ``controller_clock_hz``, ``peer`` and ``electrical.signals``.
        Firmware register programming determines controller timing; declared clock
        frequency is not a silent override for baud/bitrate/mode registers.
        SI physical inputs are required rather than invented: signal ``name`` and
        ``mode``; ``model=lumped_rc`` with ``series_r_ohm`` / ``wire_capacitance_f``, or
        ``model=lossless_tline`` with located segments ``from,to,z0_ohm,propagation_s``.
        Endpoint records declare ``id,at,input_capacitance_f``; receiver ``vil_v,vih_v``
        (CAN ``recessive_max_v,dominant_min_v``); driver ``drive,v_low_v,v_high_v,
        r_low_ohm,r_high_ohm,rise_s,fall_s,tx_delay_s,rx_delay_s``.
        Bias/termination records declare location, ``resistance_ohm`` and rail/ground/
        differential reference. Resistances are ohms, capacitances farads, voltages
        volts, all delays seconds; rise/fall are 10–90% exponential source transitions.
        No nominal impedance/clock/termination is inferred from a board label.
        Peer protocol settings: I2C ``response_delay_s,clock_stretch_s``; TTL UART
        ``baud_hz,data_bits,parity,stop_bits,electrical_variant=ttl``; SPI ``cpol,cpha,
        clock_to_output_s,setup_s,hold_s``; CAN ``bitrate_hz,sample_point,sjw_tq,
        clock_hz,ack_enabled``. Hz means cycles/second; sample_point is a fraction.
        SPI's receive budget includes the clock link/receiver delay, peer clock-to-output
        delay and MISO return propagation, rise/threshold settling and load, plus setup.
        In the exercised 50 ns half-period profile, 20 ns edges plus 20 ns peer output
        delay exceeded that budget and produced actual SPI_BAD with the same ELF;
        the healthy 5 ns-edge profile is not a universal SPI timing guarantee.

        ``bus_summary`` reports resolved_links, limitations, retained event_count and
        truncation. ``bus_events`` contains actual register/frame/electrical outcomes
        with bus, protocol, event and virtual ``t_ns``; other observed fields are strings.
        Sample level -1/cause floating_network or threshold_indeterminate is unresolved
        electrical evidence, independent of hardware error flags. UART outcome=unresolved
        preserves that cause even with ONEBIT, which does not invent a hardware NF flag.
        Resolved samples do not by themselves prove correct received data or framing.
        Inspect firmware-visible status/data/IRQ/completion/timeouts against these events.
        Run the SAME ELF with healthy and faulty loading/timing, compare actual firmware
        outcomes, and preserve scenario inputs + native evidence. Console text alone
        is not proof that electrical latency affected firmware. Evidence is finite:
        parsed streams/trace cap at 4096 entries, detailed samples at 4096 and model
        events at 65536; uart_log caps at 32000 bytes. Inspect truncation diagnostics;
        UART transmit completions are selected before their independent 4096-entry cap.
        Plain UART and uart_within assertions are not evaluated on selected UART loss,
        including UART or unscoped emitter loss. Other protocol loss, the global parsed
        bus cap and UART sample-detail-only loss do not prevent their evaluation.

        ``gpio_events`` is the retained pin timeline; ``trace_path`` is the best-effort
        JSON artifact (schema ``aicad.firmware.trace/1``) next to the ELF, containing
        GPIO/UART evidence, not the full bus/resolved-link packet. Renode 1.16.1 observes
        final ODR state but lacks the observer needed for timestamped GPIO/toggle/period
        assertions; those refuse rather than turning a final snapshot into a timeline.
        Timed F4 UART assertions reconstruct per-bus bytes; uart_within uses virtual
        transmit completion, not host console receipt. Legacy UART timestamps may be coarse/null.

        Timed/electrical I2C, TTL UART, master SPI and bxCAN share the existing Renode
        bus runtime, bounded to STM32F4 i2c1–3, usart1/2, spi1–3 and can1/2.
        Query fw_status platform capabilities and inspect resolved_links before choosing
        a bus. Other families' functional UART/I2C/SPI support does not imply electrical
        support. ESP32 has no emulation. Missing physical values or unsupported models/
        variants are refused, not silently idealized. Without links the F4 controllers
        assume reset 16 MHz HSI peripheral clocks and an ideal medium. RCC/PLL/APB clock
        changes are not modeled; controller_clock_hz declares the assumed peripheral
        input, not an implementation of RCC. Functional runs are not calibrated evidence.
        Linear RC MNA / lossless Branin lines start with zero stored energy; explicit
        biases charge the medium, with no invented gmin or termination. Receiver voltage/
        validity history is preserved for delayed/past samples, not replaced by the latest
        voltage. Quiet line ports do not skip travelling-wave integration.
        The numerical timestep floor (0.1 ns) and quantized schedules are controls, not
        accuracy promises. CAN is differential only; common-mode, asymmetry, EMC,
        crosstalk, nonlinear drivers, dispersion and general SPICE behavior are excluded.
        Respect all additional reported limitations; this is not full-flight,
        Windows/macOS or live-hardware proof.

        When the project is linked to an ECAD board (``board_doc_ref``) and a
        trace was written, the result also carries ``power_profile_path`` — an
        ``aicad.powerprofile/1`` artifact (``<elf>.power.json``, contract §11): duty-
        cycle-weighted per-rail current profile (CPU run/sleep residency + per-pin GPIO
        duty x the board's ``eng.supply.rails`` currents). Bookkeeping, not
        SPICE. Use fw_power_profile to derive one from an existing trace.

        On Renode, ``run_s`` is the TARGET virtual-time horizon in seconds (0 = native default,
        1 second without a scenario; an unspecified scenario horizon includes the last
        stimulus + 1 second). An explicit horizon must include every scheduled stimulus.
        ``timeout_s`` is the independent HOST process safety budget in seconds
        (0 = native default, 30 seconds). Neither replaces the other; no wall sleeps
        count as virtual latency. Short virtual runs may need a larger host budget.
        A stimulus can still have an unfinished serialized transfer at the horizon.
        MCP/UI 0 timeout omits the native field; native RPC requires positive timeout_s.
        simavr uses a wall-duration stop, not a precise virtual horizon; QEMU refuses
        explicit positive run_s. Neither accepts peripheral scenarios or supplies
        timed/electrical bus evidence; use Renode for the bounded models above.

        ``power_profile=True`` is not a knob here — the power profile is a SEPARATE tool,
        ``fw_power_profile(trace_path=...)``, which post-processes the ``trace_path`` this
        call returns. Passing it makes this call chain into fw_power_profile for you and
        return the profile under ``power_profile``; ``arg_notes`` always names the tool.

        Falls back gracefully when neither Renode nor QEMU is installed (``passed=False``,
        ``uart_log`` explains what's missing). Run ``fw_build`` first to produce the ELF.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            return _dumps(_err("invalid_request", "project_id must be a non-empty string"))
        if elf_path:
            problem = _file_problem(elf_path, "elf_path", elf=True)
            if problem:
                return _dumps(_err("invalid_request", problem))
        if not _finite_number(run_s, nonnegative=True):
            return _dumps(_err(
                "invalid_request", "run_s must be finite and non-negative (0 uses the engine default)"))
        if not _finite_number(timeout_s, nonnegative=True):
            return _dumps(_err(
                "invalid_request", "timeout_s must be finite and non-negative (0 uses the engine default)"))
        if backend not in ("auto", "renode", "qemu", "simavr"):
            return _dumps(_err(
                "invalid_request", "backend must be auto, renode, qemu or simavr"))
        if scenario_path:
            if scenario:
                return _dumps(_err(
                    "invalid_request", "Pass scenario or scenario_path, not both"))
            problem = _file_problem(scenario_path, "scenario_path")
            if problem:
                return _dumps(_err("invalid_request", problem))
            try:
                scenario = Path(scenario_path).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as exc:
                return _dumps(_err("invalid_request", f"scenario_path cannot be read: {exc}"))
            try:
                saved = json.loads(scenario)
            except ValueError as exc:
                return _dumps(_err("invalid_request", f"scenario_path is not valid JSON: {exc}"))
            if not isinstance(saved, dict):
                return _dumps(_err("invalid_request", "scenario_path JSON must be an object"))
        if assertions is not None:
            if not isinstance(assertions, list) or any(
                    not isinstance(item, str) or not item.strip() for item in assertions):
                return _dumps(_err(
                    "invalid_request", "assertions must be a list of non-empty strings"))
        bad_path = _nonfinite_path(scenario)
        if bad_path:
            return _dumps(_err(
                "invalid_request", f"scenario contains a non-finite number at {bad_path}"))
        p: dict = {"project_id": project_id}
        if elf_path:
            p["elf_path"] = elf_path
        if scenario:
            p["scenario"] = scenario
        if assertions:
            p["assertions"] = assertions
        if backend != "auto":
            p["backend"] = backend
        if float(run_s) > 0:
            p["run_s"] = float(run_s)
        if float(timeout_s) > 0:
            p["timeout_s"] = float(timeout_s)
        notes: list[str] = []
        if power_profile:
            notes.append(
                "power_profile is not a fw_sim_run option — the profile comes from "
                "fw_power_profile(trace_path=...), which needs the trace_path THIS call "
                "returns; chaining it for you and returning it under 'power_profile'")
        result = cb.call("fw_sim_run", p)
        if not isinstance(result, dict):
            return _dumps(result)
        if notes or power_profile:
            result = dict(result)
        if power_profile:
            trace = result.get("trace_path") or ""
            if not trace:
                notes.append(
                    "no trace_path in this run (emulator absent or nothing recorded), so "
                    "fw_power_profile could not be chained — build+emulate first, then "
                    "call fw_power_profile(trace_path=...) yourself")
            else:
                try:
                    result["power_profile"] = cb.call(
                        "fw_power_profile", {"trace_path": trace, "project_id": project_id})
                except Exception as exc:  # noqa: BLE001 — chaining never breaks the sim result
                    notes.append(
                        f"fw_power_profile(trace_path={trace!r}) failed: {exc} — call it "
                        "directly to see the error")
        if notes:
            result["arg_notes"] = notes
        return _dumps(result)

    @mcp.tool()
    def fw_verify_board(board_doc_ref: str, project_id: str = "",
                        sch_doc_ref: str = "", mcu_family: str = "",
                        trace_path: str = "", emulate: bool = False) -> str:
        """Firmware↔board co-verification: check the firmware BACK against the
        PCB before fab and return ERC/DRC-shaped findings.

        STATIC checks (always run): every firmware-referenced ``PIN_<NET>`` exists as a
        board net and lands on the MCU footprint; boot-strap/reset pins (BOOT0/NRST,
        ESP32 GPIO0/2/12/15/EN...) not repurposed as signals; firmware never drives a net
        that already has another push-pull driver or an input-only pin (needs
        ``sch_doc_ref`` for netlist pin types — skipped honestly without it); every MCU
        supply rail has a decoupling capacitor (rails from the eng metadata +
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
        """Derive an ``aicad.powerprofile/1`` power/duty-cycle profile
        (contracts/firmware_rpc.md §11) from an EXISTING emulation trace — no live
        emulation needed.

        ``trace_path`` is the ``aicad.firmware.trace/1`` artifact a previous fw_sim_run
        returned (``<elf>.trace.json``). The profile combines:
        - CPU run/sleep residency — integrated from optional firmware-side
          ``CHUDWARE_CPU_TRACE(state, t_ms)`` markers (chudware_trace.h); no markers →
          honestly assumed 100 % active (``source: "assumed_active"``).
        - Per-pin GPIO/PWM duty cycles — time-stamped events integrated over the run
          window (unstamped Renode hook events → duty null + toggle count; no events →
          the final-snapshot level).
        - Per-rail currents — each board component's ``eng.supply.rails``
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
    # Hardware path: probe-rs flash/debug + esptool ESP32 flash
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
        """Build an ``aicad.fwscenario/1`` from a flight/CFD/kinematic profile.

        Convert a time-series profile into modeled sensor register values (MPU6050 IMU,
        BMP280 baro, LIS3MDL mag, pitot/airspeed) as ``i2c_register_stream`` tracks and
        NEO-6M GNSS ``uart_rx`` NMEA tracks. ``fw_sim_run`` replays these through the
        selected platform's actual peripheral models. Sensor register conversion is not
        evidence that bus timing/electrical behavior passed.

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
        ``fw_sim_run(scenario=...)``. Generation runs in the native core and needs no
        emulator; replay requires a supported Renode platform. For engineering bus
        simulation, add measured ``bus_links`` and track ``link_id`` using fw_sim_run's
        SI parameter schema, then save the same JSON for Firmware > Emulate.
        Inspect native bus_summary limitations and bus_events before claiming latency/
        impedance correctness; generated sensor samples alone do not supply physical
        line, threshold, driver or controller-clock parameters.

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
        if not _finite_number(dt_s, positive=True):
            return _dumps(_err("invalid_request", "dt_s must be finite and greater than zero"))
        if not _finite_number(duration_s, nonnegative=True):
            return _dumps(_err(
                "invalid_request", "duration_s must be finite and non-negative (0 uses the profile span)"))
        if not isinstance(sensor_map, list) or not sensor_map:
            return _dumps(_err("invalid_request", "sensor_map must be a non-empty list"))
        if any(not isinstance(entry, dict) for entry in sensor_map):
            return _dumps(_err("invalid_request", "every sensor_map entry must be an object"))
        has_profile = profile not in ("", None)
        has_sim_result = sim_result not in ("", None)
        if has_profile and has_sim_result:
            return _dumps(_err(
                "invalid_request", "pass profile or sim_result, not both (profile would otherwise win)"))
        if not has_profile and not has_sim_result:
            return _dumps(_err("invalid_request", "profile or sim_result is required"))

        def _parse(blob):
            if isinstance(blob, (dict, list)):
                return blob
            try:
                return json.loads(blob)
            except (TypeError, json.JSONDecodeError) as exc:
                return _err("invalid_request", f"profile is not valid JSON: {exc}")

        parsed = _parse(profile if has_profile else sim_result)
        if isinstance(parsed, dict) and parsed.get("code"):
            return _dumps(parsed)
        if not isinstance(parsed, (dict, list)):
            return _dumps(_err(
                "invalid_request", "profile must decode to a JSON object or sample array"))
        bad_path = _nonfinite_path(parsed)
        if bad_path:
            return _dumps(_err(
                "invalid_request", f"profile contains a non-finite number at {bad_path}"))
        bad_sensor_path = _nonfinite_path(sensor_map)
        if bad_sensor_path:
            return _dumps(_err(
                "invalid_request", f"sensor_map contains a non-finite number at {bad_sensor_path}"))

        p: dict = {"dt_s": float(dt_s), "sensor_map": sensor_map}
        if float(duration_s) > 0:
            p["duration_s"] = float(duration_s)
        if name:
            p["name"] = name
        p["profile" if has_profile else "sim_result"] = parsed
        return _dumps(cb.call("fw_sensor_scenario", p))

    # -----------------------------------------------------------------------
    # Hardware path (S-FW-CORE): avrdude AVR flash, OpenOCD/GDB-MI debug,
    # simavr/Renode emulation, live serial monitor (contracts/firmware_rpc.md §12)
    # -----------------------------------------------------------------------

    @mcp.tool()
    def fw_avr_flash(hex_path: str, mcu: str, programmer: str = "arduino", port: str = "",
                     baud: int = 115200) -> str:
        """Flash an AVR target via avrdude and return FlashResult.

        ``avrdude -c <programmer> -p <mcu> [-P <port>] [-b <baud>] -U flash:w:<hex_path>:i``.
        Default ``programmer="arduino"`` matches the arduino-cli build backend (the AVR
        family's toolchain); pass e.g. ``"usbasp"``/``"stk500v1"``/``"avrispmkii"`` for other
        programmers. ``port`` empty omits ``-P`` (programmer-default, e.g. USB HID
        programmers like usbasp); pass 0 for ``baud`` to omit ``-b``.

        FlashResult fields: ``{success, output, returncode, cmd, ms}``. ``success=False``
        with a descriptive ``output`` when avrdude is absent — never an error envelope.
        Install hint: ``sudo dnf install avrdude`` / ``sudo apt install avrdude``.

        [~] Live flash requires a connected AVR programmer.
        """
        p: dict = {"hex_path": hex_path, "mcu": mcu}
        if programmer and programmer != "arduino":
            p["programmer"] = programmer
        if port:
            p["port"] = port
        if baud != 115200:
            p["baud"] = baud
        return _dumps(cb.call("fw_avr_flash", p))

    @mcp.tool()
    def fw_debug_gdb_reset(openocd_cfg: str, halt: bool = True) -> str:
        """Reset an ARM target via a fresh OpenOCD + gdb/MI session; returns FlashResult.

        Launches OpenOCD as a background GDB server (``openocd -f <openocd_cfg>``, default
        port 3333), attaches ``arm-none-eabi-gdb --batch --interpreter=mi2``, issues
        ``monitor reset halt`` (or ``monitor reset run`` when ``halt=False``), then detaches
        and stops OpenOCD. ``openocd_cfg`` is the ``-f`` argument — usually a board config
        that pulls in both interface and target (e.g. ``"board/stm32f4discovery.cfg"``).

        This is the GDB-remote fallback debug path: use it for chips
        probe-rs's target registry doesn't cover. FlashResult ``output`` carries the raw
        gdb/MI transcript. Each call is a fresh attach/detach cycle — safe for reset/halt
        because Cortex-M halt state is a hardware flag that survives disconnecting, but a
        breakpoint set in one call is not guaranteed to still be programmed in a later call.

        [~] Requires a connected SWD/JTAG probe supported by OpenOCD.
        """
        p: dict = {"openocd_cfg": openocd_cfg}
        if not halt:
            p["halt"] = False
        return _dumps(cb.call("fw_debug_gdb_reset", p))

    @mcp.tool()
    def fw_debug_gdb_halt(openocd_cfg: str) -> str:
        """Interrupt/halt an ARM target via OpenOCD + gdb/MI (``-exec-interrupt`` +
        ``monitor halt``); returns FlashResult. See fw_debug_gdb_reset for the session shape.

        [~] Requires a connected SWD/JTAG probe supported by OpenOCD.
        """
        return _dumps(cb.call("fw_debug_gdb_halt", {"openocd_cfg": openocd_cfg}))

    @mcp.tool()
    def fw_debug_gdb_step(openocd_cfg: str, step_over: bool = False) -> str:
        """Single-instruction step a halted ARM target via OpenOCD + gdb/MI
        (``-exec-step-instruction``, or ``-exec-next-instruction`` when ``step_over=True``
        to step over a call). Returns FlashResult with the raw ``*stopped`` MI record in
        ``output``. Call fw_debug_gdb_halt (or a breakpoint hit) first.

        [~] Requires a connected SWD/JTAG probe supported by OpenOCD.
        """
        p: dict = {"openocd_cfg": openocd_cfg}
        if step_over:
            p["step_over"] = True
        return _dumps(cb.call("fw_debug_gdb_step", p))

    @mcp.tool()
    def fw_debug_gdb_set_breakpoint(openocd_cfg: str, address: str) -> str:
        """Set a breakpoint at ``address`` via OpenOCD + gdb/MI (``-break-insert *<address>``);
        returns FlashResult. ``address`` accepts any gdb-recognized location (a raw
        hex/decimal address, e.g. ``"0x8000100"`` — no symbol table is loaded, so symbolic
        locations won't resolve).

        [~] Requires a connected SWD/JTAG probe supported by OpenOCD.
        """
        return _dumps(cb.call("fw_debug_gdb_set_breakpoint",
                              {"openocd_cfg": openocd_cfg, "address": address}))

    @mcp.tool()
    def fw_debug_gdb_read_mem(openocd_cfg: str, address: str, length: int) -> str:
        """Read *length* bytes from *address* via OpenOCD + gdb/MI
        (``-data-read-memory-bytes <address> <length>``); returns FlashResult whose
        ``output`` carries the raw MI memory-read record (a ``begin,end,contents`` tuple with
        the bytes as an MI-escaped hex string) — unlike fw_debug_read_mem (probe-rs), this is
        NOT decoded into a ``data_hex`` field.

        [~] Requires a connected SWD/JTAG probe supported by OpenOCD and a halted target.
        """
        return _dumps(cb.call("fw_debug_gdb_read_mem",
                              {"openocd_cfg": openocd_cfg, "address": address, "length": length}))

    @mcp.tool()
    def fw_emulate_simavr(elf_path: str, mcu: str, run_s: float = 2.0, gdb: bool = False,
                          gdb_port: int = 1234,
                          mi_commands: Optional[list[str]] = None) -> str:
        """Run an AVR ELF under simavr (``simavr -m <mcu> <elf_path>``) and return FlashResult.

        This is the low-level ELF/debug process runner, not the scenario/electrical
        evidence API. Use ``fw_sim_run(project_id, backend="simavr")`` for canonical
        project assertions/results. Here free-run ``run_s`` controls a host-side
        scheduled process stop; it does not prove a virtual-time horizon or bus fidelity.

        GDB-attach mode (``gdb=True``): launches simavr with ``-g`` (built-in GDB server on
        ``gdb_port``, default 1234) and drives ``avr-gdb --batch --interpreter=mi2`` with
        ``mi_commands`` (e.g. ``["-break-insert main", "-exec-continue"]``); ``output``
        carries the raw MI transcript.

        Practical fallback for AVR *debug*: modern UPDI-part debug
        protocols (EDBG/mEDBG/PKOB nano) aren't GDB-exposed by an open tool, so simavr's own
        GDB server is the universal AVR debug path (real-hardware debugWIRE via avarice is
        legacy/rare, out of scope here).

        [~] Software-only — no hardware required, but needs `simavr` (and `avr-gdb` for
        gdb=True) on PATH.
        """
        problem = _file_problem(
            elf_path, "elf_path", elf=True, expected_machine=83)  # EM_AVR
        if problem:
            return _dumps(_err("invalid_request", problem))
        if not isinstance(mcu, str) or not mcu.strip():
            return _dumps(_err("invalid_request", "mcu must be a non-empty string"))
        if not _finite_number(run_s, positive=True):
            return _dumps(_err("invalid_request", "run_s must be finite and greater than zero"))
        if not isinstance(gdb_port, int) or isinstance(gdb_port, bool) or not 1 <= gdb_port <= 65535:
            return _dumps(_err("invalid_request", "gdb_port must be an integer in 1..65535"))
        if mi_commands is not None and (
                not isinstance(mi_commands, list) or
                any(not isinstance(command, str) or not command.strip()
                    for command in mi_commands)):
            return _dumps(_err(
                "invalid_request", "mi_commands must be a list of non-empty strings"))
        if not gdb and (gdb_port != 1234 or mi_commands):
            return _dumps(_err(
                "invalid_request", "gdb_port and mi_commands require gdb=True"))
        p: dict = {"elf_path": elf_path, "mcu": mcu}
        if float(run_s) != 2.0:
            p["run_s"] = float(run_s)
        if gdb:
            p["gdb"] = True
            if gdb_port != 1234:
                p["gdb_port"] = gdb_port
            if mi_commands:
                p["mi_commands"] = mi_commands
        return _dumps(cb.call("fw_emulate_simavr", p))

    @mcp.tool()
    def fw_emulate_renode(resc_path: str, run_s: float = 2.0, gdb: bool = False,
                          gdb_port: int = 3333,
                          mi_commands: Optional[list[str]] = None) -> str:
        """Run an ARM machine under Renode (``renode --disable-xwt --console <resc_path>``,
        same invocation shape as fw_sim_run's Renode backend) and return FlashResult.

        ``resc_path`` is an EXISTING Renode script; this low-level process/debug runner
        does not resolve scenario JSON or produce canonical bus evidence. For timed/
        electrical scenarios use ``fw_sim_run(project_id, backend="renode", scenario=...)``.
        Here free-run ``run_s`` controls a host-side scheduled process stop, not a
        virtual-time horizon; script ``RunFor`` determines virtual execution.

        GDB-attach mode (``gdb=True``): expects the .resc to start Renode's built-in GDB
        server on ``gdb_port`` (``machine StartGdbServer <port>``, Renode default 3333) and
        drives ``arm-none-eabi-gdb --batch --interpreter=mi2`` with ``mi_commands``.

        [~] Software-only — no hardware required, but needs `renode` (.NET runtime) on PATH.
        """
        problem = _file_problem(resc_path, "resc_path")
        if problem:
            return _dumps(_err("invalid_request", problem))
        if not _finite_number(run_s, positive=True):
            return _dumps(_err("invalid_request", "run_s must be finite and greater than zero"))
        if not isinstance(gdb_port, int) or isinstance(gdb_port, bool) or not 1 <= gdb_port <= 65535:
            return _dumps(_err("invalid_request", "gdb_port must be an integer in 1..65535"))
        if mi_commands is not None and (
                not isinstance(mi_commands, list) or
                any(not isinstance(command, str) or not command.strip()
                    for command in mi_commands)):
            return _dumps(_err(
                "invalid_request", "mi_commands must be a list of non-empty strings"))
        if not gdb and (gdb_port != 3333 or mi_commands):
            return _dumps(_err(
                "invalid_request", "gdb_port and mi_commands require gdb=True"))
        p: dict = {"resc_path": resc_path}
        if float(run_s) != 2.0:
            p["run_s"] = float(run_s)
        if gdb:
            p["gdb"] = True
            if gdb_port != 3333:
                p["gdb_port"] = gdb_port
            if mi_commands:
                p["mi_commands"] = mi_commands
        return _dumps(cb.call("fw_emulate_renode", p))

    @mcp.tool()
    def fw_serial_open(port: str, baud: int = 115200) -> str:
        """Open a live serial/UART port (raw termios 8N1) for the physical-device monitor.

        Returns ``{success, handle, port, baud}`` on success, or ``{success:false, reason}``
        (e.g. the port doesn't exist or is already claimed by another process) — never an
        error envelope. The handle is process-local (valid for the hosting process's
        lifetime); pass it to fw_serial_read/fw_serial_write/fw_serial_close.

        This is the LIVE physical-device counterpart to fw_sim_run's emulator UART capture —
        there is no serial framing/protocol assumed, just a raw byte pipe.

        [~] Requires a connected device presenting a serial port (e.g. `/dev/ttyUSB0`,
        `/dev/ttyACM0`).
        """
        p: dict = {"port": port}
        if baud != 115200:
            p["baud"] = baud
        return _dumps(cb.call("fw_serial_open", p))

    @mcp.tool()
    def fw_serial_read(handle: int, max_bytes: int = 4096) -> str:
        """Non-blocking read of up to ``max_bytes`` currently buffered on an open serial
        handle (immediate return, no wait — poll this repeatedly for a live console).

        Returns ``{success, data_hex, bytes}`` — raw bytes as lowercase hex (never assumes
        text encoding; decode client-side). ``data_hex=""`` / ``bytes=0`` means nothing was
        buffered, not an error. ``{success:false, reason}`` for an unknown/closed handle.
        """
        p: dict = {"handle": handle}
        if max_bytes != 4096:
            p["max_bytes"] = max_bytes
        return _dumps(cb.call("fw_serial_read", p))

    @mcp.tool()
    def fw_serial_write(handle: int, data_hex: str = "", text: str = "") -> str:
        """Write to an open serial handle. Pass ``data_hex`` (hex-encoded raw bytes) for
        binary payloads, or ``text`` for a plain string (e.g. typing a command into a serial
        console); ``data_hex`` wins if both are given. Returns ``{success, bytes_written}``
        or ``{success:false, reason}``.
        """
        p: dict = {"handle": handle}
        if data_hex:
            p["data_hex"] = data_hex
        elif text:
            p["text"] = text
        return _dumps(cb.call("fw_serial_write", p))

    @mcp.tool()
    def fw_serial_close(handle: int) -> str:
        """Close and release a serial handle opened by fw_serial_open. Returns
        ``{success}`` — ``success=false`` if the handle was already closed/unknown (not an
        error).
        """
        return _dumps(cb.call("fw_serial_close", {"handle": handle}))
