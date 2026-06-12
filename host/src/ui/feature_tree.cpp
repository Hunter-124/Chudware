// FeatureManager tree panel (C16).
// Shows the SolidWorks-style left panel with fixed header nodes and the feature history.
// Reads AppState::docMirror for the document tree; calls AppState::loadDocMesh on rebuild.
//
// C37-E: FeatureManager overhaul — tree<->viewport selection sync (owner-map face highlights +
// scroll-to + bbox zoom), stability fixes (stable fids/folder ids, O(N) status lookup, canDoc
// gating), SolidWorks interactions (double-click edit, slow-double-click rename, Ctrl multi-select,
// per-row appearance/zoom/rollback), a real grabbable rollback bar, and SW-style sketch nesting.
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"

#include "feature_tree.h"
#include "icons.h"
#include "sketch_mode.h"   // C18 (Edit Sketch context entry)
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker
#include "cursors.h"        // C37-E: ResizeV cursor over the rollback bar

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../render/id_pass.h"
#include "../renderer.h"   // C34: clearViewportForNewDoc resets meshes

// Safe JSON string accessor (local copy avoids including app_state.h accessors).
static inline std::string jfld(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline bool jftbool(const nlohmann::json& o, const char* k, bool def = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : def;
}
// C37-E: tolerant number accessor (feature_status "ms" timing).
static inline double jftnum(const nlohmann::json& o, const char* k, double def = -1.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : def;
}

namespace ui {

// ---- icon helper ----
// Attempt to draw a named atlas icon; fall back to a text glyph if the atlas is absent.
// C37-E: `alpha` dims the icon (suppressed / rolled-back rows get a faded icon, item 6).
static void treeIcon(const char* name, const char* fallback, float size = 16.0f, float alpha = 1.0f) {
    IconUV iv = getIcon(name, 20);
    if (iv.valid) {
        ImGui::Image(iv.texId, ImVec2(size, size), iv.uv0, iv.uv1, ImVec4(1, 1, 1, alpha));
    } else {
        if (alpha < 1.0f) ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], alpha));
        ImGui::TextUnformatted(fallback);
        if (alpha < 1.0f) ImGui::PopStyleColor();
    }
}

// ---- Solid Bodies visibility helper ----
// Show/hide one renderer body slot. Host-side state only (AppState::bodyVisible) — no doc ops,
// so it is applied immediately (no s_pendingTreeOps detour needed). Hiding also drops any
// selection/hover that references the body: the id pass stops rendering it, so a stale highlight
// would point at geometry the user can no longer see or re-pick.
static void setBodyShown(AppState& st, int slot, bool shown) {
    if (slot < 0 || slot >= (int)st.bodyVisible.size()) return;
    st.bodyVisible[slot] = shown ? 1 : 0;
    if (!shown) {
        uint32_t bid = slot < (int)st.docBodyIds.size() ? st.docBodyIds[slot] : 0;
        bool selHasBody = false;
        for (const auto& it : st.selection.items())
            if (it.hit.body_id == bid) { selHasBody = true; break; }
        if (selHasBody) st.selection.clear();
        if (st.hover.body_id == bid) { st.hover = render::PickHit{}; st.pickStatus.clear(); }
    }
    st.applyBodyVisibility();
}

// ---- status badge (ok/warn/error/rolled-back) ----
// C37-E: a rolled-back feature (after the rollback bar) is NOT an error — it gets a distinct muted
// "paused" marker, never the red "!". Pass rolledBack=true for those rows.
static void featureBadge(bool ok, bool hasError, const std::string& errText,
                         const std::vector<std::string>& danglingRefs,
                         bool rolledBack = false) {
    if (rolledBack) {
        // Muted hollow ring — "not currently built" (distinct from ok/error/dangling).
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                   ui::palette.textDim[2], 0.75f));
        ImGui::TextUnformatted(u8"◦");  // hollow bullet = rolled back
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Rolled back (after the rollback bar — not built)");
        return;
    }
    if (ok && danglingRefs.empty()) {
        // Green check (just a color dot for compactness)
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.statusOk[0], ui::palette.statusOk[1],
                   ui::palette.statusOk[2], ui::palette.statusOk[3]));
        ImGui::TextUnformatted(u8"•");  // bullet
        ImGui::PopStyleColor();
    } else if (!ok) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                   ui::palette.statusErr[2], ui::palette.statusErr[3]));
        ImGui::TextUnformatted("!");
        ImGui::PopStyleColor();
        if (!errText.empty() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", errText.c_str());
    } else if (!danglingRefs.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1],
                   ui::palette.statusWarn[2], ui::palette.statusWarn[3]));
        ImGui::TextUnformatted("?");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            std::string tip = "Dangling refs:\n";
            for (const auto& r : danglingRefs) { tip += "  "; tip += r; tip += '\n'; }
            ImGui::SetTooltip("%s", tip.c_str());
        }
    }
    (void)hasError;
}

// ---- shared op applier (DocMirror ops -> rebuild -> reload mesh) ----
// Centralizes the apply_ops -> loadDocMesh pattern used by suppress/delete/reorder/rename/folder.
// Dependency violations (reorder/delete) are reported as a warning, leaving the tree unchanged.
//
// C25 CRASH FIX: DocMirror::applyOps REPLACES the mirror's tree json, but every caller here
// sits inside the draw code that iterates references INTO that json (drag-drop reorder,
// row context menus, Ctrl+Up/Down) — applying immediately is a use-after-free. applyTreeOps
// therefore only ENQUEUES; flushPendingTreeOps() applies after all tree reads are done
// (end of drawFeatureTree).
struct PendingTreeOps { nlohmann::json ops; std::string what; };
static std::vector<PendingTreeOps> s_pendingTreeOps;

static void applyTreeOpsNow(AppState& st, const nlohmann::json& ops, const char* what) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    auto resp = st.docMirror.applyOps(st.worker, ops);
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        if (resp.error_code == "dependency_exists") {
            st.addLog(std::string("[warn] ") + what +
                      ": blocked — a feature depends on this order");
            st.toast(ui::ToastLevel::Warn,
                     std::string(what) + " blocked: a feature depends on this order");
        } else {
            st.addLog(std::string("[err] ") + what + ": " + resp.error_message);
            // W1-B (item 2): surface the failure instead of only logging it. (Transport errors are
            // already toasted via the worker error sink; this covers method-level op rejections.)
            if (resp.error_code != "transport")
                st.toast(ui::ToastLevel::Error,
                         std::string(what) + " failed: " +
                         (resp.error_message.empty() ? resp.error_code : resp.error_message));
        }
    }
}

static void applyTreeOps(AppState& st, const nlohmann::json& ops, const char* what) {
    (void)st;
    s_pendingTreeOps.push_back({ops, what});
}

static void flushPendingTreeOps(AppState& st) {
    if (s_pendingTreeOps.empty()) return;
    auto pending = std::move(s_pendingTreeOps);
    s_pendingTreeOps.clear();
    for (const auto& p : pending) applyTreeOpsNow(st, p.ops, p.what.c_str());
}

// Inline-rename state: which feature id is being renamed + its edit buffer.
static std::string s_renamingId;
static char        s_renameBuf[128] = {0};

// C37-E: multi-selection (Ctrl+click). File-local; st.selectedFeatureId tracks the PRIMARY (last
// clicked). Cleared on a plain click, on doc change (featureTreeResetTransient), and whenever the
// selection is validated away. A row is "selected" if it is the primary OR in this set.
static std::unordered_set<std::string> s_multiSel;

// C37-E: slow-double-click rename arming. A second single-click on the already-primary row, with a
// gap in (0.4s, 2.0s), arms the inline editor (SolidWorks "click-pause-click to rename"). We record
// the time of the last single-click commit per row.
static std::string s_lastClickId;
static double      s_lastClickTime = -1.0;

// C37-E: hover-to-highlight is CLICK-ONLY in this revision (see DEVIATIONS). A transient hover tint
// would have to repaint st.featureHighlightFaces on enter and restore the click selection on leave
// every frame; in immediate mode that fought the click highlight and flickered, and the card
// explicitly permits click-only. So we do not track hover dwell.

// C37-E: rollback-bar drag state. While the user is dragging the bar handle we track it here so the
// drop computes the feature the bar should sit AFTER (or null = end). s_rbDropAfter is the fid the
// bar would land after given the cursor's current row (sentinel "\0end" = drop at the end).
static bool        s_rollbackDragging = false;
static std::string s_rbDropAfter;   // updated each frame while dragging; "" until a row is hit

// C37-E: per-row appearance picker target. The row's context menu stashes which body to recolor + a
// one-shot open flag; drawFeatureTree renders the picker popup (sibling) and turns a commit into a
// set_appearance op. File-local so one picker serves every row.
static std::string s_apprBody;     // body key ("b1") the picker targets, or empty
static bool        s_apprOpen = false;
static float       s_apprColor[3] = {0.71f, 0.76f, 0.82f};
static float       s_apprMetallic = 0.0f, s_apprRoughness = 0.5f;

// W1-B (item 5): drop the tree's in-progress transient state on a doc change. Declared in
// feature_tree.h; called from AppState::clearViewportForNewDoc and wholesale-replace paths.
void featureTreeResetTransient() {
    s_renamingId.clear();
    s_renameBuf[0] = '\0';
    s_pendingTreeOps.clear();
    s_multiSel.clear();                 // C37-E
    s_lastClickId.clear();              // C37-E
    s_lastClickTime = -1.0;
    s_rollbackDragging = false;
    s_rbDropAfter.clear();
    s_apprBody.clear();
    s_apprOpen = false;
}

// ---- C26: appearance color-picker popup ----
// W1-B (item 9): the MaterialPreset table + the appearance picker popup, formerly duplicated here
// and in viewport_panel.cpp, now live in ui/appearance_common.{h,cpp}. Both call ui::appearance-
// ColorPicker(); the CALLER opens the popup (ImGui::OpenPopup(popupId)) after stashing which body
// the edit targets, then turns a true return into a set_appearance op.

// ---- feature_status (ok / error / dangling / rolled-back / compile ms) ----
struct FStatus {
    bool ok = true;
    std::string error;
    std::vector<std::string> dangling;
    bool   rolledBack = false;   // C37-E: feature sits after the rollback bar (not built, not error)
    double ms = -1.0;            // C37-E: last compile time (ms); <0 = unknown
};

static FStatus parseStatusRow(const nlohmann::json& s) {
    FStatus fs;
    fs.ok = jftbool(s, "ok", true);
    fs.rolledBack = jftbool(s, "rolled_back", false);
    fs.ms = jftnum(s, "ms", -1.0);
    auto it = s.find("error");
    if (it != s.end() && it->is_string()) fs.error = it->get<std::string>();
    auto dr = s.find("dangling_refs");
    if (dr != s.end() && dr->is_array())
        for (const auto& d : *dr)
            if (d.is_string()) fs.dangling.push_back(d.get<std::string>());
    return fs;
}

// Linear scan — kept for the assembly tree (component count is small). The feature history uses the
// per-frame map below (buildStatusMap) to avoid an O(N^2) scan.
static FStatus getFeatureStatus(const nlohmann::json& statusArr, const std::string& fid) {
    if (!statusArr.is_array()) return {};
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        if (jfld(s, "id") != fid) continue;
        return parseStatusRow(s);
    }
    return {};
}

// C37-E (item 2): build one id->status map per frame so each feature row is an O(1) lookup instead
// of re-scanning the whole status array (the old O(N^2)).
static std::unordered_map<std::string, FStatus> buildStatusMap(const nlohmann::json& statusArr) {
    std::unordered_map<std::string, FStatus> m;
    if (!statusArr.is_array()) return m;
    m.reserve(statusArr.size());
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        std::string id = jfld(s, "id");
        if (!id.empty()) m.emplace(std::move(id), parseStatusRow(s));
    }
    return m;
}

// ---- C37-E: feature -> viewport highlight (owner map -> featureHighlightFaces) ----
// Fills st.featureHighlightFaces with the (body<<32)|face keys this feature owns. The viewport
// agent tints them. Tolerant of a missing owner map: the vector is simply left empty.
static void fillHighlightFaces(AppState& st, const std::string& fid,
                               std::vector<uint64_t>& out) {
    out.clear();
    const auto& om = st.docMirror.ownerMap();
    auto it = om.find(fid);
    if (it == om.end()) return;
    out.reserve(it->second.faces.size());
    for (const auto& bf : it->second.faces)
        out.push_back(((uint64_t)bf.first << 32) | (uint64_t)bf.second);
}

// Apply the persistent (click) highlight for a feature: clear topology selection (the tree owns the
// highlight now) and repopulate featureHighlightFaces. No toast when there's no owner map — an empty
// highlight is fine (the card says so).
static void selectFeatureInViewport(AppState& st, const std::string& fid) {
    st.selection.clear();
    fillHighlightFaces(st, fid, st.featureHighlightFaces);
}

static void clearFeatureHighlight(AppState& st) {
    st.featureHighlightFaces.clear();
}

// C38 (T1): public re-backfill of the feature highlight from the CURRENT owner map. The owner map's
// face tags are rebuilt on every applyOps/undo/redo/resync, so a previously-populated
// featureHighlightFaces (captured at click time) points at stale face ids and tints the wrong faces
// — or none. Re-derive it for the currently-selected feature; clear when nothing is selected or the
// feature is gone. Cheap (one map lookup + small vector fill); safe to call every rebuild.
void refreshFeatureHighlight(AppState& st) {
    if (st.selectedFeatureId.empty()) { st.featureHighlightFaces.clear(); return; }
    fillHighlightFaces(st, st.selectedFeatureId, st.featureHighlightFaces);
}

// C37-E (item 3): "Zoom to Feature" — union the owned faces'/bodies' bboxes (from the host-side
// bbox caches) and post a one-shot camera request. Toasts when nothing framable was found.
static void zoomToFeature(AppState& st, const std::string& fid) {
    const auto& om = st.docMirror.ownerMap();
    auto it = om.find(fid);
    bool any = false;
    float mn[3] = {0, 0, 0}, mx[3] = {0, 0, 0};
    auto acc = [&](const std::array<float, 6>& b) {
        if (!any) { for (int i = 0; i < 3; ++i) { mn[i] = b[i]; mx[i] = b[3 + i]; } any = true; }
        else {
            for (int i = 0; i < 3; ++i) {
                if (b[i]     < mn[i]) mn[i] = b[i];
                if (b[3 + i] > mx[i]) mx[i] = b[3 + i];
            }
        }
    };
    if (it != om.end()) {
        for (const auto& bf : it->second.faces) {
            uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
            auto fit = st.faceBBoxCache.find(key);
            if (fit != st.faceBBoxCache.end()) acc(fit->second);
        }
        // Fall back to the owned bodies' boxes (faces may not all be cached).
        if (!any)
            for (uint32_t b : it->second.bodies) {
                auto bit = st.bodyBBoxCache.find(b);
                if (bit != st.bodyBBoxCache.end()) acc(bit->second);
            }
    }
    if (!any) {
        st.toast(ui::ToastLevel::Info, "Zoom to Feature: no cached geometry for this feature");
        return;
    }
    for (int i = 0; i < 3; ++i) { st.zoomToBBoxMin[i] = mn[i]; st.zoomToBBoxMax[i] = mx[i]; }
    st.zoomToBBoxRequested = true;
}

// ---- draw one feature row ----
// All the data + behavior a single feature row needs. Returns the click outcome so the caller can
// update selection. The struct keeps the (formerly sprawling) parameter list readable.
struct RowCtx {
    std::string fid, name, type;
    const char* iconName = nullptr;
    const char* iconFallback = nullptr;
    bool suppressed = false;
    bool selected   = false;        // primary OR multi-selected
    bool rolledBack = false;        // after the rollback bar → greyed/italic, NOT error styling
    FStatus fs;                     // ok / error / dangling / ms
    int  ownerBodyCount = 0;        // for the tooltip
    bool nested = false;            // a sketch child node (indented under its consumer)
    bool canDoc = true;            // gates every mutating menu item + drag-reorder
    bool barAtEnd = true;          // hides "Roll to End" when already at the end
    std::string firstOwnedBody;    // body key for the per-row Appearance item ("b<n>")
    const char* convertLabel = nullptr;
    std::function<void()> convertCb = nullptr;
    // reorderCb(dragged, before): drop dragged immediately before `before` ("" = end).
    std::function<void(const std::string&, const std::string&)> reorderCb;
    const std::function<void(const std::string&)>* editCallback = nullptr;
};

// Outcomes of interacting with a row this frame (consumed by the caller).
struct RowResult {
    bool clicked     = false;   // plain left-click (select)
    bool ctrlClicked = false;   // Ctrl+left-click (toggle multi-select)
    bool doubleClick = false;   // open the edit form
    bool hovered     = false;
};

static RowResult featureRow(RowCtx& c, AppState& st) {
    RowResult res;
    const std::string& fid = c.fid;
    float iconW = 20.0f;
    if (c.nested) ImGui::Indent(14.0f);   // sketch child indent (visual nesting)
    // C37-E (item 6): suppressed/rolled-back rows get a faded icon (clearer than just the [S] tag).
    float iconAlpha = c.suppressed ? 0.45f : (c.rolledBack ? 0.6f : 1.0f);
    treeIcon(c.iconName, c.iconFallback, iconW, iconAlpha);
    ImGui::SameLine(0, 4);

    // ---- inline rename editor (replaces the label while active) ----
    if (s_renamingId == fid) {
        ImGui::SetNextItemWidth(-1);
        if (!ImGui::IsAnyItemActive()) ImGui::SetKeyboardFocusHere();
        bool commit = ImGui::InputText(("##rn_" + fid).c_str(), s_renameBuf, sizeof(s_renameBuf),
                                       ImGuiInputTextFlags_EnterReturnsTrue |
                                       ImGuiInputTextFlags_AutoSelectAll);
        bool cancel = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool deact  = ImGui::IsItemDeactivated();
        if (commit || cancel || deact) {
            std::string newName = s_renameBuf;
            std::string old = s_renamingId;
            s_renamingId.clear();
            // C37-E (item 2): verify the fid still exists in the live tree before sending; a rename
            // committed onto a feature that was deleted/reordered away would be a stale op.
            bool stillExists = false;
            {
                const nlohmann::json& tr = st.docMirror.tree();
                auto fa = tr.is_object() ? tr.find("features") : tr.end();
                if (tr.is_object() && fa != tr.end() && fa->is_array())
                    for (const auto& f : *fa)
                        if (f.is_object() && jfld(f, "id") == old) { stillExists = true; break; }
            }
            if (!cancel && !newName.empty() && newName != c.name) {
                if (stillExists) {
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op", "rename"}, {"id", old}, {"name", newName}});
                    applyTreeOps(st, ops, "rename");
                } else {
                    st.toast(ui::ToastLevel::Warn, "Rename dropped: feature no longer exists");
                }
            }
        }
        if (c.nested) ImGui::Unindent(14.0f);
        return res;
    }

    ImGuiTreeNodeFlags flags =
        ImGuiTreeNodeFlags_Leaf |
        ImGuiTreeNodeFlags_NoTreePushOnOpen |
        ImGuiTreeNodeFlags_SpanFullWidth;
    if (c.selected) flags |= ImGuiTreeNodeFlags_Selected;

    // Suppressed → dim heavily. Rolled back → dim + italic-ish look (we cannot truly italicize a
    // default font, so we use the lighter dim + the "(Rolled back)" cue in the badge/tooltip).
    bool dim = c.suppressed || c.rolledBack;
    if (dim) {
        float a = c.suppressed ? 0.5f : 0.62f;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                   ui::palette.textDim[2], a));
    }

    // Label: dimmed-icon suppressed cue handled via the tree icon; keep a compact [S] too.
    std::string label = c.name;
    if (c.suppressed) label += " [S]";
    label += "##ft_" + fid;

    ImGui::TreeNodeEx(label.c_str(), flags);

    if (dim) ImGui::PopStyleColor();

    // ---- click classification (defer the actual selection change to the caller) ----
    bool itemHovered = ImGui::IsItemHovered();
    res.hovered = itemHovered;
    if (ImGui::IsItemClicked(ImGuiMouseButton_Left)) {
        if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) res.doubleClick = true;
        else if (ImGui::GetIO().KeyCtrl)                        res.ctrlClicked = true;
        else                                                    res.clicked = true;
    }

    // ---- drag-drop reorder (gated on canDoc) ----
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropSource(ImGuiDragDropFlags_None)) {
        ImGui::SetDragDropPayload("FEATURE_ROW", fid.c_str(), fid.size() + 1);
        ImGui::Text("Move %s", c.name.c_str());
        ImGui::EndDragDropSource();
    }
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropTarget()) {
        // C37-E (item 3): draw the insertion indicator in the GAP at the real insertion position —
        // top edge if the cursor is in the upper half (drop BEFORE this row), bottom edge if in the
        // lower half (drop AFTER → before the next row).
        ImVec2 rmin = ImGui::GetItemRectMin(), rmax = ImGui::GetItemRectMax();
        float  mid  = (rmin.y + rmax.y) * 0.5f;
        bool   after = ImGui::GetIO().MousePos.y > mid;
        float  ly = after ? rmax.y : rmin.y;
        ImGui::GetWindowDrawList()->AddLine(ImVec2(rmin.x, ly), ImVec2(rmax.x, ly),
                                            ui::Palette::toU32(ui::palette.accent), 2.5f);
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            if (dragged != fid) {
                // reorderCb takes a "before" id. Dropping in this row's UPPER half = insert before
                // this row (pass fid). LOWER half = insert after this row, which we encode with the
                // "@after:<fid>" sentinel so the caller resolves the following feature's id.
                c.reorderCb(dragged, after ? ("@after:" + fid) : fid);
            }
        }
        ImGui::EndDragDropTarget();
    }

    // Context menu — MUST come while the TreeNode row is still the last item:
    // BeginPopupContextItem opens on right-click of the LAST item, and featureBadge() below
    // emits its own SameLine text item. With the old order the menu only opened when the user
    // right-clicked the tiny status dot, i.e. effectively never (C40 fix).
    if (ImGui::BeginPopupContextItem(("##ctx_" + fid).c_str())) {
        bool isSketch = c.iconName && std::strcmp(c.iconName, "sketch_node") == 0;
        bool multi    = s_multiSel.size() > 1 && s_multiSel.count(fid) > 0;
        if (!c.canDoc) ImGui::BeginDisabled();   // gate every mutating item while an agent run is busy

        if (isSketch) {
            if (ImGui::MenuItem("Edit Sketch")) {
                auto* sk = st.sketchMode;
                if (sk) sk->enterForFeature(st, fid);
            }
        }
        if (c.editCallback && *c.editCallback) {
            if (ImGui::MenuItem("Edit")) (*c.editCallback)(fid);
        } else {
            ImGui::BeginDisabled(); ImGui::MenuItem("Edit"); ImGui::EndDisabled();
        }
        if (ImGui::MenuItem("Rename")) {
            s_renamingId = fid;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
        }
        if (ImGui::MenuItem("Remove from folder")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_folder"}, {"id", fid}, {"folder", ""}}}), "set_folder");
        }

        ImGui::Separator();
        // C37-E (item 3): multi-selection applies Suppress/Unsuppress/Delete to ALL selected rows.
        if (multi) {
            int n = (int)s_multiSel.size();
            if (ImGui::MenuItem(("Suppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", true}});
                applyTreeOps(st, ops, "suppress");
            }
            if (ImGui::MenuItem(("Unsuppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", false}});
                applyTreeOps(st, ops, "unsuppress");
            }
            if (ImGui::MenuItem(("Delete " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
                applyTreeOps(st, ops, "delete");
            }
        } else {
            if (ImGui::MenuItem(c.suppressed ? "Unsuppress" : "Suppress")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_suppressed"}, {"id", fid}, {"value", !c.suppressed}}}), "suppress");
            }
        }

        if (c.convertCb && c.convertLabel) {
            ImGui::Separator();
            if (ImGui::MenuItem(c.convertLabel)) c.convertCb();
        }

        // C37-E (item 3): Zoom to Feature / Rollback to Here / Roll to End / Appearance.
        ImGui::Separator();
        if (ImGui::MenuItem("Zoom to Feature")) zoomToFeature(st, fid);
        if (ImGui::MenuItem("Rollback to Here")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_rollback"}, {"after", fid}}}), "rollback");
        }
        if (!c.barAtEnd) {
            if (ImGui::MenuItem("Roll to End")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_rollback"}, {"after", nullptr}}}), "rollback");
            }
        }
        if (!c.firstOwnedBody.empty()) {
            if (ImGui::MenuItem("Appearance...")) {
                s_apprBody = c.firstOwnedBody;
                s_apprColor[0] = 0.71f; s_apprColor[1] = 0.76f; s_apprColor[2] = 0.82f;
                s_apprMetallic = 0.0f; s_apprRoughness = 0.5f;
                s_apprOpen = true;   // a sibling popup opens next frame (see drawFeatureTree)
            }
        }

        ImGui::Separator();
        if (ImGui::MenuItem("Delete")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "delete_feature"}, {"id", fid}, {"cascade", false}}}), "delete");
        }
        if (!c.canDoc) ImGui::EndDisabled();
        ImGui::EndPopup();
    }

    featureBadge(c.fs.ok, !c.fs.error.empty(), c.fs.error, c.fs.dangling, c.rolledBack);

    // ---- rich tooltip (type · status · compile ms · owner body count) ----
    if (itemHovered && !ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
        std::string tip = "Type: " + (c.type.empty() ? std::string("feature") : c.type);
        if (c.suppressed) tip += "\nStatus: Suppressed";
        else if (c.rolledBack) tip += "\nStatus: Rolled back (after the bar)";
        else if (!c.fs.ok) tip += "\nStatus: Error" + (c.fs.error.empty() ? "" : (" — " + c.fs.error));
        else if (!c.fs.dangling.empty()) tip += "\nStatus: Dangling references";
        else tip += "\nStatus: OK";
        if (c.fs.ms >= 0.0) { char b[48]; std::snprintf(b, sizeof(b), "\nCompile: %.2f ms", c.fs.ms); tip += b; }
        tip += "\nBodies owned: " + std::to_string(c.ownerBodyCount);
        ImGui::SetTooltip("%s", tip.c_str());
    }

    if (c.nested) ImGui::Unindent(14.0f);
    return res;
}

// ---- feature type -> icon name ----
// C24: every type maps to an atlas name that actually exists (see host/assets/icons/atlas.json),
// so the FeatureManager shows a per-type glyph and the old 'datum_plane'/'feature' unknown-icon
// warnings are gone.
static const char* featureIcon(const std::string& type) {
    if (type == "sketch")           return "sketch_node";
    if (type == "extrude")          return "extrude_boss";
    if (type == "revolve")          return "revolve_boss";
    if (type == "sweep")            return "sweep";
    if (type == "loft")             return "loft";
    if (type == "fillet")           return "fillet";
    if (type == "chamfer")          return "chamfer";
    if (type == "shell")            return "shell";
    if (type == "draft")            return "draft";
    if (type == "rib")              return "rib";
    if (type == "hole")             return "hole_wizard";
    if (type == "linear_pattern")   return "linear_pattern";
    if (type == "circular_pattern") return "circular_pattern";
    if (type == "mirror")           return "mirror";
    if (type == "plane" || type == "datum_plane") return "plane";
    if (type == "axis")             return "axis";
    if (type == "point")            return "point";
    if (type == "import")           return "open";
    if (type == "script")           return "history";
    // Primitives (box/cylinder/sphere/cone/torus) and anything else: the generic part glyph.
    if (type == "box" || type == "cylinder" || type == "sphere" ||
        type == "cone" || type == "torus")
        return "part";
    return "part";
}
static const char* featureFallback(const std::string& type) {
    if (type == "sketch")   return "[S]";
    if (type == "extrude")  return "[E]";
    if (type == "fillet")   return "[F]";
    if (type == "chamfer")  return "[C]";
    return "[?]";
}

// ---- C20b: assembly tree (components + Mates folder) ----
// Renders an aicad.asm/1 doc: each component as a "(f)"-fixed-marked row (SolidWorks), then a
// Mates folder listing each mate as "Concentric1 (base<->peg)" with a suppress/delete context
// menu (asm_edit_mate / asm_delete_mate). Returns nothing; mutates the doc via DocMirror.
static void drawAssemblyTree(AppState& st, const nlohmann::json& tree,
                             const nlohmann::json& statusArr) {
    const auto& pal = ui::palette;

    // Map component id -> display name (for the mate captions).
    auto compName = [&](const std::string& cid) -> std::string {
        if (tree.contains("components") && tree["components"].is_array())
            for (const auto& c : tree["components"])
                if (c.is_object() && jfld(c, "id") == cid) {
                    std::string n = jfld(c, "name");
                    return n.empty() ? cid : n;
                }
        return cid;
    };

    auto reloadFrom = [&](const core::OpsResponse& r) {
        if (r.ok && !r.aicm_path.empty()) {
            std::string le; st.loadDocMesh(r.aicm_path, le);
            if (!le.empty()) st.addLog("[err] loadDocMesh: " + le);
        } else if (!r.ok) {
            st.addLog("[err] mate op: " + r.error_message);
        }
    };

    // ---- components ----
    int ci = 0;
    if (tree.contains("components") && tree["components"].is_array()) {
        for (const auto& c : tree["components"]) {
            if (!c.is_object()) { ++ci; continue; }
            std::string cid   = jfld(c, "id");
            std::string cname = jfld(c, "name");
            if (cname.empty()) cname = cid;
            bool fixed = jftbool(c, "fixed");
            bool suppressed = jftbool(c, "suppressed");
            // Component vs subassembly icon (we only have parts for now; subassemblies = C24).
            bool isAsm = false;
            if (c.contains("source") && c["source"].is_object()) {
                std::string p = jfld(c["source"], "path");
                std::string low = p; for (auto& ch : low) ch = (char)std::tolower((unsigned char)ch);
                isAsm = low.find(".asmstep") != std::string::npos;  // reserved; none today
            }
            FStatus fs = getFeatureStatus(statusArr, cid);

            if (ci % 2 == 0) {
                ImVec2 rowMin = ImGui::GetCursorScreenPos();
                ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x,
                              rowMin.y + ImGui::GetTextLineHeightWithSpacing());
                ImGui::GetWindowDrawList()->AddRectFilled(rowMin, rowMax,
                    Palette::toU32(pal.treeRowAlt));
            }
            treeIcon(isAsm ? "assembly" : "part", isAsm ? "[A]" : "[P]", 16.0f);
            ImGui::SameLine(0, 4);
            // SolidWorks marks fixed components with a "(f)" prefix.
            std::string label = (fixed ? "(f) " : "") + cname;
            if (suppressed) label += " [S]";
            ImGuiTreeNodeFlags lf = ImGuiTreeNodeFlags_Leaf |
                ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth;
            ImGui::TreeNodeEx((label + "##comp_" + cid).c_str(), lf);
            featureBadge(fs.ok, !fs.error.empty(), fs.error, fs.dangling);
            ++ci;
        }
    }

    ImGui::Separator();

    // ---- Mates folder ----
    treeIcon("mates_folder", "[M]", 16.0f);
    ImGui::SameLine(0, 4);
    ImGuiTreeNodeFlags ff = ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_SpanFullWidth;
    if (ImGui::TreeNodeEx("Mates##mates_folder", ff)) {
        if (tree.contains("mates") && tree["mates"].is_array()) {
            // Per-type running index so each mate gets a SolidWorks-style name "Concentric1".
            std::map<std::string, int> typeCount;
            int mi = 0;
            for (const auto& m : tree["mates"]) {
                if (!m.is_object()) { ++mi; continue; }
                std::string mid   = jfld(m, "id");
                std::string mtype = jfld(m, "type");
                bool msup = jftbool(m, "suppressed");
                int n = ++typeCount[mtype];
                std::string disp = mtype.empty() ? "Mate" : mtype;
                if (!disp.empty()) disp[0] = (char)std::toupper((unsigned char)disp[0]);
                disp += std::to_string(n);
                // "(base<->peg)" from the two refs' components.
                std::string pair;
                if (m.contains("refs") && m["refs"].is_array() && m["refs"].size() >= 2) {
                    std::string a = compName(jfld(m["refs"][0], "component"));
                    std::string b = compName(jfld(m["refs"][1], "component"));
                    pair = " (" + a + "<->" + b + ")";
                }
                std::string row = disp + pair;
                if (msup) row += " [S]";

                treeIcon("mate", "[m]", 16.0f);
                ImGui::SameLine(0, 4);
                if (msup) ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.5f));
                ImGuiTreeNodeFlags mf = ImGuiTreeNodeFlags_Leaf |
                    ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth;
                ImGui::TreeNodeEx((row + "##mate_" + mid).c_str(), mf);
                if (msup) ImGui::PopStyleColor();

                if (ImGui::BeginPopupContextItem(("##mctx_" + mid).c_str())) {
                    // C25: deferred via applyTreeOps — applying here would invalidate the
                    // tree json this loop iterates (same UAF as feature reorder).
                    if (ImGui::MenuItem(msup ? "Unsuppress" : "Suppress")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_edit_mate"},{"id",mid},{"suppressed",!msup}}}), "mate");
                    }
                    ImGui::Separator();
                    if (ImGui::MenuItem("Delete")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_delete_mate"},{"id",mid}}}), "mate");
                    }
                    ImGui::EndPopup();
                }
                ++mi;
            }
            if (tree["mates"].empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.7f));
                ImGui::TextUnformatted("  (no mates)");
                ImGui::PopStyleColor();
            }
        }
        ImGui::TreePop();
    }
}

// ---- C24: small icon-button used in the FeatureManager top toolbar (24px square) ----
// Returns true on click. Disabled buttons render dimmed and don't fire.
static bool toolbarIconBtn(const char* id, const char* iconName, const char* tooltip,
                           bool enabled) {
    const auto& pal = ui::palette;
    float btn = 24.0f * ui::uiScale;
    ImGui::PushID(id);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton("##tb", ImVec2(btn, btn)) && enabled;
    bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hov && enabled)
        dl->AddRectFilled(p0, ImVec2(p0.x + btn, p0.y + btn),
                          ui::Palette::toU32(pal.buttonHovered), 3.0f);
    IconUV iv = getIcon(iconName, 20);
    float iz = 18.0f * ui::uiScale, off = (btn - iz) * 0.5f;
    ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,150);
    dl->AddImage(iv.texId, ImVec2(p0.x + off, p0.y + off),
                 ImVec2(p0.x + off + iz, p0.y + off + iz), iv.uv0, iv.uv1, tint);
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    ImGui::PopID();
    return clicked;
}

// ---- "Name & group" — SolidWorks-style auto-naming + folder grouping ----
// Renames every feature to a conventional per-type name ("Boss-Extrude1", "Fillet2", ...) and
// sorts dress-up / pattern / reference / sketch features into folders. One batched op set, one
// rebuild. (A future revision can route this through the AI agent for semantic names.)
static void nameAndGroupFeatures(AppState& st) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return;

    std::map<std::string, int> counts;
    nlohmann::json ops = nlohmann::json::array();

    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        std::string fid = jfld(f, "id");
        std::string ty  = jfld(f, "type");
        std::string mode;
        if (f.contains("params") && f["params"].is_object())
            mode = jfld(f["params"], "mode");
        bool cut = (mode == "cut");

        std::string base, folder;
        if      (ty == "sketch")           { base = "Sketch";              folder = "Sketches"; }
        else if (ty == "extrude")          { base = cut ? "Cut-Extrude" : "Boss-Extrude"; }
        else if (ty == "revolve")          { base = cut ? "Cut-Revolve" : "Boss-Revolve"; }
        else if (ty == "sweep")            { base = cut ? "Cut-Sweep"   : "Boss-Sweep"; }
        else if (ty == "loft")             { base = cut ? "Cut-Loft"    : "Boss-Loft"; }
        else if (ty == "fillet")           { base = "Fillet";   folder = "Dress-up"; }
        else if (ty == "chamfer")          { base = "Chamfer";  folder = "Dress-up"; }
        else if (ty == "shell")            { base = "Shell";    folder = "Dress-up"; }
        else if (ty == "draft")            { base = "Draft";    folder = "Dress-up"; }
        else if (ty == "rib")              { base = "Rib";      folder = "Dress-up"; }
        else if (ty == "hole")             { base = "Hole"; }
        else if (ty == "linear_pattern")   { base = "LPattern"; folder = "Patterns"; }
        else if (ty == "circular_pattern") { base = "CPattern"; folder = "Patterns"; }
        else if (ty == "mirror")           { base = "Mirror";   folder = "Patterns"; }
        else if (ty == "plane" || ty == "datum_plane") { base = "Plane"; folder = "Reference"; }
        else if (ty == "axis")             { base = "Axis";  folder = "Reference"; }
        else if (ty == "point")            { base = "Point"; folder = "Reference"; }
        else                                { base = ty.empty() ? "Feature" : ty; }

        int n = ++counts[base];
        std::string name = base + std::to_string(n);
        ops.push_back({{"op", "rename"}, {"id", fid}, {"name", name}});
        ops.push_back({{"op", "set_folder"}, {"id", fid}, {"folder", folder}});
    }
    if (!ops.empty()) {
        applyTreeOps(st, ops, "name & group");
        st.addLog("[tree] named & grouped " + std::to_string(ops.size() / 2) + " features");
    }
}

// ---- C37-E (item 4): the rollback bar widget ----
// Draws a thick grabbable horizontal line at the CURRENT cursor position (the caller places it right
// after the bar feature's row, or after the last row when the bar is at the end). The bar is a
// proper drag interaction: hovering shows a vertical-resize cursor + a brighter line; dragging it and
// releasing over a row gap enqueues set_rollback{after: <fid above the drop gap> | null}. It also
// remains a DROP TARGET for a dragged FEATURE_ROW (drop-to-end reorder, kept from C24).
//
// While dragging, the per-row hit-test in the feature walk records s_rbDropAfter (the feature the
// bar would land after); the walk's release handler turns it into set_rollback. `enabled` (canDoc)
// gates the mutating feature-row drop. `barAfterFid` is unused here but documents placement.
static void drawRollbackBarLine(AppState& st, bool enabled, const std::string& /*barAfterFid*/) {
    const auto& pal = ui::palette;
    ImGui::Dummy(ImVec2(0, 2.0f * ui::uiScale));
    ImVec2 r0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    float  rsc = ui::uiScale;
    float  barH = 9.0f * rsc;          // grabbable band height
    // An invisible button across the whole band gives us hover + active (drag) state.
    ImGui::PushID("##rollback_bar");
    ImGui::InvisibleButton("##rb", ImVec2(w, barH));
    bool hovered = ImGui::IsItemHovered();
    bool active  = ImGui::IsItemActive();
    if (hovered || active) st.requestCursor(ui::CursorKind::ResizeV);
    if (active) s_rollbackDragging = true;

    ImDrawList* rdl = ImGui::GetWindowDrawList();
    float ly = r0.y + barH * 0.5f;
    ImU32 lineCol = ui::Palette::toU32((hovered || active) ? pal.accent : pal.accentMid);
    rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly), lineCol,
                 (hovered || active) ? 3.0f : 2.0f);
    float hw = 22.0f * rsc, hh = 7.0f * rsc, hx = r0.x + (w - hw) * 0.5f;
    rdl->AddRectFilled(ImVec2(hx, ly - hh * 0.5f), ImVec2(hx + hw, ly + hh * 0.5f),
                       ui::Palette::toU32(pal.accent), 3.0f);
    if (hovered && !active)
        ImGui::SetTooltip("Rollback bar — drag up/down to roll the model back/forward,\n"
                          "or drop a feature here to move it to the end");

    // Drop target: a dragged feature row landing on the bar goes to the end (kept from C24).
    if (enabled && ImGui::BeginDragDropTarget()) {
        rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly),
                     ui::Palette::toU32(pal.accent), 2.5f);
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            applyTreeOps(st, nlohmann::json::array(
                {{{"op", "reorder_feature"}, {"id", dragged}}}), "reorder");
        }
        ImGui::EndDragDropTarget();
    }
    ImGui::PopID();
}

// ---- C24: FeatureManager top strip — a SolidWorks-style tab row (tree active; properties /
// configurations tabs are visual stubs) plus a compact document toolbar (New Part / New Assembly /
// Open / Save / Undo / Redo) that replaces the old wide text-button strip at the panel bottom.
static void drawFeatureManagerTopStrip(AppState& st) {
    const auto& pal = ui::palette;
    bool canDoc = st.workerStarted && !st.job.running.load();

    // --- tab strip (tree | properties | configurations) ---
    ImDrawList* dl = ImGui::GetWindowDrawList();
    struct Tab { const char* icon; const char* tip; };
    static const Tab kTabs[] = {
        {"history",  "Feature tree"},
        {"material", "Properties"},
        {"settings", "Configurations"},
    };
    float tabBtn = 22.0f * ui::uiScale;
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine(0, 2);
        ImGui::PushID(i + 7700);
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::InvisibleButton("##fmtab", ImVec2(tabBtn, tabBtn));
        bool hov = ImGui::IsItemHovered();
        bool act = (i == 0);  // only the tree tab is functional
        if (act || hov)
            dl->AddRectFilled(p0, ImVec2(p0.x + tabBtn, p0.y + tabBtn),
                              ui::Palette::toU32(act ? pal.ribbonTabActive : pal.buttonHovered), 3.0f);
        if (act)
            dl->AddLine(ImVec2(p0.x + 2, p0.y + tabBtn - 1), ImVec2(p0.x + tabBtn - 2, p0.y + tabBtn - 1),
                        ui::Palette::toU32(pal.accent), 2.0f);
        IconUV iv = getIcon(kTabs[i].icon, 20);
        float iz = 16.0f * ui::uiScale, off = (tabBtn - iz) * 0.5f;
        dl->AddImage(iv.texId, ImVec2(p0.x + off, p0.y + off),
                     ImVec2(p0.x + off + iz, p0.y + off + iz), iv.uv0, iv.uv1,
                     act ? IM_COL32(255,255,255,255) : IM_COL32(190,190,190,180));
        if (hov) ImGui::SetTooltip("%s", kTabs[i].tip);
        ImGui::PopID();
    }
    // right-aligned document toolbar
    float nBtns = 6.0f, gap = 2.0f * ui::uiScale, bw = 24.0f * ui::uiScale;
    float toolW = nBtns * bw + (nBtns - 1) * gap;
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - toolW + ImGui::GetCursorPosX() -
                    ImGui::GetStyle().ItemSpacing.x);
    if (toolbarIconBtn("fm_new", "new_doc", "New Part", canDoc) && canDoc) {
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
            st.addLog("[doc] new part  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();   // C34: fresh part = clean viewport (planes state)
        }
        else st.addLog("[err] new part: " + err);
    }
    ImGui::SameLine(0, gap);
    if (toolbarIconBtn("fm_newasm", "assembly", "New Assembly", canDoc) && canDoc) {
        std::string err;
        if (st.docMirror.newAssembly(st.worker, err)) {
            st.addLog("[doc] new assembly  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();   // C34
            st.showRibbon = true; st.ribbonTab = 4;
        } else st.addLog("[err] new assembly: " + err);
    }
    ImGui::SameLine(0, gap);
    if (toolbarIconBtn("fm_open", "open", "Open...", canDoc) && canDoc)
        st.addLog("[doc] open: use the path field below the tree");
    ImGui::SameLine(0, gap);
    bool canSave = canDoc && st.docMirror.hasDoc();
    if (toolbarIconBtn("fm_save", "save", "Save...", canSave) && canSave)
        st.addLog("[doc] save: use the path field below the tree");
    ImGui::SameLine(0, gap);
    // W1-B (item 4): route through the shared AppState helper so the toolbar buttons and the global
    // Ctrl+Z / Ctrl+Y keys take the identical path (mesh refresh + owner map + confirmation toast).
    if (toolbarIconBtn("fm_undo", "undo", "Undo", canDoc) && canDoc) st.docUndo();
    ImGui::SameLine(0, gap);
    if (toolbarIconBtn("fm_redo", "redo", "Redo", canDoc) && canDoc) st.docRedo();

    // --- "Name & group" — auto-name features SolidWorks-style and sort into folders. ---
    // Shown only for a part doc that actually has features (not assemblies / empty docs).
    bool isPart = st.docMirror.hasDoc() &&
                  jfld(st.docMirror.tree(), "schema") != "aicad.asm/1";
    int featCount = 0;
    if (isPart) {
        const nlohmann::json& tr = st.docMirror.tree();
        auto fa = tr.find("features");
        if (fa != tr.end() && fa->is_array()) featCount = (int)fa->size();
    }
    if (isPart && featCount > 0) {
        ImGui::Spacing();
        bool can = canDoc;
        if (!can) ImGui::BeginDisabled();
        ImGui::PushStyleColor(ImGuiCol_Button, ui::Palette::toU32(pal.accentSoft));
        if (ImGui::Button("Name & group features", ImVec2(-1, 0)) && can)
            nameAndGroupFeatures(st);
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Auto-name every feature (Boss-Extrude1, Fillet2, ...) and\n"
                              "group dress-up / patterns / reference geometry into folders.");
        if (!can) ImGui::EndDisabled();
    }

    ImGui::Separator();
}

// ---- main draw function ----

void drawFeatureTree(AppState& st,
                     const std::function<void(const std::string&)>& editCallback) {
    if (!ImGui::Begin("FeatureManager")) { ImGui::End(); return; }

    const auto& pal = ui::palette;

    // C24: SolidWorks-style top strip (tab row + compact doc toolbar), replacing the old wide
    // New Part / Undo / Redo text buttons that used to live at the panel bottom.
    drawFeatureManagerTopStrip(st);

    // Panel background
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.panelBg[0], pal.panelBg[1], pal.panelBg[2], pal.panelBg[3]));

    // ---- document title ----
    const nlohmann::json& tree = st.docMirror.tree();
    std::string docName = "Part1";
    if (tree.is_object()) docName = jfld(tree, "name");
    if (docName.empty()) docName = "Part1";

    // C37-E (item 2): the SAME guard the top toolbar uses (st.workerStarted && !st.job.running).
    // Every mutating tree interaction — context menu, drag-reorder, Ctrl+Up/Down, rollback drag —
    // is gated on this so no op fires while an agent run is in flight.
    bool canDocTree = st.workerStarted && !st.job.running.load();

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.text[0], pal.text[1], pal.text[2], pal.text[3]));
    {
        ImGuiTreeNodeFlags rootFlags =
            ImGuiTreeNodeFlags_DefaultOpen |
            ImGuiTreeNodeFlags_SpanFullWidth;
        bool rootOpen = ImGui::TreeNodeEx(docName.c_str(), rootFlags);

        // C26: root-row "Appearance…" — sets the primary body's color (the common single-body
        // case). Body key "b1" matches the engine's per-body appearance key (session._write_aicm).
        // Routed through the DEFERRED applyTreeOps queue, like every other tree-originated op.
        {
            bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            static float s_rootColor[3] = {0.71f, 0.76f, 0.82f};
            static float s_rootMetallic = 0.0f, s_rootRoughness = 0.5f;  // C32-A material
            static bool  s_openRootPicker = false;
            if (!isAsmDoc && st.docMirror.hasDoc()) {
                if (ImGui::BeginPopupContextItem("##root_ctx")) {
                    if (ImGui::MenuItem("Appearance...")) {
                        s_rootColor[0] = 0.71f; s_rootColor[1] = 0.76f; s_rootColor[2] = 0.82f;
                        s_rootMetallic = 0.0f; s_rootRoughness = 0.5f;
                        s_openRootPicker = true;  // open the picker (a sibling popup) next
                    }
                    if (ImGui::MenuItem("Clear Appearance")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op", "set_appearance"}, {"id", "b1"}, {"color", nullptr}}}),
                            "set_appearance");
                    }
                    ImGui::EndPopup();
                }
                // Picker popup lives OUTSIDE the context menu (a sibling) to avoid nested-popup
                // ordering pitfalls; it is opened by the menu item via s_openRootPicker.
                if (s_openRootPicker) { ImGui::OpenPopup("##appearance_root"); s_openRootPicker = false; }
                if (appearanceColorPicker("##appearance_root", s_rootColor,
                                          &s_rootMetallic, &s_rootRoughness)) {
                    applyTreeOps(st, nlohmann::json::array({
                        {{"op", "set_appearance"}, {"id", "b1"},
                         {"color", {s_rootColor[0], s_rootColor[1], s_rootColor[2]}},
                         {"metallic", s_rootMetallic}, {"roughness", s_rootRoughness}}}),
                        "set_appearance");
                }
            }
        }

        if (rootOpen) {
            ImGui::PopStyleColor();  // text color

            // ---- fixed header nodes ----
            // Equations
            {
                int eqCount = 0;
                if (tree.is_object()) {
                    auto eq = tree.find("equations");
                    if (eq != tree.end() && eq->is_object()) {
                        auto gl = eq->find("globals");
                        if (gl != eq->end() && gl->is_array())
                            eqCount = (int)gl->size();
                    }
                }
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                char eqLabel[64];
                std::snprintf(eqLabel, sizeof(eqLabel), "Equations (%d)##eqhdr", eqCount);
                treeIcon("equations", "[=]", 16.0f);
                ImGui::SameLine(0, 4);
                ImGui::TreeNodeEx(eqLabel, f);
            }

            // Material
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                std::string mat = "Material (none)";
                if (tree.is_object()) {
                    auto m = tree.find("material");
                    if (m != tree.end() && m->is_object()) {
                        std::string key = jfld(*m, "key");
                        if (!key.empty()) mat = "Material: " + key;
                    }
                }
                treeIcon("material", "[M]", 16.0f);
                ImGui::SameLine(0, 4);
                ImGui::TreeNodeEx(("##mat_" + mat).c_str(), f | ImGuiTreeNodeFlags_NoTreePushOnOpen);
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1.0f));
                ImGui::TextUnformatted(mat.c_str());
                ImGui::PopStyleColor();
            }

            // ---- Solid Bodies folder (per-body hide/show, SolidWorks-style) ----
            // Part docs only, when the loaded doc mesh has at least one body. One row per
            // renderer body slot: click toggles visibility (same interaction as the plane rows
            // below); the context menu adds Hide/Show, Isolate (when every OTHER body is already
            // hidden it acts as SolidWorks Exit Isolate = Show All), and Show All Bodies. Pure
            // host-side state — see setBodyShown above.
            {
                bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
                int nBodies = (int)st.bodyVisible.size();
                if (!isAsmDoc && st.hasDocMesh && nBodies >= 1) {
                    char hdr[64];
                    std::snprintf(hdr, sizeof(hdr), "Solid Bodies (%d)##solid_bodies", nBodies);
                    treeIcon("mates_folder", "[F]", 16.0f);
                    ImGui::SameLine(0, 4);
                    bool open = ImGui::TreeNodeEx(hdr, ImGuiTreeNodeFlags_DefaultOpen |
                                                       ImGuiTreeNodeFlags_SpanFullWidth);
                    if (open) {
                        for (int bi = 0; bi < nBodies; ++bi) {
                            bool hidden = !st.bodyVisible[bi];
                            uint32_t bid = bi < (int)st.docBodyIds.size()
                                               ? st.docBodyIds[bi] : (uint32_t)bi + 1;
                            char bname[32];
                            std::snprintf(bname, sizeof(bname), "Body%u", bid);
                            ImGuiTreeNodeFlags bf = ImGuiTreeNodeFlags_Leaf |
                                                    ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                    ImGuiTreeNodeFlags_SpanFullWidth;
                            treeIcon("part", "[B]", 16.0f, hidden ? 0.45f : 1.0f);
                            ImGui::SameLine(0, 4);
                            if (hidden)
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(pal.textDim[0], pal.textDim[1],
                                           pal.textDim[2], 0.55f));
                            char rowId[48];
                            std::snprintf(rowId, sizeof(rowId), "%s##body%d", bname, bi);
                            ImGui::TreeNodeEx(rowId, bf);
                            if (hidden) ImGui::PopStyleColor();
                            // Capture row interactions NOW; BeginPopupContextItem binds to the
                            // LAST item, so it must come right after TreeNodeEx, before any
                            // other widget.
                            bool rowClicked = ImGui::IsItemClicked();
                            bool rowHovered = ImGui::IsItemHovered();
                            char ctxId[32];
                            std::snprintf(ctxId, sizeof(ctxId), "##body_ctx%d", bi);
                            if (ImGui::BeginPopupContextItem(ctxId)) {
                                if (ImGui::MenuItem(hidden ? "Show" : "Hide"))
                                    setBodyShown(st, bi, hidden);
                                if (ImGui::MenuItem("Isolate")) {
                                    bool othersAllHidden = true;
                                    for (int j = 0; j < nBodies; ++j)
                                        if (j != bi && st.bodyVisible[j]) {
                                            othersAllHidden = false; break;
                                        }
                                    if (othersAllHidden) {
                                        // Already isolated -> Exit Isolate = plain Show All.
                                        std::fill(st.bodyVisible.begin(),
                                                  st.bodyVisible.end(), (uint8_t)1);
                                        st.applyBodyVisibility();
                                    } else {
                                        for (int j = 0; j < nBodies; ++j)
                                            if (j != bi && st.bodyVisible[j])
                                                setBodyShown(st, j, false);
                                        if (!st.bodyVisible[bi]) setBodyShown(st, bi, true);
                                    }
                                }
                                if (ImGui::MenuItem("Show All Bodies")) {
                                    std::fill(st.bodyVisible.begin(),
                                              st.bodyVisible.end(), (uint8_t)1);
                                    st.applyBodyVisibility();
                                }
                                ImGui::EndPopup();
                            }
                            if (rowClicked) setBodyShown(st, bi, hidden);
                            if (rowHovered)
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", bname);
                        }
                        ImGui::TreePop();
                    }
                }
            }

            // Reference planes — click to show/hide in the viewport; the clicked plane is
            // emphasized (brighter) and the others dim (focus-follows-selection).
            static const char* kPlanes[] = {"Front Plane", "Top Plane", "Right Plane"};
            for (int pi = 0; pi < 3; ++pi) {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                if (st.planeEmphasis == pi) f |= ImGuiTreeNodeFlags_Selected;
                bool hidden = !st.showPlane[pi];
                treeIcon("plane", "[P]", 16.0f);
                ImGui::SameLine(0, 4);
                if (hidden)
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.55f));
                ImGui::TreeNodeEx((std::string(kPlanes[pi]) + "##plane" + std::to_string(pi)).c_str(), f);
                if (hidden) ImGui::PopStyleColor();
                if (ImGui::IsItemClicked()) {
                    if (st.sketchPlanePickActive && st.sketchMode) {
                        // Plane-pick in progress: clicking a plane in the tree picks it for the
                        // new sketch (SolidWorks behavior), instead of toggling visibility.
                        static const char* kPlaneSel[] = {"plane.front", "plane.top", "plane.right"};
                        st.sketchPlanePickActive = false;
                        st.sketchMode->createSketchOnPlane(st, kPlaneSel[pi]);
                    } else {
                        st.showPlane[pi] = !st.showPlane[pi];
                        st.planeEmphasis = st.showPlane[pi] ? pi : -1;
                    }
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(st.sketchPlanePickActive ? "Sketch on %s"
                                      : (hidden ? "Show %s" : "Hide %s"), kPlanes[pi]);
            }

            // Origin
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                treeIcon("origin", "[O]", 16.0f);
                ImGui::SameLine(0, 4);
                ImGui::TreeNodeEx("Origin##origin", f);
            }

            ImGui::Separator();

            // ---- feature history ----
            const nlohmann::json& statusArr = st.docMirror.featureStatus();
            std::string& selectedFeatureId = st.selectedFeatureId;

            // C20b: assemblies render components + a Mates folder instead of a feature history.
            bool isAssembly = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            if (isAssembly) {
                drawAssemblyTree(st, tree, statusArr);
            } else
            if (tree.is_object()) {
                auto feat = tree.find("features");
                if (feat != tree.end() && feat->is_array()) {
                    // C37-E (item 2): one status map per frame (kills the per-row O(N^2) scan).
                    std::unordered_map<std::string, FStatus> statusMap = buildStatusMap(statusArr);

                    // Ordered id list (drives selection validation, Ctrl+Up/Down, rollback math).
                    std::vector<std::string> order;
                    order.reserve(feat->size());
                    for (const auto& f : *feat)
                        if (f.is_object()) order.push_back(jfld(f, "id"));

                    // C37-E (item 2): validate the selection/multi-set against the live tree; drop
                    // anything that vanished (a delete/reorder/undo can orphan them) and clear the
                    // viewport highlight if the primary is gone.
                    {
                        std::unordered_set<std::string> live(order.begin(), order.end());
                        if (!selectedFeatureId.empty() && !live.count(selectedFeatureId)) {
                            selectedFeatureId.clear();
                            clearFeatureHighlight(st);
                        }
                        for (auto it = s_multiSel.begin(); it != s_multiSel.end();)
                            it = live.count(*it) ? std::next(it) : s_multiSel.erase(it);
                    }

                    // C37-E (item 4): rollback bar position. `rollback_after` = the feature the bar
                    // sits AFTER (null/absent = bar at end). Everything after it is "rolled back".
                    std::string rollbackAfter;
                    {
                        auto rb = tree.find("rollback_after");
                        if (rb != tree.end() && rb->is_string()) rollbackAfter = rb->get<std::string>();
                    }
                    int barPos = (int)order.size() - 1;   // index of the feature the bar sits after
                    if (!rollbackAfter.empty()) {
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == rollbackAfter) { barPos = i; break; }
                    }
                    bool barAtEnd = rollbackAfter.empty() || barPos >= (int)order.size() - 1;

                    // C37-E (item 5): sketch nesting. A sketch consumed by EXACTLY ONE downstream
                    // feature (via a region ref {"kind":"region","sketch":"fX"} or positions_sketch)
                    // nests under that consumer. Unconsumed / multiply-consumed sketches stay flat.
                    std::unordered_map<std::string, int> consumeCount;   // sketch fid -> #consumers
                    std::unordered_map<std::string, std::string> consumer;  // sketch fid -> consumer
                    std::unordered_map<std::string, std::vector<std::string>> childrenOf;  // consumer -> sketches
                    {
                        std::unordered_set<std::string> sketchIds;
                        for (const auto& f : *feat)
                            if (f.is_object() && jfld(f, "type") == "sketch")
                                sketchIds.insert(jfld(f, "id"));
                        // Recursively scan a feature's refs for sketch references. A sketch is
                        // "consumed" via (a) a region selector {"kind":"region","sketch":"fX"} or
                        // (b) a bare sketch-id string under a *_sketch / sketch / profile key (e.g.
                        // positions_sketch). We deliberately do NOT treat a bare sketch id under a
                        // generic "features" list (pattern/mirror) as consumption — patterns don't
                        // absorb sketches, and that would wrongly hide them.
                        std::function<void(const nlohmann::json&, const std::string&,
                                           std::unordered_set<std::string>&)> scan =
                            [&](const nlohmann::json& node, const std::string& key,
                                std::unordered_set<std::string>& seen) {
                            if (node.is_object()) {
                                if (jfld(node, "kind") == "region") {
                                    std::string sk = jfld(node, "sketch");
                                    if (!sk.empty() && sketchIds.count(sk)) seen.insert(sk);
                                }
                                for (auto it = node.begin(); it != node.end(); ++it)
                                    scan(it.value(), it.key(), seen);
                            } else if (node.is_array()) {
                                for (const auto& e : node) scan(e, key, seen);
                            } else if (node.is_string()) {
                                bool keyImpliesSketch =
                                    key.find("sketch") != std::string::npos || key == "profile";
                                std::string v = node.get<std::string>();
                                if (keyImpliesSketch && sketchIds.count(v)) seen.insert(v);
                            }
                        };
                        for (const auto& f : *feat) {
                            if (!f.is_object()) continue;
                            std::string fid = jfld(f, "id");
                            if (jfld(f, "type") == "sketch") continue;  // a sketch isn't a consumer
                            auto refs = f.find("refs");
                            if (refs == f.end()) continue;
                            std::unordered_set<std::string> seen;
                            scan(*refs, std::string(), seen);   // top-level refs object: no key yet
                            for (const auto& sk : seen) {
                                ++consumeCount[sk];
                                consumer[sk] = fid;  // last writer; only meaningful when count==1
                            }
                        }
                        for (const auto& kv : consumer)
                            if (consumeCount[kv.first] == 1)
                                childrenOf[kv.second].push_back(kv.first);
                    }
                    // A sketch is nested (hidden from the flat list, shown under its consumer) iff it
                    // has exactly one consumer.
                    auto isNestedSketch = [&](const std::string& fid) {
                        auto it = consumeCount.find(fid);
                        return it != consumeCount.end() && it->second == 1;
                    };

                    // Reorder op: drop draggedId before beforeId. A "@after:<id>" sentinel means drop
                    // AFTER <id> (i.e. before the feature following <id>, or end). "" = end.
                    auto reorderCb = [&st, &order](const std::string& dragged, const std::string& before) {
                        std::string beforeId = before;
                        if (before.rfind("@after:", 0) == 0) {
                            std::string anchor = before.substr(7);
                            beforeId.clear();
                            for (int i = 0; i < (int)order.size(); ++i)
                                if (order[i] == anchor) {
                                    if (i + 1 < (int)order.size()) beforeId = order[i + 1];
                                    break;
                                }
                        }
                        nlohmann::json op = {{"op", "reorder_feature"}, {"id", dragged}};
                        if (!beforeId.empty()) op["before"] = beforeId;  // omit => move to end
                        applyTreeOps(st, nlohmann::json::array({op}), "reorder");
                    };

                    // Owner-map → first owned body key ("b<n>") for the per-row Appearance item, and
                    // the owned-body count for the tooltip.
                    const auto& ownerMap = st.docMirror.ownerMap();
                    auto firstBodyKey = [&](const std::string& fid) -> std::string {
                        auto it = ownerMap.find(fid);
                        if (it == ownerMap.end() || it->second.bodies.empty()) return {};
                        return "b" + std::to_string(it->second.bodies.front());
                    };
                    auto bodyCount = [&](const std::string& fid) -> int {
                        auto it = ownerMap.find(fid);
                        return it == ownerMap.end() ? 0 : (int)it->second.bodies.size();
                    };

                    // ---- render one feature row (shared by flat + nested-sketch paths) ----
                    int rowIdx = 0;   // visual stripe index (stable across nesting)
                    auto drawOneRow = [&](const nlohmann::json& f, bool nested) {
                        std::string fid    = jfld(f, "id");
                        std::string fname  = jfld(f, "name");
                        std::string ftype  = jfld(f, "type");
                        if (fname.empty()) fname = ftype + "·" + fid;  // C37-E: stable fid, not index

                        FStatus fs;
                        { auto it = statusMap.find(fid); if (it != statusMap.end()) fs = it->second; }
                        bool suppressed = jftbool(f, "suppressed");
                        bool rolledBack = fs.rolledBack;   // authoritative (engine sets it)
                        bool sel = (selectedFeatureId == fid) || s_multiSel.count(fid) > 0;

                        // Row backgrounds (alt stripe + selection highlight).
                        ImVec2 rowMin = ImGui::GetCursorScreenPos();
                        float availW = ImGui::GetContentRegionAvail().x;
                        ImVec2 rowMax(rowMin.x + availW,
                                      rowMin.y + ImGui::GetTextLineHeightWithSpacing());
                        if (rowIdx % 2 == 0)
                            ImGui::GetWindowDrawList()->AddRectFilled(rowMin, rowMax,
                                Palette::toU32(pal.treeRowAlt));
                        if (sel) {
                            ImGui::GetWindowDrawList()->AddRectFilled(rowMin, rowMax,
                                Palette::toU32(pal.treeRowSelected));
                            if (selectedFeatureId == fid && st.treeScrollToSelection) {
                                ImGui::SetScrollHereY(0.5f);
                                st.treeScrollToSelection = false;
                            }
                        }
                        ++rowIdx;

                        // C27-C: convert fillet↔chamfer (kept).
                        const char* convertLabel = nullptr;
                        std::function<void()> convertCb = nullptr;
                        bool isFillet  = (ftype == "fillet");
                        bool isChamfer = (ftype == "chamfer");
                        if (isFillet || isChamfer) {
                            convertLabel = isFillet ? "Convert to Chamfer" : "Convert to Fillet";
                            nlohmann::json capturedFeat = f;
                            std::string capturedType = ftype, capturedFid = fid, nextId;
                            { bool found = false;
                              for (const auto& ff : *feat) {
                                  if (!ff.is_object()) continue;
                                  if (found) { nextId = jfld(ff, "id"); break; }
                                  if (jfld(ff, "id") == fid) found = true;
                              } }
                            convertCb = [&st, capturedFeat, capturedType, capturedFid, nextId]() {
                                std::string toType = (capturedType == "fillet") ? "chamfer" : "fillet";
                                nlohmann::json newParams = nlohmann::json::object();
                                if (capturedType == "fillet") {
                                    auto rIt = capturedFeat["params"].find("radius");
                                    newParams["length"] = (rIt != capturedFeat["params"].end())
                                        ? *rIt : nlohmann::json({{"value", 1.0}});
                                } else {
                                    auto lIt = capturedFeat["params"].find("length");
                                    newParams["radius"] = (lIt != capturedFeat["params"].end())
                                        ? *lIt : nlohmann::json({{"value", 2.0}});
                                }
                                nlohmann::json newFeat = {
                                    {"type", toType}, {"params", newParams},
                                    {"refs", capturedFeat.value("refs", nlohmann::json::object())},
                                };
                                nlohmann::json addOp = {{"op", "add_feature"}, {"feature", newFeat}};
                                if (!nextId.empty()) addOp["before"] = nextId;
                                applyTreeOps(st, nlohmann::json::array({
                                    {{"op", "delete_feature"}, {"id", capturedFid}, {"cascade", false}},
                                    addOp}), "convert");
                            };
                        }

                        RowCtx ctx;
                        ctx.fid = fid; ctx.name = fname; ctx.type = ftype;
                        ctx.iconName = featureIcon(ftype);
                        ctx.iconFallback = featureFallback(ftype);
                        ctx.suppressed = suppressed; ctx.selected = sel;
                        ctx.rolledBack = rolledBack; ctx.fs = fs;
                        ctx.ownerBodyCount = bodyCount(fid);
                        ctx.nested = nested;
                        ctx.canDoc = canDocTree;
                        ctx.barAtEnd = barAtEnd;
                        ctx.firstOwnedBody = firstBodyKey(fid);
                        ctx.convertLabel = convertLabel; ctx.convertCb = convertCb;
                        ctx.reorderCb = reorderCb;
                        ctx.editCallback = &editCallback;

                        RowResult rr = featureRow(ctx, st);

                        // ---- rollback-bar drag hit-test (item 4) ----
                        // While the bar is being dragged, find which row's gap the cursor is over so
                        // the release can compute "after: <fid>". A line is drawn at that gap.
                        if (s_rollbackDragging && !nested) {
                            ImVec2 imn = ImGui::GetItemRectMin(), imx = ImGui::GetItemRectMax();
                            float my = ImGui::GetIO().MousePos.y;
                            if (my >= imn.y && my <= imx.y) {
                                s_rbDropAfter = fid;  // drop the bar right after this feature
                                ImGui::GetWindowDrawList()->AddLine(
                                    ImVec2(imn.x, imx.y), ImVec2(imx.x, imx.y),
                                    ui::Palette::toU32(pal.accent), 2.5f);
                            } else if (my < imn.y && s_rbDropAfter.empty()) {
                                // cursor above the first row → roll back before everything (to start)
                                s_rbDropAfter = "\1start";
                            }
                        }

                        // ---- selection / interaction handling ----
                        if (rr.doubleClick) {
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                            if (ftype == "sketch") {
                                if (st.sketchMode) st.sketchMode->enterForFeature(st, fid);
                            } else if (editCallback) {
                                editCallback(fid);
                            }
                        } else if (rr.ctrlClicked) {
                            // Ctrl+click toggles this row in/out of the multi-set. The current
                            // primary joins the set first (so the FIRST ctrl-click on a 2nd row keeps
                            // both). Primary tracks the last-clicked; toggling the primary off moves
                            // it to any remaining member (or clears).
                            if (!selectedFeatureId.empty()) s_multiSel.insert(selectedFeatureId);
                            if (s_multiSel.count(fid)) {
                                s_multiSel.erase(fid);
                                if (selectedFeatureId == fid) {
                                    selectedFeatureId = s_multiSel.empty() ? std::string()
                                                                           : *s_multiSel.begin();
                                }
                            } else {
                                s_multiSel.insert(fid);
                                selectedFeatureId = fid;
                            }
                            selectFeatureInViewport(st, selectedFeatureId);
                        } else if (rr.clicked) {
                            // Slow-double-click rename: a 2nd plain click on the already-primary row
                            // within (0.4s, 2.0s) arms the inline editor.
                            double now = ImGui::GetTime();
                            bool armRename = (selectedFeatureId == fid && s_lastClickId == fid &&
                                              s_lastClickTime > 0 &&
                                              (now - s_lastClickTime) > 0.4 &&
                                              (now - s_lastClickTime) < 2.0);
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                            if (armRename) {
                                s_renamingId = fid;
                                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", fname.c_str());
                                s_lastClickTime = -1.0;
                            } else {
                                s_lastClickId = fid;
                                s_lastClickTime = now;
                            }
                        }
                    };

                    // fid the rollback bar currently sits AFTER (empty = end).
                    std::string barFid = (!barAtEnd && barPos >= 0 && barPos < (int)order.size())
                                       ? order[barPos] : std::string();

                    // Reset the bar drop-target each frame the bar is being dragged; rows below set
                    // it as the cursor passes over them. If no row claims it (cursor below the last
                    // row), it stays empty → set_rollback{after:null} = bar at the end.
                    if (s_rollbackDragging) s_rbDropAfter.clear();

                    // ---- walk the feature list ----
                    std::string openFolder;
                    bool folderOpen = false;
                    for (int i = 0; i < (int)feat->size(); ++i) {
                        const auto& f = (*feat)[i];
                        if (!f.is_object()) continue;
                        std::string fid    = jfld(f, "id");
                        std::string ftype  = jfld(f, "type");
                        std::string folder = jfld(f, "folder");

                        // Skip nested sketches in the flat pass — they render under their consumer.
                        if (ftype == "sketch" && isNestedSketch(fid)) continue;

                        // Folder grouping (C37-E item 2: ImGui id from the folder NAME, stable across
                        // reorders, not a per-frame counter).
                        if (folder != openFolder) {
                            if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
                            openFolder = folder;
                            if (!folder.empty()) {
                                treeIcon("mates_folder", "[F]", 16.0f);
                                ImGui::SameLine(0, 4);
                                folderOpen = ImGui::TreeNodeEx(
                                    (folder + "##fld_" + folder).c_str(),
                                    ImGuiTreeNodeFlags_DefaultOpen |
                                    ImGuiTreeNodeFlags_SpanFullWidth);
                            }
                        }
                        if (!openFolder.empty() && !folderOpen) continue;

                        drawOneRow(f, /*nested=*/false);

                        // Nested-sketch children: render absorbed sketches indented under this row.
                        auto kids = childrenOf.find(fid);
                        if (kids != childrenOf.end()) {
                            for (const auto& skId : kids->second) {
                                for (const auto& sf : *feat)
                                    if (sf.is_object() && jfld(sf, "id") == skId) {
                                        drawOneRow(sf, /*nested=*/true);
                                        break;
                                    }
                            }
                        }

                        // C37-E (item 4): draw the rollback bar inline right after the bar feature
                        // (and its absorbed sketches) when it sits mid-history.
                        if (!barFid.empty() && fid == barFid)
                            drawRollbackBarLine(st, canDocTree, barFid);
                    }
                    if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
                    // Bar at the end → draw it after the last row.
                    if (barAtEnd)
                        drawRollbackBarLine(st, canDocTree, std::string());

                    // ---- Ctrl+Up / Ctrl+Down: move the selected feature among its siblings ----
                    if (canDocTree && !selectedFeatureId.empty() &&
                        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
                        int p = -1;
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == selectedFeatureId) { p = i; break; }
                        if (p >= 0) {
                            if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_UpArrow) && p > 0)
                                reorderCb(selectedFeatureId, order[p - 1]);
                            else if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_DownArrow) &&
                                     p < (int)order.size() - 1) {
                                if (p + 2 < (int)order.size())
                                    reorderCb(selectedFeatureId, order[p + 2]);
                                else
                                    reorderCb(selectedFeatureId, "");  // already next-to-last -> end
                            }
                        }
                    }

                    // ---- Rollback bar drag release (C37-E item 4) ----
                    // The bar is rendered inline above. When a bar drag ends, enqueue set_rollback
                    // for the gap the cursor landed in: after the hovered feature, or null (= end)
                    // when below the last row, or null/first when above (roll fully back ≈ start →
                    // we encode "to start" as set_rollback{after: first feature} so at least the
                    // first feature stays, matching SolidWorks' minimum-one rolled state... actually
                    // SW allows rolling to the very top; we map "above first" to the first feature).
                    if (s_rollbackDragging && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        std::string target = s_rbDropAfter;
                        nlohmann::json after = nullptr;   // default = end
                        if (target == "\1start") {
                            after = order.empty() ? nlohmann::json(nullptr)
                                                  : nlohmann::json(order.front());
                        } else if (!target.empty()) {
                            // Dropping after the last feature = bar at end (null).
                            if (!order.empty() && target == order.back()) after = nullptr;
                            else after = target;
                        }
                        if (canDocTree)
                            applyTreeOps(st, nlohmann::json::array({
                                {{"op", "set_rollback"}, {"after", after}}}), "rollback");
                        s_rollbackDragging = false;
                        s_rbDropAfter.clear();
                    } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        // Mouse up without a drag in progress — clear stale hit state.
                        s_rbDropAfter.clear();
                    }
                } else if (s_rollbackDragging) {
                    // C38 (T2): the feature list vanished mid-drag (no walk loop, so the release
                    // handler above never runs). Clear the drag flag so it can't linger a frame and
                    // leave the bar visually "stuck" to the cursor.
                    s_rollbackDragging = false;
                    s_rbDropAfter.clear();
                }
            }

            ImGui::TreePop();
        } else {
            ImGui::PopStyleColor();  // text color
        }
    }

    ImGui::PopStyleColor();  // ChildBg

    // C37-E (item 3): per-row "Appearance..." picker. The row's context menu stashed the target body
    // key + a one-shot open flag; render the picker as a sibling popup (same pattern as the root
    // node) and turn a commit into a set_appearance op on that body.
    if (s_apprOpen && !s_apprBody.empty()) { ImGui::OpenPopup("##appearance_feat"); s_apprOpen = false; }
    if (!s_apprBody.empty()) {
        if (appearanceColorPicker("##appearance_feat", s_apprColor,
                                  &s_apprMetallic, &s_apprRoughness)) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_appearance"}, {"id", s_apprBody},
                 {"color", {s_apprColor[0], s_apprColor[1], s_apprColor[2]}},
                 {"metallic", s_apprMetallic}, {"roughness", s_apprRoughness}}}),
                "set_appearance");
            s_apprBody.clear();
        }
    }

    // ---- Save / Open path strip (the New/Undo/Redo/Save buttons live in the top toolbar now) ----
    bool canDoc = canDocTree;
    if (canDoc && st.docMirror.hasDoc()) {
        ImGui::Separator();
        static char savePath[512] = "";
        ImGui::SetNextItemWidth(-104.0f * ui::uiScale);
        ImGui::InputText("##savepath", savePath, sizeof(savePath));
        ImGui::SameLine();
        if (ImGui::Button("Save") && savePath[0]) {
            std::string err;
            if (st.docMirror.save(st.worker, savePath, err))
                st.addLog("[doc] saved: " + std::string(savePath));
            else
                st.addLog("[err] save: " + err);
        }
        ImGui::SameLine();
        if (ImGui::Button("Open") && savePath[0]) {
            std::string err;
            if (st.docMirror.open(st.worker, savePath, err)) {
                st.addLog("[doc] opened rev=" + std::to_string(st.docMirror.revision()));
                // If we get an aicm from the open response, load it (not typical for open).
            } else {
                st.addLog("[err] open: " + err);
            }
        }
    }

    // C25: apply ops queued during drawing — safe now, nothing reads the tree json below.
    flushPendingTreeOps(st);

    ImGui::End();
}

}  // namespace ui
