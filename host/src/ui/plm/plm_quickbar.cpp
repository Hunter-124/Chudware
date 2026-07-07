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
            std::string p = it.value("path", "");
            if (baseName(p) != docBase) continue;
            q.haveItem  = true;
            q.itemId    = it.value("item_id", "");
            q.partNumber= it.value("part_number", "");
            q.rev       = it.is_object() && it.contains("rev") && it["rev"].is_string()
                        ? it["rev"].get<std::string>() : "";
            q.workingRev= it.value("working_rev", "");
            q.lifecycle = it.value("lifecycle", "");
            q.dirty     = it.value("dirty", false);
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
    // chip label: revision + lifecycle when the active doc is a tracked item
    std::string label = "PLM";
    if (q.haveItem) {
        std::string r = !q.rev.empty() ? q.rev : q.workingRev;
        label = "PLM " + (r.empty() ? "" : r + " \xc2\xb7 ") + q.lifecycle;
        if (q.dirty) label += " *";
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

}  // namespace ui
