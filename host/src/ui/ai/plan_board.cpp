// See plan_board.h. The active conversation owns the editable PlanState checklist;
// checkpoints belong to the selected project document and are never a global timeline.
// The plan JSON snapshot matches conversations.cpp's planStateToJson shape.
#include "ui/ai/plan_board.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include "core/app_state.h"
#include "ui/copilot/copilot_internal.h"  // chat history, project scope, AI checkpoints
#include "ui/ai/active_work.h"             // saved/unsaved document project ownership
#include "ui_theme.h"
#include "ui/metrics.h"       // cpMenu* rhythm tokens (gutter / row pad / gap / corner)
#include "ui/ai/ai_prefs.h"
#include "ui/widgets/kit.h"   // ui::kit — the shared section header / empty state / tone table

namespace ui {
namespace {

// glassPanel / glassRowBackground take a LOGICAL dp radius (they multiply by uiScale
// themselves), so hand them the shared cpMenu corner converted back to dp — one source of
// truth for the corner without double-scaling it.
inline float rowRoundDp() { return ui::metrics::cpMenuRowRound() / ui::uiScale; }

// Arc tone for a "how far along is this" ring — the SAME ladder the Copilot composer's
// context-window meter uses (ui::kUsageWarnFrac / kUsageCritFrac, the chud-ui-shared-v1
// usage thresholds), applied to the REMAINING share so a nearly-finished plan reads green
// and a barely-started one reads hot.
inline ImU32 fracArcCol(float remaining) {
    return remaining < kUsageWarnFrac ? Palette::toU32(palette.statusOk)
         : remaining < kUsageCritFrac ? Palette::toU32(palette.statusWarn)
                                      : Palette::toU32(palette.statusErr);
}

// ---- plan snapshot (de)serialization --------------------------------------------
// Byte-compatible with planStateToJson in ui/copilot/conversations.cpp.

nlohmann::json pbPlanToJson(const PlanState& ps) {
    nlohmann::json j = nlohmann::json::object();
    j["shape"] = ps.shape;
    j["title"] = ps.title;
    j["summary"] = ps.summary;
    j["revision"] = ps.revision;
    j["from_event"] = ps.fromEvent;
    nlohmann::json items = nlohmann::json::array();
    for (const auto& it : ps.items) {
        items.push_back({{"id", it.id},
                         {"title", it.title},
                         {"desc", it.desc},
                         {"status", it.status},
                         {"kind", it.kind},
                         {"phase", it.phase},
                         {"parent_id", it.parent_id}});
    }
    j["items"] = std::move(items);
    if (!ps.edges.empty()) {
        nlohmann::json edges = nlohmann::json::array();
        for (const auto& e : ps.edges)
            edges.push_back({{"from", e.from}, {"to", e.to}});
        j["edges"] = std::move(edges);
    }
    return j;
}

// The conversation's message buffer: the ACTIVE conversation's messages live in
// AppState::chat; inactive ones park in Conversation::msgs.
const std::vector<ChatMsg>* pbMsgsForConv(const AppState& st, const std::string& convId) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
        st.conversations[st.activeConv].id == convId)
        return &st.chat;
    for (const auto& c : st.conversations)
        if (c.id == convId) return &c.msgs;
    return nullptr;
}

std::vector<ChatMsg>* pbMsgsForConv(AppState& st, const std::string& convId) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
        st.conversations[st.activeConv].id == convId)
        return &st.chat;
    for (auto& c : st.conversations)
        if (c.id == convId) return &c.msgs;
    return nullptr;
}

// The "current" plan = the newest message carrying one (later turns supersede).
ChatMsg* pbActivePlanMsg(AppState& st) {
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (it->planState.present) return &*it;
    return nullptr;
}

// ---- edit bookkeeping -------------------------------------------------------------

// Subtle "queued — applies next turn" note after steering a running agent.
double      s_noteUntil = 0.0;
// Inline-rename state (one row at a time; owner = the ChatMsg holding the plan, so a
// conversation switch or a new plan drops any in-flight edit).
int         s_renameIdx   = -1;
const void* s_renameOwner = nullptr;
bool        s_renameFocus = false;
char        s_renameBuf[256] = {0};
int         s_itemCtr = 0;

// Persist + (while a turn runs) notify the agent through the EXISTING queued-prompt
// path — drawComposer drains st.queuedPrompts into the next turn. Consecutive plan
// notes coalesce so rapid edits don't flood the queue.
void pbAfterEdit(AppState& st, const std::string& summary) {
    markChatHistoryDirty();
    for (auto& m : st.chat) m.uiCachedH = -1.0f;  // the plan card's height may change
    if (!aiTurnActive(st)) return;
    std::string note = "Plan updated: " + summary;
    if (!st.queuedPrompts.empty() &&
        st.queuedPrompts.back().rfind("Plan updated:", 0) == 0)
        st.queuedPrompts.back() = std::move(note);
    else
        st.queuedPrompts.push_back(std::move(note));
    s_noteUntil = ImGui::GetTime() + 6.0;
}

void pbDropRename() { s_renameIdx = -1; s_renameFocus = false; }

// ---- status mapping ---------------------------------------------------------------
// PlanItem.status contract: pending|running|pass|fail|done|abandoned. The board shows
// the user-facing quad Pending/Active/Done/Dropped.

const char* pbStatusLabel(const std::string& s) {
    if (s == "running" || s == "active") return "Active";
    if (s == "done" || s == "pass") return "Done";
    if (s == "abandoned" || s == "dropped" || s == "fail") return "Dropped";
    return "Pending";
}

std::string pbStatusNext(const std::string& s) {
    if (s == "running" || s == "active") return "done";
    if (s == "done" || s == "pass") return "abandoned";
    if (s == "abandoned" || s == "dropped" || s == "fail") return "pending";
    return "running";
}

ImVec4 pbStatusColor(const std::string& s, const AdaptiveSurface& surf) {
    // The two SEMANTIC states go through the kit tone table, so "active" and "done" read the
    // same here as in the Copilot task list. Pending and Dropped are not statuses but weights
    // (not-yet vs. no-longer) and there is no kit Tone that separates them — they stay on the
    // surface's own ink ladder, which is what keeps them distinguishable from each other.
    if (s == "running" || s == "active") return ui::kit::toneOn(ui::kit::Tone::Accent, surf);
    if (s == "done" || s == "pass")      return ui::kit::toneOn(ui::kit::Tone::Ok, surf);
    if (s == "abandoned" || s == "dropped" || s == "fail") return surf.textTertiary;
    return surf.textSecondary;
}

// ---- small text helpers -------------------------------------------------------------

std::string pbElide(const std::string& s, float maxW) {
    return ui::kit::elide(s.c_str(), maxW);   // cuts on a UTF-8 boundary, unlike a byte pop_back
}

// "2026-07-23T14:02:11Z" -> "14:02:11" (keep the raw string when it isn't ISO-ish).
std::string pbShortTime(const std::string& iso) {
    const size_t t = iso.find('T');
    if (t == std::string::npos || t + 1 >= iso.size())
        return iso.size() > 12 ? iso.substr(0, 12) : iso;
    return iso.substr(t + 1, 8);
}

// Domain wrapper over the shared painter. `surf` is now unused: ui::kit::emptyState resolves
// its own ink from the palette. The parameter stays so the four call sites are untouched.
void pbEmptyState(const char* msg, const char* sub, const AdaptiveSurface& surf) {
    (void)surf;
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ui::kit::emptyState(nullptr, msg, sub);
    ui::kit::popDensity();
}

// ---- ai_vc timeline ----------------------------------------------------------------

struct PbCheckpoint {
    std::string id;
    int         turn = -1;
    std::string label;
    std::string when;
    int         features = 0;
    std::string kind;      // "prompt" | "tool" | ""
    int         msgIdx = -1;
};

struct PbTimeline {
    bool        loading = false;
    std::string err;
    double      lastFetch = 0.0;
    std::string lastDoc;
    std::vector<PbCheckpoint> cps;
    int         confirmIdx = -1;    // inline click-again confirm, never a modal
    double      confirmUntil = 0.0;
};
PbTimeline s_tl;

const AppState::DocTab* pbActiveProjectDoc(const AppState& st,
                                           const std::string& projectDir) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return nullptr;
    const auto& tab = st.docTabs[(size_t)st.activeTab];
    return agentDocInProject(tab.path, tab.projectDir, projectDir) ? &tab : nullptr;
}

std::string pbActiveDocId(AppState& st) {
    const auto* tab = pbActiveProjectDoc(st, convProjectDir(st));
    return tab ? tab->docId : std::string();
}

// Same "method not registered" envelopes the chat-side AI-VC code accepts (kept in
// sync with aiVcIsAbsent in ui/copilot/cards.cpp — that helper is file-static there).
bool pbTlAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        return e.find("unknown") != std::string::npos ||
               e.find("not found") != std::string::npos ||
               e.find("not_found") != std::string::npos ||
               e.find("method") != std::string::npos;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented" ||
            c == "unknown" || c.find("unknown") != std::string::npos)
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented" || reason == "no_vault")
            return true;
    }
    return false;
}

bool pbTlOk(const core::RpcResult& r, std::string& hint) {
    if (pbTlAbsent(r)) {
        s_aiVc.available = false;   // shared with the chat-side AI-VC chrome
        hint = "AI checkpoints unavailable on this worker";
        return false;
    }
    if (!r.ok) {
        hint = r.err.empty() ? "the worker did not respond" : r.err;
        return false;
    }
    if (r.result.is_object() && !r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        hint = r.result.value("hint", reason.empty() ? std::string("timeline failed")
                                                     : reason);
        return false;
    }
    return true;
}

// Throttled (~2s, one in flight — the pollSubagentList pattern tightened for the
// checkpoint strip's faster cadence).
void pbPollTimeline(AppState& st) {
    if (!st.workerStarted || !s_aiVc.available || s_tl.loading) return;
    const std::string doc = pbActiveDocId(st);
    if (doc.empty()) return; // never fetch the worker's global checkpoint timeline
    const double now = ImGui::GetTime();
    if (doc == s_tl.lastDoc && now - s_tl.lastFetch < 2.0) return;
    if (now - s_tl.lastFetch < 0.4) return;
    s_tl.loading   = true;
    s_tl.lastFetch = now;
    s_tl.lastDoc   = doc;
    nlohmann::json p = nlohmann::json::object();
    if (!doc.empty()) p["doc_id"] = doc;
    st.rpc.call("plm_timeline", p, [doc](const core::RpcResult& r) {
        s_tl.loading = false;
        if (s_tl.lastDoc != doc) return;   // doc switched mid-flight — next frame refetches
        std::string hint;
        if (!pbTlOk(r, hint)) {
            s_tl.err = hint;
            s_tl.cps.clear();
            return;
        }
        s_tl.err.clear();
        s_tl.cps.clear();
        auto it = r.result.find("checkpoints");
        if (it == r.result.end() || !it->is_array()) return;
        for (const auto& c : *it) {
            if (!c.is_object()) continue;
            PbCheckpoint cp;
            // Null-safe reads: plm_timeline emits null for session_id / msg_idx / parent, and
            // nlohmann's value() throws type_error.302 on an existing-but-null key, which kills
            // the whole parse inside the rpc callback (see cards.cpp's aiVc parse).
            auto js = [](const nlohmann::json& o, const char* k) -> std::string {
                auto it = o.find(k);
                if (it == o.end() || !it->is_string()) return std::string();
                return it->get<std::string>();
            };
            cp.id = js(c, "id");
            if (c.contains("turn") && c["turn"].is_number())
                cp.turn = c["turn"].is_number_integer() ? c["turn"].get<int>()
                                                        : (int)c["turn"].get<double>();
            cp.when = js(c, "when");
            if (cp.when.empty()) cp.when = js(c, "time");
            cp.label = js(c, "label");
            if (c.contains("features") && c["features"].is_number())
                cp.features = c["features"].get<int>();
            else if (c.contains("features_count") && c["features_count"].is_number())
                cp.features = c["features_count"].get<int>();
            cp.kind = js(c, "kind");
            if (c.contains("msg_idx") && c["msg_idx"].is_number())
                cp.msgIdx = c["msg_idx"].is_number_integer() ? c["msg_idx"].get<int>()
                                                             : (int)c["msg_idx"].get<double>();
            s_tl.cps.push_back(std::move(cp));
        }
    });
}

void pbDrawTimeline(AppState& st, const AdaptiveSurface& surf, int verbosity) {
    const float S = uiScale;
    const auto& pal = palette;

    // Shared menu gutter so the checkpoint rows sit the same distance off the glass edge as
    // every other AI-surface card list (pop right after Begin — the child captured it).
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::BeginChild("##pb_tl", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    {
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowWidth(), c0.y + ImGui::GetWindowHeight());
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
    }

    // Section header through the shared painter: the refresh state right-aligns on the header's
    // own line instead of trailing the title, so the label position never shifts while polling.
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ui::kit::sectionHeader("Checkpoints", nullptr, s_tl.loading ? "refreshing..." : nullptr,
                           /*count=*/-1, /*first=*/true);
    ui::kit::popDensity();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextWrapped("Silent agent version control — restore the document (and "
                       "chat) to before a turn or tool call.");
    ImGui::PopStyleColor();
    ImGui::Spacing();

    // Honest states first: worker down, method absent, poll error, empty vault.
    if (!st.workerStarted) {
        pbEmptyState("Worker not running.",
                     "Checkpoints appear once the agent worker is up and a run edits "
                     "the active document.", surf);
        ImGui::EndChild();
        return;
    }
    if (!s_aiVc.available) {
        pbEmptyState("AI checkpoints unavailable on this worker.",
                     "The plm_timeline/plm_rollback methods are not registered — nothing "
                     "to restore.", surf);
        ImGui::EndChild();
        return;
    }
    const std::string docId = pbActiveDocId(st);
    if (docId != s_tl.lastDoc) {
        s_tl.lastDoc = docId;  // fence an in-flight response for the previous project's doc
        s_tl.cps.clear();
        s_tl.err.clear();
    }
    if (docId.empty()) {
        pbEmptyState("No project document selected",
                     "Select a saved document in this project to see its checkpoints.", surf);
        ImGui::EndChild();
        return;
    }
    pbPollTimeline(st);
    if (!s_tl.err.empty()) {
        pbEmptyState(s_tl.err.c_str(), nullptr, surf);
        ImGui::EndChild();
        return;
    }
    if (s_tl.cps.empty()) {
        pbEmptyState("No checkpoints yet.",
                     "The agent cuts one silently before every prompt turn and every "
                     "mutating tool call on this document.", surf);
        ImGui::EndChild();
        return;
    }

    const double now = ImGui::GetTime();
    if (s_tl.confirmIdx >= (int)s_tl.cps.size() || now > s_tl.confirmUntil)
        s_tl.confirmIdx = -1;

    // Newest first.
    for (int ri = (int)s_tl.cps.size() - 1; ri >= 0; --ri) {
        const PbCheckpoint& cp = s_tl.cps[(size_t)ri];
        ImGui::PushID(ri);
        const float rowW = ImGui::GetContentRegionAvail().x;
        const float rowH = ImGui::GetFrameHeight() + ui::metrics::cpMenuRowPadY();
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(rowW, rowH));
        glassRowBackground(dl, rowMin, ImVec2(rowMin.x + rowW, rowMin.y + rowH),
                           rowRoundDp(), (ri & 1) != 0, false, 0.30f);

        float x = rowMin.x + ui::metrics::cpMenuRowPadX();
        const float y = rowMin.y + ui::metrics::cpMenuRowPadY() * 0.5f;

        // Kind chip (prompt = accent, tool = iterBuilt blue, legacy = dim).
        const bool isPrompt = cp.kind == "prompt";
        const bool isTool   = cp.kind == "tool";
        const char* kindTag = isPrompt ? "prompt" : (isTool ? "tool" : "turn");
        const ImVec4 kindCol = isPrompt
            ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f)
            : isTool ? ImVec4(pal.iterBuilt[0], pal.iterBuilt[1], pal.iterBuilt[2], 1.0f)
                     : surf.textTertiary;
        ImGui::SetCursorScreenPos(ImVec2(x, y));
        ImGui::PushStyleColor(ImGuiCol_Text, kindCol);
        ImGui::TextUnformatted(kindTag);
        ImGui::PopStyleColor();
        x += ImGui::CalcTextSize(kindTag).x + ui::metrics::cpMenuRowPadX();

        // Restore button (right edge) — inline click-again confirm, routes through the
        // shared aiVcRollback (honors chat_truncate_to, refreshes mirror, toasts).
        const bool armed = s_tl.confirmIdx == ri;
        const char* rbLabel = armed ? "Confirm?" : "Restore";
        const float rbW = ImGui::CalcTextSize(rbLabel).x + 16.0f * S +
                          ImGui::GetStyle().FramePadding.x * 2.0f;
        ImGui::SetCursorScreenPos(ImVec2(rowMin.x + rowW - rbW - 6.0f * S, y));
        ImGui::BeginDisabled(s_aiVc.busy || cp.id.empty());
        if (ImGui::SmallButton(rbLabel)) {
            if (armed) {
                s_tl.confirmIdx = -1;
                aiVcRollback(st, cp.id, "Restored checkpoint");
                s_tl.lastFetch = 0.0;   // refresh the strip within the next throttle window
            } else {
                s_tl.confirmIdx   = ri;
                s_tl.confirmUntil = now + 4.0;
            }
        }
        ImGui::EndDisabled();
        if (cp.id.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("Checkpoint has no id — cannot restore");
        const float labelMaxW = (rowMin.x + rowW - rbW - 14.0f * S) - x;

        // Label + time (+ detail at full verbosity).
        const std::string label = pbElide(cp.label.empty() ? cp.id : cp.label, labelMaxW);
        ImGui::SetCursorScreenPos(ImVec2(x, y));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted(label.c_str());
        ImGui::PopStyleColor();
        x += ImGui::CalcTextSize(label.c_str()).x + 8.0f * S;

        ImGui::SetCursorScreenPos(ImVec2(x, y));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        std::string meta;
        if (!cp.when.empty()) meta = pbShortTime(cp.when);
        if (cp.turn >= 0) {
            if (!meta.empty()) meta += " ";
            char buf[24];
            std::snprintf(buf, sizeof(buf), "turn %d", cp.turn);
            meta += buf;
        }
        if (verbosity >= 2 && cp.msgIdx >= 0) {
            char buf[24];
            std::snprintf(buf, sizeof(buf), " msg %d", cp.msgIdx);
            meta += buf;
        }
        if (verbosity >= 2 && cp.features > 0) {
            char buf[24];
            std::snprintf(buf, sizeof(buf), " - %d feat", cp.features);
            meta += buf;
        }
        ImGui::TextUnformatted(pbElide(meta, (rowMin.x + rowW - rbW - 14.0f * S) - x).c_str());
        ImGui::PopStyleColor();

        ImGui::PopID();
    }
    ImGui::EndChild();
}

// ---- plan checklist -----------------------------------------------------------------

void pbDrawPlanSection(AppState& st, const AdaptiveSurface& surf, int verbosity) {
    const float S = uiScale;
    const auto& pal = palette;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    ChatMsg* msg = pbActivePlanMsg(st);
    if (s_renameOwner != (const void*)msg) {
        s_renameOwner = (const void*)msg;
        pbDropRename();
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::BeginChild("##pb_plan", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    {
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowWidth(), c0.y + ImGui::GetWindowHeight());
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
    }

    PlanState* plan = msg ? &msg->planState : nullptr;

    // ---- header ----------------------------------------------------------------
    // Section header through the shared painter. The completion count moves INTO the trailing
    // text (it used to live only in the ring's tooltip, invisible until hovered), and the ring
    // is placed on the header's own row immediately left of it — still the app's one shared
    // circular gauge, never a bespoke bar.
    int done = 0;
    if (plan)
        for (const auto& it : plan->items)
            if (it.status == "done" || it.status == "pass") ++done;
    std::string meta;
    if (plan) {
        char buf[64];
        if (plan->items.empty())
            std::snprintf(buf, sizeof(buf), "no items · rev %d", plan->revision);
        else
            std::snprintf(buf, sizeof(buf), "%d/%d done · rev %d", done,
                          (int)plan->items.size(), plan->revision);
        meta = buf;
    }
    const ImVec2 headPos = ImGui::GetCursorScreenPos();
    const float  headW   = ImGui::GetContentRegionAvail().x;
    const float  headLh  = ImGui::GetTextLineHeight();
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ui::kit::sectionHeader(plan && !plan->title.empty() ? plan->title.c_str() : "Session Plan",
                           nullptr, meta.empty() ? nullptr : meta.c_str(),
                           /*count=*/-1, /*first=*/true);
    ui::kit::popDensity();
    if (plan && !plan->items.empty()) {
        const float frac  = (float)done / (float)plan->items.size();
        const float r     = headLh * 0.40f;
        const float metaW = ImGui::CalcTextSize(meta.c_str()).x;
        ImVec4 track = surf.textTertiary;
        track.w = 0.35f;
        drawFractionRing(dl,
                         ImVec2(headPos.x + headW - metaW - ui::metrics::cpMenuLabelGap() - r,
                                headPos.y + headLh * 0.5f),
                         r, ui::metrics::stroke(2.0f), frac,
                         ImGui::ColorConvertFloat4ToU32(track), fracArcCol(1.0f - frac));
    }
    // Live progress line (the same one-liner the Copilot plan card shows).
    if (plan && !plan->items.empty()) {
        const std::string prog = planProgressLine(*plan, msg->activity);
        if (!prog.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextWrapped("%s", prog.c_str());
            ImGui::PopStyleColor();
        }
    }
    if (plan && verbosity >= 2 && !plan->summary.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", plan->summary.c_str());
        ImGui::PopStyleColor();
    }
    // "applies next turn" note after steering a running agent.
    if (ImGui::GetTime() < s_noteUntil) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 0.95f));
        ImGui::TextUnformatted("Plan changes queued for the agent — applies next turn.");
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    // ---- no plan: honest empty state + create affordance -------------------------
    if (!plan) {
        pbEmptyState("No plan for this session yet.",
                     "Plans the agent drafts land here as an editable checklist — or "
                     "start one yourself.", surf);
        ImGui::Spacing();
        if (ImGui::SmallButton("Start a plan")) {
            // Attach to the last assistant turn when there is one (a plan belongs to a
            // turn); otherwise open a plan-only turn — persistChatHistory keeps those.
            ChatMsg* host = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser) { host = &*it; break; }
            if (!host) {
                ChatMsg m;
                m.fromUser = false;
                st.chat.push_back(std::move(m));
                appendChatRawMessage(st, st.chat.back());
                host = &st.chat.back();
            }
            PlanState ps;
            ps.present = true;
            ps.shape   = "checklist";
            ps.title   = "Session plan";
            host->planState = std::move(ps);
            pbAfterEdit(st, "started a new plan");
        }
        ImGui::EndChild();
        return;
    }

    // ---- editable rows -------------------------------------------------------------
    const float frameH = ImGui::GetFrameHeight();
    for (int i = 0; i < (int)plan->items.size(); ++i) {
        PlanItem& it = plan->items[(size_t)i];
        ImGui::PushID(it.id.empty() ? i : (int)std::hash<std::string>{}(it.id));
        ImGui::PushID(i);   // positional id too — item ids are not guaranteed unique

        const float rowW = ImGui::GetContentRegionAvail().x;
        const float rowH = frameH + ui::metrics::cpMenuRowPadY();
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(rowW, rowH));   // reserve + advance the layout cursor
        const bool isActive = it.status == "running" || it.status == "active";
        glassRowBackground(dl, rowMin, ImVec2(rowMin.x + rowW, rowMin.y + rowH),
                           rowRoundDp(), (i & 1) != 0, isActive, 0.30f);

        const float y = rowMin.y + ui::metrics::cpMenuRowPadY() * 0.5f;
        float x = rowMin.x + ui::metrics::cpMenuRowPadX();

        // -- status chip (click cycles Pending -> Active -> Done -> Dropped) --
        const char* chipLabel = pbStatusLabel(it.status);
        const ImVec4 chipCol = pbStatusColor(it.status, surf);
        const float chipW = ImGui::CalcTextSize(chipLabel).x + 22.0f * S;
        ImGui::SetCursorScreenPos(ImVec2(x, y));
        ImGui::InvisibleButton("##status", ImVec2(chipW, frameH));
        if (ImGui::IsItemClicked()) {
            it.status = pbStatusNext(it.status);
            pbAfterEdit(st, "'" + (it.title.empty() ? it.id : it.title) + "' -> " +
                            pbStatusLabel(it.status));
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to cycle status (Pending / Active / Done / Dropped)");
        {
            const ImVec2 b0 = ImGui::GetItemRectMin();
            const ImVec2 b1 = ImGui::GetItemRectMax();
            const ImU32 col = ImGui::ColorConvertFloat4ToU32(chipCol);
            const float cy = (b0.y + b1.y) * 0.5f;
            dl->AddCircleFilled(ImVec2(b0.x + 6.0f * S, cy), 3.0f * S, col);
            dl->AddText(ImVec2(b0.x + 13.0f * S,
                               cy - ImGui::GetTextLineHeight() * 0.5f),
                        col, chipLabel);
        }
        x += chipW + 8.0f * S;

        // -- row buttons on the right: reorder up/down, remove --
        const float btnW = frameH;
        const float bx0 = rowMin.x + rowW - 3.0f * (btnW + 2.0f * S) - 6.0f * S;
        const float titleW = bx0 - x - 8.0f * S;
        ImGui::SetCursorScreenPos(ImVec2(bx0, y));
        ImGui::BeginDisabled(i == 0);
        if (ImGui::SmallButton("^##up")) {
            const std::string name = it.title.empty() ? it.id : it.title;
            std::swap(plan->items[(size_t)i], plan->items[(size_t)i - 1]);
            pbDropRename();
            pbAfterEdit(st, "moved '" + name + "' up");
            ImGui::EndDisabled();
            ImGui::PopID(); ImGui::PopID();
            break;   // items mutated under us — redraw next frame
        }
        ImGui::EndDisabled();
        ImGui::SameLine(0, 2.0f * S);
        ImGui::BeginDisabled(i + 1 >= (int)plan->items.size());
        if (ImGui::SmallButton("v##down")) {
            const std::string name = it.title.empty() ? it.id : it.title;
            std::swap(plan->items[(size_t)i], plan->items[(size_t)i + 1]);
            pbDropRename();
            pbAfterEdit(st, "moved '" + name + "' down");
            ImGui::EndDisabled();
            ImGui::PopID(); ImGui::PopID();
            break;
        }
        ImGui::EndDisabled();
        ImGui::SameLine(0, 2.0f * S);
        if (ImGui::SmallButton("x##del")) {
            const std::string name = it.title.empty() ? it.id : it.title;
            plan->items.erase(plan->items.begin() + i);
            pbDropRename();
            pbAfterEdit(st, "removed '" + name + "'");
            ImGui::PopID(); ImGui::PopID();
            break;
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove item");

        // -- title: click to rename inline --
        ImGui::SetCursorScreenPos(ImVec2(x, y));
        if (s_renameIdx == i) {
            ImGui::SetNextItemWidth(titleW);
            if (s_renameFocus) {
                ImGui::SetKeyboardFocusHere();
                s_renameFocus = false;
            }
            bool commit = ImGui::InputText("##rename", s_renameBuf, sizeof(s_renameBuf),
                                           ImGuiInputTextFlags_EnterReturnsTrue);
            if (ImGui::IsItemDeactivatedAfterEdit()) commit = true;
            if (commit) {
                std::string neo(s_renameBuf);
                const size_t a = neo.find_first_not_of(" \t");
                const size_t b = neo.find_last_not_of(" \t");
                neo = (a == std::string::npos) ? std::string() : neo.substr(a, b - a + 1);
                if (!neo.empty() && neo != it.title) {
                    it.title = neo;
                    pbAfterEdit(st, "renamed item to '" + neo + "'");
                }
                pbDropRename();
            }
        } else {
            const bool isDone = it.status == "done" || it.status == "pass";
            const bool isDropped = it.status == "abandoned" || it.status == "dropped" ||
                                   it.status == "fail";
            const ImVec4 titleCol = isDropped ? surf.textTertiary
                                    : isDone    ? surf.textSecondary
                                                : surf.textPrimary;
            const std::string shown = it.title.empty() ? it.id : it.title;
            ImGui::InvisibleButton("##title", ImVec2(titleW, frameH));
            if (ImGui::IsItemClicked()) {
                s_renameIdx = i;
                s_renameFocus = true;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", shown.c_str());
            }
            if (ImGui::IsItemHovered()) {
                if (!it.desc.empty()) ImGui::SetTooltip("%s", it.desc.c_str());
                else                  ImGui::SetTooltip("Click to rename");
            }
            const ImVec2 b0 = ImGui::GetItemRectMin();
            const ImVec2 b1 = ImGui::GetItemRectMax();
            dl->PushClipRect(b0, b1, /*intersect_with_current_clip_rect=*/true);
            dl->AddText(ImVec2(b0.x, (b0.y + b1.y - ImGui::GetTextLineHeight()) * 0.5f),
                        ImGui::ColorConvertFloat4ToU32(titleCol), shown.c_str());
            dl->PopClipRect();
        }

        ImGui::PopID();
        ImGui::PopID();
    }

    // ---- add row ------------------------------------------------------------------
    ImGui::Spacing();
    if (ImGui::SmallButton("+ Add item")) {
        PlanItem it;
        it.id = "user:" + std::to_string(++s_itemCtr);
        it.title = "New item";
        it.status = "pending";
        it.kind = "task";
        plan->items.push_back(it);
        plan->revision += 1;
        s_renameIdx = (int)plan->items.size() - 1;
        s_renameFocus = true;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", "New item");
        pbAfterEdit(st, "added an item");
    }
    ImGui::EndChild();
}

}  // namespace

void drawPlanBoardView(AppState& st) {
    const int verbosity = aiUiVerbosity();

    // One adaptive glass surface for the whole pane (same recipe as Active Work).
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_plan_board_glass"), paneRect, GlassGroup::Global);

    ensureConversations(st);

    // This checklist belongs to a conversation, not to whichever document is in the viewport.
    // Never expose a parked conversation from another project while switching folders.
    const std::string projectDir = convProjectDir(st);
    const Conversation* conv =
        (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
            ? &st.conversations[st.activeConv] : nullptr;
    if (!conv || convProjectKey(projectDir).empty() ||
        convProjectKey(conv->projectDir) != convProjectKey(projectDir)) {
        const std::string name = lexicalPath(projectDir).filename().string();
        const std::string title = "Nothing for " +
            (name.empty() ? std::string("this project") : name) + " yet";
        ui::kit::emptyState(nullptr, title.c_str(), "Start a conversation in this project.");
        return;
    }

    std::string docName = "no linked document";
    if (conv->docLink.rfind("file:", 0) == 0) {
        const std::string path = conv->docLink.substr(5);
        if (agentDocInProject(path, std::string(), projectDir)) {
            docName = lexicalPath(path).filename().string();
            for (const auto& tab : st.docTabs)
                if (!tab.path.empty() && convProjectKey(tab.path) == convProjectKey(path)) {
                    docName = tab.displayTitle();
                    break;
                }
        }
    } else if (conv->docLink.rfind("doc:", 0) == 0) {
        const std::string id = conv->docLink.substr(4);
        for (const auto& tab : st.docTabs)
            if (tab.docId == id &&
                agentDocInProject(tab.path, tab.projectDir, projectDir)) {
                docName = tab.displayTitle();
                break;
            }
    }
    if (docName == "no linked document") {
        if (const auto* selected = pbActiveProjectDoc(st, projectDir))
            docName = "selected: " + selected->displayTitle();
    }
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::Text("Session Plan | %s | %s", conv->title.c_str(), docName.c_str());
    ImGui::PopStyleColor();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted(aiTurnActive(st) ? "Agent running; edits queue for its next turn."
                                            : "Edits save with this session.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));

    pbDrawPlanSection(st, surf, verbosity);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
    pbDrawTimeline(st, surf, verbosity);
}

nlohmann::json planBoardSerialize(const AppState& st, const std::string& convId) {
    nlohmann::json out = nlohmann::json::object();
    out["conv_id"] = convId;
    out["present"] = false;
    const std::vector<ChatMsg>* msgs = pbMsgsForConv(st, convId);
    if (!msgs) return out;
    const PlanState* plan = nullptr;
    for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
        if (it->planState.present) { plan = &it->planState; break; }
    if (!plan) return out;
    out["present"] = true;
    out["plan"] = pbPlanToJson(*plan);
    return out;
}

void planBoardApply(AppState& st, const std::string& convId, const nlohmann::json& j) {
    std::vector<ChatMsg>* msgs = pbMsgsForConv(st, convId);
    if (!msgs) return;
    if (!j.is_object() || !j.value("present", false)) return;
    auto pj = j.find("plan");
    if (pj == j.end() || !pj->is_object()) return;
    PlanState ps = parsePlanState(*pj);
    if (!ps.present) return;
    // Newest plan-carrying message wins (same rule the live view uses).
    for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
        if (it->planState.present) { it->planState = std::move(ps); return; }
    // No carrier: attach to the last assistant turn so the plan still surfaces.
    for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
        if (!it->fromUser) { it->planState = std::move(ps); return; }
    ChatMsg m;
    m.fromUser = false;
    m.planState = std::move(ps);
    msgs->push_back(std::move(m));
}

}  // namespace ui
