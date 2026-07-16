// core/agent/events.cpp — see events.h. Mirrors events.py.
#include "agent/events.h"

#include <atomic>
#include <cstdint>
#include <chrono>
#include <cstdio>
#include <fstream>

#include "agent/paths.h"
#include "agent/roles.h"  // prompt_hash → sha256 hex prefix for digests

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

std::string cap_raw(const std::string& text) {
    if (text.empty()) return "";
    if (static_cast<int>(text.size()) <= kRawCap) return text;
    return text.substr(0, kRawCap) + "\n...[raw truncated; full output in sidecar]";
}

std::string cap_text(const std::string& text, int n) {
    if (n <= 0 || text.empty()) return "";
    if (static_cast<int>(text.size()) <= n) return text;
    if (n == 1) return "…";
    return text.substr(0, static_cast<size_t>(n - 1)) + "…";
}

std::string new_span_id() {
    static std::atomic<uint64_t> seq{0};
    const uint64_t n = seq.fetch_add(1, std::memory_order_relaxed);
    const uint64_t t = static_cast<uint64_t>(
        std::chrono::steady_clock::now().time_since_epoch().count());
    char buf[20];
    // 16 hex chars (opaque span id, mirrors Python secrets.token_hex(8)).
    std::snprintf(buf, sizeof(buf), "%08x%08x",
                  static_cast<unsigned>(t & 0xffffffffu),
                  static_cast<unsigned>((n ^ (t >> 32)) & 0xffffffffu));
    return std::string(buf);
}

namespace {

// First 16 hex chars of sha256 (via prompt_hash's 12 + second pass).
std::string digest16(const std::string& text) {
    std::string a = prompt_hash(text);
    std::string b = prompt_hash(a + ":" + text);
    std::string h = a + b;
    if (h.size() > 16) h.resize(16);
    return h;
}

std::string derive_ops_target(const json& args) {
    auto one = [](const json& op) -> std::string {
        if (!op.is_object()) return "";
        for (const char* k : {"target", "face", "faces", "edge", "edges", "feature",
                              "sketch", "part", "part_name", "name", "body", "component",
                              "plane", "doc_id", "path", "id"}) {
            if (!op.contains(k)) continue;
            const auto& v = op[k];
            if (v.is_string() && !v.get_ref<const std::string&>().empty())
                return cap_text(v.get<std::string>(), 120);
            if (v.is_number_integer()) return std::to_string(v.get<long long>());
            if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
            if (v.is_number_float()) return std::to_string(v.get<double>());
            if (v.is_array() && !v.empty()) {
                std::string joined;
                int n = 0;
                for (const auto& el : v) {
                    if (n >= 4) break;
                    if (!joined.empty()) joined += ", ";
                    if (el.is_string()) joined += el.get<std::string>();
                    else if (el.is_number_integer()) joined += std::to_string(el.get<long long>());
                    else if (el.is_number()) joined += el.dump();
                    else continue;
                    ++n;
                }
                if (!joined.empty()) return cap_text(joined, 120);
            }
        }
        // type/op as last-resort label fragment
        if (op.contains("type") && op["type"].is_string())
            return cap_text(op["type"].get<std::string>(), 120);
        if (op.contains("op") && op["op"].is_string())
            return cap_text(op["op"].get<std::string>(), 120);
        return "";
    };
    if (args.is_array()) {
        if (args.empty()) return "";
        if (args.size() == 1) return one(args[0]);
        // Multi-op: first op type + count
        std::string t = one(args[0]);
        if (t.empty() && args[0].is_object()) {
            if (args[0].contains("op") && args[0]["op"].is_string())
                t = args[0]["op"].get<std::string>();
            else if (args[0].contains("type") && args[0]["type"].is_string())
                t = args[0]["type"].get<std::string>();
        }
        std::string s = t.empty() ? (std::to_string(args.size()) + " ops")
                                  : (t + " +" + std::to_string(args.size() - 1) + " more");
        return cap_text(s, 120);
    }
    if (args.is_object()) return one(args);
    return "";
}

std::string derive_tool_name(const json& args) {
    auto op_name = [](const json& op) -> std::string {
        if (!op.is_object()) return "";
        if (op.contains("op") && op["op"].is_string()) return op["op"].get<std::string>();
        if (op.contains("type") && op["type"].is_string()) return op["type"].get<std::string>();
        return "";
    };
    if (args.is_array()) {
        if (args.empty()) return "doc_apply_ops";
        std::string first = op_name(args[0]);
        if (first.empty()) return "doc_apply_ops";
        for (size_t i = 1; i < args.size(); ++i) {
            if (op_name(args[i]) != first) return "doc_apply_ops";
        }
        return first;
    }
    if (args.is_object()) {
        std::string n = op_name(args);
        return n.empty() ? "doc_apply_ops" : n;
    }
    return "doc_apply_ops";
}

}  // namespace

json emit_agent_thought(RunEvents& ev,
                        const std::string& text,
                        const std::string& model,
                        const std::string& stage,
                        int attempt,
                        const std::string& parent_id,
                        const std::string& actor) {
    if (text.empty()) return json(nullptr);
    json data = json::object();
    data["id"] = new_span_id();
    data["text"] = cap_text(text, kThoughtCap);
    data["node_id"] = stage.empty() ? "run" : stage;
    if (!parent_id.empty()) data["parent_id"] = parent_id;
    if (!actor.empty()) data["actor"] = actor;
    if (!model.empty()) data["model"] = model;
    if (!stage.empty()) data["stage"] = stage;
    if (attempt > 0) data["attempt"] = attempt;
    return ev.emit("agent.thought", data);
}

json emit_agent_tool_call(RunEvents& ev,
                          const std::string& tool_name,
                          const std::string& target,
                          const json& args,
                          bool ok,
                          int latency_ms,
                          const std::string& parent_id,
                          const std::string& actor) {
    std::string name = tool_name.empty() ? derive_tool_name(args) : tool_name;
    std::string tgt = target.empty() ? derive_ops_target(args) : target;
    std::string preview;
    try {
        preview = args.is_null() ? std::string() : args.dump();
    } catch (...) {
        preview = "";
    }
    preview = cap_text(preview, kArgsPreviewCap);
    json data = json::object();
    data["id"] = new_span_id();
    data["node_id"] = "apply";
    data["tool_name"] = name;
    data["target"] = tgt;
    data["args_digest"] = digest16(preview);
    data["result_digest"] = digest16(ok ? "ok" : "err");
    data["ok"] = ok;
    data["latency_ms"] = latency_ms;
    if (!preview.empty()) data["args_preview"] = preview;
    if (!parent_id.empty()) data["parent_id"] = parent_id;
    if (!actor.empty()) data["actor"] = actor;
    return ev.emit("agent.tool_call", data);
}

// ---------------------------------------------------------------------------
// plan_state helpers
// ---------------------------------------------------------------------------

namespace {

std::string trim_ws(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n\f\v");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n\f\v");
    return s.substr(a, b - a + 1);
}

std::string join_desc_parts(const std::vector<std::string>& parts) {
    std::string out;
    for (const auto& p : parts) {
        if (p.empty()) continue;
        if (!out.empty()) out += " · ";
        out += p;
    }
    return out;
}

std::string dims_to_desc(const json& dims) {
    if (dims.is_null()) return "";
    if (dims.is_string()) return dims.get<std::string>();
    if (dims.is_number() || dims.is_boolean()) return dims.dump();
    if (dims.is_object()) {
        std::string s;
        bool first = true;
        for (auto it = dims.begin(); it != dims.end(); ++it) {
            if (!first) s += ", ";
            first = false;
            s += it.key();
            s += "=";
            if (it.value().is_string()) s += it.value().get<std::string>();
            else s += it.value().dump();
        }
        return s;
    }
    if (dims.is_array()) return dims.dump();
    return "";
}

std::string features_to_desc(const json& feats) {
    if (!feats.is_array() || feats.empty()) return "";
    std::string s;
    for (size_t i = 0; i < feats.size(); ++i) {
        if (i) s += "; ";
        if (feats[i].is_string()) s += feats[i].get<std::string>();
        else s += feats[i].dump();
    }
    return s;
}

}  // namespace

std::string plan_item_title(const std::string& text, int max_words) {
    std::string t = trim_ws(text);
    if (t.empty() || max_words <= 0) return t;
    // Collapse internal whitespace while counting words.
    std::string out;
    int words = 0;
    bool in_word = false;
    for (char c : t) {
        const bool ws = (c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v');
        if (ws) {
            if (in_word) {
                in_word = false;
                if (words >= max_words) break;
            }
            continue;
        }
        if (!in_word) {
            in_word = true;
            ++words;
            if (words > max_words) break;
            if (!out.empty()) out.push_back(' ');
        }
        out.push_back(c);
    }
    return out;
}

json make_plan_state(const std::string& shape,
                     const std::string& title,
                     const std::string& summary,
                     int revision,
                     const json& items,
                     const json& edges) {
    json data = json::object();
    data["shape"] = shape;
    data["title"] = title;
    if (!summary.empty()) data["summary"] = summary;
    data["revision"] = revision;
    data["items"] = items.is_array() ? items : json::array();
    if (!edges.is_null()) data["edges"] = edges;
    return data;
}

json emit_plan_state(RunEvents& ev,
                     const std::string& shape,
                     const std::string& title,
                     const std::string& summary,
                     int revision,
                     const json& items,
                     const json& edges) {
    return ev.emit("plan_state",
                   make_plan_state(shape, title, summary, revision, items, edges));
}

json plan_state_items_from_build_plan(const json& plan, const std::string& default_status) {
    json items = json::array();
    if (!plan.is_object() || !plan.contains("parts") || !plan["parts"].is_array()) return items;
    int i = 0;
    for (const auto& p : plan["parts"]) {
        if (!p.is_object()) continue;
        std::string name = p.value("name", std::string("part"));
        if (name.empty()) name = "part";
        std::string purpose = p.value("purpose", std::string());
        // dimensions key (PlanInfo) — also accept dims
        std::string dim_s;
        if (p.contains("dimensions")) dim_s = dims_to_desc(p["dimensions"]);
        else if (p.contains("dims")) dim_s = dims_to_desc(p["dims"]);
        std::string feat_s;
        if (p.contains("features")) feat_s = features_to_desc(p["features"]);
        std::vector<std::string> desc_parts;
        if (!purpose.empty()) desc_parts.push_back(purpose);
        if (!dim_s.empty()) desc_parts.push_back(dim_s);
        if (!feat_s.empty()) desc_parts.push_back(feat_s);
        std::string id = name;
        // Keep ids unique if names collide.
        for (const auto& existing : items) {
            if (existing.is_object() && existing.value("id", std::string()) == id) {
                id = name + "-" + std::to_string(i);
                break;
            }
        }
        items.push_back(json{{"id", id},
                             {"title", plan_item_title(name)},
                             {"desc", join_desc_parts(desc_parts)},
                             {"status", default_status},
                             {"kind", "part"}});
        ++i;
    }
    return items;
}

json plan_state_items_from_steps(const std::vector<std::string>& steps,
                                 int running_index,
                                 bool all_done,
                                 int fail_index) {
    json items = json::array();
    for (size_t i = 0; i < steps.size(); ++i) {
        std::string status = "pending";
        if (all_done) {
            status = "done";
        } else if (fail_index >= 0 && static_cast<int>(i) == fail_index) {
            status = "fail";
        } else if (running_index >= 0) {
            if (static_cast<int>(i) < running_index) status = "done";
            else if (static_cast<int>(i) == running_index) status = "running";
            else status = "pending";
        }
        // After a fail, later steps stay pending; earlier stay done (set above).
        if (fail_index >= 0 && static_cast<int>(i) < fail_index) status = "done";
        const std::string& step = steps[i];
        items.push_back(json{{"id", "step-" + std::to_string(i)},
                             {"title", plan_item_title(step)},
                             {"desc", step},
                             {"status", status},
                             {"kind", "step"}});
    }
    return items;
}

json plan_state_items_from_objectives(const json& plan) {
    json items = json::array();
    if (!plan.is_object()) return items;
    const json* arr = nullptr;
    if (plan.contains("objectives") && plan["objectives"].is_array())
        arr = &plan["objectives"];
    else if (plan.contains("acceptance") && plan["acceptance"].is_array())
        arr = &plan["acceptance"];
    else if (plan.contains("success_criteria") && plan["success_criteria"].is_array())
        arr = &plan["success_criteria"];
    if (!arr) return items;
    int i = 0;
    for (const auto& o : *arr) {
        std::string title, desc, status = "pending";
        if (o.is_string()) {
            title = o.get<std::string>();
            desc = title;
        } else if (o.is_object()) {
            if (o.contains("title") && o["title"].is_string()) title = o["title"].get<std::string>();
            else if (o.contains("name") && o["name"].is_string()) title = o["name"].get<std::string>();
            else if (o.contains("text") && o["text"].is_string()) title = o["text"].get<std::string>();
            else if (o.contains("objective") && o["objective"].is_string())
                title = o["objective"].get<std::string>();
            if (o.contains("desc") && o["desc"].is_string()) desc = o["desc"].get<std::string>();
            else if (o.contains("description") && o["description"].is_string())
                desc = o["description"].get<std::string>();
            else
                desc = title;
            if (o.contains("status") && o["status"].is_string())
                status = o["status"].get<std::string>();
        } else {
            continue;
        }
        title = trim_ws(title);
        if (title.empty()) continue;
        std::string id = "obj-" + std::to_string(i);
        if (o.is_object() && o.contains("id") && o["id"].is_string() &&
            !o["id"].get_ref<const std::string&>().empty())
            id = o["id"].get<std::string>();
        items.push_back(json{{"id", id},
                             {"title", plan_item_title(title)},
                             {"desc", desc.empty() ? title : desc},
                             {"status", status},
                             {"kind", "objective"}});
        ++i;
    }
    return items;
}

json plan_state_items_from_mission(const json& tasks, const std::string& phase) {
    json items = json::array();
    if (!tasks.is_array()) return items;
    int i = 0;
    for (const auto& t : tasks) {
        if (!t.is_object()) continue;
        std::string title = t.value("title", std::string());
        std::string cap = t.value("capability", std::string());
        if (title.empty()) title = cap.empty() ? ("Task " + std::to_string(i + 1)) : cap;
        std::string status = t.value("status", std::string("pending"));
        // Map mission result statuses onto plan_state vocabulary.
        if (status == "done" || status == "ok" || status == "pass") status = "done";
        else if (status == "failed" || status == "fail" || status == "error") status = "fail";
        else if (status == "skipped" || status == "skip") status = "abandoned";
        else if (status == "running" || status == "started") status = "running";
        else if (status.empty()) status = "pending";
        std::string desc = cap;
        if (t.contains("summary") && t["summary"].is_string()) {
            const std::string& sum = t["summary"].get_ref<const std::string&>();
            if (!sum.empty()) desc = desc.empty() ? sum : (cap + " · " + sum);
        } else if (t.contains("req") && t["req"].is_string()) {
            desc = t["req"].get<std::string>();
        }
        json item = json{{"id", "mission-" + std::to_string(i)},
                         {"title", plan_item_title(title)},
                         {"desc", desc.empty() ? title : desc},
                         {"status", status},
                         {"kind", "task"}};
        if (!phase.empty()) item["phase"] = phase;
        if (!cap.empty()) item["meta"] = json{{"capability", cap}};
        items.push_back(std::move(item));
        ++i;
    }
    return items;
}

fs::path run_dir(const std::string& run_id) {
    fs::path d = paths_cache() / "runs" / run_id;
    std::error_code ec;
    fs::create_directories(d, ec);  // best-effort, like Python mkdir(exist_ok=True).
    return d;
}

RunEvents::RunEvents(const std::string& run_id) : run_id_(run_id) {
    dir_ = run_dir(run_id);
    jsonl_ = dir_ / "events.jsonl";
    // Truncate any stale file for this run id.
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::trunc);
    // (best-effort; ignore failure to match Python's try/except pass)
}

RunEvents::RunEvents(const std::string& run_id, Callback cb)
    : run_id_(run_id), cb_(std::move(cb)) {
    dir_ = run_dir(run_id);
    jsonl_ = dir_ / "events.jsonl";
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::trunc);
}

json RunEvents::emit(const std::string& kind, const json& data) {
    // ev = {"kind":kind, "run_id":run_id, **data}. Python dict preserves
    // insertion order; the gate compares PARSED objects so key order is moot.
    json ev = json::object();
    ev["kind"] = kind;
    ev["run_id"] = run_id_;
    if (data.is_object())
        for (auto it = data.begin(); it != data.end(); ++it) ev[it.key()] = it.value();
    // Persist first (source of truth for replay), then surface live (best-effort).
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::app);
    if (fh) fh << ev.dump() << "\n";
    if (cb_) {
        try {
            cb_(ev);
        } catch (...) {
            // match Python try/except pass — a sink failure never breaks the run.
        }
    }
    return ev;
}

std::string RunEvents::write_sidecar(const std::string& stage, int attempt,
                                     const std::string& raw) {
    fs::path p = dir_ / ("attempt_" + stage + "_" + std::to_string(attempt) + ".txt");
    std::ofstream fh(p, std::ios::binary | std::ios::trunc);
    if (fh) fh << raw;
    return p.string();
}

// ---------------------------------------------------------------------------
// Telemetry helper implementations.
// ---------------------------------------------------------------------------

json emit_ai_artifact(RunEvents& ev,
                      const std::string& artifact_id,
                      const std::string& insertion_point,
                      const json& model_info,
                      const std::string& prompt_hash,
                      const std::string& input_ref,
                      const std::string& output_ref,
                      int base_revision,
                      const json& verifier_scores,
                      int64_t latency_ms,
                      const std::string& consent) {
    // For "meta" consent the caller is responsible for passing empty strings
    // for input_ref and output_ref so that path references are not persisted.
    json data;
    data["artifact_id"]     = artifact_id;
    data["insertion_point"] = insertion_point;
    data["model"]           = model_info;
    data["prompt_hash"]     = prompt_hash;
    data["input_ref"]       = input_ref;
    data["output_ref"]      = output_ref;
    data["base_revision"]   = base_revision;
    data["verifier_scores"] = verifier_scores;
    data["latency_ms"]      = latency_ms;
    data["consent"]         = consent;
    return ev.emit("ai_artifact", data);
}

json emit_ai_artifact_action(RunEvents& ev,
                             const std::string& artifact_id,
                             const std::string& action,
                             int revision,
                             const std::string& edit_ops_ref,
                             int undo_within_ops,
                             int64_t dwell_ms,
                             const std::string& note,
                             const std::string& consent) {
    // For "meta" consent the caller is responsible for passing an empty string
    // for edit_ops_ref so that path references are not persisted.
    json data;
    data["artifact_id"]    = artifact_id;
    data["action"]         = action;
    data["revision"]       = revision;
    data["edit_ops_ref"]   = edit_ops_ref;   // optional; pass "" when absent
    data["undo_within_ops"] = undo_within_ops;
    data["dwell_ms"]       = dwell_ms;
    data["note"]           = note;           // optional; pass "" when absent
    data["consent"]        = consent;
    return ev.emit("ai_artifact_action", data);
}

json emit_doc_semantic_diff(RunEvents& ev,
                            const std::string& artifact_id,
                            int from_revision,
                            int to_revision,
                            const std::string& diff_ref,
                            const std::string& consent) {
    json data;
    data["artifact_id"]   = artifact_id;
    data["from_revision"] = from_revision;
    data["to_revision"]   = to_revision;
    data["diff_ref"]      = diff_ref;
    data["consent"]       = consent;
    return ev.emit("doc_semantic_diff", data);
}

}  // namespace agent
}  // namespace aicad
