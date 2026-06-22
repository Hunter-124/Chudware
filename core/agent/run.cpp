// core/agent/run.cpp — see run.h. Mirrors agent/orchestrator/run.py (P53b).
#include "agent/run.h"

#include <chrono>
#include <cstdint>
#include <fstream>
#include <optional>
#include <regex>
#include <sstream>

#include "agent/config.h"
#include "agent/events.h"
#include "agent/machine.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/state.h"
#include "doc/session.h"

namespace aicad {
namespace agent {

namespace {

using ojson = nlohmann::ordered_json;

// How many parse/validate failures before the modeler escape hatch (raw build123d script)
// fires. Kept deliberately high so the agent keeps trying REAL parametric ops — each retry is
// re-prompted with the concrete validation error (state.last_errors flows into build_context),
// which is how it self-corrects. The script escape is a genuine last resort, not a 2-strike
// default (the old value of 2 made the agent bail to a canned script on anything non-trivial).
constexpr int kEscapeAfter = 4;

// Per-call output-token budget by provider + reasoning effort. The hardcoded 4096 throttled
// capable cloud models (a complex part is one modeler call → truncation → parse fail → escape
// hatch). Capable cloud backends get real headroom; local models stay modest.
int call_budget_for(const json& provider_cfg) {
    std::string name = provider_cfg.value("name", std::string());
    std::string effort = provider_cfg.value("reasoning_effort", std::string());
    int base = 4096;  // local / unknown
    if (name == "anthropic" || name == "gemini" || name == "openai") base = 8192;
    if (effort == "high" || effort == "max") base += 4096;  // reasoning headroom before content
    return base;
}

// Python truthiness of a JSON value (bool(x)): None/false/0/0.0/""/[]/{} -> false.
bool truthy_json(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

// Python str(value) for a scalar/JSON used in an f-string (plan_steps dims/features).
// Mirrors json scalar -> Python repr in a "{v}" context: ints/floats use Python's repr,
// strings use the raw text, bools -> True/False, null -> None, containers -> json-ish.
std::string py_scalar_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) {
        double d = v.get<double>();
        // Python str(float): integral floats render as "N.0".
        std::ostringstream ss;
        if (d == static_cast<long long>(d)) {
            ss << static_cast<long long>(d) << ".0";
        } else {
            ss.precision(17);
            ss << d;
        }
        return ss.str();
    }
    // Containers: defensive — the gate's plan parts use int/string dims only.
    return v.dump();
}

// ----------------------------------------------------------------- json helpers

// _extract_json_obj(reply): first fenced ```json object, else a bare top-level
// {...} parsed to an object, else null. Returns ojson (preserves key order so a
// spec/plan flows into build_context byte-faithfully).
ojson extract_json_obj(const std::string& reply) {
    // Each fenced ```json (or untagged) block: try to parse as an object.
    std::regex fence(R"(```(?:json|JSON)?\s*\n([\s\S]*?)```)");
    auto begin = std::sregex_iterator(reply.begin(), reply.end(), fence);
    auto end = std::sregex_iterator();
    for (auto it = begin; it != end; ++it) {
        std::string body = (*it)[1].str();
        // strip()
        size_t a = body.find_first_not_of(" \t\r\n");
        size_t b = body.find_last_not_of(" \t\r\n");
        if (a == std::string::npos) continue;
        std::string trimmed = body.substr(a, b - a + 1);
        ojson d = ojson::parse(trimmed, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    // Bare first-balanced {..}.
    std::optional<std::string> blob = first_balanced(reply, '{', '}');
    if (blob) {
        ojson d = ojson::parse(*blob, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    return ojson(nullptr);
}

// _inputs_digest(state) = "rev{revision} feats={len(tree_digest.splitlines())}".
std::string inputs_digest(const DesignState& state) {
    // Python str.splitlines(): an empty string -> 0 lines; otherwise count line
    // terminators + a trailing nonterminated segment.
    int lines = 0;
    const std::string& d = state.tree_digest;
    if (!d.empty()) {
        lines = 1;
        for (size_t i = 0; i < d.size(); ++i) {
            if (d[i] == '\n') {
                // a trailing terminator does not add an extra empty segment.
                if (i + 1 < d.size()) lines += 1;
            }
        }
    }
    return "rev" + std::to_string(state.revision) + " feats=" + std::to_string(lines);
}

// _default_step(state): "Build the requested object: {spec.object or request}".
std::string default_step(const DesignState& state) {
    std::string obj;
    if (state.spec.is_object() && state.spec.contains("object") && state.spec["object"].is_string())
        obj = state.spec["object"].get<std::string>();
    if (obj.empty()) obj = state.request;
    return "Build the requested object: " + obj;
}

// ----------------------------------------------------------------- memory persistence

// _store_memory: write to run_dir(run_id)/memory.json (the session-attr/extra-entry
// embed is a save-time host concern — we store to disk + keep on the in-memory state).
void store_memory(const std::string& run_id, const ojson& mem) {
    std::filesystem::path p = run_dir(run_id) / "memory.json";
    std::ofstream fh(p, std::ios::binary | std::ios::trunc);
    if (fh) fh << mem.dump(2);
}

// ----------------------------------------------------------------- provider call

// A reply + duration + meta, exactly like run.py _agent_call returns.
struct AgentCall {
    std::string reply;
    int duration_ms = 0;
    // event meta fields:
    std::string prompt_file;
    std::string prompt_hash;
    int context_chars = 0;
    int context_tokens_est = 0;
    std::string inputs_digest;
    bool has_call_error = false;
    std::string call_error;
};

// A reply + error from one provider completion (mirrors complete_ex).
struct ProvReply {
    std::string text;
    bool has_error = false;
    std::string error;
};

// One PERSISTENT provider instance for the whole run (the RoleScriptedProvider's
// per-role queues must survive across calls so the modeler's 1st/2nd/3rd replies are
// popped in order — exactly like the Python provider object lives for the run).
using ProviderFn = std::function<ProvReply(const std::string& system, const json& messages,
                                           int max_tokens)>;

ProviderFn make_provider(const json& provider_cfg) {
    std::string name = provider_cfg.value("name", std::string());
    if (name == "mock" && provider_cfg.contains("replies") &&
        provider_cfg["replies"].is_object()) {
        // Role-scripted mock (the gate path): sniff role from the system prompt and pop
        // the next scripted reply for that role. ONE instance, shared across all calls.
        auto rp = std::make_shared<RoleScriptedProvider>(provider_cfg["replies"]);
        return [rp](const std::string& system, const json&, int) -> ProvReply {
            ProvReply r;
            r.text = rp->complete(system);
            return r;
        };
    }
    // Real provider (or flat-list mock): route through agent::complete each call.
    json cfg = provider_cfg;
    return [name, cfg](const std::string& system, const json& messages, int max_tokens) -> ProvReply {
        ProvReply r;
        CompleteResult cr = complete(name, cfg, system, messages, max_tokens);
        if (!cr.error.empty()) {
            r.has_error = true;
            r.error = cr.error;
            r.text = cr.text;
            return r;
        }
        r.text = cr.text;
        return r;
    };
}

// _agent_call(provider, state, role): build a FRESH bounded context, prepend the
// "ROLE: <role>" marker line, call the provider, capture meta. max_tokens = 2048.
AgentCall agent_call(const ProviderFn& provider, const DesignState& state, const std::string& role,
                     const std::string& prompts_dir, int max_tokens, bool vision) {
    BuiltContext bc = build_context(state, role, prompts_dir);
    std::string system = "ROLE: " + role + "\n" + bc.system;
    auto pr = load_prompt(role, prompts_dir);
    AgentCall ac;
    ac.prompt_file = pr.second;
    ac.prompt_hash = prompt_hash(system);
    ac.context_chars = bc.context_chars;
    ac.context_tokens_est = bc.context_tokens_est;
    ac.inputs_digest = inputs_digest(state);

    // Vision: when the provider supports images and a render of the current model exists,
    // attach it so the model can SEE the geometry (reviewer/fixer self-correction).
    json parts = json::array({text_part(bc.body)});
    if (vision && state.last_render.is_string() && !state.last_render.get<std::string>().empty())
        parts.push_back(image_part(state.last_render.get<std::string>()));
    json messages = json::array({user_message(parts)});

    auto t0 = std::chrono::steady_clock::now();
    ProvReply rep = provider(system, messages, max_tokens);  // provider/effort-scaled budget
    auto t1 = std::chrono::steady_clock::now();
    ac.duration_ms =
        static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    if (rep.has_error) {
        ac.reply = "";
        ac.has_call_error = true;
        ac.call_error = rep.error;
    } else {
        ac.reply = rep.text;
    }
    return ac;
}

// Convert a doc-session response (regular json) tree into the ojson update_tree wants.
ojson tree_to_ojson(const json& tree) {
    // dump+parse preserves the doc layer's key order (sorted std::map) into ojson.
    return ojson::parse(tree.dump());
}

// feature_status_errors over a (regular json) feature_status list.
std::vector<std::string> status_errors(const json& feature_status) {
    ojson fs = ojson::parse(feature_status.dump());
    return feature_status_errors(fs);
}

}  // namespace

json run_design(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                EmitFn emit, const json& options) {
    // run_id = options.run_id or uuid hex[:12].
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty()) {
        // A process-unique token (the gate always passes a fixed run_id, so the exact
        // shape here is never compared).
        run_id = "run-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));
    }
    std::string prompts_dir = options.value("prompts_dir", std::string());

    // OFFLINE MODE defense-in-depth (assert_offline_safe). The host owns the app
    // config; the gate's mock is offline-safe and offline mode is off. We honor an
    // explicit options.offline flag + the provider descriptor's safety so the refusal
    // path is preserved without a config-file loader (DEFERRED: app-config read = host).
    bool offline = options.value("offline", false);
    if (offline && !provider_offline_safe(provider_cfg)) {
        // Mirror run.py: assert_offline_safe raises before any provider call. We surface
        // it as a failed run (the host translates to OfflineModeError).
        RunEvents ev_guard(run_id, emit);
        ev_guard.emit("run_finished", json{{"status", "failed"},
                                            {"stop_reason", "offline_mode_refused"},
                                            {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "offline_mode_refused"},
                    {"revision", 0},
                    {"doc_id", doc_id}};
    }

    RunEvents events(run_id, emit);

    // ONE provider instance for the whole run (per-role scripted queues must persist).
    ProviderFn provider = make_provider(provider_cfg);
    // Output-token budget per call, scaled to the provider + reasoning effort.
    const int call_budget = call_budget_for(provider_cfg);
    // Whether the provider can receive screenshots (drives vision review/fix).
    const bool vision = provider_cfg.value("supports_vision", false);

    // Seed the DesignState from the live session: doc_tree(doc_id) gives {revision, tree}.
    json tree0 = doc::doc_tree(doc_id);
    DesignState state;
    state.request = request;
    state.doc_id = doc_id;
    state.revision = tree0.value("revision", 0);
    // _load_memory: the on-disk/session-embed load is a host concern; start empty.
    state.memory = ojson::object();
    state.update_tree(tree_to_ojson(tree0.contains("tree") ? tree0["tree"] : json::object()));

    json stages = json::array({"intake", "plan", "model", "review", "fix", "memory"});
    events.emit("run_started",
                json{{"request", request}, {"doc_id", state.doc_id}, {"stages", stages}});

    // ----------------------------------------------------------------- actions
    StageActions actions;

    // Shared helper: copy an AgentCall's meta into a StageOutcome.
    auto fill_meta = [](StageOutcome& o, const AgentCall& ac) {
        o.prompt_file = ac.prompt_file;
        o.prompt_hash = ac.prompt_hash;
        o.context_chars = ac.context_chars;
        o.context_tokens_est = ac.context_tokens_est;
        o.inputs_digest = ac.inputs_digest;
        o.duration_ms = ac.duration_ms;
    };

    // _apply(ops): doc_apply_ops at state.revision; update rev + tree; collect status errors.
    auto apply_ops = [&](DesignState& st, const json& ops) -> std::vector<std::string> {
        json resp = doc::doc_apply_ops(doc_id, st.revision, ops, /*coalesce=*/false);
        st.update_revision(resp.value("revision", st.revision));
        st.update_tree(tree_to_ojson(resp.contains("tree") ? resp["tree"] : json::object()));
        json fs = json::array();
        if (resp.contains("rebuild") && resp["rebuild"].contains("feature_status"))
            fs = resp["rebuild"]["feature_status"];
        return status_errors(fs);
    };

    // ---- INTAKE ----
    actions.intake = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "intake", prompts_dir, call_budget, vision);
        ojson spec = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (!spec.is_null()) {
            st.spec = spec;
            o.verdict = "pass";
            o.reason = "spec parsed";
            o.parsed_ok = true;
        } else {
            o.verdict = "retry";
            o.reason = "spec not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no spec json";
            o.has_error = true;
            o.error = "intake: no spec json";
        }
        return o;
    };

    // ---- PLAN ----
    actions.plan = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "planner", prompts_dir, call_budget, vision);
        ojson p = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        bool ok = !p.is_null() && p.contains("parts") && p["parts"].is_array() &&
                  !p["parts"].empty();
        if (ok) {
            st.plan = p;
            o.verdict = "pass";
            o.reason = "plan parsed";
            o.parsed_ok = true;
        } else {
            // Plan is optional: a failure DEGRADES (retry -> exhaust -> non-fatal).
            o.verdict = "retry";
            o.reason = "plan not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no plan json";
        }
        return o;
    };

    // ---- MODEL escape hatch ----
    auto model_escape = [&](DesignState& st) -> StageOutcome {
        // Append the escape instruction; then ask for a build123d script.
        std::string appended =
            st.current_step +
            "\n\nThe op attempts failed. Instead, output a build123d script (assign `result`) in a "
            "```python block that builds this part.";
        // .strip()
        {
            size_t a = appended.find_first_not_of(" \t\r\n\f\v");
            size_t b = appended.find_last_not_of(" \t\r\n\f\v");
            appended = (a == std::string::npos) ? "" : appended.substr(a, b - a + 1);
        }
        st.current_step = appended;
        AgentCall ac = agent_call(provider, st, "modeler", prompts_dir, call_budget, vision);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        std::optional<std::string> code = extract_script(ac.reply);
        if (!code) {
            o.verdict = "fail";
            o.reason = "escape hatch: no script";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no script";
            o.has_error = true;
            o.error = "model-escape: no script";
            return o;
        }
        std::string name = "Script";
        if (st.spec.is_object() && st.spec.contains("object") && st.spec["object"].is_string() &&
            !st.spec["object"].get<std::string>().empty())
            name = st.spec["object"].get<std::string>();
        json op = script_escape_op(*code, name);
        json ops = json::array({op});
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            o.verdict = "fail";
            o.reason = "escape script invalid: " + dry.value("error", std::string());
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 1;
            o.has_error = true;
            o.error = "model-escape-validate: " + dry.value("error", std::string());
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        o.verdict = "pass";
        o.reason = "escape hatch: script feature applied";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = 1;
        return o;
    };

    // ---- MODEL ----
    actions.model = [&](DesignState& st, int attempt) -> StageOutcome {
        st.last_no_progress = false;
        if (attempt > kEscapeAfter) return model_escape(st);
        AgentCall ac = agent_call(provider, st, "modeler", prompts_dir, call_budget, vision);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        OpsResult ores = extract_ops(ac.reply);
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            o.verdict = "retry";
            o.reason = "no ops: " + perr;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "model: " + perr;
            return o;
        }
        const json& ops = *ores.ops;
        if (ops.empty()) {
            st.last_no_progress = true;
            o.verdict = "pass";
            o.reason = "no ops needed for step";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            o.no_progress = true;
            return o;
        }
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            std::string verr = dry.value("error", std::string());
            st.last_errors = {"invalid ops: " + verr};
            o.verdict = "retry";
            o.reason = "ops failed validation";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = static_cast<int>(ops.size());
            o.has_error = true;
            o.error = "model-validate: " + verr;
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        o.verdict = "pass";
        o.reason = "applied " + std::to_string(ops.size()) + " ops";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = static_cast<int>(ops.size());
        return o;
    };

    // ---- REVIEW (stats-only; vision rendering DEFERRED to host P55) ----
    actions.review = [&](DesignState& st, int) -> StageOutcome {
        // Vision review: rasterize the current revision so a vision-capable model can SEE its
        // model and self-correct (fail-soft — text-only review if the render is unavailable).
        st.last_render = nullptr;
        if (vision) {
            std::filesystem::path png =
                run_dir(run_id) / ("review_r" + std::to_string(st.revision) + ".png");
            json rr = doc::doc_render_png(doc_id, png.string());
            if (rr.value("ok", false)) st.last_render = png.string();
        }
        BuiltContext bc = build_context(st, "reviewer", prompts_dir);
        auto pr = load_prompt("reviewer", prompts_dir);
        StageOutcome o;
        o.prompt_file = pr.second;
        o.prompt_hash = prompt_hash(bc.system);  // reviewer system is NOT role-prefixed here,
        // mirroring run.py review() which hashes the bare build_context system (not "ROLE:..").
        o.context_chars = bc.context_chars;
        o.context_tokens_est = bc.context_tokens_est;
        o.inputs_digest = inputs_digest(st);
        json review_parts = json::array({text_part(bc.body)});
        if (vision && st.last_render.is_string() && !st.last_render.get<std::string>().empty())
            review_parts.push_back(image_part(st.last_render.get<std::string>()));
        json messages = json::array({user_message(review_parts)});
        // run.py review() does NOT prepend "ROLE:" — it passes the bare build_context
        // system. The RoleScriptedProvider sniffs "you are the reviewer" from the prompt's
        // first line, so routing is correct without the marker (matching Python byte-for-byte).
        auto t0 = std::chrono::steady_clock::now();
        ProvReply rep = provider(bc.system, messages, call_budget);  // provider/effort-scaled budget
        auto t1 = std::chrono::steady_clock::now();
        o.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        std::string reply = rep.has_error ? std::string() : rep.text;
        o.raw = reply;
        ojson verdict = extract_json_obj(reply);
        if (verdict.is_null()) {
            o.verdict = "retry";
            o.reason = "review verdict not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no verdict json";
            return o;
        }
        bool done = verdict.contains("done") && truthy_json(verdict["done"]);
        std::vector<std::string> issues;
        if (verdict.contains("issues") && verdict["issues"].is_array())
            for (const auto& i : verdict["issues"])
                if (i.is_string() && !i.get<std::string>().empty())
                    issues.push_back(i.get<std::string>());
        st.last_issues = issues;
        if (done || issues.empty()) {
            o.verdict = "pass";
            o.reason = "review: done";
            o.parsed_ok = true;
            o.done = true;
        } else {
            o.verdict = "retry";
            o.reason = "review: " + std::to_string(issues.size()) + " issue(s)";
            o.parsed_ok = true;
            o.done = false;
        }
        return o;
    };

    // ---- FIX ----
    actions.fix = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "fixer", prompts_dir, call_budget, vision);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (extract_revert(ac.reply)) {
            try {
                json resp = doc::doc_undo(doc_id);
                st.update_revision(resp.value("revision", st.revision));
                st.update_tree(
                    tree_to_ojson(resp.contains("tree") ? resp["tree"] : json::object()));
                o.verdict = "pass";
                o.reason = "fix: reverted last change";
                o.parsed_ok = true;
            } catch (const std::exception& e) {
                o.verdict = "fail";
                o.reason = std::string("revert failed: ") + e.what();
                o.has_error = true;
                o.error = std::string("fix-revert: ") + e.what();
            }
            return o;
        }
        OpsResult ores = extract_ops(ac.reply);
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            o.verdict = "retry";
            o.reason = "fix: no ops (" + perr + ")";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "fix: " + perr;
            return o;
        }
        const json& ops = *ores.ops;
        if (ops.empty()) {
            o.verdict = "pass";
            o.reason = "fix: no change";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            return o;
        }
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            std::string verr = dry.value("error", std::string());
            st.last_errors = {"invalid fix ops: " + verr};
            o.verdict = "retry";
            o.reason = "fix ops failed validation";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = static_cast<int>(ops.size());
            o.has_error = true;
            o.error = "fix-validate: " + verr;
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        st.last_issues.clear();
        o.verdict = "pass";
        o.reason = "fix: applied " + std::to_string(ops.size()) + " ops";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = static_cast<int>(ops.size());
        return o;
    };

    // ---- MEMORY ----
    actions.memory = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "memory", prompts_dir, call_budget, vision);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        ojson mem = extract_json_obj(ac.reply);
        if (mem.is_null()) {
            o.verdict = "pass";
            o.reason = "memory: nothing recorded";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no memory json";
            return o;
        }
        store_memory(run_id, mem);
        st.memory = mem;
        o.verdict = "pass";
        o.reason = "memory recorded";
        o.parsed_ok = true;
        return o;
    };

    // ---- plan_steps ----
    actions.plan_steps = [&](DesignState& st) -> std::vector<std::string> {
        std::vector<std::string> steps;
        if (st.plan.is_null() || !st.plan.contains("parts") || !st.plan["parts"].is_array() ||
            st.plan["parts"].empty()) {
            steps.push_back(default_step(st));
            return steps;
        }
        // Decompose each part into an ORDERED task list: a base-solid step, then one step per
        // feature. Smaller modeler calls model far more reliably than one giant "build everything"
        // call (less truncation, and each step's failure is isolated + re-promptable), and the
        // step list is exactly the long-horizon task checklist the UI surfaces. The modeler is
        // told to emit ONLY ops for the current step and not re-emit existing features.
        auto rstrip = [](std::string s) {
            size_t b = s.find_last_not_of(" \t\r\n\f\v");
            return (b == std::string::npos) ? std::string() : s.substr(0, b + 1);
        };
        for (const auto& p : st.plan["parts"]) {
            std::string name = (p.contains("name") && p["name"].is_string())
                                   ? p["name"].get<std::string>()
                                   : "part";
            std::string purpose = (p.contains("purpose") && p["purpose"].is_string())
                                      ? p["purpose"].get<std::string>()
                                      : "";
            std::string dim_s;
            if (p.contains("dimensions") && p["dimensions"].is_object()) {
                bool first = true;
                for (auto it = p["dimensions"].begin(); it != p["dimensions"].end(); ++it) {
                    if (!first) dim_s += ", ";
                    first = false;
                    dim_s += it.key() + "=" + py_scalar_str(it.value());
                }
            } else if (p.contains("dimensions")) {
                dim_s = py_scalar_str(p["dimensions"]);
            }
            // Placement: position this part in the shared assembly space so multiple parts fit
            // together instead of stacking at the origin. The modeler places primitives with `at`,
            // and sketched bodies by building the sketch at those coordinates / on an offset plane.
            std::string place_s;
            if (p.contains("placement") && p["placement"].is_object() &&
                p["placement"].contains("pos") && p["placement"]["pos"].is_array() &&
                p["placement"]["pos"].size() >= 3) {
                const auto& pos = p["placement"]["pos"];
                auto num = [&](const ojson& v) {
                    return v.is_number() ? py_scalar_str(v) : std::string("0");
                };
                std::string xyz = num(pos[0]) + "," + num(pos[1]) + "," + num(pos[2]);
                if (xyz != "0,0,0")
                    place_s = " Position this part's origin at [" + xyz +
                              "] in the shared assembly space (primitives: use \"at\":[" + xyz +
                              "]; sketched bodies: build the sketch at those coordinates or on an "
                              "offset plane) so it does not overlap the other parts.";
            }
            steps.push_back(rstrip("Build the base solid of part '" + name + "' (" + purpose +
                                   "). Dimensions: " + dim_s + "." + place_s));
            if (p.contains("features") && p["features"].is_array())
                for (const auto& f : p["features"]) {
                    std::string fs = py_scalar_str(f);
                    if (rstrip(fs).empty()) continue;
                    steps.push_back(rstrip("Refine part '" + name + "': " + fs));
                }
        }
        return steps;
    };

    // ----------------------------------------------------------------- drive
    json result = run_machine(state, actions, events, options.value("budgets", json::object()));

    // revision = session.revision (re-read live; matches run.py getattr(session,"revision")).
    json tree_now = doc::doc_tree(doc_id);
    int revision = tree_now.value("revision", state.revision);

    events.emit("run_finished", json{{"status", result["status"]},
                                     {"stop_reason", result["stop_reason"]},
                                     {"revision", revision}});

    return json{{"run_id", run_id},
                {"status", result["status"]},
                {"stop_reason", result["stop_reason"]},
                {"revision", revision},
                {"doc_id", state.doc_id}};
}

json run_assembly_design(const std::string& request, const std::string& doc_id,
                         const json& provider_cfg, EmitFn emit, const json& options) {
    std::string prompts_dir = options.value("prompts_dir", std::string());
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "asm-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));

    // 1) Plan the request to learn the parts + whether it's an assembly. One intake + one planner
    //    call (cheap relative to a full build); the per-part run_design re-plans its own part.
    ProviderFn provider = make_provider(provider_cfg);
    const int budget = call_budget_for(provider_cfg);
    DesignState pstate;
    pstate.request = request;
    pstate.doc_id = doc_id;
    {
        AgentCall ia = agent_call(provider, pstate, "intake", prompts_dir, budget, false);
        ojson spec = extract_json_obj(ia.reply);
        if (!spec.is_null()) pstate.spec = spec;
    }
    AgentCall pa = agent_call(provider, pstate, "planner", prompts_dir, budget, false);
    ojson plan = extract_json_obj(pa.reply);
    bool want_asm = !plan.is_null() && plan.is_object() && truthy_json(plan.value("assembly", ojson(false)));
    ojson parts = (!plan.is_null() && plan.is_object() && plan.contains("parts") &&
                   plan["parts"].is_array())
                      ? plan["parts"]
                      : ojson::array();

    // Not an assembly (or <2 parts): the existing single-doc multibody path is correct.
    if (!want_asm || parts.size() < 2)
        return run_design(request, doc_id, provider_cfg, emit, options);

    RunEvents events(run_id, emit);
    events.emit("run_started", json{{"request", request},
                                    {"doc_id", doc_id},
                                    {"assembly", true},
                                    {"part_count", static_cast<int>(parts.size())}});

    // 2) Build each part in its OWN child doc (full run_design), then save it to a .step+ file.
    struct SavedPart { std::string path, name; double pos[3]; };
    std::vector<SavedPart> saved;
    int idx = 0;
    for (const auto& p : parts) {
        std::string name = (p.contains("name") && p["name"].is_string())
                               ? p["name"].get<std::string>() : ("part" + std::to_string(idx + 1));
        std::string purpose = (p.contains("purpose") && p["purpose"].is_string())
                                  ? p["purpose"].get<std::string>() : "";
        std::string dim_s;
        if (p.contains("dimensions") && p["dimensions"].is_object())
            for (auto it = p["dimensions"].begin(); it != p["dimensions"].end(); ++it) {
                if (!dim_s.empty()) dim_s += ", ";
                dim_s += it.key() + "=" + py_scalar_str(it.value());
            }
        std::string feats;
        if (p.contains("features") && p["features"].is_array())
            for (const auto& f : p["features"]) {
                if (!feats.empty()) feats += "; ";
                feats += py_scalar_str(f);
            }
        double pos[3] = {0, 0, 0};
        if (p.contains("placement") && p["placement"].is_object() &&
            p["placement"].contains("pos") && p["placement"]["pos"].is_array() &&
            p["placement"]["pos"].size() >= 3)
            for (int k = 0; k < 3; ++k)
                if (p["placement"]["pos"][k].is_number()) pos[k] = p["placement"]["pos"][k].get<double>();

        events.emit("part_started", json{{"index", idx},
                                         {"total", static_cast<int>(parts.size())},
                                         {"name", name}});

        std::string part_req = "Build ONLY this single standalone part, centered at the origin (it "
                               "will be positioned in an assembly afterward). Part: '" + name +
                               "' — " + purpose + ". Dimensions: " + dim_s +
                               ". Features: " + feats;
        std::string child;
        try {
            json nd = doc::doc_new("part", name);
            child = nd.value("doc_id", std::string());
        } catch (...) {
        }
        if (child.empty()) { ++idx; continue; }

        json sub_opts = options;
        sub_opts["run_id"] = run_id + "-p" + std::to_string(idx);
        run_design(part_req, child, provider_cfg, emit, sub_opts);

        std::filesystem::path path = run_dir(run_id) / ("part_" + std::to_string(idx) + ".step+");
        try {
            doc::doc_save(child, path.string());
            SavedPart sp;
            sp.path = std::filesystem::absolute(path).string();
            sp.name = name;
            sp.pos[0] = pos[0]; sp.pos[1] = pos[1]; sp.pos[2] = pos[2];
            saved.push_back(sp);
        } catch (...) {
        }
        ++idx;
    }

    if (saved.empty()) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "no_parts_built"}, {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"}, {"stop_reason", "no_parts_built"},
                    {"revision", 0}, {"doc_id", doc_id}};
    }

    // 3) Create the assembly doc and insert each saved part as a positioned, fixed component.
    std::string asm_name = (!plan.is_null() && plan.contains("name") && plan["name"].is_string())
                               ? plan["name"].get<std::string>() : "Assembly";
    std::string asm_id;
    try {
        json nd = doc::doc_new("assembly", asm_name);
        asm_id = nd.value("doc_id", std::string());
    } catch (...) {
    }
    if (asm_id.empty()) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "assembly_doc_failed"}, {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"}, {"stop_reason", "assembly_doc_failed"},
                    {"revision", 0}, {"doc_id", doc_id}};
    }
    json comp_ops = json::array();
    for (size_t k = 0; k < saved.size(); ++k) {
        const SavedPart& sp = saved[k];
        // 4x4 row-major transform = identity + translation (placement).
        json transform = {1, 0, 0, sp.pos[0], 0, 1, 0, sp.pos[1],
                          0, 0, 1, sp.pos[2], 0, 0, 0, 1};
        comp_ops.push_back(json{{"op", "asm_insert_component"},
                                {"source", {{"path", sp.path}}},
                                {"name", sp.name},
                                {"transform", transform},
                                {"fixed", k == 0}});
    }
    int asm_rev = 0;
    // Absolute path the host opens to display the assembly. The C++ core REJECTS opening
    // assembly docs (doc_open "unsupported" by schema), so the host routes this file open to
    // the Python worker — exactly the File>Open / Explorer-double-click path. Components are
    // referenced by absolute .step+ paths (saved above under the same run_dir), so the loaded
    // assembly resolves its parts regardless of the host's cwd.
    std::string asm_path =
        std::filesystem::absolute(run_dir(run_id) / (asm_name + ".step+")).string();
    try {
        json before = doc::doc_tree(asm_id);
        json resp = doc::doc_apply_ops(asm_id, before.value("revision", 0), comp_ops, false);
        asm_rev = resp.value("revision", 0);
        doc::doc_save(asm_id, asm_path);
    } catch (const std::exception& e) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", std::string("assemble_failed: ") + e.what()},
                                         {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"},
                    {"stop_reason", "assemble_failed"}, {"revision", 0}, {"doc_id", asm_id},
                    {"created_doc", true}, {"assembly", true}};
    }

    events.emit("run_finished", json{{"status", "done"},
                                     {"stop_reason", "assembled"},
                                     {"revision", asm_rev},
                                     {"components", static_cast<int>(saved.size())}});
    return json{{"run_id", run_id}, {"status", "done"}, {"stop_reason", "assembled"},
                {"revision", asm_rev}, {"doc_id", asm_id}, {"created_doc", true},
                {"assembly", true}, {"asm_path", asm_path},
                {"components", static_cast<int>(saved.size())}};
}

}  // namespace agent
}  // namespace aicad
