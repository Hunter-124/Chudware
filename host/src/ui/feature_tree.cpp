// FeatureManager tree panel (C16).
// Shows the SolidWorks-style left panel with fixed header nodes and the feature history.
// Reads AppState::docMirror for the document tree; calls AppState::loadDocMesh on rebuild.
#include <algorithm>
#include <cstdio>
#include <cstring>
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
        bool isSketch = iconName && std::strcmp(iconName, "sketch") == 0;
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
static const char* featureIcon(const std::string& type) {
    if (type == "extrude")          return "extrude";
    if (type == "revolve")          return "revolve";
    if (type == "sketch")           return "sketch";
    if (type == "fillet")           return "fillet";
    if (type == "chamfer")          return "chamfer";
    if (type == "shell")            return "shell";
    if (type == "linear_pattern")   return "linear_pattern";
    if (type == "circular_pattern") return "circular_pattern";
    if (type == "loft")             return "loft";
    if (type == "sweep")            return "sweep";
    if (type == "datum_plane")      return "datum_plane";
    return "feature";
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

// ---- main draw function ----

void drawFeatureTree(AppState& st,
                     const std::function<void(const std::string&)>& editCallback) {
    if (!ImGui::Begin("FeatureManager")) { ImGui::End(); return; }

    const auto& pal = ui::palette;

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
            static const char* kPlaneIcons[] = {"datum_plane", "datum_plane", "datum_plane"};
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

            // ---- Rollback bar (visual only, no ops tonight) ----
            ImGui::Separator();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.7f));
            ImGui::TextUnformatted("--- Rollback bar (drag: C17) ---");
            ImGui::PopStyleColor();

            ImGui::TreePop();
        } else {
            ImGui::PopStyleColor();  // text color
        }
    }

    ImGui::PopStyleColor();  // ChildBg

    // ---- File > New Part, Undo, Redo buttons (quick access strip while C17 is pending) ----
    ImGui::Separator();
    bool canDoc = st.workerStarted && !st.job.running.load();

    if (canDoc) {
        if (ImGui::Button("New Part")) {
            std::string err;
            if (st.docMirror.newPart(st.worker, err)) {
                st.addLog("[doc] new part  rev=" + std::to_string(st.docMirror.revision()));
            } else {
                st.addLog("[err] new part: " + err);
            }
        }
        ImGui::SameLine();
        if (ImGui::Button("Undo")) {
            auto r = st.docMirror.undo(st.worker);
            if (r.ok) {
                st.addLog("[doc] undo  rev=" + std::to_string(r.revision));
                if (!r.aicm_path.empty()) {
                    std::string lerr;
                    st.loadDocMesh(r.aicm_path, lerr);
                    if (!lerr.empty()) st.addLog("[err] undo mesh: " + lerr);
                }
            } else {
                st.addLog("[doc] undo: " + r.error_message);
            }
        }
        ImGui::SameLine();
        if (ImGui::Button("Redo")) {
            auto r = st.docMirror.redo(st.worker);
            if (r.ok) {
                st.addLog("[doc] redo  rev=" + std::to_string(r.revision));
                if (!r.aicm_path.empty()) {
                    std::string lerr;
                    st.loadDocMesh(r.aicm_path, lerr);
                    if (!lerr.empty()) st.addLog("[err] redo mesh: " + lerr);
                }
            } else {
                st.addLog("[doc] redo: " + r.error_message);
            }
        }
    } else {
        ImGui::BeginDisabled();
        ImGui::Button("New Part"); ImGui::SameLine();
        ImGui::Button("Undo"); ImGui::SameLine();
        ImGui::Button("Redo");
        ImGui::EndDisabled();
    }

    // ---- Save / Open strip ----
    if (canDoc && st.docMirror.hasDoc()) {
        static char savePath[512] = "";
        ImGui::SetNextItemWidth(-70);
        ImGui::InputText("##savepath", savePath, sizeof(savePath));
        ImGui::SameLine();
        if (ImGui::Button("Save") && savePath[0]) {
            std::string err;
            if (st.docMirror.save(st.worker, savePath, err))
                st.addLog("[doc] saved: " + std::string(savePath));
            else
                st.addLog("[err] save: " + err);
        }
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
