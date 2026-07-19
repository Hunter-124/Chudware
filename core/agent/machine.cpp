// core/agent/machine.cpp — see machine.h. Mirrors agent/orchestrator/machine.py.
#include "agent/machine.h"

#include "agent/providers.h"

#include <algorithm>
#include <map>
#include <mutex>
#include <vector>

namespace aicad {
namespace agent {

const std::map<std::string, int> kDefaultBudgets = {
    {"intake", 1}, {"plan", 2}, {"model", 4}, {"review", 2}, {"fix", 4}, {"memory", 1},
};

namespace {
std::mutex g_run_ctl_mtx;
std::map<std::string, RunControl> g_run_ctl;
}  // namespace

void register_run_control(const std::string& run_id, RunControl control) {
    if (run_id.empty()) return;
    std::lock_guard<std::mutex> lk(g_run_ctl_mtx);
    g_run_ctl[run_id] = std::move(control);
}

void unregister_run_control(const std::string& run_id) {
    if (run_id.empty()) return;
    std::lock_guard<std::mutex> lk(g_run_ctl_mtx);
    g_run_ctl.erase(run_id);
}

json cancel_run(const std::string& run_id) {
    if (run_id.empty()) return json{{"ok", true}, {"cancelled", false}};
    RunControl ctl;
    bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_run_ctl_mtx);
        auto it = g_run_ctl.find(run_id);
        if (it != g_run_ctl.end()) {
            ctl = it->second;
            found = true;
        }
    }
    if (!found) return json{{"ok", true}, {"cancelled", false}};
    ctl.request_cancel();
    // Also poke the thread-local HTTP cancel if this thread owns the transfer
    // (usually agent_cancel is cross-thread — the xfer callback reads the atomic
    // flag via the RunControl shared_ptr installed at run start).
    return json{{"ok", true}, {"cancelled", true}};
}

json steer_run(const std::string& run_id, const std::string& text) {
    if (run_id.empty()) return json{{"ok", true}, {"steered", false}};
    // Trim; empty steering is a no-op.
    std::string t = text;
    const auto ws = t.find_first_not_of(" \t\r\n");
    if (ws == std::string::npos) return json{{"ok", true}, {"steered", false}};
    RunControl ctl;
    bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_run_ctl_mtx);
        auto it = g_run_ctl.find(run_id);
        if (it != g_run_ctl.end()) {
            ctl = it->second;
            found = true;
        }
    }
    if (!found) return json{{"ok", true}, {"steered", false}};
    ctl.queue_steer(t);
    return json{{"ok", true}, {"steered", true}};
}

bool is_planning_stage(const std::string& stage) {
    // Strong-model stages under prewalk: understand / plan / review / memory.
    // NOT implementation (model apply / fix / micro execute).
    return stage == "intake" || stage == "plan" || stage == "decompose" || stage == "review" ||
           stage == "memory" || stage == "mission_plan";
}

bool is_implementation_stage(const std::string& stage) {
    // Cheap-model stages under prewalk: first BUILD/model-apply onward.
    return stage == "model" || stage == "fix" || stage == "execute" || stage == "build";
}

// --------------------------------------------------------------------------- err_sig
namespace {

// Trim + drop empty lines (Python: [ln.strip() for ln ... if ln.strip()]).
std::vector<std::string> nonempty_stripped_lines(const std::string& s) {
    // Python: err.strip().splitlines() then strip each, keep nonempty.
    // splitlines() splits on \n, \r, \r\n. We split on \n / \r generically.
    std::vector<std::string> raw;
    std::string cur;
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (c == '\n') {
            raw.push_back(cur);
            cur.clear();
        } else if (c == '\r') {
            raw.push_back(cur);
            cur.clear();
            if (i + 1 < s.size() && s[i + 1] == '\n') ++i;  // \r\n
        } else {
            cur += c;
        }
    }
    raw.push_back(cur);
    auto strip = [](const std::string& x) {
        size_t a = 0, b = x.size();
        auto ws = [](char ch) {
            return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f' || ch == '\v';
        };
        while (a < b && ws(x[a])) ++a;
        while (b > a && ws(x[b - 1])) --b;
        return x.substr(a, b - a);
    };
    std::vector<std::string> out;
    for (const auto& ln : raw) {
        std::string t = strip(ln);
        if (!t.empty()) out.push_back(t);
    }
    return out;
}

// Hand-rolled ^[A-Za-z_][\w.]*(?:Error|Exception)\b match.
// \w = [A-Za-z0-9_]; \b at the end means the char after "Error"/"Exception" is
// not a word char (or end of string). The token [A-Za-z_][\w.]* is matched
// greedily from the start; we then require that token to END with Error/Exception
// at a word boundary. Because [\w.]* can include the letters of Error/Exception,
// a string like "MyError" matches: token = "MyError", and it ends with "Error"
// followed by end-of-string (boundary). We must ensure the prefix up to "Error"
// or "Exception" consists only of [A-Za-z_][\w.]* characters AND that right after
// the suffix the next char is a non-word boundary.
bool err_line_matches(const std::string& ln) {
    if (ln.empty()) return false;
    auto is_word = [](char c) {
        unsigned char u = static_cast<unsigned char>(c);
        return (u >= 'A' && u <= 'Z') || (u >= 'a' && u <= 'z') || (u >= '0' && u <= '9') ||
               u == '_';
    };
    auto is_id_dot = [&](char c) { return is_word(c) || c == '.'; };
    // First char must be [A-Za-z_].
    char c0 = ln[0];
    bool first_ok = (c0 >= 'A' && c0 <= 'Z') || (c0 >= 'a' && c0 <= 'z') || c0 == '_';
    if (!first_ok) return false;
    // Greedy span of [\w.] from index 1.
    size_t end = 1;
    while (end < ln.size() && is_id_dot(ln[end])) ++end;
    // The matched token is ln[0:end]. The regex match() anchors at start; the
    // (?:Error|Exception) must appear within the token such that there is a
    // position p where token[..p] is [A-Za-z_][\w.]*-shaped (always true for any
    // prefix that stays within the [\w.] run) and token continues with
    // "Error"/"Exception" followed by a \b boundary. Since regex is greedy but
    // backtracks to satisfy the suffix + \b, the practical rule: there exists an
    // occurrence of "Error" or "Exception" ending at some index e (within [1,end])
    // such that the char at index e (in the full line) is a non-word char or end.
    // Also the suffix's start must be >= 1 (so the [A-Za-z_] first char is consumed
    // before the suffix) — i.e. the suffix cannot start at index 0.
    auto suffix_ok = [&](const std::string& suf) -> bool {
        size_t pos = ln.find(suf);
        while (pos != std::string::npos) {
            size_t e = pos + suf.size();  // index just past the suffix
            // Must be inside / at the boundary of the greedy [\w.] token.
            if (pos >= 1 && e <= end) {
                // \b: char at e must be non-word (or end). Within the token run,
                // chars are [\w.]; a '.' is non-word so it satisfies \b. End-of-run
                // (e == end) means next char (if any) is non-[\w.] => boundary.
                bool boundary;
                if (e >= ln.size())
                    boundary = true;
                else
                    boundary = !is_word(ln[e]);
                if (boundary) return true;
            }
            pos = ln.find(suf, pos + 1);
        }
        return false;
    };
    return suffix_ok("Error") || suffix_ok("Exception");
}

}  // namespace

std::string err_sig(const std::string& err) {
    if (err.empty()) return "";
    std::vector<std::string> lines = nonempty_stripped_lines(err);
    for (auto it = lines.rbegin(); it != lines.rend(); ++it)
        if (err_line_matches(*it)) return *it;
    return lines.empty() ? "" : lines.back();
}

// --------------------------------------------------------------------------- event emit
namespace {

void emit_call_events(RunEvents& events, const std::string& stage, const std::string& role,
                      int attempt, const StageOutcome& o) {
    events.emit("agent_call",
                json{{"stage", stage},
                     {"role", role},
                     {"attempt", attempt},
                     {"prompt_file", o.prompt_file},
                     {"prompt_hash", o.prompt_hash},
                     {"context_chars", o.context_chars},
                     {"context_tokens_est", o.context_tokens_est},
                     {"inputs_digest", o.inputs_digest}});
    std::string sidecar = events.write_sidecar(stage, attempt, o.raw);
    json out = json::object();
    out["stage"] = stage;
    out["role"] = role;
    out["attempt"] = attempt;
    out["raw"] = cap_raw(o.raw);
    out["sent"] = o.sent;
    out["raw_path"] = sidecar;
    out["parsed_ok"] = o.parsed_ok;
    out["parse_error"] = o.has_parse_error ? json(o.parse_error) : json(nullptr);
    out["ops_count"] = o.has_ops_count ? json(o.ops_count) : json(nullptr);
    out["duration_ms"] = o.duration_ms;
    out["tokens"] = o.has_tokens ? o.tokens : json(nullptr);
    events.emit("agent_output", out);
    // Thinking / Doing stream: surface provider reasoning as agent.thought (rowKind 2).
    if (!o.thinking.empty()) {
        emit_agent_thought(events, o.thinking, o.model, stage, attempt,
                           /*parent_id=*/"", /*actor=*/role);
    }
}

}  // namespace

json run_machine(DesignState& state, const StageActions& actions, RunEvents& events,
                 const json& budgets_in, RunControl control) {
    std::map<std::string, int> budgets = kDefaultBudgets;
    if (budgets_in.is_object())
        for (auto it = budgets_in.begin(); it != budgets_in.end(); ++it)
            if (it.value().is_number_integer() || it.value().is_number_unsigned())
                budgets[it.key()] = it.value().get<int>();

    // Starting revision — used to detect zero-progress (no successful applies).
    const int start_revision = state.revision;

    int global_calls = 0;
    auto over_global_cap = [&] { return global_calls >= kGlobalIterCap; };
    auto cancelled = [&] { return control.cancelled(); };

    // Mid-run steering: fold queued user steering into the request so the NEXT stage /
    // attempt provider call sees it (cooperative — never interrupts an in-flight call).
    // Drained at every stage/attempt boundary below (same checkpoints as cancelled()).
    auto apply_steers = [&] {
        for (const std::string& s : control.drain_steers()) {
            state.request += "\n\n[User steering] " + s;
            events.emit("agent_steer", json{{"text", s}});
        }
    };

    auto budget_of = [&](const std::string& stage) -> int {
        auto it = budgets.find(stage);
        return it == budgets.end() ? 1 : it->second;
    };

    // ---- run ONE attempt of a stage ----
    // Tracks the latest failure reason so a budget-exhaust terminal can surface the
    // real provider HTTP error on the phase card instead of "budget exhausted (N)".
    std::string last_fail_reason;
    bool last_was_provider_error = false;
    bool last_was_parse_error = false;
    bool last_was_free_retry = false;
    // Intake explicit reject (verdict=fail + error=unintelligible_request).
    bool last_was_unintelligible = false;
    // Plan-stage advisor escalate (verdict=fail + error=needs_user_decision).
    bool last_was_needs_user_decision = false;
    std::string last_escalate_note;
    // Run-level provider error (MODEL stage terminal) — returned as stop_reason.
    std::string run_provider_error;

    auto looks_like_provider_error = [](const StageOutcome& o) -> bool {
        // Stage actions (run.cpp) tag provider failures as:
        //   reason = "provider error: <detail>"
        //   error  = "<stage>: <detail>"  / has_error=true
        // Also match concise_http_error shapes ("… HTTP 400: …").
        auto has = [](const std::string& s, const char* needle) {
            return s.find(needle) != std::string::npos;
        };
        if (has(o.reason, "provider error")) return true;
        if (o.has_error && (has(o.error, " HTTP ") || has(o.error, "provider error") ||
                            has(o.error, "HTTP 4") || has(o.error, "HTTP 5")))
            return true;
        if (has(o.reason, " HTTP ") &&
            (has(o.reason, "HTTP 4") || has(o.reason, "HTTP 5") || has(o.reason, "rate limit") ||
             has(o.reason, "daily quota")))
            return true;
        return false;
    };

    auto run_attempt = [&](const std::string& stage, const Action& action, const std::string& role,
                           int attempt, int budget) -> std::string {
        if (cancelled()) return "cancelled";
        apply_steers();
        if (over_global_cap()) {
            last_fail_reason = "global iteration cap reached";
            last_was_provider_error = false;
            last_was_parse_error = false;
            last_was_unintelligible = false;
            events.emit("stage_result", json{{"stage", stage},
                                             {"attempt", attempt},
                                             {"verdict", "fail"},
                                             {"reason", last_fail_reason}});
            return "fail";
        }
        state.ledger.bump(stage);
        events.emit("stage_started",
                    json{{"stage", stage}, {"role", role}, {"attempt", attempt}, {"budget", budget}});
        global_calls += 1;
        StageOutcome outcome = action(state, attempt);
        emit_call_events(events, stage, role, attempt, outcome);
        last_was_unintelligible =
            outcome.has_error && outcome.error == "unintelligible_request";
        if (outcome.has_error && outcome.error == "needs_user_decision") {
            last_was_needs_user_decision = true;
            last_escalate_note = outcome.reason;
        }

        if (outcome.has_error && !outcome.free_retry) {
            int streak = state.ledger.note_sig(stage, err_sig(outcome.error));
            if (streak >= kSameSigLimit) {
                last_fail_reason =
                    "identical failure x" + std::to_string(streak) + ": " + err_sig(outcome.error);
                last_was_provider_error = looks_like_provider_error(outcome);
                last_was_parse_error = outcome.has_parse_error;
                // Prefer the raw provider message on the phase card when it's a provider error.
                std::string reason = last_was_provider_error && !outcome.reason.empty()
                                         ? outcome.reason
                                         : last_fail_reason;
                if (last_was_provider_error) {
                    last_fail_reason = reason;
                    run_provider_error = reason;
                } else if (last_was_parse_error) {
                    reason = "the model kept replying in an invalid format";
                    last_fail_reason = reason;
                }
                json sr = json{{"stage", stage},
                              {"attempt", attempt},
                              {"verdict", "fail"},
                              {"reason", reason},
                              {"stop_reason", last_was_provider_error ? "provider_error"
                                                   : (last_was_parse_error ? "parse_error"
                                                                          : "identical_failure")}};
                if (last_was_provider_error) {
                    sr["error_detail"] = reason;
                    sr["error_human"] = humanize_provider_error(reason);
                } else if (last_was_parse_error) {
                    sr["error_detail"] = reason;
                    sr["error_human"] = reason;
                }
                events.emit("stage_result", sr);
                return last_was_provider_error ? "provider_error" : "fail";
            }
        } else {
            state.ledger.note_sig(stage, "");
        }

        // Remember the latest provider-facing reason so a budget-exhaust terminal
        // can re-emit it instead of "budget exhausted (N)".
        if (outcome.free_retry) {
            last_was_free_retry = true;
            last_was_provider_error = false;
            last_was_parse_error = false;
            last_fail_reason = outcome.reason;
        } else if (looks_like_provider_error(outcome)) {
            last_was_free_retry = false;
            last_was_provider_error = true;
            last_was_parse_error = false;
            last_fail_reason = !outcome.reason.empty()
                                   ? outcome.reason
                                   : (outcome.has_error ? outcome.error : std::string("provider error"));
        } else if (outcome.has_parse_error) {
            last_was_free_retry = false;
            last_was_provider_error = false;
            last_was_parse_error = true;
            last_fail_reason = !outcome.reason.empty()
                                   ? outcome.reason
                                   : (outcome.has_error ? outcome.error : outcome.parse_error);
        } else {
            last_was_free_retry = false;
            last_was_provider_error = false;
            last_was_parse_error = false;
            last_fail_reason = outcome.reason;
        }

        events.emit("stage_result", json{{"stage", stage},
                                         {"attempt", attempt},
                                         {"verdict", outcome.verdict},
                                         {"reason", outcome.reason},
                                         {"free_retry", outcome.free_retry}});
        return outcome.verdict;
    };

    // ---- run a bounded stage to a terminal verdict ----
    // budget <= 0 means the stage is omitted entirely (no stage_started, no Action call).
    // Used for solo_skip_plan (budgets.plan=0) so the planner is never invoked.
    auto run_stage = [&](const std::string& stage, const Action& action,
                         const std::string& role) -> std::string {
        int budget = budget_of(stage);
        last_fail_reason.clear();
        last_was_provider_error = false;
        last_was_parse_error = false;
        last_was_free_retry = false;
        last_was_unintelligible = false;
        last_was_needs_user_decision = false;
        last_escalate_note.clear();
        if (budget <= 0) return "skip";
        int free_retries = 0;
        constexpr int kMaxFreeRetries = 4;  // full_ops + 3 domain classes
        for (int attempt = 1; attempt <= budget; ++attempt) {
            std::string v = run_attempt(stage, action, role, attempt, budget);
            if (v == "cancelled" || v == "pass" || v == "fail" || v == "skip" ||
                v == "provider_error")
                return v;
            // v == "retry" — tool-scope free retries re-prompt without burning the attempt.
            if (last_was_free_retry && free_retries < kMaxFreeRetries) {
                ++free_retries;
                --attempt;
                last_was_free_retry = false;
                continue;
            }
            last_was_free_retry = false;
            if (attempt >= budget) {
                // Provider/API failures must NOT masquerade as a budget problem — the GUI
                // phase card renders stage_result.reason. Surface the real HTTP error.
                // Parse failures get a plain user-facing message (not silent "Done").
                std::string reason;
                std::string stop;
                if (last_was_provider_error && !last_fail_reason.empty()) {
                    reason = last_fail_reason;
                    stop = "provider_error";
                } else if (last_was_parse_error) {
                    reason = "the model kept replying in an invalid format";
                    stop = "parse_error";
                } else {
                    reason = "stage retry budget exhausted (" + std::to_string(attempt) + " of " +
                             std::to_string(budget) + ")";
                    if (!last_fail_reason.empty()) reason += ": " + last_fail_reason;
                    stop = "budget_exhausted";
                }
                json sr = json{{"stage", stage},
                              {"attempt", attempt},
                              {"verdict", "fail"},
                              {"reason", reason},
                              {"stop_reason", stop}};
                if (last_was_provider_error) {
                    run_provider_error = reason;
                    sr["error_detail"] = reason;
                    sr["error_human"] = humanize_provider_error(reason);
                } else if (last_was_parse_error) {
                    sr["error_detail"] = reason;
                    sr["error_human"] = reason;
                }
                events.emit("stage_result", sr);
                return last_was_provider_error ? "provider_error" : "fail";
            }
        }
        return "fail";
    };

    auto cancel_result = [] {
        return json{{"status", "cancelled"}, {"stop_reason", "cancelled"}};
    };

    // ---- INTAKE ----
    if (cancelled()) return cancel_result();
    {
        std::string v = run_stage("intake", actions.intake, "intake");
        if (v == "cancelled") return cancel_result();
        // Explicit reject (unintelligible / non-CAD / empty normalized request): terminal.
        // Other intake failures (parse/provider → retry → exhaust) stay non-fatal.
        if (v == "fail" && last_was_unintelligible) {
            const std::string detail =
                last_fail_reason.empty() ? "request is not a CAD design request" : last_fail_reason;
            return json{{"status", "failed"},
                       {"stop_reason", "unintelligible_request"},
                       {"error_detail", detail},
                       {"error_human", detail}};
        }
        // Other intake failure non-fatal.
    }

    // ---- PLAN ----
    // budgets.plan <= 0 (solo_skip_plan) omits the stage entirely — no LLM, no stub call.
    if (cancelled()) return cancel_result();
    if (budget_of("plan") > 0) {
        std::string plan_v = run_stage("plan", actions.plan, "planner");
        if (plan_v == "cancelled") return cancel_result();
        if (plan_v == "provider_error") {
            const std::string detail =
                run_provider_error.empty() ? last_fail_reason : run_provider_error;
            return json{{"status", "failed"},
                       {"stop_reason", "provider_error"},
                       {"error_detail", detail},
                       {"error_human", humanize_provider_error(detail)}};
        }
        // Advisor escalate during plan review: surface plan_ready for the host approve card.
        // The plan Action stamps error=needs_user_decision and leaves state.plan intact.
        if (plan_v == "fail" && last_was_needs_user_decision) {
            json out = json{{"status", "plan_ready"},
                           {"stop_reason", "needs_user_decision"},
                           {"reason", last_escalate_note}};
            if (!state.plan.is_null() && state.plan.is_object()) out["plan"] = state.plan;
            if (!state.execution_style.empty()) out["execution_style"] = state.execution_style;
            // Still emit plan_state so the host Plan section has content.
            if (!state.plan.is_null() && state.plan.is_object() && state.plan.contains("parts") &&
                state.plan["parts"].is_array() && !state.plan["parts"].empty()) {
                json plan_j = state.plan;
                std::string title, summary;
                plan_display_meta(state.plan, title, summary);
                json part_items = plan_state_items_from_build_plan(plan_j, "pending");
                emit_plan_state(events, "build_plan", title, summary, /*revision=*/1, part_items);
                events.emit("plan", json{{"plan", plan_j}, {"title", title}, {"summary", summary}});
            }
            return out;
        }
        // After a successful plan stage, surface plan_state(build_plan) so the copilot
        // Plan section fills on the default C++ path (Python already emits kind:plan).
        // Additive only — no emit when plan is absent (off/no-plan runs unchanged).
        if (!state.plan.is_null() && state.plan.is_object() && state.plan.contains("parts") &&
            state.plan["parts"].is_array() && !state.plan["parts"].empty()) {
            // nlohmann::json accepts ordered_json via conversion.
            json plan_j = state.plan;
            std::string title, summary;
            plan_display_meta(state.plan, title, summary);
            json part_items = plan_state_items_from_build_plan(plan_j, "pending");
            emit_plan_state(events, "build_plan", title, summary, /*revision=*/1, part_items);

            // Objectives / acceptance criteria — only when the planner provided them.
            json obj_items = plan_state_items_from_objectives(plan_j);
            if (!obj_items.empty()) {
                emit_plan_state(events, "objectives", "Objectives", summary, /*revision=*/1,
                                obj_items);
            }
        }
    }

    // ---- Stage checklist (todo list) ----
    // Everything except Ask shows a live checklist. Solo (plan budget 0) has no
    // planner LLM — emit a DETERMINISTIC stage-namespaced checklist from the
    // request. Ids use "model:" / "review:" / "fix:" / "memory:" prefixes so
    // they never collide with mission:/firm: items the host upserts by id.
    const bool plan_skipped = budget_of("plan") <= 0;
    int stage_check_rev = 0;
    std::string model_status = "pending";
    std::string review_status = "pending";
    std::string fix_status = "pending";
    std::string memory_status = "pending";
    bool fix_was_used = false;

    auto stage_summary = [&]() -> std::string {
        std::string s = state.request;
        // Collapse whitespace; cap for the tile title helper.
        std::string out;
        bool prev_ws = false;
        for (char c : s) {
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                if (!prev_ws && !out.empty()) {
                    out.push_back(' ');
                    prev_ws = true;
                }
            } else {
                out.push_back(c);
                prev_ws = false;
            }
        }
        while (!out.empty() && out.back() == ' ') out.pop_back();
        if (out.size() > 80) out = out.substr(0, 77) + "...";
        return out.empty() ? std::string("build part") : out;
    };

    auto emit_stage_checklist = [&]() {
        ++stage_check_rev;
        json items = json::array();
        const std::string sum = stage_summary();
        items.push_back(json{{"id", "model:"},
                             {"title", plan_item_title("model: " + sum)},
                             {"desc", "model: " + sum},
                             {"status", model_status},
                             {"kind", "step"}});
        items.push_back(json{{"id", "review:"},
                             {"title", plan_item_title("review")},
                             {"desc", "review"},
                             {"status", review_status},
                             {"kind", "step"}});
        if (fix_was_used || fix_status == "running" || fix_status == "done" ||
            fix_status == "fail") {
            items.push_back(json{{"id", "fix:"},
                                 {"title", plan_item_title("fix")},
                                 {"desc", "fix"},
                                 {"status", fix_status},
                                 {"kind", "step"}});
        }
        items.push_back(json{{"id", "memory:"},
                             {"title", plan_item_title("memory")},
                             {"desc", "name part"},
                             {"status", memory_status},
                             {"kind", "step"}});
        emit_plan_state(events, "checklist", plan_skipped ? "Build" : "Stages", sum,
                        stage_check_rev, items);
    };

    // Solo / no-plan: surface the stage checklist immediately so the UI has a todo list.
    if (plan_skipped) emit_stage_checklist();

    // ---- MODEL (loop per plan step) ----
    std::vector<std::string> steps = actions.plan_steps(state);
    if (steps.empty()) steps.push_back("");
    // Surface the ordered task list so the UI can show a live build checklist (long-horizon
    // planning). When model-step plan_state (ids model:N) is emitted, do NOT also emit the
    // legacy plan_steps event — host used to synthesize step:N rows from it and double the
    // checklist. plan_steps is kept only on paths that do NOT emit plan_state checklist
    // (e.g. solo/blank steps where emit_model_steps is false — and even then solo uses the
    // stage checklist above, so plan_steps is only for bare consumers with no plan_state).
    auto steps_have_content = [&]() {
        for (const auto& s : steps)
            if (!s.empty()) return true;
        return false;
    };
    const bool emit_model_steps = !plan_skipped && steps_have_content();
    {
        if (emit_model_steps) {
            emit_plan_state(events, "checklist", "Model steps", /*summary=*/"", /*revision=*/1,
                            plan_state_items_from_steps(steps, /*running_index=*/-1));
        } else {
            // No plan_state checklist for these steps — keep legacy plan_steps for any
            // consumer that still keys off it (solo stage checklist is already emitted).
            json task_arr = json::array();
            for (const std::string& s : steps) task_arr.push_back(s);
            events.emit("plan_steps", json{{"steps", task_arr}, {"total", (int)steps.size()}});
        }
    }
    int checklist_rev = 1;
    int stagnant = 0;
    int step_index = 0;
    model_status = "running";
    if (plan_skipped) emit_stage_checklist();
    for (const std::string& step : steps) {
        if (cancelled()) return cancel_result();
        if (over_global_cap()) return json{{"status", "failed"}, {"stop_reason", "max_iters"}};
        state.current_step = step;
        events.emit("model_step", json{{"index", step_index},
                                       {"total", (int)steps.size()},
                                       {"step", step}});
        // Live checklist: prior steps done, current running.
        if (emit_model_steps) {
            ++checklist_rev;
            emit_plan_state(events, "checklist", "Model steps", "", checklist_rev,
                            plan_state_items_from_steps(steps, step_index));
        }
        std::string v = run_stage("model", actions.model, "modeler");
        if (v == "cancelled") return cancel_result();
        if (v == "provider_error") {
            model_status = "fail";
            if (plan_skipped) emit_stage_checklist();
            if (emit_model_steps) {
                ++checklist_rev;
                emit_plan_state(events, "checklist", "Model steps", "", checklist_rev,
                                plan_state_items_from_steps(steps, /*running_index=*/-1,
                                                            /*all_done=*/false, step_index));
            }
            const std::string detail =
                run_provider_error.empty() ? last_fail_reason : run_provider_error;
            return json{{"status", "failed"},
                       {"stop_reason", "provider_error"},
                       {"error_detail", detail},
                       {"error_human", humanize_provider_error(detail)}};
        }
        if (v == "fail") {
            model_status = "fail";
            if (plan_skipped) emit_stage_checklist();
            if (emit_model_steps) {
                ++checklist_rev;
                emit_plan_state(events, "checklist", "Model steps", "", checklist_rev,
                                plan_state_items_from_steps(steps, /*running_index=*/-1,
                                                            /*all_done=*/false, step_index));
            }
            if (last_was_parse_error) {
                const std::string msg = "the model kept replying in an invalid format";
                return json{{"status", "failed"},
                           {"stop_reason", "parse_error"},
                           {"error_detail", msg},
                           {"error_human", msg}};
            }
            return json{{"status", "failed"}, {"stop_reason", "model_failed"}};
        }
        ++step_index;
        if (state.last_no_progress) {
            stagnant += 1;
            if (stagnant >= kStagnationLimit) break;
        } else {
            stagnant = 0;
        }
    }
    // Mark remaining checklist complete after a successful model loop (or early stagnation break).
    if (emit_model_steps && !steps.empty()) {
        ++checklist_rev;
        // step_index == count of successfully completed steps (incremented after pass).
        if (step_index >= static_cast<int>(steps.size())) {
            emit_plan_state(events, "checklist", "Model steps", "", checklist_rev,
                            plan_state_items_from_steps(steps, /*running_index=*/-1,
                                                        /*all_done=*/true));
        } else {
            // Completed steps done; never-attempted steps abandoned (stagnation stop).
            json items = json::array();
            for (size_t i = 0; i < steps.size(); ++i) {
                const std::string& s = steps[i];
                const char* st =
                    (static_cast<int>(i) < step_index) ? "done" : "abandoned";
                items.push_back(json{{"id", "model:" + std::to_string(i)},
                                     {"title", plan_item_title(s)},
                                     {"desc", s},
                                     {"status", st},
                                     {"kind", "step"}});
            }
            emit_plan_state(events, "checklist", "Model steps", "", checklist_rev, items);
        }
    }
    model_status = (step_index > 0) ? "done" : "fail";
    if (plan_skipped) emit_stage_checklist();
    state.current_step = "";

    // ---- REVIEW -> FIX loop ----
    // Review early-exits on pass/skip/fail/provider_error (zero fix calls when first review
    // passes). Fix budget is a RUN-LEVEL cap on fixer Action invocations (not merely
    // per-stage retries), so Solo/Standard/Complex defaults actually bound the loop.
    // Each review "retry" drains fix attempts until fix passes or the cap is hit, then
    // re-reviews when a fix landed.
    {
        const int fix_limit = std::max(0, budget_of("fix"));
        int fix_calls = 0;
        int review_round = 0;
        // Display budget for review events: enough room to re-check after each fix.
        const int review_display_budget = fix_limit + 1;
        review_status = "running";
        if (plan_skipped) emit_stage_checklist();
        while (true) {
            if (cancelled()) return cancel_result();
            if (over_global_cap()) break;
            ++review_round;
            std::string v = run_attempt("review", actions.review, "reviewer", review_round,
                                       review_display_budget);
            if (v == "cancelled") return cancel_result();
            if (v == "pass" || v == "skip") {
                review_status = "done";
                break;
            }
            if (v == "fail" || v == "provider_error") {
                review_status = "fail";
                break;
            }
            // v == "retry": burn fix attempts until pass or the run-level fix cap.
            if (fix_calls >= fix_limit) {
                review_status = "fail";
                break;
            }
            bool fix_passed = false;
            bool fix_terminal_fail = false;
            fix_was_used = true;
            fix_status = "running";
            if (plan_skipped) emit_stage_checklist();
            while (fix_calls < fix_limit) {
                if (cancelled()) return cancel_result();
                ++fix_calls;
                std::string fix_v =
                    run_attempt("fix", actions.fix, "fixer", fix_calls, fix_limit);
                if (fix_v == "cancelled") return cancel_result();
                if (fix_v == "provider_error" || fix_v == "fail") {
                    fix_terminal_fail = true;
                    fix_status = "fail";
                    break;
                }
                if (fix_v == "pass" || fix_v == "skip") {
                    fix_passed = true;
                    fix_status = "done";
                    break;
                }
                // fix_v == "retry": consume another fix slot if any remain.
            }
            if (plan_skipped) emit_stage_checklist();
            if (fix_terminal_fail) {
                review_status = "fail";
                break;
            }
            if (!fix_passed) {
                review_status = "fail";
                break;  // cap hit with no successful fix
            }
            // Successful fix → loop and re-review.
            review_status = "running";
            if (plan_skipped) emit_stage_checklist();
        }
        if (plan_skipped) emit_stage_checklist();
    }

    // ---- MEMORY (part naming) ----
    // Skip naming when the run produced no geometry: revision never advanced past the
    // starting revision AND the feature tree is still empty. Fail with no_geometry so
    // the host does not autosave an empty part_named result.
    {
        const bool rev_advanced = state.revision > start_revision;
        const bool has_features =
            !state.tree_digest.empty() && state.tree_digest.rfind("(empty", 0) != 0;
        if (!rev_advanced && !has_features) {
            const std::string msg =
                "model produced no valid geometry — nothing was built";
            memory_status = "fail";
            if (plan_skipped) emit_stage_checklist();
            events.emit("stage_result",
                        json{{"stage", "memory"},
                             {"attempt", 0},
                             {"verdict", "fail"},
                             {"reason", msg},
                             {"stop_reason", "no_geometry"},
                             {"text", msg}});
            if (cancelled()) return cancel_result();
            return json{{"status", "failed"},
                       {"stop_reason", "no_geometry"},
                       {"error_detail", msg},
                       {"error_human", msg}};
        }
    }
    if (!cancelled()) {
        memory_status = "running";
        if (plan_skipped) emit_stage_checklist();
        std::string mv = run_stage("memory", actions.memory, "memory");
        memory_status = (mv == "pass" || mv == "skip") ? "done" : "fail";
        if (plan_skipped) emit_stage_checklist();
    }

    if (cancelled()) return cancel_result();
    return json{{"status", "done"}, {"stop_reason", "completed"}};
}

}  // namespace agent
}  // namespace aicad
