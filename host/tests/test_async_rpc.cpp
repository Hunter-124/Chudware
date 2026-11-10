// test_async_rpc.cpp — core::AsyncRpc's streamed-event channel (omp_login open_url).
//
// Links the REAL async_rpc.cpp against a stub WorkerClient defined in this TU (the class's
// three members AsyncRpc touches — call / abortPending / dtor — are defined here instead of
// linking worker_client.cpp and the whole engine stack), so the queueing rules under test
// are the shipped ones.
//
// The contract under test:
//   * the 4-arg call() is byte-compatible: no Event, the worker sees an empty EventFn;
//   * streamed events NEVER run on the dispatch thread — they queue and are delivered
//     inside drain() on the calling ("GL") thread, in arrival order, before their call's
//     Done in the same drain pass;
//   * shutdown() drops undelivered events (callbacks must not run after shutdown).
// This standalone regression must execute its assertions in Release builds too.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "core/async_rpc.h"
#include "core/doc_mirror.h"
#include "worker_client.h"

using nlohmann::json;

// ---- stub WorkerClient ------------------------------------------------------------------
// The behavior each test wants from the "worker": set by the test, executed on AsyncRpc's
// dispatch thread inside WorkerClient::call.
namespace {
std::function<bool(const std::string&, const json&, json&, std::string&,
                   const WorkerClient::EventFn&)>
    g_fakeCall;
}  // namespace

WorkerClient::~WorkerClient() {}
void WorkerClient::abortPending() { abort_.store(true); }
bool WorkerClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int /*timeoutMs*/, const EventFn& onEvent) {
    return g_fakeCall ? g_fakeCall(method, params, result, err, onEvent) : false;
}

namespace {

void drainUntil(core::AsyncRpc& rpc, const bool& flag, int maxMs = 5000) {
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(maxMs);
    while (!flag) {
        assert(std::chrono::steady_clock::now() < deadline && "drain timed out");
        rpc.drain();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

void test_no_event_call_passes_empty_eventfn_and_completes() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    bool sawEmptyEventFn = false;
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn& ev) {
        assert(method == "plain_method");
        sawEmptyEventFn = !ev;   // 4-arg call() must not manufacture an event sink
        result = {{"ok", true}};
        return true;
    };
    bool done = false;
    rpc.call("plain_method", json::object(), [&](const core::RpcResult& r) {
        assert(r.ok && r.result.value("ok", false));
        done = true;
    });
    drainUntil(rpc, done);
    assert(sawEmptyEventFn);
    assert(!rpc.busy());
    rpc.shutdown();
}

void test_mesh_reference_reads_do_not_block_interaction() {
    for (const char* method : {"doc_mesh_ref_list", "doc_mesh_ref_mesh",
                               "ecad_tree", "ecad_kicad_status",
                               "doc_mesh_ref_add", "doc_mesh_ref_update",
                               "doc_mesh_ref_remove", "doc_tree"}) {
        WorkerClient w;
        core::AsyncRpc rpc(w);
        std::atomic<bool> entered{false}, release{false};
        g_fakeCall = [&](const std::string&, const json&, json& result, std::string&,
                         const WorkerClient::EventFn&) {
            entered.store(true);
            while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            result = {{"ok", true}};
            return true;
        };
        bool done = false;
        rpc.call(method, {{"doc_id", "D"}}, [&](const core::RpcResult& r) {
            assert(r.ok); done = true;
        });
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!entered.load()) {
            assert(std::chrono::steady_clock::now() < deadline);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        const std::string name(method);
        const bool isRead = name == "doc_mesh_ref_list" || name == "doc_mesh_ref_mesh" ||
                            name == "ecad_tree" || name == "ecad_kicad_status";
        assert(rpc.busy() && rpc.interactionBusy() == !isRead);
        release.store(true);
        drainUntil(rpc, done);
        assert(!rpc.busy() && !rpc.interactionBusy());
        rpc.shutdown();
    }
}

// A multi-second provider/worker status poll (ai_prompt_baseline, subscription_usage, …) must
// not queue a document open behind it: background queries have their own dispatch lane. Before
// the split, opening a part right after an assembly sat ~20 s behind these polls.
bool primaryOpenCall(const std::string& method, const json&, json& result, std::string&, int,
                     const std::atomic<bool>&) {
    result = {{"ok", true}, {"method", method}};
    return true;
}

void test_background_queries_do_not_delay_document_calls() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    rpc.setPrimaryCaller(&primaryOpenCall);
    std::atomic<bool> entered{false}, release{false};
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        if (method == "ai_prompt_baseline") {
            entered.store(true);
            while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        result = {{"ok", true}};
        return true;
    };
    bool baselineDone = false, openDone = false, readDone = false;
    rpc.call("ai_prompt_baseline", json::object(),
             [&](const core::RpcResult& r) { assert(r.ok); baselineDone = true; });
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    rpc.callPrimary("doc_open", {{"path", "p.step+"}},
                    [&](const core::RpcResult& r) { assert(r.ok); openDone = true; });
    // A doc_id-scoped call stays on the document lane even under a background name.
    rpc.call("skills_list", {{"doc_id", "D"}},
             [&](const core::RpcResult& r) { assert(r.ok); readDone = true; });
    drainUntil(rpc, openDone);
    drainUntil(rpc, readDone);
    assert(!baselineDone && rpc.busy());
    release.store(true);
    drainUntil(rpc, baselineDone);
    assert(!rpc.busy());
    rpc.shutdown();
}

void test_events_deliver_on_drain_thread_in_order_before_done() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    const std::thread::id mainThread = std::this_thread::get_id();

    std::thread::id workerThread;
    std::atomic<bool> callFinished{false};
    g_fakeCall = [&](const std::string&, const json&, json& result, std::string&,
                     const WorkerClient::EventFn& ev) {
        workerThread = std::this_thread::get_id();
        assert(ev);   // streaming call must hand the worker a live sink
        ev(json{{"kind", "open_url"}, {"url", "https://one"}});
        ev(json{{"kind", "progress"}, {"message", "two"}});
        result = {{"ok", true}, {"state", "oauth_connected"}};
        callFinished = true;
        return true;
    };

    std::vector<std::string> order;         // event payload markers + "done"
    std::vector<std::thread::id> threads;   // thread each UI callback ran on
    bool done = false;
    rpc.call(
        "omp_login", json{{"providerId", "anthropic"}},
        [&](const core::RpcResult& r) {
            assert(r.ok);
            order.push_back("done");
            threads.push_back(std::this_thread::get_id());
            done = true;
        },
        [&](const json& ev) {
            order.push_back(ev.contains("url") ? ev["url"].get<std::string>()
                                               : ev.value("message", std::string()));
            threads.push_back(std::this_thread::get_id());
        });

    // The worker fires both events on the dispatch thread; until this thread drains,
    // NOTHING may have been delivered — that is the queueing rule that keeps ImGui state
    // GL-thread-only.
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!callFinished) {
        assert(std::chrono::steady_clock::now() < deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(order.empty());

    drainUntil(rpc, done);
    assert(order.size() == 3);
    assert(order[0] == "https://one");
    assert(order[1] == "two");
    assert(order[2] == "done");
    for (const std::thread::id& t : threads) {
        assert(t == mainThread);
        assert(t != workerThread);
    }
    assert(!rpc.busy());
    rpc.shutdown();
}

void test_shutdown_drops_undelivered_events() {
    WorkerClient w;
    bool uiCallbackRan = false;
    {
        core::AsyncRpc rpc(w);
        std::atomic<bool> callFinished{false};
        g_fakeCall = [&](const std::string&, const json&, json& result, std::string&,
                         const WorkerClient::EventFn& ev) {
            ev(json{{"kind", "open_url"}, {"url", "https://never-delivered"}});
            result = {{"ok", true}};
            callFinished = true;
            return true;
        };
        rpc.call(
            "omp_login", json::object(),
            [&](const core::RpcResult&) { uiCallbackRan = true; },
            [&](const json&) { uiCallbackRan = true; });
        const auto deadline =
            std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!callFinished) {
            assert(std::chrono::steady_clock::now() < deadline);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        rpc.shutdown();   // never drained — both queues must be dropped unrun
    }
    assert(!uiCallbackRan);
}

void test_mirror_async_completion_does_not_modify_another_document() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    const json datumA = json::array({{{"id", "datum-A"}}});
    const json datumB = json::array({{{"id", "datum-B"}}});
    const json solidBodies = json::array({{{"kind", "solid"}}});
    const json surfaceBodies = json::array({{{"kind", "surface"}}});
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1},
                                     {"tree", tree}, {"datums", datumA}}, err));
    std::vector<std::pair<std::string, bool>> pending;
    std::vector<std::pair<std::string, int>> human;
    mirror.setPendingOpsObserver([&](const std::string& id, bool begun) {
        pending.emplace_back(id, begun);
    });
    mirror.setHumanMutationObserver([&](const std::string& id, int revision) {
        human.emplace_back(id, revision);
    });
    std::atomic<bool> entered{false}, release{false};
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        assert(method == "doc_apply_ops");
        entered.store(true);
        while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        result = {{"revision", 2}, {"tree", tree}, {"datums", datumA},
                  {"tess", {{"aicm_path", "A.aicm"}, {"bodies", solidBodies}}}};
        return true;
    };
    bool done = false, current = true;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse& response, bool wasCurrent) {
            assert(response.ok && response.aicm_path == "A.aicm");
            current = wasCurrent; done = true;
        });
    assert(pending == (std::vector<std::pair<std::string, bool>>{{"A", true}}));
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 9},
                                     {"tree", tree}, {"datums", datumB},
                                     {"tess", {{"bodies", surfaceBodies}}}}, err));
    release.store(true);
    drainUntil(rpc, done);
    assert(!current && mirror.docId() == "B" && mirror.revision() == 9);
    assert(pending == (std::vector<std::pair<std::string, bool>>{{"A", true}, {"A", false}}));
    assert(human == (std::vector<std::pair<std::string, int>>{{"A", 2}}));
    assert(mirror.refDatums() == datumB);
    assert(mirror.solidPresenceKnown() && !mirror.hasSolidBody());
    // Current responses still adopt datums, while omitted datums retain the last known state.
    assert(mirror.ingestOpsResponse({{"revision", 10}, {"tree", tree}, {"datums", datumA}}).ok);
    assert(mirror.refDatums() == datumA);
    assert(mirror.ingestOpsResponse({{"revision", 11}, {"tree", tree}}).ok);
    assert(mirror.refDatums() == datumA);
    // Returning to A before its old reply arrives must not revive that reply.
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 3},
                                     {"tree", tree}, {"datums", datumA}}, err));
    entered.store(false); release.store(false); done = false; current = true;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse&, bool wasCurrent) { current = wasCurrent; done = true; });
    const auto secondDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < secondDeadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 9},
                                     {"tree", tree}, {"datums", datumB}}, err));
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 4},
                                     {"tree", tree}, {"datums", datumB},
                                     {"tess", {{"bodies", surfaceBodies}}}}, err));
    release.store(true);
    drainUntil(rpc, done);
    assert(!current && mirror.docId() == "A" && mirror.revision() == 4);
    assert(mirror.refDatums() == datumB);
    assert(mirror.solidPresenceKnown() && !mirror.hasSolidBody());
    rpc.shutdown();
}

void test_mirror_rejections_preserve_committed_preview_and_diagnostics() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json initial = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "f1"}, {"type", "box"}, {"params", {{"length", 10}}}},
        {{"id", "f2"}, {"type", "fillet"}}
    })}};
    json preview = initial;
    preview["features"][0]["params"]["length"] = 12;
    const json datums = json::array({{{"id", "live-datum"}}});
    const json owners = {{"f1", {{"bodies", json::array({1})},
                                  {"faces", json::array({json::array({1, 1})})}}}};
    const json liveStatus = json::array({
        {{"id", "f1"}, {"ok", true}, {"error", nullptr}},
        {{"id", "f2"}, {"ok", true}, {"error", nullptr}, {"warning", "Adjusted radius"}}
    });
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 7}, {"tree", initial},
                                    {"datums", datums}, {"owner_map", owners},
                                    {"rebuild", {{"feature_status", liveStatus}}}}, err));
    int notifications = 0;
    mirror.setListener([&](bool) { ++notifications; });

    json badAdd = preview;
    badAdd["features"].push_back({{"id", "f3"}, {"type", "fillet"}});
    json badEdit = preview;
    badEdit["features"][0]["params"]["length"] = 0;
    json badDownstream = preview;
    badDownstream["features"][0]["params"]["length"] = 2;
    const json addStatus = json::array({
        {{"id", "f1"}, {"ok", true}}, {{"id", "f2"}, {"ok", true}},
        {{"id", "f3"}, {"ok", false}, {"error", "No matching edges"},
         {"data", {{"selected_edges", 0}}}}
    });
    const json editStatus = json::array({
        {{"id", "f1"}, {"ok", false}, {"error", "Invalid box length"}},
        {{"id", "f2"}, {"ok", false}, {"error", "Dependent geometry failed"}}
    });
    const json downstreamStatus = json::array({
        {{"id", "f1"}, {"ok", true}},
        {{"id", "f2"}, {"ok", false}, {"error", "Downstream fillet failed"}}
    });
    auto rejectedReply = [&](const json& candidate, const json& statuses) {
        return json{{"revision", 8}, {"tree", preview}, {"rejected_tree", candidate},
                    {"id_map", json::object()}, {"owner_map", owners}, {"datums", datums},
                    {"tess", {{"aicm_path", "live.aicm"}}},
                    {"rebuild", {{"feature_status", statuses}}}};
    };
    const json accepted = {{"revision", 8}, {"tree", preview}, {"id_map", json::object()},
                           {"owner_map", owners}, {"datums", datums},
                           {"rebuild", {{"feature_status", liveStatus}}}};
    const json validationError = {
        {"code", "invalid_op"}, {"message", "Invalid expression"},
        {"detail", {{"reason", "invalid expression"},
                    {"all_errors", json::array({"bad equation", "bad dimension"})}}}
    };
    json conflictingError = validationError;
    conflictingError["detail"]["current_revision"] = 10;
    json revisionMismatch = rejectedReply(badAdd, addStatus);
    revisionMismatch["revision"] = 10;
    const json unknownError = {{"code", "internal"}, {"message", "Unknown outcome"},
                               {"detail", json::object()}};
    const std::vector<json> replies = {
        accepted, rejectedReply(badAdd, addStatus), rejectedReply(badEdit, editStatus),
        rejectedReply(badDownstream, downstreamStatus), validationError,
        revisionMismatch, conflictingError, unknownError, nullptr, validationError
    };
    std::atomic<size_t> next{0};
    g_fakeCall = [&](const std::string& method, const json& params, json& result, std::string& error,
                     const WorkerClient::EventFn&) {
        if (method == "doc_revert") {
            if (params.value("expect_revision", -1) != 8) {
                result = {{"code", "stale_revision"}, {"message", "Revision changed"},
                          {"detail", {{"current_revision", 8}}}};
            } else {
                result = {{"revision", 9}, {"tree", initial}, {"owner_map", owners},
                          {"datums", datums}, {"rebuild", {{"feature_status", liveStatus}}}};
            }
            return true;
        }
        assert(method == "doc_apply_ops");
        const auto i = next.fetch_add(1);
        assert(i < replies.size());
        if (replies[i].is_null()) { error = "Connection lost"; return false; }
        result = replies[i];
        return true;
    };
    auto submit = [&](const json& op) {
        core::OpsResponse response;
        bool done = false, current = false;
        mirror.applyOpsAsync(rpc, w, json::array({op}),
            [&](const core::OpsResponse& r, bool c) { response = r; current = c; done = true; });
        drainUntil(rpc, done);
        return std::make_pair(response, current);
    };
    auto previewResult = submit({{"op", "edit_feature"}, {"id", "f1"}});
    assert(previewResult.first.ok && previewResult.second);
    assert(mirror.revision() == 8 && mirror.tree() == preview);
    assert(mirror.featureStatus() == liveStatus && mirror.ownerOfFace(1, 1) == "f1");
    assert(mirror.refDatums() == datums && notifications == 1);

    const std::vector<json> statuses = {addStatus, editStatus, downstreamStatus};
    const std::vector<std::string> errors = {
        "No matching edges", "Invalid box length", "Downstream fillet failed"
    };
    for (size_t i = 0; i < statuses.size(); ++i) {
        const auto result = submit({{"op", i == 0 ? "add_feature" : "edit_feature"}});
        assert(result.second && !result.first.ok && result.first.rejected &&
               result.first.unchanged && result.first.revision == 8);
        assert(result.first.error_code == "rebuild_rejected");
        assert(result.first.error_message.find(errors[i]) != std::string::npos);
        if (i == 1)
            assert(result.first.error_message.find("Dependent geometry failed") != std::string::npos);
        assert(result.first.feature_status == statuses[i]);
        assert(result.first.id_map.is_object() && result.first.id_map.empty());
        assert(result.first.aicm_path == "live.aicm");
        assert(mirror.docId() == "A" && mirror.revision() == 8 && mirror.tree() == preview);
        assert(mirror.featureStatus() == liveStatus && mirror.refDatums() == datums);
        assert(mirror.ownerOfFace(1, 1) == "f1" && mirror.ownerMap().count("f3") == 0);
        assert(notifications == 1);
    }
    // The rollback marker itself is authoritative, even when diagnostics are missing.
    const auto markerOnly = mirror.ingestOpsResponse({{"revision", 8}, {"tree", preview},
                                                      {"rejected_tree", badAdd}});
    assert(!markerOnly.ok && markerOnly.rejected && !markerOnly.error_message.empty());
    assert(mirror.revision() == 8 && mirror.tree() == preview && notifications == 1);
    const auto validation = submit({{"op", "edit_feature"}, {"id", "f1"}});
    assert(validation.second && !validation.first.ok && validation.first.rejected);
    assert(validation.first.unchanged && validation.first.revision == 8);
    assert(validation.first.error_code == "invalid_op");
    assert(validation.first.error_message.find("Invalid expression") != std::string::npos);
    assert(validation.first.error_detail.at("all_errors").size() == 2);
    assert(mirror.revision() == 8 && mirror.tree() == preview && mirror.featureStatus() == liveStatus);

    // A known unchanged refusal preserves the caller's ability to revert its preview.
    json reverted;
    assert(w.call("doc_revert", {{"doc_id", "A"}, {"checkpoint", "ck1"}, {"expect_revision", 8}},
                  reverted, err));
    assert(mirror.ingestOpsResponse(reverted).ok);
    assert(mirror.revision() == 9 && mirror.tree() == initial);
    assert(mirror.featureStatus() == liveStatus && mirror.refDatums() == datums);

    const auto wrongRevision = submit({{"op", "add_feature"}});
    assert(wrongRevision.second && wrongRevision.first.rejected && !wrongRevision.first.unchanged);
    assert(mirror.revision() == 9 && mirror.tree() == initial);
    const auto conflicting = submit({{"op", "edit_feature"}, {"id", "f1"}});
    assert(conflicting.second && conflicting.first.rejected && !conflicting.first.unchanged);
    const auto unknown = submit({{"op", "edit_feature"}, {"id", "f1"}});
    assert(unknown.second && !unknown.first.ok && !unknown.first.unchanged);
    const auto transport = submit({{"op", "edit_feature"}, {"id", "f1"}});
    assert(transport.second && !transport.first.ok && !transport.first.rejected &&
           !transport.first.unchanged && transport.first.error_code == "transport");
    const auto syncRefusal = mirror.applyOps(w, json::array({{{"op", "edit_feature"}, {"id", "f1"}}}));
    assert(!syncRefusal.ok && syncRefusal.rejected && syncRefusal.unchanged &&
           syncRefusal.revision == 9);
    assert(mirror.revision() == 9 && mirror.tree() == initial);
    // A checkpoint can contain a historically failed tree; the worker has already
    // restored it, so its actual live status and revision must still be installed.
    const auto historical = mirror.ingestOpsResponse({
        {"revision", 10}, {"tree", initial}, {"datums", datums},
        {"owner_map", owners}, {"rebuild", {{"feature_status", editStatus}}}});
    assert(historical.ok && !historical.rejected && mirror.revision() == 10);
    assert(mirror.tree() == initial && mirror.featureStatus() == editStatus);
    const auto notCommitted = mirror.ingestOpsResponse(rejectedReply(badAdd, addStatus));
    assert(!notCommitted.ok && notCommitted.rejected);
    assert(mirror.revision() == 10 && mirror.tree() == initial && mirror.featureStatus() == editStatus);
    rpc.shutdown();
}

void test_committed_failed_history_and_assembly_are_not_rejected() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json part = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "f1"}, {"type", "box"}}, {{"id", "f2"}, {"type", "fillet"}}
    })}};
    const json failedPart = json::array({
        {{"id", "f1"}, {"ok", true}}, {{"id", "f2"}, {"ok", false}, {"error", "Bad edge"}}
    });
    const json assembly = {{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "k1"}, {"name", "Missing source"}}
    })}};
    json changedAssembly = assembly;
    changedAssembly["components"].push_back({{"id", "k2"}, {"name", "Inserted"}});
    const json assemblyStatus = json::array({
        {{"id", "k1"}, {"ok", false}, {"error", "Source unavailable"}},
        {{"id", "k2"}, {"ok", true}}
    });
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 4}, {"tree", part},
                                    {"rebuild", {{"feature_status", failedPart}}}}, err));
    std::vector<bool> changed;
    mirror.setAcceptedOpsObserver([&](const core::OpsResponse& r, bool revisionChanged) {
        assert(mirror.revision() == r.revision && mirror.tree() == r.tree);
        changed.push_back(revisionChanged);
    });
    g_fakeCall = [&](const std::string& method, const json& params, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        assert(method == "doc_apply_ops");
        if (params.value("doc_id", std::string()) == "A") {
            result = {{"revision", 4}, {"tree", part},
                      {"id_map", {{"$bundle", {{"kind", "aicad.feature_bundle/1"}}}}},
                      {"rebuild", {{"feature_status", failedPart}}}};
        } else {
            result = {{"revision", 2}, {"tree", changedAssembly}, {"id_map", {{"$0", "k2"}}},
                      {"rebuild", {{"feature_status", assemblyStatus}}}};
        }
        return true;
    };
    bool done = false, current = false;
    core::OpsResponse response;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "copy_feature"}, {"id", "f1"}}}),
        [&](const core::OpsResponse& r, bool c) { response = r; current = c; done = true; });
    drainUntil(rpc, done);
    assert(current && response.ok && !response.rejected &&
           response.id_map.at("$bundle").at("kind") == "aicad.feature_bundle/1");
    assert(mirror.revision() == 4 && mirror.tree() == part && mirror.featureStatus() == failedPart);
    assert(changed.empty());  // read-only copy does not report a human mutation

    assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 1}, {"tree", assembly}}, err));
    done = false; current = false;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "asm_insert_component"}}}),
        [&](const core::OpsResponse& r, bool c) { response = r; current = c; done = true; });
    drainUntil(rpc, done);
    assert(current && response.ok && !response.rejected && response.id_map.at("$0") == "k2");
    assert(mirror.revision() == 2 && mirror.tree() == changedAssembly &&
           mirror.featureStatus() == assemblyStatus);
    assert(changed.size() == 1 && changed.back());
    // A null marker is not a rejected candidate; status remains the live assembly's.
    const auto nullMarker = mirror.ingestOpsResponse({{"revision", 2}, {"tree", changedAssembly},
        {"rejected_tree", nullptr}, {"rebuild", {{"feature_status", assemblyStatus}}}});
    assert(nullMarker.ok && !nullMarker.rejected && mirror.featureStatus() == assemblyStatus);
    assert(changed.size() == 2 && !changed.back());
    rpc.shutdown();
}

void test_mirror_superseded_rejection_and_foreign_revision() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "f1"}, {"type", "box"}, {"params", {{"length", 10}}}}
    })}};
    json nextTree = tree;
    nextTree["features"][0]["params"]["length"] = 20;
    json foreignTree = nextTree;
    foreignTree["features"][0]["params"]["length"] = 30;
    json laterTree = foreignTree;
    laterTree["features"][0]["params"]["length"] = 40;
    const json failed = json::array({{{"id", "f2"}, {"ok", false}, {"error", "Invalid edge"}}});
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree}}, err));
    std::vector<std::pair<std::string, int>> catchups;
    mirror.setSupersededOpsObserver([&](const std::string& doc, int base) {
        catchups.emplace_back(doc, base);
    });
    std::atomic<bool> entered{false}, release{false};
    std::atomic<int> call{0};
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        assert(method == "doc_apply_ops");
        const int n = call.fetch_add(1);
        if (n == 0 || n == 2 || n == 3) {
            entered = true;
            while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        if (n == 0) {
            result = {{"revision", 1}, {"tree", tree}, {"rejected_tree", nextTree},
                      {"id_map", json::object()}, {"rebuild", {{"feature_status", failed}}}};
        } else if (n == 1) {
            result = {{"revision", 2}, {"tree", nextTree}};
        } else if (n == 2) {
            result = {{"code", "invalid_op"}, {"message", "Invalid expression"},
                      {"detail", {{"reason", "invalid expression"}}}};
        } else {
            result = {{"revision", 4}, {"tree", nextTree}};
        }
        return true;
    };
    bool oldDone = false, newDone = false;
    core::OpsResponse oldResponse;
    bool oldCurrent = true, newCurrent = false;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "add_feature"}}}),
        [&](const core::OpsResponse& r, bool c) { oldResponse = r; oldCurrent = c; oldDone = true; });
    const auto firstDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < firstDeadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse&, bool c) { newCurrent = c; newDone = true; });
    release = true;
    drainUntil(rpc, oldDone);
    drainUntil(rpc, newDone);
    assert(!oldCurrent && !oldResponse.ok && oldResponse.rejected && !oldResponse.unchanged);
    assert(oldResponse.error_message.find("Invalid edge") != std::string::npos);
    assert(newCurrent && mirror.revision() == 2 && mirror.tree() == nextTree);
    assert(catchups.empty());  // the superseded candidate was atomically rolled back

    entered = false;
    release = false;
    bool delayedDone = false, delayedCurrent = true;
    core::OpsResponse delayed;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse& r, bool c) { delayed = r; delayedCurrent = c; delayedDone = true; });
    const auto secondDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < secondDeadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(mirror.ingestOpsResponse({{"revision", 3}, {"tree", foreignTree}}).ok);
    release = true;
    drainUntil(rpc, delayedDone);
    assert(!delayedCurrent && delayed.rejected && !delayed.unchanged);
    assert(mirror.revision() == 3 && mirror.tree() == foreignTree);
    assert(catchups.empty());  // a part validation refusal made no mutation
    entered = false;
    release = false;
    bool successDone = false, successCurrent = true;
    core::OpsResponse success;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse& r, bool c) { success = r; successCurrent = c; successDone = true; });
    const auto thirdDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load()) {
        assert(std::chrono::steady_clock::now() < thirdDeadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(mirror.ingestOpsResponse({{"revision", 5}, {"tree", laterTree}}).ok);
    release = true;
    drainUntil(rpc, successDone);
    assert(success.ok && !successCurrent);
    assert(mirror.revision() == 5 && mirror.tree() == laterTree);
    assert(catchups.size() == 1 && catchups.back() == std::make_pair(std::string("A"), 3));
    rpc.shutdown();
}

void test_superseded_origin_hints_only_for_uncertain_changes() {
    const json part = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    const json assembly = {{"schema", "aicad.asm/1"}, {"components", json::array()}};
    for (int scenario = 0; scenario < 4; ++scenario) {
        WorkerClient w;
        core::AsyncRpc rpc(w);
        core::DocMirror mirror;
        const json& source = scenario == 3 ? assembly : part;
        std::string err;
        assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 7}, {"tree", source}}, err));
        std::vector<std::pair<std::string, int>> catchups;
        mirror.setSupersededOpsObserver([&](const std::string& doc, int base) {
            catchups.emplace_back(doc, base);
        });
        std::vector<std::pair<std::string, bool>> pending;
        std::vector<std::pair<std::string, int>> human;
        mirror.setPendingOpsObserver([&](const std::string& doc, bool begun) {
            pending.emplace_back(doc, begun);
        });
        mirror.setHumanMutationObserver([&](const std::string& doc, int revision) {
            human.emplace_back(doc, revision);
        });
        std::atomic<bool> entered{false}, release{false};
        g_fakeCall = [&](const std::string& method, const json&, json& result, std::string& error,
                         const WorkerClient::EventFn&) {
            assert(method == "doc_apply_ops");
            entered = true;
            while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            if (scenario == 0) {
                result = {{"revision", 7}, {"tree", source}, {"id_map", {{"$bundle", json::object()}}}};
            } else if (scenario == 1) {
                error = "Connection lost";
                return false;
            } else if (scenario == 2) {
                result = {{"revision", 8}, {"tree", source}, {"rejected_tree", source}};
            } else {
                result = {{"code", "invalid_op"}, {"message", "Assembly refresh failed"},
                          {"detail", json::object()}};
            }
            return true;
        };
        bool done = false, current = true;
        const json op = scenario == 0 ? json{{"op", "copy_feature"}}
                                      : json{{"op", "edit_feature"}};
        mirror.applyOpsAsync(rpc, w, json::array({op}),
            [&](const core::OpsResponse&, bool c) { current = c; done = true; });
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!entered.load()) {
            assert(std::chrono::steady_clock::now() < deadline);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 9}, {"tree", part}}, err));
        release = true;
        drainUntil(rpc, done);
        assert(!current && mirror.docId() == "B" && mirror.revision() == 9 && mirror.tree() == part);
        if (scenario == 0) assert(catchups.empty());
        else assert(catchups.size() == 1 && catchups[0] == std::make_pair(std::string("A"), 7));
        if (scenario == 0) assert(pending.empty());
        else assert(pending == (std::vector<std::pair<std::string, bool>>{{"A", true}, {"A", false}}));
        assert(human.empty());
        rpc.shutdown();
    }
}

void test_matching_tree_only_resync_recovers_accepted_response() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json initial = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "f1"}, {"params", {{"length", 10}}}}
    })}};
    json tree2 = initial, tree3 = initial, tree4 = initial, foreign4 = initial, tree5 = initial;
    tree2["features"][0]["params"]["length"] = 20;
    tree3["features"][0]["params"]["length"] = 30;
    tree4["features"][0]["params"]["length"] = 40;
    foreign4["features"][0]["params"]["length"] = 41;
    tree5["features"][0]["params"]["length"] = 50;
    const json statuses = json::array({{{"id", "f1"}, {"ok", true}}});
    const json datums = json::array({{{"id", "datum-2"}}});
    const std::vector<json> replies = {
        {{"revision", 2}, {"tree", tree2}, {"datums", datums},
         {"tess", {{"aicm_path", "tree2.aicm"}, {"bodies", json::array({{{"kind", "solid"}}})}}},
         {"rebuild", {{"feature_status", statuses}}}},
        {{"revision", 3}, {"tree", tree3}},
        {{"revision", 4}, {"tree", tree4}},
        {{"revision", 5}}
    };
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", initial}}, err));
    std::vector<std::string> events;
    std::vector<bool> changed;
    std::vector<std::pair<std::string, int>> catchups;
    mirror.setListener([&](bool) { events.push_back("listener"); });
    mirror.setAcceptedOpsObserver([&](const core::OpsResponse& r, bool revisionChanged) {
        assert(mirror.revision() == r.revision && mirror.tree() == r.tree);
        changed.push_back(revisionChanged);
        events.push_back("accepted");
    });
    mirror.setSupersededOpsObserver([&](const std::string& doc, int base) {
        catchups.emplace_back(doc, base);
    });
    std::atomic<bool> entered{false}, release{false};
    std::atomic<size_t> next{0};
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        assert(method == "doc_apply_ops");
        const auto index = next.fetch_add(1);
        assert(index < replies.size());
        entered = true;
        while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        result = replies[index];
        return true;
    };
    auto waitEntered = [&] {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!entered.load()) {
            assert(std::chrono::steady_clock::now() < deadline);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    };
    bool done = false, current = false;
    core::OpsResponse response;
    auto submit = [&] {
        done = false; current = false; entered = false; release = false;
        mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
            [&](const core::OpsResponse& r, bool c) { response = r; current = c; done = true; });
        waitEntered();
    };
    submit();
    assert(mirror.ingestTreeResponse({{"revision", 2}, {"tree", tree2}}));
    assert(!mirror.featureStatusCurrent());
    release = true;
    drainUntil(rpc, done);
    assert(current && response.ok && response.has_tess && response.aicm_path == "tree2.aicm");
    assert(mirror.revision() == 2 && mirror.tree() == tree2 && mirror.featureStatus() == statuses);
    assert(mirror.featureStatusCurrent() && mirror.refDatums() == datums);
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody());
    assert(changed.size() == 1 && changed[0] && catchups.empty());
    assert(events.size() == 3 && events[0] == "listener" &&
           events[1] == "accepted" && events[2] == "listener");

    // Even an equal same-revision tree cannot revive a response after the exact
    // tree-only payload was replaced by another accepted ingestion.
    submit();
    assert(mirror.ingestTreeResponse({{"revision", 3}, {"tree", tree3}}));
    assert(mirror.ingestOpsResponse({{"revision", 3}, {"tree", tree3}}).ok);
    assert(changed.size() == 2 && !changed.back());
    release = true;
    drainUntil(rpc, done);
    assert(response.ok && !current && mirror.revision() == 3 && mirror.tree() == tree3);
    assert(catchups.size() == 1 && catchups.back() == std::make_pair(std::string("A"), 2));

    submit();
    assert(mirror.ingestTreeResponse({{"revision", 4}, {"tree", foreign4}}));
    release = true;
    drainUntil(rpc, done);
    assert(response.ok && !current && mirror.revision() == 4 && mirror.tree() == foreign4);
    assert(catchups.size() == 2 && catchups.back() == std::make_pair(std::string("A"), 3));

    // Without the returned candidate tree there is no proof of equality.
    submit();
    assert(mirror.ingestTreeResponse({{"revision", 5}, {"tree", tree5}}));
    release = true;
    drainUntil(rpc, done);
    assert(response.ok && !current && mirror.revision() == 5 && mirror.tree() == tree5);
    assert(catchups.size() == 3 && catchups.back() == std::make_pair(std::string("A"), 4));
    assert(changed.size() == 2);
    rpc.shutdown();
}

void test_accepted_observer_receives_only_usable_tess() {
    core::DocMirror mirror;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree}}, err));
    std::vector<bool> usability;
    std::vector<bool> changed;
    mirror.setAcceptedOpsObserver([&](const core::OpsResponse& r, bool revisionChanged) {
        assert(mirror.revision() == r.revision && mirror.tree() == tree);
        usability.push_back(r.has_tess);
        changed.push_back(revisionChanged);
    });
    const std::vector<json> tess = {
        {{"aicm_path", "model.aicm"}},
        {{"aicm_path", ""}, {"bodies", json::array()}},
        {{"aicm_path", ""}, {"bodies", json::array({{{"kind", "solid"}}})}},
        {{"aicm_path", ""}},
        {{"bodies", json::array()}},
        nullptr
    };
    for (size_t i = 0; i < tess.size(); ++i) {
        json result = {{"revision", static_cast<int>(i + 2)}, {"tree", tree}};
        if (!tess[i].is_null()) result["tess"] = tess[i];
        const auto r = mirror.ingestOpsResponse(result);
        assert(r.ok && r.has_tess == (i < 2));
    }
    assert(usability == std::vector<bool>({true, true, false, false, false, false}));
    assert(changed == std::vector<bool>({true, true, true, true, true, true}));
    const auto push = mirror.ingestPushPullResponse({
        {"ok", true}, {"draggable", true}, {"revision", 8}, {"tree", tree},
        {"tess", {{"aicm_path", "drag.aicm"}}}});
    assert(push.ok && push.draggable && push.ops.ok && push.ops.has_tess);
    assert(mirror.revision() == 8 && usability.size() == tess.size() + 1 &&
           usability.back() && changed.back());
    const auto blocked = mirror.ingestPushPullResponse({{"ok", true}, {"draggable", false}});
    assert(blocked.ok && !blocked.draggable && usability.size() == tess.size() + 1);
    const auto rejected = mirror.ingestOpsResponse({{"revision", 8}, {"tree", tree},
                                                    {"rejected_tree", tree}});
    assert(!rejected.ok && usability.size() == tess.size() + 1);
}

void test_mirror_stale_revision_resync_is_not_an_unchanged_refusal() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    core::DocMirror mirror;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "f1"}, {"params", {{"length", 10}}}}
    })}};
    json foreign = tree;
    foreign["features"][0]["params"]["length"] = 15;
    const json foreignStatus = json::array({{{"id", "f1"}, {"ok", true}}});
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 4}, {"tree", tree}}, err));
    int resyncs = 0;
    mirror.setStaleResyncCallback([&] { ++resyncs; });
    g_fakeCall = [&](const std::string& method, const json&, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        if (method == "doc_apply_ops") {
            result = {{"code", "stale_revision"}, {"message", "Revision changed"},
                      {"detail", {{"current_revision", 5}}}};
        } else {
            assert(method == "doc_tree");
            result = {{"revision", 5}, {"tree", foreign},
                      {"rebuild", {{"feature_status", foreignStatus}}}};
        }
        return true;
    };
    bool done = false, current = false;
    core::OpsResponse response;
    mirror.applyOpsAsync(rpc, w, json::array({{{"op", "edit_feature"}}}),
        [&](const core::OpsResponse& r, bool c) { response = r; current = c; done = true; });
    drainUntil(rpc, done);
    assert(current && !response.ok && !response.rejected && !response.unchanged);
    assert(response.error_code == "stale_revision" &&
           response.error_detail.at("current_revision") == 5);
    assert(resyncs == 1 && mirror.revision() == 5 && mirror.tree() == foreign);
    assert(mirror.featureStatus() == foreignStatus);
    rpc.shutdown();
}

void test_mirror_build_facts_follow_revisions_and_snapshots() {
    core::DocMirror mirror;
    const json part = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    const json assembly = {{"schema", "aicad.asm/1"}, {"components", json::array()}};
    const json solid = json::array({{{"kind", "solid"}}});
    const json surface = json::array({{{"kind", "surface"}}});
    const json mixed = json::array({{{"kind", "surface"}}, {{"kind", "solid"}}});
    const json unknown = json::array({{{"body_id", 1}}});
    const json statuses = json::array({{{"id", "f1"}, {"ok", true}}});
    std::string err;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", part},
        {"tess", {{"bodies", solid}}}, {"rebuild", {{"feature_status", statuses}}}}, err));
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody() && mirror.featureStatusCurrent());
    const auto solidSnapshot = mirror.snapshot();

    // A tree-only refresh at the same revision does not discard cached build evidence.
    assert(mirror.ingestTreeResponse({{"revision", 1}, {"tree", part}}));
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody() && mirror.featureStatusCurrent());
    const json rejected = {{"revision", 1}, {"tree", part}, {"rejected_tree", part},
                           {"tess", {{"bodies", surface}}},
                           {"rebuild", {{"feature_status", json::array({
                               {{"id", "f2"}, {"ok", false}, {"error", "Failed"}}})}}}};
    assert(!mirror.ingestOpsResponse(rejected).ok);
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody() && mirror.featureStatusCurrent());
    assert(mirror.featureStatus() == statuses);
    // A new rebuild with no body evidence is not merely a tree-only refresh.
    assert(mirror.ingestOpsResponse({{"revision", 1}, {"tree", part},
                                    {"rebuild", {{"feature_status", statuses}}}}).ok);
    assert(!mirror.solidPresenceKnown() && mirror.featureStatusCurrent());
    assert(mirror.restoreSnapshot(solidSnapshot));

    assert(mirror.ingestOpsResponse({{"revision", 2}, {"tree", part},
        {"tess", {{"bodies", surface}}}, {"rebuild", {{"feature_status", statuses}}}}).ok);
    assert(mirror.solidPresenceKnown() && !mirror.hasSolidBody() && mirror.featureStatusCurrent());
    const auto surfaceSnapshot = mirror.snapshot();
    // A newer tree without a rebuild must not reuse prior geometry or status as current.
    assert(mirror.ingestTreeResponse({{"revision", 3}, {"tree", part}}));
    assert(!mirror.solidPresenceKnown() && !mirror.hasSolidBody() && !mirror.featureStatusCurrent());
    assert(mirror.featureStatus() == statuses);
    assert(mirror.restoreSnapshot(solidSnapshot));
    assert(mirror.revision() == 1 && mirror.solidPresenceKnown() && mirror.hasSolidBody());
    assert(mirror.featureStatusCurrent());
    assert(mirror.restoreSnapshot(surfaceSnapshot));
    assert(mirror.revision() == 2 && mirror.solidPresenceKnown() && !mirror.hasSolidBody());
    assert(mirror.featureStatusCurrent());

    // Assembly body IDs alone do not distinguish solids from surfaces.
    assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 2}, {"tree", assembly},
                                    {"tess", {{"bodies", unknown}}}}, err));
    assert(!mirror.solidPresenceKnown() && !mirror.hasSolidBody() && !mirror.featureStatusCurrent());
    assert(mirror.adoptOpenResponse({{"doc_id", "C"}, {"revision", 1}, {"tree", part},
                                    {"tess", {{"bodies", json::array()}}}}, err));
    assert(mirror.solidPresenceKnown() && !mirror.hasSolidBody());

    // Positive part volume proves a solid; zero volume without body kinds does not
    // prove absence. Conflicting body metadata and volume also stay unknown.
    assert(mirror.adoptOpenResponse({{"doc_id", "D"}, {"revision", 1}, {"tree", part},
                                    {"volume", 17.0}}, err));
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody());
    assert(mirror.ingestOpsResponse({{"revision", 2}, {"tree", part}, {"volume", 0.0}}).ok);
    assert(!mirror.solidPresenceKnown());
    assert(mirror.ingestOpsResponse({{"revision", 3}, {"tree", part}, {"volume", 0.0},
                                    {"tess", {{"bodies", unknown}}}}).ok);
    assert(!mirror.solidPresenceKnown());
    assert(mirror.ingestOpsResponse({{"revision", 4}, {"tree", part}, {"volume", 3.0},
                                    {"tess", {{"bodies", unknown}}}}).ok);
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody());
    assert(mirror.ingestOpsResponse({{"revision", 5}, {"tree", part}, {"volume", 3.0},
                                    {"tess", {{"bodies", surface}}}}).ok);
    assert(!mirror.solidPresenceKnown());
    assert(mirror.ingestOpsResponse({{"revision", 6}, {"tree", part},
                                    {"tess", {{"bodies", mixed}}}}).ok);
    assert(mirror.solidPresenceKnown() && mirror.hasSolidBody());
    mirror.reset();
    assert(!mirror.solidPresenceKnown() && !mirror.hasSolidBody() && !mirror.featureStatusCurrent());
    WorkerClient w;
    g_fakeCall = [&](const std::string& method, const json& params, json& result, std::string&,
                     const WorkerClient::EventFn&) {
        if (method == "doc_apply_ops") {
            result = {{"code", "invalid_op"}, {"message", "Invalid expression"},
                      {"detail", {{"reason", "invalid expression"}}}};
            return true;
        }
        assert(method == "doc_new");
        if (params.value("kind", std::string()) == "part")
            result = {{"doc_id", "new-part"}, {"revision", 0}, {"tree", part},
                      {"tess", {{"bodies", json::array()}}}};
        else
            result = {{"doc_id", "new-assembly"}, {"revision", 0}, {"tree", assembly}};
        return true;
    };
    assert(mirror.newPart(w, err));
    assert(mirror.tree() == part);
    const auto refused = mirror.applyOps(w, json::array({{{"op", "add_feature"}}}));
    assert(!refused.ok && refused.rejected && refused.unchanged && refused.revision == 0);
    assert(mirror.solidPresenceKnown() && !mirror.hasSolidBody() && !mirror.featureStatusCurrent());
    assert(mirror.newAssembly(w, err));
    assert(!mirror.solidPresenceKnown() && !mirror.hasSolidBody() && !mirror.featureStatusCurrent());
}

void test_rejected_open_does_not_replace_the_existing_mirror() {
    core::DocMirror mirror;
    std::string err;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    const json datums = json::array({{{"id", "datum-B"}}});
    assert(mirror.adoptOpenResponse({{"doc_id", "B"}, {"revision", 9},
                                     {"tree", tree}, {"datums", datums}}, err));
    const auto generation = mirror.mutationGen();
    const json rejected = {{"doc_id", "bad"}, {"revision", 0}, {"tree", json::object()},
        {"datums", json::array()}, {"rejected_tree", tree},
        {"rebuild", {{"feature_status", json::array({{{"id", "scad"}, {"ok", false},
                                                        {"error", "SCAD syntax error"}}})}}}};
    assert(!mirror.adoptOpenResponse(rejected, err));
    assert(err.find("SCAD syntax error") != std::string::npos);
    assert(mirror.docId() == "B" && mirror.revision() == 9 && mirror.tree() == tree);
    assert(mirror.refDatums() == datums && mirror.mutationGen() == generation);
    err.clear();
    assert(!mirror.adoptOpenResponse({{"code", "import_failed"}, {"message", "renderer unavailable"}}, err));
    assert(err == "renderer unavailable" && mirror.docId() == "B" && mirror.refDatums() == datums);
}

void test_sync_human_observers_and_open_mesh_admission() {
    WorkerClient w;
    core::DocMirror mirror;
    const json tree = {{"schema", "aicad.part/1"}, {"features", json::array()}};
    std::string error;
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree},
        {"tess", {{"aicm_path", ""}, {"bodies", json::array()}}}}, error));
    assert(mirror.lastOpenedMeshUsable());
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree},
        {"tess", {{"aicm_path", ""}, {"bodies", json::array({{{"kind", "solid"}}})}}}}, error));
    assert(!mirror.lastOpenedMeshUsable());
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree},
        {"tess", {{"mesh_error", "aicm_publication_failed"}}}}, error));
    assert(!mirror.lastOpenedMeshUsable());
    assert(mirror.adoptOpenResponse({{"doc_id", "A"}, {"revision", 1}, {"tree", tree},
        {"tess", {{"aicm_path", "live.aicm"}}}}, error));
    assert(mirror.lastOpenedMeshUsable() && mirror.lastOpenedAicm() == "live.aicm");
    int revision = 1;
    g_fakeCall = [&](const std::string& method, const json& params, json& result,
                     std::string&, const WorkerClient::EventFn&) {
        if (method == "doc_apply_ops") {
            if (params["ops"][0].value("op", std::string()) != "copy_feature") ++revision;
        } else {
            assert(method == "doc_undo" || method == "doc_redo");
            ++revision;
        }
        result = {{"revision", revision}, {"tree", tree}};
        return true;
    };
    std::vector<bool> changed;
    mirror.setAcceptedOpsObserver([&](const core::OpsResponse&, bool advance) {
        changed.push_back(advance);
    });
    assert(mirror.applyOps(w, json::array({{{"op", "copy_feature"}}})).ok);
    assert(changed.empty());
    assert(mirror.applyOps(w, json::array({{{"op", "edit_feature"}}})).ok);
    assert(mirror.undo(w).ok && mirror.redo(w).ok);
    assert(changed == std::vector<bool>({true, true, true}));
    mirror.reset();
    assert(!mirror.lastOpenedMeshUsable() && mirror.lastOpenedAicm().empty());
}

// workerMethodAbsent is latched for the whole session by its callers (usage rings, provider
// polls), so it must name an absent RPC and nothing else: a missing hostd binary while a rebuild
// replaces it, a timeout, or a run holding the agent lane are outages/waits, never "no such method".
void test_worker_method_absent_is_strict_and_separate_from_lane_busy() {
    core::RpcResult r;
    r.ok = false;
    r.err = "hostd binary not found (set CHUDWARE_HOSTD_BIN); connect failed: No such file";
    assert(!core::workerMethodAbsent(r));
    r.err = "engine call timed out after 15000ms";
    assert(!core::workerMethodAbsent(r));
    r.err = "daemon response has an invalid request id for method omp_provider_list";
    assert(!core::workerMethodAbsent(r));
    r.err = "unknown method: omp_provider_list";
    assert(core::workerMethodAbsent(r));

    r.ok = true;
    r.err.clear();
    // hostd's answer for a worker that lacks the method.
    r.result = {{"code", "worker_error"}, {"message", "unknown method: omp_provider_list"}};
    assert(core::workerMethodAbsent(r));
    r.result = {{"code", "worker_error"}, {"message", "KeyError: 'method'"}};
    assert(!core::workerMethodAbsent(r));
    r.result = {{"code", "method_not_found"}};
    assert(core::workerMethodAbsent(r));
    r.result = {{"ok", false}, {"reason", "unknown_method"}};
    assert(core::workerMethodAbsent(r));
    r.result = {{"ok", false}, {"reason", "session_not_live"}};
    assert(!core::workerMethodAbsent(r));

    // A run holding the lane is a wait; the two predicates never overlap.
    r.result = {{"code", "worker_lane_busy"}, {"message", "worker lane busy (agent turn in progress)"}};
    assert(core::workerLaneBusy(r) && !core::workerMethodAbsent(r));
    r.result = {{"ok", true}, {"providers", json::array()}};
    assert(!core::workerLaneBusy(r) && !core::workerMethodAbsent(r));
}

}  // namespace

int main() {
    test_no_event_call_passes_empty_eventfn_and_completes();
    test_mesh_reference_reads_do_not_block_interaction();
    test_background_queries_do_not_delay_document_calls();
    test_events_deliver_on_drain_thread_in_order_before_done();
    test_shutdown_drops_undelivered_events();
    test_mirror_async_completion_does_not_modify_another_document();
    test_mirror_rejections_preserve_committed_preview_and_diagnostics();
    test_committed_failed_history_and_assembly_are_not_rejected();
    test_mirror_superseded_rejection_and_foreign_revision();
    test_superseded_origin_hints_only_for_uncertain_changes();
    test_matching_tree_only_resync_recovers_accepted_response();
    test_accepted_observer_receives_only_usable_tess();
    test_mirror_stale_revision_resync_is_not_an_unchanged_refusal();
    test_mirror_build_facts_follow_revisions_and_snapshots();
    test_rejected_open_does_not_replace_the_existing_mirror();
    test_sync_human_observers_and_open_mesh_admission();
    test_worker_method_absent_is_strict_and_separate_from_lane_busy();
    std::printf("test_async_rpc: OK\n");
    return 0;
}
