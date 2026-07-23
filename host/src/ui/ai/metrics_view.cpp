// See metrics_view.h.
#include "metrics_view.h"

#include <algorithm>
#include <cstdio>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (glassPanel/adaptivePanel rects)

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/copilot/copilot.h"   // BridgeModelMeta / bridgeModelMeta

namespace ui {
namespace {

std::string fmtUsd(double v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "$%.4g", v);
    return buf;
}

std::string fmtTok(long long t) {
    char buf[24];
    if (t >= 1000000)
        std::snprintf(buf, sizeof(buf), "%.1fM", t / 1.0e6);
    else if (t >= 1000)
        std::snprintf(buf, sizeof(buf), "%.1fk", t / 1.0e3);
    else
        std::snprintf(buf, sizeof(buf), "%lld", t);
    return buf;
}

// Same visual grammar as the other DAG-pane views: bold label + a thin accent underline.
void secHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::TextUnformatted(label);
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

struct ModelSpend {
    std::string provider, model;
    double usd = 0;
    long long tin = 0, tout = 0;
};

struct SessionSpend {
    std::string title;
    double usd = 0;
    long long tin = 0, tout = 0;
    int turns = 0;
    bool active = false;
};

}  // namespace

void drawMetricsView(AppState& st) {
    const float S = ui::uiScale;

    // ---- aggregate every priced (completed, model-stamped) assistant turn across every
    // session — active (st.chat) + parked (Conversation::msgs) — pricing with the cached
    // OMP bridge catalog. Same $/Mtok formula the per-session cost strip's agent_cost_summary
    // RPC uses (a pure linear calculator), computed client-side so Metrics needs zero extra
    // round-trips and works even when the bridge RPC path is unavailable.
    double totalUsd = 0;
    long long totalIn = 0, totalOut = 0;
    int totalTurns = 0;
    std::vector<SessionSpend> sessions;
    std::unordered_map<std::string, ModelSpend> byModel;

    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const Conversation& c = st.conversations[i];
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        SessionSpend ss;
        ss.title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        ss.active = (i == st.activeConv);
        for (const auto& m : msgs) {
            if (m.fromUser || m.running || m.model.empty()) continue;
            long long tin = 0, tout = 0;
            for (const auto& a : m.activity) {
                if (a.rowKind != 0) continue;   // tool/ctx/provider rows aren't turn tokens
                tin += a.tokensIn;
                tout += a.tokensOut;
            }
            if (tin <= 0 && tout <= 0) continue;
            const BridgeModelMeta* meta = bridgeModelMeta("", m.model);
            const std::string prov = (meta && !meta->provider.empty()) ? meta->provider
                                                                       : std::string("unknown");
            double usd = 0;
            if (meta && meta->hasCost)
                usd = (tin / 1.0e6) * meta->costIn + (tout / 1.0e6) * meta->costOut;

            ss.usd += usd; ss.tin += tin; ss.tout += tout; ss.turns++;
            totalUsd += usd; totalIn += tin; totalOut += tout; totalTurns++;

            ModelSpend& ms = byModel[prov + "/" + m.model];
            ms.provider = prov; ms.model = m.model;
            ms.usd += usd; ms.tin += tin; ms.tout += tout;
        }
        if (ss.turns > 0) sessions.push_back(std::move(ss));
    }

    if (totalTurns == 0) {
        float avail = ImGui::GetContentRegionAvail().y;
        ImGui::Dummy(ImVec2(0, avail * 0.35f));
        const char* msg = "No priced turns yet";
        const char* sub = "Token spend shows up here once an assistant turn completes with a "
                          "priced model. Start a run to see it.";
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
        ImGui::Spacing();
        float avail2 = ImGui::GetContentRegionAvail().x;
        float wrapW  = std::min(avail2 - 16.0f * S, 460.0f * S);
        float sw     = ImGui::CalcTextSize(sub, nullptr, false, wrapW).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, (avail2 - sw) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.75f));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::TextWrapped("%s", sub);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        return;
    }

    std::vector<ModelSpend> models;
    models.reserve(byModel.size());
    for (auto& kv : byModel) models.push_back(std::move(kv.second));
    std::sort(models.begin(), models.end(),
             [](const ModelSpend& a, const ModelSpend& b) { return a.usd > b.usd; });
    std::sort(sessions.begin(), sessions.end(),
             [](const SessionSpend& a, const SessionSpend& b) { return a.usd > b.usd; });

    ImGui::Indent(6.0f * S);

    // ---- headline totals ----
    secHeader("Overall");
    if (ImGui::BeginTable("##ai_metrics_totals", 4,
                          ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX)) {
        auto stat = [&](const char* label, const std::string& value) {
            ImGui::TableNextColumn();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(label);
            ImGui::PopStyleColor();
            ImGui::PushFont(st.fonts.medium);
            ImGui::TextUnformatted(value.c_str());
            ImGui::PopFont();
        };
        stat("Total spend", fmtUsd(totalUsd));
        stat("Total tokens", fmtTok(totalIn + totalOut));
        stat("Sessions tracked", std::to_string((int)sessions.size()));
        stat("Priced turns", std::to_string(totalTurns));
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, 10.0f * S));

    // ---- per-model breakdown ----
    secHeader("Spend by model");
    if (ImGui::BeginTable("##ai_metrics_models", 4,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 1.1f);
        ImGui::TableSetupColumn("Tokens (in / out)", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("Spend", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();
        for (const auto& mrow : models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(mrow.model.c_str());
            ImGui::TableNextColumn();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(mrow.provider.c_str());
            ImGui::PopStyleColor();
            ImGui::TableNextColumn();
            ImGui::Text("%s / %s", fmtTok(mrow.tin).c_str(), fmtTok(mrow.tout).c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(fmtUsd(mrow.usd).c_str());
        }
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, 10.0f * S));

    // ---- per-session breakdown ----
    secHeader("Spend by session");
    if (ImGui::BeginTable("##ai_metrics_sessions", 4,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableSetupColumn("Session", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Turns", ImGuiTableColumnFlags_WidthStretch, 0.7f);
        ImGui::TableSetupColumn("Tokens (in / out)", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("Spend", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();
        for (const auto& srow : sessions) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            if (srow.active) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
                ImGui::TextUnformatted(srow.title.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::TextUnformatted(srow.title.c_str());
            }
            ImGui::TableNextColumn();
            ImGui::Text("%d", srow.turns);
            ImGui::TableNextColumn();
            ImGui::Text("%s / %s", fmtTok(srow.tin).c_str(), fmtTok(srow.tout).c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(fmtUsd(srow.usd).c_str());
        }
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, 10.0f * S));

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1], palette.textDisabled[2], 1.0f));
    ImGui::TextWrapped("OMP-observed spend (catalog pricing \xc3\x97 tokens from completed "
                       "turns) \xe2\x80\x94 not a bill. Sessions or models with no priced turns "
                       "aren't listed; unpriced/local models show token counts with no $ figure.");
    ImGui::PopStyleColor();

    ImGui::Unindent(6.0f * S);
}

}  // namespace ui
