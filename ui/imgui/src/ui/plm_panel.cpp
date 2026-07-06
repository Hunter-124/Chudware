// T-R5: in-editor PLM GUI — history timeline panel, checkpoint/release flow,
// compare view, revision badges, released-doc silent auto-branch.
//
// Talks to services/plm/worker_rpc.py over the worker seam (same transport as
// cam_panel / bom_panel: synchronous st.worker.call on user actions, never per
// frame). View-model structs + grouping logic live in ui/abstract/plm_view.h.
//
// Ease-of-use law (contract aicad.plm/1 §8.1, NORMATIVE): no git vocabulary in
// any user-facing string in this file. Users see Project / History /
// Checkpoint / Release / Restore ("return to") / Compare only. Machine entry
// ids are passed to the worker but never rendered.
#include "ui/plm_panel.h"

#include "imgui.h"

#include "plm_view.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "theme/texture_cache.h"
#include "ui/panel_scaffold.h"
#include "ui/toast.h"
#include "ui/feature_tree.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <set>
#include <string>
#include <vector>

namespace ui {

namespace {

using aicad::ui::PlmEntryView;
using aicad::ui::PlmItemView;
using aicad::ui::PlmTimelineRow;

// toast action ids (ui/toast.h takeToastAction) — file-unique constants.
constexpr int kActDiscardDraft = 5501;

struct SessionChange {
    std::string when;   // "15:42"
    std::string desc;
    int count = 1;
};

struct CompareState {
    bool loaded = false;
    std::string title;        // "Bracket — 05 Jul 15:02 vs latest"
    std::string summary;      // T-R3 plain-language change list
    std::vector<std::string> lines;  // structured change rows
    std::string matchLine;    // "Geometry match: 98.7%" ("" = unknown)
    std::string visualHint;   // degraded reason ("" = images available)
    std::string imageA, imageB;
    std::string labelA, labelB;
};

struct PlmUI {
    bool panelOpen = false;
    bool compareOpen = false;

    std::string projectDir;   // "" = no project yet (doc never saved)
    std::string projectId;
    bool vaultAvailable = true;      // false => plm reports no_vault
    std::string unavailableHint;

    // status mirror (plm_status)
    std::vector<PlmItemView> items;
    std::string aiMode = "assisted";
    std::string numberPrefix, nextNumber;

    // history mirror (plm_history)
    std::vector<PlmEntryView> entries;   // newest first
    std::vector<PlmTimelineRow> rows;
    std::set<int> expanded;              // expanded group rows (key = row.first)
    std::string filterItemId;            // "" = whole project
    bool historyLoaded = false;
    std::string thumbDir;

    // checkpoint popover
    bool checkpointOpen = false;
    char msgBuf[512] = {0};
    std::string draftSource;   // "local_model" | "manual"
    std::string draftSummary;

    // release popover
    bool releaseOpen = false;
    char relMsgBuf[512] = {0};
    char partNumBuf[128] = {0};
    char prefixBuf[64] = {0};
    std::string releaseItemId;
    std::vector<std::string> preflightProblems;
    bool releaseNeedsConfirm = false;

    CompareState cmp;

    // released-doc auto-branch (T-R5 locked decision)
    std::string discardItemId;   // target of the toast's discard action
    bool branchInFlight = false;

    // session-changes log (the two-timelines confusion killer)
    std::vector<SessionChange> session;
    double lastSessionAt = 0.0;

    std::string statusLine;
    bool statusIsError = false;
};

PlmUI g;
PanelScaffoldState g_scaffold;

// ---- small utils -------------------------------------------------------------

std::string baseNameOf(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

std::string parentDirOf(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? std::string() : p.substr(0, cut);
}

// "2026-07-05T15:42:10Z" -> "05 Jul, 15:42" (pure string slicing — portable,
// no locale/timezone parsing; timeline times are UTC as stored).
std::string whenDisplay(const std::string& iso) {
    if (iso.size() < 16) return iso;
    static const char* kMon[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    int mon = (iso[5] - '0') * 10 + (iso[6] - '0');
    std::string day = iso.substr(8, 2);
    std::string hhmm = iso.substr(11, 5);
    if (mon < 1 || mon > 12) return iso;
    return day + " " + kMon[mon - 1] + ", " + hhmm;
}

std::string jstr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return {};
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

// ---- worker transport --------------------------------------------------------

// Synchronous plm_* call. Transport failure or an {ok:false} envelope both
// land in g.statusLine (plain language, contract hint text).
bool plmCall(app::AppState& st, const char* method, const nlohmann::json& params,
             nlohmann::json& out, int timeoutMs = 30000) {
    if (!st.workerStarted) st.tryStartWorker();
    if (!st.workerStarted) {
        g.statusLine = "history is unavailable until the engine starts";
        g.statusIsError = true;
        return false;
    }
    std::string err;
    if (!st.worker.call(method, params, out, err, timeoutMs)) {
        g.statusLine = err.empty() ? std::string("history service did not answer") : err;
        g.statusIsError = true;
        return false;
    }
    if (!out.value("ok", false)) {
        const std::string reason = out.value("reason", std::string());
        const std::string hint = out.value("hint", std::string());
        if (reason == "no_vault") {
            g.vaultAvailable = false;
            g.unavailableHint = hint;
        }
        g.statusLine = hint.empty() ? ("could not complete that (" + reason + ")") : hint;
        g.statusIsError = true;
        return false;
    }
    g.statusIsError = false;
    return true;
}

// ---- mirrors -----------------------------------------------------------------

void refreshStatus(app::AppState& st) {
    if (g.projectDir.empty()) return;
    nlohmann::json r;
    if (!plmCall(st, "plm_status", {{"path", g.projectDir}}, r)) return;
    g.items.clear();
    std::set<std::string> dirty;
    if (r.contains("dirty"))
        for (const auto& d : r["dirty"]) dirty.insert(d.get<std::string>());
    if (r.contains("items")) {
        for (const auto& it : r["items"]) {
            PlmItemView v;
            v.itemId = jstr(it, "item_id");
            v.path = jstr(it, "path");
            v.docKind = jstr(it, "kind");
            v.partNumber = jstr(it, "part_number");
            v.rev = jstr(it, "rev");
            v.lifecycle = jstr(it, "lifecycle");
            v.workingRev = jstr(it, "working_rev");
            v.dirty = dirty.count(v.itemId) > 0;
            g.items.push_back(std::move(v));
        }
    }
    g.aiMode = r.value("ai_mode", std::string("assisted"));
    g.numberPrefix = r.value("part_number_prefix", std::string());
    g.nextNumber = r.value("next_part_number", std::string());
}

void refreshHistory(app::AppState& st) {
    if (g.projectDir.empty()) return;
    nlohmann::json params = {{"path", g.projectDir}};
    if (!g.filterItemId.empty()) params["item_id"] = g.filterItemId;
    if (!g.thumbDir.empty()) params["thumb_dir"] = g.thumbDir;
    nlohmann::json r;
    if (!plmCall(st, "plm_history", params, r, 60000)) return;
    g.entries.clear();
    if (r.contains("entries")) {
        for (const auto& e : r["entries"]) {
            PlmEntryView v;
            v.id = jstr(e, "commit");   // machine handle only — never rendered
            v.kind = jstr(e, "kind");
            v.whenIso = jstr(e, "authored_at");
            v.message = jstr(e, "message");
            v.summary = jstr(e, "summary");
            v.thumbFile = jstr(e, "thumbnail_file");
            if (e.contains("author") && e["author"].is_object()) {
                v.author = jstr(e["author"], "display");
                v.aiAuthored = jstr(e["author"], "kind") == "ai";
            }
            if (e.contains("ai") && e["ai"].is_object()) v.aiAuthored = true;
            if (v.author.empty()) v.author = "User";
            g.entries.push_back(std::move(v));
        }
    }
    g.rows = aicad::ui::buildTimelineRows(g.entries);
    g.expanded.clear();
    g.historyLoaded = true;
}

// Open (auto-creating) the vault for a project folder and refresh the mirrors.
bool ensureProjectDir(app::AppState& st, const std::string& dir) {
    if (dir.empty()) return false;
    if (dir == g.projectDir && !g.projectId.empty()) return true;
    nlohmann::json r;
    if (!plmCall(st, "plm_open", {{"path", dir}}, r, 60000)) return false;
    g.projectDir = dir;
    g.projectId = r.value("project_id", std::string());
    std::error_code ec;
    std::filesystem::path td = std::filesystem::temp_directory_path(ec);
    if (!ec) {
        td /= "aicad_plm_thumbs";
        td /= g.projectId.empty() ? std::string("project") : g.projectId;
        std::filesystem::create_directories(td, ec);
        g.thumbDir = td.string();
    }
    g.historyLoaded = false;
    refreshStatus(st);
    return true;
}

bool ensureProjectFor(app::AppState& st, const std::string& docPath) {
    return ensureProjectDir(st, parentDirOf(docPath));
}

const PlmItemView* itemForPath(const std::string& docPath) {
    if (docPath.empty()) return nullptr;
    const std::string base = baseNameOf(docPath);
    for (const auto& it : g.items)
        if (it.path == base) return &it;
    return nullptr;
}

std::string activeDocPath(app::AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        !st.docTabs[st.activeTab].path.empty())
        return st.docTabs[st.activeTab].path;
    return featureTreeActivePath();
}

const PlmItemView* activeItem(app::AppState& st) {
    return itemForPath(activeDocPath(st));
}

// ---- actions -----------------------------------------------------------------

// Reload the active doc's working file from disk after a restore/discard (the
// vault rewrote it). featureTreeOpenPath routes through the shared open seam
// (viewport mesh refresh included).
void reloadActiveDocFromDisk(app::AppState& st) {
    const std::string path = activeDocPath(st);
    if (!path.empty()) featureTreeOpenPath(st, path);
}

void openCheckpointPopover(app::AppState& st) {
    const std::string path = activeDocPath(st);
    if (!path.empty()) ensureProjectFor(st, path);
    if (g.projectDir.empty()) {
        postToast(ToastLevel::Info, "Save this document first — history begins with the first save");
        return;
    }
    g.msgBuf[0] = 0;
    g.draftSource = "manual";
    g.draftSummary.clear();
    nlohmann::json r;
    if (plmCall(st, "plm_draft", {{"path", g.projectDir}, {"mode", "checkpoint"}}, r, 60000)) {
        g.draftSource = r.value("source", std::string("manual"));
        g.draftSummary = r.value("summary", std::string());
        const std::string msg = r.value("message", std::string());
        if (!msg.empty()) {
            std::snprintf(g.msgBuf, sizeof(g.msgBuf), "%s", msg.c_str());
        }
    }
    g.checkpointOpen = true;
}

void doCheckpoint(app::AppState& st, const std::string& message) {
    nlohmann::json r;
    if (!plmCall(st, "plm_checkpoint", {{"path", g.projectDir}, {"message", message}}, r, 60000)) {
        postToast(ToastLevel::Error, "Checkpoint failed: " + g.statusLine);
        return;
    }
    postToast(ToastLevel::Info, "Checkpoint saved");
    refreshStatus(st);
    refreshHistory(st);
}

void openReleasePopover(app::AppState& st) {
    const std::string path = activeDocPath(st);
    if (!path.empty()) ensureProjectFor(st, path);
    const PlmItemView* it = activeItem(st);
    if (!it) {
        postToast(ToastLevel::Info, "Save this document first — a release needs a saved document");
        return;
    }
    g.releaseItemId = it->itemId;
    // Part number: auto-suggested from the configurable scheme, freely editable
    // (T-R5 locked decision). The item already carries a suggestion; fall back
    // to the project's next number.
    std::snprintf(g.partNumBuf, sizeof(g.partNumBuf), "%s",
                  !it->partNumber.empty() ? it->partNumber.c_str() : g.nextNumber.c_str());
    std::snprintf(g.prefixBuf, sizeof(g.prefixBuf), "%s", g.numberPrefix.c_str());
    g.relMsgBuf[0] = 0;
    g.draftSource = "manual";
    g.draftSummary.clear();
    nlohmann::json r;
    if (plmCall(st, "plm_draft", {{"path", g.projectDir}, {"mode", "release"},
                                  {"item_id", g.releaseItemId}}, r, 60000)) {
        g.draftSource = r.value("source", std::string("manual"));
        g.draftSummary = r.value("summary", std::string());
        const std::string msg = r.value("message", std::string());
        if (!msg.empty())
            std::snprintf(g.relMsgBuf, sizeof(g.relMsgBuf), "%s", msg.c_str());
    }
    g.preflightProblems.clear();
    g.releaseNeedsConfirm = false;
    g.releaseOpen = true;
}

void doRelease(app::AppState& st, bool confirm) {
    // apply an edited part number first (kept unique by the service)
    const PlmItemView* it = nullptr;
    for (const auto& i : g.items)
        if (i.itemId == g.releaseItemId) { it = &i; break; }
    const std::string typedNumber = g.partNumBuf;
    if (it && !typedNumber.empty() && typedNumber != it->partNumber) {
        nlohmann::json r0;
        if (!plmCall(st, "plm_item_set", {{"path", g.projectDir},
                                          {"item_id", g.releaseItemId},
                                          {"part_number", typedNumber}}, r0)) {
            postToast(ToastLevel::Warn, "Part number not applied: " + g.statusLine);
            return;
        }
    }
    nlohmann::json r;
    nlohmann::json params = {{"path", g.projectDir}, {"item_id", g.releaseItemId},
                             {"message", std::string(g.relMsgBuf)}, {"confirm", confirm}};
    if (!plmCall(st, "plm_release", params, r, 120000)) {
        g.preflightProblems.clear();
        if (r.is_object() && r.contains("problems") && r["problems"].is_array()) {
            for (const auto& p : r["problems"]) {
                if (p.is_string()) g.preflightProblems.push_back(p.get<std::string>());
                else if (p.is_object() && p.contains("hint"))
                    g.preflightProblems.push_back(jstr(p, "hint"));
                else if (p.is_object() && p.contains("message"))
                    g.preflightProblems.push_back(jstr(p, "message"));
                else g.preflightProblems.push_back(p.dump());
            }
            g.releaseNeedsConfirm = true;   // warnings, not walls — "Release anyway"
        } else {
            postToast(ToastLevel::Error, "Release failed: " + g.statusLine);
        }
        return;
    }
    const std::string rev = r.value("rev", std::string());
    postToast(ToastLevel::Info, rev.empty() ? std::string("Released")
                                            : ("Released rev " + rev));
    g.releaseOpen = false;
    refreshStatus(st);
    refreshHistory(st);
}

void doRestore(app::AppState& st, const std::string& entryId) {
    nlohmann::json r;
    if (!plmCall(st, "plm_restore", {{"path", g.projectDir}, {"commit", entryId}}, r, 60000)) {
        postToast(ToastLevel::Error, "Could not return to that version: " + g.statusLine);
        return;
    }
    postToast(ToastLevel::Info, "Returned to the earlier version — nothing was erased");
    refreshStatus(st);
    refreshHistory(st);
    reloadActiveDocFromDisk(st);
}

void doDiscardDraft(app::AppState& st) {
    if (g.discardItemId.empty()) return;
    nlohmann::json r;
    if (!plmCall(st, "plm_discard_draft", {{"path", g.projectDir},
                                           {"item_id", g.discardItemId}}, r, 60000)) {
        postToast(ToastLevel::Error, "Could not discard the draft: " + g.statusLine);
        return;
    }
    std::string rev;
    if (r.contains("item") && r["item"].is_object()) rev = jstr(r["item"], "rev");
    postToast(ToastLevel::Info, rev.empty() ? std::string("Back to the released version")
                                            : ("Back to rev " + rev));
    g.discardItemId.clear();
    refreshStatus(st);
    refreshHistory(st);
    reloadActiveDocFromDisk(st);
}

void doCompare(app::AppState& st, const std::string& olderId, const std::string& newerId,
               const std::string& itemId) {
    if (itemId.empty()) {
        postToast(ToastLevel::Info, "Pick a document to compare (use the filter above the timeline)");
        return;
    }
    nlohmann::json r;
    std::string outDir = g.thumbDir.empty() ? std::string() : (g.thumbDir + "/compare");
    nlohmann::json params = {{"path", g.projectDir}, {"commit_a", olderId},
                             {"commit_b", newerId}, {"item_id", itemId}, {"visual", true}};
    if (!outDir.empty()) params["out_dir"] = outDir;
    if (!plmCall(st, "plm_compare", params, r, 180000)) {
        postToast(ToastLevel::Error, "Compare failed: " + g.statusLine);
        return;
    }
    CompareState c;
    c.loaded = true;
    c.summary = r.value("summary", std::string());
    // structured change rows from the semantic diff
    if (r.contains("doc_diff") && r["doc_diff"].is_object()) {
        const auto& d = r["doc_diff"];
        auto addList = [&](const char* key, const char* verb) {
            if (!d.contains(key) || !d[key].is_array()) return;
            for (const auto& x : d[key])
                c.lines.push_back(std::string(verb) + " " +
                                  (x.is_string() ? x.get<std::string>() : x.dump()));
        };
        addList("features_added", "Added");
        addList("features_removed", "Removed");
        addList("features_changed", "Changed");
        if (d.contains("params_changed") && d["params_changed"].is_array()) {
            for (const auto& pc : d["params_changed"]) {
                if (pc.is_object()) {
                    std::string line = jstr(pc, "id");
                    const std::string pname = jstr(pc, "param");
                    if (!pname.empty()) line += "." + pname;
                    const std::string o = jstr(pc, "old"), n = jstr(pc, "new");
                    if (!o.empty() || !n.empty())
                        line += ": " + o + " \xe2\x86\x92 " + n;
                    c.lines.push_back(line);
                } else {
                    c.lines.push_back(pc.dump());
                }
            }
        }
    }
    if (r.contains("eval_compare") && r["eval_compare"].is_object()) {
        const auto& v = r["eval_compare"];
        if (v.value("degraded", false)) {
            c.visualHint = v.value("hint", std::string("side-by-side view is not available here"));
        } else {
            c.imageA = v.value("image_a", std::string());
            c.imageB = v.value("image_b", std::string());
            if (v.contains("match_pct") && v["match_pct"].is_number()) {
                char buf[64];
                std::snprintf(buf, sizeof(buf), "Geometry match: %.1f%%",
                              v["match_pct"].get<double>());
                c.matchLine = buf;
            }
        }
    } else if (r.contains("visual_unavailable")) {
        c.visualHint = "side-by-side view is not available here";
    }
    // labels from the timeline
    for (const auto& e : g.entries) {
        if (e.id == olderId) c.labelA = whenDisplay(e.whenIso);
        if (e.id == newerId) c.labelB = whenDisplay(e.whenIso);
    }
    if (c.labelB.empty()) c.labelB = "latest";
    c.title = "Compare \xc2\xb7 " + c.labelA + "  vs  " + c.labelB;
    g.cmp = std::move(c);
    g.compareOpen = true;
}

void applyNumberScheme(app::AppState& st) {
    nlohmann::json r;
    if (!plmCall(st, "plm_scheme_set", {{"path", g.projectDir},
                                        {"prefix", std::string(g.prefixBuf)}}, r)) {
        postToast(ToastLevel::Warn, "Numbering scheme not changed: " + g.statusLine);
        return;
    }
    refreshStatus(st);
    // refresh the suggestion in the open release popover (only if untouched by hand)
    if (std::string(g.partNumBuf).rfind(g.numberPrefix + "-", 0) == 0 || g.partNumBuf[0] == 0)
        std::snprintf(g.partNumBuf, sizeof(g.partNumBuf), "%s", g.nextNumber.c_str());
    postToast(ToastLevel::Info, "Part numbers will now suggest " + g.nextNumber);
}

// ---- session-changes log -------------------------------------------------------

void noteSessionChange(app::AppState& st) {
    if (st.suppressTabDirty) return;   // tab switches / programmatic loads aren't edits
    std::string doc = "document";
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        doc = st.docTabs[st.activeTab].displayTitle();
    const double now = ImGui::GetTime();
    if (!g.session.empty() && g.session.front().desc == ("Edited " + doc) &&
        now - g.lastSessionAt < 5.0) {
        g.session.front().count++;
        g.lastSessionAt = now;
        return;
    }
    SessionChange sc;
    sc.desc = "Edited " + doc;
    char buf[16];
    int secs = (int)now;
    std::snprintf(buf, sizeof(buf), "+%02d:%02d", secs / 60, secs % 60);
    sc.when = buf;   // minutes into this sitting — session-scoped by design
    g.session.insert(g.session.begin(), sc);
    if (g.session.size() > 24) g.session.pop_back();
    g.lastSessionAt = now;
}

// ---- timeline rendering ---------------------------------------------------------

void entryKindChip(const PlmEntryView& e) {
    const theme::Palette& p = theme::pal();
    unsigned int role = p.txt2;
    const char* label = "saved";
    if (e.kind == "checkpoint") { role = p.good; label = "checkpoint"; }
    else if (e.kind == "release") { role = p.accent; label = "release"; }
    else if (e.kind == "restore") { role = p.warn; label = "returned"; }
    ImGui::PushStyleColor(ImGuiCol_Text, theme::vec(role));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
}

void drawEntryThumb(const PlmEntryView& e, float side) {
    const float S = theme::S();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    bool drew = false;
    if (!e.thumbFile.empty()) {
        int tw = 0, th = 0;
        ImTextureID tex = ui::loadPreviewTexture(e.thumbFile, &tw, &th);
        if (tex && tw > 0 && th > 0) {
            ImGui::Image(tex, ImVec2(side, side));
            drew = true;
        }
    }
    if (!drew) {
        theme::glassRowBackground(dl, p0, ImVec2(p0.x + side, p0.y + side), 3.0f,
                                  false, false, theme::rowWash::idle);
        icons::drawGlass(dl, e.kind == "release" ? "success" : "part",
                         ImVec2(p0.x + side * 0.5f, p0.y + side * 0.5f), side * 0.32f,
                         ImGui::ColorConvertFloat4ToU32(TVEC(txt2)),
                         theme::roleU32A(theme::pal().vibrancyDarkPrimary, 0.31f));
        ImGui::Dummy(ImVec2(side, side));
    }
    (void)S;
}

// One story row (or one expanded group member). Returns via out-params the
// context-menu actions requested this frame.
struct RowAction {
    std::string restoreId;
    std::string compareOlderId;
    std::string checkpointHereId;
};

void drawEntryRow(app::AppState& st, const PlmEntryView& e, int idx, bool sub,
                  RowAction& act) {
    const float S = theme::S();
    const float thumb = (sub ? 26.f : 36.f) * S;
    ImGui::PushID(idx);
    if (sub) ImGui::Indent(theme::U(1.5f));

    ImGui::BeginGroup();
    drawEntryThumb(e, thumb);
    ImGui::EndGroup();
    ImGui::SameLine(0, theme::U(1.f));
    ImGui::BeginGroup();
    // title line: message / summary / kind placeholder
    ImGui::TextWrapped("%s", aicad::ui::plmEntryTitle(e).c_str());
    // meta line: kind chip · when · who (+ AI mark, §8.4 visible attribution)
    entryKindChip(e);
    ImGui::SameLine(0, theme::U(0.75f));
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    std::string meta = whenDisplay(e.whenIso) + " \xc2\xb7 " + e.author;
    ImGui::TextUnformatted(meta.c_str());
    ImGui::PopStyleColor();
    if (e.aiAuthored) {
        ImGui::SameLine(0, theme::U(0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
        ImGui::TextUnformatted("\xe2\x9c\xa6 AI");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Recorded by the copilot — one click below returns to the version before it");
    }
    ImGui::EndGroup();

    // full-row hover affordance + context menu (restore/compare are right-click
    // affordances, not modes)
    ImGui::SetCursorScreenPos(ImGui::GetItemRectMin());
    ImVec2 rowSize(ImGui::GetContentRegionAvail().x,
                   ImGui::GetItemRectMax().y - ImGui::GetItemRectMin().y);
    ImGui::Dummy(ImVec2(1, 1));  // keep layout; context menu binds to the group below
    if (ImGui::BeginPopupContextItem("##entry_ctx")) {
        const bool isLatest = (idx == 0);
        if (ImGui::MenuItem("Return to this version", nullptr, false, !isLatest))
            act.restoreId = e.id;
        if (ImGui::MenuItem("Compare with latest", nullptr, false, !isLatest))
            act.compareOlderId = e.id;
        if (ImGui::MenuItem("Save a checkpoint of this version", nullptr, false, !isLatest))
            act.checkpointHereId = e.id;
        ImGui::EndPopup();
    }
    (void)rowSize;

    if (!e.summary.empty() && !e.message.empty() && ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", e.summary.c_str());

    if (sub) ImGui::Unindent(theme::U(1.5f));
    ImGui::PopID();
    ImGui::Spacing();
}

}  // namespace

// ---- public: visibility -----------------------------------------------------

bool* plmPanelOpen() { return &g.panelOpen; }
bool* plmComparePanelOpen() { return &g.compareOpen; }

// ---- public: badges -----------------------------------------------------------

PlmTabBadge plmTabBadgeForPath(const std::string& path) {
    PlmTabBadge b;
    const PlmItemView* it = itemForPath(path);
    if (!it) return b;
    b.text = aicad::ui::plmBadgeText(*it);
    if (b.text.empty()) return b;
    b.tooltip = aicad::ui::plmLifecycleLabel(*it);
    b.released = (it->lifecycle == "Released");
    b.valid = true;
    return b;
}

// ---- public: vault lifecycle hooks -------------------------------------------

void plmOnDocSaved(app::AppState& st, const std::string& path) {
    if (path.empty()) return;
    if (!ensureProjectFor(st, path)) return;
    nlohmann::json r;
    if (!plmCall(st, "plm_save", {{"path", g.projectDir}, {"kind", "auto"}}, r, 60000))
        return;   // save itself succeeded; the history entry is best-effort
    refreshStatus(st);
    if (g.panelOpen) refreshHistory(st);
    else g.historyLoaded = false;
}

void plmOnDocMutation(app::AppState& st) {
    noteSessionChange(st);
    if (st.suppressTabDirty || g.branchInFlight) return;
    if (g.projectDir.empty() || !g.vaultAvailable) return;
    const PlmItemView* it = activeItem(st);
    if (!it || it->lifecycle != "Released") return;

    // T-R5 locked decision: AUTO-BRANCH SILENTLY. First edit of a Released doc
    // starts the next-rev draft behind the scenes — no prompt, no read-only
    // wall — and the toast carries the "back to rev X (discard draft)" action.
    g.branchInFlight = true;
    const std::string rev = it->rev;
    const std::string itemId = it->itemId;
    nlohmann::json r;
    if (plmCall(st, "plm_item_set", {{"path", g.projectDir}, {"item_id", itemId},
                                     {"start_new_rev", true}}, r)) {
        std::string draftRev = r.value("working_rev", std::string());
        refreshStatus(st);
        g.discardItemId = itemId;
        std::string msg = "Rev " + rev + " is released and stays untouched - "
                          "you are now editing a rev " +
                          (draftRev.empty() ? std::string("draft") : draftRev) + " draft.";
        postToastAction(ToastLevel::Info, msg,
                        "Back to rev " + rev + " (discard draft)", kActDiscardDraft);
    }
    g.branchInFlight = false;
}

// ---- public: per-frame tick + popovers ----------------------------------------

void plmFrameTick(app::AppState& st) {
    // toast action: discard the auto-branched draft
    if (takeToastAction() == kActDiscardDraft) doDiscardDraft(st);

    const float S = theme::S();
    ImGuiWindowFlags pf = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_AlwaysAutoResize;

    // -- checkpoint popover ------------------------------------------------------
    if (g.checkpointOpen) {
        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f,
                                       vp->WorkPos.y + vp->WorkSize.y * 0.35f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        if (ImGui::Begin("Checkpoint###plm_checkpoint", &g.checkpointOpen, pf)) {
            if (g.draftSource == "local_model") {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextUnformatted("Drafted from your changes - edit freely.");
                ImGui::PopStyleColor();
            } else if (!g.draftSummary.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextWrapped("What changed: %s", g.draftSummary.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextUnformatted("Describe this moment so you can find it later.");
                ImGui::PopStyleColor();
            }
            ImGui::SetNextItemWidth(360.f * S);
            ImGui::InputTextWithHint("##ckpt_msg", "What changed?", g.msgBuf, sizeof(g.msgBuf));
            ImGui::Spacing();
            if (ImGui::Button("Save checkpoint", ImVec2(theme::U(16.f), 0))) {
                std::string msg = g.msgBuf;
                if (msg.empty()) msg = g.draftSummary;   // deterministic draft as fallback
                doCheckpoint(st, msg);
                g.checkpointOpen = false;
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(theme::U(10.f), 0))) g.checkpointOpen = false;
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) g.checkpointOpen = false;
        }
        ImGui::End();
    }

    // -- release popover ---------------------------------------------------------
    if (g.releaseOpen) {
        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f,
                                       vp->WorkPos.y + vp->WorkSize.y * 0.4f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        if (ImGui::Begin("Release###plm_release", &g.releaseOpen, pf)) {
            const PlmItemView* it = nullptr;
            for (const auto& i : g.items)
                if (i.itemId == g.releaseItemId) { it = &i; break; }
            if (it) {
                ImGui::TextUnformatted(it->path.c_str());
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextUnformatted(aicad::ui::plmLifecycleLabel(*it).c_str());
                ImGui::PopStyleColor();
            }
            ImGui::Spacing();
            ImGui::TextUnformatted("Part number");
            ImGui::SetNextItemWidth(200.f * S);
            ImGui::InputText("##rel_pn", g.partNumBuf, sizeof(g.partNumBuf));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Suggested from the project scheme (%s) - edit freely",
                                  g.numberPrefix.c_str());
            ImGui::SameLine();
            if (ImGui::TreeNodeEx("scheme...", ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                               ImGuiTreeNodeFlags_Leaf))
                ImGui::OpenPopup("##plm_scheme");
            if (ImGui::BeginPopup("##plm_scheme")) {
                ImGui::TextUnformatted("Numbering scheme prefix");
                ImGui::SetNextItemWidth(120.f * S);
                ImGui::InputText("##scheme_prefix", g.prefixBuf, sizeof(g.prefixBuf));
                ImGui::SameLine();
                if (ImGui::Button("Apply")) {
                    applyNumberScheme(st);
                    ImGui::CloseCurrentPopup();
                }
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::Text("Next suggestion: %s", g.nextNumber.c_str());
                ImGui::PopStyleColor();
                ImGui::EndPopup();
            }

            ImGui::Spacing();
            ImGui::TextUnformatted("Release note");
            if (g.draftSource == "local_model") {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextUnformatted("Drafted from your changes - edit freely.");
                ImGui::PopStyleColor();
            } else if (!g.draftSummary.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextWrapped("Since the last release: %s", g.draftSummary.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::SetNextItemWidth(360.f * S);
            ImGui::InputTextWithHint("##rel_msg", "What ships in this revision?",
                                     g.relMsgBuf, sizeof(g.relMsgBuf));

            if (!g.preflightProblems.empty()) {
                ImGui::Spacing();
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(warn));
                ImGui::TextUnformatted("Checks found problems:");
                ImGui::PopStyleColor();
                for (const auto& p : g.preflightProblems)
                    ImGui::BulletText("%s", p.c_str());
            }

            ImGui::Spacing();
            const char* btn = g.releaseNeedsConfirm ? "Release anyway" : "Release";
            if (ImGui::Button(btn, ImVec2(theme::U(16.f), 0)))
                doRelease(st, g.releaseNeedsConfirm);
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(theme::U(10.f), 0))) g.releaseOpen = false;
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) g.releaseOpen = false;
        }
        ImGui::End();
    }
}

// ---- public: FM strip controls -------------------------------------------------

void drawPlmStripControls(app::AppState& st, const std::string& docPath) {
    const PlmItemView* it = itemForPath(docPath);
    if (it) {
        std::string line = aicad::ui::plmLifecycleLabel(*it);
        if (!it->partNumber.empty()) line = it->partNumber + "  \xc2\xb7  " + line;
        ImGui::PushStyleColor(ImGuiCol_Text,
                              it->lifecycle == "Released" ? TVEC(accent) : TVEC(txt2));
        ImGui::TextUnformatted(line.c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("This document's place in the project history");
    }

    const ImGuiStyle& style = ImGui::GetStyle();
    const float gap = style.ItemSpacing.x;
    const float halfW = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
    const bool canAct = !docPath.empty();
    if (!canAct) ImGui::BeginDisabled();
    if (ImGui::Button("Checkpoint...", ImVec2(halfW, 0))) openCheckpointPopover(st);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Save a named moment you can always return to");
    ImGui::SameLine(0, gap);
    if (ImGui::Button("Release...", ImVec2(halfW, 0))) openReleasePopover(st);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Stamp the next revision letter and freeze this design");
    if (ImGui::Button("History", ImVec2(-1, 0))) {
        if (!docPath.empty()) ensureProjectFor(st, docPath);
        g.panelOpen = true;
        if (!g.projectDir.empty()) refreshHistory(st);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("The project timeline - every save, checkpoint and release");
    if (!canAct) ImGui::EndDisabled();
}

// ---- public: the History panel --------------------------------------------------

void drawPlmPanel(app::AppState& st) {
    // lazy load when the panel opens with a doc already on disk
    if (!g.historyLoaded && !g.projectDir.empty()) refreshHistory(st);
    if (g.projectDir.empty()) {
        const std::string path = activeDocPath(st);
        if (!path.empty() && ensureProjectFor(st, path)) refreshHistory(st);
    }

    PanelScaffoldSpec spec;
    spec.id = "plm_history";
    spec.title = "History";
    spec.icon = "history";
    g_scaffold.statusText = g.statusLine;
    g_scaffold.statusIsError = g.statusIsError;

    drawPanelScaffold(spec, g_scaffold, [&]() {
        if (!g.vaultAvailable) {
            drawPanelEmptyState("Version history is not available on this machine",
                                g.unavailableHint.empty() ? nullptr : g.unavailableHint.c_str());
            return;
        }
        if (g.projectDir.empty()) {
            drawPanelEmptyState("Save this document to start its history",
                                "History begins with the first save into a project folder");
            return;
        }

        // -- Session changes (undo/redo) vs Project history (persistent) --------
        // The two-timelines confusion killer: session edits are NOT saved
        // history; the split makes that visible.
        if (ImGui::CollapsingHeader("Session changes (this sitting)")) {
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
            ImGui::TextWrapped("Undo and redo live here and reset when you close the app. "
                               "Saved project history is below.");
            ImGui::PopStyleColor();
            if (g.session.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
                ImGui::TextUnformatted("No edits yet this sitting.");
                ImGui::PopStyleColor();
            }
            int shown = 0;
            for (const auto& sc : g.session) {
                if (++shown > 6) break;
                std::string line = sc.desc;
                if (sc.count > 1) line += " (\xc3\x97" + std::to_string(sc.count) + ")";
                ImGui::BulletText("%s", line.c_str());
            }
            ImGui::Spacing();
        }

        // -- Project history header: filter + refresh ---------------------------
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt0));
        ImGui::TextUnformatted("Project history");
        ImGui::PopStyleColor();
        ImGui::SameLine(0, theme::U(1.f));
        {
            std::string current = "All documents";
            for (const auto& it : g.items)
                if (it.itemId == g.filterItemId) current = it.path;
            ImGui::SetNextItemWidth(160.f * theme::S());
            if (ImGui::BeginCombo("##plm_filter", current.c_str())) {
                if (ImGui::Selectable("All documents", g.filterItemId.empty())) {
                    g.filterItemId.clear();
                    refreshHistory(st);
                }
                for (const auto& it : g.items) {
                    if (ImGui::Selectable(it.path.c_str(), it.itemId == g.filterItemId)) {
                        g.filterItemId = it.itemId;
                        refreshHistory(st);
                    }
                }
                ImGui::EndCombo();
            }
        }

        if (g.entries.empty()) {
            drawPanelEmptyState("No history yet", "Save the document to record the first entry");
            return;
        }

        // -- timeline ------------------------------------------------------------
        RowAction act;
        for (const auto& row : g.rows) {
            if (!row.isGroup) {
                drawEntryRow(st, g.entries[(size_t)row.first], row.first, false, act);
                continue;
            }
            // collapsed auto-save group (T-R5 locked decision)
            const bool open = g.expanded.count(row.first) > 0;
            ImGui::PushID(row.first);
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
            std::string glabel = std::string(open ? "\xe2\x96\xbe  " : "\xe2\x96\xb8  ") +
                                 aicad::ui::plmGroupLabel(row.count);
            if (ImGui::Selectable(glabel.c_str(), false)) {
                if (open) g.expanded.erase(row.first);
                else g.expanded.insert(row.first);
            }
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Automatic safety entries between checkpoints - click to expand");
            ImGui::PopID();
            if (open)
                for (int k = 0; k < row.count; ++k)
                    drawEntryRow(st, g.entries[(size_t)(row.first + k)],
                                 row.first + k, true, act);
        }

        // -- deferred row actions (after iteration: actions mutate g.entries) ----
        if (!act.restoreId.empty()) doRestore(st, act.restoreId);
        if (!act.compareOlderId.empty() && !g.entries.empty()) {
            std::string itemId = g.filterItemId;
            if (itemId.empty()) {
                const PlmItemView* it = activeItem(st);
                if (it) itemId = it->itemId;
                else if (g.items.size() == 1) itemId = g.items[0].itemId;
            }
            doCompare(st, act.compareOlderId, g.entries[0].id, itemId);
        }
        if (!act.checkpointHereId.empty()) {
            // "Checkpoint from here": return to that version as a NEW entry,
            // then name it — forward-only, nothing rewritten.
            doRestore(st, act.checkpointHereId);
            openCheckpointPopover(st);
        }
    });
}

// ---- public: compare view --------------------------------------------------------

void drawPlmComparePanel(app::AppState& st) {
    (void)st;
    if (!g.cmp.loaded) {
        drawPanelEmptyState("Nothing to compare yet",
                            "Right-click a history entry and pick Compare");
        return;
    }
    const float S = theme::S();
    ImGui::TextUnformatted(g.cmp.title.c_str());
    if (!g.cmp.matchLine.empty()) {
        ImGui::SameLine(0, theme::U(1.5f));
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
        ImGui::TextUnformatted(g.cmp.matchLine.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Separator();

    // side-by-side visual (parts/assemblies render; drawings get the overlay
    // image from the service — both arrive as plain PNG paths)
    if (!g.cmp.imageA.empty() || !g.cmp.imageB.empty()) {
        const float half = (ImGui::GetContentRegionAvail().x - theme::U(1.f)) * 0.5f;
        auto drawSide = [&](const std::string& img, const std::string& label) {
            ImGui::BeginGroup();
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
            ImGui::TextUnformatted(label.c_str());
            ImGui::PopStyleColor();
            int tw = 0, th = 0;
            ImTextureID tex = img.empty() ? (ImTextureID)0
                                          : ui::loadPreviewTexture(img, &tw, &th);
            if (tex && tw > 0 && th > 0) {
                float w = half;
                float h = w * (float)th / (float)tw;
                ImGui::Image(tex, ImVec2(w, h));
            } else {
                drawPanelEmptyState("no view");
            }
            ImGui::EndGroup();
        };
        drawSide(g.cmp.imageA, g.cmp.labelA);
        ImGui::SameLine(0, theme::U(1.f));
        drawSide(g.cmp.imageB, g.cmp.labelB);
        ImGui::Separator();
    } else if (!g.cmp.visualHint.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        ImGui::TextWrapped("%s", g.cmp.visualHint.c_str());
        ImGui::PopStyleColor();
        ImGui::Separator();
    }

    // docked change list from the semantic compare
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt0));
    ImGui::TextUnformatted("What changed");
    ImGui::PopStyleColor();
    if (!g.cmp.summary.empty()) ImGui::TextWrapped("%s", g.cmp.summary.c_str());
    for (const auto& line : g.cmp.lines) ImGui::BulletText("%s", line.c_str());
    if (g.cmp.lines.empty() && g.cmp.summary.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        ImGui::TextUnformatted("No design changes between these versions.");
        ImGui::PopStyleColor();
    }
    (void)S;
}

// ---- public: shot seams ------------------------------------------------------------

void plmShotOpenProject(app::AppState& st, const std::string& projectDir) {
    g.projectDir.clear();
    g.projectId.clear();
    if (ensureProjectDir(st, projectDir)) refreshHistory(st);
    g.panelOpen = true;
}

void plmShotOpenCheckpoint(app::AppState& st) { openCheckpointPopover(st); }

void plmShotOpenCompare(app::AppState& st) {
    if (g.entries.size() < 2) return;
    std::string itemId = g.filterItemId;
    if (itemId.empty() && !g.items.empty()) itemId = g.items[0].itemId;
    // oldest vs newest so the change list is non-trivial
    doCompare(st, g.entries.back().id, g.entries.front().id, itemId);
}

}  // namespace ui
