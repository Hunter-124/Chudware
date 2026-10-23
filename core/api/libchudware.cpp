// libchudware C ABI implementation — see libchudware.h.
//
// Dispatches string-in/string-out JSON calls to the C++ core. Today it serves:
//   ping             liveness + ABI echo (no kernel work; used by the host to
//                    confirm the DLL loaded and its OCCT deps resolved).
//   doc_compile_cpp  DIAGNOSTIC compile: takes a full document feature tree +
//                    env, runs chudware::doc::compile_document, publishes the
//                    surviving bodies' AICM (aicm_handoff, or an explicit
//                    params["aicm_out"] file), and returns the per-feature statuses
//                    + body count + the AICM path. This is the strangler's
//                    parity probe (Step 2 of the strangler migration): the host can
//                    feed the SAME tree it sends to the Python worker and diff the
//                    results, WITHOUT changing the production Python path.
//
// Everything here is exception-safe: any throw becomes a nonzero return with an
// {"error":...} payload so a bug in the C++ core can never crash the host.

#include "api/libchudware.h"

#include "util/aicm_handoff.h"
#include "version_info.h"  // CMake-configured CHUDWARE_HOSTD_BUILD_ID (shared with chudware-hostd)

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <exception>
#include <regex>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

extern "C" void randombytes(unsigned char*, unsigned long long);

#include <nlohmann/json.hpp>

#ifdef _WIN32
// For the dispatch-boundary structured-exception (SEH) guard that converts a hardware
// fault (access violation) into a clean JSON error + orphaned-lock recovery, instead of
// leaving the session mutex held => "resource deadlock would occur" on the next call.
#define NOMINMAX          // <windows.h>'s min/max macros clobber std::min/std::max (used below); no-op on Linux
#include <windows.h>
#include <excpt.h>
#endif

#include "agent/ask.h"
#include "agent/clarify.h"
#include "agent/config.h"
#include "agent/copilot_modes.h"
#include "agent/events.h"
#include "agent/machine.h"
#include "agent/micro.h"
#include "agent/pm_assist.h"
#include "agent/paths.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/http.h"
#include "agent/roles.h"
#include "sim/flow_run.h"
#include "agent/mission.h"
#include "agent/run.h"
#include "agent/state.h"
#include "doc/document.h"
#include "doc/export2d.h"
#include "doc/drawing_session.h"
#include "doc/session.h"
#include "doc/stepplus.h"
#include "doc/formats.h"
#include "doc/validate.h"
#include "doc/op_registry.h"
#include "ecad/op_registry.h"
#include "parts/parts.h"
#include "prof/sampler.h"
#include "prof/phases.h"
#include "ecad/autoroute.h"
#include "ecad/recipes.h"
#include "ecad/schema.h"
#include "ecad/session.h"
#include "ecad/symlib.h"
#include "cam/session.h"
#include "cam/extract.h"
#include "geom/aicm.h"
#include "geom/ecad_3d.h"
#include "parts/parts.h"
#include "prof/phases.h"
#include "prof/sampler.h"
#include "sim/flow_run.h"
#include "sim/pipe_flow.h"
#ifdef CHUDWARE_WITH_POLYMESH
#include "sim/fea_adaptive.h"
#include "sim/fea_fatigue.h"
#include "sim/fea_run.h"
#include "sim/topopt_run.h"
#include "sim/mesher.h"
#endif
#include "dfm/process_profiles.h"
#include "ecad/container.h"
#include "firmware/firmware_rpc.h"
#include "hydraulic/hydraulic.h"
#include "materials/materials.h"
#include "mechanical/mechanical.h"
#include "parts/place.h"
#include "piping/piping.h"
#include "piping/fluid_properties.h"
#include "piping/solve.h"
#include "sim/fea_modal.h"
#include "sim/fea_static.h"
#include "sim/lbm.h"
#include "sim/netlist.h"
#include "sim/numpy_rng.h"
#include "sim/overlay.h"
#include "sim/sim_cancel.h"
#include "sim/progress.h"
#include "firmware/firmware_rpc.h"
#include "sourcing/sourcing.h"
#include "ecad/container.h"
#include "hydraulic/hydraulic.h"
#include "piping/piping.h"
#include "structural/structural_rpc.h"
#include "tolerance/stackup.h"
#include "mechanical/mechanical.h"
#include "materials/materials.h"
#include "dfm/process_profiles.h"
#include "parts/place.h"
#include "sim/spice.h"

using nlohmann::json;
namespace chudware::doc {
nlohmann::json export_drawing_selected(const std::string& doc_id,
                                       const std::string& path,
                                       const std::string& fmt,
                                       const nlohmann::json& sheet);
}


namespace {

// malloc a NUL-terminated copy of `s` so the host frees it with chudware_free()
// (std::free). Returns nullptr only on allocation failure.
char* dup_cstr(const std::string& s) {
    char* p = static_cast<char*>(std::malloc(s.size() + 1));
    if (!p) return nullptr;
    std::memcpy(p, s.data(), s.size());
    p[s.size()] = '\0';
    return p;
}

// Read a string field tolerating absent or null (nlohmann value() throws on null).
template<typename JsonT>
std::string jstr(const JsonT& j, const char* key, const std::string& d = std::string()) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return d;
    return it->template get<std::string>();
}

// Emit `j` into *out_json (heap) and return `rc`. On allocation failure returns 1
// with an {"error":"out of memory"} payload (or nullptr if even that fails).
int emit(json&& j, char** out_json, int rc) {
    if (chudware::sim::cell_request_revoked()) {
        j = json{{"code", "revoked"}, {"message", "principal revoked"}};
        rc = 1;
    }
    if (!out_json) return rc;
    char* p = dup_cstr(j.dump());
    if (p) {
        *out_json = p;
        return rc;
    }
    *out_json = dup_cstr(R"({"error":"out of memory"})");
    return 1;
}

int emit_error(const std::string& msg, char** out_json) {
    return emit(json{{"error", msg}}, out_json, 1);
}

struct NativeRunOwner {
    std::string workspace;
    std::string actor;
    std::int64_t epoch = 0;
    bool active = true;
};
std::mutex nativeRunOwnersMutex;
std::map<std::string, NativeRunOwner> nativeRunOwners;
std::map<std::string, std::map<std::string, std::int64_t>> nativeRevokeThrough;

bool safeRunComponent(const std::string& id) {
    if (id.empty() || id.size() > 192 || id == "." || id == "..") return false;
    return std::all_of(id.begin(), id.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
               (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    });
}

bool nativeRunOwned(const std::string& id, bool allowManager) {
    const auto* principal = chudware::sim::current_cell_principal();
    if (!principal) return true;
    std::lock_guard<std::mutex> lk(nativeRunOwnersMutex);
    for (const auto& [root, owner] : nativeRunOwners) {
        if (id != root && !(id.size() > root.size() &&
            id.compare(0, root.size(), root) == 0 && id[root.size()] == '-')) continue;
        return owner.workspace == principal->workspaceKey &&
            (owner.actor == principal->actorKey || (allowManager && principal->manager));
    }
    return false;
}

class NativeRunOwnerScope {
public:
    NativeRunOwnerScope(const std::string& method, json& params) {
        const auto* principal = chudware::sim::current_cell_principal();
        if (!principal || (method != "agent_run" && method != "agent_run_design" &&
            method != "agent_run_micro" && method != "agent_mission_v1")) return;
        std::lock_guard<std::mutex> lk(nativeRunOwnersMutex);
        auto workspace = nativeRevokeThrough.find(principal->workspaceKey);
        if (workspace != nativeRevokeThrough.end()) {
            auto actor = workspace->second.find(principal->actorKey);
            if (actor != workspace->second.end() && principal->epoch <= actor->second)
                throw chudware::sim::CellRevokedError();
        }
        // Tenant IDs are opaque server-minted handles, never caller-selected paths.
        unsigned char bytes[16];
        randombytes(bytes, sizeof(bytes));
        static constexpr char hex[] = "0123456789abcdef";
        id_ = "run-";
        for (unsigned char b : bytes) {
            id_.push_back(hex[b >> 4]);
            id_.push_back(hex[b & 15]);
        }
        if (!params.contains("options") || !params["options"].is_object())
            params["options"] = json::object();
        params["options"]["run_id"] = id_;
        // Register the actual shared control under the ownership lock. A revoke
        // before run_design/run_mission starts flips the flag they later adopt.
        chudware::agent::RunControl control;
        chudware::agent::register_run_control(id_, control);
        try {
            nativeRunOwners.emplace(id_, NativeRunOwner{
                principal->workspaceKey, principal->actorKey, principal->epoch});
        } catch (...) {
            chudware::agent::unregister_run_control(id_);
            throw;
        }
    }
    ~NativeRunOwnerScope() {
        if (id_.empty()) return;
        std::lock_guard<std::mutex> lk(nativeRunOwnersMutex);
        chudware::agent::unregister_run_control(id_);
        nativeRunOwners.at(id_).active = false;
    }
private:
    std::string id_;
};

json cancelOwnNativeRuns(std::optional<std::int64_t> revokeThrough = std::nullopt) {
    const auto& principal = *chudware::sim::current_cell_principal();
    std::vector<std::string> ids;
    {
        std::lock_guard<std::mutex> lk(nativeRunOwnersMutex);
        if (revokeThrough) {
            auto& actors = nativeRevokeThrough[principal.workspaceKey];
            auto [it, inserted] = actors.emplace(principal.actorKey, *revokeThrough);
            if (!inserted) it->second = std::max(it->second, *revokeThrough);
        }
        for (const auto& [id, owner] : nativeRunOwners)
            if (owner.active && owner.workspace == principal.workspaceKey &&
                owner.actor == principal.actorKey &&
                (!revokeThrough || owner.epoch <= *revokeThrough)) ids.push_back(id);
    }
    int count = 0;
    for (const auto& id : ids)
        if (chudware::agent::cancel_run(id).value("cancelled", false)) ++count;
    return json{{"ok", true}, {"cancelled", count != 0}, {"count", count}};
}


// --- sampling-profiler scoping ------------------------------------------------------
// The profiler samples only the thread that is registered as the "work thread" (see
// prof::set_target). To keep samples focused on MODELING — and to keep simulation/ECAD
// out, per the request that spawned this — we register the calling thread only around the
// RPCs that actually do geometry/document work. In "all" mode every call is registered.
bool prof_is_modeling_method(const std::string& m) {
    // The doc session RPCs are where the OCCT B-rep work (extrude/fillet/boolean/pattern/
    // tessellate) and the sketch solver run. Renders re-tessellate, so include them. The
    // INTERACTIVE read paths (open a doc, resolve a viewport pick, expand a face into its edges,
    // push-pull-probe a face) also do real kernel work — they were the slow paths reported when
    // clicking a surface / opening a big assembly — so sample them too by default.
    static const std::array<const char*, 23> kModeling = {{
        "doc_compile_cpp", "doc_apply_ops", "doc_dry_run_ops", "doc_rebuild", "doc_activate",
        "doc_solve_mates", "doc_tree", "doc_new", "doc_checkpoint", "doc_undo",
        "doc_redo", "doc_render_png", "doc_render_view", "doc_render_contact_sheet",
        "doc_render_diff", "doc_view_metrics",
        "doc_export", "doc_import", "asm_world_bodies",
        "doc_open", "doc_resolve_pick", "doc_face_edges", "doc_push_pull"}};
    for (const char* k : kModeling)
        if (m == k) return true;
    return false;
}

// Default output directory for env-var auto-profile reports: %APPDATA%/Chudware/prof
// (matches the handoff/cache convention used elsewhere), falling back to the temp dir.
std::filesystem::path prof_default_out_dir() {
    std::error_code ec;
    const char* appdata = std::getenv("APPDATA");
    std::filesystem::path base;
    if (appdata && appdata[0]) {
        base = std::filesystem::path(appdata) / "Chudware" / "prof";
    } else {
        base = std::filesystem::temp_directory_path(ec) / "Chudware-prof";
    }
    std::filesystem::create_directories(base, ec);
    return base;
}

// One-time env-var auto-profile: CHUDWARE_PROFILE=1 starts the sampler (modeling mode) for
// the whole process; on exit a report (folded + speedscope + a flat .txt) is written under
// CHUDWARE_PROFILE_OUT (default %APPDATA%/Chudware/prof). CHUDWARE_PROFILE_HZ tunes the rate.
// Zero-touch: just launch the app/MCP with the var set, model normally, close it.
std::once_flag g_prof_env_once;
void prof_env_init_once() {
    std::call_once(g_prof_env_once, []() {
        const char* on = std::getenv("CHUDWARE_PROFILE");
        if (!on || on[0] == '\0' || on[0] == '0') return;
        unsigned hz = 1000;
        if (const char* h = std::getenv("CHUDWARE_PROFILE_HZ")) {
            int v = std::atoi(h);
            if (v > 0) hz = static_cast<unsigned>(v);
        }
        unsigned interval_us = hz > 0 ? (1000000u / hz) : 1000u;
        if (!chudware::prof::start(interval_us, "modeling")) return;
        std::atexit([]() {
            if (!chudware::prof::active()) return;
            chudware::prof::stop();
            std::filesystem::path dir;
            if (const char* o = std::getenv("CHUDWARE_PROFILE_OUT")) {
                std::error_code ec;
                dir = std::filesystem::path(o);
                std::filesystem::create_directories(dir, ec);
            } else {
                dir = prof_default_out_dir();
            }
            json opts = {
                {"top", 60},
                {"write_folded", (dir / "modeling.folded").string()},
                {"write_speedscope", (dir / "modeling.speedscope.json").string()}};
            chudware::prof::report(opts);
        });
    });
}

// Flatten a JSON numeric array — accepts nested ([[x,y,z],...]) OR already-flat
// ([x,y,z,...]) — into a flat double vector. Used by the overlay/map handlers.
std::vector<double> flat_doubles(const json& j) {
    std::vector<double> v;
    if (!j.is_array()) return v;
    for (const auto& x : j) {
        if (x.is_array()) {
            for (const auto& c : x) v.push_back(c.get<double>());
        } else {
            v.push_back(x.get<double>());
        }
    }
    return v;
}

// Flatten a JSON int array (flat or nested) to a vector<int> — the tet-connectivity
// shape the boundary-surface overlay needs (mirrors flat_doubles for ints).
std::vector<int> flat_ints(const json& j) {
    std::vector<int> v;
    if (!j.is_array()) return v;
    for (const auto& x : j) {
        if (x.is_array()) {
            for (const auto& c : x) v.push_back(c.get<int>());
        } else {
            v.push_back(x.get<int>());
        }
    }
    return v;
}

// Hex-encode a byte blob (lowercase) for the chunk-bytes diagnostic handlers.
std::string to_hex(const std::vector<uint8_t>& b) {
    static const char* hexd = "0123456789abcdef";
    std::string s;
    s.reserve(b.size() * 2);
    for (uint8_t c : b) {
        s.push_back(hexd[c >> 4]);
        s.push_back(hexd[c & 0xF]);
    }
    return s;
}

// Publish the compile AICM. An explicit params["aicm_out"] (caller-pinned output file)
// is written verbatim; otherwise the bytes go through aicm_handoff, whose shm object or
// temp-file fallback is scoped by this process id. Empty on hard failure — never a
// second, unscoped temp file.
std::string publish_or_write_aicm(const json& params, const std::vector<uint8_t>& aicm) {
    if (params.contains("aicm_out") && params["aicm_out"].is_string()) {
        std::filesystem::path target = params["aicm_out"].get<std::string>();
        std::ofstream f(target, std::ios::binary | std::ios::trunc);
        if (!f) return {};
        f.write(reinterpret_cast<const char*>(aicm.data()),
                static_cast<std::streamsize>(aicm.size()));
        f.close();
        if (!f) return {};
        return std::filesystem::absolute(target).string();
    }
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    return chudware::aicm_handoff::publish("compile_" + std::to_string(n), aicm);
}

// doc_compile_cpp: compile a document tree to AICM + statuses (diagnostic).
int handle_compile(const json& params, char** out_json) {
    // The tree may arrive under "doc" (the {"features":[...]} shape compile_document
    // wants) or, for convenience, the host's tree may be passed directly. Accept
    // either: if there's a top-level "features", treat params itself as the doc.
    json doc;
    if (params.contains("doc")) {
        doc = params["doc"];
    } else if (params.contains("features")) {
        doc = params;
    } else {
        return emit_error("doc_compile_cpp: params need a 'doc' object (or a top-level 'features')", out_json);
    }
    json env = params.contains("env") ? params["env"] : json::object();

    chudware::doc::CompileResult res = chudware::doc::compile_document(doc, env);

    json statuses = json::array();
    for (const auto& s : res.statuses) {
        statuses.push_back(json{
            {"id", s.id},
            {"type", s.type},
            {"ok", s.ok},
            {"error", s.error},
            {"warning", s.warning},
        });
    }

    std::string aicm_path;
    if (!res.aicm.empty()) {
        aicm_path = publish_or_write_aicm(params, res.aicm);
        if (aicm_path.empty()) {
            return emit_error("doc_compile_cpp: cannot publish/write AICM output", out_json);
        }
    }

    // Per-surviving-body stats (surfacing parity gate: area/face/edge counts for open
    // shells, volume for solids). Additive — existing volume fixtures are unaffected.
    json bodies = json::array();
    for (const auto& b : res.bodies_info) {
        bodies.push_back(json{
            {"name", b.name},
            {"kind", b.kind},
            {"volume", b.volume},
            {"area", b.area},
            {"n_faces", b.n_faces},
            {"n_edges", b.n_edges},
        });
    }

    return emit(json{
                    {"ok", res.ok},
                    {"body_count", res.body_count},
                    {"volume", res.volume},
                    {"bodies", bodies},
                    {"statuses", statuses},
                    {"owner_map", res.owner_map.is_null() ? json::object() : res.owner_map},
                    {"aicm_path", aicm_path},
                    {"aicm_bytes", static_cast<uint64_t>(res.aicm.size())},
                },
                out_json, 0);
}

// P54b host-ergonomics: when an assist caller (agent_answer_question / agent_pm_assist)
// passes a live C++ doc_id but omits the explicit doc / revision / feature_status, read
// them from that live session so the host can send just {question|params, doc_id}. The
// existing explicit-params path (the gates pass doc/revision/feature_status inline) is
// untouched: each field is filled ONLY when absent. doc_id "?" / empty / not a live
// session leaves everything as-is (the gate behavior). Best-effort — any failure to read
// the session is swallowed so the assist still runs from whatever was provided.
//   `op` is the ordered_json params; mutated in place. `doc_id` is the resolved id.
void hydrate_assist_from_session(nlohmann::ordered_json& op, const std::string& doc_id) {
    using ojson = nlohmann::ordered_json;
    if (doc_id.empty() || doc_id == "?") return;
    const bool need_doc = !op.contains("doc") || op["doc"].is_null();
    const bool need_rev = !(op.contains("revision") && op["revision"].is_number());
    const bool need_fs = !op.contains("feature_status");
    if (!need_doc && !need_rev && !need_fs) return;
    try {
        // doc_tree -> {revision, tree}. Throws OpError for an unknown doc.
        json t = chudware::doc::doc_tree(doc_id);
        if (need_doc && t.contains("tree"))
            op["doc"] = ojson::parse(t["tree"].dump());
        if (need_rev && t.contains("revision") && t["revision"].is_number())
            op["revision"] = t["revision"].get<long>();
        if (need_fs) {
            // feature_status lives in the rebuild response (doc_rebuild forces a
            // recompile without bumping the revision).
            json rb = chudware::doc::doc_rebuild(doc_id);
            if (rb.contains("rebuild") && rb["rebuild"].contains("feature_status"))
                op["feature_status"] = ojson::parse(rb["rebuild"]["feature_status"].dump());
        }
    } catch (...) {
        // Unknown/closed doc or read failure: leave op untouched (run from provided data).
    }
}

// O1 (GUI model picker): apply the caller's explicit model / reasoning-effort overrides onto a
// resolved provider descriptor. Only overwrites when the param is a non-empty string; a refusal
// descriptor ({error:...}) passes through untouched.
static nlohmann::json apply_model_overrides(nlohmann::json desc, const nlohmann::json& params) {
    if (!desc.is_object() || desc.contains("error")) return desc;
    if (params.contains("model") && params["model"].is_string() &&
        !params["model"].get<std::string>().empty())
        desc["model"] = params["model"];
    if (params.contains("reasoning_effort") && params["reasoning_effort"].is_string() &&
        !params["reasoning_effort"].get<std::string>().empty())
        desc["reasoning_effort"] = params["reasoning_effort"];
    return desc;
}

// Native top-level execution must derive safety from the actual provider identity and endpoint,
// never from a caller-supplied offline_safe=true assertion. Experimental review calls obey the
// same rule so a local command model cannot smuggle a cloud review stage into the fallback.
bool native_provider_offline_safe(const json& provider) {
    if (!provider.is_object()) return false;
    json probe = provider;
    probe.erase("offline_safe");
    if (!chudware::agent::provider_offline_safe(probe)) return false;
    auto review = provider.find("review_provider");
    if (review == provider.end() || review->is_null()) return true;
    if (!review->is_object()) return false;
    json review_probe = *review;
    review_probe.erase("offline_safe");
    return chudware::agent::provider_offline_safe(review_probe);
}

// P54b host-ergonomics: resolve the assist provider from the ambient config.json when
// the caller did not pass one (the host sends bare {question|params, doc_id}). Returns
// the descriptor to feed make_assist_provider. On a config-provider refusal/resolution
// failure, returns {error:...} (the caller may surface it). The gates always pass an
// explicit provider, so this fallback never runs under them.
// O1: params.provider may now also be a provider NAME string (the GUI picker's explicit
// override) — resolved via resolve_provider(name, cfg, env) so it beats an ambient
// CHUDWARE_PROVIDER env var, then params.model / params.reasoning_effort refine the descriptor.
// The offline (ITAR) guard still applies — a refused name returns {error:...} as before.
nlohmann::json resolve_assist_provider(const nlohmann::json& params) {
    if (params.contains("provider") && params["provider"].is_object() &&
        !params["provider"].empty())
        return params["provider"];  // explicit descriptor (gate mock / full host override)
    json cfg = chudware::agent::load_config();
    std::optional<std::string> name;
    if (params.contains("provider") && params["provider"].is_string() &&
        !params["provider"].get<std::string>().empty())
        name = params["provider"].get<std::string>();
    json desc = chudware::agent::resolve_provider(name, cfg, nlohmann::json::object());
    return apply_model_overrides(std::move(desc), params);
}

// Wind-tunnel CFD on a live doc. Extracted from the agent_simulate_v1 handler so the
// mission orchestrator (run_mission, a simulate_flow task) can call the SAME flow-sim
// path the host's CFD quick-path uses. No LLM (the flow condition is regex-parsed from
// `request`), so it works fully offline. `doc_id` may be a part OR an assembly doc —
// doc_tessellate_bodies resolves world-placed component geometry for assemblies.
// Returns {ok, answer, drag_n, lift_n, cd, frontal_area_mm2, reynolds, velocity_ms,
// aoa_deg} on success, or {ok:false, answer} when there is no geometry / the sim fails.
nlohmann::json run_flow_sim(const std::string& doc_id, const std::string& request,
                            const nlohmann::json& params) {
    std::string lo = request;
    std::transform(lo.begin(), lo.end(), lo.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    chudware::sim::FlowParams fp;
    // velocity: explicit param wins; else parse "<n> m/s|mps|mph|km/h|kph|knots".
    if (params.contains("velocity")) {
        fp.velocity_ms = params.value("velocity", 10.0);
    } else {
        std::smatch mm;
        std::regex rv(R"((\d+(?:\.\d+)?)\s*(m/s|mps|mph|km/?h|kph|knots?|kt))");
        if (std::regex_search(lo, mm, rv)) {
            double v = std::stod(mm[1].str());
            std::string u = mm[2].str();
            if (u == "mph") v *= 0.44704;
            else if (u.rfind("km", 0) == 0 || u == "kph") v *= 0.27778;
            else if (u.rfind("kn", 0) == 0 || u == "kt") v *= 0.514444;
            fp.velocity_ms = v;
        }
    }
    // aoa: explicit param wins; else parse "<n> deg" (optionally near 'attack'/'aoa').
    if (params.contains("aoa")) {
        fp.aoa_deg = params.value("aoa", 0.0);
    } else {
        std::smatch ma;
        std::regex raoa(R"((-?\d+(?:\.\d+)?)\s*(?:deg|degree|degrees|\xc2\xb0))");
        if (std::regex_search(lo, ma, raoa)) fp.aoa_deg = std::stod(ma[1].str());
    }
    std::string fluid = params.value("fluid", std::string());
    if (fluid.empty()) fluid = (lo.find("water") != std::string::npos) ? "water" : "air";
    if (fluid == "water") { fp.nu_fluid = 1.0e-6; fp.rho_fluid = 1000.0; }
    fp.cells = params.value("cells", 96);
    fp.n_steps = params.value("n_steps", 2000);

    std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(doc_id);
    if (bodies.empty())
        return json{{"ok", false},
                    {"answer", "There's no solid geometry to simulate yet — model "
                               "the part first, then ask me to run the flow sim."}};

    // Accurate (OpenFOAM RANS) hand-off. OpenFOAM lives in the Python worker, which the HOST
    // owns; the core can't run it. So on an explicit solver:"openfoam" (the host chat sets it
    // when the user asks for an "accurate"/OpenFOAM run AND the worker reports OpenFOAM
    // available), we export a watertight STL and return a hand-off the host fulfills via
    // sim_run_flow(solver:openfoam). Missions pass empty params, so they always take the LBM
    // path below (the core mission can't reach the worker). Falls back to LBM on export failure.
    if (params.value("solver", std::string()) == "openfoam") {
        try {
            std::filesystem::path p = std::filesystem::temp_directory_path() /
                ("chudware_cfd_" + doc_id + ".stl");
            chudware::doc::doc_export_file(doc_id, p.string(), "stl", 0.0, 0.0, false, false);
            return json{{"ok", true}, {"needs_openfoam", true}, {"stl_path", p.string()},
                        {"velocity_ms", fp.velocity_ms}, {"aoa_deg", fp.aoa_deg},
                        {"fluid", fluid},
                        {"answer", "Running an accurate OpenFOAM RANS wind tunnel (a few "
                                   "minutes)..."}};
        } catch (const std::exception&) {
            // fall through to the LBM estimate so the agent still answers
        }
    }

    std::vector<std::vector<float>> bp;
    std::vector<std::vector<uint32_t>> bi;
    for (auto& b : bodies) { bp.push_back(b.pos); bi.push_back(b.idx); }
    double re = 0.0;
    chudware::sim::LbmResult r = chudware::sim::run_wind_tunnel(bp, bi, fp, &re);
    if (!r.ok)
        return json{{"ok", false}, {"answer", "The flow sim could not run: " + r.error}};
    std::string warn = r.warning.empty() ? std::string() : (" WARNING: " + r.warning);
    char buf[900];
    std::snprintf(buf, sizeof(buf),
                  "Wind-tunnel CFD at %.1f m/s%s, %.1f deg AoA (Re %.0f, ref length %.0f mm): "
                  "drag %.3f N, lift %.3f N, Cd %.3f, frontal area %.0f mm^2. Coarse LBM "
                  "(%dx%dx%d, %d steps, %s) — a directional estimate; absolute Cd is uncalibrated "
                  "(drag scales V^2, Cd velocity-independent). Ask for an \"accurate\" (OpenFOAM) "
                  "run for a validated coefficient.%s",
                  fp.velocity_ms, fluid == "water" ? " in water" : "", fp.aoa_deg, re,
                  r.ref_length_mm, r.drag_n, r.lift_n, r.cd, r.frontal_area_mm2, r.nx, r.ny, r.nz,
                  r.steps_run, r.converged ? "converged" : "not fully converged", warn.c_str());
    return json{{"ok", true},
                {"answer", std::string(buf)},
                {"drag_n", r.drag_n}, {"lift_n", r.lift_n}, {"cd", r.cd},
                {"frontal_area_mm2", r.frontal_area_mm2}, {"reynolds", re},
                {"ref_length_mm", r.ref_length_mm}, {"converged", r.converged},
                {"residual", r.residual}, {"voxel_mm", r.voxel_mm},
                {"min_feature_voxels", r.min_feature_voxels}, {"warning", r.warning},
                {"velocity_ms", fp.velocity_ms}, {"aoa_deg", fp.aoa_deg}};
}

}  // namespace

extern "C" {

CHUDWARE_API const char* chudware_abi_version(void) { return "1"; }

// Same configure_file'd id chudware-hostd embeds (core/hostd/version_info.h.in) —
// one source of truth, so a matched-build daemon + engine report ONE id.
CHUDWARE_API const char* chudware_build_id(void) { return CHUDWARE_HOSTD_BUILD_ID; }

CHUDWARE_API void chudware_free(char* s) { std::free(s); }


// Live event streaming: one thread-local C hook for agent events and simulation phases.
// Armed on the dispatch/solver thread before chudware_call; cleared immediately after.
CHUDWARE_API void chudware_set_event_sink(void (*cb)(const char* event_json, void* ud), void* ud) {
    try {
        chudware::sim::set_progress_sink(cb, ud);
        if (cb) {
            chudware::agent::set_stream_event_sink([cb, ud](const nlohmann::json& ev) {
                const std::string s = ev.dump();
                cb(s.c_str(), ud);
            });
        } else {
            chudware::agent::set_stream_event_sink({});
        }
    } catch (...) {
        // A void C setter cannot report allocation failures across the ABI.
    }
}

// Direct document calls inside run.cpp do not re-enter hostd's outer RPC dispatcher. Bridge them
// to a second thread-local callback so the daemon can feed each completed call through the same
// DocRegistry::postCall path as a socket-level doc_new/doc_apply_ops. Publication happens only
// after the document operation returns, so no per-document engine mutex is held here.
CHUDWARE_API void chudware_set_doc_publish_sink(
    void (*cb)(const char* method, const char* params_json, const char* result_json, void* ud),
    void* ud) {
    try {
        if (cb) {
            chudware::agent::set_doc_publish_sink(
                [cb, ud](const std::string& method, const nlohmann::json& params,
                         const nlohmann::json& result) {
                    const std::string p = params.dump();
                    const std::string r = result.dump();
                    cb(method.c_str(), p.c_str(), r.c_str(), ud);
                });
        } else {
            chudware::agent::set_doc_publish_sink({});
        }
    } catch (...) {
        // A void C setter cannot report allocation failures across the ABI.
    }
}

CHUDWARE_API void chudware_set_cache_refresh_sink(
    void (*cb)(const char* doc_id, const char* response_json, void* ud), void* ud) {
    try {
        if (cb) chudware::doc::set_cache_refresh_sink(
            [cb, ud](const std::string& id, const nlohmann::json& response) {
                const std::string bytes = response.dump();
                cb(id.c_str(), bytes.c_str(), ud);
            });
        else chudware::doc::set_cache_refresh_sink({});
    } catch (...) {}
}

CHUDWARE_API int chudware_activity(uint64_t counts[3]) {
    if (!counts) return 1;
    counts[0] = counts[1] = counts[2] = 0;
    try {
        bool complete = chudware::doc::activity_counts(counts[0], counts[2]);
        complete = chudware::ecad::dirty_document_count(counts[2]) && complete;
        complete = chudware::cam::dirty_document_count(counts[2]) && complete;
        complete = chudware::doc::dirty_drawing_count(counts[2]) && complete;
        counts[1] = chudware::prof::active() ? 1 : 0;
        return complete ? 0 : 1;
    } catch (...) { return 1; }
}

// ---- CAM program recipe (deterministic, request-aware, no LLM) ------------------
// Build a complete CAM machining JOB from the CURRENT PART and post it to G-code,
// reusing the exact cam_* engine the GUI and MCP drive. This is the offline "make me a
// CNC program" path for the in-app copilot — the manufacturing analogue of the W17
// drawing fast-path. It mines the free-text request for the machining STRATEGY (which
// operations, which post dialect, which material) and tailors the op set + tooling to
// it, falling back to a sensible face+contour+drill recipe when nothing specific is
// asked. It does NOT invent CAM logic; it orchestrates the existing RPCs. Returns a
// {ok, ...} envelope with the job tree, op summary, sim, and gcode. Never throws.
static json cam_program_from_part(const std::string& part_doc_id, const json& options) {
    if (part_doc_id.empty()) return json{{"ok", false}, {"error", "no active part"}};

    // --- mine the request ----------------------------------------------------------
    std::string req = jstr(options, "request");
    std::string lc; lc.reserve(req.size());
    for (char c : req) lc += (char)std::tolower((unsigned char)c);
    auto has = [&](const char* w) { return lc.find(w) != std::string::npos; };

    std::string dialect = jstr(options, "post");
    if (dialect.empty()) {
        for (const char* d : {"linuxcnc", "fanuc", "haas", "mach3", "marlin", "siemens",
                              "centroid", "grbl"})
            if (has(d)) { dialect = d; break; }
    }
    if (dialect.empty() && has("tormach")) dialect = "centroid";
    if (dialect.empty()) dialect = "grbl";

    std::string material = jstr(options, "material");
    if (material.empty()) {
        for (const char* mt : {"aluminium", "aluminum", "stainless", "steel", "brass",
                               "titanium", "copper", "acrylic", "abs", "mdf", "wood"})
            if (has(mt)) { material = mt; break; }
    }
    if (material == "aluminium") material = "aluminum";
    if (material.empty()) material = "aluminum";

    // --- strategy + which operations the request asks for --------------------------
    const bool turning = has("lathe") || has("turning") || has("turn down") || has("turn it") ||
                         has(" turn ") || has("turned") || has("part off") || has("parting");
    bool wFace    = has("face") || has("facing") || has("skim") || has("flatten");
    bool wContour = has("contour") || has("profile") || has("outline") || has("perimeter") ||
                    has("cut out") || has("cutout") || has("cut it out");
    bool wPocket  = has("pocket") || has("cavity") || has("hollow");
    bool wAdaptive= has("adaptive") || has("rough") || has("hsm") || has("clear out") ||
                    has("clearing") || has("bulk");
    bool wSlot    = has("slot");
    bool wEngrave = has("engrave") || has("etch") || has("mark ") || has("engraving");
    bool wChamfer = has("chamfer") || has("deburr") || has("break edge") || has("bevel");
    bool wDrill   = has("drill") || has("bore") || has("hole") || has("peck") || has("tap ");
    bool w3d      = has("3d") || has("3-d") || has("parallel") || has("scallop") ||
                    has("waterline") || has("surface finish") || has("finish pass") ||
                    has("contour finish") || has("ball");
    // No specific verb anywhere -> the default general-purpose recipe.
    const bool anyReq = turning || wFace || wContour || wPocket || wAdaptive || wSlot ||
                        wEngrave || wChamfer || wDrill || w3d;
    if (!anyReq) { wFace = true; wContour = true; wDrill = true; }

    // --- export the part to a temp STEP, then extract what the chosen ops need ------
    std::error_code ec;
    std::filesystem::path step =
        std::filesystem::temp_directory_path(ec) / ("chudware_cam_" + part_doc_id + ".step");
    try {
        chudware::doc::doc_export_step(part_doc_id, step.string());
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("export failed: ") + e.what()}};
    }
    auto extract = [&](const char* kind, double z, bool useZ) -> json {
        json p = {{"path", step.string()}, {"kind", kind}, {"deflection", 0.1}};
        if (useZ) p["z"] = z;
        try { return chudware::cam::cam_extract(p); } catch (...) { return json::object(); }
    };

    json ex = extract("all", 0, false);
    if (ex.is_null() || ex.empty())
        return json{{"ok", false}, {"error", "extract failed (OCCT geometry unavailable)"}};
    json boxSrc;
    if (ex.contains("stock") && ex["stock"].contains("box")) boxSrc = ex["stock"]["box"];
    else if (ex.contains("bbox")) boxSrc = ex["bbox"];
    if (!boxSrc.is_object() || !boxSrc.contains("min") || !boxSrc.contains("max") ||
        !boxSrc["min"].is_array() || boxSrc["min"].size() < 3)
        return json{{"ok", false}, {"error", "could not determine stock from part"}};
    json stockBox = json{{"type", "box"}, {"min", boxSrc["min"]}, {"max", boxSrc["max"]}};
    const double zmin = boxSrc["min"][2].get<double>(), zmax = boxSrc["max"][2].get<double>();
    const double xspan = boxSrc["max"][0].get<double>() - boxSrc["min"][0].get<double>();
    const double yspan = boxSrc["max"][1].get<double>() - boxSrc["min"][1].get<double>();
    const double clearance = zmax + 15.0, retract = zmax + 5.0;

    // planar section regions (2.5D ops), drill points, mesh (3D), turning profile — lazily.
    json regions, holePoints = json::array(), mesh, profile;
    auto needRegions = [&]() -> bool {
        if (regions.is_null()) { json s = extract("section", 0.5 * (zmin + zmax), true);
            regions = s.contains("regions") ? s["regions"] : json::array(); }
        return regions.is_array() && !regions.empty();
    };
    if (wDrill) { json h = extract("holes", 0, false);
        if (h.contains("points") && h["points"].is_array()) holePoints = h["points"]; }
    const bool hasHoles = !holePoints.empty();
    if (w3d) { json m = extract("mesh", 0, false); if (m.contains("mesh")) mesh = m["mesh"]; }
    if (turning) { json pr = extract("profile", 0, false);
        if (pr.contains("profile")) profile = pr["profile"]; }

    // --- create the job ------------------------------------------------------------
    std::string partName;
    try {
        json t = chudware::doc::doc_tree(part_doc_id);
        if (t.contains("tree")) partName = t["tree"].value("name", std::string());
    } catch (...) {}
    if (partName.empty()) partName = "Part";
    std::string cam_id;
    try {
        json nj = chudware::cam::cam_new(json{{"name", partName + " CAM"}, {"post", dialect}});
        cam_id = nj.value("doc_id", std::string());
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("cam_new: ") + e.what()}};
    }
    if (cam_id.empty()) return json{{"ok", false}, {"error", "cam_new returned no doc_id"}};

    // --- tool registry: add a tool of each kind on demand, sized to the part --------
    const double minspan = (xspan > 0 && yspan > 0) ? std::min(xspan, yspan) : 60.0;
    const double flatDia = minspan >= 60 ? 10.0 : (minspan < 20 ? 3.0 : 6.0);
    const double ballDia = minspan >= 60 ? 6.0 : 3.0;
    int nextTool = 1;
    json toolBatch = json::array();
    std::map<std::string, int> toolNo;
    auto tool = [&](const std::string& type, double dia) -> int {
        auto it = toolNo.find(type);
        if (it != toolNo.end()) return it->second;
        int n = nextTool++;
        json t = {{"number", n}, {"type", type}, {"diameter", dia}, {"flutes", 3}};
        std::string nm = std::to_string((int)dia) + "mm " + type;
        if (type == "endmill_flat") nm = std::to_string((int)dia) + "mm flat endmill";
        else if (type == "endmill_ball") { nm = std::to_string((int)dia) + "mm ball endmill"; t["flutes"] = 2; }
        else if (type == "drill") { nm = std::to_string((int)dia) + "mm drill"; t["flutes"] = 2; }
        else if (type == "chamfer_mill") { nm = "chamfer mill"; t["tip_angle"] = 90.0; }
        else if (type == "vbit") { nm = "60deg V-bit"; t["tip_angle"] = 60.0; t["flutes"] = 1; }
        else if (type == "turning_general") nm = "turning tool";
        else if (type == "parting_tool") nm = "parting tool";
        t["name"] = nm;
        toolBatch.push_back(json{{"op", "add_tool"}, {"tool", t}});
        toolNo[type] = n;
        return n;
    };

    // --- plan the operations FIRST (machining order: rough -> 2.5D -> finish -> holes). Each op's
    //     tool() call registers into toolBatch on demand; ops carry a "$S" setup placeholder that is
    //     patched to the real id after the setup is created. Planning before the setup-apply is what
    //     guarantees every tool the ops reference is actually in the job's tool library. -----------
    json opBatch = json::array();
    std::vector<std::string> summary;
    auto addOp = [&](const std::string& type, int toolN, const json& geom,
                     double top, double bottom, const json& params) {
        json op = {{"type", type}, {"tool_number", toolN},
                   {"heights", {{"top", top}, {"bottom", bottom},
                                {"clearance", clearance}, {"retract", retract}}}};
        if (geom.is_object() && !geom.empty()) op["geometry"] = geom;
        if (params.is_object() && !params.empty()) op["params"] = params;
        opBatch.push_back(json{{"op", "add_operation"}, {"setup_id", "$S"}, {"operation", op}});
        summary.push_back(type);
    };

    const char* machineKind = turning ? "lathe" : "mill3";

    if (turning) {
        // Lathe job: face the end, rough + finish the turned profile, optional part-off. Heights are
        // not meaningful for radial turning; the engine works from the profile.
        int tg = tool("turning_general", 12.0);
        json geom = profile.is_null() ? json::object() : json{{"profile", profile}};
        addOp("turn_face", tg, geom, zmax, zmin, json::object());
        addOp("turn_rough", tg, geom, zmax, zmin, json::object());
        addOp("turn_finish", tg, geom, zmax, zmin, json::object());
        if (has("part off") || has("parting") || has("cut off"))
            addOp("turn_part", tool("parting_tool", 3.0), geom, zmax, zmin, json::object());
    } else {
        if (wFace)
            addOp("face", tool("endmill_flat", flatDia), json::object(), zmax, zmax - 1.0, json::object());
        if (wAdaptive && needRegions())
            addOp("adaptive2d", tool("endmill_flat", flatDia), json{{"regions", regions}}, zmax, zmin,
                  json{{"stepover", flatDia * 0.4}, {"stepdown", flatDia}});
        if (wPocket && needRegions())
            addOp("pocket2d", tool("endmill_flat", flatDia), json{{"regions", regions}}, zmax, zmin, json::object());
        if (wSlot && needRegions())
            addOp("slot", tool("endmill_flat", flatDia), json{{"regions", regions}}, zmax, zmin, json::object());
        if (wContour && needRegions())
            addOp("contour2d", tool("endmill_flat", flatDia), json{{"regions", regions}}, zmax, zmin, json::object());
        if (w3d && mesh.is_object() && !mesh.empty()) {
            std::string t3 = has("scallop") ? "scallop3d" : has("waterline") ? "contour3d" : "parallel3d";
            addOp(t3, tool("endmill_ball", ballDia), json{{"mesh", mesh}}, zmax, zmin,
                  json{{"stepover", ballDia * 0.25}});
        }
        if (wChamfer && needRegions())
            addOp("chamfer2d", tool("chamfer_mill", 6.0), json{{"regions", regions}}, zmax, zmax - 1.0, json::object());
        if (wEngrave && needRegions())
            addOp("engrave", tool("vbit", 6.0), json{{"regions", regions}}, zmax, zmax - 0.5, json::object());
        if (wDrill && hasHoles)
            addOp("drill", tool("drill", 5.0), json{{"points", holePoints}}, zmax, zmin, json{{"cycle", "peck"}});
    }

    if (opBatch.empty())
        return json{{"ok", false},
                    {"error", "the requested operations need geometry this part doesn't expose "
                              "(e.g. drilling but no holes, or a profile/section that came back empty)"}};

    // --- apply tools + setup (toolBatch is now fully populated by planning), resolve the setup id --
    json setupStock = stockBox;
    setupStock["material"] = material;
    json setupBatch = toolBatch;
    setupBatch.push_back(json{{"op", "add_setup"},
                              {"setup", {{"name", "Setup1"}, {"machine", {{"kind", machineKind}}},
                                         {"stock", setupStock}}}});
    json applied;
    try {
        applied = chudware::cam::cam_apply_ops(cam_id, -1, setupBatch);
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("setup/tools: ") + e.what()}};
    }
    std::string setupId;
    if (applied.contains("tree") && applied["tree"].contains("setups") &&
        applied["tree"]["setups"].is_array() && !applied["tree"]["setups"].empty())
        setupId = applied["tree"]["setups"][0].value("id", std::string());
    if (setupId.empty() && applied.contains("id_map"))
        setupId = applied["id_map"].value("_s", std::string());
    if (setupId.empty()) return json{{"ok", false}, {"error", "setup id not found"}};

    for (auto& o : opBatch) o["setup_id"] = setupId;  // patch the "$S" placeholder
    try {
        chudware::cam::cam_apply_ops(cam_id, -1, opBatch);
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("operations: ") + e.what()}};
    }

    // --- generate, simulate, post --------------------------------------------------
    json gen, sim, post;
    try {
        gen = chudware::cam::cam_generate(cam_id, json::object());
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("generate: ") + e.what()}};
    }
    try {
        sim = chudware::cam::cam_simulate(cam_id, json::object());
    } catch (const std::exception& e) {
        // captured below into out["sim_error"]
        sim = json{{"_sim_error", e.what()}};
    } catch (...) {
        sim = json{{"_sim_error", "unknown sim error"}};
    }
    try {
        post = chudware::cam::cam_post(cam_id, json{{"dialect", dialect}, {"program_name", "Chudware"}});
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("post: ") + e.what()}};
    }

    const std::string gcode = post.value("gcode", std::string());
    std::string ncPath;
    try {
        std::filesystem::path nc = std::filesystem::temp_directory_path(ec) /
                                   ("chudware_cam_" + part_doc_id + "." + dialect + ".nc");
        std::ofstream f(nc);
        if (f) { f << gcode; ncPath = nc.string(); }
    } catch (...) {}

    json tree = chudware::cam::cam_tree(cam_id);
    int opCount = 0;
    if (tree.contains("tree") && tree["tree"].contains("setups"))
        for (auto& s : tree["tree"]["setups"])
            if (s.contains("operations")) opCount += (int)s["operations"].size();
    std::string opsCsv;
    for (size_t i = 0; i < summary.size(); ++i) opsCsv += (i ? ", " : "") + summary[i];

    json out = {{"ok", true},
                {"cam_doc_id", cam_id},
                {"part_doc_id", part_doc_id},
                {"dialect", dialect},
                {"material", material},
                {"strategy", turning ? "turning" : "milling"},
                {"op_count", opCount},
                {"ops_summary", opsCsv},
                {"hole_count", (int)holePoints.size()},
                {"tree", tree.contains("tree") ? tree["tree"] : json::object()},
                {"gcode", gcode},
                {"gcode_path", ncPath},
                {"line_count", post.value("line_count", 0)},
                {"warnings", gen.contains("warnings") ? gen["warnings"] : json::array()}};
    if (sim.is_object() && sim.contains("_sim_error"))
        out["sim_error"] = sim["_sim_error"];
    else if (sim.is_object() && sim.contains("results"))
        out["sim"] = sim["results"];
    return out;
}

// Server-side STEP export + cam_extract. The GUI used to export_file to a CLIENT temp
// path then call cam_extract on the SERVER with that path — on web/daemon the server
// cannot see the client's filesystem, so extract always failed. Compose export+extract
// here (same pattern as cam_program_from_part ~611-621) and return cam_extract's shape.
static json cam_extract_from_doc(const std::string& part_doc_id, const json& params) {
    if (part_doc_id.empty())
        return json{{"error", "cam_extract_from_doc: 'doc_id' is required"}};
    std::error_code ec;
    std::filesystem::path step =
        std::filesystem::temp_directory_path(ec) /
        ("chudware_cam_extract_" + part_doc_id + ".step");
    try {
        chudware::doc::doc_export_step(part_doc_id, step.string());
    } catch (const std::exception& e) {
        return json{{"error", std::string("export failed: ") + e.what()}};
    }
    json p = params.is_object() ? params : json::object();
    p["path"] = step.string();
    // cam_extract only understands path/kind/z/... — drop the doc_id we were given.
    if (p.contains("doc_id")) p.erase("doc_id");
    try {
        return chudware::cam::cam_extract(p);
    } catch (const std::exception& e) {
        return json{{"error", std::string("cam_extract failed: ") + e.what()}};
    } catch (...) {
        return json{{"error", "cam_extract failed"}};
    }
}

// Real dispatch body. Extracted from chudware_call so the public entry can wrap it in a
// Windows structured-exception (SEH) guard WITHOUT this function — which is full of
// C++ objects that require stack unwinding — having to host the __try/__except itself
// (MSVC C2712: a function that needs C++ object unwinding can't contain __try). This
// function keeps its own top-level C++ try/catch, so a thrown OpError / std::exception
// becomes a clean JSON error with every lock_guard/DocLock already released by unwind.
// Only a hardware fault (access violation) escapes as an SEH; that's caught one frame
// up in chudware_call.
// Factored body of the parts_make dispatch — shared by the `parts_make` branch and
// the PlaceSeams::parts_make injection (core/parts/place). rc mirrors the old emit codes.
static json parts_make_impl(const json& params, int& rc) {
    rc = 0;
    try {
        const std::string family = params.value("family", std::string());
        const std::string size = params.value("size", std::string());
        double length = 0.0;
        if (params.contains("length_mm") && params["length_mm"].is_number())
            length = params["length_mm"].get<double>();
        else if (params.contains("length") && params["length"].is_number())
            length = params["length"].get<double>();
        const json opts = params.value("options", json::object());
        chudware::parts::PartBuild pb = chudware::parts::build(family, size, length, opts);
        const std::string nm = params.value("name", pb.name);
        json nresp = chudware::doc::doc_new("part", nm);
        const std::string doc_id = nresp.value("doc_id", std::string());
        if (doc_id.empty()) {
            rc = 1;
            return json{{"ok", false}, {"error", "parts_make: doc_new failed"}};
        }
        json aresp = chudware::doc::doc_apply_ops(doc_id, 0, pb.ops, false);
        json out;
        out["ok"] = true;
        out["doc_id"] = doc_id;
        out["name"] = nm;
        out["meta"] = pb.meta;
        // Extents + placement datum (Bug A-1); standard parts seat at z=0, axis +Z.
        try {
            json bb = chudware::doc::doc_bbox(doc_id);
            out["bbox"] = bb;
            const auto& mn = bb["min"]; const auto& mx = bb["max"];
            out["datum"] = {
                {"base_center", {(mn[0].get<double>() + mx[0].get<double>()) / 2.0,
                                 (mn[1].get<double>() + mx[1].get<double>()) / 2.0,
                                 mn[2].get<double>()}},
                {"axis", {0, 0, 1}},
                {"note", "base seats at z=0, axis +Z; translate base_center to the seat point"}};
            // Unlike coaxial hardware, a servo's output axis is offset toward
            // one end of its case. Keep the existing seating datum intact.
            if (pb.meta.contains("output_axis")) {
                out["datum"]["output_axis"] = pb.meta["output_axis"];
                out["output_axis"] = pb.meta["output_axis"];
            }
        } catch (...) {
        }
        if (aresp.contains("revision")) out["revision"] = aresp["revision"];
        if (aresp.contains("tess")) out["tess"] = aresp["tess"];
        if (aresp.contains("volume")) out["volume"] = aresp["volume"];
        if (aresp.contains("rebuild")) out["rebuild"] = aresp["rebuild"];
        const std::string sp = params.value("save_path", std::string());
        if (!sp.empty()) {
            json sresp = chudware::doc::doc_export_step(doc_id, sp);
            out["step_path"] = sp;
            out["step_ok"] = sresp.value("ok", true);
        }
        return out;
    } catch (const std::exception& e) {
        rc = 1;
        return json{{"ok", false}, {"error", e.what()}};
    }
}

// --- sourcing seams (production defaults for core/sourcing) ------------
// The sourcing lib is pure; every side effect is injected here: assembly/board
// loading via the doc/ecad sessions, vendor index reads, model building via
// parts+doc, and HTTP via the agent transport (8s cap; providers fail closed
// without credentials). The LLM dims lane stays empty (spec_sheet_match degrades
// per contract).
static chudware::sourcing::Seams build_sourcing_seams() {
    using chudware::sourcing::HttpResult;
    using chudware::sourcing::Seams;
    Seams s;
    s.load_assembly = [](const std::string& doc_path) -> json {
        json opened = chudware::doc::doc_open(doc_path);
        if (!opened.is_object() || !opened.contains("doc_id")) return json();
        return chudware::doc::doc_tree(opened["doc_id"].get<std::string>());
    };
    s.load_board = [](const std::string& path) -> json {
        try {
            return chudware::ecad::ecad_container_open(path).doc;
        } catch (...) {
            return json();
        }
    };
    s.vendor_lookup = [](const json& component) -> json {
        try {
            namespace fs = std::filesystem;
            const fs::path idx = fs::path(chudware::sourcing::vendor_parts_dir()) / "index.json";
            if (!fs::exists(idx)) return json();
            std::ifstream in(idx);
            const json index = json::parse(in);
            return chudware::sourcing::vendor_index_lookup(index, component);
        } catch (...) {
            return json();
        }
    };
    s.build_model = [](const json& match) -> std::string {
        try {
            if (!match.is_object() || !match.contains("family") ||
                match.value("source", std::string()) != "bolts")
                return std::string();
            const std::string family = match["family"].get<std::string>();
            const std::string size = match["size"].get<std::string>();
            const double length_mm = match.value("length_mm", 0.0);
            auto pb = chudware::parts::build(family, size, length_mm, json::object());
            json created = chudware::doc::doc_new("part", pb.name);
            const std::string doc_id = created["doc_id"].get<std::string>();
            chudware::doc::doc_apply_ops(doc_id, created.value("revision", 0L), pb.ops, false);
            namespace fs = std::filesystem;
            const fs::path dir = fs::temp_directory_path() / "chudware_spec_models";
            fs::create_directories(dir);
            json saved = chudware::doc::doc_export_step(doc_id, (dir / (family + "_" + size + ".step")).string());
            return saved.value("path", std::string());
        } catch (...) {
            return std::string();
        }
    };
    s.build_spec_model = [](const std::string& tmpl, const json& dims) -> std::string {
        try {
            json ops = chudware::sourcing::template_build_ops(tmpl, dims);
            json created = chudware::doc::doc_new("part", tmpl);
            const std::string doc_id = created["doc_id"].get<std::string>();
            chudware::doc::doc_apply_ops(doc_id, created.value("revision", 0L), ops, false);
            namespace fs = std::filesystem;
            const fs::path dir = fs::temp_directory_path() / "chudware_spec_models";
            fs::create_directories(dir);
            json saved = chudware::doc::doc_export_step(doc_id, (dir / (tmpl + ".step")).string());
            return saved.value("path", std::string());
        } catch (...) {
            return std::string();
        }
    };
    // extract_dims: intentionally empty (LLM lane unported).
    s.http_post = [](const std::string& url, const chudware::sourcing::HttpHeaders& headers,
                     const std::string& body) -> HttpResult {
        auto r = chudware::agent::http_post(url, headers, body, 8000);
        return HttpResult{r.status, std::move(r.body), std::move(r.error)};
    };
    s.http_get = [](const std::string& url, const chudware::sourcing::HttpHeaders& headers) -> HttpResult {
        auto r = chudware::agent::http_get(url, headers, 8000);
        return HttpResult{r.status, std::move(r.body), std::move(r.error)};
    };
    return s;
}

static int chudware_call_inner(const char* method, const char* params_json, char** out_json) {
    if (out_json) *out_json = nullptr;
    if (!method) return emit_error("null method", out_json);

    std::string m(method);
    json params = json::object();
    try {
        if (params_json && params_json[0] != '\0') {
            params = json::parse(params_json);
            if (params.is_null())
                params = json::object();
            else if (!params.is_object())
                return emit_error("params must be a JSON object", out_json);
        }
    } catch (const std::exception& e) {
        return emit_error(std::string("invalid params JSON: ") + e.what(), out_json);
    }

    // Host-version-robust aliases: accept the host's in-app agent RPC names (agent_run_v2
    // / agent_ask_v2 / get_prompts_v2 / agent_pm_assist_v1) directly. The host normally
    // pre-translates these in worker_client, but a stale host binary (or any direct
    // caller) would otherwise hit "unknown method: agent_run_v2". We mirror that
    // translation here so the core is self-sufficient. Param shapes already match the
    // target handlers (request/doc_id, question/doc_id) except pm_assist, which wraps.
    if (m == "agent_run_v2") {
        m = "agent_run";
    } else if (m == "agent_ask_v2") {
        m = "agent_answer_question";
    } else if (m == "agent_clarify_v1") {
        m = "agent_clarify";
    } else if (m == "get_prompts_v2") {
        m = "agent_get_prompts_v2";
    } else if (m == "agent_pm_assist_v1") {
        if (params.is_object() && !params.contains("params")) {
            json wrapped = json::object();
            wrapped["params"] = params;
            wrapped["doc_id"] = jstr(params, "doc_id");
            if (params.contains("_cell_ctx")) wrapped["_cell_ctx"] = params["_cell_ctx"];
            params = std::move(wrapped);
        }
        m = "agent_pm_assist";
    }

    // Env-var auto-profile (CHUDWARE_PROFILE=1): start the sampler on the first call so the
    // whole session is covered; a report is written on process exit. No-op when unset.
    prof_env_init_once();
    // Register this thread as the profiler's sample target for the duration of the call when
    // the profiler is running AND this is a method we want sampled (modeling-only by default,
    // every method in "all" mode). RAII: target is cleared on every return/throw below.
    chudware::prof::TargetScope _prof_scope(
        chudware::prof::active() &&
        (chudware::prof::mode() == "all" || prof_is_modeling_method(m)));
    // Stamp the calling method so the g_mtx contention meters (DocLock) can attribute lock
    // wait/hold to this RPC (Phase 1b). `m` outlives the call; the pointer is cleared on return.
    struct RpcMethodScope {
        explicit RpcMethodScope(const char* s) { chudware::doc::set_current_rpc_method(s); }
        ~RpcMethodScope() { chudware::doc::set_current_rpc_method(nullptr); }
    } _rpc_method_scope(m.c_str());

    try {
        std::optional<chudware::sim::CellPrincipal> cellPrincipal;
        if (params.contains("_cell_ctx")) {
            const json& ctx = params["_cell_ctx"];
            if (!ctx.is_object() || !ctx.value("cell", false) ||
                ctx.value("workspace_key", std::string()).empty() ||
                ctx.value("actor_key", std::string()).empty())
                return emit(json{{"code", "cell_refused"}, {"message", "invalid native principal"}},
                            out_json, 1);
            cellPrincipal = chudware::sim::CellPrincipal{
                ctx["workspace_key"].get<std::string>(), ctx["actor_key"].get<std::string>(),
                ctx.value("right", std::string()) == "manager",
                ctx.value("epoch", std::int64_t{0})};
            params.erase("_cell_ctx");
        }
        chudware::sim::CellRequestScope cellScope(cellPrincipal ? &*cellPrincipal :
            chudware::sim::current_cell_principal());
        std::optional<std::int64_t> revokeThrough;
        if (chudware::sim::current_cell_principal() &&
            (m == "agent_cancel" || m == "sim_cancel") &&
            params.contains("_cell_revoke_through"))
            revokeThrough = params["_cell_revoke_through"].get<std::int64_t>();
        params.erase("_cell_revoke_through");
        NativeRunOwnerScope nativeRunScope(m, params);
        if (m == "agent_run_event" || m == "agent_run_events_batch" ||
            m == "agent_run_events_read" || m == "agent_steer" ||
            (m == "agent_cancel" && !jstr(params, "run_id").empty())) {
            const std::string id = jstr(params, "run_id");
            if (!safeRunComponent(id) || !nativeRunOwned(id, m == "agent_cancel"))
                return emit(json{{"code", "not_found"}, {"message", "run not found"}},
                            out_json, 1);
        }
        if (m == "ping") {
            return emit(json{{"ok", true},
                             {"abi", chudware_abi_version()},
                             {"capabilities",
                              json::array({"doc_render_view.camera_pose.v1",
                                           "doc_render_view.section.v1",
                                           "doc_view_metrics.v1"})},
                             {"echo", params}},
                        out_json, 0);
        }
        // --- sampling profiler control (see core/prof/sampler.{h,cpp}) ----------
        // prof_start {interval_us?|hz?, mode?:"modeling"|"all"} -> begin sampling.
        if (m == "prof_start") {
            unsigned iv = 0;
            if (params.contains("interval_us") && params["interval_us"].is_number())
                iv = params["interval_us"].get<unsigned>();
            else if (params.contains("hz") && params["hz"].is_number()) {
                int hz = params["hz"].get<int>();
                if (hz > 0) iv = 1000000u / static_cast<unsigned>(hz);
            }
            std::string md = params.value("mode", std::string("modeling"));
            bool started = chudware::prof::start(iv, md);
            return emit(json{{"ok", true},
                             {"started", started},
                             {"already_running", !started},
                             {"interval_us", chudware::prof::interval_us()},
                             {"mode", chudware::prof::mode()}},
                        out_json, 0);
        }
        // prof_stop {top?, write_folded?, write_speedscope?, min_samples?} -> stop +
        // return the symbolized report (also writes the requested files).
        if (m == "prof_stop") {
            chudware::prof::stop();
            json r = chudware::prof::report(params);
            // Additive: per-study simulation phase timings (prof/phases.h). Attached even
            // when the sampler captured nothing (r.ok=false) — the phase registry is
            // independent of the sampler and often the only data a sim-only run has.
            r["phases"] = chudware::prof::phases::report(false);
            return emit(std::move(r), out_json, 0);
        }
        // prof_report {...} -> symbolize + return the report WITHOUT stopping.
        if (m == "prof_report") {
            json r = chudware::prof::report(params);
            r["phases"] = chudware::prof::phases::report(false);  // additive (see prof_stop)
            return emit(std::move(r), out_json, 0);
        }
        if (m == "prof_status") {
            return emit(json{{"ok", true},
                             {"active", chudware::prof::active()},
                             {"samples", chudware::prof::sample_count()},
                             {"interval_us", chudware::prof::interval_us()},
                             {"mode", chudware::prof::mode()},
                             // Additive: completed sim studies in the phase registry.
                             {"phase_studies", chudware::prof::phases::completed_count()}},
                        out_json, 0);
        }
        // prof_phases {reset?} -> per-study simulation phase timings (prof/phases.h):
        // mesher / FEA static / modal / thermal / fatigue / LBM / flow_run / overlay, each as
        // {domain, total_ms, phases:[{name,ms,count,pct}]}. Always-on (no start needed).
        // reset=true clears the registry AFTER building the report (call before a run).
        if (m == "prof_phases") {
            return emit(chudware::prof::phases::report(params.value("reset", false)), out_json, 0);
        }
        // prof_tree {path?, depth?, top?} -> nested call-tree over the current sampling
        // aggregation (prof/tree.h). path drills into a subtree ("a/b/c"); depth caps
        // nesting (-1 unlimited); top caps children per level (hot-first). Additive —
        // folded/speedscope/flat report() is unchanged.
        if (m == "prof_tree") {
            return emit(chudware::prof::tree_report(params), out_json, 0);
        }
        // prof_phases_tree {path?, depth?, top?, reset?} -> nested study->phase->subphase
        // tree with wall-clock self/total rollups (dotted phase names split on '.').
        // reset=true clears AFTER building (same as prof_phases).
        if (m == "prof_phases_tree") {
            const bool do_reset = params.value("reset", false);
            return emit(chudware::prof::phases::report_tree(params, do_reset), out_json, 0);
        }
        // prof_contention {reset?} -> g_mtx lock wait/hold/queue-depth meters (Phase 1b).
        // The on-CPU sampler above sees only the ONE thread holding the lock; this reveals the
        // time OTHER threads spend BLOCKED on it — the dominant cost when many subagents drive
        // one engine. reset=true zeroes the counters + restarts the window (call before a run).
        if (m == "prof_contention") {
            return emit(chudware::doc::contention_report(params.value("reset", false)), out_json, 0);
        }
        // prof_target_push / prof_target_pop {} -> persistently (un)register the CALLING
        // thread as the sampler's stack-walk target beyond this call (ref-counted, see
        // prof::set_target). Lets a GUI host sample its render thread: push at frame start,
        // pop at frame end (the CHUDWARE_SHOT=glassperf harness does exactly that). Harmless
        // when the sampler is idle; pop without a matching push is a no-op at depth 0.
        if (m == "prof_target_push") {
            chudware::prof::set_target();
            return emit(json{{"ok", true}}, out_json, 0);
        }
        if (m == "prof_target_pop") {
            chudware::prof::clear_target();
            return emit(json{{"ok", true}}, out_json, 0);
        }
        if (m == "doc_compile_cpp") {
            return handle_compile(params, out_json);
        }
        // --- P51: agent/LLM provider layer diagnostics (no live network) -------
        // These let the Python parity gate inspect request construction + response
        // parsing of the C++ provider port WITHOUT a live POST.
        if (m == "agent_build_request") {
            // {provider, model?, system?, messages, max_tokens?, base_url?} ->
            // {url, headers, body}. Pure (no network). headers as an object.
            std::string provider = params.value("provider", std::string());
            std::string system = params.value("system", std::string());
            int max_tokens = params.value("max_tokens", 8000);
            json messages = params.contains("messages") ? params["messages"] : json::array();
            chudware::agent::BuiltRequest req;
            if (provider == "anthropic") {
                std::string model = params.value("model",
                                                 std::string(chudware::agent::kAnthropicDefaultModel));
                std::string effort = params.value("reasoning_effort", std::string());
                req = chudware::agent::anthropic_build_request(model, system, messages, max_tokens,
                                                           effort);
            } else if (provider == "openai" || provider == "llamacpp") {
                std::string def = provider == "llamacpp" ? std::string("local-gguf")
                                                         : std::string(chudware::agent::kOpenAIDefaultModel);
                std::string model = params.value("model", def);
                std::string base_url = params.value("base_url", std::string());
                req = chudware::agent::openai_build_request(model, system, messages, max_tokens,
                                                         base_url);
            } else if (provider == "gemini") {
                std::string model = params.value("model",
                                                 std::string(chudware::agent::kGeminiDefaultModel));
                std::string effort = params.value("reasoning_effort", std::string());
                req = chudware::agent::gemini_build_request(model, system, messages, max_tokens, effort);
            } else {
                return emit_error("agent_build_request: unknown provider: " + provider, out_json);
            }
            json headers = json::object();
            for (const auto& h : req.headers) headers[h.first] = h.second;
            return emit(json{{"url", req.url}, {"headers", headers}, {"body", req.body_json}},
                        out_json, 0);
        }
        if (m == "agent_parse_response") {
            // {provider, response} -> {text, thinking?}.
            std::string provider = params.value("provider", std::string());
            json resp = params.contains("response") ? params["response"] : json::object();
            if (provider == "anthropic") {
                chudware::agent::ParsedReply pr = chudware::agent::anthropic_parse_response(resp);
                return emit(json{{"text", pr.text}, {"thinking", pr.thinking}}, out_json, 0);
            }
            if (provider == "openai" || provider == "llamacpp") {
                chudware::agent::ParsedReply pr = chudware::agent::openai_parse_response(resp);
                return emit(json{{"text", pr.text}}, out_json, 0);
            }
            if (provider == "gemini") {
                chudware::agent::ParsedReply pr = chudware::agent::gemini_parse_response(resp);
                return emit(json{{"text", pr.text}, {"thinking", pr.thinking}}, out_json, 0);
            }
            return emit_error("agent_parse_response: unknown provider: " + provider, out_json);
        }
        if (m == "agent_b64_file") {
            // {path} -> {b64}. base64.standard_b64encode of the file bytes.
            std::string path = params.value("path", std::string());
            std::string b64 = chudware::agent::base64_encode(chudware::agent::read_file_bytes(path));
            return emit(json{{"b64", b64}}, out_json, 0);
        }
        if (m == "agent_normalize_base_url") {
            // {base_url} -> {base_url} (the normalized form). Pure helper probe.
            std::string base_url = params.value("base_url", std::string());
            return emit(json{{"base_url", chudware::agent::openai_normalize_base_url(base_url)}},
                        out_json, 0);
        }
        if (m == "agent_complete") {
            // {provider, cfg?, system?, messages?, max_tokens?} -> {text, thinking, error, usage}.
            // For provider:"mock" this is fully offline (scripted replies in cfg.replies).
            // For anthropic/openai/llamacpp it performs a LIVE http_post (the gate only
            // drives this when an API key is present).
            std::string provider = params.value("provider", std::string());
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            std::string system = params.value("system", std::string());
            json messages = params.contains("messages") ? params["messages"] : json::array();
            int max_tokens = params.value("max_tokens", 8000);
            chudware::agent::CompleteResult cr =
                chudware::agent::complete(provider, cfg, system, messages, max_tokens);
            return emit(json{{"text", cr.text},
                             {"thinking", cr.thinking},
                             {"error", cr.error},
                             {"http_status", cr.http_status},
                             {"latency_ms", cr.latency_ms},
                             {"attempt", cr.attempt},
                             {"endpoint", cr.endpoint},
                             {"model", cr.model},
                             {"request_id", cr.request_id},
                             {"error_excerpt", cr.error_excerpt},
                             {"usage", cr.usage}},
                        out_json, 0);
        }
        if (m == "agent_role_of") {
            // {system} -> {role}. RoleScriptedProvider role sniffer probe.
            std::string system = params.value("system", std::string());
            return emit(json{{"role", chudware::agent::RoleScriptedProvider::role_of(system)}},
                        out_json, 0);
        }
        // --- P52: agent SUPPORT layer diagnostics (paths/config/ops/events/roles) ---
        // All PURE string/file-IO ports, gated byte-for-byte vs the Python oracle.
        if (m == "agent_extract_ops") {
            // {reply} -> {ops|null, error|null}.
            std::string reply = params.value("reply", std::string());
            chudware::agent::OpsResult r = chudware::agent::extract_ops(reply);
            return emit(json{{"ops", r.ops ? *r.ops : json(nullptr)},
                             {"error", r.error ? json(*r.error) : json(nullptr)}},
                        out_json, 0);
        }
        if (m == "agent_extract_revert") {
            std::string reply = params.value("reply", std::string());
            return emit(json{{"revert", chudware::agent::extract_revert(reply)}}, out_json, 0);
        }
        if (m == "agent_extract_script") {
            std::string reply = params.value("reply", std::string());
            auto s = chudware::agent::extract_script(reply);
            return emit(json{{"script", s ? json(*s) : json(nullptr)}}, out_json, 0);
        }
        if (m == "agent_first_balanced") {
            // {text, open, close} -> {span|null}.
            std::string text = params.value("text", std::string());
            std::string open = params.value("open", std::string("["));
            std::string close = params.value("close", std::string("]"));
            auto s = chudware::agent::first_balanced(text, open.empty() ? '[' : open[0],
                                                  close.empty() ? ']' : close[0]);
            return emit(json{{"span", s ? json(*s) : json(nullptr)}}, out_json, 0);
        }
        if (m == "agent_script_escape_op") {
            std::string code = params.value("code", std::string());
            std::string name = params.value("name", std::string("Script"));
            return emit(chudware::agent::script_escape_op(code, name), out_json, 0);
        }
        if (m == "agent_offline_safe") {
            json provider = params.contains("provider") ? params["provider"] : json::object();
            return emit(json{{"safe", chudware::agent::provider_offline_safe(provider)}},
                        out_json, 0);
        }
        if (m == "agent_host_is_loopback") {
            std::string base_url = params.value("base_url", std::string());
            return emit(json{{"loopback", chudware::agent::host_is_loopback(base_url)}},
                        out_json, 0);
        }
        if (m == "agent_resolve_provider") {
            // {name?, cfg, env:{...}} -> resolved descriptor or {error}.
            std::optional<std::string> name;
            if (params.contains("name") && params["name"].is_string())
                name = params["name"].get<std::string>();
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            json env = params.contains("env") ? params["env"] : json::object();
            return emit(chudware::agent::resolve_provider(name, cfg, env), out_json, 0);
        }
        if (m == "agent_list_models") {
            // {provider, base_url?, api_key?} -> {ok, models:[id...], model_rows?[{id,...}], error?}.
            // Probes the backend so the host model picker can hide unreachable / unkeyed providers.
            // model_rows is additive metadata (context/max_output/vision/tools/reasoning_efforts);
            // legacy clients keep reading flat string ids from `models`.
            std::string provider = params.value("provider", std::string());
            std::string base_url = params.value("base_url", std::string());
            std::string api_key = params.value("api_key", std::string());
            json models = json::array();
            json model_rows = json::array();
            auto push_id = [&](const std::string& id) {
                models.push_back(id);
            };
            auto push_row = [&](const std::string& id, json meta = json::object()) {
                meta["id"] = id;
                if (!meta.contains("label")) meta["label"] = id;
                model_rows.push_back(std::move(meta));
                push_id(id);
            };
            // Id-substring heuristics mirrored from model_registry / model_catalog so the
            // probe can enrich OpenAI-compat endpoints that only return bare ids.
            auto heuristic_meta = [&](const std::string& id) -> json {
                std::string mlow = id;
                for (char& c : mlow) c = (char)std::tolower((unsigned char)c);
                auto has = [&](const char* sub) {
                    return mlow.find(sub) != std::string::npos;
                };
                int context = 8192;
                bool vision = false;
                bool tools = true;
                json efforts = json::array();
                if (has("1.5-pro")) context = 2097152;
                else if (has("1.5-flash") || has("2.0") || has("2.5") || has("flash") || has("gemini"))
                    context = 1048576;
                else if (has("claude") || has("opus") || has("sonnet") || has("haiku"))
                    context = (has("opus-5") || has("sonnet-5")) ? 1000000 : 200000;
                else if (has("gpt-4.1")) context = 1047576;
                else if (has("o1") || has("o3") || has("o4") || has("reasoner") || has("r1"))
                    context = 200000;
                else if (has("gpt-4o") || has("gpt-4-turbo") || has("4o-mini")) context = 128000;
                else if (has("gpt-3.5")) context = 16385;
                else if (has("qwen") || has("deepseek")) context = 128000;
                else if (has("grok-4.5") || has("grok-4-5")) context = 500000;
                else if (has("grok")) context = 131072;
                else if (provider == "gemini") context = 1048576;
                else if (provider == "anthropic") context = 200000;
                else if (provider == "openai" || provider == "xai") context = 128000;

                if (has("vision") || has("-vl") || has("gpt-4o") || has("gemini") ||
                    has("claude") || has("grok-4") || has("4o"))
                    vision = true;
                if (provider == "gemini" || provider == "anthropic" || provider == "xai")
                    vision = true;

                if (provider == "gemini" || has("gemini")) {
                    if (!(has("2.0") && has("flash"))) {
                        if (has("3.1-pro"))
                            efforts = json::array({"low", "high"});
                        else efforts = json::array({"minimal", "low", "medium", "high"});
                    }
                } else if (provider == "anthropic" || has("claude")) {
                    if (has("-4-5"))
                        efforts = json::array({"minimal", "low", "medium", "high", "xhigh"});
                    else if (has("sonnet-4-6"))
                        efforts = json::array({"low", "medium", "high"});
                    else if (has("-4-6"))
                        efforts = json::array({"low", "medium", "high", "max"});
                    else efforts = json::array({"low", "medium", "high", "xhigh", "max"});
                } else if (provider == "xai" || has("grok")) {
                    if (!has("build") && !has("non-reasoning"))
                        efforts = json::array({"off", "low", "medium", "high"});
                } else if (has("gpt-6") || has("gpt-7") || has("gpt-5.6")) {
                    efforts = json::array({"low", "medium", "high", "xhigh", "max"});
                } else if (has("o1") || has("o3") || has("o4") || has("gpt-5")) {
                    efforts = json::array({"low", "medium", "high"});
                } else if (has("reason") || has("r1") || has("think") || has("qwen3")) {
                    efforts = json::array({"off", "low", "medium", "high"});
                }
                return json{{"context", context},
                            {"max_output", 0},
                            {"vision", vision},
                            {"tools", tools},
                            {"reasoning_efforts", efforts},
                            {"auto_source", "live+heuristic"}};
            };

            if (provider == "openai" || provider == "llamacpp" || provider == "xai" ||
                provider == "freellmapi") {
                std::string root = chudware::agent::openai_normalize_base_url(base_url);
                if (root.empty()) {
                    if (provider == "xai") root = "https://api.x.ai/v1";
                    else if (provider == "freellmapi") root = "http://localhost:3001/v1";
                    else root = "https://api.openai.com/v1";
                }
                std::vector<std::pair<std::string, std::string>> hdrs;
                if (!api_key.empty()) hdrs.push_back({"Authorization", "Bearer " + api_key});
                chudware::agent::HttpResponse hr = chudware::agent::http_get(root + "/models", hdrs);
                if (!hr.error.empty())
                    return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                                     {"error", hr.error}},
                                out_json, 0);
                if (hr.status < 200 || hr.status >= 300)
                    return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                                     {"error", "HTTP " + std::to_string(hr.status)}}, out_json, 0);
                json parsed = json::parse(hr.body, nullptr, false);
                json free = json::array();  // ids priced at $0 prompt (OpenRouter ":free" tiers etc.)
                if (parsed.is_object() && parsed.contains("data") && parsed["data"].is_array())
                    for (const auto& d : parsed["data"]) {
                        if (!d.contains("id") || !d["id"].is_string()) continue;
                        std::string id = d["id"].get<std::string>();
                        json meta = heuristic_meta(id);
                        // OpenRouter / rich OpenAI-compat rows may carry real limits.
                        if (d.contains("context_length") && d["context_length"].is_number_integer()) {
                            meta["context"] = d["context_length"].get<int>();
                            meta["auto_source"] = "live";
                        } else if (d.contains("context_length") && d["context_length"].is_number()) {
                            meta["context"] = static_cast<int>(d["context_length"].get<double>());
                            meta["auto_source"] = "live";
                        }
                        if (d.contains("architecture") && d["architecture"].is_object()) {
                            const auto& a = d["architecture"];
                            if (a.contains("input_modalities") && a["input_modalities"].is_array()) {
                                for (const auto& im : a["input_modalities"])
                                    if (im.is_string() && im.get<std::string>() == "image")
                                        meta["vision"] = true;
                            } else if (a.contains("modality") && a["modality"].is_string()) {
                                std::string mod = a["modality"].get<std::string>();
                                size_t arrow = mod.find("->");
                                std::string inp = arrow == std::string::npos ? mod : mod.substr(0, arrow);
                                if (inp.find("image") != std::string::npos) meta["vision"] = true;
                            }
                        }
                        if (d.contains("name") && d["name"].is_string())
                            meta["label"] = d["name"].get<std::string>();
                        push_row(id, std::move(meta));
                        // OpenRouter (and some others) expose per-model pricing; prompt=="0" => free.
                        if (d.contains("pricing") && d["pricing"].is_object()) {
                            const auto& pr = d["pricing"];
                            auto pp = pr.find("prompt");
                            auto cp = pr.find("completion");
                            bool pfree = pp != pr.end() && pp->is_string() && pp->get<std::string>() == "0";
                            bool cfree = cp == pr.end() ||
                                         (cp->is_string() && cp->get<std::string>() == "0");
                            // Only TEXT-ONLY-output models (real chat LLMs) — exclude free image/
                            // audio/video generators (e.g. lyria outputs ["text","audio"]) that price
                            // at $0 but can't drive the agent. Rule: output has "text" and no media.
                            bool textOut = true;
                            auto isMedia = [](const std::string& s) {
                                return s == "audio" || s == "image" || s == "video";
                            };
                            if (d.contains("architecture") && d["architecture"].is_object()) {
                                const auto& a = d["architecture"];
                                if (a.contains("output_modalities") && a["output_modalities"].is_array()) {
                                    bool hasText = false, hasMedia = false;
                                    for (const auto& om : a["output_modalities"])
                                        if (om.is_string()) {
                                            if (om.get<std::string>() == "text") hasText = true;
                                            else if (isMedia(om.get<std::string>())) hasMedia = true;
                                        }
                                    textOut = hasText && !hasMedia;
                                } else if (a.contains("modality") && a["modality"].is_string()) {
                                    std::string mod = a["modality"];
                                    size_t arrow = mod.find("->");
                                    std::string outp = arrow == std::string::npos ? mod : mod.substr(arrow + 2);
                                    textOut = outp.find("text") != std::string::npos &&
                                              outp.find("audio") == std::string::npos &&
                                              outp.find("image") == std::string::npos &&
                                              outp.find("video") == std::string::npos;
                                }
                            }
                            if (pfree && cfree && textOut) free.push_back(id);
                        }
                    }
                // The OpenAI-compatible /v1/models lists every DOWNLOADED model — including ones
                // not currently loaded into memory, which can't actually serve a request. LM
                // Studio's native REST endpoint (/api/v0/models) reports a per-model "state", so
                // when it's reachable we use it to report which ids are actually loaded. Other
                // OpenAI-compatible servers (vLLM, llama.cpp) only serve loaded models, so the
                // absence of this endpoint just means "treat all listed as available".
                // Skip LM Studio probe for cloud xAI / openai.com roots.
                bool tryLmStudio = !base_url.empty() && provider != "xai" &&
                                   provider != "freellmapi" &&
                                   root.find("api.openai.com") == std::string::npos &&
                                   root.find("api.x.ai") == std::string::npos;
                if (tryLmStudio) {
                    std::string origin = root;
                    if (origin.size() >= 3 && origin.compare(origin.size() - 3, 3, "/v1") == 0)
                        origin.erase(origin.size() - 3);
                    chudware::agent::HttpResponse lr =
                        chudware::agent::http_get(origin + "/api/v0/models", hdrs);
                    if (lr.error.empty() && lr.status >= 200 && lr.status < 300) {
                        json lp = json::parse(lr.body, nullptr, false);
                        if (lp.is_object() && lp.contains("data") && lp["data"].is_array()) {
                            json loaded = json::array();
                            for (const auto& d : lp["data"])
                                if (d.value("state", std::string()) == "loaded" &&
                                    d.contains("id") && d["id"].is_string())
                                    loaded.push_back(d["id"].get<std::string>());
                            return emit(json{{"ok", true}, {"models", models},
                                             {"model_rows", model_rows}, {"free", free},
                                             {"loaded", loaded}, {"loaded_known", true}},
                                        out_json, 0);
                        }
                    }
                }
                return emit(json{{"ok", true}, {"models", models}, {"model_rows", model_rows},
                                 {"free", free}}, out_json, 0);
            }
            if (provider == "gemini") {
                if (api_key.empty())
                    return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                                     {"error", "no api key"}},
                                out_json, 0);
                std::vector<std::pair<std::string, std::string>> hdrs = {{"x-goog-api-key", api_key}};
                chudware::agent::HttpResponse hr = chudware::agent::http_get(
                    "https://generativelanguage.googleapis.com/v1beta/models", hdrs);
                if (!hr.error.empty())
                    return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                                     {"error", hr.error}},
                                out_json, 0);
                if (hr.status < 200 || hr.status >= 300)
                    return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                                     {"error", "HTTP " + std::to_string(hr.status)}}, out_json, 0);
                json parsed = json::parse(hr.body, nullptr, false);
                if (parsed.is_object() && parsed.contains("models") && parsed["models"].is_array())
                    for (const auto& md : parsed["models"]) {
                        bool gen = false;
                        if (md.contains("supportedGenerationMethods") &&
                            md["supportedGenerationMethods"].is_array())
                            for (const auto& meth : md["supportedGenerationMethods"])
                                if (meth.is_string() && meth.get<std::string>() == "generateContent")
                                    gen = true;
                        if (gen && md.contains("name") && md["name"].is_string()) {
                            std::string nm = md["name"].get<std::string>();
                            if (nm.rfind("models/", 0) == 0) nm = nm.substr(7);
                            json meta = heuristic_meta(nm);
                            // Keep live Gemini token limits (previously discarded).
                            if (md.contains("inputTokenLimit") && md["inputTokenLimit"].is_number())
                                meta["context"] = md["inputTokenLimit"].get<int>();
                            if (md.contains("outputTokenLimit") && md["outputTokenLimit"].is_number())
                                meta["max_output"] = md["outputTokenLimit"].get<int>();
                            meta["auto_source"] = "live";
                            meta["vision"] = true;
                            meta["tools"] = true;
                            if (md.contains("displayName") && md["displayName"].is_string())
                                meta["label"] = md["displayName"].get<std::string>();
                            push_row(nm, std::move(meta));
                        }
                    }
                return emit(json{{"ok", true}, {"models", models}, {"model_rows", model_rows}},
                            out_json, 0);
            }
            if (provider == "anthropic") {
                // Pure native probe: use Anthropic's published list endpoint when the caller
                // supplied a raw API key. Without one, retain a static picker fallback but mark
                // the provider unavailable; subscription/OAuth discovery belongs to Chudcode.
                auto str_of = [](const json& o, const char* k) -> std::string {
                    if (!o.is_object()) return {};
                    auto it = o.find(k);
                    return it != o.end() && it->is_string() ? it->get<std::string>() : std::string();
                };
                std::string err;
                if (!api_key.empty()) {
                    std::vector<std::pair<std::string, std::string>> hdrs = {
                        {"x-api-key", api_key}, {"anthropic-version", "2023-06-01"}};
                    chudware::agent::HttpResponse hr = chudware::agent::http_get(
                        "https://api.anthropic.com/v1/models?limit=1000", hdrs);
                    if (hr.error.empty() && hr.status >= 200 && hr.status < 300) {
                        json parsed = json::parse(hr.body, nullptr, false);
                        if (parsed.is_object() && parsed.contains("data") &&
                            parsed["data"].is_array()) {
                            for (const auto& d : parsed["data"]) {
                                std::string id = str_of(d, "id");
                                if (id.empty()) continue;
                                json meta = heuristic_meta(id);
                                std::string disp = str_of(d, "display_name");
                                if (!disp.empty()) meta["label"] = disp;
                                meta["auto_source"] = "live";
                                push_row(id, std::move(meta));
                            }
                        }
                    } else {
                        err = hr.error.empty() ? ("HTTP " + std::to_string(hr.status)) : hr.error;
                    }
                }
                if (models.empty()) {
                    static const std::pair<const char*, const char*> kStatic[] = {
                        {"claude-opus-5-5", "Claude Opus 5.5"},
                        {"claude-sonnet-5-5", "Claude Sonnet 5.5"},
                        {"claude-opus-5", "Claude Opus 5"},
                        {"claude-sonnet-5", "Claude Sonnet 5"},
                        {"claude-fable-5", "Claude Fable 5"},
                        {"claude-mythos-5", "Claude Mythos 5"},
                        {"claude-haiku-4-5", "Claude Haiku 4.5"}};
                    for (const auto& sm : kStatic) {
                        json meta = heuristic_meta(sm.first);
                        meta["label"] = sm.second;
                        meta["auto_source"] = "heuristic";
                        push_row(sm.first, std::move(meta));
                    }
                }
                json out = {{"ok", !api_key.empty() && err.empty()},
                            {"models", std::move(models)},
                            {"model_rows", std::move(model_rows)}};
                if (api_key.empty())
                    out["error"] = "no Anthropic API key";
                else if (!err.empty())
                    out["error"] = err;
                return emit(std::move(out), out_json, 0);
            }
            return emit(json{{"ok", false}, {"models", models}, {"model_rows", model_rows},
                             {"error", "unknown provider"}},
                        out_json, 0);
        }
        if (m == "agent_cap_raw") {
            std::string text = params.value("text", std::string());
            return emit(json{{"capped", chudware::agent::cap_raw(text)}}, out_json, 0);
        }
        if (m == "agent_run_event") {
            // {run_id, kind, data} -> the emitted event (also persisted to jsonl).
            std::string run_id = params.value("run_id", std::string());
            std::string kind = params.value("kind", std::string());
            json data = params.contains("data") ? params["data"] : json::object();
            chudware::agent::RunEvents ev(run_id);
            // NOTE: a fresh RunEvents truncates events.jsonl on construct. The gate
            // drives a multi-event run via agent_run_events_batch (below) instead.
            return emit(ev.emit(kind, data), out_json, 0);
        }
        if (m == "agent_run_events_batch") {
            // {run_id, events:[{kind, data}]} -> [emitted events]. One RunEvents
            // (one truncate), then emit each — the realistic multi-event sequence.
            std::string run_id = params.value("run_id", std::string());
            chudware::agent::RunEvents ev(run_id);
            json out = json::array();
            if (params.contains("events") && params["events"].is_array())
                for (const auto& e : params["events"]) {
                    std::string kind = e.value("kind", std::string());
                    json data = e.contains("data") ? e["data"] : json::object();
                    out.push_back(ev.emit(kind, data));
                }
            return emit(std::move(out), out_json, 0);
        }
        if (m == "agent_run_events_read") {
            // {run_id} -> [parsed events] read back from events.jsonl.
            std::string run_id = params.value("run_id", std::string());
            std::filesystem::path p =
                chudware::agent::paths_cache() / "runs" / run_id / "events.jsonl";
            json out = json::array();
            std::ifstream fh(p, std::ios::binary);
            std::string line;
            while (std::getline(fh, line)) {
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (line.empty()) continue;
                out.push_back(json::parse(line));
            }
            return emit(std::move(out), out_json, 0);
        }
        if (m == "agent_load_prompt") {
            std::string role = params.value("role", std::string());
            std::string pd = params.value("prompts_dir", std::string());
            auto pr = chudware::agent::load_prompt(role, pd);
            return emit(json{{"text", pr.first}, {"source", pr.second}}, out_json, 0);
        }
        if (m == "agent_prompt_hash") {
            std::string text = params.value("text", std::string());
            return emit(json{{"hash", chudware::agent::prompt_hash(text)}}, out_json, 0);
        }
        if (m == "agent_get_prompts_v2") {
            std::string pd = params.value("prompts_dir", std::string());
            return emit(chudware::agent::get_prompts_v2(pd), out_json, 0);
        }
        if (m == "agent_load_cheatsheet") {
            std::string ft = params.value("feature_type", std::string());
            std::string pd = params.value("prompts_dir", std::string());
            return emit(json{{"text", chudware::agent::load_cheatsheet(ft, pd)}}, out_json, 0);
        }
        // --- P53a: orchestrator state machine + state/context (agent/orchestrator) ---
        if (m == "agent_est_tokens") {
            std::string text = params.value("text", std::string());
            return emit(json{{"n", chudware::agent::est_tokens(text)}}, out_json, 0);
        }
        if (m == "agent_err_sig") {
            std::string err = params.value("err", std::string());
            return emit(json{{"sig", chudware::agent::err_sig(err)}}, out_json, 0);
        }
        if (m == "agent_digest_tree") {
            // Re-parse from the RAW request text as ordered_json so object key
            // iteration preserves insertion order (matching Python dict order).
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson tree = op.contains("tree") ? op["tree"] : ojson::object();
            return emit(json{{"digest", chudware::agent::digest_tree(tree)}}, out_json, 0);
        }
        if (m == "agent_digest_params") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson p = op.contains("params") ? op["params"] : ojson::object();
            return emit(json{{"s", chudware::agent::digest_params(p)}}, out_json, 0);
        }
        if (m == "agent_looks_like_edit") {
            std::string req = params.value("request", std::string());
            return emit(json{{"edit", chudware::agent::looks_like_edit(req)}}, out_json, 0);
        }
        if (m == "agent_feature_status_errors") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            return emit(json{{"errors", chudware::agent::feature_status_errors(fs)}}, out_json, 0);
        }
        if (m == "agent_build_context") {
            // {state:{...}, role, prompts_dir?} -> {system, body, context_chars,
            // context_tokens_est}. Re-parse as ordered_json for spec/plan/memory.
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson st = op.contains("state") ? op["state"] : ojson::object();
            std::string role = op.value("role", std::string());
            std::string pd = op.value("prompts_dir", std::string());
            chudware::agent::DesignState ds;
            ds.request = st.value("request", std::string());
            ds.doc_id = st.value("doc_id", std::string());
            ds.revision = st.value("revision", 0);
            ds.spec = st.contains("spec") ? st["spec"] : ojson(nullptr);
            ds.plan = st.contains("plan") ? st["plan"] : ojson(nullptr);
            ds.tree_digest = st.value("tree_digest", std::string());
            if (st.contains("last_errors") && st["last_errors"].is_array())
                for (const auto& e : st["last_errors"]) ds.last_errors.push_back(e.get<std::string>());
            if (st.contains("last_issues") && st["last_issues"].is_array())
                for (const auto& e : st["last_issues"]) ds.last_issues.push_back(e.get<std::string>());
            ds.memory = (st.contains("memory") && st["memory"].is_object()) ? st["memory"]
                                                                            : ojson::object();
            ds.current_step = st.value("current_step", std::string());
            chudware::agent::BuiltContext bc = chudware::agent::build_context(ds, role, pd);
            return emit(json{{"system", bc.system},
                             {"body", bc.body},
                             {"context_chars", bc.context_chars},
                             {"context_tokens_est", bc.context_tokens_est}},
                        out_json, 0);
        }
        if (m == "agent_run_machine") {
            // SCRIPTED machine drive: {script:{intake:[...],...}, plan_steps:[...],
            // budgets?:{}, cancel_after?:int} -> {status, stop_reason, events:[...]}.
            json script = params.contains("script") ? params["script"] : json::object();
            json plan_steps_j = params.contains("plan_steps") ? params["plan_steps"] : json::array();
            json budgets = params.contains("budgets") ? params["budgets"] : json::object();
            bool has_cancel = params.contains("cancel_after") &&
                              params["cancel_after"].is_number_integer();
            int cancel_after = has_cancel ? params["cancel_after"].get<int>() : -1;

            // Per-stage queues of scripted outcomes (popped in call order).
            auto build_queue = [&](const char* stage) {
                std::vector<chudware::agent::StageOutcome> q;
                if (script.contains(stage) && script[stage].is_array())
                    for (const auto& o : script[stage]) {
                        chudware::agent::StageOutcome so;
                        so.verdict = o.value("verdict", std::string("pass"));
                        so.reason = o.value("reason", std::string());
                        if (o.contains("error") && !o["error"].is_null()) {
                            so.has_error = true;
                            so.error = o["error"].get<std::string>();
                        }
                        so.done = o.value("done", false);
                        so.raw = o.value("raw", std::string());
                        so.parsed_ok = o.value("parsed_ok", true);
                        if (o.contains("parse_error") && !o["parse_error"].is_null()) {
                            so.has_parse_error = true;
                            so.parse_error = o["parse_error"].get<std::string>();
                        }
                        if (o.contains("ops_count") && !o["ops_count"].is_null()) {
                            so.has_ops_count = true;
                            so.ops_count = o["ops_count"].get<int>();
                        }
                        so.duration_ms = o.value("duration_ms", 0);
                        so.no_progress = o.value("no_progress", false);
                        q.push_back(so);
                    }
                return q;
            };

            auto intake_q = std::make_shared<std::vector<chudware::agent::StageOutcome>>(
                build_queue("intake"));
            auto plan_q =
                std::make_shared<std::vector<chudware::agent::StageOutcome>>(build_queue("plan"));
            auto model_q =
                std::make_shared<std::vector<chudware::agent::StageOutcome>>(build_queue("model"));
            auto review_q =
                std::make_shared<std::vector<chudware::agent::StageOutcome>>(build_queue("review"));
            auto fix_q =
                std::make_shared<std::vector<chudware::agent::StageOutcome>>(build_queue("fix"));
            auto memory_q =
                std::make_shared<std::vector<chudware::agent::StageOutcome>>(build_queue("memory"));

            // Invocation counter for cancel_after (counts action calls = agent calls).
            auto invoked = std::make_shared<int>(0);

            auto make_action = [&](std::shared_ptr<std::vector<chudware::agent::StageOutcome>> q) {
                auto cursor = std::make_shared<size_t>(0);
                return [q, cursor, invoked](chudware::agent::DesignState& s,
                                            int) -> chudware::agent::StageOutcome {
                    (*invoked)++;
                    chudware::agent::StageOutcome o;
                    if (*cursor < q->size()) {
                        o = (*q)[*cursor];
                        (*cursor)++;
                    } else if (!q->empty()) {
                        o = q->back();  // repeat last (defensive; scripts should supply enough)
                    } else {
                        o.verdict = "pass";
                    }
                    s.last_no_progress = o.no_progress;
                    return o;
                };
            };

            chudware::agent::StageActions actions;
            actions.intake = make_action(intake_q);
            actions.plan = make_action(plan_q);
            actions.model = make_action(model_q);
            actions.review = make_action(review_q);
            actions.fix = make_action(fix_q);
            actions.memory = make_action(memory_q);
            std::vector<std::string> steps;
            if (plan_steps_j.is_array())
                for (const auto& s : plan_steps_j) steps.push_back(s.get<std::string>());
            actions.plan_steps = [steps](chudware::agent::DesignState&) { return steps; };

            chudware::agent::RunControl control;
            if (has_cancel) {
                control.cancelled = [invoked, cancel_after] {
                    return *invoked >= cancel_after;
                };
            }

            // RunEvents to a temp run dir AND capture every emitted event.
            std::string run_id = "gate-" + std::to_string(std::time(nullptr)) + "-" +
                                 std::to_string(reinterpret_cast<uintptr_t>(&actions));
            // Capture wrapper: subclass-free — we read back the jsonl after the run,
            // but to capture WITHOUT volatile run_id we just collect emitted events.
            chudware::agent::RunEvents events(run_id);
            // Drive the machine, then read the persisted events back (the source of
            // truth; identical to what emit() returned).
            chudware::agent::DesignState ds;
            ds.request = params.value("request", std::string("test request"));
            json result = chudware::agent::run_machine(ds, actions, events, budgets, control);

            // Read back captured events from the jsonl.
            json captured = json::array();
            {
                std::filesystem::path p = events.dir() / "events.jsonl";
                std::ifstream fh(p, std::ios::binary);
                std::string line;
                while (std::getline(fh, line)) {
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    if (line.empty()) continue;
                    captured.push_back(json::parse(line));
                }
            }
            result["events"] = captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_cancel") {
            // Best-effort stop at the next stage boundary / in-flight HTTP xfer.
            // {run_id} -> {ok, cancelled:bool}. Empty/missing run_id cancels ALL live
            // runs so Stop still works when run_started has not landed yet.
            const std::string run_id = jstr(params, "run_id");
            if (run_id.empty() && chudware::sim::current_cell_principal())
                return emit(cancelOwnNativeRuns(revokeThrough), out_json, 0);
            if (run_id.empty())
                return emit(chudware::agent::cancel_all_runs(), out_json, 0);
            return emit(chudware::agent::cancel_run(run_id), out_json, 0);
        }
        if (m == "agent_steer") {
            // Mid-run steering: {run_id, text} -> {ok, steered:bool}. The run folds queued
            // steering into the request at its next stage/attempt boundary (cooperative —
            // never interrupts an in-flight provider call). Unknown/finished => steered:false.
            const std::string run_id = jstr(params, "run_id");
            const std::string text = jstr(params, "text");
            return emit(chudware::agent::steer_run(run_id, text), out_json, 0);
        }
        if (m == "agent_run") {
            // Native fallback entry: {request, doc_id, options?, provider?}. It is intentionally
            // limited to deterministic offline-safe providers. Online execution belongs to
            // agent_admin_turn/Chudcode; this path retains local modeling and no-network
            // drawing/CAM fast paths.
            std::string request = jstr(params, "request");
            std::string doc_id = jstr(params, "doc_id");
            json options = params.contains("options") ? params["options"] : json::object();
            // Copilot mode overhaul: host stamps options.{mode, plan_only, approved_plan,
            // execute_as, corporate_force, scope, advisor}. Normalize defaults here so
            // run_design / run_assembly_design / run_micro_design always see a consistent
            // shape. Absent mode = legacy byte-identical behavior.
            if (!options.is_object()) options = json::object();
            // Composer attachments (pasted images / attached files) ride the payload
            // TOP-LEVEL — services/worker/worker_client.cpp forwards them alongside
            // provider/model/reasoning_effort, not inside `options`. run_design only ever
            // receives `options`, so without this relay the array is dropped right here and
            // a pasted screenshot never reaches the vision model. An attachments key already
            // inside `options` wins (an explicit nesting by a newer host is not overwritten).
            if (!options.contains("attachments") && params.contains("attachments") &&
                params["attachments"].is_array() && !params["attachments"].empty())
                options["attachments"] = params["attachments"];
            if (!options.contains("advisor")) options["advisor"] = true;  // default ON
            // Normalize explicit modes once, before either deterministic mutating shortcut.
            // Keep absent mode absent for the legacy modeling path.
            std::string mode;
            if (options.contains("mode")) {
                const auto modes = chudware::agent::CopilotModes::load();
                if (!modes)
                    return emit(json{{"ok", false}, {"status", "failed"},
                                     {"stop_reason", "mode_policy_unavailable"},
                                     {"error", "Copilot mode policy is unavailable."}}, out_json, 0);
                mode = chudware::agent::copilot_normalize_mode(*modes, jstr(options, "mode"));
                options["mode"] = mode;
            }
            const bool approved_plan_attached =
                options.contains("approved_plan") && !options["approved_plan"].is_null() &&
                !(options["approved_plan"].is_string() &&
                  options["approved_plan"].get_ref<const std::string&>().empty());
            // Plan implies plan_only unless an explicit approved plan resumes execution.
            if (mode == "plan" && !options.value("plan_only", false) &&
                !approved_plan_attached) {
                options["plan_only"] = true;
            }
            // swarm mode implies corporate_force for the Python mission path.
            if (mode == "swarm" &&
                !options.value("corporate_force", false))
                options["corporate_force"] = true;
            const bool read_only = mode == "ask" || mode == "plan" ||
                                   options.value("plan_only", false);

            // Resolve and reject before any fast path so an online provider can never be
            // accepted by native agent_run, even when the request itself is deterministic.
            json provider;
            const bool explicit_provider = params.contains("provider") &&
                                           params["provider"].is_object() &&
                                           !params["provider"].empty();
            json cfg = chudware::agent::load_config();
            json native_cfg = cfg;
            native_cfg["offline_mode"] = true;
            if (explicit_provider) {
                provider = params["provider"];
            } else {
                std::optional<std::string> pname;
                if (params.contains("provider") && params["provider"].is_string() &&
                    !params["provider"].get<std::string>().empty())
                    pname = params["provider"].get<std::string>();
                json resolved = apply_model_overrides(
                    chudware::agent::resolve_provider(pname, native_cfg, json::object()), params);
                if (resolved.is_object() && resolved.contains("error")) {
                    const std::string detail =
                        jstr(resolved, "error", "provider resolution failed");
                    const std::string error =
                        "Native agent_run could not resolve an offline-safe local provider: " +
                        detail + ". Use agent_admin_turn (agent service) for online providers.";
                    return emit(json{{"ok", false},
                                     {"status", "failed"},
                                     {"stop_reason", "provider_error"},
                                     {"error_detail", error},
                                     {"error", error}},
                                out_json, 0);
                }
                provider =
                    chudware::agent::apply_experimental(resolved, native_cfg, json::object());
            }
            if (!native_provider_offline_safe(provider)) {
                const std::string error =
                    "Native agent_run supports only deterministic offline-safe local providers; "
                    "use agent_admin_turn (agent service) for online providers.";
                return emit(json{{"ok", false},
                                 {"status", "failed"},
                                 {"stop_reason", "provider_error"},
                                 {"error_detail", error},
                                 {"error", error}},
                            out_json, 0);
            }

            // Ask never enters the modeling drivers, even with a stale approved_plan.
            // Reuse the native question implementation with a read-only tree snapshot;
            // assist hydration would rebuild the document to obtain feature statuses.
            if (mode == "ask") {
                using ojson = nlohmann::ordered_json;
                ojson doc = nullptr;
                long revision = 0;
                if (!doc_id.empty()) {
                    const json tree = chudware::doc::doc_tree(doc_id);
                    doc = tree.at("tree");
                    revision = tree.value("revision", 0L);
                }
                auto complete = chudware::agent::make_assist_provider(provider);
                const std::string answer = chudware::agent::answer_question(
                    request, doc, doc_id, revision, ojson::array(),
                    [&](const std::string& system, const std::string& body) {
                        std::string error;
                        return complete(system, body, &error);
                    });
                return emit(json{{"status", "done"}, {"stop_reason", "ask"},
                                 {"run_id", options.value("run_id", std::string("ask"))},
                                 {"doc_id", doc_id}, {"revision", revision}, {"mode", mode},
                                 {"answer", answer}, {"engaged", "ask"},
                                 {"events", json::array()}}, out_json, 0);
            }

            // ---- W17 drawing-intent fast-path ----
            // "make a drawing / blueprint of this part" doesn't need the modeler: build the 2D
            // sheet directly from the current part (deterministic standard views + auto-dims +
            // hole callouts + title block). No provider call -> works offline + shines on small
            // local models. Only fires when the active doc already has geometry to project;
            // otherwise we fall through to normal modeling (e.g. "draw a bracket" on an empty doc).
            {
                std::string lc;
                lc.reserve(request.size());
                for (char c : request) lc += (char)std::tolower((unsigned char)c);
                bool wants_drawing = lc.find("blueprint") != std::string::npos ||
                                     lc.find("drafting") != std::string::npos ||
                                     lc.find("drawing") != std::string::npos;
                if (!read_only && !approved_plan_attached && wants_drawing && !doc_id.empty()) {
                    json t = chudware::doc::doc_tree(doc_id);
                    bool has_geom = t.is_object() && t.contains("tree") && t["tree"].is_object() &&
                                    t["tree"].contains("features") && t["tree"]["features"].is_array() &&
                                    !t["tree"]["features"].empty();
                    if (has_geom) {
                        std::string part_name = t["tree"].value("name", std::string());
                        json spec = {{"views", json::array({"front", "top", "right", "iso"})},
                                     {"model_items", true}, {"hole_callouts", true}};
                        if (!part_name.empty()) spec["title"] = json{{"title", part_name}};
                        json dr;
                        try {
                            dr = chudware::doc::drawing_make(doc_id, spec);
                        } catch (const chudware::doc::OpError& oe) {
                            return emit(json{{"ok", false}, {"error", oe.what()}}, out_json, 0);
                        }
                        json events = json::array();
                        events.push_back(json{{"kind", "run_started"}, {"request", request},
                                              {"doc_id", doc_id}});
                        events.push_back(json{{"kind", "run_finished"}, {"status", "done"},
                                              {"stop_reason", "drawing"}});
                        return emit(json{
                                        {"status", "done"}, {"stop_reason", "drawing"},
                                        {"run_id", options.value("run_id", std::string("drawing"))},
                                        {"revision", t.value("revision", 0)}, {"doc_id", doc_id},
                                        {"created_drawing", true},
                                        {"drawing_doc_id", dr.value("doc_id", std::string())},
                                        {"drawing_name", part_name.empty()
                                                             ? std::string("Drawing")
                                                             : part_name + " - Drawing"},
                                        {"source_doc_id", doc_id}, {"events", events},
                                        {"engaged", "drawing"}},
                                    out_json, 0);
                    }
                }
            }

            // ---- CAM-intent fast-path ----
            // "generate toolpaths / post G-code / make a CNC program for this part" builds a
            // complete machining job from the current part deterministically (no provider) —
            // the manufacturing analogue of the drawing fast-path. Fires when the request is
            // clearly CAM-oriented (tight keyword set so it never grabs a modeling request),
            // or when the host set options.cam from the Manufacture workspace, AND the active
            // doc already has geometry. Otherwise falls through to normal modeling (so "model a
            // CNC fixture" on an empty doc still builds a part).
            {
                bool wants_cam = options.value("cam", false) ||
                                 options.value("workspace", std::string()) == "cam";
                std::string lc;
                lc.reserve(request.size());
                for (char c : request) lc += (char)std::tolower((unsigned char)c);
                auto has = [&](const char* w) { return lc.find(w) != std::string::npos; };
                const bool cam_words =
                    has("toolpath") || has("g-code") || has("gcode") || has("g code") ||
                    has("cnc") || has("post-process") || has("post process") ||
                    has("nc program") || has("machining program") || has("cam program") ||
                    has("cam job") || (has("machine") && has("part") && has("this"));
                // options.cam (Manufacture workspace) lowers the bar — but still requires a
                // manufacturing NOUN, so a modeling request typed there (e.g. "generate a
                // fillet") falls through to the modeler instead of being read as a CAM build.
                const bool cam_noun =
                    has("program") || has("toolpath") || has("g-code") || has("gcode") ||
                    has("g code") || has("cnc") || has("post") || has("machining") ||
                    has("mill") || has("router") || has("lathe") || has("nc ");
                if (!read_only && !approved_plan_attached &&
                    (cam_words || (wants_cam && cam_noun)) && !doc_id.empty()) {
                    json t = chudware::doc::doc_tree(doc_id);
                    bool has_geom = t.is_object() && t.contains("tree") && t["tree"].is_object() &&
                                    t["tree"].contains("features") &&
                                    t["tree"]["features"].is_array() &&
                                    !t["tree"]["features"].empty();
                    if (has_geom) {
                        json copts = options;
                        copts["request"] = request;
                        json prog = cam_program_from_part(doc_id, copts);
                        json events = json::array();
                        events.push_back(json{{"kind", "run_started"}, {"request", request},
                                              {"doc_id", doc_id}});
                        if (prog.value("ok", false)) {
                            const int opc = prog.value("op_count", 0);
                            const int lc2 = prog.value("line_count", 0);
                            const std::string dlt = prog.value("dialect", std::string("grbl"));
                            const std::string opsCsv = prog.value("ops_summary", std::string());
                            std::string note =
                                "Built a CAM job from the part: " + std::to_string(opc) +
                                " operation(s)" + (opsCsv.empty() ? "" : " (" + opsCsv + ")") +
                                ", posted " + std::to_string(lc2) + " lines of " + dlt +
                                " G-code to " + prog.value("gcode_path", std::string()) +
                                ". Open the Manufacture workspace to refine tools, setups, and operations.";
                            events.push_back(json{{"kind", "run_note"}, {"text", note}});
                            events.push_back(json{{"kind", "run_finished"},
                                                  {"status", "done"}, {"stop_reason", "cam"}});
                            json res = {{"status", "done"}, {"stop_reason", "cam"},
                                        {"run_id", options.value("run_id", std::string("cam"))},
                                        {"doc_id", doc_id}, {"created_cam", true},
                                        {"cam_doc_id", prog.value("cam_doc_id", std::string())},
                                        {"op_count", opc}, {"line_count", lc2}, {"dialect", dlt},
                                        {"gcode", prog.value("gcode", std::string())},
                                        {"gcode_path", prog.value("gcode_path", std::string())},
                                        {"summary", note}, {"events", events}, {"engaged", "cam"}};
                            if (prog.contains("sim")) res["sim"] = prog["sim"];
                            if (prog.contains("warnings")) res["warnings"] = prog["warnings"];
                            return emit(std::move(res), out_json, 0);
                        }
                        const std::string err = prog.value("error", std::string("CAM build failed"));
                        events.push_back(json{{"kind", "run_finished"},
                                              {"status", "error"}, {"stop_reason", "cam"}});
                        return emit(json{{"status", "error"}, {"stop_reason", "cam"},
                                         {"error", err}, {"doc_id", doc_id},
                                         {"events", events}, {"engaged", "cam"}},
                                    out_json, 0);
                    }
                }
            }


            // ---- micro vs design ----
            // should_engage reads provider.{name,base_url,context_tokens} + cfg
            // .{micro_mode,context_tokens}. The resolved descriptor carries name/
            // base_url; cfg is the ambient config.json (so micro_mode is honored).
            // Read-only plans and explicit approval resumes use run_design, never micro
            // or deterministic shortcuts, so planning and approved-plan semantics hold.
            bool force_design_path = read_only || approved_plan_attached;
            bool micro = !force_design_path && chudware::agent::should_engage(provider, cfg);

            auto captured = std::make_shared<json>(json::array());
            json result;
            if (micro) {
                chudware::agent::EmitFnMicro cb = [captured](const json& ev) {
                    captured->push_back(ev);
                };
                result = chudware::agent::run_micro_design(request, doc_id, provider, cb, options);
            } else if (force_design_path) {
                // Skip assembly multi-part driver for plan_only / approved_plan so we get
                // exactly one plan_ready (or one execute) instead of per-part side effects.
                chudware::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
                result = chudware::agent::run_design(request, doc_id, provider, cb, options);
            } else {
                chudware::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
                // run_assembly_design plans first: a true multi-part assembly builds each part in
                // its own doc + assembles; a single part delegates to run_design unchanged.
                result = chudware::agent::run_assembly_design(request, doc_id, provider, cb, options);
            }
            result["events"] = *captured;
            result["engaged"] = micro ? "micro" : "design";  // diagnostic: which profile ran
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_mission_v1") {
            // TOP-LEVEL orchestration: {request, doc_id, options?, provider?} -> decompose the
            // request into an ordered task list (design/assembly/simulate_flow/report/...), run
            // them threading the working doc forward, and return a verdict. This is the entry
            // that lets ONE message express a whole workflow ("model X, then CFD it and prove
            // it's good") instead of the old one-capability-per-message routing. Provider is
            // resolved exactly like agent_run; the flow-sim capability is wired to run_flow_sim
            // (the same path agent_simulate_v1 uses), so design->CFD chains in one run.
            std::string request = jstr(params, "request");
            std::string doc_id = jstr(params, "doc_id");
            json options = params.contains("options") ? params["options"] : json::object();

            json provider;
            const bool explicit_provider = params.contains("provider") &&
                                           params["provider"].is_object() &&
                                           !params["provider"].empty();
            json cfg = chudware::agent::load_config();
            json native_cfg = cfg;
            native_cfg["offline_mode"] = true;
            if (explicit_provider) {
                provider = params["provider"];
            } else {
                // O1: honor a provider NAME string + model/effort overrides (GUI picker).
                std::optional<std::string> pname;
                if (params.contains("provider") && params["provider"].is_string() &&
                    !params["provider"].get<std::string>().empty())
                    pname = params["provider"].get<std::string>();
                json resolved = apply_model_overrides(
                    chudware::agent::resolve_provider(pname, native_cfg, json::object()), params);
                if (resolved.is_object() && resolved.contains("error")) {
                    const std::string detail =
                        jstr(resolved, "error", "provider resolution failed");
                    const std::string error =
                        "Native agent_mission_v1 could not resolve an offline-safe local "
                        "provider: " + detail +
                        ". Use agent_admin_turn (agent service) for online providers.";
                    return emit(json{{"ok", false},
                                     {"status", "failed"},
                                     {"stop_reason", "provider_error"},
                                     {"error_detail", error},
                                     {"error", error}},
                                out_json, 0);
                }
                provider =
                    chudware::agent::apply_experimental(resolved, native_cfg, json::object());
            }
            if (!native_provider_offline_safe(provider)) {
                const std::string error =
                    "Native agent_mission_v1 supports only deterministic offline-safe local "
                    "providers; use agent_admin_turn (agent service) for online providers.";
                return emit(json{{"ok", false},
                                 {"status", "failed"},
                                 {"stop_reason", "provider_error"},
                                 {"error_detail", error},
                                 {"error", error}},
                            out_json, 0);
            }

            auto captured = std::make_shared<json>(json::array());
            chudware::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
            chudware::agent::SimFn sim = [](const std::string& sim_doc, const std::string& sim_req,
                                         const json& sim_params) -> json {
                return run_flow_sim(sim_doc, sim_req, sim_params);
            };
            json result = chudware::agent::run_mission(request, doc_id, provider, cb, sim, options);
            result["events"] = *captured;
            result["engaged"] = "mission";
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_run_design") {
            // LIVE end-to-end run: {request, doc_id, provider:{...}, options?} ->
            // {run_id, status, stop_reason, revision, doc_id, events:[...]}. The
            // provider descriptor is the resolved {name,model,api_key,...} OR the
            // role-scripted mock {name:"mock", replies:{by_role}} for the gate.
            std::string request = jstr(params, "request");
            std::string doc_id = jstr(params, "doc_id");
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json options = params.contains("options") ? params["options"] : json::object();
            // Capture every emitted event (like agent_run_machine returns `events`).
            auto captured = std::make_shared<json>(json::array());
            chudware::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
            json result = chudware::agent::run_design(request, doc_id, provider, cb, options);
            result["events"] = *captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_run_micro") {
            // MICRO-ORCHESTRATION run: {request, doc_id, provider:{...}, options?} ->
            // {run_id, status, stop_reason, revision, doc_id, events:[...]}. Parallels
            // agent_run_design (P54a).
            std::string request = jstr(params, "request");
            std::string doc_id = jstr(params, "doc_id");
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json options = params.contains("options") ? params["options"] : json::object();
            auto captured = std::make_shared<json>(json::array());
            chudware::agent::EmitFnMicro cb = [captured](const json& ev) {
                captured->push_back(ev);
            };
            json result =
                chudware::agent::run_micro_design(request, doc_id, provider, cb, options);
            result["events"] = *captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_should_engage") {
            // Pure: {provider:{name,base_url?,context_tokens?}, cfg:{micro_mode?,
            // context_tokens?}} -> {engage:bool}.
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            return emit(json{{"engage", chudware::agent::should_engage(provider, cfg)}}, out_json, 0);
        }
        if (m == "agent_micro_normalize_steps") {
            // Pure: {raw:[...]} -> {steps:[{type,desc,params}, ...]}.
            chudware::agent::ojson raw = params.contains("raw")
                                          ? chudware::agent::ojson::parse(params["raw"].dump())
                                          : chudware::agent::ojson(nullptr);
            std::vector<chudware::agent::Step> steps = chudware::agent::normalize_steps(raw);
            json out = json::array();
            for (const auto& s : steps)
                out.push_back(json{{"type", s.type}, {"desc", s.desc},
                                   {"params", json::parse(s.params.dump())}});
            return emit(json{{"steps", out}}, out_json, 0);
        }
        if (m == "agent_micro_summarize_doc") {
            // Pure: {tree, volume?} -> {summary}. volume present => "volume=N" head.
            chudware::agent::ojson tree =
                params.contains("tree") ? chudware::agent::ojson::parse(params["tree"].dump())
                                        : chudware::agent::ojson::object();
            std::optional<double> vol;
            if (params.contains("volume") && params["volume"].is_number())
                vol = params["volume"].get<double>();
            return emit(json{{"summary", chudware::agent::summarize_doc(tree, vol)}}, out_json, 0);
        }
        if (m == "agent_micro_verify_step") {
            // Pure: {step:{type,desc,params}, tree, feature_status, vol_before?, vol_after?}
            // -> {verified, notes}.
            chudware::agent::Step step;
            if (params.contains("step") && params["step"].is_object()) {
                const json& sp = params["step"];
                step.type = sp.value("type", std::string());
                step.desc = sp.value("desc", std::string());
                if (sp.contains("params") && sp["params"].is_object())
                    step.params = chudware::agent::ojson::parse(sp["params"].dump());
            }
            chudware::agent::ojson tree =
                params.contains("tree") ? chudware::agent::ojson::parse(params["tree"].dump())
                                        : chudware::agent::ojson::object();
            chudware::agent::ojson fs =
                params.contains("feature_status")
                    ? chudware::agent::ojson::parse(params["feature_status"].dump())
                    : chudware::agent::ojson::array();
            std::optional<double> vb, va;
            if (params.contains("vol_before") && params["vol_before"].is_number())
                vb = params["vol_before"].get<double>();
            if (params.contains("vol_after") && params["vol_after"].is_number())
                va = params["vol_after"].get<double>();
            chudware::agent::VerifyResult vr = chudware::agent::verify_step(step, tree, fs, vb, va);
            return emit(json{{"verified", vr.verified}, {"notes", vr.notes}}, out_json, 0);
        }
        // --- P54b: in-app LLM assists (services/agent/ask.py + services/agent/pm_assist.py) ---
        // All re-parse params_json as ordered_json so doc/form_schema/current
        // key order survives end-to-end (matching Python dict insertion order).
        if (m == "agent_build_doc_context") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            return emit(json{{"text", chudware::agent::build_doc_context(doc, doc_id, rev, fs)}},
                        out_json, 0);
        }
        if (m == "agent_build_ask_prompt") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            std::string question = op.value("question", std::string());
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            chudware::agent::AskPrompt ap =
                chudware::agent::build_ask_prompt(question, doc, doc_id, rev, fs);
            return emit(json{{"system", ap.system}, {"body", ap.body}}, out_json, 0);
        }
        if (m == "agent_answer_question") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            std::string question = jstr(op, "question");
            std::string doc_id = jstr(op, "doc_id", "?");
            // P54b host-ergonomics: hydrate doc/revision/feature_status from the live
            // C++ session when not passed explicitly (host sends just {question, doc_id}).
            hydrate_assist_from_session(op, doc_id);
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            // Provider: explicit descriptor (gate mock) OR resolved from config.json when
            // the host omits it. On an offline refusal, answer with a clear note.
            json prov = resolve_assist_provider(params);
            if (prov.is_object() && prov.contains("error")) {
                return emit(json{{"answer", std::string("I can't answer that right now: ") +
                                                jstr(prov, "error", "provider unavailable")}},
                            out_json, 0);
            }
            auto provider = chudware::agent::make_assist_provider(prov);
            std::string answer = chudware::agent::answer_question(
                question, doc, doc_id, rev, fs,
                [&](const std::string& sys, const std::string& body) {
                    std::string err;
                    return provider(sys, body, &err);  // ask path ignores err
                });
            return emit(json{{"answer", answer}}, out_json, 0);
        }
        if (m == "agent_clarify") {
            // Pre-flight clarify gate: one provider call returns the build-critical
            // questions (or ready=true) for the host's Design/Edit submit path. Mirrors
            // agent_answer_question's provider/context resolution exactly.
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            // Accept both {request} (Design submit) and {question} (defensive alias).
            std::string request = jstr(op, "request");
            if (request.empty()) request = jstr(op, "question");
            std::string doc_id = jstr(op, "doc_id", "?");
            // Hydrate doc/revision/feature_status from the live C++ session when present
            // (Edit mode); for Design-new no doc exists yet and `doc` stays null.
            hydrate_assist_from_session(op, doc_id);
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            // Provider: explicit descriptor (gate mock) OR resolved from config.json. On an
            // offline/unavailable refusal, return ready=true so the build proceeds (the host
            // also treats any clarify failure as "skip clarify").
            json prov = resolve_assist_provider(params);
            if (prov.is_object() && prov.contains("error")) {
                return emit(json{{"ready", true},
                                 {"questions", json::array()},
                                 {"assumptions", json::array()}},
                            out_json, 0);
            }
            auto provider = chudware::agent::make_assist_provider(prov);
            chudware::agent::ojson result = chudware::agent::clarify(
                request, doc, doc_id, rev, fs,
                [&](const std::string& sys, const std::string& body) {
                    std::string err;
                    return provider(sys, body, &err);  // clarify treats an error as empty reply
                });
            return emit(json::parse(result.dump()), out_json, 0);
        }
        if (m == "agent_pm_extract_json") {
            std::string reply = params.value("reply", std::string());
            chudware::agent::ojson plan = chudware::agent::pm_extract_json(reply);
            return emit(json{{"plan", json::parse(plan.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_coerce") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson field = op.contains("field") ? op["field"] : ojson::object();
            ojson value = op.contains("value") ? op["value"] : ojson(nullptr);
            ojson v = chudware::agent::pm_coerce_patch_value(field, value);
            return emit(json{{"value", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_validate_plan") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson plan = op.contains("plan") ? op["plan"] : ojson::object();
            ojson fs = op.contains("form_schema") ? op["form_schema"] : ojson::object();
            ojson cur = op.contains("current") ? op["current"] : ojson::object();
            ojson v = chudware::agent::pm_validate_plan(plan, fs, cur);
            return emit(json{{"validated", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_heuristic_plan") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson fs = op.contains("form_schema") ? op["form_schema"] : ojson::object();
            ojson cur = op.contains("current") ? op["current"] : ojson::object();
            std::string intent = op.value("intent", std::string());
            ojson v = chudware::agent::pm_heuristic_plan(fs, cur, intent);
            return emit(json{{"plan", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_build_prompt") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson prm = op.contains("params") ? op["params"] : ojson::object();
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            chudware::agent::PmAssistPrompt pp =
                chudware::agent::build_pm_assist_prompt(prm, doc, doc_id, rev, fs);
            return emit(json{{"system", pp.system}, {"body", pp.body}}, out_json, 0);
        }
        if (m == "agent_pm_assist") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson prm = op.contains("params") ? op["params"] : ojson::object();
            std::string doc_id = op.value("doc_id", std::string("?"));
            // P54b host-ergonomics: hydrate doc/revision/feature_status from the live
            // C++ session when not passed explicitly (host sends just {params, doc_id}).
            hydrate_assist_from_session(op, doc_id);
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            // Provider: explicit descriptor (gate path) OR resolved from config.json when
            // the host omits it. An EXPLICIT descriptor is always honored as-is (the gate's
            // {error:"boom"} mock signals a provider that raises — NOT a config refusal). On
            // the config-fallback path, a refusal/resolution failure ({error}) or an empty
            // descriptor leaves cb=nullptr so assist falls back to its heuristic plan.
            bool has_explicit = params.contains("provider") && !params["provider"].is_null();
            chudware::agent::PmCompleteFn cb = nullptr;
            json prov = has_explicit ? params["provider"] : resolve_assist_provider(params);
            bool usable = has_explicit
                              ? !prov.is_null()
                              : (prov.is_object() && !prov.contains("error") && !prov.empty());
            if (usable) {
                auto provider = chudware::agent::make_assist_provider(prov);
                cb = [provider](const std::string& sys, const std::string& body,
                                std::string* err) { return provider(sys, body, err); };
            }
            ojson v = chudware::agent::assist_property_manager(prm, doc, doc_id, rev, fs, cb);
            return emit(json{{"validated", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "sim_fea_static") {
            // Linear-static FEA (engine/sim/fea_static.py parity probe). Params carry an
            // ALREADY-RESOLVED problem: points[n*3], tets[m*4], E_mpa, nu, yield_mpa,
            // fixed_nodes[], nodal_forces[n*3]. Returns displacement/von_mises/summary.
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            chudware::sim::FeaStaticResult r = chudware::sim::fea_static_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("yield_mpa", 250.0),
                geti("fixed_nodes"), getd("nodal_forces"),
                params.value("nodes_per_elem", 4));
            auto emit_static = [&](json value) {
                if (r.iterative.iteration_limit > 0)
                    value["provenance"] = {
                        {"solver", r.solver},
                        {"linear_solver", chudware::sim::fea_iterative_report_json(r)}};
                return emit(std::move(value), out_json, 0);
            };
            if (!r.ok) return emit_static(json{{"ok", false}, {"error", r.error}});
            return emit_static(json{{"ok", true},
                             {"displacement", r.displacement},
                             {"disp_magnitude", r.disp_magnitude},
                             {"von_mises", r.von_mises},
                             {"n_dof", r.n_dof},
                             {"summary",
                              {{"max_von_mises_mpa", r.max_von_mises_mpa},
                               {"max_disp_mm", r.max_disp_mm},
                               {"min_fos", r.min_fos}}}});
        }
        if (m == "sim_fea_modal") {
            // Modal FEA (engine/sim/fea_modal.py parity probe). Params: points[n*3],
            // tets[m*4], E_mpa, nu, density_g_cm3, fixed_nodes[], n_modes. Returns
            // freqs_hz[] + mode_shapes[n_modes*n*3].
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            chudware::sim::FeaModalResult r = chudware::sim::fea_modal_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("density_g_cm3", 0.0),
                geti("fixed_nodes"), params.value("n_modes", 6),
                params.value("nodes_per_elem", 4));
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"freqs_hz", r.freqs_hz},
                             {"mode_shapes", r.mode_shapes},
                             {"n_modes", r.n_modes},
                             {"n_nodes", r.n_nodes},
                             {"n_dof", r.n_dof}},
                        out_json, 0);
        }
        if (m == "sim_spice") {
            // Native MNA circuit sim. Params: {circuit:{...}, study:{...}, job_id?}.
            // Returns the sim.md result shape.
            //
            // job_id makes the run cancellable: the solver polls the shared token at every
            // unbounded loop head, so `sim_cancel {job_id}` stops it. This matters because the
            // work is genuinely unbounded — a large transient reaches millions of timesteps and
            // a Monte-Carlo study tens of thousands of full re-analyses — and the solve runs
            // in-process, so an abandoned caller otherwise leaves it burning a core to
            // completion. Omitting job_id still works and costs nothing; the run is then
            // simply uncancellable. ecad_simulate registers its own scope, so only this raw
            // doc-less path needs the guard here.
            const json& circ = params.contains("circuit") ? params["circuit"] : json::object();
            const json& study = params.contains("study") ? params["study"] : json::object();
            const std::string job_id = params.value("job_id", std::string());
            chudware::sim::SimCancelToken token = chudware::sim::sim_job_register(job_id);
            // Unregister on EVERY exit path, including the throwing one — a leaked
            // registration would make a later run reusing the id cancel itself.
            struct Unregister {
                const std::string& id;
                const chudware::sim::SimCancelToken& tok;
                ~Unregister() { chudware::sim::sim_job_unregister(id, tok); }
            } unregister{job_id, token};
            return emit(chudware::sim::spice_simulate(circ, study, token), out_json, 0);
        }
        if (m == "sim_numpy_normal") {
            // DIAGNOSTIC: bit-exact-parity probe for the numpy RNG that drives the Monte-Carlo
            // (mc) SPICE analysis. Params: {"seed": int, "n": int}. Returns
            // {"draws": [...]} == np.random.default_rng(seed).standard_normal(size=n).
            long long seed = params.contains("seed") && params["seed"].is_number()
                                 ? params["seed"].get<long long>() : 0;
            int n = params.contains("n") && params["n"].is_number()
                        ? params["n"].get<int>() : 0;
            chudware::sim::NumpyGenerator rng(static_cast<uint64_t>(seed));
            json draws = json::array();
            for (int i = 0; i < n; ++i) draws.push_back(rng.standard_normal());
            return emit(json{{"draws", draws}}, out_json, 0);
        }
        if (m == "sim_netlist_parse") {
            // DIAGNOSTIC: parse a SPICE netlist deck text into the Circuit dict
            // (engine/sim/spice.py:parse_spice + subckt.py parity probe). Params:
            // {"netlist": "<deck text>"}. Returns the frozen Circuit dict (sim.md §1)
            // so the host's .cir flow can run on the C++ core (it then feeds sim_spice).
            std::string deck = params.contains("netlist") && params["netlist"].is_string()
                                   ? params["netlist"].get<std::string>()
                                   : std::string();
            return emit(chudware::sim::parse_netlist(deck), out_json, 0);
        }
        if (m == "sim_netlist_to_circuit") {
            // DIAGNOSTIC: map a schematic netlist dict (engine/ecad/netlist.py
            // build_netlist output) to a (Circuit, warnings) pair — the schematic->SPICE
            // translation ecad_simulate drives (engine/sim/spice.py:netlist_to_spice_circuit
            // parity probe). Params: {"netlist": {...}, "sim_params"?}. Returns
            // {"circuit": {...}, "warnings": [...]}.
            const json& nl = params.contains("netlist") ? params["netlist"] : json::object();
            const json& sp = params.contains("sim_params") ? params["sim_params"] : json(nullptr);
            return emit(chudware::sim::netlist_to_spice_circuit(nl, sp), out_json, 0);
        }
        if (m == "sim_lbm") {
            // D3Q19 BGK LBM wind-tunnel kernel (engine/sim/lbm.py parity probe). The
            // trimesh voxelization is NOT done here: the caller passes an ALREADY-
            // precomputed solid mask + lattice params + step count so this isolates the
            // lattice numerics (the eventual CUDA payload). Contract:
            //   mask : {"nx","ny","nz","solid":[flat (Nx,Ny,Nz) row-major 0/1 ints]}
            //          solid[(x*Ny+y)*Nz+z] != 0 => solid cell.
            //   params : {"u_lat","tau0","voxel_mm","dx_m","dt_s","rho_air"}
            //   n_steps : number of LBM time steps.
            // Returns: {"ok", nx,ny,nz, ux,uy,uz,rho (flat (Nx,Ny,Nz) row-major),
            //           cd, drag_n, lift_n, frontal_area_mm2, steps_run, diverged}.
            const json& mask = params.contains("mask") ? params["mask"] : json::object();
            const json& pp = params.contains("params") ? params["params"] : json::object();
            int nx = mask.value("nx", 0);
            int ny = mask.value("ny", 0);
            int nz = mask.value("nz", 0);
            std::vector<uint8_t> solid;
            if (mask.contains("solid") && mask["solid"].is_array()) {
                solid.reserve(mask["solid"].size());
                for (const auto& v : mask["solid"]) solid.push_back(v.get<int>() ? 1 : 0);
            }
            chudware::sim::LbmParams lp;
            lp.u_lat = pp.value("u_lat", 0.05);
            lp.tau0 = pp.value("tau0", 1.0);
            lp.voxel_mm = pp.value("voxel_mm", 1.0);
            lp.dx_m = pp.value("dx_m", 1.0e-3);
            lp.dt_s = pp.value("dt_s", 1.0e-6);
            lp.rho_air = pp.value("rho_air", 1.225);
            // Additive: optional backend select (auto|cpu|cuda). Empty keeps env/default.
            if (pp.contains("backend") && pp["backend"].is_string())
                lp.backend = pp["backend"].get<std::string>();
            else if (params.contains("backend") && params["backend"].is_string())
                lp.backend = params["backend"].get<std::string>();
            int n_steps = params.value("n_steps", 0);
            chudware::sim::LbmResult r = chudware::sim::lbm_run(solid, nx, ny, nz, lp, n_steps);
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"nx", r.nx},
                             {"ny", r.ny},
                             {"nz", r.nz},
                             {"ux", r.ux},
                             {"uy", r.uy},
                             {"uz", r.uz},
                             {"rho", r.rho},
                             {"cd", r.cd},
                             {"drag_n", r.drag_n},
                             {"lift_n", r.lift_n},
                             {"frontal_area_mm2", r.frontal_area_mm2},
                             {"steps_run", r.steps_run},
                             {"diverged", r.diverged},
                             {"backend", r.backend},
                             {"backend_fallback", r.backend_fallback}},
                        out_json, 0);
        }
#ifdef CHUDWARE_WITH_POLYMESH  // Polyhedral-FEA mesh and meshing-study RPCs
        if (m == "topology_capabilities") {
            return emit(chudware::sim::topology_capabilities(), out_json, 0);
        }
        if (m == "sim_run_topology") {
            const json& study = params.contains("study") ? params["study"] : json::object();
            return emit(chudware::sim::fea_run_topology(study), out_json, 0);
        }
    if (m == "sim_mesh") {
      // Params: {step_path, fineness?, order|mesh_order?, target_size_mm?,
      // min_size_mm?, max_size_mm?, learned_advisor?, advisor_max_dof?}.
      // Returns normalized tet4/tet10 connectivity plus quality metrics.
      std::string step_path =
          params.contains("step_path") && params["step_path"].is_string()
              ? params["step_path"].get<std::string>()
              : std::string();
      if (step_path.empty())
        return emit_error("sim_mesh: 'step_path' (string) is required",
                          out_json);
      static constexpr const char *kRemovedMeshOptions[] = {
          "density",         "curvature",        "curv_elems_per_circle",
          "optimize",        "mesher_backend",   "structured",
          "mesh_type",       "auto_type",        "quad_surface",
          "fallback_to_tet", "thin_wall_layers", "algorithm3d"};
      for (const char *key : kRemovedMeshOptions)
        if (params.contains(key))
          return emit_error(std::string("sim_mesh: option '") + key +
                                "' was removed with the Polyhedral-FEA cutover",
                            out_json);
      auto options = chudware::sim::mesh_options_from_study(params);
      options.order = params.value("order", params.value("mesh_order", 1));
      chudware::sim::report_progress("mesh.begin", 0);
      const auto mesh = chudware::sim::mesh_step_cached(step_path, options);
      const chudware::sim::MeshOut &r = *mesh;
      chudware::sim::report_progress(r.ok ? "mesh.complete" : "mesh.failed",
                                    static_cast<std::size_t>(r.n_elems));
      if (!r.ok)
        return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
      return emit(json{{"ok", true},
                       {"points", r.points},
                       {"tets", r.tets},
                       {"char_len_mm", r.char_len_mm},
                       {"n_nodes", r.n_nodes},
                       {"n_elems", r.n_elems},
                       {"mesh_quality", {
                           {"size_band", {{"applied_floor_mm", r.size_band_applied_floor_mm},
                                          {"reason", r.size_band_reason}}},
                           {"edge_mm", {{"min", r.edge_min_mm}, {"median", r.edge_median_mm},
                                        {"max", r.edge_max_mm}}},
                           {"char_len_mm", r.char_len_mm},
                           {"thin_wall_ratio", r.thin_wall_ratio}}},
                       {"provenance", {{"governor", chudware::sim::mesh_governor_json(r)}}},
                       {"nodes_per_elem", r.nodes_per_elem},
                       {"min_tet_volume_mm3", r.min_tet_volume_mm3},
                       {"max_tet_edge_ratio", r.max_tet_edge_ratio},
                       {"aspect_min", r.aspect_min},
                       {"aspect_avg", r.aspect_avg},
                       {"aspect_max", r.aspect_max},
                       {"jacobian_min", r.jacobian_min},
                       {"pct_poor", r.pct_poor},
                       {"aspect_hist", r.aspect_hist},
                       {"mesh_type_used", r.mesh_type_used},
                       {"note", r.note}},
                  out_json, 0);
    }
        if (m == "sim_run_static") {
            // P50b: combined mesh + BC-resolve + linear-static solve (the glue on
            // top of sim_mesh + sim_fea_static). Params: {"study": {...}} where the
            // study is the host's shape — step_path|points/tets, density, material,
            // fixtures:[{points,tol_mm}], loads:[{faces,type,vector|magnitude}].
            // Returns {ok, displacement, disp_magnitude, von_mises, n_dof, summary,
            // solver_notes} plus mesh_quality, reactions and provenance, and — when the
            // study carries an absolute "vtu_path" — a VTK UnstructuredGrid written
            // there and echoed back as "vtu_path". (overlay/npz are display-only.)
            const json& study = params.contains("study") ? params["study"] : json::object();
            json res = chudware::sim::fea_run_static(study);
            // P55b: when the study carries a STEP path, ALSO package a display overlay
            // (best-effort — mirrors the Python worker _static_overlay_path; an overlay
            // failure NEVER fails the solve). Existing result fields are untouched.
            if (res.value("ok", false) && study.contains("step_path") &&
                study["step_path"].is_string()) {
                const std::string step_path = study["step_path"].get<std::string>();
                if (std::filesystem::exists(step_path) && res.contains("von_mises")) {
                    // A STEP-backed solve always meshes the STEP with the active options;
                    // caller-supplied inline points/tets are intentionally ignored by
                    // resolve_mesh. Recover that exact cached mesh for the overlay too.
                    std::vector<double> vol;
                    std::vector<int> tets;
                    int npe = 4;
                    const auto options = chudware::sim::mesh_options_from_study(study);
          auto mo = chudware::sim::mesh_step_cached(step_path, options);
                    if (mo->ok) {
                        vol = mo->points;
                        tets = mo->tets;
                        npe = mo->nodes_per_elem;
                    }
                    std::vector<double> vm = flat_doubles(res.value("von_mises", json::array()));
                    std::vector<double> dm = flat_doubles(res.value("disp_magnitude", json::array()));
                    std::vector<double> dv = flat_doubles(res.value("displacement", json::array()));
                    std::vector<double> p1 = flat_doubles(res.value("principal1", json::array()));
                    std::vector<double> p3 = flat_doubles(res.value("principal3", json::array()));
                    std::vector<double> ms = flat_doubles(res.value("max_shear", json::array()));
                    double yield_mpa = 250.0;
                    if (study.contains("material") && study["material"].is_object())
                        yield_mpa = study["material"].value("yield_mpa", 250.0);
                    json summary = res.contains("summary") ? res["summary"] : json::object();
                    std::string ov =
                        chudware::sim::static_overlay(step_path, vol, tets, npe, vm, dm, dv,
                                                   p1, p3, ms, yield_mpa, summary);
                    if (!ov.empty()) res["overlay_aicm"] = ov;
                }
            }
            return emit(std::move(res), out_json, 0);
        }
        if (m == "sim_run_adaptive") {
            // Error-driven static solve: mesh, solve, score the discretization error,
            // refine where the error is, repeat. Params: {"study": {...}} — an ordinary
            // sim_run_static study (step_path REQUIRED; an inline mesh cannot be refined)
            // plus an optional "adapt":{max_passes, eta_target}. Returns the final pass's
            // sim_run_static payload plus "adapt":{passes[], global_eta, eta_target,
            // eta_target_met, passes_run, stop_reason} and, unlike sim_run_static, an
            // "overlay_aicm" already built on the FINAL refined mesh. An optional
            // study "vtu_path" is written from the FINAL pass only.
            const json& study = params.contains("study") ? params["study"] : json::object();
            return emit(chudware::sim::fea_run_adaptive(study), out_json, 0);
        }
        if (m == "sim_run_fatigue") {
            // High-cycle fatigue study (Goodman/Gerber/Soderberg + S-N life). Reuses
            // the static stress field; reads study["fatigue"] for the method/load
            // case + strength overrides. The overlay (when a STEP path is present) is
            // packaged inside fea_run_fatigue and returned as "overlay_aicm".
            const json& study = params.contains("study") ? params["study"] : json::object();
            json res = chudware::sim::fea_run_fatigue(study);
            return emit(std::move(res), out_json, 0);
        }
        if (m == "sim_run_thermal") {
            // Combined mesh + BC-resolve + steady-state heat-conduction solve
            // (fea_run_thermal on top of the fea_thermal kernel). Params: {"study": {...}}
            // where the study is step_path|points/tets, material.k_w_mk, fixtures with
            // temp_c (Dirichlet), loads of type "flux"/"convection". Returns {ok,
            // temperature, n_nodes, n_dof, n_elems, summary:{max_temp_c,min_temp_c},
            // flux_summary, solver_notes} plus mesh_quality/provenance, and an optional
            // study "vtu_path" carrying the final temperature field.
            const json& study = params.contains("study") ? params["study"] : json::object();
            json res = chudware::sim::fea_run_thermal(study);
            return emit(std::move(res), out_json, 0);
        }
        if (m == "sim_run_modal") {
            // P50b: combined mesh + fixture-resolve + modal solve. Params:
            // {"study": {...}, "n_modes": <int, default 6>}. Returns {ok, freqs_hz,
            // mode_shapes, n_modes, n_nodes, n_dof} plus mesh_quality/provenance, and an
            // optional study "vtu_path" with one 3-vector field per mode.
            const json& study = params.contains("study") ? params["study"] : json::object();
            int n_modes = params.value("n_modes", 6);
            json res = chudware::sim::fea_run_modal(study, n_modes);
            // P55b: when the study carries a STEP path, ALSO package a display overlay
            // for mode 0 (best-effort — mirrors the Python worker _modal_overlay_path;
            // an overlay failure NEVER fails the solve). Existing fields are untouched.
            if (res.value("ok", false) && study.contains("step_path") &&
                study["step_path"].is_string()) {
                const std::string step_path = study["step_path"].get<std::string>();
                if (std::filesystem::exists(step_path) && res.contains("mode_shapes") &&
                    res["mode_shapes"].is_array() && !res["mode_shapes"].empty() &&
                    res.contains("freqs_hz") && res["freqs_hz"].is_array() &&
                    !res["freqs_hz"].empty()) {
                    // fea_run_modal defaults STEP studies to tet10. Recover the same
                    // content/options-keyed mesh rather than reinterpreting it as tet4.
                    std::vector<double> vol;
                    std::vector<int> tets;
                    int npe = 4;
                    auto options = chudware::sim::mesh_options_from_study(study);
                    options.order = study.value("mesh_order", 2);
          auto mo = chudware::sim::mesh_step_cached(step_path, options);
                    if (mo->ok) {
                        vol = mo->points;
                        tets = mo->tets;
                        npe = mo->nodes_per_elem;
                    }

                    // mode_shapes is serialized as one flat [mode][node][xyz] array.
                    // Extract exactly the first mode; indexing the JSON array first
                    // produces a scalar and silently yielded an empty overlay.
                    const int n_nodes = res.value("n_nodes", 0);
                    const std::vector<double> modes = flat_doubles(res["mode_shapes"]);
                    const size_t mode_size =
                        n_nodes > 0 ? static_cast<size_t>(n_nodes) * 3u : 0u;
                    const double freq = res["freqs_hz"][0].get<double>();
                    if (mode_size > 0 && modes.size() >= mode_size &&
                        std::isfinite(freq) &&
                        std::all_of(modes.begin(), modes.begin() + mode_size,
                                    [](double value) { return std::isfinite(value); })) {
                        std::vector<double> mode0(modes.begin(), modes.begin() + mode_size);
                        std::string ov = chudware::sim::modal_overlay(
                            step_path, vol, tets, npe, mode0, freq);
                        if (!ov.empty()) res["overlay_aicm"] = ov;
                    }
                }
            }
            return emit(std::move(res), out_json, 0);
        }
        if (m == "sim_run_buckling") {
            // Engine-only real-geometry prestress + eigenvalue solve; same C ABI
            // and study envelope as modal, with the static reference loads.
            if (params.contains("n_modes") &&
                (!params["n_modes"].is_number_integer() ||
                 params["n_modes"] < 1 || params["n_modes"] > 100))
                return emit(json{
                                {"ok", false},
                                {"error", "fea_buckling: n_modes must be an integer from 1 to 100"},
                                {"reason", "fea_buckling: n_modes must be an integer from 1 to 100"}},
                            out_json, 0);
            if (params.contains("max_load_factor") &&
                !params["max_load_factor"].is_number())
                return emit(json{
                                {"ok", false},
                                {"error", "fea_buckling: max_load_factor must be numeric"},
                                {"reason", "fea_buckling: max_load_factor must be numeric"}},
                            out_json, 0);
            const json& study = params.contains("study") ? params["study"] : json::object();
            return emit(chudware::sim::fea_run_buckling(
                            study, params.value("n_modes", 4),
                            params.value("max_load_factor", 1e4)), out_json, 0);
        }
        if (m == "aicm_scalar_chunk") {
            // DIAGNOSTIC (byte-contract proof): return the SCLR chunk bytes (hex) for a
            // crafted (body_id, name, values, vmin, vmax) so the gate can byte-compare
            // against engine.doc.tessellate.scalar_chunk.
            uint32_t body_id = params.value("body_id", 1u);
            std::string name = params.value("name", std::string());
            std::vector<double> vals_d = flat_doubles(params.value("values", json::array()));
            std::vector<float> vals(vals_d.begin(), vals_d.end());
            float vmin = static_cast<float>(params.value("vmin", 0.0));
            float vmax = static_cast<float>(params.value("vmax", 0.0));
            std::vector<uint8_t> bytes = aicm::scalar_chunk(body_id, name, vals, vmin, vmax);
            return emit(json{{"hex", to_hex(bytes)}}, out_json, 0);
        }
        if (m == "aicm_disp_chunk") {
            // DIAGNOSTIC: return the DISP chunk bytes (hex) for crafted (body_id,
            // scale_hint, vectors) — byte-compares against tessellate.disp_chunk.
            uint32_t body_id = params.value("body_id", 1u);
            float scale_hint = static_cast<float>(params.value("scale_hint", 1.0));
            std::vector<double> vec_d = flat_doubles(params.value("vectors", json::array()));
            std::vector<float> vec(vec_d.begin(), vec_d.end());
            std::vector<uint8_t> bytes = aicm::disp_chunk(body_id, scale_hint, vec);
            return emit(json{{"hex", to_hex(bytes)}}, out_json, 0);
        }
#endif  // CHUDWARE_WITH_POLYMESH
        if (m == "sim_map_to_surface") {
            // DIAGNOSTIC (map value-parity): nearest-volume-node mapping. Accepts
            // {surf:[[x,y,z]...], vol:[[x,y,z]...], values:[...]| [[...]...], dim?}.
            // dim is inferred from values shape when omitted (3 if nested triples, else 1).
            std::vector<double> surf = flat_doubles(params.value("surf", json::array()));
            std::vector<double> vol = flat_doubles(params.value("vol", json::array()));
            const json& vj = params.value("values", json::array());
            int dim = 1;
            if (params.contains("dim") && params["dim"].is_number()) {
                dim = params["dim"].get<int>();
            } else if (vj.is_array() && !vj.empty() && vj[0].is_array()) {
                dim = static_cast<int>(vj[0].size());
            }
            std::vector<double> values = flat_doubles(vj);
            std::vector<double> mapped = chudware::sim::map_to_surface(surf, vol, values, dim);
            return emit(json{{"mapped", mapped}, {"dim", dim}}, out_json, 0);
        }
        if (m == "sim_static_overlay") {
            // P55b: package a static-result overlay AICM from a STEP + volume results.
            // {step_path, points[n*3], von_mises[n], disp_magnitude[n],
            //  displacement[n*3], yield_mpa?, summary?} -> {overlay_aicm}.
            const std::string step_path = params.value("step_path", std::string());
            std::vector<double> pts = flat_doubles(params.value("points", json::array()));
            std::vector<int> tets = flat_ints(params.value("tets", json::array()));
            int npe = params.value("nodes_per_elem", 4);
            std::vector<double> vm = flat_doubles(params.value("von_mises", json::array()));
            std::vector<double> dm = flat_doubles(params.value("disp_magnitude", json::array()));
            std::vector<double> dv = flat_doubles(params.value("displacement", json::array()));
            std::vector<double> p1 = flat_doubles(params.value("principal1", json::array()));
            std::vector<double> p3 = flat_doubles(params.value("principal3", json::array()));
            std::vector<double> ms = flat_doubles(params.value("max_shear", json::array()));
            double yield_mpa = params.value("yield_mpa", 250.0);
            json summary = params.contains("summary") ? params["summary"] : json::object();
            std::string ov =
                chudware::sim::static_overlay(step_path, pts, tets, npe, vm, dm, dv, p1, p3,
                                           ms, yield_mpa, summary);
            if (ov.empty()) return emit_error("sim_static_overlay: overlay packaging failed", out_json);
            return emit(json{{"overlay_aicm", ov}}, out_json, 0);
        }
        if (m == "sim_modal_overlay") {
            // P55b: package a single-mode modal overlay AICM.
            // {step_path, points[n*3], mode_shape[n*3], freq_hz?} -> {overlay_aicm}.
            const std::string step_path = params.value("step_path", std::string());
            std::vector<double> pts = flat_doubles(params.value("points", json::array()));
            std::vector<int> tets = flat_ints(params.value("tets", json::array()));
            int npe = params.value("nodes_per_elem", 4);
            std::vector<double> shp = flat_doubles(params.value("mode_shape", json::array()));
            double freq_hz = params.value("freq_hz", 0.0);
            std::string ov =
                chudware::sim::modal_overlay(step_path, pts, tets, npe, shp, freq_hz);
            if (ov.empty()) return emit_error("sim_modal_overlay: overlay packaging failed", out_json);
            return emit(json{{"overlay_aicm", ov}}, out_json, 0);
        }
        if (m == "doc_validate") {
            // DIAGNOSTIC: structural-validate a document tree (schema.validate_document
            // parity probe). Accepts {"doc":{...}} or a top-level {"features":[...]}.
            json doc;
            if (params.contains("doc")) doc = params["doc"];
            else if (params.contains("features")) doc = params;
            else return emit_error("doc_validate: params need a 'doc' object", out_json);
            std::vector<std::string> errs = chudware::doc::validate_document(doc);
            return emit(json{{"ok", errs.empty()}, {"errors", errs}}, out_json, 0);
        }
        // --- stateful document session RPCs (chudware::doc::session) ---------------
        // Each returns the host's ops-response JSON. An OpError becomes a nonzero
        // return carrying the {code,message,detail} envelope (so the host can treat
        // it as a structured error / fall back to Python).
        try {
            if (m == "doc_new")
                return emit(chudware::doc::doc_new(params.value("kind", std::string("part")),
                                                params.value("name", std::string()),
                                                params.value("resume_if_open", false),
                                                params.value("project_dir", std::string())),
                            out_json, 0);
            if (m == "doc_apply_ops")
                return emit(chudware::doc::doc_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array(),
                                params.value("coalesce", false),
                                params.value("with_tess", true)),
                            out_json, 0);
            if (m == "doc_dry_run_ops")
                return emit(chudware::doc::doc_dry_run_ops(
                                params.value("doc_id", std::string()),
                                params.contains("ops") ? params["ops"] : json::array(),
                                params.value("budget_ms", 0)),
                            out_json, 0);
            if (m == "doc_undo")
                return emit(chudware::doc::doc_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_redo")
                return emit(chudware::doc::doc_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_clear_history")
                return emit(chudware::doc::doc_clear_history(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "doc_tree")
                return emit(chudware::doc::doc_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_rebuild")
                return emit(chudware::doc::doc_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_activate")
                return emit(chudware::doc::doc_activate(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_solve_mates")
                return emit(chudware::doc::doc_solve_mates(
                                params.value("doc_id", std::string()),
                                params.contains("drag") ? params["drag"] : json(nullptr)),
                            out_json, 0);
            if (m == "doc_set_explode_state")
                return emit(chudware::doc::doc_set_explode_state(
                                params.value("doc_id", std::string()),
                                params.value("active", false),
                                params.contains("factor") && params["factor"].is_number()
                                    ? params["factor"].get<double>() : 1.0),
                            out_json, 0);
            if (m == "doc_set_display_quality")
                return emit(chudware::doc::doc_set_display_quality(
                                params.value("doc_id", std::string()),
                                params.value("quality", std::string("fine"))),
                            out_json, 0);
            if (m == "asm_world_bodies")
                return emit(chudware::doc::doc_asm_world_bodies(
                                params.value("doc_id", std::string()),
                                params.value("explode", false)),
                            out_json, 0);
            if (m == "doc_close")
                return emit(json{{"closed", chudware::doc::doc_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "doc_list")
                return emit(chudware::doc::doc_list(), out_json, 0);
            if (m == "doc_list_ops") {
                // Closed vocabulary for add_features / doc_apply_ops — single source
                // from op_registry (must match apply_one). Python MCP + agent middleware
                // query this so they never hand-copy the list.
                json ops = json::array();
                for (const auto& k : chudware::doc::doc_op_kinds()) ops.push_back(k);
                json ftypes = json::array();
                for (const auto& t : chudware::doc::feature_type_names()) ftypes.push_back(t);
                return emit(json{
                    {"ok", true},                    {"ops", ops},
                    {"feature_types", ftypes},
                    {"example_add_feature", json{
                        {"op", "add_feature"},
                        {"feature", {
                            {"type", "box"},
                            {"name", "Block"},
                            {"params", {
                                {"length", {{"value", 40}, {"expr", nullptr}}},
                                {"width",  {{"value", 30}, {"expr", nullptr}}},
                                {"height", {{"value", 20}, {"expr", nullptr}}},
                            }},
                        }},
                    }},
                    {"cheatsheet", chudware::doc::doc_ops_cheatsheet_snippet()},
                    {"hint", "Feature types (box/cylinder/sketch/…) are NOT ops — wrap with "
                             "op=add_feature. Call cad_guide() for worked examples."},
                }, out_json, 0);
            }
            if (m == "doc_formats")
                return emit(chudware::doc::doc_formats(), out_json, 0);
            if (m == "doc_checkpoint")
                return emit(chudware::doc::doc_checkpoint(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_revert")
                return emit(chudware::doc::doc_revert(params.value("doc_id", std::string()),
                                                   params.value("checkpoint", std::string()),
                                                   params.contains("expect_revision") && params["expect_revision"].is_number_integer()
                                                       ? params["expect_revision"].get<long>() : -1),
                            out_json, 0);
            if (m == "doc_refresh_derived")
                return emit(chudware::doc::doc_refresh_derived(params.value("doc_id", std::string()),
                                                            params.value("id", std::string())),
                            out_json, 0);
            if (m == "config_list")
                return emit(chudware::doc::doc_config_list(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "config_add")
                return emit(chudware::doc::doc_config_add(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.contains("copy_from") ? params["copy_from"] : json(nullptr)),
                            out_json, 0);
            if (m == "config_delete")
                return emit(chudware::doc::doc_config_delete(params.value("doc_id", std::string()),
                                                          params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_activate")
                return emit(chudware::doc::doc_config_activate(params.value("doc_id", std::string()),
                                                            params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_set_override")
                return emit(chudware::doc::doc_config_set_override(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("param", std::string()),
                                params.contains("value") ? params["value"] : json(nullptr),
                                params.value("clear", false)),
                            out_json, 0);
            if (m == "config_set_suppression")
                return emit(chudware::doc::doc_config_set_suppression(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("suppressed", false)),
                            out_json, 0);
            if (m == "sim_study_list")
                return emit(chudware::doc::doc_sim_study_list(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "sim_study_add")
                return emit(chudware::doc::doc_sim_study_add(
                                params.value("doc_id", std::string()),
                                params.contains("study") ? params["study"] : json(nullptr)),
                            out_json, 0);
            if (m == "sim_study_edit")
                return emit(chudware::doc::doc_sim_study_edit(
                                params.value("doc_id", std::string()),
                                params.value("study_id", std::string()),
                                params.contains("study") ? params["study"] : json(nullptr)),
                            out_json, 0);
            if (m == "sim_study_delete")
                return emit(chudware::doc::doc_sim_study_delete(
                                params.value("doc_id", std::string()),
                                params.value("study_id", std::string())),
                            out_json, 0);
            if (m == "doc_file_operations")
                return emit(chudware::doc::native_file_operations(params), out_json, 0);
            if (m == "doc_save")
                return emit(chudware::doc::doc_save(params.value("doc_id", std::string()),
                                                 params.value("path", std::string()),
                                                 params.value("preserve_path", false)),
                            out_json, 0);
            if (m == "doc_open")
                return emit(chudware::doc::doc_open(params.value("path", std::string()),
                                                 params.value("as_solid", false),
                                                 params.value("detach_path", false)),
                            out_json, 0);
            if (m == "doc_resolve_pick")
                return emit(chudware::doc::doc_resolve_pick(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("kind", std::string()),
                                params.value("topo_id", 0)),
                            out_json, 0);
            if (m == "doc_face_edges")
                return emit(chudware::doc::doc_face_edges(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("topo_id", 0)),
                            out_json, 0);
            if (m == "doc_push_pull")
                return emit(chudware::doc::doc_push_pull(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("topo_id", 0),
                                params.contains("distance") && params["distance"].is_number()
                                    ? params["distance"].get<double>() : 0.0,
                                params.value("commit", false)),
                            out_json, 0);
            if (m == "doc_export_step")
                return emit(chudware::doc::doc_export_step(
                                params.value("doc_id", std::string()),
                                params.value("path", std::string())),
                            out_json, 0);
            if (m == "parts_catalog_list")
                // The parametric standard-parts catalog (families + sizes + lengths)
                // for the GUI library browser and the agent context. No doc needed.
                return emit(chudware::parts::catalog_json(), out_json, 0);
            if (m == "parts_make") {
                // {family, size, length_mm?, options?, name?, save_path?} -> build a
                // NEW editable part doc from the catalog generator. With save_path,
                // also export a .step so an assembly can asm_insert_component it.
                int rc = 0;
                return emit(parts_make_impl(params, rc), out_json, rc);
            }
            // `background` (optional [r,g,b] 0-255) rides all three render entries: the engine
            // clears the frame to it instead of the constant 245 light grey. Absent/null keeps
            // the legacy pixels exactly.
            if (m == "doc_render_png")
                return emit(chudware::doc::doc_render_png(
                                params.value("doc_id", std::string()),
                                params.value("out_path", std::string()),
                                params.value("annotate", true),
                                params.contains("background") ? params.at("background")
                                                              : json(nullptr)),
                            out_json, 0);
            if (m == "doc_render_view")
                return emit(chudware::doc::doc_render_view(
                                params.value("doc_id", std::string()),
                                params.value("out_path", std::string()),
                                params.contains("view") ? params.at("view") : json(nullptr),
                                params.value("annotate", true),
                                params.contains("background") ? params.at("background")
                                                              : json(nullptr)),
                            out_json, 0);
            // Exact per-camera visibility metrics for the shot planner (contracts/worker_rpc.md
            // "doc_view_metrics"); the whole params object carries doc_id/size/cameras/sections.
            if (m == "doc_view_metrics")
                return emit(chudware::doc::doc_view_metrics(params), out_json, 0);
            // PLM graphical revision diff — the whole params object goes through (doc_a/doc_b,
            // body_a/body_b, out_path, view, width/height, mode).
            if (m == "doc_render_diff")
                return emit(chudware::doc::doc_render_diff(params), out_json, 0);
            if (m == "doc_render_contact_sheet")
                return emit(chudware::doc::doc_render_contact_sheet(
                                params.value("doc_id", std::string()),
                                params.value("out_path", std::string()),
                                params.contains("views") ? params.at("views") : json::array(),
                                params.contains("background") ? params.at("background")
                                                              : json(nullptr)),
                            out_json, 0);
            if (m == "drawing_render_png")
                return emit(chudware::doc::drawing_render_png(
                                params.value("doc_id", std::string()),
                                params.value("out_path", std::string())),
                            out_json, 0);
            if (m == "sim_run_flow") {
                // STL-shaped call (host sim-panel flow path; formerly the Python sim
                // worker): voxelize the STL + run the tunnel via run_flow_stl. The
                // doc_id shape below tessellates a document instead.
                if (params.contains("stl_path") && params["stl_path"].is_string() &&
                    !params["stl_path"].get<std::string>().empty())
                    return emit(chudware::sim::run_flow_stl(params), out_json, 0);
                // {doc_id, velocity, aoa?, fluid?, cells?, n_steps?} -> wind-tunnel LBM forces.
                std::string doc_id = params.value("doc_id", std::string());
                std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(doc_id);
                if (bodies.empty())
                    return emit(json{{"ok", false}, {"error", "no solid geometry to simulate"}},
                                out_json, 0);
                std::vector<std::vector<float>> bp;
                std::vector<std::vector<uint32_t>> bi;
                for (auto& b : bodies) { bp.push_back(b.pos); bi.push_back(b.idx); }
                chudware::sim::FlowParams fp;
                fp.velocity_ms = params.value("velocity", 10.0);
                fp.aoa_deg = params.value("aoa", 0.0);
                std::string fluid = params.value("fluid", std::string("air"));
                if (fluid == "water") { fp.nu_fluid = 1.0e-6; fp.rho_fluid = 1000.0; }
                fp.cells = params.value("cells", 96);
                fp.n_steps = params.value("n_steps", 2000);
                fp.progress_path = params.value("progress_path", std::string());
                fp.job_id = params.value("job_id", std::string());  // sim_cancel registry key
                double re = 0.0;
                chudware::sim::LbmResult r = chudware::sim::run_wind_tunnel(bp, bi, fp, &re);
                if (!r.ok)
                    return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
                json flow{{"ok", true},
                          {"drag_n", r.drag_n},
                          {"lift_n", r.lift_n},
                          {"side_n", r.side_n},
                          {"cd", r.cd},
                          {"frontal_area_mm2", r.frontal_area_mm2},
                          {"reynolds", re},
                          {"ref_length_mm", r.ref_length_mm},
                          {"velocity_ms", fp.velocity_ms},
                          {"aoa_deg", fp.aoa_deg},
                          {"fluid", fluid},
                          {"steps_run", r.steps_run},
                          {"diverged", r.diverged},
                          {"converged", r.converged},
                          {"residual", r.residual},
                          {"mass_imbalance", r.mass_imbalance},
                          {"voxel_mm", r.voxel_mm},
                          {"min_feature_mm", r.min_feature_mm},
                          {"min_feature_voxels", r.min_feature_voxels},
                          {"grid", {r.nx, r.ny, r.nz}}};
                if (!r.warning.empty()) flow["warning"] = r.warning;
                return emit(std::move(flow), out_json, 0);
            }
            if (m == "sim_run_pipe_flow") {
                // Native internal-flow LBM over a closed fluid volume (core/sim/pipe_flow.h):
                // {stl_path | doc_id (+body)}, inlet/outlet planes, fluid, velocity or flow,
                // optional piping + run_id for the fittings-aware 1-D comparison
                // (piping::run_loss_1d — piping_solve's friction + K-factors).
                json p = params;
                // Fluid: explicit density_kgm3 + viscosity_pas win; otherwise a piping
                // fluid_properties name (default water) at temp_c / gauge pressure_kpa.
                if (!(p.contains("density_kgm3") && p.contains("viscosity_pas"))) {
                    if (p.contains("fluid") && !p["fluid"].is_null() && !p["fluid"].is_string())
                        return emit(json{{"ok", false},
                                         {"reason", "invalid_params"},
                                         {"hint", "fluid must be a name (water, air, methane, "
                                                  "natural_gas, nitrogen, carbon_dioxide); pass "
                                                  "density_kgm3 + viscosity_pas for a custom fluid"}},
                                    out_json, 0);
                    const std::string name = p.value("fluid", std::string("water"));
                    const double t_k = p.value("temp_c", 20.0) + chudware::piping::T0_C;
                    const double p_pa =
                        p.value("pressure_kpa", 0.0) * 1000.0 + chudware::piping::P_ATM;
                    const auto fp = chudware::piping::fluid_properties(
                        chudware::piping::ojson(name), t_k, p_pa);
                    if (!fp.value("ok", false))
                        return emit(json{{"ok", false},
                                         {"reason", "unknown_fluid"},
                                         {"hint", fp.value("reason", std::string()) + "; " +
                                                      fp.value("hint", std::string())}},
                                    out_json, 0);
                    p["density_kgm3"] = fp["rho"].get<double>();
                    p["viscosity_pas"] = fp["mu"].get<double>();
                    p["fluid"] = fp.value("name", name);
                }
                json r;
                if (p.contains("stl_path") && p["stl_path"].is_string() &&
                    !p["stl_path"].get<std::string>().empty()) {
                    r = chudware::sim::run_pipe_flow_stl(p, &chudware::piping::run_loss_1d);
                } else {
                    const std::string doc_id = p.value("doc_id", std::string());
                    if (doc_id.empty())
                        return emit(json{{"ok", false},
                                         {"reason", "invalid_params"},
                                         {"hint", "pass stl_path or doc_id (+ optional body) of "
                                                  "the closed fluid volume"}},
                                    out_json, 0);
                    const std::string body = p.value("body", std::string());
                    std::vector<chudware::sim::PipeFlowShell> shells;
                    json names = json::array();
                    for (const auto& b : chudware::doc::doc_tessellate_bodies(doc_id)) {
                        names.push_back(b.name);
                        if (!body.empty() && b.name != body) continue;
                        shells.push_back({std::vector<float>(b.pos.begin(), b.pos.end()),
                                          std::vector<uint32_t>(b.idx.begin(), b.idx.end())});
                    }
                    if (shells.empty())
                        return emit(
                            json{{"ok", false},
                                 {"reason", body.empty() ? "no_geometry" : "unknown_body"},
                                 {"hint", body.empty()
                                              ? "doc " + doc_id + " has no solid fluid volume"
                                              : "no body named '" + body + "'; bodies: " +
                                                    names.dump()}},
                            out_json, 0);
                    r = chudware::sim::run_pipe_flow(shells, p, &chudware::piping::run_loss_1d);
                }
                if (r.value("ok", false) && r.contains("provenance"))
                    r["provenance"]["engine_build_id"] = CHUDWARE_HOSTD_BUILD_ID;
                return emit(std::move(r), out_json, 0);
            }
            if (m == "sim_cancel") {
                // Cancel a registered sim job (core/sim/sim_cancel.cpp — same
                // registry semantics as agent cancel: idempotent, miss = cancelled:false).
                json result = chudware::sim::sim_cancel(params.value("job_id", std::string()),
                                                      revokeThrough);
                const int rc = result.value("ok", true) ? 0 : 1;
                return emit(std::move(result), out_json, rc);
            }
            if (m == "agent_simulate_v1") {
                // "AI runs CFD": {doc_id, request} (+ optional explicit velocity/aoa/fluid) ->
                // parse a flow condition from natural language, run the wind tunnel, return forces
                // + a human summary. No LLM needed (regex parse), so it works fully offline. The
                // body lives in run_flow_sim so the mission orchestrator shares the exact path.
                return emit(run_flow_sim(params.value("doc_id", std::string()),
                                         params.value("request", std::string()), params),
                            out_json, 0);
            }
            if (m == "doc_face_points")
                return emit(chudware::doc::doc_face_points(
                                params.value("doc_id", std::string()),
                                params.contains("selector") ? params["selector"] : json(nullptr),
                                params.contains("max_n") && params["max_n"].is_number()
                                    ? params["max_n"].get<int>() : 400),
                            out_json, 0);
            if (m == "doc_query")
                return emit(chudware::doc::doc_query(
                                params.value("doc_id", std::string()),
                                params.contains("query") ? params["query"] : params),
                            out_json, 0);
            if (m == "doc_adjacency")
                return emit(chudware::doc::doc_adjacency(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "doc_set_meta")
                return emit(chudware::doc::doc_set_meta(
                                params.value("doc_id", std::string()),
                                params.value("key", std::string()),
                                params.contains("value") ? params["value"] : json(nullptr)),
                            out_json, 0);
            if (m == "doc_get_meta")
                return emit(chudware::doc::doc_get_meta(
                                params.value("doc_id", std::string()),
                                params.value("key", std::string())),
                            out_json, 0);
            if (m == "doc_set_name") {
                std::string did = jstr(params, "doc_id");
                std::string nm = jstr(params, "name");
                return emit(chudware::doc::doc_set_name(did, nm), out_json, 0);
            }
            // --- C18 interactive sketch mode (engine/doc/session.py sketch_*) ---
            // The worker passes the sketch feature id under "sketch".
            if (m == "sketch_enter")
                return emit(chudware::doc::doc_sketch_enter(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_solve")
                return emit(chudware::doc::doc_sketch_solve(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "doc_sketch_overview")
                return emit(chudware::doc::doc_sketch_overview(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "doc_sketch_regions")
                return emit(chudware::doc::doc_sketch_regions(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_drag")
                return emit(chudware::doc::doc_sketch_drag(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.value("handle", std::string()),
                                params.contains("xy") ? params["xy"] : json(nullptr),
                                params.contains("targets") ? params["targets"] : json(nullptr)),
                            out_json, 0);
            if (m == "sketch_drag_commit")
                return emit(chudware::doc::doc_sketch_drag_commit(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_exit")
                return emit(chudware::doc::doc_sketch_exit(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_probe")
                return emit(chudware::doc::doc_sketch_probe(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("action") ? params["action"] : json::object()),
                            out_json, 0);
            // Sketch completion suite.
            if (m == "sketch_auto_constrain")
                return emit(chudware::doc::doc_sketch_auto_constrain(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("opts") ? params["opts"] : json::object()),
                            out_json, 0);
            if (m == "sketch_health")
                return emit(chudware::doc::doc_sketch_health(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            // 3D free-space sketch (feature type "sketch3d"). Same "sketch" param
            // carries the feature id; contract in contracts/sketch3d_gui_contract.md.
            if (m == "sketch3d_health")
                return emit(chudware::doc::doc_sketch3d_health(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch3d_drag")
                return emit(chudware::doc::doc_sketch3d_drag(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.value("handle", std::string()),
                                params.contains("xyz") ? params["xyz"] : json(nullptr),
                                params.contains("targets") ? params["targets"] : json(nullptr)),
                            out_json, 0);
            if (m == "sketch_project_ref")
                return emit(chudware::doc::doc_sketch_project_ref(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("ref") ? params["ref"] : json(nullptr)),
                            out_json, 0);
            if (m == "sketch_project_entities")
                return emit(chudware::doc::doc_sketch_project_entities(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("refs") ? params["refs"] : json::array()),
                            out_json, 0);
            if (m == "weld_cut_list")
                return emit(chudware::doc::doc_weld_cut_list(
                                params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "eval_mass")
                return emit(chudware::doc::doc_eval_mass(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.contains("density_g_cm3") && params["density_g_cm3"].is_number()
                                    ? std::optional<double>(params["density_g_cm3"].get<double>())
                                    : std::nullopt),
                            out_json, 0);
            if (m == "eval_fit_cylinder") {
                // Least-squares cylinder axis of the body's surface near a world point —
                // the measured mating axis for independently sourced CAD. `near` is
                // required (there is no sensible default point on an arbitrary body).
                std::array<double, 3> near_pt{0.0, 0.0, 0.0};
                if (!(params.contains("near") && params["near"].is_array() &&
                      params["near"].size() == 3))
                    throw chudware::doc::OpError(
                        "invalid_op",
                        "eval_fit_cylinder requires near:[x,y,z] — a world point on or "
                        "near the cylindrical surface to fit");
                for (int i = 0; i < 3; ++i) near_pt[i] = params["near"][i].get<double>();
                return emit(chudware::doc::doc_fit_cylinder_axis(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                near_pt,
                                params.contains("search_radius_mm") &&
                                        params["search_radius_mm"].is_number()
                                    ? params["search_radius_mm"].get<double>() : 5.0),
                            out_json, 0);
            }
            if (m == "doc_bbox")
                return emit(chudware::doc::doc_bbox(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "eval_check")
                return emit(chudware::doc::doc_eval_check(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0),
                            out_json, 0);
            if (m == "eval_geometry_analysis")
                return emit(chudware::doc::doc_eval_geometry_analysis(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_solid_health")
                return emit(chudware::doc::doc_eval_solid_health(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>()
                                    : (params.contains("body_id") && params["body_id"].is_number()
                                           ? params["body_id"].get<int>() : 0)),
                            out_json, 0);
            if (m == "eval_model_integrity")
                return emit(chudware::doc::doc_eval_model_integrity(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_section_props")
                return emit(chudware::doc::doc_eval_section_props(
                                params.value("doc_id", std::string()),
                                params.contains("plane") ? params["plane"] : json(nullptr)),
                            out_json, 0);
            if (m == "eval_symmetry")
                return emit(chudware::doc::doc_eval_symmetry(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.contains("plane") ? params["plane"] : json(nullptr)),
                            out_json, 0);
            if (m == "eval_interference")
                return emit(chudware::doc::doc_eval_interference(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_feature_overlap")
                return emit(chudware::doc::doc_eval_feature_overlap(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_feature_attachment")
                return emit(chudware::doc::doc_eval_feature_attachment(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_compare")
                return emit(chudware::doc::doc_eval_compare(params), out_json, 0);
            if (m == "doc_face_wires")
                return emit(chudware::doc::doc_face_wires(
                                params.value("doc_id", std::string()), params),
                            out_json, 0);
            if (m == "eval_measure")
                return emit(chudware::doc::doc_eval_measure(
                                params.value("doc_id", std::string()),
                                params.contains("refs") ? params["refs"] : json::array()),
                            out_json, 0);
            if (m == "eval_draft") {
                std::array<double, 3> pull{0.0, 0.0, 1.0};
                if (params.contains("pull") && params["pull"].is_array() &&
                    params["pull"].size() == 3) {
                    for (int i = 0; i < 3; ++i) pull[i] = params["pull"][i].get<double>();
                }
                return emit(chudware::doc::doc_eval_draft(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                pull,
                                params.contains("angle_deg") && params["angle_deg"].is_number()
                                    ? params["angle_deg"].get<double>() : 0.0),
                            out_json, 0);
            }
            if (m == "eval_curvature")
                return emit(chudware::doc::doc_eval_curvature(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.value("kind", std::string("mean"))),
                            out_json, 0);
            if (m == "eval_thickness")
                return emit(chudware::doc::doc_eval_thickness(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0),
                            out_json, 0);
            if (m == "export_file")
                return emit(chudware::doc::doc_export_file(
                                params.value("doc_id", std::string()),
                                params.value("path", std::string()),
                                params.value("format", std::string()),
                                params.value("lin_deflection", 0.05),
                                params.value("ang_deflection", 0.35),
                                params.value("relative", false), params.value("ascii", false),
                                // dxf/dwg/sat: "mm" | "in" | ... or an $INSUNITS code; empty =
                                // the drawing units of the doc's DWG/DXF/SAT import, else mm.
                                params.contains("units") && params["units"].is_number_integer()
                                    ? std::to_string(params["units"].get<int>())
                                    : params.value("units", std::string())),
                            out_json, 0);
            // Live triangle/vertex estimate for the export dialog — tessellates in memory
            // at the requested quality and returns counts without writing a file.
            if (m == "export_tess_estimate")
                return emit(chudware::doc::doc_export_estimate(
                                params.value("doc_id", std::string()),
                                params.value("lin_deflection", 0.05),
                                params.value("ang_deflection", 0.35),
                                params.value("relative", false)),
                            out_json, 0);
            // 2D exports (engine/doc/export2d.py). Format = explicit param else the path's
            // .dxf/.pdf/.svg extension (mirrors export2d.py::_resolve_format).
            if (m == "export_drawing" || m == "drawing_export" || m == "export_flat_dxf") {
                const std::string path = params.value("path", std::string());
                std::string fmt = params.value("format", std::string());
                if (fmt.empty()) {
                    auto dot = path.find_last_of('.');
                    std::string ext = dot == std::string::npos ? "" : path.substr(dot + 1);
                    for (char& c : ext) c = (char)std::tolower((unsigned char)c);
                    if (ext == "dxf" || ext == "dwg" || ext == "pdf" || ext == "svg") fmt = ext;
                }
                const std::string doc_id = params.value("doc_id", std::string());
                if (m == "export_drawing" || m == "drawing_export")
                    return emit(chudware::doc::export_drawing_selected(
                                    doc_id, path, fmt,
                                    params.contains("sheet")
                                        ? params["sheet"]
                                        : json(m == "drawing_export" ? "" : "all")),
                                out_json, 0);
                return emit(chudware::doc::export_flat_dxf(doc_id, path, fmt), out_json, 0);
            }
            if (m == "import_file")
                // Top-level keep_bodies / exclude_bodies (agent-side import_part) ride
                // through as `params` itself: doc_import_file copies just those two keys
                // onto the import feature.
                return emit(chudware::doc::doc_import_file(params.value("path", std::string()),
                                                        params.value("into", std::string()),
                                                        params.value("as_solid", false),
                                                        params.value("format", std::string()),
                                                        params),
                            out_json, 0);
            if (m == "mesh_read_raw")
                return emit(chudware::doc::mesh_read_raw(params.value("path", std::string()),
                                                      params.value("format", std::string())),
                            out_json, 0);
            if (m == "vendor_part_insert")
                return emit(chudware::doc::doc_vendor_part_insert(
                                params.contains("into") ? params.value("into", std::string())
                                                        : params.value("doc_id", std::string()),
                                params.value("supplier", std::string()),
                                params.value("part_number", std::string()),
                                params.value("name", std::string())),
                            out_json, 0);
            // --- 2D DRAWING projection + export (engine/doc/drawing.py + export2d.py) ---
            // STATELESS diagnostic form (the strangler parity probe): compile a part
            // tree + project its views via OCCT HLR, or pool the views to DXF/PDF.
            if (m == "doc_drawing_views") {
                json doc;
                if (params.contains("doc")) doc = params["doc"];
                else if (params.contains("features")) doc = params;
                else throw chudware::doc::OpError("invalid_op",
                                               "doc_drawing_views: params need a 'doc' object");
                json env = params.contains("env") ? params["env"] : json::object();
                json views = params.contains("views") ? params["views"] : json::array();
                return emit(chudware::doc::drawing_views(doc, env, views), out_json, 0);
            }
            if (m == "export2d_drawing") {
                json doc;
                if (params.contains("doc")) doc = params["doc"];
                else if (params.contains("features")) doc = params;
                else throw chudware::doc::OpError("invalid_op",
                                               "export2d_drawing: params need a 'doc' object");
                json env = params.contains("env") ? params["env"] : json::object();
                json views = params.contains("views") ? params["views"] : json::array();
                return emit(chudware::doc::export2d_drawing(doc, env, views,
                                                         params.value("path", std::string()),
                                                         params.value("format", std::string())),
                            out_json, 0);
            }
            // --- STATEFUL DrawingSession lifecycle (engine/doc/drawing.py) -----
            // A live drawing doc that projects a source part tree through the same
            // HLR path on every fetch (associative). Mirrors the doc_* / ecad_*
            // session registries: an OpError becomes a nonzero {code,message,detail}.
            auto opt = [&](const char* k) -> json {
                return params.contains(k) ? params[k] : json(nullptr);
            };
            if (m == "drawing_new") {
                // LIVE source: the host sends source_doc_id (an open doc-cpp part) and the
                // drawing re-projects it associatively. INLINE source: source_doc/doc/features
                // carry the tree (the stateless probe / parity path).
                std::string src_id = params.value("source_doc_id", std::string());
                json src;
                if (params.contains("source_doc")) src = params["source_doc"];
                else if (params.contains("doc")) src = params["doc"];
                else if (params.contains("features")) src = params;
                else if (src_id.empty())
                    throw chudware::doc::OpError(
                        "invalid_op", "drawing_new: params need a 'source_doc' (or 'doc') or "
                                      "a 'source_doc_id'");
                json env = params.contains("env") ? params["env"] : json::object();
                return emit(chudware::doc::drawing_new(src, env, params.value("name", std::string()),
                                                    src_id),
                            out_json, 0);
            }
            if (m == "drawing_set_source") {
                json src;
                if (params.contains("source_doc")) src = params["source_doc"];
                else if (params.contains("doc")) src = params["doc"];
                else throw chudware::doc::OpError("invalid_op",
                                               "drawing_set_source: params need a 'source_doc'");
                json env = params.contains("env") ? params["env"] : json::object();
                return emit(chudware::doc::drawing_set_source(params.value("doc_id", std::string()),
                                                           src, env),
                            out_json, 0);
            }
            if (m == "drawing_add_sheet")
                return emit(chudware::doc::drawing_add_sheet(
                                params.value("doc_id", std::string()),
                                params.value("size", std::string("A3")),
                                params.value("name", params.value("title", std::string()))),
                            out_json, 0);
            if (m == "drawing_list_sheets")
                return emit(chudware::doc::drawing_list_sheets(
                                params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "drawing_set_active_sheet")
                return emit(chudware::doc::drawing_set_active_sheet(
                                params.value("doc_id", std::string()),
                                params.value("sheet_id", std::string())),
                            out_json, 0);
            if (m == "drawing_add_view")
                return emit(chudware::doc::drawing_add_view(
                                params.value("doc_id", std::string()),
                                params.value("type", std::string()),
                                params.value("orientation", std::string("front")),
                                params.value("sheet_id", std::string()),
                                opt("scale"), opt("position"), opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_add_section") {
                json sopts = params.contains("opts") && params["opts"].is_object() ? params["opts"]
                                                                                   : json::object();
                for (const char* k : {"mode", "depth", "reverse", "offset",
                                      "hatch_style", "hatch_styles"})
                    if (params.contains(k)) sopts[k] = params[k];
                return emit(chudware::doc::drawing_add_section(
                                params.value("doc_id", std::string()),
                                params.value("parent_view_id", std::string()),
                                opt("plane"), opt("cut_line"), opt("label"), opt("scale"),
                                opt("position"), params.value("sheet_id", std::string()),
                                opt("show_hidden"), sopts),
                            out_json, 0);
            }
            if (m == "drawing_add_detail")
                return emit(chudware::doc::drawing_add_detail(
                                params.value("doc_id", std::string()),
                                params.value("parent_view_id", std::string()),
                                opt("center"),
                                params.contains("radius") && params["radius"].is_number()
                                    ? params["radius"].get<double>() : 0.0,
                                opt("scale"), opt("label"), opt("position"),
                                params.value("sheet_id", std::string()), opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_remove_view")
                return emit(chudware::doc::drawing_remove_view(params.value("doc_id", std::string()),
                                                            params.value("view_id", std::string())),
                            out_json, 0);
            if (m == "drawing_set_view")
                return emit(chudware::doc::drawing_set_view(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                opt("scale"), opt("position"), opt("orientation"),
                                opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_add_dim")
                return emit(chudware::doc::drawing_add_dim(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("type", std::string("linear_dim")),
                                opt("a"), opt("b"), opt("c"), opt("d"), opt("pos"), opt("text")),
                            out_json, 0);
            if (m == "drawing_add_chamfer_dimension" ||
                m == "drawing_add_ordinate_dimensions") {
                json spec = params.contains("spec") && params["spec"].is_object()
                                ? params["spec"] : json::object();
                for (const char* key : {"a", "pos", "length", "distance", "angle",
                                        "angle_deg", "count", "text", "precision",
                                        "prefix", "suffix", "origin", "axis", "points"})
                    if (params.contains(key)) spec[key] = params[key];
                const std::string did = params.value("doc_id", std::string());
                const std::string vid = params.value("view_id", std::string());
                if (m == "drawing_add_chamfer_dimension")
                    return emit(chudware::doc::drawing_add_chamfer_dimension(
                                    did, vid, spec), out_json, 0);
                return emit(chudware::doc::drawing_add_ordinate_dimensions(
                                did, vid, spec), out_json, 0);
            }
            if (m == "drawing_remove_annotation")
                return emit(chudware::doc::drawing_remove_annotation(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("ann_id", std::string())),
                            out_json, 0);
            if (m == "drawing_set_annotation") {
                // Detailing patch: any of tol_type/tol_upper/tol_lower/prefix/suffix/
                // precision/critical/count/pos/value/text may ride either as a nested
                // "patch" object or flat alongside the ids.
                json patch = params.contains("patch") && params["patch"].is_object()
                                 ? params["patch"]
                                 : json::object();
                for (const char* k : {"tol_type", "tol_upper", "tol_lower", "prefix", "suffix",
                                      "precision", "critical", "count", "pos", "value", "text"})
                    if (params.contains(k)) patch[k] = params[k];
                return emit(chudware::doc::drawing_set_annotation(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("ann_id", std::string()), patch),
                            out_json, 0);
            }
            if (m == "drawing_add_callout") {
                json o = params.contains("opts") && params["opts"].is_object() ? params["opts"]
                                                                               : json::object();
                for (const char* k : {"pos", "item", "text", "suffix"})
                    if (params.contains(k)) o[k] = params[k];
                return emit(chudware::doc::drawing_add_callout(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("kind", std::string("hole")), opt("a"), o),
                            out_json, 0);
            }
            // GD&T (ASME Y14.5): the structured spec rides as "spec" (or flat keys).
            if (m == "drawing_add_gdt" || m == "drawing_add_datum" ||
                m == "drawing_add_datum_target" || m == "drawing_add_surface_finish" ||
                m == "drawing_add_weld") {
                json spec = params.contains("spec") && params["spec"].is_object() ? params["spec"]
                                                                                  : json::object();
                for (const char* k : {"symbol", "tol_value", "tol_diameter", "tol_modifier",
                                      "datums", "row2", "label", "target_type", "size", "value",
                                      "ra", "machining", "all_around", "lay", "weld_type",
                                      "other_side", "a", "pos", "intermittent_length", "length",
                                      "pitch", "contour", "finish", "field_weld", "tail_text",
                                      "full_penetration"})
                    if (params.contains(k)) spec[k] = params[k];
                std::string did = params.value("doc_id", std::string());
                std::string vid = params.value("view_id", std::string());
                if (m == "drawing_add_gdt")
                    return emit(chudware::doc::drawing_add_gdt(did, vid, spec), out_json, 0);
                if (m == "drawing_add_datum")
                    return emit(chudware::doc::drawing_add_datum(did, vid, spec), out_json, 0);
                if (m == "drawing_add_datum_target")
                    return emit(chudware::doc::drawing_add_datum_target(did, vid, spec), out_json, 0);
                if (m == "drawing_add_surface_finish")
                    return emit(chudware::doc::drawing_add_surface_finish(did, vid, spec), out_json, 0);
                return emit(chudware::doc::drawing_add_weld(did, vid, spec), out_json, 0);
            }
            if (m == "drawing_model_items")
                return emit(chudware::doc::drawing_model_items(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string())),
                            out_json, 0);
            if (m == "drawing_set_units")
                return emit(chudware::doc::drawing_set_units(
                                params.value("doc_id", std::string()),
                                params.value("system", std::string())),
                            out_json, 0);
            if (m == "drawing_set_titleblock") {
                json fields = params.contains("fields") && params["fields"].is_object()
                                  ? params["fields"]
                                  : json::object();
                return emit(chudware::doc::drawing_set_titleblock(
                                params.value("doc_id", std::string()),
                                params.value("sheet_id", std::string()), fields),
                            out_json, 0);
            }
            if (m == "drawing_set_sheet")
                return emit(chudware::doc::drawing_set_sheet(
                                params.value("doc_id", std::string()),
                                params.value("sheet_id", std::string()),
                                params.value("size", std::string("A3"))),
                            out_json, 0);
            if (m == "drawing_arrange")
                return emit(chudware::doc::drawing_arrange(
                                params.value("doc_id", std::string()),
                                params.value("sheet_id", std::string()),
                                params.value("projection", std::string()),
                                params.contains("scale") ? params["scale"] : json(nullptr)),
                            out_json, 0);
            if (m == "drawing_add_table") {
                json table_spec =
                    params.contains("spec") && params["spec"].is_object()
                        ? params["spec"] : json::object();
                for (const char* key : {"position", "anchor", "title", "rows",
                                        "columns", "row_height"})
                    if (params.contains(key)) table_spec[key] = params[key];
                return emit(chudware::doc::drawing_add_table(
                                params.value("doc_id", std::string()),
                                params.value("sheet_id", std::string()),
                                params.value("kind", std::string()), table_spec),
                            out_json, 0);
            }
            if (m == "drawing_make") {
                // One-shot copilot drawing authoring. Source is a live part session id
                // (source_doc_id|doc_id); the compact spec rides as "spec" (or flat keys).
                // NOTE: named drawing_* (NOT agent_*) so worker_client routes it to the C++
                // core by its doc-cpp source_doc_id (agent_* is pinned to the agent seam).
                std::string src = params.value("source_doc_id", std::string());
                if (src.empty()) src = params.value("doc_id", std::string());
                json spec = params.contains("spec") && params["spec"].is_object()
                                ? params["spec"]
                                : json::object();
                for (const char* k : {"name", "sheet", "views", "model_items", "hole_callouts",
                                      "title", "projection", "sections", "scale"})
                    if (params.contains(k)) spec[k] = params[k];
                return emit(chudware::doc::drawing_make(src, spec), out_json, 0);
            }
            if (m == "drawing_save_standalone")
                return emit(chudware::doc::drawing_save_standalone(
                                params.value("drawing_id",
                                             params.value("doc_id", std::string())),
                                params.value("path", std::string())),
                            out_json, 0);
            if (m == "drawing_open_standalone")
                return emit(chudware::doc::drawing_open_standalone(
                                params.value("path", std::string())),
                            out_json, 0);
            if (m == "drawing_get")
                return emit(chudware::doc::drawing_get(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "drawing_bom")
                return emit(chudware::doc::drawing_bom(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "drawing_auto_balloon")
                return emit(chudware::doc::drawing_auto_balloon(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string())),
                            out_json, 0);
            if (m == "drawing_close")
                return emit(json{{"closed", chudware::doc::drawing_close(
                                                params.value("doc_id", std::string()))}},
                            out_json, 0);

            // ---- kinematic motion study (simulate_motion) ------------------
            // Drive dimensional mates together on an immutable snapshot, solving
            // their shared graph at every sample. Native transforms are column-major.
            if (m == "simulate_motion") {
                const std::string sm_doc = params.value("doc_id", std::string());
                const json sm_drivers = params.contains("drivers") ? params["drivers"] : json(nullptr);
                const std::string sm_mid = sm_drivers.is_null()
                    ? params.value("mate_id", std::string()) : std::string();
                const double sm_start = sm_drivers.is_null() ? params.value("start", 0.0) : 0.0;
                const double sm_stop = sm_drivers.is_null() ? params.value("stop", 90.0) : 90.0;
                const int    sm_steps = params.value("steps", 24);
                const bool   sm_ci    = params.value("check_interference", true);
                const double sm_mcl   = params.value("min_clearance_mm", 0.0);
                // Optional inverse dynamics and synchronized multi-driver sweep.
                const json   sm_dyn   = params.contains("dynamics") ? params["dynamics"]
                                                                    : json(nullptr);
                return emit(chudware::doc::doc_simulate_motion(
                                sm_doc, sm_mid, sm_start, sm_stop, sm_steps, sm_ci, sm_mcl,
                                sm_dyn, sm_drivers),
                            out_json, 0);
            }

        } catch (const chudware::doc::OpError& oe) {
            // Structured error envelope (matches the Python worker's doc_* errors).
            return emit(oe.envelope(), out_json, 3);
        }
        // --- stateful ECAD document session RPCs (chudware::ecad::session) ----------
        // Same contract as the doc_* block: an OpError becomes a nonzero return
        // carrying the {code,message,detail} envelope. The ECAD doc/ops layer is
        // pure JSON (no OCCT) — schematic + board document model + apply-ops.
        try {
            if (m == "ecad_new")
                return emit(chudware::ecad::ecad_new(params.value("kind", std::string("sch")),
                                                  params.value("name", std::string())),
                            out_json, 0);
            if (m == "ecad_apply_ops")
                return emit(chudware::ecad::ecad_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array()),
                            out_json, 0);
            if (m == "ecad_list_ops") {
                json ops = json::array();
                for (const auto& k : chudware::ecad::ecad_op_kinds()) ops.push_back(k);
                return emit(json{{"ok", true}, {"ops", ops},
                                {"hint", "see cad_guide() for ECAD tool workflows"}},
                            out_json, 0);
            }
            if (m == "ecad_undo")
                return emit(chudware::ecad::ecad_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_redo")
                return emit(chudware::ecad::ecad_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_tree")
                return emit(chudware::ecad::ecad_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_rebuild")
                return emit(chudware::ecad::ecad_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_swap_doc")
                return emit(chudware::ecad::ecad_swap_doc(params.value("doc_id", std::string()),
                                                       params.value("doc", json::object()),
                                                       params.value("base_revision", -1L),
                                                       params.value("warnings", json::array())),
                            out_json, 0);
            if (m == "ecad_close")
                return emit(json{{"closed", chudware::ecad::ecad_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "ecad_describe")
                return emit(chudware::ecad::ecad_describe(
                                params.value("doc_id", std::string()),
                                params.contains("budget") && params["budget"].is_number()
                                    ? params["budget"].get<long>()
                                    : 0L),
                            out_json, 0);
            if (m == "ecad_resolve_pick")
                return emit(chudware::ecad::ecad_resolve_pick(
                                params.value("doc_id", std::string()),
                                params.contains("xy") ? params["xy"] : json(nullptr),
                                params.contains("kinds") ? params["kinds"] : json(nullptr),
                                params.contains("tol") && params["tol"].is_number()
                                    ? params["tol"].get<double>()
                                    : 0.0,
                                params.value("layer", std::string())),
                            out_json, 0);
            if (m == "ecad_design")
                return emit(chudware::ecad::ecad_design(params.value("request", std::string()),
                                                        params.value("name", std::string())),
                            out_json, 0);
            if (m == "ecad_symbols") {
                const std::string q = params.value("query", std::string());
                const int limit = params.contains("limit") && params["limit"].is_number()
                                      ? params["limit"].get<int>()
                                      : 20;
                if (q.empty()) {
                    json cat = chudware::ecad::symlib::symbol_catalog();
                    if (limit > 0 && cat.is_array() && (int)cat.size() > limit)
                        cat.erase(cat.begin() + limit, cat.end());
                    return emit(json{{"ok", true}, {"symbols", cat},
                                    {"count", (int)cat.size()},
                                    {"total", (int)chudware::ecad::symlib::symbol_libids().size()}},
                                out_json, 0);
                }
                return emit(chudware::ecad::symlib::search_symbols(q, limit), out_json, 0);
            }
            if (m == "ecad_load_symbol_library")
                return emit(chudware::ecad::symlib::load_kicad_sym_file(
                                params.value("path", std::string())),
                            out_json, 0);
            if (m == "ecad_schema")
                return emit(chudware::ecad::ecad_schema(params.value("kind", std::string("sch"))),
                            out_json, 0);
            if (m == "ecad_dfm") {
                json rules = params.contains("rules") ? params["rules"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(chudware::ecad::ecad_dfm_board(params["board"], rules), out_json, 0);
                return emit(chudware::ecad::ecad_dfm(params.value("doc_id", std::string()), rules),
                            out_json, 0);
            }
            auto base_rev = [&](void) -> long {
                return params.contains("base_revision") && params["base_revision"].is_number()
                           ? params["base_revision"].get<long>()
                           : -1;
            };
            if (m == "ecad_autoroute") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    // Stateless inline-board path (parity harness): returns the PURE
                    // {tracks, vias, report} of engine.ecad.autoroute.autoroute(board, opts).
                    return emit(chudware::ecad::autoroute(params["board"], opts), out_json, 0);
                // Doc-id session path (the host's form): route the open board, apply the
                // tracks/vias as ops, return the board ops-response + report.
                return emit(chudware::ecad::ecad_autoroute(params.value("doc_id", std::string()), opts,
                                                        base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_route_diff_pair") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(chudware::ecad::route_diff_pair(params["board"],
                                                             params.value("net_p", std::string()),
                                                             params.value("net_n", std::string()),
                                                             opts),
                                out_json, 0);
                return emit(chudware::ecad::ecad_route_diff_pair(
                                params.value("doc_id", std::string()),
                                params.value("net_p", std::string()),
                                params.value("net_n", std::string()), opts, base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_fill_zones") {
                json resolution = params.contains("resolution") ? params["resolution"] : json(nullptr);
                return emit(chudware::ecad::ecad_fill_zones(params.value("doc_id", std::string()),
                                                         resolution, base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_simulate") {
                // job_id (optional) makes the solve cancellable via `sim_cancel {job_id}` —
                // ecad_simulate owns the register/unregister scope internally. The GUI always
                // passes one so a heavy study can be stopped instead of pinning a core.
                json study = params.contains("study") ? params["study"] : json(nullptr);
                json sim_params = params.contains("sim_params") ? params["sim_params"] : json(nullptr);
                return emit(chudware::ecad::ecad_simulate(params.value("doc_id", std::string()), study,
                                                       sim_params, params.value("job_id", std::string())),
                            out_json, 0);
            }
            if (m == "ecad_to_3d") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    // Stateless inline-board path (parity harness): build the OCCT board
                    // model (substrate extrude + drill cuts + analytic component proxies)
                    // and return the geometry summary + exact BRep volume stats. Mirrors
                    // engine.ecad.threed.board_to_step's summary; no STEP file is written.
                    return emit(chudware::ecad::board_to_3d(params["board"], opts), out_json, 0);
                // Doc-id session path (the host's form): fetch the open board doc, build
                // the model, WRITE the STEP at `path`, and return {step_path, summary, ...}
                // exactly like engine.ecad.threed.board_to_step.
                json board = chudware::ecad::ecad_session_doc(params.value("doc_id", std::string()));
                if (!chudware::ecad::schema::is_board(board))
                    return emit(chudware::ecad::OpError("invalid_op",
                                                     "ecad_to_3d requires a pcb document",
                                                     json{{"reason", "wrong doc kind"}})
                                    .envelope(),
                                out_json, 3);
                json to3d = chudware::ecad::board_to_3d_step(board, opts,
                                                          params.value("path", std::string()));
                // Mint (or append to `into_doc_id`) the CAD part document that carries the
                // board as an `import` feature, and return its id. fab_outputs.md §5 always
                // promised this; until now the RPC only wrote the STEP and every caller had
                // to re-do the mint+import client-side, which meant a re-export spawned a
                // second anonymous tab instead of updating the first.
                //
                // doc_import_file already IS the "mint-or-append, return doc_id" primitive
                // (`into` empty -> new part session; otherwise append to that session), so
                // this is a call, not a new capability. No publish sink is needed: ecad_to_3d
                // is a top-level method, so DocRegistry::postCall's generic response-doc_id
                // sniff registers the new doc exactly as it does for parts_make.
                const std::string step_path = to3d.value("step_path", std::string());
                if (!step_path.empty()) {
                    try {
                        json imported = chudware::doc::doc_import_file(
                            step_path, params.value("into_doc_id", std::string()),
                            /*as_solid=*/false, "step");
                        const std::string part_doc_id = imported.value("doc_id", std::string());
                        to3d["part_doc_id"] = part_doc_id;
                        to3d["doc_id"] = part_doc_id;  // DocRegistry mint sniff
                    } catch (const std::exception& ie) {
                        // The STEP is already on disk and valid — a bad/closed `into_doc_id`
                        // must degrade to "exported but not opened", never erase a successful
                        // export. doc_import_file throws chudware::doc::OpError, a different
                        // type than the chudware::ecad::OpError this branch's enclosing catch
                        // handles, so it must be caught locally or it would surface as a flat
                        // rc=1 "threw" envelope.
                        to3d["part_doc_id"] = nullptr;
                        to3d["warnings"].push_back(std::string("board exported to ") + step_path +
                                                   " but opening it as a part failed: " + ie.what());
                    }
                }
                return emit(std::move(to3d), out_json, 0);
            }
            if (m == "ecad_impedance")
                return emit(chudware::ecad::ecad_impedance(params), out_json, 0);
            if (m == "ecad_export")
                return emit(chudware::ecad::ecad_export(params), out_json, 0);
            if (m == "ecad_save")
                return emit(chudware::ecad::ecad_save(params.value("doc_id", std::string()),
                                                   params.value("path", std::string())),
                            out_json, 0);
            if (m == "ecad_open")
                return emit(chudware::ecad::ecad_open(params.value("path", std::string())), out_json, 0);
            if (m == "ecad_update_from_sch") {
                // Stateless inline path (parity harness): {board, sch_netlist, prune?,
                // place_origin?} -> sync's {mutated_board, report}. Else the stateful
                // path over two open sessions.
                json place = params.contains("place_origin") ? params["place_origin"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(chudware::ecad::ecad_update_from_sch_inline(
                                    params["board"],
                                    params.contains("sch_netlist") ? params["sch_netlist"]
                                                                    : json::object(),
                                    params.value("prune", false), place),
                                out_json, 0);
                return emit(chudware::ecad::ecad_update_from_sch(
                                params.value("board_doc_id", std::string()),
                                params.value("sch_doc_id", std::string()),
                                params.value("prune", false), place,
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1),
                            out_json, 0);
            }
            // Deterministic recipe generators (engine/ecad/recipes.py). The LLM
            // orchestrator (engine/ecad/orchestrator.py / the ecad_design RPC) is
            // DEFERRED to the agent/LLM epic; only the pure recipe builders are ported.
            if (m == "ecad_recipe")
                return emit(chudware::ecad::recipes::build(
                                params.value("name", std::string()),
                                params.contains("params") ? params["params"] : json::object()),
                            out_json, 0);
            if (m == "ecad_closest_recipe")
                return emit(chudware::ecad::recipes::closest_recipe(params.value("request", std::string())),
                            out_json, 0);
        } catch (const chudware::ecad::OpError& oe) {
            return emit(oe.envelope(), out_json, 3);
        }

        // --- CAM (computer-aided machining) document session RPCs --------------
        // Same OpError-envelope contract as the ecad block. The CAM job/ops/post/
        // sim layer is pure JSON (no OCCT); the OCCT extraction adapter (cam_extract)
        // lives in geom/ and is dispatched below when CHUDWARE_WITH_OCCT.
        try {
            auto cam_base_rev = [&](void) -> long {
                return (params.contains("base_revision") && params["base_revision"].is_number())
                           ? params["base_revision"].get<long>() : -1;
            };
            if (m == "cam_new") return emit(chudware::cam::cam_new(params), out_json, 0);
            if (m == "cam_apply_ops")
                return emit(chudware::cam::cam_apply_ops(params.value("doc_id", std::string()),
                                                      cam_base_rev(),
                                                      params.contains("ops") ? params["ops"] : json::array()),
                            out_json, 0);
            if (m == "cam_undo") return emit(chudware::cam::cam_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "cam_redo") return emit(chudware::cam::cam_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "cam_tree") return emit(chudware::cam::cam_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "cam_close")
                return emit(json{{"closed", chudware::cam::cam_close(params.value("doc_id", std::string()))}}, out_json, 0);
            if (m == "cam_describe") return emit(chudware::cam::cam_describe(params.value("doc_id", std::string())), out_json, 0);
            if (m == "cam_generate") return emit(chudware::cam::cam_generate(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_simulate") return emit(chudware::cam::cam_simulate(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_post") return emit(chudware::cam::cam_post(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_setup_sheet") return emit(chudware::cam::cam_setup_sheet(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_generate_inline") return emit(chudware::cam::cam_generate_inline(params), out_json, 0);
            if (m == "cam_post_inline") return emit(chudware::cam::cam_post_inline(params), out_json, 0);
            if (m == "cam_post_dialects") return emit(chudware::cam::cam_post_dialects(), out_json, 0);
            if (m == "cam_materials") return emit(chudware::cam::cam_materials(), out_json, 0);
            if (m == "cam_tool_types") return emit(chudware::cam::cam_tool_types(), out_json, 0);
            if (m == "cam_op_types") return emit(chudware::cam::cam_op_types(), out_json, 0);
            if (m == "cam_machines") return emit(chudware::cam::cam_machines(), out_json, 0);
            if (m == "cam_feeds") return emit(chudware::cam::cam_feeds(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_audit") return emit(chudware::cam::cam_audit(params.value("doc_id", std::string()), params), out_json, 0);
            if (m == "cam_save")
                return emit(chudware::cam::cam_save(params.value("doc_id", std::string()),
                                                 params.value("path", std::string())), out_json, 0);
            if (m == "cam_open") return emit(chudware::cam::cam_open(params.value("path", std::string())), out_json, 0);
            // Cross-doc dependency registry (feat(xdoc)).
            if (m == "doc_dep_register")
                return emit(chudware::doc::doc_dep_register(
                                params.value("source_doc_id", std::string()),
                                params.value("dep_doc_id", std::string()),
                                params.value("kind", std::string()),
                                params.value("linked_revision", (long)-1)),
                            out_json, 0);
            if (m == "doc_dep_query")
                return emit(chudware::doc::doc_dep_query(
                                params.value("source_doc_id", std::string()),
                                params.value("kind", std::string())),
                            out_json, 0);
            if (m == "doc_dep_notify")
                return emit(chudware::doc::doc_dep_notify(
                                params.value("source_doc_id", std::string()),
                                params.value("new_revision", (long)-1)),
                            out_json, 0);
            if (m == "doc_dep_unregister")
                return emit(chudware::doc::doc_dep_unregister(
                                params.value("dep_doc_id", std::string()),
                                params.value("kind", std::string())),
                            out_json, 0);

            // OCCT extraction adapter: live B-rep part -> CAM geometry inputs.
            if (m == "cam_extract") return emit(chudware::cam::cam_extract(params), out_json, 0);
            // Server-side export+extract (web/daemon-safe; no client temp path).
            if (m == "cam_extract_from_doc")
                return emit(cam_extract_from_doc(jstr(params, "doc_id"), params), out_json, 0);
            // One-shot: build a complete machining job + G-code from the current part
            // (deterministic recipe; the in-app copilot's CAM fast-path calls the same fn).
            if (m == "cam_program")
                return emit(cam_program_from_part(jstr(params, "doc_id"), params), out_json, 0);
        } catch (const chudware::cam::OpError& oe) {
            return emit(oe.envelope(), out_json, 3);
        }
        // --- sourcing/BOM/cost/spec-match (core/sourcing) ---
        // sourcing::call returns false for methods it does not own (fall through).
        {
            json src_result;
            if (chudware::sourcing::call(m, params, build_sourcing_seams(), src_result)) {
                return emit(std::move(src_result), out_json, 0);
            }
        }
        // --- parts placement (core/parts/place) ---
        // The parts hub FTS stays Python — callers pass `record` (get_record seam is a
        // null fallback). parts_make/import_file/build_model wire to in-core paths.
        {
            chudware::parts::PlaceSeams ps;
            ps.get_record = [](const std::string&) -> json { return json(); };
            ps.parts_make = [](const json& p) -> json { int rc = 0; return parts_make_impl(p, rc); };
            ps.import_file = [](const std::string& path, bool as_solid) -> json {
                return chudware::doc::doc_import_file(path, std::string(), as_solid, std::string());
            };
            ps.build_model = [](const json& match) -> std::string {
                return build_sourcing_seams().build_model(match);
            };
            json pl_result;
            std::string pl_err;
            if (chudware::parts::place_call(m, params, pl_result, pl_err, ps)) {
                return emit(std::move(pl_result), out_json, 0);
            }
        }
        // piping_route over a live document: every solid body of `doc_id` (world AABB, minus
        // `ignore_bodies` names/components) joins the caller's `obstacles` before the module
        // dispatch below routes around them.
        if (m == "piping_route" && params.contains("doc_id") && params["doc_id"].is_string() &&
            !params["doc_id"].get<std::string>().empty()) {
            if (params.contains("obstacles") && !params["obstacles"].is_array())
                return emit(json{{"ok", false},
                                 {"reason", "invalid_params"},
                                 {"hint", "obstacles must be a list of {min,max,name?}"}},
                            out_json, 0);
            std::vector<std::string> ignore;
            if (params.contains("ignore_bodies")) {
                if (!params["ignore_bodies"].is_array())
                    return emit(json{{"ok", false},
                                     {"reason", "invalid_params"},
                                     {"hint", "ignore_bodies must be a list of body names"}},
                                out_json, 0);
                for (const auto& n : params["ignore_bodies"])
                    if (n.is_string()) ignore.push_back(n.get<std::string>());
            }
            json world;
            try {
                world = chudware::doc::doc_asm_world_bodies(params["doc_id"].get<std::string>());
            } catch (const chudware::doc::OpError& e) {
                return emit(json{{"ok", false},
                                 {"reason", e.code.empty() ? "unknown_doc" : e.code},
                                 {"hint", std::string(e.what()) +
                                              " (piping_route doc_id must be an open document)"}},
                            out_json, 0);
            }
            json obstacles = params.value("obstacles", json::array());
            for (const auto& b : world.value("bodies", json::array())) {
                const std::string name = b.value("name", std::string());
                const std::string comp = b.value("component", std::string());
                if (std::find(ignore.begin(), ignore.end(), name) != ignore.end() ||
                    (!comp.empty() &&
                     std::find(ignore.begin(), ignore.end(), comp) != ignore.end()))
                    continue;
                const json& bb = b.contains("bbox") ? b["bbox"] : json(nullptr);
                if (!bb.is_object() || !bb.contains("min") || !bb.contains("max")) continue;
                obstacles.push_back({{"min", bb["min"]},
                                     {"max", bb["max"]},
                                     {"name", name.empty() ? comp : name}});
            }
            params["obstacles"] = std::move(obstacles);
        }
        // --- hydraulic + piping (core/hydraulic, core/piping) ---
        // ojson preserves dict-insertion-order fidelity the Python envelopes rely on;
        // dump/parse conversion is shortest-round-trip exact.
        {
            namespace py = chudware::pyutil;
            py::ojson fp_params = py::ojson::parse(params.dump());
            py::ojson fp_result;
            if (chudware::hydraulic::call(m, fp_params, fp_result) ||
                chudware::piping::call(m, fp_params, fp_result)) {
                return emit(json::parse(fp_result.dump()), out_json, 0);
            }
        }
        // --- structural (core/structural) ---
        {
            json st_result;
            std::string st_err;
            if (chudware::structural::call(m, params, st_result, st_err)) {
                return emit(std::move(st_result), out_json, 0);
            }
        }
        // --- tolerance / mechanical / materials / dfm ---
        {
            json sm_result;
            std::string sm_err;
            if (chudware::tolerance::call(m, params, sm_result, sm_err) ||
                chudware::mechanical::call(m, params, sm_result, sm_err) ||
                chudware::materials::call(m, params, sm_result, sm_err) ||
                chudware::dfm::call(m, params, sm_result, sm_err)) {
                return emit(std::move(sm_result), out_json, 0);
            }
        }
        // --- firmware engine surface (core/firmware) ---
        // firmware::call returns false for methods still owned by the Python
        // oracle (fw_sim_run / fw_power_profile / fw_sensor_scenario).
        {
            json fw_result;
            std::string fw_err;
            if (chudware::firmware::call(m, params, fw_result, fw_err)) {
                return emit(std::move(fw_result), out_json, 0);
            }
        }
        // Unknown to the C++ core -> nonzero so the host falls back to Python.
        return emit(json{{"error", "unknown method: " + m}}, out_json, 2);
    } catch (const chudware::sim::CellRevokedError&) {
        return emit(json{{"code", "revoked"}, {"message", "principal revoked"}}, out_json, 1);
    } catch (const chudware::sim::CellOwnershipError&) {
        return emit(json{{"code", "not_found"}, {"message", "job not found"}}, out_json, 1);
    } catch (const std::exception& e) {
        return emit_error(std::string("chudware_call(") + m + ") threw: " + e.what(), out_json);
    } catch (...) {
        return emit_error("chudware_call(" + m + ") threw a non-std exception", out_json);
    }
}

#ifdef _WIN32
// SEH filter for the dispatch boundary: only catch HARDWARE faults that would otherwise
// escape as an unhandled structured exception and leave the session lock orphaned (the
// P0 "resource deadlock would occur" bug). Access violation (the reported 0x60 null
// deref), stack/array overrun, misalignment, page faults, illegal instruction. Real C++
// exceptions are NOT routed here — they're already handled inside chudware_call_inner's
// try/catch and unwind their locks normally — so we EXCEPTION_CONTINUE_SEARCH anything
// that isn't a hardware fault (a C++ throw surfaces to MSVC as code 0xE06D7363).
static int chudware_seh_filter(unsigned int code) {
    switch (code) {
        case EXCEPTION_ACCESS_VIOLATION:
        case EXCEPTION_DATATYPE_MISALIGNMENT:
        case EXCEPTION_ARRAY_BOUNDS_EXCEEDED:
        case EXCEPTION_STACK_OVERFLOW:
        case EXCEPTION_IN_PAGE_ERROR:
        case EXCEPTION_ILLEGAL_INSTRUCTION:
        case EXCEPTION_PRIV_INSTRUCTION:
        case EXCEPTION_INT_DIVIDE_BY_ZERO:
        case EXCEPTION_FLT_DIVIDE_BY_ZERO:
            return EXCEPTION_EXECUTE_HANDLER;
        default:
            return EXCEPTION_CONTINUE_SEARCH;
    }
}

// malloc a fixed C error string for the SEH path (no C++ objects: this lives in the
// __except function, which must not require C++ object unwinding).
static char* dup_seh_error(const char* msg) {
    size_t n = std::strlen(msg);
    char* p = static_cast<char*>(std::malloc(n + 1));
    if (p) std::memcpy(p, msg, n + 1);
    return p;
}

// Public entry. Thin SEH wrapper around chudware_call_inner. A hardware fault (e.g. an
// access violation deep inside compile_document while the session mutex is held) is
// caught HERE so that: (1) we drop the orphaned doc lock left by the skipped DocLock
// destructor — without this, the SAME worker thread re-locks the still-held mutex on
// its next call and gets std::system_error EDEADLK "resource deadlock would occur",
// bricking every document; (2) we return a clean JSON error instead of crashing the
// host or corrupting the JSON-RPC channel. This function deliberately holds NO C++
// objects with destructors so MSVC permits __try/__except (C2712) even under /EHsc.
CHUDWARE_API int chudware_call(const char* method, const char* params_json, char** out_json) {
    __try {
        return chudware_call_inner(method, params_json, out_json);
    } __except (chudware_seh_filter(GetExceptionCode())) {
        // The structured-exception unwind skipped chudware_call_inner's C++ destructors,
        // so any registry lock (doc / drawing / ecad) taken on this thread is still held
        // by it. Drop all three now (each call is a no-op if that registry's lock is not
        // held by this thread) so the next call on this worker thread doesn't deadlock.
        chudware::doc::force_release_doc_lock();
        chudware::doc::force_release_drawing_lock();
        chudware::ecad::force_release_ecad_lock();
        chudware::cam::force_release_cam_lock();
        if (out_json) *out_json = dup_seh_error("{\"error\":\"internal error: access violation (the engine hit a hardware fault and recovered; the document is unchanged)\"}");
        return 1;
    }
}
#else
// Non-Windows: no SEH. The C++ try/catch inside chudware_call_inner already releases locks
// on any C++ exception; a hardware fault delivers a POSIX signal (SIGSEGV) which is a
// separate, process-level concern, not a held-mutex deadlock.
CHUDWARE_API int chudware_call(const char* method, const char* params_json, char** out_json) {
    return chudware_call_inner(method, params_json, out_json);
}
#endif

}  // extern "C"
