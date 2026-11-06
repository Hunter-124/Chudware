#include "hostd/doc_registry.h"
#include "api/libchudware.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <vector>

using nlohmann::json;

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
    require(!hostd::DocRegistry::isReadOnlyMethod("agent_pm_assist"), "agent prefix is not read authorization");
    require(!hostd::DocRegistry::isReadOnlyMethod("sim_study_add"), "sim prefix is not read authorization");
    require(registry.preCall(2, "gui", "", "", "worker_read_probe", params, error, warning, true), "trusted exact cell policy admits document worker reads");
    require(!registry.preCall(2, "gui", "", "", "worker_read_probe", params, error, warning), "unlisted document worker method fails closed");
    json write = {{"revision", 1}, {"tree", json::object()},
                  {"tess", {{"bodies", json::array({json{{"id", "body"}}})},
                            {"mesh_error", "aicm_publication_failed"}}},
                  {"mesh_error", "aicm_publication_failed"}};
    events.clear();
    registry.postCall(1, "gui", "spoofed", "Spoofed", "doc_apply_ops", params, write);
    require(events.size() == 2, "doc change reaches attached viewer and desktop only");
    for (const auto& [id, event] : events) {
        require(id != 3, "unattached subscription receives nothing");
        require(event["data"]["actor_key"] == "writer", "doc event attributed to trusted actor");
        require(event["data"]["tess"]["bodies"].size() == 1 &&
            event["data"]["mesh_error"] == "aicm_publication_failed",
            "change preserves mesh publication diagnostics");
    }
    require(registry.lastPushFor("part")["actor_key"] == "writer", "snapshot actor attribution");
    require(registry.lastPushFor("part")["tess"]["bodies"].size() == 1,
        "snapshot preserves body evidence for failed mesh publication");
    json listed = {{"docs", json::array({json{{"doc_id", "part"}}})}};
    registry.annotateDocList(listed, 2);
    require(listed["docs"][0]["lastWriter"]["actor_key"] == "writer", "lastWriter actor attribution");
    events.clear();
    json refresh = {{"doc_id", "part"}, {"revision", 1}, {"tree", {{"verified", true}}},
        {"cache_trust", "verified"}, {"cache_verification", "matched"}, {"warnings", json::array()},
        {"mismatch", false}, {"warning", nullptr}};
    registry.publishCacheRefresh("part", refresh);
    require(events.size() == 2, "cache refresh reaches attached cell and wildcard desktop subscribers");
    for (const auto& [id, event] : events)
        require((id == 2 || id == 4) && event["event"] == "doc.cache_refreshed" &&
            event["data"]["source_kind"] == "system", "refresh is system-only and retains cell isolation");
    require(registry.lastPushFor("part")["source_client"] == -1, "cache refresh uses system attribution");
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

static void registryRunOwnership() {
    hostd::DocRegistry registry;
    json result, error;
    bool isError = false;
    json minted{{"doc_id", "run-part"}, {"revision", 0}};
    registry.postCall(1, "gui", "", "", "doc_new", json::object(), minted);
    registry.onDisconnect(1, "gui");

    registry.startAgentRun("run-a", 10, "alice", "");
    registry.joinAgentRun(11, "run-a", "bob");
    registry.handleMethod(11, "agent", "attach_doc",
                          {{"doc_id", "run-part"}, {"mode", "ro"}}, result, isError);
    require(!isError, "foreign actor may read but cannot claim another actor's run");
    registry.onDisconnect(11, "agent");
    std::uint64_t token = 0;
    require(registry.reapToken("run-part", token), "foreign actor did not pin run document");

    registry.joinAgentRun(12, "run-a", "alice");
    json params{{"doc_id", "run-part"}};
    std::string warning;
    require(registry.preCall(12, "agent", "", "", "doc_tree", params, error, warning),
        "run can read GUI-origin document");
    registry.onDisconnect(12, "agent");
    bool called = false;
    require(!registry.reapToken("run-part", token) &&
        !registry.closeAbandoned("run-part", token, "gc", [&] { called = true; return true; }) &&
        !called, "live run invalidates an earlier GC token even after socket disconnect");

    registry.startAgentRun("run-b", 20, "alice", "");
    registry.joinAgentRun(21, "run-b", "alice");
    registry.handleMethod(21, "agent", "attach_doc",
                          {{"doc_id", "run-part"}, {"mode", "ro"}}, result, isError);
    registry.onDisconnect(21, "agent");
    registry.endAgentRun("run-a");
    require(!registry.reapToken("run-part", token), "second live run retains the shared doc");
    registry.endAgentRun("run-b");
    require(registry.reapToken("run-part", token), "last run releases the doc for GC");

    registry.startAgentRun("reopen", 50, "alice", "");
    registry.joinAgentRun(51, "reopen", "alice");
    json reused{{"doc_id", "run-part"}, {"revision", 0}, {"reused", true}};
    registry.postCall(51, "agent", "", "", "doc_open", {{"path", "/tmp/part.step+"}}, reused);
    registry.onDisconnect(51, "agent");
    require(!registry.reapToken("run-part", token), "reused open holds GUI-origin doc");
    registry.endAgentRun("reopen");
    require(registry.reapToken("run-part", token), "reused open releases with its run");

    registry.startAgentRun("run-c", 30, "alice", "");
    registry.joinAgentRun(31, "run-c", "alice");
    json internal{{"doc_id", "clone"}, {"revision", 0}};
    registry.postCall(31, "agent", "", "", "doc_new", {{"internal", true}}, internal);
    registry.onDisconnect(31, "agent");
    require(registry.reapToken("clone", token), "internal clone does not acquire a run hold");
    registry.endAgentRun("run-c");

    registry.startAgentRun("outer", 40, "alice", "");
    registry.joinAgentRun(41, "outer", "alice");
    registry.startAgentRun("inner", 41, "alice", "");
    json nested{{"doc_id", "nested"}, {"revision", 0}};
    registry.postCall(41, "agent", "", "", "doc_new", json::object(), nested);
    registry.endAgentRun("inner");
    registry.onDisconnect(41, "agent");
    require(!registry.reapToken("nested", token), "outer run retains nested work");
    registry.endAgentRun("outer");
    require(registry.reapToken("nested", token), "nested work releases when outer run ends");
}

static void registryRunMirrorNotice() {
    for (bool disconnect : {false, true}) {
        hostd::DocRegistry registry;
        std::vector<json> events;
        registry.setEventSender([&](int, const std::string& line) {
            events.push_back(json::parse(line));
        });
        json result;
        bool isError = false;
        registry.handleMethod(9, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
        json minted{{"doc_id", "run-part"}, {"revision", 0}};
        registry.postCall(1, "gui", "", "", "doc_new", json::object(), minted);
        registry.onDisconnect(1, "gui");
        events.clear();
        registry.startAgentRun("run-a", 10, "alice", "run-part");
        registry.startAgentRun("run-b", 20, "alice", "run-part");
        registry.joinAgentRun(11, "run-a", "alice");
        registry.handleMethod(11, "mcp", "attach_doc",
                              {{"doc_id", "run-part"}, {"mode", "rw"}}, result, isError);
        if (disconnect) registry.onDisconnect(11, "mcp");
        else registry.handleMethod(11, "mcp", "detach_doc",
                                   {{"doc_id", "run-part"}}, result, isError);
        auto closed = [&] {
            int count = 0;
            for (const auto& event : events)
                if (event.value("event", std::string()) == "doc.closed") ++count;
            return count;
        };
        require(closed() == 0, "tool disconnect must not mark an active run finished");
        registry.endAgentRun("run-a");
        require(closed() == 0, "another live run still owns the mirror");
        registry.endAgentRun("run-b");
        require(closed() == 1, "last run sends the deferred mirror notice once");
    }
}

static void registrySourceRefresh() {
    hostd::DocRegistry registry;
    std::vector<std::pair<int, json>> events;
    registry.setEventSender([&](int id, const std::string& line) {
        events.emplace_back(id, json::parse(line));
    });
    registry.setCellPrincipal(1, "writer", false);
    registry.setCellPrincipal(2, "viewer", true);
    registry.setCellPrincipal(3, "unattached", false);
    json result;
    bool isError = false;
    for (int id : {1, 2, 3, 4})
        registry.handleMethod(id, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json minted = {{"doc_id", "assembly"}, {"revision", 0}, {"tree", json::object()}};
    registry.postCall(1, "gui", "user-id", "User", "doc_new", json::object(), minted);
    registry.handleMethod(2, "gui", "attach_doc",
        {{"doc_id", "assembly"}, {"mode", "ro"}}, result, isError);
    require(!isError, "viewer attaches to assembly");
    json write = {{"revision", 1}, {"tree", {{"version", "writer"}}}};
    registry.postCall(1, "gui", "user-id", "User", "doc_apply_ops",
        {{"doc_id", "assembly"}}, write);

    json verified = {{"doc_id", "assembly"}, {"revision", 1},
        {"tree", {{"version", "verified"}}}, {"cache_trust", "verified"},
        {"cache_verification", "matched"}};
    events.clear();
    registry.publishCacheRefresh("assembly", verified);
    require(events.size() == 3, "verification reaches attached cell and wildcard desktop subscribers");
    bool verifiedWriter = false, verifiedViewer = false, verifiedDesktop = false;
    for (const auto& [id, event] : events) {
        verifiedWriter |= id == 1;
        verifiedViewer |= id == 2;
        verifiedDesktop |= id == 4;
        require(id == 1 || id == 2 || id == 4, "verification excludes unattached cell subscriber");
        require(event["event"] == "doc.cache_refreshed" && event["data"]["tree"] == verified["tree"],
            "verification publishes updated snapshot without a changed revision");
        require(event["data"]["source_client"] == -1 && event["data"]["source_kind"] == "system" &&
            event["data"]["method"] == "cache_refresh" && !event["data"].contains("actor_key"),
            "verification is attributed to system");
        require(event["data"]["cache_trust"] == "verified" &&
            event["data"]["cache_verification"] == "matched", "verification publishes cache metadata");
    }
    require(verifiedWriter && verifiedViewer && verifiedDesktop, "verification reaches all eligible subscribers");
    const json verifiedPush = registry.lastPushFor("assembly");
    require(verifiedPush["source_client"] == -1 && verifiedPush["source_kind"] == "system" &&
        verifiedPush["method"] == "cache_refresh" && verifiedPush["cache_trust"] == "verified",
        "verification snapshot preserves cache metadata");
    json saved{{"saved_revision", 1}, {"metadata_revision", 0}};
    registry.postCall(1, "gui", "", "", "doc_save", {{"doc_id", "assembly"}}, saved);
    require(!registry.humanDirty("assembly"), "save clears authored change before source refresh");

    json refreshed = {{"doc_id", "assembly"}, {"revision", 2},
        {"tree", {{"version", "source-refresh"}}}, {"owner_map", {{"body", "feature"}}}};
    events.clear();
    registry.publishCacheRefresh("assembly", refreshed);
    require(events.size() == 3, "advanced refresh reaches wildcard desktop subscriber");
    bool writerReceived = false, viewerReceived = false, desktopReceived = false;
    for (const auto& [id, event] : events) {
        writerReceived |= id == 1;
        viewerReceived |= id == 2;
        desktopReceived |= id == 4;
        require(id == 1 || id == 2 || id == 4, "source refresh excludes unattached cell subscriber");
        require(event["event"] == "doc.changed" && event["data"]["doc_id"] == "assembly" &&
            event["data"]["revision"] == 2 && event["data"]["tree"] == refreshed["tree"] &&
            event["data"]["owner_map"] == refreshed["owner_map"],
            "advanced source refresh publishes new revision and snapshot");
        require(event["data"]["source_client"] == -1 && event["data"]["source_kind"] == "system" &&
            event["data"]["method"] == "assembly_source_refresh" && !event["data"].contains("actor_key") &&
            event["data"]["user"]["id"] == "" && event["data"]["user"]["display"] == "",
            "advanced source refresh is attributed to system");
        require(event["data"]["human_dirty"] == false, "derived refresh is not a human edit");
    }
    require(writerReceived && viewerReceived && desktopReceived, "advanced refresh reaches all eligible subscribers");
    const json latestPush = registry.lastPushFor("assembly");
    require(latestPush["revision"] == 2 && latestPush["tree"] == refreshed["tree"] &&
        latestPush["owner_map"] == refreshed["owner_map"] && latestPush["source_client"] == -1 &&
        latestPush["source_kind"] == "system" && latestPush["method"] == "assembly_source_refresh" &&
        !latestPush.contains("actor_key"), "advanced refresh replaces cached snapshot and attribution");
    events.clear();
    registry.publishCacheRefresh("assembly", verified);
    require(events.empty() && registry.lastPushFor("assembly") == latestPush,
        "stale verification cannot publish or overwrite refreshed snapshot");
}

static void registryMeshReferenceMethods() {
    hostd::DocRegistry registry;
    std::vector<std::pair<int, json>> events;
    registry.setEventSender([&](int id, const std::string& line) {
        events.emplace_back(id, json::parse(line));
    });
    registry.setCellPrincipal(1, "writer", false);
    registry.setCellPrincipal(2, "viewer", true);
    registry.setCellPrincipal(3, "unattached", false);
    json result, error;
    bool isError = false;
    for (int id : {2, 3, 4})
        registry.handleMethod(id, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json minted = {{"doc_id", "mesh-part"}, {"revision", 0}, {"tree", json::object()}};
    registry.postCall(1, "gui", "", "", "doc_new", json::object(), minted);
    json params = {{"doc_id", "mesh-part"}};
    registry.handleMethod(2, "gui", "attach_doc",
        {{"doc_id", "mesh-part"}, {"mode", "rw"}}, result, isError);
    require(result["mode"] == "ro", "mesh viewer is pinned read-only");
    registry.handleMethod(4, "gui", "attach_doc",
        {{"doc_id", "mesh-part"}, {"mode", "ro"}}, result, isError);

    std::string warning;
    for (const char* method : {"doc_mesh_ref_list", "doc_mesh_ref_mesh"}) {
        require(hostd::DocRegistry::isReadOnlyMethod(method), "mesh inspection is a cell read");
        require(registry.preCall(2, "gui", "", "", method, params, error, warning),
            "cell viewer may inspect mesh references");
        require(registry.preCall(4, "gui", "", "", method, params, error, warning),
            "desktop read-only attachment may inspect mesh references");
        events.clear();
        json inspected = {{"revision", 0}};
        registry.postCall(2, "gui", "", "", method, params, inspected);
        require(events.empty(), "mesh inspection does not publish a change");
    }
    int revision = 0;
    for (const char* method : {"doc_mesh_ref_add", "doc_mesh_ref_update", "doc_mesh_ref_remove"}) {
        require(!hostd::DocRegistry::isReadOnlyMethod(method), "mesh mutation is not a cell read");
        require(!registry.preCall(2, "gui", "", "", method, params, error, warning) &&
            error["code"] == "forbidden_right", "cell viewer cannot mutate mesh references");
        require(!registry.preCall(4, "gui", "", "", method, params, error, warning) &&
            error["code"] == "read_only", "desktop read-only attachment cannot mutate mesh references");
        require(registry.preCall(1, "gui", "", "", method, params, error, warning),
            "writer can mutate mesh references");
        json changed = {{"revision", ++revision}, {"tree", {{"version", revision}}},
                        {"tess", {{"aicm_path", "part.aicm"}}}};
        events.clear();
        registry.postCall(1, "gui", "user-id", "User", method, params, changed);
        require(events.size() == 2, "mesh mutation reaches attached viewer and desktop only");
        for (const auto& [id, event] : events) {
            require((id == 2 || id == 4) && event["event"] == "doc.changed" &&
                event["data"]["doc_id"] == "mesh-part" && event["data"]["revision"] == revision &&
                event["data"]["method"] == method && event["data"]["actor_key"] == "writer" &&
                event["data"]["tree"] == changed["tree"] &&
                event["data"]["aicm_path"] == "part.aicm",
                "mesh mutation publishes attributed document snapshot");
        }
        require(registry.lastPushFor("mesh-part")["revision"] == revision,
            "mesh mutation updates cached snapshot");
    }

    registry.handleMethod(3, "gui", "attach_doc",
        {{"doc_id", "mesh-part"}, {"mode", "rw"}}, result, isError);
    registry.handleMethod(1, "gui", "edit_hold", params, result, isError);
    require(!isError, "writer holds mesh document");
    require(!registry.preCall(3, "gui", "", "", "doc_mesh_ref_update", params, error, warning) &&
        error["code"] == "busy_preview", "mesh mutation respects another writer's edit hold");
    require(registry.preCall(3, "gui", "", "", "doc_mesh_ref_list", params, error, warning),
        "mesh inspection remains allowed during edit hold");
    registry.handleMethod(1, "gui", "edit_release", params, result, isError);
}

static void nativeMeshReferenceDispatch(const std::filesystem::path& root) {
    std::filesystem::create_directories(root);
    const char* priorRoot = std::getenv("CHUDWARE_NATIVE_ROOT");
    const bool hadRoot = priorRoot != nullptr;
    const std::string oldRoot = priorRoot ? priorRoot : "";
#ifdef _WIN32
    _putenv_s("CHUDWARE_NATIVE_ROOT", root.string().c_str());
#else
    setenv("CHUDWARE_NATIVE_ROOT", root.string().c_str(), 1);
#endif
    const auto path = root / "reference.ply";
    {
        std::ofstream out(path);
        out << "ply\nformat ascii 1.0\nelement vertex 4\n"
               "property float x\nproperty float y\nproperty float z\n"
               "element face 2\nproperty list uchar int vertex_indices\nend_header\n"
               "0 0 0\n1 0 0\n1 1 0\n0 1 0\n3 0 1 2\n3 0 2 3\n";
        require(out.good(), "mesh reference fixture written");
    }
    const json created = call("doc_new", {{"kind", "part"}, {"name", "native-mesh-ref"}});
    const std::string doc = created.at("doc_id").get<std::string>();
    const json added = call("doc_mesh_ref_add", {{"doc_id", doc}, {"path", path.string()}});
    const std::string id = added.at("id").get<std::string>();
    require(added["ok"] == true && added["triangles"] == 2, "native add dispatch");
    const long revision = added.at("revision").get<long>();
    json listed = call("doc_mesh_ref_list", {{"doc_id", doc}});
    require(listed["ok"] == true && listed["revision"] == revision &&
        listed["refs"].size() == 1 && listed["refs"][0]["id"] == id,
        "native list dispatch");
    json page = call("doc_mesh_ref_mesh", {{"doc_id", doc}, {"id", id},
        {"offset_tris", 0}, {"max_tris", 1}, {"expected_revision", revision}});
    require(page["ok"] == true && page["revision"] == revision && page["total_tris"] == 2 &&
        page["indices"].size() == 3 && page["next_offset_tris"] == 1,
        "native first page dispatch");
    page = call("doc_mesh_ref_mesh", {{"doc_id", doc}, {"id", id},
        {"offset_tris", 1}, {"max_tris", 1}, {"expected_revision", revision}});
    require(page["indices"].size() == 3 && page["next_offset_tris"].is_null(),
        "native final page dispatch");
    const json updated = call("doc_mesh_ref_update", {{"doc_id", doc}, {"id", id},
        {"visible", false}});
    require(updated["ok"] == true && updated["revision"].get<long>() > revision,
        "native update dispatch");
    require(call("doc_mesh_ref_mesh", {{"doc_id", doc}, {"id", id},
        {"offset_tris", 1}, {"max_tris", 1}, {"expected_revision", revision}}, 3)["code"] ==
        "stale_revision", "native paged read detects revision changes");
    const json removed = call("doc_mesh_ref_remove", {{"doc_id", doc}, {"id", id}});
    require(removed["ok"] == true && removed["revision"].get<long>() >
        updated["revision"].get<long>(), "native remove dispatch");
    listed = call("doc_mesh_ref_list", {{"doc_id", doc}});
    require(listed["refs"].empty(), "native list reflects removal");
    call("doc_close", {{"doc_id", doc}});
#ifdef _WIN32
    _putenv_s("CHUDWARE_NATIVE_ROOT", hadRoot ? oldRoot.c_str() : "");
#else
    if (hadRoot) setenv("CHUDWARE_NATIVE_ROOT", oldRoot.c_str(), 1);
    else unsetenv("CHUDWARE_NATIVE_ROOT");
#endif
}

static void nativeSimRevocation() {
    const json oldCtx = ctx("revoke-native", "editor", "team:one", 4);
    call("sim_cancel", {{"_cell_ctx", oldCtx}, {"_cell_revoke_through", 4}});
    require(call("sim_spice", {{"_cell_ctx", oldCtx}, {"job_id", "stale-native-sim"}}, 1)["code"] == "revoked",
        "native dispatch exposes canonical revoked simulation registration");
}

static void registryPublicationRaces() {
    hostd::DocRegistry registry;
    std::vector<json> events;
    registry.setEventSender([&](int, const std::string& line) { events.push_back(json::parse(line)); });
    json result, error;
    bool isError = false;
    registry.handleMethod(9, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json minted{{"doc_id", "race"}, {"revision", 0}, {"tree", {{"version", 0}}}};
    registry.postCall(1, "gui", "", "", "doc_new", json::object(), minted);
    events.clear();

    // Engine reads may complete after a commit but reach postCall before its writer.
    json params{{"doc_id", "race"}};
    json read{{"revision", 1}, {"tree", {{"version", 1}}}};
    registry.postCall(2, "agent", "", "", "doc_tree", params, read);
    registry.publishCacheRefresh("race", json{{"revision", 1}, {"tree", {{"version", 1}}},
        {"_refresh_kind", "cache_verification"}});
    require(events.size() == 1 && events[0]["event"] == "doc.cache_refreshed",
        "read-ahead cache verification is not a commit");
    events.clear();
    json write{{"revision", 1}, {"tree", {{"version", 1}}}};
    registry.postCall(1, "gui", "", "", "doc_apply_ops", params, write);
    require(events.size() == 1 && events[0]["event"] == "doc.changed" &&
        events[0]["data"]["revision"] == 1 && events[0]["data"]["human_dirty"] == true,
        "read-before-postCall cannot swallow a human mutation event");
    require(registry.closeBlockedBy("race", 1), "read-created attachment blocks another client's close");
    json save{{"saved_revision", 0}};
    registry.postCall(1, "gui", "", "", "doc_save", params, save);
    require(save["human_dirty"] == true && registry.savedRevision("race") == 0,
        "older save does not clear later human edit");
    save = {{"saved_revision", 1}};
    registry.postCall(1, "gui", "", "", "doc_save", params, save);
    require(save["human_dirty"] == false && registry.savedRevision("race") == 1,
        "covering save clears human edit");
    json systemWrite{{"revision", 2}, {"tree", {{"version", 2}}}};
    registry.postCall(1, "gui", "", "", "doc_apply_ops",
                      {{"doc_id", "race"}, {"origin", "system"}}, systemWrite);
    require(!registry.humanDirty("race"), "system-origin GUI mutation is not human dirty");
    json humanWrite{{"revision", 3}, {"tree", {{"version", 3}}}};
    registry.postCall(1, "gui", "", "", "doc_apply_ops", params, humanWrite);
    json snapshot{{"saved_revision", 3}};
    registry.postCall(1, "agent", "", "", "doc_save",
                      {{"doc_id", "race"}, {"preserve_path", true}}, snapshot);
    require(registry.humanDirty("race") && registry.savedRevision("race") == 1,
        "private snapshot does not clear dirty or change saved revision");

    hostd::DocRegistry opening;
    events.clear();
    opening.setEventSender([&](int, const std::string& line) { events.push_back(json::parse(line)); });
    opening.handleMethod(9, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json openingParams{{"doc_id", "opening"}};
    std::string warning;
    require(opening.preCall(7, "agent", "", "", "doc_apply_ops", openingParams, error, warning),
        "unregistered call reaches engine");
    require(!opening.hasAttachments(7), "preCall of unknown id makes no phantom attachment");
    json firstWrite{{"revision", 1}, {"tree", {{"version", 1}}}};
    opening.postCall(7, "agent", "", "", "doc_apply_ops", openingParams, firstWrite);
    require(events.empty(), "write waits for mint origin and internal status");
    json opened{{"doc_id", "opening"}, {"revision", 0}, {"tree", {{"version", 0}}}};
    opening.postCall(1, "gui", "", "", "doc_open", {{"path", "/tmp/open.step+"}}, opened);
    require(events.size() == 2 && events[0]["event"] == "doc.opened" &&
        events[1]["event"] == "doc.changed" && events[1]["data"]["revision"] == 1,
        "mint completes placeholder and publishes first write after opened");
    require(opening.lastPushFor("opening")["tree"]["version"] == 1,
        "older mint response cannot replace newer snapshot");
    json listed{{"docs", json::array({json{{"doc_id", "opening"}}})}};
    opening.annotateDocList(listed, 9);
    require(listed["docs"][0]["origin_kind"] == "gui" &&
        listed["docs"][0]["human_dirty"] == false && listed["docs"][0]["saved_revision"] == 0,
        "mint installs origin and saved baseline without attributing agent write to human");

    opening.handleMethod(1, "gui", "detach_doc", openingParams, result, isError);
    opening.handleMethod(7, "agent", "detach_doc", openingParams, result, isError);
    std::uint64_t oldToken = 0;
    require(opening.reapToken("opening", oldToken), "abandoned document has a reap token");
    opening.handleMethod(6, "agent", "attach_doc",
                         {{"doc_id", "opening"}, {"mode", "ro"}}, result, isError);
    opening.handleMethod(6, "agent", "detach_doc", openingParams, result, isError);
    bool called = false;
    require(!opening.closeAbandoned("opening", oldToken, "gc", [&] { called = true; return true; }) &&
        !called, "attach-detach invalidates retirement token");
    std::uint64_t currentToken = 0;
    require(opening.reapToken("opening", currentToken) &&
        opening.closeAbandoned("opening", currentToken, "gc", [] { return true; }),
        "current abandonment can close");
    isError = false;
    opening.handleMethod(6, "agent", "attach_doc", openingParams, result, isError);
    require(isError && result["code"] == "not_found", "closed id cannot create an attachment");
    opening.postCall(7, "agent", "", "", "doc_apply_ops", openingParams, firstWrite);
    require(!opening.hasAttachments(7), "late successful response cannot recreate closed registry entry");

    hostd::DocRegistry metadata;
    events.clear();
    metadata.setEventSender([&](int, const std::string& line) { events.push_back(json::parse(line)); });
    metadata.handleMethod(9, "gui", "subscribe", {{"doc_ids", "*"}}, result, isError);
    json metaOpen{{"doc_id", "metadata"}, {"revision", 0}, {"metadata_revision", 0},
                  {"tree", json::object()}};
    metadata.postCall(1, "gui", "", "", "doc_open", {{"path", "/tmp/meta.step+"}}, metaOpen);
    events.clear();
    json metaWrite{{"revision", 0}, {"metadata_revision", 1}, {"ok", true}};
    metadata.postCall(1, "gui", "", "", "doc_set_meta", {{"doc_id", "metadata"}}, metaWrite);
    require(metadata.humanDirty("metadata") && events.size() == 1 &&
        events[0]["event"] == "doc.changed.notice" && events[0]["data"]["human_dirty"] == true,
        "metadata-only human edit is dirty without a geometry revision change");
    json olderSave{{"saved_revision", 0}, {"metadata_revision", 0}};
    metadata.postCall(1, "gui", "", "", "doc_save", {{"doc_id", "metadata"}}, olderSave);
    require(metadata.humanDirty("metadata"), "save of older metadata cannot clear human edit");
    json latestSave{{"saved_revision", 0}, {"metadata_revision", 1}};
    metadata.postCall(1, "gui", "", "", "doc_save", {{"doc_id", "metadata"}}, latestSave);
    require(!metadata.humanDirty("metadata") &&
        metadata.lastPushFor("metadata")["human_dirty"] == false,
        "covering metadata save clears registry and cached snapshot");
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
    registryRunOwnership();
    registryRunMirrorNotice();
    registrySourceRefresh();
    registryMeshReferenceMethods();
    registryPublicationRaces();
    nativeMeshReferenceDispatch(root);
    nativeSimRevocation();
    std::filesystem::remove_all(root);
    std::cout << "hostd cell ownership tests passed\n";
}
