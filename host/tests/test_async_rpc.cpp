// test_async_rpc.cpp — core::AsyncRpc's streamed-event channel (chudcode_login open_url).
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
#include <cassert>
#include <chrono>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "core/async_rpc.h"
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

void test_events_deliver_on_drain_thread_in_order_before_done() {
    WorkerClient w;
    core::AsyncRpc rpc(w);
    const std::thread::id mainThread = std::this_thread::get_id();

    std::thread::id workerThread;
    bool callFinished = false;
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
        "chudcode_login", json{{"providerId", "anthropic"}},
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
        bool callFinished = false;
        g_fakeCall = [&](const std::string&, const json&, json& result, std::string&,
                         const WorkerClient::EventFn& ev) {
            ev(json{{"kind", "open_url"}, {"url", "https://never-delivered"}});
            result = {{"ok", true}};
            callFinished = true;
            return true;
        };
        rpc.call(
            "chudcode_login", json::object(),
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

}  // namespace

int main() {
    test_no_event_call_passes_empty_eventfn_and_completes();
    test_events_deliver_on_drain_thread_in_order_before_done();
    test_shutdown_drops_undelivered_events();
    std::printf("test_async_rpc: OK\n");
    return 0;
}
