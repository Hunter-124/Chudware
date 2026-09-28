// context_chips.cpp — see context_chips.h.
#include "ui/copilot/context_chips.h"

#include "copilot_internal.h"
#include "ui/feature_tree.h"

namespace ui {
namespace {

bool samePath(const std::string& a, const std::string& b) {
    if (a.empty() || b.empty()) return false;
    if (a == b) return true;
    namespace fs = std::filesystem;
    return fs::path(a).lexically_normal() == fs::path(b).lexically_normal();
}

// A feature (part tree) or component (assembly tree) row by id.
const nlohmann::json* treeRow(const nlohmann::json& tree, const std::string& id) {
    if (id.empty() || !tree.is_object()) return nullptr;
    for (const char* fam : {"features", "components"}) {
        auto arr = tree.find(fam);
        if (arr == tree.end() || !arr->is_array()) continue;
        for (const auto& row : *arr)
            if (row.is_object() && row.value("id", std::string()) == id) return &row;
    }
    return nullptr;
}

std::string treeName(const nlohmann::json& tree, const std::string& id) {
    const nlohmann::json* row = treeRow(tree, id);
    return row ? row->value("name", std::string()) : std::string();
}

bool isDocTab(const AppState::DocTab& t) {
    return t.kind == AppState::DocTab::TabKind::Part ||
           t.kind == AppState::DocTab::TabKind::Assembly;
}

std::string clip(const std::string& s, size_t n) {
    return s.size() <= n ? s : s.substr(0, n - 1) + "\xE2\x80\xA6";
}

// ---- async doc_resolve_pick -----------------------------------------------------------------
bool samePick(const core::ContextRef& r, const core::ContextRef& key) {
    return r.docId == key.docId && r.kind == key.kind && r.bodyId == key.bodyId &&
           r.topoId == key.topoId && r.revision == key.revision;
}

// Fold the engine's reply into `r`. Returns false when the pick could not be resolved.
bool applyResolve(AppState& st, core::ContextRef& r, const core::RpcResult& reply) {
    r.resolving = false;
    if (!reply.ok) {
        r.resolveError = reply.err.empty() ? "The engine could not resolve this pick." : reply.err;
        return false;
    }
    const nlohmann::json& res = reply.result;
    if (res.contains("code")) {
        r.resolveError = res.value("message", std::string("The pick could not be resolved."));
        return false;
    }
    if (!res.contains("selector") || !res["selector"].is_object()) {
        r.resolveError = "The picked geometry no longer exists. Pick it again.";
        return false;
    }
    r.resolveError.clear();
    r.selector = res["selector"];
    const std::string sid = r.selector.value("sid", std::string());
    if (!sid.empty()) r.id = sid;
    auto own = r.selector.find("owner_feature");
    if (own != r.selector.end() && own->is_string() && !own->get<std::string>().empty())
        r.owner = own->get<std::string>();
    else if (res.contains("owner_feature") && res["owner_feature"].is_string())
        r.owner = res["owner_feature"].get<std::string>();
    if (res.contains("component") && res["component"].is_string())
        r.component = res["component"].get<std::string>();
    if (st.docMirror.docId() == r.docId) {
        const nlohmann::json& tree = st.docMirror.tree();
        if (r.component.empty() && r.ownerName.empty()) r.ownerName = treeName(tree, r.owner);
        if (!r.component.empty() && r.componentName.empty())
            r.componentName = treeName(tree, r.component);
    }
    return true;
}

void startResolve(AppState& st, const core::ContextRef& key) {
    st.rpc.call("doc_resolve_pick",
        {{"doc_id", key.docId}, {"revision", key.revision}, {"body_id", key.bodyId},
         {"kind", key.kind}, {"topo_id", (int)key.topoId}},
        [&st, key](const core::RpcResult& reply) {
            auto& refs = st.pendingContextRefs;
            auto it = std::find_if(refs.begin(), refs.end(), [&](const core::ContextRef& r) {
                return r.resolving && samePick(r, key);
            });
            if (it != refs.end()) {
                if (!applyResolve(st, *it, reply)) {
                    st.addLog("[context] " + it->resolveError);
                } else {
                    // The same entity picked again after an edit resolves to an attached sid:
                    // a re-pick detaches, exactly like re-picking at the same revision.
                    const size_t self = (size_t)(it - refs.begin());
                    for (size_t j = 0; j < refs.size(); ++j) {
                        if (j == self || refs[j].kind != it->kind || refs[j].docId != it->docId ||
                            refs[j].id != it->id)
                            continue;
                        refs.erase(refs.begin() + (std::ptrdiff_t)std::max(j, self));
                        refs.erase(refs.begin() + (std::ptrdiff_t)std::min(j, self));
                        break;
                    }
                }
                return;
            }
            // Already sent: complete the copy on the sent bubble so its chip can still be
            // located later (the worker resolved the pick itself for the prompt).
            int scanned = 0;
            for (auto m = st.chat.rbegin(); m != st.chat.rend() && scanned < 8; ++m) {
                if (!m->fromUser) continue;
                ++scanned;
                for (auto& r : m->contextRefs)
                    if (r.resolving && samePick(r, key)) {
                        applyResolve(st, r, reply);
                        markChatHistoryDirty();
                        return;
                    }
            }
        });
}

// ---- focus ----------------------------------------------------------------------------------
struct PendingFocus {
    bool active = false;
    core::ContextRef ref;
    std::string docId;
    double since = 0.0;
};
PendingFocus s_focus;

void highlightBody(AppState& st, uint32_t body) {
    st.selection.clear();
    st.selectedFeatureId.clear();
    st.featureHighlightFaces.clear();
    for (const auto& kv : st.faceNormalCache)
        if ((uint32_t)(kv.first >> 32) == body) st.featureHighlightFaces.push_back(kv.first);
}

bool showFeature(AppState& st, const std::string& fid) {
    if (fid.empty() || !treeRow(st.docMirror.tree(), fid)) return false;
    st.selection.clear();
    st.selectedFeatureId = fid;
    st.treeScrollToSelection = true;
    refreshFeatureHighlight(st);
    return true;
}

// Nearest cached face/edge to the selector's centroid (part docs: world == selector frame).
render::PickHit relocate(const AppState& st, const core::ContextRef& ref) {
    render::PickHit hit;
    const nlohmann::json& s = ref.selector;
    if (!s.is_object() || !ref.component.empty()) return hit;
    auto c = s.find("centroid");
    if (c == s.end() || !c->is_array() || c->size() != 3 || !(*c)[0].is_number() ||
        !(*c)[1].is_number() || !(*c)[2].is_number())
        return hit;
    const float cx = (*c)[0].get<float>(), cy = (*c)[1].get<float>(), cz = (*c)[2].get<float>();
    float bestD = 1e30f;
    uint64_t best = 0;
    auto consider = [&](uint64_t key, float x, float y, float z) {
        const float d = (x - cx) * (x - cx) + (y - cy) * (y - cy) + (z - cz) * (z - cz);
        if (d < bestD) { bestD = d; best = key; }
    };
    if (ref.kind == "face") {
        for (const auto& kv : st.faceNormalCache) consider(kv.first, kv.second[3], kv.second[4], kv.second[5]);
    } else if (ref.kind == "edge") {
        for (const auto& kv : st.edgeCentroidCache) consider(kv.first, kv.second[0], kv.second[1], kv.second[2]);
    }
    // Mesh centroids approximate the B-rep ones (curved faces drift with the tessellation):
    // accept within 1 mm or 5 % of the entity's own size, whichever is larger.
    const auto m = s.find("area_or_len");
    const double size = (m != s.end() && m->is_number()) ? m->get<double>() : 0.0;
    const double scale = ref.kind == "face" ? std::sqrt(std::max(0.0, size)) : size;
    const float tol = (float)std::max(1.0, 0.05 * scale);
    if (best == 0 || bestD > tol * tol) return hit;
    hit.body_id = (uint32_t)(best >> 32);
    hit.topo_id = (uint32_t)(best & 0xFFFFFFFFu);
    hit.kind = ref.kind == "face" ? render::PickKind::Face : render::PickKind::Edge;
    return hit;
}

void applyFocus(AppState& st, const core::ContextRef& ref) {
    const std::string what = ref.label.empty() ? ref.kind : ref.label;
    if (ref.kind == "feature" || ref.kind == "sketch") {
        if (!showFeature(st, ref.id))
            st.toast(ToastLevel::Info, "\"" + what + "\" is no longer in this document.");
        return;
    }
    if (ref.kind == "component") {
        const nlohmann::json& tree = st.docMirror.tree();
        for (const auto& kv : st.bodyBBoxCache) {
            const nlohmann::json* comp = featureTreeAssemblyComponent(tree, kv.first);
            if (comp && comp->value("id", std::string()) == ref.id) {
                highlightBody(st, kv.first);
                return;
            }
        }
        if (!showFeature(st, ref.id))  // an imported body in a part is a feature row
            st.toast(ToastLevel::Info, "\"" + what + "\" is no longer in this document.");
        return;
    }
    if (ref.kind == "body") {
        uint32_t body = ref.bodyId;
        if (body == 0 && ref.id.size() > 1 && ref.id[0] == 'b')
            body = (uint32_t)std::strtoul(ref.id.c_str() + 1, nullptr, 10);
        if (body != 0 && st.bodyBBoxCache.count(body)) highlightBody(st, body);
        else st.toast(ToastLevel::Info, "\"" + what + "\" is no longer in this document.");
        return;
    }
    render::PickHit hit;
    if (ref.bodyId != 0 && ref.topoId != 0 && ref.revision == st.docMirror.revision()) {
        hit.body_id = ref.bodyId;
        hit.topo_id = ref.topoId;
        hit.kind = ref.kind == "face"   ? render::PickKind::Face
                 : ref.kind == "edge"   ? render::PickKind::Edge
                                        : render::PickKind::Vertex;
    } else {
        hit = relocate(st, ref);
    }
    if (hit.valid()) {
        st.selectedFeatureId.clear();
        st.featureHighlightFaces.clear();
        st.selection.selectOne(hit);
        featureTreeRevealPick(st, hit);
        return;
    }
    if (ref.component.empty() && showFeature(st, ref.owner)) {
        const std::string owner = ref.ownerName.empty() ? ref.owner : ref.ownerName;
        st.toast(ToastLevel::Info, "That " + ref.kind + " changed since it was picked \xE2\x80\x94 "
                                   "showing the feature that made it (" + owner + ").");
        return;
    }
    st.toast(ToastLevel::Info, "That " + ref.kind + " changed since it was picked and can't be "
                               "located any more.");
}

const char* chipIcon(const core::ContextRef& ref) {
    if (ref.kind == "face") return "display_shaded";
    if (ref.kind == "edge") return "display_edges";
    if (ref.kind == "vertex") return "point";
    if (ref.kind == "body") return "body_folder";
    if (ref.kind == "component") return "part";
    if (ref.kind == "sketch") return fmFeatureIcon(ref.type.empty() ? "sketch" : ref.type);
    if (ref.kind == "feature" && !ref.type.empty()) return fmFeatureIcon(ref.type);
    return "ws_features";
}

std::string chipLabel(const core::ContextRef& ref) {
    return clip(ref.label.empty() ? (ref.kind.empty() ? std::string("ref") : ref.kind) : ref.label,
                28);
}

// " · Bracket" when the chip names another document, " · not open" when that doc is closed.
std::string chipDocSuffix(const AppState& st, const core::ContextRef& ref) {
    const int idx = contextRefTabIndex(st, ref);
    if (idx < 0) return "  \xC2\xB7  not open";
    const auto& tab = st.docTabs[(size_t)idx];
    if (tab.docId == st.docMirror.docId()) return {};
    return "  \xC2\xB7  " + clip(tab.title.empty() ? ref.docName : tab.title, 18);
}

std::string chipTooltip(const AppState& st, const core::ContextRef& ref) {
    const std::string sid = ref.selector.is_object() ? ref.selector.value("sid", std::string())
                                                     : std::string();
    std::string t = ref.kind + " \xC2\xB7 " + (ref.label.empty() ? ref.id : ref.label);
    if (!sid.empty() || (!ref.id.empty() && !core::contextRefIsTopology(ref)))
        t += "\nid: " + (sid.empty() ? ref.id : sid);
    if (!ref.type.empty()) t += "\ntype: " + ref.type;
    if (!ref.owner.empty())
        t += "\nfeature: " + (ref.ownerName.empty() ? ref.owner : ref.ownerName + " (" + ref.owner + ")");
    if (!ref.component.empty())
        t += "\ncomponent: " +
             (ref.componentName.empty() ? ref.component
                                        : ref.componentName + " (" + ref.component + ")");
    const std::string geom = core::contextRefGeometry(ref);
    if (!geom.empty()) t += "\n" + geom;
    if (!ref.docName.empty() || !ref.docPath.empty())
        t += "\ndocument: " + (ref.docName.empty() ? ref.docPath : ref.docName);
    if (ref.resolving) t += "\nResolving the pick\xE2\x80\xA6";
    if (!ref.resolveError.empty()) t += "\n" + ref.resolveError;
    if (contextRefTabIndex(st, ref) < 0) t += "\nThat document is not open.";
    else t += "\nClick to show it in the model.";
    return t;
}

}  // namespace

void addContextRef(AppState& st, core::ContextRef ref) {
    if (ref.docId.empty()) ref.docId = st.docMirror.docId();
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        const auto& tab = st.docTabs[(size_t)st.activeTab];
        if (tab.docId == ref.docId) {
            ref.docName = tab.title;
            ref.docPath = tab.path;
        }
    }
    const bool resolve = !ref.docId.empty() && ref.bodyId != 0 &&
                         (core::contextRefIsTopology(ref) ? ref.topoId != 0 : ref.kind == "body");
    if (resolve) {
        ref.resolving = true;
        if (ref.revision < 0) ref.revision = st.docMirror.revision();
    }
    const core::ContextRef key = ref;
    if (core::toggleContextRef(st.pendingContextRefs, std::move(ref)) && resolve)
        startResolve(st, key);
}

int contextRefTabIndex(const AppState& st, const core::ContextRef& ref) {
    for (size_t i = 0; i < st.docTabs.size(); ++i) {
        const auto& tab = st.docTabs[i];
        if (!isDocTab(tab)) continue;
        if (!ref.docId.empty() && tab.docId == ref.docId) return (int)i;
    }
    if (ref.docPath.empty()) return -1;
    for (size_t i = 0; i < st.docTabs.size(); ++i)
        if (isDocTab(st.docTabs[i]) && samePath(st.docTabs[i].path, ref.docPath)) return (int)i;
    return -1;
}

void reconcileContextRefDocs(AppState& st) {
    for (auto& ref : st.pendingContextRefs) {
        const int idx = contextRefTabIndex(st, ref);
        if (idx < 0) continue;
        const auto& tab = st.docTabs[(size_t)idx];
        if (ref.docId != tab.docId) {
            ref.docId = tab.docId;   // rebound by path: pick ids belong to the old session
            ref.bodyId = ref.topoId = 0;
            ref.revision = -1;
        }
        if (!tab.title.empty()) ref.docName = tab.title;
        if (!tab.path.empty()) ref.docPath = tab.path;
    }
}

float contextRefChipWidth(const AppState& st, const core::ContextRef& ref, bool removable) {
    const float sc = ui::uiScale;
    const float lineH = ImGui::GetTextLineHeight();
    float w = 6.0f * sc * 2.0f + lineH + 4.0f * sc +
              ImGui::CalcTextSize(chipLabel(ref).c_str()).x +
              ImGui::CalcTextSize(chipDocSuffix(st, ref).c_str()).x;
    if (removable) w += 4.0f * sc + ImGui::CalcTextSize("\xC3\x97").x + 4.0f * sc;
    return w;
}

ContextChipAction drawContextRefChip(AppState& st, const core::ContextRef& ref, bool removable) {
    const ui::AdaptiveSurface& asurf = chatSurf();
    const float sc = ui::uiScale;
    const float lineH = ImGui::GetTextLineHeight();
    const float padX = 6.0f * sc, padY = 3.0f * sc, gap = 4.0f * sc;
    const std::string label = chipLabel(ref);
    const std::string suffix = chipDocSuffix(st, ref);
    const bool open = contextRefTabIndex(st, ref) >= 0;
    const float xW = removable ? gap + ImGui::CalcTextSize("\xC3\x97").x + gap : 0.0f;
    const float w = contextRefChipWidth(st, ref, removable);
    const float h = lineH + padY * 2.0f;
    const float inkA = open && !ref.resolving ? 1.0f : 0.55f;
    const ImVec4& tp = asurf.textPrimary;
    const ImU32 fill = ImGui::ColorConvertFloat4ToU32(ImVec4(tp.x, tp.y, tp.z, 0.08f));
    const ImU32 edge = ref.resolveError.empty()
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(tp.x, tp.y, tp.z, 0.35f))
        : ImGui::ColorConvertFloat4ToU32(semanticOnGlass(ui::palette.statusWarn, asurf));
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(ImVec4(tp.x, tp.y, tp.z, inkA));
    const ImU32 dim = ImGui::ColorConvertFloat4ToU32(ImVec4(tp.x, tp.y, tp.z, 0.55f * inkA));

    ContextChipAction action = ContextChipAction::None;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float round = ui::metrics::rounding(6.0f);
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), fill, round);
    dl->AddRect(p, ImVec2(p.x + w, p.y + h), edge, round, 0, ui::metrics::stroke(1.0f));

    ImGui::PushID(&ref);
    ImGui::InvisibleButton("##ctxchip", ImVec2(w - xW, h));
    if (ImGui::IsItemClicked()) action = ContextChipAction::Focus;
    if (ImGui::IsItemHovered()) {
        if (open) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("%s", chipTooltip(st, ref).c_str());
    }
    float x = p.x + padX;
    const IconUV icon = getIcon(chipIcon(ref), 20);
    if (icon.valid) {
        dl->AddImage(icon.texId, ImVec2(x, p.y + padY), ImVec2(x + lineH, p.y + padY + lineH),
                     icon.uv0, icon.uv1, ink);
    }
    x += lineH + gap;
    dl->AddText(ImVec2(x, p.y + padY), ink, label.c_str());
    x += ImGui::CalcTextSize(label.c_str()).x;
    if (!suffix.empty()) dl->AddText(ImVec2(x, p.y + padY), dim, suffix.c_str());
    if (removable) {
        ImGui::SameLine(0.0f, 0.0f);
        ImGui::InvisibleButton("##ctxchip_x", ImVec2(xW, h));
        const bool hot = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) action = ContextChipAction::Remove;
        if (hot) ImGui::SetTooltip("Detach");
        const ImVec2 xm = ImGui::GetItemRectMin();
        dl->AddText(ImVec2(xm.x + gap, p.y + padY), hot ? ink : dim, "\xC3\x97");
    }
    ImGui::PopID();
    return action;
}

void focusContextRef(AppState& st, const core::ContextRef& ref) {
    const int idx = contextRefTabIndex(st, ref);
    const std::string kindWord = ref.kind.empty() ? std::string("item") : ref.kind;
    if (idx < 0) {
        const std::string doc = ref.docName.empty() ? std::string("Its document") : "\"" + ref.docName + "\"";
        st.toast(ToastLevel::Info, doc + " is not open \xE2\x80\x94 open it to see this " + kindWord + ".");
        return;
    }
    if (st.activeWorkspace == AppState::Workspace::AI) st.activeWorkspace = AppState::Workspace::Model;
    const std::string docId = st.docTabs[(size_t)idx].docId;
    if (idx != st.activeTab || st.docMirror.docId() != docId) {
        s_focus.active = true;
        s_focus.ref = ref;
        s_focus.ref.docId = docId;
        s_focus.docId = docId;
        s_focus.since = ImGui::GetTime();
        st.switchToTab(idx);
        return;
    }
    s_focus.active = false;
    applyFocus(st, ref);
}

void pumpContextRefFocus(AppState& st) {
    if (!s_focus.active) return;
    if (ImGui::GetTime() - s_focus.since > 20.0) {   // the switch never landed
        s_focus.active = false;
        return;
    }
    if (st.docMirror.docId() != s_focus.docId || !st.docMirror.hasDoc()) return;
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
        st.docTabs[(size_t)st.activeTab].docId != s_focus.docId)
        return;
    // Topology focus reads the mesh caches, which land with the activation's mesh load.
    if (core::contextRefIsTopology(s_focus.ref) && st.faceNormalCache.empty()) return;
    s_focus.active = false;
    applyFocus(st, s_focus.ref);
}

}  // namespace ui
