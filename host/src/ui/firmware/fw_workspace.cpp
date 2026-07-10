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
// GL-thread rules honored throughout: every engine call is ASYNC (st.rpc.call + callbacks
// run in the drain), listing is throttled, and nothing grabs focus per-frame.
#include "fw_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
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

// ---- async loaders (throttled; NEVER sync in the draw path) -----------------------------

static void loadStatus(AppState& st, bool force = false) {
    FwState& s = S();
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

// ---- left rail: projects + sources -------------------------------------------------------

static void drawProjectRail(AppState& st, float w) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_rail", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    ImGui::TextUnformatted("Projects");
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 48.0f * sc);
    if (ImGui::SmallButton(s.listPending ? "..." : "Refresh")) loadProjects(st, /*force=*/true);
    ImGui::Separator();

    // New project (create only when the toolchain family can at least be selected —
    // creation itself is always allowed; the BUILD is what fw_status gates).
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##fw_new_name", "New project name", s.newNameBuf,
                             sizeof(s.newNameBuf));
    ImGui::SetNextItemWidth(-1);
    ImGui::Combo("##fw_new_mcu", &s.newMcuIdx, kMcuFamilies,
                 (int)(sizeof(kMcuFamilies) / sizeof(kMcuFamilies[0])));
    const bool canCreate = st.workerStarted && !s.createPending;
    if (!canCreate) ImGui::BeginDisabled();
    if (ImGui::Button(s.createPending ? "Creating\xe2\x80\xa6" : "+ New Project",
                      ImVec2(-1, 0))) {
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
    if (!canCreate) ImGui::EndDisabled();
    if (!st.workerStarted && ImGui::IsItemHovered()) ImGui::SetTooltip("Worker not running");
    if (!s.createErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1));
        ImGui::TextWrapped("%s", s.createErr.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Separator();

    if (!st.workerStarted) {
        ImGui::TextDisabled("Worker not running.");
    } else if (s.projects.empty()) {
        if (!s.listErr.empty()) ImGui::TextWrapped("No projects. %s", s.listErr.c_str());
        else ImGui::TextDisabled("No firmware projects yet.\nUse + New Project above.");
    } else {
        for (int i = 0; i < (int)s.projects.size(); ++i) {
            const FwProject& p = s.projects[i];
            ImGui::PushID(i);
            if (ImGui::Selectable(p.name.c_str(), s.selected == i)) {
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
            ImGui::SameLine();
            ImGui::TextDisabled("%s", p.mcu.c_str());
            ImGui::PopID();
        }
    }

    // Sources of the selected project.
    if (s.selected >= 0 && s.selected < (int)s.projects.size()) {
        const FwProject& p = s.projects[s.selected];
        ImGui::Spacing();
        ImGui::Separator();
        ImGui::TextUnformatted("Sources");
        if (p.srcs.empty()) ImGui::TextDisabled("(none registered)");
        for (const std::string& src : p.srcs) ImGui::BulletText("%s", src.c_str());
        if (!p.rootDir.empty()) {
            ImGui::Spacing();
            ImGui::TextDisabled("Root");
            ImGui::TextWrapped("%s", p.rootDir.c_str());
        }
    }
    ImGui::EndChild();
}

// ---- center: build console + emulation + trace viewer ------------------------------------

static void drawBuildSection(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_build_sec", ImVec2(0, h), ImGuiChildFlags_None);
    ImGui::TextUnformatted("Build");
    ImGui::SameLine();

    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (s.statusLoaded && !s.canBuild) {
        // Honest gating: no dead Build button — the doctor's guidance instead.
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                     palette.statusWarn[2], 1));
        ImGui::TextWrapped("Toolchain missing — install cmake + arm-none-eabi-gcc "
                           "(see Backend panel for fw_status guidance).");
        ImGui::PopStyleColor();
    } else {
        const bool canGo = haveProject && st.workerStarted && !s.buildPending;
        if (!canGo) ImGui::BeginDisabled();
        if (ImGui::Button(s.buildPending ? "Building\xe2\x80\xa6" : "Build",
                          ImVec2(110 * sc, 0))) {
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
        if (!canGo) ImGui::EndDisabled();
        if (!haveProject && ImGui::IsItemHovered()) ImGui::SetTooltip("Select a project first");
    }
    if (s.buildDone) {
        ImGui::SameLine();
        if (s.buildOk) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusOk[0], palette.statusOk[1],
                                                        palette.statusOk[2], 1));
            ImGui::Text("OK  %.0f ms  %zu warning(s)", s.buildMs, s.warnings.size());
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusErr[0], palette.statusErr[1],
                                                        palette.statusErr[2], 1));
            ImGui::TextUnformatted("FAILED");
        }
        ImGui::PopStyleColor();
    }

    // Parsed-warnings console.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * sc, 6 * sc));
    ImGui::BeginChild("##fw_build_console", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (!s.buildDone && !s.buildPending) {
        ImGui::TextDisabled("Build output appears here.");
    } else {
        for (const std::string& w : s.warnings) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                        palette.statusWarn[2], 1));
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
    ImGui::EndChild();
    ImGui::EndChild();
}

static void drawEmulationSection(AppState& st, float h) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_sim_sec", ImVec2(0, h), ImGuiChildFlags_None);
    ImGui::TextUnformatted("Emulation");
    const bool haveProject = s.selected >= 0 && s.selected < (int)s.projects.size();
    if (haveProject) {
        ImGui::SameLine();
        ImGui::TextDisabled("platform: %s (Renode preferred, QEMU fallback)",
                            s.projects[s.selected].mcu.c_str());
    }

    if (s.statusLoaded && !s.canEmulate) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                     palette.statusWarn[2], 1));
        ImGui::TextWrapped("Emulation unavailable: install Renode (preferred) or "
                           "qemu-system-arm.");
        ImGui::PopStyleColor();
    } else {
        ImGui::SetNextItemWidth(-(130.0f * sc));
        ImGui::InputTextWithHint("##fw_asserts",
                                 "Scenario assertions, ';'-separated  e.g. uart:hello; gpio:PA5=1",
                                 s.assertBuf, sizeof(s.assertBuf));
        ImGui::SameLine();
        const bool canGo = haveProject && st.workerStarted && !s.simPending;
        if (!canGo) ImGui::BeginDisabled();
        if (ImGui::Button(s.simPending ? "Running\xe2\x80\xa6" : "Run emulation",
                          ImVec2(-1, 0))) {
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
        if (!canGo) ImGui::EndDisabled();
        if (!haveProject && ImGui::IsItemHovered()) ImGui::SetTooltip("Select a project first");
    }

    if (s.simDone) {
        if (s.simPassed) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusOk[0], palette.statusOk[1],
                                                        palette.statusOk[2], 1));
            ImGui::Text("PASSED  %.0f ms", s.simMs);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusErr[0], palette.statusErr[1],
                                                        palette.statusErr[2], 1));
            ImGui::Text("NOT PASSED  %.0f ms", s.simMs);
        }
        ImGui::PopStyleColor();
        for (const std::string& a : s.assertionLines) ImGui::TextDisabled("%s", a.c_str());
        if (!s.simErr.empty()) ImGui::TextDisabled("%s", s.simErr.c_str());
    }

    // Live UART console (auto-scrolls to the newest output).
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * sc, 6 * sc));
    ImGui::BeginChild("##fw_uart_console", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (s.uartLog.empty()) {
        ImGui::TextDisabled(s.simPending ? "Emulator starting\xe2\x80\xa6"
                                         : "UART / semihosting output appears here.");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.consoleDim[0], palette.consoleDim[1],
                                                    palette.consoleDim[2], 1));
        ImGui::TextUnformatted(s.uartLog.c_str());
        ImGui::PopStyleColor();
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4.0f) ImGui::SetScrollHereY(1.0f);
    }
    ImGui::EndChild();
    ImGui::EndChild();
}

static void drawTraceViewer(AppState& st) {
    (void)st;
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::BeginChild("##fw_trace_sec", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::TextUnformatted("GPIO trace");
    if (!s.tracePath.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("%s", s.tracePath.c_str());
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * sc, 6 * sc));
    ImGui::BeginChild("##fw_trace_body", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();
    if (s.gpio.empty()) {
        ImGui::TextDisabled("No GPIO events. Run an emulation with gpio: assertions —\n"
                            "the aicad.firmware.trace/1 artifact renders here as waveforms.");
        ImGui::EndChild();
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
    const ImU32 gridCol = palette.toU32(palette.glassBorder);

    for (const std::string& pin : pins) {
        const ImVec2 rowP = ImGui::GetCursorScreenPos();
        ImGui::Text("%s", pin.c_str());
        const float x0 = rowP.x + labelW;
        const float yHi = rowP.y + 4.0f * sc;            // level 1
        const float yLo = rowP.y + laneH - 8.0f * sc;    // level 0
        dl->AddLine(ImVec2(x0, yLo), ImVec2(x0 + plotW, yLo), gridCol, 1.0f);

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
                dl->AddLine(ImVec2(prevX, eyOld), ImVec2(ex, eyOld), waveCol, 2.0f);
                if (prevLevel != e.level)
                    dl->AddLine(ImVec2(ex, eyOld), ImVec2(ex, eyNew), waveCol, 2.0f);
            }
            prevX = ex;
            prevLevel = e.level;
        }
        if (prevLevel >= 0) {
            const float ey = prevLevel ? yHi : yLo;
            dl->AddLine(ImVec2(prevX, ey), ImVec2(x0 + plotW, ey), waveCol, 2.0f);
        }
        ImGui::Dummy(ImVec2(0, laneH - ImGui::GetTextLineHeightWithSpacing()));
    }
    if (anyT) ImGui::TextDisabled("t: %.1f .. %.1f ms", xMin, xMax);
    else      ImGui::TextDisabled("no timestamps — event order (seq %d .. %d)", (int)xMin, (int)xMax);
    ImGui::EndChild();
    ImGui::EndChild();
}

// ---- right rail: backend doctor + gated flash/debug ---------------------------------------

static void drawBackendRail(AppState& st, float w) {
    FwState& s = S();
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##fw_backend", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    ImGui::TextUnformatted("Backend");
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 48.0f * sc);
    if (ImGui::SmallButton(s.statusPending ? "..." : "Recheck")) loadStatus(st, /*force=*/true);
    ImGui::Separator();

    if (!s.statusLoaded) {
        ImGui::TextDisabled(s.statusErr.empty() ? "Checking fw_status\xe2\x80\xa6"
                                                : s.statusErr.c_str());
    } else {
        for (const FwState::ToolRow& t : s.tools) {
            if (t.found) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusOk[0],
                                                            palette.statusOk[1],
                                                            palette.statusOk[2], 1));
                ImGui::TextUnformatted("\xe2\x97\x8f");
                ImGui::PopStyleColor();
            } else {
                ImGui::TextDisabled("\xe2\x97\x8b");
            }
            ImGui::SameLine();
            ImGui::TextUnformatted(t.name.c_str());
            if (!t.info.empty() && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", t.info.c_str());
        }
        for (const std::string& b : s.banners) {
            ImGui::Spacing();
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                        palette.statusWarn[2], 1));
            ImGui::TextWrapped("%s", b.c_str());
            ImGui::PopStyleColor();
        }
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::TextUnformatted("Flash & Debug");
    // Honest gating (task DoD): the buttons only EXIST when fw_status says the backend
    // (probe-rs / esptool) is present; otherwise the doctor's guidance shows instead.
    if (!s.statusLoaded) {
        ImGui::TextDisabled("Waiting for fw_status\xe2\x80\xa6");
    } else if (!s.canFlash) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                    palette.statusWarn[2], 1));
        ImGui::TextWrapped("Flashing/debug unavailable: `cargo install probe-rs-tools` "
                           "(Cortex-M) or `pip install esptool` (ESP32).");
        ImGui::PopStyleColor();
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
        if (!canHw || !haveElf) ImGui::BeginDisabled();
        if (ImGui::Button(s.hwPending ? "Working\xe2\x80\xa6" : "Flash ELF", ImVec2(-1, 0)))
            hwCall("fw_flash", {{"elf_path", s.elfPath}, {"chip", std::string(s.chipBuf)}});
        if (!canHw || !haveElf) ImGui::EndDisabled();
        if (!haveElf && ImGui::IsItemHovered())
            ImGui::SetTooltip("Build first — the last successful ELF is flashed");
        if (!canHw) ImGui::BeginDisabled();
        if (ImGui::Button("Debug: reset + run", ImVec2(-1, 0)))
            hwCall("fw_debug_start", {{"chip", std::string(s.chipBuf)}});
        if (ImGui::Button("Debug: halt", ImVec2(-1, 0)))
            hwCall("fw_debug_halt", {{"chip", std::string(s.chipBuf)}});
        if (!canHw) ImGui::EndDisabled();
        if (!s.hwOut.empty()) ImGui::TextWrapped("%s", s.hwOut.c_str());
    }
    ImGui::EndChild();
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
    const float gutter = 8.0f * sc;

    drawProjectRail(st, railW);
    ImGui::SameLine(0.0f, gutter);

    const float midW =
        std::max(220.0f * sc, ImGui::GetContentRegionAvail().x - backW - gutter);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 8 * sc));
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
    firmware::FwState& s = firmware::S();
    // Kick the throttled async listing so existing projects disclose the tab at startup.
    firmware::loadProjects(st);
    return s.listedOnce && !s.projects.empty();
}

}  // namespace ui
