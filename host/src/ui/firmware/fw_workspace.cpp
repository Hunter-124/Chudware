// Firmware workspace — unified Arduino-style embedded IDE view (T-FIN5 restructure).
//
// Layout (full central band, same host window pattern as PLM/RevEng):
//   target bar : [Board/MCU chip][Toolchain][Port] .......... [Build][Flash][Debug][Monitor]
//   left  rail : project/board tree (fw_projects, ASYNC) + New Project + selected project's
//                sources | Pinmap (pins observed in the last emulation trace) | Backend doctor
//                (fw_status, T-FIN4) — honest gating: missing tools show a guidance banner
//                instead of a dead button.
//   center     : tabs — Editor (read-only source preview) | Debug (fw_debug_gdb_* console:
//                reset/halt/step, breakpoints, memory view) | Emulate (fw_sim_run /
//                fw_emulate_renode / fw_emulate_simavr live UART + GPIO trace viewer, contract
//                §7).
//   right      : AI Copilot pane — reuses the docked Copilot's own chat state/send path
//                (ui/copilot/copilot.h reuse hooks) so the firmware hub doesn't fork a second
//                chat implementation.
//   bottom dock: tabs — Build Log | Serial Monitor (fw_serial_*) | Flash Progress | GDB
//                Console. Collapsible via ui::PanelCollapseAnim (vertical roll-up, same
//                recipe as the FeatureManager column).
//
// Styling: the workspace speaks the shared "liquid glass" language (ui_theme.h) instead of
// stock ImGui chrome — glass children, accent-gradient section headers, tinted status
// pills, glass action buttons and glow selection, all scaled by ui::uiScale and painted
// from palette roles only (gui_lint).
//
// GL-thread rules honored throughout: every engine call is ASYNC (st.rpc.call + callbacks
// run in the drain), listing is throttled, and nothing grabs focus per-frame. Every new
// fw_debug_gdb_*/fw_serial_*/fw_avr_flash/fw_emulate_* control degrades gracefully — an
// unknown-RPC error just surfaces as text in the relevant console instead of hiding the
// control (the backend doctor still gates Flash/Debug on fw_status like before).
#include "fw_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui/copilot/copilot.h"
#include "ui/icons.h"
#include "ui/metrics.h"
#include "ui/panel_collapse.h"
#include "ui_theme.h"

namespace ui {
namespace firmware {

struct GpioEvent {
    std::string pin;
    int level = 0;
    double tMs = 0.0;
    bool hasT = false;
    int seq = 0;
};

struct FwProject {
    std::string id, name, mcu, toolchain, rootDir;
    std::vector<std::string> srcs;
};

struct FwState {
    // ---- backend doctor (fw_status, T-FIN4) ----
    bool statusPending = false, statusLoaded = false;
    double lastStatusT = -1e9;
    bool canBuild = false, canEmulate = false, canFlash = false;
    std::vector<std::string> banners;
    // name -> {found, version-or-hint}
    struct ToolRow { std::string name; bool found = false; std::string info; };
    std::vector<ToolRow> tools;
    std::string statusErr;

    // ---- project rail (fw_projects) ----
    bool listPending = false, listedOnce = false;
    double lastListT = -1e9;
    std::vector<FwProject> projects;
    int selected = -1;
    std::string listErr;
    char newNameBuf[64] = "";
    int newMcuIdx = 0;
    bool createPending = false;
    std::string createErr;

    // ---- build console (fw_build) ----
    bool buildPending = false, buildDone = false, buildOk = false;
    std::vector<std::string> warnings;
    std::string buildStderr, elfPath;
    double buildMs = 0.0;

    // ---- emulation (fw_sim_run / fw_emulate_renode / fw_emulate_simavr) ----
    bool simPending = false, simDone = false, simPassed = false;
    std::string uartLog, simErr, tracePath;
    double simMs = 0.0;
    std::vector<std::string> assertionLines;   // "PASS uart:hello" style summaries
    std::vector<GpioEvent> gpio;
    char assertBuf[256] = "";

    // ---- hardware rail (fw_flash / fw_avr_flash / fw_debug_start / fw_debug_halt) ----
    char chipBuf[64] = "STM32F407VGTx";
    bool hwPending = false;
    std::string hwOut;

    // ---- unified target bar ----
    char portBuf[64] = "";           // serial device, e.g. /dev/ttyACM0 — shared by
                                      // fw_avr_flash and fw_serial_open
    bool focusDebugTabOnce = false;  // top-bar "Debug" jumps the center tab strip
    bool focusMonitorDockOnce = false;  // top-bar "Monitor" jumps the bottom dock strip

    // ---- editor tab: read-only source preview (local disk, click-triggered) ----
    int editorSelSrc = -1;
    std::string editorContent, editorErr, editorPath;

    // ---- debug tab: gdb console (fw_debug_gdb_*) ----
    bool gdbPending = false;
    std::string gdbConsole;   // running transcript of every gdb command's raw output
    char bpFileLine[128] = "";
    struct Breakpoint { std::string loc, status; bool pending = false; };
    std::vector<Breakpoint> breakpoints;
    char memAddrBuf[32] = "0x20000000";
    char memLenBuf[16] = "64";
    std::string memDump, memErr;

    // ---- serial monitor (fw_serial_*) ----
    bool serialOpen = false, serialPending = false;
    std::string serialLog, serialErr;
    char serialSendBuf[256] = "";
    double lastSerialPollT = -1e9;

    // ---- bottom dock ----
    bool dockCollapsed = false;
    ui::PanelCollapseAnim dockAnim;
};

static FwState& S() {
    static FwState s;
    return s;
}

static const char* kMcuFamilies[] = {"cortex-m4", "cortex-m0", "cortex-m3", "cortex-m7",
                                     "esp32", "avr"};

// ---- glass styling helpers ------------------------------------------------------
// Small local widgets built on the shared ui_theme primitives (the same recipes the
// PLM hub and title bar use). Kept local so the firmware workspace stays self-contained.

// Section title: accent atlas icon + accent label + a short accent->transparent gradient
// underline (the title-bar divider trick). Replaces the old plain text + Separator().
static void fwSectionHeader(const char* icon, const char* label) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float h = ImGui::GetFrameHeight();
    float x = p0.x;
    if (icon && icon[0]) {
        const float isz = 16.0f * sc;
        ui::drawIconGlass(dl, icon, ImVec2(x + isz * 0.5f, p0.y + h * 0.5f), isz,
                          palette.toU32(palette.accent), 0);
        x += isz + 7.0f * sc;
    }
    dl->AddText(ImVec2(x, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f),
                palette.toU32(palette.accent), label);
    ImGui::Dummy(ImVec2(0.0f, h));
    const float w = std::min(ImGui::GetContentRegionAvail().x, 140.0f * sc);
    const float uy = p0.y + h + 1.0f * sc;
    const ImU32 ca = ImGui::GetColorU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.55f));
    const ImU32 cb = ImGui::GetColorU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.0f));
    dl->AddRectFilledMultiColor(ImVec2(p0.x, uy), ImVec2(p0.x + w, uy + sc), ca, cb, cb, ca);
    ImGui::Dummy(ImVec2(0.0f, 4.0f * sc));
}

// Status pill: soft tinted fill + tinted text + hairline border (the graph-tab kind-pill
// recipe). Used for build/sim verdicts so they read at a glance.
static void fwStatusPill(const char* text, const float tint[3]) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 ts = ImGui::CalcTextSize(text);
    const float pillH = ImGui::GetTextLineHeight() + 6.0f * sc;
    const float pillW = ts.x + 16.0f * sc;
    const ImVec2 p1(p0.x + pillW, p0.y + pillH);
    dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(tint[0], tint[1], tint[2], 0.16f)),
                      pillH * 0.5f);
    dl->AddRect(p0, p1, ImGui::GetColorU32(ImVec4(tint[0], tint[1], tint[2], 0.55f)),
                pillH * 0.5f, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + 8.0f * sc, p0.y + 3.0f * sc),
                ImGui::GetColorU32(ImVec4(tint[0], tint[1], tint[2], 1.0f)), text);
    ImGui::Dummy(ImVec2(pillW, pillH));
}

// Width a fwGlassButton will occupy — for laying an input row out next to one.
static float fwGlassButtonWidth(const char* label, const char* icon) {
    const float sc = ui::uiScale;
    float w = 28.0f * sc + ImGui::CalcTextSize(label).x;   // 2 * 14dp horizontal padding
    if (icon && icon[0]) w += 21.0f * sc;                  // 15dp glyph + 6dp gap
    return w;
}

// Liquid-glass action button (the plmGlassButton recipe): frosted base + optional accent
// fill with a hover/held alpha ramp, atlas icon + centered label. `enabled=false` draws
// the same shell dimmed and inert — BeginDisabled can't fade custom draw-list paint.
static bool fwGlassButton(const char* id, const char* label, const char* icon, bool accent,
                          float width, bool enabled = true) {
    const float sc = ui::uiScale;
    const float iconSz = (icon && icon[0]) ? 15.0f * sc : 0.0f;
    const float gap = iconSz > 0.0f ? 6.0f * sc : 0.0f;
    const float padX = 14.0f * sc;
    const float textW = ImGui::CalcTextSize(label).x;
    const float content = iconSz + gap + textW;
    const float w = width > 0.0f ? width : padX * 2.0f + content;
    const float h = ImGui::GetFrameHeight() + 2.0f * sc;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);
    bool clicked = false, hov = false, held = false;
    if (enabled) {
        clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
        hov = ImGui::IsItemHovered();
        held = ImGui::IsItemActive();
    } else {
        ImGui::Dummy(ImVec2(w, h));
        hov = ImGui::IsItemHovered();   // still lets callers show a gating tooltip
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float dim = enabled ? 1.0f : 0.45f;
    ui::glassBackground(dl, p0, p1, 9.0f, dim);
    if (accent) {
        const float a = (held ? 0.92f : hov ? 0.80f : 0.62f) * dim;
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], a)), 9.0f * sc);
    } else if (hov && enabled) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.text[0], palette.text[1], palette.text[2], held ? 0.10f : 0.06f)),
            9.0f * sc);
    }
    ImU32 tcol;
    if (accent && enabled) {
        tcol = ImGui::GetColorU32(ui::maxContrastTextOn(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f)));
    } else {
        tcol = enabled ? palette.toU32(palette.text) : palette.toU32(palette.textDisabled);
    }
    float tx = p0.x + (w - content) * 0.5f;
    if (iconSz > 0.0f) {
        ui::drawIconGlass(dl, icon, ImVec2(tx + iconSz * 0.5f, p0.y + h * 0.5f), iconSz,
                          tcol, 0);
        tx += iconSz + gap;
    }
    dl->AddText(ImVec2(tx, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol, label);
    return clicked;
}

// Doctor guidance banner: a tinted liquid-glass card (glassCardWash gradient) behind
// wrapped text. Height is pre-measured with CalcTextSize so the card hugs the text.
static void fwBanner(const char* text, const float tint[3]) {
    const float sc = ui::uiScale;
    const float padX = 10.0f * sc;
    const float padY = 7.0f * sc;
    const ImVec2 cp = ImGui::GetCursorPos();
    const float cardW = ImGui::GetContentRegionAvail().x;
    const float wrapW = std::max(40.0f * sc, cardW - padX * 2.0f);
    const ImVec2 ts = ImGui::CalcTextSize(text, nullptr, false, wrapW);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + cardW, p0.y + ts.y + padY * 2.0f);
    ui::glassCardWash(ImGui::GetWindowDrawList(), p0, p1, 8.0f, tint, 0.16f);
    ImGui::SetCursorPos(ImVec2(cp.x + padX, cp.y + padY));
    ImGui::PushTextWrapPos(cp.x + padX + wrapW);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(tint[0], tint[1], tint[2], 1.0f));
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
    ImGui::PopTextWrapPos();
    ImGui::SetCursorPos(ImVec2(cp.x, cp.y + (p1.y - p0.y) + 2.0f * sc));
}

// Project rail row: frosted pill + left->right accent gradient wash on hover/selected and
// a glow border when selected (the title-bar chip recipe). Returns clicked.
static bool fwProjectRow(const FwProject& p, bool selected) {
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight() + 4.0f * sc;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    const ImVec2 p1(p0.x + w, p0.y + rowH);
    const bool clicked = ImGui::InvisibleButton("##fw_row", ImVec2(w, rowH));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (selected || hov) {
        ui::glassRowBackground(dl, p0, p1, 6.0f, false, false, 0.20f);
        ui::coloredRowWash(dl, p0, p1, 6.0f, palette.accent, selected ? 0.34f : 0.16f, false);
    }
    if (selected) ui::glassSelectionGlow(dl, p0, p1, 6.0f, palette.accent);
    const float ty = p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;
    dl->AddText(ImVec2(p0.x + 10.0f * sc, ty), palette.toU32(palette.text), p.name.c_str());
    if (!p.mcu.empty()) {
        const ImVec2 ms = ImGui::CalcTextSize(p.mcu.c_str());
        dl->AddText(ImVec2(p1.x - ms.x - 10.0f * sc, ty),
                    palette.toU32(selected ? palette.accent : palette.textDisabled),
                    p.mcu.c_str());
    }
    return clicked;
}

// Small secondary action ("Refresh"/"Recheck") parked on the right edge of the header row
// that the next fwSectionHeader() call draws. Call IMMEDIATELY before fwSectionHeader.
static void fwHeaderAction(const char* label, float width, std::function<void()> onClick) {
    const ImVec2 cp = ImGui::GetCursorPos();
    ImGui::SetCursorPosX(cp.x + ImGui::GetContentRegionAvail().x - width);
    if (ImGui::SmallButton(label)) onClick();
    ImGui::SetCursorPos(cp);
}

// ---- async loaders (throttled; NEVER sync in the draw path) -----------------------------

static void loadStatus(AppState& st, bool force = false) {
    FwState& s = S();
#ifdef __EMSCRIPTEN__
    // Python-only firmware doctor is not on the wasm daemon path. Mark quiet-unavailable
    // so the rail shows an inline note instead of Worker error toasts.
    (void)st; (void)force;
    if (!s.statusLoaded) {
        s.statusLoaded = true;
        s.statusErr = "Firmware tooling is unavailable in the web client";
        s.canBuild = s.canEmulate = s.canFlash = false;
    }
    return;
#endif
    if (!st.workerStarted || s.statusPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.statusLoaded && now - s.lastStatusT < 15.0) return;
    s.statusPending = true;
    s.lastStatusT = now;
    st.rpc.call("fw_status", nlohmann::json::object(), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.statusPending = false;
        if (!r.ok || !r.result.is_object()) {
            s2.statusErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        s2.statusLoaded = true;
        s2.statusErr.clear();
        s2.banners.clear();
        s2.tools.clear();
        const auto& j = r.result;
        if (j.contains("families") && j["families"].is_object()) {
            // Gate on the cortex-m family (the primary bundled path); esp32/avr banners
            // still surface below via `banners`.
            const auto& fam = j["families"].value("cortex-m", nlohmann::json::object());
            s2.canBuild   = fam.value("can_build", false);
            s2.canEmulate = fam.value("can_emulate", false);
            s2.canFlash   = fam.value("can_flash", false);
        }
        if (j.contains("banners") && j["banners"].is_array())
            for (const auto& b : j["banners"])
                if (b.is_string()) s2.banners.push_back(b.get<std::string>());
        if (j.contains("tools") && j["tools"].is_object()) {
            for (auto it = j["tools"].begin(); it != j["tools"].end(); ++it) {
                if (!it.value().is_object()) continue;
                FwState::ToolRow row;
                row.name  = it.key();
                row.found = it.value().value("found", false);
                row.info  = row.found ? it.value().value("version", std::string())
                                      : it.value().value("install_hint", std::string());
                s2.tools.push_back(std::move(row));
            }
            std::sort(s2.tools.begin(), s2.tools.end(),
                      [](const FwState::ToolRow& a, const FwState::ToolRow& b) {
                          return a.name < b.name;
                      });
        }
    });
}

static void loadProjects(AppState& st, bool force = false) {
    FwState& s = S();
#ifdef __EMSCRIPTEN__
    // fw_projects lives on the Python worker only — never auto-poll under wasm.
    (void)st; (void)force;
    if (!s.listedOnce) {
        s.listedOnce = true;
        s.listErr = "Firmware projects are unavailable in the web client";
    }
    return;
#endif
    if (!st.workerStarted || s.listPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.listedOnce && now - s.lastListT < 4.0) return;
    s.listPending = true;
    s.lastListT = now;
    st.rpc.call("fw_projects", nlohmann::json::object(), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.listPending = false;
        s2.listedOnce = true;
        if (!r.ok || !r.result.is_object()) {
            s2.listErr = r.err.empty() ? "firmware tools unavailable" : r.err;
            return;
        }
        s2.listErr.clear();
        std::string keepId = (s2.selected >= 0 && s2.selected < (int)s2.projects.size())
                                 ? s2.projects[s2.selected].id : std::string();
        s2.projects.clear();
        if (r.result.contains("projects") && r.result["projects"].is_array()) {
            for (const auto& j : r.result["projects"]) {
                if (!j.is_object()) continue;
                FwProject p;
                p.id        = j.value("project_id", std::string());
                p.name      = j.value("name", p.id);
                p.mcu       = j.value("mcu_family", std::string());
                p.toolchain = j.value("toolchain", std::string());
                p.rootDir   = j.value("root_dir", std::string());
                if (j.contains("src_paths") && j["src_paths"].is_array())
                    for (const auto& sp : j["src_paths"])
                        if (sp.is_string()) p.srcs.push_back(sp.get<std::string>());
                s2.projects.push_back(std::move(p));
            }
        }
        s2.selected = -1;
        for (int i = 0; i < (int)s2.projects.size(); ++i)
            if (s2.projects[i].id == keepId) { s2.selected = i; break; }
        if (s2.selected < 0 && !s2.projects.empty()) s2.selected = 0;
    });
}

// Parse the aicad.firmware.trace/1 artifact (contract §7). Small file, local disk —
// reading it inline in the fw_sim_run completion callback is fine (drain runs on GL
// thread but this is a one-shot, not per-frame).
static void loadTraceArtifact(FwState& s) {
    s.gpio.clear();
    if (s.tracePath.empty()) return;
    std::ifstream f(s.tracePath);
    if (!f.good()) return;
    nlohmann::json j = nlohmann::json::parse(f, nullptr, /*allow_exceptions=*/false);
    if (!j.is_object() || j.value("schema", std::string()) != "aicad.firmware.trace/1") return;
    if (!j.contains("events") || !j["events"].is_array()) return;
    for (const auto& e : j["events"]) {
        if (!e.is_object() || e.value("type", std::string()) != "gpio") continue;
        GpioEvent ev;
        ev.pin   = e.value("pin", std::string());
        ev.level = e.value("level", 0);
        ev.seq   = e.value("seq", 0);
        if (e.contains("t_ms") && e["t_ms"].is_number()) {
            ev.tMs = e["t_ms"].get<double>();
            ev.hasT = true;
        }
        if (!ev.pin.empty()) s.gpio.push_back(std::move(ev));
    }
}

// Apply a SimResult payload (contract §4/§7.1) to the state. Shared by fw_sim_run and the
// backend-specific fw_emulate_renode / fw_emulate_simavr entry points — same result shape.
static void applySimResult(FwState& s, const nlohmann::json& j) {
    s.simDone   = true;
    s.simPassed = j.value("passed", false);
    s.uartLog   = j.value("uart_log", std::string());
    s.simMs     = j.value("ms", 0.0);
    s.tracePath = (j.contains("trace_path") && j["trace_path"].is_string())
                      ? j["trace_path"].get<std::string>() : std::string();
    s.assertionLines.clear();
    if (j.contains("assertion_results") && j["assertion_results"].is_array()) {
        for (const auto& a : j["assertion_results"]) {
            if (!a.is_object()) continue;
            std::string line = a.value("passed", false) ? "PASS  " : "FAIL  ";
            line += a.value("assertion", std::string());
            const std::string detail = a.value("detail", std::string());
            if (!detail.empty()) line += "  (" + detail + ")";
            s.assertionLines.push_back(std::move(line));
        }
    }
    // Prefer the structured trace artifact; fall back to the inline gpio_events.
    loadTraceArtifact(s);
    if (s.gpio.empty() && j.contains("gpio_events") && j["gpio_events"].is_array()) {
        for (const auto& e : j["gpio_events"]) {
            if (!e.is_object()) continue;
            GpioEvent ev;
            ev.pin   = e.value("pin", std::string());
            ev.level = e.value("level", 0);
            ev.seq   = e.value("seq", 0);
            if (e.contains("t_ms") && e["t_ms"].is_number()) {
                ev.tMs = e["t_ms"].get<double>();
                ev.hasT = true;
            }
            if (!ev.pin.empty()) s.gpio.push_back(std::move(ev));
        }
    }
}

// ---- tab disclosure probe ----------------------------------------------------
// T-FIN24: the tab discloses when a firmware project EXISTS. fw_projects is a
// Python-worker-only RPC (on Linux it dies with "worker not started" whenever the
// GUI's worker pipe isn't up — slow boot, missing runtime, or the AsyncRpc dispatch
// thread jammed behind startup calls), so an RPC-only probe hid the tab until the
// user happened into the Electronics workspace. The project store is plain
// <firmware_root>/<id>/project.json sidecars on LOCAL disk (same files the trace
// viewer reads), so probe the disk directly: read-only, throttled, worker-independent.
bool firmwareProjectsExistOnDisk() {
    static double lastProbe = -1e9;
    static bool cached = false;
    const double now = ImGui::GetTime();
    if (now - lastProbe < 5.0) return cached;
    lastProbe = now;
    cached = false;
    // Mirror engine.firmware.project.firmware_root(): CHUDWARE_FIRMWARE_DIR override,
    // else %APPDATA%/Chudware/firmware, else <tmp>/Chudware/firmware (Python
    // tempfile.gettempdir() honors TMPDIR/TEMP/TMP, then /tmp).
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path root;
    if (const char* ov = std::getenv("CHUDWARE_FIRMWARE_DIR"); ov && *ov) {
        root = ov;
    } else if (const char* ad = std::getenv("APPDATA"); ad && *ad) {
        root = fs::path(ad) / "Chudware" / "firmware";
    } else {
        const char* tmp = std::getenv("TMPDIR");
        if (!tmp || !*tmp) tmp = std::getenv("TEMP");
        if (!tmp || !*tmp) tmp = std::getenv("TMP");
        if (!tmp || !*tmp) tmp = "/tmp";
        root = fs::path(tmp) / "Chudware" / "firmware";
    }
    if (!fs::is_directory(root, ec) || ec) return false;
    for (fs::directory_iterator it(root, ec), end; !ec && it != end; it.increment(ec)) {
        if (it->is_directory(ec) && !ec && fs::is_regular_file(it->path() / "project.json", ec)) {
            cached = true;
            break;
        }
    }
    return cached;
}

// ---- unified IDE triggers: build / flash / debug / emulate / serial ---------------------
// Every one of these is a thin ASYNC wrapper over st.rpc.call — none ever block the GL
// thread, and every completion callback captures nothing but small values (indices/strings),
// reaching state ONLY through S() so it stays valid even if the AppState& that triggered the
// call has moved on by the time the worker replies.

// Generic {success, output/reason} envelope -> a one-line status string in s.hwOut (the
// existing fw_flash/fw_debug_start/fw_debug_halt convention, now shared by fw_avr_flash too).
static void hwCall(AppState& st, const char* method, nlohmann::json p) {
    FwState& s = S();
    s.hwPending = true;
    s.hwOut.clear();
    st.rpc.call(method, std::move(p), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.hwPending = false;
        if (!r.ok) { s2.hwOut = r.err.empty() ? "worker unreachable" : r.err; return; }
        const auto& j = r.result;
        if (!j.is_object()) { s2.hwOut = "unexpected result"; return; }
        if (j.contains("code")) { s2.hwOut = j.value("message", std::string("failed")); return; }
        std::string out = j.value("success", false) ? "OK" : "failed";
        const std::string detail = j.contains("output") ? j.value("output", std::string())
                                                          : j.value("reason", std::string());
        if (!detail.empty()) out += ": " + detail;
        s2.hwOut = out;
    });
}

static void triggerBuild(AppState& st) {
    FwState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.projects.size() || s.buildPending) return;
    s.buildPending = true;
    s.buildDone = false;
    s.warnings.clear(); s.buildStderr.clear(); s.elfPath.clear();
    nlohmann::json p{{"project_id", s.projects[s.selected].id}};
    st.rpc.call("fw_build", std::move(p), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.buildPending = false;
        s2.buildDone = true;
        if (!r.ok || !r.result.is_object()) {
            s2.buildOk = false;
            s2.buildStderr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        const auto& j = r.result;
        if (j.contains("code")) {   // error envelope (unknown project, ...)
            s2.buildOk = false;
            s2.buildStderr = j.value("message", std::string("build failed"));
            return;
        }
        s2.buildOk = j.value("success", false);
        s2.buildStderr = j.value("stderr", std::string());
        s2.buildMs = j.value("ms", 0.0);
        if (j.contains("elf_path") && j["elf_path"].is_string())
            s2.elfPath = j["elf_path"].get<std::string>();
        if (j.contains("warnings") && j["warnings"].is_array())
            for (const auto& w : j["warnings"])
                if (w.is_string()) s2.warnings.push_back(w.get<std::string>());
    });
}

// Picks fw_avr_flash for avr-family projects (needs a serial port for avrdude-style
// flashing), else the existing probe-rs fw_flash path.
static void triggerFlash(AppState& st) {
    FwState& s = S();
    if (s.hwPending || s.elfPath.empty()) return;
    const bool avr = s.selected >= 0 && s.selected < (int)s.projects.size() &&
                     s.projects[s.selected].mcu == "avr";
    nlohmann::json p{{"elf_path", s.elfPath}, {"chip", std::string(s.chipBuf)}};
    if (avr && s.portBuf[0]) p["port"] = std::string(s.portBuf);
    hwCall(st, avr ? "fw_avr_flash" : "fw_flash", std::move(p));
}

// gdb console call: fw_debug_gdb_reset/halt/step/set_breakpoint/read_mem all share the
// generic {success, output, returncode, cmd, ms} envelope — no structured registers/
// callstack/vars in that shape, so the raw "output" text is what surfaces (in the caller's
// own display AND the shared GDB Console dock tab transcript).
static void gdbCall(AppState& st, const char* method, nlohmann::json p,
                    std::function<void(bool ok, const std::string& out)> onDone = nullptr) {
    FwState& s = S();
    s.gdbPending = true;
    std::string cmdLabel = method;
    st.rpc.call(method, std::move(p), [onDone, cmdLabel](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.gdbPending = false;
        bool ok = false;
        std::string text;
        if (!r.ok || !r.result.is_object()) {
            text = r.err.empty() ? "worker unreachable" : r.err;
        } else if (r.result.contains("code")) {
            text = r.result.value("message", std::string("unavailable"));
        } else {
            const auto& j = r.result;
            ok = j.value("success", false);
            text = j.contains("output") ? j.value("output", std::string())
                                        : j.value("reason", std::string());
        }
        std::string line = "$ " + cmdLabel + "\n" + (ok ? "OK" : "FAILED");
        if (!text.empty()) line += "\n" + text;
        if (!s2.gdbConsole.empty()) s2.gdbConsole += "\n";
        s2.gdbConsole += line;
        if (s2.gdbConsole.size() > 32000)   // cap the transcript so it never runs away
            s2.gdbConsole.erase(0, s2.gdbConsole.size() - 32000);
        if (onDone) onDone(ok, text);
    });
}

// Split the ';'-separated scenario text (assertBuf) into an assertion array. Shared by
// fw_sim_run and the two backend-specific emulate RPCs.
static nlohmann::json parseAssertionBuf(const char* buf) {
    nlohmann::json asserts = nlohmann::json::array();
    std::string s(buf);
    size_t start = 0;
    while (start <= s.size()) {
        size_t end = s.find(';', start);
        std::string tok = s.substr(start, end == std::string::npos ? std::string::npos
                                                                   : end - start);
        const size_t a = tok.find_first_not_of(" \t");
        const size_t b = tok.find_last_not_of(" \t");
        if (a != std::string::npos) asserts.push_back(tok.substr(a, b - a + 1));
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return asserts;
}

static void runEmulation(AppState& st, const char* method) {
    FwState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.projects.size() || s.simPending) return;
    s.simPending = true;
    s.simDone = false;
    s.uartLog.clear(); s.assertionLines.clear(); s.gpio.clear();
    s.tracePath.clear(); s.simErr.clear();
    nlohmann::json p{{"project_id", s.projects[s.selected].id}};
    nlohmann::json asserts = parseAssertionBuf(s.assertBuf);
    if (!asserts.empty()) p["assertions"] = asserts;
    st.rpc.call(method, std::move(p), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.simPending = false;
        if (!r.ok || !r.result.is_object()) {
            s2.simDone = true;
            s2.simPassed = false;
            s2.simErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (r.result.contains("code")) {
            s2.simDone = true;
            s2.simPassed = false;
            s2.simErr = r.result.value("message", std::string("emulation failed"));
            return;
        }
        applySimResult(s2, r.result);
    });
}

// Read-only source preview: local disk, one-shot on user click (same justification as
// loadTraceArtifact — not per-frame, so a sync ifstream on the GL thread is fine).
static void loadEditorFile(FwState& s, const FwProject& p, const std::string& rel) {
    s.editorContent.clear();
    s.editorErr.clear();
    s.editorPath = rel;
    namespace fs = std::filesystem;
    const fs::path full = p.rootDir.empty() ? fs::path(rel) : fs::path(p.rootDir) / rel;
    std::ifstream f(full, std::ios::binary);
    if (!f.good()) { s.editorErr = "Could not open " + full.string(); return; }
    std::ostringstream oss;
    oss << f.rdbuf();
    std::string content = oss.str();
    constexpr size_t kMax = 96 * 1024;   // cap huge files so the preview never lags a frame
    if (content.size() > kMax) {
        content.resize(kMax);
        content += "\n...(truncated)...\n";
    }
    s.editorContent = std::move(content);
}

// ---- serial monitor (fw_serial_*) --------------------------------------------------------

static void triggerSerialOpen(AppState& st) {
    FwState& s = S();
    if (s.serialPending || s.serialOpen) return;
    s.serialPending = true;
    s.serialErr.clear();
    nlohmann::json p{{"port", std::string(s.portBuf)}, {"baud", 115200}};
    st.rpc.call("fw_serial_open", std::move(p), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.serialPending = false;
        if (!r.ok || !r.result.is_object()) {
            s2.serialErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (r.result.contains("code")) {
            s2.serialErr = r.result.value("message", std::string("open failed"));
            return;
        }
        s2.serialOpen = r.result.value("success", false);
        if (!s2.serialOpen) s2.serialErr = r.result.value("reason", std::string("open failed"));
    });
}

static void triggerSerialClose(AppState& st) {
    FwState& s = S();
    if (s.serialPending) return;
    s.serialPending = true;
    st.rpc.call("fw_serial_close", nlohmann::json::object(), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.serialPending = false;
        s2.serialOpen = false;   // always end locally — a failed close still frees the UI
        (void)r;
    });
}

static void triggerSerialSend(AppState& st) {
    FwState& s = S();
    if (!s.serialOpen || !s.serialSendBuf[0]) return;
    std::string data(s.serialSendBuf);
    s.serialSendBuf[0] = '\0';
    st.rpc.call("fw_serial_write", {{"data", data}}, [](const core::RpcResult& r) {
        FwState& s2 = S();
        if (!r.ok) { s2.serialErr = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (r.result.is_object() && r.result.contains("code"))
            s2.serialErr = r.result.value("message", std::string("write failed"));
    });
}

// Throttled poll while the port is open — mirrors loadStatus/loadProjects's pattern, just on
// a much shorter period since serial data is latency-sensitive.
static void pollSerial(AppState& st) {
    FwState& s = S();
    if (!s.serialOpen || s.serialPending || !st.workerStarted) return;
    const double now = ImGui::GetTime();
    if (now - s.lastSerialPollT < 0.2) return;
    s.serialPending = true;
    s.lastSerialPollT = now;
    st.rpc.call("fw_serial_read", nlohmann::json::object(), [](const core::RpcResult& r) {
        FwState& s2 = S();
        s2.serialPending = false;
        if (!r.ok || !r.result.is_object()) {
            s2.serialErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (r.result.contains("code")) {
            s2.serialErr = r.result.value("message", std::string("read failed"));
            return;
        }
        if (!r.result.value("success", true)) {
            s2.serialErr = r.result.value("reason", std::string("read failed"));
            return;
        }
        const std::string data = r.result.value("data", r.result.value("output", std::string()));
        if (!data.empty()) {
            s2.serialLog += data;
            s2.serialErr.clear();
            constexpr size_t kMax = 64 * 1024;
            if (s2.serialLog.size() > kMax) s2.serialLog.erase(0, s2.serialLog.size() - kMax);
        }
    });
}

// ---- target bar: Board/MCU + Toolchain + Port .. Build / Flash / Debug / Monitor ---------

static void drawTargetBar(AppState& st) {
    FwState& s = S();
    const float sc = ui::uiScale;
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();

    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 8 * sc));
    ImGui::BeginChild("##fw_targetbar", ImVec2(0, ImGui::GetFrameHeight() + 16.0f * sc),
                      ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    const float rowStartX = ImGui::GetCursorPosX();
    const float contentMaxX = rowStartX + ImGui::GetContentRegionAvail().x;

    ImGui::AlignTextToFramePadding();
    ImGui::TextDisabled("Board/MCU");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(150.0f * sc);
    ImGui::InputText("##fw_target_chip", s.chipBuf, sizeof(s.chipBuf));
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Target chip id used by Flash/Debug (probe-rs style, e.g. "
                          "STM32F407VGTx)");

    ImGui::SameLine(0.0f, 16.0f * sc);
    ImGui::AlignTextToFramePadding();
    ImGui::TextDisabled("Toolchain");
    ImGui::SameLine();
    ImGui::TextUnformatted(haveProject && !s.projects[s.selected].toolchain.empty()
                           ? s.projects[s.selected].toolchain.c_str() : "-");

    ImGui::SameLine(0.0f, 16.0f * sc);
    ImGui::AlignTextToFramePadding();
    ImGui::TextDisabled("Port");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(130.0f * sc);
    ImGui::InputTextWithHint("##fw_target_port", "/dev/ttyACM0", s.portBuf, sizeof(s.portBuf));

    // Right-aligned action cluster: Build / Flash / Debug / Monitor.
    const float bw = 84.0f * sc;
    const float gap = 8.0f * sc;
    const float clusterW = bw * 4.0f + gap * 3.0f;
    ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX(), contentMaxX - clusterW));

    const bool canBuildGo = haveProject && st.workerStarted && !s.buildPending &&
                            (!s.statusLoaded || s.canBuild);
    if (fwGlassButton("##fw_tb_build", s.buildPending ? "Building\xe2\x80\xa6" : "Build",
                      "eng_firm", /*accent=*/true, bw, canBuildGo))
        triggerBuild(st);
    if (!canBuildGo && s.statusLoaded && !s.canBuild && ImGui::IsItemHovered())
        ImGui::SetTooltip("Toolchain missing — see Backend panel for fw_status guidance");
    ImGui::SameLine(0.0f, gap);

    const bool canFlashGo = haveProject && st.workerStarted && !s.hwPending &&
                            !s.elfPath.empty() && (!s.statusLoaded || s.canFlash);
    if (fwGlassButton("##fw_tb_flash", s.hwPending ? "\xe2\x80\xa6" : "Flash", "issue_bug",
                      /*accent=*/false, bw, canFlashGo))
        triggerFlash(st);
    if (!canFlashGo && ImGui::IsItemHovered()) {
        if (s.elfPath.empty()) ImGui::SetTooltip("Build first — the last successful ELF is flashed");
        else if (s.statusLoaded && !s.canFlash)
            ImGui::SetTooltip("Flashing unavailable — see Backend panel for fw_status guidance");
    }
    ImGui::SameLine(0.0f, gap);

    if (fwGlassButton("##fw_tb_debug", "Debug", "run_study", /*accent=*/false, bw,
                      st.workerStarted)) {
        s.focusDebugTabOnce = true;
    }
    ImGui::SameLine(0.0f, gap);

    if (fwGlassButton("##fw_tb_monitor", s.serialOpen ? "Monitor*" : "Monitor", "activity",
                      /*accent=*/false, bw, st.workerStarted)) {
        s.focusMonitorDockOnce = true;
        s.dockCollapsed = false;
        if (!s.serialOpen && !s.serialPending && s.portBuf[0]) triggerSerialOpen(st);
    }
    if (!s.portBuf[0] && !s.serialOpen && ImGui::IsItemHovered())
        ImGui::SetTooltip("Set a Port above to auto-open the serial monitor");

    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- left rail: project/board tree + sources + pinmap + backend doctor ------------------

static void drawProjectRail(AppState& st, float w) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 10 * sc));
    ImGui::BeginChild("##fw_rail", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    fwHeaderAction(s.listPending ? "..." : "Refresh", 64.0f * sc,
                   [&st]() { loadProjects(st, /*force=*/true); });
    fwSectionHeader("folder_new", "Projects");

    // New project (create only when the toolchain family can at least be selected —
    // creation itself is always allowed; the BUILD is what fw_status gates).
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##fw_new_name", "New project name", s.newNameBuf,
                             sizeof(s.newNameBuf));
    ImGui::SetNextItemWidth(-1);
    ImGui::Combo("##fw_new_mcu", &s.newMcuIdx, kMcuFamilies,
                 (int)(sizeof(kMcuFamilies) / sizeof(kMcuFamilies[0])));
    const bool canCreate = st.workerStarted && !s.createPending;
    if (fwGlassButton("##fw_create",
                      s.createPending ? "Creating\xe2\x80\xa6" : "New Project",
                      "new_doc", /*accent=*/true, ImGui::GetContentRegionAvail().x,
                      canCreate)) {
        s.createPending = true;
        s.createErr.clear();
        nlohmann::json p;
        if (s.newNameBuf[0]) p["name"] = std::string(s.newNameBuf);
        p["mcu_family"] = kMcuFamilies[s.newMcuIdx];
        st.rpc.call("fw_new_project", p, [&st](const core::RpcResult& r) {
            FwState& s2 = S();
            s2.createPending = false;
            if (!r.ok) { s2.createErr = r.err.empty() ? "worker unreachable" : r.err; return; }
            if (r.result.is_object() && r.result.contains("code")) {
                s2.createErr = r.result.value("message", std::string("create failed"));
                return;
            }
            s2.newNameBuf[0] = '\0';
            s2.lastListT = -1e9;   // expire throttle → refresh the rail now
            loadProjects(st, /*force=*/true);
        });
    }
    if (!canCreate && !st.workerStarted && ImGui::IsItemHovered())
        ImGui::SetTooltip("Worker not running");
    if (!s.createErr.empty()) fwBanner(s.createErr.c_str(), palette.statusErr);
    ImGui::Spacing();

    if (!st.workerStarted) {
        ImGui::TextDisabled("Worker not running.");
    } else if (s.projects.empty()) {
        if (!s.listErr.empty()) fwBanner(s.listErr.c_str(), palette.statusWarn);
        else ImGui::TextDisabled("No firmware projects yet.\nUse New Project above.");
    } else {
        for (int i = 0; i < (int)s.projects.size(); ++i) {
            const FwProject& p = s.projects[i];
            ImGui::PushID(i);
            if (fwProjectRow(p, s.selected == i)) {
                if (s.selected != i) {
                    s.selected = i;
                    // Reset per-project consoles so results never leak across projects.
                    s.buildDone = false;
                    s.warnings.clear(); s.buildStderr.clear(); s.elfPath.clear();
                    s.simDone = false;
                    s.uartLog.clear(); s.assertionLines.clear(); s.gpio.clear();
                    s.tracePath.clear();
                    s.editorSelSrc = -1;
                    s.editorContent.clear(); s.editorErr.clear(); s.editorPath.clear();
                    s.breakpoints.clear();
                    s.gdbConsole.clear();
                    s.memDump.clear(); s.memErr.clear();
                }
            }
            ImGui::PopID();
        }
    }

    // Sources of the selected project.
    if (s.selected >= 0 && s.selected < (int)s.projects.size()) {
        const FwProject& p = s.projects[s.selected];
        ImGui::Spacing();
        fwSectionHeader(nullptr, "Sources");
        if (p.srcs.empty()) ImGui::TextDisabled("(none registered)");
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        for (const std::string& src : p.srcs) ImGui::BulletText("%s", src.c_str());
        if (st.fonts.mono) ImGui::PopFont();
        if (!p.rootDir.empty()) {
            ImGui::Spacing();
            ImGui::TextDisabled("Root");
            ImGui::TextWrapped("%s", p.rootDir.c_str());
        }
    }

    // Pinmap: no dedicated pin-assignment RPC exists yet, so this surfaces pins OBSERVED
    // during the last emulation trace rather than a synthesized full chip pinout — an
    // honest, graceful stand-in until a real fw_pinmap-style RPC lands.
    ImGui::Spacing();
    fwSectionHeader("cat_electronics", "Pinmap (observed)");
    if (s.gpio.empty()) {
        ImGui::TextDisabled("Run an emulation (Emulate tab) to populate observed pins.");
    } else {
        std::vector<std::string> pins;
        for (const GpioEvent& e : s.gpio)
            if (std::find(pins.begin(), pins.end(), e.pin) == pins.end()) pins.push_back(e.pin);
        for (const std::string& pin : pins) ImGui::BulletText("%s", pin.c_str());
    }

    // Backend doctor (fw_status) — moved here from the old right rail now that the right
    // column hosts the AI Copilot pane. Flash/Debug actions themselves live in the target
    // bar + Debug tab; this stays the honest-gating status readout.
    ImGui::Spacing();
    fwHeaderAction(s.statusPending ? "..." : "Recheck", 68.0f * sc,
                   [&st]() { loadStatus(st, /*force=*/true); });
    fwSectionHeader("health_check", "Backend");
    if (!s.statusLoaded) {
        ImGui::TextDisabled(s.statusErr.empty() ? "Checking fw_status\xe2\x80\xa6"
                                                : s.statusErr.c_str());
    } else {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float dotR = 4.0f * sc;
        for (const FwState::ToolRow& t : s.tools) {
            const ImVec2 rp = ImGui::GetCursorScreenPos();
            const float rowH = ImGui::GetFrameHeight();
            const ImVec2 c(rp.x + dotR + 2.0f * sc, rp.y + rowH * 0.5f);
            if (t.found) {
                dl->AddCircleFilled(c, dotR + 2.5f * sc,
                                    ImGui::GetColorU32(ImVec4(palette.statusOk[0],
                                        palette.statusOk[1], palette.statusOk[2], 0.18f)));
                dl->AddCircleFilled(c, dotR, palette.toU32(palette.statusOk));
            } else {
                dl->AddCircle(c, dotR, palette.toU32(palette.textDisabled), 0,
                              ui::metrics::stroke(1.0f));
            }
            ImGui::Dummy(ImVec2(dotR * 2.0f + 10.0f * sc, rowH));
            ImGui::SameLine(0.0f, 0.0f);
            if (t.found) ImGui::TextUnformatted(t.name.c_str());
            else         ImGui::TextDisabled("%s", t.name.c_str());
            if (!t.info.empty() && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", t.info.c_str());
        }
        for (const std::string& b : s.banners) {
            ImGui::Spacing();
            fwBanner(b.c_str(), palette.statusWarn);
        }
    }

    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- center tab: Editor (read-only source preview) --------------------------------------

static void drawEditorTab(AppState& st) {
    FwState& s = S();
    const float sc = ui::uiScale;
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (!haveProject) {
        ImGui::TextDisabled("Select a project on the left to browse its sources.");
        return;
    }
    const FwProject& p = s.projects[s.selected];

    const float listW = 190.0f * sc;
    ui::pushGlassChildStyle(8.0f);
    ImGui::BeginChild("##fw_editor_list", ImVec2(listW, 0), ImGuiChildFlags_Border);
    fwSectionHeader(nullptr, "Sources");
    if (p.srcs.empty()) {
        ImGui::TextDisabled("(none registered)");
    } else {
        for (int i = 0; i < (int)p.srcs.size(); ++i) {
            ImGui::PushID(i);
            if (ImGui::Selectable(p.srcs[i].c_str(), s.editorSelSrc == i)) {
                s.editorSelSrc = i;
                loadEditorFile(s, p, p.srcs[i]);
            }
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
    ui::popGlassChildStyle();

    ImGui::SameLine(0.0f, 8.0f * sc);
    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_editor_view", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (s.editorSelSrc < 0) {
        ImGui::TextDisabled("Pick a source file on the left to preview it here.\n"
                            "(Read-only preview for now — edit in your external editor.)");
    } else if (!s.editorErr.empty()) {
        fwBanner(s.editorErr.c_str(), palette.statusErr);
    } else {
        ImGui::TextDisabled("%s", s.editorPath.c_str());
        ImGui::Separator();
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleDefault[0],
                                                    palette.consoleDefault[1],
                                                    palette.consoleDefault[2], 1));
        ImGui::TextUnformatted(s.editorContent.c_str());
        ImGui::PopStyleColor();
        if (st.fonts.mono) ImGui::PopFont();
    }
    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- center tab: Debug (fw_debug_gdb_* console) ------------------------------------------

static void drawDebugTab(AppState& st) {
    FwState& s = S();
    const float sc = ui::uiScale;
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();

    if (s.statusLoaded && !s.canFlash) {
        fwBanner("Debug backend unavailable: `cargo install probe-rs-tools` (Cortex-M) or "
                 "`pip install esptool` (ESP32). The controls below still degrade to a "
                 "per-command error instead of hiding.", palette.statusWarn);
    }
    ImGui::TextDisabled("Target: %s", s.chipBuf[0] ? s.chipBuf : "(set a chip in the target bar)");

    ImGui::Spacing();
    fwSectionHeader("run_study", "Session");
    const bool canHw = st.workerStarted && !s.hwPending && s.chipBuf[0] != '\0';
    const float bw2 = 130.0f * sc;
    if (fwGlassButton("##fw_dbg_run", s.hwPending ? "Working\xe2\x80\xa6" : "Reset + run",
                      "run_study", /*accent=*/true, bw2, canHw && haveProject))
        hwCall(st, "fw_debug_start", {{"chip", std::string(s.chipBuf)}});
    ImGui::SameLine(0.0f, 8.0f * sc);
    if (fwGlassButton("##fw_dbg_halt2", "Halt", nullptr, /*accent=*/false, bw2, canHw))
        hwCall(st, "fw_debug_halt", {{"chip", std::string(s.chipBuf)}});
    if (!s.hwOut.empty()) {
        ImGui::Spacing();
        fwBanner(s.hwOut.c_str(), s.hwOut.rfind("OK", 0) == 0 ? palette.statusOk
                                                              : palette.statusErr);
    }

    ImGui::Spacing();
    fwSectionHeader("health_check", "GDB controls");
    const bool gdbGo = st.workerStarted && !s.gdbPending;
    const float bw = 88.0f * sc;
    if (fwGlassButton("##fw_gdb_reset", "Reset", nullptr, /*accent=*/false, bw, gdbGo))
        gdbCall(st, "fw_debug_gdb_reset", {{"chip", std::string(s.chipBuf)}});
    ImGui::SameLine();
    if (fwGlassButton("##fw_gdb_halt", "Halt", nullptr, /*accent=*/false, bw, gdbGo))
        gdbCall(st, "fw_debug_gdb_halt", {{"chip", std::string(s.chipBuf)}});
    ImGui::SameLine();
    if (fwGlassButton("##fw_gdb_step", "Step", nullptr, /*accent=*/false, bw, gdbGo))
        gdbCall(st, "fw_debug_gdb_step", {{"chip", std::string(s.chipBuf)}});

    ImGui::Spacing();
    fwSectionHeader("milestone_flag", "Breakpoints");
    const float addW = fwGlassButtonWidth("Add", nullptr);
    ImGui::SetNextItemWidth(-(addW + 8.0f * sc));
    ImGui::InputTextWithHint("##fw_bp_loc", "file:line, e.g. main.c:42", s.bpFileLine,
                             sizeof(s.bpFileLine));
    ImGui::SameLine(0.0f, 8.0f * sc);
    if (fwGlassButton("##fw_bp_add", "Add", nullptr, /*accent=*/true, addW,
                      gdbGo && s.bpFileLine[0] != '\0')) {
        FwState::Breakpoint bp;
        bp.loc = s.bpFileLine;
        bp.pending = true;
        s.breakpoints.push_back(bp);
        const int idx = (int)s.breakpoints.size() - 1;
        const std::string loc = s.bpFileLine;
        gdbCall(st, "fw_debug_gdb_set_breakpoint", {{"location", loc}},
                [idx](bool ok, const std::string& out) {
            FwState& s3 = S();
            if (idx >= 0 && idx < (int)s3.breakpoints.size()) {
                s3.breakpoints[idx].pending = false;
                s3.breakpoints[idx].status = ok ? "set" : (out.empty() ? "failed" : out);
            }
        });
        s.bpFileLine[0] = '\0';
    }
    if (s.breakpoints.empty()) {
        ImGui::TextDisabled("No breakpoints yet.");
    } else {
        for (const FwState::Breakpoint& bp : s.breakpoints) {
            const std::string label = bp.pending ? "..." : (bp.status.empty() ? "?" : bp.status);
            ImGui::BulletText("%s  [%s]", bp.loc.c_str(), label.c_str());
        }
    }

    ImGui::Spacing();
    fwSectionHeader(nullptr, "Memory");
    ImGui::SetNextItemWidth(120.0f * sc);
    ImGui::InputText("##fw_mem_addr", s.memAddrBuf, sizeof(s.memAddrBuf));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(64.0f * sc);
    ImGui::InputText("##fw_mem_len", s.memLenBuf, sizeof(s.memLenBuf));
    ImGui::SameLine();
    if (fwGlassButton("##fw_mem_read", "Read", nullptr, /*accent=*/true, 64.0f * sc, gdbGo)) {
        int len = (int)std::strtol(s.memLenBuf, nullptr, 10);
        if (len <= 0) len = 64;
        s.memErr.clear();
        gdbCall(st, "fw_debug_gdb_read_mem",
                {{"address", std::string(s.memAddrBuf)}, {"length", len}},
                [](bool ok, const std::string& out) {
            FwState& s3 = S();
            if (ok) { s3.memDump = out; s3.memErr.clear(); }
            else    { s3.memErr = out.empty() ? "read failed" : out; }
        });
    }
    ui::pushGlassChildStyle(6.0f);
    ImGui::BeginChild("##fw_mem_dump", ImVec2(0, 90.0f * sc), ImGuiChildFlags_Border);
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (!s.memErr.empty()) ImGui::TextWrapped("%s", s.memErr.c_str());
    else if (s.memDump.empty()) ImGui::TextDisabled("Memory reads appear here.");
    else ImGui::TextUnformatted(s.memDump.c_str());
    if (st.fonts.mono) ImGui::PopFont();
    ImGui::EndChild();
    ui::popGlassChildStyle();

    ImGui::Spacing();
    ImGui::TextDisabled("Registers / call stack / locals: no dedicated RPC on this build yet —\n"
                        "Reset/Halt/Step/Memory results stream into the GDB Console dock tab.");
}

// ---- center tab: Emulate (fw_sim_run / fw_emulate_renode / fw_emulate_simavr) ------------

static void drawEmulationSection(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_sim_sec", ImVec2(0, h), ImGuiChildFlags_None);
    fwSectionHeader("run_study", "Emulation");
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (haveProject) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], 1));
        ImGui::Text("platform: %s  (Renode preferred, QEMU/simavr fallback)",
                    s.projects[s.selected].mcu.c_str());
        ImGui::PopStyleColor();
    }

    if (s.statusLoaded && !s.canEmulate) {
        fwBanner("Emulation unavailable: install Renode (preferred), simavr, or "
                 "qemu-system-arm.", palette.statusWarn);
    } else {
        const char* runLabel = s.simPending ? "Running\xe2\x80\xa6" : "Run emulation";
        const float runW = fwGlassButtonWidth(runLabel, "run_study");
        const float renodeW = fwGlassButtonWidth("Renode", "run_study");
        const float avrW = fwGlassButtonWidth("simavr", "run_study");
        ImGui::SetNextItemWidth(-(runW + renodeW + avrW + 24.0f * sc));
        ImGui::InputTextWithHint("##fw_asserts",
                                 "Scenario assertions, ';'-separated  e.g. uart:hello; gpio:PA5=1",
                                 s.assertBuf, sizeof(s.assertBuf));
        const bool canGo = haveProject && st.workerStarted && !s.simPending;

        ImGui::SameLine(0.0f, 8.0f * sc);
        if (fwGlassButton("##fw_sim_btn", runLabel, "run_study", /*accent=*/true, runW, canGo))
            runEmulation(st, "fw_sim_run");
        if (!canGo && !haveProject && ImGui::IsItemHovered())
            ImGui::SetTooltip("Select a project first");

        // Backend-specific variants — same SimResult contract as fw_sim_run's auto-pick;
        // useful when a project needs ONE backend forced (e.g. AVR chips only run under
        // simavr, not Renode/QEMU).
        ImGui::SameLine(0.0f, 6.0f * sc);
        if (fwGlassButton("##fw_sim_renode", "Renode", "run_study", /*accent=*/false, renodeW,
                          canGo))
            runEmulation(st, "fw_emulate_renode");
        ImGui::SameLine(0.0f, 6.0f * sc);
        if (fwGlassButton("##fw_sim_avr", "simavr", "run_study", /*accent=*/false, avrW, canGo))
            runEmulation(st, "fw_emulate_simavr");
    }

    if (s.simDone) {
        {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%s  \xc2\xb7  %.0f ms",
                          s.simPassed ? "PASSED" : "NOT PASSED", s.simMs);
            fwStatusPill(buf, s.simPassed ? palette.statusOk : palette.statusErr);
        }
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        for (const std::string& a : s.assertionLines) {
            const bool pass = a.rfind("PASS", 0) == 0;
            const float* tint = pass ? palette.statusOk : palette.statusErr;
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(tint[0], tint[1], tint[2], 0.9f));
            ImGui::TextUnformatted(a.c_str());
            ImGui::PopStyleColor();
        }
        if (!s.simErr.empty()) ImGui::TextDisabled("%s", s.simErr.c_str());
        if (st.fonts.mono) ImGui::PopFont();
    }

    // Live UART console (auto-scrolls to the newest output).
    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_uart_console", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (s.uartLog.empty()) {
        ImGui::TextDisabled(s.simPending ? "Emulator starting\xe2\x80\xa6"
                                         : "UART / semihosting output appears here.");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleDefault[0],
                                                    palette.consoleDefault[1],
                                                    palette.consoleDefault[2], 1));
        ImGui::TextUnformatted(s.uartLog.c_str());
        ImGui::PopStyleColor();
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4.0f) ImGui::SetScrollHereY(1.0f);
    }
    if (st.fonts.mono) ImGui::PopFont();
    ImGui::EndChild();
    ui::popGlassChildStyle();
    ImGui::EndChild();
}

static void drawTraceViewer(AppState& st) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_trace_sec", ImVec2(0, 0), ImGuiChildFlags_None);
    fwSectionHeader("activity", "GPIO trace");
    if (!s.tracePath.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDisabled[0],
                                                    palette.textDisabled[1],
                                                    palette.textDisabled[2], 1));
        ImGui::TextUnformatted(s.tracePath.c_str());
        ImGui::PopStyleColor();
    }

    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_trace_body", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (s.gpio.empty()) {
        ImGui::TextDisabled("No GPIO events. Run an emulation with gpio: assertions —\n"
                            "the aicad.firmware.trace/1 artifact renders here as waveforms.");
        ImGui::EndChild();
        ui::popGlassChildStyle();
        ImGui::EndChild();
        return;
    }

    // Group events per pin (stable order of first appearance) + x-domain from t_ms when the
    // events carry timestamps, else the seq order (contract §7: Renode hooks have t_ms null).
    std::vector<std::string> pins;
    bool anyT = false;
    double xMin = 1e300, xMax = -1e300;
    for (const GpioEvent& e : s.gpio) {
        if (std::find(pins.begin(), pins.end(), e.pin) == pins.end()) pins.push_back(e.pin);
        anyT = anyT || e.hasT;
    }
    auto xOf = [&](const GpioEvent& e) -> double {
        return (anyT && e.hasT) ? e.tMs : (double)e.seq;
    };
    for (const GpioEvent& e : s.gpio) {
        if (anyT && !e.hasT) continue;   // mixed traces: plot only the time-stamped events
        xMin = std::min(xMin, xOf(e));
        xMax = std::max(xMax, xOf(e));
    }
    if (xMax <= xMin) { xMin -= 1.0; xMax += 1.0; }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float labelW = 64.0f * sc;
    const float laneH  = 30.0f * sc;
    const float plotW  = std::max(60.0f, ImGui::GetContentRegionAvail().x - labelW - 8.0f * sc);
    const ImU32 waveCol = palette.toU32(palette.accent);
    const ImU32 glowCol = ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                                    palette.accent[2], 0.16f));
    const ImU32 gridCol = palette.toU32(palette.glassBorder);
    // Wave segments get a soft accent glow pass under the crisp line (oscilloscope look).
    auto waveSeg = [&](ImVec2 a, ImVec2 b) {
        dl->AddLine(a, b, glowCol, 4.5f * sc);
        dl->AddLine(a, b, waveCol, ui::metrics::stroke(2.0f));
    };

    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    int laneIdx = 0;
    for (const std::string& pin : pins) {
        const ImVec2 rowP = ImGui::GetCursorScreenPos();
        // Faint alternating lane wash so adjacent channels read as separate rows.
        if (laneIdx % 2 == 1)
            ui::coloredRowWash(dl, rowP, ImVec2(rowP.x + ImGui::GetContentRegionAvail().x,
                                                rowP.y + laneH),
                               4.0f, palette.text, 0.05f, false);
        ImGui::Text("%s", pin.c_str());
        const float x0 = rowP.x + labelW;
        const float yHi = rowP.y + 4.0f * sc;            // level 1
        const float yLo = rowP.y + laneH - 8.0f * sc;    // level 0
        dl->AddLine(ImVec2(x0, yLo), ImVec2(x0 + plotW, yLo), gridCol,
                    ui::metrics::stroke(1.0f));

        // Step waveform: horizontal hold at the current level, vertical edge at each event.
        float prevX = x0;
        int prevLevel = -1;
        for (const GpioEvent& e : s.gpio) {
            if (e.pin != pin) continue;
            if (anyT && !e.hasT) continue;
            const float ex = x0 + (float)((xOf(e) - xMin) / (xMax - xMin)) * plotW;
            const float eyNew = e.level ? yHi : yLo;
            if (prevLevel >= 0) {
                const float eyOld = prevLevel ? yHi : yLo;
                waveSeg(ImVec2(prevX, eyOld), ImVec2(ex, eyOld));
                if (prevLevel != e.level)
                    waveSeg(ImVec2(ex, eyOld), ImVec2(ex, eyNew));
            }
            prevX = ex;
            prevLevel = e.level;
        }
        if (prevLevel >= 0) {
            const float ey = prevLevel ? yHi : yLo;
            waveSeg(ImVec2(prevX, ey), ImVec2(x0 + plotW, ey));
        }
        ImGui::Dummy(ImVec2(0, laneH - ImGui::GetTextLineHeightWithSpacing()));
        ++laneIdx;
    }
    if (st.fonts.mono) ImGui::PopFont();
    if (anyT) ImGui::TextDisabled("t: %.1f .. %.1f ms", xMin, xMax);
    else      ImGui::TextDisabled("no timestamps — event order (seq %d .. %d)", (int)xMin, (int)xMax);
    ImGui::EndChild();
    ui::popGlassChildStyle();
    ImGui::EndChild();
}

static void drawEmulateTab(AppState& st) {
    const float availH = ImGui::GetContentRegionAvail().y;
    drawEmulationSection(st, availH * 0.55f);
    ImGui::Spacing();
    drawTraceViewer(st);
}

// ---- center pane: tab strip (Editor | Debug | Emulate) -----------------------------------

static void drawCenterTabs(AppState& st) {
    FwState& s = S();
    if (ImGui::BeginTabBar("##fw_center_tabs")) {
        auto tf = [&](int idx) {
            if (s.focusDebugTabOnce && idx == 1) {
                s.focusDebugTabOnce = false;
                return ImGuiTabItemFlags_SetSelected;
            }
            return ImGuiTabItemFlags_None;
        };
        if (ImGui::BeginTabItem("Editor", nullptr, tf(0))) { drawEditorTab(st); ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Debug", nullptr, tf(1)))  { drawDebugTab(st);  ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Emulate", nullptr, tf(2))) { drawEmulateTab(st); ImGui::EndTabItem(); }
        ImGui::EndTabBar();
    }
}

// ---- right pane: AI Copilot (reuses the docked Copilot's own chat state/send path) -------

static void drawCopilotPane(AppState& st, float w) {
    const float sc = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_copilot", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    fwSectionHeader("ai_panel", "AI Copilot");

    const bool busy = st.convRunning(st.activeConvId());
    const bool canRun = st.workerStarted && !busy;

    // Same one-frame-lag composer-height trick the docked Copilot / AI workspace use, so the
    // log fills exactly what the composer doesn't need.
    static float s_composerH = 0.0f;
    const float reserve = (s_composerH > 0.0f ? s_composerH
                                               : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                          10.0f * sc;
    const float logH = std::max(80.0f * sc, ImGui::GetContentRegionAvail().y - reserve);

    ImGui::BeginChild("##fw_copilot_log", ImVec2(0, logH), ImGuiChildFlags_None);
    if (st.chat.empty()) {
        ImGui::TextDisabled("Ask the copilot about this firmware project — it shares the "
                            "same conversation as the docked panel.");
    } else {
        ui::chatDrawLog(st);
        ui::chatLogStickToBottom("firmware.workspace");
    }
    ImGui::EndChild();

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    ImGui::BeginGroup();
    ui::chatDrawComposer(st, busy, canRun);
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + 6.0f * sc;

    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- bottom dock: Build Log | Serial Monitor | Flash Progress | GDB Console --------------

static void drawBuildLogDockTab(AppState& st) {
    FwState& s = S();
    if (s.buildDone) {
        if (s.buildOk) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "OK  \xc2\xb7  %.0f ms  \xc2\xb7  %zu warning(s)",
                          s.buildMs, s.warnings.size());
            fwStatusPill(buf, palette.statusOk);
        } else {
            fwStatusPill("FAILED", palette.statusErr);
        }
        ImGui::Spacing();
    }
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (!s.buildDone && !s.buildPending) {
        ImGui::TextDisabled("Build output appears here. Use the Build button in the target bar.");
    } else {
        for (const std::string& w : s.warnings) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleWarn[0],
                                                        palette.consoleWarn[1],
                                                        palette.consoleWarn[2], 1));
            ImGui::TextWrapped("%s", w.c_str());
            ImGui::PopStyleColor();
        }
        if (!s.buildStderr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleDim[0],
                                                        palette.consoleDim[1],
                                                        palette.consoleDim[2], 1));
            ImGui::TextWrapped("%s", s.buildStderr.c_str());
            ImGui::PopStyleColor();
        }
        if (!s.elfPath.empty()) ImGui::TextDisabled("ELF: %s", s.elfPath.c_str());
    }
    if (st.fonts.mono) ImGui::PopFont();
}

static void drawSerialDockTab(AppState& st) {
    FwState& s = S();
    const float sc = ui::uiScale;
    if (!s.serialOpen) {
        if (fwGlassButton("##fw_ser_open", s.serialPending ? "Opening\xe2\x80\xa6" : "Open port",
                          "activity", /*accent=*/true, 120.0f * sc,
                          st.workerStarted && !s.serialPending && s.portBuf[0] != '\0'))
            triggerSerialOpen(st);
        if (!s.portBuf[0] && ImGui::IsItemHovered())
            ImGui::SetTooltip("Set a Port in the target bar first");
    } else {
        fwStatusPill("OPEN", palette.statusOk);
        ImGui::SameLine(0.0f, 10.0f * sc);
        if (fwGlassButton("##fw_ser_close", "Close", nullptr, /*accent=*/false, 90.0f * sc,
                          st.workerStarted && !s.serialPending))
            triggerSerialClose(st);
    }
    if (!s.serialErr.empty()) {
        ImGui::SameLine(0.0f, 10.0f * sc);
        ImGui::TextDisabled("%s", s.serialErr.c_str());
    }

    ui::pushGlassChildStyle(6.0f);
    ImGui::BeginChild("##fw_ser_log", ImVec2(0, -ImGui::GetFrameHeightWithSpacing()),
                      ImGuiChildFlags_Border);
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (s.serialLog.empty()) {
        ImGui::TextDisabled("Serial data appears here once the port is open.");
    } else {
        ImGui::TextUnformatted(s.serialLog.c_str());
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4.0f) ImGui::SetScrollHereY(1.0f);
    }
    if (st.fonts.mono) ImGui::PopFont();
    ImGui::EndChild();
    ui::popGlassChildStyle();

    const float sendW = fwGlassButtonWidth("Send", nullptr);
    ImGui::SetNextItemWidth(-(sendW + 8.0f * sc));
    const bool enterSend = ImGui::InputTextWithHint("##fw_ser_send", "Send text + newline",
                                                    s.serialSendBuf, sizeof(s.serialSendBuf),
                                                    ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine(0.0f, 8.0f * sc);
    if ((enterSend || fwGlassButton("##fw_ser_sendbtn", "Send", nullptr, /*accent=*/true, sendW,
                                    s.serialOpen)) && s.serialOpen)
        triggerSerialSend(st);
}

static void drawFlashDockTab(AppState& st) {
    FwState& s = S();
    ImGui::TextDisabled("ELF: %s", s.elfPath.empty() ? "(build first)" : s.elfPath.c_str());
    ImGui::TextDisabled("Target: %s", s.chipBuf[0] ? s.chipBuf : "(none)");
    ImGui::Spacing();
    if (s.hwPending) {
        fwStatusPill("Working\xe2\x80\xa6", palette.statusWarn);
    } else if (!s.hwOut.empty()) {
        const bool ok = s.hwOut.rfind("OK", 0) == 0;
        fwStatusPill(ok ? "OK" : "FAILED", ok ? palette.statusOk : palette.statusErr);
        ImGui::Spacing();
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        ImGui::TextWrapped("%s", s.hwOut.c_str());
        if (st.fonts.mono) ImGui::PopFont();
    } else {
        ImGui::TextDisabled("No flash attempt yet. Use the Flash button in the target bar.");
    }
}

static void drawGdbConsoleDockTab(AppState& st) {
    FwState& s = S();
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (s.gdbConsole.empty()) {
        ImGui::TextDisabled("GDB command output appears here (Reset/Halt/Step/Breakpoints/"
                            "Memory, from the Debug tab).");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleDefault[0],
                                                    palette.consoleDefault[1],
                                                    palette.consoleDefault[2], 1));
        ImGui::TextUnformatted(s.gdbConsole.c_str());
        ImGui::PopStyleColor();
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4.0f) ImGui::SetScrollHereY(1.0f);
    }
    if (st.fonts.mono) ImGui::PopFont();
}

// Collapsible via ui::PanelCollapseAnim (same vertical roll-up recipe as the FeatureManager
// column) — `h` is the ALREADY-ANIMATED height for this frame (computed by the caller so the
// top row above can shrink/grow to match in the same frame, no one-frame lag).
static void drawBottomDock(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 6 * sc));
    ImGui::BeginChild("##fw_dock", ImVec2(0, h), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    ImGui::BeginGroup();
    if (ImGui::SmallButton(s.dockCollapsed ? "v" : "^")) s.dockCollapsed = !s.dockCollapsed;
    ImGui::SameLine();
    ImGui::TextDisabled("Dock");
    ImGui::EndGroup();
    s.dockAnim.measureHeader();

    if (s.dockAnim.bodyVisible() && ImGui::BeginTabBar("##fw_dock_tabs")) {
        auto tf = [&](int idx) {
            if (s.focusMonitorDockOnce && idx == 1) {
                s.focusMonitorDockOnce = false;
                return ImGuiTabItemFlags_SetSelected;
            }
            return ImGuiTabItemFlags_None;
        };
        if (ImGui::BeginTabItem("Build Log", nullptr, tf(0))) {
            drawBuildLogDockTab(st);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Serial Monitor", nullptr, tf(1))) {
            drawSerialDockTab(st);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Flash Progress", nullptr, tf(2))) {
            drawFlashDockTab(st);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("GDB Console", nullptr, tf(3))) {
            drawGdbConsoleDockTab(st);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }

    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- CHUDWARE_SHOT=firmware fixture ---------------------------------------------------------

void seedFirmwareShotFixture() {
    FwState& s = S();
    FwProject p;
    p.id = "fw_canned"; p.name = "blinky (canned)"; p.mcu = "cortex-m4"; p.toolchain = "cmake";
    p.srcs = {"src/main.c", "include/pinmap.h", "include/chudware_trace.h"};
    p.rootDir = "/tmp/chudware-firmware/fw_canned";
    s.projects.push_back(std::move(p));
    s.selected = 0;
    s.listedOnce = true;

    s.buildDone = true; s.buildOk = true; s.buildMs = 1834.0;
    s.elfPath = "/tmp/chudware-firmware/fw_canned/build/blinky.elf";
    s.warnings = {
        "src/main.c:42:9: warning: unused variable 'tick' [-Wunused-variable]",
        "src/main.c:57:5: warning: implicit declaration of function 'delay_ms'",
    };

    s.simDone = true; s.simPassed = true; s.simMs = 2210.0;
    s.uartLog = "boot: blinky v0.1\nhello from PA5 blink loop\nCHUDWARE_GPIO_EVT PA5=1 t=100ms\n"
                "CHUDWARE_GPIO_EVT PA5=0 t=200ms\nloop tick 1\nloop tick 2\n";
    s.assertionLines = {"PASS  uart:hello", "PASS  gpio:PA5=1@>50ms"};
    s.tracePath = "/tmp/chudware-firmware/fw_canned/build/blinky.elf.trace.json";
    for (int i = 0; i < 8; ++i) {
        GpioEvent e;
        e.pin = "PA5"; e.level = i % 2 == 0 ? 1 : 0;
        e.tMs = 100.0 * (i + 1); e.hasT = true; e.seq = i;
        s.gpio.push_back(e);
        if (i % 2 == 0) {
            GpioEvent l;
            l.pin = "PD12"; l.level = (i / 2) % 2;
            l.tMs = 100.0 * (i + 1) + 25.0; l.hasT = true; l.seq = 100 + i;
            s.gpio.push_back(l);
        }
    }
}

// ---- entry point --------------------------------------------------------------------------

static void drawFirmwareWorkspaceBody(AppState& st) {
    loadStatus(st);
    loadProjects(st);
    pollSerial(st);
    ui::chatWorkspaceSync(st);

    FwState& s = S();
    const float sc = ui::uiScale;
    const float gutter = 10.0f * sc;

    drawTargetBar(st);
    ImGui::Spacing();

    const float railW = 250.0f * sc;
    const float copilotW = 320.0f * sc;
    const float dockFullH = 220.0f * sc;
    // Tick the dock's collapse animation ONCE per frame up front so the top row below can
    // reclaim/cede exactly the space the dock animates through — no one-frame lag between
    // the two (ui::PanelCollapseAnim, the same recipe the FeatureManager column uses).
    const float dockH = s.dockAnim.tick(s.dockCollapsed, dockFullH);

    const float bodyH = ImGui::GetContentRegionAvail().y;
    const float topH = std::max(160.0f * sc, bodyH - dockH - gutter);

    ImGui::BeginChild("##fw_top_row", ImVec2(0, topH), ImGuiChildFlags_None);
    drawProjectRail(st, railW);
    ImGui::SameLine(0.0f, gutter);

    const float midW =
        std::max(220.0f * sc, ImGui::GetContentRegionAvail().x - copilotW - gutter);
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14 * sc, 10 * sc));
    ImGui::BeginChild("##fw_center", ImVec2(midW, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    drawCenterTabs(st);
    ImGui::EndChild();
    ui::popGlassChildStyle();

    ImGui::SameLine(0.0f, gutter);
    drawCopilotPane(st, copilotW);
    ImGui::EndChild();   // ##fw_top_row

    ImGui::Spacing();
    drawBottomDock(st, dockH);
}

}  // namespace firmware

void drawFirmwareWorkspace(AppState& st) { firmware::drawFirmwareWorkspaceBody(st); }

bool firmwareTabVisible(AppState& st) {
    // Always offered once active (never trap the user out of the workspace they're in).
    if (st.activeWorkspace == AppState::Workspace::Firmware) return true;
    // ECAD in play → firmware is the natural next step (T-FIN24 disclosure rule).
    if (st.activeWorkspace == AppState::Workspace::Electronics) return true;
#ifdef __EMSCRIPTEN__
    // No project disk on web: never probe for tab disclosure.
    (void)st;
    return false;
#else
    firmware::FwState& s = firmware::S();
    // Kick the throttled async listing so the rail is warm when the user enters.
    firmware::loadProjects(st);
    // Disclose on the worker listing OR the local disk probe — the disk probe covers
    // the boot window where the Python-worker-only fw_projects RPC can't answer yet.
    return (s.listedOnce && !s.projects.empty()) || firmware::firmwareProjectsExistOnDisk();
#endif
}

}  // namespace ui
