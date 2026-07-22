// Firmware workspace (T-FIN5) — see fw_workspace.h.
//
// Layout (full central band, same host window pattern as PLM/RevEng):
//   left  : project rail (fw_projects, ASYNC) + New Project + selected project's sources
//   center: Build section (fw_build → parsed-warnings console) over the Emulation section
//           (platform + scenario assertions → fw_sim_run → live UART console) over the
//           GPIO trace viewer (aicad.firmware.trace/1 waveforms, contract §7)
//   right : backend doctor (fw_status, T-FIN4) + Flash/Debug — buttons exist ONLY when the
//           doctor says the backend exists; otherwise the doctor's guidance banner shows.
//
// Styling: the workspace speaks the shared "liquid glass" language (ui_theme.h) instead of
// stock ImGui chrome — glass children, accent-gradient section headers, tinted status
// pills, glass action buttons and glow selection, all scaled by ui::uiScale and painted
// from palette roles only (gui_lint).
//
// GL-thread rules honored throughout: every engine call is ASYNC (st.rpc.call + callbacks
// run in the drain), listing is throttled, and nothing grabs focus per-frame.
#include "fw_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui/icons.h"
#include "ui/metrics.h"
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

    // ---- emulation (fw_sim_run) ----
    bool simPending = false, simDone = false, simPassed = false;
    std::string uartLog, simErr, tracePath;
    double simMs = 0.0;
    std::vector<std::string> assertionLines;   // "PASS uart:hello" style summaries
    std::vector<GpioEvent> gpio;
    char assertBuf[256] = "";

    // ---- hardware rail (fw_flash / fw_debug_*) ----
    char chipBuf[64] = "STM32F407VGTx";
    bool hwPending = false;
    std::string hwOut;
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

// Apply a SimResult payload (contract §4/§7.1) to the state.
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
    // Mirror engine.firmware.project.firmware_root(): AI_CAD_FIRMWARE_DIR override,
    // else %APPDATA%/AI-CAD/firmware, else <tmp>/AI-CAD/firmware (Python
    // tempfile.gettempdir() honors TMPDIR/TEMP/TMP, then /tmp).
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path root;
    if (const char* ov = std::getenv("AI_CAD_FIRMWARE_DIR"); ov && *ov) {
        root = ov;
    } else if (const char* ad = std::getenv("APPDATA"); ad && *ad) {
        root = fs::path(ad) / "AI-CAD" / "firmware";
    } else {
        const char* tmp = std::getenv("TMPDIR");
        if (!tmp || !*tmp) tmp = std::getenv("TEMP");
        if (!tmp || !*tmp) tmp = std::getenv("TMP");
        if (!tmp || !*tmp) tmp = "/tmp";
        root = fs::path(tmp) / "AI-CAD" / "firmware";
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

// ---- left rail: projects + sources -------------------------------------------------------

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
    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- center: build console + emulation + trace viewer ------------------------------------

static void drawBuildSection(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_build_sec", ImVec2(0, h), ImGuiChildFlags_None);
    fwSectionHeader("eng_firm", "Build");

    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (s.statusLoaded && !s.canBuild) {
        // Honest gating: no dead Build button — the doctor's guidance instead.
        fwBanner("Toolchain missing — install cmake + arm-none-eabi-gcc "
                 "(see Backend panel for fw_status guidance).", palette.statusWarn);
    } else {
        const bool canGo = haveProject && st.workerStarted && !s.buildPending;
        if (fwGlassButton("##fw_build_btn",
                          s.buildPending ? "Building\xe2\x80\xa6" : "Build",
                          "eng_firm", /*accent=*/true, 0.0f, canGo)) {
            s.buildPending = true;
            s.buildDone = false;
            s.warnings.clear(); s.buildStderr.clear(); s.elfPath.clear();
            nlohmann::json p{{"project_id", s.projects[s.selected].id}};
            st.rpc.call("fw_build", p, [](const core::RpcResult& r) {
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
        if (!canGo && !haveProject && ImGui::IsItemHovered())
            ImGui::SetTooltip("Select a project first");
        if (s.buildDone) {
            ImGui::SameLine(0.0f, 10.0f * sc);
            if (s.buildOk) {
                char buf[96];
                std::snprintf(buf, sizeof(buf), "OK  \xc2\xb7  %.0f ms  \xc2\xb7  %zu warning(s)",
                              s.buildMs, s.warnings.size());
                fwStatusPill(buf, palette.statusOk);
            } else {
                fwStatusPill("FAILED", palette.statusErr);
            }
        }
    }

    // Parsed-warnings console (monospace, glass).
    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_build_console", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
    if (!s.buildDone && !s.buildPending) {
        ImGui::TextDisabled("Build output appears here.");
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
    ImGui::EndChild();
    ui::popGlassChildStyle();
    ImGui::EndChild();
}

static void drawEmulationSection(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_sim_sec", ImVec2(0, h), ImGuiChildFlags_None);
    fwSectionHeader("run_study", "Emulation");
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (haveProject) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], 1));
        ImGui::Text("platform: %s  (Renode preferred, QEMU fallback)",
                    s.projects[s.selected].mcu.c_str());
        ImGui::PopStyleColor();
    }

    if (s.statusLoaded && !s.canEmulate) {
        fwBanner("Emulation unavailable: install Renode (preferred) or qemu-system-arm.",
                 palette.statusWarn);
    } else {
        const char* runLabel = s.simPending ? "Running\xe2\x80\xa6" : "Run emulation";
        const float runW = fwGlassButtonWidth(runLabel, "run_study");
        ImGui::SetNextItemWidth(-(runW + 8.0f * sc));
        ImGui::InputTextWithHint("##fw_asserts",
                                 "Scenario assertions, ';'-separated  e.g. uart:hello; gpio:PA5=1",
                                 s.assertBuf, sizeof(s.assertBuf));
        ImGui::SameLine(0.0f, 8.0f * sc);
        const bool canGo = haveProject && st.workerStarted && !s.simPending;
        if (fwGlassButton("##fw_sim_btn", runLabel, "run_study", /*accent=*/true, runW,
                          canGo)) {
            s.simPending = true;
            s.simDone = false;
            s.uartLog.clear(); s.assertionLines.clear(); s.gpio.clear();
            s.tracePath.clear(); s.simErr.clear();
            nlohmann::json p{{"project_id", s.projects[s.selected].id}};
            // Split the ';'-separated scenario text into the assertion list.
            nlohmann::json asserts = nlohmann::json::array();
            std::string buf(s.assertBuf);
            size_t start = 0;
            while (start <= buf.size()) {
                size_t end = buf.find(';', start);
                std::string tok = buf.substr(
                    start, end == std::string::npos ? std::string::npos : end - start);
                const size_t a = tok.find_first_not_of(" \t");
                const size_t b = tok.find_last_not_of(" \t");
                if (a != std::string::npos) asserts.push_back(tok.substr(a, b - a + 1));
                if (end == std::string::npos) break;
                start = end + 1;
            }
            if (!asserts.empty()) p["assertions"] = asserts;
            st.rpc.call("fw_sim_run", p, [](const core::RpcResult& r) {
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
        if (!canGo && !haveProject && ImGui::IsItemHovered())
            ImGui::SetTooltip("Select a project first");
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

// ---- right rail: backend doctor + gated flash/debug ---------------------------------------

static void drawBackendRail(AppState& st, float w) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 10 * sc));
    ImGui::BeginChild("##fw_backend", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

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
                // Soft glow halo + solid core (the "tool present" look).
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

    ImGui::Spacing();
    fwSectionHeader("issue_bug", "Flash & Debug");
    // Honest gating (task DoD): the buttons only EXIST when fw_status says the backend
    // (probe-rs / esptool) is present; otherwise the doctor's guidance shows instead.
    if (!s.statusLoaded) {
        ImGui::TextDisabled("Waiting for fw_status\xe2\x80\xa6");
    } else if (!s.canFlash) {
        fwBanner("Flashing/debug unavailable: `cargo install probe-rs-tools` (Cortex-M) "
                 "or `pip install esptool` (ESP32).", palette.statusWarn);
    } else {
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##fw_chip", "Chip (e.g. STM32F407VGTx)", s.chipBuf,
                                 sizeof(s.chipBuf));
        const bool haveElf = !s.elfPath.empty();
        const bool canHw = st.workerStarted && !s.hwPending && s.chipBuf[0] != '\0';
        auto hwCall = [&st, &s](const char* method, nlohmann::json p) {
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
        };
        const float fullW = ImGui::GetContentRegionAvail().x;
        if (fwGlassButton("##fw_flash", s.hwPending ? "Working\xe2\x80\xa6" : "Flash ELF",
                          "eng_firm", /*accent=*/true, fullW, canHw && haveElf))
            hwCall("fw_flash", {{"elf_path", s.elfPath}, {"chip", std::string(s.chipBuf)}});
        if (!haveElf && ImGui::IsItemHovered())
            ImGui::SetTooltip("Build first — the last successful ELF is flashed");
        if (fwGlassButton("##fw_dbg_run", "Debug: reset + run", "run_study",
                          /*accent=*/false, fullW, canHw))
            hwCall("fw_debug_start", {{"chip", std::string(s.chipBuf)}});
        if (fwGlassButton("##fw_dbg_halt", "Debug: halt", nullptr,
                          /*accent=*/false, fullW, canHw))
            hwCall("fw_debug_halt", {{"chip", std::string(s.chipBuf)}});
        if (!s.hwOut.empty()) {
            ImGui::Spacing();
            const bool ok = s.hwOut.rfind("OK", 0) == 0;
            fwBanner(s.hwOut.c_str(), ok ? palette.statusOk : palette.statusErr);
        }
    }
    ImGui::EndChild();
    ui::popGlassChildStyle();
}

// ---- AI_CAD_SHOT=firmware fixture ---------------------------------------------------------

void seedFirmwareShotFixture() {
    FwState& s = S();
    FwProject p;
    p.id = "fw_canned"; p.name = "blinky (canned)"; p.mcu = "cortex-m4"; p.toolchain = "cmake";
    p.srcs = {"src/main.c", "include/pinmap.h", "include/aicad_trace.h"};
    p.rootDir = "/tmp/aicad-firmware/fw_canned";
    s.projects.push_back(std::move(p));
    s.selected = 0;
    s.listedOnce = true;

    s.buildDone = true; s.buildOk = true; s.buildMs = 1834.0;
    s.elfPath = "/tmp/aicad-firmware/fw_canned/build/blinky.elf";
    s.warnings = {
        "src/main.c:42:9: warning: unused variable 'tick' [-Wunused-variable]",
        "src/main.c:57:5: warning: implicit declaration of function 'delay_ms'",
    };

    s.simDone = true; s.simPassed = true; s.simMs = 2210.0;
    s.uartLog = "boot: blinky v0.1\nhello from PA5 blink loop\nAICAD_GPIO_EVT PA5=1 t=100ms\n"
                "AICAD_GPIO_EVT PA5=0 t=200ms\nloop tick 1\nloop tick 2\n";
    s.assertionLines = {"PASS  uart:hello", "PASS  gpio:PA5=1@>50ms"};
    s.tracePath = "/tmp/aicad-firmware/fw_canned/build/blinky.elf.trace.json";
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

    const float sc = ui::uiScale;
    const float railW = 250.0f * sc;
    const float backW = 260.0f * sc;
    const float gutter = 10.0f * sc;

    drawProjectRail(st, railW);
    ImGui::SameLine(0.0f, gutter);

    const float midW =
        std::max(220.0f * sc, ImGui::GetContentRegionAvail().x - backW - gutter);
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14 * sc, 10 * sc));
    ImGui::BeginChild("##fw_center", ImVec2(midW, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    {
        const float availH = ImGui::GetContentRegionAvail().y;
        drawBuildSection(st, availH * 0.32f);
        ImGui::Spacing();
        drawEmulationSection(st, availH * 0.34f);
        ImGui::Spacing();
        drawTraceViewer(st);
    }
    ImGui::EndChild();
    ui::popGlassChildStyle();

    ImGui::SameLine(0.0f, gutter);
    drawBackendRail(st, backW);
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
