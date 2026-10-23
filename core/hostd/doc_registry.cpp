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
        "asm_world_bodies", "config_list", "sim_study_list", "weld_cut_list",
        "sketch_health", "sketch3d_health", "sketch_probe",
        "drawing_get", "drawing_bom", "drawing_model_items", "drawing_render_png",
        "cam_describe", "cam_tree", "cam_extract", "ecad_describe", "ecad_tree",
        // Piping RPCs return networks/K without editing the CAD doc; piping_route
        // also reads world body boxes as obstacles. Persistence uses sim_study_*.
        "piping_route", "piping_edit", "piping_fitting_k",
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

bool DocRegistry::isReadOnlyMethod(const std::string& m) {
    // Cell authorization must not treat whole agent/sim/export families as reads.
    // Keep desktop document-only semantics separate from this security allowlist.
    static const std::set<std::string> reads = {
        "doc_list", "doc_tree", "doc_query", "doc_bbox", "doc_adjacency",
        "doc_get_meta", "doc_validate", "doc_resolve_pick", "doc_activate",
        "doc_rebuild", "doc_dry_run_ops", "doc_dep_query", "doc_drawing_views",
        "doc_sketch_overview", "doc_sketch_regions", "doc_view_metrics", "doc_snapshot",
        "doc_face_edges", "doc_face_points", "doc_face_wires", "doc_formats", "doc_list_ops",
        "doc_render_contact_sheet", "doc_render_diff", "doc_render_png", "doc_render_view",
        "asm_world_bodies", "config_list", "sim_study_list", "weld_cut_list",
        "sketch_health", "sketch3d_health", "sketch_probe",
        "drawing_get", "drawing_bom", "drawing_model_items", "drawing_list_sheets",
        "drawing_render_png", "cam_describe", "cam_tree", "cam_extract", "cam_extract_from_doc",
        "ecad_describe", "ecad_tree", "ecad_resolve_pick", "ecad_dfm",
        "eval_check", "eval_compare", "eval_curvature", "eval_draft", "eval_feature_attachment",
        "eval_feature_overlap", "eval_fit_cylinder", "eval_geometry_analysis", "eval_interference",
        "eval_mass", "eval_measure", "eval_model_integrity", "eval_section_props",
        "eval_solid_health", "eval_symmetry", "eval_thickness",
        "mesh_read_raw", "sim_map_to_surface", "worker_normalize_face_query"
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
    if (!d.attach.empty()) {
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
        if (cellPrincipals_.count(cid)) {
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
}

std::vector<std::string> DocRegistry::tick() {
    std::vector<PendingEvent> pending;
    std::vector<std::string> expired;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        const long long now = monoMs();
        for (auto& [id, d] : docs_) {
            expireHoldLocked(id, d, pending);
            // GC sweep: re-check emptiness at expiry — an attach since the
            // countdown started clears the deadline in updateOrphanDeadlineLocked.
            if (d.orphanDeadlineMs > 0 && now >= d.orphanDeadlineMs && d.attach.empty())
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

bool DocRegistry::isReapable(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return false;  // vanished — nothing for GC to reap
    return it->second.attach.empty();     // a re-attach since tick() cancels GC
}

bool DocRegistry::reapUnderLock(const std::string& docId, const std::function<void()>& closeFn) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return false;           // already closed elsewhere
    if (!it->second.attach.empty()) return false;  // (re)attached since the last check — abort
    closeFn();
    return true;
}

void DocRegistry::onDaemonClosedDoc(const std::string& docId, const std::string& reason) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        auto it = docs_.find(docId);
        if (it == docs_.end()) return;
        // Internal doc: the registry state still drops below, but a doc nobody was told
        // about must not produce a doc.closed (DocState::internal).
        if (!it->second.internal) {
            json evt = {{"event", "doc.closed"},
                        {"data", {{"doc_id", docId},
                                  {"reason", reason},
                                  {"source_kind", "daemon"}}}};
            fanoutLocked(docId, evt, /*exceptClient=*/-1, pending);
        }
        docs_.erase(it);
    }
    flush(pending);
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

        if (method == "attach_doc") {
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
            } else {
                DocState& d = docs_[docId];
                std::string otherWriter;
                if (mode == "rw") {
                    for (const auto& [cid, a] : d.attach)
                        if (!sameWriterLocked(cid, clientId) && a.mode == "rw")
                            otherWriter = kind + "#" + std::to_string(cid);
                }
                Attach& a = d.attach[clientId];
                a.mode = mode;
                long& seen = lastSeenRevLocked(d, clientId);
                if (seen < 0) seen = d.revision;
                // attach_doc itself returns the current revision to every member.
                if (!writerGroupLocked(clientId).empty()) seen = std::max(seen, d.revision);
                updateOrphanDeadlineLocked(d);
                result = json{{"ok", true}, {"mode", mode}, {"revision", d.revision}};
                if (!otherWriter.empty()) {
                    // Multi-editor is discouraged, not prohibited (plan): SUCCEED
                    // with a warning; the CAS overlap guard covers actual races.
                    result["warning"] = "doc has another writer: " + otherWriter;
                }
            }
        } else if (method == "detach_doc") {
            auto it = docs_.find(docId);
            if (it != docs_.end()) {
                DocState& d = it->second;
                auto ait = d.attach.find(clientId);
                const bool wasRw = ait != d.attach.end() && ait->second.mode == "rw";
                if (ait != d.attach.end()) d.attach.erase(ait);
                forgetDetachedGroupLocked(d, clientId);
                if (d.hold.active && d.hold.clientId == clientId) d.hold = Hold{};
                updateOrphanDeadlineLocked(d);
                if (wasRw && rwCountLocked(d) == 0 && !d.internal) {
                    // Last writer left: viewers should treat their mirror as final.
                    // Internal docs are excluded — no viewer ever mirrored one.
                    json evt = {{"event", "doc.closed"},
                                {"data", {{"doc_id", docId},
                                          {"reason", "last_writer_detached"},
                                          {"source_kind", kind}}}};
                    fanoutLocked(docId, evt, clientId, pending);
                }
            }
            result = json{{"ok", true}};
        } else if (method == "edit_hold") {
            if (cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly) {
                isError = true;
                result = json{{"code", "forbidden_right"}, {"message", "viewer cannot edit"}};
                return true;
            }
            DocState& d = docs_[docId];
            expireHoldLocked(docId, d, pending);
            if (d.hold.active && !sameWriterLocked(d.hold.clientId, clientId)) {
                isError = true;
                result = json{{"code", "busy_preview"},
                              {"message", "doc is held by another client"},
                              {"holder", d.hold.kind},
                              {"retry_ms", 500}};
            } else {
                const bool fresh = !d.hold.active;
                if (fresh) d.hold = Hold{true, clientId, kind, 0};
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
        DocState& d = docs_[docId];
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
                    a.mode = "rw";
                    long& seen = lastSeenRevLocked(d, clientId);
                    if (seen < 0) seen = d.revision;
                    updateOrphanDeadlineLocked(d);
                    ait = d.attach.find(clientId);
                }
                // Overlap CAS — only when the doc actually has >1 writer, so the
                // single-writer path stays byte-identical to Phase 1/2.
                const long seen = lastSeenRevLocked(d, clientId);
                if (rwCountLocked(d) > 1 && seen >= 0) {
                    if (method == "doc_apply_ops") {
                        if (!params.contains("base_revision"))
                            params["base_revision"] = seen;
                    } else if (method == "doc_undo" || method == "doc_redo") {
                        // The engine's undo/redo take no base_revision, so the CAS
                        // check lives here: reject when the caller's view is stale
                        // (same envelope the engine emits for apply_ops races).
                        if (d.revision >= 0 && d.revision != seen) {
                            err = json{{"code", "stale_revision"},
                                       {"message", "base_revision " +
                                                       std::to_string(seen) +
                                                       " != current " +
                                                       std::to_string(d.revision)},
                                       {"detail", {{"doc_id", docId},
                                                   {"revision", d.revision}}}};
                            ok = false;
                        }
                    }
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
    if (result.contains("tess") && result["tess"].is_object() &&
        result["tess"].contains("aicm_path"))
        data["aicm_path"] = result["tess"]["aicm_path"];
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
        if (revision < 0 || revision != d.revision) return;
        json data = d.lastPush.is_object() ? d.lastPush : json::object();
        data.update(snapshotPayload(docId, revision, d.lastWriterClient,
            d.lastWriterKind, d.lastWriterMethod, d.lastWriterUserId,
            d.lastWriterUserDisplay, response));
        if (!d.lastWriterActorKey.empty()) data["actor_key"] = d.lastWriterActorKey;
        d.lastPush = std::move(data);
        const std::string line = json{{"event", "doc.changed"}, {"data", d.lastPush}}.dump();
        for (const auto& [cid, ids] : subs_)
            if (d.attach.count(cid) && (ids.count("*") || ids.count(docId)))
                pending.push_back({cid, line});
    }
    flush(pending);
}

void DocRegistry::postCall(int clientId, const std::string& kind, const std::string& userId,
                           const std::string& userDisplay, const std::string& method,
                           const json& params, json& result, bool fanoutToSource) {
    std::vector<PendingEvent> pending;
    // A nested in-process agent call originates on the GUI's RPC connection but behaves as a
    // separate AI writer: the GUI is precisely the subscriber that must see it. Keep attachment
    // ownership on the real client id while allowing that same connection into fanout.
    const int fanoutExclude = fanoutToSource ? -1 : clientId;
    {
        std::lock_guard<std::mutex> lk(mtx_);

        // doc_close: drop registry state + tell subscribers.
        if (method == "doc_close") {
            const std::string docId = params.value("doc_id", std::string());
            auto it = docId.empty() ? docs_.end() : docs_.find(docId);
            if (it != docs_.end()) {
                // Internal doc (DocState::internal): drop the state silently. A client
                // that was never told the doc opened must not be told it closed — the
                // preflight clone is minted and closed inside one add_features call.
                if (!it->second.internal) {
                    json evt = {{"event", "doc.closed"},
                                {"data", {{"doc_id", docId},
                                          {"reason", "closed"},
                                          {"source_kind", kind}}}};
                    fanoutLocked(docId, evt, fanoutExclude, pending);
                }
                docs_.erase(it);
            }
            flush(pending);
            return;
        }

        // Resolve the doc this call touched: response doc_id (doc_new/doc_open/
        // parts_make/import all return one) beats the request param.
        std::string docId;
        if (result.is_object() && result.contains("doc_id") && result["doc_id"].is_string())
            docId = result["doc_id"].get<std::string>();
        if (docId.empty() && params.is_object())
            docId = params.value("doc_id", std::string());
        if (docId.empty()) {
            flush(pending);
            return;
        }

        const bool minted = !docs_.count(docId);
        DocState& d = docs_[docId];
        const long rev = sniffRevision(result);

        if (minted) {
            // The client whose call minted the doc gets rw implicitly.
            Attach& a = d.attach[clientId];
            a.mode = cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly
                         ? "ro" : "rw";
            updateOrphanDeadlineLocked(d);
            lastSeenRevLocked(d, clientId) = rev;
            d.revision = rev;
            d.lastWriterClient = clientId;
            d.lastWriterGroup = writerGroupLocked(clientId);
            d.lastWriterKind = kind;
            d.lastWriterMethod = method;
            d.lastWriterUserId = userId;
            d.lastWriterUserDisplay = userDisplay;
            if (cellPrincipals_.count(clientId))
                d.lastWriterActorKey = cellPrincipals_.at(clientId).actorKey;
            d.originKind = kind;  // who created it — user (gui) vs AI (mcp/agent)
            // {"internal":true} on the MINTING request marks a doc the daemon must never
            // announce. find() is safe on a null/non-object params and a non-bool value
            // degrades to false, so a client that never sends the flag behaves as today.
            const auto iflag = params.find("internal");
            d.internal = iflag != params.end() && iflag->is_boolean() && iflag->get<bool>();
            if (d.internal) {
                // Registered exactly like any other doc (implicit rw attach, origin kind
                // and GC bookkeeping above are untouched) but silent: the agent preflight
                // advisor mints a shadow clone of the live part inside a single
                // add_features call, and mirroring it opened a throwaway GUI part tab for
                // every operation the AI performed.
                flush(pending);
                return;
            }
            // Mint-time snapshot, so a GUI mirror tab can prime the INSTANT it appears.
            // WHY (live run, lexus_f_keychain 2026-07-29 — "really big delay from when it
            // shows up in the viewport and when i can actually op the part preview, its
            // almost like i have to wait until it adds the initial geometry"): lastPush was
            // written ONLY by the doc.changed branch below, i.e. only once a LATER
            // advancing mutating call landed. The mirror tab this doc.opened reveals primes
            // itself through the lock-free doc_snapshot (host/src/core/engine_events.cpp
            // primeMirrorFromSnapshot), served straight out of lastPush — still empty at
            // that moment, so the prime answered has_snapshot=false, the tab took focus
            // anyway and AppState::switchToTab fell through to a real doc_activate.
            // doc_activate takes the doc's EXCLUSIVE per-doc engine mutex (core/doc/
            // session.cpp DocLock + require_session), which the agent's add_features batch
            // holds for its entire compile+tessellate — so the user's open returned exactly
            // when the first geometry committed. A minting doc_new answers with the full
            // ops_response (revision/tree/owner_map/datums/rebuild plus a tess.aicm_path
            // for the bodiless AICM: write_aicm always emits its 12-byte header, so the
            // host's aicm_loader parses it to zero bodies), which is precisely what the
            // doc.changed push carries — hence the SAME builder, no second shape.
            // Stored only when the response carried BOTH a revision and a tree: without
            // either, host-side DocMirrorSnapshot::valid()/pushedSnapshot reject it and
            // has_snapshot=true would be a lie the GUI pays a round-trip for.
            // It can never shadow real geometry: this branch runs on the doc's FIRST
            // sighting only, and the doc.changed branch below overwrites lastPush on every
            // strictly-higher revision (`advanced` requires rev > d.revision).
            if (rev >= 0 && result.is_object() && result.contains("tree") &&
                result["tree"].is_object())
                d.lastPush = snapshotPayload(docId, rev, clientId, kind, method, userId,
                                             userDisplay, result);
            if (!d.lastPush.is_null() && !d.lastWriterActorKey.empty())
                d.lastPush["actor_key"] = d.lastWriterActorKey;
            // "name" lets a subscriber title the doc's tab on the spot instead of
            // chasing a doc_tree round-trip (which takes the doc's engine lock while
            // the minting client is still mid-op). Omitted when the minting response
            // carried no name — subscribers must tolerate its absence.
            json data = {{"doc_id", docId},
                         {"revision", rev},
                         {"source_client", clientId},
                         {"source_kind", kind},
                         {"origin_kind", kind},
                         {"method", method},
                         {"user", {{"id", userId}, {"display", userDisplay}}}};
            if (!d.lastWriterActorKey.empty()) data["actor_key"] = d.lastWriterActorKey;
            if (const std::string nm = sniffName(result); !nm.empty()) data["name"] = nm;
            json evt = {{"event", "doc.opened"}, {"data", data}};
            fanoutLocked(docId, evt, fanoutExclude, pending);
            flush(pending);
            return;
        }

        // _foreign_edits piggyback — BEFORE this call updates our last-seen: if
        // another client advanced the doc since this client's last touch, say so
        // in the result envelope (cad_tools turns it into a tool-output note).
        Attach& observer = d.attach[clientId];
        if (cellPrincipals_.count(clientId) && cellPrincipals_.at(clientId).readOnly &&
            observer.mode.empty()) observer.mode = "ro";
        long& seen = lastSeenRevLocked(d, clientId);
        const auto& group = writerGroupLocked(clientId);
        updateOrphanDeadlineLocked(d);   // any live touch resets the GC countdown
        const bool ownWrite = d.lastWriterClient == clientId ||
                              (!group.empty() && group == d.lastWriterGroup);
        if (result.is_object() && seen >= 0 && d.revision > seen &&
            !ownWrite && d.lastWriterClient != -1) {
            result["_foreign_edits"] = json::array({json{
                {"doc_id", docId},
                {"revision", d.revision},
                {"source_kind", d.lastWriterKind},
                {"method", d.lastWriterMethod},
                {"user", {{"id", d.lastWriterUserId},
                          {"display", d.lastWriterUserDisplay}}}}});
            if (!d.lastWriterActorKey.empty())
                result["_foreign_edits"][0]["actor_key"] = d.lastWriterActorKey;
        }

        if (rev >= 0) {
            const bool advanced = rev > d.revision && d.revision >= 0;
            if (rev > d.revision) d.revision = rev;
            // Concurrent responses may arrive out of revision order. A shared
            // writer must never forget a newer revision seen by another member.
            seen = group.empty() ? rev : std::max(seen, rev);
            if (advanced && !isDocumentReadOnlyMethod(method)) {
                d.lastWriterClient = clientId;
                d.lastWriterGroup = group;
                d.lastWriterKind = kind;
                d.lastWriterMethod = method;
                d.lastWriterUserId = userId;
                d.lastWriterUserDisplay = userDisplay;
                d.lastWriterActorKey = cellPrincipals_.count(clientId)
                    ? cellPrincipals_.at(clientId).actorKey : std::string();
                // No doc.changed for an internal doc (DocState::internal): no subscriber
                // was told it opened, and the payload below copies the whole tree.
                if (!d.internal) {
                    json data = snapshotPayload(docId, rev, clientId, kind, method, userId,
                                                userDisplay, result);
                    if (!d.lastWriterActorKey.empty()) data["actor_key"] = d.lastWriterActorKey;
                    // Keep the payload so a mirror tab that appears AFTER this push can
                    // still be populated without a doc_activate (see DocState::lastPush).
                    d.lastPush = data;
                    fanoutLocked(docId, json{{"event", "doc.changed"}, {"data", data}},
                                 fanoutExclude, pending);
                }
            } else if (advanced && !d.internal && method.rfind("sim_study_", 0) == 0 &&
                       method != "sim_study_list") {
                if (cellPrincipals_.count(clientId)) {
                    d.lastWriterClient = clientId;
                    d.lastWriterGroup = group;
                    d.lastWriterKind = kind;
                    d.lastWriterMethod = method;
                    d.lastWriterUserId = userId;
                    d.lastWriterUserDisplay = userDisplay;
                    d.lastWriterActorKey = cellPrincipals_.at(clientId).actorKey;
                }
                // Desktop viewers may edit studies; cell viewers are denied by the
                // exact write policy. Study revisions have no tree, so use their
                // dedicated event rather than a doc.changed snapshot.
                // Without it an agent's persisted FEA/motion study never reached an open
                // Simulation tree until some unrelated edit bumped the mirror's revision.
                json data = {{"doc_id", docId}, {"revision", rev},
                             {"source_client", clientId}, {"source_kind", kind},
                             {"method", method}};
                if (!d.lastWriterActorKey.empty()) data["actor_key"] = d.lastWriterActorKey;
                fanoutLocked(docId, json{{"event", "doc.studies_changed"}, {"data", std::move(data)}},
                             fanoutExclude, pending);
            }
        } else if (seen < 0) {
            seen = d.revision;  // first touch via a rev-less response
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
            if (heldHere) d.hold = Hold{};
            if (ait != d.attach.end()) d.attach.erase(ait);
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
            // A disconnecting agent is the same signal as an explicit detach:
            // when its rw attach was the last writer, tell viewers their mirror
            // is final so they can auto-close (mirrors the detach_doc path).
            // Without this, a crashed/killed agent leaves its live view stuck open
            // forever (the GUI's ro attach also blocks GC, so no other event fires).
            if (wasRw && rwCountLocked(d) == 0 && !d.internal) {  // internal: no mirror
                json evt = {{"event", "doc.closed"},
                            {"data", {{"doc_id", docId},
                                      {"reason", "last_writer_detached"},
                                      {"source_kind", kind}}}};
                fanoutLocked(docId, evt, clientId, pending);
            }
        }
        writerGroups_.erase(clientId);
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

bool DocRegistry::hasOtherRwWriter(const std::string& docId, int clientId) {
    // This is a document-lifetime check, not edit ownership: even another member
    // of the same writer group still needs the live doc when this connection closes.
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return false;
    for (const auto& [cid, a] : it->second.attach)
        if (cid != clientId && a.mode == "rw") return true;
    return false;
}

bool DocRegistry::isAgentOrigin(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return false;
    const std::string& k = it->second.originKind;
    return k == "agent" || k == "mcp";
}

bool DocRegistry::isInternal(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    return it != docs_.end() && it->second.internal;
}

json DocRegistry::lastPushFor(const std::string& docId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return json();
    return it->second.lastPush;
}

}  // namespace hostd
