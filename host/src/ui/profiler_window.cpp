// In-app sampling-profiler window — implementation. See profiler_window.h.
//
// Drives the libaicad sampling profiler over core::cpp_core() (prof_start / prof_stop /
// prof_report — the same RPCs app.cpp's Evaluate>Profiler toggle uses), then renders the
// returned report: a module self-time table, flat self/total tables, and a flame graph parsed
// from the "folded" stacks. The parsed flame layout is cached in this TU and rebuilt only when
// a new report (a new folded string) arrives. Glass styling throughout; {ok:false} is handled
// gracefully (a hint / warning, never a crash).
#include "profiler_window.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "icons.h"
#include "metrics.h"

#include "../core/app_state.h"
#include "../core/cpp_core.h"  // core::cpp_core() — the prof_* RPC seam
#include "../ui_theme.h"

namespace ui {

namespace {

using nlohmann::json;
namespace M = ui::metrics;

void pushText(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}

// ----------------------------------------------------------------- module colours
// Mirror tools/flamegraph.py's fixed palette so a subsystem keeps its colour, then a stable
// hash-derived warm colour for everything else. "aicad" aliases "libaicad" (the Linux module
// name strips the leading "lib").
uint32_t fnv1a(const std::string& s) {
    uint32_t h = 2166136261u;
    for (unsigned char c : s) {
        h ^= c;
        h *= 16777619u;
    }
    return h;
}

ImU32 flameColorForModule(const std::string& mod) {
    struct Entry {
        const char* m;
        ImU32 c;
    };
    static const Entry kPal[] = {
        {"libaicad", IM_COL32(0x4e, 0x79, 0xa7, 255)},
        {"aicad", IM_COL32(0x4e, 0x79, 0xa7, 255)},
        {"TKBO", IM_COL32(0xe1, 0x57, 0x59, 255)},
        {"TKBRep", IM_COL32(0xf2, 0x8e, 0x2b, 255)},
        {"TKFillet", IM_COL32(0xed, 0xc9, 0x48, 255)},
        {"TKMesh", IM_COL32(0x76, 0xb7, 0xb2, 255)},
        {"TKTopAlgo", IM_COL32(0xff, 0x9d, 0xa7, 255)},
        {"TKGeomAlgo", IM_COL32(0x59, 0xa1, 0x4f, 255)},
        {"TKG3d", IM_COL32(0xb0, 0x7a, 0xa1, 255)},
        {"TKGeomBase", IM_COL32(0x9c, 0x75, 0x5f, 255)},
        {"TKMath", IM_COL32(0xba, 0xb0, 0xac, 255)},
    };
    for (const auto& e : kPal)
        if (mod == e.m) return e.c;
    uint32_t h = fnv1a(mod);
    int r = 205 + static_cast<int>(h & 0x3F);
    int g = 80 + static_cast<int>((h >> 6) & 0x7F);
    int b = 55 + static_cast<int>((h >> 13) & 0x3F);
    if (r > 255) r = 255;
    if (g > 255) g = 255;
    if (b > 255) b = 255;
    return IM_COL32(r, g, b, 255);
}

std::string moduleOf(const std::string& frame) {
    auto p = frame.find('!');
    return p == std::string::npos ? std::string("?") : frame.substr(0, p);
}

ImU32 flameColor(const std::string& frame) { return flameColorForModule(moduleOf(frame)); }

// ----------------------------------------------------------------- flame layout cache
// One laid-out box: position/size as fractions of the canvas width (resolution-independent, so
// the cache survives window resizes), plus its depth and sample stats.
struct FlameRect {
    float x = 0.0f;   // left edge, fraction of canvas width [0,1]
    float w = 0.0f;   // width, fraction [0,1]
    int depth = 0;    // 0 = a top-level frame (drawn at the bottom of the graph)
    std::string name;
    unsigned long long samples = 0;
    double pct = 0.0;
};

// A node in the folded-stacks tree, built then discarded each rebuild.
struct BuildNode {
    std::string name;
    unsigned long long total = 0;
    std::unordered_map<std::string, int> kids;  // child frame name -> node index
};

std::string s_foldedCache;          // the folded text the cache below was built from
std::vector<FlameRect> s_rects;     // laid-out boxes (root excluded)
int s_maxDepth = 0;
unsigned long long s_total = 0;

// Recursively lay out a node's subtree into s_rects (fractions of width). Widest children
// first, so the hottest paths sit on the left (matches tools/flamegraph.py).
void layoutNode(const std::vector<BuildNode>& nodes, int idx, int depth, double x, double total) {
    if (idx != 0) {  // node 0 is the synthetic root ("all") — not drawn
        const BuildNode& n = nodes[idx];
        double w = double(n.total) / total;
        FlameRect r;
        r.x = float(x);
        r.w = float(w);
        r.depth = depth - 1;  // root's children render at depth 0
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
    nodes.push_back(BuildNode{"all", 0, {}});  // root

    size_t i = 0;
    while (i < folded.size()) {
        size_t nl = folded.find('\n', i);
        std::string line = folded.substr(i, nl == std::string::npos ? std::string::npos : nl - i);
        i = (nl == std::string::npos) ? folded.size() : nl + 1;

        // strip trailing CR / spaces
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
                {
                    auto it = nodes[cur].kids.find(fr);
                    if (it != nodes[cur].kids.end()) ci = it->second;
                }
                if (ci < 0) {
                    ci = static_cast<int>(nodes.size());
                    nodes.push_back(BuildNode{fr, 0, {}});  // may realloc -> re-index below
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

// ----------------------------------------------------------------- flame draw
void drawFlame(float rowH) {
    ImVec2 avail = ImGui::GetContentRegionAvail();
    float canvasW = avail.x;
    if (canvasW < 1.0f) return;
    float canvasH = (s_maxDepth + 1) * rowH;

    ImVec2 origin = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##flame_hit", ImVec2(canvasW, canvasH + M::px(2)));
    bool canvasHovered = ImGui::IsItemHovered();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float bottom = origin.y + canvasH;
    const ImVec2 mouse = ImGui::GetMousePos();
    const ImU32 border = ui::Palette::toU32(ui::palette.glassBorder);
    const ImU32 ink = IM_COL32(24, 24, 28, 255);  // dark label ink on the warm fills
    const FlameRect* hover = nullptr;

    for (const auto& r : s_rects) {
        float x = origin.x + r.x * canvasW;
        float w = r.w * canvasW;
        if (w < 0.5f) continue;
        float y = bottom - (r.depth + 1) * rowH;
        ImVec2 mn(x, y);
        ImVec2 mx(x + (w > 1.0f ? w - 1.0f : w), y + rowH - 1.0f);
        dl->AddRectFilled(mn, mx, flameColor(r.name), M::rounding(2));
        dl->AddRect(mn, mx, border, M::rounding(2));
        if (w > M::px(34)) {
            dl->PushClipRect(mn, mx, true);
            dl->AddText(ImVec2(x + M::px(3), y + M::px(1)), ink, r.name.c_str());
            dl->PopClipRect();
        }
        if (canvasHovered && mouse.x >= mn.x && mouse.x <= mx.x && mouse.y >= mn.y &&
            mouse.y <= mx.y)
            hover = &r;
    }

    if (hover) {
        ui::pushGlassTooltipStyle();
        if (ImGui::BeginTooltip()) {
            ImVec2 tp0 = ImGui::GetWindowPos();
            ImVec2 tp1(tp0.x + ImGui::GetWindowSize().x, tp0.y + ImGui::GetWindowSize().y);
            ui::glassBackground(ImGui::GetWindowDrawList(), tp0, tp1);
            ImGui::TextUnformatted(hover->name.c_str());
            ImGui::TextDisabled("%llu samples (%.1f%%)", hover->samples, hover->pct);
            ImGui::EndTooltip();
        }
        ui::popGlassTooltipStyle();
    }
}

// ----------------------------------------------------------------- tables
float tableHeight(size_t rows) {
    float n = std::min<float>(static_cast<float>(rows), 10.0f) + 1.0f;  // +1 header row
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
        ImGui::TableSetupColumn("Self", ImGuiTableColumnFlags_WidthFixed, M::px(64));
        ImGui::TableSetupColumn("%", ImGuiTableColumnFlags_WidthFixed, M::px(54));
        ImGui::TableHeadersRow();
        for (const auto& m : mods) {
            std::string name = m.value("module", std::string("?"));
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            // colour swatch tying the row to its flame-graph boxes
            float sz = ImGui::GetTextLineHeight();
            ImVec2 cp = ImGui::GetCursorScreenPos();
            ImGui::GetWindowDrawList()->AddRectFilled(
                cp, ImVec2(cp.x + sz, cp.y + sz), flameColorForModule(name), M::rounding(2));
            ImGui::Dummy(ImVec2(sz + M::px(4), sz));
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
        ImGui::TableSetupColumn(valHdr, ImGuiTableColumnFlags_WidthFixed, M::px(64));
        ImGui::TableSetupColumn("%", ImGuiTableColumnFlags_WidthFixed, M::px(54));
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

// ----------------------------------------------------------------- RPC helper
// Dispatch a prof_* RPC into the core; toast + return false if the core didn't handle it
// (library not loaded / unknown method) so the UI degrades gracefully.
bool profCall(AppState& st, const char* method, const json& params, json& res) {
    std::string err;
    if (!core::cpp_core().call(method, params, res, err)) {
        st.toast(ui::ToastLevel::Error,
                 std::string("Profiler ") + method + " failed: " +
                     (err.empty()
                          ? std::string("engine unavailable - rebuild + restart so libaicad "
                                        "exposes prof_*")
                          : err));
        return false;
    }
    return true;
}

// The last report (a prof_stop / prof_report result), kept across frames so the window keeps
// showing it after the call returns. Either {ok:true, summary, modules, flat_*, folded} or
// {ok:false, error}.
json s_report;
bool s_haveReport = false;

}  // namespace

void drawProfilerWindow(AppState& st) {
    if (!st.showProfilerWindow) return;
    const auto& pal = ui::palette;

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(
        ImVec2(vp->WorkPos.x + M::px(120), vp->WorkPos.y + M::px(80)), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSize(ImVec2(M::px(580), M::px(540)), ImGuiCond_FirstUseEver);

    bool open = st.showProfilerWindow;
    ui::pushGlassWindowStyle();
    if (ImGui::Begin("Profiler", &open)) {
        // ---- header ----
        IconUV iv = ui::getIcon("performance", 20);
        if (iv.valid) {
            ImGui::Image(iv.texId, ImVec2(M::iconHeader(), M::iconHeader()), iv.uv0, iv.uv1);
            ImGui::SameLine(0, M::asmHeaderIconGap());
        }
        pushText(pal.text);
        ImGui::TextUnformatted("Sampling Profiler");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        pushText(st.profilerRunning ? pal.statusOk : pal.textDim);
        ImGui::TextUnformatted(st.profilerRunning ? "  [running]" : "  [idle]");
        ImGui::PopStyleColor();
        // Live GUI frame rate (ImGui Framerate) — labeled so Evaluate > Performance
        // always shows FPS while this window is open (H-B1 joint verify).
        {
            const float fps = ImGui::GetIO().Framerate;
            ImGui::SameLine(0, M::px(16));
            pushText(pal.textDim);
            ImGui::TextUnformatted("FPS");
            ImGui::PopStyleColor();
            ImGui::SameLine(0, M::px(6));
            pushText(fps >= 45.f ? pal.statusOk : (fps >= 25.f ? pal.statusWarn : pal.statusErr));
            ImGui::Text("%.1f", fps);
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "GUI frames per second (ImGui). Not the sampling-profiler tick rate.");
        }
        ImGui::Separator();

        // ---- controls ----
        const bool running = st.profilerRunning;
        ImGui::BeginDisabled(running);
        if (ImGui::Button("Start")) {
            json res;
            if (profCall(st, "prof_start", {{"hz", 1000}, {"mode", "modeling"}}, res)) {
                if (res.is_object() && res.contains("error")) {
                    st.toast(ui::ToastLevel::Error,
                             "Profiler couldn't start: " +
                                 res.value("error", std::string("unavailable")));
                } else {
                    st.profilerRunning = true;
                    s_haveReport = false;
                    s_foldedCache.clear();  // drop the previous session's flame cache
                    st.toast(ui::ToastLevel::Info,
                             "Profiler ON - exercise the model, then Stop (or Refresh).");
                }
            }
        }
        ImGui::EndDisabled();

        ImGui::SameLine();
        ImGui::BeginDisabled(!running);
        if (ImGui::Button("Stop")) {
            json res;
            if (profCall(st, "prof_stop", {{"top", 40}}, res)) {
                st.profilerRunning = false;
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
            ImGui::SetTooltip("Symbolize the samples so far without stopping the profiler.");

        ImGui::Spacing();

        // ---- report ----
        if (!s_haveReport) {
            pushText(pal.textDim);
            ImGui::TextWrapped(
                "Click Start, exercise the model (extrude / fillet / boolean / rebuild), then "
                "Stop to see where the time went. In modeling mode only doc_* operations are "
                "sampled.");
            ImGui::PopStyleColor();
        } else if (!s_report.value("ok", false)) {
            pushText(pal.statusWarn);
            ImGui::TextWrapped("%s",
                               s_report.value("error", std::string("no samples captured"))
                                   .c_str());
            ImGui::PopStyleColor();
        } else {
            json s = s_report.value("summary", json::object());
            pushText(pal.textDim);
            ImGui::TextWrapped(
                "%llu samples  -  %llu unique stacks  -  %u us  -  %s mode  -  %.2fs wall  -  "
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
                    pushText(pal.textDim);
                    ImGui::TextUnformatted("Widest = hottest (left). Hover a box for details.");
                    ImGui::PopStyleColor();
                    const float rowH = ImGui::GetTextLineHeight() + M::px(3);
                    float wantH = (s_maxDepth + 1) * rowH + M::px(6);
                    float childH = std::min(wantH, M::px(320));
                    ui::pushGlassChildStyle();
                    if (ImGui::BeginChild("##prof_flame", ImVec2(0, childH), ImGuiChildFlags_Border))
                        drawFlame(rowH);
                    ImGui::EndChild();
                    ui::popGlassChildStyle();
                }
            }
        }
    }
    ImGui::End();
    ui::popGlassWindowStyle();
    st.showProfilerWindow = open;  // window close box drives the AppState flag

    // Corner overlay — always-on-top labeled FPS while the Performance profiler
    // window is open so drag/rebuild interactivity is visible without hunting
    // the window header (H-B1).
    if (st.showProfilerWindow) {
        const ImGuiViewport* mvp = ImGui::GetMainViewport();
        const float fps = ImGui::GetIO().Framerate;
        const ImVec2 pad(M::px(12), M::px(12));
        ImGui::SetNextWindowPos(
            ImVec2(mvp->WorkPos.x + mvp->WorkSize.x - pad.x, mvp->WorkPos.y + pad.y),
            ImGuiCond_Always, ImVec2(1.f, 0.f));
        ImGui::SetNextWindowBgAlpha(0.72f);
        const ImGuiWindowFlags flags =
            ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoDocking |
            ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings |
            ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoNav |
            ImGuiWindowFlags_NoMove;
        if (ImGui::Begin("##perf_fps_overlay", nullptr, flags)) {
            pushText(pal.textDim);
            ImGui::TextUnformatted("FPS");
            ImGui::PopStyleColor();
            ImGui::SameLine(0, M::px(6));
            pushText(fps >= 45.f ? pal.statusOk : (fps >= 25.f ? pal.statusWarn : pal.statusErr));
            ImGui::Text("%.1f", fps);
            ImGui::PopStyleColor();
        }
        ImGui::End();
    }
}

}  // namespace ui
