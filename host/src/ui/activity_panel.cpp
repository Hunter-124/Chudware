// Chudware Agent Activity panel (C22b).
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <fstream>
#include <string>
#include <sstream>
#include <vector>

#include "imgui.h"

#include "activity_panel.h"
#include "metrics.h"
#include "../core/app_state.h"

// --- run-directory listing + timing (portable) ---
#include <chrono>
#include <filesystem>

namespace ui {

// =========================================================================
// helpers
// =========================================================================

static long long nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// =========================================================================
// onEvent — feed one live orchestrator event (GL thread)
// =========================================================================

void ActivityPanel::onEvent(const std::string& kind, const nlohmann::json& data) {
    if (kind == "run_started") {
        clearLive();
        hasLiveRun_ = true;
        liveRun_.runId   = jstr(data, "run_id");
        liveRun_.request = jstr(data, "request");
        liveRun_.docId   = jstr(data, "doc_id");
        liveRun_.status  = "running";
        // Pre-populate stage nodes from the stages array if present
        if (data.contains("stages") && data["stages"].is_array()) {
            for (auto& sn : data["stages"]) {
                StageNode st2;
                st2.name = sn.is_string() ? sn.get<std::string>() : "";
                liveRun_.stages.push_back(std::move(st2));
            }
        }
        activeStageIdx_ = -1;
    } else if (kind == "stage_started") {
        std::string stageName = jstr(data, "stage");
        // find or create the stage node
        int idx = -1;
        for (int i = 0; i < (int)liveRun_.stages.size(); ++i)
            if (liveRun_.stages[i].name == stageName) { idx = i; break; }
        if (idx == -1) {
            StageNode sn;
            sn.name = stageName;
            liveRun_.stages.push_back(std::move(sn));
            idx = (int)liveRun_.stages.size() - 1;
        }
        liveRun_.stages[idx].role   = jstr(data, "role");
        liveRun_.stages[idx].active = true;
        // clear active on the previously active stage
        for (int i = 0; i < (int)liveRun_.stages.size(); ++i)
            if (i != idx) liveRun_.stages[i].active = false;
        activeStageIdx_ = idx;
    } else if (kind == "agent_call") {
        std::string stageName = jstr(data, "stage");
        for (auto& sn : liveRun_.stages) {
            if (sn.name == stageName) {
                AttemptNode an;
                an.role             = jstr(data, "role");
                an.promptFile       = jstr(data, "prompt_file");
                an.contextChars     = jint(data, "context_chars");
                an.contextTokensEst = jint(data, "context_tokens_est");
                sn.attempts.push_back(std::move(an));
                break;
            }
        }
    } else if (kind == "agent_output") {
        std::string stageName = jstr(data, "stage");
        int attempt = jint(data, "attempt");
        for (auto& sn : liveRun_.stages) {
            if (sn.name != stageName) continue;
            // find the matching attempt (0-based; we may need to create it)
            while ((int)sn.attempts.size() <= attempt) sn.attempts.emplace_back();
            auto& an       = sn.attempts[attempt];
            an.rawOutput   = jstr(data, "raw");
            an.sentPrompt  = jstr(data, "sent");
            an.reply       = an.rawOutput;
            an.parsedOk    = jbool(data, "parsed_ok");
            an.parseError  = jstr(data, "parse_error");
            an.durationMs  = jint(data, "duration_ms");
            an.opsCount    = jint(data, "ops_count");
            if (data.contains("tokens") && data["tokens"].is_object()) {
                an.tokensIn  = jint(data["tokens"], "in");
                an.tokensOut = jint(data["tokens"], "out");
            }
            break;
        }
    } else if (kind == "stage_result") {
        std::string stageName = jstr(data, "stage");
        for (auto& sn : liveRun_.stages) {
            if (sn.name == stageName) {
                sn.finalVerdict = jstr(data, "verdict");
                sn.finalReason  = jstr(data, "reason");
                sn.active       = false;
                break;
            }
        }
        if (activeStageIdx_ >= 0 &&
            activeStageIdx_ < (int)liveRun_.stages.size() &&
            liveRun_.stages[activeStageIdx_].name == stageName) {
            activeStageIdx_ = -1;
        }
    } else if (kind == "run_finished" || kind == "mission_finished") {
        liveRun_.status     = jstr(data, "status");
        liveRun_.stopReason = jstr(data, "stop_reason");
        liveRun_.revision   = jint(data, "revision");
        // clear any still-active stage
        for (auto& sn : liveRun_.stages) sn.active = false;
        activeStageIdx_ = -1;
    } else if (kind == "swarm_wave") {
        // Surface as a synthetic stage so the activity tree shows wave headers.
        int wave = jint(data, "wave");
        int total = jint(data, "total_waves");
        char name[64];
        if (total > 0) std::snprintf(name, sizeof(name), "wave_%d/%d", wave, total);
        else std::snprintf(name, sizeof(name), "wave_%d", wave > 0 ? wave : 1);
        StageNode sn;
        sn.name = name;
        sn.role = "swarm";
        sn.active = true;
        sn.finalVerdict = "running";
        // One-line task summary into finalReason.
        std::string reason;
        if (data.contains("tasks") && data["tasks"].is_array()) {
            for (const auto& t : data["tasks"]) {
                if (!t.is_object()) continue;
                std::string tt = jstr(t, "title");
                if (tt.empty()) tt = jstr(t, "capability");
                if (tt.empty()) continue;
                if (!reason.empty()) reason += " · ";
                reason += tt;
            }
        }
        sn.finalReason = reason;
        for (auto& s : liveRun_.stages) s.active = false;
        liveRun_.stages.push_back(std::move(sn));
        activeStageIdx_ = (int)liveRun_.stages.size() - 1;
        if (!hasLiveRun_) { hasLiveRun_ = true; liveRun_.status = "running"; }
    } else if (kind == "firm_bids" || kind == "firm_award" || kind == "firm_team" ||
               kind == "firm_comm" || kind == "advisor" || kind == "sub_run") {
        // Compact stage-ish rows so the side panel isn't silent; chat owns the rich cards.
        StageNode sn;
        sn.name = kind;
        sn.role = (kind.rfind("firm_", 0) == 0) ? "eng_firm"
                : (kind == "advisor") ? "advisor" : "sub_run";
        sn.active = false;
        sn.finalVerdict = "done";
        if (kind == "firm_award") {
            std::string firm = jstr(data, "firm");
            sn.finalReason = firm.empty() ? "awarded" : ("awarded " + firm);
        } else if (kind == "advisor") {
            sn.finalReason = jstr(data, "stage");
            if (sn.finalReason.empty()) sn.finalReason = jstr(data, "question");
        } else if (kind == "sub_run") {
            sn.finalReason = jstr(data, "purpose");
            std::string stt = jstr(data, "status");
            if (!stt.empty()) sn.finalVerdict = stt;
        } else if (kind == "firm_bids") {
            sn.finalReason = jstr(data, "domain");
        } else if (kind == "firm_team") {
            int n = 0;
            if (data.contains("members") && data["members"].is_array())
                n = (int)data["members"].size();
            char buf[48];
            std::snprintf(buf, sizeof(buf), "%d members", n);
            sn.finalReason = buf;
        } else if (kind == "firm_comm") {
            // Don't spam a stage per message — skip if last stage is already firm_comm.
            if (!liveRun_.stages.empty() && liveRun_.stages.back().name == "firm_comm")
                return;
            sn.finalReason = "internal comms";
        }
        liveRun_.stages.push_back(std::move(sn));
        if (!hasLiveRun_) { hasLiveRun_ = true; liveRun_.status = "running"; }
    }
}

void ActivityPanel::clearLive() {
    liveRun_       = RunNode{};
    hasLiveRun_    = false;
    activeStageIdx_ = -1;
}

// =========================================================================
// replay helpers
// =========================================================================

void ActivityPanel::refreshReplayList() {
    long long now = nowMs();
    if (now - lastListMs_ < 3000) return;  // throttle to once per 3 s
    lastListMs_ = now;

    replayList_.clear();

    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path runsDir = fs::path(appdataAppDir()) / "cache" / "runs";
    if (!fs::is_directory(runsDir, ec)) return;

    struct Entry { std::string id; std::string jsonlPath; fs::file_time_type ft; };
    std::vector<Entry> entries;
    for (fs::directory_iterator it(runsDir, ec), end; it != end; it.increment(ec)) {
        if (ec) break;
        if (!it->is_directory(ec)) continue;
        std::string runId = it->path().filename().string();
        fs::path jp = it->path() / "events.jsonl";
        if (!fs::exists(jp, ec)) continue;  // only runs that actually logged events
        Entry e;
        e.id = runId;
        e.jsonlPath = jp.string();
        e.ft = fs::last_write_time(it->path(), ec);
        entries.push_back(std::move(e));
    }

    // Sort newest first
    std::sort(entries.begin(), entries.end(),
              [](const Entry& a, const Entry& b) { return a.ft > b.ft; });

    for (auto& e : entries) {
        ReplayEntry re;
        re.runId     = e.id;
        re.jsonlPath = e.jsonlPath;
        // Build a short label: "run_abc... (truncated)"
        std::string shortId = e.id.size() > 16 ? e.id.substr(0, 16) + "..." : e.id;
        re.label = shortId;
        replayList_.push_back(std::move(re));
    }
}

void ActivityPanel::loadReplay(const std::string& path) {
    replayRun_ = RunNode{};
    replayLoaded_ = false;

    std::ifstream f(path);
    if (!f.is_open()) return;

    std::vector<nlohmann::json> events;
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        try {
            events.push_back(nlohmann::json::parse(line));
        } catch (...) {}
    }
    if (events.empty()) return;

    // Parse events into a RunNode
    replayRun_ = parseEvents(events);
    replayLoaded_ = true;
}

ActivityPanel::RunNode ActivityPanel::parseEvents(const std::vector<nlohmann::json>& events) {
    RunNode run;
    for (auto& ev : events) {
        // Events may use "kind" (live) or "event" (worker envelope)
        std::string k = jstr(ev, "kind");
        if (k.empty()) k = jstr(ev, "event");
        // data may be top-level (live) or under "data" (worker envelope)
        const nlohmann::json* d = &ev;
        if (ev.contains("data") && ev["data"].is_object()) d = &ev["data"];

        if (k == "run_started") {
            run.runId   = jstr(*d, "run_id");
            run.request = jstr(*d, "request");
            run.docId   = jstr(*d, "doc_id");
            run.status  = "unknown";
        } else if (k == "stage_started") {
            std::string sn = jstr(*d, "stage");
            bool found = false;
            for (auto& s : run.stages) if (s.name == sn) { found = true; break; }
            if (!found) {
                StageNode st2;
                st2.name = sn;
                st2.role = jstr(*d, "role");
                run.stages.push_back(st2);
            }
        } else if (k == "agent_call") {
            std::string sn = jstr(*d, "stage");
            for (auto& s : run.stages) {
                if (s.name != sn) continue;
                AttemptNode an;
                an.role             = jstr(*d, "role");
                an.promptFile       = jstr(*d, "prompt_file");
                an.contextChars     = jint(*d, "context_chars");
                an.contextTokensEst = jint(*d, "context_tokens_est");
                s.attempts.push_back(an);
                break;
            }
        } else if (k == "agent_output") {
            std::string sn  = jstr(*d, "stage");
            int attempt     = jint(*d, "attempt");
            for (auto& s : run.stages) {
                if (s.name != sn) continue;
                while ((int)s.attempts.size() <= attempt) s.attempts.emplace_back();
                auto& an       = s.attempts[attempt];
                an.rawOutput   = jstr(*d, "raw");
                an.sentPrompt  = jstr(*d, "sent");
                an.reply       = an.rawOutput;
                an.parsedOk    = jbool(*d, "parsed_ok");
                an.parseError  = jstr(*d, "parse_error");
                an.durationMs  = jint(*d, "duration_ms");
                an.opsCount    = jint(*d, "ops_count");
                if (d->contains("tokens") && (*d)["tokens"].is_object()) {
                    an.tokensIn  = jint((*d)["tokens"], "in");
                    an.tokensOut = jint((*d)["tokens"], "out");
                }
                break;
            }
        } else if (k == "stage_result") {
            std::string sn = jstr(*d, "stage");
            for (auto& s : run.stages) {
                if (s.name == sn) {
                    s.finalVerdict = jstr(*d, "verdict");
                    s.finalReason  = jstr(*d, "reason");
                    // Propagate verdict to the last attempt
                    if (!s.attempts.empty()) {
                        s.attempts.back().verdict = s.finalVerdict;
                        s.attempts.back().reason  = s.finalReason;
                    }
                    break;
                }
            }
        } else if (k == "run_finished") {
            run.status     = jstr(*d, "status");
            run.stopReason = jstr(*d, "stop_reason");
            run.revision   = jint(*d, "revision");
        }
    }
    return run;
}

// =========================================================================
// draw helpers
// =========================================================================

static void verdictChip(const std::string& verdict) {
    if (verdict.empty()) return;
    const auto& p = ui::palette;
    ImVec4 col;
    if (verdict == "pass")
        col = ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1);
    else if (verdict == "retry")
        col = ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1);
    else
        col = ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1);
    ImGui::SameLine();
    ImGui::TextColored(col, "[%s]", verdict.c_str());
}

void ActivityPanel::drawAttempt(AppState& st, AttemptNode& attempt, int idx) {
    const auto& p = ui::palette;
    char label[64];
    std::snprintf(label, sizeof(label), "Attempt %d##att%d%p", idx + 1, idx, (void*)&attempt);
    bool open = ImGui::TreeNodeEx(label, ImGuiTreeNodeFlags_SpanAvailWidth);
    verdictChip(attempt.verdict);
    if (!attempt.parsedOk) {
        ImGui::SameLine();
        ImGui::TextColored(ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1),
                           "[parse error]");
    }
    if (open) {
        // Role + prompt_file (click = open editor)
        if (!attempt.role.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::Text("Role: ");
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 0);
            ImGui::TextUnformatted(attempt.role.c_str());
            if (!attempt.promptFile.empty()) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(p.accent[0], p.accent[1], p.accent[2], 1));
                char pbtn[64];
                std::snprintf(pbtn, sizeof(pbtn), "[edit prompt]##pe%p", (void*)&attempt);
                if (ImGui::SmallButton(pbtn)) {
                    // C22b prompt editor: open Settings > Agents tab on this role
                    st.openSettings = true;
                    st.pendingPromptRole = attempt.role;
                }
                ImGui::PopStyleColor();
            }
        }
        // Context size
        if (attempt.contextChars > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::Text("Context: %d chars  (~%d tokens est)",
                        attempt.contextChars, attempt.contextTokensEst);
            ImGui::PopStyleColor();
        }
        // Duration
        if (attempt.durationMs > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::Text("Duration: %d ms", attempt.durationMs);
            if (attempt.tokensIn > 0 || attempt.tokensOut > 0) {
                ImGui::SameLine();
                ImGui::Text("  tokens: %d in / %d out", attempt.tokensIn, attempt.tokensOut);
            }
            ImGui::PopStyleColor();
        }
        // Verdict + reason
        if (!attempt.verdict.empty()) {
            verdictChip(attempt.verdict);
            if (!attempt.reason.empty()) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
                ImGui::TextWrapped("%s", attempt.reason.c_str());
                ImGui::PopStyleColor();
            }
        }
        // Ops count
        if (attempt.opsCount > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::Text("Ops: %d", attempt.opsCount);
            ImGui::PopStyleColor();
        }
        // Parse error
        if (!attempt.parsedOk && !attempt.parseError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1));
            ImGui::TextWrapped("Parse error: %s", attempt.parseError.c_str());
            ImGui::PopStyleColor();
        }
        // Model I/O — expandable sent prompt + raw reply (drawProseBox scrollable body)
        if (!attempt.sentPrompt.empty()) {
            char sentId[64];
            std::snprintf(sentId, sizeof(sentId), "Sent to model##sent%p", (void*)&attempt);
            if (ImGui::TreeNodeEx(sentId, ImGuiTreeNodeFlags_SpanAvailWidth)) {
                char boxId[64];
                std::snprintf(boxId, sizeof(boxId), "##sentbox%p", (void*)&attempt);
                drawProseBox(boxId, attempt.sentPrompt, ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        }
        {
            const std::string& replyTxt =
                !attempt.reply.empty() ? attempt.reply : attempt.rawOutput;
            if (!replyTxt.empty()) {
                char replyId[64];
                std::snprintf(replyId, sizeof(replyId), "Model reply##reply%p", (void*)&attempt);
                if (ImGui::TreeNodeEx(replyId, ImGuiTreeNodeFlags_SpanAvailWidth)) {
                    char boxId[64];
                    std::snprintf(boxId, sizeof(boxId), "##replybox%p", (void*)&attempt);
                    drawProseBox(boxId, replyTxt, ui::metrics::copilotProseMaxH());
                    ImGui::TreePop();
                }
            }
        }
        ImGui::TreePop();
    }
}

void ActivityPanel::drawStage(AppState& st, StageNode& stage, bool isActive, bool live) {
    const auto& p = ui::palette;
    bool open = false;
    if (isActive && live) {
        // Pulsing chip on the active stage (accent color)
        float pulse = 0.70f + 0.30f * std::sin(pulseT_ * 4.0f);
        ImGui::PushStyleColor(ImGuiCol_Header,
            ImVec4(p.glassBg[0], p.glassBg[1], p.glassBg[2], p.glassBg[3] * pulse));
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * ui::uiScale);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(p.accent[0], p.accent[1], p.accent[2], 1));
        char slbl[128];
        std::snprintf(slbl, sizeof(slbl), ">> %s  (running...)##st%p",
                      stage.name.c_str(), (void*)&stage);
        open = ImGui::TreeNodeEx(slbl,
                   ImGuiTreeNodeFlags_SpanAvailWidth | ImGuiTreeNodeFlags_DefaultOpen);
        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar();
    } else {
        char slbl[128];
        std::snprintf(slbl, sizeof(slbl), "%s##st%p", stage.name.c_str(), (void*)&stage);
        open = ImGui::TreeNodeEx(slbl, ImGuiTreeNodeFlags_SpanAvailWidth);
        // Verdict chip for finished stages
        if (!stage.finalVerdict.empty()) verdictChip(stage.finalVerdict);
    }
    if (open) {
        if (!stage.finalReason.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::TextWrapped("%s", stage.finalReason.c_str());
            ImGui::PopStyleColor();
        }
        for (int i = 0; i < (int)stage.attempts.size(); ++i)
            drawAttempt(st, stage.attempts[i], i);
        ImGui::TreePop();
    }
}

void ActivityPanel::drawRunTree(AppState& st, RunNode& run, bool live) {
    const auto& p = ui::palette;

    // Run header
    {
        ImVec4 statusCol;
        if (run.status == "done")
            statusCol = ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1);
        else if (run.status == "failed" || run.status == "cancelled")
            statusCol = ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1);
        else
            statusCol = ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1);

        std::string shortId = run.runId.size() > 12 ? run.runId.substr(0, 12) + "..." : run.runId;
        ImGui::PushStyleColor(ImGuiCol_Text, statusCol);
        ImGui::Text("[%s]  %s", run.status.empty() ? "running" : run.status.c_str(),
                    shortId.c_str());
        ImGui::PopStyleColor();
    }
    if (!run.request.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
        ImGui::TextWrapped("Request: %s", run.request.c_str());
        ImGui::PopStyleColor();
    }
    if (run.revision > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
        ImGui::Text("Revision: %d", run.revision);
        ImGui::PopStyleColor();
    }
    if (!run.stopReason.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
        ImGui::Text("Stop reason: %s", run.stopReason.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, ui::metrics::U()));

    for (auto& sn : run.stages) {
        bool isActive = live && sn.active;
        drawStage(st, sn, isActive, live);
    }
}

// =========================================================================
// draw (main entry point — called every frame from app.cpp)
// =========================================================================

void ActivityPanel::draw(AppState& st) {
    if (!st.showActivityPanel) return;
    ImGui::SetNextWindowSize(ImVec2(340 * ui::uiScale, 500 * ui::uiScale), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (!ImGui::Begin("Agent Activity", &st.showActivityPanel)) {
        ImGui::End();
        return;
    }
    drawBody(st);
    ImGui::End();
}

void ActivityPanel::drawBody(AppState& st) {
    pulseT_ += ImGui::GetIO().DeltaTime;

    const auto& p = ui::palette;

    // ---- Open run combo ----
    refreshReplayList();
    {
        ImGui::TextUnformatted("Open run:");
        ImGui::SameLine();
        ImGui::SetNextItemWidth(-1);
        const char* preview = selectedReplay_ < 0 ? "(live run)" :
            (selectedReplay_ < (int)replayList_.size() ?
                replayList_[selectedReplay_].label.c_str() : "(live run)");
        if (ImGui::BeginCombo("##runcombo", preview)) {
            if (ImGui::Selectable("(live run)", selectedReplay_ < 0)) {
                selectedReplay_ = -1;
                replayLoaded_   = false;
            }
            for (int i = 0; i < (int)replayList_.size(); ++i) {
                bool sel = (i == selectedReplay_);
                if (ImGui::Selectable(replayList_[i].label.c_str(), sel)) {
                    selectedReplay_ = i;
                    loadReplay(replayList_[i].jsonlPath);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
    }
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, ui::metrics::U()));

    // ---- Content ----
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##actcontent", ImVec2(0, 0), ImGuiChildFlags_None);

    if (selectedReplay_ < 0) {
        // Live run view
        if (!hasLiveRun_) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1));
            ImGui::TextWrapped("No agent run in progress.\n"
                               "Use \"Design with AI\" (v2 mode) to start one.");
            ImGui::PopStyleColor();
        } else {
            drawRunTree(st, liveRun_, /*live=*/true);
        }
    } else {
        // Replay view
        if (!replayLoaded_) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1));
            ImGui::TextUnformatted("Failed to load events.jsonl.");
            ImGui::PopStyleColor();
        } else {
            drawRunTree(st, replayRun_, /*live=*/false);
        }
    }

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

}  // namespace ui
