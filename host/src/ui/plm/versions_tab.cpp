#include "versions_tab.h"

#include "plm_hub.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/anim.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"

namespace ui {
namespace {

struct LayoutNode { int lane = 0; };
struct VersionsView {
    float zoom = 1.0f, zoomTarget = 1.0f;
    float panX = 0.0f, panY = 0.0f, panTargetX = 0.0f, panTargetY = 0.0f;
    bool fit = true;
    bool compareLoading = false, timelineLoading = false;
    int comparePending = 0;  // outstanding per-item plm_compare calls for compareKey
    std::string compareKey, compareSummary, compareError;
    std::vector<std::string> compareChanges;
    std::string timelineKey, timelineError;
    struct TimelineRow { std::string authoredAt, authoredBy, summary; };
    std::vector<TimelineRow> timeline;
    bool showChanges = true, showAi = true;
    char tagName[64] = {};
    char checkpointMessage[192] = {};
    char branchName[96] = {};
};

VersionsView& view() { static VersionsView v; return v; }
float px(float v) { return v * ui::uiScale; }

std::string shortSha(const std::string& sha) {
    return sha.size() > 8 ? sha.substr(0, 8) : sha;
}

bool containsInsensitive(const std::string& haystack, const char* needle) {
    if (!needle || !needle[0]) return true;
    std::string h = haystack, n = needle;
    std::transform(h.begin(), h.end(), h.begin(), [](unsigned char c){ return (char)std::tolower(c); });
    std::transform(n.begin(), n.end(), n.begin(), [](unsigned char c){ return (char)std::tolower(c); });
    return h.find(n) != std::string::npos;
}

ImU32 laneColor(int lane, float alpha = 1.0f) {
    static const float* roles[] = {
        palette.accent, palette.axisY, palette.statusWarn, palette.diffHunk,
        palette.axisX, palette.sketchExternal, palette.simMesh, palette.axisZ
    };
    const float* c = roles[(unsigned)lane % (sizeof(roles) / sizeof(roles[0]))];
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], alpha));
}

std::vector<LayoutNode> assignLanes(const std::vector<PlmVersionNodeRow>& nodes) {
    std::vector<LayoutNode> out(nodes.size());
    std::unordered_map<std::string, int> reserved;
    std::unordered_set<int> occupied;
    for (size_t i = 0; i < nodes.size(); ++i) {
        const auto& n = nodes[i];
        int lane = 0;
        auto own = reserved.find(n.commit);
        if (own != reserved.end()) {
            lane = own->second;
            reserved.erase(own);
        } else {
            while (occupied.count(lane)) ++lane;
        }
        out[i].lane = lane;
        occupied.erase(lane);
        for (size_t p = 0; p < n.parents.size(); ++p) {
            if (reserved.count(n.parents[p])) continue;
            int parentLane = lane;
            if (p > 0) {
                parentLane = 0;
                while (occupied.count(parentLane)) ++parentLane;
            }
            reserved[n.parents[p]] = parentLane;
            occupied.insert(parentLane);
        }
    }
    return out;
}

void finishAction(AppState& st, const core::RpcResult& r, const char* success) {
    g_plmHub.busy = false;
    std::string hint;
    if (!plmOk(r, hint)) { st.toast(ui::ToastLevel::Error, hint); return; }
    st.toast(ui::ToastLevel::Info, success);
    plmInvalidate();
}

void callMutation(AppState& st, const char* method, nlohmann::json p, const char* success) {
    if (g_plmHub.busy) return;
    g_plmHub.busy = true;
    p["path"] = st.plmProjectPath();
    st.rpc.call(method, p, [&st, success](const core::RpcResult& r) { finishAction(st, r, success); });
}

// plm_compare is per ITEM: it answers "what changed in this file between these two commits",
// and refuses with "that item is not present at one of those points" for an item that does
// not exist at both ends. "What changed in this version" is therefore the union over the
// project's items, with the not-present refusals treated as "untouched" rather than surfaced
// as an error (which is what made this panel read as broken on a perfectly good commit).
void fetchCompare(AppState& st, const PlmVersionNodeRow& node) {
    auto& V = view();
    if (node.parents.empty()) return;
    const std::string key = node.parents.front() + "|" + node.commit;
    if (V.compareLoading || V.compareKey == key) return;
    if (g_plmHub.items.empty()) { plmEnsureItems(st); return; }
    V.compareKey = key;
    V.compareSummary.clear(); V.compareChanges.clear(); V.compareError.clear();
    V.comparePending = 0;
    for (const auto& item : g_plmHub.items) {
        if (item.itemId.empty()) continue;
        ++V.comparePending;
        V.compareLoading = true;
        nlohmann::json p{{"path", st.plmProjectPath()}, {"item_id", item.itemId},
                         {"commit_a", node.parents.front()}, {"commit_b", node.commit},
                         {"visual", false}, {"annotate", false}};
        std::string label = item.path;
        if (const size_t slash = label.find_last_of("/\\"); slash != std::string::npos)
            label = label.substr(slash + 1);
        if (label.empty()) label = item.partNumber.empty() ? item.itemId : item.partNumber;
        st.rpc.call("plm_compare", p, [key, label](const core::RpcResult& r) {
            auto& V = view();
            if (V.compareKey != key) return;
            if (V.comparePending > 0) --V.comparePending;
            V.compareLoading = V.comparePending > 0;
            std::string reason;
            if (!plmOk(r, reason)) return;  // absent at one end == untouched here
            std::string summary = r.result.value("summary", std::string());
            if (auto d = r.result.find("doc_diff"); d != r.result.end() && d->is_object()) {
                for (auto it = d->begin(); it != d->end(); ++it) {
                    if (it->is_array() && it->empty()) continue;
                    V.compareChanges.push_back(label + " — " + it.key() + ": " +
                        (it->is_string() ? it->get<std::string>() : it->dump()));
                }
            }
            if (!summary.empty()) V.compareChanges.push_back(label + " — " + summary);
        });
    }
    if (V.comparePending == 0) V.compareLoading = false;
}

// plm_timeline is the AI-VC micro-history: per-DOC agent checkpoints, addressed by doc_id,
// with no notion of a vault commit. Its rows are {label, when|time, kind, authored_by,
// features} — an earlier revision of this panel sent a `commit` it does not accept and read
// keys it never returns, so the section was permanently empty.
void fetchTimeline(AppState& st, const PlmVersionNodeRow& node) {
    auto& V = view();
    if (V.timelineLoading || V.timelineKey == node.commit) return;
    V.timelineLoading = true; V.timelineKey = node.commit;
    V.timeline.clear(); V.timelineError.clear();
    nlohmann::json p{{"path", st.plmProjectPath()}};
    st.rpc.call("plm_timeline", p, [key=node.commit](const core::RpcResult& r) {
        auto& V = view(); V.timelineLoading = false;
        if (V.timelineKey != key) return;
        if (!plmOk(r, V.timelineError)) return;
        auto rows = r.result.find("checkpoints");
        if (rows == r.result.end() || !rows->is_array()) return;
        for (const auto& row : *rows) {
            if (!row.is_object()) continue;
            VersionsView::TimelineRow t;
            t.authoredAt = row.value("when", row.value("time", std::string()));
            t.authoredBy = row.value("authored_by", std::string());
            t.summary = row.value("label", std::string());
            const std::string kind = row.value("kind", std::string());
            if (!kind.empty()) t.summary = kind + ": " + t.summary;
            V.timeline.push_back(std::move(t));
        }
    });
}

const PlmVersionNodeRow* selectedNode() {
    const auto& V = g_plmHub.versionsGraph;
    for (const auto& n : V.nodes) if (n.commit == V.selected) return &n;
    return nullptr;
}

void drawToolbar(AppState& st) {
    auto& G = g_plmHub.versionsGraph;
    auto& V = view();
    ImGui::SetNextItemWidth(px(220.0f));
    ImGui::InputTextWithHint("##versions_search", "Search messages, authors, refs", G.search, sizeof(G.search));
    ImGui::SameLine(0.0f, px(8.0f));
    const char* scopes[] = {"Design", "Planning", "All"};
    int scope = G.scope;
    if (kit::segmented("##versions_scope", scopes, 3, &scope, px(250.0f))) {
        G.scope = scope; G.gen = -1;
    }
    ImGui::SameLine(0.0f, px(8.0f));
    if (kit::iconButton("##versions_fit", "mesh", "Fit all commits")) V.fit = true;
    ImGui::SameLine(0.0f, px(6.0f));
    if (kit::iconButton("##versions_refresh", "history", "Refresh versions")) G.gen = -1;
    ImGui::SameLine(0.0f, px(8.0f));
    const bool two = !G.selected.empty() && !G.compareSecond.empty();
    if (kit::secondaryButton(two ? "Compare these two" : "Compare", two,
                             "Ctrl-click or Cmd-click a second commit")) {
        const PlmVersionNodeRow* a = selectedNode();
        if (a) {
            PlmVersionNodeRow temp = *a;
            temp.parents = {G.compareSecond};
            fetchCompare(st, temp);
        }
    }
}

// Onshape's legend also lists a release CANDIDATE state. This tree has exactly three item
// lifecycle states (In work / Released / Superseded, contract plm.md §5) and never produces
// a candidate, so advertising one here would be a legend entry for a glyph that can never
// appear. Merge is listed instead, which we DO produce and Onshape's panel does not.
void drawLegend() {
    kit::chip("Workspace", kit::Tone::Accent);
    ImGui::SameLine(); kit::chip("Version", kit::Tone::Neutral);
    ImGui::SameLine(); kit::chip("Change", kit::Tone::Neutral);
    ImGui::SameLine(); kit::chip("Merge", kit::Tone::Neutral);
    ImGui::SameLine(); kit::chip("Release", kit::Tone::Ok);
}

void drawCanvas(AppState& st, ImVec2 size) {
    auto& G = g_plmHub.versionsGraph;
    auto& V = view();
    const auto lanes = assignLanes(G.nodes);
    const float lanePitch = px(34.0f), rowPitch = px(58.0f), left = px(42.0f), top = px(34.0f);
    int maxLane = 0; for (const auto& l : lanes) maxLane = std::max(maxLane, l.lane);
    if (V.fit && !G.nodes.empty()) {
        const float worldW = left * 2.0f + lanePitch * (maxLane + 1) + px(280.0f);
        const float worldH = top * 2.0f + rowPitch * G.nodes.size();
        V.zoomTarget = std::clamp(std::min(size.x / worldW, size.y / worldH), 0.35f, 1.25f);
        V.panTargetX = px(16.0f); V.panTargetY = px(10.0f); V.fit = false;
    }
    V.zoom = ui::anim::toward(ImGui::GetID("##versions_zoom"), V.zoomTarget, ui::anim::kBase);
    V.panX = ui::anim::toward(ImGui::GetID("##versions_pan_x"), V.panTargetX, ui::anim::kBase);
    V.panY = ui::anim::toward(ImGui::GetID("##versions_pan_y"), V.panTargetY, ui::anim::kBase);

    ImGui::InvisibleButton("##versions_canvas", size, ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    const bool hovered = ImGui::IsItemHovered();
    const ImVec2 origin = ImGui::GetItemRectMin();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(origin, ImVec2(origin.x + size.x, origin.y + size.y), true);
    ui::glassBackground(dl, origin, ImVec2(origin.x + size.x, origin.y + size.y), 10.0f);
    auto screen = [&](int lane, int row) {
        return ImVec2(origin.x + V.panX + (left + lanePitch * lane) * V.zoom,
                      origin.y + V.panY + (top + rowPitch * row) * V.zoom);
    };
    if (hovered && ImGui::GetIO().MouseWheel != 0.0f) {
        const ImVec2 mp = ImGui::GetIO().MousePos;
        const float old = V.zoom;
        const float next = std::clamp(old * (ImGui::GetIO().MouseWheel > 0.0f ? 1.12f : 1.0f / 1.12f), 0.25f, 2.4f);
        const float lx = (mp.x - origin.x - V.panX) / old;
        const float ly = (mp.y - origin.y - V.panY) / old;
        V.zoomTarget = next;
        V.panTargetX = mp.x - origin.x - lx * next;
        V.panTargetY = mp.y - origin.y - ly * next;
    }
    if (hovered && (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) || ImGui::IsMouseDragging(ImGuiMouseButton_Left))) {
        const ImVec2 d = ImGui::GetIO().MouseDelta;
        V.panX += d.x; V.panY += d.y; V.panTargetX += d.x; V.panTargetY += d.y;
    }

    std::unordered_map<std::string, size_t> index;
    for (size_t i = 0; i < G.nodes.size(); ++i) index[G.nodes[i].commit] = i;
    for (size_t i = 0; i < G.nodes.size(); ++i) {
        const auto& n = G.nodes[i];
        const ImVec2 a = screen(lanes[i].lane, (int)i);
        for (size_t p = 0; p < n.parents.size(); ++p) {
            auto found = index.find(n.parents[p]); if (found == index.end()) continue;
            const size_t pi = found->second;
            const ImVec2 b = screen(lanes[pi].lane, (int)pi);
            const float bend = std::min(px(26.0f), std::fabs(b.y - a.y) * 0.35f);
            dl->AddBezierCubic(a, ImVec2(a.x, a.y + bend), ImVec2(b.x, b.y - bend), b,
                               laneColor((int)(p == 0 ? lanes[i].lane : lanes[pi].lane), 0.78f), px(2.0f));
        }
    }

    std::vector<ImRect> labels;
    std::string hoverCommit;
    // Rows are a fixed pitch, so the visible span is arithmetic — at limit=400 nodes this
    // keeps the per-frame draw work proportional to what is actually on screen.
    const float rowStepPx = rowPitch * V.zoom;
    const int firstRow = rowStepPx > 0.0f
        ? std::max(0, (int)std::floor((-V.panY - top * V.zoom - rowStepPx) / rowStepPx))
        : 0;
    const int lastRow = rowStepPx > 0.0f
        ? std::min((int)G.nodes.size(), (int)std::ceil((size.y - V.panY) / rowStepPx) + 1)
        : (int)G.nodes.size();
    for (size_t i = (size_t)firstRow; i < (size_t)lastRow; ++i) {
        const auto& n = G.nodes[i];
        std::string searchable = n.message + " " + n.summary + " " + n.author.display;
        for (const auto& ref : n.refs) searchable += " " + ref.name;
        if (!containsInsensitive(searchable, G.search)) continue;
        const ImVec2 c = screen(lanes[i].lane, (int)i);
        const float radius = px(n.kind == "auto" ? 4.0f : 7.0f) * std::max(0.75f, V.zoom);
        const float dx = ImGui::GetIO().MousePos.x - c.x, dy = ImGui::GetIO().MousePos.y - c.y;
        if (hovered && dx * dx + dy * dy <= radius * radius) hoverCommit = n.commit;
        const bool selected = n.commit == G.selected || n.commit == G.compareSecond;
        const ImU32 color = laneColor(lanes[i].lane);
        if (n.kind == "release") {
            dl->AddCircleFilled(c, radius, Palette::toU32(palette.panelBg));
            dl->AddCircle(c, radius, color, 20, px(3.0f));
        } else if (n.kind == "merge") {
            dl->AddCircle(c, radius, color, 20, px(2.0f));
            dl->AddCircle(c, radius * 0.55f, color, 16, px(2.0f));
        } else if (n.kind == "restore") {
            dl->AddRectFilled(ImVec2(c.x-radius, c.y-radius), ImVec2(c.x+radius, c.y+radius), color, px(2.0f));
        } else {
            dl->AddCircleFilled(c, radius, color);
        }
        if (selected || n.commit == G.head) dl->AddCircle(c, radius + px(3.0f), Palette::toU32(palette.accent), 24, px(2.0f));
        std::string label = n.message.empty() ? n.summary : n.message;
        if (label.empty()) label = shortSha(n.commit);
        const ImVec2 lp(c.x + px(14.0f), c.y - ImGui::GetTextLineHeight() * 0.5f);
        const ImVec2 ts = ImGui::CalcTextSize(label.c_str());
        ImRect r(lp, ImVec2(lp.x + std::min(ts.x, px(260.0f)), lp.y + ts.y));
        bool clash = false; for (const auto& used : labels) if (used.Overlaps(r)) { clash = true; break; }
        if (!clash || selected || n.commit == hoverCommit) {
            labels.push_back(r);
            const std::string elided = kit::elide(label.c_str(), px(260.0f));
            dl->AddText(lp, Palette::toU32(selected ? palette.text : palette.textDim), elided.c_str());
        }
        float badgeX = lp.x + std::min(ts.x, px(260.0f)) + px(8.0f);
        // A tip commit carries BOTH a branch ref and a head ref with the same name, and the
        // pin collapse can hand the release node a second copy. The accent rim already says
        // "this is HEAD", so chip only distinct branch/version/release names.
        std::vector<std::string> shown;
        for (const auto& ref : n.refs) {
            if (ref.name.empty() || ref.type == "head") continue;
            if (std::find(shown.begin(), shown.end(), ref.name) != shown.end()) continue;
            shown.push_back(ref.name);
            const ImVec2 rs = ImGui::CalcTextSize(ref.name.c_str());
            const ImVec2 p0(badgeX, c.y - px(10.0f)), p1(badgeX + rs.x + px(12.0f), c.y + px(10.0f));
            dl->AddRectFilled(p0, p1, kit::toneU32(ref.type == "release" ? kit::Tone::Ok : kit::Tone::Accent, 0.24f), kit::chipRound());
            dl->AddText(ImVec2(p0.x + px(6.0f), c.y - rs.y * 0.5f), Palette::toU32(palette.text), ref.name.c_str());
            badgeX = p1.x + px(5.0f);
        }
    }
    // Select on RELEASE and only when the press did not turn into a pan drag: the canvas
    // pans with the left button too, so selecting on press would fire on every drag start.
    const bool dragged = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left).x != 0.0f ||
                         ImGui::GetMouseDragDelta(ImGuiMouseButton_Left).y != 0.0f;
    if (hovered && ImGui::IsMouseReleased(ImGuiMouseButton_Left) && !dragged &&
        !hoverCommit.empty()) {
        if (ImGui::GetIO().KeyCtrl || ImGui::GetIO().KeySuper) {
            if (hoverCommit != G.selected) G.compareSecond = hoverCommit;
        } else {
            G.selected = hoverCommit; G.compareSecond.clear();
            V.compareKey.clear(); V.timelineKey.clear();
        }
    }
    dl->PopClipRect();
}

void drawInspector(AppState& st, const PlmVersionNodeRow& n) {
    auto& G = g_plmHub.versionsGraph;
    auto& V = view();
    plmSectionHeader("Version details", "history");
    ImGui::TextUnformatted((n.message.empty() ? n.summary : n.message).c_str());
    ImGui::TextDisabled("%s  %s", shortSha(n.commit).c_str(), n.authoredAt.c_str());
    if (!n.author.display.empty()) ImGui::Text("By %s", n.author.display.c_str());
    if (!n.ai.model.empty()) {
        kit::valueChip("AI model", n.ai.model.c_str(), kit::Tone::Accent);
        if (!n.ai.provider.empty()) { ImGui::SameLine(); kit::chip(n.ai.provider.c_str()); }
    }
    if (!n.thumbnailRef.empty()) kit::inlineStatus("Geometry thumbnail is available for this commit.", kit::Tone::Neutral);
    ImGui::Separator();
    if (kit::secondaryButton("Tag this version", !g_plmHub.busy)) ImGui::OpenPopup("##tag_version");
    ImGui::SameLine();
    if (kit::dangerButton("Restore", !g_plmHub.busy)) callMutation(st, "plm_restore", {{"commit", n.commit}}, "Version restored");
    if (ImGui::BeginPopup("##tag_version")) {
        ImGui::InputTextWithHint("##tag_name", "Name (optional, e.g. V4)", V.tagName, sizeof(V.tagName));
        if (kit::primaryButton("Create version")) {
            nlohmann::json p{{"commit", n.commit}};
            if (V.tagName[0]) p["name"] = V.tagName;
            callMutation(st, "plm_version_tag", std::move(p), "Version tagged");
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    ImGui::InputTextWithHint("##checkpoint_message", "Checkpoint message", V.checkpointMessage, sizeof(V.checkpointMessage));
    if (kit::secondaryButton("New checkpoint", !g_plmHub.busy))
        callMutation(st, "plm_checkpoint", {{"message", V.checkpointMessage}}, "Checkpoint created");
    ImGui::InputTextWithHint("##branch_name", "New branch name", V.branchName, sizeof(V.branchName));
    if (kit::secondaryButton("Create branch", !g_plmHub.busy && V.branchName[0]))
        callMutation(st, "plm_branch_create", {{"name", V.branchName}, {"from", n.commit}}, "Branch created");
    for (const auto& branch : G.branches) {
        if (branch.current) continue;
        if (kit::secondaryButton(("Switch to " + branch.name).c_str(), !g_plmHub.busy))
            callMutation(st, "plm_branch_switch", {{"name", branch.name}}, "Branch switched");
        if (kit::secondaryButton(("Merge " + branch.name).c_str(), !g_plmHub.busy))
            callMutation(st, "plm_branch_merge", {{"name", branch.name}}, "Branch merged");
    }
    ImGui::Separator();
    if (ImGui::CollapsingHeader("Show changes", ImGuiTreeNodeFlags_DefaultOpen)) {
        fetchCompare(st, n);
        if (V.compareLoading) kit::spinner("Comparing geometry and document changes");
        else if (!V.compareError.empty()) kit::inlineStatus(V.compareError.c_str(), kit::Tone::Warn);
        else {
            if (!V.compareSummary.empty()) ImGui::TextWrapped("%s", V.compareSummary.c_str());
            // BulletText does not wrap, and a doc_diff row is long enough to be clipped to
            // uselessness in a 300px inspector.
            for (const auto& change : V.compareChanges) {
                ImGui::Bullet();
                ImGui::SameLine();
                ImGui::TextWrapped("%s", change.c_str());
            }
            if (n.parents.empty()) kit::emptyState("history", "First commit", "There is no parent version to compare.");
        }
    }
    if (ImGui::CollapsingHeader("AI activity", ImGuiTreeNodeFlags_DefaultOpen)) {
        fetchTimeline(st, n);
        if (V.timelineLoading) kit::spinner("Loading agent micro-history");
        else if (!V.timelineError.empty()) kit::inlineStatus(V.timelineError.c_str(), kit::Tone::Warn);
        else if (V.timeline.empty()) kit::emptyState("history", "No agent micro-history", "Agent-authored checkpoints will appear here with provenance.");
        else for (const auto& row : V.timeline) {
            if (!kit::cardBegin(("##timeline_" + row.authoredAt + row.summary).c_str())) { kit::cardEnd(); continue; }
            ImGui::TextWrapped("%s", row.summary.c_str());
            ImGui::TextDisabled("%s  %s", row.authoredBy.c_str(), row.authoredAt.c_str());
            kit::cardEnd();
        }
    }
}

}  // namespace

bool plmVersionsAvailable() { return g_plmHub.versionsGraph.available; }

void plmVersionsOnProjectChanged() {
    auto& G = g_plmHub.versionsGraph;
    const bool available = G.available;
    G = PlmVersionsState{};
    G.available = available;
    view() = VersionsView{};
}

void plmDrawVersionsPage(AppState& st) {
    auto& G = g_plmHub.versionsGraph;
    plmEnsureVersionGraph(st);
    drawToolbar(st);
    ImGui::Dummy(ImVec2(0.0f, px(4.0f)));
    drawLegend();
    ImGui::Dummy(ImVec2(0.0f, px(6.0f)));
    if (G.loading && G.nodes.empty()) { kit::spinner("Loading versions and history"); return; }
    if (!G.err.empty()) { kit::blockedState("Versions unavailable", G.err.c_str(), "Refresh after the PLM service is upgraded."); return; }
    if (G.nodes.empty()) { kit::emptyState("history", "No versions yet", "Create a checkpoint or save a design to begin its history."); return; }
    const float inspectorW = std::min(px(320.0f), ImGui::GetContentRegionAvail().x * 0.38f);
    const float canvasW = std::max(px(260.0f), ImGui::GetContentRegionAvail().x - inspectorW - px(10.0f));
    ImGui::BeginChild("##versions_graph", ImVec2(canvasW, 0.0f), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    drawCanvas(st, ImGui::GetContentRegionAvail());
    ImGui::EndChild();
    ImGui::SameLine(0.0f, px(10.0f));
    ImGui::BeginChild("##versions_inspector", ImVec2(0.0f, 0.0f), ImGuiChildFlags_Borders);
    if (const auto* n = selectedNode()) drawInspector(st, *n);
    else kit::emptyState("history", "Select a commit", "Choose any node to inspect its provenance and changes.");
    ImGui::EndChild();
}

}  // namespace ui
