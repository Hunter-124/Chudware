// Shared Model Leaderboard UI (aicad.leaderboard/1) — Settings compact table + AI workspace pane.
// Header-only so settings_modal.cpp and ai/ai_workspace.cpp share one parser + draw helpers.
#pragma once

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "metrics.h"

namespace ui {
namespace leaderboard {

struct ChallengeRow {
    std::string challenge;
    std::string tier;
    float passRate = 0.f;
    int seeds = 0;
};

struct ModelRow {
    std::string id;
    std::string label;
    std::string provider;
    std::string kind;  // paid | local
    float passRate = 0.f;
    int cellsTotal = 0;
    int cellsPassed = 0;
    float toolValidity = 0.f;
    float visionFix = 0.f;
    float featureId = 0.f;
    float vcHygiene = 0.f;
    float costUsd = -1.f;   // -1 = null/local
    float latencyS = 0.f;
    int tokensMed = 0;
    std::vector<std::pair<std::string, int>> failures;
    std::vector<ChallengeRow> perChallenge;
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
                r.visionFix    = jnumf(*it, "vision_fix");
                r.featureId    = jnumf(*it, "feature_id");
                r.vcHygiene    = jnumf(*it, "vc_hygiene");
            }
            if (auto it = m.find("cost"); it != m.end() && it->is_object()) {
                auto c = it->find("usd_per_task_med");
                if (c != it->end() && c->is_number()) r.costUsd = (float)c->get<double>();
                r.latencyS  = jnumf(*it, "latency_s_med");
                r.tokensMed = it->value("tokens_per_task_med", 0);
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
                    cr.passRate  = jnumf(c, "pass_rate");
                    cr.seeds     = c.value("seeds", 0);
                    r.perChallenge.push_back(std::move(cr));
                }
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

// Compact table for Settings > AI (summary).
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
    if (ImGui::BeginTable("##lb_compact", 5,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_NoHostExtendX)) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 1.2f);
        ImGui::TableSetupColumn("Pass rate", ImGuiTableColumnFlags_WidthStretch, 2.2f);
        ImGui::TableSetupColumn("Tool valid", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Cost", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();
        for (const auto& m : b.models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(m.label.empty() ? m.id.c_str() : m.label.c_str());
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

    // Summary table
    if (ImGui::BeginTable("##lb_full", 6,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_ScrollY,
                          ImVec2(0, 140.0f * S))) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 0.30f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 0.14f);
        ImGui::TableSetupColumn("Pass", ImGuiTableColumnFlags_WidthStretch, 0.20f);
        ImGui::TableSetupColumn("Tool", ImGuiTableColumnFlags_WidthStretch, 0.10f);
        ImGui::TableSetupColumn("Cost", ImGuiTableColumnFlags_WidthStretch, 0.12f);
        ImGui::TableSetupColumn("Latency", ImGuiTableColumnFlags_WidthStretch, 0.14f);
        ImGui::TableHeadersRow();
        for (const auto& m : b.models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(m.label.c_str());
            ImGui::TableNextColumn();
            ImGui::Text("%s · %s", m.provider.c_str(), m.kind.c_str());
            ImGui::TableNextColumn();
            drawPassBar(m.passRate, -1.f, 8.0f * S);
            ImGui::SameLine(0, 4.0f * S);
            ImGui::Text("%.0f%%", m.passRate * 100.f);
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

    // Per-model detail cards: challenge bars + axes + failure taxonomy
    for (const auto& m : b.models) {
        ImGui::PushID(m.id.c_str());
        if (ImGui::CollapsingHeader(m.label.c_str(), ImGuiTreeNodeFlags_DefaultOpen)) {
            ImGui::Indent(8.0f * S);

            ImGui::TextUnformatted("Per-challenge pass rate");
            if (m.perChallenge.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("(no per-challenge rows)");
                ImGui::PopStyleColor();
            } else {
                for (const auto& c : m.perChallenge) {
                    char lbl[64];
                    std::snprintf(lbl, sizeof(lbl), "%s  %.0f%%",
                                  c.challenge.empty() ? "?" : c.challenge.c_str(),
                                  c.passRate * 100.f);
                    ImGui::TextUnformatted(lbl);
                    ImGui::SameLine(90.0f * S);
                    drawPassBar(c.passRate, 180.0f * S, 8.0f * S);
                }
            }

            ImGui::Dummy(ImVec2(0, 4.0f * S));
            ImGui::TextUnformatted("Diagnostic axes");
            auto axis = [&](const char* name, float v) {
                ImGui::Text("%s", name);
                ImGui::SameLine(110.0f * S);
                drawPassBar(v, 180.0f * S, 8.0f * S);
                ImGui::SameLine();
                ImGui::Text("%.0f%%", v * 100.f);
            };
            axis("tool_validity", m.toolValidity);
            axis("vision_fix",    m.visionFix);
            axis("feature_id",    m.featureId);
            axis("vc_hygiene",    m.vcHygiene);

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

            ImGui::Unindent(8.0f * S);
        }
        ImGui::PopID();
    }
}

}  // namespace leaderboard
}  // namespace ui
