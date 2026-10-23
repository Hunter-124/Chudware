// Firmware workspace — full-viewport body for the "Firmware" title-bar tab.
// Project browser + sources | build console (parsed warnings) + emulation run panel with
// live UART console | GPIO/waveform trace viewer (aicad.firmware.trace/1) |
// fw_status-gated Flash/Debug rail (honest gating: missing backend shows the
// doctor guidance, never a dead button).
//
// GL-thread only. ALL engine traffic goes through ASYNC st.rpc.call — never a sync
// worker call in a draw path (known GL-thread freeze bug), and no per-frame focus grabs.
#pragma once

#include <string>
#include <nlohmann/json_fwd.hpp>

struct AppState;

namespace ui {

// Full-body workspace renderer — hosted by app.cpp in the borderless central-band window
// (same pattern as drawPlmWorkspace / drawRevEngWorkspace).
void drawFirmwareWorkspace(AppState& st);

// Progressive disclosure rules: the title-bar tab is offered when a firmware
// project exists, an ECAD workspace/doc is in play, or the workspace is already active.
// Cheap to call per frame — "project exists" is a throttled LOCAL disk probe of
// firmware_root() (worker-independent, so the tab discloses at startup even before the
// Python-worker-only fw_projects RPC can answer), plus the throttled async listing.
bool firmwareTabVisible(AppState& st);

namespace firmware {
// CHUDWARE_SHOT=firmware fixture: seeds a canned project/build console, native
// editable GPIO/UART scenario, UART log and GPIO trace, and opens Emulate.
// This exercises setup rendering without requiring compiler/emulator tooling.
void seedFirmwareShotFixture();

// Same canonical scenario and draft consumed by Emulate's fw_sim_run; useful for
// observable gesture smoke checks without relying on a particular emulator install.
nlohmann::json firmwareScenarioDefinition();
std::string firmwareScenarioRawText();
bool firmwareScenarioDraftValid();
bool configureFirmwareScenario(const nlohmann::json& definition, std::string& error);
void openFirmwareEmulationSetup();
}  // namespace firmware

}  // namespace ui
