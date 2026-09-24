// PLM quickbar — see plm_quickbar.h.
#include "plm_quickbar.h"
#include "plm_hub.h"

#include <cstring>
#include <string>
#include <utility>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"

namespace ui {
namespace {

// Per-document resolved item (event-driven; refetched only when the doc changes).
struct QuickState {
    std::string proj;        // project folder this state was resolved for
    std::string docBase;     // active document file name (basename)
    bool        loading = false;
    unsigned long long itemsRevision = 0;
    bool        haveItem = false;
    std::string itemId, partNumber, rev, workingRev, lifecycle;
    bool        dirty = false;
    bool        busy = false;      // a mutation is in flight
    char        msg[256] = {};
    char        relNote[256] = {};
    bool        showRelease = false;
    // advisory-lock actions (contract §6.2/§6.3) — presence itself is read from
    // the g_plmHub team cache, not stored here
    bool        lockBusy = false;        // a lock/unlock is in flight
    bool        confirmOverride = false; // "Take over…" armed, awaiting confirmation
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

// Resolve against the hub's shared list. Changing document tabs only changes the
// local lookup; it must not reload every item in the project.
void resolve(AppState& st) {
    const std::string proj = st.plmProjectPath();
    const std::string docBase = baseName(activeDocPath(st));
    const bool changed = proj != q.proj || docBase != q.docBase;
    if (changed) {
        q.proj = proj; q.docBase = docBase;
        q.haveItem = false; q.itemId.clear(); q.showRelease = false;
        q.itemsRevision = 0;
    }
    q.loading = false;
    if (proj.empty() || docBase.empty() || !st.workerStarted) return;
    plmEnsureItems(st);
    const auto& H = g_plmHub;
    q.loading = H.itemsLoading;
    if (H.itemsProjectDir != proj || H.itemsGen != H.generation ||
        !H.itemsErr.empty() || q.itemsRevision == H.itemsRevision) return;
    q.itemsRevision = H.itemsRevision;
    q.haveItem = false;
    q.itemId.clear();
    for (const auto& it : H.items) {
        if (baseName(it.path) != docBase) continue;
        q.haveItem = true;
        q.itemId = it.itemId;
        q.partNumber = it.partNumber;
        q.rev = it.rev;
        q.workingRev = it.workingRev;
        q.lifecycle = it.lifecycle;
        q.dirty = it.dirty;
        break;
    }
}

void refetch() {  // force a re-resolve next frame (after a mutation)
    q.proj.clear(); q.docBase.clear();
    plmInvalidate(); // the quickbar and hub must observe the same post-mutation list
}

void discardDraft(AppState& st, nlohmann::json path, std::string itemId) {
    path["item_id"] = itemId;
    st.rpc.call("plm_discard_draft", path, [&st](const core::RpcResult& r) {
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        st.toast(ui::ToastLevel::Info, "Draft discarded — back to the released version");
        refetch();
    });
}

// Cut the checkpoint; if this was an auto-branch, offer a Discard-draft action.
void chainCheckpoint(AppState& st, nlohmann::json path, bool branched,
                     const std::string& newRev, std::string itemId, std::string message) {
    nlohmann::json p = path;
    p["message"] = message;
    st.rpc.call("plm_checkpoint", p,
                [&st, path = std::move(path), branched, newRev,
                 itemId = std::move(itemId), message = std::move(message)](const core::RpcResult& r) {
        q.busy = false;
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        if (st.plmProjectPath() == path.value("path", std::string()) &&
            message == q.msg) q.msg[0] = '\0';
        refetch();
        if (branched) {
            std::string m = "New draft in " + path.value("path", std::string()) +
                            (newRev.empty() ? std::string() : " (revision " + newRev + ")") +
                            " — the released version is safe.";
            ui::toastWithAction(st, ui::ToastLevel::Info, m, "Discard draft",
                                [path, itemId](AppState& s) { discardDraft(s, path, itemId); });
        } else {
            st.toast(ui::ToastLevel::Success,
                     st.plmProjectPath() == path.value("path", std::string())
                         ? "Checkpoint saved"
                         : "Checkpoint saved in " + path.value("path", std::string()));
        }
    });
}

void doCheckpoint(AppState& st) {
    if (q.busy) return;
    nlohmann::json path = pathParam(st);
    const std::string itemId = q.itemId;
    const std::string message = q.msg;
    q.busy = true;
    // Editing a Released document starts a new-revision draft first.
    if (q.haveItem && q.lifecycle == "Released") {
        nlohmann::json p = path;
        p["item_id"] = itemId;
        p["start_new_rev"] = true;
        st.rpc.call("plm_item_set", p,
                    [&st, path = std::move(path), itemId, message](const core::RpcResult& r) {
            std::string h;
            if (!plmOk(r, h)) { q.busy = false; st.toast(ui::ToastLevel::Error, h); return; }
            std::string newRev = r.result.value("working_rev", "");
            chainCheckpoint(st, path, /*branched=*/true, newRev, itemId, message);
        });
        return;
    }
    chainCheckpoint(st, std::move(path), /*branched=*/false, "", itemId, message);
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

// Advisory-lock actions (contract §6.2/§6.3). These run against the SHARED
// LOCATION and are a courtesy signal to teammates — completely unrelated to the
// property manager's local engine-daemon document hold. `force` overrides another
// live holder (plm_lock force=true) and is only reachable through an explicit
// confirmation step in the popup.
void doLockAction(AppState& st, bool acquire, bool force) {
    if (q.lockBusy || !q.haveItem || q.itemId.empty()) return;
    const PlmRemoteRow* rm = plmTeamLockRemote();
    if (!rm) return;
    q.lockBusy = true;
    nlohmann::json p = pathParam(st);
    p["item_id"] = q.itemId;
    p["remote_id"] = rm->id;
    if (force) p["force"] = true;
    st.rpc.call(acquire ? "plm_lock" : "plm_unlock", p,
                [&st, acquire, force](const core::RpcResult& r) {
        q.lockBusy = false;
        q.confirmOverride = false;
        std::string h;
        if (!plmOk(r, h)) {
            st.toast(ui::ToastLevel::Error, plmTeamFriendlyError(plmReason(r), h));
            return;
        }
        if (!acquire) {
            st.toast(ui::ToastLevel::Info, "Editing lock released");
        } else if (force) {
            std::string prior;
            if (auto o = r.result.find("overrode"); o != r.result.end() && o->is_object()) {
                prior = jstr(*o, "display");
                if (prior.empty()) prior = jstr(*o, "user_id");
            }
            st.toast(ui::ToastLevel::Info,
                     prior.empty()
                         ? std::string("You now hold the editing lock")
                         : "You now hold the editing lock — " + prior +
                               "'s lock was released");
        } else {
            st.toast(ui::ToastLevel::Success,
                     "You're editing this design — the team can see it");
        }
        plmInvalidate();   // locks changed → Team page + presence caches refetch
    });
}

}  // namespace

void drawPlmQuickChip(AppState& st) {
    if (!st.workerStarted || st.plmProjectPath().empty()) return;
    // The live co-editing session outlives any one workspace: poll + presence
    // pacing runs from the status bar every frame (bounded, async, never blocks).
    plmLiveTick(st);
    if (st.activeWorkspace == AppState::Workspace::PLM) return;  // hub already showing
    resolve(st);

    // Team presence: once the item resolves, keep the shared advisory-lock cache
    // warm (g_plmHub team cache — refetches at most once a minute) and find a
    // LIVE lock on this document. Advisory locks warn, they never block.
    if (q.haveItem) plmEnsureTeam(st);
    // Live co-editing (plm-server profile): while THIS item is joined, the
    // session is the presence signal — every advisory-lock control for it hides.
    const PlmRemoteRow* liveRemote = plmTeamLiveRemote();
    const bool liveJoined = q.haveItem && plmLiveJoinedItem(q.itemId);
    const PlmRemoteRow* lockRemote = plmTeamLockRemote();
    const PlmLockRow* lock = nullptr;
    if (lockRemote && q.haveItem && !liveJoined)
        for (const auto& l : g_plmHub.locks)
            if (l.itemId == q.itemId && plmLockLive(l)) { lock = &l; break; }
    const bool lockMine = lock && !lockRemote->identityUserId.empty() &&
                          lock->userId == lockRemote->identityUserId;

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

    // Presence chip: obvious, never blocking — a teammate holds the advisory
    // lock for THIS document (contract §6.2 "Sarah is editing this part").
    if (lock && !lockMine) {
        const std::string who = lock->display.empty() ? lock->userId : lock->display;
        std::string chipLabel = who + " is editing";
        std::string chipTip = who + " holds the editing lock for this design.\n";
        if (!lock->acquiredAt.empty())
            chipTip += "Since " + plmNiceTime(lock->acquiredAt);
        if (!lock->expiresAt.empty())
            chipTip += "  \xc2\xb7  until " + plmNiceTime(lock->expiresAt);
        chipTip += "\nYou can keep working — if you both edit, Sync keeps both versions.";
        ImGui::SameLine();
        kit::chip(chipLabel.c_str(), kit::Tone::Warn, /*dot=*/true, chipTip.c_str());
    }

    // Live session chips: an unmissable LIVE state plus who else is in the
    // session, straight in the status bar. Reconnect state is honest — the chip
    // never claims LIVE while the transport is down.
    if (liveJoined) {
        const auto& L = g_plmHub.live;
        ImGui::SameLine();
        if (L.connected) {
            std::string liveTip = "Live editing on " + L.remoteName +
                                  " \xe2\x80\x94 every change is ordered by the "
                                  "server and everyone sees one design.";
            kit::chip("LIVE", kit::Tone::Ok, /*dot=*/true, liveTip.c_str());
        } else {
            std::string liveTip = "Reconnecting to " + L.remoteName +
                                  " \xe2\x80\xa6 your edits resume once the "
                                  "connection is back.";
            kit::chip("LIVE \xc2\xb7 reconnecting", kit::Tone::Warn, /*dot=*/true,
                      liveTip.c_str());
        }
        int shown = 0;
        for (const auto& peer : L.peers) {
            if (shown == 2) break;
            ImGui::SameLine();
            std::string peerTip = peer.display + " is in this live session.";
            kit::chip(peer.display.c_str(), kit::Tone::Accent, /*dot=*/true,
                      peerTip.c_str());
            ++shown;
        }
        if ((int)L.peers.size() > shown) {
            ImGui::SameLine();
            char more[16];
            std::snprintf(more, sizeof(more), "+%d", (int)L.peers.size() - shown);
            kit::chip(more, kit::Tone::Accent, /*dot=*/false,
                      "More teammates are in this live session.");
        }
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

        // Live co-editing (plm-server profile — contracts/plm_live.md). Joining
        // binds this open document to the one authoritative server order; while
        // joined the session IS the presence signal, so the advisory-lock
        // controls below hide for this item.
        if (q.haveItem && (liveRemote || liveJoined)) {
            ImGui::Separator();
            auto& L = g_plmHub.live;
            if (liveJoined) {
                std::string line;
                if (!L.connected)
                    line = "Live editing \xe2\x80\x94 reconnecting to " +
                           L.remoteName + "\xe2\x80\xa6";
                else if (L.peers.empty())
                    line = "Live editing on " + L.remoteName +
                           " \xe2\x80\x94 you're the only one here";
                else if (L.peers.size() == 1)
                    line = "Live editing with " + L.peers.front().display;
                else
                    line = "Live editing with " +
                           std::to_string(L.peers.size()) + " teammates";
                const float* tone = L.connected ? pal.statusOk : pal.statusWarn;
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(tone[0], tone[1], tone[2], 1));
                ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
                if (!L.err.empty()) plmError(L.err);
                if (L.leaving) {
                    plmSpinner("Leaving live editing");
                } else if (ImGui::SmallButton("Leave live editing")) {
                    plmLiveLeave(st);
                }
            } else if (L.joining) {
                plmSpinner("Joining live editing");
            } else if (L.joined) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
                ImGui::TextUnformatted("Live editing is active on another document.");
                ImGui::PopStyleColor();
            } else {
                if (ImGui::SmallButton("Join live editing"))
                    plmLiveJoin(st, *liveRemote, q.itemId);
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Edit this design together on %s.\nEvery "
                                      "change is ordered by the server \xe2\x80\x94 "
                                      "everyone sees one design, live.",
                                      liveRemote->name.empty()
                                          ? liveRemote->id.c_str()
                                          : liveRemote->name.c_str());
                if (!L.err.empty()) plmError(L.err);
            }
        }

        // Team presence + the advisory lock for this document. Only shown when a
        // lock-capable shared location is configured; this is the teammate
        // courtesy signal, not the local engine document hold. Hidden while the
        // item is joined live — the session already says who is here.
        if (q.haveItem && lockRemote && !liveJoined) {
            ImGui::Separator();
            if (lock && !lockMine) {
                const std::string who =
                    lock->display.empty() ? lock->userId : lock->display;
                std::string line = who + " is editing this design";
                if (!lock->expiresAt.empty())
                    line += " (until " + plmNiceTime(lock->expiresAt) + ")";
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1));
                ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
                if (!q.confirmOverride) {
                    if (ImGui::SmallButton("Take over\xe2\x80\xa6") && !q.lockBusy)
                        q.confirmOverride = true;
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Releases their lock so yours takes its "
                                          "place. Asks you to confirm first.");
                } else {
                    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 300 * S);
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
                    ImGui::TextWrapped("This releases %s's editing lock. Nobody "
                                       "loses work — if you both edit, Sync keeps "
                                       "both versions.", who.c_str());
                    ImGui::PopStyleColor();
                    ImGui::PopTextWrapPos();
                    if (kit::dangerButton("Take over anyway", !q.lockBusy))
                        doLockAction(st, /*acquire=*/true, /*force=*/true);
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Never mind")) q.confirmOverride = false;
                }
            } else if (lock && lockMine) {
                std::string line = "You hold the editing lock";
                if (!lock->expiresAt.empty())
                    line += " (until " + plmNiceTime(lock->expiresAt) + ")";
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
                ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
                if (ImGui::SmallButton("Release editing lock") && !q.lockBusy)
                    doLockAction(st, /*acquire=*/false, /*force=*/false);
            } else {
                if (ImGui::SmallButton("Take the editing lock") && !q.lockBusy)
                    doLockAction(st, /*acquire=*/true, /*force=*/false);
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Shows teammates you're working on this "
                                      "design.\nA courtesy signal — it never "
                                      "blocks anyone.");
            }
            if (q.lockBusy) plmSpinner("Updating lock");
        }

        ImGui::Separator();
        if (ImGui::SmallButton("Open Project Hub")) {
            st.activeWorkspace = AppState::Workspace::PLM;
            ImGui::CloseCurrentPopup();
        }
        if (q.busy) plmSpinner("Working");
        ImGui::EndPopup();
    } else {
        q.confirmOverride = false;   // closing the popup disarms take-over
    }
}

void plmAutoRecordSave(AppState& st, const std::string& savedAbsPath) {
    if (!st.workerStarted) return;
    const std::string proj = st.plmProjectPath();
    if (proj.empty() || savedAbsPath.empty()) return;
    // The saved file must live under the project folder.
    std::string a = savedAbsPath, p = proj;
    for (char& c : a) if (c == '\\') c = '/';
    for (char& c : p) if (c == '\\') c = '/';
    if (!p.empty() && p.back() != '/') p += '/';
    if (a.rfind(p, 0) != 0) return;  // file must sit inside the project folder
    // Dirty badges follow disk saves even when automatic history is disabled.
    plmInvalidate();
    if (!g_plmHub.autoCommit) return;
    nlohmann::json params; params["path"] = proj;
    st.rpc.call("plm_save", params, [&st](const core::RpcResult& r) {
        std::string h;
        if (plmOk(r, h)) { refetch(); return; }
        st.toast(ui::ToastLevel::Warn, "Couldn't record history: " + h);
    });
}
void boxMirrorNotifySave(AppState& st, const std::string& savedAbsPath) {
    if (!st.workerStarted || !st.settings.boxEnabled || savedAbsPath.empty()) return;
    st.rpc.call("box_notify_save", nlohmann::json{{"path", savedAbsPath}},
                [](const core::RpcResult&) {});
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
