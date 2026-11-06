#include "doc_registry.h"

#include <chrono>
#include <algorithm>
#include <cstdlib>
#include <string_view>

#include "hostd_log.h"

namespace hostd {

using nlohmann::json;

static long long monoMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// ------------------------------------------------------------- classification

// Read-only allowlist (see the header for the rationale). Exact names first,
// then prefixes. Everything else that carries a doc_id counts as MUTATING.
static bool isDocumentReadOnlyMethod(const std::string& m) {
    static const std::set<std::string> kExact = {
        // doc reads
        "doc_list", "doc_tree", "doc_query", "doc_bbox", "doc_adjacency",
        "doc_get_meta", "doc_validate", "doc_dry_run_ops", "doc_resolve_pick",
        "doc_mesh_ref_list", "doc_mesh_ref_mesh",
        // doc_rebuild recomputes + re-tessellates but never edits the model —
        // an ro GUI mirror tab activates via forced doc_rebuild (Phase 4:
        // DocMirror::activate), so viewers must be allowed to run it.
        // doc_activate is its cached-serve fast path (tab-switch perf) — same
        // read-only semantics, so viewers may run it too.
        "doc_rebuild", "doc_activate",
        "doc_dep_query", "doc_drawing_views", "doc_sketch_overview",
        "doc_sketch_regions", "doc_compile_cpp",
        // doc_save serializes to disk but does not modify the model — allowing
        // it means an ro viewer can still snapshot what it sees.
        // doc_clear_history empties undo stacks only (no revision bump / no model edit).
        // doc_render_diff booleans two freshly-compiled docs into a PNG — no model mutation.
        "doc_save", "doc_export_step", "doc_clear_history", "doc_render_diff",
        // doc_view_metrics rasterizes candidate cameras for the shot planner — pure reads.
        "doc_view_metrics",
        // misc reads
        "asm_world_bodies", "asm_source_status", "config_list", "sim_study_list", "weld_cut_list",
        "sketch_health", "sketch3d_health", "sketch_probe",
        "drawing_get", "drawing_bom", "drawing_model_items", "drawing_render_png",
        "cam_describe", "cam_tree", "cam_extract", "ecad_describe", "ecad_tree",
        "ecad_kicad_status", "ecad_kicad_check", "ecad_footprints", "ecad_kicad_libraries", "ecad_symbols",
        // Piping RPCs return networks/K without editing the CAD doc; piping_route
        // also reads world body boxes as obstacles. Persistence uses sim_study_*.
        "piping_route", "piping_edit", "piping_fitting_k",
        "harness_route", "harness_path_check",
    };
    static const std::vector<std::string> kPrefixes = {
        "doc_face_",   // face_points / face_edges / face_wires
        "doc_render_", // png / view / contact_sheet
        "describe_", "render_", "measure_", "query_",
        "eval_",       // measure/mass/interference/... — pure geometry reads
        "sim_",        // solves read geometry; results live outside the doc
        "export",      // export_file / export_drawing / export2d_ / export_tess_
        "mesh_", "aicm_", "prof_", "agent_",
    };
    if (kExact.count(m)) return true;
    for (const auto& p : kPrefixes)
        if (m.rfind(p, 0) == 0) return true;
    return false;
}

static bool isStudyMutation(const std::string& method) {
    return method.rfind("sim_study_", 0) == 0 && method != "sim_study_list";
}

bool DocRegistry::requiresMutationLock(const std::string& method) {
    return method == "doc_save" || isStudyMutation(method) || !isDocumentReadOnlyMethod(method);
}

DocRegistry::MutationGuard DocRegistry::lockMutation(const std::string& docId) {
    std::shared_ptr<std::recursive_mutex> mutex;
    {
        std::lock_guard<std::mutex> lk(mutationMtx_);
        auto& weak = mutationLocks_[docId];
        mutex = weak.lock();
        if (!mutex) {
            mutex = std::make_shared<std::recursive_mutex>();
            weak = mutex;
        }
        if (mutationLocks_.size() > 256) {
            for (auto it = mutationLocks_.begin(); it != mutationLocks_.end();) {
                if (it->second.expired()) it = mutationLocks_.erase(it);
                else ++it;
            }
        }
    }
    return MutationGuard{mutex, std::unique_lock<std::recursive_mutex>(*mutex)};
}

bool DocRegistry::isReadOnlyMethod(const std::string& m) {
    // Cell authorization must not treat whole agent/sim/export families as reads.
    // Keep desktop document-only semantics separate from this security allowlist.
    static const std::set<std::string> reads = {
        "doc_list", "doc_tree", "doc_query", "doc_bbox", "doc_adjacency",
        "doc_get_meta", "doc_validate", "doc_resolve_pick", "doc_activate",
        "doc_rebuild", "doc_dry_run_ops", "doc_dep_query", "doc_drawing_views",
        "doc_sketch_overview", "doc_sketch_regions", "doc_view_metrics", "doc_snapshot",
        "doc_face_edges", "doc_face_points", "doc_face_wires", "doc_formats", "doc_list_ops",
        "doc_mesh_ref_list", "doc_mesh_ref_mesh",
        "doc_render_contact_sheet", "doc_render_diff", "doc_render_png", "doc_render_view",
        "asm_bom_components",
        "asm_world_bodies", "asm_source_status", "config_list", "sim_study_list", "weld_cut_list",
        "sketch_health", "sketch3d_health", "sketch_probe",
        "drawing_get", "drawing_bom", "drawing_model_items", "drawing_list_sheets",
        "drawing_render_png", "cam_describe", "cam_tree", "cam_extract", "cam_extract_from_doc",
        "ecad_describe", "ecad_tree", "ecad_resolve_pick", "ecad_dfm",
        "ecad_kicad_status", "ecad_kicad_check", "ecad_footprints", "ecad_kicad_libraries", "ecad_symbols",
        "eval_assembly_dof",
        "eval_check", "eval_compare", "eval_curvature", "eval_draft", "eval_edge_analysis", "eval_feature_attachment",
        "eval_feature_overlap", "eval_fit_cylinder", "eval_geometry_analysis", "eval_interference",
        "eval_envelope_clearance", "eval_removal_clearance",
        "eval_mass", "eval_measure", "eval_model_history", "eval_model_integrity", "eval_section_props",
        "eval_local_thickness", "eval_solid_health", "eval_symmetry", "eval_thickness",
        "eval_sheet_metal_bends",
        "harness_route", "harness_path_check",
        "mesh_read_raw", "mesh_wall_thickness", "sim_map_to_surface",
        "worker_normalize_face_query"
    };
    return reads.count(m) != 0;
}

// Mutating responses carry a top-level "revision" (ops_response / doc_tree /
// doc_open all do — core/doc/session.cpp); tolerate a nested doc_status too.
long DocRegistry::sniffRevision(const json& result) {
    if (!result.is_object()) return -1;
    if (result.contains("revision") && result["revision"].is_number())
        return result["revision"].get<long>();
    if (result.contains("rebuild") && result["rebuild"].is_object()) {
        const json& rb = result["rebuild"];
        if (rb.contains("doc_status") && rb["doc_status"].is_object() &&
            rb["doc_status"].contains("revision") &&
            rb["doc_status"]["revision"].is_number())
            return rb["doc_status"]["revision"].get<long>();
    }
    return -1;
}

std::string DocRegistry::sniffName(const json& result) {
    if (!result.is_object()) return {};
    if (result.contains("name") && result["name"].is_string())
        return result["name"].get<std::string>();
    if (result.contains("tree") && result["tree"].is_object() &&
        result["tree"].contains("name") && result["tree"]["name"].is_string())
        return result["tree"]["name"].get<std::string>();
    return {};
}

int DocRegistry::holdSecs() const {
    if (const char* h = std::getenv("CHUDWARE_HOSTD_HOLD_SECS"); h && *h)
        return std::max(1, std::atoi(h));
    return 120;
}

// Doc-GC knobs. Base grace default 90 s: long enough for the GUI to adopt a doc a
// disconnecting agent just minted (and for quick app relaunches), short enough
// that zombie docs never pile up behind dead sessions.
// Agent/mcp-origin docs get CHUDWARE_DOC_GC_AGENT_GRACE_S (default 1800 s / 30 min)
// so mission-built parts remain listed after the agent detaches — the GUI can
// still open them after a run finishes.
static long long gcGraceMsBase() {
    static const long long ms = [] {
        if (const char* v = std::getenv("CHUDWARE_DOC_GC"); v && *v && std::atoi(v) == 0)
            return -1LL;  // disabled
        if (const char* v = std::getenv("CHUDWARE_DOC_GC_GRACE_S"); v && *v)
            return (long long)std::max(1, std::atoi(v)) * 1000;
        return 90'000LL;
    }();
    return ms;
}

static long long gcAgentGraceMs() {
    static const long long ms = [] {
        if (const char* v = std::getenv("CHUDWARE_DOC_GC"); v && *v && std::atoi(v) == 0)
            return -1LL;
        if (const char* v = std::getenv("CHUDWARE_DOC_GC_AGENT_GRACE_S"); v && *v)
            return (long long)std::max(1, std::atoi(v)) * 1000;
        return 1'800'000LL;  // 30 min
    }();
    return ms;
}

static bool isAgentOrigin(const std::string& originKind) {
    return originKind == "mcp" || originKind == "agent";
}

// Grace for an abandoned doc: agent/mcp-origin uses the longer agent grace so
// mission-built parts survive past the default 90 s orphan window.
static long long gcGraceMsForOrigin(const std::string& originKind) {
    const long long base = gcGraceMsBase();
    if (base < 0) return -1LL;
    if (isAgentOrigin(originKind)) {
        const long long agent = gcAgentGraceMs();
        if (agent < 0) return -1LL;
        // Agent grace is a floor for agent-origin docs — never shorter than base.
        return std::max(base, agent);
    }
    return base;
}

void DocRegistry::updateOrphanDeadlineLocked(DocState& d) {
    if (!d.attach.empty() || !d.agentRuns.empty()) {
        d.orphanDeadlineMs = 0;
        return;
    }
    // Internal docs (DocState::internal) never take the 30-min agent floor. That floor
    // exists so a mission-built part stays openable from the GUI after the agent
    // detaches — but no client was ever TOLD an internal doc exists, so none can open
    // it and the grace buys nothing. When the agent's worker dies mid-preflight (the
    // one path where the clone's own `finally` cannot run), the base grace bounds the
    // orphan to ~90 s instead of pinning a full shadow copy of the part for half an hour.
    const long long grace = d.internal ? gcGraceMsBase() : gcGraceMsForOrigin(d.originKind);
    if (grace < 0) return;                                   // GC disabled
    if (d.orphanDeadlineMs == 0) d.orphanDeadlineMs = monoMs() + grace;
}

void DocRegistry::rememberAgentRunLocked(DocState& d, int clientId) {
    if (d.internal) return;
    const auto client = clientAgentRuns_.find(clientId);
    if (client == clientAgentRuns_.end()) return;
    bool added = false;
    for (const auto& scope : client->second)
        if (agentRuns_.count(scope) && d.agentRuns.insert(scope).second) added = true;
    if (added) {
        ++d.attachmentGeneration;
        updateOrphanDeadlineLocked(d);
    }
}

void DocRegistry::startAgentRun(const std::string& scope, int clientId,
                                const std::string& actorKey, const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    agentRuns_[scope] = actorKey;
    clientAgentRuns_[clientId].push_back(scope);
    const auto it = docs_.find(docId);
    if (it != docs_.end()) rememberAgentRunLocked(it->second, clientId);
}

void DocRegistry::joinAgentRun(int clientId, const std::string& scope,
                               const std::string& actorKey) {
    std::lock_guard<std::mutex> lk(mtx_);
    clientAgentRuns_.erase(clientId);
    const auto run = agentRuns_.find(scope);
    if (run != agentRuns_.end() && run->second == actorKey)
        clientAgentRuns_[clientId].push_back(scope);
}

void DocRegistry::endAgentRun(const std::string& scope) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        agentRuns_.erase(scope);
        for (auto it = clientAgentRuns_.begin(); it != clientAgentRuns_.end();) {
            auto& scopes = it->second;
            scopes.erase(std::remove(scopes.begin(), scopes.end(), scope), scopes.end());
            if (scopes.empty()) it = clientAgentRuns_.erase(it);
            else ++it;
        }
        for (auto& [id, d] : docs_) {
            if (!d.agentRuns.erase(scope)) continue;
            ++d.attachmentGeneration;
            updateOrphanDeadlineLocked(d);
            if (d.agentRuns.empty() && !d.deferredLastWriterKind.empty() &&
                rwCountLocked(d) == 0 && !d.internal) {
                json evt = {{"event", "doc.closed"},
                            {"data", {{"doc_id", id}, {"reason", "last_writer_detached"},
                                      {"source_kind", d.deferredLastWriterKind}}}};
                fanoutLocked(id, evt, -1, pending);
                d.deferredLastWriterKind.clear();
            }
        }
    }
    flush(pending);
}

void DocRegistry::setWriterGroup(int clientId, const std::string& writerGroup) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (writerGroup.empty()) writerGroups_.erase(clientId);
    else writerGroups_[clientId] = writerGroup;
}

void DocRegistry::setCellPrincipal(int clientId, const std::string& actorKey, bool readOnly) {
    std::lock_guard<std::mutex> lk(mtx_);
    cellPrincipals_[clientId] = CellPrincipal{actorKey, readOnly};
    if (!readOnly) return;
    for (auto& [id, d] : docs_) {
        auto it = d.attach.find(clientId);
        if (it != d.attach.end()) it->second.mode = "ro";
        if (d.hold.active && d.hold.clientId == clientId) d.hold = Hold{};
    }
}

const std::string& DocRegistry::writerGroupLocked(int clientId) const {
    static const std::string empty;
    const auto it = writerGroups_.find(clientId);
    return it == writerGroups_.end() ? empty : it->second;
}

bool DocRegistry::sameWriterLocked(int a, int b) const {
    if (a == b) return true;
    const auto& group = writerGroupLocked(a);
    return !group.empty() && group == writerGroupLocked(b);
}

long& DocRegistry::lastSeenRevLocked(DocState& d, int clientId) {
    const auto& group = writerGroupLocked(clientId);
    if (group.empty()) return d.attach[clientId].lastSeenRev;
    return d.groupLastSeenRev.try_emplace(group, -1).first->second;
}

void DocRegistry::forgetDetachedGroupLocked(DocState& d, int clientId) {
    const auto& group = writerGroupLocked(clientId);
    if (group.empty()) return;
    for (const auto& [cid, a] : d.attach)
        if (sameWriterLocked(cid, clientId)) return;
    d.groupLastSeenRev.erase(group);
}

int DocRegistry::rwCountLocked(const DocState& d) const {
    int n = 0;
    std::set<std::string_view> groups;
    for (const auto& [id, a] : d.attach) {
        if (a.mode != "rw") continue;
        const auto& group = writerGroupLocked(id);
        if (group.empty() || groups.insert(group).second) ++n;
    }
    return n;
}

// ------------------------------------------------------------------- events

void DocRegistry::fanoutLocked(const std::string& docId, const json& evt, int exceptClient,
                               std::vector<PendingEvent>& out) {
    // Backstop for DocState::internal: an internal doc was never announced to anyone,
    // so NO event about it may reach a subscriber. Every call site guards too; this
    // catch-all is what keeps a future one from resurrecting the phantom-tab bug (a
    // preflight shadow clone mirrored into the GUI as a part tab per AI operation).
    if (auto it = docs_.find(docId); it != docs_.end() && it->second.internal) return;

    const std::string line = evt.dump();
    for (const auto& [cid, ids] : subs_) {
        if (cid == exceptClient) continue;
        // A cell is one authenticated workspace. Presentation is workspace-visible
        // like doc_list, so a subscriber can learn about a newly presented document
        // before attaching it. All other document events remain attachment-scoped.
        if (cellPrincipals_.count(cid) && evt.value("event", std::string()) != "doc.presented") {
            auto doc = docs_.find(docId);
            if (doc == docs_.end() || !doc->second.attach.count(cid)) continue;
        }
        if (ids.count("*") || ids.count(docId)) out.push_back({cid, line});
    }
}

void DocRegistry::flush(std::vector<PendingEvent>& pending) {
    // Sent OUTSIDE mtx_: the sender takes the target conn's writeMtx, and a
    // slow client must never stall registry policy for everyone else.
    if (!send_) return;
    for (const auto& p : pending) send_(p.target, p.line);
    pending.clear();
}

void DocRegistry::expireHoldLocked(const std::string& docId, DocState& d,
                                   std::vector<PendingEvent>& out) {
    if (!d.hold.active || monoMs() < d.hold.deadlineMs) return;
    log().line("edit-hold on %s by %s#%d expired", docId.c_str(), d.hold.kind.c_str(),
               d.hold.clientId);
    // Internal doc (DocState::internal): the hold still expires below, it is simply
    // never announced — no client was ever told this doc exists.
    if (!d.internal) {
        json evt = {{"event", "doc.lease_change"},
                    {"data", {{"doc_id", docId},
                              {"holder", d.hold.kind},
                              {"state", "expired"}}}};
        fanoutLocked(docId, evt, /*exceptClient=*/-1, out);
    }
    d.hold = Hold{};
    ++d.attachmentGeneration;
}

std::vector<std::string> DocRegistry::tick() {
    std::vector<PendingEvent> pending;
    std::vector<std::string> expired;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const long long now = monoMs();
        for (auto& [id, d] : docs_) {
            expireHoldLocked(id, d, pending);
            if (d.orphanDeadlineMs > 0 && now >= d.orphanDeadlineMs &&
                d.attach.empty() && d.agentRuns.empty())
                expired.push_back(id);
        }
    }
    flush(pending);
    return expired;
}

// A pure read under the same lock as tick(), cheap enough to run on every 500 ms idle check.
bool DocRegistry::hasAttachments(int clientId) {
    std::lock_guard<std::mutex> lk(mtx_);
    for (const auto& [id, d] : docs_) {
        (void)id;
        if (d.attach.count(clientId)) return true;
    }
    return false;
}



bool DocRegistry::reapToken(const std::string& docId, std::uint64_t& token) const {
    std::lock_guard<std::mutex> lk(mtx_);
    const auto it = docs_.find(docId);
    if (it == docs_.end() || !it->second.attach.empty() || !it->second.agentRuns.empty() ||
        it->second.hold.active) return false;
    token = it->second.attachmentGeneration;
    return true;
}


bool DocRegistry::closeAbandoned(const std::string& docId, std::uint64_t token,
                                 const std::string& reason, const std::function<bool()>& closeFn) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const auto it = docs_.find(docId);
        if (it == docs_.end() || !it->second.attach.empty() || !it->second.agentRuns.empty() ||
            it->second.hold.active ||
            it->second.attachmentGeneration != token || !closeFn()) return false;
        if (!it->second.internal)
            fanoutLocked(docId, json{{"event", "doc.closed"},
                {"data", {{"doc_id", docId}, {"reason", reason},
                          {"source_kind", "daemon"}}}}, -1, pending);
        closedIds_.insert(docId);
        docs_.erase(it);
    }
    flush(pending);
    return true;
}


// ----------------------------------------------------------- daemon methods

bool DocRegistry::handleMethod(int clientId, const std::string& kind,
                               const std::string& method, const json& params,
                               json& result, bool& isError) {
    std::vector<PendingEvent> pending;
    bool handled = true;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const std::string docId = params.value("doc_id", std::string());

        if (method == "doc_present") {
            auto it = docs_.find(docId);
            if (docId.empty()) {
                isError = true;
                result = json{{"code", "invalid_params"}, {"message", "doc_id required"}};
            } else if (it == docs_.end() || it->second.originKind.empty() ||
                       it->second.internal) {
                isError = true;
                result = json{{"code", "not_found"}, {"message", "document not found"}};
            } else {
                DocState& d = it->second;
                d.presented = true;
                json data{{"doc_id", docId}, {"name", d.name}, {"revision", d.revision}};
                result = data;
                result["ok"] = true;
                result["presented"] = true;
                fanoutLocked(docId, json{{"event", "doc.presented"}, {"data", data}},
                             -1, pending);
            }
        } else if (method == "attach_doc") {
            std::string mode = params.value("mode", std::string("rw"));
            if (cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly &&
                mode == "rw") mode = "ro";
            if (mode != "rw" && mode != "ro") {
                isError = true;
                result = json{{"code", "invalid_params"},
                              {"message", "attach mode must be rw|ro"}};
            } else if (docId.empty()) {
                isError = true;
                result = json{{"code", "invalid_params"}, {"message", "doc_id required"}};
            } else if (!docs_.count(docId)) {
                isError = true;
                result = json{{"code", "not_found"}, {"message", "document not found"}};
            } else {
                DocState& d = docs_.at(docId);
                std::string otherWriter;
                if (mode == "rw") {
                    for (const auto& [cid, a] : d.attach)
                        if (!sameWriterLocked(cid, clientId) && a.mode == "rw")
                            otherWriter = kind + "#" + std::to_string(cid);
                }
                ++d.attachmentGeneration;
                Attach& a = d.attach[clientId];
                a.mode = mode;
                if (mode == "rw") d.deferredLastWriterKind.clear();
                rememberAgentRunLocked(d, clientId);
                long& seen = lastSeenRevLocked(d, clientId);
                if (seen < 0) seen = d.revision;
                // attach_doc itself returns the current revision to every member.
                if (!writerGroupLocked(clientId).empty()) seen = std::max(seen, d.revision);
                updateOrphanDeadlineLocked(d);
                result = json{{"ok", true}, {"mode", mode}, {"revision", d.revision},
                              {"metadata_revision", d.metadataRevision},
                              {"human_dirty", d.humanDirty}, {"saved_revision", d.savedRevision},
                              {"saved_metadata_revision", d.savedMetadataRevision}};
                if (!otherWriter.empty()) {
                    // Co-editors remain sequential; warn without rejecting the attach.
                    result["warning"] = "doc has another writer: " + otherWriter;
                }
            }
        } else if (method == "detach_doc") {
            auto it = docs_.find(docId);
            if (it != docs_.end()) {
                DocState& d = it->second;
                auto ait = d.attach.find(clientId);
                const bool wasRw = ait != d.attach.end() && ait->second.mode == "rw";
                if (ait != d.attach.end()) { d.attach.erase(ait); ++d.attachmentGeneration; }
                forgetDetachedGroupLocked(d, clientId);
                if (d.hold.active && d.hold.clientId == clientId) {
                    d.hold = Hold{};
                    ++d.attachmentGeneration;
                }
                updateOrphanDeadlineLocked(d);
                if (wasRw && rwCountLocked(d) == 0 && !d.internal) {
                    if (!d.agentRuns.empty()) {
                        d.deferredLastWriterKind = kind;
                    } else {
                        json evt = {{"event", "doc.closed"},
                                    {"data", {{"doc_id", docId},
                                              {"reason", "last_writer_detached"},
                                              {"source_kind", kind}}}};
                        fanoutLocked(docId, evt, clientId, pending);
                        d.deferredLastWriterKind.clear();
                    }
                }
            }
            result = json{{"ok", true}};
        } else if (method == "edit_hold") {
            if (cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly) {
                isError = true;
                result = json{{"code", "forbidden_right"}, {"message", "viewer cannot edit"}};
                return true;
            }
            auto found = docs_.find(docId);
            if (found == docs_.end()) {
                isError = true;
                result = json{{"code", "not_found"}, {"message", "document not found"}};
                return true;
            }
            DocState& d = found->second;
            expireHoldLocked(docId, d, pending);
            if (d.hold.active && !sameWriterLocked(d.hold.clientId, clientId)) {
                isError = true;
                result = json{{"code", "busy_preview"},
                              {"message", "doc is held by another client"},
                              {"holder", d.hold.kind},
                              {"retry_ms", 500}};
            } else {
                const bool fresh = !d.hold.active;
                if (fresh) { d.hold = Hold{true, clientId, kind, 0}; ++d.attachmentGeneration; }
                // A group member refreshes the lease without stealing the connection
                // that owns its detach/disconnect lifetime.
                d.hold.deadlineMs = monoMs() + (long long)holdSecs() * 1000;
                if (fresh && !d.internal) {  // internal docs announce no lease state
                    json evt = {{"event", "doc.lease_change"},
                                {"data", {{"doc_id", docId},
                                          {"holder", kind},
                                          {"state", "held"}}}};
                    fanoutLocked(docId, evt, clientId, pending);
                }
                result = json{{"ok", true}, {"hold_secs", holdSecs()}};
            }
        } else if (method == "edit_release") {
            auto it = docs_.find(docId);
            if (it != docs_.end() && it->second.hold.active &&
                sameWriterLocked(it->second.hold.clientId, clientId)) {
                it->second.hold = Hold{};
                ++it->second.attachmentGeneration;
                if (!it->second.internal) {  // internal docs announce no lease state
                    json evt = {{"event", "doc.lease_change"},
                                {"data", {{"doc_id", docId},
                                          {"holder", kind},
                                          {"state", "released"}}}};
                    fanoutLocked(docId, evt, clientId, pending);
                }
            }
            result = json{{"ok", true}};
        } else if (method == "subscribe") {
            // {"doc_ids": ["doc-...", ...]} or {"doc_ids": "*"} — additive.
            std::set<std::string>& ids = subs_[clientId];
            const json& want = params.contains("doc_ids") ? params["doc_ids"] : json("*");
            if (want.is_string()) {
                ids.insert(want.get<std::string>());
            } else if (want.is_array()) {
                for (const auto& v : want)
                    if (v.is_string()) ids.insert(v.get<std::string>());
            }
            result = json{{"ok", true}, {"subscribed", json(ids)}};
        } else {
            handled = false;
        }
    }
    flush(pending);
    return handled;
}

// -------------------------------------------------------------- policy gate

bool DocRegistry::preCall(int clientId, const std::string& kind, const std::string& /*userId*/,
                          const std::string& /*userDisplay*/, const std::string& method,
                          json& params, json& err, std::string& warning, bool cellPolicyRead) {
    // Agent undo gating (P2 multi-user): interactive undo/redo is human-only.
    // Agents keep doc_checkpoint/doc_revert + autosave/PLM for rollback. Kill-switch
    // CHUDWARE_UNDO_AGENTS=1 restores the old free-for-all behaviour.
    if ((method == "doc_undo" || method == "doc_redo") &&
        (kind == "mcp" || kind == "agent")) {
        const char* allow = std::getenv("CHUDWARE_UNDO_AGENTS");
        if (!(allow && std::string(allow) == "1")) {
            err = json{{"code", "undo_denied"},
                       {"message",
                        "agents use doc_checkpoint/doc_revert or PLM rollback"}};
            return false;
        }
    }

    const std::string docId =
        params.is_object() ? params.value("doc_id", std::string()) : std::string();
    // Document-free methods (including mint admission) are governed by the server's
    // authoritative cell method policy, not this document attachment gate.
    if (docId.empty()) return true;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        auto principal = cellPrincipals_.find(clientId);
        if (principal != cellPrincipals_.end() && principal->second.readOnly &&
            !cellPolicyRead && !isReadOnlyMethod(method)) {
            err = json{{"code", "forbidden_right"}, {"message", "viewer cannot edit"}};
            return false;
        }
        const auto doc = docs_.find(docId);
        if (doc != docs_.end()) rememberAgentRunLocked(doc->second, clientId);
        if (principal == cellPrincipals_.end() && isDocumentReadOnlyMethod(method))
            return true;
        if (principal != cellPrincipals_.end() &&
            (cellPolicyRead || isReadOnlyMethod(method))) return true;
    }
    // A protected document operation reaches the attachment/hold gate below.

    std::vector<PendingEvent> pending;
    bool ok = true;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        auto found = docs_.find(docId);
        // A failed call to an unknown id must not create a permanent attachment.
        // A successful call will register the doc in postCall instead.
        if (found == docs_.end()) return true;
        DocState& d = found->second;
        expireHoldLocked(docId, d, pending);

        // Edit-hold: someone (typically the GUI mid-preview) has exclusive edit.
        if (d.hold.active && !sameWriterLocked(d.hold.clientId, clientId)) {
            err = json{{"code", "busy_preview"},
                       {"message", "doc is held for editing by " + d.hold.kind +
                                       "; retry shortly"},
                       {"holder", d.hold.kind},
                       {"retry_ms", 500}};
            ok = false;
        } else {
            if (d.hold.active)  // same writer (including a group member): refresh
                d.hold.deadlineMs = monoMs() + (long long)holdSecs() * 1000;

            auto ait = d.attach.find(clientId);
            if (ait != d.attach.end() && ait->second.mode == "ro") {
                std::string holder;
                for (const auto& [cid, a] : d.attach)
                    if (a.mode == "rw") holder = "client#" + std::to_string(cid);
                err = json{{"code", "read_only"},
                           {"message", "attached read-only to " + docId +
                                           "; attach_doc mode=rw to edit"},
                           {"holder", holder}};
                ok = false;
            } else {
                if (ait == d.attach.end() || ait->second.mode.empty()) {
                    // Unattached mutator: implicit rw attach (compat with every
                    // pre-registry flow), warning when a writer already exists.
                    for (const auto& [cid, a] : d.attach)
                        if (!sameWriterLocked(cid, clientId) && a.mode == "rw")
                            warning = "doc has another writer: client#" + std::to_string(cid);
                    Attach& a = d.attach[clientId];
                    ++d.attachmentGeneration;
                    a.mode = "rw";
                    long& seen = lastSeenRevLocked(d, clientId);
                    if (seen < 0) seen = d.revision;
                    updateOrphanDeadlineLocked(d);
                }
            }
        }
    }
    flush(pending);
    return ok;
}

// -------------------------------------------------------- response sniffing

// The doc.changed / DocState::lastPush payload built out of a mutating engine response.
// ONE builder for BOTH the doc.changed fanout and the mint-time placeholder in postCall,
// so the two shapes can never drift: lastPush is served VERBATIM to the GUI by
// doc_snapshot (Server::handleDaemonMethod), and a payload the GUI mis-parses is worse
// than none. Every geometry field is optional — a response that carries none yields the
// bare envelope, exactly as before.
static json snapshotPayload(const std::string& docId, long rev, int clientId,
                            const std::string& kind, const std::string& method,
                            const std::string& userId, const std::string& userDisplay,
                            const json& result) {
    json data = {{"doc_id", docId},
                 {"revision", rev},
                 {"source_client", clientId},
                 {"source_kind", kind},
                 {"method", method},
                 {"user", {{"id", userId}, {"display", userDisplay}}}};
    if (!result.is_object()) return data;
    if (result.contains("tess") && result["tess"].is_object()) {
        data["tess"] = result["tess"];
        if (result["tess"].contains("aicm_path"))
            data["aicm_path"] = result["tess"]["aicm_path"];
    }
    if (result.contains("mesh_error")) data["mesh_error"] = result["mesh_error"];
    // Push the immutable revision snapshot with the mesh path. Additive fields let GUI
    // viewers prewarm inactive-tab caches without a serialized follow-up doc_tree call.
    if (result.contains("tree") && result["tree"].is_object())
        data["tree"] = result["tree"];
    if (result.contains("owner_map") && result["owner_map"].is_object())
        data["owner_map"] = result["owner_map"];
    if (result.contains("datums") && result["datums"].is_array())
        data["datums"] = result["datums"];
    if (result.contains("rebuild") && result["rebuild"].is_object()) {
        data["rebuild"] = result["rebuild"];
        if (result["rebuild"].contains("feature_status"))
            data["feature_status"] = result["rebuild"]["feature_status"];
    }
    for (const char* key : {"cache_trust", "cache_verification", "warnings", "mismatch", "warning"})
        if (result.contains(key)) data[key] = result[key];
    return data;
}

void DocRegistry::publishCacheRefresh(const std::string& docId, const json& response) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        auto it = docs_.find(docId);
        if (it == docs_.end() || it->second.internal || !response.is_object()) return;
        DocState& d = it->second;
        const long revision = sniffRevision(response);
        if (revision < 0 || revision < d.revision || revision < d.publishedRevision) return;
        // Verification reports the revision it inspected; it did not commit it.
        // A concurrent writer may not yet have reached postCall even if a read
        // already observed that revision. Only its postCall may publish the write.
        const bool verification = response.value("_refresh_kind", std::string()) == "cache_verification";
        const bool advanced = !verification && revision > d.publishedRevision;
        d.revision = std::max(d.revision, revision);
        if (advanced) {
            d.publishedRevision = revision;
            d.lastWriterClient = -1;
            d.lastWriterGroup.clear();
            d.lastWriterKind = "system";
            d.lastWriterMethod = "assembly_source_refresh";
            d.lastWriterUserId.clear();
            d.lastWriterUserDisplay.clear();
            d.lastWriterActorKey.clear();
        }
        json data = snapshotPayload(docId, revision, -1, "system",
                                    advanced ? "assembly_source_refresh" : "cache_refresh",
                                    "", "", response);
        data["human_dirty"] = d.humanDirty;
        data["saved_revision"] = d.savedRevision;
        data["metadata_revision"] = d.metadataRevision;
        data["saved_metadata_revision"] = d.savedMetadataRevision;
        d.lastPush = data;
        fanoutLocked(docId, json{{"event", advanced ? "doc.changed" : "doc.cache_refreshed"},
                                  {"data", std::move(data)}}, -1, pending);
    }
    flush(pending);
}

void DocRegistry::postCall(int clientId, const std::string& kind, const std::string& userId,
                           const std::string& userDisplay, const std::string& method,
                           const json& params, json& result, bool fanoutToSource) {
    if ((method == "ecad_new_project" || method == "ecad_open_project") &&
        result.is_object() && (result.contains("sch") || result.contains("pcb"))) {
        // A project RPC mints two independent sessions; attach and publish both.
        for (const char* key : {"sch", "pcb"}) {
            auto it = result.find(key);
            if (it != result.end() && it->is_object())
                postCall(clientId, kind, userId, userDisplay, method, params, *it, fanoutToSource);
        }
        return;
    }
    std::vector<PendingEvent> pending;
    const int fanoutExclude = fanoutToSource ? -1 : clientId;
    {
        std::unique_lock<std::mutex> lk(mtx_);

        std::string docId;
        if (result.is_object() && result.contains("doc_id") && result["doc_id"].is_string())
            docId = result["doc_id"].get<std::string>();
        if (docId.empty() && params.is_object())
            docId = params.value("doc_id", std::string());
        if (docId.empty()) return;
        if (closedIds_.count(docId)) return;

        DocState& d = docs_[docId];
        const long rev = sniffRevision(result);
        const long metaRev = result.is_object() && result.contains("metadata_revision") &&
                             result["metadata_revision"].is_number_integer()
            ? result["metadata_revision"].get<long>() : -1;
        const bool mintCall = method == "doc_new" || method == "doc_open" ||
            (params.value("doc_id", std::string()).empty() &&
             result.is_object() && result.contains("doc_id"));
        const bool minted = mintCall && d.originKind.empty() &&
                            !(method == "doc_open" && result.value("reused", false));
        if (const std::string name = sniffName(result); !name.empty() &&
            (d.name.empty() || rev >= d.revision)) d.name = name;

        if (minted) {
            const bool hadNewerWrite = d.publishedRevision > rev;
            Attach& a = d.attach[clientId];
            ++d.attachmentGeneration;
            a.mode = cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly
                         ? "ro" : "rw";
            lastSeenRevLocked(d, clientId) = rev;
            updateOrphanDeadlineLocked(d);
            d.revision = std::max(d.revision, rev);
            d.publishedRevision = std::max(d.publishedRevision, rev);
            const long initialMetaRev = metaRev >= 0 ? metaRev : 0;
            d.metadataRevision = std::max(d.metadataRevision, initialMetaRev);
            d.publishedMetadataRevision = std::max(d.publishedMetadataRevision, initialMetaRev);
            d.originKind = kind;
            const auto iflag = params.find("internal");
            d.internal = iflag != params.end() && iflag->is_boolean() && iflag->get<bool>();
            rememberAgentRunLocked(d, clientId);
            if (method == "doc_open" && !params.value("recovered", false)) {
                d.savedRevision = std::max(d.savedRevision, rev);
                d.savedMetadataRevision = std::max(d.savedMetadataRevision, initialMetaRev);
                d.refreshHumanDirty();
            }
            if (!hadNewerWrite) {
                d.lastWriterClient = clientId;
                d.lastWriterGroup = writerGroupLocked(clientId);
                d.lastWriterKind = kind;
                d.lastWriterMethod = method;
                d.lastWriterUserId = userId;
                d.lastWriterUserDisplay = userDisplay;
                d.lastWriterActorKey = cellPrincipals_.count(clientId)
                    ? cellPrincipals_.at(clientId).actorKey : std::string();
                if (rev >= 0 && result.is_object() && result.contains("tree") &&
                    result["tree"].is_object()) {
                    d.lastPush = snapshotPayload(docId, rev, clientId, kind, method,
                                                 userId, userDisplay, result);
                    if (!d.lastWriterActorKey.empty()) d.lastPush["actor_key"] = d.lastWriterActorKey;
                }
            }
            if (d.lastPush.is_object()) {
                d.lastPush["human_dirty"] = d.humanDirty;
                d.lastPush["saved_revision"] = d.savedRevision;
                d.lastPush["metadata_revision"] = d.metadataRevision;
                d.lastPush["saved_metadata_revision"] = d.savedMetadataRevision;
            }
            if (method == "doc_open" && result.is_object()) {
                result["human_dirty"] = d.humanDirty;
                result["saved_revision"] = d.savedRevision;
                result["saved_metadata_revision"] = d.savedMetadataRevision;
            }
            if (!d.internal) {
                json data = {{"doc_id", docId}, {"revision", rev}, {"source_client", clientId},
                             {"source_kind", kind}, {"origin_kind", kind}, {"method", method},
                             {"human_dirty", d.humanDirty}, {"saved_revision", d.savedRevision},
                             {"metadata_revision", d.metadataRevision},
                             {"saved_metadata_revision", d.savedMetadataRevision},
                             {"user", {{"id", userId}, {"display", userDisplay}}}};
                if (cellPrincipals_.count(clientId))
                    data["actor_key"] = cellPrincipals_.at(clientId).actorKey;
                if (!d.name.empty()) data["name"] = d.name;
                fanoutLocked(docId, json{{"event", "doc.opened"}, {"data", data}},
                             fanoutExclude, pending);
                for (auto& [event, exclude] : d.pendingChanges) {
                    event["data"]["human_dirty"] = d.humanDirty;
                    event["data"]["saved_revision"] = d.savedRevision;
                    event["data"]["saved_metadata_revision"] = d.savedMetadataRevision;
                    fanoutLocked(docId, event, exclude, pending);
                }
            }
            d.pendingChanges.clear();
            lk.unlock();
            flush(pending);
            return;
        }

        if (clientId >= 0) {
            rememberAgentRunLocked(d, clientId);
            const bool newAttach = !d.attach.count(clientId);
            Attach& observer = d.attach[clientId];
            if (newAttach) ++d.attachmentGeneration;
            if (observer.mode.empty()) {
                if (cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly)
                    observer.mode = "ro";
                else if (method == "doc_open" || isStudyMutation(method) ||
                         !isDocumentReadOnlyMethod(method))
                    observer.mode = "rw";
            }
            if (observer.mode == "rw") d.deferredLastWriterKind.clear();
            long& seen = lastSeenRevLocked(d, clientId);
            const auto& group = writerGroupLocked(clientId);
            const bool ownWrite = d.lastWriterClient == clientId ||
                                  (!group.empty() && group == d.lastWriterGroup);
            if (result.is_object() && seen >= 0 && d.revision > seen &&
                !ownWrite && d.lastWriterClient != -1) {
                result["_foreign_edits"] = json::array({json{
                    {"doc_id", docId}, {"revision", d.revision},
                    {"source_kind", d.lastWriterKind}, {"method", d.lastWriterMethod},
                    {"user", {{"id", d.lastWriterUserId}, {"display", d.lastWriterUserDisplay}}}}});
                if (!d.lastWriterActorKey.empty())
                    result["_foreign_edits"][0]["actor_key"] = d.lastWriterActorKey;
            }
            if (rev >= 0) seen = std::max(seen, rev);
            else if (seen < 0) seen = d.revision;
            updateOrphanDeadlineLocked(d);
        }

        if (method == "doc_open" && result.is_object()) {
            result["human_dirty"] = d.humanDirty;
            result["saved_revision"] = d.savedRevision;
            result["saved_metadata_revision"] = d.savedMetadataRevision;
        }

        if (rev >= 0) d.revision = std::max(d.revision, rev);
        if (metaRev >= 0) d.metadataRevision = std::max(d.metadataRevision, metaRev);
        if (method == "doc_save" && result.is_object()) {
            const auto saved = result.find("saved_revision");
            if (!params.value("preserve_path", false) && saved != result.end() &&
                saved->is_number_integer()) {
                d.savedRevision = std::max(d.savedRevision, saved->get<long>());
                if (metaRev >= 0)
                    d.savedMetadataRevision = std::max(d.savedMetadataRevision, metaRev);
                d.refreshHumanDirty();
            }
            result["saved_metadata_revision"] = d.savedMetadataRevision;
            result["human_dirty"] = d.humanDirty;
            if (!params.value("preserve_path", false) && !d.internal) {
                json data{{"doc_id", docId}, {"revision", d.revision},
                          {"source_client", clientId}, {"source_kind", kind},
                          {"method", method}, {"human_dirty", d.humanDirty},
                          {"saved_revision", d.savedRevision}};
                data["metadata_revision"] = d.metadataRevision;
                data["saved_metadata_revision"] = d.savedMetadataRevision;
                fanoutLocked(docId, json{{"event", "doc.changed.notice"}, {"data", data}},
                             -1, pending);
            }
        }
        if (method == "doc_reload" && rev >= 0) {
            d.savedRevision = std::max(d.savedRevision, rev);
            if (metaRev >= 0) {
                d.metadataRevision = std::max(d.metadataRevision, metaRev);
                d.publishedMetadataRevision = std::max(d.publishedMetadataRevision, metaRev);
                d.savedMetadataRevision = std::max(d.savedMetadataRevision, metaRev);
            }
            d.humanDirty = false;
            d.lastHumanRevision = -1;
            d.lastHumanMetadataRevision = -1;
        }
        const bool metadataOnly = method == "doc_set_meta" || method == "doc_set_name";
        if (metadataOnly && metaRev > d.publishedMetadataRevision) {
            d.publishedMetadataRevision = metaRev;
            const bool system = params.value("origin", std::string()) == "system";
            if (kind == "gui" && !system) {
                d.lastHumanMetadataRevision = std::max(d.lastHumanMetadataRevision, metaRev);
                d.refreshHumanDirty();
            }
            if (method == "doc_set_name" && !d.name.empty() && d.lastPush.is_object() &&
                d.lastPush.contains("tree") && d.lastPush["tree"].is_object())
                d.lastPush["tree"]["name"] = d.name;
            if (!d.internal) {
                json data{{"doc_id", docId}, {"revision", d.revision},
                          {"metadata_revision", metaRev},
                          {"saved_metadata_revision", d.savedMetadataRevision},
                          {"source_client", clientId}, {"source_kind", system ? "system" : kind},
                          {"method", method}, {"human_dirty", d.humanDirty},
                          {"saved_revision", d.savedRevision}};
                if (method == "doc_set_name" && !d.name.empty()) data["name"] = d.name;
                json event{{"event", "doc.changed.notice"}, {"data", std::move(data)}};
                if (d.originKind.empty()) d.pendingChanges.emplace_back(std::move(event), fanoutExclude);
                else fanoutLocked(docId, event, fanoutExclude, pending);
            }
        }

        if (rev >= 0) {
            const bool study = isStudyMutation(method);
            const bool mutation = !mintCall && !metadataOnly &&
                                  (study || !isDocumentReadOnlyMethod(method));
            const bool advanced = mutation && rev > d.publishedRevision;
            if (advanced) {
                d.publishedRevision = rev;
                const bool system = params.value("origin", std::string()) == "system";
                if (kind == "gui" && !system && method != "doc_reload") {
                    d.lastHumanRevision = std::max(d.lastHumanRevision, rev);
                    d.refreshHumanDirty();
                }
                d.lastWriterClient = clientId;
                d.lastWriterGroup = writerGroupLocked(clientId);
                d.lastWriterKind = system ? "system" : kind;
                d.lastWriterMethod = method;
                d.lastWriterUserId = userId;
                d.lastWriterUserDisplay = userDisplay;
                d.lastWriterActorKey = cellPrincipals_.count(clientId)
                    ? cellPrincipals_.at(clientId).actorKey : std::string();
                if (!d.internal) {
                    json data = study
                        ? json{{"doc_id", docId}, {"revision", rev}, {"source_client", clientId},
                               {"source_kind", d.lastWriterKind}, {"method", method}}
                        : snapshotPayload(docId, rev, clientId, d.lastWriterKind, method,
                                          userId, userDisplay, result);
                    data["human_dirty"] = d.humanDirty;
                    data["saved_revision"] = d.savedRevision;
                    data["metadata_revision"] = d.metadataRevision;
                    data["saved_metadata_revision"] = d.savedMetadataRevision;
                    if (!d.lastWriterActorKey.empty()) data["actor_key"] = d.lastWriterActorKey;
                    if (!study) d.lastPush = data;
                    json event{{"event", study ? "doc.studies_changed" : "doc.changed"},
                               {"data", std::move(data)}};
                    if (d.originKind.empty()) d.pendingChanges.emplace_back(std::move(event), fanoutExclude);
                    else fanoutLocked(docId, event, fanoutExclude, pending);
                }
            }
        }
        if (d.lastPush.is_object()) {
            d.lastPush["human_dirty"] = d.humanDirty;
            d.lastPush["saved_revision"] = d.savedRevision;
            d.lastPush["metadata_revision"] = d.metadataRevision;
            d.lastPush["saved_metadata_revision"] = d.savedMetadataRevision;
        }
    }
    flush(pending);
}

void DocRegistry::notifyHistoryCleared(const std::string& docId, int /*sourceClientId*/) {
    // Fan to every subscriber, including the saver: every GUI must grey undo/redo
    // for this doc after a human save. exceptClient = -1 => no exclusion.
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        // An unknown doc still fans out (the registry may have dropped it while mirrors
        // are still open); an internal one never does — see DocState::internal.
        if (auto it = docs_.find(docId); it == docs_.end() || !it->second.internal) {
            json evt = {{"event", "doc.history_cleared"},
                        {"data", {{"doc_id", docId}}}};
            fanoutLocked(docId, evt, /*exceptClient=*/-1, pending);
        }
    }
    flush(pending);
}

// --------------------------------------------------------------- disconnect

void DocRegistry::onDisconnect(int clientId, const std::string& kind) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        subs_.erase(clientId);
        for (auto& [docId, d] : docs_) {
            const bool heldHere = d.hold.active && d.hold.clientId == clientId;
            auto ait = d.attach.find(clientId);
            const bool wasRw = ait != d.attach.end() && ait->second.mode == "rw";
            if (heldHere) { d.hold = Hold{}; ++d.attachmentGeneration; }
            if (ait != d.attach.end()) { d.attach.erase(ait); ++d.attachmentGeneration; }
            forgetDetachedGroupLocked(d, clientId);
            bool writerRemains = false;
            for (const auto& [cid, a] : d.attach)
                if (a.mode == "rw" && sameWriterLocked(cid, clientId)) {
                    writerRemains = true;
                    break;
                }
            updateOrphanDeadlineLocked(d);
            // Internal docs (DocState::internal) hold no announced lease: nobody was
            // ever told they exist, so there is no lease state to correct.
            if ((heldHere || (wasRw && !writerRemains)) && !d.internal) {
                json evt = {{"event", "doc.lease_change"},
                            {"data", {{"doc_id", docId},
                                      {"holder", kind},
                                      {"state", "disconnected"}}}};
                fanoutLocked(docId, evt, clientId, pending);
            }
            // A short-lived tool socket is not the end of the owning turn.
            // Defer the mirror's agent-done notice until all live runs release it.
            if (wasRw && rwCountLocked(d) == 0 && !d.internal) {
                if (!d.agentRuns.empty()) {
                    d.deferredLastWriterKind = kind;
                } else {
                    json evt = {{"event", "doc.closed"},
                                {"data", {{"doc_id", docId},
                                          {"reason", "last_writer_detached"},
                                          {"source_kind", kind}}}};
                    fanoutLocked(docId, evt, clientId, pending);
                    d.deferredLastWriterKind.clear();
                }
            }
        }
        writerGroups_.erase(clientId);
        clientAgentRuns_.erase(clientId);
        cellPrincipals_.erase(clientId);
    }
    flush(pending);
}

// ------------------------------------------------------------- ownership reads

void DocRegistry::annotateDocList(json& result, int clientId) {
    if (!result.is_object() || !result.contains("docs") || !result["docs"].is_array())
        return;
    std::lock_guard<std::mutex> lk(mtx_);
    // An INTERNAL doc (a preflight shadow clone) is dropped from the listing entirely, not
    // just annotated. Suppressing its doc.opened fanout is not enough on its own: the GUI
    // also mirrors every doc it finds in doc_list at bootstrap and reconnect
    // (host/src/core/engine_events.cpp tryBootstrap), so a bootstrap landing inside the
    // clone's sub-second lifetime would still open the throwaway part tab this flag exists
    // to prevent. A doc no client was ever told about must not appear in the roster either.
    json kept = json::array();
    for (auto& e : result["docs"]) {
        if (!e.is_object()) { kept.push_back(std::move(e)); continue; }
        e["other_writer"] = false;
        const std::string id = e.value("doc_id", std::string());
        if (id.empty()) { kept.push_back(std::move(e)); continue; }
        auto it = docs_.find(id);
        if (it != docs_.end() && it->second.internal) continue;
        e["origin_kind"] = (it != docs_.end() && !it->second.originKind.empty())
                               ? it->second.originKind
                               : std::string("unknown");
        e["human_dirty"] = it != docs_.end() && it->second.humanDirty;
        e["saved_metadata_revision"] = it != docs_.end() ? it->second.savedMetadataRevision : -1;
        e["saved_revision"] = it != docs_.end() ? it->second.savedRevision : -1;
        e["presented"] = it != docs_.end() && it->second.presented;
        if (it != docs_.end()) {
            if (!it->second.lastWriterActorKey.empty())
                e["lastWriter"] = {{"actor_key", it->second.lastWriterActorKey}};
            for (const auto& [cid, attach] : it->second.attach) {
                if (attach.mode == "rw" && !sameWriterLocked(cid, clientId)) {
                    e["other_writer"] = true;
                    break;
                }
            }
        }
        kept.push_back(std::move(e));
    }
    result["docs"] = std::move(kept);
}

bool DocRegistry::humanDirty(const std::string& docId) const {
    std::lock_guard<std::mutex> lk(mtx_);
    const auto it = docs_.find(docId);
    return it != docs_.end() && it->second.humanDirty;
}

int DocRegistry::savedRevision(const std::string& docId) const {
    std::lock_guard<std::mutex> lk(mtx_);
    const auto it = docs_.find(docId);
    return it == docs_.end() ? -1 : static_cast<int>(it->second.savedRevision);
}

bool DocRegistry::closeBlockedBy(const std::string& docId, int requester) const {
    std::lock_guard<std::mutex> lk(mtx_);
    const auto it = docs_.find(docId);
    if (it == docs_.end()) return false;
    const DocState& d = it->second;
    if (!d.agentRuns.empty()) return true;
    if (d.hold.active && d.hold.clientId != requester) return true;
    for (const auto& [client, attachment] : d.attach) {
        (void)attachment;
        if (client != requester) return true;
    }
    return false;
}

void DocRegistry::restoreRecoveryState(const std::string& docId, const std::string& originKind,
                                        bool humanDirty, long revision, long savedRevision) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const auto it = docs_.find(docId);
        if (it == docs_.end()) return;
        DocState& d = it->second;
        d.originKind = originKind;
        d.revision = std::max(d.revision, revision);
        d.savedRevision = savedRevision;
        d.humanDirty = humanDirty;
        d.lastHumanRevision = humanDirty ? revision : -1;
        d.savedMetadataRevision = -1;
        d.lastHumanMetadataRevision = humanDirty ? d.metadataRevision : -1;
        if (d.lastPush.is_object()) {
            d.lastPush["human_dirty"] = humanDirty;
            d.lastPush["saved_revision"] = savedRevision;
            d.lastPush["saved_metadata_revision"] = d.savedMetadataRevision;
            d.lastPush["origin_kind"] = originKind;
        }
        if (!d.internal) {
            json data{{"doc_id", docId}, {"revision", d.revision},
                      {"source_client", -1}, {"source_kind", "system"},
                      {"method", "recovery_restore"}, {"origin_kind", originKind},
                      {"human_dirty", humanDirty}, {"saved_revision", savedRevision}};
            data["metadata_revision"] = d.metadataRevision;
            data["saved_metadata_revision"] = d.savedMetadataRevision;
            fanoutLocked(docId, json{{"event", "doc.changed.notice"}, {"data", data}}, -1, pending);
        }
    }
    flush(pending);
}

void DocRegistry::notifyDiskConflict(const std::string& docId, const std::string& path,
                                     const json& fileStamp) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const auto it = docs_.find(docId);
        if (it == docs_.end() || !it->second.humanDirty || it->second.internal) return;
        fanoutLocked(docId, json{{"event", "doc.disk_conflict"},
                                 {"data", {{"doc_id", docId}, {"path", path},
                                           {"observed_stamp", fileStamp}}}}, -1, pending);
    }
    flush(pending);
}



bool DocRegistry::isInternal(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    return it != docs_.end() && it->second.internal;
}

json DocRegistry::lastPushFor(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end() || it->second.internal) return json();
    return it->second.lastPush;
}

}  // namespace hostd
