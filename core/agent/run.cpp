// core/agent/run.cpp — see run.h. Mirrors agent/orchestrator/run.py (P53b).
#include "agent/run.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <map>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <utility>

#if !defined(_WIN32)
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <thread>
#endif

#include "agent/complexity.h"
#include "agent/config.h"
#include "agent/copilot_modes.h"
#include "agent/domain.h"
#include "agent/events.h"
#include "agent/firm_bid.h"
#include "agent/iface_plan.h"
#include "agent/firm_team.h"
#include "agent/logging_provider.h"
#include "agent/machine.h"
#include "security/integrity.h"
#include "util/platform_paths.h"
#include "agent/mission.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/state.h"
#include "doc/session.h"
#if !defined(_WIN32)
#include "util/parent_death.h"
#endif
#include "util/platform_paths.h"

namespace chudware {
namespace agent {

namespace {
thread_local DocPublishFn t_doc_publish_sink;
}

void set_doc_publish_sink(DocPublishFn sink) {
    t_doc_publish_sink = std::move(sink);
}

namespace {

void publish_doc_call(const std::string& method, const json& params, const json& result) {
    if (!t_doc_publish_sink) return;
    try {
        t_doc_publish_sink(method, params, result);
    } catch (...) {
        // A spectator that disconnects while an agent is building must never break the build.
        // This matches RunEvents' live-stream sink: publication is best-effort, document commits
        // remain authoritative.
    }
}

json doc_publish_params(std::initializer_list<std::pair<const char*, json>> fields,
                        const json& options) {
    json params = json::object();
    for (const auto& [key, value] : fields) params[key] = value;
    if (options.value("internal", false)) params["internal"] = true;
    return params;
}

}

namespace {

using ojson = nlohmann::ordered_json;

template<typename JsonT>
std::string jstr(const JsonT& j, const char* key, const std::string& d = std::string()) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return d;
    return it->template get<std::string>();
}

// How many parse/validate failures before the modeler escape hatch (raw build123d script)
// fires. Kept deliberately high so the agent keeps trying REAL parametric ops — each retry is
// re-prompted with the concrete validation error (state.last_errors flows into build_context),
// which is how it self-corrects. The script escape is a genuine last resort, not a 2-strike
// default (the old value of 2 made the agent bail to a canned script on anything non-trivial).
constexpr int kEscapeAfter = 4;

// Prepended to ERRORS on ops-parse failures so the next modeler attempt gets an
// explicit format directive (not just the underlying parse error string).
const char* kOpsParseFormatDirective =
    "REPLY FORMAT ERROR: your previous reply did not contain a valid ```json ops array. "
    "Reply with ONLY a fenced ```json code block containing the ops array — no prose "
    "before or after.";

// Feedback when the model re-requests full_ops after it was already granted.
const char* kAlreadyHaveFullOps =
    "you already have the full op set; emit the real document ops now (do not call "
    "request_tools for full_ops again)";

// True when a dry-run / validate error looks like an unknown op or feature type —
// the wording produced by doc/op_registry (unknown op '…') and doc/validate
// (…type: is not a feature type) / document rebuild (no compiler for feature type).
bool looks_like_unknown_op_error(const std::string& err) {
    auto has = [&](const char* n) { return err.find(n) != std::string::npos; };
    return has("unknown op") || has("is not a feature type") ||
           has("no compiler for feature type");
}

// Apply a peeled request_tools payload onto DesignState. Returns the list of
// classes newly granted this call (for the tool_scope event). full_ops already
// held is a no-op; domain classes already held are skipped.
std::vector<std::string> grant_tool_scope(DesignState& st, const std::vector<std::string>& requested) {
    std::vector<std::string> newly;
    for (const std::string& cls : requested) {
        if (cls == "full_ops") {
            if (!st.tool_scope_full) {
                st.tool_scope_full = true;
                newly.push_back("full_ops");
            }
            st.tool_scope_classes.insert("full_ops");
            continue;
        }
        if (st.tool_scope_classes.insert(cls).second) newly.push_back(cls);
    }
    return newly;
}


// Per-call output-token budget by provider + reasoning effort. The hardcoded 4096 throttled
// capable cloud models (a complex part is one modeler call → truncation → parse fail → escape
// hatch). Capable cloud backends get real headroom; local models stay modest.
int call_budget_for(const json& provider_cfg) {
    // Explicit user override (config "max_output_tokens" -> provider.max_tokens) wins. This is the
    // knob the AI settings expose: raise it when a reasoning model spends its output budget
    // thinking and returns no/truncated JSON. Clamp to a sane floor so a tiny value can't starve
    // every stage; no hard ceiling (the user opts into the cost).
    if (provider_cfg.contains("max_tokens") && provider_cfg["max_tokens"].is_number_integer()) {
        int mt = provider_cfg["max_tokens"].get<int>();
        if (mt > 0) return mt < 512 ? 512 : mt;
    }
    std::string name = provider_cfg.value("name", std::string());
    std::string effort = provider_cfg.value("reasoning_effort", std::string());
    int base = 4096;  // local / unknown
    if (name == "anthropic" || name == "anthropic-sub" || name == "gemini" || name == "openai" ||
        name == "xai" || name == "xai-sub")
        base = 8192;
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

// ----------------------------------------------------------------- part naming




// Deterministic fallback name from the user request: first ~4 words, title-cased.
std::string fallback_part_name(const std::string& request) {
    std::string acc;
    acc.reserve(48);
    int words = 0;
    bool in_word = false;
    bool word_start = true;
    for (unsigned char c : request) {
        if (std::isalnum(c) || c == '-' || c == '_') {
            if (!in_word) {
                if (words >= 4) break;
                if (!acc.empty()) acc.push_back(' ');
                ++words;
                in_word = true;
                word_start = true;
            }
            if (word_start) {
                acc.push_back(static_cast<char>(std::toupper(c)));
                word_start = false;
            } else {
                acc.push_back(static_cast<char>(std::tolower(c)));
            }
        } else {
            in_word = false;
        }
    }
    if (acc.empty()) acc = "Part";
    return sanitize_part_name(acc);
}

// ----------------------------------------------------------------- file-first persistence

// Where the agent persists the part file it creates BEFORE modeling starts (so
// version control / PLM sees a real file from the first feature, not only after
// the run). Explicit caller/configured paths are usable only when they already
// name a directory. With no explicit workspace, create and validate the
// product-owned Documents/Chudware default. Returning empty means "absent" and
// causes the worker RPC to omit workspace_dir.
std::string agent_workspace_dir(const json& options) {
    namespace fs = std::filesystem;
    auto existing_dir = [](const std::string& value) -> std::string {
        if (value.empty()) return {};
        std::error_code ec;
        const fs::path absolute = fs::absolute(value, ec).lexically_normal();
        if (ec || !fs::is_directory(absolute, ec) || ec) return {};
        return absolute.string();
    };
    if (options.contains("save_dir") && options["save_dir"].is_string()) {
        const std::string requested = options["save_dir"].get<std::string>();
        if (!requested.empty()) return existing_dir(requested);
    }
    json cfg = load_config();
    if (cfg.is_object() && cfg.contains("workspace_dir") && cfg["workspace_dir"].is_string()) {
        const std::string configured = cfg["workspace_dir"].get<std::string>();
        if (!configured.empty()) return existing_dir(configured);
    }
#ifdef _WIN32
    const char* up = std::getenv("USERPROFILE");
    const fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path();
#else
    const char* home = std::getenv("HOME");
    const fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path();
#endif
    if (base.empty()) return {};
    const fs::path product_default = base / "Chudware";
    std::error_code ec;
    fs::create_directories(product_default, ec);
    if (ec || !fs::is_directory(product_default, ec) || ec) return {};
    return fs::absolute(product_default, ec).lexically_normal().string();
}

// Filename stem from a part name — mirrors the host auto-save rule (app.cpp):
// spaces -> '_', keep alnum / '_' / '-', drop the rest.
std::string stepplus_stem(const std::string& name) {
    std::string stem;
    stem.reserve(name.size());
    for (unsigned char c : name) {
        if (c == ' ') stem.push_back('_');
        else if (std::isalnum(c) || c == '_' || c == '-')
            stem.push_back(static_cast<char>(c));
    }
    return stem.empty() ? std::string("Part") : stem;
}

// First non-existing <dir>/<stem>.step+, then <stem>_2, _3, … (host rule).
std::string unique_stepplus_path(const std::string& dir, const std::string& stem) {
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::create_directories(dir, ec);  // idempotent
    fs::path candidate = fs::path(dir) / (stem + ".step+");
    if (!fs::exists(candidate, ec)) return candidate.string();
    for (int n = 2; n < 10000; ++n) {
        fs::path alt = fs::path(dir) / (stem + "_" + std::to_string(n) + ".step+");
        if (!fs::exists(alt, ec)) return alt.string();
    }
    return candidate.string();  // absurd; overwrite rather than fail
}

// ----------------------------------------------------------------- provider call

// A reply + duration + meta, exactly like run.py _agent_call returns.
struct AgentCall {
    std::string reply;
    std::string sent_prompt;  // exact system+user prompt sent to the model (capped)
    std::string thinking;
    std::string model;
    int duration_ms = 0;
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
    std::string thinking;
    std::string model;
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
        r.thinking = cr.thinking;
        r.model = !cr.model.empty() ? cr.model : cfg.value("model", std::string());
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
                     const std::string& prompts_dir, int max_tokens, bool vision,
                     int context_cap = kContextTokenCap,
                     const std::string& extra_directive = std::string()) {
    BuiltContext bc = build_context(state, role, prompts_dir, context_cap);
    std::string system = "ROLE: " + role + "\n" + bc.system;
    AgentCall ac;
    ac.prompt_file = bc.prompt_source;
    ac.prompt_hash = prompt_hash(system);
    ac.context_chars = bc.context_chars;
    ac.context_tokens_est = bc.context_tokens_est;
    ac.inputs_digest = inputs_digest(state);

    // Experimental: an optional run-scoped directive appended to the user body (empty by default,
    // so the context + the parity gate are unchanged). Used by cad_sequence_mode to nudge the
    // modeler toward an editable parametric feature sequence.
    std::string body = extra_directive.empty() ? bc.body : (bc.body + "\n\n" + extra_directive);

    // Capture the exact prompt text for agent_output transparency (capped ~4k).
    ac.sent_prompt = system + "\n\n" + body;
    if (ac.sent_prompt.size() > 4000) {
        ac.sent_prompt.resize(4000);
        ac.sent_prompt += "\n\u2026[truncated]";
    }

    // Vision: when the provider supports images and a render of the current model exists,
    // attach it so the model can SEE the geometry (reviewer/fixer self-correction).
    json parts = json::array({text_part(body)});
    if (vision && state.last_render.is_string() && !state.last_render.get<std::string>().empty())
        parts.push_back(image_part(state.last_render.get<std::string>()));
    json messages = json::array({user_message(parts)});

    auto t0 = std::chrono::steady_clock::now();
    ProvReply rep = provider(system, messages, max_tokens);  // provider/effort-scaled budget
    auto t1 = std::chrono::steady_clock::now();
    ac.duration_ms =
        static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    ac.thinking = rep.thinking;
    ac.model = rep.model;
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

// Forward streaming token deltas to a run's event stream so the GUI shows the
// model's continuous output live (channel "text" | "thinking"). Deltas are
// COALESCED per channel (flush at >=48 chars, newline, or >=60ms since last
// flush) so we don't reopen events.jsonl per token — grok emits thousands.
// The final assistant message is persisted separately, so a trailing sub-flush
// buffer is not authoritative.
struct DeltaCoalescer {
    // Mirrored by services/agent/agent_turn.py::_AssistantDeltaCoalescer.
    // Keep cross-channel ordering and flush rules identical in both lanes.
    RunEvents* ev = nullptr;
    std::string buf_text, buf_think;
    std::string active_channel;
    std::chrono::steady_clock::time_point last_flush_text =
        std::chrono::steady_clock::now();
    std::chrono::steady_clock::time_point last_flush_think =
        std::chrono::steady_clock::now();
    std::string& chan(const std::string& kind) {
        return kind == "thinking" ? buf_think : buf_text;
    }
    std::chrono::steady_clock::time_point& last_flush(const std::string& kind) {
        return kind == "thinking" ? last_flush_think : last_flush_text;
    }
    void flush(const std::string& kind) {
        std::string& b = chan(kind);
        if (b.empty()) return;
        ev->emit("assistant_delta", json{{"text", b}, {"channel", kind}});
        b.clear();
        last_flush(kind) = std::chrono::steady_clock::now();
    }
    void push(const std::string& d, const std::string& kind) {
        const std::string channel = kind == "thinking" ? "thinking" : "text";
        // A tail from the prior channel arrived first and must be emitted first.
        // Keeping both buffers open reordered the transcript and glued an old
        // tail directly onto a later completion's opening text.
        if (!active_channel.empty() && active_channel != channel)
            flush(active_channel);
        active_channel = channel;
        std::string& b = chan(channel);
        b += d;
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - last_flush(channel))
                            .count();
        if (b.size() >= 48 || b.find('\n') != std::string::npos || ms >= 60)
            flush(channel);
    }
    void flush_all() {
        if (!active_channel.empty()) flush(active_channel);
        if (active_channel != "text") flush("text");
        if (active_channel != "thinking") flush("thinking");
        active_channel.clear();
    }
};

// Install `coalescer` as this thread's provider delta sink, restoring the
// previous emitter on scope exit (mission -> assembly probe -> run_design nest
// on one dispatch thread, so clear-to-null would orphan the parent's stream).
struct ProviderDeltaGuard {
    std::shared_ptr<DeltaCoalescer> c;
    std::function<void(const std::string&, const std::string&)> prev;
    explicit ProviderDeltaGuard(std::shared_ptr<DeltaCoalescer> coalescer) : c(std::move(coalescer)) {
        prev = provider_delta_emitter();
        std::weak_ptr<DeltaCoalescer> w = c;
        set_provider_delta_emitter([w](const std::string& delta, const std::string& kind) {
            if (auto sp = w.lock()) sp->push(delta, kind);
        });
    }
    ~ProviderDeltaGuard() {
        if (c) c->flush_all();
        set_provider_delta_emitter(std::move(prev));
    }
};

// ---- Admin turn seam: services worker "agent_admin_turn" RPC ----------------
// core/hostd/worker_bridge.cpp already owns a persistent worker bridge, but it lives in the
// hostd executable, which dlopens this agent code as a shared library (never the other way
// around), so nothing in core/agent can reach that singleton across the link boundary. This
// is a minimal, single-shot client for the one method the admin branch needs, mirroring
// hostd's WorkerBridge fork/pipe/PDEATHSIG idiom. Packaged builds require
// CHUDWARE_WORKER_BIN; developer builds retain Python source execution. POSIX only;
// Windows reports unavailable so the caller falls back to the existing blocked response.
#if !defined(_WIN32)
bool admin_worker_file_exists(const std::string& p) {
    std::error_code ec;
    return std::filesystem::is_regular_file(p, ec);
}

// Packaged builds derive the canonical adjacent worker from the current executable and reject
// non-adjacent inherited overrides. Developer builds retain compiled overrides and Python.
std::string admin_worker_bin(bool& compiled, std::string& err) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env) {
        compiled = true;
        std::error_code ec;
        const std::string path = std::filesystem::weakly_canonical(env, ec).string();
        return ec ? std::string(env) : path;
    }
    compiled = false;
    if (const char* env = std::getenv("CHUDWARE_PYTHON"); env && *env) return std::string(env);
    const char* home_c = std::getenv("HOME");
    const std::string home = home_c ? home_c : "";
    if (!home.empty()) {
        const std::string venv = home + "/.local/share/Chudware/runtime/venv/bin/python";
        if (admin_worker_file_exists(venv)) return venv;
    }
    for (const char* cand : {"/usr/bin/python3", "/usr/local/bin/python3", "/usr/bin/python"})
        if (admin_worker_file_exists(cand)) return cand;
    return "python3";
#else
    compiled = true;
    const std::string executable = platform::executablePath();
    if (executable.empty()) {
        err = "could not resolve the current executable path";
        return {};
    }
#ifdef _WIN32
    constexpr const char* name = "chudware-worker.exe";
#else
    constexpr const char* name = "chudware-worker";
#endif
    std::error_code ec;
    const std::string adjacent = std::filesystem::weakly_canonical(
                                     std::filesystem::path(executable).parent_path() / name, ec)
                                     .string();
    if (ec) {
        err = "could not canonicalize adjacent worker path";
        return {};
    }
    if (const char* env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env) {
        std::error_code envEc;
        const auto supplied = std::filesystem::weakly_canonical(env, envEc);
        if (envEc || supplied != std::filesystem::path(adjacent)) {
            err = "CHUDWARE_WORKER_BIN must name the adjacent packaged worker";
            return {};
        }
    }
    return adjacent;
#endif
}

std::string admin_worker_repo_root() {
    if (const char* env = std::getenv("CHUDWARE_REPO"); env && *env) return std::string(env);
    const char* home_c = std::getenv("HOME");
    return home_c ? std::string(home_c) + "/Repositories/CAD" : std::string(".");
}

// Spawns the services worker, sends ONE
// {"id":1,"method":"agent_admin_turn",...} line carrying the request text, run_id,
// resolved provider descriptor, workspace_dir, session identity, the prior-turn
// `history`, and the turn's `attachments` array (pasted images / attached files, as
// built by the GUI composer's consumeAttachments), then relays every streamed
// notification onto `events` as it arrives (same live stream the rest of the
// run uses), and fills `turn_out` with the handler's `turn` object on success.
// Returns false (with `err` set) on ANY transport failure, timeout, or a
// worker-reported ok:false (offline mode, no provider, turn exception, ...) —
// the caller's contract is to treat every false return identically: fall back
// to the blocked response.
//
// `crashed` is set when the worker DIED rather than answered (killed by a signal,
// or exited without a response). 2026-07-28: a shorthand sketch entity
// ({"type":"circle","cx":…} with no "center") drove nlohmann's UB const
// operator[] inside chudware::sketch::allocate_entity and SIGSEGV'd the worker
// mid-turn; the operator saw only "admin worker closed stdout" and a
// `non_modeling_request` stop reason, because the child's stderr went to the
// host's own stderr and the wait status was never inspected. Both are now
// captured: `err` carries the signal name and the worker's last stderr, and the
// caller reports stop_reason "worker_crash". `timed_out` distinguishes a worker that
// went SILENT from one that died or declined, so the caller can report the honest
// stop_reason instead of filing a timeout under "non_modeling_request".
bool call_admin_worker_turn(const std::string& request, const std::string& run_id,
                            const json& provider_cfg, const std::string& workspace_dir,
                            const std::string& session_id, const std::string& sessions_dir,
                            const json& history, const json& attachments,
                            RunEvents& events, json& turn_out,
                            std::string& err, bool& crashed, bool& timed_out) {
    const auto executable = chudware::platform::executablePath();
    const auto integrity = chudware::security::verifyInstalledPayload(
        executable, chudware::security::VerifyMode::ProtectedOperation);
    if (!integrity.ok) {
        err = "worker spawn blocked: protected_file_hash_mismatch";
        return false;
    }
    bool compiled_worker = false;
    const std::string worker_bin = admin_worker_bin(compiled_worker, err);
    if (worker_bin.empty()) {
        if (err.empty()) err = "worker launcher unavailable";
        return false;
    }
    if ((compiled_worker || worker_bin.find('/') != std::string::npos) &&
        !admin_worker_file_exists(worker_bin)) {
        err = (compiled_worker ? "compiled worker not found at " : "python runtime not found at ") +
              worker_bin;
        return false;
    }
    const std::string repo = compiled_worker
                                 ? std::filesystem::path(worker_bin).parent_path().string()
                                 : admin_worker_repo_root();

    int out_pipe[2] = {-1, -1}, in_pipe[2] = {-1, -1}, err_pipe[2] = {-1, -1};
    auto close_pipes = [&] {
        for (int fd : out_pipe) if (fd >= 0) ::close(fd);
        for (int fd : in_pipe) if (fd >= 0) ::close(fd);
        for (int fd : err_pipe) if (fd >= 0) ::close(fd);
    };
    if (::pipe(out_pipe) != 0 || ::pipe(in_pipe) != 0 || ::pipe(err_pipe) != 0) {
        err = std::string("pipe() failed: ") + std::strerror(errno);
        close_pipes();
        return false;
    }
    const pid_t parent_pid = ::getpid();
    pid_t pid = ::fork();
    if (pid < 0) {
        err = std::string("fork() failed: ") + std::strerror(errno);
        close_pipes();
        return false;
    }
    if (pid == 0) {
        ::setpgid(0, 0);
        // Parent-death linkage: PDEATHSIG on Linux, a kqueue babysitter on Darwin.
        // Never returns if the host process already died.
        platform::guardParentDeath(parent_pid);
        ::dup2(in_pipe[0], STDIN_FILENO);
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(err_pipe[1], STDERR_FILENO);
        ::close(in_pipe[0]); ::close(in_pipe[1]); ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        platform::closeInheritedFds(3);  // never leak the host process's own fds into the worker
        if (!repo.empty()) { if (::chdir(repo.c_str()) != 0) { /* exec from cwd; worker import fails clearly */ } }
        if (compiled_worker)
            ::execlp(worker_bin.c_str(), worker_bin.c_str(), "--module", "worker", (char*)nullptr);
        else
            ::execlp(worker_bin.c_str(), worker_bin.c_str(), "-m", "services.worker_main",
                     (char*)nullptr);
        _exit(127);
    }
    ::setpgid(pid, pid);
    ::close(in_pipe[0]);
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);
    ::fcntl(out_pipe[0], F_SETFL, O_NONBLOCK);
    ::fcntl(err_pipe[0], F_SETFL, O_NONBLOCK);
    const int rd = out_pipe[0];
    const int ed = err_pipe[0];
    const int wr = in_pipe[1];
    bool child_reaped = false;

    // Rolling tail of the worker's stderr. It MUST be drained even when nothing
    // goes wrong: an unread 64 KiB pipe would block a chatty worker forever.
    // Only the tail is kept — a python traceback's last lines are the diagnosis.
    constexpr size_t kStderrTailMax = 8192;
    std::string stderr_tail;
    auto drain_stderr = [&]() {
        char buf[4096];
        for (;;) {
            ssize_t n = ::read(ed, buf, sizeof(buf));
            if (n <= 0) break;
            stderr_tail.append(buf, static_cast<size_t>(n));
            if (stderr_tail.size() > kStderrTailMax)
                stderr_tail.erase(0, stderr_tail.size() - kStderrTailMax);
        }
    };
    // Why the worker stopped answering: the wait status (signal name for a native
    // crash, exit code otherwise) plus the tail of what it printed on the way out.
    auto death_detail = [&](const char* prefix) {
        drain_stderr();
        int status = 0;
        std::string what = prefix;
        for (int i = 0; i < 100; ++i) {
            if (child_reaped) break;
            pid_t r = ::waitpid(pid, &status, WNOHANG);
            if (r == pid || (r < 0 && errno == ECHILD)) child_reaped = true;
            if (r == pid) {
                if (WIFSIGNALED(status)) {
                    const int sig = WTERMSIG(status);
                    what = std::string("admin worker crashed: ") + ::strsignal(sig) +
                           " (signal " + std::to_string(sig) + ")";
                } else if (WIFEXITED(status)) {
                    what = "admin worker exited with code " +
                           std::to_string(WEXITSTATUS(status)) + " without answering";
                }
                break;
            }
            if (r < 0 && errno == ECHILD) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        std::string tail = stderr_tail;
        while (!tail.empty() && (tail.back() == '\n' || tail.back() == '\r')) tail.pop_back();
        if (!tail.empty()) {
            // Last few lines only: the chat card has to stay readable.
            size_t cut = tail.size();
            for (int lines = 0; lines < 6 && cut > 0; ++lines) {
                size_t nl = tail.rfind('\n', cut - 1);
                if (nl == std::string::npos) { cut = 0; break; }
                cut = nl;
            }
            if (cut > 0) tail = "…" + tail.substr(cut + 1);
            events.emit("run_note", json{{"note", "admin worker stderr"},
                                         {"reason", stderr_tail}});
            what += " — worker stderr: " + tail;
        }
        return what;
    };

    auto reap = [&](bool graceful) {
        if (child_reaped) return;
        if (graceful) {
            for (int i = 0; i < 100; ++i) {
                int status = 0;
                pid_t r = ::waitpid(pid, &status, WNOHANG);
                if (r == pid || (r < 0 && errno == ECHILD)) {
                    child_reaped = true;
                    return;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        }
        // Whole process group: the worker plus any tool subprocesses it spawned
        // (both sides setpgid the child into its own group).
        ::kill(-pid, SIGKILL);
        ::kill(pid, SIGKILL);
        if (::waitpid(pid, nullptr, 0) == pid) child_reaped = true;
    };

    // C4: the provider the user picked in the composer is ALREADY resolved into
    // provider_cfg by the time we get here. Thread the whole descriptor through so
    // _agent_admin_turn runs THAT provider instead of re-resolving config.json's flat
    // `provider` field (which silently ignored the GUI's choice). Empty fields are
    // omitted rather than sent as "" so the worker's "caller supplied nothing" branch
    // stays distinguishable from "caller supplied a blank".
    json params{{"request", request}, {"run_id", run_id}, {"role", "general_agent"}};
    // Invalid/absent workspace configuration is omitted. Python distinguishes
    // omission from an explicit invalid workspace_dir and rejects the latter
    // before provider construction.
    emit_existing_workspace_dir(params, workspace_dir);
    // Bind the services worker's lossless sub-agent raw transcripts to the owning GUI
    // conversation. Empty identity is omitted, matching workspace/history semantics.
    if (!session_id.empty()) params["session_id"] = session_id;
    if (!sessions_dir.empty()) params["sessions_dir"] = sessions_dir;
    // Prior conversation turns from the GUI's session store (options.history), oldest
    // first — the worker forks fresh per turn, so without this every follow-up was a
    // cold start. Absent/empty array == previous behaviour (no key sent at all).
    if (history.is_array() && !history.empty()) params["history"] = history;
    // Turn attachments ({path,name,kind,mime,...}) from the composer — the vision
    // path needs the image objects to reach the provider, and the worker is the
    // only place that can build them. Absent/empty array == previous behaviour
    // (no key sent at all).
    if (attachments.is_array() && !attachments.empty()) params["attachments"] = attachments;
    if (provider_cfg.is_object()) {
        auto put = [&](const char* cfg_key, const char* param_key) {
            const auto it = provider_cfg.find(cfg_key);
            if (it == provider_cfg.end() || !it->is_string()) return;
            std::string v = it->get<std::string>();
            if (!v.empty()) params[param_key] = std::move(v);
        };
        put("name", "provider");
        put("model", "model");
        put("api_key", "api_key");
        put("base_url", "base_url");
        put("reasoning_effort", "reasoning_effort");
    }
    const json req = {{"id", 1}, {"method", "agent_admin_turn"}, {"params", std::move(params)}};
    const std::string line = req.dump() + "\n";
    bool wrote = true;
    {
        const char* p = line.data();
        size_t left = line.size();
        while (left > 0) {
            ssize_t n = ::write(wr, p, left);
            if (n < 0) { if (errno == EINTR) continue; wrote = false; break; }
            if (n == 0) { wrote = false; break; }
            p += n; left -= static_cast<size_t>(n);
        }
    }
    ::close(wr);  // EOF once we're answered -> the worker's `for line in sys.stdin` loop exits cleanly
    if (!wrote) {
        err = "write to admin worker failed";
        ::close(rd);
        reap(false);
        ::close(ed);
        return false;
    }

    // A bounded project_organizer turn (up to 30 tool calls) genuinely needs minutes,
    // not the sub-second budget of a status/list RPC. This ceiling used to be an
    // ABSOLUTE 5 minutes from the write, which killed runs that were working the entire
    // time: a live modelling run (run-42508173312214, 2026-07-29) streamed 270 assistant
    // deltas and built five real, persisted features, then hit the wall clock and was
    // reported to the user as `non_modeling_request` — "nothing was changed" — while the
    // part sat in the feature tree. The worker STREAMS every event onto this run's stream
    // (the notification branch below), so bytes on its stdout are proof of liveness: time
    // out on SILENCE, not on duration. There is no absolute wall-clock cap: a 2 h one
    // stopped healthy long turns mid-build. A worker that streams without answering is
    // ended by the Stop button, polled every iteration, or by its own death.
    constexpr auto kIdleTimeout = std::chrono::minutes(5);
    auto idle_deadline = std::chrono::steady_clock::now() + kIdleTimeout;
    std::string read_buf;
    constexpr size_t kMaxWorkerLine = 64u * 1024u * 1024u;
    bool got_final = false;
    size_t scan_from = 0;
    bool ok_final = false;
    // Stop button: run_design registered this run's RunControl and pointed the
    // provider cancel flag at it before entering the pre-flight branch, but the
    // worker sidecar cannot observe that flag across the process boundary — THIS
    // loop is the cancellation point. Poll it every iteration (200ms poll tick);
    // on cancel, hard-kill the worker's process group so the output stream dies
    // immediately instead of streaming on for up to 5 minutes.
    const std::atomic<bool>* cancel_flag = provider_cancel_flag();
    bool cancelled = false;
    for (;;) {
        if (cancel_flag && cancel_flag->load(std::memory_order_relaxed)) {
            cancelled = true;
            err = "cancelled";
            break;
        }
        size_t nl = read_buf.find('\n', scan_from);
        if ((nl != std::string::npos && nl > kMaxWorkerLine) ||
            (nl == std::string::npos && read_buf.size() > kMaxWorkerLine)) {
            err = "admin worker response line exceeds 64 MiB";
            break;
        }
        scan_from = read_buf.size();
        if (nl == std::string::npos) {
            if (std::chrono::steady_clock::now() >= idle_deadline) {
                err = "timeout waiting for admin worker response (no output for 5 min)";
                timed_out = true;
                break;
            }
            struct pollfd pfds[2] = {{rd, POLLIN, 0}, {ed, POLLIN, 0}};
            int pr = ::poll(pfds, 2, 200);
            if (pr < 0) { if (errno == EINTR) continue; err = "poll() failed"; break; }
            if (pfds[1].revents) drain_stderr();
            if (pr == 0 || !(pfds[0].revents & (POLLIN | POLLHUP | POLLERR))) {
                int status = 0;
                pid_t r = ::waitpid(pid, &status, WNOHANG);
                if (r == pid || (r < 0 && errno == ECHILD)) {
                    child_reaped = true;
                    // Reap already consumed the status when r == pid; re-derive from it
                    // directly rather than waiting again on a gone child.
                    drain_stderr();
                    if (r == pid && WIFSIGNALED(status)) {
                        const int sig = WTERMSIG(status);
                        err = std::string("admin worker crashed: ") + ::strsignal(sig) +
                              " (signal " + std::to_string(sig) + ")";
                    } else if (r == pid && WIFEXITED(status)) {
                        err = "admin worker exited with code " +
                              std::to_string(WEXITSTATUS(status)) + " without answering";
                    } else {
                        err = "admin worker exited early";
                    }
                    if (!stderr_tail.empty()) {
                        events.emit("run_note", json{{"note", "admin worker stderr"},
                                                     {"reason", stderr_tail}});
                        err += " — worker stderr: " + stderr_tail.substr(
                                   stderr_tail.size() > 600 ? stderr_tail.size() - 600 : 0);
                    }
                    crashed = true;
                    break;
                }
                continue;
            }
            char chunk[4096];
            ssize_t got = ::read(rd, chunk, sizeof(chunk));
            if (got > 0) {
                read_buf.append(chunk, static_cast<size_t>(got));
                // Progress: the worker is alive and talking, so the silence clock restarts.
                idle_deadline = std::chrono::steady_clock::now() + kIdleTimeout;
                continue;
            }
            if (got == 0) {
                // EOF on the worker's stdout: it is gone, not idle. Report WHY.
                err = death_detail("admin worker closed stdout without answering");
                crashed = true;
                break;
            }
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
            err = std::string("read() failed: ") + std::strerror(errno);
            break;
        }
        scan_from = 0;
        const std::string resp_line = read_buf.substr(0, nl);
        read_buf.erase(0, nl + 1);
        if (resp_line.empty()) continue;
        json j;
        try {
            j = json::parse(resp_line);
        } catch (...) {
            continue;  // ignore stray non-JSON noise on stdout
        }
        if (!j.is_object() || !j.contains("id") || !j["id"].is_number_integer() ||
            j["id"].get<int>() != 1)
            continue;
        if (j.contains("event") && !j.contains("ok")) {
            // Streamed agent-turn notification: {"id":1,"event":kind,"data":payload}.
            // Relay onto THIS run's event stream so the GUI renders the turn live,
            // the same way every other stage's events already do.
            json payload = j.value("data", json::object());
            std::string kind = payload.is_object() ? payload.value("kind", std::string("agent.admin_event"))
                                                    : std::string("agent.admin_event");
            if (payload.is_object()) payload.erase("kind");
            events.emit(kind, payload);
            continue;
        }
        got_final = true;
        if (j.value("ok", false)) {
            json result = j.value("result", json::object());
            if (result.is_object() && result.value("ok", false) && result.contains("turn")) {
                turn_out = result["turn"];
                ok_final = true;
            } else {
                err = result.is_object() ? result.value("reason", std::string("agent_admin_turn declined"))
                                         : std::string("agent_admin_turn declined");
            }
        } else {
            const auto it = j.find("error");
            err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                      : std::string("agent_admin_turn transport error");
        }
        break;
    }
    ::close(rd);
    ::close(ed);
    reap(got_final && !cancelled);
    return ok_final;
}
#else   // _WIN32
bool call_admin_worker_turn(const std::string&, const std::string&, const json&,
                            const std::string&, const std::string&, const std::string&,
                            const json&, const json&, RunEvents&, json&,
                            std::string& err, bool&, bool&) {
    err = "admin worker bridge unavailable on this platform";
    return false;
}
#endif  // !_WIN32

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
    // Cooperative cancel: register this run so agent_cancel can flip the flag from
    // another thread (HTTP xfer + stage boundaries both observe it).
    RunControl run_ctl;
    register_run_control(run_id, run_ctl);
    struct RunCtlGuard {
        std::string id;
        ~RunCtlGuard() { unregister_run_control(id); }
    } run_ctl_guard{run_id};
    // Save/restore (not clear-to-null): a mission sub-run's guard must hand the
    // parent's flag back so later parent-level provider calls stay cancellable.
    const std::atomic<bool>* prev_cancel_flag = provider_cancel_flag();
    set_provider_cancel_flag(run_ctl.flag.get());
    // A cancel that hit the parent run while this nest was being entered must not
    // be lost: inherit an already-flipped parent flag into this run's control.
    if (prev_cancel_flag && prev_cancel_flag->load(std::memory_order_acquire))
        run_ctl.request_cancel();
    struct ProvCancelGuard {
        const std::atomic<bool>* prev;
        ~ProvCancelGuard() { set_provider_cancel_flag(prev); }
    } prov_cancel_guard{prev_cancel_flag};
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

    // ---- Copilot mode options (agent|plan|swarm|edit|ask) ----
    // Absent mode = today's byte-identical path. plan_only / approved_plan / scope /
    // advisor only engage when explicitly set by the host.
    const std::string copilot_mode = options.value("mode", std::string());
    // Mode→tool gating contract (contracts/copilot_modes.json) — the same file the
    // Python path (services/agent/copilot_mode_tools.py) reads, so in-app and
    // MCP/DAG runs cannot diverge. std::nullopt when unreadable (packaged install
    // without the repo tree): the delegate gate below keeps its legacy inline
    // behavior in that case.
    const std::optional<CopilotModes> copilot_modes = CopilotModes::load();
    // plan_only: explicit flag OR mode==plan. Resume with approved_plan is NOT plan_only
    // (host keeps mode=plan while re-sending the approved plan to execute).
    bool plan_only = options.value("plan_only", false) || copilot_mode == "plan";
    const bool corporate_force = options.value("corporate_force", false) ||
                                 copilot_mode == "swarm" || copilot_mode == "eng_firm";
    const std::string execute_as = options.value("execute_as", std::string());
    // advisor defaults ON; only an explicit false disables the seam.
    const bool advisor_on = !options.contains("advisor") ? true : options.value("advisor", true);
    // scope: edit-mode feature targeting (whole-doc when absent/empty).
    json scope = (options.contains("scope") && options["scope"].is_object())
                     ? options["scope"]
                     : json::object();
    std::vector<std::string> scope_feature_ids;
    if (scope.contains("feature_ids") && scope["feature_ids"].is_array()) {
        for (const auto& fid : scope["feature_ids"])
            if (fid.is_string() && !fid.get<std::string>().empty())
                scope_feature_ids.push_back(fid.get<std::string>());
    }
    // Item 3: scope engages when mode==edit AND feature_ids present (whole-doc otherwise).
    const bool edit_scoped = (copilot_mode == "edit") && !scope_feature_ids.empty();
    // approved_plan: string (JSON text) or object — skip re-planning when set.
    ojson approved_plan = nullptr;
    if (options.contains("approved_plan") && !options["approved_plan"].is_null()) {
        const auto& ap = options["approved_plan"];
        if (ap.is_object()) {
            approved_plan = ojson::parse(ap.dump());
        } else if (ap.is_string()) {
            const std::string& s = ap.get_ref<const std::string&>();
            if (!s.empty()) {
                ojson parsed = ojson::parse(s, nullptr, false);
                if (!parsed.is_discarded() && parsed.is_object())
                    approved_plan = std::move(parsed);
                else {
                    // Non-JSON plan text: wrap as a single-part plan so plan_steps still works.
                    approved_plan = ojson::object();
                    approved_plan["name"] = "Approved plan";
                    approved_plan["summary"] = s;
                    approved_plan["parts"] = ojson::array({ojson{{"name", "part"},
                                                                {"purpose", s},
                                                                {"dimensions", ojson::object()},
                                                                {"features", ojson::array()}}});
                }
            }
        }
    }
    const bool has_approved_plan =
        !approved_plan.is_null() && approved_plan.is_object() &&
        approved_plan.contains("parts") && approved_plan["parts"].is_array() &&
        !approved_plan["parts"].empty();
    // Resume after Plan approve: execute the approved plan (do not stop at plan_ready).
    if (has_approved_plan) plan_only = false;

    // Nested sub-run depth (delegate / swarm unit). Cap at 2.
    const int run_depth = options.value("run_depth", 0);
    const bool is_sub_run = options.value("is_sub_run", false) || run_depth > 0;

    // Advisor seam: reserved. C++ has no foreman battery — leave empty (no fabricated
    // advisories). Host/Python may later push text via options.advisory_text.
    // Nested runs inherit advisory_text if the parent set one for firm team seeds.
    clear_context_advisory();
    if (advisor_on) {
        std::string adv_text;
        if (options.contains("advisory_text") && options["advisory_text"].is_string())
            adv_text = options["advisory_text"].get<std::string>();
        // Firm-team role seeds PREFIX the sub-run prompt when provided.
        if (options.contains("firm_role_seed") && options["firm_role_seed"].is_string()) {
            const std::string seed = options["firm_role_seed"].get<std::string>();
            if (!seed.empty())
                adv_text = adv_text.empty() ? seed : (seed + "\n\n" + adv_text);
        }
        if (!adv_text.empty()) set_context_advisory(adv_text);
        // else: seam armed but empty → no ADVISORY block (legacy-identical body).
    }
    struct AdvisoryGuard {
        ~AdvisoryGuard() { clear_context_advisory(); }
    } advisory_guard;

    RunEvents events(run_id, emit);

    // ---- PRE-FLIGHT: general agent is the ENTRY; the model decides to escalate ----
    // No deterministic keyword classifier decides the execution structure anymore.
    // Every top-level ONLINE run is handed to ONE free general_agent turn (fs/shell/
    // plm floor + on-demand modeling/sketch/sim/... packs via enable_tools, plus
    // delegate / mission_start to reach the build swarm). The agent itself decides
    // whether the request is basic work it finishes inline or a geometry build it
    // escalates — instead of a regex forcing everything through the modeling pipeline
    // (which turned each admin step into an extruded plate). is_general_work_request
    // survives only as an ADVISORY label for the Routing card, never a gate.
    //
    // OFFLINE is the one exception: the services worker turn needs a provider, so an
    // offline top-level run keeps the old deterministic split — general work is
    // blocked (nothing can run it), and an offline-safe local modeling run falls
    // through to the C++ pipeline below. Sub-runs are exempt everywhere: their
    // request text is a parent-authored task (a delegated build step), already vetted.
    if (!is_sub_run && (!offline || is_general_work_request(request))) {
        const bool looks_general = is_general_work_request(request);
        std::string why = capability_request_reason(request);
        if (why.empty())
            why = looks_general ? "general work (agent will finish it or escalate)"
                                : "possible build (agent will decide whether to escalate)";
        events.emit("task_route", json{{"domain", "general"},
                                       {"reason", why},
                                       {"tool_classes", json::array()},
                                       {"context_sheets", json::array()}});

        // `worker_err` is the concrete reason the general-agent turn failed — a provider
        // auth failure, a spawn error, a timeout, the worker's own offline refusal. It
        // used to be buried in a run_note the user never sees while the chat card claimed
        // the worker was unreachable, which was wrong for every case except the offline
        // pre-flight below (the ONE path that never calls the worker, and therefore the
        // one where an empty reason is the honest answer).
        // The stop_reason/status/"did anything change" split now lives in
        // classify_worker_stop (run.h) so a test can LINK the shipped rule instead of
        // restating it in a lambda — the older stop_reason cases in test_agent.cpp are
        // mirrored copies, which is exactly how a rule and its test drift apart.
        auto worker_failure_response =
            [&](const std::string& worker_err, bool worker_crashed = false,
                bool worker_timed_out = false) -> json {
            const std::string detail = worker_err.empty() ? why : worker_err;
            const WorkerStop cls =
                classify_worker_stop(worker_crashed, worker_timed_out, !worker_err.empty());
            const std::string& stop = cls.stop_reason;
            const std::string& status = cls.status;
            const std::string human =
                worker_crashed
                    ? "The agent worker process died part-way through this turn, so the "
                      "turn was abandoned. This is a crash in Chudware, not something you "
                      "did. Details: " +
                          worker_err
                : worker_timed_out
                    ? "The agent stopped responding, so this turn was abandoned part-way "
                      "through. Any features it had already built are still in the part — "
                      "check the feature tree before re-running, so a retry does not build "
                      "the same geometry twice. Details: " +
                          worker_err
                : !cls.may_have_changed_the_model
                    ? std::string("I handed this to the general agent, but couldn't reach it "
                                  "(offline mode is on), so nothing was changed. Turn offline "
                                  "mode off, or use the PLM tab / file explorer directly.")
                    : "I handed this to the general agent, but the turn didn't complete. "
                      "Anything it had already built is still in the part. Reason: " +
                          worker_err;
            events.emit("run_finished", json{{"status", status},
                                             {"stop_reason", stop},
                                             {"error_detail", detail}});
            return json{{"run_id", run_id},
                        {"status", status},
                        {"stop_reason", stop},
                        {"error_detail", detail},
                        {"error_human", human},
                        {"doc_id", doc_id}};
        };

        // Offline runs never attempt the worker bridge — same refusal as before.
        if (offline) return worker_failure_response(std::string());

        // Execute instead of refuse: hand the request to a bounded general_agent
        // role turn (fs/plm/shell + on-demand modeling/sketch/sim/... tool packs
        // via enable_tools) over the services worker's agent_admin_turn RPC
        // (services/agent/worker_rpc.py), threading the caller's resolved provider
        // descriptor through so the worker runs the provider the user picked. Any
        // failure to reach/complete it (worker unavailable, spawn failure, timeout,
        // provider auth, the worker's OWN offline check, ...) falls back to the blocked
        // response carrying that reason — never a crash, never a silent hang.
        json turn;
        std::string worker_err;
        bool worker_crashed = false;
        bool worker_timed_out = false;
        const std::string ws_dir = agent_workspace_dir(options);
        // Only `options` is reachable here: libchudware's agent_run handler splits the
        // RPC params into (request, doc_id, provider, options) and run_design never sees
        // the top-level payload, so a top-level `attachments` field must be relayed into
        // options by the caller to arrive.
        const std::string session_id = options.value("session_id", std::string());
        const std::string sessions_dir = options.value("sessions_dir", std::string());
        const json history = options.value("history", json::array());
        const json attachments = options.value("attachments", json::array());
        if (!call_admin_worker_turn(request, run_id, provider_cfg, ws_dir, session_id,
                                    sessions_dir, history, attachments,
                                    events, turn, worker_err, worker_crashed,
                                    worker_timed_out)) {
            if (worker_err == "cancelled" || run_ctl.cancelled()) {
                events.emit("run_finished", json{{"status", "cancelled"},
                                                 {"stop_reason", "cancelled"}});
                return json{{"run_id", run_id},
                            {"status", "cancelled"},
                            {"stop_reason", "cancelled"},
                            {"doc_id", doc_id}};
            }
            events.emit("run_note",
                        json{{"note", worker_crashed ? "agent worker process died mid-turn"
                              : worker_timed_out     ? "agent worker stopped responding mid-turn"
                                                     : "general-work worker turn unavailable"},
                             {"reason", worker_err}});
            return worker_failure_response(worker_err, worker_crashed, worker_timed_out);
        }

        const bool turn_ok = turn.value("success", false);
        const std::string stop_reason = turn.value("stop_reason", std::string("unknown"));
        const json turn_result = turn.value("result", json::object());
        const json handoff = turn_result.is_object()
                                 ? turn_result.value("handoff", json::object())
                                 : json::object();
        const std::string handoff_status =
            handoff.is_object() ? handoff.value("status", std::string()) : std::string();
        const std::string role = turn.value("role", std::string("general_agent"));

        // The general lane runs NO geometry pipeline: whatever happened, the
        // turn's own handoff is the only honest account of it. Hand it up so
        // the host can print it instead of the built-geometry boilerplate
        // (2026-07-27: a turn that created nothing still rendered "Done - I
        // built this as parametric features; see the FeatureManager tree").
        // Harvested BEFORE the status/error_detail derivation below, which both
        // fall back to it.
        std::string summary;
        if (turn_result.is_object()) {
            // Models write the finish text under several keys: the canonical
            // role envelope uses `summary`, kimi k3's {"done":true,"handoff":{…}}
            // dialect uses `response`. Read every known one, top level and
            // under `outputs`, or the chat bubble comes up blank.
            static const char* kTextKeys[] = {"summary", "response", "text",
                                              "message", "answer", "reply",
                                              "verdict"};
            auto harvest = [&](const json& obj) {
                if (!obj.is_object()) return;
                for (const char* key : kTextKeys) {
                    if (!summary.empty()) return;
                    const auto it = obj.find(key);
                    if (it != obj.end() && it->is_string() && !it->get<std::string>().empty())
                        summary = it->get<std::string>();
                }
            };
            harvest(handoff);
            if (handoff.is_object()) harvest(handoff.value("outputs", json::object()));
            harvest(turn_result);
        }

        // Both mappings live in run_stop.cpp behind run.h declarations so a test can
        // LINK the shipped rule instead of restating it (same reason as
        // classify_worker_stop above).
        const std::string status =
            general_lane_status(turn_ok, handoff_status, stop_reason);
        const std::string error_detail = general_lane_error_detail(
            turn_result.is_object() ? turn_result.value("error_detail", std::string())
                                    : std::string(),
            status, summary);
        json finished{{"status", status},
                      {"stop_reason", stop_reason},
                      {"role", role}};
        if (!error_detail.empty()) finished["error_detail"] = error_detail;
        events.emit("run_finished", std::move(finished));
        json out{{"run_id", run_id},
                 {"status", status},
                 {"stop_reason", stop_reason},
                 {"result", turn_result},
                 {"role", role},
                 {"lane", "general"},
                 {"summary", summary},
                 {"doc_id", doc_id}};
        if (!error_detail.empty()) out["error_detail"] = error_detail;
        return out;
    }

    // Live provider_call debug events (endpoint/status/latency) land on the same
    // emit stream as stage events so the host can orch_-prefix them into chat.
    set_provider_call_emitter([&events](const json& ev) {
        json data = ev;
        std::string kind = "provider_call";
        if (data.contains("kind") && data["kind"].is_string()) {
            kind = data["kind"].get<std::string>();
            data.erase("kind");
        }
        events.emit(kind, data);
    });
    struct ProviderEmitGuard {
        ~ProviderEmitGuard() { set_provider_call_emitter(nullptr); }
    } provider_emit_guard;
    // Live provider token streaming (shared DeltaCoalescer — see anonymous namespace).
    auto coalescer = std::make_shared<DeltaCoalescer>();
    coalescer->ev = &events;
    ProviderDeltaGuard provider_delta_guard{coalescer};

    // ONE provider instance for the whole run (per-role scripted queues must persist).
    ProviderFn provider = make_provider(provider_cfg);

    // T-H7: resolve consent early (same logic as post-run; consent_tier/load_config are
    // cheap and idempotent — we re-read at emit time too, but need it now for the wrapper).
    std::string run_consent = consent_tier(load_config());
    if (offline) run_consent = "off";  // offline hard-overrides to "off" (same as emit site)

    // Wrap with timing + sidecar logging (thin; never changes behaviour).
    // LoggingProviderState collects the most-recent sidecar paths so emit_ai_artifact
    // (below) can pass input_ref/output_ref instead of empty strings.
    chudware::agent::LoggingProviderState lp_state;
    provider = chudware::agent::wrap_logging(std::move(provider), run_id, run_consent, &lp_state);
    // Output-token budget + INPUT context cap, jointly fitted to the model's context window so a
    // small local window is never overrun (which would truncate the prompt and break the chain).
    const CallBudget budget = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int call_budget = budget.output;
    const int ctx_cap = budget.input_cap;
    // Whether the provider can receive screenshots (drives vision review/fix).
    const bool vision = provider_cfg.value("supports_vision", false);

    // Experimental: a SEPARATE provider for the vision review + fix stages (Seek-CAD split — a
    // strong/cloud planner emits ops while a local vision judge sees the render and self-corrects;
    // also enables an all-local ITAR review loop). Absent -> identical to the main provider, so the
    // behavior and the Python-parity gate are unchanged.
    ProviderFn review_provider = provider;
    bool review_vision = vision;
    int review_budget = call_budget;
    int review_ctx_cap = ctx_cap;
    bool review_provider_specialized = false;
    if (provider_cfg.contains("review_provider") && provider_cfg["review_provider"].is_object()) {
        const json& rpc = provider_cfg["review_provider"];
        review_provider = make_provider(rpc);
        review_vision = rpc.value("supports_vision", false);
        const CallBudget rb = fit_call_budget(rpc, call_budget_for(rpc));
        review_budget = rb.output;
        review_ctx_cap = rb.input_cap;
        review_provider_specialized = true;
        events.emit("run_note",
                    json{{"note", "experimental: separate vision-review provider"},
                         {"review_provider", rpc.value("name", std::string())},
                         {"review_model", rpc.value("model", std::string())}});
    }

    // Experimental: modeler prompt augmentation toward an editable parametric feature SEQUENCE
    // (DeepCAD/CAD-Coder-style design history) rather than a monolithic solid. Empty when off.
    const std::string cad_seq_directive =
        provider_cfg.value("cad_sequence_mode", false)
            ? std::string(
                  "[experimental: parametric sequence mode] Build the part as a minimal, ORDERED "
                  "sequence of individually editable features (sketch -> extrude/revolve/loft/sweep, "
                  "then fillet/chamfer/hole/pattern/mirror) that reconstructs a clean design history. "
                  "Avoid a single fused/imported solid; each feature must stay separately editable.")
            : std::string();

    // ---- PREWALK (strong plan → cheap implement) ----
    // An explicit prewalk_provider descriptor routes model/fix stages to a
    // separate provider. Planning stages (intake/plan) keep the main provider.
    // The first implementation stage emits a one-shot run_note. Without a
    // descriptor, every stage keeps the main provider.
    ProviderFn implement_provider = provider;
    int implement_budget = call_budget;
    int implement_ctx_cap = ctx_cap;
    bool implement_vision = vision;
    bool prewalk_armed = false;
    bool prewalk_switched = false;
    std::string prewalk_model_label;
    if (provider_cfg.contains("prewalk_provider") &&
        provider_cfg["prewalk_provider"].is_object()) {
        const json& pw_desc = provider_cfg["prewalk_provider"];
        if (!pw_desc.contains("error") && !pw_desc.empty()) {
            // Same provider+model as main → nothing to switch (disarm silently).
            const std::string main_name = provider_cfg.value("name", std::string());
            const std::string main_model =
                provider_cfg.contains("model") && provider_cfg["model"].is_string()
                    ? provider_cfg["model"].get<std::string>()
                    : std::string();
            const std::string pw_name = pw_desc.value("name", std::string());
            const std::string pw_model = pw_desc.contains("model") && pw_desc["model"].is_string()
                                            ? pw_desc["model"].get<std::string>()
                                            : std::string();
            if (!(pw_name == main_name && pw_model == main_model && !pw_model.empty())) {
                implement_provider = make_provider(pw_desc);
                implement_provider =
                    chudware::agent::wrap_logging(std::move(implement_provider), run_id, run_consent,
                                               &lp_state);
                const CallBudget ib = fit_call_budget(pw_desc, call_budget_for(pw_desc));
                implement_budget = ib.output;
                implement_ctx_cap = ib.input_cap;
                implement_vision = pw_desc.value("supports_vision", false);
                prewalk_armed = true;
                prewalk_model_label =
                    pw_model.empty() ? pw_name : (pw_name.empty() ? pw_model : pw_name + "/" + pw_model);
            }
        }
    }
    // Lazy one-shot notice on first implementation-stage call.
    auto use_implement_provider = [&]() -> const ProviderFn& {
        if (prewalk_armed && !prewalk_switched) {
            prewalk_switched = true;
            events.emit("run_note",
                        json{{"note", "Prewalk: switched to " + prewalk_model_label +
                                          " for implementation"},
                             {"text", "Prewalk: switched to " + prewalk_model_label +
                                          " for implementation"},
                             {"prewalk", true},
                             {"model", prewalk_model_label}});
        }
        return prewalk_armed ? implement_provider : provider;
    };

    // Seed the DesignState from the live session: doc_tree(doc_id) gives {revision, tree}.
    json tree0 = doc::doc_tree(doc_id);
    DesignState state;
    state.request = request;
    state.doc_id = doc_id;
    state.revision = tree0.value("revision", 0);
    // Complexity/domain classification + derived locals (rec_struct, execution_style,
    // solo_skip_plan) are computed here rather than near the top of run_design(): the
    // online top-level pre-flight branch above returns before ever reading them, so
    // computing them earlier was dead work on that path. Only the offline/sub-run/
    // swarm fall-through below reads these values.
    // Deterministic complexity router: a Solo request (one simple/standard part)
    // skips the LLM planner and runs as a single model loop; Standard/Complex keep
    // the plan stage. Honour an explicit override in options["complexity"].
    ComplexityVerdict cx = classify_complexity(request);
    DomainVerdict dom = classify_domain(request);
    if (options.contains("complexity") && options["complexity"].is_string()) {
        const std::string forced = options["complexity"].get<std::string>();
        if (forced == "solo") cx = {Complexity::Solo, "forced by options"};
        else if (forced == "standard") cx = {Complexity::Standard, "forced by options"};
        else if (forced == "complex") cx = {Complexity::Complex, "forced by options"};
    }
    // A Solo build skips the planner unless the host explicitly wants a plan
    // (plan_only / approved plan / swarm-forced structure).
    // Edit mode: same Solo default (no plan); Standard/Complex keep the plan stage.
    const bool solo_skip_plan =
        cx.level == Complexity::Solo && !plan_only && !has_approved_plan && !corporate_force;

    // execution_style: decide BEFORE drafting. recommend_structure → edit|swarm;
    // explicit mode swarm/eng_firm overrides. eng_firm is never auto-recommended.
    // Company structure is opt-in via options.corporate_mode / agent.corporate_mode
    // (Settings → AI → Behavior); default off so Agent never auto-invokes company.
    const bool cross_discipline =
        (dom.domain == Domain::Ecad || dom.domain == Domain::Cam ||
         dom.domain == Domain::Simulation);
    const bool allow_company = options.value("corporate_mode", false) ||
                               options.value("allow_company", false);
    ExecStructure rec_struct =
        recommend_structure(cx.level, cross_discipline, allow_company);
    std::string execution_style = execution_style_from_structure(structure_label(rec_struct));
    if (copilot_mode == "swarm" || execute_as == "swarm")
        execution_style = "swarm";
    else if (copilot_mode == "eng_firm" || execute_as == "eng_firm")
        execution_style = "eng_firm";
    else if (copilot_mode == "edit")
        execution_style = "edit";
    // options.execution_style explicit override (host resume / tests).
    if (options.contains("execution_style") && options["execution_style"].is_string()) {
        const std::string es = options["execution_style"].get<std::string>();
        if (es == "edit" || es == "swarm" || es == "eng_firm") execution_style = es;
    }

    state.complexity = cx.level;
    state.complexity_set = true;
    state.domain = dom.domain;
    state.domain_set = true;
    state.execution_style = execution_style;
    state.copilot_mode = copilot_mode;
    state.run_depth = run_depth;
    state.is_sub_run = is_sub_run;
    state.advisor_calls = 0;
    // Pre-seed domain cheatsheet classes so Sheetmetal (etc.) starts with sm_* ops
    // without burning a modeler turn on request_tools(["sheetmetal"]).
    if (dom.domain == Domain::Sheetmetal) state.tool_scope_classes.insert("sheetmetal");
    // Per-part notes (ai_notes) persisted in the .step+ by a prior session's memory stage:
    // load them so intake understands an EXISTING part on a follow-up run ("come back to the
    // part"). Empty object for a fresh doc or a doc that never earned notes (Solo one-offs).
    state.memory = ojson::object();
    try {
        json m = doc::doc_get_meta(state.doc_id, "ai_notes");
        if (m.is_object() && m.contains("value") && m["value"].is_object() && !m["value"].empty())
            state.memory = ojson::parse(m["value"].dump());
    } catch (...) {
    }
    state.update_tree(tree_to_ojson(tree0.contains("tree") ? tree0["tree"] : json::object()));

    // Seed an approved plan so the plan stage is skipped (host Plan-mode resume).
    if (has_approved_plan) state.plan = approved_plan;

    // Edit-mode scope directive: constrain modeler to listed feature ids.
    std::string edit_scope_directive;
    if (edit_scoped) {
        std::ostringstream ss;
        ss << "[EDIT SCOPE] Constrain changes to these feature id(s) only: ";
        for (size_t i = 0; i < scope_feature_ids.size(); ++i) {
            if (i) ss << ", ";
            ss << scope_feature_ids[i];
        }
        ss << ". Prefer edit_feature / delete_feature on those ids. Do NOT add unrelated "
              "features or mutate features outside this set unless the request explicitly "
              "requires a new supporting feature.";
        if (scope.contains("section") && scope["section"].is_string() &&
            !scope["section"].get<std::string>().empty())
            ss << " Section focus: " << scope["section"].get<std::string>() << ".";
        if (scope.contains("selection") && scope["selection"].is_array() &&
            !scope["selection"].empty())
            ss << " Selection: " << scope["selection"].dump() << ".";
        edit_scope_directive = ss.str();
    }
    // outer_run_started: assembly probe (or other outer orchestrator) already emitted
    // run_started + classification events — do not double-emit for the UI.
    const bool outer_run_started = options.value("outer_run_started", false);
    // Solo (trivial single part): intake → model → memory. The LLM plan stage
    // was already skipped; the LLM review + fix loop is the bulk of the
    // "bloated actions/thoughts" on a one-loop build, and no non-LLM review
    // exists — so those stages are omitted too (budgets below; the machine
    // skips budget-0 stages entirely). Standard/Complex keep the full path.
    json stages = solo_skip_plan
                      ? json::array({"intake", "model", "memory"})
                      : json::array({"intake", "plan", "model", "review", "fix", "memory"});
    if (!outer_run_started) {
        events.emit("run_started",
                    json{{"request", request}, {"doc_id", state.doc_id}, {"stages", stages}});
        // Pre-flight classification (GUI "Routing" card). Surfaced ONCE at entry, before any
        // provider call, so the user sees how the request was deterministically classified +
        // routed. Mirrors aicad.router/1 / the Python agent.classification producer so the
        // host renders the same card for the C++ live path (previously only complexity/
        // task_route were emitted, which the GUI does not draw as a routing card).
        // reason carries domain match terms AND complexity so the card shows why.
        {
            std::string why = dom.reason;
            if (!cx.reason.empty()) {
                if (!why.empty()) why += " · ";
                why += cx.reason;
            }
            const std::string cap_why = capability_request_reason(request);
            if (!cap_why.empty()) {
                if (!why.empty()) why += " · ";
                why += cap_why;
            }
            events.emit("agent.classification",
                        json{{"task_class", dom.label()},
                             {"scaffold_level", cx.label()},
                             {"tier", provider_cfg.value("model", std::string())},
                             {"reason", why},
                             {"request_snippet", request.substr(0, 120)},
                             {"picked_model", provider_cfg.value("model", std::string())},
                             {"picked_provider", provider_cfg.value("name", std::string())}});
        }
        // Complexity routing decision (GUI shows which structure was chosen + why).
        events.emit("complexity", json{{"level", cx.label()},
                                       {"reason", cx.reason},
                                       {"skip_plan", solo_skip_plan},
                                       {"structure", structure_label(rec_struct)},
                                       {"execution_style", execution_style}});
        // Task-domain routing: the scoped tool classes + on-topic context for this
        // request's domain, so the host can narrow the toolset (modeling task ->
        // modeling/sketch/sourcing, not CAM/ECAD/sim) instead of loading everything.
        {
            json tclasses = json::array();
            for (const auto& t : tool_classes_for_domain(dom.domain)) tclasses.push_back(t);
            json sheets = json::array();
            for (const auto& s : context_sheets_for_domain(dom.domain)) sheets.push_back(s);
            // structure field: recommended structure label; execution_style is the copilot mode stamp.
            const char* struct_field =
                solo_skip_plan ? "solo" : structure_label(rec_struct);
            events.emit("task_route", json{{"domain", dom.label()},
                                           {"reason", dom.reason},
                                           {"tool_classes", tclasses},
                                           {"context_sheets", sheets},
                                           {"complexity", cx.label()},
                                           {"structure", struct_field},
                                           {"execution_style", execution_style}});
        }
    }

    // ---- FILE-FIRST: persist the part to disk BEFORE modeling starts ----
    // The host historically saved only AFTER the run (its end-of-run auto-save
    // on part_named). For the built-in version control / PLM the file must
    // exist from the first feature so every milestone lands as a save state on
    // a real document. Create <workspace>/<name>.step+ now — named
    // deterministically from the request (the memory stage renames it to the
    // AI-chosen name at the end) — then re-save it at each successful
    // model/fix milestone below. Skipped for: sub-runs (assembly children are
    // saved by the parent loop), plan_only runs (no modeling), mock-provider
    // parity runs (no disk writes in the gate), and docs that already have a
    // path (edit follow-ups — never rewrite the user's existing file mid-run;
    // the in-memory doc_checkpoint save states below still apply). Never fails
    // the run.
    std::string file_first_path;
    const bool is_mock_provider = provider_cfg.value("name", std::string()) == "mock";
    if (!is_sub_run && !plan_only && !is_mock_provider && options.value("file_first", true)) {
        try {
            std::string existing_path;
            json dl = doc::doc_list();
            if (dl.is_object() && dl.contains("docs") && dl["docs"].is_array())
                for (const auto& d : dl["docs"])
                    if (d.is_object() && d.value("doc_id", std::string()) == doc_id) {
                        existing_path = d.value("path", std::string());
                        break;
                    }
            if (existing_path.empty()) {
                const std::string dir = agent_workspace_dir(options);
                if (!dir.empty()) {
                    file_first_path = unique_stepplus_path(
                        dir, stepplus_stem(fallback_part_name(request)));
                    doc::doc_save(doc_id, file_first_path);
                    events.emit("run_note", json{{"note", "file_created"},
                                                 {"path", file_first_path},
                                                 {"doc_id", doc_id}});
                }
            }
        } catch (...) {
            file_first_path.clear();  // persistence is best-effort; model anyway
        }
    }
    // Milestone save state: an in-memory doc_checkpoint (existing revert
    // mechanism — the same save-state primitive PLM rollback uses) after every
    // successful feature batch, plus a re-save of the file-first .step+ so the
    // on-disk file tracks modeling progress.
    auto milestone_save = [&](const char* stage_tag) {
        try {
            doc::doc_checkpoint(doc_id);
        } catch (...) {
        }
        if (file_first_path.empty()) return;
        try {
            doc::doc_save(doc_id, file_first_path);
            events.emit("run_note", json{{"note", "milestone_saved"},
                                         {"stage", stage_tag},
                                         {"path", file_first_path},
                                         {"doc_id", doc_id}});
        } catch (...) {
        }
    };

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
        o.thinking = ac.thinking;
        o.model = ac.model;
        o.sent = ac.sent_prompt;
    };

    // ---- request_advice peel handler (cap 3 / run) ----
    // Returns true when the caller should free-retry (advice injected for next turn).
    auto handle_request_advice = [&](DesignState& st, AdviceRequest& ar, StageOutcome& o,
                                     const char* stage_tag) -> bool {
        if (!ar.had_request) return false;
        constexpr int kAdvisorCap = 3;
        if (!advisor_on) {
            st.last_errors.push_back("request_advice ignored (advisor disabled)");
            return false;  // continue with remaining ops
        }
        if (st.advisor_calls >= kAdvisorCap) {
            events.emit("run_note",
                        json{{"note", "request_advice ignored: cap of 3 advisor calls reached"},
                             {"stage", stage_tag}});
            st.last_errors.push_back(
                "request_advice ignored: advisor call cap (3) reached for this run");
            return false;
        }
        if (ar.question.empty()) {
            st.last_errors.push_back("request_advice missing question");
            return false;
        }
        ++st.advisor_calls;
        // Build a focused advisor state: measured doc + stage + question.
        DesignState adv_st = st;
        adv_st.request = std::string("STAGE: ") + stage_tag + "\nQUESTION: " + ar.question;
        if (!ar.context.empty()) adv_st.request += "\nCONTEXT: " + ar.context;
        if (!st.tree_digest.empty())
            adv_st.request += "\nFEATURE TREE:\n" + st.tree_digest;
        if (!st.last_errors.empty()) {
            adv_st.request += "\nERRORS:";
            for (const auto& e : st.last_errors) adv_st.request += "\n- " + e;
        }
        AgentCall ac =
            agent_call(provider, adv_st, "advisor", prompts_dir, call_budget, false, ctx_cap);
        AdvisorAdvice advice = parse_advisor_advice(ac.reply);
        std::string advice_text = advice.ok ? advice.advice
                                            : (ac.has_call_error ? ("advisor error: " + ac.call_error)
                                                                : "advisor returned no advice");
        json adv_ev = json{{"kind", "advisor"},
                           {"stage", stage_tag},
                           {"question", ar.question},
                           {"advice", advice_text},
                           {"confidence", advice.confidence},
                           {"call", st.advisor_calls}};
        // Honest fallback stamp when the advisor call failed or was unparseable.
        if (ac.has_call_error)
            adv_ev["fallback"] = "provider_error";
        else if (!advice.ok)
            adv_ev["fallback"] = "unparseable";
        events.emit("advisor", adv_ev);
        // Inject into next turn via advisory seam.
        set_context_advisory(advice_text);
        // Pure advice peel (no remaining ops) → free retry so modeler re-prompts with advice.
        if (ar.remaining.empty() || (ar.remaining.is_array() && ar.remaining.empty())) {
            o.verdict = "retry";
            o.reason = "request_advice: injected";
            o.parsed_ok = true;
            o.free_retry = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            return true;
        }
        return false;  // mixed batch: advice injected; continue with remaining ops
    };

    // ---- delegate peel handler (edit/plan/agent; HARD-disabled Solo + sub-runs) ----
    // Returns true when the peel was consumed (either ran a nested design or refused).
    auto handle_delegate = [&](DesignState& st, DelegateRequest& dr, StageOutcome& o,
                               const char* stage_tag) -> bool {
        if (!dr.had_request) return false;
        auto refuse = [&](const std::string& why) {
            events.emit("run_note",
                        json{{"note", "delegate refused: " + why}, {"stage", stage_tag}});
            st.last_errors.push_back("delegate refused: " + why);
            if (dr.remaining.empty() || (dr.remaining.is_array() && dr.remaining.empty())) {
                o.verdict = "retry";
                o.reason = "delegate refused: " + why;
                o.parsed_ok = true;
                o.free_retry = false;
                o.has_ops_count = true;
                o.ops_count = 0;
                return true;
            }
            return false;  // mixed: note and continue with remaining
        };
        // Mode gate via the shared contract (parity with the Python path):
        // delegate is denied under ask, allowed under agent/plan/edit/swarm
        // (empty mode normalizes to agent). Legacy inline gate when the
        // contract file is unreadable.
        const bool mode_ok =
            copilot_modes
                ? copilot_tool_allowed(*copilot_modes, "delegate", copilot_mode)
                : (copilot_mode.empty() || copilot_mode == "edit" ||
                   copilot_mode == "plan" || copilot_mode == "agent");
        if (!mode_ok) return refuse("mode '" + copilot_mode + "' disallows delegate");
        if (st.complexity_set && st.complexity == Complexity::Solo)
            return refuse("Solo complexity cannot delegate");
        if (st.is_sub_run || st.run_depth >= 2)
            return refuse("delegate depth cap (2) exceeded");
        if (dr.task.empty()) return refuse("missing task");

        const std::string sub_id = run_id + "-d" + std::to_string(st.run_depth + 1);
        events.emit("sub_run", json{{"kind", "sub_run"},
                                    {"id", sub_id},
                                    {"purpose", dr.task},
                                    {"parent_stage", stage_tag},
                                    {"status", "running"}});
        json sub_opts = options.is_object() ? options : json::object();
        sub_opts["run_id"] = sub_id;
        sub_opts["run_depth"] = st.run_depth + 1;
        sub_opts["is_sub_run"] = true;
        sub_opts["outer_run_started"] = true;  // share parent's routing card
        sub_opts.erase("approved_plan");
        sub_opts.erase("plan_only");
        // Nested run on the SAME doc_id; share emit sink.
        json rr = run_design(dr.task, doc_id, provider_cfg, emit, sub_opts);
        // Refresh parent state from live doc after child mutates it.
        try {
            json tree_now = doc::doc_tree(doc_id);
            st.update_revision(tree_now.value("revision", st.revision));
            st.update_tree(
                tree_to_ojson(tree_now.contains("tree") ? tree_now["tree"] : json::object()));
        } catch (...) {
        }
        const std::string child_status = rr.value("status", std::string("done"));
        events.emit("sub_run", json{{"kind", "sub_run"},
                                    {"id", sub_id},
                                    {"purpose", dr.task},
                                    {"parent_stage", stage_tag},
                                    {"status", child_status == "done" ? "done" : child_status}});
        if (dr.remaining.empty() || (dr.remaining.is_array() && dr.remaining.empty())) {
            o.verdict = (child_status == "done" || child_status == "plan_ready") ? "pass" : "retry";
            o.reason = "delegate: " + child_status;
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            return true;
        }
        return false;  // mixed: child ran; continue with remaining ops
    };

    // Agent-decided plan advisor review (non-plan_only). Returns:
    //   "approve" | "revise" | "escalate". On revise, note is the revision instruction.
    // Emits advisor{stage:"plan"} with the verdict.
    auto advisor_review_plan = [&](DesignState& st, const ojson& plan,
                                   std::string& note_out) -> std::string {
        note_out.clear();
        if (!advisor_on) return "approve";
        // Build review prompt: plan + request + execution_style.
        DesignState adv_st = st;
        std::string plan_s = plan.is_null() ? "{}" : py_json_dumps_indent0(plan);
        adv_st.request =
            std::string("STAGE: plan\n") +
            "Review this BUILD PLAN for execution_style='" + execution_style + "'.\n" +
            "Return verdict approve|revise|escalate with a note.\n\n" +
            "REQUEST:\n" + request + "\n\nPLAN:\n" + plan_s;
        AgentCall ac =
            agent_call(provider, adv_st, "advisor", prompts_dir, call_budget, false, ctx_cap);
        AdvisorPlanVerdict v = parse_advisor_plan_verdict(ac.reply);
        // Provider hard-fail: still soft-approve, but stamp fallback honestly.
        if (ac.has_call_error) {
            v.verdict = "approve";
            v.note = "advisor provider error; continuing";
            v.fallback = "provider_error";
            v.ok = false;
        }
        note_out = v.note;
        json adv_ev = json{{"kind", "advisor"},
                           {"stage", "plan"},
                           {"question", "review build plan"},
                           {"advice", v.verdict + (v.note.empty() ? "" : (": " + v.note))},
                           {"verdict", v.verdict},
                           {"note", v.note},
                           {"confidence", v.confidence},
                           {"execution_style", execution_style}};
        if (!v.fallback.empty()) adv_ev["fallback"] = v.fallback;
        events.emit("advisor", adv_ev);
        return v.verdict;
    };

    // _apply(ops): doc_apply_ops at state.revision; update rev + tree; collect status errors.
    // On success (and on apply-with-status-errors) emit synthetic agent.tool_call Doing row
    // plus a live doc.preview so the host viewport can paint intermediate geometry.
    auto apply_ops = [&](DesignState& st, const json& ops) -> std::vector<std::string> {
        auto t0 = std::chrono::steady_clock::now();
        const int base_revision = st.revision;
        json resp = doc::doc_apply_ops(doc_id, base_revision, ops, /*coalesce=*/false);
        publish_doc_call(
            "doc_apply_ops",
            doc_publish_params({{"doc_id", doc_id}, {"base_revision", base_revision},
                                {"ops", ops}, {"coalesce", false}},
                               options),
            resp);
        auto t1 = std::chrono::steady_clock::now();
        int lat = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        st.update_revision(resp.value("revision", st.revision));
        st.update_tree(tree_to_ojson(resp.contains("tree") ? resp["tree"] : json::object()));
        json fs = json::array();
        if (resp.contains("rebuild") && resp["rebuild"].contains("feature_status"))
            fs = resp["rebuild"]["feature_status"];
        std::vector<std::string> errs = status_errors(fs);
        std::string errText;
        for (const auto& e : errs) {
            if (!errText.empty()) errText += "\n";
            errText += e;
        }
        emit_agent_tool_call(events, /*tool_name=*/"", /*target=*/"", ops,
                             /*ok=*/errs.empty(), lat, /*parent_id=*/"", /*actor=*/"modeler",
                             errText);
        std::string aicm_path;
        if (resp.contains("tess") && resp["tess"].is_object())
            aicm_path = resp["tess"].value("aicm_path", std::string());
        if (!aicm_path.empty())
            emit_doc_preview(events, doc_id, resp.value("revision", st.revision), aicm_path);
        return errs;
    };

    // ---- INTAKE ----
    // Reject protocol: model replies {"reject":true,"reason":"..."} for non-CAD /
    // unintelligible requests. Also belt-and-suspenders: empty/whitespace object
    // (or empty features+dims with blank object) after a "successful" parse is a reject.
    actions.intake = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "intake", prompts_dir, call_budget, vision, ctx_cap);
        ojson spec = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = ac.call_error;
            o.has_error = true;
            o.error = "intake: " + ac.call_error;
            return o;
        }
        auto reject_out = [&](const std::string& reason) -> StageOutcome {
            o.verdict = "fail";
            o.reason = reason;
            o.parsed_ok = true;
            o.has_error = true;
            o.error = "unintelligible_request";
            return o;
        };
        // Non-geometry capability (PLM / project + file organization / measure-only /
        // BOM). Terminal, but NOT "unintelligible" — the request is understood, it
        // just must not enter the modeling pipeline.
        auto handoff_out = [&](const std::string& why) -> StageOutcome {
            o.verdict = "fail";
            o.reason = why;
            o.parsed_ok = true;
            o.has_error = true;
            o.error = "non_modeling_request";
            return o;
        };
        if (!spec.is_null() && spec.is_object()) {
            // Explicit reject signal from the intake prompt protocol.
            if (spec.contains("reject") && truthy_json(spec["reject"])) {
                std::string reason = "request is not a CAD design request";
                if (spec.contains("reason") && spec["reason"].is_string() &&
                    !spec["reason"].get<std::string>().empty())
                    reason = spec["reason"].get<std::string>();
                // Capability-aware override: parts-library / McMaster / measure /
                // CAM / sim / ECAD / RE / drawing requests are in-product work even
                // when the LLM mislabels them "not a CAD design request". Accept
                // with a minimal capability spec so the pipeline keeps routing.
                // Only GEOMETRY-producing capabilities may override the reject and
                // continue into the modeling pipeline. Non-geometry capabilities are
                // real work the app supports, but not work this pipeline can do:
                // overriding them here is what made a "clean up the folders + put it in
                // PLM" request come back as a part full of extruded plates.
                if (capability_kind(st.request) == CapabilityKind::NonGeometry)
                    return handoff_out(capability_request_reason(st.request));
                if (is_capability_request(st.request)) {
                    const std::string cap_why = capability_request_reason(st.request);
                    ojson cap_spec = ojson::object();
                    cap_spec["object"] = "capability request";
                    cap_spec["key_dims"] = ojson::object();
                    cap_spec["features"] = ojson::array({cap_why.empty()
                                                            ? std::string("in-product capability")
                                                            : cap_why});
                    cap_spec["constraints"] = ojson::array();
                    cap_spec["capability"] = true;
                    if (!cap_why.empty()) cap_spec["capability_reason"] = cap_why;
                    // Preserve the model's reject reason for transparency.
                    if (!reason.empty()) cap_spec["intake_note"] =
                        std::string("model rejected but capability override: ") + reason;
                    st.spec = cap_spec;
                    o.verdict = "pass";
                    o.reason = cap_why.empty()
                                   ? std::string("capability request accepted (override reject)")
                                   : (std::string("capability request accepted: ") + cap_why);
                    o.parsed_ok = true;
                    o.has_error = false;
                    o.error.clear();
                    return o;
                }
                return reject_out(reason);
            }
            // Deterministic empty-normalized-request belt (model may "pass" with blanks).
            // Skip when the open doc already has geometry and the raw request is non-empty:
            // edit follow-ups ("chamfer the top edges") are valid against the open part and
            // the LLM reject path is the gate — not this belt.
            auto strip_ws = [](std::string s) {
                size_t a = s.find_first_not_of(" \t\r\n\f\v");
                if (a == std::string::npos) return std::string();
                size_t b = s.find_last_not_of(" \t\r\n\f\v");
                return s.substr(a, b - a + 1);
            };
            std::string object;
            if (spec.contains("object") && spec["object"].is_string())
                object = strip_ws(spec["object"].get<std::string>());
            bool has_dims = spec.contains("key_dims") && spec["key_dims"].is_object() &&
                            !spec["key_dims"].empty();
            bool has_feats = spec.contains("features") && spec["features"].is_array() &&
                             !spec["features"].empty();
            if (object.empty() && !has_dims && !has_feats) {
                const bool doc_has_geom =
                    !st.tree_digest.empty() && st.tree_digest.rfind("(empty", 0) != 0;
                const bool raw_nonempty =
                    st.request.find_first_not_of(" \t\r\n\f\v") != std::string::npos;
                if (is_capability_request(st.request)) {
                    // Capability asks often don't look like "object + dims" specs —
                    // synthesize a pass so they aren't rejected as empty.
                    const std::string cap_why = capability_request_reason(st.request);
                    ojson cap_spec = ojson::object();
                    cap_spec["object"] = object.empty() ? "capability request" : object;
                    cap_spec["key_dims"] = ojson::object();
                    cap_spec["features"] = ojson::array(
                        {cap_why.empty() ? std::string("in-product capability") : cap_why});
                    cap_spec["constraints"] = ojson::array();
                    cap_spec["capability"] = true;
                    if (!cap_why.empty()) cap_spec["capability_reason"] = cap_why;
                    st.spec = cap_spec;
                    o.verdict = "pass";
                    o.reason = cap_why.empty()
                                   ? std::string("capability request accepted (empty-spec belt)")
                                   : (std::string("capability request accepted: ") + cap_why);
                    o.parsed_ok = true;
                    return o;
                }
                if (!(doc_has_geom && raw_nonempty))
                    return reject_out("normalized request is empty");
            }
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
    // When approved_plan was seeded, skip the planner LLM call (host Plan-mode resume).
    // Agent-decided plans (non-plan_only) get an advisor approve/revise/escalate after draft.
    // revise → ONE revision pass then continue (no second review). escalate → needs_user_decision.
    actions.plan = [&](DesignState& st, int) -> StageOutcome {
        if (has_approved_plan && !st.plan.is_null()) {
            StageOutcome o;
            o.verdict = "pass";
            o.reason = "approved_plan (re-plan skipped)";
            o.parsed_ok = true;
            return o;
        }
        if (solo_skip_plan) {
            StageOutcome o;
            o.verdict = "pass";
            o.reason = "solo (planner skipped: " + cx.reason + ")";
            o.parsed_ok = true;
            return o;  // st.plan stays null -> plan_steps() -> single default step
        }
        // Tell the planner the chosen execution_style so the plan fits it.
        const std::string style_dir =
            "[execution_style: " + execution_style +
            "] Structure the plan for this style. "
            "swarm → multi-part units with clear deps; eng_firm → discipline-scoped jobs; "
            "edit → lean single-body feature sequence.";
        AgentCall ac = agent_call(provider, st, "planner", prompts_dir, call_budget, vision, ctx_cap,
                                  style_dir);
        ojson p = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = ac.call_error;
            o.has_error = true;
            o.error = "plan: " + ac.call_error;
            return o;
        }
        bool ok = !p.is_null() && p.contains("parts") && p["parts"].is_array() &&
                  !p["parts"].empty();
        if (!ok) {
            // Plan is optional: a failure DEGRADES (retry -> exhaust -> non-fatal).
            o.verdict = "retry";
            o.reason = "plan not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no plan json";
            return o;
        }
        st.plan = p;

        // Manual plan_only: no advisor gate — host shows the approve card.
        if (plan_only) {
            o.verdict = "pass";
            o.reason = "plan parsed (plan_only)";
            o.parsed_ok = true;
            return o;
        }

        // Agent-decided plan: advisor reviews. Sub-runs skip (parent already approved).
        if (st.is_sub_run || !advisor_on) {
            o.verdict = "pass";
            o.reason = "plan parsed";
            o.parsed_ok = true;
            return o;
        }

        std::string adv_note;
        std::string verdict = advisor_review_plan(st, st.plan, adv_note);
        if (verdict == "escalate") {
            o.verdict = "fail";
            o.reason = adv_note.empty() ? "advisor escalated plan to user" : adv_note;
            o.parsed_ok = true;
            o.has_error = true;
            o.error = "needs_user_decision";
            return o;
        }
        if (verdict == "revise") {
            // ONE revision pass with the note; then continue (no second review).
            const std::string rev_dir =
                style_dir + "\n\n[PLAN REVISION REQUIRED]\n" +
                (adv_note.empty() ? "Revise the plan for clarity and executability." : adv_note);
            AgentCall rac =
                agent_call(provider, st, "planner", prompts_dir, call_budget, vision, ctx_cap,
                           rev_dir);
            ojson rp = extract_json_obj(rac.reply);
            bool rok = !rp.is_null() && rp.contains("parts") && rp["parts"].is_array() &&
                       !rp["parts"].empty();
            if (rok) st.plan = rp;
            // Continue either way — do not block on a failed revision parse.
            o.verdict = "pass";
            o.reason = rok ? "plan revised per advisor" : "plan revision unparseable; keeping prior";
            o.parsed_ok = true;
            return o;
        }
        // approve (default)
        o.verdict = "pass";
        o.reason = "plan parsed (advisor approved)";
        o.parsed_ok = true;
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
        AgentCall ac =
            agent_call(use_implement_provider(), st, "modeler", prompts_dir, implement_budget,
                       implement_vision, implement_ctx_cap);
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
    // Combine experimental cad-sequence + edit-scope directives for the modeler body.
    auto modeler_directive = [&]() -> std::string {
        if (cad_seq_directive.empty()) return edit_scope_directive;
        if (edit_scope_directive.empty()) return cad_seq_directive;
        return cad_seq_directive + "\n\n" + edit_scope_directive;
    };
    // Soft-filter applied ops when edit scope is active: drop edit/delete targeting
    // features outside the allowlist (prompt is the primary constraint; this is belt).
    auto filter_ops_to_scope = [&](const json& ops) -> json {
        if (!edit_scoped || scope_feature_ids.empty()) return ops;
        std::set<std::string> allow(scope_feature_ids.begin(), scope_feature_ids.end());
        json out = json::array();
        for (const auto& op : ops) {
            if (!op.is_object()) {
                out.push_back(op);
                continue;
            }
            const std::string opn = op.value("op", std::string());
            if (opn == "edit_feature" || opn == "delete_feature" || opn == "suppress_feature" ||
                opn == "unsuppress_feature") {
                std::string id = op.value("id", std::string());
                if (id.empty() && op.contains("feature") && op["feature"].is_object())
                    id = op["feature"].value("id", std::string());
                if (!id.empty() && !allow.count(id)) continue;  // drop out-of-scope mutation
            }
            out.push_back(op);
        }
        return out;
    };
    // Handle request_tools meta-ops peeled from a modeler/fixer ops array.
    // Returns true when the caller should return `o` immediately (escalation
    // free-retry, or already-have-full feedback with no remaining ops).
    auto handle_tool_scope = [&](DesignState& st, ToolScopeRequest& tsr, StageOutcome& o,
                                 const char* stage_tag) -> bool {
        if (!tsr.had_request) return false;

        const bool had_full_already = st.tool_scope_full;
        bool asked_full = false;
        for (const std::string& c : tsr.requested)
            if (c == "full_ops") {
                asked_full = true;
                break;
            }

        std::vector<std::string> newly = grant_tool_scope(st, tsr.requested);

        // Build the granted snapshot for the event (full + domain classes).
        json granted = json::array();
        if (st.tool_scope_full) granted.push_back("full_ops");
        for (const std::string& c : st.tool_scope_classes)
            if (c != "full_ops") granted.push_back(c);

        if (!newly.empty()) {
            events.emit("tool_scope", json{{"granted", granted},
                                           {"newly", newly},
                                           {"stage", stage_tag}});
        }

        // Pure escalation (only request_tools, no real ops).
        if (tsr.remaining.empty()) {
            if (!newly.empty()) {
                // Fresh grant — free retry with the bigger sheet (does NOT burn stage budget).
                st.last_errors.clear();
                o.verdict = "retry";
                o.reason = "tool_scope: granted " + std::to_string(newly.size()) + " class(es)";
                o.parsed_ok = true;
                o.has_ops_count = true;
                o.ops_count = 0;
                o.free_retry = true;
                return true;
            }
            // No new classes. Second full_ops (or re-request of held domains) is a
            // no-op: strip, feed back, and BURN the attempt so this cannot loop.
            st.last_errors = {asked_full && had_full_already
                                  ? std::string(kAlreadyHaveFullOps)
                                  : std::string("requested tool classes already granted; "
                                                "emit the document ops now")};
            o.verdict = "retry";
            o.reason = asked_full && had_full_already ? "tool_scope: already have full_ops"
                                                      : "tool_scope: no new classes";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            o.free_retry = false;  // burn budget — prevents infinite request_tools loops
            o.has_error = true;
            o.error = std::string(stage_tag) + ": tool scope already granted";
            return true;
        }

        // Mixed batch: request_tools + real ops. Scope is upgraded for THIS dry-run
        // (and future turns); strip meta-ops and continue with remaining.
        // If they re-asked full_ops with no new grant, leave a soft note only when
        // remaining is empty (handled above); with remaining ops just proceed.
        return false;
    };

    // Solo + slim sheet: if validation fails on an unknown op/feature type the model
    // correctly guessed exists, auto-escalate to the full sheet for the retry.
    auto maybe_auto_escalate = [&](DesignState& st, const std::string& verr,
                                   StageOutcome& o, const char* stage_tag) -> bool {
        if (st.tool_scope_full) return false;
        if (!st.complexity_set || st.complexity != Complexity::Solo) return false;
        if (!looks_like_unknown_op_error(verr)) return false;
        st.tool_scope_full = true;
        st.tool_scope_classes.insert("full_ops");
        events.emit("tool_scope",
                    json{{"granted", json::array({"full_ops"})},
                         {"newly", json::array({"full_ops"})},
                         {"stage", stage_tag},
                         {"auto", true},
                         {"reason", verr}});
        st.last_errors = {"invalid ops: " + verr,
                          "auto-escalated to full op set after unknown-op validation failure; "
                          "retry with the full vocabulary"};
        o.verdict = "retry";
        o.reason = "tool_scope: auto-escalated on unknown op";
        o.parsed_ok = true;
        o.free_retry = true;
        o.has_error = true;
        o.error = std::string(stage_tag) + "-validate-auto-escalate: " + verr;
        return true;
    };

    actions.model = [&](DesignState& st, int attempt) -> StageOutcome {
        st.last_no_progress = false;
        if (attempt > kEscapeAfter) return model_escape(st);
        AgentCall ac =
            agent_call(use_implement_provider(), st, "modeler", prompts_dir, implement_budget,
                       implement_vision, implement_ctx_cap, modeler_directive());
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            // The provider itself failed (HTTP error, empty/truncated completion). Report the
            // real cause so the run doesn't masquerade it as a "no JSON ops" parse failure.
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.has_error = true;
            o.error = "model: " + ac.call_error;
            return o;
        }
        OpsResult ores = extract_ops(ac.reply);
        // grok-4.5 (and other CoT models) sometimes emit the ops array in the reasoning
        // stream, leaving the content channel empty or a bare `[]`. When content yields no
        // usable ops, harvest them from the reasoning channel (last non-empty ops array).
        if ((!ores.ops || ores.ops->empty()) && !ac.thinking.empty()) {
            OpsResult tres = extract_ops_last_nonempty(ac.thinking);
            if (tres.ops && !tres.ops->empty()) ores = std::move(tres);
        }
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            // Strengthen retry feedback: explicit format directive first, then the
            // underlying parse error (flows into build_context ERRORS block).
            st.last_errors = {std::string(kOpsParseFormatDirective), perr};
            o.verdict = "retry";
            o.reason = "no ops: " + perr;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "model: " + perr;
            return o;
        }
        json ops = filter_ops_to_scope(*ores.ops);
        // Meta-ops first: request_tools, request_advice, delegate (never reach doc_apply).
        ToolScopeRequest tsr = peel_request_tools(ops);
        if (handle_tool_scope(st, tsr, o, "model")) return o;
        ops = tsr.remaining;
        AdviceRequest ar = peel_request_advice(ops);
        if (handle_request_advice(st, ar, o, "model")) return o;
        ops = ar.remaining;
        DelegateRequest dr = peel_delegate(ops);
        if (handle_delegate(st, dr, o, "model")) return o;
        ops = dr.remaining;
        if (ops.empty()) {
            // Empty ops on a non-empty build step while the tree is still empty is NOT a
            // genuine "no ops needed" pass — the model bailed. Retry with feedback. Keep
            // true no-op passes for edit-requests on existing geometry.
            bool step_nonempty = false;
            {
                const std::string& cs = st.current_step;
                size_t a = cs.find_first_not_of(" \t\r\n\f\v");
                step_nonempty = (a != std::string::npos);
            }
            const bool empty_tree =
                st.tree_digest.empty() || st.tree_digest.rfind("(empty", 0) == 0;
            if (step_nonempty && empty_tree) {
                const std::string msg =
                    "you emitted an empty ops array but nothing has been built; emit the "
                    "ops for this step";
                st.last_errors = {msg};
                o.verdict = "retry";
                o.reason = msg;
                o.parsed_ok = true;
                o.has_ops_count = true;
                o.ops_count = 0;
                o.has_error = true;
                o.error = "model: " + msg;
                return o;
            }
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
            if (maybe_auto_escalate(st, verr, o, "model")) {
                o.has_ops_count = true;
                o.ops_count = static_cast<int>(ops.size());
                return o;
            }
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
        milestone_save("model");  // feature batch landed → checkpoint + re-save
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
        if (review_vision) {
            std::filesystem::path png =
                run_dir(run_id) / ("review_r" + std::to_string(st.revision) + ".png");
            json rr = doc::doc_render_png(doc_id, png.string());
            if (rr.value("ok", false)) st.last_render = png.string();
        }
        BuiltContext bc = build_context(st, "reviewer", prompts_dir, review_ctx_cap);
        StageOutcome o;
        o.prompt_file = bc.prompt_source;
        o.prompt_hash = prompt_hash(bc.system);  // reviewer system is NOT role-prefixed here,
        // mirroring run.py review() which hashes the bare build_context system (not "ROLE:..").
        o.context_chars = bc.context_chars;
        o.context_tokens_est = bc.context_tokens_est;
        o.inputs_digest = inputs_digest(st);
        // Feed DETERMINISTIC measured geometry into the review so it can CONFIRM the requested
        // dimensions instead of defaulting to done:false ("need to confirm across-flats is 16mm")
        // and burning a full ~70-90s model+review retry cycle on EVERY part. The engine already
        // holds the exact solid — measure it (fail-soft: text-only review if measurement throws).
        try {
            json bb = doc::doc_bbox(doc_id);
            json mp = doc::doc_eval_mass(doc_id, -1, 1.0);
            if (bb.contains("size") && bb["size"].is_array() && bb["size"].size() == 3) {
                char buf[320];
                std::snprintf(buf, sizeof(buf),
                    "\n\nMEASURED (exact, from the built solid): bounding box %.2f x %.2f x %.2f mm"
                    "; volume %.1f mm^3; %d body(ies). Confirm the requested dimensions against THESE"
                    " measured values — do NOT ask to verify what is already measured here.",
                    bb["size"][0].get<double>(), bb["size"][1].get<double>(),
                    bb["size"][2].get<double>(), mp.value("volume_mm3", 0.0),
                    mp.value("body_count", 0));
                bc.body += buf;
            }
        } catch (...) { /* fail-soft: text-only review */ }
        json review_parts = json::array({text_part(bc.body)});
        if (review_vision && st.last_render.is_string() && !st.last_render.get<std::string>().empty())
            review_parts.push_back(image_part(st.last_render.get<std::string>()));
        json messages = json::array({user_message(review_parts)});
        auto t0 = std::chrono::steady_clock::now();
        ProvReply rep = review_provider(bc.system, messages, review_budget);  // review-scaled budget
        auto t1 = std::chrono::steady_clock::now();
        o.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        o.thinking = rep.thinking;
        o.model = rep.model;
        std::string reply = rep.has_error ? std::string() : rep.text;
        o.raw = reply;
        o.sent = bc.system + "\n\n" + bc.body;
        if (o.sent.size() > 4000) {
            o.sent.resize(4000);
            o.sent += "\n\u2026[truncated]";
        }
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
        // Fix is an implementation stage: when prewalk is armed and there is no separate
        // vision-review provider, use the cheap implement provider (one-way switch notice).
        const bool use_prewalk_fix = prewalk_armed && !review_provider_specialized;
        AgentCall ac =
            use_prewalk_fix
                ? agent_call(use_implement_provider(), st, "fixer", prompts_dir, implement_budget,
                             implement_vision, implement_ctx_cap)
                : agent_call(review_provider, st, "fixer", prompts_dir, review_budget, review_vision,
                             review_ctx_cap);
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
        // Same reasoning-channel fallback as the model stage (grok emits fix ops in CoT).
        if ((!ores.ops || ores.ops->empty()) && !ac.thinking.empty()) {
            OpsResult tres = extract_ops_last_nonempty(ac.thinking);
            if (tres.ops && !tres.ops->empty()) ores = std::move(tres);
        }
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            st.last_errors = {std::string(kOpsParseFormatDirective), perr};
            o.verdict = "retry";
            o.reason = "fix: no ops (" + perr + ")";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "fix: " + perr;
            return o;
        }
        json ops = filter_ops_to_scope(*ores.ops);
        ToolScopeRequest tsr = peel_request_tools(ops);
        if (handle_tool_scope(st, tsr, o, "fix")) return o;
        ops = tsr.remaining;
        AdviceRequest ar = peel_request_advice(ops);
        if (handle_request_advice(st, ar, o, "fix")) return o;
        ops = ar.remaining;
        DelegateRequest dr = peel_delegate(ops);
        if (handle_delegate(st, dr, o, "fix")) return o;
        ops = dr.remaining;
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
            if (maybe_auto_escalate(st, verr, o, "fix")) {
                o.has_ops_count = true;
                o.ops_count = static_cast<int>(ops.size());
                // Keep fix-prefixed error text for the auto path's last_errors first line.
                if (!st.last_errors.empty())
                    st.last_errors[0] = "invalid fix ops: " + verr;
                return o;
            }
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
        milestone_save("fix");  // fix batch landed → checkpoint + re-save
        o.verdict = "pass";
        o.reason = "fix: applied " + std::to_string(ops.size()) + " ops";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = static_cast<int>(ops.size());
        return o;
    };

    // ---- MEMORY (task-classified) ----
    // ALWAYS name the part (cheap, drives the file name + viewport + Feature Manager). For
    // Standard/Complex tasks (multi-feature parts, assemblies, projects) ALSO record durable
    // per-part notes so a future session understands the part — persisted in the .step+ (doc
    // meta "ai_notes") and reloaded at run start above. Solo one-off parts are named only:
    // no notes, no wasted tokens (matches the solo_skip_plan gate).
    std::string captured_part_name;
    // Non-Solo tasks earn notes; a part that ALREADY has notes (loaded above) keeps them
    // refreshed even on a Solo-classified follow-up edit, so revisit context never goes stale.
    const bool want_notes = (cx.level != Complexity::Solo) || !state.memory.empty();
    actions.memory = [&, want_notes](DesignState& st, int) -> StageOutcome {
        const std::string directive =
            want_notes
                ? "INSTRUCTION: name the part AND record durable notes for a future session. "
                  "The name must carry no version, revision, or iteration token because revisions "
                  "are tracked in PLM. Reply with EXACTLY one json block: "
                  "{\"name\": \"<2-5 word name, no dimensions>\", "
                  "\"summary\": \"one sentence: what this part is\", "
                  "\"decisions\": [\"key committed dimensions / design decisions\"], "
                  "\"open_issues\": [\"unresolved problems, [] if none\"]}."
                : "INSTRUCTION: just name the part — no notes needed. The name must carry no "
                  "version, revision, or iteration token because revisions are tracked in PLM. "
                  "Reply with EXACTLY one json block: "
                  "{\"name\": \"<2-5 word name, no dimensions, no file extension>\"}.";
        AgentCall ac = agent_call(provider, st, "memory", prompts_dir, call_budget, vision, ctx_cap,
                                  directive);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        ojson parsed = extract_json_obj(ac.reply);
        std::string name;
        if (parsed.is_object() && parsed.contains("name") && parsed["name"].is_string())
            name = parsed["name"].get<std::string>();
        name = sanitize_part_name(name);
        if (name.empty()) name = fallback_part_name(st.request);
        // Apply in-engine so doc_tree / Feature Manager see the new display name.
        try {
            doc::doc_set_name(st.doc_id, name);
        } catch (...) {
            // Naming must never fail the run — keep going with the chosen label.
        }
        events.emit("part_named", json{{"name", name}, {"doc_id", st.doc_id}});
        captured_part_name = name;
        // Durable per-part notes only for non-Solo tasks — persisted in the part file so a
        // follow-up session reloads them (see run-start load). Never fails the run.
        if (want_notes && parsed.is_object()) {
            ojson notes = ojson::object();
            for (const char* k : {"summary", "decisions", "open_issues"})
                if (parsed.contains(k)) notes[k] = parsed[k];
            if (!notes.empty()) {
                notes["name"] = name;
                st.memory = notes;
                try {
                    doc::doc_set_meta(st.doc_id, "ai_notes", json::parse(notes.dump()));
                } catch (...) {
                }
            }
        }
        // File-first rename: the file was created before modeling under the
        // deterministic request-derived name; move it to the AI-chosen name
        // (save to the new path, drop the old file) so the workspace ends with
        // ONE part file carrying the final name + notes. Never fails the run.
        if (!file_first_path.empty()) {
            try {
                namespace fs = std::filesystem;
                const fs::path old_path(file_first_path);
                const std::string new_path =
                    unique_stepplus_path(old_path.parent_path().string(), stepplus_stem(name));
                if (new_path != file_first_path) {
                    doc::doc_save(st.doc_id, new_path);
                    std::error_code ec;
                    fs::remove(old_path, ec);
                    file_first_path = new_path;
                    events.emit("run_note", json{{"note", "file_renamed"},
                                                 {"path", file_first_path},
                                                 {"doc_id", st.doc_id}});
                } else {
                    doc::doc_save(st.doc_id, file_first_path);  // flush name + notes meta
                }
            } catch (...) {
            }
        }
        o.verdict = "pass";
        o.reason = (want_notes ? "named + notes: " : "named: ") + name;
        o.parsed_ok = !name.empty();
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
    const auto t_run0 = std::chrono::steady_clock::now();

    // plan_only: run intake + plan, emit plan/plan_state, STOP before model (no mutation).
    // approved_plan clears plan_only above so resume falls through to full run_machine.
    if (plan_only) {
        auto run_one_stage = [&](const std::string& stage, const Action& action,
                                 const std::string& role, int budget) -> StageOutcome {
            StageOutcome last;
            last.verdict = "fail";
            for (int attempt = 1; attempt <= budget; ++attempt) {
                state.ledger.bump(stage);
                events.emit("stage_started",
                            json{{"stage", stage},
                                 {"role", role},
                                 {"attempt", attempt},
                                 {"budget", budget}});
                last = action(state, attempt);
                events.emit("stage_result", json{{"stage", stage},
                                                 {"attempt", attempt},
                                                 {"verdict", last.verdict},
                                                 {"reason", last.reason}});
                if (last.verdict == "pass" || last.verdict == "skip" || last.verdict == "fail")
                    return last;
                // retry
            }
            return last;
        };
        json budgets = options.value("budgets", json::object());
        int intake_b = budgets.value("intake", 2);
        int plan_b = budgets.value("plan", 2);
        StageOutcome intake_o = run_one_stage("intake", actions.intake, "intake", intake_b);
        if (intake_o.verdict == "fail" && intake_o.has_error &&
            intake_o.error == "unintelligible_request") {
            const std::string detail =
                intake_o.reason.empty() ? "request is not a CAD design request" : intake_o.reason;
            events.emit("run_finished",
                        json{{"status", "failed"},
                             {"stop_reason", "unintelligible_request"},
                             {"error_detail", detail},
                             {"revision", state.revision}});
            return json{{"run_id", run_id},
                       {"status", "failed"},
                       {"stop_reason", "unintelligible_request"},
                       {"error_detail", detail},
                       {"error_human", detail},
                       {"revision", state.revision},
                       {"doc_id", state.doc_id}};
        }
        std::string plan_v =
            run_one_stage("plan", actions.plan, "planner", plan_b).verdict;

        json plan_j = nullptr;
        if (!state.plan.is_null() && state.plan.is_object()) plan_j = state.plan;

        // Surface plan to the copilot Plan section (same as machine post-plan emit).
        if (plan_j.is_object() && plan_j.contains("parts") && plan_j["parts"].is_array() &&
            !plan_j["parts"].empty()) {
            std::string title, summary;
            plan_display_meta(state.plan, title, summary);
            json part_items = plan_state_items_from_build_plan(plan_j, "pending");
            emit_plan_state(events, "build_plan", title, summary, /*revision=*/1, part_items);
            json obj_items = plan_state_items_from_objectives(plan_j);
            if (!obj_items.empty())
                emit_plan_state(events, "objectives", "Objectives", summary, /*revision=*/1,
                                obj_items);
            // Python parity: kind:"plan" for the Plan card consumers.
            events.emit("plan", json{{"plan", plan_j}, {"title", title}, {"summary", summary}});
        }

        json tree_now = doc::doc_tree(doc_id);
        int revision = tree_now.value("revision", state.revision);
        const bool have_plan =
            plan_j.is_object() && plan_j.contains("parts") && plan_j["parts"].is_array() &&
            !plan_j["parts"].empty();
        // Provider hard-fail on plan with nothing usable → failed; else plan_ready
        // (even a degraded/empty plan is returned so the host can still present UI).
        // Manual plan mode stop_reason is awaiting_plan_approval (host approve card);
        // legacy plan_only flag keeps "plan_only" for older callers.
        std::string status = "plan_ready";
        std::string stop = (copilot_mode == "plan") ? "awaiting_plan_approval" : "plan_only";
        if (plan_v == "fail" && !have_plan) {
            status = "failed";
            stop = "plan_failed";
        }
        events.emit("run_finished",
                    json{{"status", status},
                         {"stop_reason", stop},
                         {"revision", revision},
                         {"execution_style", execution_style}});
        json out = json{{"run_id", run_id},
                        {"status", status},
                        {"stop_reason", stop},
                        {"revision", revision},
                        {"doc_id", state.doc_id},
                        {"execution_style", execution_style}};
        if (have_plan) out["plan"] = plan_j;
        else if (!state.plan.is_null()) out["plan"] = state.plan;
        else out["plan"] = nullptr;
        if (!copilot_mode.empty()) out["mode"] = copilot_mode;
        (void)t_run0;
        return out;
    }
    // Per-stage budgets: caller options override; else complexity-derived defaults.
    // Solo: plan=0, review=0, fix=0 (machine omits all three stages entirely —
    // the modeler already self-corrects via dry-run validation on every
    // attempt, and no non-LLM review exists to keep). Standard: fix=4.
    // Complex: fix=6. Intake/model/review/memory keep kDefaultBudgets.
    json budgets = options.value("budgets", json::object());
    if (!budgets.is_object()) budgets = json::object();
    auto ensure_budget = [&](const char* key, int def) {
        if (!budgets.contains(key) ||
            !(budgets[key].is_number_integer() || budgets[key].is_number_unsigned()))
            budgets[key] = def;
    };
    if (solo_skip_plan) {
        ensure_budget("plan", 0);    // omit plan stage (no stub LLM call)
        ensure_budget("review", 0);  // omit the LLM review/fix loop on trivial parts
        ensure_budget("fix", 0);
    }
    int fix_default = 4;
    if (cx.level == Complexity::Solo) fix_default = 2;
    else if (cx.level == Complexity::Complex) fix_default = 6;
    if (!solo_skip_plan) ensure_budget("fix", fix_default);

    json result = run_machine(state, actions, events, budgets, run_ctl);

    // revision = session.revision (re-read live; matches run.py getattr(session,"revision")).
    json tree_now = doc::doc_tree(doc_id);
    int revision = tree_now.value("revision", state.revision);

    // T-H1 telemetry (contracts/telemetry.md): the design run's output is one AI
    // artifact; the run applies its ops to the live document as it goes, so a
    // completed run IS the apply — "applied" is inferred from completion
    // (buttonless v1; the GUI takes the result over the C-ABI bridge and has no
    // core/agent linkage, so the emission lives here — see work/questions/T-H1.md).
    {
        std::string consent = consent_tier(load_config());
        if (offline) consent = "off";  // options-level offline flag: same hard override
        const int64_t run_latency_ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t_run0).count();
        const json model_info = {{"provider", provider_cfg.value("name", std::string())},
                                 {"model", provider_cfg.value("model", std::string())},
                                 {"adapter", nullptr}};
        // One artifact per design run; sidecar refs stay empty at run granularity
        // (per-attempt raws are already in attempt_<stage>_<n>.txt via agent_output).
        emit_ai_artifact(events, /*artifact_id=*/run_id, /*insertion_point=*/"IP-1",
                         model_info, /*prompt_hash=*/"",
                         /*input_ref=*/lp_state.last_input_ref,
                         /*output_ref=*/lp_state.last_output_ref,
                         /*base_revision=*/tree0.value("revision", 0),
                         /*verifier_scores=*/json::object(), run_latency_ms, consent);
        if (result.value("status", std::string()) == "done")
            emit_ai_artifact_action(events, /*artifact_id=*/run_id, "applied", revision,
                                    /*edit_ops_ref=*/"", /*undo_within_ops=*/0,
                                    /*dwell_ms=*/0, /*note=*/"", consent);
    }

    {
        json fin = json{{"status", result["status"]},
                       {"stop_reason", result["stop_reason"]},
                       {"revision", revision},
                       {"execution_style", execution_style}};
        // Surface provider HTTP detail on the phase card (never "budget exhausted").
        if (result.contains("error_detail") && result["error_detail"].is_string() &&
            !result["error_detail"].get<std::string>().empty())
            fin["error_detail"] = result["error_detail"];
        if (result.contains("error_human") && result["error_human"].is_string() &&
            !result["error_human"].get<std::string>().empty())
            fin["error_human"] = result["error_human"];
        if (result.contains("reason") && result["reason"].is_string())
            fin["reason"] = result["reason"];
        events.emit("run_finished", fin);
    }

    json out = json{{"run_id", run_id},
                    {"status", result["status"]},
                    {"stop_reason", result["stop_reason"]},
                    {"revision", revision},
                    {"doc_id", state.doc_id},
                    {"execution_style", execution_style}};
    if (!captured_part_name.empty()) out["part_name"] = captured_part_name;
    if (!file_first_path.empty()) out["saved_path"] = file_first_path;
    if (!copilot_mode.empty()) out["mode"] = copilot_mode;
    if (result.contains("error_detail") && result["error_detail"].is_string() &&
        !result["error_detail"].get<std::string>().empty())
        out["error_detail"] = result["error_detail"];
    if (result.contains("error_human") && result["error_human"].is_string() &&
        !result["error_human"].get<std::string>().empty())
        out["error_human"] = result["error_human"];
    // Advisor escalate / plan_ready from machine — forward plan + reason.
    if (result.contains("plan")) out["plan"] = result["plan"];
    if (result.contains("reason") && result["reason"].is_string())
        out["reason"] = result["reason"];
    return out;
}

json run_assembly_design(const std::string& request, const std::string& doc_id,
                         const json& provider_cfg, EmitFn emit, const json& options) {
    std::string prompts_dir = options.value("prompts_dir", std::string());
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "asm-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));

    // Plan gate / approved resume: always use the single-doc run_design path so
    // plan_only stops once (plan_ready) and approved_plan skips re-planning. Multi-
    // part assembly builds would otherwise re-plan and mutate child docs.
    {
        bool plan_only = options.value("plan_only", false) ||
                         options.value("mode", std::string()) == "plan";
        bool has_ap = options.contains("approved_plan") && !options["approved_plan"].is_null() &&
                      !(options["approved_plan"].is_string() &&
                        options["approved_plan"].get<std::string>().empty());
        if (has_ap) plan_only = false;
        if (plan_only || has_ap)
            return run_design(request, doc_id, provider_cfg, emit, options);
    }

    // Online top-level runs never need the probe: run_design's PRE-FLIGHT (see the
    // `!is_sub_run && (!offline || is_general_work_request(request))` gate below its
    // offline check) hands EVERY non-sub-run online request to the general_agent turn
    // over agent_admin_turn, where the MODEL decides whether to mission_start a
    // multi-part build. So the intake+planner probe's want_asm verdict can only ever
    // route somewhere the general lane already reaches on its own — while costing two
    // extra billed completions, ~20s of dead air, and a red stage card whenever the
    // planner call fails (see the "probe plan: provider call failed" emit further
    // down). Live evidence: run asm-152734881473315 (2026-07-28) painted exactly that
    // red card and then routed to general_agent anyway. Short-circuit BEFORE
    // classify_complexity so no classification work is done for a run we are about to
    // hand off. Derived from the SAME options run_design reads so the two cannot drift;
    // the probe below stays alive for the paths that still need it — offline runs and
    // sub-runs (a parent-authored delegated build step), plus explicit mission mode
    // which never reaches here.
    {
        // Mirror run_design: nested sub-run depth (delegate / swarm unit).
        const int run_depth = options.value("run_depth", 0);
        const bool is_sub_run = options.value("is_sub_run", false) || run_depth > 0;
        // Mirror run_design: explicit options.offline flag (run.cpp:943).
        const bool offline = options.value("offline", false);
        if (!is_sub_run && !offline)
            return run_design(request, doc_id, provider_cfg, emit, options);
    }

    // Free classify first — skip the expensive intake+planner probe when the
    // request is confidently single-part (not assembly-plausible). Err toward
    // probing when unsure (Complex, Domain::Assembly, or multi-part cues).
    ComplexityVerdict cx = classify_complexity(request);
    DomainVerdict dom = classify_domain(request);
    if (options.contains("complexity") && options["complexity"].is_string()) {
        const std::string forced = options["complexity"].get<std::string>();
        if (forced == "solo") cx = {Complexity::Solo, "forced by options"};
        else if (forced == "standard") cx = {Complexity::Standard, "forced by options"};
        else if (forced == "complex") cx = {Complexity::Complex, "forced by options"};
    }
    const bool assembly_plausible =
        cx.level == Complexity::Complex || dom.domain == Domain::Assembly;
    const bool cross_discipline =
        (dom.domain == Domain::Ecad || dom.domain == Domain::Cam ||
         dom.domain == Domain::Simulation);
    const bool allow_company = options.value("corporate_mode", false) ||
                               options.value("allow_company", false);
    ExecStructure rec_struct =
        recommend_structure(cx.level, cross_discipline, allow_company);
    std::string execution_style = execution_style_from_structure(structure_label(rec_struct));
    {
        const std::string mode = options.value("mode", std::string());
        const std::string execute_as = options.value("execute_as", std::string());
        if (mode == "swarm" || execute_as == "swarm") execution_style = "swarm";
        else if (mode == "eng_firm" || execute_as == "eng_firm") execution_style = "eng_firm";
        else if (mode == "edit") execution_style = "edit";
        if (options.contains("execution_style") && options["execution_style"].is_string()) {
            const std::string es = options["execution_style"].get<std::string>();
            if (es == "edit" || es == "swarm" || es == "eng_firm") execution_style = es;
        }
    }
    if (!assembly_plausible) {
        // Confident single-part (Solo/Standard + non-Assembly domain): no probe.
        return run_design(request, doc_id, provider_cfg, emit, options);
    }

    // Probe path: emit routing events BEFORE the two LLM calls so the UI is not
    // silent for ~20s. Classification is free (already computed above).
    RunEvents events(run_id, emit);
    // Cooperative cancel for the assembly ORCHESTRATION itself (the intake/planner probe LLM
    // calls and the per-part loop run on THIS thread, not inside a registered sub-run). Without
    // registering here, agent_cancel(run_id) found nothing in the registry and Stop was a no-op
    // during the probe/plan phase — the run kept building (engine_inflight stuck at 1). Mirror
    // run_design / run_mission: register the run so cancel_run flips the flag, set it as the
    // provider cancel flag so in-flight HTTP aborts, and check cancelled() at stage boundaries.
    // Per-part sub-runs (run_design below) still register their own children; cancel_run cascades
    // to "<run_id>-*", and run_design's save/restore hands this flag back after each sub-run.
    RunControl run_ctl;
    register_run_control(run_id, run_ctl);
    struct RunCtlGuard {
        std::string id;
        ~RunCtlGuard() { unregister_run_control(id); }
    } run_ctl_guard{run_id};
    const std::atomic<bool>* prev_cancel_flag = provider_cancel_flag();
    set_provider_cancel_flag(run_ctl.flag.get());
    if (prev_cancel_flag && prev_cancel_flag->load(std::memory_order_acquire))
        run_ctl.request_cancel();
    struct ProvCancelGuard {
        const std::atomic<bool>* prev;
        ~ProvCancelGuard() { set_provider_cancel_flag(prev); }
    } prov_cancel_guard{prev_cancel_flag};
    events.emit("run_started",
                json{{"request", request},
                     {"doc_id", doc_id},
                     {"stages", json::array({"intake", "plan", "model", "review", "fix", "memory"})},
                     {"assembly_probe", true}});
    events.emit("agent.classification",
                json{{"task_class", dom.label()},
                     {"scaffold_level", cx.label()},
                     {"tier", provider_cfg.value("model", std::string())},
                     {"reason", dom.reason.empty() ? cx.reason : dom.reason},
                     {"request_snippet", request.substr(0, 120)},
                     {"picked_model", provider_cfg.value("model", std::string())},
                     {"picked_provider", provider_cfg.value("name", std::string())}});
    events.emit("complexity", json{{"level", cx.label()},
                                   {"reason", cx.reason},
                                   {"skip_plan", false},
                                   {"assembly_probe", true},
                                   {"structure", structure_label(rec_struct)},
                                   {"execution_style", execution_style}});
    {
        json tclasses = json::array();
        for (const auto& t : tool_classes_for_domain(dom.domain)) tclasses.push_back(t);
        json sheets = json::array();
        for (const auto& s : context_sheets_for_domain(dom.domain)) sheets.push_back(s);
        events.emit("task_route", json{{"domain", dom.label()},
                                       {"reason", dom.reason},
                                       {"tool_classes", tclasses},
                                       {"context_sheets", sheets},
                                       {"complexity", cx.label()},
                                       {"structure", structure_label(rec_struct)},
                                       {"execution_style", execution_style}});
    }

    // Live provider token streaming for the probe intake/planner calls (the ~20s
    // of planning the user watches before the first build event).
    auto probe_coalescer = std::make_shared<DeltaCoalescer>();
    probe_coalescer->ev = &events;
    ProviderDeltaGuard probe_delta_guard{probe_coalescer};

    // 1) Probe: intake + planner to learn parts + whether it's an assembly.
    ProviderFn provider = make_provider(provider_cfg);
    const CallBudget cb = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int budget = cb.output;
    const int ctx_cap = cb.input_cap;
    DesignState pstate;
    pstate.request = request;
    pstate.doc_id = doc_id;
    pstate.complexity = cx.level;
    pstate.complexity_set = true;
    pstate.domain = dom.domain;
    pstate.domain_set = true;
    if (dom.domain == Domain::Sheetmetal) pstate.tool_scope_classes.insert("sheetmetal");
    // Seed live doc state so probe intake sees existing geometry (same as run_design).
    // Without this, edit follow-ups on a finished part look object-less and get rejected.
    try {
        json tree0 = doc::doc_tree(doc_id);
        pstate.revision = tree0.value("revision", 0);
        pstate.update_tree(
            tree_to_ojson(tree0.contains("tree") ? tree0["tree"] : json::object()));
    } catch (...) {
    }

    events.emit("stage_started",
                json{{"stage", "intake"}, {"role", "intake"}, {"attempt", 1}, {"budget", 1},
                     {"probe", true}});
    {
        AgentCall ia = agent_call(provider, pstate, "intake", prompts_dir, budget, false, ctx_cap);
        ojson spec = extract_json_obj(ia.reply);
        // BUG-A reject: abort before planner / run_design — unless the request maps
        // to a real in-product capability (parts library, measure, CAM, …).
        if (!spec.is_null() && spec.is_object() && spec.contains("reject") &&
            truthy_json(spec["reject"])) {
            std::string reason = "request is not a CAD design request";
            if (spec.contains("reason") && spec["reason"].is_string() &&
                !spec["reason"].get<std::string>().empty())
                reason = spec["reason"].get<std::string>();
            // Non-geometry capability (PLM / file + project organization / measure /
            // BOM): understood, but the modeling pipeline must not run. Terminate with
            // a handoff instead of synthesizing a spec and building plates.
            if (capability_kind(request) == CapabilityKind::NonGeometry) {
                const std::string why = capability_request_reason(request);
                const std::string human =
                    "That's project/file management, not a modeling task — I build "
                    "geometry, so running it here would have produced a part full of "
                    "meaningless features. Nothing was built and nothing was moved.\n\n"
                    "Use the PLM tab for version control, releases and deprecating "
                    "parts, and the file explorer for reorganizing folders. (" +
                    why + ")";
                events.emit("stage_result", json{{"stage", "intake"},
                                                 {"attempt", 1},
                                                 {"verdict", "fail"},
                                                 {"reason", why},
                                                 {"stop_reason", "non_modeling_request"}});
                events.emit("run_finished", json{{"status", "blocked"},
                                                 {"stop_reason", "non_modeling_request"},
                                                 {"error_detail", why},
                                                 {"revision", pstate.revision}});
                return json{{"run_id", run_id},
                            {"status", "blocked"},
                            {"stop_reason", "non_modeling_request"},
                            {"error_detail", why},
                            {"error_human", human},
                            {"revision", pstate.revision},
                            {"doc_id", doc_id}};
            }
            if (is_capability_request(request)) {
                const std::string cap_why = capability_request_reason(request);
                ojson cap_spec = ojson::object();
                cap_spec["object"] = "capability request";
                cap_spec["key_dims"] = ojson::object();
                cap_spec["features"] = ojson::array(
                    {cap_why.empty() ? std::string("in-product capability") : cap_why});
                cap_spec["constraints"] = ojson::array();
                cap_spec["capability"] = true;
                if (!cap_why.empty()) cap_spec["capability_reason"] = cap_why;
                pstate.spec = cap_spec;
                events.emit("stage_result",
                            json{{"stage", "intake"},
                                 {"attempt", 1},
                                 {"verdict", "pass"},
                                 {"reason", cap_why.empty()
                                                ? "capability request accepted (override reject)"
                                                : ("capability request accepted: " + cap_why)}});
            } else {
                events.emit("stage_result",
                            json{{"stage", "intake"},
                                 {"attempt", 1},
                                 {"verdict", "fail"},
                                 {"reason", reason},
                                 {"stop_reason", "unintelligible_request"}});
                events.emit("run_finished",
                            json{{"status", "failed"},
                                 {"stop_reason", "unintelligible_request"},
                                 {"error_detail", reason},
                                 {"revision", pstate.revision}});
                return json{{"run_id", run_id},
                           {"status", "failed"},
                           {"stop_reason", "unintelligible_request"},
                           {"error_detail", reason},
                           {"error_human", reason},
                           {"revision", pstate.revision},
                           {"doc_id", doc_id}};
            }
        } else {
            if (!spec.is_null()) pstate.spec = spec;
            events.emit("stage_result",
                        json{{"stage", "intake"},
                             {"attempt", 1},
                             {"verdict", "pass"},
                             {"reason", "probe intake"}});
        }
    }
    events.emit("stage_started",
                json{{"stage", "plan"}, {"role", "planner"}, {"attempt", 1}, {"budget", 1},
                     {"probe", true}});
    AgentCall pa = agent_call(provider, pstate, "planner", prompts_dir, budget, false, ctx_cap);
    ojson plan = extract_json_obj(pa.reply);
    // The probe is ADVISORY: no parseable plan just means the single-doc path
    // below (want_asm stays false). Only a real call failure is a stage fail —
    // anything else paints a false-alarm red card on a normal single-part run.
    const bool plan_ok = !plan.is_null() && plan.is_object();
    const bool plan_call_failed = pa.has_call_error || pa.reply.empty();
    events.emit("stage_result",
                json{{"stage", "plan"},
                     {"attempt", 1},
                     {"verdict", (!plan_ok && plan_call_failed) ? "fail" : "pass"},
                     {"reason", plan_ok ? std::string("probe plan")
                                        : (plan_call_failed
                                               ? std::string("probe plan: provider call failed")
                                               : std::string("no assembly plan (single-part path)"))}});
    bool want_asm = !plan.is_null() && plan.is_object() && truthy_json(plan.value("assembly", ojson(false)));
    ojson parts = (!plan.is_null() && plan.is_object() && plan.contains("parts") &&
                   plan["parts"].is_array())
                      ? plan["parts"]
                      : ojson::array();

    // Not an assembly (or <2 parts): single-doc path. Flag outer_run_started so
    // run_design does not re-emit run_started/classification (already sent above).
    if (!want_asm || parts.size() < 2) {
        json inner = options.is_object() ? options : json::object();
        inner["outer_run_started"] = true;
        if (!inner.contains("run_id") || !inner["run_id"].is_string() ||
            inner["run_id"].get<std::string>().empty())
            inner["run_id"] = run_id;
        return run_design(request, doc_id, provider_cfg, emit, inner);
    }

    // True multi-part assembly — extend the already-emitted run with assembly meta.
    events.emit("run_note", json{{"note", "assembly confirmed"},
                                {"assembly", true},
                                {"part_count", static_cast<int>(parts.size())}});

    // Normalize every planner-authored part title before it enters interface
    // lookup maps, doc_new payloads, emitted names, or persistence paths.
    std::vector<std::string> part_names;
    part_names.reserve(parts.size());
    for (size_t pi = 0; pi < parts.size(); ++pi) {
        const auto& p = parts[pi];
        const std::string raw_name =
            (p.contains("name") && p["name"].is_string())
                ? p["name"].get<std::string>()
                : std::string();
        part_names.push_back(
            mission_mint_name(raw_name, "part" + std::to_string(pi + 1)));
    }

    // Interface ledger from the planner plan (interfaces:[{between,kind,seed,holes,...}]).
    // Drives build order (seed before dependent), advisory injection, and pre-assemble verify.
    json plan_j_for_iface = plan.is_null() ? json::object() : json::parse(plan.dump());
    if (plan_j_for_iface.contains("parts") && plan_j_for_iface["parts"].is_array()) {
        for (size_t pi = 0; pi < plan_j_for_iface["parts"].size() &&
                            pi < part_names.size();
             ++pi) {
            if (plan_j_for_iface["parts"][pi].is_object())
                plan_j_for_iface["parts"][pi]["name"] = part_names[pi];
        }
    }
    if (plan_j_for_iface.contains("interfaces") &&
        plan_j_for_iface["interfaces"].is_array()) {
        for (auto& iface : plan_j_for_iface["interfaces"]) {
            if (!iface.is_object()) continue;
            if (iface.contains("seed") && iface["seed"].is_string())
                iface["seed"] =
                    mission_mint_name(iface["seed"].get<std::string>(), "Part");
            if (iface.contains("between") && iface["between"].is_array()) {
                for (auto& participant : iface["between"])
                    if (participant.is_string())
                        participant =
                            mission_mint_name(participant.get<std::string>(), "Part");
            }
        }
    }
    InterfaceLedger iface_ledger = parse_interface_ledger(plan_j_for_iface);
    if (!iface_ledger.interfaces.empty()) {
        json iface_arr = json::array();
        for (const auto& is : iface_ledger.interfaces) {
            json row = json{{"id", is.id},
                           {"kind", is.kind},
                           {"seed", is.seed},
                           {"between", is.between},
                           {"summary", is.raw_summary}};
            if (!is.params.empty()) row["params"] = is.params;
            iface_arr.push_back(std::move(row));
        }
        events.emit("run_note", json{{"note", "interface_ledger"},
                                    {"interfaces", iface_arr},
                                    {"count", static_cast<int>(iface_ledger.interfaces.size())}});
    }

    // Existing-assembly follow-up: seed the measured map from the open doc when it already
    // has features (the already-built parts are seeds; new mating parts consume their dims).
    std::map<std::string, MeasuredInterface> built_ifaces;
    try {
        json tree0 = doc::doc_tree(doc_id);
        json tree_body = tree0.contains("tree") ? tree0["tree"] : json::object();
        bool has_feats = tree_body.is_object() && tree_body.contains("features") &&
                         tree_body["features"].is_array() && !tree_body["features"].empty();
        if (has_feats) {
            json bb = json::object();
            try {
                bb = doc::doc_bbox(doc_id);
            } catch (...) {
            }
            std::string existing_name = "existing";
            try {
                // Prefer the live doc's display name when present on the tree payload.
                if (tree0.contains("name") && tree0["name"].is_string())
                    existing_name = tree0["name"].get<std::string>();
            } catch (...) {
            }
            existing_name = mission_mint_name(existing_name, "existing");
            MeasuredInterface em =
                measure_interface_from_tree(existing_name, tree_body, bb);
            em.doc_id = doc_id;
            built_ifaces[existing_name] = em;
            // Also alias under each plan part name that is NOT about to be rebuilt when
            // the request looks like an add-to-assembly (single new part). Heuristic: if
            // planner listed a part matching the existing doc name, bind it.
            for (size_t pi = 0; pi < parts.size() && pi < part_names.size(); ++pi) {
                const std::string& pn = part_names[pi];
                if (iface_norm_name(pn) == iface_norm_name(existing_name))
                    built_ifaces[pn] = em;
            }
            events.emit("run_note",
                        json{{"note", "existing_assembly_seed_measured"},
                             {"part", existing_name},
                             {"summary", em.summary}});
        }
    } catch (...) {
    }

    // 2) Build each part in its OWN child doc (full run_design), then save it to a .step+ file.
    // Order by interface deps: SEED parts first so dependents receive measured mate geometry.
    struct SavedPart {
        std::string path, name, doc_id;
        double pos[3];
        double rot[3];
        size_t plan_index = 0;
    };
    std::vector<size_t> build_order = order_parts_by_interfaces(part_names, iface_ledger);
    if (build_order.size() != part_names.size()) {
        build_order.clear();
        for (size_t i = 0; i < part_names.size(); ++i) build_order.push_back(i);
    }
    {
        json order_j = json::array();
        for (size_t oi : build_order) order_j.push_back(part_names[oi]);
        events.emit("run_note", json{{"note", "interface_build_order"}, {"order", order_j}});
    }

    std::vector<SavedPart> saved;
    // Track child docs by part name for mismatch re-fix.
    std::map<std::string, std::string> part_doc_ids;
    std::map<std::string, std::string> part_reqs;

    int built_count = 0;
    bool any_child_failed = false;
    bool child_cancelled = false;
    std::string first_child_failure;
    auto record_child_failure = [&](const std::string& reason) {
        any_child_failed = true;
        if (first_child_failure.empty())
            first_child_failure = reason.empty() ? "child_failed" : reason;
    };
    for (size_t oi = 0; oi < build_order.size(); ++oi) {
        // User Stop mid-build: don't start another part. In-flight sub-run HTTP already
        // aborted via the cascaded cancel flag; just stop orchestrating.
        if (run_ctl.cancelled()) break;
        const size_t idx = build_order[oi];
        const auto& p = parts[idx];
        std::string name = part_names[idx];
        std::string purpose = (p.contains("purpose") && p["purpose"].is_string())
                                  ? p["purpose"].get<std::string>()
                                  : "";
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
        // Placement: position (mm) + rotation (Euler degrees about X, then Y, then Z).
        double pos[3] = {0, 0, 0};
        double rot[3] = {0, 0, 0};
        if (p.contains("placement") && p["placement"].is_object()) {
            const json& pl = p["placement"];
            if (pl.contains("pos") && pl["pos"].is_array() && pl["pos"].size() >= 3)
                for (int k = 0; k < 3; ++k)
                    if (pl["pos"][k].is_number()) pos[k] = pl["pos"][k].get<double>();
            if (pl.contains("rot") && pl["rot"].is_array() && pl["rot"].size() >= 3)
                for (int k = 0; k < 3; ++k)
                    if (pl["rot"][k].is_number()) rot[k] = pl["rot"][k].get<double>();
        }

        events.emit("part_started", json{{"index", static_cast<int>(built_count)},
                                         {"plan_index", static_cast<int>(idx)},
                                         {"total", static_cast<int>(parts.size())},
                                         {"name", name}});

        std::string part_req =
            "Build ONLY this single standalone part, centered at the origin (it "
            "will be positioned in an assembly afterward). Part: '" +
            name + "' — " + purpose + ". Dimensions: " + dim_s + ". Features: " + feats;
        part_reqs[name] = part_req;

        // Interface advisory: measured mate if built, else binding seed / plan standard.
        std::string iface_adv = build_interface_advisory(name, iface_ledger, built_ifaces);
        if (!iface_adv.empty()) {
            events.emit("run_note", json{{"note", "interface_advisory"},
                                        {"part", name},
                                        {"chars", static_cast<int>(iface_adv.size())}});
        }

        std::string child;
        std::string mint_failure;
        try {
            json nd = doc::doc_new("part", name);
            publish_doc_call("doc_new",
                             doc_publish_params({{"kind", "part"}, {"name", name}}, options),
                             nd);
            child = jstr(nd, "doc_id");
        } catch (const std::exception& e) {
            mint_failure = e.what();
        } catch (...) {
            mint_failure = "doc_new failed";
        }
        if (child.empty()) {
            if (mint_failure.empty()) mint_failure = "doc_new failed";
            record_child_failure(mint_failure);
            events.emit("part_skipped",
                        json{{"index", static_cast<int>(idx)},
                             {"name", name},
                             {"reason", mint_failure}});
            continue;
        }

        json sub_opts = options;
        sub_opts["run_id"] = run_id + "-p" + std::to_string(idx);
        sub_opts["is_sub_run"] = true;
        // Inject via the same advisory_text seam used by advisor/spec/firm seeds.
        if (!iface_adv.empty()) {
            std::string combined = iface_adv;
            if (sub_opts.contains("advisory_text") && sub_opts["advisory_text"].is_string()) {
                const std::string prior = sub_opts["advisory_text"].get<std::string>();
                if (!prior.empty()) combined = prior + "\n\n" + iface_adv;
            }
            sub_opts["advisory_text"] = combined;
            // Ensure advisor path is on so run_design applies the advisory block.
            if (!sub_opts.contains("advisor")) sub_opts["advisor"] = true;
        }
        const json child_result = run_design(part_req, child, provider_cfg, emit, sub_opts);
        bool geometry_proven = false;
        if (child_result.value("status", std::string()) == "done") {
            try {
                const json mass = doc::doc_eval_mass(child, -1, 1.0);
                geometry_proven = mass.value("body_count", 0) > 0;
            } catch (...) {
                // No independent body proof means the child is not component-worthy.
            }
        }
        AssemblyChildDecision child_decision =
            classify_assembly_child(child_result, geometry_proven);
        if (child_decision.cancelled || run_ctl.cancelled()) {
            child_cancelled = true;
            break;
        }
        if (!child_decision.accept) {
            record_child_failure(child_decision.failure_reason);
            events.emit("part_skipped",
                        json{{"index", static_cast<int>(idx)},
                             {"name", name},
                             {"reason", child_decision.failure_reason},
                             {"child_status",
                              child_result.value("status", std::string("failed"))}});
            continue;
        }

        // Measure proven interface geometry for downstream dependents + verify.
        try {
            json tree_now = doc::doc_tree(child);
            json tree_body = tree_now.contains("tree") ? tree_now["tree"] : json::object();
            json bb = json::object();
            try {
                bb = doc::doc_bbox(child);
            } catch (...) {
            }
            MeasuredInterface mi = measure_interface_from_tree(name, tree_body, bb);
            mi.doc_id = child;
            built_ifaces[name] = mi;
            part_doc_ids[name] = child;
            events.emit("run_note", json{{"note", "part_interface_measured"},
                                        {"part", name},
                                        {"summary", mi.summary}});
        } catch (...) {
            part_doc_ids[name] = child;
        }

        std::filesystem::path path =
            run_dir(run_id) / ("part_" + std::to_string(idx) + ".step+");
        try {
            doc::doc_save(child, path.string());
            SavedPart sp;
            sp.path = std::filesystem::absolute(path).string();
            sp.name = name;
            sp.doc_id = child;
            sp.pos[0] = pos[0];
            sp.pos[1] = pos[1];
            sp.pos[2] = pos[2];
            sp.rot[0] = rot[0];
            sp.rot[1] = rot[1];
            sp.rot[2] = rot[2];
            sp.plan_index = idx;
            saved.push_back(sp);
            ++built_count;
        } catch (const std::exception& e) {
            record_child_failure(e.what());
            events.emit("part_skipped",
                        json{{"index", static_cast<int>(idx)},
                             {"name", name},
                             {"reason", e.what()}});
        } catch (...) {
            record_child_failure("doc_save failed");
            events.emit("part_skipped",
                        json{{"index", static_cast<int>(idx)},
                             {"name", name},
                             {"reason", "doc_save failed"}});
        }
    }

    // Stopped by the user: report honestly and skip the (expensive) interface verify +
    // assembly creation. Any parts already built are left as saved .step+ files.
    if (child_cancelled || run_ctl.cancelled()) {
        events.emit("run_finished", json{{"status", "cancelled"},
                                         {"stop_reason", "cancelled"},
                                         {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "cancelled"}, {"stop_reason", "cancelled"},
                    {"revision", 0}, {"doc_id", doc_id}};
    }

    // 2b) Assembly-time interface verification — seed dims win; route fix to DEPENDENT.
    if (!iface_ledger.interfaces.empty() && built_ifaces.size() >= 2) {
        std::vector<InterfaceMismatch> mismatches =
            verify_interfaces(iface_ledger, built_ifaces);
        if (!mismatches.empty()) {
            json mm_j = json::array();
            std::set<std::string> deps_to_fix;
            for (const auto& mm : mismatches) {
                mm_j.push_back(json{{"interface_id", mm.interface_id},
                                    {"kind", mm.kind},
                                    {"seed", mm.seed_part},
                                    {"dependent", mm.dependent_part},
                                    {"detail", mm.detail},
                                    {"expected", mm.expected},
                                    {"actual", mm.actual}});
                deps_to_fix.insert(mm.dependent_part);
            }
            events.emit("run_note", json{{"note", "interface_mismatch"},
                                        {"count", static_cast<int>(mismatches.size())},
                                        {"mismatches", mm_j}});
            // Targeted fix: re-run ONLY the dependent part with mismatch advisory.
            for (const std::string& dep : deps_to_fix) {
                auto dit = part_doc_ids.find(dep);
                if (dit == part_doc_ids.end() || dit->second.empty()) continue;
                std::string fix_adv = build_mismatch_fix_advisory(mismatches, dep);
                if (fix_adv.empty()) continue;
                // Also re-include measured seed interface so the fixer has exact targets.
                std::string full_adv =
                    fix_adv + "\n" + build_interface_advisory(dep, iface_ledger, built_ifaces);
                events.emit("run_note", json{{"note", "interface_fix_dependent"},
                                            {"part", dep},
                                            {"doc_id", dit->second}});
                json fix_opts = options;
                fix_opts["run_id"] = run_id + "-iface-fix-" + dep;
                fix_opts["is_sub_run"] = true;
                fix_opts["advisor"] = true;
                fix_opts["advisory_text"] = full_adv;
                std::string req = part_reqs.count(dep) ? part_reqs[dep]
                                                       : ("Fix interface on part '" + dep + "'");
                req += "\n\nINTERFACE FIX REQUIRED: match the SEED part's measured interface. "
                       "Do not change the seed.";
                const json fix_result =
                    run_design(req, dit->second, provider_cfg, emit, fix_opts);
                bool fix_geometry_proven = false;
                if (fix_result.value("status", std::string()) == "done") {
                    try {
                        const json mass = doc::doc_eval_mass(dit->second, -1, 1.0);
                        fix_geometry_proven = mass.value("body_count", 0) > 0;
                    } catch (...) {
                    }
                }
                const AssemblyChildDecision fix_decision =
                    classify_assembly_child(fix_result, fix_geometry_proven);
                if (fix_decision.cancelled || run_ctl.cancelled()) {
                    child_cancelled = true;
                    break;
                }
                if (!fix_decision.accept) {
                    record_child_failure(fix_decision.failure_reason);
                    events.emit("part_skipped",
                                json{{"name", dep},
                                     {"reason", fix_decision.failure_reason},
                                     {"child_status",
                                      fix_result.value("status", std::string("failed"))}});
                    continue;
                }
                // Re-measure + re-save the fixed dependent.
                try {
                    json tree_now = doc::doc_tree(dit->second);
                    json tree_body =
                        tree_now.contains("tree") ? tree_now["tree"] : json::object();
                    json bb = json::object();
                    try {
                        bb = doc::doc_bbox(dit->second);
                    } catch (...) {
                    }
                    MeasuredInterface mi = measure_interface_from_tree(dep, tree_body, bb);
                    mi.doc_id = dit->second;
                    built_ifaces[dep] = mi;
                    // Update saved path for this part if present.
                    for (auto& sp : saved) {
                        if (sp.name != dep) continue;
                        try {
                            doc::doc_save(dit->second, sp.path);
                        } catch (const std::exception& e) {
                            record_child_failure(e.what());
                        } catch (...) {
                            record_child_failure("doc_save failed");
                        }
                        break;
                    }
                } catch (...) {
                }
            }
        } else {
            events.emit("run_note", json{{"note", "interface_verify_ok"},
                                        {"parts", static_cast<int>(built_ifaces.size())}});
        }
    }

    if (child_cancelled || run_ctl.cancelled()) {
        events.emit("run_finished", json{{"status", "cancelled"},
                                         {"stop_reason", "cancelled"},
                                         {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "cancelled"},
                    {"stop_reason", "cancelled"},
                    {"revision", 0},
                    {"doc_id", doc_id}};
    }

    if (saved.empty()) {
        const AssemblyFinish finish =
            classify_assembly_finish(0, any_child_failed, first_child_failure);
        events.emit("run_finished", json{{"status", finish.status},
                                         {"stop_reason", finish.stop_reason},
                                         {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", finish.status},
                    {"stop_reason", finish.stop_reason},
                    {"revision", 0},
                    {"doc_id", doc_id}};
    }

    // 3) Create the assembly doc and insert each saved part as a positioned, fixed component.
    const std::string raw_asm_name =
        (!plan.is_null() && plan.contains("name") && plan["name"].is_string())
            ? plan["name"].get<std::string>()
            : std::string();
    const std::string asm_name = mission_mint_name(raw_asm_name, "Assembly");
    std::string asm_id;
    try {
        json nd = doc::doc_new("assembly", asm_name);
        publish_doc_call("doc_new",
                         doc_publish_params({{"kind", "assembly"}, {"name", asm_name}}, options),
                         nd);
        asm_id = jstr(nd, "doc_id");
    } catch (const std::exception& e) {
        events.emit("assembly_failed", json{{"reason", e.what()}});
    } catch (...) {
        events.emit("assembly_failed", json{{"reason", "doc_new failed"}});
    }
    if (asm_id.empty()) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "assembly_doc_failed"}, {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "assembly_doc_failed"},
                    {"first_fail_reason", first_child_failure},
                    {"revision", 0},
                    {"doc_id", doc_id}};
    }
    // 4x4 row-major component transform = R(rot)·... + translation. Rotation is Euler degrees
    // applied X then Y then Z (R = Rz·Ry·Rx), matching the placement.rot the planner emits.
    auto placement_transform = [](const double pos[3], const double rot[3]) {
        const double d2r = 3.14159265358979323846 / 180.0;
        double cx = std::cos(rot[0] * d2r), sx = std::sin(rot[0] * d2r);
        double cy = std::cos(rot[1] * d2r), sy = std::sin(rot[1] * d2r);
        double cz = std::cos(rot[2] * d2r), sz = std::sin(rot[2] * d2r);
        // R = Rz * Ry * Rx (row-major 3x3).
        double r00 = cz * cy, r01 = cz * sy * sx - sz * cx, r02 = cz * sy * cx + sz * sx;
        double r10 = sz * cy, r11 = sz * sy * sx + cz * cx, r12 = sz * sy * cx - cz * sx;
        double r20 = -sy,     r21 = cy * sx,                r22 = cy * cx;
        return json{r00, r01, r02, pos[0],
                    r10, r11, r12, pos[1],
                    r20, r21, r22, pos[2],
                    0,   0,   0,   1};
    };
    json comp_ops = json::array();
    for (size_t k = 0; k < saved.size(); ++k) {
        const SavedPart& sp = saved[k];
        json transform = placement_transform(sp.pos, sp.rot);
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
    const std::filesystem::path asm_output =
        mission_output_path(run_dir(run_id), asm_name, "Assembly");
    if (asm_output.empty() ||
        !mission_path_is_beneath(run_dir(run_id), asm_output)) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "invalid_assembly_path"},
                                         {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "invalid_assembly_path"},
                    {"revision", 0},
                    {"doc_id", asm_id},
                    {"created_doc", true},
                    {"assembly", true},
                    {"first_fail_reason", first_child_failure}};
    }
    const std::string asm_path = asm_output.string();
    try {
        json before = doc::doc_tree(asm_id);
        const int base_revision = before.value("revision", 0);
        json resp = doc::doc_apply_ops(asm_id, base_revision, comp_ops, false);
        publish_doc_call(
            "doc_apply_ops",
            doc_publish_params({{"doc_id", asm_id}, {"base_revision", base_revision},
                                {"ops", comp_ops}, {"coalesce", false}},
                               options),
            resp);
        asm_rev = resp.value("revision", 0);
        doc::doc_save(asm_id, asm_path);
    } catch (const std::exception& e) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", std::string("assemble_failed: ") + e.what()},
                                         {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "assemble_failed"},
                    {"first_fail_reason", first_child_failure},
                    {"revision", 0},
                    {"doc_id", asm_id},
                    {"created_doc", true},
                    {"assembly", true}};
    }

    const AssemblyFinish finish = classify_assembly_finish(
        static_cast<int>(saved.size()), any_child_failed, first_child_failure);
    events.emit("run_finished", json{{"status", finish.status},
                                     {"stop_reason", finish.stop_reason},
                                     {"revision", asm_rev},
                                     {"components", static_cast<int>(saved.size())}});
    json result{{"run_id", run_id},
                {"status", finish.status},
                {"stop_reason", finish.stop_reason},
                {"revision", asm_rev},
                {"doc_id", asm_id},
                {"created_doc", true},
                {"assembly", true},
                {"asm_path", asm_path},
                {"components", static_cast<int>(saved.size())}};
    if (!first_child_failure.empty())
        result["first_fail_reason"] = first_child_failure;
    return result;
}

// ============================================================================ mission
// The closed planner/storage vocabulary. It intentionally includes capabilities
// without native executors so approved plans remain parseable; the acceptance
// gate below rejects those plans with capability_unavailable before execution.
namespace {

// One human + deterministic line per flow result, plus a qualitative read. Kept honest:
// the LBM grid is coarse (the result string itself says "directional estimate"), so the
// verdict reports the L/D ratio and flags clearly-bad signs rather than over-claiming.
std::string deterministic_verdict(const ojson& sim_results) {
    if (!sim_results.is_array() || sim_results.empty())
        return "No simulation was run, so there is nothing to judge yet.";
    std::ostringstream os;
    int n = 0;
    for (const auto& sr : sim_results) {
        if (!sr.is_object() || !truthy_json(sr.value("ok", ojson(false)))) {
            os << "- A flow run did not produce usable forces"
               << (sr.contains("answer") ? (": " + sr.value("answer", std::string())) : ".")
               << "\n";
            continue;
        }
        double drag = sr.value("drag_n", 0.0);
        double lift = sr.value("lift_n", 0.0);
        double cd = sr.value("cd", 0.0);
        double v = sr.value("velocity_ms", 0.0);
        double aoa = sr.value("aoa_deg", 0.0);
        double ld = lift / std::max(std::fabs(drag), 1e-9);
        std::ostringstream line;
        line.precision(2);
        line << std::fixed;
        line << "- At " << v << " m/s, " << aoa << " deg AoA: lift " << lift << " N, drag "
             << drag << " N (Cd " << cd << "), lift-to-drag ratio " << ld << ". ";
        if (lift <= 0.0)
            line << "Lift is non-positive — this orientation does not generate useful lift; "
                    "check the angle of attack or the wing.";
        else if (ld >= 10.0)
            line << "That is an efficient L/D for a model aircraft — the design looks "
                    "aerodynamically sound.";
        else if (ld >= 4.0)
            line << "That is a workable L/D — the design flies, with room to cut drag.";
        else
            line << "That L/D is low — the shape is draggy for the lift it makes; consider a "
                    "cleaner profile or higher aspect ratio.";
        os << line.str() << "\n";
        ++n;
    }
    if (n == 0) os << "No flow run produced usable forces.\n";
    os << "\n(LBM is a coarse, directional estimate — trust the trend and the L/D, not the "
          "absolute drag to three digits.)";
    return os.str();
}

// Build the verdict text: deterministic baseline always, then (best-effort) an LLM reporter
// pass layered on top. Falls back to the deterministic text when offline or on any error.
std::string build_verdict(const ProviderFn& provider, const std::string& prompts_dir, int budget,
                          int ctx_cap, const std::string& mission_request,
                          const std::string& design_summary, const ojson& sim_results) {
    std::string deterministic = deterministic_verdict(sim_results);
    // Compose the reporter's context into the request field (build_context surfaces request).
    DesignState rstate;
    rstate.request = "Mission: " + mission_request + "\n\nWhat was built:\n" +
                     (design_summary.empty() ? "(model summary unavailable)" : design_summary) +
                     "\n\nMeasured results:\n" + deterministic;
    AgentCall ra = agent_call(provider, rstate, "reporter", prompts_dir, budget, false, ctx_cap);
    if (ra.has_call_error || ra.reply.empty()) return deterministic;
    // Trim whitespace.
    size_t a = ra.reply.find_first_not_of(" \t\r\n");
    size_t b = ra.reply.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) return deterministic;
    return ra.reply.substr(a, b - a + 1);
}

// A compact digest of the doc's feature tree, for the reporter's "what was built" context.
std::string doc_design_summary(const std::string& doc_id) {
    try {
        json t = doc::doc_tree(doc_id);
        if (t.is_object() && t.contains("tree"))
            return digest_tree(tree_to_ojson(t["tree"]), 20);
    } catch (...) {
    }
    return "";
}

}  // namespace

json run_mission(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                 EmitFn emit, SimFn sim, const json& options) {
    std::string prompts_dir = options.value("prompts_dir", std::string());
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "mission-" + std::to_string(static_cast<unsigned long long>(
                                  std::chrono::steady_clock::now().time_since_epoch().count()));

    RunControl run_ctl;
    register_run_control(run_id, run_ctl);
    struct RunCtlGuard {
        std::string id;
        ~RunCtlGuard() { unregister_run_control(id); }
    } run_ctl_guard{run_id};
    // Save/restore (not clear-to-null): a mission sub-run's guard must hand the
    // parent's flag back so later parent-level provider calls stay cancellable.
    const std::atomic<bool>* prev_cancel_flag = provider_cancel_flag();
    set_provider_cancel_flag(run_ctl.flag.get());
    if (prev_cancel_flag && prev_cancel_flag->load(std::memory_order_acquire))
        run_ctl.request_cancel();
    struct ProvCancelGuard {
        const std::atomic<bool>* prev;
        ~ProvCancelGuard() { set_provider_cancel_flag(prev); }
    } prov_cancel_guard{prev_cancel_flag};

    ProviderFn provider = make_provider(provider_cfg);
    const CallBudget cb = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int budget = cb.output;
    const int ctx_cap = cb.input_cap;
    RunEvents events(run_id, emit);
    // Live provider token streaming: the mission planner's drafting + the verdict
    // synthesis run on this thread — without a delta sink their thinking never
    // reached the GUI until the call completed (looked frozen, not "live").
    auto mission_coalescer = std::make_shared<DeltaCoalescer>();
    mission_coalescer->ev = &events;
    ProviderDeltaGuard mission_delta_guard{mission_coalescer};

    // ---- Mode / execution_style (before drafting) ----
    const std::string copilot_mode = options.value("mode", std::string());
    const std::string org = options.value("org", copilot_mode);
    const std::string execute_as = options.value("execute_as", std::string());
    const bool plan_only = options.value("plan_only", false) || copilot_mode == "plan";
    const bool advisor_on = !options.contains("advisor") ? true : options.value("advisor", true);

    ComplexityVerdict mcx = classify_complexity(request);
    DomainVerdict mdom = classify_domain(request);
    if (options.contains("complexity") && options["complexity"].is_string()) {
        const std::string forced = options["complexity"].get<std::string>();
        if (forced == "solo") mcx = {Complexity::Solo, "forced by options"};
        else if (forced == "standard") mcx = {Complexity::Standard, "forced by options"};
        else if (forced == "complex") mcx = {Complexity::Complex, "forced by options"};
    }
    const bool cross_discipline =
        (mdom.domain == Domain::Ecad || mdom.domain == Domain::Cam ||
         mdom.domain == Domain::Simulation);
    const bool allow_company = options.value("corporate_mode", false) ||
                               options.value("allow_company", false);
    ExecStructure rec_struct =
        recommend_structure(mcx.level, cross_discipline, allow_company);
    std::string execution_style = execution_style_from_structure(structure_label(rec_struct));
    if (copilot_mode == "swarm" || org == "swarm" || execute_as == "swarm")
        execution_style = "swarm";
    else if (copilot_mode == "eng_firm" || org == "eng_firm" || execute_as == "eng_firm")
        execution_style = "eng_firm";
    else if (copilot_mode == "edit")
        execution_style = "edit";
    if (options.contains("execution_style") && options["execution_style"].is_string()) {
        const std::string es = options["execution_style"].get<std::string>();
        if (es == "edit" || es == "swarm" || es == "eng_firm") execution_style = es;
    }
    const bool swarm_mode = (execution_style == "swarm");
    const bool eng_firm_mode = (execution_style == "eng_firm");
    const bool needs_plan_gate = swarm_mode || eng_firm_mode;

    // Eng. Firm per-role model config (optional). Empty/absent → ambient provider
    // (byte-identical to today). Loaded once for the eng_firm branch.
    EngFirmModels ef_models;
    json ef_cfg = json::object();
    ProviderFn planner_provider = provider;
    int planner_budget = budget;
    int planner_ctx_cap = ctx_cap;
    ProviderFn advisor_provider = provider;
    int advisor_budget = budget;
    int advisor_ctx_cap = ctx_cap;
    if (eng_firm_mode) {
        ef_cfg = load_config();
        if (!ef_cfg.is_object()) ef_cfg = json::object();
        ef_models = load_eng_firm_models(ef_cfg);
        json mp_desc = resolve_eng_firm_provider(ef_models.mission_planner, ef_cfg);
        if (mp_desc.is_object() && !mp_desc.contains("error") && !mp_desc.empty()) {
            planner_provider = make_provider(mp_desc);
            const CallBudget pb = fit_call_budget(mp_desc, call_budget_for(mp_desc));
            planner_budget = pb.output;
            planner_ctx_cap = pb.input_cap;
        }
        json adv_desc = resolve_eng_firm_provider(ef_models.advisor, ef_cfg);
        if (adv_desc.is_object() && !adv_desc.contains("error") && !adv_desc.empty()) {
            advisor_provider = make_provider(adv_desc);
            const CallBudget ab = fit_call_budget(adv_desc, call_budget_for(adv_desc));
            advisor_budget = ab.output;
            advisor_ctx_cap = ab.input_cap;
        }
    }

    // approved_plan: host-approved or previously advisor-approved mission plan.
    ojson approved_plan = nullptr;
    if (options.contains("approved_plan") && !options["approved_plan"].is_null()) {
        const auto& ap = options["approved_plan"];
        if (ap.is_object()) approved_plan = ojson::parse(ap.dump());
        else if (ap.is_string()) {
            const std::string& s = ap.get_ref<const std::string&>();
            if (!s.empty()) {
                ojson parsed = ojson::parse(s, nullptr, false);
                if (!parsed.is_discarded() && parsed.is_object()) approved_plan = std::move(parsed);
            }
        }
    }
    const bool has_approved_plan = !approved_plan.is_null() && approved_plan.is_object() &&
                                   approved_plan.contains("tasks") &&
                                   approved_plan["tasks"].is_array() &&
                                   !approved_plan["tasks"].empty();

    events.emit("task_route", json{{"domain", mdom.label()},
                                   {"reason", mdom.reason},
                                   {"complexity", mcx.label()},
                                   {"structure", structure_label(rec_struct)},
                                   {"execution_style", execution_style}});
    events.emit("complexity", json{{"level", mcx.label()},
                                   {"reason", mcx.reason},
                                   {"structure", structure_label(rec_struct)},
                                   {"execution_style", execution_style}});

    // 1) Decompose the request into an ordered task list.
    struct Task {
        std::string capability, title, req, target, id, discipline;
        std::vector<std::string> deps;
    };
    static const std::set<std::string> kCaps = {
        "design",  "assembly", "simulate_flow", "simulate_fea",
        "drawing", "evaluate", "report",        "reverse_engineer"};

    auto parse_tasks_from_plan = [&](const ojson& plan) -> std::vector<Task> {
        std::vector<Task> out;
        if (plan.is_null() || !plan.is_object() || !plan.contains("tasks") ||
            !plan["tasks"].is_array())
            return out;
        int idx = 0;
        for (const auto& t : plan["tasks"]) {
            if (!t.is_object()) continue;
            Task tk;
            tk.capability = t.value("capability", std::string());
            tk.title = t.value("title", std::string());
            tk.req = t.value("request", std::string());
            tk.target = t.value("target", std::string("previous"));
            tk.id = t.value("id", std::string());
            if (tk.id.empty()) tk.id = "u" + std::to_string(idx + 1);
            tk.discipline = t.value("discipline", std::string());
            if (t.contains("deps") && t["deps"].is_array()) {
                for (const auto& d : t["deps"])
                    if (d.is_string()) tk.deps.push_back(d.get<std::string>());
            }
            if (tk.capability.empty()) continue;
            if (tk.req.empty()) tk.req = request;
            out.push_back(std::move(tk));
            ++idx;
            if (out.size() >= 8) break;
        }
        return out;
    };

    std::vector<Task> tasks;
    ojson mission_plan_obj = nullptr;
    const std::string capability_override = options.value("capability", std::string());
    // True when the mission planner failed/empty and we fell back to a single stub task.
    // Distinguishes real 1-task plans from 403/provider-failure degradation.
    bool planner_degraded = false;

    if (has_approved_plan) {
        mission_plan_obj = approved_plan;
        tasks = parse_tasks_from_plan(approved_plan);
    } else if (!capability_override.empty() && kCaps.count(capability_override)) {
        tasks.push_back(Task{capability_override, "Reverse-engineer import", request, "active",
                             "u1", "", {}});
    } else {
        DesignState mstate;
        mstate.request = request;
        mstate.doc_id = doc_id;
        mstate.execution_style = execution_style;
        const std::string style_dir =
            "[execution_style: " + execution_style + "] " +
            (swarm_mode
                 ? "Structure tasks as swarm units with id+deps (parts first, assembly last). "
                   "For EVERY mating pair declare top-level interfaces:[{between:[partA,partB],"
                   "kind,seed,holes/fit...}] and put the SEED part unit in an earlier wave "
                   "(dependent deps includes seed id)."
                 : (eng_firm_mode
                        ? "Structure tasks as eng_firm discipline jobs (optional discipline field). "
                          "Include interfaces:[{between,kind,seed,...}] for mating parts."
                        : "Minimal capability sequence. If multi-part with mates, still emit "
                          "interfaces:[{between,kind,seed,holes/fit}]."));
        AgentCall ma = agent_call(planner_provider, mstate, "mission_planner", prompts_dir,
                                  planner_budget, false, planner_ctx_cap, style_dir);
        ojson plan = extract_json_obj(ma.reply);
        if (!plan.is_null() && plan.is_object()) {
            mission_plan_obj = plan;
            tasks = parse_tasks_from_plan(plan);
        }
        if (tasks.empty()) {
            // Planner call failed (e.g. 403) or returned unusable JSON → single stub.
            planner_degraded = true;
            tasks.push_back(Task{"design", "Build the model", request, "active", "u1", "", {}});
            if (ma.has_call_error) {
                events.emit("run_note",
                            json{{"note", "mission planner degraded to stub"},
                                 {"reason", ma.call_error},
                                 {"fallback", "provider_error"}});
            } else {
                events.emit("run_note",
                            json{{"note", "mission planner degraded to stub"},
                                 {"reason", "empty or unparseable plan"},
                                 {"fallback", "unparseable"}});
            }
        }
    }

    // ---- Plan gate for swarm/eng_firm: advisor-approve draft when no approved_plan ----
    if (needs_plan_gate && !has_approved_plan && !plan_only) {
        // Build a plan object if the planner only emitted tasks.
        if (mission_plan_obj.is_null() || !mission_plan_obj.is_object()) {
            mission_plan_obj = ojson::object();
            mission_plan_obj["mission"] = request;
        }
        ojson tarr = ojson::array();
        for (const auto& tk : tasks) {
            ojson t = ojson{{"capability", tk.capability},
                            {"title", tk.title},
                            {"request", tk.req},
                            {"target", tk.target},
                            {"id", tk.id}};
            if (!tk.deps.empty()) {
                ojson deps = ojson::array();
                for (const auto& d : tk.deps) deps.push_back(d);
                t["deps"] = deps;
            }
            if (!tk.discipline.empty()) t["discipline"] = tk.discipline;
            tarr.push_back(t);
        }
        mission_plan_obj["tasks"] = tarr;

        if (advisor_on) {
            DesignState adv_st;
            adv_st.request =
                std::string("STAGE: plan\nReview this MISSION PLAN for execution_style='") +
                execution_style + "'.\nReturn verdict approve|revise|escalate with a note.\n\n" +
                "REQUEST:\n" + request + "\n\nPLAN:\n" + py_json_dumps_indent0(mission_plan_obj);
            AgentCall ac = agent_call(advisor_provider, adv_st, "advisor", prompts_dir,
                                      advisor_budget, false, advisor_ctx_cap);
            AdvisorPlanVerdict v = parse_advisor_plan_verdict(ac.reply);
            if (ac.has_call_error) {
                v.verdict = "approve";
                v.note = "advisor provider error; continuing";
                v.fallback = "provider_error";
                v.ok = false;
            }
            json adv_ev = json{{"kind", "advisor"},
                               {"stage", "plan"},
                               {"question", "review mission plan"},
                               {"advice", v.verdict + (v.note.empty() ? "" : (": " + v.note))},
                               {"verdict", v.verdict},
                               {"note", v.note},
                               {"confidence", v.confidence},
                               {"execution_style", execution_style}};
            if (!v.fallback.empty()) adv_ev["fallback"] = v.fallback;
            events.emit("advisor", adv_ev);
            if (v.verdict == "escalate") {
                json plan_j = mission_plan_obj;
                events.emit("mission_finished",
                            json{{"status", "plan_ready"},
                                 {"stop_reason", "needs_user_decision"},
                                 {"execution_style", execution_style}});
                return json{{"run_id", run_id},
                            {"status", "plan_ready"},
                            {"stop_reason", "needs_user_decision"},
                            {"plan", plan_j},
                            {"reason", v.note},
                            {"execution_style", execution_style},
                            {"doc_id", doc_id},
                            {"revision", 0},
                            {"tasks", json::array()}};
            }
            if (v.verdict == "revise") {
                // ONE revision pass, then continue (no second review).
                DesignState mstate;
                mstate.request = request;
                mstate.doc_id = doc_id;
                const std::string rev_dir =
                    std::string("[execution_style: ") + execution_style +
                    "]\n[PLAN REVISION REQUIRED]\n" +
                    (v.note.empty() ? "Revise the mission plan." : v.note);
                AgentCall ma = agent_call(planner_provider, mstate, "mission_planner", prompts_dir,
                                          planner_budget, false, planner_ctx_cap, rev_dir);
                ojson rp = extract_json_obj(ma.reply);
                if (!rp.is_null() && rp.is_object()) {
                    mission_plan_obj = rp;
                    auto revised = parse_tasks_from_plan(rp);
                    if (!revised.empty()) tasks = std::move(revised);
                }
            }
            // approve → fall through to execution
        }
    }

    // Plan-acceptance capability gate. Stored plans keep the full planner
    // vocabulary, but a task with no native executor must terminate here,
    // before mission_started, per-task provider resolution, or any mutation.
    for (const auto& tk : tasks) {
        // Draft plans may advertise the full vocabulary for user review. Once
        // a plan is approved, plan_only must not bypass executor validation.
        if (plan_only && !has_approved_plan) continue;
        const MissionCapabilityDecision capability =
            classify_mission_capability(tk.capability);
        if (capability.executable) continue;
        json refused_tasks = json::array();
        for (const auto& planned : tasks) {
            const bool unavailable = planned.id == tk.id;
            refused_tasks.push_back(
                json{{"capability", planned.capability},
                     {"title", planned.title},
                     {"id", planned.id},
                     {"status", unavailable ? "failed" : "pending"},
                     {"doc_id", doc_id},
                     {"summary", unavailable ? capability.stop_reason : ""}});
        }
        events.emit("mission_finished",
                    json{{"status", "failed"},
                         {"stop_reason", capability.stop_reason},
                         {"capability", tk.capability},
                         {"execution_style", execution_style},
                         {"tasks_completed", 0},
                         {"tasks_failed", 1},
                         {"tasks_abandoned", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", capability.stop_reason},
                    {"capability", tk.capability},
                    {"error_detail",
                     "Mission capability '" + tk.capability +
                         "' has no native executor in this build."},
                    {"execution_style", execution_style},
                    {"doc_id", doc_id},
                    {"revision", 0},
                    {"tasks", refused_tasks},
                    {"tasks_completed", 0},
                    {"tasks_failed", 1},
                    {"tasks_abandoned", 0}};
    }
    // Manual plan_only on mission: return plan_ready for the host.
    if (plan_only) {
        if (mission_plan_obj.is_null() || !mission_plan_obj.is_object()) {
            mission_plan_obj = ojson::object();
            mission_plan_obj["mission"] = request;
            ojson tarr = ojson::array();
            for (const auto& tk : tasks)
                tarr.push_back(ojson{{"capability", tk.capability},
                                     {"title", tk.title},
                                     {"request", tk.req},
                                     {"target", tk.target},
                                     {"id", tk.id}});
            mission_plan_obj["tasks"] = tarr;
        }
        json plan_j = mission_plan_obj;
        json task_list = json::array();
        for (const auto& tk : tasks)
            task_list.push_back(json{{"capability", tk.capability}, {"title", tk.title},
                                     {"id", tk.id}, {"status", "pending"}});
        emit_plan_state(events, "phased_todo", "Mission", "", 1,
                        plan_state_items_from_mission(task_list, "Mission"));
        events.emit("mission_finished",
                    json{{"status", "plan_ready"},
                         {"stop_reason", "awaiting_plan_approval"},
                         {"execution_style", execution_style}});
        return json{{"run_id", run_id},
                    {"status", "plan_ready"},
                    {"stop_reason", "awaiting_plan_approval"},
                    {"plan", plan_j},
                    {"execution_style", execution_style},
                    {"doc_id", doc_id},
                    {"revision", 0},
                    {"tasks", task_list}};
    }


    json task_list = json::array();
    for (const auto& tk : tasks)
        task_list.push_back(json{{"capability", tk.capability},
                                 {"title", tk.title},
                                 {"id", tk.id},
                                 {"status", "pending"}});
    {
        json ms = json{{"request", request},
                       {"task_count", static_cast<int>(tasks.size())},
                       {"tasks", task_list},
                       {"execution_style", execution_style}};
        // Distinguish planner 403/empty stub from a real single-task plan.
        if (planner_degraded) ms["degraded"] = true;
        events.emit("mission_started", ms);
    }

    // ---- Eng. Firm: firm_bids + firm_team telemetry (roster once) ----
    // Mission-level award is firm selection telemetry plus the default task lead.
    std::vector<TeamMember> firm_team_members;
    std::string awarded_firm_id, awarded_firm_name;
    long awarded_tokens = 0;
    json firm_ledger = json::array();  // in-memory per-run ledger

    if (eng_firm_mode) {
        std::vector<Bid> bids = collect_bids(request, mcx.level, mdom.domain);
        json jbids = json::array();
        for (const auto& b : bids) {
            jbids.push_back(json{{"firm_id", b.firm_id},
                                 {"firm", b.firm_name},
                                 {"in_scope", b.in_scope},
                                 {"tokens", b.tokens}});
            // Bids are sorted cheapest-first with in-scope firms first.
            if (awarded_firm_id.empty() && b.in_scope) {
                awarded_firm_id = b.firm_id;
                awarded_firm_name = b.firm_name;
                awarded_tokens = b.tokens;
            }
        }
        // The static roster contains generalists, but preserve a deterministic
        // selection if that invariant is ever broken.
        if (awarded_firm_id.empty() && !bids.empty()) {
            awarded_firm_id = bids.front().firm_id;
            awarded_firm_name = bids.front().firm_name;
            awarded_tokens = bids.front().tokens;
        }
        events.emit("firm_bids", json{{"domain", mdom.label()},
                                      {"complexity", mcx.label()},
                                      {"bids", jbids},
                                      {"awarded_firm_id", awarded_firm_id},
                                      {"awarded_firm", awarded_firm_name},
                                      {"awarded_tokens", awarded_tokens}});
        firm_team_members = assemble_team(request, mdom.domain);
        json jteam = json::array();
        for (const auto& tm : firm_team_members)
            jteam.push_back(json{{"firm_id", tm.firm_id},
                                 {"firm", tm.firm_name},
                                 {"role", tm.role_title},
                                 {"scope", DomainVerdict{tm.scope, ""}.label()},
                                 {"lead", tm.lead},
                                 {"subcontractor", tm.subcontractor}});
        events.emit("firm_team", json{{"kind", "firm_team"}, {"members", jteam}});
    }

    // Additive plan_state (mission:-namespaced via plan_state_items_from_mission).
    int mission_plan_rev = 1;
    json mission_plan_tasks = task_list;
    emit_plan_state(events, "phased_todo", "Mission", /*summary=*/"", mission_plan_rev,
                    plan_state_items_from_mission(mission_plan_tasks, "Mission"));

    // ---- Interface-aware deps (mission plan.interfaces) + wave decomposition ----
    // Fold interface seed→dependent edges into task.deps so a mating part never
    // lands in the same (or earlier) swarm wave as its seed.
    InterfaceLedger mission_ifaces;
    if (!mission_plan_obj.is_null() && mission_plan_obj.is_object()) {
        json mpj = json::parse(mission_plan_obj.dump());
        mission_ifaces = parse_interface_ledger(mpj);
    }
    if (!mission_ifaces.interfaces.empty()) {
        // Build a JSON task array with id/title/request/deps for the folder.
        json tasks_j = json::array();
        for (const auto& tk : tasks) {
            json t = json{{"id", tk.id},
                         {"title", tk.title},
                         {"request", tk.req},
                         {"capability", tk.capability}};
            json deps = json::array();
            for (const auto& d : tk.deps) deps.push_back(d);
            t["deps"] = deps;
            tasks_j.push_back(std::move(t));
        }
        json folded = fold_interface_deps_into_tasks(tasks_j, mission_ifaces);
        // Write folded deps back onto Task structs.
        for (size_t i = 0; i < tasks.size() && i < folded.size(); ++i) {
            if (!folded[i].is_object() || !folded[i].contains("deps") ||
                !folded[i]["deps"].is_array())
                continue;
            tasks[i].deps.clear();
            for (const auto& d : folded[i]["deps"])
                if (d.is_string()) tasks[i].deps.push_back(d.get<std::string>());
        }
        events.emit("run_note",
                    json{{"note", "mission_interface_deps_folded"},
                         {"interfaces", static_cast<int>(mission_ifaces.interfaces.size())}});
    }

    // ---- Wave decomposition for swarm (deps → sequential waves) ----
    // v1: waves run sequentially; units within a wave also sequential.
    // Prefer the shared helper so interface-folded deps stay consistent with tests.
    std::vector<std::vector<size_t>> waves;
    if (swarm_mode) {
        json tasks_for_waves = json::array();
        for (const auto& tk : tasks) {
            json t = json{{"id", tk.id}};
            json deps = json::array();
            for (const auto& d : tk.deps) deps.push_back(d);
            t["deps"] = deps;
            tasks_for_waves.push_back(std::move(t));
        }
        waves = waves_from_task_deps(tasks_for_waves);
        if (waves.empty() && !tasks.empty()) {
            // Fallback identical to prior Kahn loop.
            std::map<std::string, size_t> id_to_idx;
            for (size_t i = 0; i < tasks.size(); ++i) id_to_idx[tasks[i].id] = i;
            std::vector<int> indeg(tasks.size(), 0);
            std::vector<std::vector<size_t>> adj(tasks.size());
            for (size_t i = 0; i < tasks.size(); ++i) {
                for (const auto& d : tasks[i].deps) {
                    auto it = id_to_idx.find(d);
                    if (it == id_to_idx.end()) continue;
                    adj[it->second].push_back(i);
                    indeg[i] += 1;
                }
            }
            std::vector<size_t> ready;
            for (size_t i = 0; i < tasks.size(); ++i)
                if (indeg[i] == 0) ready.push_back(i);
            std::vector<bool> placed(tasks.size(), false);
            while (!ready.empty()) {
                waves.push_back(ready);
                std::vector<size_t> next;
                for (size_t u : ready) {
                    placed[u] = true;
                    for (size_t v : adj[u]) {
                        if (--indeg[v] == 0) next.push_back(v);
                    }
                }
                ready.swap(next);
            }
            std::vector<size_t> rest;
            for (size_t i = 0; i < tasks.size(); ++i)
                if (!placed[i]) rest.push_back(i);
            if (!rest.empty()) waves.push_back(rest);
        }
    } else {
        // Non-swarm: one unit per sequential "wave" (preserves prior linear order).
        for (size_t i = 0; i < tasks.size(); ++i) waves.push_back({i});
    }

    // 2) Execute tasks (wave-ordered), threading the working document forward.
    std::string current_doc = doc_id;
    std::string final_doc = doc_id;
    bool created_doc = false;
    std::string asm_path;
    int components = 0;
    int revision = 0;
    bool any_built = false;
    bool any_ok = false;
    bool reported = false;
    // First real failure cause from a unit (provider_error / no_geometry / …).
    // Used so mission_finished never returns opaque stop_reason="planned".
    std::string first_fail_reason;
    ojson sim_results = ojson::array();
    std::string design_summary;
    std::string verdict;
    json re_report = json::object();
    json task_results = json::array();
    // Measured interface geometry from completed design/assembly units (seed → dependent).
    std::map<std::string, MeasuredInterface> mission_built_ifaces;
    std::vector<std::string> task_status(tasks.size(), "pending");

    auto set_task_plan_status = [&](size_t i, const std::string& st, const std::string& sum) {
        if (i < mission_plan_tasks.size() && mission_plan_tasks[i].is_object()) {
            mission_plan_tasks[i]["status"] = st;
            if (!sum.empty()) mission_plan_tasks[i]["summary"] = sum;
        }
        ++mission_plan_rev;
        emit_plan_state(events, "phased_todo", "Mission", "", mission_plan_rev,
                        plan_state_items_from_mission(mission_plan_tasks, "Mission"));
    };

    auto run_one_task = [&](size_t i) -> void {
        if (run_ctl.cancelled()) { task_status[i] = "skipped"; return; }
        const Task& tk = tasks[i];
        std::string target_doc = (tk.target == "active") ? doc_id : current_doc;
        events.emit("mission_task_started",
                    json{{"index", static_cast<int>(i)},
                         {"total", static_cast<int>(tasks.size())},
                         {"capability", tk.capability},
                         {"title", tk.title},
                         {"id", tk.id},
                         {"doc_id", target_doc}});
        set_task_plan_status(i, "running", "");

        // Eng-firm per-task bid/award + team seeds + ledger.
        std::string firm_role_seed;
        long task_bid_tokens = 0;
        // Per-task provider override (eng_firm models). Defaults to ambient.
        json task_provider_cfg = provider_cfg;
        if (eng_firm_mode) {
            ComplexityVerdict tcx = classify_complexity(tk.req);
            DomainVerdict tdom = classify_domain(tk.req);
            std::vector<Bid> tbids = collect_bids(tk.req, tcx.level, tdom.domain);
            std::string win_id, win_name;
            long win_tokens = 0;
            std::string award_reason;
            // Prefer the mission-selected firm even if task-domain reclassification
            // dropped it out of scope.
            if (!awarded_firm_id.empty()) {
                for (const auto& b : tbids) {
                    if (b.firm_id != awarded_firm_id) continue;
                    win_id = b.firm_id;
                    win_name = b.firm_name.empty() ? awarded_firm_name : b.firm_name;
                    win_tokens = b.tokens;
                    award_reason = "mission-selected firm";
                    break;
                }
            }
            // Otherwise use the cheapest in-scope estimated-token bid.
            if (win_id.empty()) {
                for (const auto& b : tbids) {
                    if (!b.in_scope) continue;
                    win_id = b.firm_id;
                    win_name = b.firm_name;
                    win_tokens = b.tokens;
                    award_reason = "lowest in-scope estimated-token bid";
                    break;
                }
            }
            if (win_id.empty() && !tbids.empty()) {
                win_id = tbids.front().firm_id;
                win_name = tbids.front().firm_name;
                win_tokens = tbids.front().tokens;
                award_reason = "first available estimated-token bid";
            }
            if (win_name.empty()) win_name = awarded_firm_name;
            task_bid_tokens = win_tokens;
            events.emit("firm_award", json{{"kind", "firm_award"},
                                           {"firm", win_name},
                                           {"firm_id", win_id},
                                           {"tokens", win_tokens},
                                           {"reason", award_reason},
                                           {"task_id", tk.id},
                                           {"title", tk.title}});
            // firm:-namespaced plan_state item for the award.
            {
                json firm_items = json::array();
                firm_items.push_back(json{{"id", "firm:" + tk.id},
                                          {"title", plan_item_title(tk.title)},
                                          {"desc", "awarded " + win_name},
                                          {"status", "running"},
                                          {"kind", "task"}});
                emit_plan_state(events, "checklist", "Firm awards", "", mission_plan_rev + 1000,
                                firm_items);
            }
            // Role seeds: lead + subcontractors PREFIX the sub-run prompt.
            std::vector<TeamMember> team =
                firm_team_members.empty() ? assemble_team(tk.req, tdom.domain) : firm_team_members;
            std::ostringstream seed;
            std::string lead_name, lead_role;
            for (const auto& tm : team) {
                const DisciplineRole& role = role_for_domain(tm.scope);
                if (tm.lead) {
                    lead_name = tm.firm_name;
                    lead_role = tm.role_title;
                    seed << "[LEAD " << tm.role_title << " @ " << tm.firm_name << "]\n"
                         << role.system_seed << "\n";
                } else if (tm.subcontractor) {
                    seed << "[SUB " << tm.role_title << " @ " << tm.firm_name << "]\n"
                         << role.system_seed << "\n";
                }
            }
            // Ledger context so far.
            if (!firm_ledger.empty())
                seed << "\n[FIRM LEDGER SO FAR]\n" << firm_ledger.dump() << "\n";
            firm_role_seed = seed.str();
            // Per-task model: disciplines.<disc> → lead|sub by TeamMember role → ambient.
            {
                const Discipline task_disc = discipline_for_domain(tdom.domain);
                const std::string dkey = eng_firm_discipline_key(task_disc);
                EngFirmModelEntry pick;
                auto dit = ef_models.disciplines.find(dkey);
                if (dit != ef_models.disciplines.end() && !dit->second.empty()) {
                    pick = dit->second;
                } else {
                    // Prefer the team member covering this task's domain.
                    bool is_sub = false;
                    bool found_role = false;
                    for (const auto& tm : team) {
                        if (discipline_for_domain(tm.scope) == task_disc) {
                            is_sub = tm.subcontractor && !tm.lead;
                            found_role = true;
                            break;
                        }
                    }
                    if (!found_role) {
                        // No domain match: lead if any lead member, else subcontractor.
                        for (const auto& tm : team) {
                            if (tm.lead) {
                                is_sub = false;
                                found_role = true;
                                break;
                            }
                        }
                        if (!found_role) {
                            for (const auto& tm : team) {
                                if (tm.subcontractor) {
                                    is_sub = true;
                                    break;
                                }
                            }
                        }
                    }
                    pick = is_sub ? ef_models.subcontractor : ef_models.lead;
                }
                json tdesc = resolve_eng_firm_provider(pick, ef_cfg);
                if (tdesc.is_object() && !tdesc.contains("error") && !tdesc.empty())
                    task_provider_cfg = tdesc;
            }
            events.emit("firm_comm",
                        json{{"kind", "firm_comm"},
                             {"from", "orchestrator"},
                             {"to", lead_name.empty() ? win_name : lead_name},
                             {"text", "Starting task '" + tk.title + "' (" + tk.capability + ")"}});
        }

        std::string status = "skipped";
        std::string summary;

        if (!kCaps.count(tk.capability)) {
            summary = "unknown capability '" + tk.capability + "'";
        } else if (tk.capability == "design" || tk.capability == "assembly") {
            const std::string sub_id = run_id + "-t" + std::to_string(i);
            events.emit("sub_run", json{{"kind", "sub_run"},
                                        {"id", sub_id},
                                        {"purpose", tk.title},
                                        {"parent_stage", "mission"},
                                        {"task_id", tk.id},
                                        {"status", "running"},
                                        {"execution_style", execution_style}});
            json sub_opts = options.is_object() ? options : json::object();
            sub_opts["run_id"] = sub_id;
            sub_opts["is_sub_run"] = true;
            sub_opts["run_depth"] = 1;
            // Inherit parent mission style so nested complexity/task_route stamps match.
            // Child units must NOT re-derive edit from recommend_structure.
            sub_opts["execution_style"] = execution_style;
            // Don't recurse into swarm/eng_firm planner paths for child units.
            sub_opts.erase("mode");
            sub_opts.erase("org");
            sub_opts.erase("execute_as");
            sub_opts.erase("corporate_force");
            sub_opts.erase("approved_plan");
            if (!firm_role_seed.empty()) sub_opts["firm_role_seed"] = firm_role_seed;
            // Interface injection for multi-unit mating: if this unit's title/request names a
            // part in the mission interface ledger, push measured-or-plan advisory_text.
            if (!mission_ifaces.interfaces.empty()) {
                // Prefer an explicit part name match from title, else try each interface participant.
                std::string part_hint = tk.title;
                std::string adv;
                for (const auto& iface : mission_ifaces.interfaces) {
                    for (const auto& pn : iface.between) {
                        if (iface_norm_name(tk.title).find(iface_norm_name(pn)) != std::string::npos ||
                            iface_norm_name(tk.req).find(iface_norm_name(pn)) != std::string::npos ||
                            iface_norm_name(tk.id) == iface_norm_name(pn)) {
                            part_hint = pn;
                            break;
                        }
                    }
                }
                adv = build_interface_advisory(part_hint, mission_ifaces, mission_built_ifaces);
                // If hint didn't participate, still try each between-name that looks like this unit.
                if (adv.empty()) {
                    for (const auto& iface : mission_ifaces.interfaces) {
                        for (const auto& pn : iface.between) {
                            std::string a =
                                build_interface_advisory(pn, mission_ifaces, mission_built_ifaces);
                            if (!a.empty() &&
                                (iface_norm_name(tk.title).find(iface_norm_name(pn)) !=
                                     std::string::npos ||
                                 iface_norm_name(tk.req).find(iface_norm_name(pn)) !=
                                     std::string::npos)) {
                                adv = a;
                                part_hint = pn;
                                break;
                            }
                        }
                        if (!adv.empty()) break;
                    }
                }
                if (!adv.empty()) {
                    std::string combined = adv;
                    if (sub_opts.contains("advisory_text") &&
                        sub_opts["advisory_text"].is_string()) {
                        const std::string prior = sub_opts["advisory_text"].get<std::string>();
                        if (!prior.empty()) combined = prior + "\n\n" + adv;
                    }
                    sub_opts["advisory_text"] = combined;
                    sub_opts["advisor"] = true;
                    events.emit("run_note",
                                json{{"note", "mission_interface_advisory"},
                                     {"task_id", tk.id},
                                     {"part", part_hint},
                                     {"chars", static_cast<int>(adv.size())}});
                }
            }
            json rr = run_assembly_design(tk.req, target_doc, task_provider_cfg, emit, sub_opts);
            status = rr.value("status", std::string("done"));
            summary = rr.value("stop_reason", std::string());
            events.emit("sub_run", json{{"kind", "sub_run"},
                                        {"id", sub_id},
                                        {"purpose", tk.title},
                                        {"parent_stage", "mission"},
                                        {"task_id", tk.id},
                                        {"status", status == "done" ? "done" : status},
                                        {"stop_reason", summary},
                                        {"execution_style", execution_style}});
            if (status != "done" && first_fail_reason.empty() && !summary.empty())
                first_fail_reason = summary;
            if (truthy_json(ojson(rr.value("created_doc", false)))) {
                current_doc = rr.value("doc_id", current_doc);
                created_doc = true;
                asm_path = rr.value("asm_path", asm_path);
                components = rr.value("components", components);
            } else {
                current_doc = target_doc;
            }
            final_doc = current_doc;
            revision = rr.value("revision", revision);
            // A partial assembly still produced a proven, persisted assembly
            // document; retain that fact so the mission terminal is partial
            // rather than collapsing the mixed outcome to failed.
            if (status == "done" || (status == "partial" && created_doc && components > 0)) {
                any_built = true;
                design_summary = doc_design_summary(current_doc);
                // Record measured interface for later dependent units.
                try {
                    json tree_now = doc::doc_tree(current_doc);
                    json tree_body =
                        tree_now.contains("tree") ? tree_now["tree"] : json::object();
                    json bb = json::object();
                    try {
                        bb = doc::doc_bbox(current_doc);
                    } catch (...) {
                    }
                    // Key under every interface part name that matches this task.
                    std::vector<std::string> keys;
                    keys.push_back(tk.title);
                    keys.push_back(tk.id);
                    for (const auto& iface : mission_ifaces.interfaces) {
                        for (const auto& pn : iface.between) {
                            if (iface_norm_name(tk.title).find(iface_norm_name(pn)) !=
                                    std::string::npos ||
                                iface_norm_name(tk.req).find(iface_norm_name(pn)) !=
                                    std::string::npos ||
                                iface_norm_name(tk.id) == iface_norm_name(pn)) {
                                keys.push_back(pn);
                            }
                        }
                    }
                    for (const auto& k : keys) {
                        if (k.empty()) continue;
                        MeasuredInterface mi = measure_interface_from_tree(k, tree_body, bb);
                        mi.doc_id = current_doc;
                        mission_built_ifaces[k] = mi;
                    }
                } catch (...) {
                }
            }
        } else if (tk.capability == "simulate_flow") {
            if (sim) {
                json sr = sim(current_doc, tk.req, json::object());
                sim_results.push_back(tree_to_ojson(sr));
                bool ok = sr.value("ok", false);
                status = ok ? "done" : "failed";
                summary = sr.value("answer", std::string());
            } else {
                summary = "no flow-sim backend wired";
            }
        } else if (tk.capability == "report") {
            verdict = build_verdict(provider, prompts_dir, budget, ctx_cap, request, design_summary,
                                    sim_results);
            reported = true;
            status = "done";
            summary = verdict;
        } else if (tk.capability == "reverse_engineer") {
            const std::string ref_doc = target_doc;
            std::string new_doc_id;
            try {
                json nd = doc::doc_new("part", "Reverse-Engineered");
                publish_doc_call(
                    "doc_new",
                    doc_publish_params({{"kind", "part"}, {"name", "Reverse-Engineered"}},
                                       options),
                    nd);
                new_doc_id = nd.value("doc_id", std::string());
            } catch (const std::exception& e) {
                summary = std::string("could not create reconstruction doc: ") + e.what();
            } catch (...) {
                summary = "could not create reconstruction doc";
            }
            if (!new_doc_id.empty()) {
                const std::string augmented =
                    tk.req +
                    "\n\nDocument " + ref_doc + " holds the ORIGINAL imported body — it is a "
                    "READ-ONLY reference; never edit it. You are building INTO this new, empty "
                    "document instead. Reproduce the reference geometry as a normal parametric "
                    "feature tree (sketches, extrudes, holes, fillets, patterns, ...), not a "
                    "single dumb import.";
                json sub_opts = options.is_object() ? options : json::object();
                sub_opts["run_id"] = run_id + "-t" + std::to_string(i);
                sub_opts["is_sub_run"] = true;
                sub_opts["run_depth"] = 1;
                sub_opts["execution_style"] = execution_style;
                sub_opts.erase("mode");
                sub_opts.erase("org");
                sub_opts.erase("execute_as");
                sub_opts.erase("corporate_force");
                json rr = run_assembly_design(augmented, new_doc_id, task_provider_cfg, emit, sub_opts);
                status = rr.value("status", std::string("done"));
                summary = rr.value("stop_reason", std::string());
                if (status != "done" && first_fail_reason.empty() && !summary.empty())
                    first_fail_reason = summary;
                current_doc = rr.value("doc_id", new_doc_id);
                final_doc = current_doc;
                revision = rr.value("revision", revision);
                if (status == "done") {
                    any_built = true;
                    design_summary = doc_design_summary(current_doc);
                }
                try {
                    json cmp =
                        doc::doc_eval_compare(json{{"doc_a", current_doc}, {"doc_b", ref_doc}});
                    re_report = json{{"new_doc_id", current_doc},
                                     {"ref_doc_id", ref_doc},
                                     {"compare", cmp}};
                    events.emit("reverse_engineer_compare", re_report);
                    if (cmp.value("degraded", false)) {
                        summary += " | compare degraded (near-identical geometry defeated the "
                                   "boolean); volume_ratio=" +
                                   std::to_string(cmp.value("volume_ratio", 0.0));
                    } else {
                        const double pct = cmp.value("match_pct", 0.0);
                        summary += " | match_pct=" + std::to_string(pct);
                        if (pct < 99.0)
                            summary += " — residual remains; iterate with add_features on " +
                                       current_doc + " and re-check via eval_compare(" +
                                       current_doc + ", " + ref_doc + ").";
                    }
                } catch (const std::exception& e) {
                    summary += std::string(" | compare failed: ") + e.what();
                } catch (...) {
                    summary += " | compare failed";
                }
            }
        } else {
            summary = "capability '" + tk.capability + "' is not yet wired into missions";
        }

        if (eng_firm_mode) {
            firm_ledger.push_back(json{{"task_id", tk.id},
                                       {"title", tk.title},
                                       {"status", status},
                                       {"tokens", task_bid_tokens},
                                       {"summary", summary}});
            events.emit("firm_comm",
                        json{{"kind", "firm_comm"},
                             {"from", awarded_firm_name.empty() ? "lead" : awarded_firm_name},
                             {"to", "orchestrator"},
                             {"text", "Finished '" + tk.title + "': " + status +
                                          (summary.empty() ? "" : (" — " + summary))}});
            // firm: item done/fail
            {
                json firm_items = json::array();
                firm_items.push_back(json{{"id", "firm:" + tk.id},
                                          {"title", plan_item_title(tk.title)},
                                          {"desc", summary.empty() ? status : summary},
                                          {"status", status == "done" ? "done" : "fail"},
                                          {"kind", "task"}});
                emit_plan_state(events, "checklist", "Firm awards", "", mission_plan_rev + 1000,
                                firm_items);
            }
        }

        if (status == "done") {
            any_ok = true;
        } else if (status != "abandoned" && status != "pending" && status != "skipped") {
            // Real unit failure (failed / model_failed / provider_error / …).
            if (first_fail_reason.empty()) {
                first_fail_reason = !summary.empty() ? summary : status;
            }
        }
        task_status[i] = status;
        events.emit("mission_task_result", json{{"index", static_cast<int>(i)},
                                                 {"capability", tk.capability},
                                                 {"id", tk.id},
                                                 {"status", status},
                                                 {"doc_id", current_doc},
                                                 {"summary", summary}});
        set_task_plan_status(i, status, summary);
        task_results.push_back(json{{"capability", tk.capability},
                                    {"title", tk.title},
                                    {"id", tk.id},
                                    {"status", status},
                                    {"doc_id", current_doc},
                                    {"summary", summary}});
    };

    // Drive waves.
    const int total_waves = static_cast<int>(waves.size());
    for (int wi = 0; wi < total_waves; ++wi) {
        if (run_ctl.cancelled()) break;
        const auto& wave = waves[wi];
        if (swarm_mode) {
            json wave_tasks = json::array();
            for (size_t idx : wave) {
                wave_tasks.push_back(json{{"id", tasks[idx].id},
                                          {"title", tasks[idx].title},
                                          {"status", "pending"}});
            }
            events.emit("swarm_wave", json{{"kind", "swarm_wave"},
                                           {"wave", wi + 1},
                                           {"total_waves", total_waves},
                                           {"tasks", wave_tasks}});
        }
        // Sequential units within the wave (v1).
        for (size_t idx : wave) {
            if (run_ctl.cancelled()) break;
            run_one_task(idx);
            // Update swarm_wave task status after each unit.
            if (swarm_mode) {
                json wave_tasks = json::array();
                for (size_t j : wave) {
                    wave_tasks.push_back(json{{"id", tasks[j].id},
                                              {"title", tasks[j].title},
                                              {"status", task_status[j]}});
                }
                events.emit("swarm_wave", json{{"kind", "swarm_wave"},
                                               {"wave", wi + 1},
                                               {"total_waves", total_waves},
                                               {"tasks", wave_tasks}});
            }
        }
    }

    if (!reported && sim_results.is_array() && !sim_results.empty())
        verdict = build_verdict(provider, prompts_dir, budget, ctx_cap, request, design_summary,
                                sim_results);

    // Tally task outcomes for partial vs done vs failed.
    int tasks_completed = 0;
    int tasks_failed = 0;
    int tasks_abandoned = 0;
    for (const auto& tr : task_results) {
        if (!tr.is_object()) continue;
        const std::string st = tr.value("status", std::string());
        if (st == "done")
            ++tasks_completed;
        else if (st == "abandoned")
            ++tasks_abandoned;
        else if (st != "pending" && st != "skipped")
            ++tasks_failed;
    }
    const bool any_failed = tasks_failed > 0;

    std::string stop_reason;
    std::string status;
    // Resolve failure reason once (used by partial + full-fail paths).
    auto resolve_fail_reason = [&]() -> std::string {
        if (!first_fail_reason.empty()) return first_fail_reason;
        for (auto it = task_results.rbegin(); it != task_results.rend(); ++it) {
            if (!it->is_object()) continue;
            const std::string s = it->value("summary", std::string());
            const std::string st = it->value("status", std::string());
            if (st != "done" && st != "pending" && st != "skipped" && !s.empty()) return s;
        }
        return "mission_failed";
    };

    if (run_ctl.cancelled()) {
        // User stop: sub-run already unwound via its own RunControl (cancel cascade
        // in cancel_run); remaining tasks were skipped above. Report honestly.
        status = "cancelled";
        stop_reason = "cancelled";
    } else if (any_failed && (any_built || any_ok || tasks_completed > 0)) {
        // Mixed outcomes: keep completed geometry, surface the first unit failure.
        // e.g. u1 design done + u2 assembly model_failed → partial / model_failed
        // (NOT done/built — that masked the failed primary goal).
        status = "partial";
        stop_reason = resolve_fail_reason();
    } else if (any_built && !any_failed) {
        status = "done";
        stop_reason = created_doc ? "assembled" : "built";
    } else if (any_ok && !any_failed) {
        status = "done";
        stop_reason = "done";
    } else {
        // Total failure: surface the real unit cause, never opaque "planned".
        status = "failed";
        stop_reason = resolve_fail_reason();
    }

    json fin = json{{"status", status},
                    {"stop_reason", stop_reason},
                    {"doc_id", final_doc},
                    {"verdict", verdict},
                    {"execution_style", execution_style},
                    {"tasks_completed", tasks_completed},
                    {"tasks_failed", tasks_failed},
                    {"tasks_abandoned", tasks_abandoned}};
    if (status == "partial" && !first_fail_reason.empty())
        fin["first_fail_reason"] = first_fail_reason;
    events.emit("mission_finished", fin);

    json result = json{{"run_id", run_id},
                       {"status", status},
                       {"stop_reason", stop_reason},
                       {"doc_id", final_doc},
                       {"revision", revision},
                       {"verdict", verdict},
                       {"tasks", task_results},
                       {"execution_style", execution_style},
                       {"tasks_completed", tasks_completed},
                       {"tasks_failed", tasks_failed},
                       {"tasks_abandoned", tasks_abandoned}};
    if (status == "partial" && !first_fail_reason.empty())
        result["first_fail_reason"] = first_fail_reason;
    if (created_doc) {
        result["created_doc"] = true;
        result["assembly"] = true;
        if (!asm_path.empty()) result["asm_path"] = asm_path;
        result["components"] = components;
    }
    if (!re_report.empty()) result["reverse_engineer"] = re_report;
    if (eng_firm_mode) result["firm_ledger"] = firm_ledger;
    return result;
}


}  // namespace agent
}  // namespace chudware
