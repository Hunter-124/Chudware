// Shared Model Leaderboard UI (aicad.leaderboard/1) — Settings compact table + AI workspace pane.
// Header-only so settings_modal.cpp and ai/ai_workspace.cpp share one parser + draw helpers.
// Additive extension (2026-07-14): divisions, calibration, new axes, instrumentation_summary.
// All new keys are optional; missing keys keep the previous rendering for old-format files.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "metrics.h"

namespace ui {
namespace leaderboard {

// Optional float: present=false means JSON null / absent ("— untested", NEVER scored as 0).
struct OptFloat {
    float value = 0.f;
    bool present = false;
};

struct ChallengeRow {
    std::string challenge;
    std::string tier;
    std::string division;  // optional: "local" | "open" | empty
    float passRate = 0.f;
    int seeds = 0;
};

// Per-division slice: pass rate + Discipline Index (DI).
struct DivisionStats {
    bool present = false;
    float passRate = 0.f;
    int cellsTotal = 0;
    int cellsPassed = 0;
    OptFloat disciplineIndex;  // null = untested
};

struct CalibrationStats {
    bool present = false;
    OptFloat localPassRate;
    OptFloat openPassRate;
    OptFloat bridgeDelta;
};

struct InstrumentationSummary {
    bool present = false;
    std::string phaseMortalityTop;   // e.g. "specify 60%"
    std::string topMisuseFingerprint;
};

struct ModelRow {
    std::string id;
    std::string label;
    std::string provider;
    std::string kind;  // paid | local
    float passRate = 0.f;
    int cellsTotal = 0;
    int cellsPassed = 0;
    // Legacy axes (kept as floats; null → 0 for backward-compat bar display).
    float toolValidity = 0.f;
    float visionFix = 0.f;
    float featureId = 0.f;
    float vcHygiene = 0.f;
    // New axes: null = untested, NEVER rendered as 0.
    OptFloat disciplineIndex;
    OptFloat verifyEfficacy;
    OptFloat checkpointUtil;
    // Also track null for legacy vision_fix so we can show untested if wanted later.
    bool visionFixPresent = false;
    float costUsd = -1.f;   // -1 = null/local
    float latencyS = 0.f;
    int tokensMed = 0;
    std::vector<std::pair<std::string, int>> failures;
    std::vector<ChallengeRow> perChallenge;
    // Additive extension (optional):
    DivisionStats divLocal;
    DivisionStats divOpen;
    CalibrationStats calibration;
    InstrumentationSummary instrumentation;
};

struct Board {
    std::string schema;
    std::string generated;
    std::string taskSuite;
    std::vector<ModelRow> models;
    bool loaded = false;
    bool mock = false;
    std::string path;
    std::string error;  // non-empty only for parse issues (missing file is empty-state, not error)
};

// Resolve the committed leaderboard.json relative to repo/app root.
inline std::string resolvePath() {
    namespace fs = std::filesystem;
    auto tryPath = [](const fs::path& p) -> std::string {
        std::error_code ec;
        if (fs::is_regular_file(p, ec)) return p.string();
        return {};
    };
    if (const char* repo = std::getenv("AI_CAD_REPO")) {
        std::string hit = tryPath(fs::path(repo) / "benchmarks" / "leaderboard" / "leaderboard.json");
        if (!hit.empty()) return hit;
    }
    // Walk up from CWD looking for benchmarks/leaderboard/leaderboard.json or a repo root.
    fs::path d = fs::current_path();
    for (int i = 0; i < 8; ++i) {
        std::string hit = tryPath(d / "benchmarks" / "leaderboard" / "leaderboard.json");
        if (!hit.empty()) return hit;
        if (!d.has_parent_path() || d.parent_path() == d) break;
        d = d.parent_path();
    }
    // Last resort: relative path (dev runs from repo root).
    return "benchmarks/leaderboard/leaderboard.json";
}

inline float jnumf(const nlohmann::json& o, const char* k, float def = 0.f) {
    auto it = o.find(k);
    if (it == o.end() || it->is_null()) return def;
    if (it->is_number()) return (float)it->get<double>();
    return def;
}

// Parse an optional numeric: null/absent → present=false (untested).
inline OptFloat joptf(const nlohmann::json& o, const char* k) {
    OptFloat out;
    auto it = o.find(k);
    if (it == o.end() || it->is_null()) return out;
    if (it->is_number()) {
        out.value = (float)it->get<double>();
        out.present = true;
    }
    return out;
}

inline DivisionStats parseDivision(const nlohmann::json& o) {
    DivisionStats d;
    if (!o.is_object()) return d;
    d.present = true;
    d.passRate = jnumf(o, "pass_rate");
    d.cellsTotal = o.value("cells_total", 0);
    d.cellsPassed = o.value("cells_passed", 0);
    d.disciplineIndex = joptf(o, "discipline_index");
    return d;
}

// Best available Discipline Index for the compact table column.
// Prefer axes.discipline_index; else max of division DIs that are present.
inline OptFloat bestDiscipline(const ModelRow& m) {
    if (m.disciplineIndex.present) return m.disciplineIndex;
    OptFloat best;
    if (m.divLocal.disciplineIndex.present) {
        best = m.divLocal.disciplineIndex;
    }
    if (m.divOpen.disciplineIndex.present) {
        if (!best.present || m.divOpen.disciplineIndex.value > best.value)
            best = m.divOpen.disciplineIndex;
    }
    return best;
}

// Division badge letter(s): L, O, L/O, or empty when no division block present.
inline const char* divisionBadge(const ModelRow& m) {
    if (m.divLocal.present && m.divOpen.present) return "L/O";
    if (m.divLocal.present) return "L";
    if (m.divOpen.present) return "O";
    // Infer from kind when no divisions block (old format): local → L, paid → O.
    // Spec says badge per row when present; for old files leave empty so column is quiet.
    return "";
}

inline Board load(const std::string& pathOverride = {}) {
    Board b;
    b.path = pathOverride.empty() ? resolvePath() : pathOverride;
    std::ifstream f(b.path);
    if (!f) {
        // Absence = empty-state, never an error.
        b.loaded = false;
        return b;
    }
    nlohmann::json j;
    try {
        f >> j;
    } catch (...) {
        b.error = "Could not parse leaderboard.json";
        return b;
    }
    if (!j.is_object()) {
        b.error = "leaderboard.json is not an object";
        return b;
    }
    b.schema    = j.value("schema", std::string());
    b.generated = j.value("generated", std::string());
    b.taskSuite = j.value("task_suite", std::string());
    b.mock = b.taskSuite.find("-MOCK") != std::string::npos;

    const auto models = j.find("models");
    if (models != j.end() && models->is_array()) {
        for (const auto& m : *models) {
            if (!m.is_object()) continue;
            ModelRow r;
            r.id       = m.value("id", std::string());
            r.label    = m.value("label", r.id);
            r.provider = m.value("provider", std::string());
            r.kind     = m.value("kind", std::string());
            if (auto it = m.find("primary"); it != m.end() && it->is_object()) {
                r.passRate    = jnumf(*it, "pass_rate");
                r.cellsTotal  = it->value("cells_total", 0);
                r.cellsPassed = it->value("cells_passed", 0);
            }
            if (auto it = m.find("axes"); it != m.end() && it->is_object()) {
                r.toolValidity = jnumf(*it, "tool_validity");
                {
                    auto vf = joptf(*it, "vision_fix");
                    r.visionFixPresent = vf.present;
                    r.visionFix = vf.present ? vf.value : 0.f;
                }
                r.featureId = jnumf(*it, "feature_id");
                r.vcHygiene = jnumf(*it, "vc_hygiene");
                // New axes: null = untested, never 0.
                r.disciplineIndex = joptf(*it, "discipline_index");
                r.verifyEfficacy  = joptf(*it, "verify_efficacy");
                r.checkpointUtil  = joptf(*it, "checkpoint_util");
            }
            if (auto it = m.find("cost"); it != m.end() && it->is_object()) {
                auto c = it->find("usd_per_task_med");
                if (c != it->end() && c->is_number()) r.costUsd = (float)c->get<double>();
                r.latencyS  = jnumf(*it, "latency_s_med");
                // tokens_per_task_med may be float (median); coerce carefully.
                auto t = it->find("tokens_per_task_med");
                if (t != it->end() && t->is_number()) r.tokensMed = (int)std::lround(t->get<double>());
            }
            if (auto it = m.find("failure_taxonomy"); it != m.end() && it->is_object()) {
                for (auto ft = it->begin(); ft != it->end(); ++ft) {
                    if (ft->is_number_integer())
                        r.failures.emplace_back(ft.key(), ft->get<int>());
                    else if (ft->is_number())
                        r.failures.emplace_back(ft.key(), (int)ft->get<double>());
                }
                std::sort(r.failures.begin(), r.failures.end(),
                          [](const auto& a, const auto& b) { return a.second > b.second; });
            }
            if (auto it = m.find("per_challenge"); it != m.end() && it->is_array()) {
                for (const auto& c : *it) {
                    if (!c.is_object()) continue;
                    ChallengeRow cr;
                    cr.challenge = c.value("challenge", std::string());
                    cr.tier      = c.value("tier", std::string());
                    cr.division  = c.value("division", std::string());
                    cr.passRate  = jnumf(c, "pass_rate");
                    cr.seeds     = c.value("seeds", 0);
                    r.perChallenge.push_back(std::move(cr));
                }
            }
            // Additive: divisions.{local,open}
            if (auto it = m.find("divisions"); it != m.end() && it->is_object()) {
                if (auto loc = it->find("local"); loc != it->end())
                    r.divLocal = parseDivision(*loc);
                if (auto op = it->find("open"); op != it->end())
                    r.divOpen = parseDivision(*op);
            }
            // Additive: calibration bridge
            if (auto it = m.find("calibration"); it != m.end() && it->is_object()) {
                r.calibration.present = true;
                r.calibration.localPassRate = joptf(*it, "local_pass_rate");
                r.calibration.openPassRate  = joptf(*it, "open_pass_rate");
                r.calibration.bridgeDelta   = joptf(*it, "bridge_delta");
            }
            // Additive: instrumentation_summary
            if (auto it = m.find("instrumentation_summary"); it != m.end() && it->is_object()) {
                r.instrumentation.present = true;
                // phase_mortality_top may be object {phase: rate} or string.
                auto pmt = it->find("phase_mortality_top");
                if (pmt != it->end()) {
                    if (pmt->is_string()) {
                        r.instrumentation.phaseMortalityTop = pmt->get<std::string>();
                    } else if (pmt->is_object() && !pmt->empty()) {
                        // Pick the highest-rate phase.
                        std::string topPhase;
                        float topRate = -1.f;
                        for (auto pit = pmt->begin(); pit != pmt->end(); ++pit) {
                            if (!pit->is_number()) continue;
                            float rate = (float)pit->get<double>();
                            if (rate > topRate) {
                                topRate = rate;
                                topPhase = pit.key();
                            }
                        }
                        if (!topPhase.empty()) {
                            char buf[96];
                            std::snprintf(buf, sizeof(buf), "%s %.0f%%",
                                          topPhase.c_str(), topRate * 100.f);
                            r.instrumentation.phaseMortalityTop = buf;
                        }
                    }
                }
                auto tmf = it->find("top_misuse_fingerprint");
                if (tmf != it->end() && tmf->is_string())
                    r.instrumentation.topMisuseFingerprint = tmf->get<std::string>();
            }
            b.models.push_back(std::move(r));
        }
    }
    std::sort(b.models.begin(), b.models.end(),
              [](const ModelRow& a, const ModelRow& b) { return a.passRate > b.passRate; });
    b.loaded = true;
    return b;
}

inline void drawPassBar(float rate, float width, float height) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = height > 0 ? height : 8.0f * sc;
    const float w = width > 0 ? width : ImGui::GetContentRegionAvail().x;
    const float r = std::max(0.f, std::min(1.f, rate));
    ImU32 bg = ui::Palette::toU32(pal.accentSoft);
    ImU32 fg = r >= 0.5f ? ui::Palette::toU32(pal.statusOk)
             : r >= 0.2f ? ui::Palette::toU32(pal.statusWarn)
                         : ui::Palette::toU32(pal.statusErr);
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), bg, 3.0f * sc);
    if (r > 0.001f)
        dl->AddRectFilled(p, ImVec2(p.x + w * r, p.y + h), fg, 3.0f * sc);
    ImGui::Dummy(ImVec2(w, h));
}

// Format OptFloat as percent or "— untested" / "—".
// compact=true uses a short em-dash only (settings table).
inline void formatOptPct(char* buf, size_t n, const OptFloat& v, bool compact = false) {
    if (!v.present) {
        if (compact) std::snprintf(buf, n, "\xe2\x80\x94");  // —
        else std::snprintf(buf, n, "\xe2\x80\x94 untested");
        return;
    }
    std::snprintf(buf, n, "%.0f%%", v.value * 100.f);
}

// Compact table for Settings > AI (summary).
// Columns: Model | Provider | Pass rate | Discipline | Tool valid | Cost
// Discipline shows axes/division DI or "—"; division badge L/O on the model cell when present.
inline void drawCompactTable(const Board& b, float /*sc*/) {
    const float S = ui::uiScale;
    if (!b.error.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", b.error.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (!b.loaded || b.models.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextWrapped("No models evaluated yet. Run the benchmark harness to populate "
                           "benchmarks/leaderboard/leaderboard.json.");
        ImGui::PopStyleColor();
        return;
    }

    if (b.mock) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextUnformatted("MOCK");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextWrapped("task_suite ends in -MOCK (smoke data, not a real eval).");
        ImGui::PopStyleColor();
    }

    // Stretch weights leave room for the pass-rate bar + percent so the bar is never clipped
    // when the Settings modal is at its default width. Numeric columns right-align.
    if (ImGui::BeginTable("##lb_compact", 6,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_NoHostExtendX)) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.2f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 1.1f);
        ImGui::TableSetupColumn("Pass rate", ImGuiTableColumnFlags_WidthStretch, 2.0f);
        ImGui::TableSetupColumn("Discipline", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableSetupColumn("Tool valid", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableSetupColumn("Cost", ImGuiTableColumnFlags_WidthStretch, 0.8f);
        ImGui::TableHeadersRow();
        for (const auto& m : b.models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(m.label.empty() ? m.id.c_str() : m.label.c_str());
            // Division badge (L / O / L/O) — only when divisions block present.
            {
                const char* badge = divisionBadge(m);
                if (badge[0] != '\0') {
                    ImGui::SameLine(0, 6.0f * S);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accentDim));
                    ImGui::TextUnformatted(badge);
                    ImGui::PopStyleColor();
                }
            }
            if (b.mock) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
                ImGui::TextUnformatted("MOCK");
                ImGui::PopStyleColor();
            }
            ImGui::TableNextColumn();
            {
                char badge[64];
                std::snprintf(badge, sizeof(badge), "%s/%s",
                              m.provider.empty() ? "?" : m.provider.c_str(),
                              m.kind.empty() ? "?" : m.kind.c_str());
                ImGui::TextUnformatted(badge);
            }
            ImGui::TableNextColumn();
            {
                // Reserve room for the percent label so the bar never clips under it.
                const float pctW = 42.0f * S;
                const float barW = std::max(48.0f * S, ImGui::GetContentRegionAvail().x - pctW - 6.0f * S);
                char pct[32];
                std::snprintf(pct, sizeof(pct), "%.0f%%", m.passRate * 100.f);
                drawPassBar(m.passRate, barW, 8.0f * S);
                ImGui::SameLine(0, 6.0f * S);
                ImGui::TextUnformatted(pct);
            }
            ImGui::TableNextColumn();
            {
                char buf[32];
                formatOptPct(buf, sizeof(buf), bestDiscipline(m), /*compact=*/true);
                const float tw = ImGui::CalcTextSize(buf).x;
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                     std::max(0.0f, ImGui::GetContentRegionAvail().x - tw));
                if (!bestDiscipline(m).present)
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                ImGui::TextUnformatted(buf);
                if (!bestDiscipline(m).present)
                    ImGui::PopStyleColor();
            }
            ImGui::TableNextColumn();
            {
                char buf[32];
                std::snprintf(buf, sizeof(buf), "%.0f%%", m.toolValidity * 100.f);
                const float tw = ImGui::CalcTextSize(buf).x;
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                     std::max(0.0f, ImGui::GetContentRegionAvail().x - tw));
                ImGui::TextUnformatted(buf);
            }
            ImGui::TableNextColumn();
            {
                char buf[32];
                if (m.costUsd < 0.f) std::snprintf(buf, sizeof(buf), "\xe2\x80\x94");
                else std::snprintf(buf, sizeof(buf), "$%.3f", m.costUsd);
                const float tw = ImGui::CalcTextSize(buf).x;
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                     std::max(0.0f, ImGui::GetContentRegionAvail().x - tw));
                ImGui::TextUnformatted(buf);
            }
        }
        ImGui::EndTable();
    }
}

// Draw one division sub-table row block (Local or Open).
inline void drawDivisionBlock(const char* title, const DivisionStats& d, float S) {
    const auto& pal = ui::palette;
    if (!d.present) return;
    ImGui::TextUnformatted(title);
    ImGui::Indent(8.0f * S);
    {
        char pct[32];
        std::snprintf(pct, sizeof(pct), "%.0f%%  (%d/%d)", d.passRate * 100.f,
                      d.cellsPassed, d.cellsTotal);
        ImGui::Text("Pass");
        ImGui::SameLine(110.0f * S);
        drawPassBar(d.passRate, 180.0f * S, 8.0f * S);
        ImGui::SameLine();
        ImGui::TextUnformatted(pct);
    }
    {
        ImGui::Text("Discipline");
        ImGui::SameLine(110.0f * S);
        if (d.disciplineIndex.present) {
            drawPassBar(d.disciplineIndex.value, 180.0f * S, 8.0f * S);
            ImGui::SameLine();
            ImGui::Text("%.0f%%", d.disciplineIndex.value * 100.f);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted("\xe2\x80\x94 untested");
            ImGui::PopStyleColor();
        }
    }
    ImGui::Unindent(8.0f * S);
}

// Full pane for the AI workspace Leaderboard view.
inline void drawFullPane(Board& b, bool& wantRefresh) {
    const float S = ui::uiScale;
    const auto& pal = ui::palette;

    ImGui::BeginGroup();
    ImGui::TextUnformatted("Model Leaderboard");
    ImGui::SameLine();
    if (b.mock) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextUnformatted("[MOCK]");
        ImGui::PopStyleColor();
        ImGui::SameLine();
    }
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
    if (!b.taskSuite.empty()) ImGui::Text("suite: %s", b.taskSuite.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         std::max(0.0f, ImGui::GetContentRegionAvail().x - 80.0f * S));
    if (ImGui::SmallButton("Refresh##lb")) wantRefresh = true;
    ImGui::EndGroup();

    if (!b.error.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("%s", b.error.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (!b.loaded || b.models.empty()) {
        ImGui::Dummy(ImVec2(0, 24.0f * S));
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextWrapped("No models evaluated yet.\n"
                           "The benchmark harness writes benchmarks/leaderboard/leaderboard.json "
                           "(schema aicad.leaderboard/1). Absence is an empty-state, not an error.");
        ImGui::PopStyleColor();
        return;
    }

    // Summary table (+ Discipline column)
    if (ImGui::BeginTable("##lb_full", 7,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_ScrollY,
                          ImVec2(0, 140.0f * S))) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 0.26f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 0.13f);
        ImGui::TableSetupColumn("Pass", ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableSetupColumn("DI", ImGuiTableColumnFlags_WidthStretch, 0.10f);
        ImGui::TableSetupColumn("Tool", ImGuiTableColumnFlags_WidthStretch, 0.09f);
        ImGui::TableSetupColumn("Cost", ImGuiTableColumnFlags_WidthStretch, 0.11f);
        ImGui::TableSetupColumn("Latency", ImGuiTableColumnFlags_WidthStretch, 0.13f);
        ImGui::TableHeadersRow();
        for (const auto& m : b.models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(m.label.c_str());
            {
                const char* badge = divisionBadge(m);
                if (badge[0] != '\0') {
                    ImGui::SameLine(0, 4.0f * S);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.accentDim));
                    ImGui::TextUnformatted(badge);
                    ImGui::PopStyleColor();
                }
            }
            ImGui::TableNextColumn();
            ImGui::Text("%s · %s", m.provider.c_str(), m.kind.c_str());
            ImGui::TableNextColumn();
            drawPassBar(m.passRate, -1.f, 8.0f * S);
            ImGui::SameLine(0, 4.0f * S);
            ImGui::Text("%.0f%%", m.passRate * 100.f);
            ImGui::TableNextColumn();
            {
                char buf[32];
                OptFloat di = bestDiscipline(m);
                formatOptPct(buf, sizeof(buf), di, /*compact=*/true);
                if (!di.present)
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted(buf);
                if (!di.present)
                    ImGui::PopStyleColor();
            }
            ImGui::TableNextColumn();
            ImGui::Text("%.0f%%", m.toolValidity * 100.f);
            ImGui::TableNextColumn();
            if (m.costUsd < 0.f) ImGui::TextUnformatted("local");
            else ImGui::Text("$%.3f", m.costUsd);
            ImGui::TableNextColumn();
            ImGui::Text("%.0fs", m.latencyS);
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 8.0f * S));

    // Per-model detail cards: divisions + calibration + challenge bars + axes + failures + instr.
    for (const auto& m : b.models) {
        ImGui::PushID(m.id.c_str());
        if (ImGui::CollapsingHeader(m.label.c_str(), ImGuiTreeNodeFlags_DefaultOpen)) {
            ImGui::Indent(8.0f * S);

            // Division sub-tables — only when present (hidden for old-format files).
            if (m.divLocal.present || m.divOpen.present) {
                ImGui::TextUnformatted("Divisions");
                if (m.divLocal.present)
                    drawDivisionBlock("Local division", m.divLocal, S);
                if (m.divOpen.present)
                    drawDivisionBlock("Open division", m.divOpen, S);
                ImGui::Dummy(ImVec2(0, 4.0f * S));
            }

            // Calibration bridge row — only when present.
            if (m.calibration.present) {
                ImGui::TextUnformatted("Calibration bridge");
                ImGui::Indent(8.0f * S);
                char localBuf[32], openBuf[32], deltaBuf[32];
                formatOptPct(localBuf, sizeof(localBuf), m.calibration.localPassRate, true);
                formatOptPct(openBuf, sizeof(openBuf), m.calibration.openPassRate, true);
                if (m.calibration.bridgeDelta.present) {
                    // bridge_delta is a signed rate difference; show signed percentage points.
                    std::snprintf(deltaBuf, sizeof(deltaBuf), "%+.0f pp",
                                  m.calibration.bridgeDelta.value * 100.f);
                } else {
                    std::snprintf(deltaBuf, sizeof(deltaBuf), "\xe2\x80\x94");
                }
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::Text("local %s  ·  open %s  ·  bridge %s",
                            localBuf, openBuf, deltaBuf);
                ImGui::PopStyleColor();
                ImGui::Unindent(8.0f * S);
                ImGui::Dummy(ImVec2(0, 4.0f * S));
            }

            ImGui::TextUnformatted("Per-challenge pass rate");
            if (m.perChallenge.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("(no per-challenge rows)");
                ImGui::PopStyleColor();
            } else {
                for (const auto& c : m.perChallenge) {
                    char lbl[80];
                    if (!c.division.empty()) {
                        // Compact division tag on the challenge label.
                        const char* tag = (c.division == "local") ? "L"
                                        : (c.division == "open")  ? "O"
                                                                  : c.division.c_str();
                        std::snprintf(lbl, sizeof(lbl), "%s[%s]  %.0f%%",
                                      c.challenge.empty() ? "?" : c.challenge.c_str(),
                                      tag, c.passRate * 100.f);
                    } else {
                        std::snprintf(lbl, sizeof(lbl), "%s  %.0f%%",
                                      c.challenge.empty() ? "?" : c.challenge.c_str(),
                                      c.passRate * 100.f);
                    }
                    ImGui::TextUnformatted(lbl);
                    ImGui::SameLine(110.0f * S);
                    drawPassBar(c.passRate, 180.0f * S, 8.0f * S);
                }
            }

            ImGui::Dummy(ImVec2(0, 4.0f * S));
            ImGui::TextUnformatted("Diagnostic axes");
            auto axis = [&](const char* name, float v) {
                ImGui::Text("%s", name);
                ImGui::SameLine(140.0f * S);
                drawPassBar(v, 180.0f * S, 8.0f * S);
                ImGui::SameLine();
                ImGui::Text("%.0f%%", v * 100.f);
            };
            auto axisOpt = [&](const char* name, const OptFloat& v) {
                ImGui::Text("%s", name);
                ImGui::SameLine(140.0f * S);
                if (v.present) {
                    drawPassBar(v.value, 180.0f * S, 8.0f * S);
                    ImGui::SameLine();
                    ImGui::Text("%.0f%%", v.value * 100.f);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                    ImGui::TextUnformatted("\xe2\x80\x94 untested");
                    ImGui::PopStyleColor();
                }
            };
            axis("tool_validity", m.toolValidity);
            if (m.visionFixPresent) axis("vision_fix", m.visionFix);
            else {
                ImGui::Text("vision_fix");
                ImGui::SameLine(140.0f * S);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("\xe2\x80\x94 untested");
                ImGui::PopStyleColor();
            }
            axis("feature_id", m.featureId);
            axis("vc_hygiene", m.vcHygiene);
            // New axes — null renders as "— untested", NEVER 0.
            axisOpt("discipline_index", m.disciplineIndex);
            axisOpt("verify_efficacy", m.verifyEfficacy);
            axisOpt("checkpoint_util", m.checkpointUtil);

            ImGui::Dummy(ImVec2(0, 4.0f * S));
            ImGui::TextUnformatted("Failure taxonomy");
            if (m.failures.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("(none recorded)");
                ImGui::PopStyleColor();
            } else {
                for (const auto& f : m.failures)
                    ImGui::BulletText("%s  ·  %d", f.first.c_str(), f.second);
            }

            // Instrumentation summary — small dim text, only when present.
            if (m.instrumentation.present) {
                ImGui::Dummy(ImVec2(0, 4.0f * S));
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("Instrumentation");
                if (!m.instrumentation.phaseMortalityTop.empty())
                    ImGui::Text("  top phase mortality: %s",
                                m.instrumentation.phaseMortalityTop.c_str());
                if (!m.instrumentation.topMisuseFingerprint.empty())
                    ImGui::Text("  top misuse: %s",
                                m.instrumentation.topMisuseFingerprint.c_str());
                ImGui::PopStyleColor();
            }

            ImGui::Unindent(8.0f * S);
        }
        ImGui::PopID();
    }
}

}  // namespace leaderboard
}  // namespace ui
