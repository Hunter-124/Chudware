// core/agent/machine.cpp — see machine.h. Mirrors agent/orchestrator/machine.py.
#include "agent/machine.h"

#include "agent/providers.h"

#include <vector>

namespace aicad {
namespace agent {

const std::map<std::string, int> kDefaultBudgets = {
    {"intake", 1}, {"plan", 2}, {"model", 4}, {"review", 2}, {"fix", 4}, {"memory", 1},
};

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
        if (over_global_cap()) {
            last_fail_reason = "global iteration cap reached";
            last_was_provider_error = false;
            last_was_parse_error = false;
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
    auto run_stage = [&](const std::string& stage, const Action& action,
                         const std::string& role) -> std::string {
        int budget = budget_of(stage);
        last_fail_reason.clear();
        last_was_provider_error = false;
        last_was_parse_error = false;
        last_was_free_retry = false;
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
        // intake failure non-fatal.
    }

    // ---- PLAN ----
    if (cancelled()) return cancel_result();
    {
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

    // ---- MODEL (loop per plan step) ----
    std::vector<std::string> steps = actions.plan_steps(state);
    if (steps.empty()) steps.push_back("");
    // Surface the ordered task list so the UI can show a live build checklist (long-horizon
    // planning). Unknown event kinds are ignored by consumers, so this is forward-compatible.
    // Also emit plan_state shape=checklist (additive; keep raw plan_steps).
    // Skip plan_state when the only step is blank (no-plan / default empty step) so off/no-plan
    // runs stay free of an empty Plan card; raw plan_steps still fires for back-compat.
    auto steps_have_content = [&]() {
        for (const auto& s : steps)
            if (!s.empty()) return true;
        return false;
    };
    const bool emit_checklist = steps_have_content();
    {
        json task_arr = json::array();
        for (const std::string& s : steps) task_arr.push_back(s);
        events.emit("plan_steps", json{{"steps", task_arr}, {"total", (int)steps.size()}});
        if (emit_checklist)
            emit_plan_state(events, "checklist", "Model steps", /*summary=*/"", /*revision=*/1,
                            plan_state_items_from_steps(steps, /*running_index=*/-1));
    }
    int checklist_rev = 1;
    int stagnant = 0;
    int step_index = 0;
    for (const std::string& step : steps) {
        if (cancelled()) return cancel_result();
        if (over_global_cap()) return json{{"status", "failed"}, {"stop_reason", "max_iters"}};
        state.current_step = step;
        events.emit("model_step", json{{"index", step_index},
                                       {"total", (int)steps.size()},
                                       {"step", step}});
        // Live checklist: prior steps done, current running.
        if (emit_checklist) {
            ++checklist_rev;
            emit_plan_state(events, "checklist", "Model steps", "", checklist_rev,
                            plan_state_items_from_steps(steps, step_index));
        }
        std::string v = run_stage("model", actions.model, "modeler");
        if (v == "cancelled") return cancel_result();
        if (v == "provider_error") {
            if (emit_checklist) {
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
            if (emit_checklist) {
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
    if (emit_checklist && !steps.empty()) {
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
                items.push_back(json{{"id", "step-" + std::to_string(i)},
                                     {"title", plan_item_title(s)},
                                     {"desc", s},
                                     {"status", st},
                                     {"kind", "step"}});
            }
            emit_plan_state(events, "checklist", "Model steps", "", checklist_rev, items);
        }
    }
    state.current_step = "";

    // ---- REVIEW -> FIX loop ----
    int review_budget = budget_of("review");
    for (int review_round = 1; review_round <= review_budget; ++review_round) {
        if (cancelled()) return cancel_result();
        if (over_global_cap()) break;
        std::string v =
            run_attempt("review", actions.review, "reviewer", review_round, review_budget);
        if (v == "cancelled") return cancel_result();
        if (v == "pass" || v == "skip" || v == "fail" || v == "provider_error") break;
        // v == "retry": run FIX, then re-review.
        std::string fix_v = run_stage("fix", actions.fix, "fixer");
        if (fix_v == "cancelled") return cancel_result();
        if (fix_v == "provider_error" || fix_v == "fail") break;
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
    if (!cancelled()) run_stage("memory", actions.memory, "memory");

    if (cancelled()) return cancel_result();
    return json{{"status", "done"}, {"stop_reason", "completed"}};
}

}  // namespace agent
}  // namespace aicad
