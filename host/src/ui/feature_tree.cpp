// FeatureManager tree panel (C16).
// Shows the SolidWorks-style left panel with fixed header nodes and the feature history.
// Reads AppState::docMirror for the document tree; calls AppState::loadDocMesh on rebuild.
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "imgui.h"

#include "feature_tree.h"
#include "icons.h"
#include "sketch_mode.h"   // C18 (Edit Sketch context entry)

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../render/id_pass.h"

// Safe JSON string accessor (local copy avoids including app_state.h accessors).
static inline std::string jfld(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline bool jftbool(const nlohmann::json& o, const char* k, bool def = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : def;
}

namespace ui {

// ---- icon helper ----
// Attempt to draw a named atlas icon; fall back to a text glyph if the atlas is absent.
static void treeIcon(const char* name, const char* fallback, float size = 16.0f) {
    IconUV iv = getIcon(name, 20);
    if (iv.valid) {
        ImGui::Image(iv.texId, ImVec2(size, size), iv.uv0, iv.uv1);
    } else {
        ImGui::TextUnformatted(fallback);
    }
}

// ---- status badge (ok/warn/error) ----
static void featureBadge(bool ok, bool hasError, const std::string& errText,
                         const std::vector<std::string>& danglingRefs) {
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

// ---- draw one feature row ----
// Returns true if the row was left-clicked (selection).
static bool featureRow(const std::string& fid, const std::string& name, const char* iconName,
                       const char* iconFallback, bool suppressed, bool selected,
                       bool ok, bool hasError, const std::string& errText,
                       const std::vector<std::string>& danglingRefs,
                       const std::function<void(const std::string&)>& editCallback,
                       AppState& st) {
    ImGuiTreeNodeFlags flags =
        ImGuiTreeNodeFlags_Leaf |
        ImGuiTreeNodeFlags_NoTreePushOnOpen |
        ImGuiTreeNodeFlags_SpanFullWidth;
    if (selected) flags |= ImGuiTreeNodeFlags_Selected;

    // Suppress dimming
    if (suppressed) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                   ui::palette.textDim[2], 0.5f));
    }

    // Small icon before the label
    float iconW = 20.0f;
    ImGui::SetCursorPosX(ImGui::GetCursorPosX());
    treeIcon(iconName, iconFallback, iconW);
    ImGui::SameLine(0, 4);

    // Build the label string; add "[S]" suffix for suppressed
    std::string label = name;
    if (suppressed) label += " [S]";
    label += "##ft_" + fid;

    bool clicked = ImGui::TreeNodeEx(label.c_str(), flags);

    if (suppressed) ImGui::PopStyleColor();

    featureBadge(ok, hasError, errText, danglingRefs);

    // Context menu
    if (ImGui::BeginPopupContextItem(("##ctx_" + fid).c_str())) {
        // C18: sketch features get an "Edit Sketch" entry that enters sketch mode (camera lock +
        // tools). iconName == "sketch" identifies the row's feature type (see featureIcon()).
        bool isSketch = iconName && std::strcmp(iconName, "sketch_node") == 0;
        if (isSketch) {
            if (ImGui::MenuItem("Edit Sketch")) {
                auto* sk = static_cast<ui::SketchMode*>(st.sketchMode);
                if (sk) sk->enterForFeature(st, fid);
            }
        }
        // Edit callback (C17 wires this)
        if (editCallback) {
            if (ImGui::MenuItem("Edit")) editCallback(fid);
        } else {
            ImGui::BeginDisabled();
            ImGui::MenuItem("Edit");
            ImGui::EndDisabled();
        }
        // Rename (inline) — we use a small InputText below the menu
        if (ImGui::MenuItem("Rename")) {
            // Trigger rename mode by setting a flag via log (simple approach for M1)
            st.addLog("[tree] rename: " + fid + " (UI: click to rename)");
        }
        ImGui::Separator();
        if (ImGui::MenuItem(suppressed ? "Unsuppress" : "Suppress")) {
            if (st.docMirror.hasDoc() && st.workerStarted) {
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op", "set_suppressed"}, {"id", fid}, {"value", !suppressed}});
                auto resp = st.docMirror.applyOps(st.worker, ops);
                if (resp.ok && !resp.aicm_path.empty()) {
                    std::string lerr;
                    st.loadDocMesh(resp.aicm_path, lerr);
                    if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
                } else if (!resp.ok) {
                    st.addLog("[err] suppress: " + resp.error_message);
                }
            }
        }
        ImGui::Separator();
        if (ImGui::MenuItem("Delete")) {
            // Confirm before deleting (simple: just proceed for M1; dependency_exists returns error)
            if (st.docMirror.hasDoc() && st.workerStarted) {
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op", "delete_feature"}, {"id", fid}, {"cascade", false}});
                auto resp = st.docMirror.applyOps(st.worker, ops);
                if (resp.ok && !resp.aicm_path.empty()) {
                    std::string lerr;
                    st.loadDocMesh(resp.aicm_path, lerr);
                    if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
                } else if (!resp.ok) {
                    if (resp.error_code == "dependency_exists")
                        st.addLog("[warn] delete: feature has dependents (cascade needed)");
                    else
                        st.addLog("[err] delete: " + resp.error_message);
                }
            }
        }
        ImGui::EndPopup();
    }

    return clicked;
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

// ---- highlight feature's owned faces in the viewport ----
static void highlightFeature(AppState& st, const std::string& fid) {
    // Look up which faces in the loaded AICM are owned by fid.
    // The AICM's face_owners come from the worker's ops-response rebuild. For M1 we don't yet
    // store a face->owner map in the C++ layer, so we scan the last featureStatus to confirm
    // the feature exists, then select all faces of bodies whose owner matches.
    // If owner data isn't available in the AICM, skip with a note (as per the card).
    st.selection.clear();
    if (!st.hasDocMesh || !st.idPass) return;

    // For M1: we don't have a face-owner map in C++ yet (that's C17/C19 territory).
    // Log a note and skip — the card allows this: "if owner data isn't in the AICM, skip and note".
    st.addLog("[tree] face highlight for " + fid + ": owner map not yet in C++ layer (deferred)");
}

// ---- lookup feature_status for a feature id ----
struct FStatus {
    bool ok = true;
    std::string error;
    std::vector<std::string> dangling;
};

static FStatus getFeatureStatus(const nlohmann::json& statusArr, const std::string& fid) {
    FStatus fs;
    if (!statusArr.is_array()) return fs;
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        if (jfld(s, "id") != fid) continue;
        fs.ok = jftbool(s, "ok", true);
        auto it = s.find("error");
        if (it != s.end() && it->is_string()) fs.error = it->get<std::string>();
        auto dr = s.find("dangling_refs");
        if (dr != s.end() && dr->is_array())
            for (const auto& d : *dr)
                if (d.is_string()) fs.dangling.push_back(d.get<std::string>());
        return fs;
    }
    return fs;
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
                    if (ImGui::MenuItem(msup ? "Unsuppress" : "Suppress")) {
                        if (st.docMirror.hasDoc() && st.workerStarted) {
                            auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                                {{"op","asm_edit_mate"},{"id",mid},{"suppressed",!msup}}}));
                            reloadFrom(r);
                        }
                    }
                    ImGui::Separator();
                    if (ImGui::MenuItem("Delete")) {
                        if (st.docMirror.hasDoc() && st.workerStarted) {
                            auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                                {{"op","asm_delete_mate"},{"id",mid}}}));
                            reloadFrom(r);
                        }
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
    float btn = 24.0f;
    ImGui::PushID(id);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton("##tb", ImVec2(btn, btn)) && enabled;
    bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hov && enabled)
        dl->AddRectFilled(p0, ImVec2(p0.x + btn, p0.y + btn),
                          ui::Palette::toU32(pal.buttonHovered), 3.0f);
    IconUV iv = getIcon(iconName, 20);
    float iz = 18.0f, off = (btn - iz) * 0.5f;
    ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,150);
    dl->AddImage(iv.texId, ImVec2(p0.x + off, p0.y + off),
                 ImVec2(p0.x + off + iz, p0.y + off + iz), iv.uv0, iv.uv1, tint);
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    ImGui::PopID();
    return clicked;
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
    float tabBtn = 22.0f;
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
        float iz = 16.0f, off = (tabBtn - iz) * 0.5f;
        dl->AddImage(iv.texId, ImVec2(p0.x + off, p0.y + off),
                     ImVec2(p0.x + off + iz, p0.y + off + iz), iv.uv0, iv.uv1,
                     act ? IM_COL32(255,255,255,255) : IM_COL32(190,190,190,180));
        if (hov) ImGui::SetTooltip("%s", kTabs[i].tip);
        ImGui::PopID();
    }
    // right-aligned document toolbar
    float nBtns = 6.0f, gap = 2.0f, bw = 24.0f;
    float toolW = nBtns * bw + (nBtns - 1) * gap;
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - toolW + ImGui::GetCursorPosX() -
                    ImGui::GetStyle().ItemSpacing.x);
    if (toolbarIconBtn("fm_new", "new_doc", "New Part", canDoc) && canDoc) {
        std::string err;
        if (st.docMirror.newPart(st.worker, err))
            st.addLog("[doc] new part  rev=" + std::to_string(st.docMirror.revision()));
        else st.addLog("[err] new part: " + err);
    }
    ImGui::SameLine(0, gap);
    if (toolbarIconBtn("fm_newasm", "assembly", "New Assembly", canDoc) && canDoc) {
        std::string err;
        if (st.docMirror.newAssembly(st.worker, err)) {
            st.addLog("[doc] new assembly  rev=" + std::to_string(st.docMirror.revision()));
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
    if (toolbarIconBtn("fm_undo", "undo", "Undo", canDoc) && canDoc) {
        auto r = st.docMirror.undo(st.worker);
        if (r.ok) {
            st.addLog("[doc] undo  rev=" + std::to_string(r.revision));
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        } else st.addLog("[doc] undo: " + r.error_message);
    }
    ImGui::SameLine(0, gap);
    if (toolbarIconBtn("fm_redo", "redo", "Redo", canDoc) && canDoc) {
        auto r = st.docMirror.redo(st.worker);
        if (r.ok) {
            st.addLog("[doc] redo  rev=" + std::to_string(r.revision));
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        } else st.addLog("[doc] redo: " + r.error_message);
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

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.text[0], pal.text[1], pal.text[2], pal.text[3]));
    {
        ImGuiTreeNodeFlags rootFlags =
            ImGuiTreeNodeFlags_DefaultOpen |
            ImGuiTreeNodeFlags_SpanFullWidth;
        if (ImGui::TreeNodeEx(docName.c_str(), rootFlags)) {
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

            // Reference planes
            static const char* kPlanes[] = {"Front Plane", "Top Plane", "Right Plane"};
            static const char* kPlaneIcons[] = {"plane", "plane", "plane"};
            for (int pi = 0; pi < 3; ++pi) {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                treeIcon(kPlaneIcons[pi], "[P]", 16.0f);
                ImGui::SameLine(0, 4);
                ImGui::TreeNodeEx((std::string(kPlanes[pi]) + "##plane" + std::to_string(pi)).c_str(), f);
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
            static std::string selectedFeatureId;

            // C20b: assemblies render components + a Mates folder instead of a feature history.
            bool isAssembly = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            if (isAssembly) {
                drawAssemblyTree(st, tree, statusArr);
            } else
            if (tree.is_object()) {
                auto feat = tree.find("features");
                if (feat != tree.end() && feat->is_array()) {
                    int idx = 0;
                    for (const auto& f : *feat) {
                        if (!f.is_object()) { ++idx; continue; }
                        std::string fid  = jfld(f, "id");
                        std::string fname = jfld(f, "name");
                        std::string ftype = jfld(f, "type");
                        bool suppressed = jftbool(f, "suppressed");
                        if (fname.empty()) fname = ftype + std::to_string(idx + 1);

                        FStatus fs = getFeatureStatus(statusArr, fid);
                        bool sel = (selectedFeatureId == fid);

                        // Alternate row background
                        if (idx % 2 == 0) {
                            ImVec2 rowMin = ImGui::GetCursorScreenPos();
                            ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x,
                                          rowMin.y + ImGui::GetTextLineHeightWithSpacing());
                            ImGui::GetWindowDrawList()->AddRectFilled(
                                rowMin, rowMax,
                                Palette::toU32(pal.treeRowAlt));
                        }
                        if (sel) {
                            ImVec2 rowMin = ImGui::GetCursorScreenPos();
                            ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x,
                                          rowMin.y + ImGui::GetTextLineHeightWithSpacing());
                            ImGui::GetWindowDrawList()->AddRectFilled(
                                rowMin, rowMax,
                                Palette::toU32(pal.treeRowSelected));
                        }

                        bool clicked = featureRow(
                            fid, fname,
                            featureIcon(ftype), featureFallback(ftype),
                            suppressed, sel,
                            fs.ok, !fs.error.empty(), fs.error, fs.dangling,
                            editCallback, st);

                        if (clicked) {
                            selectedFeatureId = fid;
                            highlightFeature(st, fid);
                        }
                        ++idx;
                    }
                }
            }

            // ---- Rollback bar ----
            // C24: SolidWorks-style thin accent line with a centered grab handle, sitting at the
            // end of the feature history. (Dragging is not wired — visual affordance only.)
            {
                ImGui::Dummy(ImVec2(0, 2));
                ImVec2 r0 = ImGui::GetCursorScreenPos();
                float w = ImGui::GetContentRegionAvail().x;
                ImDrawList* rdl = ImGui::GetWindowDrawList();
                float ly = r0.y + 3.0f;
                rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly),
                             ui::Palette::toU32(pal.accentMid), 2.0f);
                // grab handle (a small rounded tab centered on the line)
                float hw = 18.0f, hh = 6.0f, hx = r0.x + (w - hw) * 0.5f;
                rdl->AddRectFilled(ImVec2(hx, ly - hh * 0.5f), ImVec2(hx + hw, ly + hh * 0.5f),
                                   ui::Palette::toU32(pal.accent), 3.0f);
                ImGui::Dummy(ImVec2(w, hh + 4.0f));
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("Rollback bar");
            }

            ImGui::TreePop();
        } else {
            ImGui::PopStyleColor();  // text color
        }
    }

    ImGui::PopStyleColor();  // ChildBg

    // ---- Save / Open path strip (the New/Undo/Redo/Save buttons live in the top toolbar now) ----
    bool canDoc = st.workerStarted && !st.job.running.load();
    if (canDoc && st.docMirror.hasDoc()) {
        ImGui::Separator();
        static char savePath[512] = "";
        ImGui::SetNextItemWidth(-104);
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

    ImGui::End();
}

}  // namespace ui
