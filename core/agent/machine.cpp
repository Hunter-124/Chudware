// core/agent/machine.cpp — see machine.h. Mirrors agent/orchestrator/machine.py.
#include "agent/machine.h"

#include <vector>

namespace aicad {
namespace agent {

const std::map<std::string, int> kDefaultBudgets = {
    {"intake", 1}, {"plan", 2}, {"model", 4}, {"review", 2}, {"fix", 4}, {"memory", 1},
};

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
    out["raw_path"] = sidecar;
    out["parsed_ok"] = o.parsed_ok;
    out["parse_error"] = o.has_parse_error ? json(o.parse_error) : json(nullptr);
    out["ops_count"] = o.has_ops_count ? json(o.ops_count) : json(nullptr);
    out["duration_ms"] = o.duration_ms;
    out["tokens"] = o.has_tokens ? o.tokens : json(nullptr);
    events.emit("agent_output", out);
}

}  // namespace

json run_machine(DesignState& state, const StageActions& actions, RunEvents& events,
                 const json& budgets_in, RunControl control) {
    std::map<std::string, int> budgets = kDefaultBudgets;
    if (budgets_in.is_object())
        for (auto it = budgets_in.begin(); it != budgets_in.end(); ++it)
            if (it.value().is_number_integer() || it.value().is_number_unsigned())
                budgets[it.key()] = it.value().get<int>();

    int global_calls = 0;
    auto over_global_cap = [&] { return global_calls >= kGlobalIterCap; };
    auto cancelled = [&] { return control.cancelled(); };

    auto budget_of = [&](const std::string& stage) -> int {
        auto it = budgets.find(stage);
        return it == budgets.end() ? 1 : it->second;
    };

    // ---- run ONE attempt of a stage ----
    auto run_attempt = [&](const std::string& stage, const Action& action, const std::string& role,
                           int attempt, int budget) -> std::string {
        if (cancelled()) return "cancelled";
        if (over_global_cap()) {
            events.emit("stage_result", json{{"stage", stage},
                                             {"attempt", attempt},
                                             {"verdict", "fail"},
                                             {"reason", "global iteration cap reached"}});
            return "fail";
        }
        state.ledger.bump(stage);
        events.emit("stage_started",
                    json{{"stage", stage}, {"role", role}, {"attempt", attempt}, {"budget", budget}});
        global_calls += 1;
        StageOutcome outcome = action(state, attempt);
        emit_call_events(events, stage, role, attempt, outcome);

        if (outcome.has_error) {
            int streak = state.ledger.note_sig(stage, err_sig(outcome.error));
            if (streak >= kSameSigLimit) {
                events.emit("stage_result",
                            json{{"stage", stage},
                                 {"attempt", attempt},
                                 {"verdict", "fail"},
                                 {"reason", "identical failure x" + std::to_string(streak) + ": " +
                                                err_sig(outcome.error)}});
                return "fail";
            }
        } else {
            state.ledger.note_sig(stage, "");
        }
        events.emit("stage_result", json{{"stage", stage},
                                         {"attempt", attempt},
                                         {"verdict", outcome.verdict},
                                         {"reason", outcome.reason}});
        return outcome.verdict;
    };

    // ---- run a bounded stage to a terminal verdict ----
    auto run_stage = [&](const std::string& stage, const Action& action,
                         const std::string& role) -> std::string {
        int budget = budget_of(stage);
        for (int attempt = 1; attempt <= budget; ++attempt) {
            std::string v = run_attempt(stage, action, role, attempt, budget);
            if (v == "cancelled" || v == "pass" || v == "fail" || v == "skip") return v;
            // v == "retry"
            if (attempt >= budget) {
                events.emit("stage_result",
                            json{{"stage", stage},
                                 {"attempt", attempt},
                                 {"verdict", "fail"},
                                 {"reason", "budget exhausted (" + std::to_string(budget) + ")"}});
                return "fail";
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
    }

    // ---- MODEL (loop per plan step) ----
    std::vector<std::string> steps = actions.plan_steps(state);
    if (steps.empty()) steps.push_back("");
    int stagnant = 0;
    for (const std::string& step : steps) {
        if (cancelled()) return cancel_result();
        if (over_global_cap()) return json{{"status", "failed"}, {"stop_reason", "max_iters"}};
        state.current_step = step;
        std::string v = run_stage("model", actions.model, "modeler");
        if (v == "cancelled") return cancel_result();
        if (v == "fail") return json{{"status", "failed"}, {"stop_reason", "model_failed"}};
        if (state.last_no_progress) {
            stagnant += 1;
            if (stagnant >= kStagnationLimit) break;
        } else {
            stagnant = 0;
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
        if (v == "pass" || v == "skip" || v == "fail") break;
        // v == "retry": run FIX, then re-review.
        std::string fix_v = run_stage("fix", actions.fix, "fixer");
        if (fix_v == "cancelled") return cancel_result();
        if (fix_v == "fail") break;
    }

    // ---- MEMORY ----
    if (!cancelled()) run_stage("memory", actions.memory, "memory");

    if (cancelled()) return cancel_result();
    return json{{"status", "done"}, {"stop_reason", "completed"}};
}

}  // namespace agent
}  // namespace aicad
