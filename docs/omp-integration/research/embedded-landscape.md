# Embedded/Firmware Landscape: AVR + ARM Programming, Debugging, Emulation, Communication

Scope: practical open-source stack for wiring OMP-style agent tooling into real microcontroller
work (AVR + ARM broadly), covering debug, flash, emulate, comms/protocols, and toolchains.
OMP already ships a generic DAP client (README: "28 dap ops", demoed live against `lldb-dap`),
so any tool below that speaks GDB/MI or Microsoft DAP is a near-zero-glue fit; anything that only
speaks a private protocol needs a translation shim (GLUE) or a small native adapter (FORK/PORT).

## 1. DEBUG

| Tool | URL | License | Language | Covers | GDB server? | DAP bridge? |
|---|---|---|---|---|---|---|
| OpenOCD | https://github.com/openocd-org/openocd | GPL-2.0 | C | ARM Cortex-M/A (SWD/JTAG via ST-Link/CMSIS-DAP/J-Link/FTDI/etc.), some AVR-adjacent via JTAG; huge board/chip config library | Yes — built-in `gdb_port` (default 3333) + Tcl RPC (6666) + telnet (4444) | Indirect only, via **cortex-debug** (VS Code) which spawns OpenOCD as a GDB server and drives it with gdb/MI; no native MS-DAP server in OpenOCD itself |
| pyOCD | https://github.com/pyocd/pyOCD | Apache-2.0 | Python | ARM Cortex-M via CMSIS-DAP (and other probes via plugins) | Yes — `pyocd gdbserver` | Yes — pyOCD ships a native DAP server (`pyocd server` / `pyocd-py.debug`) used directly by VS Code "Debugger for ARM Cortex-M"; also usable via cortex-debug as a gdbserver backend |
| probe-rs | https://github.com/probe-rs/probe-rs | MIT/Apache-2.0 (dual) | Rust | ARM Cortex-M/A + RISC-V, via CMSIS-DAP, ST-Link, J-Link, FTDI | Yes, but **deprecated as of recent releases** — GDB stub replaced by DAP-only workflow (README/CHANGELOG: "The GDB server has been...") | Yes, first-class — probe-rs implements Microsoft DAP natively (`probe-rs dap-server`); this is now the primary interface (VS Code "probe-rs-debug" ext) |
| J-Link GDB Server | https://www.segger.com/products/debug-probes/j-link/tools/j-link-gdb-server/ | Proprietary (free to use with J-Link hardware) | C (closed) | ARM Cortex-M/A/R, some RISC-V, via SEGGER J-Link probes | Yes — native GDB server, port 2331 | No native DAP; bridged only via cortex-debug (spawns JLinkGDBServer, drives via gdb/MI) |
| Black Magic Probe / Black Magic Debug | https://black-magic.org/ , https://github.com/blackmagic-debug/blackmagic | GPL-3.0+ | C | ARM Cortex-M (native firmware on probe hardware), also BMDA (Black Magic Debug App) runs same core as a host-side app driving ST-Link/CMSIS-DAP/J-Link probes | Yes — GDB server **embedded in probe firmware itself** (or in BMDA on host), exposed over serial/USB-CDC or TCP directly to gdb, no separate server process needed | No native DAP; cortex-debug lists Black Magic Probe as a supported "server type" (it's actually serverless — cortex-debug just points gdb at the probe's serial port) |
| CMSIS-DAP | https://github.com/ARM-software/CMSIS-DAP | Apache-2.0 (firmware + docs) | C (probe firmware) + protocol spec | Not a debugger itself — a standardized USB HID/WinUSB probe **firmware protocol** (SWD/JTAG transport) implemented by many cheap probes (DAPLink, Pico-probe, etc.) | N/A (protocol/firmware layer) | Consumed by pyOCD and probe-rs as the probe backend; those tools provide the GDB/DAP surface |
| avarice | https://github.com/avrdudes/avarice | GPL-2.0/3.0+ | C | AVR debugWIRE/JTAG via Atmel JTAGICE mkI/mkII/3, bridges to `avr-gdb` | Yes — the whole point of avarice is "GDB remote serial protocol proxy for Atmel JTAG ICE" | No DAP; strictly a GDB remote-serial-protocol bridge; AVR has no CMSIS-DAP equivalent, this is the closest thing to a standard AVR hardware-debug GDB path |

**Coverage gap:** avarice/JTAGICE mkII hardware is increasingly rare; most modern AVR debugging on
UPDI parts (ATtiny/ATmega 0/1-series, AVR-DU) goes through **pymcuprog** or **avrdude** (program-only,
no live debug) since Microchip's newer debug protocol (EDBG/mEDBG/PKOB nano) isn't GDB-exposed by an
open tool — this is a genuine hole; simavr (see §3) is the practical fallback for AVR *source-level*
debugging via its own built-in GDB server.

## 2. PROGRAM / FLASH

| Tool | URL | License | Language | Covers | Notes |
|---|---|---|---|---|---|
| avrdude | https://github.com/avrdudes/avrdude | GPL-2.0+ | C | AVR ISP/HVSP/HVPP/debugWIRE/UPDI/TPI/JTAG programming via USBasp, Arduino bootloaders, JTAGICE, AVRISP mkII, PICkit, STK500, etc. | The AVR flashing tool; canonical, actively maintained (avrdudes org fork of the abandoned original) |
| probe-rs | (as above) | MIT/Apache-2.0 | Rust | Flash ARM Cortex-M/RISC-V via any supported probe (`probe-rs download`, `cargo-embed`, `cargo-flash`) | Also does RTT logging + flash algorithms per-chip via YAML target defs |
| pyOCD | (as above) | Apache-2.0 | Python | Flash ARM Cortex-M via CMSIS-DAP (`pyocd flash`) | Same probe stack as its debug path |
| dfu-util | https://sourceforge.net/projects/dfu-util/ (mirror: https://github.com/Stefan-Schmidt/dfu-util) | GPL-2.0 | C | USB DFU class (STM32 built-in bootloader, Atmel/Microchip SAM-BA-adjacent DFU parts, many USB MCUs) | No debug capability, flash-only over USB, no probe hardware needed |
| esptool | https://github.com/espressif/esptool | GPL-2.0 | Python | Espressif ESP8266/ESP32 family flashing over UART/USB (not AVR/ARM Cortex-M, but ESP32-C/H are RISC-V and ESP32 classic is Xtensa) | Out of strict AVR/ARM scope but ubiquitous in "microcontroller" contexts; include as adjacent/optional |
| stm32flash | https://sourceforge.net/projects/stm32flash/ (mirror: https://github.com/stm32duino/stm32flash) | GPL-2.0+ | C | STM32 UART bootloader (ROM bootloader, not DFU) flashing | Complements dfu-util for STM32 boards without USB or with BOOT0 strapped for UART |
| OpenOCD flashing | (as above) | GPL-2.0 | C | `program` command flashes via any OpenOCD-supported probe/target (ARM Cortex-M/A, some AVR32-era parts) | One process reused for both debug and flash — reduces moving parts if OpenOCD is already the debug backend |

## 3. EMULATE

| Tool | URL | License | Language | Covers | Scriptability |
|---|---|---|---|---|---|
| QEMU (arm/cortex-m) | https://www.qemu.org/docs/master/system/target-arm.html , https://github.com/qemu/qemu | GPL-2.0 (core) | C | Broad Cortex-A support; Cortex-M support is comparatively thin — only specific boards (e.g. `netduino2`, `olimex-stm32-h405`, `microbit`) have machine models; **peripheral fidelity is low** for most STM32/NXP/nRF parts | Built-in **GDB stub** (`-s -S` / `-gdb tcp::1234`) — this is QEMU's native debug interface, drop-in for arm-none-eabi-gdb; scriptable via QMP (QEMU Machine Protocol, JSON) and full command-line automation |
| Renode | https://github.com/renode/renode (docs: https://renode.readthedocs.io) | MIT | C#/.NET + Python (Robot Framework tests) | Deep Cortex-M/A peripheral-accurate emulation (STM32, nRF, ESP32, many vendor SoCs), multi-node/networked simulation, sensor injection | **Best-in-class scriptability**: `.resc` script DSL, Robot Framework test integration, Monitor REPL (Python-like), exposes its own **GDB server** per-CPU (`machine StartGdbServer <port>`) so arm-none-eabi-gdb / cortex-debug can attach to a simulated target exactly like real hardware |
| simavr | https://github.com/buserror/simavr | GPL-3.0/LGPL (dual, per-file) | C | AVR core + peripheral simulation (timers, UART, ADC, SPI/TWI), part of avr-gcc ecosystem, VCD trace output | Ships a **built-in GDB server** (`simavr -g`) — direct avr-gdb target, no bridge needed; scriptable via its C API (register custom peripherals) and Python wrapper `pysimavr` |
| avr8js | https://github.com/wokwi/avr8js | MIT | TypeScript/JavaScript | AVR core sim (ATmega328p-class) runnable in-browser/Node; powers Wokwi's web simulator | No GDB server — pure library API (step, read/write registers/memory, hardware hooks in JS); scriptable by construction since it *is* a JS library, but requires custom glue to expose a GDB/DAP endpoint |

**Recommendation:** Renode covers ARM peripheral-accurate emulation with the best test/scripting
story and a native GDB server; simavr is the AVR equivalent (native GDB server, lower fidelity
peripheral set but sufficient for core+timer+UART work); QEMU is a fallback only where a specific
board machine model already exists and peripheral accuracy doesn't matter (e.g. pure Cortex-M core
logic, no peripherals). avr8js is developer-tool (Wokwi-style in-page sim) territory, not a
debugger backend — skip unless building a browser-embedded simulator surface.

## 4. COMMUNICATE / PROTOCOLS

| Library | URL | License | Language | Covers |
|---|---|---|---|---|
| pyserial | https://github.com/pyserial/pyserial | BSD-3-Clause | Python | Cross-platform serial/UART port I/O; the base for nearly every other tool above (avrdude/pyOCD's serial-attached probes, simavr UART bridges, esptool) |
| libusb | https://github.com/libusb/libusb | LGPL-2.1 | C | Cross-platform raw USB access; underlies pyusb, dfu-util, most CMSIS-DAP host drivers, OpenOCD's USB probe backends |
| pyusb | https://github.com/pyusb/pyusb | BSD-3-Clause | Python (wraps libusb) | Python-side raw USB device access when a probe/programmer has no serial-CDC personality |
| python-can | https://github.com/hardbyte/python-can | LGPL-3.0 | Python | CAN bus (SocketCAN, PCAN, Vector, Kvaser, slcan) — needed once comms scope includes automotive/industrial CAN targets |
| pymodbus | https://github.com/pymodbus-dev/pymodbus | BSD-3-Clause | Python | Modbus RTU/TCP master+slave — needed for industrial/PLC-adjacent comms |
| smbus2 / periphery (python-periphery) | https://github.com/vsergeev/python-periphery | MIT | Python | Linux-host SPI/I2C/GPIO/UART/PWM/LED access (for host-side glue when the desktop machine itself has a debug-adjacent SPI/I2C bus, e.g. Raspberry Pi bridging) |
| JTAG/SWD | (protocol, not a library) | — | — | Exposed transitively through CMSIS-DAP / ST-Link / J-Link / FTDI MPSSE backends inside OpenOCD, pyOCD, probe-rs — no separate glue needed beyond picking a probe backend already in §1 |

**Recommendation:** pyserial + pyusb cover the transport layer for essentially every flashing/debug
tool above (they're already internal dependencies of avrdude-alternatives, pyOCD, probe-rs's host
tooling isn't Python but uses equivalent Rust crates `serialport-rs`/`rusb`). Add python-can and
pymodbus only if/when a target explicitly needs those buses — they're independent, low-risk,
optional glue, not part of the minimal set.

## 5. TOOLCHAINS

| Tool | URL | License | Covers |
|---|---|---|---|
| arm-none-eabi-gcc (Arm GNU Toolchain) | https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads | GPL (GCC) + newlib (BSD-ish) | Bare-metal ARM Cortex-M/A/R cross-compiler, canonical for embedded ARM C/C++ |
| avr-gcc | https://gcc.gnu.org/wiki/avr-gcc / packaged via https://github.com/osx-cross/homebrew-avr or distro | GPL (GCC) | AVR 8-bit cross-compiler, canonical for AVR C/C++ |
| clangd (embedded) | https://clangd.llvm.org/installation ; issue tracking cross-toolchain quirks: https://github.com/clangd/clangd/issues/2289, #784 | Apache-2.0 with LLVM exceptions | LSP-driven completion/diagnostics for embedded C/C++ *when fed a correct `compile_commands.json`* generated by the arm-none-eabi-gcc/avr-gcc build; clangd defaults to a "BareMetal" driver heuristic that needs the actual cross-compiler path in `compile_commands.json` (or a `--query-driver=/path/to/arm-none-eabi-*` flag) to find target-specific system includes — known friction point, not a blocker |

## Recommended Minimal Glue Set ("any AVR or ARM target, fewest moving parts")

1. **Debug + flash (ARM):** **probe-rs** as the single binary — DAP-native (matches OMP's existing
   DAP client directly, no bridge), also flashes, also does RTT logging, covers the overwhelming
   majority of Cortex-M parts via CMSIS-DAP/ST-Link/J-Link. Fall back to **OpenOCD** only for chips
   probe-rs's target registry doesn't yet cover (OpenOCD's config library is the largest in
   existence) — bridge OpenOCD through gdb/MI since it has no native DAP.
2. **Debug + flash (AVR):** **avrdude** for flash-only workflows (covers ~all AVR programmers).
   For AVR *debug*, **simavr** (built-in GDB server, `avr-gdb` target) is the practical universal
   answer since real-hardware AVR debug tooling (avarice/JTAGICE) is legacy/rare; treat live-hardware
   AVR debugWIRE as a SIDECAR/GLUE case via avarice only if a user specifically owns JTAGICE-class
   hardware.
3. **Emulate (both):** **Renode** for ARM (native GDB server + Robot Framework scripting, best
   fidelity) and **simavr** for AVR (native GDB server) — both slot straight into the same
   GDB-client code path used for real hardware, so the agent's debug integration is probe-agnostic:
   same GDB/MI or DAP flow whether the target is silicon or simulated.
4. **Comms:** **pyserial** + **pyusb** (or Rust `serialport`/`rusb` if embedding in probe-rs's own
   process) cover the transport layer for every tool above; add **python-can**/**pymodbus** only
   on demand.
5. **Toolchain:** **arm-none-eabi-gcc** + **avr-gcc** for building; **clangd** for editor
   intelligence, fed `compile_commands.json` from whichever build system is in use (CMake/Make/
   PlatformIO) with `--query-driver` pointed at the cross-compiler to avoid the BareMetal-driver
   include-path gap.

This set is 4 external processes total (probe-rs, avrdude, simavr, Renode) plus two compilers and
one language server — every debug-capable one of them (probe-rs natively, Renode/simavr via GDB
remote-serial, OpenOCD as an ARM fallback via GDB/MI) can be driven through OMP's existing generic
DAP/GDB client machinery with no new protocol work, only adapter-launch configuration.

## DAP/GDB Integration Notes (mapping onto OMP's existing tooling)

- OMP's DAP layer already speaks Microsoft DAP generically (demoed live against `lldb-dap`,
  README: "28 dap ops"). **probe-rs's `dap-server`** is a drop-in second adapter: same protocol,
  just point OMP's DAP launch config at `probe-rs dap-server` instead of `lldb-dap`, with
  `chip`/`probe` selection in the launch args (mirrors VS Code's `probe-rs-debug` extension
  launch.json shape, which is public and copyable).
- **pyOCD** also runs a native DAP server (`pyocd server`) — same integration shape as probe-rs;
  useful as a secondary path if a probe/chip is in pyOCD's target list but not yet in probe-rs's.
- Everything else (**OpenOCD, J-Link GDB Server, Black Magic Probe, avarice, Renode's GDB server,
  simavr's GDB server, QEMU's GDB stub**) exposes the older **GDB remote serial protocol**, not
  DAP. Two integration options, both used by existing prior art (cortex-debug does the first):
  (a) run the tool as a subprocess GDB server and drive it via **gdb itself in `--interpreter=mi2`
  mode** (GDB/MI is a stable, well-documented text protocol OMP can parse without vendoring gdb
  internals), or (b) put a small translation shim in front (e.g. `msdap-gdbserver`-style bridges
  exist in the wild, e.g. GDB DAP bridges bundled with some VS Code debug extensions) to present
  those as DAP. Recommend (a): fewer moving parts, and OMP's agent loop can shell out to
  `arm-none-eabi-gdb --interpreter=mi2` / `avr-gdb --interpreter=mi2` uniformly for every
  GDB-remote-only backend, keeping exactly two adapter shapes (native-DAP tools, and gdb/MI-wrapped
  GDB-remote tools) instead of one bespoke shim per tool.
- **CMSIS-DAP** is a firmware/transport standard, not a server — it's already covered indirectly
  whenever pyOCD or probe-rs is chosen as the backend; no separate integration needed.

## MCP Servers Already Wrapping These Tools

- **embedded-debugger-mcp** (https://github.com/adancurusul/embedded-debugger-mcp, also on
  lib.rs/crates and mcpmarket.com) — an MCP server wrapping **probe-rs** directly for AI-agent-driven
  embedded debugging (ARM Cortex-M + RISC-V), with RTT/defmt log support and CLI/Codex integration.
  This is the closest existing prior art to "OMP agent talks to a debug probe" and could be studied
  or even wrapped as a sidecar rather than reimplementing probe-rs's DAP client logic from scratch.
- Vendor-specific MCP servers exist (Microchip's MCP server, Infineon AURIX MCP server) but these
  are proprietary/vendor-tool wrappers (e.g. around MPLAB X / AURIX Development Studio), not
  relevant to an open-source AVR/ARM glue stack — noted for awareness only, not recommended.
- No MCP server was found wrapping OpenOCD, pyOCD, Renode, simavr, or QEMU directly as of this
  research pass; these remain GLUE-required (spawn + gdb/MI or DAP as above) rather than
  drop-in-MCP-available.

## Per-Item Recommendation Summary

| Item | Recommendation | Effort | Deps/Risks |
|---|---|---|---|
| probe-rs | **KEEP/PORT** — primary ARM debug+flash backend, native DAP | S | Rust binary, needs per-chip target defs (bundled or downloadable); GDB server path deprecated, don't rely on it |
| pyOCD | **KEEP** — secondary ARM backend for probe-rs target-list gaps | S | Python + CMSIS-DAP probes only (no ST-Link/J-Link native without plugins) |
| OpenOCD | **GLUE** — fallback for widest chip/board coverage via gdb/MI | M | GPL-2.0 (license-compatible for sidecar/subprocess use, not static-linking concerns); config-file sprawl |
| J-Link GDB Server | **GLUE** (optional) — only if targeting SEGGER-probe-only users | S | Proprietary binary, redistribution restrictions — treat as user-provided, not bundled |
| Black Magic Probe | **GLUE** (optional) — serverless GDB-over-serial, simplest possible hardware path if user owns the probe | S | Requires specific probe hardware (or flashing BMP firmware onto a supported dongle) |
| CMSIS-DAP | **KEEP** (transitive) — no direct integration, just probe firmware standard consumed by pyOCD/probe-rs | — | none |
| avarice | **GLUE** (optional/legacy) — only for users with real JTAGICE mkII/3 hardware wanting debugWIRE | S | Legacy hardware dependency, low priority |
| avrdude | **KEEP** — primary AVR flash tool | S | GPL-2.0+, mature, actively maintained fork |
| dfu-util | **KEEP** (optional) — USB DFU flashing for ARM parts with ROM DFU bootloader | S | none significant |
| stm32flash | **GLUE** (optional) — UART bootloader flashing fallback for STM32 | S | STM32-specific, narrow scope |
| esptool | **GLUE** (optional, out-of-scope-adjacent) — only if ESP32/8266 targets are in scope | S | Xtensa/RISC-V, not AVR/ARM Cortex-M proper |
| Renode | **KEEP/SIDECAR** — primary ARM emulator, scriptable, native GDB server | M | .NET runtime dependency, larger install footprint than QEMU |
| simavr | **KEEP/SIDECAR** — primary AVR emulator, native GDB server | S | Dual GPL/LGPL licensing per-file, check which files are vendored if statically linking |
| QEMU (arm) | **GLUE** (optional fallback) — only for boards with existing machine models, low peripheral fidelity | M | GPL-2.0, large codebase if vendoring; prefer system package over building from source |
| avr8js | **SKIP** (for backend use) — no GDB server, JS-only library; revisit only if building a browser-embedded simulator UI | — | MIT license, low risk if revisited later |
| pyserial / pyusb / libusb | **KEEP** — transport glue underlying everything above | S | BSD/LGPL, standard, low risk |
| python-can / pymodbus | **GLUE** (optional, on-demand) — add only when a target needs CAN/Modbus | S | Independent, no interaction with debug/flash stack |
| arm-none-eabi-gcc / avr-gcc | **KEEP** — canonical compilers, no alternative needed | — | GPL, standard distro packages |
| clangd | **KEEP** — editor intelligence, needs compile_commands.json + `--query-driver` fix | S | Known BareMetal-driver include-path friction (clangd#2289) — solvable via query-driver flag, not a blocker |
| embedded-debugger-mcp | **STUDY/PORT-REFERENCE** — existing MCP wrapper around probe-rs, useful prior-art for OMP's own probe-rs integration shape | S | Third-party project, not required as a dependency, just a design reference |
