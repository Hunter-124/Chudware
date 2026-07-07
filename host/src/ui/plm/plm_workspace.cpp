// PLM workspace — full-viewport body for the PLM title-bar tab.
// Vault status, checkpoint list + create, history timeline, release/lock indicators
// over services/plm. All RPC calls are async (st.rpc). Degrades to an informative
// empty-state when the PLM vault is not initialised.
#include "plm_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui_theme.h"

namespace ui {

// ---- module state ---------------------------------------------------------------
namespace {

struct PlmCheckpoint {
    std::string id;
    std::string message;
    std::string author;
    std::string timestamp;
    bool released = false;
    bool locked   = false;
};

struct PlmItem {
    std::string partNumber;
    std::string name;
    std::string revision;
    std::string state;   // "draft" | "released" | "locked" | "obsolete"
    std::string owner;
};

struct PlmVaultStatus {
    bool        initialised      = false;
    bool        hasActiveDoc     = false;
    std::string repoPath;
    std::string branch;
    std::string currentDocName;
    int         totalCheckpoints = 0;
    bool        dirty            = false;
};

struct PlmState {
    bool           statusLoaded  = false;
    bool           statusLoading = false;
    PlmVaultStatus vaultStatus;
    std::string    statusErr;

    bool                       checkpointsLoaded  = false;
    bool                       checkpointsLoading = false;
    std::vector<PlmCheckpoint> checkpoints;
    std::string                checkpointsErr;
    int                        selectedCheckpoint = -1;

    bool                 itemsLoaded  = false;
    bool                 itemsLoading = false;
    std::vector<PlmItem> items;
    std::string          itemsErr;

    char        newMsg[256] = {};
    bool        committing  = false;
    std::string commitErr;
    std::string commitOk;

    bool        releasing  = false;
    std::string releaseErr;
    std::string releaseOk;

    bool        restoring  = false;
    std::string restoreErr;

    bool firstFrame = true;
};

static PlmState s_plm;

// ---- helpers -------------------------------------------------------------------

static void plmRowPill(bool sel = false, float rows = 1.0f) {
    float rowH = ImGui::GetFrameHeight() * rows;
    ImVec2 p0  = ImGui::GetCursorScreenPos();
    float  w   = ImGui::GetContentRegionAvail().x;
    ImVec2 p1(p0.x + w, p0.y + rowH);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f,
                           false, sel, sel ? 0.42f : 0.20f);
}

static void sectionHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::TextUnformatted(label);
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1],
                   palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

static void emptyState(const char* icon, const char* msg, const char* sub = nullptr) {
    const float S = ui::uiScale;
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.35f));
    {
        float tw = ImGui::CalcTextSize(icon).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - tw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.5f));
        ImGui::TextUnformatted(icon);
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();
    {
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }
    if (sub) {
        float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - sw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::TextUnformatted(sub);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));
}

static void spinner(const char* prefix) {
    static const char* kF[] = {". . .", "o . .", ". o .", ". . o"};
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::Text("%s %s", prefix, kF[(int)(ImGui::GetTime() * 4.0) & 3]);
    ImGui::PopStyleColor();
}

static void stateBadge(const char* state) {
    ImVec4 col;
    if (std::strcmp(state, "released") == 0)
        col = ImVec4(palette.statusOk[0],  palette.statusOk[1],  palette.statusOk[2],  1.0f);
    else if (std::strcmp(state, "locked") == 0)
        col = ImVec4(palette.accent[0],    palette.accent[1],    palette.accent[2],    1.0f);
    else if (std::strcmp(state, "obsolete") == 0)
        col = ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                     palette.textDisabled[2], 1.0f);
    else
        col = ImVec4(palette.textDim[0],   palette.textDim[1],   palette.textDim[2],   1.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::Text("[%s]", state);
    ImGui::PopStyleColor();
}

// ---- async loaders -------------------------------------------------------------

static void loadStatus(AppState& st) {
    if (s_plm.statusLoaded || s_plm.statusLoading) return;
    s_plm.statusLoading = true;
    st.rpc.call("plm_status", {}, [](const core::RpcResult& r) {
        s_plm.statusLoading = false;
        s_plm.statusLoaded  = true;
        if (!r.ok) { s_plm.statusErr = r.err; return; }
        if (!r.result.is_object()) return;
        auto& v          = s_plm.vaultStatus;
        v.initialised    = r.result.value("initialised",      false);
        v.hasActiveDoc   = r.result.value("has_active_doc",   false);
        v.repoPath       = r.result.value("repo_path",        "");
        v.branch         = r.result.value("branch",           "main");
        v.currentDocName = r.result.value("doc_name",         "");
        v.totalCheckpoints = r.result.value("checkpoint_count", 0);
        v.dirty          = r.result.value("dirty",             false);
    });
}

static void loadCheckpoints(AppState& st) {
    if (s_plm.checkpointsLoaded || s_plm.checkpointsLoading) return;
    s_plm.checkpointsLoading = true;
    st.rpc.call("plm_history", {{"limit", 100}}, [](const core::RpcResult& r) {
        s_plm.checkpointsLoading = false;
        s_plm.checkpointsLoaded  = true;
        if (!r.ok) { s_plm.checkpointsErr = r.err; return; }
        if (!r.result.is_array()) return;
        s_plm.checkpoints.clear();
        for (auto& c : r.result) {
            PlmCheckpoint cp;
            cp.id        = c.value("id",        "");
            cp.message   = c.value("message",   "");
            cp.author    = c.value("author",    "");
            cp.timestamp = c.value("timestamp", "");
            cp.released  = c.value("released",  false);
            cp.locked    = c.value("locked",    false);
            s_plm.checkpoints.push_back(std::move(cp));
        }
    });
}

static void loadItems(AppState& st) {
    if (s_plm.itemsLoaded || s_plm.itemsLoading) return;
    s_plm.itemsLoading = true;
    st.rpc.call("plm_item_list", {}, [](const core::RpcResult& r) {
        s_plm.itemsLoading = false;
        s_plm.itemsLoaded  = true;
        if (!r.ok) { s_plm.itemsErr = r.err; return; }
        if (!r.result.is_array()) return;
        s_plm.items.clear();
        for (auto& it : r.result) {
            PlmItem item;
            item.partNumber = it.value("part_number", "");
            item.name       = it.value("name",        "");
            item.revision   = it.value("revision",    "A");
            item.state      = it.value("state",       "draft");
            item.owner      = it.value("owner",       "");
            s_plm.items.push_back(std::move(item));
        }
    });
}

// ---- sub-panels ----------------------------------------------------------------

static void drawCheckpointPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##plm_ckpts", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Checkpoints");

    // New checkpoint input + save button.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-1.0f);
    bool enter = ImGui::InputText("##plm_msg", s_plm.newMsg, sizeof(s_plm.newMsg),
                                  ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Checkpoint message — press Enter or click Save");

    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    bool doSave = ImGui::Button("Save Checkpoint", ImVec2(-1.0f, 0.0f)) || enter;
    ImGui::PopStyleColor(2);

    if (doSave && !s_plm.committing && s_plm.newMsg[0]) {
        s_plm.committing = true;
        s_plm.commitErr.clear();
        s_plm.commitOk.clear();
        nlohmann::json p;
        p["message"] = s_plm.newMsg;
        st.rpc.call("plm_save", p, [](const core::RpcResult& r) {
            s_plm.committing = false;
            if (r.ok) {
                s_plm.commitOk = "Saved " + r.result.value("id", "ok");
                s_plm.newMsg[0] = '\0';
                s_plm.checkpointsLoaded = false;
                s_plm.statusLoaded      = false;
            } else {
                s_plm.commitErr = r.err;
            }
        });
    }
    if (s_plm.committing) spinner("Saving");
    if (!s_plm.commitErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("%s", s_plm.commitErr.c_str());
        ImGui::PopStyleColor();
    }
    if (!s_plm.commitOk.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusOk[0], palette.statusOk[1],
                   palette.statusOk[2], 1.0f));
        ImGui::TextUnformatted(s_plm.commitOk.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    // Checkpoint list.
    if (s_plm.checkpointsLoading) {
        spinner("Loading");
    } else if (!s_plm.checkpointsErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", s_plm.checkpointsErr.c_str());
        ImGui::PopStyleColor();
    } else if (s_plm.checkpoints.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No checkpoints yet.");
        ImGui::TextUnformatted("Use the field above to save one.");
        ImGui::PopStyleColor();
    } else {
        int idx = 0;
        for (auto& cp : s_plm.checkpoints) {
            ImGui::PushID(cp.id.c_str());
            bool sel = (s_plm.selectedCheckpoint == idx);
            plmRowPill(sel, 2.2f);
            ImGui::BeginGroup();

            // Badge.
            if (cp.released) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusOk[0], palette.statusOk[1],
                           palette.statusOk[2], 1.0f));
                ImGui::TextUnformatted("[REL]");
                ImGui::PopStyleColor();
                ImGui::SameLine();
            } else if (cp.locked) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 1.0f));
                ImGui::TextUnformatted("[LCK]");
                ImGui::PopStyleColor();
                ImGui::SameLine();
            }
            ImGui::TextUnformatted(cp.message.empty() ? "<no message>" : cp.message.c_str());
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::Text("  %s  %s", cp.author.c_str(), cp.timestamp.c_str());
            ImGui::PopStyleColor();

            ImGui::EndGroup();
            if (ImGui::IsItemClicked())
                s_plm.selectedCheckpoint = sel ? -1 : idx;

            // Restore on hover of selected.
            if (sel && ImGui::IsItemHovered()) {
                float bx = ImGui::GetContentRegionAvail().x + ImGui::GetCursorPosX()
                           - 70.0f * S;
                ImGui::SameLine();
                ImGui::SetCursorPosX(bx);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.button[0], palette.button[1],
                           palette.button[2], 0.80f));
                if (!s_plm.restoring && ImGui::SmallButton("Restore")) {
                    s_plm.restoring  = true;
                    s_plm.restoreErr.clear();
                    nlohmann::json p2;
                    p2["checkpoint_id"] = cp.id;
                    st.rpc.call("plm_restore", p2, [](const core::RpcResult& r) {
                        s_plm.restoring = false;
                        if (!r.ok) s_plm.restoreErr = r.err;
                        else {
                            s_plm.checkpointsLoaded = false;
                            s_plm.statusLoaded      = false;
                        }
                    });
                }
                ImGui::PopStyleColor();
            }
            ImGui::Spacing();
            ImGui::PopID();
            ++idx;
        }
    }
    ImGui::EndChild();
}

static void drawCentrePane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##plm_centre", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    ImGui::PushStyleColor(ImGuiCol_Tab,
        ImVec4(palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 1.0f));
    ImGui::PushStyleColor(ImGuiCol_TabSelected,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.25f));
    if (ImGui::BeginTabBar("##plm_tabs")) {

        // ---- History tab -------------------------------------------------------
        if (ImGui::BeginTabItem("History")) {
            ImGui::Spacing();
            sectionHeader("History Timeline");
            if (s_plm.checkpoints.empty()) {
                emptyState("[~]", "No history",
                           "Save a checkpoint to start tracking.");
            } else {
                float lineX = ImGui::GetCursorScreenPos().x + 12.0f * S;
                float dotR  = 5.0f * S;
                ImDrawList* dl = ImGui::GetWindowDrawList();
                ImU32 lineCol = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.border[0], palette.border[1],
                           palette.border[2], palette.border[3]));
                ImU32 dotCol  = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 0.85f));
                ImU32 relCol  = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.statusOk[0], palette.statusOk[1],
                           palette.statusOk[2], 1.0f));

                float prevY = ImGui::GetCursorScreenPos().y;
                int idx = 0;
                for (auto& cp : s_plm.checkpoints) {
                    ImGui::PushID(cp.id.c_str());
                    bool sel = (s_plm.selectedCheckpoint == idx);
                    float rowY = ImGui::GetCursorScreenPos().y;
                    float midY = rowY + ImGui::GetFrameHeight() * 0.5f;

                    if (idx > 0)
                        dl->AddLine(ImVec2(lineX, prevY), ImVec2(lineX, midY),
                                    lineCol, 2.0f * S);
                    dl->AddCircleFilled(ImVec2(lineX, midY), dotR,
                                        (cp.released || cp.locked) ? relCol : dotCol);

                    ImGui::Indent(28.0f * S);
                    plmRowPill(sel, 1.8f);
                    ImGui::BeginGroup();
                    if (cp.released) {
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.statusOk[0], palette.statusOk[1],
                                   palette.statusOk[2], 1.0f));
                        ImGui::TextUnformatted("[REL]");
                        ImGui::PopStyleColor();
                        ImGui::SameLine();
                    } else if (cp.locked) {
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.accent[0], palette.accent[1],
                                   palette.accent[2], 1.0f));
                        ImGui::TextUnformatted("[LCK]");
                        ImGui::PopStyleColor();
                        ImGui::SameLine();
                    }
                    ImGui::TextUnformatted(cp.message.empty()
                                           ? "<no message>" : cp.message.c_str());
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                               palette.textDisabled[2], 1.0f));
                    ImGui::Text("  %s  %s", cp.author.c_str(), cp.timestamp.c_str());
                    ImGui::PopStyleColor();
                    ImGui::EndGroup();
                    if (ImGui::IsItemClicked())
                        s_plm.selectedCheckpoint = sel ? -1 : idx;
                    ImGui::Unindent(28.0f * S);
                    ImGui::Spacing();
                    ImGui::PopID();
                    prevY = midY;
                    ++idx;
                }
            }
            ImGui::EndTabItem();
        }

        // ---- Items / BOM tab ---------------------------------------------------
        if (ImGui::BeginTabItem("Items / BOM")) {
            ImGui::Spacing();
            sectionHeader("Items");
            if (s_plm.itemsLoading) {
                spinner("Loading");
            } else if (!s_plm.itemsErr.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusErr[0], palette.statusErr[1],
                           palette.statusErr[2], 1.0f));
                ImGui::TextWrapped("Error: %s", s_plm.itemsErr.c_str());
                ImGui::PopStyleColor();
            } else if (s_plm.items.empty()) {
                emptyState("(#)", "No items",
                           "Items appear here once a document is tracked by the vault.");
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDim[0], palette.textDim[1],
                           palette.textDim[2], 1.0f));
                ImGui::Text("%-12s  %-28s  %-6s  %-12s  %s",
                            "P/N", "Name", "Rev", "State", "Owner");
                ImGui::PopStyleColor();
                ImGui::Separator();
                ImGui::Spacing();
                for (auto& it : s_plm.items) {
                    ImGui::PushID(it.partNumber.c_str());
                    plmRowPill(false, 1.1f);
                    ImGui::Text("%-12.12s", it.partNumber.c_str());
                    ImGui::SameLine();
                    ImGui::Text("  %-28.28s", it.name.c_str());
                    ImGui::SameLine();
                    ImGui::TextDisabled("  %-6.6s  ", it.revision.c_str());
                    ImGui::SameLine();
                    stateBadge(it.state.c_str());
                    ImGui::SameLine();
                    ImGui::TextDisabled("  %s", it.owner.c_str());
                    ImGui::Spacing();
                    ImGui::PopID();
                }
            }
            ImGui::EndTabItem();
        }

        ImGui::EndTabBar();
    }
    ImGui::PopStyleColor(2);
    ImGui::EndChild();
}

static void drawVaultPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##plm_vault", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Vault Status");

    if (s_plm.statusLoading) {
        spinner("Loading");
    } else if (!s_plm.statusErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", s_plm.statusErr.c_str());
        ImGui::PopStyleColor();
    } else {
        auto& v = s_plm.vaultStatus;
        if (!v.initialised) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextUnformatted("Vault not initialised.");
            ImGui::TextUnformatted("Open a project to begin.");
            ImGui::PopStyleColor();
            ImGui::Spacing();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
            if (ImGui::Button("Initialise Vault", ImVec2(-1.0f, 0.0f)))
                st.rpc.call("plm_init", {}, [](const core::RpcResult&) {
                    s_plm.statusLoaded = false;
                });
            ImGui::PopStyleColor();
        } else {
            plmRowPill(false);
            ImGui::Text("Branch: ");
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
            ImGui::TextUnformatted(v.branch.c_str());
            ImGui::PopStyleColor();

            plmRowPill(false);
            ImGui::Text("Path:   ");
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(v.repoPath.empty() ? "(embedded)" : v.repoPath.c_str());
            ImGui::PopStyleColor();

            plmRowPill(false);
            ImGui::Text("Commits: %d", v.totalCheckpoints);
            if (v.dirty) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                           palette.statusWarn[2], 1.0f));
                ImGui::TextUnformatted("  [unsaved]");
                ImGui::PopStyleColor();
            }
            if (v.hasActiveDoc && !v.currentDocName.empty()) {
                ImGui::Spacing();
                plmRowPill(false);
                ImGui::Text("Doc: %s", v.currentDocName.c_str());
            }
        }
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Release Management");

    if (s_plm.releasing) {
        spinner("Working");
    } else {
        // Release button.
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.statusOk[0], palette.statusOk[1],
                   palette.statusOk[2], 0.75f));
        if (ImGui::Button("Release", ImVec2(-1.0f, 0.0f))) {
            s_plm.releasing = true;
            s_plm.releaseErr.clear();
            s_plm.releaseOk.clear();
            st.rpc.call("plm_release", {}, [](const core::RpcResult& r) {
                s_plm.releasing = false;
                if (r.ok) {
                    s_plm.releaseOk = "Released: " + r.result.value("revision", "");
                    s_plm.checkpointsLoaded = false;
                    s_plm.statusLoaded      = false;
                } else {
                    s_plm.releaseErr = r.err;
                }
            });
        }
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
            ImGui::SetTooltip("Create a formal release from the current checkpoint.");

        // Lock button.
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.65f));
        if (ImGui::Button("Lock", ImVec2(-1.0f, 0.0f))) {
            s_plm.releasing = true;
            s_plm.releaseErr.clear();
            s_plm.releaseOk.clear();
            nlohmann::json p;
            p["lock"] = true;
            st.rpc.call("plm_release", p, [](const core::RpcResult& r) {
                s_plm.releasing = false;
                if (r.ok) {
                    s_plm.releaseOk = "Locked.";
                    s_plm.checkpointsLoaded = false;
                } else {
                    s_plm.releaseErr = r.err;
                }
            });
        }
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
            ImGui::SetTooltip("Lock the current revision — no further edits allowed.");
    }

    if (!s_plm.releaseErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("%s", s_plm.releaseErr.c_str());
        ImGui::PopStyleColor();
    }
    if (!s_plm.releaseOk.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusOk[0], palette.statusOk[1],
                   palette.statusOk[2], 1.0f));
        ImGui::TextUnformatted(s_plm.releaseOk.c_str());
        ImGui::PopStyleColor();
    }
    if (!s_plm.restoreErr.empty()) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Restore failed: %s", s_plm.restoreErr.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void drawPlmWorkspace(AppState& st) {
    const float S = ui::uiScale;

    if (s_plm.firstFrame) {
        s_plm.firstFrame = false;
        if (st.workerStarted) {
            loadStatus(st);
            loadCheckpoints(st);
            loadItems(st);
        }
    }
    if (st.workerStarted) {
        if (!s_plm.statusLoaded      && !s_plm.statusLoading)      loadStatus(st);
        if (!s_plm.checkpointsLoaded && !s_plm.checkpointsLoading) loadCheckpoints(st);
        if (!s_plm.itemsLoaded       && !s_plm.itemsLoading)       loadItems(st);
    }

    if (!st.workerStarted) {
        emptyState("[V]", "PLM unavailable",
                   "The AI-CAD worker service is not running.");
        return;
    }

    // Three-column layout: [checkpoints 240] | [timeline+bom flex] | [vault 230]
    float totalW = ImGui::GetContentRegionAvail().x;
    const float leftW  = 240.0f * S;
    const float rightW = 230.0f * S;
    const float midW   = totalW - leftW - rightW - 12.0f * S;

    drawCheckpointPane(st, leftW);
    ImGui::SameLine();
    drawCentrePane(st, std::max(midW, 200.0f * S));
    ImGui::SameLine();
    drawVaultPane(st, rightW);
}

}  // namespace ui
