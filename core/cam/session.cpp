// core/cam/session.cpp — the stateful CAM job document session + the cam_* RPC
// entry points the libchudware C ABI dispatches to. Mirrors chudware::ecad::session:
// a global store keyed by doc_id, a revision counter, bounded JSON-snapshot
// undo/redo, and an OpError-enveloped ops vocabulary.
#include "cam/session.h"

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <utility>
#include <mutex>
#include <unordered_map>

namespace fs = std::filesystem;

#include "cam/feeds.h"
#include "cam/model.h"
#include "cam/operations.h"
#include "cam/post.h"
#include "cam/stock_sim.h"
#include "cam/audit.h"
#include "cam/heightfield.h"
#include "miniz.h"

namespace chudware {
namespace cam {

namespace {

struct CamSession {
    Job job;
    long revision = 1;
    std::string path;
    std::vector<json> undo, redo;
    // cached generated toolpaths per setup id; invalidated on edit.
    std::unordered_map<std::string, std::vector<Toolpath>> cache;
    // Generation diagnostics must survive alongside cached paths, including refused ops.
    std::unordered_map<std::string, json> generation_warnings;
    // last simulate_setup remaining heightfield per setup id (rest stock / from_sim).
    std::unordered_map<std::string, HeightField> lastRemaining;
    bool stale = true;
};

std::mutex g_mtx;
std::unordered_map<std::string, CamSession> g_store;
long g_seq = 0;
thread_local bool g_locked_by_me = false;

std::string new_id(const char* prefix) {
    char b[48];
    std::snprintf(b, sizeof(b), "%s%ld", prefix, ++g_seq);
    return b;
}

CamSession& must(const std::string& doc_id) {
    auto it = g_store.find(doc_id);
    if (it == g_store.end())
        throw OpError("unknown_doc", "no such CAM document: " + doc_id, json{{"doc_id", doc_id}});
    return it->second;
}

// Flush the bounded undo stack to a per-doc telemetry journal next to the .step+ file.
// Called after snapshot() on every mutating op so the ops journal survives restart.
// Journal path: <doc_path>.cam_journal.jsonl (next to the .step+, NOT inside it).
// Format: one JSON per line, each entry is a revision snapshot.
// Best-effort: ignore all IO errors (mirrors the events.cpp pattern).
static void flush_cam_journal(const CamSession& s) {
    if (s.path.empty()) return;  // unsaved doc — nothing to persist yet
    try {
        fs::path journal = fs::path(s.path).replace_extension("").string() + ".cam_journal.jsonl";
        std::ofstream fh(journal, std::ios::binary | std::ios::trunc);
        if (!fh) return;
        for (const auto& snap : s.undo) {
            fh << snap.dump() << "\n";
        }
    } catch (...) {}  // best-effort
}

void snapshot(CamSession& s) {
    s.undo.push_back(s.job.to_json());
    if (s.undo.size() > 50) s.undo.erase(s.undo.begin());
    s.redo.clear();
    flush_cam_journal(s);
}

// compact tree view for the GUI / agent
json tree_of(const Job& job) {
    json setups = json::array();
    for (auto& su : job.setups) {
        json ops = json::array();
        for (auto& o : su.operations) {
            json op{{"id", o.id}, {"name", o.name}, {"type", to_string(o.type)},
                    {"tool_number", o.tool_number}, {"suppressed", o.suppressed}};
            // Per-op frame + untyped overflow (datum/fixture notes live in
            // `extra`) — additive so setup sheets / work instructions can show
            // the real per-op WCS instead of assuming the setup's.
            if (o.wcs) op["wcs"] = o.wcs->to_json();
            if (!o.extra.empty()) op["extra"] = o.extra;
            ops.push_back(std::move(op));
        }
        setups.push_back(json{{"id", su.id}, {"name", su.name},
                              {"machine", to_string(su.machine.kind)},
                              {"machine_detail", su.machine.to_json()},
                              {"stock", su.stock.to_json()}, {"wcs", su.wcs.to_json()},
                              {"operations", ops}});
    }
    json tools = json::array();
    for (auto& t : job.tools.tools)
        tools.push_back(json{{"number", t.number}, {"name", t.name}, {"type", to_string(t.type)},
                             {"diameter", t.diameter},
                             {"holder_diameter", t.holder_diameter},
                             {"holder_length", t.holder_length},
                             {"tool_material", t.tool_material}});
    return json{{"id", job.id}, {"name", job.name}, {"units", job.units}, {"post", job.post},
                {"tools", tools}, {"setups", setups}};
}

Setup* find_setup(Job& job, const std::string& id) {
    for (auto& s : job.setups) if (s.id == id) return &s;
    return nullptr;
}
Setup& first_or(Job& job, const std::string& id) {
    if (!id.empty()) { Setup* s = find_setup(job, id); if (s) return *s; }
    if (job.setups.empty()) throw OpError("invalid_op", "no setups in job", json::object());
    return job.setups.front();
}

OpContext make_ctx(const Setup& su, const ToolLibrary& lib, int tool_number) {
    OpContext ctx;
    ctx.machine = su.machine;
    ctx.stock = su.stock;
    ctx.wcs = su.wcs;
    ctx.material_id = su.stock.material.empty() ? "aluminum" : su.stock.material;
    const Tool* t = lib.find(tool_number);
    if (t) ctx.tool = *t;
    else { ctx.tool.number = tool_number; }
    return ctx;
}

// generate (and cache) all toolpaths for one setup
std::vector<Toolpath> generate_setup(const Job& job, const Setup& su, json* warnings) {
    std::vector<Toolpath> out;
    for (const Operation& op : su.operations) {
        if (op.suppressed) continue;
        OpContext ctx = make_ctx(su, job.tools, op.tool_number);
        GenResult r = generate(op, ctx);
        if (!r.ok || r.path.moves.empty()) {
            if (warnings) warnings->push_back(json{{"op", op.id},
                                                   {"error", r.ok ? "no toolpath moves generated" : r.error}});
            continue;
        }
        for (auto& w : r.warnings) if (warnings) warnings->push_back(json{{"op", op.id}, {"warning", w}});
        out.push_back(std::move(r.path));
    }
    return out;
}

const std::vector<Toolpath>& ensure_setup(CamSession& s, const Setup& su) {
    auto it = s.cache.find(su.id);
    if (it == s.cache.end() || s.stale) {
        json warnings = json::array();
        s.cache[su.id] = generate_setup(s.job, su, &warnings);
        s.generation_warnings[su.id] = std::move(warnings);
    }
    return s.cache.at(su.id);
}

bool append_generation_warnings(json& warnings, const json& generated,
                                json* errors = nullptr) {
    bool incomplete = false;
    for (const auto& entry : generated) {
        if (entry.contains("error")) {
            std::string note = "operation " + entry.value("op", std::string()) +
                               ": " + entry["error"].get<std::string>();
            warnings.push_back(note);
            if (errors) errors->push_back(note);
            incomplete = true;
        } else if (entry.contains("warning")) {
            warnings.push_back("operation " + entry.value("op", std::string()) +
                               ": " + entry["warning"].get<std::string>());
        }
    }
    return incomplete;
}

// ── ops vocabulary ──
void apply_one(CamSession& s, const json& op, json& id_map) {
    std::string kind = op.value("op", std::string());
    Job& job = s.job;
    if (kind == "set_job") {
        if (op.contains("name")) job.name = op["name"].get<std::string>();
        if (op.contains("units")) job.units = op["units"].get<std::string>();
        if (op.contains("post")) job.post = op["post"].get<std::string>();
    } else if (kind == "set_post") {
        job.post = op.value("post", job.post);
        if (op.contains("post_options")) job.post_options = op["post_options"];
    } else if (kind == "add_setup") {
        Setup su = op.contains("setup") ? Setup::from_json(op["setup"]) : Setup{};
        if (su.id.empty()) su.id = new_id("setup");
        if (op.contains("ref")) id_map[op["ref"].get<std::string>()] = su.id;
        job.setups.push_back(su);
    } else if (kind == "update_setup") {
        Setup* su = find_setup(job, op.value("setup_id", std::string()));
        if (!su) throw OpError("unknown_setup", "no such setup", op);
        if (op.contains("name")) su->name = op["name"].get<std::string>();
        if (op.contains("machine")) su->machine = Machine::from_json(op["machine"]);
        if (op.contains("stock")) su->stock = Stock::from_json(op["stock"]);
        if (op.contains("wcs")) su->wcs = WCS::from_json(op["wcs"]);
        if (op.contains("model_bodies")) {
            su->model_bodies.clear();
            for (auto& b : op["model_bodies"]) su->model_bodies.push_back(b.get<std::string>());
        }
    } else if (kind == "remove_setup") {
        std::string id = op.value("setup_id", std::string());
        job.setups.erase(std::remove_if(job.setups.begin(), job.setups.end(),
                                        [&](const Setup& x) { return x.id == id; }), job.setups.end());
    } else if (kind == "set_stock") {
        Setup& su = first_or(job, op.value("setup_id", std::string()));
        json stock_j = op.value("stock", json::object());
        if (stock_j.value("type", std::string()) == "from_sim") {
            std::string src = stock_j.value("setup_id", std::string());
            if (src.empty())
                throw OpError("invalid_op",
                              "from_sim stock requires setup_id of the source simulation", op);
            auto it = s.lastRemaining.find(src);
            if (it == s.lastRemaining.end())
                throw OpError("invalid_op",
                              "no simulation remaining stock for setup " + src +
                                  " — run cam_simulate first",
                              op);
            Stock st;
            st.type = StockType::FromSolid;
            st.solid = heightfield_to_mesh(it->second);
            st.material = stock_j.value("material", su.stock.material);
            if (!st.solid.verts.empty()) {
                st.min = st.solid.verts[0];
                st.max = st.solid.verts[0];
                for (const auto& v : st.solid.verts) {
                    st.min.x = std::min(st.min.x, v.x);
                    st.min.y = std::min(st.min.y, v.y);
                    st.min.z = std::min(st.min.z, v.z);
                    st.max.x = std::max(st.max.x, v.x);
                    st.max.y = std::max(st.max.y, v.y);
                    st.max.z = std::max(st.max.z, v.z);
                }
            }
            su.stock = std::move(st);
        } else {
            su.stock = Stock::from_json(stock_j);
        }
    } else if (kind == "set_wcs") {
        Setup& su = first_or(job, op.value("setup_id", std::string()));
        su.wcs = WCS::from_json(op.value("wcs", json::object()));
    } else if (kind == "set_machine") {
        Setup& su = first_or(job, op.value("setup_id", std::string()));
        su.machine = Machine::from_json(op.value("machine", json::object()));
    } else if (kind == "add_operation") {
        Setup& su = first_or(job, op.value("setup_id", std::string()));
        Operation o = Operation::from_json(op.value("operation", json::object()));
        if (o.id.empty()) o.id = new_id("op");
        if (op.contains("ref")) id_map[op["ref"].get<std::string>()] = o.id;
        su.operations.push_back(o);
    } else if (kind == "update_operation") {
        std::string oid = op.value("op_id", std::string());
        bool found = false;
        for (auto& su : job.setups)
            for (auto& o : su.operations)
                if (o.id == oid) {
                    json merged = o.to_json();
                    if (op.contains("operation")) merged.update(op["operation"]);
                    o = Operation::from_json(merged);
                    o.id = oid;
                    found = true;
                }
        if (!found) throw OpError("unknown_op", "no such operation: " + oid, op);
    } else if (kind == "remove_operation") {
        std::string oid = op.value("op_id", std::string());
        for (auto& su : job.setups)
            su.operations.erase(std::remove_if(su.operations.begin(), su.operations.end(),
                                               [&](const Operation& x) { return x.id == oid; }),
                                su.operations.end());
    } else if (kind == "reorder_operations") {
        Setup& su = first_or(job, op.value("setup_id", std::string()));
        std::vector<Operation> reordered;
        if (op.contains("order"))
            for (auto& idj : op["order"]) {
                std::string id = idj.get<std::string>();
                for (auto& o : su.operations) if (o.id == id) reordered.push_back(o);
            }
        if (reordered.size() == su.operations.size()) su.operations = reordered;
    } else if (kind == "add_tool") {
        Tool t = Tool::from_json(op.value("tool", json::object()));
        if (op.contains("ref")) id_map[op["ref"].get<std::string>()] = std::to_string(t.number);
        // replace if number exists
        bool repl = false;
        for (auto& e : job.tools.tools) if (e.number == t.number) { e = t; repl = true; }
        if (!repl) job.tools.tools.push_back(t);
    } else if (kind == "update_tool") {
        int num = op.value("number", 0);
        for (auto& e : job.tools.tools)
            if (e.number == num) {
                json merged = e.to_json();
                if (op.contains("tool")) merged.update(op["tool"]);
                e = Tool::from_json(merged);
            }
    } else if (kind == "remove_tool") {
        int num = op.value("number", 0);
        job.tools.tools.erase(std::remove_if(job.tools.tools.begin(), job.tools.tools.end(),
                                             [&](const Tool& x) { return x.number == num; }),
                              job.tools.tools.end());
    } else {
        throw OpError("invalid_op", "unknown CAM op: " + kind, op);
    }
}

}  // namespace

// ───────────────────────────────────────────────────────── public RPCs ──
json cam_new(const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession s;
    s.job.id = new_id("cam");
    s.job.name = params.value("name", std::string("CAM Job"));
    s.job.units = params.value("units", std::string("mm"));
    s.job.post = params.value("post", std::string("grbl"));
    std::string doc_id = s.job.id;
    g_store[doc_id] = std::move(s);
    return json{{"doc_id", doc_id}, {"revision", g_store[doc_id].revision}, {"tree", tree_of(g_store[doc_id].job)}};
}

json cam_apply_ops(const std::string& doc_id, long base_revision, const json& ops) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision", "document changed since base_revision",
                      json{{"base", base_revision}, {"current", s.revision}});
    snapshot(s);
    json id_map = json::object();
    const json& list = ops.is_array() ? ops : (ops.contains("ops") ? ops["ops"] : json::array());
    for (auto& op : list) apply_one(s, op, id_map);
    s.revision += 1;
    s.stale = true;
    s.cache.clear();
    s.generation_warnings.clear();
    return json{{"revision", s.revision}, {"id_map", id_map}, {"tree", tree_of(s.job)}};
}

json cam_undo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    if (s.undo.empty()) return json{{"revision", s.revision}, {"tree", tree_of(s.job)}, {"noop", true}};
    s.redo.push_back(s.job.to_json());
    s.job = Job::from_json(s.undo.back());
    s.undo.pop_back();
    s.revision += 1;
    s.cache.clear(); s.generation_warnings.clear(); s.stale = true;
    return json{{"revision", s.revision}, {"tree", tree_of(s.job)}};
}
json cam_redo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    if (s.redo.empty()) return json{{"revision", s.revision}, {"tree", tree_of(s.job)}, {"noop", true}};
    s.undo.push_back(s.job.to_json());
    s.job = Job::from_json(s.redo.back());
    s.redo.pop_back();
    s.cache.clear(); s.generation_warnings.clear(); s.stale = true;
    return json{{"revision", s.revision}, {"tree", tree_of(s.job)}};
}
json cam_tree(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    int nops = 0;
    for (auto& su : s.job.setups) nops += (int)su.operations.size();
    return json{{"revision", s.revision}, {"tree", tree_of(s.job)},
                {"stats", json{{"setups", s.job.setups.size()}, {"operations", nops},
                               {"tools", s.job.tools.tools.size()}}}};
}
bool cam_close(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    return g_store.erase(doc_id) > 0;
}
json cam_describe(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    return json{{"doc_id", doc_id}, {"name", s.job.name}, {"revision", s.revision},
                {"setups", s.job.setups.size()}, {"post", s.job.post}};
}

json cam_generate(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    std::string setup_id = params.value("setup_id", std::string());
    std::string op_id = params.value("op_id", std::string());
    json warnings = json::array();
    json out = json::array();
    s.cache.clear();
    s.generation_warnings.clear();
    for (Setup& su : s.job.setups) {
        if (!setup_id.empty() && su.id != setup_id) continue;
        std::vector<Toolpath> tps;
        if (!op_id.empty()) {
            for (const Operation& op : su.operations)
                if (op.id == op_id && !op.suppressed) {
                    OpContext ctx = make_ctx(su, s.job.tools, op.tool_number);
                    GenResult r = generate(op, ctx);
                    if (r.ok && !r.path.moves.empty()) tps.push_back(std::move(r.path));
                    else warnings.push_back(json{{"op", op.id},
                                                 {"error", r.ok ? "no toolpath moves generated" : r.error}});
                }
        } else {
            json per_setup = json::array();
            tps = generate_setup(s.job, su, &per_setup);
            for (const auto& entry : per_setup) warnings.push_back(entry);
            s.generation_warnings[su.id] = std::move(per_setup);
        }
        for (auto& tp : tps) out.push_back(tp.to_json());
        if (op_id.empty()) s.cache[su.id] = std::move(tps);
    }
    s.stale = !op_id.empty();
    return json{{"toolpaths", out}, {"warnings", warnings},
                {"stats", json{{"count", out.size()}}}};
}

json cam_simulate(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    std::string setup_id = params.value("setup_id", std::string());
    SimOptions so;
    if (params.contains("options")) {
        const json& o = params["options"];
        so.resolution = o.value("resolution", so.resolution);
        so.check_gouge = o.value("check_gouge", so.check_gouge);
        so.check_rapid = o.value("check_rapid", so.check_rapid);
        so.check_holder = o.value("check_holder", so.check_holder);
        so.gouge_tol = o.value("gouge_tol", so.gouge_tol);
        so.frames = o.value("frames", so.frames);
    }
    bool include_field = params.value("include_field", false);
    json results = json::array();
    for (Setup& su : s.job.setups) {
        if (!setup_id.empty() && su.id != setup_id) continue;
        const auto& tps = ensure_setup(s, su);
        TriMesh part;  // optional part mesh for gouge (supplied via params or empty)
        if (params.contains("part_mesh")) part = trimesh_from_json(params["part_mesh"]);
        SimResult r = simulate_setup(su, s.job.tools, tps, part, so);
        s.lastRemaining[su.id] = r.remaining;
        json rj = r.to_json(include_field);
        rj["setup_id"] = su.id;
        json& notes = rj["warnings"];
        rj["incomplete"] = append_generation_warnings(notes, s.generation_warnings.at(su.id));
        results.push_back(std::move(rj));
    }
    s.stale = false;
    return json{{"results", results}};
}

json cam_post(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    std::string dialect = params.value("dialect", s.job.post);
    std::string setup_id = params.value("setup_id", std::string());
    PostOptions po;
    if (params.contains("options")) {
        const json& o = params["options"];
        po.sequence_numbers = o.value("sequence_numbers", po.sequence_numbers);
        po.comments = o.value("comments", po.comments);
        po.metric = o.value("metric", po.metric);
        po.program_name = o.value("program_name", po.program_name);
        po.program_number = o.value("program_number", po.program_number);
        if (o.contains("overrides")) po.overrides = o["overrides"];
    }
    // Keep diagnostics paired with cached toolpaths even after an earlier generate/simulate.
    for (Setup& su : s.job.setups) ensure_setup(s, su);
    s.stale = false;
    json generated = json::array();
    if (!setup_id.empty()) {
        Setup* su = find_setup(s.job, setup_id);
        if (!su) throw OpError("unknown_setup", "no such setup", params);
        PostResult r = post_setup(*su, s.job.tools, dialect, s.cache.at(su->id), po);
        json result = r.to_json();
        result["incomplete"] = append_generation_warnings(result["warnings"],
                                                            s.generation_warnings.at(su->id));
        return result;
    }
    std::vector<std::vector<Toolpath>> per;
    for (auto& su : s.job.setups) {
        per.push_back(s.cache.at(su.id));
        for (const auto& entry : s.generation_warnings.at(su.id)) generated.push_back(entry);
    }
    // post_job posts with job.post; honor the per-call `dialect` override by posting against
    // a copy whose default dialect is the requested one.
    Job jd = s.job;
    jd.post = dialect;
    PostResult r = post_job(jd, per, po);
    json result = r.to_json();
    result["incomplete"] = append_generation_warnings(result["warnings"], generated);
    return result;
}

json cam_setup_sheet(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    std::string dialect = params.value("dialect", s.job.post);
    json generated = json::array();
    for (Setup& su : s.job.setups) ensure_setup(s, su);
    s.stale = false;
    std::vector<std::vector<Toolpath>> per;
    for (auto& su : s.job.setups) {
        per.push_back(s.cache.at(su.id));
        for (const auto& entry : s.generation_warnings.at(su.id)) generated.push_back(entry);
    }
    json result = setup_sheet(s.job, per, dialect).to_json();
    result["warnings"] = json::array();
    result["incomplete"] = append_generation_warnings(result["warnings"], generated);
    return result;
}

// ── stateless inline helpers (parity harness / MCP) ──
json cam_generate_inline(const json& params) {
    Job job = Job::from_json(params.value("job", json::object()));
    json warnings = json::array();
    json out = json::array();
    int si = params.value("setup_index", 0);
    std::string op_id = params.value("op_id", std::string());
    if (si < 0 || si >= (int)job.setups.size())
        throw OpError("invalid_op", "setup_index out of range", params);
    Setup& su = job.setups[si];
    for (const Operation& op : su.operations) {
        if (op.suppressed) continue;
        if (!op_id.empty() && op.id != op_id) continue;
        OpContext ctx = make_ctx(su, job.tools, op.tool_number);
        GenResult r = generate(op, ctx);
        if (r.ok && !r.path.moves.empty()) out.push_back(r.path.to_json());
        else warnings.push_back(json{{"op", op.id},
                                     {"error", r.ok ? "no toolpath moves generated" : r.error}});
    }
    return json{{"toolpaths", out}, {"warnings", warnings}};
}
json cam_post_inline(const json& params) {
    Job job = Job::from_json(params.value("job", json::object()));
    std::string dialect = params.value("dialect", job.post);
    std::vector<Toolpath> tps;
    if (params.contains("toolpaths"))
        for (auto& t : params["toolpaths"]) tps.push_back(Toolpath::from_json(t));
    if (job.setups.empty()) throw OpError("invalid_op", "job has no setups", params);
    PostOptions po;
    PostResult r = post_setup(job.setups.front(), job.tools, dialect, tps, po);
    return r.to_json();
}

// ── reference data ──
json cam_post_dialects() { return json{{"dialects", post_dialects_json()}}; }
json cam_materials() { return CuttingDB::builtin().to_json(); }
json cam_tool_types() {
    json a = json::array();
    for (int i = 0; i <= (int)ToolType::Probe; ++i) {
        ToolType t = (ToolType)i;
        a.push_back(json{{"id", to_string(t)}, {"class", tool_class(t)}});
    }
    return json{{"tool_types", a}};
}
json cam_op_types() {
    json a = json::array();
    for (int i = 0; i <= (int)OpType::Probe; ++i)
        a.push_back(json{{"id", to_string((OpType)i)}});
    return json{{"op_types", a}};
}

json cam_machines() { return MachineDB::builtin().to_json(); }

json cam_feeds(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    int tool_number = params.value("tool_number", 0);
    if (tool_number <= 0)
        throw OpError("invalid_op", "cam_feeds requires tool_number", params);
    const Tool* tool = s.job.tools.find(tool_number);
    if (!tool)
        throw OpError("unknown_tool", "no tool T" + std::to_string(tool_number) + " in job", params);

    std::string setup_id = params.value("setup_id", std::string());
    Setup* su = nullptr;
    if (!setup_id.empty()) {
        su = find_setup(s.job, setup_id);
        if (!su) throw OpError("unknown_setup", "no such setup", params);
    } else if (!s.job.setups.empty()) {
        su = &s.job.setups.front();
    }

    std::string material = params.value("material", std::string());
    if (material.empty())
        material = su ? su->stock.material : std::string("aluminum");

    const CuttingDB& db = CuttingDB::builtin();
    if (!db.find(material)) {
        auto ids = db.material_ids();
        std::string list;
        for (size_t i = 0; i < ids.size(); ++i) {
            if (i) list += ", ";
            list += ids[i];
        }
        throw OpError("unknown_material",
                      "unknown material '" + material + "'; valid ids: " + list,
                      json{{"material", material}, {"valid", ids}});
    }

    Machine mach = su ? su->machine : MachineDB::fallback();
    Operation op;
    op.tool_number = tool_number;
    // operation: "roughing"|"finishing" — mild bias via OpType when no concrete op.
    std::string operation = params.value("operation", std::string("roughing"));
    op.type = (operation == "finishing") ? OpType::Contour2D : OpType::Pocket2D;

    // Prefer a real op on the setup that uses this tool (for heights/overrides).
    if (su) {
        for (const Operation& cand : su->operations) {
            if (cand.tool_number == tool_number && !cand.suppressed) {
                op = cand;
                break;
            }
        }
    }

    if (op.extra.value("feed_mode", std::string("per_min")) == "per_rev" &&
        mach.kind != MachineKind::Lathe)
        throw OpError("invalid_op", "G95 feed requires a lathe machine", params);
    Feeds f = compute_feeds(mach, *tool, op, material, db);
    json result = f.to_json();
    if (op.extra.value("feed_mode", std::string("per_min")) == "per_rev") {
        double rpm = op.rpm > 0 ? op.rpm : f.rpm;
        if (rpm <= 0)
            throw OpError("invalid_op", "G95 feed requires positive spindle rpm", params);
        result["feed"] = f.feed / rpm;
        result["feed_mode"] = "per_rev";
    }
    return result;
}

json cam_audit(const std::string& doc_id, const json& params) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);

    std::string setup_id = params.value("setup_id", std::string());
    std::string dialect = params.value("dialect", s.job.post);
    std::string machine_id = params.value("machine", std::string());
    std::string gcode = params.value("gcode", std::string());
    std::string source = gcode.empty() ? "cam_post" : "supplied";

    // Resolve machine profile.
    const MachineDB& mdb = MachineDB::builtin();
    const MachineRecord* rec = machine_id.empty() ? nullptr : mdb.find_record(machine_id);
    Machine profile = rec ? rec->machine : MachineDB::fallback();
    std::string profile_id = rec ? rec->id : std::string("benchtop_vmc");
    if (rec) {
        // Prefer the record's post dialect when caller omitted dialect and job has default.
        if (!params.contains("dialect") && !rec->post_dialect.empty())
            dialect = rec->post_dialect;
    }

    // Ensure toolpaths and their diagnostics are drawn from the same generation.
    std::vector<Toolpath> tps;
    json generated = json::array();
    if (!setup_id.empty()) {
        Setup* su = find_setup(s.job, setup_id);
        if (!su) throw OpError("unknown_setup", "no such setup", params);
        tps = ensure_setup(s, *su);
        generated = s.generation_warnings.at(su->id);
        s.stale = false;
        // When no explicit machine profile, use setup machine limits as envelope.
        if (!rec) {
            profile = su->machine;
            profile_id = "setup";
        }
    } else {
        for (Setup& su : s.job.setups) ensure_setup(s, su);
        s.stale = false;
        for (auto& su : s.job.setups) {
            const auto& paths = s.cache.at(su.id);
            tps.insert(tps.end(), paths.begin(), paths.end());
            for (const auto& entry : s.generation_warnings.at(su.id)) generated.push_back(entry);
        }
        if (!rec && !s.job.setups.empty()) {
            profile = s.job.setups.front().machine;
            profile_id = "setup";
        }
    }

    std::string post_error;
    // Empty gcode → run the same post path as cam_post in-memory.
    if (gcode.empty()) {
        PostOptions po;
        if (params.contains("options")) {
            const json& o = params["options"];
            po.sequence_numbers = o.value("sequence_numbers", po.sequence_numbers);
            po.comments = o.value("comments", po.comments);
            po.metric = o.value("metric", po.metric);
            po.program_name = o.value("program_name", po.program_name);
            po.program_number = o.value("program_number", po.program_number);
            if (o.contains("overrides")) po.overrides = o["overrides"];
        }
        if (!setup_id.empty()) {
            Setup* su = find_setup(s.job, setup_id);
            if (!su) throw OpError("unknown_setup", "no such setup", params);
            PostResult r = post_setup(*su, s.job.tools, dialect, s.cache[su->id], po);
            gcode = r.gcode;
            if (!r.ok)
                post_error = r.error.empty() ? "post failed" : r.error;
        } else {
            std::vector<std::vector<Toolpath>> per;
            for (auto& su : s.job.setups) per.push_back(s.cache[su.id]);
            Job jd = s.job;
            jd.post = dialect;
            PostResult r = post_job(jd, per, po);
            gcode = r.gcode;
            if (!r.ok)
                post_error = r.error.empty() ? "post failed" : r.error;
        }
    }

    // Build a job view scoped to the setup when setup_id is set (stock/tools still full).
    Job audit_job = s.job;
    if (!setup_id.empty()) {
        Setup* su = find_setup(s.job, setup_id);
        if (su) {
            audit_job.setups.clear();
            audit_job.setups.push_back(*su);
        }
    }

    json result = audit_program(audit_job, gcode, dialect, profile, tps, source, profile_id);
    if (!post_error.empty()) {
        result["errors"].push_back("post: " + post_error);
        result["ok"] = false;
    }
    if (append_generation_warnings(result["warnings"], generated, &result["errors"]))
        result["ok"] = false;
    return result;
}

// ── save / open (.cam+ ZIP via miniz) ──
json cam_save(const std::string& doc_id, const std::string& path_in) {
    std::lock_guard<std::mutex> lk(g_mtx);
    CamSession& s = must(doc_id);
    std::string path = path_in.empty() ? s.path : path_in;
    if (path.empty()) throw OpError("io_error", "no path for cam_save", json::object());
    std::string data = s.job.to_json().dump();
    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_writer_init_file(&zip, path.c_str(), 0))
        throw OpError("io_error", "cannot open file for write: " + path, json::object());
    bool ok = mz_zip_writer_add_mem(&zip, "job.json", data.data(), data.size(), MZ_BEST_COMPRESSION);
    ok = ok && mz_zip_writer_add_mem(&zip, "MANIFEST", "chudware-cam/1\n", 12, MZ_BEST_COMPRESSION);
    mz_zip_writer_finalize_archive(&zip);
    mz_zip_writer_end(&zip);
    if (!ok) throw OpError("io_error", "failed to write .cam+ archive", json::object());
    s.path = path;
    return json{{"path", path}};
}
json cam_open(const std::string& path) {
    std::lock_guard<std::mutex> lk(g_mtx);
    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_file(&zip, path.c_str(), 0))
        throw OpError("io_error", "cannot open .cam+ file: " + path, json::object());
    size_t sz = 0;
    void* p = mz_zip_reader_extract_file_to_heap(&zip, "job.json", &sz, 0);
    if (!p) { mz_zip_reader_end(&zip); throw OpError("io_error", "no job.json in archive", json::object()); }
    std::string data((char*)p, sz);
    mz_free(p);
    mz_zip_reader_end(&zip);
    CamSession s;
    try {
        s.job = Job::from_json(json::parse(data));
    } catch (const std::exception& e) {
        throw OpError("parse_error", std::string("bad job.json: ") + e.what(), json::object());
    }
    if (s.job.id.empty()) s.job.id = new_id("cam");
    s.path = path;
    std::string doc_id = new_id("cam");  // fresh SESSION id (independent of the persistent job.id)
    g_store[doc_id] = std::move(s);
    return json{{"doc_id", doc_id}, {"revision", g_store[doc_id].revision}, {"tree", tree_of(g_store[doc_id].job)}};
}

bool force_release_cam_lock() {
    if (g_locked_by_me) { g_mtx.unlock(); g_locked_by_me = false; return true; }
    return false;
}

}  // namespace cam
}  // namespace chudware
