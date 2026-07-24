#include "doc_registry.h"

#include <chrono>
#include <algorithm>
#include <cstdlib>

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
bool DocRegistry::isReadOnlyMethod(const std::string& m) {
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
        "doc_save", "doc_export_step", "doc_clear_history",
        // misc reads
        "asm_world_bodies", "config_list", "sim_study_list", "weld_cut_list",
        "sketch_health", "sketch3d_health", "sketch_probe",
        "drawing_get", "drawing_bom", "drawing_model_items", "drawing_render_png",
        "cam_describe", "cam_tree", "cam_extract", "ecad_describe", "ecad_tree",
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
    const long long grace = gcGraceMsForOrigin(d.originKind);
    if (grace < 0) return;                                   // GC disabled
    if (d.orphanDeadlineMs == 0) d.orphanDeadlineMs = monoMs() + grace;
}

int DocRegistry::rwCountLocked(const DocState& d) const {
    int n = 0;
    for (const auto& [id, a] : d.attach)
        if (a.mode == "rw") ++n;
    return n;
}

// ------------------------------------------------------------------- events

void DocRegistry::fanoutLocked(const std::string& docId, const json& evt, int exceptClient,
                               std::vector<PendingEvent>& out) {
    const std::string line = evt.dump();
    for (const auto& [cid, ids] : subs_) {
        if (cid == exceptClient) continue;
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
    json evt = {{"event", "doc.lease_change"},
                {"data", {{"doc_id", docId},
                          {"holder", d.hold.kind},
                          {"state", "expired"}}}};
    fanoutLocked(docId, evt, /*exceptClient=*/-1, out);
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

// Deliberately NOT a variant of tick(): tick() mutates (expires holds, fans out events) and is
// the accept loop's alone. This is a pure count under the same lock, cheap enough to run on
// every 500 ms idle check.
int DocRegistry::pendingGcCount() {
    std::lock_guard<std::mutex> lk(mtx_);
    const long long now = monoMs();
    int n = 0;
    for (const auto& [id, d] : docs_) {
        (void)id;
        if (d.orphanDeadlineMs > 0 && now < d.orphanDeadlineMs) ++n;
    }
    return n;
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
        json evt = {{"event", "doc.closed"},
                    {"data", {{"doc_id", docId},
                              {"reason", reason},
                              {"source_kind", "daemon"}}}};
        fanoutLocked(docId, evt, /*exceptClient=*/-1, pending);
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
                        if (cid != clientId && a.mode == "rw")
                            otherWriter = kind + "#" + std::to_string(cid);
                }
                Attach& a = d.attach[clientId];
                a.mode = mode;
                if (a.lastSeenRev < 0) a.lastSeenRev = d.revision;
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
                if (d.hold.active && d.hold.clientId == clientId) d.hold = Hold{};
                updateOrphanDeadlineLocked(d);
                if (wasRw && rwCountLocked(d) == 0) {
                    // Last writer left: viewers should treat their mirror as final.
                    json evt = {{"event", "doc.closed"},
                                {"data", {{"doc_id", docId},
                                          {"reason", "last_writer_detached"},
                                          {"source_kind", kind}}}};
                    fanoutLocked(docId, evt, clientId, pending);
                }
            }
            result = json{{"ok", true}};
        } else if (method == "edit_hold") {
            DocState& d = docs_[docId];
            expireHoldLocked(docId, d, pending);
            if (d.hold.active && d.hold.clientId != clientId) {
                isError = true;
                result = json{{"code", "busy_preview"},
                              {"message", "doc is held by another client"},
                              {"holder", d.hold.kind},
                              {"retry_ms", 500}};
            } else {
                const bool fresh = !d.hold.active;
                d.hold = Hold{true, clientId, kind, monoMs() + (long long)holdSecs() * 1000};
                if (fresh) {
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
                it->second.hold.clientId == clientId) {
                it->second.hold = Hold{};
                json evt = {{"event", "doc.lease_change"},
                            {"data", {{"doc_id", docId},
                                      {"holder", kind},
                                      {"state", "released"}}}};
                fanoutLocked(docId, evt, clientId, pending);
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
                          json& params, json& err, std::string& warning) {
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

    if (isReadOnlyMethod(method)) return true;
    const std::string docId =
        params.is_object() ? params.value("doc_id", std::string()) : std::string();
    if (docId.empty()) return true;  // doc_new/doc_open etc. mint; nothing to guard yet

    std::vector<PendingEvent> pending;
    bool ok = true;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        DocState& d = docs_[docId];
        expireHoldLocked(docId, d, pending);

        // Edit-hold: someone (typically the GUI mid-preview) has exclusive edit.
        if (d.hold.active && d.hold.clientId != clientId) {
            err = json{{"code", "busy_preview"},
                       {"message", "doc is held for editing by " + d.hold.kind +
                                       "; retry shortly"},
                       {"holder", d.hold.kind},
                       {"retry_ms", 500}};
            ok = false;
        } else {
            if (d.hold.active && d.hold.clientId == clientId)  // refresh on re-call
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
                        if (cid != clientId && a.mode == "rw")
                            warning = "doc has another writer: client#" + std::to_string(cid);
                    Attach& a = d.attach[clientId];
                    a.mode = "rw";
                    if (a.lastSeenRev < 0) a.lastSeenRev = d.revision;
                    updateOrphanDeadlineLocked(d);
                    ait = d.attach.find(clientId);
                }
                // Overlap CAS — only when the doc actually has >1 writer, so the
                // single-writer path stays byte-identical to Phase 1/2.
                if (rwCountLocked(d) > 1 && ait->second.lastSeenRev >= 0) {
                    if (method == "doc_apply_ops") {
                        if (!params.contains("base_revision"))
                            params["base_revision"] = ait->second.lastSeenRev;
                    } else if (method == "doc_undo" || method == "doc_redo") {
                        // The engine's undo/redo take no base_revision, so the CAS
                        // check lives here: reject when the caller's view is stale
                        // (same envelope the engine emits for apply_ops races).
                        if (d.revision >= 0 && d.revision != ait->second.lastSeenRev) {
                            err = json{{"code", "stale_revision"},
                                       {"message", "base_revision " +
                                                       std::to_string(ait->second.lastSeenRev) +
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

void DocRegistry::postCall(int clientId, const std::string& kind, const std::string& userId,
                           const std::string& userDisplay, const std::string& method,
                           const json& params, json& result) {
    std::vector<PendingEvent> pending;
    {
        std::lock_guard<std::mutex> lk(mtx_);

        // doc_close: drop registry state + tell subscribers.
        if (method == "doc_close") {
            const std::string docId = params.value("doc_id", std::string());
            if (!docId.empty() && docs_.count(docId)) {
                json evt = {{"event", "doc.closed"},
                            {"data", {{"doc_id", docId},
                                      {"reason", "closed"},
                                      {"source_kind", kind}}}};
                fanoutLocked(docId, evt, clientId, pending);
                docs_.erase(docId);
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
            a.mode = "rw";
            updateOrphanDeadlineLocked(d);
            a.lastSeenRev = rev;
            d.revision = rev;
            d.lastWriterClient = clientId;
            d.lastWriterKind = kind;
            d.lastWriterMethod = method;
            d.lastWriterUserId = userId;
            d.lastWriterUserDisplay = userDisplay;
            d.originKind = kind;  // who created it — user (gui) vs AI (mcp/agent)
            json evt = {{"event", "doc.opened"},
                        {"data", {{"doc_id", docId},
                                  {"revision", rev},
                                  {"source_client", clientId},
                                  {"source_kind", kind},
                                  {"origin_kind", kind},
                                  {"method", method},
                                  {"user", {{"id", userId}, {"display", userDisplay}}}}}};
            fanoutLocked(docId, evt, clientId, pending);
            flush(pending);
            return;
        }

        // _foreign_edits piggyback — BEFORE this call updates our last-seen: if
        // another client advanced the doc since this client's last touch, say so
        // in the result envelope (cad_tools turns it into a tool-output note).
        Attach& a = d.attach[clientId];  // observer entry (mode "") for pure readers
        updateOrphanDeadlineLocked(d);   // any live touch resets the GC countdown
        if (result.is_object() && a.lastSeenRev >= 0 && d.revision > a.lastSeenRev &&
            d.lastWriterClient != clientId && d.lastWriterClient != -1) {
            result["_foreign_edits"] = json::array({json{
                {"doc_id", docId},
                {"revision", d.revision},
                {"source_kind", d.lastWriterKind},
                {"method", d.lastWriterMethod},
                {"user", {{"id", d.lastWriterUserId},
                          {"display", d.lastWriterUserDisplay}}}}});
        }

        if (rev >= 0) {
            const bool advanced = rev > d.revision && d.revision >= 0;
            if (rev > d.revision) d.revision = rev;
            a.lastSeenRev = rev;
            if (advanced && !isReadOnlyMethod(method)) {
                d.lastWriterClient = clientId;
                d.lastWriterKind = kind;
                d.lastWriterMethod = method;
                d.lastWriterUserId = userId;
                d.lastWriterUserDisplay = userDisplay;
                json data = {{"doc_id", docId},
                             {"revision", rev},
                             {"source_client", clientId},
                             {"source_kind", kind},
                             {"method", method},
                             {"user", {{"id", userId}, {"display", userDisplay}}}};
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
                if (result.contains("rebuild") && result["rebuild"].is_object())
                    data["rebuild"] = result["rebuild"];
                if (result.contains("rebuild") && result["rebuild"].is_object() &&
                    result["rebuild"].contains("feature_status"))
                    data["feature_status"] = result["rebuild"]["feature_status"];
                fanoutLocked(docId, json{{"event", "doc.changed"}, {"data", data}},
                             clientId, pending);
            }
        } else if (a.lastSeenRev < 0) {
            a.lastSeenRev = d.revision;  // first touch via a rev-less response
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
        json evt = {{"event", "doc.history_cleared"},
                    {"data", {{"doc_id", docId}}}};
        fanoutLocked(docId, evt, /*exceptClient=*/-1, pending);
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
            updateOrphanDeadlineLocked(d);
            if (heldHere || wasRw) {
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
            if (wasRw && rwCountLocked(d) == 0) {
                json evt = {{"event", "doc.closed"},
                            {"data", {{"doc_id", docId},
                                      {"reason", "last_writer_detached"},
                                      {"source_kind", kind}}}};
                fanoutLocked(docId, evt, clientId, pending);
            }
        }
    }
    flush(pending);
}

// ------------------------------------------------------------- ownership reads

void DocRegistry::annotateDocList(json& result) {
    if (!result.is_object() || !result.contains("docs") || !result["docs"].is_array())
        return;
    std::lock_guard<std::mutex> lk(mtx_);
    for (auto& e : result["docs"]) {
        if (!e.is_object()) continue;
        const std::string id = e.value("doc_id", std::string());
        if (id.empty()) continue;
        auto it = docs_.find(id);
        e["origin_kind"] = (it != docs_.end() && !it->second.originKind.empty())
                               ? it->second.originKind
                               : std::string("unknown");
    }
}

bool DocRegistry::hasOtherRwWriter(const std::string& docId, int clientId) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = docs_.find(docId);
    if (it == docs_.end()) return false;
    for (const auto& [cid, a] : it->second.attach)
        if (cid != clientId && a.mode == "rw") return true;
    return false;
}

}  // namespace hostd
