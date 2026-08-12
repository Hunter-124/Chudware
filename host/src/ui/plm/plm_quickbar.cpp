// PLM quickbar — see plm_quickbar.h.
#include "plm_quickbar.h"
#include "plm_hub.h"

#include <cstring>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui_theme.h"

namespace ui {
namespace {

// Per-document resolved item (event-driven; refetched only when the doc changes).
struct QuickState {
    std::string proj;        // project folder this state was resolved for
    std::string docBase;     // active document file name (basename)
    bool        loading = false;
    bool        haveItem = false;
    std::string itemId, partNumber, rev, workingRev, lifecycle;
    bool        dirty = false;
    bool        busy = false;      // a mutation is in flight
    char        msg[256] = {};
    char        relNote[256] = {};
    bool        showRelease = false;
};
static QuickState q;

std::string baseName(const std::string& p) {
    if (p.empty()) return "";
    size_t s = p.find_last_of("/\\");
    return s == std::string::npos ? p : p.substr(s + 1);
}

// Null-safe string field: old vaults emit null for part_number/rev/working_rev
// (items predating those fields) and json::value() throws on the type mismatch.
std::string jstr(const nlohmann::json& j, const char* key) {
    auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

nlohmann::json pathParam(AppState& st) {
    nlohmann::json p; p["path"] = st.plmProjectPath(); return p;
}

std::string activeDocPath(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        return st.docTabs[st.activeTab].path;
    return "";
}

// Refetch the active document's item when the (project, document) pair changes.
void resolve(AppState& st) {
    std::string proj = st.plmProjectPath();
    std::string docBase = baseName(activeDocPath(st));
    if (proj == q.proj && docBase == q.docBase) return;
    q.proj = proj; q.docBase = docBase;
    q.haveItem = false; q.itemId.clear(); q.showRelease = false;
    if (proj.empty() || docBase.empty() || q.loading) return;
    q.loading = true;
    st.rpc.call("plm_item_list", pathParam(st), [docBase](const core::RpcResult& r) {
        q.loading = false;
        std::string h;
        if (!plmOk(r, h)) return;
        auto items = r.result.find("items");
        if (items == r.result.end() || !items->is_array()) return;
        for (const auto& it : *items) {
            if (!it.is_object()) continue;
            if (baseName(jstr(it, "path")) != docBase) continue;
            q.haveItem  = true;
            q.itemId    = jstr(it, "item_id");
            q.partNumber= jstr(it, "part_number");
            q.rev       = jstr(it, "rev");
            q.workingRev= jstr(it, "working_rev");
            q.lifecycle = jstr(it, "lifecycle");
            q.dirty     = it.contains("dirty") && it["dirty"].is_boolean() && it["dirty"].get<bool>();
            break;
        }
    });
}

void refetch() {  // force a re-resolve next frame (after a mutation)
    q.proj.clear(); q.docBase.clear();
}

void discardDraft(AppState& st, std::string itemId) {
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    st.rpc.call("plm_discard_draft", p, [&st](const core::RpcResult& r) {
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        st.toast(ui::ToastLevel::Info, "Draft discarded — back to the released version");
        refetch();
    });
}

// Cut the checkpoint; if this was an auto-branch, offer a Discard-draft action.
void chainCheckpoint(AppState& st, bool branched, const std::string& newRev) {
    nlohmann::json p = pathParam(st);
    p["message"] = q.msg;
    std::string itemId = q.itemId;
    st.rpc.call("plm_checkpoint", p, [&st, branched, newRev, itemId](const core::RpcResult& r) {
        q.busy = false;
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        q.msg[0] = '\0';
        refetch();
        if (branched) {
            std::string m = "Now editing a new draft" +
                            (newRev.empty() ? std::string() : " (revision " + newRev + ")") +
                            " — the released version is safe.";
            ui::toastWithAction(st, ui::ToastLevel::Info, m, "Discard draft",
                                [itemId](AppState& s) { discardDraft(s, itemId); });
        } else {
            st.toast(ui::ToastLevel::Success, "Checkpoint saved");
        }
    });
}

void doCheckpoint(AppState& st) {
    if (q.busy) return;
    q.busy = true;
    // Silent auto-branch: editing a Released document starts a new-revision draft
    // first (Hunter's locked decision — never edit a released design in place).
    if (q.haveItem && q.lifecycle == "Released") {
        nlohmann::json p = pathParam(st);
        p["item_id"] = q.itemId;
        p["start_new_rev"] = true;
        st.rpc.call("plm_item_set", p, [&st](const core::RpcResult& r) {
            std::string h;
            if (!plmOk(r, h)) { q.busy = false; st.toast(ui::ToastLevel::Error, h); return; }
            std::string newRev = r.result.value("working_rev", "");
            chainCheckpoint(st, /*branched=*/true, newRev);
        });
        return;
    }
    chainCheckpoint(st, /*branched=*/false, "");
}

void doRelease(AppState& st) {
    if (q.busy || !q.haveItem) return;
    q.busy = true;
    nlohmann::json p = pathParam(st);
    p["item_id"] = q.itemId;
    p["message"] = q.relNote;
    st.rpc.call("plm_release", p, [&st](const core::RpcResult& r) {
        q.busy = false;
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        st.toast(ui::ToastLevel::Success,
                 "Released revision " + r.result.value("rev", std::string()));
        q.relNote[0] = '\0'; q.showRelease = false;
        refetch();
    });
}

}  // namespace

void drawPlmQuickChip(AppState& st) {
    if (st.activeWorkspace == AppState::Workspace::PLM) return;  // hub already showing
    if (!st.workerStarted || st.plmProjectPath().empty()) return;
    resolve(st);

    const auto& pal = ui::palette;
    // Chip label + hover copy. A naked "PLM" reads as a stray button, so the label always
    // carries context — the tracked item's revision/lifecycle when we have one, otherwise the
    // open project's name — and the tooltip says what clicking it does.
    std::string projPath = st.plmProjectPath();
    while (!projPath.empty() && (projPath.back() == '/' || projPath.back() == '\\'))
        projPath.pop_back();
    const std::string projName = baseName(projPath);
    std::string label = "PLM";
    std::string tip;
    if (q.haveItem) {
        std::string r = !q.rev.empty() ? q.rev : q.workingRev;
        label = "PLM " + (r.empty() ? "" : r + " \xc2\xb7 ") + q.lifecycle;
        if (q.dirty) label += " *";
        tip = "PLM \xe2\x80\x94 revision & lifecycle for this document.\n";
        if (!q.partNumber.empty()) tip += q.partNumber + "   ";
        tip += (r.empty() ? std::string("(no revision)") : r) + " \xc2\xb7 " + q.lifecycle + "\n";
        if (q.dirty) tip += "* uncommitted changes since the last checkpoint\n";
        if (!projName.empty()) tip += "Project: " + projName + "\n";
        tip += "Click for checkpoint / release actions.";
    } else {
        label = projName.empty() ? "PLM project" : "PLM \xc2\xb7 " + projName;
        if (q.loading) label += " \xe2\x80\xa6";
        tip = "PLM project open \xe2\x80\x94 this document is not tracked yet.\n";
        if (!projPath.empty()) tip += "Project: " + projPath + "\n";
        tip += "Click for project actions (checkpoint, open the Project Hub).";
    }

    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
    ImGui::TextUnformatted("|");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.20f));
    ImGui::PushStyleColor(ImGuiCol_Text,
        q.dirty ? ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1)
                : ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
    if (ImGui::SmallButton(label.c_str()))
        ImGui::OpenPopup("##plm_quick");
    ImGui::PopStyleColor(3);
    // Tooltip after the pops so the hover copy uses normal text color, not the chip's accent.
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("%s", tip.c_str());
    }

    if (ImGui::BeginPopup("##plm_quick")) {
        const float S = ui::uiScale;
        if (q.haveItem) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
            std::string rr = !q.rev.empty() ? q.rev : q.workingRev;
            ImGui::Text("%s   %s \xc2\xb7 %s", q.partNumber.c_str(), rr.c_str(),
                        q.lifecycle.c_str());
            ImGui::PopStyleColor();
            ImGui::Separator();
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
            ImGui::TextUnformatted("This document is not tracked yet.");
            ImGui::PopStyleColor();
            ImGui::Separator();
        }

        // quick checkpoint (whole project)
        ImGui::SetNextItemWidth(240 * S);
        bool enter = ImGui::InputTextWithHint("##qmsg", "checkpoint note (optional)",
            q.msg, sizeof(q.msg), ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
        if ((ImGui::Button("Checkpoint") || enter) && !q.busy) doCheckpoint(st);
        ImGui::PopStyleColor();

        // release (only meaningful for an In-work item)
        if (q.haveItem && q.lifecycle != "Released") {
            if (ImGui::SmallButton(q.showRelease ? "Cancel release" : "Release\xe2\x80\xa6"))
                q.showRelease = !q.showRelease;
            if (q.showRelease) {
                ImGui::SetNextItemWidth(240 * S);
                ImGui::InputTextWithHint("##qrel", "release note", q.relNote, sizeof(q.relNote));
                ImGui::SameLine();
                if (ImGui::Button("Release") && !q.busy) doRelease(st);
            }
        }

        ImGui::Separator();
        if (ImGui::SmallButton("Open Project Hub")) {
            st.activeWorkspace = AppState::Workspace::PLM;
            ImGui::CloseCurrentPopup();
        }
        if (q.busy) plmSpinner("Working");
        ImGui::EndPopup();
    }
}

void plmAutoRecordSave(AppState& st, const std::string& savedAbsPath) {
    if (!st.workerStarted) return;
    const std::string proj = st.plmProjectPath();
    if (proj.empty() || savedAbsPath.empty()) return;
    if (!g_plmHub.autoCommit) return;  // cached; default true before first fetch
    // The saved file must live under the project folder.
    std::string a = savedAbsPath, p = proj;
    for (char& c : a) if (c == '\\') c = '/';
    for (char& c : p) if (c == '\\') c = '/';
    if (!p.empty() && p.back() != '/') p += '/';
    if (a.rfind(p, 0) != 0) return;  // file must sit inside the project folder
    nlohmann::json params; params["path"] = proj;
    st.rpc.call("plm_save", params, [&st](const core::RpcResult& r) {
        std::string h;
        if (plmOk(r, h)) { refetch(); plmInvalidate(); return; }
        st.toast(ui::ToastLevel::Warn, "Couldn't record history: " + h);
    });
}

bool plmActiveDocPlmState(AppState& st, std::string& rev, std::string& lifecycle,
                          bool& dirty) {
    resolve(st);
    if (!q.haveItem) return false;
    rev = !q.rev.empty() ? q.rev : q.workingRev;
    lifecycle = q.lifecycle;
    dirty = q.dirty;
    return true;
}

}  // namespace ui
