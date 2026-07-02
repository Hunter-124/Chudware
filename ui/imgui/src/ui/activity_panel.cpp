// Agent Activity panel — orchestrator event tree + jsonl replay.
#include "activity_panel.h"
#include "settings_modal.h"
#include "theme/theme.h"
#include "app/app_state.h"
#include "worker_types.h"

#include <platform/paths.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "imgui.h"

namespace ui {

namespace {

ActivityPanel gPanel;

static long long nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

static bool toggleChip(const char* label, bool active) {
    if (active) ImGui::PushStyleColor(ImGuiCol_Button, TVEC(bg2));
    bool clicked = ImGui::SmallButton(label);
    if (active) ImGui::PopStyleColor();
    return clicked;
}

}  // namespace

ActivityPanel& activityPanel() { return gPanel; }

void ActivityPanel::onEvent(const std::string& kind, const nlohmann::json& data) {
    if (kind == "run_started") {
        clearLive();
        hasLiveRun_ = true;
        liveRun_.runId   = jstr(data, "run_id");
        liveRun_.request = jstr(data, "request");
        liveRun_.docId   = jstr(data, "doc_id");
        liveRun_.status  = "running";
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
            liveRun_.stages[activeStageIdx_].name == stageName)
            activeStageIdx_ = -1;
    } else if (kind == "run_finished") {
        liveRun_.status     = jstr(data, "status");
        liveRun_.stopReason = jstr(data, "stop_reason");
        liveRun_.revision   = jint(data, "revision");
        for (auto& sn : liveRun_.stages) sn.active = false;
        activeStageIdx_ = -1;
    }
}

void ActivityPanel::clearLive() {
    liveRun_        = RunNode{};
    hasLiveRun_     = false;
    activeStageIdx_ = -1;
}

void ActivityPanel::refreshReplayList() {
    long long now = nowMs();
    if (now - lastListMs_ < 3000) return;
    lastListMs_ = now;
    replayList_.clear();

    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path runsDir = fs::path(platform::appdataDir()) / "AI-CAD" / "cache" / "runs";
    if (!fs::is_directory(runsDir, ec)) return;

    struct Entry { std::string id; std::string jsonlPath; fs::file_time_type ft; };
    std::vector<Entry> entries;
    for (fs::directory_iterator it(runsDir, ec), end; it != end; it.increment(ec)) {
        if (ec) break;
        if (!it->is_directory(ec)) continue;
        std::string runId = it->path().filename().string();
        fs::path jp = it->path() / "events.jsonl";
        if (!fs::exists(jp, ec)) continue;
        Entry e;
        e.id = runId;
        e.jsonlPath = jp.string();
        e.ft = fs::last_write_time(it->path(), ec);
        entries.push_back(std::move(e));
    }
    std::sort(entries.begin(), entries.end(),
              [](const Entry& a, const Entry& b) { return a.ft > b.ft; });
    for (auto& e : entries) {
        ReplayEntry re;
        re.runId     = e.id;
        re.jsonlPath = e.jsonlPath;
        re.label = e.id.size() > 16 ? e.id.substr(0, 16) + "..." : e.id;
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
        try { events.push_back(nlohmann::json::parse(line)); } catch (...) {}
    }
    if (events.empty()) return;
    replayRun_ = parseEvents(events);
    replayLoaded_ = true;
}

ActivityPanel::RunNode ActivityPanel::parseEvents(const std::vector<nlohmann::json>& events) {
    RunNode run;
    for (auto& ev : events) {
        std::string k = jstr(ev, "kind");
        if (k.empty()) k = jstr(ev, "event");
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

namespace {

static void verdictChip(const std::string& verdict) {
    if (verdict.empty()) return;
    ImVec4 col = TVEC(txt1);
    if (verdict == "pass") col = TVEC(good);
    else if (verdict == "retry") col = TVEC(warn);
    else col = TVEC(bad);
    ImGui::SameLine();
    ImGui::TextColored(col, "[%s]", verdict.c_str());
}

}  // namespace

void ActivityPanel::drawAttempt(app::AppState& /*st*/, AttemptNode& attempt, int idx) {
    char label[64];
    std::snprintf(label, sizeof(label), "Attempt %d##att%d%p", idx + 1, idx, (void*)&attempt);
    bool open = ImGui::TreeNodeEx(label, ImGuiTreeNodeFlags_SpanAvailWidth);
    verdictChip(attempt.verdict);
    if (!attempt.parsedOk) {
        ImGui::SameLine();
        ImGui::TextColored(TVEC(warn), "[parse error]");
    }
    if (open) {
        if (!attempt.role.empty()) {
            ImGui::TextColored(TVEC(txt2), "Role: ");
            ImGui::SameLine(0, 0);
            ImGui::TextUnformatted(attempt.role.c_str());
            if (!attempt.promptFile.empty()) {
                ImGui::SameLine();
                char pbtn[64];
                std::snprintf(pbtn, sizeof(pbtn), "[edit prompt]##pe%p", (void*)&attempt);
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
                if (ImGui::SmallButton(pbtn)) {
                    settingsUi().openSettings = true;
                    settingsUi().openSettingsTab = 2;
                    settingsUi().pendingPromptRole = attempt.role;
                }
                ImGui::PopStyleColor();
            }
        }
        if (attempt.contextChars > 0)
            ImGui::TextColored(TVEC(txt2), "Context: %d chars  (~%d tokens est)",
                               attempt.contextChars, attempt.contextTokensEst);
        if (attempt.durationMs > 0) {
            ImGui::TextColored(TVEC(txt2), "Duration: %d ms", attempt.durationMs);
            if (attempt.tokensIn > 0 || attempt.tokensOut > 0) {
                ImGui::SameLine();
                ImGui::TextColored(TVEC(txt2), "  tokens: %d in / %d out",
                                   attempt.tokensIn, attempt.tokensOut);
            }
        }
        if (!attempt.verdict.empty()) {
            verdictChip(attempt.verdict);
            if (!attempt.reason.empty()) {
                ImGui::SameLine();
                ImGui::TextWrapped("%s", attempt.reason.c_str());
            }
        }
        if (attempt.opsCount > 0)
            ImGui::TextColored(TVEC(txt2), "Ops: %d", attempt.opsCount);
        if (!attempt.parsedOk && !attempt.parseError.empty())
            ImGui::TextColored(TVEC(warn), "Parse error: %s", attempt.parseError.c_str());
        if (!attempt.rawOutput.empty()) {
            char rawId[64];
            std::snprintf(rawId, sizeof(rawId), "Raw output##ro%p", (void*)&attempt);
            if (toggleChip(attempt.showRaw ? "Hide output" : "Show output", attempt.showRaw))
                attempt.showRaw = !attempt.showRaw;
            if (attempt.showRaw) {
                theme::pushGlassChildStyle(6.f * theme::S());
                ImGui::BeginChild(rawId, ImVec2(0, 140.f * theme::S()), ImGuiChildFlags_Borders,
                                  ImGuiWindowFlags_HorizontalScrollbar);
                ImGui::PushFont(theme::font(theme::Font::Mono));
                ImGui::TextUnformatted(attempt.rawOutput.c_str());
                ImGui::PopFont();
                ImGui::EndChild();
                theme::popGlassChildStyle();
            }
        }
        ImGui::TreePop();
    }
}

void ActivityPanel::drawStage(app::AppState& st, StageNode& stage, bool isActive, bool live) {
    const float S = theme::S();
    bool open = false;
    if (isActive && live) {
        float pulse = 0.70f + 0.30f * std::sin(pulseT_ * 4.0f);
        ImGui::PushStyleColor(ImGuiCol_Header, theme::roleVec(theme::pal().glassBg, pulse));
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
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
        if (!stage.finalVerdict.empty()) verdictChip(stage.finalVerdict);
    }
    if (open) {
        if (!stage.finalReason.empty())
            ImGui::TextWrapped("%s", stage.finalReason.c_str());
        for (int i = 0; i < (int)stage.attempts.size(); ++i)
            drawAttempt(st, stage.attempts[i], i);
        ImGui::TreePop();
    }
}

void ActivityPanel::drawRunTree(app::AppState& st, RunNode& run, bool live) {
    {
        ImVec4 statusCol = TVEC(warn);
        if (run.status == "done") statusCol = TVEC(good);
        else if (run.status == "failed" || run.status == "cancelled") statusCol = TVEC(bad);
        std::string shortId = run.runId.size() > 12 ? run.runId.substr(0, 12) + "..." : run.runId;
        ImGui::TextColored(statusCol, "[%s]  %s",
                           run.status.empty() ? "running" : run.status.c_str(), shortId.c_str());
    }
    if (!run.request.empty())
        ImGui::TextWrapped("Request: %s", run.request.c_str());
    if (run.revision > 0)
        ImGui::TextColored(TVEC(txt2), "Revision: %d", run.revision);
    if (!run.stopReason.empty())
        ImGui::TextColored(TVEC(txt2), "Stop reason: %s", run.stopReason.c_str());
    ImGui::Dummy(ImVec2(0, theme::U()));
    for (auto& sn : run.stages) {
        bool isActive = live && sn.active;
        drawStage(st, sn, isActive, live);
    }
}

void ActivityPanel::draw(app::AppState& st) {
    const float S = theme::S();
    ImGui::SetNextWindowSize(ImVec2(340 * S, 500 * S), ImGuiCond_Appearing);
    theme::pushGlassWindowStyle();
    if (!ImGui::Begin("Agent Activity", nullptr)) {
        ImGui::End();
        theme::popGlassWindowStyle();
        return;
    }
    drawBody(st);
    ImGui::End();
    theme::popGlassWindowStyle();
}

void ActivityPanel::drawBody(app::AppState& st, const theme::AdaptiveSurface* surf) {
    // Embedded in the Copilot pane: ambient text follows the shared agent_glass surface;
    // semantic status colors (good/warn/bad/accent) below stay palette-driven.
    const bool adaptive = surf != nullptr;
    if (adaptive) ImGui::PushStyleColor(ImGuiCol_Text, surf->textPrimary);
    struct PopGuard { bool on; ~PopGuard() { if (on) ImGui::PopStyleColor(); } } popGuard{adaptive};

    pulseT_ += ImGui::GetIO().DeltaTime;
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
    ImGui::Dummy(ImVec2(0, theme::U()));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##actcontent", ImVec2(0, 0), ImGuiChildFlags_None);
    if (selectedReplay_ < 0) {
        if (!hasLiveRun_) {
            ImGui::TextColored(TVEC(txt2),
                "No agent run in progress.\n"
                "Use \"Design with AI\" (v2 mode) to start one.");
        } else {
            drawRunTree(st, liveRun_, true);
        }
    } else if (!replayLoaded_) {
        ImGui::TextColored(TVEC(warn), "Failed to load events.jsonl.");
    } else {
        drawRunTree(st, replayRun_, false);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

}  // namespace ui
