// In-app sampling profiler — see profiler_window.h.
#include "ui/profiler_window.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "app/app_state.h"
#include "ui/settings_modal.h"
#include "ui/viewport.h"
#include "theme/icons.h"
#include "theme/theme.h"
#include "cpp_core.h"

namespace ui {
namespace {

using nlohmann::json;

inline float px(float n) { return n * theme::S(); }
inline float rounding(float n) { return n * theme::S(); }

void pushText(unsigned int c) {
    ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(c));
}

uint32_t fnv1a(const std::string& s) {
    uint32_t h = 2166136261u;
    for (unsigned char c : s) {
        h ^= c;
        h *= 16777619u;
    }
    return h;
}

ImU32 flameColorForModule(const std::string& mod) {
    struct Entry { const char* m; unsigned int c; };
    static const Entry kPal[] = {
        {"libaicad", 0x4E79A7FF}, {"aicad", 0x4E79A7FF},
        {"TKBO", 0xE15759FF}, {"TKBRep", 0xF28E2BFF}, {"TKFillet", 0xEDC948FF},
        {"TKMesh", 0x76B7B2FF}, {"TKTopAlgo", 0xFF9DA7FF}, {"TKGeomAlgo", 0x59A14FFF},
        {"TKG3d", 0xB07AA1FF}, {"TKGeomBase", 0x9C755FFF}, {"TKMath", 0xBAB0ACFF},
    };
    for (const auto& e : kPal)
        if (mod == e.m) return theme::rgba(e.c);
    uint32_t h = fnv1a(mod);
    int r = 205 + static_cast<int>(h & 0x3F);
    int g = 80 + static_cast<int>((h >> 6) & 0x7F);
    int b = 55 + static_cast<int>((h >> 13) & 0x3F);
    return theme::rgba((unsigned(r) << 24) | (unsigned(g) << 16) | (unsigned(b) << 8) | 0xFF);
}

std::string moduleOf(const std::string& frame) {
    auto p = frame.find('!');
    return p == std::string::npos ? std::string("?") : frame.substr(0, p);
}

ImU32 flameColor(const std::string& frame) { return flameColorForModule(moduleOf(frame)); }

struct FlameRect {
    float x = 0.f, w = 0.f;
    int depth = 0;
    std::string name;
    unsigned long long samples = 0;
    double pct = 0.0;
};

struct BuildNode {
    std::string name;
    unsigned long long total = 0;
    std::unordered_map<std::string, int> kids;
};

std::string s_foldedCache;
std::vector<FlameRect> s_rects;
int s_maxDepth = 0;
unsigned long long s_total = 0;

void layoutNode(const std::vector<BuildNode>& nodes, int idx, int depth, double x, double total) {
    if (idx != 0) {
        const BuildNode& n = nodes[idx];
        double w = double(n.total) / total;
        FlameRect r;
        r.x = float(x);
        r.w = float(w);
        r.depth = depth - 1;
        r.name = n.name;
        r.samples = n.total;
        r.pct = 100.0 * double(n.total) / total;
        s_rects.push_back(std::move(r));
        if (depth - 1 > s_maxDepth) s_maxDepth = depth - 1;
    }
    std::vector<int> kids;
    kids.reserve(nodes[idx].kids.size());
    for (const auto& kv : nodes[idx].kids) kids.push_back(kv.second);
    std::sort(kids.begin(), kids.end(),
              [&](int a, int b) { return nodes[a].total > nodes[b].total; });
    double cx = x;
    for (int k : kids) {
        layoutNode(nodes, k, depth + 1, cx, total);
        cx += double(nodes[k].total) / total;
    }
}

void rebuildFlameIfNeeded(const std::string& folded) {
    if (folded == s_foldedCache) return;
    s_foldedCache = folded;
    s_rects.clear();
    s_maxDepth = 0;
    s_total = 0;

    std::vector<BuildNode> nodes;
    nodes.push_back(BuildNode{"all", 0, {}});

    size_t i = 0;
    while (i < folded.size()) {
        size_t nl = folded.find('\n', i);
        std::string line = folded.substr(i, nl == std::string::npos ? std::string::npos : nl - i);
        i = (nl == std::string::npos) ? folded.size() : nl + 1;
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        size_t sp = line.rfind(' ');
        if (sp == std::string::npos) continue;
        unsigned long long cnt = std::strtoull(line.c_str() + sp + 1, nullptr, 10);
        if (cnt == 0) continue;
        std::string stack = line.substr(0, sp);

        nodes[0].total += cnt;
        int cur = 0;
        size_t p = 0;
        while (p <= stack.size()) {
            size_t semi = stack.find(';', p);
            std::string fr =
                stack.substr(p, semi == std::string::npos ? std::string::npos : semi - p);
            p = (semi == std::string::npos) ? stack.size() + 1 : semi + 1;
            if (!fr.empty()) {
                int ci = -1;
                auto it = nodes[cur].kids.find(fr);
                if (it != nodes[cur].kids.end()) ci = it->second;
                if (ci < 0) {
                    ci = static_cast<int>(nodes.size());
                    nodes.push_back(BuildNode{fr, 0, {}});
                    nodes[cur].kids.emplace(fr, ci);
                }
                nodes[ci].total += cnt;
                cur = ci;
            }
            if (semi == std::string::npos) break;
        }
    }

    s_total = nodes[0].total;
    if (s_total == 0) return;
    layoutNode(nodes, 0, 0, 0.0, double(s_total));
}

void drawFlame(float rowH) {
    ImVec2 avail = ImGui::GetContentRegionAvail();
    float canvasW = avail.x;
    if (canvasW < 1.f) return;
    float canvasH = (s_maxDepth + 1) * rowH;

    ImVec2 origin = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##flame_hit", ImVec2(canvasW, canvasH + px(2)));
    bool canvasHovered = ImGui::IsItemHovered();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float bottom = origin.y + canvasH;
    const ImVec2 mouse = ImGui::GetMousePos();
    const ImU32 border = TCOL(glassBorder);
    const ImU32 ink = TCOL(txt0);
    const FlameRect* hover = nullptr;

    for (const auto& r : s_rects) {
        float x = origin.x + r.x * canvasW;
        float w = r.w * canvasW;
        if (w < 0.5f) continue;
        float y = bottom - (r.depth + 1) * rowH;
        ImVec2 mn(x, y);
        ImVec2 mx(x + (w > 1.f ? w - 1.f : w), y + rowH - 1.f);
        dl->AddRectFilled(mn, mx, flameColor(r.name), rounding(2));
        dl->AddRect(mn, mx, border, rounding(2));
        if (w > px(34)) {
            dl->PushClipRect(mn, mx, true);
            dl->AddText(ImVec2(x + px(3), y + px(1)), ink, r.name.c_str());
            dl->PopClipRect();
        }
        if (canvasHovered && mouse.x >= mn.x && mouse.x <= mx.x && mouse.y >= mn.y &&
            mouse.y <= mx.y)
            hover = &r;
    }

    if (hover) {
        theme::pushGlassTooltipStyle();
        if (ImGui::BeginTooltip()) {
            ImVec2 tp0 = ImGui::GetWindowPos();
            ImVec2 tp1(tp0.x + ImGui::GetWindowSize().x, tp0.y + ImGui::GetWindowSize().y);
            theme::glassBackground(ImGui::GetWindowDrawList(), tp0, tp1);
            ImGui::TextUnformatted(hover->name.c_str());
            ImGui::TextDisabled("%llu samples (%.1f%%)", hover->samples, hover->pct);
            ImGui::EndTooltip();
        }
        theme::popGlassTooltipStyle();
    }
}

float tableHeight(size_t rows) {
    float n = std::min<float>(static_cast<float>(rows), 10.f) + 1.f;
    return n * ImGui::GetTextLineHeightWithSpacing();
}

void drawModulesTable(const json& mods) {
    if (!mods.is_array() || mods.empty()) {
        ImGui::TextDisabled("(none)");
        return;
    }
    ImGuiTableFlags tf = ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg |
                         ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp;
    if (ImGui::BeginTable("##prof_mods", 3, tf, ImVec2(0, tableHeight(mods.size())))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Module", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("Self", ImGuiTableColumnFlags_WidthFixed, px(64));
        ImGui::TableSetupColumn("%", ImGuiTableColumnFlags_WidthFixed, px(54));
        ImGui::TableHeadersRow();
        for (const auto& m : mods) {
            std::string name = m.value("module", std::string("?"));
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            float sz = ImGui::GetTextLineHeight();
            ImVec2 cp = ImGui::GetCursorScreenPos();
            ImGui::GetWindowDrawList()->AddRectFilled(
                cp, ImVec2(cp.x + sz, cp.y + sz), flameColorForModule(name), rounding(2));
            ImGui::Dummy(ImVec2(sz + px(4), sz));
            ImGui::SameLine();
            ImGui::TextUnformatted(name.c_str());
            ImGui::TableSetColumnIndex(1);
            ImGui::Text("%llu", m.value("self", 0ULL));
            ImGui::TableSetColumnIndex(2);
            ImGui::Text("%.1f", m.value("pct", 0.0));
        }
        ImGui::EndTable();
    }
}

void drawFlatTable(const char* id, const json& arr, const char* valKey, const char* valHdr) {
    if (!arr.is_array() || arr.empty()) {
        ImGui::TextDisabled("(none)");
        return;
    }
    ImGuiTableFlags tf = ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg |
                         ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp;
    if (ImGui::BeginTable(id, 3, tf, ImVec2(0, tableHeight(arr.size())))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Frame", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn(valHdr, ImGuiTableColumnFlags_WidthFixed, px(64));
        ImGui::TableSetupColumn("%", ImGuiTableColumnFlags_WidthFixed, px(54));
        ImGui::TableHeadersRow();
        for (const auto& row : arr) {
            std::string frame = row.value("frame", std::string());
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(frame.c_str());
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", frame.c_str());
            ImGui::TableSetColumnIndex(1);
            ImGui::Text("%llu", row.value(valKey, 0ULL));
            ImGui::TableSetColumnIndex(2);
            ImGui::Text("%.1f", row.value("pct", 0.0));
        }
        ImGui::EndTable();
    }
}

bool profCall(app::AppState& st, const char* method, const json& params, json& res) {
    std::string err;
    if (!core::cpp_core().call(method, params, res, err)) {
        st.status = std::string("Profiler ") + method + " failed: " +
                    (err.empty() ? "libaicad prof_* unavailable" : err);
        return false;
    }
    return true;
}

json s_report;
bool s_haveReport = false;

}  // namespace

void drawProfilerWindow(app::AppState& st) {
    SettingsUiState& ui = settingsUi();
    if (!ui.showProfilerWindow) return;

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(
        ImVec2(vp->WorkPos.x + px(120), vp->WorkPos.y + px(80)), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSize(ImVec2(px(580), px(540)), ImGuiCond_FirstUseEver);

    bool open = ui.showProfilerWindow;
    theme::pushGlassWindowStyle();
    if (ImGui::Begin("Profiler", &open)) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        icons::draw(dl, "performance", ImVec2(ImGui::GetCursorScreenPos().x + px(11),
                                              ImGui::GetCursorScreenPos().y + px(11)),
                    px(11), TCOL(txt0));
        ImGui::Dummy(ImVec2(px(22), px(22)));
        ImGui::SameLine(0, px(8));
        pushText(theme::pal().txt0);
        ImGui::TextUnformatted("Sampling Profiler");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        pushText(ui.profilerRunning ? theme::pal().good : theme::pal().txt2);
        ImGui::TextUnformatted(ui.profilerRunning ? "  [running]" : "  [idle]");
        ImGui::PopStyleColor();
        ImGui::Separator();

        const bool running = ui.profilerRunning;
        ImGui::BeginDisabled(running);
        if (ImGui::Button("Start")) {
            json res;
            if (profCall(st, "prof_start", {{"hz", 1000}, {"mode", "modeling"}}, res)) {
                if (res.is_object() && res.contains("error")) {
                    st.status = "Profiler couldn't start: " +
                                res.value("error", std::string("unavailable"));
                } else {
                    ui.profilerRunning = true;
                    s_haveReport = false;
                    s_foldedCache.clear();
                    st.status = "Profiler ON — exercise the model, then Stop.";
                }
            }
        }
        ImGui::EndDisabled();

        ImGui::SameLine();
        ImGui::BeginDisabled(!running);
        if (ImGui::Button("Stop")) {
            json res;
            if (profCall(st, "prof_stop", {{"top", 40}}, res)) {
                ui.profilerRunning = false;
                s_report = std::move(res);
                s_haveReport = true;
            }
        }
        ImGui::SameLine();
        if (ImGui::Button("Refresh")) {
            json res;
            if (profCall(st, "prof_report", {{"top", 40}}, res)) {
                s_report = std::move(res);
                s_haveReport = true;
            }
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Symbolize samples so far without stopping.");

        GlassTimings glassT = viewportGlassTimings();
        if (glassT.valid) {
            ImGui::SeparatorText("GUI / Glass (GPU)");
            pushText(theme::pal().txt2);
            ImGui::Text("scene %.2f ms", glassT.sceneMs);
            ImGui::Text("blur %.2f ms", glassT.blurMs);
            ImGui::Text("luma %.2f ms", glassT.lumaMs);
            ImGui::PopStyleColor();
        }

        ImGui::Spacing();

        if (!s_haveReport) {
            pushText(theme::pal().txt2);
            ImGui::TextWrapped(
                "Click Start, exercise the model (extrude / fillet / boolean / rebuild), then "
                "Stop to see where the time went.");
            ImGui::PopStyleColor();
        } else if (!s_report.value("ok", false)) {
            pushText(theme::pal().warn);
            ImGui::TextWrapped("%s",
                               s_report.value("error", std::string("no samples captured")).c_str());
            ImGui::PopStyleColor();
        } else {
            json s = s_report.value("summary", json::object());
            pushText(theme::pal().txt2);
            ImGui::TextWrapped(
                "%llu samples · %llu unique stacks · %u us · %s mode · %.2fs wall · "
                "%llu unresolved",
                s.value("samples", 0ULL), s.value("unique_stacks", 0ULL),
                s.value("interval_us", 0u), s.value("mode", std::string("?")).c_str(),
                s.value("wall_seconds", 0.0), s.value("unresolved_frames", 0ULL));
            ImGui::PopStyleColor();
            ImGui::Spacing();

            if (ImGui::CollapsingHeader("Modules (self-time)", ImGuiTreeNodeFlags_DefaultOpen))
                drawModulesTable(s_report.value("modules", json::array()));
            if (ImGui::CollapsingHeader("Flat self-time (hottest leaves)"))
                drawFlatTable("##prof_self", s_report.value("flat_self", json::array()), "self",
                              "Self");
            if (ImGui::CollapsingHeader("Flat total-time (callers on the stack)"))
                drawFlatTable("##prof_total", s_report.value("flat_total", json::array()),
                              "total", "Total");

            if (ImGui::CollapsingHeader("Flame graph", ImGuiTreeNodeFlags_DefaultOpen)) {
                rebuildFlameIfNeeded(s_report.value("folded", std::string()));
                if (s_rects.empty()) {
                    ImGui::TextDisabled("(no stacks to chart)");
                } else {
                    pushText(theme::pal().txt2);
                    ImGui::TextUnformatted("Widest = hottest (left). Hover a box for details.");
                    ImGui::PopStyleColor();
                    const float rowH = ImGui::GetTextLineHeight() + px(3);
                    float wantH = (s_maxDepth + 1) * rowH + px(6);
                    float childH = std::min(wantH, px(320));
                    theme::pushGlassChildStyle();
                    if (ImGui::BeginChild("##prof_flame", ImVec2(0, childH), ImGuiChildFlags_Border))
                        drawFlame(rowH);
                    ImGui::EndChild();
                    theme::popGlassChildStyle();
                }
            }
        }
    }
    ImGui::End();
    theme::popGlassWindowStyle();
    ui.showProfilerWindow = open;
}

}  // namespace ui
