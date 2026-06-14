// AI-CAD Agent Activity panel (C22b).
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

// --- platform includes for run directory listing ---
#ifndef WIN32_LEAN_AND_MEAN
#  define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#  define NOMINMAX
#endif
#include <windows.h>

namespace ui {

// =========================================================================
// helpers
// =========================================================================

static long long nowMs() {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return (long long)(u.QuadPart / 10000LL);
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
    } else if (kind == "run_finished") {
        liveRun_.status     = jstr(data, "status");
        liveRun_.stopReason = jstr(data, "stop_reason");
        liveRun_.revision   = jint(data, "revision");
        // clear any still-active stage
        for (auto& sn : liveRun_.stages) sn.active = false;
        activeStageIdx_ = -1;
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

    std::string runsDir = appdataDir() + "\\AI-CAD\\cache\\runs";
    WIN32_FIND_DATAA fd;
    std::string pat = runsDir + "\\*";
    HANDLE h = FindFirstFileA(pat.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return;

    struct Entry { std::string id; std::string jsonlPath; FILETIME ft; };
    std::vector<Entry> entries;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        if (strcmp(fd.cFileName, ".") == 0 || strcmp(fd.cFileName, "..") == 0) continue;
        std::string runId = fd.cFileName;
        std::string jp = runsDir + "\\" + runId + "\\events.jsonl";
        // Only include if events.jsonl exists
        DWORD attr = GetFileAttributesA(jp.c_str());
        if (attr == INVALID_FILE_ATTRIBUTES) continue;
        Entry e;
        e.id = runId;
        e.jsonlPath = jp;
        e.ft = fd.ftLastWriteTime;
        entries.push_back(std::move(e));
    } while (FindNextFileA(h, &fd));
    FindClose(h);

    // Sort newest first
    std::sort(entries.begin(), entries.end(), [](const Entry& a, const Entry& b) {
        if (a.ft.dwHighDateTime != b.ft.dwHighDateTime)
            return a.ft.dwHighDateTime > b.ft.dwHighDateTime;
        return a.ft.dwLowDateTime > b.ft.dwLowDateTime;
    });

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
        // Raw output (collapsible, capped scroll)
        if (!attempt.rawOutput.empty()) {
            char rawId[64];
            std::snprintf(rawId, sizeof(rawId), "Raw output##ro%p", (void*)&attempt);
            if (toggleChip(attempt.showRaw ? "Hide output" : "Show output", attempt.showRaw))
                attempt.showRaw = !attempt.showRaw;
            if (attempt.showRaw) {
                ImGui::PushStyleColor(ImGuiCol_ChildBg,
                    ImVec4(p.codeBg[0], p.codeBg[1], p.codeBg[2], p.codeBg[3]));
                float rawH = 140.0f;
                ImGui::BeginChild(rawId, ImVec2(0, rawH), ImGuiChildFlags_Border,
                                  ImGuiWindowFlags_HorizontalScrollbar);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                ImGui::TextUnformatted(attempt.rawOutput.c_str());
                if (st.fonts.mono) ImGui::PopFont();
                ImGui::EndChild();
                ImGui::PopStyleColor();
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
        float alpha = 0.55f + 0.45f * std::sin(pulseT_ * 4.0f);
        ImGui::PushStyleColor(ImGuiCol_Header,
            ImVec4(p.accent[0], p.accent[1], p.accent[2], alpha * 0.25f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(p.accent[0], p.accent[1], p.accent[2], 1));
        char slbl[128];
        std::snprintf(slbl, sizeof(slbl), ">> %s  (running...)##st%p",
                      stage.name.c_str(), (void*)&stage);
        open = ImGui::TreeNodeEx(slbl,
                   ImGuiTreeNodeFlags_SpanAvailWidth | ImGuiTreeNodeFlags_DefaultOpen);
        ImGui::PopStyleColor(2);
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
