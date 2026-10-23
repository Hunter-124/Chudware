// Hardware flashing and debug wrappers (C++ port of
// engine_oracle/firmware/hardware.py). probe-rs (MIT/Apache-2.0) and esptool
// (GPL-2.0+) are SUBPROCESS tools — never linked. A missing binary is
// graceful: FlashResult{success=false}, never an error envelope or a throw.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// Outcome of a flash/debug operation (mirrors hardware.FlashResult).
struct FlashResult {
    bool success = false;
    std::string output;      // combined stdout+stderr, capped at 32k
    int returncode = 0;      // -1 = binary absent or timed out
    std::vector<std::string> cmd;
    double ms = 0.0;

    nlohmann::json to_dict() const;
};

// probe-rs download --chip <chip> <elf>
FlashResult probe_rs_flash(const std::string& elf_path, const std::string& chip,
                           double timeout_s = 60.0);
// probe-rs reset --chip <chip>
FlashResult probe_rs_reset(const std::string& chip, double timeout_s = 60.0);
// probe-rs reset --chip <chip> --halt
FlashResult probe_rs_halt(const std::string& chip, double timeout_s = 60.0);

// probe-rs read --chip <chip> <addr> <len>. Returns raw bytes on success;
// throws std::runtime_error on tool-absent/failure (the fw_debug_read_mem RPC
// wrapper catches it into the graceful {success:false, reason} shape, exactly
// like the Python handler).
std::vector<uint8_t> probe_rs_read_memory(long long address, long long length,
                                          const std::string& chip, double timeout_s = 60.0);

// esptool.py --port <port> --baud <baud> write_flash <offset> <bin>
FlashResult esptool_flash(const std::string& bin_path, const std::string& port, int baud = 460800,
                          const std::string& flash_offset = "0x0", double timeout_s = 60.0);

// avrdude -c <programmer> -p <mcu> [-P <port>] [-b <baud>] -U flash:w:<hex_path>:i
// Default programmer "arduino" matches the arduino-cli build backend (provision.cpp AVR
// family); pass e.g. "usbasp"/"stk500v1" for other programmers. `port` empty omits -P
// (programmer-default, e.g. USB HID programmers); `baud` <= 0 omits -b.
FlashResult avrdude_flash(const std::string& hex_path, const std::string& mcu,
                          const std::string& programmer = "arduino", const std::string& port = "",
                          int baud = 115200, double timeout_s = 60.0);

// --- GDB/MI-driven debug (openocd/simavr/renode) ----------------------------
//
// None of openocd/simavr/renode speak Microsoft DAP (embedded-landscape.md §"DAP/GDB
// Integration Notes"); each exposes the older GDB remote-serial protocol instead. Per the
// dossier's recommendation, every one of them is driven the SAME way: launch the tool as a
// background GDB server, then run `<gdb> --batch --interpreter=mi2` against it with a list
// of MI commands, capturing the text output. This keeps exactly one adapter shape for every
// GDB-remote-only backend instead of a bespoke shim per tool.
//
// POSIX-only (fork/exec background-process management, SIGTERM teardown); a build on a
// non-POSIX target returns FlashResult{success=false, output="unsupported_platform: ..."}.
//
// Each call is a fresh server+gdb attach/detach cycle — stateless, mirroring
// probe_rs_reset/probe_rs_halt above. This is safe for reset/halt/read-mem/breakpoint on a
// real Cortex-M target because halt state (DHCSR C_HALT) is a HARDWARE flag that persists
// across the host tool disconnecting; it is NOT safe for multi-step debug sequences that
// depend on a single continuous connection (e.g. a breakpoint set in one call is not
// guaranteed to still be programmed by the time a later call's step/continue runs) — a
// future iteration would keep the server resident across calls via a session-id registry.

// Low-level primitive: launches `server_argv` (e.g. {"openocd","-f",cfg}) as a background
// process, waits `startup_delay_s` for its GDB server to come up, then runs
// `<gdb_binary> --batch --interpreter=mi2 -ex "target remote <remote_target>" -ex <mi_commands...>
// -ex detach -ex quit`, captures combined output, and stops the background process.
FlashResult gdb_mi_session(const std::vector<std::string>& server_argv,
                           const std::string& remote_target,
                           const std::vector<std::string>& mi_commands, double timeout_s,
                           const std::string& gdb_binary, double startup_delay_s = 0.4);

// openocd -f <openocd_cfg> (a board/interface+target config, e.g.
// "board/stm32f4discovery.cfg") as the GDB server (default port 3333), MI-driven via
// arm-none-eabi-gdb. `output` on success carries the RAW gdb/MI text (register/memory
// values arrive as MI tuples, not decoded to bytes — unlike probe_rs_read_memory below).
FlashResult openocd_gdb_mi(const std::string& openocd_cfg, const std::vector<std::string>& mi_commands,
                           double timeout_s = 30.0, const std::string& gdb_binary = "arm-none-eabi-gdb");
// monitor reset halt|run
FlashResult openocd_gdb_reset(const std::string& openocd_cfg, bool halt = true, double timeout_s = 30.0);
// -exec-interrupt + monitor halt
FlashResult openocd_gdb_halt(const std::string& openocd_cfg, double timeout_s = 30.0);
// -exec-step-instruction (or -exec-next-instruction when step_over)
FlashResult openocd_gdb_step(const std::string& openocd_cfg, bool step_over = false,
                             double timeout_s = 30.0);
// -break-insert *<address>
FlashResult openocd_gdb_set_breakpoint(const std::string& openocd_cfg, const std::string& address,
                                       double timeout_s = 30.0);
// -data-read-memory-bytes <address> <length>
FlashResult openocd_gdb_read_mem(const std::string& openocd_cfg, const std::string& address,
                                 long long length, double timeout_s = 30.0);

// --- Emulation (simavr = AVR, renode = ARM) ----------------------------------
//
// Free-run mode (gdb_commands empty): runs the emulator for `run_s` wall-clock seconds and
// captures its stdout/stderr (UART etc.); since neither emulator self-exits, a timeout kill
// at `run_s` is the EXPECTED outcome and reported as success=true (output notes the
// scheduled stop), not a failure — only a genuinely absent binary or a non-zero early exit
// is success=false.
//
// GDB-attach mode (gdb_commands non-empty): launches the emulator with its built-in GDB
// server enabled and drives it via gdb_mi_session (same shape as openocd_gdb_mi above).

// simavr -m <mcu> [-g] <elf_path>. `gdb_port` is simavr's built-in GDB server port
// (simavr default 1234) used only when `gdb_commands` is non-empty.
FlashResult simavr_run(const std::string& elf_path, const std::string& mcu, double run_s = 2.0,
                       int gdb_port = 1234, const std::vector<std::string>& gdb_commands = {},
                       double timeout_s = 30.0, const std::string& gdb_binary = "avr-gdb");

// renode --disable-xwt --console <resc_path> (same invocation shape as
// emulator.cpp's existing Renode call). `resc_path` is a pre-generated Renode script (the
// project's emulator.cpp already builds one for fw_sim_run); when `gdb_commands` is
// non-empty the script is expected to start Renode's GDB server on `gdb_port` (Renode
// default 3333, `machine StartGdbServer <port>`).
FlashResult renode_run(const std::string& resc_path, double run_s = 2.0, int gdb_port = 3333,
                       const std::vector<std::string>& gdb_commands = {}, double timeout_s = 30.0,
                       const std::string& gdb_binary = "arm-none-eabi-gdb");

// --- Live serial monitor (physical UART) -------------------------------------
//
// Raw termios (POSIX 8N1, non-canonical, non-blocking reads) — no pyserial/serialport-rs
// dependency added to the C++ core, per the landscape dossier's "minimal robust path" note
// for the serial transport layer. Handles are process-local (an in-core registry in
// hardware.cpp), valid for the lifetime of the hosting process (chudware-hostd). Non-POSIX
// builds: serial_open always fails with `err = "unsupported_platform: ..."`.

// Opens `port` at `baud` (8N1). Returns a handle id >= 0 on success, or -1 with `err` set.
int serial_open(const std::string& port, int baud, std::string& err);
// Non-blocking read of up to `max_bytes` currently buffered (immediate return, no wait).
// Throws std::runtime_error for an unknown/already-closed handle.
std::string serial_read(int handle, size_t max_bytes = 4096);
// Writes `data` verbatim; returns the number of bytes written. Throws for an unknown/closed
// handle.
size_t serial_write(int handle, const std::string& data);
// Closes and releases `handle`. Returns false (no-op) if already closed/unknown.
bool serial_close(int handle);

}  // namespace chudware::firmware
