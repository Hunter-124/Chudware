// Firmware workspace (T-FIN5) — full-viewport body for the "Firmware" title-bar tab.
// Project browser + sources | build console (parsed warnings) + emulation run panel with
// live UART console | GPIO/waveform trace viewer (aicad.firmware.trace/1, T-FIN1) |
// fw_status-gated Flash/Debug rail (honest gating: missing backend shows the T-FIN4
// doctor guidance, never a dead button).
//
// GL-thread only. ALL engine traffic goes through ASYNC st.rpc.call — never a sync
// worker call in a draw path (known GL-thread freeze bug), and no per-frame focus grabs.
#pragma once

struct AppState;

namespace ui {

// Full-body workspace renderer — hosted by app.cpp in the borderless central-band window
// (same pattern as drawPlmWorkspace / drawRevEngWorkspace).
void drawFirmwareWorkspace(AppState& st);

// Progressive disclosure (T-FIN24 rules): the title-bar tab is offered when a firmware
// project exists, an ECAD workspace/doc is in play, or the workspace is already active.
// Cheap to call per frame — project listing behind it is async + throttled.
bool firmwareTabVisible(AppState& st);

namespace firmware {
// AI_CAD_SHOT=firmware fixture: seeds a canned project + build console + emulation UART
// log + GPIO trace so the headless scenario shot exercises the full workspace rendering
// path without requiring arm-none-eabi-gcc / Renode on the box.
void seedFirmwareShotFixture();
}  // namespace firmware

}  // namespace ui
