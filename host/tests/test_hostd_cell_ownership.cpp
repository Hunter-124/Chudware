#include "hostd/doc_registry.h"
#include "sim/sim_cancel.h"
#include "api/libchudware.h"

#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>

using nlohmann::json;
using namespace chudware::sim;

static void require(bool value, const char* message) {
    if (!value) { std::cerr << "FAILED: " << message << '\n'; std::exit(1); }
}

static json call(const char* method, const json& params, int expectedRc = 0) {
    char* output = nullptr;
    const int rc = chudware_call(method, params.dump().c_str(), &output);
    require(output != nullptr, "native call returns JSON");
    json result = json::parse(output);
    chudware_free(output);
    if (rc != expectedRc) std::cerr << method << ": " << result << '\n';
    require(rc == expectedRc, "native return code");
    return result;
}

static json ctx(const char* actor, const char* right = "editor",
                const char* workspace = "team:one", std::int64_t epoch = 0) {
    return {{"cell", true}, {"workspace_key", workspace}, {"actor_key", actor},
            {"right", right}, {"epoch", epoch}};
}

static void registryOwnership() {
    hostd::DocRegistry registry;
    std::vector<std::pair<int, json>> events;
    registry.setEventSender([&](int id, const std::string& line) {
        events.emplace_back(id, json::parse(line));
    });
    registry.setCellPrincipal(1, "writer", false);
    registry.setCellPrincipal(2, "viewer", true);
    registry.setCellPrincipal(3, "unattached", false);
    registry.setWriterGroup(1, "team:one/writer");
    json result, error;
    bool isError = false;
    for (int id : {2, 3, 4})
        registry.handleMethod(id, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json minted = {{"doc_id", "part"}, {"revision", 0}, {"tree", json::object()}};
    registry.postCall(1, "gui", "spoofed", "Spoofed", "doc_new", json::object(), minted);
    require(events.size() == 1 && events[0].first == 4, "cell wildcard cannot reveal unattached mint; desktop mirrors unchanged");
    registry.handleMethod(2, "gui", "attach_doc", {{"doc_id", "part"}, {"mode", "rw"}}, result, isError);
    require(result["mode"] == "ro", "viewer explicit upgrade pinned ro");
    json params = {{"doc_id", "part"}};
    std::string warning;
    require(!registry.preCall(2, "gui", "", "", "doc_apply_ops", params, error, warning), "viewer cannot write attached doc");
    require(error["code"] == "forbidden_right", "viewer write refusal");
    require(!hostd::DocRegistry::isReadOnlyMethod("agent_run"), "agent prefix is not read authorization");
    require(!hostd::DocRegistry::isReadOnlyMethod("sim_study_add"), "sim prefix is not read authorization");
    require(hostd::DocRegistry::isDesktopReadOnlyMethod("agent_run"), "desktop agent classification preserved");
    require(hostd::DocRegistry::isDesktopReadOnlyMethod("sim_study_add"), "desktop study classification preserved");
    require(registry.preCall(2, "gui", "", "", "worker_read_probe", params, error, warning, true), "trusted exact cell policy admits document worker reads");
    require(!registry.preCall(2, "gui", "", "", "worker_read_probe", params, error, warning), "unlisted document worker method fails closed");
    json write = {{"revision", 1}, {"tree", json::object()}};
    events.clear();
    registry.postCall(1, "gui", "spoofed", "Spoofed", "doc_apply_ops", params, write);
    require(events.size() == 2, "doc change reaches attached viewer and desktop only");
    for (const auto& [id, event] : events) {
        require(id != 3, "unattached subscription receives nothing");
        require(event["data"]["actor_key"] == "writer", "doc event attributed to trusted actor");
    }
    require(registry.lastPushFor("part")["actor_key"] == "writer", "snapshot actor attribution");
    json listed = {{"docs", json::array({json{{"doc_id", "part"}}})}};
    registry.annotateDocList(listed, 2);
    require(listed["docs"][0]["lastWriter"]["actor_key"] == "writer", "lastWriter actor attribution");
    events.clear();
    json refresh = {{"doc_id", "part"}, {"revision", 1}, {"tree", {{"verified", true}}},
        {"cache_trust", "verified"}, {"cache_verification", "matched"}, {"warnings", json::array()},
        {"mismatch", false}, {"warning", nullptr}};
    registry.publishCacheRefresh("part", refresh);
    require(events.size() == 1 && events[0].first == 2, "cache refresh requires attachment even for desktop subscribers");
    require(events[0].second["data"]["actor_key"] == "writer", "cache refresh preserves trusted actor");
    require(registry.lastPushFor("part")["source_client"] == 1, "cache refresh preserves actual writer");
    require(registry.lastPushFor("part")["cache_trust"] == "verified", "cache verification snapshot metadata updated");
    require(registry.lastPushFor("part")["mismatch"] == false &&
        registry.lastPushFor("part")["warning"].is_null(), "cache verification mismatch/warning metadata preserved");
    refresh["revision"] = 0;
    events.clear();
    registry.publishCacheRefresh("part", refresh);
    require(events.empty() && registry.lastPushFor("part")["revision"] == 1, "stale cache callback cannot replace live revision");
    registry.publishCacheRefresh("not-open", refresh);
    require(!registry.hasAttachments(0), "cache callback never creates synthetic attachment");
    registry.handleMethod(2, "gui", "detach_doc", params, result, isError);
    events.clear();
    write["revision"] = 2;
    registry.postCall(1, "gui", "", "", "doc_apply_ops", params, write);
    require(events.size() == 1 && events[0].first == 4, "detached viewer receives no later document event");
    json viewerMint = {{"doc_id", "viewer-mint"}, {"revision", 0}};
    registry.postCall(2, "gui", "", "", "doc_new", json::object(), viewerMint);
    params["doc_id"] = "viewer-mint";
    require(!registry.preCall(2, "gui", "", "", "doc_apply_ops", params, error, warning), "mint cannot upgrade viewer attachment");
    registry.handleMethod(2, "gui", "edit_hold", params, result, isError);
    require(isError && result["code"] == "forbidden_right", "viewer cannot acquire write hold");
    registry.onDisconnect(2, "gui");
}

static void simulationOwnership() {
    CellPrincipal a{"team:one", "a", false}, b{"team:one", "b", false};
    CellPrincipal manager{"team:one", "manager", true}, foreignManager{"team:two", "manager", true};
    SimCancelToken aToken, bToken;
    {
        CellRequestScope scope(&a);
        aToken = sim_job_register("a-job");
    }
    {
        CellRequestScope scope(&b);
        bToken = sim_job_register("b-job");
        require(sim_cancel("a-job")["code"] == "not_found", "foreign sim cancel hidden");
        require(sim_cancel("missing")["code"] == "not_found", "absent and foreign sim errors match");
        bool refused = false;
        try { sim_job_register("a-job"); } catch (const CellOwnershipError&) { refused = true; }
        require(refused && !aToken->load(), "foreign register cannot replace or cancel owner token");
        require(sim_cancel("")["count"] == 1, "empty cancel only own sim jobs");
        require(bToken->load() && !aToken->load(), "own empty cancel does not affect another actor");
        sim_job_unregister("a-job", aToken);
    }
    {
        CellRequestScope scope(&foreignManager);
        require(sim_cancel("a-job")["code"] == "not_found", "manager cannot cross workspace");
    }
    {
        CellRequestScope scope(&manager);
        require(sim_cancel("")["count"] == 0, "manager empty cancel remains own only");
        require(sim_cancel("a-job")["cancelled"] == true, "workspace manager can cancel named job");
    }
    {
        CellRequestScope scope(&a);
        auto newer = sim_job_register("a-job");
        sim_job_unregister("a-job", aToken);
        require(sim_cancel("a-job")["cancelled"] == true && newer->load(), "stale unregister cannot erase new owner generation");
        sim_job_unregister("a-job", newer);
    }
    {
        CellRequestScope scope(&b);
        sim_job_unregister("b-job", bToken);
    }
    require(current_cell_principal() == nullptr, "request context restored after scopes");
    require(sim_cancel("absent")["cancelled"] == false, "desktop absent cancel stays idempotent");
    auto desktop = sim_job_register("desktop");
    require(sim_cancel("")["cancelled"] == false && !desktop->load(), "desktop empty sim cancellation unchanged");
    require(sim_cancel("desktop")["cancelled"] == true, "desktop named cancellation unchanged");
    sim_job_unregister("desktop", desktop);
}

static void simulationRevocation() {
    CellPrincipal current{"team:revocation", "actor", false, 2};
    CellPrincipal stale{"team:revocation", "actor", false, 1};
    SimCancelToken token, anonymous;
    {
        CellRequestScope scope(&current);
        token = sim_job_register("epoch-two");
        anonymous = sim_job_register("");
        require(anonymous != nullptr, "cell anonymous simulation receives cancellation token");
        require(sim_cancel("", 1)["count"] == 0 && !token->load() && !anonymous->load(),
            "role-update cutoff preserves newer epoch jobs");
        sim_cancel("", 0);
    }
    for (const char* id : {"late-named", ""}) {
        CellRequestScope scope(&stale);
        bool revoked = false;
        try { sim_job_register(id); } catch (const CellRevokedError&) { revoked = true; }
        require(revoked && cell_request_revoked(), "late stale named/anonymous simulation refused");
    }
    {
        CellRequestScope scope(&current);
        auto fresh = sim_job_register("fresh-epoch-two");
        require(fresh != nullptr, "new epoch simulation admitted after update");
        sim_job_unregister("fresh-epoch-two", fresh);
        require(sim_cancel("", 2)["count"] == 2 && token->load() && anonymous->load(),
            "revocation stops both named and anonymous owned simulations");
        sim_job_unregister("epoch-two", token);
        sim_job_unregister("", anonymous);
        bool revoked = false;
        try { sim_job_register("late-after-full-revoke"); } catch (const CellRevokedError&) { revoked = true; }
        require(revoked, "empty live set retains revocation tombstone");
    }
    current.epoch = 3;
    {
        CellRequestScope scope(&current);
        auto next = sim_job_register("epoch-three");
        require(next != nullptr && !cell_request_revoked(), "new simulation epoch restores clean request context");
        sim_job_unregister("epoch-three", next);
    }
}

struct LiveRun {
    std::mutex mutex;
    std::condition_variable condition;
    bool ready = false;
    bool release = false;
    std::string id;
};

static void pauseEvent(const char* text, void* data) {
    auto& run = *static_cast<LiveRun*>(data);
    const json event = json::parse(text);
    std::unique_lock<std::mutex> lock(run.mutex);
    if (run.ready) return;
    run.id = event.value("run_id", std::string());
    run.ready = true;
    run.condition.notify_all();
    run.condition.wait(lock, [&] { return run.release; });
}

static void nativeOwnership() {
    LiveRun live;
    json result;
    std::thread runner([&] {
        chudware_set_event_sink(pauseEvent, &live);
        // Real native refusal emits and persists an event with a live RunControl.
        // No model/kernel operation or provider network request is required.
        result = call("agent_run_design", {{"_cell_ctx", ctx("a")}, {"provider", {{"name", "openai"}}},
            {"options", {{"offline", true}, {"run_id", "caller-chosen"}}}});
        chudware_set_event_sink(nullptr, nullptr);
    });
    {
        std::unique_lock<std::mutex> lock(live.mutex);
        require(live.condition.wait_for(lock, std::chrono::seconds(10), [&] { return live.ready; }), "native run emits before controls");
    }
    require(!live.id.empty() && live.id != "caller-chosen", "cell native run id server minted");
    LiveRun desktop;
    std::thread desktopRunner([&] {
        chudware_set_event_sink(pauseEvent, &desktop);
        call("agent_run_design", {{"provider", {{"name", "openai"}}},
            {"options", {{"offline", true}, {"run_id", "desktop-live"}}}});
        chudware_set_event_sink(nullptr, nullptr);
    });
    {
        std::unique_lock<std::mutex> lock(desktop.mutex);
        require(desktop.condition.wait_for(lock, std::chrono::seconds(10), [&] { return desktop.ready; }), "desktop native run active");
    }
    for (const char* method : {"agent_cancel", "agent_steer", "agent_run_event", "agent_run_events_batch", "agent_run_events_read"}) {
        json params = {{"_cell_ctx", ctx("b")}, {"run_id", live.id}, {"text", "steer"}, {"kind", "test"}};
        require(call(method, params, 1)["code"] == "not_found", "foreign native control/events hidden");
        params["run_id"] = "missing";
        require(call(method, params, 1)["code"] == "not_found", "absent native control/events identical");
    }
    require(call("agent_cancel", {{"_cell_ctx", ctx("b")}})["count"] == 0, "foreign empty native cancel reaches none");
    require(call("agent_steer", {{"_cell_ctx", ctx("a")}, {"run_id", live.id}, {"text", "own steer"}})["steered"] == true, "owner native steer delivered");
    require(call("agent_cancel", {{"_cell_ctx", ctx("a")}})["count"] == 1, "own empty native cancel finds one run");
    require(call("agent_cancel", {{"run_id", "desktop-live"}})["cancelled"] == true, "desktop native named control preserved");
    require(call("agent_cancel", json::object())["count"] == 2, "desktop empty native cancel still reaches every live run");
    require(call("agent_cancel", {{"_cell_ctx", ctx("manager", "manager", "team:two")}, {"run_id", live.id}}, 1)["code"] == "not_found", "manager native control workspace bound");
    require(call("agent_cancel", {{"_cell_ctx", ctx("manager", "manager")}, {"run_id", live.id}})["cancelled"] == true, "manager native named cancel allowed");
    require(call("agent_run_events_read", {{"_cell_ctx", ctx("manager", "manager")}, {"run_id", live.id}}, 1)["code"] == "not_found", "manager control right does not expose actor event history");
    require(call("agent_run_events_read", {{"_cell_ctx", ctx("a")}, {"run_id", "../escape"}}, 1)["code"] == "not_found", "events path traversal refused");
    require(call("agent_steer", {{"_cell_ctx", ctx("manager", "manager")}, {"run_id", live.id},
        {"text", "foreign manager steer"}}, 1)["code"] == "not_found", "manager cannot steer another actor");
    {
        std::lock_guard<std::mutex> lock(live.mutex);
        live.release = true;
    }
    {
        std::lock_guard<std::mutex> lock(desktop.mutex);
        desktop.release = true;
    }
    desktop.condition.notify_all();
    desktopRunner.join();
    live.condition.notify_all();
    runner.join();
    require(result["run_id"] == live.id, "native result retains minted owner handle");
    require(!call("agent_run_events_read", {{"_cell_ctx", ctx("a")}, {"run_id", live.id}}).empty(), "owner reads actual persisted events after finish");
    require(call("agent_cancel", {{"run_id", "desktop-live"}})["cancelled"] == false, "desktop finished native cancellation idempotent");
    const json oldCtx = ctx("revoke-native", "editor", "team:one", 4);
    const json newCtx = ctx("revoke-native", "editor", "team:one", 5);
    require(call("agent_cancel", {{"_cell_ctx", oldCtx}, {"_cell_revoke_through", 4}})["count"] == 0,
        "native empty cancel records tombstone without any active run");
    json late = {{"_cell_ctx", oldCtx}, {"provider", {{"name", "openai"}}},
        {"options", {{"offline", true}}}};
    require(call("agent_run_design", late, 1)["code"] == "revoked", "late native old-epoch registration refused");
    late["_cell_ctx"] = newCtx;
    require(call("agent_run_design", late)["stop_reason"] == "offline_mode_refused",
        "new-epoch native run admitted after revocation");
    call("agent_cancel", {{"_cell_ctx", newCtx}, {"_cell_revoke_through", 3}});
    late["_cell_ctx"] = oldCtx;
    require(call("agent_run_design", late, 1)["code"] == "revoked", "native tombstone never decreases");
    call("sim_cancel", {{"_cell_ctx", oldCtx}, {"_cell_revoke_through", 4}});
    require(call("sim_spice", {{"_cell_ctx", oldCtx}, {"job_id", "stale-native-sim"}}, 1)["code"] == "revoked",
        "native dispatch exposes canonical revoked simulation registration");
}

int main() {
    const auto root = std::filesystem::temp_directory_path() / ("chudware-cell-owner-test-" +
        std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
#ifdef _WIN32
    _putenv_s("APPDATA", root.string().c_str());
#else
    setenv("XDG_DATA_HOME", root.string().c_str(), 1);
#endif
    registryOwnership();
    simulationOwnership();
    simulationRevocation();
    nativeOwnership();
    std::filesystem::remove_all(root);
    std::cout << "hostd cell ownership tests passed\n";
}
