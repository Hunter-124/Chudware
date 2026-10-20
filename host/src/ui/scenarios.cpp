// Scripted screenshot/test scenarios — implementation. Mechanically extracted from
// app.cpp's run_gui frame loop (behavior-preserving; see scenarios.h).
#include "scenarios.h"

#include "gl_compat.h"
#include "ui/feature_tree.h"
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <vector>
#include <map>
#include <sstream>

#include "../core/app_state.h"
#include "../core/sim_client.h"  // simaudit: mesh-preview idle wait (SimClient::busy)
#include "../renderer.h"  // T-G15 iconpipe: transparent thumbnail render + FBO readback
#include "../render/splat_renderer.h"  // RE-28: 3DGS splat renderer
#include "cam/cam_panel.h"  // camLoadJob (CAM copilot fast-path scenario)
#include "asm_panels.h"
#include "camera_anim.h"
#include "evaluate_dialogs.h"
#include "manufacture/manufacture_panel.h"
#include "property_manager.h"
#include "doc_tabs.h"
#include "onboarding.h"
#include "sim_manager.h"
#include "sim_topology.h"
#include "sketch_mode.h"
#include "sketch3d_mode.h"
#include "ui_audit.h"  // simaudit + CHUDWARE_IDAUDIT: test-engine-hook ID-conflict / clipping audit
#include "viewport_panel.h"
#include "layout.h"  // overlayBand: demoreview Simulation-column clip check
#include "copilot/copilot_internal.h"  // projectchat: session store, root reconciliation, Sessions probe
#include "sim/sim_scene.h"
#include "../ui_theme.h"
#include <array>
#include "../render/id_pass.h"
#include <imgui_internal.h>
#include "firmware/fw_workspace.h"
#include "engine_backend.h"
#include "ipc_client.h"
#ifndef __EMSCRIPTEN__
#include "account_panel.h"
#include "security/event_queue.h"
#include "util/device_identity.h"
#include "util/secret_store.h"
#endif

namespace {

// Read the current GL_BACK buffer, flip it vertically, and write it as an RGBA PNG to `path`
// (and `mirrorPath` when given). A null path skips that write; the readback still happens,
// matching the former inline capture blocks.
void writeBackbufferPng(const char* path, int dw, int dh, const char* mirrorPath = nullptr) {
    std::vector<unsigned char> px(dw * dh * 4);
    glReadBuffer(GL_BACK);
    glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    std::vector<unsigned char> flip(dw * dh * 4);
    for (int y = 0; y < dh; ++y)
        std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
    if (path) stbi_write_png(path, dw, dh, 4, flip.data(), dw * 4);
    if (mirrorPath) stbi_write_png(mirrorPath, dw, dh, 4, flip.data(), dw * 4);
}

// Use the exact published image rectangle and last renderer camera, including lens shift.
// The invisible input item covers only the content; it is not the full-bleed image origin.
bool sceneShotViewport(AppState& st, ui::simscene::ViewportCtx& vp) {
    if (!st.renderer || !ui::viewportBackdrop().valid()) return false;
    vp.cam = st.renderer->camera();
    vp.imgMin = ui::viewportBackdrop().screenMin;
    vp.imgMax = ui::viewportBackdrop().screenMax;
    vp.vw = vp.cam.vpWidth; vp.vh = vp.cam.vpHeight;
    return vp.vw > 0 && vp.vh > 0;
}

bool sceneShotPoint(AppState& st, const float world[3], ImVec2& screen) {
    ui::simscene::ViewportCtx vp;
    ImVec2 lo, hi;
    return sceneShotViewport(st, vp) && ui::viewportClearRect(lo, hi) &&
        ui::simscene::worldToScreen(vp, world, screen) &&
        screen.x > lo.x + 16 && screen.x < hi.x - 16 &&
        screen.y > lo.y + 36 && screen.y < hi.y - 36;
}

// Read the actual shaded viewport FBO, not chrome/schematic pixels. Count foreground
// in projected geometry patches against a same-row background sample (the studio
// background has a vertical gradient). Patches away from the obstacle keep the original
// piping shot from passing merely because its solid box is visible.
nlohmann::json sceneShotPixels(AppState& st, const std::vector<std::array<float, 3>>& points) {
    ui::simscene::ViewportCtx vp;
    ImVec2 lo, hi;
    if (!sceneShotViewport(st, vp) || !ui::viewportClearRect(lo, hi))
        return {{"ok", false}, {"reason", "no_rendered_viewport"}};
    const int w = vp.cam.ssWidth, h = vp.cam.ssHeight;
    if (w <= 0 || h <= 0) return {{"ok", false}, {"reason", "empty_framebuffer"}};
    std::vector<unsigned char> pixels(static_cast<size_t>(w) * h * 4);
    st.renderer->beginOverlay();
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
    st.renderer->endOverlay();
    auto pixel = [&](float x, float y) {
        const int ix = std::clamp((int)((x - vp.imgMin.x) * w / vp.vw), 0, w - 1);
        const int iy = std::clamp(h - 1 - (int)((y - vp.imgMin.y) * h / vp.vh), 0, h - 1);
        return &pixels[(static_cast<size_t>(iy) * w + ix) * 4];
    };
    int different = 0, sampled = 0, patches = 0, visiblePatches = 0;
    for (const auto& p : points) {
        ImVec2 s;
        if (!sceneShotPoint(st, p.data(), s)) continue;
        ++patches;
        const int before = different;
        for (int y = -8; y <= 8; ++y) for (int x = -8; x <= 8; ++x) {
            const auto* a = pixel(s.x + x, s.y + y);
            const auto* b = pixel(lo.x + 4, s.y + y);
            int delta = 0;
            for (int c = 0; c < 3; ++c) delta = std::max(delta, std::abs(int(a[c]) - int(b[c])));
            ++sampled;
            if (delta > 24) ++different;
        }
        if (different - before >= 20) ++visiblePatches;
    }
    return {{"ok", patches > 0 && different >= 40}, {"foreground_pixels", different},
            {"sampled_pixels", sampled}, {"projected_patches", patches}, {"visible_patches", visiblePatches},
            {"clear_rect", {lo.x, lo.y, hi.x, hi.y}}, {"source", "rendered_viewport_fbo"}};
}

struct SceneShotInput {
    int phase = 0;
    ImVec2 point;
    bool click(ImVec2 p, bool shift = false) {
        auto& io = ImGui::GetIO();
        if (phase == 0) point = p;
        io.AddMousePosEvent(point.x, point.y);
        io.AddKeyEvent(ImGuiMod_Shift, shift);
        if (phase == 1) io.AddMouseButtonEvent(0, true);
        if (phase == 2) io.AddMouseButtonEvent(0, false);
        if (++phase < 5) return false;
        io.AddKeyEvent(ImGuiMod_Shift, false);
        phase = 0;
        return true;
    }
    bool key(ImGuiKey k) {
        auto& io = ImGui::GetIO();
        // Headless backends may report an absent mouse between injected gestures.
        // Enter/Tab belong to the scene only while the last clicked point stays hovered.
        io.AddMousePosEvent(point.x, point.y);
        io.AddKeyEvent(k, phase == 0);
        if (++phase < 3) return false;
        phase = 0;
        return true;
    }
};

#ifndef __EMSCRIPTEN__
// CHUDWARE_SHOT=entitlement_delayed uses the ordinary tick-triggered refresh and
// real HTTP worker against an owned held error response. The opaque bearer and
// storage sentinels are NOT signed entitlement/policy/enforcement authority.
struct DelayedAccountShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0;
    std::chrono::steady_clock::time_point started;
    std::filesystem::path root, control;
    nlohmann::json report, signedOutState;
    using Secret = std::pair<bool, std::vector<unsigned char>>;
    std::map<std::string, Secret> signedOutSecrets;
    std::map<std::string, std::string> signedOutFiles;
} g_delayedAccount;

const char* const kDelayedAccountSecrets[] = {
    "entitlement", "entitlement-subject-v1", "security-policy-v1",
    "device-signing-pending-v1", "device-signing-v1", "entitlement-seal-key-v1",
    "security-state-v1", "security-event-counters-v1",
};

nlohmann::json delayedAccountState(const AppState& st) {
    return {{"signed_in", st.entitlementSignedIn}, {"read_only", st.entitlementReadOnly},
            {"token_empty", st.entitlementToken.empty()}, {"plan", st.entitlementPlan},
            {"email", st.entitlementEmail}, {"status", st.entitlementStatus},
            {"mutation_verified", st.entitlementMutationVerified},
            {"ai_verified", st.entitlementAiVerified},
            {"banner_height", ui::entitlementBannerHeight(st)}};
}

DelayedAccountShot::Secret delayedAccountSecret(const char* account) {
    std::vector<unsigned char> bytes;
    const bool present = chudware::secret_store::load(
        "com.chudware.desktop", account, bytes, appdataAppDir());
    return {present, std::move(bytes)};
}

std::map<std::string, std::string> delayedAccountFiles() {
    std::map<std::string, std::string> files;
    for (const char* relative : {"security/events.v1",
            "secrets/com_chudware_desktop-device-refresh-sequence-v1.state"}) {
        std::ifstream in(std::filesystem::path(appdataAppDir()) / relative, std::ios::binary);
        std::ostringstream out;
        out << in.rdbuf();
        files.emplace(relative, out.str());
    }
    return files;
}

bool delayedAccountCheck(const char* name, bool ok, const nlohmann::json& evidence = {}) {
    auto& P = g_delayedAccount;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("entitlement_delayed: %s %s\n", ok ? "PASS" : "FAIL", name);
    std::fflush(stdout);
    if (!ok) {
        P.report["failure"] = name;
        P.failed = P.done = true;
    }
    return ok;
}

nlohmann::json delayedAccountMarker(const char* name) {
    std::ifstream in(g_delayedAccount.control / name);
    if (!in) return {};
    try { return nlohmann::json::parse(in); }
    catch (...) { return {}; }
}

void delayedAccountTick(AppState& st) {
    auto& P = g_delayedAccount;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "entitlement_delayed") == 0;
        if (!P.want) return;
        P.started = std::chrono::steady_clock::now();
        P.report = {{"scenario", "entitlement_delayed"}, {"ok", false},
                    {"proof", "real held HTTP error; no trusted entitlement fixture"},
                    {"checks", nlohmann::json::array()}};
        const char* root = std::getenv("CHUDWARE_ENTITLEMENT_DELAYED_ROOT");
        P.root = root && *root ? std::filesystem::path(root) : std::filesystem::path();
        P.control = P.root / "control";
        auto ownedPath = [&](const char* path) {
            if (!path || !*path || !P.root.is_absolute()) return false;
            const auto relative = std::filesystem::path(path).lexically_normal()
                .lexically_relative(P.root.lexically_normal());
            return !relative.empty() && !relative.is_absolute() &&
                *relative.begin() != ".." && relative != ".";
        };
        const char* daemon = std::getenv("CHUDWARE_ENGINE_DAEMON");
        const char* endpoint = std::getenv("CHUDWARE_ENTITLEMENT_REFRESH_URL");
        const auto fixture = delayedAccountMarker("fixture.json");
        if (!delayedAccountCheck("owned_isolated_fixture",
                ownedPath(appdataAppDir().c_str()) && ownedPath(std::getenv("HOME")) &&
                ownedPath(std::getenv("CHUDWARE_SESSIONS_DIR")) &&
                daemon && std::strcmp(daemon, "0") == 0 && endpoint && fixture.is_object() &&
                fixture.value("scenario", "") == "entitlement_delayed" &&
                fixture.value("loopback_only", false) &&
                fixture.value("refresh_url", "") == endpoint &&
                std::string(endpoint).rfind("http://127.0.0.1:", 0) == 0))
            return;
    }
    if (!P.want || P.done) return;
    if (std::chrono::steady_clock::now() - P.started > std::chrono::seconds(30)) {
        delayedAccountCheck("completion_timeout", false, {{"step", P.step}});
        return;
    }
    if (st.frame < 3) return;
    switch (P.step) {
    case 0: {
        if (!delayedAccountCheck("initial_workers_idle", ui::desktopAccountRequestsIdle())) return;
        const std::string dir = appdataAppDir();
        const auto identity = chudware::device_identity::ensureForAuthorization(dir);
        const auto candidate = chudware::device_identity::prepareAuthorizationKey(dir);
        bool seeded = identity.status == chudware::device_identity::KeyStatus::Ready &&
                      candidate.status == chudware::device_identity::KeyStatus::Ready;
        const std::vector<unsigned char> sentinel{'o', 'w', 'n', 'e', 'd', '-', 'u', 'n', 's', 'i', 'g', 'n', 'e', 'd'};
        for (const char* account : {"entitlement", "entitlement-subject-v1",
                                    "security-policy-v1", "security-state-v1"})
            seeded = chudware::secret_store::store("com.chudware.desktop", account, sentinel, dir) && seeded;
        seeded = chudware::secret_store::store("com.chudware.desktop", "entitlement-seal-key-v1",
                                              std::vector<unsigned char>(32, 0x5a), dir) && seeded;
        chudware::security::EventQueue queue(dir);
        std::string error;
        const auto now = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        seeded = queue.append({"heartbeat", now, chudware::security::securityEventScore("heartbeat")},
                              &error) && seeded;
        const auto batch = queue.prepareBatch("owned-fixture-subject", "standard",
                                              "fixture", "linux", now, &error);
        if (!delayedAccountCheck("real_owned_storage_and_device_key_seeded",
                seeded && static_cast<bool>(batch) && queue.size() > 0,
                {{"error", error}, {"pending_sequence", batch.sequence}})) return;
        // The normal tick below launches startRefresh. No mailbox/result injection
        // and no local grant of editing authority is involved.
        st.entitlementSignedIn = true;
        st.entitlementReadOnly = true;
        st.entitlementToken = "unsigned-owned-fixture-bearer";
        st.entitlementStatus = "Owned unsigned request fixture";
        ++P.step;
        return;
    }
    case 1: {
        const auto received = delayedAccountMarker("received.json");
        if (received.is_null()) return;
        if (!delayedAccountCheck("real_refresh_request_held_before_signout",
                received.value("request_index", 0) == 1 &&
                received.value("signature_verified", false) &&
                !ui::desktopAccountRequestsIdle(), received)) return;
        std::map<std::string, DelayedAccountShot::Secret> beforeSecrets;
        for (const char* account : kDelayedAccountSecrets)
            beforeSecrets[account] = delayedAccountSecret(account);
        const auto beforeFiles = delayedAccountFiles();
        ui::signOutDesktopAccount(st);
        P.signedOutState = delayedAccountState(st);
        for (const char* account : kDelayedAccountSecrets)
            P.signedOutSecrets[account] = delayedAccountSecret(account);
        P.signedOutFiles = delayedAccountFiles();
        const auto& secrets = P.signedOutSecrets;
        bool erased = true;
        for (const char* account : {"entitlement", "entitlement-subject-v1",
                                    "security-policy-v1", "device-signing-pending-v1"})
            erased = erased && !secrets.at(account).first;
        bool retained = true;
        for (const char* account : {"device-signing-v1", "entitlement-seal-key-v1",
                                    "security-state-v1", "security-event-counters-v1"})
            retained = retained && secrets.at(account).first &&
                       secrets.at(account) == beforeSecrets.at(account);
        if (!delayedAccountCheck("signout_erases_session_retains_identity_and_security",
                erased && retained && !st.entitlementSignedIn && st.entitlementReadOnly &&
                st.entitlementToken.empty() && P.signedOutFiles == beforeFiles &&
                std::all_of(P.signedOutFiles.begin(), P.signedOutFiles.end(),
                    [](const auto& entry) { return !entry.second.empty(); }), P.signedOutState)) return;
        std::ofstream release(P.control / "release", std::ios::trunc);
        release << "signed out\n";
        release.close();
        if (!delayedAccountCheck("held_response_released_after_signout", static_cast<bool>(release))) return;
        ++P.step;
        return;
    }
    case 2: {
        const auto sent = delayedAccountMarker("sent.json");
        if (sent.is_null() || !ui::desktopAccountRequestsIdle()) return;
        if (!delayedAccountCheck("real_error_sent_and_worker_reaped",
                sent.value("status", 0) == 503 && sent.value("released", false), sent)) return;
        ui::tickAccountPanel(st);  // exercise the production UI-owner commit path once more
        const auto after = delayedAccountState(st);
        if (!delayedAccountCheck("late_completion_cannot_mutate_account_or_banner",
                after == P.signedOutState, {{"signed_out", P.signedOutState}, {"after_tick", after}})) return;
        nlohmann::json storage = nlohmann::json::object();
        bool unchanged = true;
        for (const char* account : kDelayedAccountSecrets) {
            const bool same = delayedAccountSecret(account) == P.signedOutSecrets.at(account);
            storage[account] = same;
            unchanged = unchanged && same;
        }
        storage["event_queue_and_refresh_sequence_bytes"] = delayedAccountFiles() == P.signedOutFiles;
        unchanged = unchanged && storage["event_queue_and_refresh_sequence_bytes"].get<bool>();
        if (!delayedAccountCheck("late_completion_cannot_mutate_credentials_or_security",
                                 unchanged, storage)) return;
        P.report["ok"] = true;
        P.done = true;
        return;
    }
    }
}

bool delayedAccountEndFrame(AppState& st, int dw, int dh,
                            const char* screenshotPath, bool& breakLoop) {
    auto& P = g_delayedAccount;
    if (!P.want) return false;
    ++st.frame;
    breakLoop = false;
    if (P.done) {
        writeBackbufferPng(screenshotPath, dw, dh);
        std::ofstream out(P.root / "report.json", std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        std::printf("entitlement_delayed: report -> %s ok=%d\n",
                    (P.root / "report.json").string().c_str(), !P.failed);
        std::fflush(stdout);
        if (P.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    glfwSwapBuffers(st.win);
    return true;
}
#endif

// ---------- piping scenario (CHUDWARE_SHOT=piping) ----------
// A fresh part with a 200 mm box in the way opens Simulate > Piping; a 2" sch 40 pipe is routed
// from -X to +X (the engine injects the box as an obstacle through doc_id, so the route has to
// bend around it), then Solve, ASME B31.3 Spec Check and BOM run through the panel's own button
// entry points. The network must come back from sim_study_list as a piping_network study holding
// all three results (so it saves with the .step+). Each panel state is captured to
// scratch/piping_shots with a JSON report; a failure exits non-zero. State is file-local because
// the scenario drives only public SimManager / PipingPanel entry points.
struct PipingShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, settle = 0, captureWait = 0, idleFrames = 0;
    std::string dir, capture, queued;
    nlohmann::json report;
    double waitStart = 0.0;
};
PipingShot g_piping;

double pipingNow() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

void pipingFail(const std::string& code, const std::string& detail) {
    std::printf("piping: FAILED [%s] %s\n", code.c_str(), detail.c_str());
    std::fflush(stdout);
    g_piping.report["ok"] = false;
    g_piping.report["failure"] = {{"code", code}, {"detail", detail}, {"step", g_piping.step}};
    g_piping.failed = true;
    g_piping.done = true;
    g_piping.queued.clear();
    g_piping.capture = g_piping.dir + "/piping_FAILED.png";
}

void pipingShotTick(AppState& st) {
    PipingShot& P = g_piping;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "piping") == 0;
        if (P.want) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            P.dir = std::string(repo ? repo : ".") + "/scratch/piping_shots";
            std::error_code ec;
            std::filesystem::create_directories(P.dir, ec);
            P.report = {{"scenario", "piping"}, {"ok", false},
                        {"checks", nlohmann::json::array()}, {"captures", nlohmann::json::array()}};
        }
    }
    if (!P.want || P.done) return;
    if (!st.workerStarted || st.frame < 5) return;
    if (!P.capture.empty()) return;  // endFrame consumes it
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) {
        P.capture = P.queued;
        P.queued.clear();
        return;
    }

    const int kSettle = 8;
    const double kTimeoutS = 180.0;
    auto shoot = [&](const char* name) {
        P.queued = P.dir + "/" + name;
        P.settle = kSettle * 2;
    };
    ui::SimManager* sm = st.simManager;
    if (P.step > 1 && !sm) {
        pipingFail("no_sim_manager", "the part lost its Simulation manager");
        return;
    }
    // True while the panel's request (or its study save) is still in flight.
    auto waiting = [&](const char* what) -> bool {
        if (!sm->piping().busy() && !st.rpc.busy()) return false;
        if (pipingNow() - P.waitStart > kTimeoutS)
            pipingFail(std::string(what) + "_timeout",
                       "no answer after " + std::to_string((int)kTimeoutS) + " s");
        return true;
    };
    auto launch = [&](const char* cmd, bool (ui::PipingPanel::*run)(AppState&), const char* what) {
        sm->handleCommand(st, cmd);
        if (!(sm->piping().*run)(st)) {
            pipingFail(std::string(what) + "_not_started", sm->piping().lastError());
            return;
        }
        P.waitStart = pipingNow();
        ++P.step;
    };
    auto refused = [&](const char* what) -> bool {
        if (sm->piping().lastError().empty()) return false;
        pipingFail(std::string(what) + "_refused", sm->piping().lastError());
        return true;
    };

    switch (P.step) {
        case 0: {  // part with a 200 mm cube centred on the origin: the obstacle
            std::string err;
            if (!st.docMirror.newPart(st.worker, err)) { pipingFail("new_part", err); return; }
            st.clearViewportForNewDoc();
            st.addDocTab(false);
            auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Obstacle"},
                {"params", {{"length", dim(200.0)}, {"width", dim(200.0)}, {"height", dim(200.0)}}},
                {"centered", true}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (!r.ok) { pipingFail("obstacle", r.error_message); return; }
            if (r.aicm_path.empty() || !st.loadDocMesh(r.aicm_path, err)) {
                pipingFail("obstacle_mesh", err.empty() ? "obstacle returned no mesh" : err); return;
            }
            std::printf("piping: obstacle box 200 mm at the origin (rev %d)\n", r.revision);
            std::fflush(stdout);
            st.yaw = 35; st.pitch = 30; st.zoom = 0.6f;
            P.settle = 3;
            P.idleFrames = 0;
            ++P.step;
            return;
        }
        case 1: {  // Simulate workspace, Piping tab, new network
            if (!sm) {
                if (++P.idleFrames > 120) pipingFail("no_sim_manager", "the part has no Simulation manager");
                return;
            }
            if (st.rpc.busy()) return;
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab = AppState::LP_Simulation;
            st.showRibbon = true;
            st.showSimulationTools = true;
            sm->handleCommand(st, "sim_piping_new");
            if (!sm->piping().isOpen()) { pipingFail("panel", "sim_piping_new did not open the panel"); return; }
            P.waitStart = pipingNow();
            ++P.step;
            return;
        }
        case 2: {  // route a 2" line straight through where the box is
            if (waiting("new_network")) return;
            const double start[3] = {-400.0, 0.0, 0.0}, end[3] = {400.0, 0.0, 0.0};
            const double dir[3] = {1.0, 0.0, 0.0};
            sm->piping().setRouteEndpoints(start, dir, end, dir);
            sm->piping().setRouteSpec(2.0, "40", "A106-B", 50.0);
            launch("sim_piping_route", &ui::PipingPanel::runRoute, "route");
            return;
        }
        case 3: {
            if (waiting("route")) return;
            if (refused("route")) return;
            const nlohmann::json& rt = sm->piping().lastRoute();
            const int bends = rt.value("bends", 0);
            const double clearance = rt.value("min_clearance_mm", -1.0);
            const nlohmann::json& net = sm->piping().network();
            const auto runs = net.is_object() ? net.value("runs", nlohmann::json::array())
                                              : nlohmann::json::array();
            std::printf("piping: routed %s length %.1f mm, %d bends, min clearance %.1f mm, %zu run(s)\n",
                        rt.value("run_id", std::string("?")).c_str(), rt.value("length_mm", 0.0), bends,
                        clearance, runs.size());
            std::fflush(stdout);
            P.report["route"] = rt;
            if (runs.size() != 1) { pipingFail("route_runs", "expected 1 run, got " + std::to_string(runs.size())); return; }
            if (bends < 2) { pipingFail("route_straight", "the route did not bend around the box"); return; }
            if (clearance < 49.9) { pipingFail("route_clearance", "min clearance " + std::to_string(clearance) + " mm < 50 mm"); return; }
            shoot("piping_01_route.png");
            ++P.step;
            return;
        }
        case 4:
            launch("sim_piping_solve", &ui::PipingPanel::runSolve, "solve");
            return;
        case 5: {
            if (waiting("solve")) return;
            if (refused("solve")) return;
            const nlohmann::json& s = sm->piping().solveResult();
            const auto links = s.value("links", nlohmann::json::array());
            std::printf("piping: solve converged=%d links=%zu\n", (int)s.value("converged", false), links.size());
            std::fflush(stdout);
            P.report["solve"] = s;
            if (!s.value("converged", false) || links.empty()) { pipingFail("solve_result", s.dump()); return; }
            shoot("piping_02_solve.png");
            ++P.step;
            return;
        }
        case 6:
            launch("sim_piping_spec", &ui::PipingPanel::runSpecCheck, "spec");
            return;
        case 7: {
            if (waiting("spec")) return;
            if (refused("spec")) return;
            const nlohmann::json& s = sm->piping().specResult();
            const auto runs = s.value("runs", nlohmann::json::array());
            std::printf("piping: B31.3 all_pass=%d runs=%zu\n", (int)s.value("all_pass", false), runs.size());
            std::fflush(stdout);
            P.report["spec_check"] = s;
            if (runs.empty()) { pipingFail("spec_result", s.dump()); return; }
            shoot("piping_03_spec.png");
            ++P.step;
            return;
        }
        case 8:
            launch("sim_piping_bom", &ui::PipingPanel::runBom, "bom");
            return;
        case 9: {
            if (waiting("bom")) return;
            if (refused("bom")) return;
            const nlohmann::json& b = sm->piping().bomResult();
            const auto items = b.value("items", nlohmann::json::array());
            std::printf("piping: BOM items=%zu totals=%s\n", items.size(),
                        b.value("totals", nlohmann::json::object()).dump().c_str());
            std::fflush(stdout);
            P.report["bom"] = b;
            if (items.empty()) { pipingFail("bom_result", b.dump()); return; }
            shoot("piping_04_bom.png");
            ++P.step;
            return;
        }
        case 10: {  // the network and all three results are a study of the document
            if (st.rpc.busy()) return;
            const std::string id = sm->piping().networkStudyId();
            nlohmann::json res;
            std::string err;
            if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res, err, 15000) ||
                !res.is_object()) {
                pipingFail("study_list", err);
                return;
            }
            nlohmann::json found;
            for (const auto& s : res.value("studies", nlohmann::json::array()))
                if (s.is_object() && s.value("id", std::string()) == id) found = s;
            if (id.empty() || !found.is_object() || found.value("type", std::string()) != "piping_network") {
                pipingFail("study_missing", "no piping_network study '" + id + "' in sim_study_list");
                return;
            }
            const auto results = found.value("results", nlohmann::json::object());
            for (const char* k : {"solve", "spec_check", "bom"})
                if (!results.contains(k)) {
                    pipingFail("study_incomplete", std::string("persisted network lacks results.") + k);
                    return;
                }
            P.report["study_id"] = id;
            std::printf("piping: study %s persisted with solve + spec_check + bom\n", id.c_str());
            std::fflush(stdout);
            P.report["ok"] = true;
            P.done = true;
            P.capture = P.dir + "/piping_05_final.png";
            return;
        }
        default:
            return;
    }
}

// Per-step capture + frame-loop control for the piping scenario. Returns false when the scenario
// is not active; otherwise `breakLoop` says whether the run is over.
bool pipingShotEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    PipingShot& P = g_piping;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty() && (!st.rpc.busy() || ++P.captureWait > 600)) {
        std::error_code ec;
        std::filesystem::create_directories(P.dir, ec);
        if (!P.failed && P.capture.find("01_route") != std::string::npos) {
            std::vector<std::array<float, 3>> points;
            for (const auto& run : st.simManager->piping().network().value("runs", nlohmann::json::array())) {
                const auto path = run.value("path", nlohmann::json::array());
                for (size_t i = 1; i < path.size(); ++i) for (float t : {0.25f, 0.5f, 0.75f}) {
                    std::array<float, 3> p;
                    for (int k = 0; k < 3; ++k)
                        p[k] = path[i-1][k].get<float>() * (1-t) + path[i][k].get<float>() * t;
                    if (std::fabs(p[0]) > 220) points.push_back(p);
                }
            }
            auto check = sceneShotPixels(st, points);
            check["name"] = "routed_pipe_visible";
            P.report["checks"].push_back(check);
            if (!check.value("ok", false)) pipingFail("routed_pipe_invisible", check.dump());
        }
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        std::printf("piping: screenshot -> %s (%dx%d)\n", P.capture.c_str(), dw, dh);
        std::fflush(stdout);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
        P.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (P.done && P.capture.empty()) {
        const std::string path = P.dir + "/piping_report.json";
        std::ofstream out(path, std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        std::printf("piping: report -> %s ok=%d\n", path.c_str(), (int)P.report.value("ok", false));
        std::fflush(stdout);
        if (P.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// ---------- hydraulics scenario (CHUDWARE_SHOT=hydraulics) ----------
// A fresh part (a base plate to carry the study) opens Simulate > Hydraulics and builds, through
// the panel's own button entry points (the same engine RPCs the agent tools call), a circuit of a
// reservoir, a gear pump, a relief valve, a 4/3 tandem-centre directional valve and a
// double-acting cylinder joined by six hoses (the pressure line is a three-way tee). The cylinder
// is sized (63/36 mm at 160 bar, applied to the component) and the pump's electric drive is
// sized. A 3 s transient then extends the cylinder against a 10 kN load after the valve shifts
// to "a" at 0.2 s: the rod must move. The BOM and the hose-to-piping handoff run next, and the
// circuit must come back from sim_study_list holding simulate, BOM, piping handoff and sizing.
// A second record then adopts the native cylinder_circuit template through the Start-card path
// and runs its included setup. Captures and hydraulics_report.json land in scratch/hydraulics_shots.
struct HydraulicsShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, sub = 0, settle = 0, captureWait = 0, idleFrames = 0;
    std::string dir, capture, queued;
    nlohmann::json report;
    double waitStart = 0.0, started = 0.0;
    bool view3d = false;
    SceneShotInput input;
    std::string placedId;
    nlohmann::json beforePosition;
};
HydraulicsShot g_hyd;

void hydFail(const std::string& code, const std::string& detail) {
    std::printf("hydraulics: FAILED [%s] %s\n", code.c_str(), detail.c_str());
    std::fflush(stdout);
    g_hyd.report["ok"] = false;
    g_hyd.report["failure"] = {{"code", code}, {"detail", detail}, {"step", g_hyd.step}};
    g_hyd.failed = true;
    g_hyd.done = true;
    g_hyd.queued.clear();
    g_hyd.capture = g_hyd.dir + "/hydraulics_FAILED.png";
}

void hydraulicsShotTick(AppState& st) {
    HydraulicsShot& H = g_hyd;
    if (!H.init) {
        H.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        H.view3d = mode && std::strcmp(mode, "hydraulics3d") == 0;
        H.want = st.shot && mode && (std::strcmp(mode, "hydraulics") == 0 || H.view3d);
        if (H.want) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            H.dir = std::string(repo ? repo : ".") + (H.view3d ? "/scratch/hydraulics3d_shots" : "/scratch/hydraulics_shots");
            std::error_code ec;
            std::filesystem::create_directories(H.dir, ec);
            H.report = {{"scenario", H.view3d ? "hydraulics3d" : "hydraulics"}, {"ok", false},
                        {"checks", nlohmann::json::array()}, {"captures", nlohmann::json::array()}};
            H.started = pipingNow();
        }
    }
    if (!H.want || H.done) return;
    if (H.view3d && pipingNow() - H.started > 600) {
        hydFail("scenario_timeout", "3D layout scenario exceeded 600 s"); return;
    }
    if (!st.workerStarted || st.frame < 5) return;
    if (!H.capture.empty()) return;  // endFrame consumes it
    if (H.settle > 0) { --H.settle; return; }
    if (!H.queued.empty()) {
        H.capture = H.queued;
        H.queued.clear();
        return;
    }

    const int kSettle = 8;
    const double kTimeoutS = 300.0;
    auto shoot = [&](const char* name) {
        H.queued = H.dir + "/" + name;
        H.settle = kSettle * 2;
    };
    ui::SimManager* sm = st.simManager;
    if (H.step > 1 && !sm) {
        hydFail("no_sim_manager", "the part lost its Simulation manager");
        return;
    }
    // True while a panel request (or its study save) is still in flight.
    auto waiting = [&](const char* what) -> bool {
        if (!sm->hydraulic().busy() && !st.rpc.busy()) return false;
        if (pipingNow() - H.waitStart > kTimeoutS)
            hydFail(std::string(what) + "_timeout", "no answer after " + std::to_string((int)kTimeoutS) + " s");
        return true;
    };
    auto refused = [&](const char* what) -> bool {
        if (sm->hydraulic().lastError().empty()) return false;
        hydFail(std::string(what) + "_refused", sm->hydraulic().lastError());
        return true;
    };
    auto started = [&](bool ok, const char* what) {
        if (!ok) {
            hydFail(std::string(what) + "_not_started", sm->hydraulic().lastError());
            return;
        }
        H.waitStart = pipingNow();
    };
    auto id = [&](const char* name) { return sm->hydraulic().componentIdByName(name); };

    struct Comp {
        const char* type;
        const char* name;
        double x, y;
        nlohmann::json params;
    };
    static const std::vector<Comp> kComps = {
        {"reservoir", "Tank", 0.0, -150.0, {{"volume_l", 60.0}}},
        {"pump", "Pump", 0.0, 0.0,
         {{"pump_type", "gear"}, {"displacement_cc_rev", 16.0}, {"speed_rpm", 1450.0},
          {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}}},
        {"valve_relief", "Relief", 180.0, 0.0, {{"set_pressure_bar", 160.0}}},
        {"valve_directional", "DCV 4/3", 0.0, 160.0,
         {{"ways", 4}, {"positions", 3}, {"center_condition", "tandem"}, {"actuator", "solenoid"},
          {"response_ms", 30.0}}},
        {"cylinder_linear", "Lift cylinder", 0.0, 320.0,
         {{"actuator_type", "double_acting"}, {"bore_mm", 50.0}, {"rod_mm", 28.0},
          {"stroke_mm", 300.0}, {"piston_mass_kg", 5.0}}},
    };
    struct Hose {
        const char* net;
        std::vector<std::pair<const char*, const char*>> ends;  // component name, port
        double lengthMm, idMm, ratingBar;
    };
    static const std::vector<Hose> kHoses = {
        {"Pressure", {{"Pump", "P"}, {"Relief", "P"}, {"DCV 4/3", "P"}}, 800.0, 12.7, 280.0},
        {"Relief return", {{"Relief", "T"}, {"Tank", "T"}}, 600.0, 19.0, 40.0},
        {"Return", {{"DCV 4/3", "T"}, {"Tank", "T2"}}, 1200.0, 19.0, 40.0},
        {"Suction", {{"Tank", "S"}, {"Pump", "T"}}, 400.0, 25.4, 10.0},
        {"Cylinder A", {{"DCV 4/3", "A"}, {"Lift cylinder", "A"}}, 1500.0, 12.7, 280.0},
        {"Cylinder B", {{"DCV 4/3", "B"}, {"Lift cylinder", "B"}}, 1500.0, 12.7, 280.0},
    };

    switch (H.step) {
        case 0: {  // a part to carry the study: a 300 x 200 x 20 mm base plate
            std::string err;
            if (!st.docMirror.newPart(st.worker, err)) { hydFail("new_part", err); return; }
            st.clearViewportForNewDoc();
            st.addDocTab(false);
            if (H.view3d) {
                st.planesUserToggled = st.axesUserToggled = st.originUserToggled = true;
                for (auto& p : st.showPlane) p = false;
                for (const char* id : {"axis.x", "axis.y", "axis.z", "point.origin"}) st.hiddenFeatures.insert(id);
                H.settle = 3; ++H.step; return;
            }
            auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Power unit base"},
                {"params", {{"length", dim(300.0)}, {"width", dim(200.0)}, {"height", dim(20.0)}}},
                {"centered", true}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (!r.ok) { hydFail("base_plate", r.error_message); return; }
            if (r.aicm_path.empty() || !st.loadDocMesh(r.aicm_path, err)) {
                hydFail("base_plate_mesh", err.empty() ? "base plate returned no mesh" : err); return;
            }
            std::printf("hydraulics: base plate part (rev %d)\n", r.revision);
            std::fflush(stdout);
            H.settle = 3;
            H.idleFrames = 0;
            ++H.step;
            return;
        }
        case 1: {  // Simulate workspace, Hydraulics tab, New Circuit
            if (!sm) {
                if (++H.idleFrames > 120) hydFail("no_sim_manager", "the part has no Simulation manager");
                return;
            }
            if (st.rpc.busy()) return;
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab = AppState::LP_Simulation;
            st.showRibbon = true;
            st.showSimulationTools = true;
            sm->handleCommand(st, "sim_hyd_new");
            if (!sm->hydraulic().isOpen()) { hydFail("panel", "sim_hyd_new did not open the panel"); return; }
            H.waitStart = pipingNow();
            ++H.step;
            return;
        }
        case 2: {
            if (waiting("new_circuit")) return;
            if (refused("new_circuit")) return;
            if (!sm->hydraulic().circuit().is_object()) { hydFail("new_circuit", "no circuit after hydraulic_new"); return; }
            H.sub = 0;
            ++H.step;
            return;
        }
        case 3: {  // Add Component x5
            if (waiting("add_component")) return;
            if (refused("add_component")) return;
            if (H.sub < (int)kComps.size()) {
                const Comp& c = kComps[H.sub];
                sm->handleCommand(st, "sim_hyd_add");
                started(sm->hydraulic().addComponent(st, c.type, c.name, c.x, c.y, c.params), "add_component");
                ++H.sub;
                return;
            }
            const auto comps = sm->hydraulic().circuit().value("components", nlohmann::json::array());
            if (comps.size() != kComps.size()) {
                hydFail("components", "expected " + std::to_string(kComps.size()) + " components, got " +
                                          std::to_string(comps.size()));
                return;
            }
            for (const auto& c : kComps)
                if (id(c.name).empty()) { hydFail("component_missing", c.name); return; }
            std::printf("hydraulics: %zu components added\n", comps.size());
            std::fflush(stdout);
            shoot("hydraulics_01_components.png");
            H.sub = 0;
            ++H.step;
            return;
        }
        case 4: {  // Connect x6 (the pressure line is a three-way tee)
            if (waiting("connect")) return;
            if (refused("connect")) return;
            if (H.sub < (int)kHoses.size()) {
                const Hose& h = kHoses[H.sub];
                std::vector<std::pair<std::string, std::string>> ends;
                for (const auto& e : h.ends) ends.push_back({id(e.first), e.second});
                sm->handleCommand(st, "sim_hyd_hose");
                started(sm->hydraulic().addHose(st, h.net, ends, h.lengthMm, h.idMm, h.ratingBar), "connect");
                ++H.sub;
                return;
            }
            const auto hoses = sm->hydraulic().circuit().value("hoses", nlohmann::json::array());
            if (hoses.size() != kHoses.size()) {
                hydFail("hoses", "expected " + std::to_string(kHoses.size()) + " hoses, got " +
                                     std::to_string(hoses.size()));
                return;
            }
            for (const auto& h : hoses)
                if (!h.value("id_mm", 0.0) || !h.value("length_mm", 0.0)) {
                    hydFail("hose_geometry", "hose " + h.value("id", std::string("?")) + " lost its id_mm/length_mm");
                    return;
                }
            H.report["circuit"] = sm->hydraulic().circuit();
            std::printf("hydraulics: %zu hoses connected\n", hoses.size());
            std::fflush(stdout);
            shoot("hydraulics_02_circuit.png");
            ++H.step;
            return;
        }
        case 5:  // Size Cylinder: 63/36 mm at 160 bar, 300 mm stroke, 100 mm/s
            sm->handleCommand(st, "sim_hyd_size_cyl");
            sm->hydraulic().setCylinderInputs(id("Lift cylinder"), 63.0, 36.0, 160.0, 300.0, 100.0);
            started(sm->hydraulic().runSizeCylinder(st), "size_cylinder");
            ++H.step;
            return;
        case 6: {
            if (waiting("size_cylinder")) return;
            if (refused("size_cylinder")) return;
            const nlohmann::json& r = sm->hydraulic().cylinderResult();
            const double fkn = r.value("extend_force_kn", 0.0);
            std::printf("hydraulics: cylinder extend %.2f kN retract %.2f kN flow %.2f L/min\n", fkn,
                        r.value("retract_force_kn", 0.0), r.value("flow_extend_lpm", 0.0));
            std::fflush(stdout);
            H.report["cylinder_sizing"] = r;
            // 63 mm bore at 160 bar, 95 %: pi/4 * 0.063^2 * 160e5 * 0.95 = 47.4 kN.
            if (std::fabs(fkn - 47.37) > 0.5) { hydFail("cylinder_force", r.dump()); return; }
            if (!sm->hydraulic().applyCylinderSizing(st)) { hydFail("cylinder_apply", sm->hydraulic().lastError()); return; }
            const auto comps = sm->hydraulic().circuit().value("components", nlohmann::json::array());
            double bore = 0.0;
            for (const auto& c : comps)
                if (c.value("name", std::string()) == "Lift cylinder")
                    bore = c.value("params", nlohmann::json::object()).value("bore_mm", 0.0);
            if (std::fabs(bore - 63.0) > 1e-9) { hydFail("cylinder_apply", "bore_mm is " + std::to_string(bore)); return; }
            H.waitStart = pipingNow();
            shoot("hydraulics_03_cylinder.png");
            ++H.step;
            return;
        }
        case 7:  // Pump Drive for the gear pump (flow from its displacement, pressure = relief)
            if (waiting("apply_cylinder")) return;
            sm->handleCommand(st, "sim_hyd_drive");
            sm->hydraulic().setDriveFromPump(id("Pump"));
            started(sm->hydraulic().runPumpDrive(st), "pump_drive");
            ++H.step;
            return;
        case 8: {
            if (waiting("pump_drive")) return;
            if (refused("pump_drive")) return;
            const nlohmann::json& r = sm->hydraulic().driveResult();
            const double kw = r.value("motor_rating_kw", 0.0);
            std::printf("hydraulics: pump drive %.2f kW motor, %d poles, shaft %.2f kW\n", kw,
                        r.value("poles", 0), r.value("shaft_power_kw", 0.0));
            std::fflush(stdout);
            H.report["pump_drive"] = r;
            // 22.04 L/min at 160 bar = 5.88 kW hydraulic: the shaft and the rated motor exceed it.
            if (!(kw > r.value("shaft_power_kw", 0.0)) || !(r.value("shaft_power_kw", 0.0) > 5.8)) {
                hydFail("pump_drive_result", r.dump());
                return;
            }
            shoot("hydraulics_04_drive.png");
            ++H.step;
            return;
        }
        case 9: {  // Simulate 3 s: valve to "a" at 0.2 s, extending against 10 kN + 200 kg
            ui::HydraulicPanel& hp = sm->hydraulic();
            hp.clearSchedule();
            hp.addScheduleRow(0.0, id("DCV 4/3"), "center");
            hp.addScheduleRow(0.2, id("DCV 4/3"), "a");
            hp.setLoad(id("Lift cylinder"), 10000.0, 200.0);
            hp.setDuration(3.0);
            sm->handleCommand(st, "sim_hyd_simulate");
            if (!hp.busy()) { hydFail("simulate_not_started", hp.lastError()); return; }
            H.waitStart = pipingNow();
            H.sub = 0;
            ++H.step;
            return;
        }
        case 10: {
            if (waiting("simulate")) return;
            if (refused("simulate")) return;
            if (H.sub == 1) {  // second pass, after the schematic capture: the full-size plots
                sm->hydraulic().setCentralView(ui::HydraulicPanel::CentralView::Plots);
                if (!sm->centralViewActive()) { hydFail("central_plots", "the Plots view does not own the central view"); return; }
                shoot("hydraulics_05b_plots.png");
                H.sub = 0;
                ++H.step;
                return;
            }
            const nlohmann::json& r = sm->hydraulic().simulateResult();
            const auto t = r.value("time_s", nlohmann::json::array());
            const std::string cyl = id("Lift cylinder");
            const auto series = r.value("components", nlohmann::json::object())
                                    .value(cyl, nlohmann::json::object())
                                    .value("series", nlohmann::json::object());
            const auto pos = series.value("position_mm", nlohmann::json::array());
            double xmax = 0.0;
            for (const auto& v : pos)
                if (v.is_number()) xmax = std::max(xmax, v.get<double>());
            const auto summary = r.value("summary", nlohmann::json::object());
            std::printf("hydraulics: simulated %zu samples, cylinder max %.1f mm, peak %.1f bar\n", t.size(),
                        xmax, summary.value("peak_pressure_bar", 0.0));
            std::fflush(stdout);
            H.report["simulate_summary"] = summary;
            H.report["simulate_solver"] = r.value("solver", nlohmann::json::object());
            H.report["cylinder_max_position_mm"] = xmax;
            if (t.size() < 2 || pos.size() != t.size()) { hydFail("simulate_series", "missing time / position series"); return; }
            if (!(xmax > 10.0)) { hydFail("cylinder_did_not_extend", "max position " + std::to_string(xmax) + " mm"); return; }
            if (!summary.contains("peak_pressure_bar")) { hydFail("simulate_summary", summary.dump()); return; }
            if (H.view3d) {
                sm->handleCommand(st, "sim_hyd_view_3d");
                if (sm->centralViewActive() || sm->hydraulic().centralView() != ui::HydraulicPanel::CentralView::View3D) {
                    hydFail("central_3d", "3D Layout did not return the central view to the native viewport"); return;
                }
                H.report["checks"].push_back({{"name", "native_viewport_not_schematic"}, {"ok", true}});
                shoot("hydraulics3d_01_simulated.png");
                H.step = 20;
                return;
            }
            // Results in place: the full-size schematic first (node peaks on its nets), then the plots.
            sm->hydraulic().setCentralView(ui::HydraulicPanel::CentralView::Schematic);
            if (!sm->centralViewActive()) { hydFail("central_schematic", "the Schematic view does not own the central view"); return; }
            shoot("hydraulics_05a_schematic.png");
            H.sub = 1;
            return;
        }
        case 11:  // BOM
            sm->handleCommand(st, "sim_hyd_bom");
            started(sm->hydraulic().runBom(st), "bom");
            ++H.step;
            return;
        case 12: {
            if (waiting("bom")) return;
            if (refused("bom")) return;
            const auto lines = sm->hydraulic().bomResult().value("bom_lines", nlohmann::json::array());
            if (lines.size() != kComps.size()) { hydFail("bom_lines", sm->hydraulic().bomResult().dump()); return; }
            shoot("hydraulics_06_bom.png");
            ++H.step;
            return;
        }
        case 13:  // To Piping: every hose becomes a solved pipe run
            sm->handleCommand(st, "sim_hyd_piping");
            started(sm->hydraulic().runPipingHandoff(st), "piping_handoff");
            ++H.step;
            return;
        case 14: {
            if (waiting("piping_handoff")) return;
            if (refused("piping_handoff")) return;
            const nlohmann::json& r = sm->hydraulic().handoffResult();
            const auto runs = r.value("piping_doc", nlohmann::json::object()).value("runs", nlohmann::json::array());
            std::printf("hydraulics: piping handoff %zu runs, stage %s\n", runs.size(),
                        r.value("stage", std::string("?")).c_str());
            std::fflush(stdout);
            H.report["piping_handoff_stage"] = r.value("stage", std::string());
            if (runs.size() != kHoses.size()) { hydFail("piping_runs", "expected one run per hose"); return; }
            shoot("hydraulics_07_piping.png");
            ++H.step;
            return;
        }
        case 15: {  // the circuit and every result are a hydraulic_circuit study of the document
            if (waiting("study_save")) return;
            const std::string sid = sm->hydraulic().studyId();
            nlohmann::json res;
            std::string err;
            if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res, err, 15000) ||
                !res.is_object()) {
                hydFail("study_list", err);
                return;
            }
            nlohmann::json found;
            for (const auto& s : res.value("studies", nlohmann::json::array()))
                if (s.is_object() && s.value("id", std::string()) == sid) found = s;
            if (sid.empty() || !found.is_object() || found.value("type", std::string()) != "hydraulic_circuit") {
                hydFail("study_missing", "no hydraulic_circuit study '" + sid + "' in sim_study_list");
                return;
            }
            const auto results = found.value("results", nlohmann::json::object());
            for (const char* k : {"simulate", "bom", "piping_handoff", "sizing"})
                if (!results.contains(k)) {
                    hydFail("study_incomplete", std::string("persisted circuit lacks results.") + k);
                    return;
                }
            const auto sim = results["simulate"];
            if (!sim.is_object() || !sim.contains("summary") ||
                sim.value("time_s", nlohmann::json::array()).size() >
                    (std::size_t)ui::HydraulicPanel::kMaxStoredSamples) {
                hydFail("study_simulate", "persisted simulate lacks a summary or exceeds the sample cap");
                return;
            }
            const auto sizing = results["sizing"];
            if (!sizing.value("cylinders", nlohmann::json::object()).contains(id("Lift cylinder")) ||
                !sizing.value("drives", nlohmann::json::object()).contains(id("Pump"))) {
                hydFail("study_sizing", "persisted sizing lacks the cylinder or the pump drive: " + sizing.dump());
                return;
            }
            const auto params = found.value("params", nlohmann::json::object());
            if (params.value("circuit", nlohmann::json::object()).value("hoses", nlohmann::json::array()).size() !=
                    kHoses.size() ||
                params.value("valve_schedule", nlohmann::json::array()).size() != 2) {
                hydFail("study_params", "persisted params lack the circuit hoses or the valve schedule");
                return;
            }
            H.report["study_id"] = sid;
            std::printf("hydraulics: study %s persisted with simulate + bom + piping_handoff + sizing\n", sid.c_str());
            std::fflush(stdout);
            sm->hydraulic().openResults();
            sm->hydraulic().setCentralView(ui::HydraulicPanel::CentralView::Plots);
            H.queued = H.dir + "/hydraulics_08_final.png";
            H.settle = kSettle * 2;
            ++H.step;
            return;
        }
        case 16: {  // start a second record, then use the exact Start-card template adoption path
            if (waiting("study_save")) return;
            if (refused("study_save")) return;
            started(sm->hydraulic().newCircuit(st, "Cylinder template circuit"), "template_new");
            ++H.step;
            return;
        }
        case 17:
            if (waiting("template_new")) return;
            if (refused("template_new")) return;
            started(sm->hydraulic().applyTemplate(st, "cylinder_circuit"), "template_apply");
            ++H.step;
            return;
        case 18: {
            if (waiting("template_apply")) return;
            if (refused("template_apply")) return;
            auto& hp = sm->hydraulic();
            const auto setup = hp.simulationDefinition();
            if (hp.studyId().empty() || hp.studyId() == H.report.value("study_id", std::string()) ||
                hp.circuit().value("components", nlohmann::json::array()).empty() ||
                hp.circuit().value("hoses", nlohmann::json::array()).empty() ||
                setup.value("valve_schedule", nlohmann::json::array()).empty()) {
                hydFail("template_setup", "template did not create a second connected circuit with its schedule");
                return;
            }
            H.report["template_setup"] = setup;
            sm->handleCommand(st, "sim_hyd_simulate");
            if (!hp.busy()) { hydFail("template_run_not_started", hp.lastError()); return; }
            H.waitStart = pipingNow();
            ++H.step;
            return;
        }
        case 19: {
            if (waiting("template_simulate")) return;
            if (refused("template_simulate")) return;
            const auto& result = sm->hydraulic().simulateResult();
            if (!result.value("ok", false) || !result.contains("summary") ||
                result.value("time_s", nlohmann::json::array()).size() < 2) {
                hydFail("template_simulate", result.dump());
                return;
            }
            H.report["checks"].push_back({{"name", "cylinder_template_simulation"}, {"ok", true},
                {"study_id", sm->hydraulic().studyId()}, {"summary", result["summary"]}});
            H.report["ok"] = true;
            H.done = true;
            H.capture = H.dir + "/hydraulics_09_template.png";
            return;
        }
        case 20: {
            if (waiting("layout")) return;
            auto& hp = sm->hydraulic();
            H.placedId = id("Pump");
            for (const auto& c : hp.circuit()["components"]) if (c.value("id", std::string()) == H.placedId) {
                float p[3]; hp.componentPosition3d(c, p);
                H.beforePosition = {p[0], p[1], p[2]};
            }
            hp.selectComponent3d(H.placedId);
            hp.placementPlane = ui::simscene::Plane::XY;
            hp.placementGridMm = 10;
            hp.armPlacement3d(true);
            if (!hp.placing3d()) { hydFail("placement_not_armed", "pump placement tool did not arm"); return; }
            H.waitStart = pipingNow();
            ++H.step;
            return;
        }
        case 21: {
            const float world[3] = {120, 100, 0};
            ImVec2 p;
            if (!sceneShotPoint(st, world, p)) { hydFail("placement_projection", "plane point outside clear viewport"); return; }
            if (!H.input.click(p)) return;
            H.report["checks"].push_back({{"name", "real_plane_click"}, {"ok", true},
                {"world", {world[0], world[1], world[2]}}, {"screen", {p.x, p.y}}});
            H.waitStart = pipingNow(); ++H.step;
            return;
        }
        case 22: {
            if (waiting("placement_save")) return;
            auto& hp = sm->hydraulic();
            const auto pos = hp.layout3d().value(H.placedId, nlohmann::json());
            if (pos == H.beforePosition || !pos.is_array() || pos.size() != 3 ||
                std::fabs(pos[0].get<double>() - 120) > 0.1 ||
                std::fabs(pos[1].get<double>() - 100) > 0.1 || std::fabs(pos[2].get<double>()) > 0.1) {
                hydFail("placement_input", "real viewport click did not move pump: " + pos.dump()); return;
            }
            hp.armPlacement3d(false);
            nlohmann::json list, found; std::string error;
            if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, list, error, 15000)) {
                hydFail("layout_study_list", error); return;
            }
            for (const auto& s : list.value("studies", nlohmann::json::array()))
                if (s.value("id", std::string()) == hp.studyId()) found = s;
            if (!found.is_object() || found.value("type", std::string()) != "hydraulic_circuit" ||
                found.value("params", nlohmann::json::object()).value("layout3d", nlohmann::json::object()).value(H.placedId, nlohmann::json()) != pos ||
                !found.value("results", nlohmann::json::object()).contains("simulate")) {
                hydFail("layout_not_persisted", found.dump()); return;
            }
            H.report["checks"].push_back({{"name", "real_click_layout_persisted"}, {"ok", true},
                {"before", H.beforePosition}, {"after", pos}, {"study_id", hp.studyId()}});
            shoot("hydraulics3d_02_placed.png");
            ++H.step;
            return;
        }
        case 23:
            H.report["ok"] = true; H.done = true;
            H.capture = H.dir + "/hydraulics3d_03_final.png";
            return;
        default:
            return;
    }
}

bool hydraulicsShotEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    HydraulicsShot& H = g_hyd;
    if (!H.want) return false;
    ++st.frame;
    if (!H.capture.empty() && (!st.rpc.busy() || ++H.captureWait > 600)) {
        std::error_code ec;
        std::filesystem::create_directories(H.dir, ec);
        if (H.view3d && !H.failed && H.capture.find("3d_01_") != std::string::npos) {
            std::vector<std::array<float, 3>> points;
            for (const auto& c : st.simManager->hydraulic().circuit()["components"]) {
                std::array<float, 3> p;
                st.simManager->hydraulic().componentPosition3d(c, p.data());
                points.push_back(p);
            }
            auto check = sceneShotPixels(st, points);
            check["name"] = "component_glyphs_visible";
            check["ok"] = check.value("ok", false) && check.value("visible_patches", 0) >= 2 && !st.hasDocMesh;
            check["has_doc_mesh"] = st.hasDocMesh;
            H.report["checks"].push_back(check);
            if (!check.value("ok", false)) hydFail("glyphs_invisible", check.dump());
        }
        const bool last = H.done || (!H.view3d && H.step >= 16);
        writeBackbufferPng(H.capture.c_str(), dw, dh, last ? screenshotPath : nullptr);
        std::printf("hydraulics: screenshot -> %s (%dx%d)\n", H.capture.c_str(), dw, dh);
        std::fflush(stdout);
        H.report["captures"].push_back(H.capture);
        H.capture.clear();
        H.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (H.done && H.capture.empty() && H.queued.empty()) {
        const std::string path = H.dir + (H.view3d ? "/hydraulics3d_report.json" : "/hydraulics_report.json");
        std::ofstream out(path, std::ios::trunc);
        out << H.report.dump(2) << '\n';
        out.close();
        std::printf("hydraulics: report -> %s ok=%d\n", path.c_str(), (int)H.report.value("ok", false));
        std::fflush(stdout);
        if (H.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// ---------- simulation UI audit (CHUDWARE_SHOT=simaudit) ----------
// A UI correctness gate for the whole Simulate workspace. A 60 x 40 x 10 mm box part (plus, at the
// end, a two-part assembly for Motion) is driven through every simulation surface state via the
// public SimManager / PipingPanel / HydraulicPanel / EvaluateDialogs entry points — the same
// sim_* commands the ribbon dispatches: each study type's New Study and setup forms, the run
// panel and the Results nodes, the EM, Topology, Piping and Hydraulics panels (every form and
// the central Schematic / Plots / 3D views) and the Evaluate dialogs. Per state: wait until the
// engine and panels are idle, settle, run a ui::audit pass (ImGui test-engine item hooks see every
// item of every window: ID conflicts + horizontally clipped items, no pointer involved), capture
// scratch/simaudit/<nn>_<state>.png and rewrite scratch/simaudit/report.json {states:[{name,
// conflicts, overflows, items_seen, ...}], skipped, totals}. One "simaudit: <state> conflicts=N"
// line per state; the process exits non-zero when any ID conflict was found (or setup failed).
// CHUDWARE_SIMAUDIT_FRAMES: frames recorded per state (default 4); CHUDWARE_SIMAUDIT_MAX_FRAMES:
// per-state cap including ID-stack path resolution (default 240, 0 = unlimited).
enum class SaEnter { Done, Again, Skip };
struct SimAuditShot;
using SaEnterFn = std::function<SaEnter(AppState&, ui::SimManager&, SimAuditShot&)>;
using SaReadyFn = std::function<bool(AppState&, ui::SimManager&)>;

struct SaStep {
    std::string name;
    unsigned regions = 0;  // ui::audit regions reported on top of LeftPane | Floating
    SaEnterFn enter;       // Again = call next frame (multi-frame setup); Skip = reason in note
    SaReadyFn ready;       // null = engine + panels idle
    double timeoutS = 20.0;
};

// Shot-only navigation: observations select the real scrollable ancestor, while input remains
// mouse hover + wheel. Never mutate ImGui Scroll/ScrollTarget or application form state.
struct ShotNavigation {
    std::string target;
    ImGuiID window = 0;
    int ticks = 0, wheels = 0;
    float direction = -1.0f;
    nlohmann::json evidence = nlohmann::json::array();
};
ShotNavigation g_simNavigation;

bool shotLabel(const nlohmann::json& item, const char* label) {
    const auto& raw = item["label"].get_ref<const std::string&>();
    const size_t length = std::strlen(label);
    return raw == label || (raw.size() > length && raw.compare(0, length, label) == 0 &&
                           raw.compare(length, 2, "##") == 0);
}

const nlohmann::json* shotItem(const char* label, bool visibleOnly = true,
                               const char* window = nullptr) {
    for (const auto& item : ui::audit::observedItems()) {
        if (!item.value("enabled", true)) continue; // disabled controls are evidence, never pointer targets
        if (!shotLabel(item, label)) continue;
        const auto& name = item["window"].get_ref<const std::string&>();
        if (window && name.find(window) == std::string::npos) continue;
        if (visibleOnly) {
            if (!item.value("visible", false)) continue;
            const auto& r = item["rect"];
            if (r[2].get<float>() - r[0].get<float>() < 6.0f ||
                r[3].get<float>() - r[1].get<float>() < 6.0f) continue;
        }
        return &item;
    }
    return nullptr;
}

ImVec2 shotPoint(const nlohmann::json& item) {
    const auto& r = item["rect"];
    const float x = (r[0].get<float>() + r[2].get<float>()) * 0.5f;
    const float y = (r[1].get<float>() + r[3].get<float>()) * 0.5f;
    return ImVec2(x, y);
}

void shotScrollTo(const char* label, const char* window = nullptr, bool frameWholeItem = false) {
    auto& N = g_simNavigation;
    if (N.target != label) { N = ShotNavigation(); N.target = label; }
    if (N.wheels >= 96) return;  // Existing step deadlines report the bounded search failure.
    const auto* target = shotItem(label, false, window);
    const nlohmann::json* pane = nullptr;
    size_t depth = 0;
    for (const auto& w : ui::audit::observedWindows()) {
        if (!w.value("wheel_enabled", false) || w.value("scroll_max_y", 0.0f) <= 0.0f) continue;
        const auto& name = w["window"].get_ref<const std::string&>();
        if (target) {
            const auto& owner = (*target)["window"].get_ref<const std::string&>();
            if (owner != name && (owner.size() <= name.size() ||
                owner.compare(0, name.size(), name) != 0 || owner[name.size()] != '/')) continue;
        } else if (name.find("SimulationManager") == std::string::npos &&
                   name.find("Firmware") == std::string::npos &&
                   name.find("firmware") == std::string::npos) continue;
        // With a submitted target use its nearest scrolling ancestor. Without one, scan the
        // outer setup body rather than trapping wheel input inside a multiline JSON editor.
        if (!pane || (target ? name.size() > depth : name.size() < depth)) {
            pane = &w;
            depth = name.size();
        }
    }
    if (!pane) return;
    const ImGuiID id = (*pane)["id"].get<ImGuiID>();
    if (N.window != id) { N.window = id; N.direction = -1.0f; N.ticks = 0; }
    const float y = pane->value("scroll_y", 0.0f), max = pane->value("scroll_max_y", 0.0f);
    float wheel = N.direction;
    if (target) {
        const auto& r = (*target)["full_rect"];
        const auto& clip = (*pane)["rect"];
        N.direction = r[1].get<float>() < clip[1].get<float>() ? 1.0f : -1.0f;
        wheel = N.direction;
        if (frameWholeItem) {
            // Fine alignment still uses real wheel input, with the submitted ancestor's actual
            // ImGui wheel displacement. Whole-field capture must not oscillate in full wheel steps.
            const float step = pane->value("wheel_step_y", 0.0f);
            if (step <= 0.0f) return;
            const float delta = (r[1].get<float>() + r[3].get<float>() -
                                 clip[1].get<float>() - clip[3].get<float>()) * 0.5f;
            wheel = std::max(-1.0f, std::min(1.0f, -delta / step));
        }
    } else if (y >= max - 0.5f) wheel = N.direction = 1.0f;
    else if (y <= 0.5f) wheel = N.direction = -1.0f;
    // Hover the observed scrollbar, not an independently scrolling child JSON editor.
    const auto& wheelPoint = (*pane)["wheel_point"];
    const ImVec2 center(wheelPoint[0].get<float>(), wheelPoint[1].get<float>());
    ImGuiIO& io = ImGui::GetIO();
    // Hover on one frame before wheeling; ImGui routes the wheel using the hovered window.
    io.AddMousePosEvent(center.x, center.y);
    if (N.ticks++ % 4 != 1) return;
    io.AddMouseWheelEvent(0.0f, wheel);
    ++N.wheels;
    if (N.evidence.size() >= 64) N.evidence.erase(N.evidence.begin());
    N.evidence.push_back({{"target", label}, {"window", (*pane)["window"]},
                         {"scroll_y", y}, {"scroll_max_y", max}, {"wheel", wheel},
                         {"hover", {center.x, center.y}}});
}

nlohmann::json shotNavigationEvidence() {
    return {{"target", g_simNavigation.target}, {"wheel_events", g_simNavigation.wheels},
            {"scrolls", g_simNavigation.evidence}, {"windows", ui::audit::observedWindows()},
            {"items", ui::audit::observedItems()}};
}

// ---------- saved-demo review scenario (CHUDWARE_SHOT=demoreview) ----------
// Proves a saved part shows every study it carries. $CHUDWARE_SHOT_DOC is copied to
// scratch/demoreview_shots/demo_copy.step+ and the COPY opens through the File > Open request pump
// (st.requestOpenPath) with the FeatureManager roll-up persisted (CHUDWARE_FM_COLLAPSED). The selected
// root is the source demo's folder (in memory only); opening the copy from outside it must leave the
// root and plmProjectPath() unchanged. Simulate > Piping then opens normally: the Simulation tree must
// list every saved study from its own refresh and its rail column must not be clipped (captured
// before any direct adoption). The part is captured fitted in iso; then every hydraulic_circuit record is
// opened as its Simulation-tree row click does (HydraulicPanel::openStudyNamed) and captured on its
// Results/Outcome page with the Schematic, Plots and 3D Layout central views; the piping_network
// record opens the same way (PipingPanel::openStudyNamed) and is captured on Solve > Results
// coloured by Pressure, then Pump drive, Spec > Results and BOM; the vessel and duct records are
// captured on their Results pages; the Simulation tree's study rows, then the Model workspace,
// close the run. Stored numbers are checked
// on the adopted panel state. Every capture is preceded by a ui::audit pass (left pane, viewport,
// floating) whose ID conflicts, clipped items and rendered status lines are reported per capture.
// Nothing is saved. Captures + demoreview_report.json land in scratch/demoreview_shots; a failure
// exits non-zero.
struct DemoReviewShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, settle = 0, captureWait = 0, shotNo = 0, auditStage = 0;
    size_t circuit = 0;
    int pipingRecords = 0;  // saved non-hydraulic study records (piping/vessel/duct rows)
    double started = 0.0, stepStart = 0.0;
    std::string dir, copy, capture, shotName;
    std::string root;  // selected working folder: the source demo's own folder
    nlohmann::json report, studies;
    std::vector<std::string> circuits;  // hydraulic_circuit record names, in document order
    std::string network, vessel, duct;  // piping_network / vessel / duct_system record names
};
DemoReviewShot g_demo;

void demoFail(const std::string& code, const std::string& detail) {
    auto& D = g_demo;
    std::printf("demoreview: FAILED [%s] %s\n", code.c_str(), detail.c_str());
    std::fflush(stdout);
    D.report["ok"] = false;
    D.report["failure"] = {{"code", code}, {"detail", detail}, {"step", D.step}};
    D.failed = D.done = true;
    D.capture = D.dir + "/demoreview_FAILED.png";
}

bool demoCheck(const std::string& name, bool ok, const nlohmann::json& evidence) {
    g_demo.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    if (!ok) demoFail(name, evidence.dump());
    return ok;
}

std::string demoSlug(const std::string& name) {
    std::string s;
    for (char c : name) {
        if (std::isalnum((unsigned char)c)) s += (char)std::tolower((unsigned char)c);
        else if (!s.empty() && s.back() != '_') s += '_';
    }
    while (!s.empty() && s.back() == '_') s.pop_back();
    return s;
}

// The audit that precedes every capture: ID conflicts, clipped items and the status lines the pane
// rendered (kit::inlineStatus is observable text; cards and chips are not).
void demoAuditDone() {
    auto& D = g_demo;
    const nlohmann::json& res = ui::audit::lastResult();
    nlohmann::json overflows = nlohmann::json::array(), conflicts = nlohmann::json::array(),
                   status = nlohmann::json::array();
    for (const auto& o : res.value("overflows", nlohmann::json::array()))
        overflows.push_back({{"label", o.value("item_label", o.value("label", std::string()))},
                             {"window", o.value("window", std::string())},
                             {"overflow_px", o.value("overflow_px", 0.0)}});
    for (const auto& c : res.value("conflicts", nlohmann::json::array()))
        conflicts.push_back({{"label", c.value("item_label", c.value("label", std::string()))},
                             {"window", c.value("window", std::string())}, {"count", c.value("count", 0)}});
    for (const auto& item : ui::audit::observedItems())
        if (item.value("text_only", false) && item.value("visible", false) &&
            item["window"].get_ref<const std::string&>().find("LeftPane") != std::string::npos)
            status.push_back(item["label"]);
    const std::string file = std::filesystem::path(D.shotName).filename().string();
    std::printf("demoreview: %s conflicts=%zu overflows=%zu items_seen=%d\n", file.c_str(), conflicts.size(),
                overflows.size(), res.value("items_seen", 0));
    std::fflush(stdout);
    D.report["audits"].push_back({{"capture", D.shotName}, {"conflicts", conflicts}, {"overflows", overflows},
                                  {"pane_status", status}, {"items_seen", res.value("items_seen", 0)}});
}

constexpr int kDemoFinalStep = 43;

void demoReviewTick(AppState& st) {
    auto& D = g_demo;
    if (!D.init) {
        D.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        D.want = st.shot && mode && std::strcmp(mode, "demoreview") == 0;
        if (!D.want) return;
        const char* repo = std::getenv("CHUDWARE_REPO");
        D.dir = std::string(repo ? repo : ".") + "/scratch/demoreview_shots";
        D.copy = D.dir + "/demo_copy.step+";
        std::error_code ec;
        std::filesystem::create_directories(D.dir, ec);
        D.started = D.stepStart = pipingNow();
        D.report = {{"scenario", "demoreview"}, {"ok", false}, {"checks", nlohmann::json::array()},
                    {"audits", nlohmann::json::array()}, {"captures", nlohmann::json::array()}};
    }
    if (!D.want || D.done) return;
    ui::audit::observedItems();  // keeps item observation on while an audit records
    if (pipingNow() - D.started > 900 || pipingNow() - D.stepStart > 240) {
        demoFail("timeout", "scenario/step exceeded its wall-clock deadline"); return;
    }
    if (!st.workerStarted || st.frame < 5 || !D.capture.empty()) return;
    if (D.settle > 0) { --D.settle; return; }
    if (D.auditStage == 1) {
        ui::audit::AuditOptions o;
        o.regions = ui::audit::RegionLeftPane | ui::audit::RegionViewport | ui::audit::RegionFloating;
        if (!ui::audit::begin(std::filesystem::path(D.shotName).stem().string(), o)) {
            demoFail("audit", "ui::audit::begin refused"); return;
        }
        D.auditStage = 2;
        return;
    }
    if (D.auditStage == 2) {
        if (ui::audit::active()) return;  // the audit is recording; keep the state still
        D.auditStage = 0;
        demoAuditDone();
        if (D.failed) return;
        if (D.step == kDemoFinalStep) {  // the last capture ends the run (mirrored to --ui-screenshot)
            D.report["ok"] = true;
            D.done = true;
        }
        D.capture = D.shotName;
        return;
    }

    auto next = [&](int step) { D.step = step; D.stepStart = pipingNow(); };
    auto shoot = [&](const std::string& name) {
        char prefix[24];
        std::snprintf(prefix, sizeof prefix, "demoreview_%02d_", ++D.shotNo);
        D.shotName = D.dir + "/" + prefix + name + ".png";
        D.settle = 20;
        D.auditStage = 1;
    };
    auto check = demoCheck;
    // Iso orientation, scene fitted (zoom 1 = fit, pan reset), eased like the HUD presets.
    auto fitIso = [&] {
        static const float kZeroPan[3] = {0, 0, 0};
        ui::requestCameraAnimation(st, 35.0f, 30.0f, 1.0f, kZeroPan);
    };
    ui::SimManager* sm = st.simManager;
    auto idle = [&] {
        return sm && !st.rpc.busy() && !sm->hydraulic().busy() && !sm->piping().busy() &&
               !ui::cameraAnimating();
    };

    switch (D.step) {
    case 0: {  // copy the saved demo; open the COPY through the File > Open pump
        const char* doc = std::getenv("CHUDWARE_SHOT_DOC");
        if (!doc || !*doc) { demoFail("no_doc", "CHUDWARE_SHOT_DOC is not set"); return; }
        std::error_code ec;
        if (!std::filesystem::is_regular_file(doc, ec)) {
            demoFail("doc_missing", std::string("CHUDWARE_SHOT_DOC does not exist: ") + doc); return;
        }
        std::filesystem::copy_file(doc, D.copy, std::filesystem::copy_options::overwrite_existing, ec);
        if (ec) { demoFail("copy", ec.message()); return; }
        D.report["source_doc"] = doc;
        D.report["opened_copy"] = D.copy;
        // The selected root is the SOURCE demo's folder; the copy lives outside it (scratch), so
        // opening it must not move the root. Assigned in memory only — the shot never saves
        // settings, so the owner's persisted working folder is untouched.
        D.root = std::filesystem::path(doc).parent_path().string();
        st.workspaceDir = D.root;
        D.report["workspace_root"] = D.root;
        // Reproduce a persisted FeatureManager roll-up (config fm_collapsed:true): shot mode resets
        // the columns otherwise, and the rail workspaces must not inherit that collapse.
#ifdef _WIN32
        _putenv_s("CHUDWARE_FM_COLLAPSED", "1");
#else
        setenv("CHUDWARE_FM_COLLAPSED", "1", 1);
#endif
        st.requestOpenPath = D.copy;
        std::printf("demoreview: opening copy %s\n", D.copy.c_str());
        std::fflush(stdout);
        next(1);
        return;
    }
    case 1: {  // the copy is the active, meshed document
        const bool tabOk = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
            std::filesystem::path(st.docTabs[st.activeTab].path).filename() == "demo_copy.step+";
        if (!tabOk || !st.docMirror.hasDoc() || !st.hasDocMesh || !sm || st.rpc.busy()) return;
        if (!check("root_unchanged_by_outside_open",
                   st.workspaceDir == D.root && st.plmProjectPath() == D.root,
                   {{"root", D.root}, {"workspace_dir", st.workspaceDir},
                    {"plm_project_path", st.plmProjectPath()},
                    {"opened", st.docTabs[st.activeTab].path}}))
            return;
        st.activeWorkspace = AppState::Workspace::Model;
        st.leftPaneTab = AppState::LP_FeatureManager;
        fitIso();
        nlohmann::json list;
        std::string err;
        if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, list, err, 15000) ||
            !list.is_object()) {
            demoFail("study_list", err); return;
        }
        nlohmann::json found = nlohmann::json::array();
        for (const auto& s : list.value("studies", nlohmann::json::array())) {
            if (!s.is_object()) continue;
            const std::string type = s.value("type", std::string()), name = s.value("name", std::string());
            const nlohmann::json results = s.value("results", nlohmann::json::object());
            nlohmann::json keys = nlohmann::json::array();
            for (const auto& kv : results.items()) keys.push_back(kv.key());
            found.push_back({{"id", s.value("id", std::string())}, {"type", type}, {"name", name},
                             {"results", keys}});
            if (type == "hydraulic_circuit") D.circuits.push_back(name);
            else if (type == "piping_network" && D.network.empty()) D.network = name;
            else if (type == "vessel" && D.vessel.empty()) D.vessel = name;
            else if (type == "duct_system" && D.duct.empty()) D.duct = name;
            D.studies[name] = s;
        }
        D.report["studies_found"] = found;
        std::printf("demoreview: %zu studies (%zu hydraulic circuits)\n", found.size(), D.circuits.size());
        std::fflush(stdout);
        if (!check("studies_found", D.circuits.size() == 2 && !D.network.empty() && !D.vessel.empty() &&
                       !D.duct.empty(), found)) return;
        // The Simulation tree is NOT refreshed here: the rows must come from the normal open path
        // (SimManager::drawTreeBody's own refresh), proven in step 3.
        D.pipingRecords = (int)found.size() - (int)D.circuits.size();
        st.activeWorkspace = AppState::Workspace::Simulate;
        st.revealSimManager = true;
        st.ribbonTab = ui::kRibbonTabPiping;
        next(3);
        return;
    }
    case 3: {  // Simulate > Piping opened normally: the tree lists every saved study, unclipped
        if (!idle()) return;
        if (!check("simulation_tree_normal_open",
                   sm->hydraulic().studyCount() == (int)D.circuits.size() &&
                       sm->piping().studyCount() == D.pipingRecords,
                   {{"hydraulic_rows", sm->hydraulic().studyCount()},
                    {"piping_rows", sm->piping().studyCount()},
                    {"want_hydraulic", (int)D.circuits.size()}, {"want_piping", D.pipingRecords}}))
            return;
        const ImGuiWindow* w = ImGui::FindWindowByName("SimulationManager##LeftPane");
        const ui::OverlayBand& band = ui::overlayBand();
        const float bandH = band.maxY - band.minY;
        if (!check("simulation_column_not_clipped", w && w->Size.y > 0.5f * bandH,
                   {{"window_found", w != nullptr}, {"window_h", w ? w->Size.y : 0.0f},
                    {"band_h", bandH}}))
            return;
        shoot("simulate_tree_normal_open");
        next(4);
        return;
    }
    case 4:  // back to Model for the iso capture (its FeatureManager stays rolled up, as persisted)
        st.activeWorkspace = AppState::Workspace::Model;
        st.leftPaneTab = AppState::LP_FeatureManager;
        next(2);
        return;
    case 2:
        if (!idle()) return;
        shoot("model_iso");
        D.circuit = 0;
        next(10);
        return;

    // ---- Simulate > Hydraulics: every saved circuit -------------------------------------------
    case 10: {
        if (D.circuit >= D.circuits.size()) { next(20); return; }
        if (!idle()) return;
        const std::string& name = D.circuits[D.circuit];
        auto& hp = sm->hydraulic();
        if (!check("hyd_adopt_" + demoSlug(name), hp.openStudyNamed(st, name), {{"name", name}})) return;
        const std::string wantId = D.studies[name].value("id", std::string());
        if (!check("hyd_adopted_id_" + demoSlug(name), hp.studyId() == wantId,
                   {{"want", wantId}, {"got", hp.studyId()}})) return;
        hp.openResults(ui::HydraulicPanel::ResultPage::Outcome);
        hp.setCentralView(ui::HydraulicPanel::CentralView::Schematic);
        next(11);
        return;
    }
    case 11: {  // results present, numbers match the record, no error; schematic owns the view
        if (!idle()) return;
        auto& hp = sm->hydraulic();
        const std::string& name = D.circuits[D.circuit];
        const std::string slug = demoSlug(name);
        const auto& r = hp.simulateResult();
        const auto summary = r.is_object() ? r.value("summary", nlohmann::json::object()) : nlohmann::json::object();
        const size_t samples = r.is_object() ? r.value("time_s", nlohmann::json::array()).size() : 0;
        if (!check("hyd_results_" + slug, !summary.empty() && samples >= 2 && summary.contains("peak_pressure_bar"),
                   {{"samples", samples}, {"summary", summary}})) return;
        if (!check("hyd_no_error_" + slug, hp.lastError().empty(), {{"last_error", hp.lastError()}})) return;
        if (!check("hyd_schematic_" + slug, sm->centralViewActive(), {})) return;
        nlohmann::json numbers = {{"efficiency", summary.value("efficiency", 0.0)},
                                  {"peak_pressure_bar", summary.value("peak_pressure_bar", 0.0)}};
        if (name == "Test Stand Clamp Cylinder") {
            const double eff = summary.value("efficiency", 0.0);
            if (!check("hyd_metric_" + slug, std::fabs(eff - 0.199) < 0.001, numbers)) return;
        } else if (name == "Rotary Actuator Drive") {
            double rpm = 0.0;
            for (const auto& m : summary.value("motors", nlohmann::json::object()))
                rpm = m.value("steady_speed_rpm", 0.0);
            numbers["motor_steady_speed_rpm"] = rpm;
            if (!check("hyd_metric_" + slug, std::fabs(rpm - 261.7) < 0.1, numbers)) return;
        }
        D.report["hydraulic_numbers"][name] = numbers;
        shoot("hyd_" + slug + "_schematic");
        next(12);
        return;
    }
    case 12: {
        const std::string slug = demoSlug(D.circuits[D.circuit]);
        sm->hydraulic().setCentralView(ui::HydraulicPanel::CentralView::Plots);
        if (!check("hyd_plots_" + slug, sm->centralViewActive(), {})) return;
        shoot("hyd_" + slug + "_plots");
        next(13);
        return;
    }
    case 13: {  // 3D Layout: native viewport, hoses coloured by pressure from the stored run
        const std::string slug = demoSlug(D.circuits[D.circuit]);
        sm->handleCommand(st, "sim_hyd_view_3d");
        sm->hydraulic().openResults(ui::HydraulicPanel::ResultPage::Outcome);
        if (!check("hyd_3d_" + slug, !sm->centralViewActive() &&
                       sm->hydraulic().centralView() == ui::HydraulicPanel::CentralView::View3D, {})) return;
        next(14);
        return;
    }
    case 14: {
        if (!idle()) return;
        const std::string slug = demoSlug(D.circuits[D.circuit]);
        shoot("hyd_" + slug + "_3d");
        ++D.circuit;
        next(10);
        return;
    }

    // ---- Simulate > Piping: the network, then the vessel and the duct records -------------------
    case 20: {
        if (!idle()) return;
        sm->hydraulic().setCentralView(ui::HydraulicPanel::CentralView::View3D);
        sm->hydraulic().close();
        auto& pp = sm->piping();
        if (!check("pipe_adopt_network", pp.openStudyNamed(st, D.network), {{"name", D.network}})) return;
        if (!check("pipe_adopted_id", pp.networkStudyId() == D.studies[D.network].value("id", std::string()),
                   {{"got", pp.networkStudyId()}})) return;
        sm->handleCommand(st, "sim_piping_solve");
        pp.setPage(ui::PipingPanel::Form::Solve, 1);  // Results
        pp.setColorBy(1);                              // Colour pipes by: Pressure
        fitIso();
        next(21);
        return;
    }
    case 21: {
        if (!idle()) return;
        auto& pp = sm->piping();
        const auto& s = pp.solveResult();
        const auto links = s.is_object() ? s.value("links", nlohmann::json::array()) : nlohmann::json::array();
        if (!check("pipe_solve_results", s.is_object() && s.value("converged", false) && !links.empty(),
                   {{"converged", s.is_object() && s.value("converged", false)}, {"links", links.size()}})) return;
        if (!check("pipe_colour_pressure", pp.colorBy() == 1, {{"color_by", pp.colorBy()}})) return;
        if (!check("pipe_no_error", pp.lastError().empty(), {{"last_error", pp.lastError()}})) return;
        D.report["piping_numbers"]["solve_links"] = links.size();
        D.report["piping_numbers"]["solve_iterations"] = s.value("iterations", nlohmann::json());
        shoot("pipe_solve_pressure");
        next(22);
        return;
    }
    case 22: {
        auto& pp = sm->piping();
        if (!check("pipe_pump_drive", pp.pumpDrives().is_object() && !pp.pumpDrives().empty(),
                   {{"pump_drive", pp.pumpDrives()}})) return;
        pp.setPage(ui::PipingPanel::Form::Solve, 2);  // Pump drive
        shoot("pipe_pump_drive");
        next(23);
        return;
    }
    case 23: {
        auto& pp = sm->piping();
        sm->handleCommand(st, "sim_piping_spec");
        pp.setPage(ui::PipingPanel::Form::Spec, 1);  // Results
        const auto& s = pp.specResult();
        const auto runs = s.is_object() ? s.value("runs", nlohmann::json::array()) : nlohmann::json::array();
        if (!check("pipe_spec_results", !runs.empty(), {{"runs", runs.size()},
                   {"all_pass", s.is_object() ? s.value("all_pass", nlohmann::json()) : nlohmann::json()}})) return;
        D.report["piping_numbers"]["spec_runs"] = runs.size();
        D.report["piping_numbers"]["spec_all_pass"] = s.value("all_pass", nlohmann::json());
        shoot("pipe_spec");
        next(24);
        return;
    }
    case 24: {
        sm->handleCommand(st, "sim_piping_bom");
        const auto& b = sm->piping().bomResult();
        const auto items = b.is_object() ? b.value("items", nlohmann::json::array()) : nlohmann::json::array();
        if (!check("pipe_bom_results", !items.empty(), {{"items", items.size()}})) return;
        D.report["piping_numbers"]["bom_items"] = items.size();
        shoot("pipe_bom");
        next(32);
        return;
    }
    case 32:
        if (!idle()) return;
        if (!check("vessel_adopt", sm->piping().openStudyNamed(st, D.vessel), {{"name", D.vessel}})) return;
        next(33);
        return;
    case 33:
        if (!idle()) return;
        if (!check("vessel_no_error", sm->piping().lastError().empty(), {{"last_error", sm->piping().lastError()}})) return;
        shoot("vessel_" + demoSlug(D.vessel));
        next(34);
        return;
    case 34:
        if (!idle()) return;
        if (!check("duct_adopt", sm->piping().openStudyNamed(st, D.duct), {{"name", D.duct}})) return;
        next(35);
        return;
    case 35:
        if (!idle()) return;
        if (!check("duct_no_error", sm->piping().lastError().empty(), {{"last_error", sm->piping().lastError()}})) return;
        shoot("duct_" + demoSlug(D.duct));
        next(40);
        return;

    // ---- the Simulation tree lists every saved study; then the Model workspace ------------------
    case 40:
        if (!idle()) return;
        sm->piping().close();  // "Done": the pane returns to the study rows
        shoot("simulation_studies");
        next(41);
        return;
    case 41:
        st.activeWorkspace = AppState::Workspace::Model;
        st.shotWorkspacePin = -1;  // the shot pin restores the last non-Model workspace otherwise
        st.leftPaneTab = AppState::LP_FeatureManager;
        fitIso();
        next(42);
        return;
    case 42:
        if (!idle()) return;
        if (!check("model_workspace", st.activeWorkspace == AppState::Workspace::Model,
                   {{"workspace", (int)st.activeWorkspace}})) return;
        shoot("model_final");
        next(kDemoFinalStep);  // the audit branch above finishes the run with this capture
        return;
    default:
        return;
    }
}

bool demoReviewEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& D = g_demo;
    if (!D.want) return false;
    ++st.frame;
    if (!D.capture.empty() && (!st.rpc.busy() || ++D.captureWait > 600)) {
        std::error_code ec;
        std::filesystem::create_directories(D.dir, ec);
        writeBackbufferPng(D.capture.c_str(), dw, dh, D.done ? screenshotPath : nullptr);
        std::printf("demoreview: screenshot -> %s (%dx%d)\n", D.capture.c_str(), dw, dh);
        std::fflush(stdout);
        D.report["captures"].push_back(D.capture);
        D.capture.clear();
        D.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (D.done && D.capture.empty()) {
        const std::string path = D.dir + "/demoreview_report.json";
        D.report["seconds"] = pipingNow() - D.started;
        std::ofstream out(path, std::ios::trunc);
        out << D.report.dump(2) << '\n';
        out.close();
        std::printf("demoreview: report -> %s ok=%d\n", path.c_str(), (int)D.report.value("ok", false));
        std::fflush(stdout);
        if (D.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// ---------- CAD-file open scenario (CHUDWARE_SHOT=dwgopen) ----------
// Runtime smoke for any CAD import lane (here AutoCAD DWG/DXF ACIS solids). $CHUDWARE_SHOT_DOC is
// copied (extension kept) next to the --ui-screenshot path and the COPY opens through the File >
// Open request pump (st.requestOpenPath). Once the copy's tab is active and meshed and the async
// lane is idle, the report records every toast and [warn]/[err] log line the GUI received for the
// open (doc_open `warnings` arrive as Warn toasts + "[warn] [open]" lines; toasts stay visible in
// this shot), the world body list (asm_world_bodies names/bboxes joined with eval_mass volumes) and
// rendered-pixel evidence at the body centres. Captures: the fitted iso viewport + FeatureManager,
// then the document section and every collapsed FeatureManager row opened by real pointer clicks
// (a pointer shot: app.cpp ticks it after the platform backend). Fails by name when the document
// has no bodies or fewer than two solid bodies. Nothing is saved. Captures + dwgopen_report.json
// land beside the --ui-screenshot path (the last capture is mirrored to it); failure exits non-zero.
struct DwgOpenShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, settle = 0, captureWait = 0, shotNo = 0, auditStage = 0;
    int clickPhase = 0, clicks = 0, quiet = 0;
    bool docSection = false;  // the document section header was clicked open
    double started = 0.0, stepStart = 0.0;
    std::string dir, copy, capture, shotName;
    ImVec2 clickAt;
    std::vector<ImGuiID> clicked;
    std::vector<std::string> seenToasts, seenLog;
    nlohmann::json report;
};
DwgOpenShot g_dwg;

void dwgFail(const std::string& code, const nlohmann::json& evidence) {
    auto& D = g_dwg;
    std::printf("dwgopen: FAILED [%s] %s\n", code.c_str(), evidence.dump().c_str());
    std::fflush(stdout);
    D.report["ok"] = false;
    D.report["failure"] = {{"code", code}, {"evidence", evidence}, {"step", D.step}};
    D.failed = D.done = true;
    ImGui::GetIO().AddMouseButtonEvent(0, false);
    D.capture = D.dir + "/dwgopen_FAILED.png";
}

bool dwgCheck(const std::string& name, bool ok, const nlohmann::json& evidence) {
    g_dwg.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("dwgopen: %s %s\n", ok ? "PASS" : "FAIL", name.c_str());
    std::fflush(stdout);
    if (!ok) dwgFail(name, evidence);
    return ok;
}

// Every toast and every [warn]/[err] log line the GUI received since the open was requested.
// Toasts expire after a few seconds, so they are collected every frame, not at capture time.
void dwgCollectMessages(AppState& st) {
    auto& D = g_dwg;
    static const char* kLevel[] = {"info", "success", "warn", "error"};
    {
        std::lock_guard<std::mutex> lk(st.toastsMtx);
        for (const auto& t : st.toasts) {
            const std::string key = std::string(kLevel[(int)t.level]) + "|" + t.msg;
            if (std::find(D.seenToasts.begin(), D.seenToasts.end(), key) != D.seenToasts.end()) continue;
            D.seenToasts.push_back(key);
            D.report["toasts"].push_back({{"level", kLevel[(int)t.level]}, {"msg", t.msg}});
        }
    }
    std::lock_guard<std::mutex> lk(st.logMtx);
    for (const auto& line : st.log) {
        if (line.rfind("[warn]", 0) != 0 && line.rfind("[err]", 0) != 0) continue;
        if (std::find(D.seenLog.begin(), D.seenLog.end(), line) != D.seenLog.end()) continue;
        D.seenLog.push_back(line);
        D.report["log_warnings"].push_back(line);
        if (line.rfind("[warn] [open] ", 0) == 0) D.report["open_warnings"].push_back(line.substr(14));
    }
}

// The FeatureManager rows the capture's audit saw: label + open state, visible rows only.
nlohmann::json dwgTreeRows() {
    nlohmann::json rows = nlohmann::json::array();
    for (const auto& item : ui::audit::observedItems()) {
        if (!item.value("visible", false) ||
            item["window"].get_ref<const std::string&>().find("FeatureManager##LeftPane") == std::string::npos)
            continue;
        const std::string& label = item["label"].get_ref<const std::string&>();
        if (label.empty()) continue;
        rows.push_back({{"label", label}, {"text_only", item.value("text_only", false)}, {"rect", item["rect"]},
                        {"window", item["window"]}, {"openable", item.value("openable", false)},
                        {"opened", item.value("opened", false)}});
    }
    return rows;
}

constexpr int kDwgFinalStep = 9;

void dwgOpenTick(AppState& st) {
    auto& D = g_dwg;
    if (!D.init) {
        D.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        D.want = st.shot && mode && std::strcmp(mode, "dwgopen") == 0;
        if (!D.want) return;
        // The open's warnings are toasts: this shot records and shows them (shot mode hides
        // toasts by default for capture hygiene).
        st.suppressToasts = false;
        D.started = D.stepStart = pipingNow();
        D.report = {{"scenario", "dwgopen"}, {"ok", false}, {"checks", nlohmann::json::array()},
                    {"toasts", nlohmann::json::array()}, {"log_warnings", nlohmann::json::array()},
                    {"open_warnings", nlohmann::json::array()}, {"audits", nlohmann::json::array()},
                    {"captures", nlohmann::json::array()}};
    }
    if (!D.want || D.done || D.dir.empty()) return;  // dir comes from --ui-screenshot (end frame)
    ui::audit::observedItems();  // keeps per-frame item observation on (pointer shot)
    if (D.step >= 1) dwgCollectMessages(st);
    if (pipingNow() - D.started > 900 || pipingNow() - D.stepStart > 600) {
        dwgFail("timeout", {{"seconds", pipingNow() - D.started}}); return;
    }
    if (!st.workerStarted || st.frame < 5 || !D.capture.empty()) return;
    if (D.settle > 0) { --D.settle; return; }
    if (D.auditStage == 1) {
        ui::audit::AuditOptions o;
        o.regions = ui::audit::RegionLeftPane | ui::audit::RegionViewport | ui::audit::RegionFloating;
        if (!ui::audit::begin(std::filesystem::path(D.shotName).stem().string(), o)) {
            dwgFail("audit", {{"detail", "ui::audit::begin refused"}}); return;
        }
        D.auditStage = 2;
        return;
    }
    if (D.auditStage == 2) {
        if (ui::audit::active()) return;  // the audit is recording; keep the state still
        D.auditStage = 0;
        const nlohmann::json& res = ui::audit::lastResult();
        D.report["audits"].push_back({{"capture", D.shotName},
                                      {"conflicts", res.value("conflicts", nlohmann::json::array()).size()},
                                      {"overflows", res.value("overflows", nlohmann::json::array()).size()},
                                      {"tree_rows", dwgTreeRows()}});
        if (D.step == kDwgFinalStep) {  // the last capture ends the run (mirrored to --ui-screenshot)
            // Evidence (not a gate): the Solid Bodies folder and its body rows as the tree drew them.
            nlohmann::json folder = nullptr, rows = nlohmann::json::array();
            for (const auto& row : D.report["audits"].back()["tree_rows"]) {
                const std::string& label = row["label"].get_ref<const std::string&>();
                if (label.rfind("Solid Bodies (", 0) == 0) folder = label.substr(0, label.find("##"));
                else if (label.rfind("Body", 0) == 0 && label.find("##body") != std::string::npos)
                    rows.push_back(label.substr(0, label.find("##")));
            }
            D.report["tree_solid_bodies_folder"] = folder;
            D.report["tree_body_rows"] = rows;
            D.report["ok"] = true;
            D.done = true;
        }
        D.capture = D.shotName;
        return;
    }

    auto next = [&](int step) { D.step = step; D.stepStart = pipingNow(); };
    auto shoot = [&](const std::string& name) {
        char prefix[24];
        std::snprintf(prefix, sizeof prefix, "dwgopen_%02d_", ++D.shotNo);
        D.shotName = D.dir + "/" + prefix + name + ".png";
        D.settle = 20;
        D.auditStage = 1;
    };
    auto idle = [&] { return !st.rpc.busy() && st.rebuildsInFlight == 0 && !ui::cameraAnimating(); };

    switch (D.step) {
    case 0: {  // copy the source file; open the COPY through the File > Open pump
        const char* doc = std::getenv("CHUDWARE_SHOT_DOC");
        if (!doc || !*doc) { dwgFail("no_doc", {{"detail", "CHUDWARE_SHOT_DOC is not set"}}); return; }
        std::error_code ec;
        if (!std::filesystem::is_regular_file(doc, ec)) { dwgFail("doc_missing", {{"path", doc}}); return; }
        const std::filesystem::path src(doc);
        D.copy = D.dir + "/" + src.stem().string() + "_copy" + src.extension().string();
        std::filesystem::copy_file(src, D.copy, std::filesystem::copy_options::overwrite_existing, ec);
        if (ec) { dwgFail("copy", {{"detail", ec.message()}}); return; }
        D.report["source_doc"] = doc;
        D.report["opened_copy"] = D.copy;
        st.requestOpenPath = D.copy;
        std::printf("dwgopen: opening copy %s\n", D.copy.c_str());
        std::fflush(stdout);
        next(1);
        return;
    }
    case 1: {  // the copy is the active, meshed document and the async lane is idle
        const std::string want = std::filesystem::path(D.copy).filename().string();
        const bool tabOk = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
            std::filesystem::path(st.docTabs[st.activeTab].path).filename() == want;
        if (!tabOk || !st.docMirror.hasDoc() || !st.hasDocMesh || !idle()) return;
        const std::string docId = st.docMirror.docId();
        D.report["doc_id"] = docId;
        D.report["open_seconds"] = pipingNow() - D.stepStart;
        D.report["tab_title"] = st.docTabs[st.activeTab].title;
        D.report["renderer_bodies"] = (int)st.bodyVisible.size();
        nlohmann::json world, mass;
        std::string err;
        if (!st.worker.call("asm_world_bodies", {{"doc_id", docId}, {"explode", false}}, world, err, 60000) ||
            !world.is_object()) {
            dwgFail("world_bodies", {{"error", err}}); return;
        }
        std::string massErr;
        const bool massOk = st.worker.call("eval_mass", {{"doc_id", docId}}, mass, massErr, 60000) &&
                            mass.is_object();
        D.report["eval_mass"] = massOk ? mass : nlohmann::json{{"error", massErr}};
        // eval_mass explodes every compiled body into its solids (a solid-less body is one entry), in
        // the same compile order asm_world_bodies walks; join volumes onto the world list that way.
        nlohmann::json massBodies = nlohmann::json::array();
        if (massOk) {
            if (mass.contains("bodies") && mass["bodies"].is_array()) massBodies = mass["bodies"];
            else massBodies.push_back(mass);
        }
        size_t mi = 0;
        int solidBodies = 0, solids = 0;
        nlohmann::json bodies = nlohmann::json::array();
        std::vector<std::array<float, 3>> centres;
        for (const auto& b : world.value("bodies", nlohmann::json::array())) {
            if (!b.is_object()) continue;
            const auto ordinals = b.value("source_ordinals", nlohmann::json::array());
            const size_t n = ordinals.size();
            nlohmann::json row = {{"name", b.value("name", std::string())}, {"solids", n},
                                  {"bbox_size_mm", b.contains("bbox") && b["bbox"].is_object()
                                                       ? b["bbox"].value("size", nlohmann::json())
                                                       : nlohmann::json()}};
            double volume = 0.0, area = 0.0;
            bool surface = false, open = false;
            for (size_t k = 0; k < std::max<size_t>(n, 1) && mi < massBodies.size(); ++k, ++mi) {
                const auto& m = massBodies[mi];
                volume += m.value("volume_mm3", 0.0);
                area += m.value("area_mm2", 0.0);
                surface = surface || m.value("surface", false);
                open = open || !m.value("closed_solid", true);
            }
            row["volume_mm3"] = volume;
            if (area > 0.0) row["area_mm2"] = area;
            if (surface) row["surface"] = true;
            if (open) row["closed_solid"] = false;
            if (n > 0) { ++solidBodies; solids += (int)n; }
            if (b.contains("bbox") && b["bbox"].is_object()) {
                const auto lo = b["bbox"].value("min", nlohmann::json::array());
                const auto hi = b["bbox"].value("max", nlohmann::json::array());
                if (lo.size() == 3 && hi.size() == 3)
                    centres.push_back({(float)(0.5 * (lo[0].get<double>() + hi[0].get<double>())),
                                       (float)(0.5 * (lo[1].get<double>() + hi[1].get<double>())),
                                       (float)(0.5 * (lo[2].get<double>() + hi[2].get<double>()))});
            }
            bodies.push_back(std::move(row));
        }
        D.report["world_bodies"] = bodies;
        D.report["solid_bodies"] = solidBodies;
        D.report["solids"] = solids;
        D.report["body_centres"] = centres;
        std::printf("dwgopen: %zu world bodies (%d solid bodies, %d solids)\n", bodies.size(), solidBodies,
                    solids);
        std::fflush(stdout);
        if (!dwgCheck("bodies_present", !bodies.empty(), {{"world", world}})) return;
        if (!dwgCheck("at_least_two_solids", solidBodies >= 2, {{"world_bodies", bodies}})) return;
        st.activeWorkspace = AppState::Workspace::Model;
        st.leftPaneTab = AppState::LP_FeatureManager;
        static const float kZeroPan[3] = {0, 0, 0};
        ui::requestCameraAnimation(st, 35.0f, 30.0f, 1.0f, kZeroPan);  // fitted iso, eased like the HUD
        next(2);
        return;
    }
    case 2: {
        if (!idle()) return;
        std::vector<std::array<float, 3>> centres;
        for (const auto& c : D.report["body_centres"])
            centres.push_back({c[0].get<float>(), c[1].get<float>(), c[2].get<float>()});
        D.report["viewport_pixels"] = sceneShotPixels(st, centres);
        shoot("iso");
        next(3);
        return;
    }
    case 3: {  // expand the document section, then every collapsed FeatureManager row, by real clicks
        ImGuiIO& io = ImGui::GetIO();
        // Hover for two frames first: the section header's toggle allows overlap, and such an item
        // only takes a press once it was already the hovered item on the previous frame.
        if (D.clickPhase >= 1 && D.clickPhase <= 4) {
            io.AddMousePosEvent(D.clickAt.x, D.clickAt.y);
            if (D.clickPhase == 3) io.AddMouseButtonEvent(0, true);
            if (D.clickPhase == 4) io.AddMouseButtonEvent(0, false);
            ++D.clickPhase;
            return;
        }
        if (D.clickPhase >= 5 && D.clickPhase < 10) { ++D.clickPhase; return; }  // let the row open
        D.clickPhase = 0;
        if (!idle()) return;
        const nlohmann::json* target = nullptr;
        bool section = false;
        for (const auto& item : ui::audit::observedItems()) {
            if (!item.value("visible", false) || !item.value("enabled", true) ||
                item["window"].get_ref<const std::string&>().find("FeatureManager##LeftPane") == std::string::npos)
                continue;
            // The first section header is the document's (Solid Bodies folder, material...): a part
            // that opens with geometry seeds it collapsed. Its whole row is the toggle.
            if (!D.docSection && item["label"].get_ref<const std::string&>() == "##sec_toggle") {
                target = &item;
                section = true;
                break;
            }
            if (!D.docSection || !item.value("openable", false) || item.value("opened", false)) continue;
            const ImGuiID id = item.value("id", 0u);
            if (std::find(D.clicked.begin(), D.clicked.end(), id) != D.clicked.end()) continue;
            target = &item;
            break;
        }
        if (!target || D.clicks >= 32) {
            if (++D.quiet < 10) return;  // a just-opened row may reveal more collapsed rows
            D.report["expand_clicks"] = D.clicks;
            next(4);
            return;
        }
        D.quiet = 0;
        D.clicked.push_back(target->value("id", 0u));
        const auto& r = (*target)["rect"];
        // A tree row opens on its arrow at the row's left edge (a label click selects it).
        D.clickAt = section ? shotPoint(*target)
                            : ImVec2(r[0].get<float>() + ImGui::GetStyle().FramePadding.x + ImGui::GetFontSize() * 0.5f,
                                     0.5f * (r[1].get<float>() + r[3].get<float>()));
        D.report["expanded_rows"].push_back(
            {{"row", section ? nlohmann::json("document section") : (*target)["label"]},
             {"click", {D.clickAt.x, D.clickAt.y}}, {"rect", r}, {"window", (*target)["window"]}});
        D.docSection = true;
        io.AddMousePosEvent(D.clickAt.x, D.clickAt.y);
        ++D.clicks;
        D.clickPhase = 1;
        return;
    }
    case 4:
        if (!idle()) return;
        ImGui::GetIO().AddMousePosEvent(-FLT_MAX, -FLT_MAX);  // no hover highlight in the capture
        shoot("tree_expanded");
        next(kDwgFinalStep);  // the audit branch above finishes the run with this capture
        return;
    default:
        return;
    }
}

bool dwgOpenEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& D = g_dwg;
    if (!D.want) return false;
    ++st.frame;
    if (D.dir.empty()) {
        const std::filesystem::path shot = screenshotPath && *screenshotPath ? screenshotPath : "";
        const char* repo = std::getenv("CHUDWARE_REPO");
        D.dir = shot.has_parent_path() ? shot.parent_path().string()
                                       : std::string(repo ? repo : ".") + "/scratch/dwgopen_shots";
        std::error_code ec;
        std::filesystem::create_directories(D.dir, ec);
    }
    if (!D.capture.empty() && (!st.rpc.busy() || ++D.captureWait > 600)) {
        writeBackbufferPng(D.capture.c_str(), dw, dh, D.done ? screenshotPath : nullptr);
        std::printf("dwgopen: screenshot -> %s (%dx%d)\n", D.capture.c_str(), dw, dh);
        std::fflush(stdout);
        D.report["captures"].push_back(D.capture);
        D.capture.clear();
        D.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (D.done && D.capture.empty()) {
        const std::string path = D.dir + "/dwgopen_report.json";
        D.report["seconds"] = pipingNow() - D.started;
        std::ofstream out(path, std::ios::trunc);
        out << D.report.dump(2) << '\n';
        out.close();
        std::printf("dwgopen: report -> %s ok=%d\n", path.c_str(), (int)D.report.value("ok", false));
        std::fflush(stdout);
        if (D.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// ---------- project-scoped chat scenario (CHUDWARE_SHOT=projectchat) ----------
// The selected root (st.workspaceDir) is the sole chat authority. Seeds the shot's PRIVATE session
// store with an ME-490 session and a Demo-SpaceX session plus an index whose global `active` and
// polluted `active_by_project` (old builds keyed the on-screen session under the current root)
// both name the ME-490 one, then reloads that store under Demo-SpaceX the way startup does.
// Checks, each driven through the real frame loop (Copilot column + Sessions list):
//   startup: the active session reconciles to Demo-SpaceX's own; the rewritten index carries no
//     cross-project entry; Sessions shows Demo-SpaceX.
//   root A/B/C: with a (simulated) run live in Demo-SpaceX's session, selecting ME-490 restores
//     ME-490's session and parks the run untouched; an empty root opens a new session there;
//     returning restores the still-running Demo-SpaceX session; Sessions follows each root.
//   outside document: a part saved outside the root opens through File > Open and tab
//     switches between it and an in-root part never move the root, plmProjectPath() or the chat.
//   explicit pick: opening the ME-490 session from Sessions keeps it open under Demo-SpaceX
//     without claiming Demo-SpaceX's remembered slot; Sessions still shows the root.
// Root switches assign st.workspaceDir in memory only (no settings save). Refuses to run against
// a named CHUDWARE_SESSIONS_DIR. Captures + projectchat_report.json land in
// scratch/projectchat_shots; a failure exits non-zero.
struct ProjectChatShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, settle = 0, captureWait = 0, outsideTab = -1, insideTab = -1;
    size_t runMsgs = 0;
    double started = 0.0, stepStart = 0.0;
    std::string dir, rootA, rootB, rootC, outsidePath, keyA, keyB, keyC, capture, queued;
    nlohmann::json report;
};
ProjectChatShot g_pchat;

constexpr const char* kPcSessA = "pc_me490";
constexpr const char* kPcSessB = "pc_demo_spacex";

void pchatFail(const std::string& code, const nlohmann::json& evidence) {
    auto& P = g_pchat;
    std::printf("projectchat: FAILED [%s] %s\n", code.c_str(), evidence.dump().c_str());
    std::fflush(stdout);
    P.report["ok"] = false;
    P.report["failure"] = {{"code", code}, {"evidence", evidence}, {"step", P.step}};
    P.failed = P.done = true;
    P.queued.clear();
    P.capture = P.dir + "/projectchat_FAILED.png";
}

bool pchatCheck(const std::string& name, bool ok, const nlohmann::json& evidence) {
    g_pchat.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("projectchat: %s %s\n", ok ? "PASS" : "FAIL", name.c_str());
    std::fflush(stdout);
    if (!ok) pchatFail(name, evidence);
    return ok;
}

int pchatConvIndex(const AppState& st, const std::string& id) {
    for (int i = 0; i < (int)st.conversations.size(); ++i)
        if (st.conversations[i].id == id) return i;
    return -1;
}

std::string pchatActiveKey(const AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return std::string();
    return ui::convProjectKey(st.conversations[st.activeConv].projectDir);
}

// The index the store holds right now (forces a persist first).
nlohmann::json pchatActiveByProject(AppState& st) {
    ui::persistChatHistory(st);
    nlohmann::json index = nlohmann::json::object();
    std::ifstream in(ui::chatSessionsDir() + "/index.json");
    if (in) { try { in >> index; } catch (...) {} }
    return index.is_object() ? index.value("active_by_project", nlohmann::json::object())
                             : nlohmann::json::object();
}

void pchatShowSessions(AppState& st) {
    st.showTaskList = st.showArtifacts = st.showAISettingsOverlay = false;
    st.showSessionsPicker = true;
}

// Root + document + chat state, recorded with every root/document check.
nlohmann::json pchatState(AppState& st) {
    return {{"workspace_dir", st.workspaceDir}, {"plm_project_path", st.plmProjectPath()},
            {"conv_project_dir", ui::convProjectDir(st)}, {"active_session", st.activeConvId()},
            {"active_session_project_key", pchatActiveKey(st)},
            {"active_tab_path", (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                                    ? st.docTabs[st.activeTab].path : std::string()}};
}

void projectChatTick(AppState& st) {
    auto& P = g_pchat;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "projectchat") == 0;
        if (!P.want) return;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::error_code ec;
        P.dir = std::filesystem::absolute(std::string(repo ? repo : ".") + "/scratch/projectchat_shots", ec)
                    .lexically_normal().string();
        std::filesystem::remove_all(P.dir, ec);
        std::filesystem::create_directories(P.dir, ec);
        P.started = P.stepStart = pipingNow();
        P.report = {{"scenario", "projectchat"}, {"ok", false}, {"checks", nlohmann::json::array()},
                    {"captures", nlohmann::json::array()}};
    }
    if (!P.want || P.done) return;
    if (pipingNow() - P.started > 600 || pipingNow() - P.stepStart > 180) {
        pchatFail("timeout", {{"step", P.step}}); return;
    }
    if (!st.workerStarted || st.frame < 5 || !P.capture.empty()) return;
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) { P.capture = P.queued; P.queued.clear(); return; }

    enum { kSettle = 8 };
    auto next = [&](int step, int settle = kSettle) {
        P.step = step; P.settle = settle; P.stepStart = pipingNow();
    };
    auto shoot = [&](const char* name) {
        P.queued = P.dir + "/projectchat_" + name + ".png";
        P.settle = kSettle;
    };
    auto sessionsShow = [&](const std::string& key, const char* name) {
        const ui::SessionsScopeProbe probe = ui::sessionsScopeProbe();
        return pchatCheck(name, probe.drawn && probe.followedKey == key && probe.viewedKey == key,
                          {{"want_key", key}, {"drawn", probe.drawn},
                           {"followed_key", probe.followedKey}, {"viewed_key", probe.viewedKey}});
    };
    auto rootHolds = [&](const char* name) {
        return pchatCheck(name,
                          st.workspaceDir == P.rootB && st.plmProjectPath() == P.rootB &&
                              ui::convProjectDir(st) == P.rootB && st.activeConvId() == kPcSessB,
                          pchatState(st));
    };
    const int idxA = pchatConvIndex(st, kPcSessA), idxB = pchatConvIndex(st, kPcSessB);

    switch (P.step) {
    case 0: {  // seed the private store, then load it under Demo-SpaceX as startup does
        if (std::getenv("CHUDWARE_SESSIONS_DIR")) {
            pchatFail("named_session_store",
                      {{"reason", "projectchat rewrites its session store; unset CHUDWARE_SESSIONS_DIR"}});
            return;
        }
        P.rootA = P.dir + "/ME-490";
        P.rootB = P.dir + "/Demo-SpaceX";
        P.rootC = P.dir + "/Fresh-Project";
        P.outsidePath = P.dir + "/Outside/outside_bracket.step+";
        std::error_code ec;
        for (const auto& d : {P.rootA, P.rootB, P.rootC, P.dir + "/Outside"})
            std::filesystem::create_directories(d, ec);
        P.keyA = ui::convProjectKey(P.rootA);
        P.keyB = ui::convProjectKey(P.rootB);
        P.keyC = ui::convProjectKey(P.rootC);
        const std::string store = ui::chatSessionsDir();
        auto session = [&](const char* id, const char* title, const std::string& project,
                           const char* prompt, const char* reply) {
            const nlohmann::json js = {
                {"id", id}, {"title", title}, {"project", project},
                {"msgs", nlohmann::json::array({{{"u", true}, {"t", prompt}, {"ts", 1.7e9}},
                                                {{"u", false}, {"t", reply}, {"ts", 1.7e9 + 5}}})}};
            std::ofstream(store + "/" + id + ".json", std::ios::trunc) << js.dump(1);
        };
        session(kPcSessA, "ME-490 bracket revision", P.rootA, "Thicken the ME-490 bracket web",
                "The web is now 6 mm.");
        session(kPcSessB, "Demo-SpaceX HPU cooling", P.rootB, "Size the HPU cooling loop",
                "The loop needs 38 L/min.");
        const nlohmann::json index = {
            {"version", 3}, {"active", kPcSessA},
            {"active_by_project", {{P.keyA, kPcSessA}, {P.keyB, kPcSessA}}},
            {"order", nlohmann::json::array({kPcSessB, kPcSessA})}, {"session_seq", 2}};
        std::ofstream(store + "/index.json", std::ios::trunc) << index.dump(1);
        P.report["store"] = store;
        P.report["seeded_index"] = index;
        st.workspaceDir = P.rootB;   // the selected root (in memory only; settings are not saved)
        ui::loadChatHistory(st);
        P.report["loaded_active"] = st.activeConvId();
        std::printf("projectchat: store %s loaded under %s (restored %s)\n", store.c_str(),
                    P.rootB.c_str(), st.activeConvId().c_str());
        std::fflush(stdout);
        next(1);
        return;
    }
    case 1: {  // startup reconciled to the root; the index no longer lets ME-490 claim it
        if (!pchatCheck("startup_active_is_root_session",
                        idxA >= 0 && idxB >= 0 && st.activeConvId() == kPcSessB &&
                            pchatActiveKey(st) == P.keyB,
                        pchatState(st)))
            return;
        const nlohmann::json abp = pchatActiveByProject(st);
        if (!pchatCheck("index_active_by_project_clean",
                        abp.size() == 2 && abp.value(P.keyA, std::string()) == kPcSessA &&
                            abp.value(P.keyB, std::string()) == kPcSessB,
                        abp))
            return;
        pchatShowSessions(st);
        next(2);
        return;
    }
    case 2:
        if (!sessionsShow(P.keyB, "sessions_startup_shows_root")) return;
        shoot("01_startup_sessions");
        next(3, 0);
        return;
    case 3: {  // a live turn in Demo-SpaceX's session, then select ME-490 as the root
        ChatMsg user;
        user.fromUser = true;
        user.text = "Check the HPU return line velocity";
        ChatMsg reply;
        reply.fromUser = false;
        reply.running = true;
        reply.text = "Checking the return line\xe2\x80\xa6";
        st.chat.push_back(std::move(user));
        st.chat.push_back(std::move(reply));
        st.runFor(kPcSessB)->running.store(true);
        P.runMsgs = st.chat.size();
        st.workspaceDir = P.rootA;
        next(4);
        return;
    }
    case 4: {
        if (!pchatCheck("root_a_restores_its_session",
                        st.activeConvId() == kPcSessA && pchatActiveKey(st) == P.keyA, pchatState(st)))
            return;
        const bool parked = idxB >= 0 && st.convRunning(kPcSessB) &&
                            st.conversations[idxB].msgs.size() == P.runMsgs &&
                            st.conversations[idxB].msgs.back().running;
        if (!pchatCheck("run_parked_not_stopped", parked,
                        {{"running", st.convRunning(kPcSessB)},
                         {"parked_msgs", idxB >= 0 ? st.conversations[idxB].msgs.size() : 0},
                         {"want_msgs", P.runMsgs}}))
            return;
        pchatShowSessions(st);
        next(5);
        return;
    }
    case 5:
        if (!sessionsShow(P.keyA, "sessions_follow_root_a")) return;
        shoot("02_root_me490");
        st.workspaceDir = P.rootC;
        next(6);
        return;
    case 6: {  // a root with no sessions opens a new one there
        const bool fresh = st.activeConvId() != kPcSessA && st.activeConvId() != kPcSessB &&
                           pchatActiveKey(st) == P.keyC && st.chat.empty();
        if (!pchatCheck("empty_root_opens_new_session", fresh, pchatState(st))) return;
        st.workspaceDir = P.rootB;
        next(7);
        return;
    }
    case 7: {
        if (!pchatCheck("root_b_restores_running_session",
                        st.activeConvId() == kPcSessB && st.convRunning(kPcSessB) &&
                            st.chat.size() == P.runMsgs && st.chat.back().running,
                        pchatState(st)))
            return;
        const nlohmann::json abp = pchatActiveByProject(st);
        if (!pchatCheck("index_each_root_remembers_its_own",
                        abp.value(P.keyA, std::string()) == kPcSessA &&
                            abp.value(P.keyB, std::string()) == kPcSessB && !abp.contains(P.keyC),
                        abp))
            return;
        pchatShowSessions(st);
        next(8);
        return;
    }
    case 8:
        if (!sessionsShow(P.keyB, "sessions_follow_root_b")) return;
        shoot("03_root_demo_running");
        next(9, 0);
        return;
    case 9: {  // an in-root part, then a part saved OUTSIDE the root opened through File > Open
        // File > Open queues until every run is idle. The synthetic run above
        // has already proved parking/restoration; complete it before exercising
        // the normal file pump rather than bypassing that production guard.
        st.runFor(kPcSessB)->running.store(false);
        if (!st.chat.empty()) st.chat.back().running = false;
        auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
        nlohmann::json res, ap, sv, cl;
        std::string err;
        if (!st.worker.call("doc_new", {{"kind", "part"}, {"name", "Outside bracket"}}, res, err)) {
            pchatFail("outside_doc_new", {{"error", err}}); return;
        }
        const std::string did = res.value("doc_id", std::string());
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "Bracket"}, {"suppressed", false},
            {"params", {{"length", dim(40)}, {"width", dim(30)}, {"height", dim(8)}, {"centered", true}}},
            {"refs", nlohmann::json::object()}}}});
        if (!st.worker.call("doc_apply_ops", {{"doc_id", did}, {"base_revision", res.value("revision", 0)},
                                              {"ops", ops}}, ap, err) ||
            !st.worker.call("doc_save", {{"doc_id", did}, {"path", P.outsidePath}}, sv, err)) {
            pchatFail("outside_doc_save", {{"error", err}}); return;
        }
        st.worker.call("doc_close", {{"doc_id", did}}, cl, err);
        if (!st.docMirror.newPart(st.worker, err)) { pchatFail("inside_new_part", {{"error", err}}); return; }
        st.clearViewportForNewDoc();
        st.addDocTab(false);
        P.insideTab = st.activeTab;
        st.requestOpenPath = P.outsidePath;
        next(10, 0);
        return;
    }
    case 10: {  // the outside part is the active document; nothing about the root moved
        const bool open = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                          std::filesystem::path(st.docTabs[st.activeTab].path).filename() ==
                              "outside_bracket.step+";
        if (!open || !st.docMirror.hasDoc() || st.rpc.busy()) return;
        P.outsideTab = st.activeTab;
        if (!rootHolds("outside_open_keeps_root")) return;
        st.switchToTab(P.insideTab);
        next(11);
        return;
    }
    case 11:
        if (st.rpc.busy()) return;
        if (!rootHolds("inside_tab_select_keeps_root")) return;
        st.switchToTab(P.outsideTab);
        next(12);
        return;
    case 12:
        if (st.rpc.busy()) return;
        if (!rootHolds("outside_tab_select_keeps_root")) return;
        pchatShowSessions(st);
        next(13);
        return;
    case 13:
        if (!sessionsShow(P.keyB, "sessions_hold_root_with_outside_doc")) return;
        shoot("04_outside_document");
        next(14, 0);
        return;
    case 14:  // the user explicitly opens ME-490's session from the Sessions list
        if (idxA < 0) { pchatFail("me490_session_missing", pchatState(st)); return; }
        ui::switchConversation(st, idxA);
        next(15);
        return;
    case 15: {
        if (!pchatCheck("explicit_pick_stays_open",
                        st.activeConvId() == kPcSessA && st.workspaceDir == P.rootB, pchatState(st)))
            return;
        const nlohmann::json abp = pchatActiveByProject(st);
        if (!pchatCheck("explicit_pick_no_cross_project_claim",
                        abp.value(P.keyB, std::string()) == kPcSessB &&
                            abp.value(P.keyA, std::string()) == kPcSessA,
                        abp))
            return;
        pchatShowSessions(st);
        next(16);
        return;
    }
    case 16:
        if (!sessionsShow(P.keyB, "sessions_label_root_not_picked_project")) return;
        shoot("05_explicit_pick");
        next(17, 0);
        return;
    case 17:  // return to the root's session
        if (idxB >= 0) ui::switchConversation(st, idxB);
        P.report["ok"] = true;
        P.done = true;
        P.capture = P.dir + "/projectchat_06_final.png";
        return;
    default:
        return;
    }
}

bool projectChatEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& P = g_pchat;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty() && (!st.rpc.busy() || ++P.captureWait > 600)) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        std::printf("projectchat: screenshot -> %s (%dx%d)\n", P.capture.c_str(), dw, dh);
        std::fflush(stdout);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
        P.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (P.done && P.capture.empty()) {
        const std::string path = P.dir + "/projectchat_report.json";
        P.report["seconds"] = pipingNow() - P.started;
        std::ofstream out(path, std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        std::printf("projectchat: report -> %s ok=%d\n", path.c_str(), (int)P.report.value("ok", false));
        std::fflush(stdout);
        if (P.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// CHUDWARE_SHOT=steerstatus: chat-transcript positioning of mid-run user input. Seeds one live
// turn with a steer in every state, then drives the real event/settle paths: agent.steer
// delivered moves its bubble, late queued/failed/carried responses never move it back, and
// the run's end fails what it never confirmed. Resolved asks recover exact text-only replies
// on a real persistence/reload; ambiguous, unrelated, cross-turn and image replies stay honest.
// Live and restored pre-marker question cards both draw where the agent asked.
// Captures + steerstatus_report.json land in
// scratch/steerstatus_shots; a failure exits non-zero. Run with a private HOME/XDG_DATA_HOME
// (gate.sh shot_env) — the seeded chat is persisted like any other.
struct SteerStatusShot {
    bool init = false, want = false, done = false, failed = false;
    int step = 0, settle = 0;
    double started = 0.0;
    std::string dir, capture, queued;
    nlohmann::json report;
};
SteerStatusShot g_steer;

bool steerCheck(const std::string& name, bool ok, const nlohmann::json& evidence) {
    auto& P = g_steer;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("steerstatus: %s %s\n", ok ? "PASS" : "FAIL", name.c_str());
    std::fflush(stdout);
    if (!ok) {
        P.report["failure"] = name;
        P.failed = P.done = true;
        P.queued.clear();
        P.capture = P.dir + "/steerstatus_FAILED.png";
    }
    return ok;
}

int steerStateOf(const AppState& st, const char* id) {
    for (const auto& m : st.chat)
        if (m.fromUser && m.steerId == id) return m.steerState;
    return -1;
}

void steerStatusTick(AppState& st) {
    auto& P = g_steer;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "steerstatus") == 0;
        if (!P.want) return;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::error_code ec;
        P.dir = std::filesystem::absolute(std::string(repo ? repo : ".") + "/scratch/steerstatus_shots", ec)
                    .lexically_normal().string();
        std::filesystem::remove_all(P.dir, ec);
        std::filesystem::create_directories(P.dir, ec);
        P.started = pipingNow();
        P.report = {{"scenario", "steerstatus"}, {"ok", false}, {"checks", nlohmann::json::array()},
                    {"captures", nlohmann::json::array()}};
    }
    if (!P.want || P.done) return;
    if (pipingNow() - P.started > 120) { steerCheck("timeout", false, {{"step", P.step}}); return; }
    if (st.frame < 5 || !P.capture.empty()) return;
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) { P.capture = P.queued; P.queued.clear(); return; }

    auto user = [&](const char* text, const char* id, int state) {
        ChatMsg m;
        m.fromUser = true;
        m.text = text;
        m.steerId = id ? id : "";
        m.steerState = state;
        st.chat.push_back(std::move(m));
    };
    switch (P.step) {
    case 0: {
        st.chat.clear();
        user("Redesign the turret base for one MG996R.", nullptr, STEER_NONE);
        ChatMsg split;
        split.text = "Single shoulder servo; checking the carrier with an FEA ladder.";
        split.steerSplit = true;
        st.chat.push_back(std::move(split));
        user("Don't worry about FEA, just eyeball it.", "s-queued", STEER_QUEUED);
        user("Remove the dead space and add a roof.", "s-sent", STEER_SENDING);
        user("Make it out of PETG.", "s-carried", STEER_CARRIED);
        user("Mount the motor above it too.", "s-failed", STEER_FAILED);
        ChatMsg live;
        live.running = true;
        live.liveStatus = "Running write";
        st.chat.push_back(std::move(live));
        // The real delivery event, then the control-lane "queued" reply arriving late.
        const bool handled = ::feedSteerEvent(
            st.chat, "agent.steer", {{"steer_id", "s-sent"}, {"status", "delivered"}});
        const bool regressed = ::setSteerState(st.chat, "s-sent", STEER_QUEUED);
        if (!steerCheck("delivered_event_moves_its_bubble",
                        handled && !regressed && steerStateOf(st, "s-sent") == STEER_DELIVERED,
                        {{"state", steerStateOf(st, "s-sent")}, {"regressed", regressed}}))
            return;
        const bool lateFailed = ::setSteerState(st.chat, "s-sent", STEER_FAILED);
        const bool lateCarried = ::setSteerState(st.chat, "s-sent", STEER_CARRIED);
        if (!steerCheck("delivery_survives_late_failed_and_carried_control",
                        !lateFailed && !lateCarried &&
                            steerStateOf(st, "s-sent") == STEER_DELIVERED,
                        {{"state", steerStateOf(st, "s-sent")}}))
            return;
        ::feedSteerEvent(st.chat, "agent.steer",
                         {{"steer_id", "s-failed"}, {"status", "delivered"}});
        if (!steerCheck("late_delivery_recovers_previously_failed_bubble",
                        steerStateOf(st, "s-failed") == STEER_DELIVERED,
                        {{"state", steerStateOf(st, "s-failed")}}))
            return;
        if (!steerCheck("other_kinds_are_not_steer_events",
                        !::feedSteerEvent(st.chat, "agent.answer", nlohmann::json::object()),
                        nlohmann::json::object()))
            return;
        P.queued = P.dir + "/steerstatus_01_states.png";
        P.settle = 8;
        P.step = 1;
        return;
    }
    case 1: {
        st.chat.back().running = false;
        const bool changed = ::settleOpenSteers(st.chat);
        if (!steerCheck("run_end_fails_unconfirmed_steers",
                        changed && steerStateOf(st, "s-queued") == STEER_FAILED &&
                            steerStateOf(st, "s-sent") == STEER_DELIVERED &&
                            steerStateOf(st, "s-carried") == STEER_CARRIED,
                        {{"queued", steerStateOf(st, "s-queued")},
                         {"sent", steerStateOf(st, "s-sent")},
                         {"carried", steerStateOf(st, "s-carried")}}))
            return;
        P.queued = P.dir + "/steerstatus_02_settled.png";
        P.settle = 8;
        P.step = 2;
        return;
    }
    case 2: {
        // Question cards sit where the agent asked, not after the rest of the turn.
        st.chat.clear();
        auto tool = [](const char* name, const char* target) {
            ActivityItem a;
            a.rowKind = 3;
            a.title = name;
            a.reason = target;
            a.status = "done";
            return a;
        };
        auto batch = [](const char* id, const char* status) {
            return askBatchFromJson({
                {"batch_id", id}, {"status", status},
                {"prompt", "Shorten the upper-arm frame and lower the motor onto a new roof?"},
                {"questions", {{{"id", "layout"}, {"kind", "choice"},
                                {"prompt", "Shorten the upper-arm frame and lower the motor onto a new roof?"},
                                {"options", {"Compact the frame", "Keep 80 mm spacing"}}}}},
                {"answers", {{"layout", "Compact the frame"}}}});
        };
        user("Remove the dead space and add a roof.", nullptr, STEER_NONE);
        // A turn saved before markers existed: anchored on load after its ask_user row.
        ChatMsg saved;
        saved.activity = {tool("inspect_assembly", "6-axis"), tool("ask_user", "upper arm"),
                          tool("add_features", "upper_arm"), tool("add_features", "upper_arm"),
                          tool("verify_battery", "upper_arm"), tool("save_part", "upper_arm")};
        saved.asks.push_back(batch("b_saved", "answered"));
        saved.text = "Compacted the frame and added the roof.";
        anchorLegacyAsks(saved);
        const bool savedAnchored = saved.activity.size() == 7 && saved.activity[2].rowKind == 20 &&
                                   saved.activity[2].stage == ui::askMarkerStage("b_saved");
        st.chat.push_back(std::move(saved));
        // A live turn: the question event marks its position; later tool rows follow it.
        user("Also round the roof edges.", nullptr, STEER_NONE);
        ChatMsg live;
        live.running = true;
        live.activity = {tool("describe_part", "upper_arm")};
        st.chat.push_back(std::move(live));
        ChatMsg* active = &st.chat.back();
        ::feedAskEvent(st.chat, active, "agent.question",
                       {{"batch_id", "b_live"},
                        {"questions", {{{"id", "r"}, {"kind", "text"},
                                        {"prompt", "Fillet radius for the roof edges?"}}}}});
        active->activity.push_back(tool("add_features", "roof fillets"));
        const bool liveAnchored = active->activity.size() == 3 && active->activity[1].rowKind == 20 &&
                                  active->activity[1].stage == ui::askMarkerStage("b_live");
        if (!steerCheck("question_cards_sit_where_asked", savedAnchored && liveAnchored,
                        {{"saved", savedAnchored}, {"live", liveAnchored}}))
            return;
        P.settle = 8;
        P.step = 3;
        return;
    }
    case 3:
        P.queued = P.dir + "/steerstatus_03_questions.png";
        P.settle = 8;
        P.step = 4;
        return;
    case 4: {
        ui::ensureConversations(st);
        st.chat.clear();
        user("Design the camera mount.", nullptr, STEER_NONE);
        ChatMsg asked;
        asked.text = "Checking the mount; this thought must survive recovery.";
        asked.steerSplit = true;
        auto resolved = [](const char* id, const char* reply) {
            return askBatchFromJson({
                {"batch_id", id}, {"status", "reply"}, {"reply", reply},
                {"questions", {{{"id", "mount"}, {"kind", "text"},
                                {"prompt", "How will you mount it?"}}}}});
        };
        asked.asks.push_back(resolved("b-recover", "Use command strips outdoors."));
        asked.asks.push_back(resolved("b-images", "Use the four holes [Image #1]."));
        asked.asks.push_back(resolved("b-ambiguous", "PETG"));
        st.chat.push_back(std::move(asked));
        user("Use command strips outdoors.", "s-resolved", STEER_FAILED);
        user("Keep it stealthy.", "s-unconfirmed", STEER_QUEUED);
        user("Use the four holes [Image #1].", "s-image", STEER_FAILED);
        st.chat.back().imagePaths.push_back(P.dir + "/missing.png");
        user("PETG", "s-duplicate-a", STEER_FAILED);
        user("PETG", "s-duplicate-b", STEER_FAILED);
        user("A different turn.", nullptr, STEER_NONE);
        user("Use command strips outdoors.", "s-other-turn", STEER_FAILED);
        const size_t count = st.chat.size();
        ui::persistChatHistory(st);
        ui::loadChatHistory(st);
        if (!steerCheck("resolved_ask_recovers_saved_failed_text_reply",
                        steerStateOf(st, "s-resolved") == STEER_DELIVERED &&
                            st.chat.size() == count && st.chat[1].text ==
                                "Checking the mount; this thought must survive recovery.",
                        {{"state", steerStateOf(st, "s-resolved")}, {"messages", st.chat.size()}}))
            return;
        if (!steerCheck("reload_keeps_unknown_images_ambiguous_and_other_turn_unread",
                        steerStateOf(st, "s-unconfirmed") == STEER_FAILED &&
                            steerStateOf(st, "s-image") == STEER_FAILED &&
                            steerStateOf(st, "s-duplicate-a") == STEER_FAILED &&
                            steerStateOf(st, "s-duplicate-b") == STEER_FAILED &&
                            steerStateOf(st, "s-other-turn") == STEER_FAILED,
                        {{"unknown", steerStateOf(st, "s-unconfirmed")},
                         {"image", steerStateOf(st, "s-image")},
                         {"ambiguous_a", steerStateOf(st, "s-duplicate-a")},
                         {"ambiguous_b", steerStateOf(st, "s-duplicate-b")},
                         {"other_turn", steerStateOf(st, "s-other-turn")}}))
            return;
        // Fold the actual question-answer feed path, then the corresponding mapped
        // delivery event, with a failed control result arriving on either side.
        ::setSteerState(st.chat, "s-unconfirmed", STEER_FAILED);
        ::feedAskEvent(st.chat, &st.chat[1], "agent.question",
                       {{"batch_id", "b-live-reply"},
                        {"questions", {{{"id", "finish"}, {"kind", "text"},
                                        {"prompt", "What finish?"}}}}});
        ::feedAskEvent(st.chat, nullptr, "agent.answer",
                       {{"batch_id", "b-live-reply"}, {"source", "reply"},
                        {"reply", "Keep it stealthy."}});
        ::feedSteerEvent(st.chat, "agent.steer",
                         {{"steer_id", "s-unconfirmed"}, {"status", "delivered"}});
        const bool regressed = ::setSteerState(st.chat, "s-unconfirmed", STEER_FAILED);
        if (!steerCheck("consumption_evidence_wins_in_both_response_orders",
                        !regressed && steerStateOf(st, "s-unconfirmed") == STEER_DELIVERED &&
                            st.chat[1].asks.back().status == AskStatus::Reply,
                        {{"state", steerStateOf(st, "s-unconfirmed")}}))
            return;
        P.settle = 8;
        P.step = 5;
        return;
    }
    case 5:
        P.report["ok"] = true;
        P.done = true;
        P.capture = P.dir + "/steerstatus_04_recovered.png";
        return;
    default:
        return;
    }
}

bool steerStatusEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& P = g_steer;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        std::printf("steerstatus: screenshot -> %s (%dx%d)\n", P.capture.c_str(), dw, dh);
        std::fflush(stdout);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (P.done && P.capture.empty()) {
        const std::string path = P.dir + "/steerstatus_report.json";
        std::ofstream out(path, std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        std::printf("steerstatus: report -> %s ok=%d\n", path.c_str(), (int)P.report.value("ok", false));
        std::fflush(stdout);
        if (P.failed || !out) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// Real clipboard/keyboard proof, isolated by the caller's HOME/XDG paths.
struct ChatInlineShot {
    struct Key { ImGuiKey key; bool ctrl = false, shift = false; };
    bool want = false, init = false, done = false, failed = false, keyDown = false;
    int step = 0, settle = 0, keyIndex = 0;
    double started = 0;
    std::vector<Key> keys;
    std::vector<std::string> paths;
    std::string capture, convId;
    nlohmann::json report = {{"checks", nlohmann::json::array()}};
} g_chatInline;

bool chatInlineCheck(const char* name, bool ok, const nlohmann::json& evidence) {
    auto& P = g_chatInline;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("chat_inline: %s %s\n", ok ? "PASS" : "FAIL", name);
    std::fflush(stdout);
    if (!ok) {
        P.failed = P.done = true;
        P.capture = "scratch/chat_inline_shots/FAILED.png";
    }
    return ok;
}

void chatInlineTick(AppState& st) {
    auto& P = g_chatInline;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "chat_inline") == 0;
        P.started = pipingNow();
    }
    if (!P.want || P.done) return;
    if (pipingNow() - P.started > 120) {
        chatInlineCheck("timeout", false, {{"step", P.step}});
        return;
    }
    if (!st.workerStarted || st.frame < 5 || !P.capture.empty()) return;
    st.activeWorkspace = AppState::Workspace::AI;
    ui::audit::observedItems();  // enable the real-item hooks before the click proof
    if (P.settle > 0) { --P.settle; return; }
    ImGuiIO& io = ImGui::GetIO();
    if (P.keyIndex < (int)P.keys.size()) {
        const auto& key = P.keys[P.keyIndex];
        io.AddKeyEvent(ImGuiMod_Ctrl, !P.keyDown && key.ctrl);
        io.AddKeyEvent(ImGuiMod_Shift, !P.keyDown && key.shift);
        io.AddKeyEvent(key.key, !P.keyDown);
        P.keyDown = !P.keyDown;
        if (!P.keyDown) ++P.keyIndex;
        return;
    }
    auto keys = [&](std::vector<ChatInlineShot::Key> k) {
        P.keys = std::move(k); P.keyIndex = 0; P.keyDown = false; P.settle = 2;
    };
    auto next = [&](int step, int settle = 6) { P.step = step; P.settle = settle; };
    auto restoreImages = [&](const std::string& text) {
        st.pendingInlineImagePaths = P.paths;
        st.pendingAttachments.clear();
        ui::composerSetText(st, text);
    };
    switch (P.step) {
    case 0: {
        std::filesystem::create_directories("scratch/chat_inline_shots");
        glfwSetWindowSize(st.win, 1340, 1400);
        st.chat.clear();
        st.designStarted = true;
        ui::composerSetText(st, "Before SELECT After");
        if (!chatInlineCheck("clipboard_portrait_ready",
                std::system("xclip -selection clipboard -t image/png -i scratch/chat_inline_shots/portrait.png") == 0,
                nlohmann::json::object())) return;
        next(1);
        break;
    }
    case 1: {
        std::vector<ChatInlineShot::Key> k = {{ImGuiKey_Home, true}};
        for (int i = 0; i < 7; ++i) k.push_back({ImGuiKey_RightArrow});
        for (int i = 0; i < 6; ++i) k.push_back({ImGuiKey_RightArrow, false, true});
        k.push_back({ImGuiKey_V, true});
        keys(std::move(k)); next(2, 0);
        break;
    }
    case 2: {
        const auto text = ui::composerText(st);
        if (!chatInlineCheck("paste_replaces_mid_text_selection",
                             text == "Before [Image #1] After",
                             {{"text", text}})) return;
        io.AddInputCharactersUTF8("TAIL");
        next(3);
        break;
    }
    case 3: {
        const auto text = ui::composerText(st);
        if (!chatInlineCheck("caret_follows_pasted_reference",
                             text == "Before [Image #1]TAIL After", {{"text", text}})) return;
        if (!chatInlineCheck("clipboard_landscape_ready",
                std::system("xclip -selection clipboard -t image/png -i scratch/chat_inline_shots/landscape.png") == 0,
                nlohmann::json::object())) return;
        keys({{ImGuiKey_End, true}, {ImGuiKey_V, true}});
        next(4, 0);
        break;
    }
    case 4: {
        io.AddInputCharactersUTF8(" End");
        next(5);
        break;
    }
    case 5: {
        const auto text = ui::composerText(st);
        if (!chatInlineCheck("two_references_stay_at_paste_positions",
                    text == "Before [Image #1]TAIL After[Image #2] End", {{"text", text}})) return;
        P.paths = st.pendingInlineImagePaths;
        P.capture = "scratch/chat_inline_shots/composer.png";
        P.convId = st.activeConvId();
        ui::savePromptDraft(st, true);
        ChatMsg m; m.fromUser = true; m.text = text; m.imagePaths = P.paths;
        st.chat.push_back(std::move(m));
        ui::persistChatHistory(st);
        next(50);
        break;
    }
    case 50: {
        ui::newConversation(st);
        next(6);
        break;
    }
    case 6: {
        int index = -1;
        for (int i = 0; i < (int)st.conversations.size(); ++i)
            if (st.conversations[i].id == P.convId) index = i;
        if (!chatInlineCheck("original_conversation_exists", index >= 0,
                             {{"id", P.convId}})) return;
        ui::switchConversation(st, index);
        next(7);
        break;
    }
    case 7: {
        if (!chatInlineCheck("draft_restores_exact_image_roster",
                st.pendingInlineImagePaths == P.paths &&
                ui::composerText(st) == "Before [Image #1]TAIL After[Image #2] End",
                {{"text", ui::composerText(st)}, {"paths", st.pendingInlineImagePaths}})) return;
        std::string request = ui::composerText(st), bubble;
        const auto attachments = ui::consumeComposerAttachments(st, request, &bubble);
        if (!chatInlineCheck("send_preserves_order_without_duplicate_listing",
                attachments.size() == 2 && attachments[0].value("path", "") == P.paths[0] &&
                attachments[1].value("path", "") == P.paths[1] &&
                request == bubble && bubble == "Before [Image #1]TAIL After[Image #2] End",
                {{"request", request}, {"attachments", attachments}})) return;
        restoreImages("[Image #2]");
        next(8);
        break;
    }
    case 8: {
        std::string request = ui::composerText(st), bubble;
        const auto attachments = ui::consumeComposerAttachments(st, request, &bubble);
        if (!chatInlineCheck("deleted_reference_omitted_image_only_send_compacts_number",
                attachments.size() == 1 && attachments[0].value("path", "") == P.paths[1] &&
                request == "[Image #1]" && bubble == request,
                {{"request", request}, {"attachments", attachments}})) return;
        restoreImages("No images");
        next(9);
        break;
    }
    case 9: {
        std::string request = ui::composerText(st);
        const auto attachments = ui::consumeComposerAttachments(st, request);
        if (!chatInlineCheck("deleting_all_references_sends_no_images", attachments.empty(),
                             {{"attachments", attachments}})) return;
        restoreImages("[Image #2] then [Image #1]");
        next(10);
        break;
    }
    case 10: {
        std::string request = ui::composerText(st);
        const auto attachments = ui::consumeComposerAttachments(st, request);
        if (!chatInlineCheck("reordered_references_reorder_model_images",
                attachments.size() == 2 && attachments[0].value("path", "") == P.paths[1] &&
                attachments[1].value("path", "") == P.paths[0] &&
                request == "[Image #1] then [Image #2]",
                {{"request", request}, {"attachments", attachments}})) return;
        ui::composerSetText(st, "Unsent draft");
        ui::beginEditStage(st, 0);
        next(11);
        break;
    }
    case 11: {
        if (!chatInlineCheck("edit_restores_sent_references_and_exact_images",
                st.pendingInlineImagePaths == P.paths &&
                ui::composerText(st) == st.chat[0].text,
                {{"text", ui::composerText(st)}, {"paths", st.pendingInlineImagePaths}})) return;
        if (!chatInlineCheck("edit_cancel_restores_previous_draft",
                ui::cancelEditStage(st) && ui::composerText(st) == "Unsent draft" &&
                st.pendingInlineImagePaths.empty(),
                {{"text", ui::composerText(st)}})) return;
        ui::composerSetText(st, "");
        ui::persistChatHistory(st);
        ui::loadChatHistory(st);
        next(12);
        break;
    }
    case 12: {
        if (!chatInlineCheck("history_reopen_restores_inline_message",
                !st.chat.empty() && st.chat[0].imagePaths == P.paths &&
                st.chat[0].text == "Before [Image #1]TAIL After[Image #2] End",
                {{"messages", st.chat.size()}})) return;
        P.capture = "scratch/chat_inline_shots/transcript.png";
        next(13);
        break;
    }
    case 13: {
        const auto& items = ui::audit::observedItems();
        const auto item = std::find_if(items.begin(), items.end(), [](const auto& i) {
            return i.value("label", "") == "##attach_thumb" && i.value("visible", false) &&
                   i.value("window", "").find("##umsg") != std::string::npos;
        });
        if (item == items.end()) return;
        const auto& rect = (*item)["rect"];
        io.AddMousePosEvent((rect[0].template get<float>() + rect[2].template get<float>()) * 0.5f,
                            (rect[1].template get<float>() + rect[3].template get<float>()) * 0.5f);
        io.AddMouseButtonEvent(ImGuiMouseButton_Left, true);
        next(14, 0);
        break;
    }
    case 14:
        io.AddMouseButtonEvent(ImGuiMouseButton_Left, false);
        next(15);
        break;
    case 15: {
        const auto* viewer = ImGui::FindWindowByName("##copilot_image_lightbox");
        if (!chatInlineCheck("inline_image_click_opens_full_size_viewer",
                             viewer && viewer->WasActive, nlohmann::json::object())) return;
        P.capture = "scratch/chat_inline_shots/viewer.png";
        next(16, 0);
        break;
    }
    case 16:
        io.AddKeyEvent(ImGuiKey_Escape, true);
        next(160, 0);
        break;
    case 160:
        io.AddKeyEvent(ImGuiKey_Escape, false);
        next(17);
        break;
    case 17: {
        st.chat.clear();
        ChatMsg m;
        m.fromUser = true;
        m.text = "Legacy before\n[attached image: portrait.png]\nLegacy between\n"
                 "[attached image: landscape.png]\nLegacy after";
        m.imagePaths = P.paths;
        st.chat.push_back(std::move(m));
        next(18);
        break;
    }
    case 18:
        P.report["ok"] = true;
        P.capture = "scratch/chat_inline_shots/legacy.png";
        P.done = true;
        break;
    }
}

bool chatInlineEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& P = g_chatInline;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
    }
    glfwSwapBuffers(st.win);
    breakLoop = P.done;
    if (P.done) {
        std::ofstream("scratch/chat_inline_shots/report.json") << P.report.dump(2) << '\n';
        if (P.failed) { std::fflush(nullptr); std::_Exit(EXIT_FAILURE); }
    }
    return true;
}

// CHUDWARE_SHOT=agentlock: a real foreign write lands during an open human sketch.
// The post-checkpoint human feature and foreign feature must both survive the keep-exit.
struct AgentLockShot {
    bool init = false, want = false, done = false, failed = false, realForeign = false;
    int step = 0, settle = 0;
    double started = 0.0;
    std::string dir, capture, queued, doc, sketch, conv, viewed;
    nlohmann::json report;
};
AgentLockShot g_agentLock;

bool agentLockCheck(const char* name, bool ok, const nlohmann::json& evidence) {
    auto& P = g_agentLock;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("[agentlock-shot] %s %s %s\n", ok ? "PASS" : "FAIL", name, evidence.dump().c_str());
    std::fflush(stdout);
    if (!ok) {
        P.failed = P.done = true;
        P.report["failure"] = name;
        P.queued.clear();
        P.capture = P.dir + "/agentlock_FAILED.png";
    }
    return ok;
}

nlohmann::json agentLockSketchOp(const char* name) {
    return {{"op", "add_feature"}, {"feature", {
        {"type", "sketch"}, {"name", name}, {"refs", {{"plane", "plane.top"}}},
        {"params", {{"entities", nlohmann::json::array({
            {{"id", "circle1"}, {"type", "circle"}, {"center", {0.0, 0.0}}, {"radius", 4.0}}
        })}, {"constraints", nlohmann::json::array()}}}}}};
}

void agentLockTick(AppState& st) {
    auto& P = g_agentLock;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "agentlock") == 0;
        if (!P.want) return;
        const char* repo = std::getenv("CHUDWARE_REPO");
        P.dir = std::string(repo ? repo : ".") + "/scratch/agentlock_shots";
        std::error_code ec;
        std::filesystem::create_directories(P.dir, ec);
        P.started = pipingNow();
        P.report = {{"scenario", "agentlock"}, {"ok", false},
                    {"checks", nlohmann::json::array()}, {"captures", nlohmann::json::array()}};
    }
    if (!P.want || P.done) return;
    if (pipingNow() - P.started > 120.0) {
        agentLockCheck("timeout", false, {{"step", P.step}}); return;
    }
    if (!st.workerStarted || st.frame < 5 || !P.capture.empty() || st.rpc.busy()) return;
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) { P.capture = std::move(P.queued); P.queued.clear(); return; }
    auto shoot = [&](const char* name) {
        P.queued = P.dir + "/agentlock_" + name + ".png";
        P.settle = 4;
        ++P.step;
    };
    auto featureNamed = [&](const char* name) {
        const auto& tree = st.docMirror.tree();
        if (!tree.contains("features") || !tree["features"].is_array()) return false;
        for (const auto& feature : tree["features"])
            if (feature.value("name", std::string()) == name) return true;
        return false;
    };
    std::string err;
    switch (P.step) {
    case 0: {
        st.showRibbon = true;
        st.activeWorkspace = AppState::Workspace::Model;
        st.entitlementReadOnly = false;
        if (!agentLockCheck("new_part", st.docMirror.newPart(st.worker, err), {{"error", err}})) return;
        st.addDocTab(false);
        P.doc = st.docMirror.docId();
        const auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Lock test block"},
                {"params", {{"length", {{"value", 30.0}, {"expr", nullptr}}},
                            {"width", {{"value", 20.0}, {"expr", nullptr}}},
                            {"height", {{"value", 8.0}, {"expr", nullptr}}},
                            {"centered", true}}}}}},
            agentLockSketchOp("Human sketch")
        }));
        if (!agentLockCheck("build_part", r.ok, {{"error", r.error_message}})) return;
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        P.sketch = r.id_map.value("$1", std::string());
        if (!agentLockCheck("open_human_sketch",
                            !P.sketch.empty() && st.sketchMode->enterForFeature(st, P.sketch),
                            {{"sketch", P.sketch}})) return;
        // This feature is younger than the sketch's cancel checkpoint: a revert loses it.
        const auto human = st.docMirror.applyOps(st.worker,
            nlohmann::json::array({agentLockSketchOp("Human work after checkpoint")}));
        if (!agentLockCheck("human_work_after_checkpoint", human.ok,
                            {{"revision", human.revision}, {"error", human.error_message}})) return;
        ++P.step;
        P.settle = 4;
        return;
    }
    case 1: {
        const auto ops = nlohmann::json::array({agentLockSketchOp("Foreign agent feature")});
#ifndef __EMSCRIPTEN__
        if (engine::daemonMode()) {
            chudware_ipc::EngineClient foreign;
            chudware_ipc::EngineClient::Options options;
            options.clientKind = "mcp";
            options.allowSpawn = false;
            nlohmann::json result;
            bool ok = foreign.connectOrSpawn(options, err) &&
                foreign.call("attach_doc", {{"doc_id", P.doc}, {"mode", "rw"}}, result, err, 15000) &&
                foreign.call("doc_apply_ops",
                    {{"doc_id", P.doc}, {"base_revision", st.docMirror.revision()}, {"ops", ops}},
                    result, err, 15000);
            ok = ok && !result.contains("code") && result.contains("revision");
            if (!agentLockCheck("real_foreign_write", ok, {{"error", err}, {"result", result}})) return;
            P.realForeign = true;
        } else
#endif
        {
            const auto r = st.docMirror.applyOps(st.worker, ops);
            if (!agentLockCheck("foreign_write_without_daemon", r.ok,
                                {{"error", r.error_message}})) return;
            st.docTabs[st.activeTab].lastForeignWriteAt = std::chrono::steady_clock::now();
            P.report["foreign_event_skipped"] = "This runtime has no second native daemon connection";
        }
        ++P.step;
        P.settle = 4;
        return;
    }
    case 2: {
        if (!agentLockCheck("foreign_event_stamps_lock",
                            st.activeDocLockedByAgent() &&
                                st.docTabs[st.activeTab].lastForeignWriteAt !=
                                    std::chrono::steady_clock::time_point{},
                            {{"real_foreign_write", P.realForeign},
                             {"reason", st.agentLockReason(P.doc)}})) return;
        if (!agentLockCheck("session_closed_without_revert", !st.sketchMode->active() &&
                            st.docMirror.resync(st.worker) && featureNamed("Human sketch") &&
                            featureNamed("Human work after checkpoint") &&
                            featureNamed("Foreign agent feature"),
                            {{"sketch_active", st.sketchMode->active()},
                             {"tree", st.docMirror.tree()}})) return;
        shoot("01_external_locked");
        return;
    }
    case 3: {
        ImGuiWindow* banner = ImGui::FindWindowByName("##agent_lock_strip");
        if (!agentLockCheck("external_banner_reserved_chrome",
                            banner && banner->WasActive && ui::overlayBand().minY >=
                                banner->Pos.y + banner->Size.y,
                            {{"banner_visible", banner && banner->WasActive}})) return;
        ui::ensureConversations(st);
        P.viewed = st.activeConvId();
        P.conv = "agentlock-background";
        Conversation conv;
        conv.id = P.conv;
        conv.title = "Document editing agent";
        conv.projectDir = st.workspaceDir;
        ChatMsg live;
        live.running = true;
        live.text = "Editing this part";
        conv.msgs.push_back(std::move(live));
        st.conversations.push_back(std::move(conv));
        st.runFor(P.conv)->running.store(true);
        st.docTabs[st.activeTab].lastForeignWriteAt = std::chrono::steady_clock::now();
        shoot("02_in_app_locked");
        return;
    }
    case 4: {
        if (st.ribbonDispatch) st.ribbonDispatch("extrude_boss");
        if (!agentLockCheck("ribbon_mutation_refused", st.propertyManager &&
                            !st.propertyManager->active(), nlohmann::json::object())) return;
        const int revision = st.docMirror.revision();
        const auto undo = st.docUndo();
        if (!agentLockCheck("undo_refused", !undo.ok && undo.error_code == "agent_edit_locked" &&
                            st.docMirror.revision() == revision,
                            {{"error_code", undo.error_code}, {"revision", revision}})) return;
        // The synthetic run has no provider to cancel. Exercise the real named-stop UI path
        // without sending an empty run_id cancel to any actual worker.
        const bool workerStarted = st.workerStarted;
        st.workerStarted = false;
        ui::requestStopRunFor(st, P.conv);
        st.workerStarted = workerStarted;
        const auto* run = st.runIfExists(P.conv);
        const auto* msgs = conversationMsgs(st, P.conv);
        if (!agentLockCheck("stop_targets_background_conversation",
                            run && run->running.load() && run->stopRequested.load() &&
                                st.activeConvId() == P.viewed && msgs && !msgs->back().running &&
                                st.activeDocLockedByAgent(),
                            {{"viewed_conversation", st.activeConvId()},
                             {"stopped_conversation", P.conv}})) return;
        shoot("03_stopping");
        return;
    }
    case 5: {
        ImGuiWindow* banner = ImGui::FindWindowByName("##agent_lock_strip");
        if (!agentLockCheck("stopping_retains_banner_and_lock",
                            banner && banner->WasActive && st.activeDocLockedByAgent(),
                            nlohmann::json::object())) return;
        st.runFor(P.conv)->running.store(false);
        st.agentV2Running = st.anyRunActive();
        st.docTabs[st.activeTab].lastForeignWriteAt =
            std::chrono::steady_clock::now() - std::chrono::seconds(10);
        ++P.step;
        P.settle = 4;
        return;
    }
    case 6: {
        if (!agentLockCheck("controls_unlock_after_grace",
                            !st.activeDocLockedByAgent() && st.agentLockReason(P.doc).empty(),
                            {{"grace_s", AppState::kAgentLockGraceS}})) return;
        if (!agentLockCheck("human_can_edit_again", st.sketchMode->enterForFeature(st, P.sketch),
                            {{"sketch", P.sketch}})) return;
        st.sketchMode->exitSketch(st, true);
        const auto undo = st.docUndo();
        if (!agentLockCheck("undo_available_after_unlock", undo.ok,
                            {{"error", undo.error_message}})) return;
        const auto redo = st.docRedo();
        if (!agentLockCheck("redo_restores_agent_work", redo.ok &&
                            featureNamed("Foreign agent feature"),
                            {{"error", redo.error_message}})) return;
        shoot("04_unlocked");
        return;
    }
    default:
        P.report["ok"] = true;
        P.done = true;
        P.capture = P.dir + "/agentlock_final.png";
        return;
    }
}

bool agentLockEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    auto& P = g_agentLock;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (P.done) {
        const std::string path = P.dir + "/agentlock_report.json";
        std::ofstream out(path, std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        std::printf("[agentlock-shot] report %s %s\n", path.c_str(), P.report.dump().c_str());
        std::fflush(stdout);
        if (P.failed || !out) { std::fflush(nullptr); std::_Exit(EXIT_FAILURE); }
        breakLoop = true;
    }
    return true;
}

// CHUDWARE_SHOT=featurestate: native document authority through the actual PM and tab listener.
struct FeatureStateShot {
    bool init = false, want = false, done = false, failed = false, dragApplied = false;
    int step = 0, settle = 0, refreshRevision = -1;
    double started = 0.0;
    SceneShotInput input;
    std::string dir, path, feature, capture, queued;
    nlohmann::json report;
};
FeatureStateShot g_featureState;

bool featureStateCheck(const char* name, bool ok, const nlohmann::json& evidence) {
    auto& P = g_featureState;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("[featurestate-shot] %s %s %s\n", ok ? "PASS" : "FAIL", name,
                evidence.dump().c_str());
    std::fflush(stdout);
    if (!ok) {
        P.failed = P.done = true;
        P.report["failure"] = name;
        P.queued.clear();
        P.capture = P.dir + "/FAILED.png";
    }
    return ok;
}

void featureStateTick(AppState& st) {
    auto& P = g_featureState;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "featurestate") == 0;
        if (!P.want) return;
        const char* out = std::getenv("CHUDWARE_FEATURESTATE_OUT");
        P.dir = out ? out : "scratch/featurestate_shots";
        std::error_code ec;
        std::filesystem::create_directories(P.dir, ec);
        P.path = (std::filesystem::absolute(P.dir) / "qualification.step+").string();
        P.started = pipingNow();
        P.report = {{"scenario", "featurestate"}, {"ok", false},
                    {"checks", nlohmann::json::array()}, {"captures", nlohmann::json::array()}};
    }
    if (!P.want || P.done) return;
    if (pipingNow() - P.started > 120.0) {
        featureStateCheck("timeout", false, {{"step", P.step}}); return;
    }
    if (!st.workerStarted || st.frame < 5 || st.rpc.busy() || !P.capture.empty()) return;
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) { P.capture = std::move(P.queued); P.queued.clear(); return; }

    auto shoot = [&](const char* name) {
        P.queued = P.dir + "/" + name + ".png";
        P.settle = 4;
        ++P.step;
    };
    auto nativeState = [&]() {
        std::string error;
        const auto docs = st.docMirror.list(st.worker, error);
        for (const auto& doc : docs)
            if (doc.value("doc_id", std::string()) == st.docMirror.docId()) return doc;
        return nlohmann::json{{"error", error}};
    };
    auto stateMatches = [&](bool dirty) {
        const auto native = nativeState();
        return featureStateCheck(dirty ? "native_and_tab_dirty" : "native_and_tab_clean",
            native.contains("dirty") && native["dirty"] == dirty &&
            native.value("revision", -1) == st.docMirror.revision() &&
            st.docTabs[st.activeTab].dirty == dirty,
            {{"native", native}, {"tab_dirty", st.docTabs[st.activeTab].dirty},
             {"mirror_revision", st.docMirror.revision()}});
    };
    auto formValues = [&]() {
        nlohmann::json values = nlohmann::json::object();
        for (const auto& item : st.propertyManager->calloutItems(st)) values[item.label] = item.value;
        return values;
    };
    auto dimensions = [&]() {
        nlohmann::json values = nlohmann::json::object();
        for (const auto& feature : st.docMirror.tree()["features"]) {
            if (feature.value("id", std::string()) != P.feature) continue;
            for (const char* key : {"length", "width", "height"}) {
                const auto& value = feature["params"][key];
                values[key] = value.is_object() ? value["value"] : value;
            }
        }
        return values;
    };
    auto save = [&]() {
        st.docTabs[st.activeTab].path = P.path;
        const auto result = ui::saveDocTab(st, st.activeTab, false);
        return featureStateCheck("ordinary_tab_save", result == ui::TabSaveResult::Saved,
                                 {{"path", P.path}, {"result", (int)result}}) && stateMatches(false);
    };
    auto reopen = [&]() {
        std::string error;
        const bool opened = st.docMirror.open(st.worker, P.path, error);
        if (!featureStateCheck("saved_cache_reopen", opened &&
                st.docMirror.lastOpenedCacheHit() && st.docMirror.lastOpenedWarm(),
                {{"error", error}, {"cache_hit", st.docMirror.lastOpenedCacheHit()},
                 {"warm", st.docMirror.lastOpenedWarm()}})) return false;
        st.addDocTab(false);
        st.docTabs[st.activeTab].path = P.path;
        if (!st.docMirror.lastOpenedAicm().empty()) {
            const bool loaded = st.loadDocMesh(st.docMirror.lastOpenedAicm(), error);
            if (!featureStateCheck("cached_mesh_load", loaded, {{"error", error}})) return false;
        }
        return stateMatches(false);
    };
    std::string err;
    switch (P.step) {
    case 0: {
        st.activeWorkspace = AppState::Workspace::Model;
        st.settings.units = Units::MMGS;
        st.entitlementReadOnly = false;
        st.workspaceDir = std::filesystem::absolute(P.dir).string();
        const bool created = st.docMirror.newPart(st.worker, err);
        if (!featureStateCheck("new_part", created, {{"error", err}})) return;
        st.addDocTab(false);
        const auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Qualification box"},
                {"params", {{"length", 40}, {"width", 30.0}, {"height", 20}}},
                {"refs", nlohmann::json::object()}}}}}));
        P.feature = r.id_map.value("$0", std::string());
        if (!featureStateCheck("canonical_numeric_box", r.ok && !P.feature.empty(),
                               {{"response_tree", r.tree}, {"error", r.error_message}})) return;
        if (!stateMatches(true) || !save() || !reopen()) return;
        P.refreshRevision = st.docMirror.revision();
        if (!featureStateCheck("opened_revision_zero", P.refreshRevision == 0,
                               {{"revision", P.refreshRevision}})) return;
        shoot("01_open_clean");
        return;
    }
    case 1: {
        if (!featureStateCheck("open_actual_property_manager",
                st.propertyManager->openForEdit(st, P.feature), {{"feature", P.feature}})) return;
        const auto values = formValues();
        if (!featureStateCheck("canonical_dimensions_and_labels",
                values.size() == 3 && values.value("Length", "") == "40 mm" &&
                values.value("Width", "") == "30 mm" && values.value("Height", "") == "20 mm",
                {{"displayed", values}, {"canonical", dimensions()}})) return;
        const bool resynced = st.docMirror.resync(st.worker);
        if (!featureStateCheck("unchanged_tree_resync", resynced,
                               {{"revision", st.docMirror.revision()}}) || !stateMatches(false)) return;
        const auto snapshot = st.docMirror.snapshot();
        if (!featureStateCheck("unchanged_snapshot_restore", st.docMirror.restoreSnapshot(snapshot),
                               {{"revision", snapshot.revision}}) || !stateMatches(false)) return;
        std::string aicm;
        const bool activated = st.docMirror.activate(st.worker, snapshot.docId, aicm, err);
        if (!featureStateCheck("unchanged_activation",
                activated && st.docMirror.revision() == P.refreshRevision, {{"error", err}}) ||
            !stateMatches(false)) return;
        shoot("02_edit_40_30_20_clean");
        return;
    }
    case 2: {
        st.propertyManager->closeIfActive();
        st.settings.units = Units::IPS;
        if (!featureStateCheck("ips_edit_open", st.propertyManager->openForEdit(st, P.feature),
                               {{"feature", P.feature}})) return;
        const auto values = formValues();
        bool correct = values.size() == 3;
        for (const auto& item : {std::pair<const char*, double>{"Length", 40.0},
                                {"Width", 30.0}, {"Height", 20.0}}) {
            const auto value = values.value(item.first, std::string());
            correct = correct && value.find("in") != std::string::npos &&
                std::abs(std::strtod(value.c_str(), nullptr) - item.second / 25.4) < 0.001;
        }
        if (!featureStateCheck("canonical_mm_displayed_in_inches", correct, {{"displayed", values}}) ||
            !stateMatches(false)) return;
        st.propertyManager->closeIfActive();
        st.settings.units = Units::MMGS;
        if (!featureStateCheck("drag_form_open", st.propertyManager->openForEdit(st, P.feature),
                               {{"feature", P.feature}})) return;
        ++P.step;
        P.settle = 4;
        return;
    }
    case 3: {
        if (!P.dragApplied) {
            ImVec2 lo, hi;
            if (!ui::viewportClearRect(lo, hi)) {
                featureStateCheck("drag_viewport_available", false, {{"feature", P.feature}});
                return;
            }
            // A real viewport click releases the auto-focused dimension text editor
            // before its drag consumer runs; mutating the field under active text input
            // races ImGui's retained edit buffer rather than exercising a human gesture.
            if (!P.input.click(ImVec2(lo.x + (hi.x - lo.x) * 0.02f,
                                      lo.y + (hi.y - lo.y) * 0.12f))) return;
            if (!featureStateCheck("dimension_drag_consumer",
                    st.propertyManager->dragSetDim("length", 42.0), {{"feature", P.feature}})) return;
            P.dragApplied = true;
            P.settle = 4;
            return;
        }
        const auto dims = dimensions();
        if (!featureStateCheck("actual_pm_preview_keeps_other_dimensions",
                dims == nlohmann::json{{"length", 42.0}, {"width", 30.0}, {"height", 20.0}},
                {{"canonical", dims}, {"displayed", formValues()}}) || !stateMatches(true)) return;
        st.propertyManager->closeIfActive();
        if (!save()) return;
        // The engine commits outside this mirror, just as another human/agent writer does.
        nlohmann::json response;
        const bool edited = st.worker.call("doc_apply_ops", {{"doc_id", st.docMirror.docId()},
            {"base_revision", -1}, {"ops", nlohmann::json::array({
                {{"op", "edit_feature"}, {"id", P.feature}, {"params", {{"height", 25.0}}}}
            })}}, response, err);
        if (!featureStateCheck("out_of_band_native_edit",
                edited && !response.contains("code"), {{"error", err}})) return;
        core::DocMirror pushed;
        std::string aicm;
        const bool adopted = pushed.adoptActivateResponse(st.docMirror.docId(), response, aicm, err) &&
                             st.docMirror.restoreSnapshot(pushed.snapshot());
        if (!featureStateCheck("adopt_real_published_snapshot", adopted,
                {{"revision", response.value("revision", -1)}, {"error", err}}) ||
            !stateMatches(true)) return;
        if (!aicm.empty()) st.loadDocMesh(aicm, err);
        const auto human = st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op", "edit_feature"}, {"id", P.feature}, {"params", {{"width", 35.0}}}}
        }));
        if (!featureStateCheck("arrival_order_keeps_both_edits", human.ok &&
                dimensions() == nlohmann::json{{"length", 42.0}, {"width", 35.0}, {"height", 25.0}},
                {{"canonical", dimensions()}, {"error", human.error_message}})) return;
        const auto undo = st.docMirror.undo(st.worker);
        if (!featureStateCheck("undo_keeps_foreign_edit", undo.ok &&
                dimensions() == nlohmann::json{{"length", 42.0}, {"width", 30.0}, {"height", 25.0}},
                {{"canonical", dimensions()}, {"error", undo.error_message}}) || !stateMatches(true)) return;
        const auto redo = st.docMirror.redo(st.worker);
        if (!featureStateCheck("redo_keeps_foreign_edit", redo.ok &&
                dimensions() == nlohmann::json{{"length", 42.0}, {"width", 35.0}, {"height", 25.0}},
                {{"canonical", dimensions()}, {"error", redo.error_message}}) || !stateMatches(true)) return;
        if (!save() || !reopen()) return;
        const bool opened = st.propertyManager->openForEdit(st, P.feature);
        const auto displayed = formValues();
        if (!featureStateCheck("wrapped_dimensions_reopen", opened &&
                displayed == nlohmann::json{{"Length", "42 mm"}, {"Width", "35 mm"}, {"Height", "25 mm"}},
                {{"displayed", displayed}, {"canonical", dimensions()}})) return;
        shoot("03_shared_edits_saved_clean");
        return;
    }
    case 4: {
        st.propertyManager->closeIfActive();
        const auto edit = st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op", "edit_feature"}, {"id", P.feature},
             {"params", {{"length", {{"value", 46.0}, {"expr", "46"}}},
                         {"height", {{"value", 26.0}, {"expr", nullptr}}}}}}
        }));
        const bool opened = edit.ok && st.propertyManager->openForEdit(st, P.feature);
        const auto displayed = formValues();
        if (!featureStateCheck("expression_and_wrapped_dimensions_preserved", opened &&
                displayed == nlohmann::json{{"Length", "=46"}, {"Width", "35 mm"}, {"Height", "26 mm"}},
                {{"displayed", displayed}, {"canonical", dimensions()}, {"error", edit.error_message}}) ||
            !stateMatches(true)) return;
        P.report["ok"] = true;
        P.done = true;
        P.capture = P.dir + "/04_expression_preserved.png";
        return;
    }
    }
}

bool featureStateEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& done) {
    auto& P = g_featureState;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        P.report["captures"].push_back(P.capture);
        P.capture.clear();
    }
    glfwSwapBuffers(st.win);
    done = P.done;
    if (done) {
        std::ofstream out(P.dir + "/report.json", std::ios::trunc);
        out << P.report.dump(2) << '\n';
        out.close();
        if (!out || P.failed) { std::fflush(nullptr); std::_Exit(EXIT_FAILURE); }
    }
    return true;
}

struct SimAuditShot {
    bool init = false, want = false, done = false, failed = false, motionOk = false;
    int setup = 0, stage = 0, settle = 0, sub = 0, captureWait = 0, idleFrames = 0, shotNo = 0;
    int lastRibbonTab = -1, auditFrames = 4, maxFrames = 240, totalConflicts = 0, totalOverflows = 0;
    int motionStudiesBefore = 0;
    double waitStart = 0.0, subStart = 0.0, t0 = 0.0;
    size_t idx = 0;
    std::string dir, capture, queued, note, asmDoc;
    std::string topoStructId;  // the structural topology study (re-selected after topology_heat_flow)
    bool meshWait = false;  // the current mesh state's preview started (waits for the SimClient)
    int partTab = -1;
    std::string schematicDoc, boardDoc;
    ImVec2 inputPoint;
    const ui::SimManager* partSm = nullptr;  // the box part's manager (motion waits for the re-seat)
    nlohmann::json faces = nlohmann::json::object();
    nlohmann::json report;
    std::vector<SaStep> steps;
};
SimAuditShot g_sa;

// Ribbon tab indices of the per-type simulation tabs (widgets/ribbon.cpp kRibbonTabs[] 13..19).
constexpr int kSaTabStatic = 13, kSaTabModal = 14, kSaTabFlow = 15, kSaTabThermal = 16,
              kSaTabFatigue = 17, kSaTabMotion = 18, kSaTabEm = 19;
constexpr double kSaIdleTimeoutS = 20.0;

void saWriteReport(SimAuditShot& A) {
    A.report["total_conflicts"] = A.totalConflicts;
    A.report["total_overflows"] = A.totalOverflows;
    std::error_code ec;
    std::filesystem::create_directories(A.dir, ec);
    std::ofstream out(A.dir + "/report.json", std::ios::trunc);
    out << A.report.dump(2) << '\n';
}

void saFail(const std::string& code, const std::string& detail) {
    SimAuditShot& A = g_sa;
    std::printf("simaudit: FAILED [%s] %s\n", code.c_str(), detail.c_str());
    std::fflush(stdout);
    A.report["failure"] = {{"code", code}, {"detail", detail}};
    A.report["navigation_at_failure"] = shotNavigationEvidence();
    A.failed = true;
    A.done = true;
    A.queued.clear();
    A.capture = A.dir + "/simaudit_FAILED.png";
}

void saSkip(SimAuditShot& A, const std::string& name, const std::string& why) {
    A.report["skipped"].push_back({{"name", name}, {"reason", why}});
    std::printf("simaudit: %s SKIPPED (%s)\n", name.c_str(), why.c_str());
    std::fflush(stdout);
}

bool saIdle(AppState& st, ui::SimManager& sm) {
    return !st.rpc.busy() && !sm.piping().busy() && !sm.hydraulic().busy();
}

// The box face of `role` lands in st.selection exactly as a viewport click resolves it.
void saPick(AppState& st, const SimAuditShot& A, const char* role) {
    st.selection.clear();
    if (!A.faces.contains(role)) return;
    core::SelItem it;
    it.hit.body_id = 1;
    it.hit.kind = render::PickKind::Face;
    it.hit.topo_id = A.faces[role]["tid"].get<uint32_t>();
    it.selector = A.faces[role]["sel"].dump();
    it.pending_resolve = false;
    st.selection.mutableItems().push_back(it);
}

// The ribbon command on `tab`, re-asserting the tab (study commands keep whatever tab was up).
void saRun(AppState& st, ui::SimManager& sm, int tab, const char* cmd) {
    if (tab >= 0) st.ribbonTab = tab;
    sm.handleCommand(st, cmd);
    if (tab >= 0) st.ribbonTab = tab;
}

// Why the last Run was refused: SimManager::run only toasts + logs the gate ("[sim] run blocked:
// …" / "[motion] run blocked: …"), it never sets st.status. "Run refused: <reason>" from the
// newest such log line (the refusal that just happened logs one).
std::string saRunRefusal(AppState& st) {
    std::lock_guard<std::mutex> lk(st.logMtx);
    for (auto it = st.log.rbegin(); it != st.log.rend(); ++it) {
        const size_t p = it->find("run blocked:");
        if (p == std::string::npos) continue;
        const size_t b = it->find_first_not_of(' ', p + 12);
        return "Run refused: " + (b == std::string::npos ? std::string("(no reason logged)") : it->substr(b));
    }
    return "Run refused (no reason logged)";
}

SaStep saCmd(const char* name, int tab, const char* cmd, unsigned regions = 0) {
    SaStep s;
    s.name = name;
    s.regions = regions;
    s.enter = [tab, cmd](AppState& st, ui::SimManager& sm, SimAuditShot&) {
        saRun(st, sm, tab, cmd);
        return SaEnter::Done;
    };
    return s;
}


// Public commands open the form; real pointer input opens the expanded disclosure/palette.
// Explicit button disclosures supply their real opened label and frame the native JSON field;
// cards/popovers retain the semantic Openable/Opened proof from their completed-frame annotation.
SaStep saExpanded(const char* name, int tab, const char* cmd, const char* title, unsigned regions = 0,
                  const char* openedTitle = nullptr, const char* setupTitle = nullptr) {
    SaStep step;
    step.name = name;
    step.regions = regions;
    step.enter = [name, tab, cmd, title, openedTitle, setupTitle](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        ui::audit::observedItems();
        ImGuiIO& io = ImGui::GetIO();
        if (std::strncmp(name, "firmware_workspace", 18) == 0)
            st.activeWorkspace = AppState::Workspace::Firmware;
        if (A.sub == 0) {
            io.AddKeyEvent(ImGuiKey_Escape, true);
            g_simNavigation = ShotNavigation();
            A.subStart = pipingNow();
            if (A.report["expansions"].is_object()) A.report["expansions"].erase(name);
            if (cmd) saRun(st, sm, tab, cmd);
            A.sub = 1;
            // A palette can cover the parent's wheel target. Dismiss it with a real
            // click on the already selected ribbon tab before revealing the next control.
            if (tab >= 0 && ImGui::IsPopupOpen(nullptr,
                    ImGuiPopupFlags_AnyPopupId | ImGuiPopupFlags_AnyPopupLevel)) {
                const char* tabLabel = tab == kSaTabStatic ? "Static" : tab == kSaTabModal ? "Modal" :
                    tab == kSaTabFlow ? "Flow" : tab == kSaTabThermal ? "Thermal" :
                    tab == kSaTabFatigue ? "Fatigue" : tab == kSaTabMotion ? "Motion" :
                    tab == kSaTabEm ? "EM" : nullptr;
                if (const auto* control = tabLabel ? shotItem(tabLabel, true, "##RibbonBar") : nullptr) {
                    A.inputPoint = shotPoint(*control);
                    io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
                    io.AddMouseButtonEvent(0, true);
                    A.sub = -1;
                }
            }
            return SaEnter::Again;
        }
        if (A.sub == -1) {
            io.AddKeyEvent(ImGuiKey_Escape, false);
            io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
            io.AddMouseButtonEvent(0, false);
            A.sub = 1;
            return SaEnter::Again;
        }
        if (A.sub == -3) {
            const auto* control = shotItem(setupTitle);
            if (!control) { A.sub = 1; return SaEnter::Again; }
            A.inputPoint = shotPoint(*control);
            io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
            io.AddMouseButtonEvent(0, true);
            A.sub = -2;
            return SaEnter::Again;
        }
        if (A.sub == -2) {
            io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
            io.AddMouseButtonEvent(0, false);
            A.sub = 6;
            return SaEnter::Again;
        }
        if (pipingNow() - A.subStart > kSaIdleTimeoutS) {
            saFail(A.sub == 5 ? "expanded_editor_not_framed" : "expanded_target_missing",
                   std::string(name) + ": " + (A.sub == 5 ? "native JSON field" : title));
            return SaEnter::Done;
        }
        auto annotatedOpen = [&]() {
            const auto* item = shotItem(title);
            if (!item || !item->value("openable", false) || !item->value("opened", false)) return false;
            A.report["expansions"][name] = {{"control", *item}, {"proof", "Openable/Opened"}};
            return true;
        };
        if (A.sub == 1 || A.sub == 6) {
            io.AddKeyEvent(ImGuiKey_Escape, false);
            if (A.sub == 1 && setupTitle) {
                if (const auto* control = shotItem(setupTitle)) {
                    A.inputPoint = shotPoint(*control);
                    io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
                    A.sub = -3;
                    return SaEnter::Again;
                }
                shotScrollTo(setupTitle);
                return SaEnter::Again;
            }
            if (const auto* control = openedTitle ? shotItem(openedTitle, false) : nullptr) {
                A.report["expansions"][name]["control"] = *control;
                A.sub = 5;
                return SaEnter::Again;
            }
            if (annotatedOpen()) return SaEnter::Done;
            if (const auto* item = shotItem(title)) {
                A.inputPoint = shotPoint(*item);
                io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
                A.sub = 2;
                return SaEnter::Again;
            }
            shotScrollTo(openedTitle ? openedTitle : title);
            return SaEnter::Again;
        }
        if (A.sub == 2) {
            const auto* item = shotItem(title);
            if (!item) { A.sub = 1; return SaEnter::Again; }
            A.inputPoint = shotPoint(*item);
            io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
            io.AddMouseButtonEvent(0, true);
            A.sub = 3;
            return SaEnter::Again;
        }
        if (A.sub == 3) {
            io.AddMousePosEvent(A.inputPoint.x, A.inputPoint.y);
            io.AddMouseButtonEvent(0, false);
            A.sub = 4;
            return SaEnter::Again;
        }
        if (A.sub == 5) {
            const auto* control = shotItem(openedTitle, false);
            // The observed open disclosure may scroll offscreen while framing its
            // editor. Retain that proof, but require live field/apply rectangles.
            if (!control && A.report["expansions"].contains(name))
                control = &A.report["expansions"][name]["control"];
            if (!control) { shotScrollTo(openedTitle); return SaEnter::Again; }
            const auto& parent = (*control)["window"].get_ref<const std::string&>();
            // The native card can be below the submitted toggle and not drawn yet. Reveal it
            // before resolving its multiline field; don't accept an open button alone.
            const auto* apply = shotItem("Apply parameters", false, parent.c_str());
            if (!apply) { shotScrollTo("Apply parameters", parent.c_str()); return SaEnter::Again; }
            const auto& owner = (*apply)["window"].get_ref<const std::string&>();
            const auto* editor = shotItem("##t", false, owner.c_str());
            if (!editor) { shotScrollTo("Apply parameters", owner.c_str()); return SaEnter::Again; }
            const auto& full = (*editor)["full_rect"];
            const auto& clip = (*editor)["clip"];
            const bool framed = editor->value("visible", false) &&
                full[0].get<float>() >= clip[0].get<float>() - 1.0f &&
                full[1].get<float>() >= clip[1].get<float>() - 1.0f &&
                full[2].get<float>() <= clip[2].get<float>() + 1.0f &&
                full[3].get<float>() <= clip[3].get<float>() + 1.0f;
            if (!framed) { shotScrollTo("##t", owner.c_str(), true); return SaEnter::Again; }
            A.report["expansions"][name] = {{"control", *control}, {"editor", *editor},
                                           {"apply", *apply}, {"proof", "opened button and fully visible native editor"}};
            return SaEnter::Done;
        }
        if (const auto* control = openedTitle ? shotItem(openedTitle, false) : nullptr) {
            A.report["expansions"][name]["control"] = *control;
            A.sub = 5;
            return SaEnter::Again;
        }
        if (annotatedOpen()) return SaEnter::Done;
        // Observe the post-release disclosure state instead of assuming a click opened it.
        A.sub = 1;
        return SaEnter::Again;
    };
    return step;
}

SaStep saPickCmd(const char* name, int tab, const char* role, const char* cmd) {
    SaStep s;
    s.name = name;
    s.enter = [tab, role, cmd](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        saPick(st, A, role);
        saRun(st, sm, tab, cmd);
        st.selection.clear();
        return SaEnter::Done;
    };
    return s;
}

// A panel form opened by its ribbon command, then filled by the panel's own button entry point
// (an engine RPC); `act` returns false when the panel refused to start.
SaStep saAction(const char* name, int tab, const char* cmd,
                std::function<bool(AppState&, ui::SimManager&)> act, double timeoutS,
                unsigned regions = 0) {
    SaStep s;
    s.name = name;
    s.regions = regions;
    s.timeoutS = timeoutS;
    s.enter = [tab, cmd, act](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        if (cmd) saRun(st, sm, tab, cmd);
        if (!act(st, sm)) A.note = "action not started";
        return SaEnter::Done;
    };
    return s;
}

// Hydraulic circuit the Hydraulics states run on (the hydraulics scenario's power unit).
struct SaHydComp {
    const char* type;
    const char* name;
    double x, y;
    nlohmann::json params;
};
struct SaHydHose {
    const char* net;
    std::vector<std::pair<const char*, const char*>> ends;  // component name, port
    double lengthMm, idMm, ratingBar;
};
const std::vector<SaHydComp>& saHydComps() {
    static const std::vector<SaHydComp> k = {
        {"reservoir", "Tank", 0.0, -150.0, {{"volume_l", 60.0}}},
        {"pump", "Pump", 0.0, 0.0,
         {{"pump_type", "gear"}, {"displacement_cc_rev", 16.0}, {"speed_rpm", 1450.0},
          {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}}},
        {"valve_relief", "Relief", 180.0, 0.0, {{"set_pressure_bar", 160.0}}},
        {"valve_directional", "DCV 4/3", 0.0, 160.0,
         {{"ways", 4}, {"positions", 3}, {"center_condition", "tandem"}, {"actuator", "solenoid"},
          {"response_ms", 30.0}}},
        {"cylinder_linear", "Lift cylinder", 0.0, 320.0,
         {{"actuator_type", "double_acting"}, {"bore_mm", 50.0}, {"rod_mm", 28.0},
          {"stroke_mm", 300.0}, {"piston_mass_kg", 5.0}}},
    };
    return k;
}
const std::vector<SaHydHose>& saHydHoses() {
    static const std::vector<SaHydHose> k = {
        {"Pressure", {{"Pump", "P"}, {"Relief", "P"}, {"DCV 4/3", "P"}}, 800.0, 12.7, 280.0},
        {"Relief return", {{"Relief", "T"}, {"Tank", "T"}}, 600.0, 19.0, 40.0},
        {"Return", {{"DCV 4/3", "T"}, {"Tank", "T2"}}, 1200.0, 19.0, 40.0},
        {"Suction", {{"Tank", "S"}, {"Pump", "T"}}, 400.0, 25.4, 10.0},
        {"Cylinder A", {{"DCV 4/3", "A"}, {"Lift cylinder", "A"}}, 1500.0, 12.7, 280.0},
        {"Cylinder B", {{"DCV 4/3", "B"}, {"Lift cylinder", "B"}}, 1500.0, 12.7, 280.0},
    };
    return k;
}

// Base + arm parts, an assembly with the arm on a distance mate (the motion scenario's rig).
// Synchronous worker calls; "" on success, else the reason.
std::string saBuildMotionAssembly(AppState& st, SimAuditShot& A) {
    auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
    auto makePart = [&](const char* name, double l, double w, double h, const std::string& path) -> std::string {
        nlohmann::json res, ap, sv, cl;
        std::string err;
        if (!st.worker.call("doc_new", {{"kind", "part"}, {"name", name}}, res, err)) return "doc_new: " + err;
        const std::string did = res.value("doc_id", std::string());
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", name}, {"suppressed", false},
            {"params", {{"length", dim(l)}, {"width", dim(w)}, {"height", dim(h)}, {"centered", true}}},
            {"refs", nlohmann::json::object()}}}});
        if (!st.worker.call("doc_apply_ops", {{"doc_id", did}, {"base_revision", res.value("revision", 0)}, {"ops", ops}},
                            ap, err))
            return "doc_apply_ops: " + err;
        if (!st.worker.call("doc_save", {{"doc_id", did}, {"path", path}}, sv, err)) return "doc_save: " + err;
        st.worker.call("doc_close", {{"doc_id", did}}, cl, err);
        return "";
    };
    const std::string basePath = A.dir + "/simaudit_motion_base.step+";
    const std::string armPath = A.dir + "/simaudit_motion_arm.step+";
    std::string why = makePart("Base", 60, 40, 10, basePath);
    if (why.empty()) why = makePart("Arm", 10, 10, 50, armPath);
    if (!why.empty()) return why;
    std::string err;
    if (!st.docMirror.newAssembly(st.worker, err)) return "new_assembly: " + err;
    // Seat the assembly's tab + DocSession now, as the tree toolbar's New Assembly does. Without
    // it the mate's synchronous mesh load below parked the assembly mesh on the PART's session
    // (the tab only appeared on the next frame's ensureActiveTab, with an empty cache), so Motion's
    // Run stayed refused with "Assembly geometry is still loading".
    st.clearViewportForNewDoc();
    st.addDocTab(/*isAssembly=*/true);
    A.asmDoc = st.docMirror.docId();
    const nlohmann::json off = nlohmann::json::array({1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 60, 0, 0, 0, 1});
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", basePath}}}, {"name", "Ground"}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", armPath}}}, {"name", "Arm"}, {"fixed", false},
         {"transform", off}},
    }));
    const std::string k1 = r.id_map.value("$0", std::string()), k2 = r.id_map.value("$1", std::string());
    if (!r.ok || k1.empty() || k2.empty()) return "component insert: " + r.error_message;
    const nlohmann::json refs = nlohmann::json::array({
        {{"component", k1}, {"selector", {{"kind", "face"}, {"centroid", nlohmann::json::array({0.0, 0.0, 5.0})}}}},
        {{"component", k2}, {"selector", {{"kind", "face"}, {"centroid", nlohmann::json::array({0.0, 0.0, 25.0})}}}},
    });
    auto m = st.docMirror.applyOps(st.worker, nlohmann::json::array({{
        {"op", "asm_add_mate"},
        {"mate", {{"type", "distance"}, {"value", dim(60.0)}, {"align", "aligned"}, {"suppressed", false}, {"refs", refs}}},
    }}));
    if (!m.ok || m.id_map.value("$0", std::string()).empty()) return "distance mate: " + m.error_message;
    // The newest tessellation (the mate's, else the insert's) becomes the assembly's viewport mesh;
    // without one the pushed doc.changed refresh loads it (motion_run waits for it, bounded).
    const std::string& aicm = !m.aicm_path.empty() ? m.aicm_path : r.aicm_path;
    std::string le;
    if (!aicm.empty() && !st.loadDocMesh(aicm, le)) return "assembly mesh: " + le;
    return "";
}

void saBuildSteps(SimAuditShot& A) {
    using HP = ui::HydraulicPanel;
    const unsigned VP = ui::audit::RegionViewport;
    const int topoTab = AppState::RT_Topology;
    const int pipeTab = ui::kRibbonTabPiping;
    const int hydTab = ui::kRibbonTabHydraulics;
    std::vector<SaStep>& S = A.steps;
    // sim_mesh meshes + shows the wireframe only when the SimClient is free (a still-running
    // solve keeps it busy), so the state waits for the SimClient only when the preview started.
    auto mesh = [&](const char* name, int tab) {
        SaStep s;
        s.name = name;
        s.timeoutS = 30.0;
        s.enter = [tab](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
            A.meshWait = !(st.simClient && st.simClient->busy());
            saRun(st, sm, tab, "sim_mesh");
            return SaEnter::Done;
        };
        s.ready = [](AppState& st, ui::SimManager& sm) {
            return saIdle(st, sm) && !(g_sa.meshWait && st.simClient && st.simClient->busy());
        };
        S.push_back(std::move(s));
    };
    auto custom = [&](const char* name, unsigned regions, SaEnterFn enter, SaReadyFn ready = nullptr,
                      double timeoutS = kSaIdleTimeoutS) {
        SaStep s;
        s.name = name;
        s.regions = regions;
        s.enter = std::move(enter);
        s.ready = std::move(ready);
        s.timeoutS = timeoutS;
        S.push_back(std::move(s));
    };

    // ---- Simulation Manager + Static ----
    custom("sim_manager_empty", VP, [](AppState& st, ui::SimManager&, SimAuditShot&) {
        st.ribbonTab = kSaTabStatic;
        return SaEnter::Done;
    });
    custom("new_study_picker", 0, [](AppState& st, ui::SimManager& sm, SimAuditShot&) {
        st.ribbonTab = kSaTabStatic;
        sm.shotForcePicker = true;  // the "+ New Study" picker, held open
        return SaEnter::Done;
    });
    S.push_back(saCmd("static_new", kSaTabStatic, "sim_new_static"));
    custom("static_material", 0, [](AppState& st, ui::SimManager& sm, SimAuditShot&) {
        saRun(st, sm, kSaTabStatic, "sim_material");
        sm.configureActive(st, 0.2f, "steel_1018");  // persisted before the run state
        return SaEnter::Done;
    });
    S.push_back(saPickCmd("static_fixture", kSaTabStatic, "min_x", "sim_fixture"));
    S.push_back(saPickCmd("static_force", kSaTabStatic, "max_x", "sim_load_force"));
    S.push_back(saPickCmd("static_pressure", kSaTabStatic, "max_z", "sim_load_pressure"));
    mesh("static_mesh", kSaTabStatic);
    S.push_back(saExpanded("static_mesh_advanced", kSaTabStatic, "sim_mesh", "Advanced"));
    S.push_back(saExpanded("static_mesh_adaptive", kSaTabStatic, nullptr, "Adaptive refinement"));
    S.push_back(saCmd("static_complete_parameters", kSaTabStatic, "sim_parameters"));
    // The Run page is captured WHILE the solve is in flight (progress + Cancel); the result lands
    // during static_results, which waits for the colormap.
    custom("static_run", 0,
           [](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
               saRun(st, sm, kSaTabStatic, "sim_run");
               if (!sm.runUi().running && !st.colormapOn)
                   A.note = saRunRefusal(st);
               return SaEnter::Done;
           },
           [](AppState& st, ui::SimManager& sm) {
               return sm.runUi().running || st.colormapOn || !g_sa.note.empty();
           }, 25.0);
    custom("static_results", VP,
           [](AppState& st, ui::SimManager& sm, SimAuditShot&) {
               saRun(st, sm, kSaTabStatic, "sim_results");
               return SaEnter::Done;
           },
           [](AppState& st, ui::SimManager&) { return st.colormapOn; }, 25.0);

    // ---- Modal ----
    S.push_back(saCmd("modal_new", kSaTabModal, "sim_new_modal"));
    S.push_back(saCmd("modal_material", kSaTabModal, "sim_material"));
    S.push_back(saPickCmd("modal_fixture", kSaTabModal, "min_x", "sim_fixture"));
    S.push_back(saCmd("modal_frequencies", kSaTabModal, "sim_modal_options"));
    mesh("modal_mesh", kSaTabModal);
    S.push_back(saCmd("modal_complete_parameters", kSaTabModal, "sim_parameters"));
    S.push_back(saCmd("modal_results", kSaTabModal, "sim_results"));

    // ---- Flow ----
    S.push_back(saCmd("flow_new", kSaTabFlow, "sim_new_flow"));
    S.push_back(saCmd("flow_fluid", kSaTabFlow, "sim_fluid"));
    S.push_back(saExpanded("flow_fluid_palette", kSaTabFlow, "sim_fluid", "Fluid presets"));
    S.push_back(saExpanded("flow_surface_palette", kSaTabFlow, "sim_fluid", "Surface presets"));
    S.push_back(saCmd("flow_boundaries", kSaTabFlow, "sim_flow_bc"));
    S.push_back(saCmd("flow_resolution", kSaTabFlow, "sim_flow_res"));
    S.push_back(saCmd("flow_complete_parameters", kSaTabFlow, "sim_parameters"));
    S.push_back(saCmd("flow_results", kSaTabFlow, "sim_results"));

    // ---- Thermal (the Thermal tab is Thermal FEA; Thermal CFD is reachable only from the picker) ----
    S.push_back(saCmd("thermal_new", kSaTabThermal, "sim_new_thermal_fea"));
    S.push_back(saPickCmd("thermal_fixed_temp", kSaTabThermal, "min_x", "sim_fixture"));
    S.push_back(saPickCmd("thermal_heat_loads", kSaTabThermal, "max_x", "sim_load_heat"));
    S.push_back(saCmd("thermal_fea_complete_parameters", kSaTabThermal, "sim_parameters"));
    S.push_back(saCmd("thermal_results", kSaTabThermal, "sim_results"));
    S.push_back(saCmd("thermal_cfd_new", kSaTabThermal, "sim_new_thermal"));
    S.push_back(saCmd("thermal_cfd_boundaries", kSaTabThermal, "sim_flow_bc"));
    S.push_back(saCmd("thermal_cfd_complete_parameters", kSaTabThermal, "sim_parameters"));

    // ---- Fatigue ----
    S.push_back(saCmd("fatigue_new", kSaTabFatigue, "sim_new_fatigue"));
    S.push_back(saCmd("fatigue_material", kSaTabFatigue, "sim_material"));
    S.push_back(saCmd("fatigue_loading_sn", kSaTabFatigue, "sim_fatigue_setup"));
    mesh("fatigue_mesh", kSaTabFatigue);
    S.push_back(saCmd("fatigue_complete_parameters", kSaTabFatigue, "sim_parameters"));
    S.push_back(saCmd("fatigue_results", kSaTabFatigue, "sim_results"));

    // ---- study kinds reached from the "+ New Study" picker ----
    S.push_back(saCmd("buckling_new", kSaTabStatic, "sim_new_buckling"));
    S.push_back(saCmd("buckling_complete_parameters", kSaTabStatic, "sim_parameters"));
    S.push_back(saCmd("contact_new", kSaTabStatic, "sim_new_contact"));
    S.push_back(saCmd("contact_complete_parameters", kSaTabStatic, "sim_parameters"));
    S.push_back(saCmd("freq_response_new", kSaTabModal, "sim_new_freqresp"));
    S.push_back(saCmd("freq_response_complete_parameters", kSaTabModal, "sim_parameters"));

    // ---- EM (the first command probes the em_* backend, so it is issued again once answered) ----
    custom("em_eigenmode", 0, [](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        if (A.sub == 0) {
            saRun(st, sm, kSaTabEm, "sim_em_eigen");
            A.sub = 1;
            A.subStart = pipingNow();
            return SaEnter::Again;
        }
        if (st.rpc.busy() && pipingNow() - A.subStart < kSaIdleTimeoutS) return SaEnter::Again;
        saRun(st, sm, kSaTabEm, "sim_em_eigen");
        return SaEnter::Done;
    });
    // Time domain first, then the driven sweep the setup focus commands act on: each of ports /
    // boundaries / range then scrolls a different card of the same driven form into view.
    S.push_back(saCmd("em_time_domain", kSaTabEm, "sim_em_time"));
    S.push_back(saExpanded("em_time_exact_parameters", kSaTabEm, "sim_em_time", "Advanced parameters"));
    S.push_back(saCmd("em_freq_sweep", kSaTabEm, "sim_em_sweep"));
    S.push_back(saExpanded("em_waveguide_palette", kSaTabEm, "sim_em_sweep", "Standard waveguide palette"));
    S.push_back(saExpanded("em_sweep_exact_parameters", kSaTabEm, "sim_em_sweep", "Advanced parameters"));
    S.push_back(saCmd("em_ports", kSaTabEm, "sim_em_ports"));
    S.push_back(saCmd("em_boundaries", kSaTabEm, "sim_em_boundary"));
    S.push_back(saCmd("em_freq_range", kSaTabEm, "sim_em_range"));
    S.push_back(saCmd("em_results", kSaTabEm, "sim_em_results"));

    // ---- Topology (the capability matrix is fetched first; nothing is assumed) ----
    custom("topology_new", 0, [topoTab](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        using State = ui::topology::Capabilities::State;
        if (A.sub == 0) {
            st.ribbonTab = topoTab;
            A.sub = 1;
            A.subStart = pipingNow();
            return SaEnter::Again;
        }
        if (sm.topologyCapabilities().state == State::Loading && pipingNow() - A.subStart < 30.0)
            return SaEnter::Again;
        if (st.rpc.busy() && pipingNow() - A.subStart < 30.0) return SaEnter::Again;
        saRun(st, sm, topoTab, "sim_new_topology");
        A.topoStructId = sm.activeStudyId();
        if (sm.topologyCapabilities().state != State::Ready)
            A.note = "topology capabilities not ready: " + sm.topologyCapabilities().error;
        return SaEnter::Done;
    });
    S.push_back(saCmd("topology_method", topoTab, "sim_topology_setup"));
    S.push_back(saCmd("topology_complete_parameters", topoTab, "sim_parameters"));
    S.push_back(saPickCmd("topology_fixture", topoTab, "min_x", "sim_fixture"));
    S.push_back(saPickCmd("topology_force", topoTab, "max_x", "sim_load_force"));
    // Heat / Flow boundaries only exist on thermal / flow physics: a second (thermal) topology
    // study with a temperature fixture, so the Boundaries page shows the heat-transfer editor.
    custom("topology_heat_flow", 0, [topoTab](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        nlohmann::json cfg = ui::topology::newTopologyConfig(sm.topologyCapabilities(), "thermal");
        if (cfg.empty()) {
            A.note = "no topology method supports thermal physics; structural Boundaries page shown";
            saRun(st, sm, topoTab, "sim_topology_boundaries");
            return SaEnter::Done;
        }
        const int before = sm.studyCount();
        saRun(st, sm, topoTab, "sim_new_topology");
        if (sm.studyCount() != before + 1 ||
            !sm.configureActiveTopology(st, {{"name", "Heat spreader topology"}, {"topology", cfg}})) {
            A.note = "thermal topology study not created: " + st.status;
            return SaEnter::Done;
        }
        saPick(st, A, "min_x");
        saRun(st, sm, topoTab, "sim_fixture");  // thermal physics: a temperature fixture
        st.selection.clear();
        saRun(st, sm, topoTab, "sim_topology_boundaries");
        return SaEnter::Done;
    });
    // The remaining topology states run on the structural study again.
    auto topoStep = [&](const char* name, const char* role, const char* cmd) {
        custom(name, 0, [topoTab, role, cmd](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
            if (!A.topoStructId.empty() && sm.activeStudyId() != A.topoStructId &&
                !sm.selectStudyById(st, A.topoStructId))
                A.note = "structural topology study not found";
            if (role) saPick(st, A, role);
            if (std::strcmp(cmd, "sim_topology_manufacturing") == 0 &&
                A.faces.contains("min_z")) {
                auto bed = A.faces["min_z"]["sel"];
                bed["_pick"] = {{"body", 1}, {"topo", A.faces["min_z"]["tid"]}};
                sm.configureActiveTopology(st, {
                    {"material", "pla"}, {"topology", {{"volume_fraction", 0.35}}},
                    {"build_plate", {{"faces", nlohmann::json::array({bed})}}}});
                if (!sm.applyActiveTopologyPrintProcess(st, "fdm_0.4"))
                    A.note = "FDM 0.4 print preset unavailable";
            }
            if (std::strcmp(cmd, "sim_mesh") == 0)
                A.meshWait = !(st.simClient && st.simClient->busy());
            saRun(st, sm, topoTab, cmd);
            st.selection.clear();
            return SaEnter::Done;
        });
    };
    topoStep("topology_preserve", "max_z", "sim_topology_preserve");
    topoStep("topology_exclude", nullptr, "sim_topology_exclude");
    topoStep("topology_manufacturing", nullptr, "sim_topology_manufacturing");
    topoStep("topology_mesh", nullptr, "sim_mesh");
    S.back().timeoutS = 30.0;
    S.back().ready = [](AppState& st, ui::SimManager& sm) {
        return saIdle(st, sm) && !(g_sa.meshWait && st.simClient && st.simClient->busy());
    };
    topoStep("topology_results", nullptr, "sim_results");
    topoStep("topology_compare", nullptr, "sim_topology_compare");

    // ---- Piping: every form; route / solve / spec / BOM through the panel's own buttons ----
    S.push_back(saCmd("piping_network", pipeTab, "sim_piping_new"));
    S.push_back(saExpanded("piping_network_native_parameters", pipeTab, nullptr, "Edit native parameters",
                            0, "Hide native parameters"));
    S.push_back(saAction("piping_route", pipeTab, "sim_piping_route",
                         [](AppState& st, ui::SimManager& sm) {
                             const double a[3] = {-400.0, 0.0, 0.0}, b[3] = {400.0, 0.0, 0.0};
                             const double dir[3] = {1.0, 0.0, 0.0};
                             sm.piping().setRouteEndpoints(a, dir, b, dir);
                             sm.piping().setRouteSpec(2.0, "40", "A106-B", 50.0);
                             return sm.piping().runRoute(st);
                         }, 60.0, VP));
    S.push_back(saAction("piping_solve", pipeTab, "sim_piping_solve",
                         [](AppState& st, ui::SimManager& sm) { return sm.piping().runSolve(st); }, 60.0));
    S.push_back(saAction("piping_spec_check", pipeTab, "sim_piping_spec",
                         [](AppState& st, ui::SimManager& sm) { return sm.piping().runSpecCheck(st); }, 60.0));
    S.push_back(saAction("piping_bom", pipeTab, "sim_piping_bom",
                         [](AppState& st, ui::SimManager& sm) { return sm.piping().runBom(st); }, 60.0));
    S.push_back(saExpanded("piping_solve_native_parameters", pipeTab, "sim_piping_solve", "Edit native parameters",
                            0, "Hide native parameters", "Conditions"));
    S.push_back(saCmd("piping_internal_cfd", pipeTab, "sim_piping_flow"));
    S.push_back(saExpanded("piping_flow_native_parameters", pipeTab, "sim_piping_flow", "Edit native parameters",
                            0, "Hide native parameters", "Setup"));
    S.push_back(saCmd("piping_vessel", pipeTab, "sim_piping_vessel"));
    S.push_back(saExpanded("piping_vessel_native_parameters", pipeTab, "sim_piping_vessel", "Edit native parameters",
                            0, "Hide native parameters", "Setup"));
    S.push_back(saCmd("piping_duct", pipeTab, "sim_piping_duct"));
    S.push_back(saExpanded("piping_duct_native_parameters", pipeTab, "sim_piping_duct", "Edit native parameters",
                            0, "Hide native parameters", "Segments"));

    // ---- Hydraulics: build the circuit through the panel, then every side form + central view ----
    custom("hyd_circuit", VP, [hydTab](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        HP& hp = sm.hydraulic();
        if (A.sub > 0 && (hp.busy() || st.rpc.busy())) {
            if (pipingNow() - A.subStart < 60.0) return SaEnter::Again;
            A.note = "circuit build timed out at step " + std::to_string(A.sub);
            return SaEnter::Done;
        }
        if (A.sub > 1 && !hp.lastError().empty() && A.note.empty()) A.note = "refused: " + hp.lastError();
        A.subStart = pipingNow();
        const auto& comps = saHydComps();
        const auto& hoses = saHydHoses();
        if (A.sub == 0) {
            saRun(st, sm, hydTab, "sim_hyd_new");
            ++A.sub;
            return SaEnter::Again;
        }
        const int c = A.sub - 1;
        if (c < (int)comps.size()) {
            saRun(st, sm, hydTab, "sim_hyd_add");
            hp.addComponent(st, comps[c].type, comps[c].name, comps[c].x, comps[c].y, comps[c].params);
            ++A.sub;
            return SaEnter::Again;
        }
        const int h = c - (int)comps.size();
        if (h < (int)hoses.size()) {
            std::vector<std::pair<std::string, std::string>> ends;
            for (const auto& e : hoses[h].ends) ends.push_back({hp.componentIdByName(e.first), e.second});
            saRun(st, sm, hydTab, "sim_hyd_hose");
            hp.addHose(st, hoses[h].net, ends, hoses[h].lengthMm, hoses[h].idMm, hoses[h].ratingBar);
            ++A.sub;
            return SaEnter::Again;
        }
        hp.setCentralView(HP::CentralView::Schematic);
        return SaEnter::Done;
    }, nullptr, 60.0);
    S.push_back(saCmd("hyd_add_component", hydTab, "sim_hyd_add", VP));
    S.push_back(saCmd("hyd_connect", hydTab, "sim_hyd_hose"));
    S.push_back(saAction("hyd_size_cylinder", hydTab, "sim_hyd_size_cyl",
                         [](AppState& st, ui::SimManager& sm) {
                             HP& hp = sm.hydraulic();
                             hp.setCylinderInputs(hp.componentIdByName("Lift cylinder"), 63.0, 36.0, 160.0, 300.0,
                                                  100.0);
                             return hp.runSizeCylinder(st);
                         }, 60.0));
    S.push_back(saCmd("hyd_size_pump", hydTab, "sim_hyd_size_pump"));
    S.push_back(saCmd("hyd_size_motor", hydTab, "sim_hyd_size_motor"));
    S.push_back(saAction("hyd_pump_drive", hydTab, "sim_hyd_drive",
                         [](AppState& st, ui::SimManager& sm) {
                             HP& hp = sm.hydraulic();
                             hp.setDriveFromPump(hp.componentIdByName("Pump"));
                             return hp.runPumpDrive(st);
                         }, 60.0));
    S.push_back(saAction("hyd_simulate_schematic", hydTab, nullptr,
                         [](AppState& st, ui::SimManager& sm) {
                             HP& hp = sm.hydraulic();
                             hp.openSimulationSetup();
                             const std::string dcv = hp.componentIdByName("DCV 4/3");
                             hp.clearSchedule();
                             hp.addScheduleRow(0.0, dcv, "center");
                             hp.addScheduleRow(0.2, dcv, "a");
                             hp.setLoad(hp.componentIdByName("Lift cylinder"), 10000.0, 200.0);
                             hp.setDuration(3.0);
                             hp.setCentralView(HP::CentralView::Schematic);
                             return hp.runSimulate(st);
                         }, 90.0, VP));
    S.push_back(saAction("hyd_results_outcome", hydTab, nullptr,
                         [](AppState&, ui::SimManager& sm) {
                             sm.hydraulic().openResults();
                             return true;
                         }, kSaIdleTimeoutS, VP));
    S.push_back(saAction("hyd_simulate_plots", hydTab, nullptr,
                         [](AppState&, ui::SimManager& sm) {
                             sm.hydraulic().setCentralView(HP::CentralView::Plots);
                             return true;
                         }, kSaIdleTimeoutS, VP));
    S.push_back(saCmd("hyd_view_3d", hydTab, "sim_hyd_view_3d", VP));
    S.push_back(saAction("hyd_bom", hydTab, "sim_hyd_bom",
                         [](AppState& st, ui::SimManager& sm) {
                             sm.hydraulic().setCentralView(HP::CentralView::Schematic);
                             return sm.hydraulic().runBom(st);
                         }, 60.0));
    S.push_back(saAction("hyd_to_piping", hydTab, "sim_hyd_piping",
                         [](AppState& st, ui::SimManager& sm) { return sm.hydraulic().runPipingHandoff(st); },
                         60.0));
    S.push_back(saAction("hyd_simulate_setup", hydTab, nullptr,
                         [](AppState&, ui::SimManager& sm) {
                             sm.hydraulic().openSimulationSetup();
                             return true;
                         }, kSaIdleTimeoutS, VP));
    S.push_back(saExpanded("hyd_simulate_native_parameters", hydTab, nullptr,
                            "Advanced simulation parameters", VP));
    S.push_back(saExpanded("hyd_handoff_native_parameters", hydTab, "sim_hyd_piping",
                            "Advanced piping handoff parameters", VP));

    // Author a real native schematic. The CAD manager remains active while its circuit panel
    // targets the separate schematic tab by stable identity; no probe/netlist data is fabricated.
    custom("circuit_operating_point", 0, [](AppState& st, ui::SimManager&, SimAuditShot& A) {
        if (A.sub == 0) {
            A.partTab = st.activeTab;
            nlohmann::json res;
            std::string err;
            if (!st.worker.call("ecad_design", {{"request", "RC low-pass filter with 1000 Hz cutoff"},
                                                {"name", "Simulation audit RC"}}, res, err) ||
                !res.is_object() || res.contains("code") || !res.value("ok", false)) {
                saFail("circuit_recipe", err.empty() ? res.dump() : err);
                return SaEnter::Done;
            }
            A.schematicDoc = res.value("doc_id", std::string());
            st.addEcadTab("sch", A.schematicDoc, res.value("revision", 0), "Simulation audit RC");
            st.switchToTab(A.partTab);
            A.sub = 1;
            return SaEnter::Again;
        }
        if (st.rpc.busy() || st.activeTab != A.partTab || !st.simManager) return SaEnter::Again;
        st.simManager->handleCommand(st, "sim_circuit_op");
        A.note = "Real native RC schematic; setup only, not a claimed circuit solve.";
        return SaEnter::Done;
    });
    S.push_back(saCmd("circuit_dc_sweep", -1, "sim_circuit_dc"));
    S.push_back(saCmd("circuit_ac_sweep", -1, "sim_circuit_ac"));
    S.push_back(saCmd("circuit_transient", -1, "sim_circuit_tran"));
    S.push_back(saExpanded("circuit_transient_native_parameters", -1, "sim_circuit_tran",
                            "Advanced (native parameters)"));
    S.push_back(saCmd("simulate_firmware_setup", -1, "sim_firmware_setup"));
    S.push_back(saExpanded("simulate_firmware_metadata", -1, "sim_firmware_setup",
                            "Scenario metadata and electrical links", 0, nullptr, "GPIO edges"));
    custom("board_thermal_setup", 0, [](AppState& st, ui::SimManager&, SimAuditShot& A) {
        if (A.sub == 0) {
            nlohmann::json res, applied;
            std::string err;
            if (!st.worker.call("ecad_new", {{"kind", "pcb"}, {"name", "Simulation audit board"}}, res, err) ||
                !res.is_object() || res.contains("code")) {
                saFail("board_new", err.empty() ? res.dump() : err); return SaEnter::Done;
            }
            A.boardDoc = res.value("doc_id", std::string());
            const auto outline = nlohmann::json::array({
                {{"type", "line"}, {"p1", {0.0, 0.0}}, {"p2", {80.0, 0.0}}},
                {{"type", "line"}, {"p1", {80.0, 0.0}}, {"p2", {80.0, 50.0}}},
                {{"type", "line"}, {"p1", {80.0, 50.0}}, {"p2", {0.0, 50.0}}},
                {{"type", "line"}, {"p1", {0.0, 50.0}}, {"p2", {0.0, 0.0}}}});
            const auto ops = nlohmann::json::array({{{"op", "pcb_set_outline"},
                                                     {"closed", true}, {"shapes", outline}}});
            if (!st.worker.call("ecad_apply_ops", {{"doc_id", A.boardDoc},
                        {"base_revision", res.value("revision", 0)}, {"ops", ops}}, applied, err) ||
                applied.contains("code")) {
                saFail("board_outline", err.empty() ? applied.dump() : err); return SaEnter::Done;
            }
            st.addEcadTab("pcb", A.boardDoc, applied.value("revision", 0), "Simulation audit board");
            st.switchToTab(A.partTab);
            A.sub = 1;
            return SaEnter::Again;
        }
        if (st.rpc.busy() || st.activeTab != A.partTab || !st.simManager) return SaEnter::Again;
        st.simManager->handleCommand(st, "sim_board_thermal_setup");
        A.note = "Real outlined PCB; no powered-result palette or thermal solve fabricated.";
        return SaEnter::Done;
    });
    custom("firmware_workspace_interactive_setup", VP, [](AppState& st, ui::SimManager&, SimAuditShot& A) {
        ui::firmware::seedFirmwareShotFixture();
        ui::firmware::openFirmwareEmulationSetup();
        st.activeWorkspace = AppState::Workspace::Firmware;
        A.note = "Explicit workspace rendering fixture with editable native tracks; emulator NOT RUN.";
        return SaEnter::Done;
    });
    S.push_back(saExpanded("firmware_workspace_metadata", -1, nullptr,
                            "Scenario metadata and electrical links", VP));

    // ---- Evaluate dialogs over the Simulate workspace ----
    auto evalStep = [&](const char* name, const char* cmd, const char* pickRole) {
        custom(name, 0, [cmd, pickRole](AppState& st, ui::SimManager&, SimAuditShot& A) {
            if (!st.evaluateDialogs) {
                A.note = "no Evaluate dialogs for the active document";
                return SaEnter::Skip;
            }
            if (pickRole) saPick(st, A, pickRole);
            st.evaluateDialogs->handleCommand(st, cmd);
            return SaEnter::Done;
        });
    };
    evalStep("eval_mass_properties", "eval_mass_props", nullptr);
    evalStep("eval_section_properties", "eval_section_props", nullptr);
    evalStep("eval_measure", "eval_measure", "max_z");

    // ---- Motion (needs an assembly with a drivable mate: built here, last) ----
    custom("motion_new", 0, [](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        if (A.sub == 0) {
            st.selection.clear();
            A.partSm = &sm;
            const std::string why = saBuildMotionAssembly(st, A);
            if (!why.empty()) {
                A.note = "assembly setup failed: " + why;
                return SaEnter::Skip;
            }
            A.sub = 1;
            A.subStart = pipingNow();
            return SaEnter::Again;
        }
        // The assembly's own SimManager must be seated and the engine idle before newStudy.
        if ((st.docMirror.docId() != A.asmDoc || &sm == A.partSm || st.rpc.busy()) &&
            pipingNow() - A.subStart < kSaIdleTimeoutS)
            return SaEnter::Again;
        const int before = sm.studyCount();
        saRun(st, sm, kSaTabMotion, "sim_new_motion");
        A.motionOk = sm.studyCount() == before + 1;
        if (!A.motionOk) A.note = "Motion study was not created: " + st.status;
        else sm.configureActiveMotion(st, 60.0f, 100.0f, 24);
        return SaEnter::Done;
    });
    auto motionStep = [&](const char* name, const char* cmd, unsigned regions, SaReadyFn ready, double timeoutS) {
        custom(name, regions, [cmd](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
            if (!A.motionOk) {
                A.note = "no Motion study (see motion_new)";
                return SaEnter::Skip;
            }
            saRun(st, sm, kSaTabMotion, cmd);
            return SaEnter::Done;
        }, std::move(ready), timeoutS);
    };
    motionStep("motion_driver_time", "sim_motion_setup", 0, nullptr, kSaIdleTimeoutS);
    motionStep("motion_complete_parameters", "sim_parameters", 0, nullptr, kSaIdleTimeoutS);
    // Run: first wait (bounded) until the assembly's parsed viewport mesh is cached on its session
    // (Motion samples that mesh and refuses Run until it exists), then
    // re-issue Run every 0.5 s while the engine finishes the setup saves. A mesh that never lands
    // or a refusal that outlasts the timeout is reported in the state note.
    custom("motion_run", 0, [](AppState& st, ui::SimManager& sm, SimAuditShot& A) {
        constexpr double kMeshTimeoutS = 30.0;
        if (!A.motionOk) {
            A.note = "no Motion study (see motion_new)";
            return SaEnter::Skip;
        }
        const double now = pipingNow();
        if (A.sub == 0) {
            A.sub = 1;
            A.subStart = now;
            A.waitStart = 0.0;
        }
        if (A.sub == 1) {
            const DocSession* ses = st.activeSession();
            // Study saves bump the revision without re-tessellating; Run's own gate compares the
            // geometry for that case, so only the cached mesh itself is awaited here.
            const bool meshReady = st.docMirror.docId() == A.asmDoc && ses && ses->cachedDocMesh &&
                                   !ses->cachedDocMesh->empty() && ses->cachedMirror.valid();
            if (!meshReady) {
                if (now - A.subStart < kMeshTimeoutS) return SaEnter::Again;
                A.note = "the assembly mesh did not load within " + std::to_string((int)kMeshTimeoutS) + " s";
                std::printf("simaudit: motion_run NOT RUN (%s)\n", A.note.c_str());
                std::fflush(stdout);
                return SaEnter::Done;
            }
            std::printf("simaudit: motion_run assembly mesh ready after %.1f s\n", now - A.subStart);
            std::fflush(stdout);
            A.sub = 2;
            A.subStart = now;
        }
        if (A.waitStart == 0.0 || now - A.waitStart >= 0.5) {
            A.waitStart = now;
            saRun(st, sm, kSaTabMotion, "sim_run");
            if (sm.runUi().running || sm.motionHasResult()) return SaEnter::Done;
        }
        if (now - A.subStart < kSaIdleTimeoutS) return SaEnter::Again;
        A.note = saRunRefusal(st);
        std::printf("simaudit: motion_run REFUSED (%s)\n", A.note.c_str());
        std::fflush(stdout);
        return SaEnter::Done;
    }, [](AppState& st, ui::SimManager& sm) {
        return !st.rpc.busy() && (sm.motionHasResult() || !sm.runUi().running);
    }, 30.0);
    motionStep("motion_playback", "sim_results", VP, nullptr, kSaIdleTimeoutS);
}

// Part + faces, then the Simulate workspace. Returns once setup is complete (A.setup == 2).
void saSetup(AppState& st, SimAuditShot& A) {
    if (A.setup == 0) {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { saFail("new_part", err); return; }
        auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "AuditBox"},
            {"params", {{"length", dim(60.0)}, {"width", dim(40.0)}, {"height", dim(10.0)}}},
            {"centered", true}}}});
        auto r = st.docMirror.applyOps(st.worker, ops);
        if (!r.ok) { saFail("box", r.error_message); return; }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        // min_x / max_x end faces and the max_z top face, by centroid.
        double ext[3] = {1e30, 1e30, 1e30};
        const char* roles[3] = {"min_x", "max_x", "max_z"};
        for (uint32_t tid = 1; tid <= 12; ++tid) {
            std::string se;
            const std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
            if (s.empty()) continue;
            nlohmann::json o = nlohmann::json::parse(s, nullptr, false);
            if (!o.is_object() || !o.contains("centroid") || !o["centroid"].is_array() || o["centroid"].size() != 3)
                continue;
            const double key[3] = {o["centroid"][0].get<double>(), -o["centroid"][0].get<double>(),
                                   -o["centroid"][2].get<double>()};
            for (int k = 0; k < 3; ++k)
                if (key[k] < ext[k]) {
                    ext[k] = key[k];
                    A.faces[roles[k]] = {{"tid", tid}, {"sel", o}};
                }
        }
        for (const char* role : roles)
            if (!A.faces.contains(role)) { saFail("faces", std::string("no '") + role + "' face resolved"); return; }
        std::printf("simaudit: box 60x40x10 mm (rev %d), faces resolved\n", r.revision);
        std::fflush(stdout);
        st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
        A.settle = 3;
        A.setup = 1;
        A.waitStart = pipingNow();
        return;
    }
    if (!st.simManager) {
        if (pipingNow() - A.waitStart > 30.0) saFail("no_sim_manager", "the part has no Simulation manager");
        return;
    }
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.showRibbon = true;
    st.showSimulationTools = true;
    st.ribbonTab = kSaTabStatic;
    A.setup = 2;
    A.waitStart = 0.0;
    A.settle = 8;
}

void simAuditTick(AppState& st) {
    SimAuditShot& A = g_sa;
    if (!A.init) {
        A.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        A.want = st.shot && mode && std::strcmp(mode, "simaudit") == 0;
        if (A.want) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            A.dir = std::string(repo ? repo : ".") + "/scratch/simaudit";
            std::error_code ec;
            std::filesystem::create_directories(A.dir, ec);
            if (const char* f = std::getenv("CHUDWARE_SIMAUDIT_FRAMES")) A.auditFrames = std::max(1, std::atoi(f));
            if (const char* f = std::getenv("CHUDWARE_SIMAUDIT_MAX_FRAMES")) A.maxFrames = std::max(0, std::atoi(f));
            A.t0 = pipingNow();
            A.report = {{"scenario", "simaudit"},
                        {"ok", false},
                        {"audit", {{"detector", "imgui_test_engine_item_hooks"},
                                   {"frames", A.auditFrames},
                                   {"max_frames", A.maxFrames}}},
                        {"states", nlohmann::json::array()},
                        {"skipped", nlohmann::json::array()},
                        {"total_conflicts", 0},
                        {"total_overflows", 0}};
            saBuildSteps(A);
            // Surfaces that exist but are not reachable without a click or a long solve.
            saSkip(A, "static_torque",
                   "schema-reserved: the Torque ribbon row is disabled and SimManager::handleCommand has no "
                   "sim_load_torque handler");
            saSkip(A, "material_new_custom_popup",
                   "material creation is outside this simulation-setup gesture and native-parameter audit");
            saSkip(A, "solver_results_modal_flow_thermal_fatigue_em_topology_pipecfd",
                   "the audit runs only the static and motion solves (the external-flow solve is covered by "
                   "CHUDWARE_SHOT=flow); their Results nodes are audited in the no-result state");
        }
    }
    if (!A.want || A.done) return;
    if (!st.workerStarted || st.frame < 5) return;
    if (!A.capture.empty()) return;  // endFrame consumes it
    if (ui::audit::active()) return;  // the audit is recording; keep the state still
    if (A.settle > 0) { --A.settle; return; }
    if (!A.queued.empty()) {
        A.capture = A.queued;
        A.queued.clear();
        return;
    }
    if (A.setup < 2) {
        saSetup(st, A);
        return;
    }
    if (A.idx >= A.steps.size()) {
        if (st.simManager) st.simManager->shotForcePicker = false;
        A.report["ok"] = A.totalConflicts == 0 && A.totalOverflows == 0;
        A.report["seconds"] = std::round((pipingNow() - A.t0) * 10.0) / 10.0;
        A.done = true;
        return;
    }
    if (!st.simManager) {  // the assembly's manager is re-seated after the document switch
        if (++A.idleFrames > 600) saFail("no_sim_manager", "the active document lost its Simulation manager");
        return;
    }
    A.idleFrames = 0;
    ui::SimManager& sm = *st.simManager;
    const SaStep& step = A.steps[A.idx];

    switch (A.stage) {
        case 0:  // the engine and panels go idle first (newStudy refuses while an RPC is in flight)
            if (A.waitStart == 0.0) A.waitStart = pipingNow();
            if (!saIdle(st, sm) && pipingNow() - A.waitStart < kSaIdleTimeoutS) return;
            A.stage = 1;
            A.sub = 0;
            A.note.clear();
            [[fallthrough]];
        case 1: {
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab = AppState::LP_Simulation;
            st.showRibbon = true;
            sm.shotForcePicker = false;
            const SaEnter r = step.enter(st, sm, A);
            if (A.failed || r == SaEnter::Again) return;
            if (r == SaEnter::Skip) {
                saSkip(A, step.name, A.note);
                saWriteReport(A);
                ++A.idx;
                A.stage = 0;
                A.waitStart = 0.0;
                return;
            }
            A.stage = 2;
            A.waitStart = pipingNow();
            return;
        }
        case 2: {  // settle into the state
            const bool ready = step.ready ? step.ready(st, sm) : saIdle(st, sm);
            if (!ready) {
                if (pipingNow() - A.waitStart < step.timeoutS) return;
                if (!A.note.empty()) A.note += "; ";
                A.note += "not settled after " + std::to_string((int)step.timeoutS) + " s";
            }
            const std::string perr = sm.piping().isOpen()      ? sm.piping().lastError()
                                     : sm.hydraulic().isOpen() ? sm.hydraulic().lastError()
                                                               : std::string();
            if (!perr.empty()) A.note += (A.note.empty() ? "panel: " : "; panel: ") + perr;
            A.stage = 3;
            A.settle = 8;
            return;
        }
        case 3: {  // audit
            ui::audit::AuditOptions o;
            o.regions = ui::audit::RegionLeftPane | ui::audit::RegionFloating | step.regions;
            if (st.ribbonTab != A.lastRibbonTab) {  // each ribbon tab is reported once
                o.regions |= ui::audit::RegionRibbon;
                A.lastRibbonTab = st.ribbonTab;
            }
            o.frames = A.auditFrames;
            o.maxFrames = A.maxFrames;
            if (!ui::audit::begin(step.name, o)) { saFail("audit", "ui::audit::begin refused " + step.name); return; }
            A.stage = 4;
            return;
        }
        case 4: {  // record, then capture the settled state
            nlohmann::json res = ui::audit::lastResult();
            char file[160];
            std::snprintf(file, sizeof(file), "%02d_%s.png", A.shotNo + 1, step.name.c_str());
            res["capture"] = A.dir + "/" + file;
            res["ribbon_tab"] = st.ribbonTab;
            if (!A.note.empty()) res["note"] = A.note;
            const int nc = (int)res.value("conflicts", nlohmann::json::array()).size();
            const int no = (int)res.value("overflows", nlohmann::json::array()).size();
            A.totalConflicts += nc;
            A.totalOverflows += no;
            std::printf("simaudit: %s conflicts=%d overflows=%d items_seen=%d%s%s%s\n", step.name.c_str(), nc, no,
                        res.value("items_seen", 0), res.value("truncated", false) ? " (capped)" : "",
                        A.note.empty() ? "" : " note: ", A.note.c_str());
            std::fflush(stdout);
            A.report["states"].push_back(std::move(res));
            saWriteReport(A);
            A.queued = A.dir + "/" + file;
            A.settle = 1;
            ++A.shotNo;
            ++A.idx;
            A.stage = 0;
            A.waitStart = 0.0;
            return;
        }
        default:
            return;
    }
}

bool simAuditEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& breakLoop) {
    SimAuditShot& A = g_sa;
    if (!A.want) return false;
    ++st.frame;
    if (!A.capture.empty() && (!st.rpc.busy() || ++A.captureWait > 120)) {
        std::error_code ec;
        std::filesystem::create_directories(A.dir, ec);
        const bool last = A.done || A.idx >= A.steps.size();
        writeBackbufferPng(A.capture.c_str(), dw, dh, last ? screenshotPath : nullptr);
        A.capture.clear();
        A.captureWait = 0;
    }
    glfwSwapBuffers(st.win);
    breakLoop = false;
    if (A.done && A.capture.empty() && A.queued.empty()) {
        if (A.failed) A.report["ok"] = false;
        saWriteReport(A);
        std::printf("simaudit: total_conflicts=%d total_overflows=%d states=%zu skipped=%zu report=%s/report.json\n",
                    A.totalConflicts, A.totalOverflows, A.report["states"].size(), A.report["skipped"].size(),
                    A.dir.c_str());
        std::fflush(stdout);
        const int idConflicts = ui::audit::idAuditFinish();  // CHUDWARE_IDAUDIT summary (0 when off)
        if (A.failed || A.totalConflicts > 0 || A.totalOverflows > 0 || idConflicts > 0) {
            std::fflush(nullptr);
            std::_Exit(EXIT_FAILURE);
        }
        breakLoop = true;
    }
    return true;
}

// CHUDWARE_SHOT=fea / flow bookkeeping (file-local so scenarios.h stays untouched). Waits are
// wall-clock bounded; a failure prints "<tag>: FAILED [code] detail", still captures the frame,
// then exits non-zero (studyShotExit) like simaudit.
struct StudyShot {
    std::string docId;       // the part this scenario built; it must stay the active document
    double t0 = 0.0;         // scenario start
    double stepStart = 0.0;  // start of the current bounded wait
    double lastTry = 0.0;    // last sim_run attempt
    double idleSince = 0.0;  // first moment the run was seen not running without a result (0 = n/a)
    bool failed = false;
};
StudyShot g_feaShot, g_flowShot;

void studyShotFail(StudyShot& S, const char* tag, const char* code, const std::string& detail) {
    std::printf("%s: FAILED [%s] %s\n", tag, code, detail.c_str());
    std::fflush(stdout);
    S.failed = true;
}

// After the final capture: a failed scenario exits non-zero (the CHUDWARE_IDAUDIT summary first).
void studyShotExit(const StudyShot& S) {
    if (!S.failed) return;
    ui::audit::idAuditFinish();
    std::fflush(nullptr);
    std::_Exit(EXIT_FAILURE);
}

// New part with one centred box, created through the same path as the tree toolbar's New Part
// (the tab and its DocSession exist before the first mesh load binds to it). "" on success.
std::string studyShotBox(AppState& st, StudyShot& S, const char* name, double l, double w, double h,
                         int& revision) {
    std::string err;
    if (!st.docMirror.newPart(st.worker, err)) return "new part: " + err;
    st.clearViewportForNewDoc();
    st.addDocTab(/*isAssembly=*/false);
    S.docId = st.docMirror.docId();
    auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
    nlohmann::json ops = nlohmann::json::array();
    ops.push_back({{"op", "add_feature"}, {"feature", {
        {"type", "box"}, {"name", name},
        {"params", {{"length", dim(l)}, {"width", dim(w)}, {"height", dim(h)}}},
        {"centered", true}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    revision = r.revision;
    if (!r.ok) return "box: " + r.error_message;
    if (!r.aicm_path.empty()) {
        std::string le;
        if (!st.loadDocMesh(r.aicm_path, le)) return "box mesh: " + le;
    }
    return "";
}

// The scenario's part is active, its Simulation manager is seated and the engine is idle
// (newStudy / run refuse while an RPC is in flight). Fails after `timeoutS`.
bool studyShotReady(AppState& st, StudyShot& S, const char* tag, double timeoutS) {
    if (st.docMirror.docId() != S.docId) {
        studyShotFail(S, tag, "doc_switched",
                      "the active document changed to '" + st.docMirror.docId() + "' (expected " +
                          S.docId + ")");
        return false;
    }
    if (st.simManager && !st.rpc.busy()) return true;
    if (pipingNow() - S.stepStart > timeoutS)
        studyShotFail(S, tag, "engine_busy",
                      std::string(st.simManager ? "the engine stayed busy" : "no Simulation manager") +
                          " for " + std::to_string((int)timeoutS) + " s");
    return false;
}

// One sim_run attempt every 0.5 s until the run starts (`started`) or `timeoutS` passes; the
// refusal the SimManager logged is the failure detail. True once started.
bool studyShotRun(AppState& st, StudyShot& S, const char* tag, double timeoutS,
                  const std::function<bool()>& started) {
    const double now = pipingNow();
    if (now - S.lastTry < 0.5) return false;
    S.lastTry = now;
    if (!st.rpc.busy()) {
        st.simManager->handleCommand(st, "sim_run");
        if (started()) return true;
    }
    if (now - S.stepStart > timeoutS)
        studyShotFail(S, tag, "run_refused",
                      st.rpc.busy() ? std::string("the engine stayed busy") : saRunRefusal(st));
    return false;
}

// Real input-driven simulation setup smoke. No payloads or selection entries are injected:
// the source face is found in the rendered ID buffer and the UI owns every drag and delivery.
struct SimInteractiveShot {
    bool init = false, want = false, done = false, failed = false;
    bool metadataDuringApply = false, metadataQueued = false;
    bool pipingInputVisited = false;
    bool nativeEditorLocated = false, pipingEditorOpened = false;
    int step = 0, settle = 0, gesture = 0, phase = 0, textPhase = 0, fidelityCase = 0;
    double started = 0.0, stepStarted = 0.0;
    int partTab = -1;
    double pulsedFinalC = 0.0;
    ImVec2 source, target, facePoint;
    ImGuiID sourceItemId = 0;
    render::PickHit face;
    std::string dir, capture, doc, study, text, pump, tank, mateA, mateB, dragTarget, editorWindow, hose, fixtureSid;
    nlohmann::json before, edited, report, faceSelector, fidelityBase, terminalFixture, terminalLoad,
                   modalReference, staticReference, staticSetup, dragOrigin, pipeBase;
    std::function<void(AppState&)> afterCapture;
};
SimInteractiveShot g_si;

bool siItem(const char* label, ImVec2& point, const char* window = nullptr) {
    if (const auto* item = shotItem(label, true, window)) {
        point = shotPoint(*item);
        return true;
    }
    return false;
}

void siFail(const char* code, const std::string& detail) {
    auto& I = g_si;
    I.failed = I.done = true;
    I.report["failure"] = {{"code", code}, {"detail", detail}, {"step", I.step}};
    I.report["visible_items_at_failure"] = ui::audit::observedItems();
    I.report["navigation_at_failure"] = shotNavigationEvidence();
    I.capture = I.dir + "/FAILED.png";
    ImGui::GetIO().AddMouseButtonEvent(0, false);
    ImGui::GetIO().AddKeyEvent(ImGuiMod_Ctrl, false);
    std::printf("siminteractive: FAILED [%s] %s\n", code, detail.c_str());
    std::fflush(stdout);
}

void siNext() {
    auto& I = g_si;
    ++I.step;
    I.stepStarted = pipingNow();
    I.pipingInputVisited = false;
    I.nativeEditorLocated = I.pipingEditorOpened = false;
    g_simNavigation = ShotNavigation();
    I.settle = 3;
}

bool siSeek(const char* label, ImVec2& point, const char* window = nullptr) {
    if (siItem(label, point, window)) return true;
    shotScrollTo(label, window);
    return false;
}

bool siFooterAction(const char* label, ImVec2& point) {
    for (const auto& item : ui::audit::observedItems()) {
        if (!item.value("enabled", true) || !item.value("visible", false) || !shotLabel(item, label)) continue;
        const auto& native = item["native_label"].get_ref<const std::string&>();
        const auto suffix = native.find("###");
        if (suffix == std::string::npos || native.compare(suffix, std::string::npos, "###kit_af_primary") != 0) continue;
        point = shotPoint(item);
        return true;
    }
    return false;
}

const nlohmann::json* siStatusPrefix(const char* prefix, bool visibleOnly = true) {
    for (const auto& item : ui::audit::observedItems()) {
        if (!item.value("text_only", false) || (visibleOnly && !item.value("visible", false))) continue;
        const auto& label = item["label"].get_ref<const std::string&>();
        if (label.compare(0, std::strlen(prefix), prefix) == 0) return &item;
    }
    return nullptr;
}

void siGesture(ImVec2 source, ImVec2 target, bool drag, const char* action) {
    auto& I = g_si;
    I.source = source; I.target = target; I.gesture = drag ? 2 : 1; I.phase = 0;
    I.metadataDuringApply = false; I.metadataQueued = false;
    I.sourceItemId = 0;
    for (const auto& item : ui::audit::observedItems()) {
        if (!item.value("visible", false) || !item.value("enabled", true) ||
            item.value("text_only", false) || !item.value("id", 0u)) continue;
        const ImVec2 center = shotPoint(item);
        if (std::fabs(center.x - source.x) <= 1.0f &&
            std::fabs(center.y - source.y) <= 1.0f) {
            I.sourceItemId = item["id"].get<ImGuiID>();
            break;
        }
    }
    I.dragTarget.clear();
    I.report["actions"].push_back({{"action", action}, {"source", {source.x, source.y}},
                                   {"target", {target.x, target.y}},
                                   {"source_navigation", g_simNavigation.evidence}});
    ImGui::GetIO().AddMousePosEvent(source.x, source.y);
}

// Source and destination need not fit in the same scroll viewport. Start from a live source,
// then keep the real drag held while wheel-navigation exposes its live destination.
bool siDragItems(const char* source, const char* target, const char* action) {
    ImVec2 p, q;
    if (!siSeek(source, p)) return false;
    const bool visible = siItem(target, q);
    siGesture(p, visible ? q : p, true, action);
    g_si.dragTarget = target;
    return true;
}

bool siExpand(const char* title, const char* action) {
    const auto* item = shotItem(title, false);
    // Read real disclosure state even when scrolling its editor hides the header.
    if (item && item->value("openable", false) && item->value("opened", false)) return true;
    if (!item || !item->value("visible", false)) { shotScrollTo(title); return false; }
    const ImVec2 point = shotPoint(*item);
    siGesture(point, point, false, action);
    return false;
}

bool siGestureTick(AppState& st) {
    auto& I = g_si;
    if (!I.gesture) return false;
    if (pipingNow() - I.stepStarted > 90.0) {
        siFail("gesture_target_timeout", shotNavigationEvidence().dump());
        return true;
    }
    ImGuiIO& io = ImGui::GetIO();
    // A queued event is not a completed input frame (ImGui may trickle a backlog).
    // Observe position/press/release before advancing; never compress a click into
    // frames that have not consumed its events.
    // Wheel/caret navigation can still move a control after its rectangle was
    // observed. Follow its live rectangle, then wait for an actual hovered frame
    // before pressing; a cursor event alone does not prove the control is ready.
    if (I.phase == 0 && I.sourceItemId) {
        for (const auto& item : ui::audit::observedItems()) {
            if (item.value("id", 0u) != I.sourceItemId) continue;
            if (!item.value("visible", false)) {
                shotScrollTo(item["label"].get_ref<const std::string&>().c_str());
                return true;
            }
            I.source = shotPoint(item);
            if (I.gesture == 1) I.target = I.source;
            I.report["actions"].back()["source"] = {I.source.x, I.source.y};
            if (I.gesture == 1)
                I.report["actions"].back()["target"] = {I.target.x, I.target.y};
            break;
        }
        if (GImGui->HoveredId != I.sourceItemId) {
            io.AddMousePosEvent(I.source.x, I.source.y);
            return true;
        }
    }
    if (I.phase == 0 &&
        (std::fabs(io.MousePos.x - I.source.x) > 2.0f ||
         std::fabs(io.MousePos.y - I.source.y) > 2.0f)) {
        io.AddMousePosEvent(I.source.x, I.source.y);
        return true;
    }
    if (I.phase == 1 && !io.MouseDown[0]) return true;
    if (I.phase == (I.gesture == 2 ? 5 : 2) && io.MouseDown[0]) return true;
    if (I.phase == 1 && I.metadataDuringApply && !I.metadataQueued) {
        // Real global discovery can arrive between press and release. It must
        // not cancel Apply; document work still has a separate blocking proof.
        st.rpc.call("fw_projects", nlohmann::json::object(), [](const core::RpcResult& r) {
            g_si.report["metadata_poll_completed"] = r.ok;
        });
        I.metadataQueued = st.rpc.busy() && !st.rpc.interactionBusy();
    }
    if (I.gesture == 2 && I.phase == 2 && !I.dragTarget.empty()) {
        ImVec2 live;
        if (!siItem(I.dragTarget.c_str(), live)) {
            shotScrollTo(I.dragTarget.c_str());
            return true;
        }
        I.target = live;
        I.dragTarget.clear();
        I.report["actions"].back()["target"] = {live.x, live.y};
        I.report["actions"].back()["navigation"] = g_simNavigation.evidence;
    }
    if (I.step == 263 && I.gesture == 2 && I.phase == 4) {
        auto& pp = st.simManager->piping();
        if (st.rpc.busy() || pp.busy()) return true;
        const auto* payload = ImGui::GetDragDropPayload();
        const auto frozen = payload && payload->DataSize > 1 ?
            nlohmann::json::parse(static_cast<const char*>(payload->Data),
                static_cast<const char*>(payload->Data) + payload->DataSize - 1, nullptr, false) :
            nlohmann::json();
        const bool immutable = frozen == I.dragOrigin &&
            !pp.acceptsSetupDrag(st, ui::PipingPanel::Form::Solve, frozen) &&
            pp.acceptsSetupDrag(st, ui::PipingPanel::Form::Solve,
                                pp.setupDragOrigin(st, ui::PipingPanel::Form::Solve));
        I.report["checks"].push_back({{"name", "piping_held_drag_origin_immutable_across_native_refresh"},
                                     {"ok", immutable}, {"evidence", frozen}});
        if (!immutable) { siFail("piping_drag_origin_not_frozen", frozen.dump()); return true; }
    }
    ++I.phase;
    const ImVec2 mouse = I.gesture == 2 && I.phase >= 3 ? I.target : I.source;
    io.AddMousePosEvent(mouse.x, mouse.y);
    if (I.phase == 1) io.AddMouseButtonEvent(0, true);
    else if (I.gesture == 2 && I.phase == 2)
        io.AddMousePosEvent(I.source.x + 12.0f, I.source.y + 2.0f);
    else if (I.gesture == 2 && I.phase == 3) io.AddMousePosEvent(I.target.x, I.target.y);
    else if (I.gesture == 2 && I.phase == 4) {
        const ImGuiPayload* payload = ImGui::GetDragDropPayload();
        if (!payload) { siFail("drag_source", "real pointer gesture produced no ImGui payload"); return true; }
        I.report["actions"].back()["payload_type"] = payload->DataType;
        if (I.step == 3) {
            const auto definition = st.simManager->activeStudyDefinition();
            const bool untouched = definition.value("fixtures", nlohmann::json::array()).empty();
            I.report["checks"].push_back({{"name", "face_preview_does_not_mutate"}, {"ok", untouched}});
            if (!untouched) { siFail("face_preview_mutated", "fixture changed before drop delivery"); return true; }
        }
        if (I.step == 263) {
            auto& pp = st.simManager->piping();
            if (std::strcmp(payload->DataType, "CHUDWARE_PIPE_PORT") != 0 || payload->DataSize <= 1) {
                siFail("piping_drag_source_type", payload->DataType); return true;
            }
            I.dragOrigin = nlohmann::json::parse(static_cast<const char*>(payload->Data),
                static_cast<const char*>(payload->Data) + payload->DataSize - 1, nullptr, false);
            if (I.dragOrigin.is_discarded() ||
                !pp.acceptsSetupDrag(st, ui::PipingPanel::Form::Solve, I.dragOrigin)) {
                siFail("piping_drag_origin_initial_context", I.dragOrigin.dump()); return true;
            }
            nlohmann::json listed, record, written;
            std::string error;
            if (!st.worker.call("sim_study_list", {{"doc_id", I.doc}}, listed, error)) {
                siFail("piping_agent_refresh_read", error); return true;
            }
            for (const auto& candidate : listed.value("studies", nlohmann::json::array()))
                if (candidate.value("id", std::string()) == pp.networkStudyId()) record = candidate;
            if (!record.is_object()) { siFail("piping_agent_refresh_record", listed.dump()); return true; }
            record["params"]["design"]["pressure_kpa"] =
                record["params"].value("design", nlohmann::json::object()).value("pressure_kpa", 1000.0) + 17.0;
            // This is a real agent-style edit in the native store while the user is dragging,
            // not an injected mirror/selection/payload. The ordinary refresh adopts it.
            if (!st.worker.call("sim_study_edit", {{"doc_id", I.doc}, {"study_id", pp.networkStudyId()},
                                                  {"study", record}}, written, error) || written.contains("code")) {
                siFail("piping_agent_refresh_edit", error.empty() ? written.dump() : error); return true;
            }
            st.simManager->markStudiesStale();
            st.simManager->refresh(st);
            I.report["piping_drag_external_edit"] = {{"native_record", record}, {"native_reply", written}};
        }
    }
    else if (I.phase == (I.gesture == 2 ? 5 : 2)) io.AddMouseButtonEvent(0, false);
    else if (I.phase >= (I.gesture == 2 ? 7 : 4)) {
        I.gesture = 0;
        I.settle = 3;
    }
    return true;
}

bool siWriteText(const std::string& text, const char* field = "##sim_parameter_json", const char* window = nullptr) {
    auto& I = g_si;
    ImGuiIO& io = ImGui::GetIO();
    if (I.textPhase == 0) {
        ImVec2 p;
        if (!siSeek(field, p, window)) return false;
        siGesture(p, p, false, "Focus complete native parameter JSON");
        I.text = text;
        I.textPhase = 1;
        return false;
    }
    if (I.textPhase == 1) {
        io.AddKeyEvent(ImGuiMod_Ctrl, true);
        io.AddKeyEvent(ImGuiKey_A, true);
        I.textPhase = 2;
    } else if (I.textPhase == 2) {
        io.AddKeyEvent(ImGuiKey_A, false);
        io.AddKeyEvent(ImGuiMod_Ctrl, false);
        I.textPhase = 3;
    } else if (I.textPhase == 3) {
        io.AddInputCharactersUTF8(I.text.c_str());
        I.textPhase = 4;
    } else {
        I.textPhase = 0;
        return true;
    }
    return false;
}

// Read the live ImGui edit buffer only after a real focus gesture. This is observation, not
// mutation of the draft or a substitute for typing/Apply.
bool siReadDraft(nlohmann::json& draft, const char* field = "##sim_parameter_json", const char* window = nullptr) {
    const auto* item = shotItem(field, true, window);
    if (!item) { shotScrollTo(field, window); return false; }
    const auto* state = ImGui::GetInputTextState(item->value("id", ImGuiID(0)));
    if (!state || state->TextA.empty()) return false;
    draft = nlohmann::json::parse(state->TextA.Data, nullptr, false);
    return !draft.is_discarded();
}

bool siFocusDraft(const char* field = "##sim_parameter_json", const char* window = nullptr) {
    ImVec2 point;
    if (!siSeek(field, point, window)) return false;
    siGesture(point, point, false, "Inspect real rendered native parameter draft");
    return true;
}

void siGo(int step) {
    g_si.step = step - 1;
    siNext();
}

bool siNativeEditor(const char* apply) {
    auto& I = g_si;
    if (!I.nativeEditorLocated) {
        const auto* item = shotItem(apply, false);
        if (!item) { shotScrollTo(apply); return false; }
        I.editorWindow = item->value("window", std::string());
        I.nativeEditorLocated = true;
    }
    ImVec2 point;
    return siSeek("##t", point, I.editorWindow.c_str());
}

bool siPipingEditor() {
    auto& I = g_si;
    ImVec2 point;
    if (I.pipingEditorOpened || shotItem("Hide native parameters", false) || shotItem("Apply parameters", false)) {
        I.pipingEditorOpened = true;
        return siNativeEditor("Apply parameters");
    }
    if (!I.pipingInputVisited) {
        // Pinned page tabs need one real visit, not a click every time an offscreen editor is sought.
        I.pipingInputVisited = true;
        for (const char* tab : {"Conditions", "Setup", "Segments", "Design basis"}) {
            if (!siItem(tab, point)) continue;
            siGesture(point, point, false, "Return to actual piping input page");
            return false;
        }
    }
    if (siSeek("Edit native parameters", point)) {
        I.pipingEditorOpened = true;
        siGesture(point, point, false, "Open actual piping canonical JSON editor");
    }
    return false;
}

bool siStudy(AppState& st, const std::string& doc, const std::string& id, nlohmann::json& found) {
    nlohmann::json list;
    std::string err;
    if (!st.worker.call("sim_study_list", {{"doc_id", doc}}, list, err, 15000) ||
        !list.is_object() || list.contains("code")) {
        siFail("study_store", err.empty() ? list.dump() : err);
        return false;
    }
    for (const auto& s : list.value("studies", nlohmann::json::array()))
        if (s.is_object() && s.value("id", std::string()) == id) { found = s; return true; }
    siFail("study_missing", id);
    return false;
}

bool siFacePoint(AppState& st) {
    auto& I = g_si;
    ImVec2 lo, hi;
    auto& probe = I.report["face_source_probe"];
    probe = {{"has_doc_mesh", st.hasDocMesh}, {"has_id_pass", st.idPass != nullptr},
             {"has_renderer", st.renderer != nullptr}, {"face_cache_count", st.faceNormalCache.size()},
             {"body_ids", st.docBodyIds}, {"body_visible", st.bodyVisible}};
    if (!st.hasDocMesh || !st.idPass || !st.renderer || !ui::viewportClearRect(lo, hi)) {
        probe["stage"] = "viewport_unavailable";
        return false;
    }
    probe["clear_rect"] = {lo.x, lo.y, hi.x, hi.y};
    ImGuiWindow* viewport = nullptr;
    for (const auto& item : ui::audit::observedItems())
        if (item.value("visible", false) && shotLabel(item, "##viewport3d")) {
            viewport = ImGui::FindWindowByName(item["window"].get_ref<const std::string&>().c_str());
            break;
        }
    if (!viewport) { probe["stage"] = "viewport_item_unavailable"; return false; }
    const auto& camera = st.renderer->camera();
    const float x0 = ImGui::GetMainViewport()->Pos.x, y0 = viewport->Pos.y;
    const int w = camera.vpWidth, h = camera.vpHeight;
    probe["camera_size"] = {w, h};
    probe["viewport_origin"] = {x0, y0};
    if (w <= 0 || h <= 0 || !st.idPass->render(camera, true)) {
        probe["stage"] = "face_buffer_unavailable";
        return false;
    }
    probe["stage"] = "no_face_in_clear_rect";
    probe["hit_counts"] = nlohmann::json::object();
    for (int y = 1; y < 20; ++y) for (int x = 1; x < 20; ++x) {
        const ImVec2 p(lo.x + (hi.x - lo.x) * x / 20.0f, lo.y + (hi.y - lo.y) * y / 20.0f);
        const auto hit = st.idPass->pickFinal((int)(p.x - x0), (int)(p.y - y0), w, h);
        auto& count = probe["hit_counts"][std::to_string(static_cast<int>(hit.kind))];
        count = count.is_number_integer() ? count.get<int>() + 1 : 1;
        if (hit.kind != render::PickKind::Face || !hit.body_id || !hit.topo_id) continue;
        I.facePoint = p; I.face = hit;
        probe["stage"] = "face_found";
        I.report["picked_face"] = {{"body", hit.body_id}, {"topo", hit.topo_id},
                                   {"pixel", {p.x, p.y}}};
        return true;
    }
    return false;
}

void simInteractiveTick(AppState& st) {
    auto& I = g_si;
    if (!I.init) {
        I.init = true;
        const char* shot = std::getenv("CHUDWARE_SHOT");
        I.want = st.shot && shot && std::strcmp(shot, "siminteractive") == 0;
        if (I.want) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            I.dir = std::string(repo ? repo : ".") + "/scratch/siminteractive";
            std::error_code ec; std::filesystem::create_directories(I.dir, ec);
            I.started = I.stepStarted = pipingNow();
            I.report = {{"scenario", "siminteractive"}, {"ok", false},
                        {"actions", nlohmann::json::array()}, {"checks", nlohmann::json::array()},
                        {"skipped", nlohmann::json::array()}, {"not_run", nlohmann::json::array()},
                        {"captures", nlohmann::json::array()},
                        {"scope", "Bounded isolated real setup gestures and canonical persistence/draft conflicts; numerical native static/modal prose/object/SID/compound, thermal, hydraulic and piping evidence. CFD null/custom-pair cases prove JSON validation/persistence, not 3D CFD execution; not general solver certification"}};
            const char* daemon = std::getenv("CHUDWARE_ENGINE_DAEMON");
            if (!daemon || daemon[0] != '0') {
                siFail("isolation_required", "Use CHUDWARE_ENGINE_DAEMON=0 and short isolated XDG directories; this shot must not mutate a running user daemon.");
                return;
            }
        }
    }
    if (!I.want || I.done) return;
    ui::audit::observedItems();
    if (pipingNow() - I.started > 1200.0) { siFail("timeout", "scenario exceeded 1200 s including additional real FEA, hydraulic and piping solves"); return; }
    if (!st.workerStarted || st.frame < 5) return;
    if (siGestureTick(st)) return;
    if (I.settle > 0) { --I.settle; return; }
    // Native forms need bounded wheel/trickle-input navigation even at low offscreen render FPS.
    if (pipingNow() - I.stepStarted > 90.0) {
        siFail("step_timeout", shotNavigationEvidence().dump());
        return;
    }
    if (st.rpc.busy() || (st.simManager && !saIdle(st, *st.simManager))) return;
    if (I.afterCapture) {
        if (!I.capture.empty()) return;
        auto action = std::move(I.afterCapture);
        I.afterCapture = {};
        action(st);
        I.settle = 3;
        return;
    }
    if (I.step > 0 && (!st.simManager || st.docMirror.docId() != I.doc)) {
        if (!st.simManager) return;
        siFail("document_changed", "the isolated scenario part lost focus"); return;
    }
    auto check = [&](const char* name, bool ok, const nlohmann::json& evidence) {
        I.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
        if (!ok) siFail(name, evidence.dump());
        return ok;
    };
    auto placedAt = [&](const nlohmann::json& component, double x, double y) {
        const auto position = component.find("position");
        if (position == component.end() || !position->is_array() || position->size() != 2 ||
            !(*position)[0].is_number() || !(*position)[1].is_number()) return false;
        const auto& panel = st.simManager->hydraulic();
        const auto origin = panel.schematicScreenPosition(x, y);
        const auto unit = panel.schematicScreenPosition(x + 1.0, y + 1.0);
        const double px = (*position)[0].get<double>(), py = (*position)[1].get<double>();
        // ImGui quantizes pointer positions to whole pixels; native coordinates remain exact doubles.
        return std::isfinite(px) && std::isfinite(py) &&
               std::abs((px - x) * (unit.first - origin.first)) <= 1.0 &&
               std::abs((py - y) * (unit.second - origin.second)) <= 1.0;
    };
    ImVec2 p, q;
    switch (I.step) {
    case 0: {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { siFail("new_part", err); return; }
        st.clearViewportForNewDoc();
        st.addDocTab(false);
        I.partTab = st.activeTab;
        auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({{
            {"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Interactive setup domain"},
            {"params", {{"length", dim(60.0)}, {"width", dim(40.0)}, {"height", dim(10.0)},
                        {"centered", true}}}, {"refs", nlohmann::json::object()}}}}}));
        if (!r.ok) { siFail("box", r.error_message); return; }
        if (!r.aicm_path.empty() && !st.loadDocMesh(r.aicm_path, err)) { siFail("mesh", err); return; }
        I.doc = st.docMirror.docId();
        st.activeWorkspace = AppState::Workspace::Simulate;
        st.leftPaneTab = AppState::LP_Simulation;
        st.showRibbon = st.showSimulationTools = true;
        st.settings.copilotCollapsed = true;
        st.ribbonTab = kSaTabStatic;
        st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
        nlohmann::json fwStatus;
        std::string probeError;
        const bool fwProbe = st.worker.call("fw_status", nlohmann::json::object(), fwStatus, probeError, 10000);
        I.report["capabilities"]["firmware"] = {{"rpc_ok", fwProbe}, {"result", fwStatus}, {"error", probeError}};
        if (fwProbe && fwStatus.is_object() && !fwStatus.contains("code")) {
            const auto cortex = fwStatus.value("families", nlohmann::json::object()).value("cortex-m", nlohmann::json::object());
            if (cortex.contains("can_emulate") && cortex["can_emulate"].is_boolean() && !cortex["can_emulate"].get<bool>())
                I.report["skipped"].push_back({{"name", "firmware_cortex_m_backend"},
                    {"reason", "Native fw_status reports Cortex-M emulation unavailable."}, {"evidence", cortex}});
        }
        I.report["not_run"].push_back({{"name", "other_family_solver_execution"},
            {"reason", "External/internal CFD, circuit, EM, topology and motion checks prove setup/native store values, not solver execution. Real static/modal query/compound reruns, transient/steady thermal, hydraulic zero-length simulation, zero-load handoff and piping fluid-temperature solves are run."}});
        siNext(); break;
    }
    case 1:
        st.simManager->handleCommand(st, "sim_new_static");
        I.study = st.simManager->activeStudyId();
        if (I.study.empty()) { siFail("new_study", st.status); return; }
        st.simManager->configureActive(st, 0.25f, "steel_1018");
        st.selection.clear();
        st.simManager->handleCommand(st, "sim_fixture");
        siNext(); break;
    case 2:
        if (st.simManager->wantsViewportFacePick(st)) {
            if (!siSeek("Picking\xe2\x80\xa6", p)) return;
            siGesture(p, p, false, "Stop armed click picking before testing face drop");
            return;
        }
        if (!siSeek("Drop faces here", q) || !siFacePoint(st)) return;
        siGesture(I.facePoint, q, true, "Drag rendered face to fixture well");
        siNext(); break;
    case 3: {
        if (!siSeek("Face 1", p)) return;
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        const auto fixtures = persisted.value("fixtures", nlohmann::json::array());
        if (!check("face_drop_persisted", fixtures.size() == 1 &&
                   fixtures[0].value("_pick", nlohmann::json()) ==
                       nlohmann::json({{"body", I.face.body_id}, {"topo", I.face.topo_id}}), persisted)) return;
        I.before = persisted;
        I.faceSelector = fixtures[0];
        if (!siDragItems("Face 1", "Drop faces here", "Copy fixture face chip back to its well")) return;
        siNext(); break;
    }
    case 4: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        if (!check("duplicate_safe_copy", persisted.value("fixtures", nlohmann::json::array()) ==
                   I.before["fixtures"], persisted["fixtures"])) return;
        st.simManager->handleCommand(st, "sim_parameters");
        siNext(); break;
    }
    case 5:
        if (!siSeek("Force", p)) return;
        siGesture(p, I.facePoint, true, "Drag force setup tool onto rendered face");
        siNext(); break;
    case 6: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        const auto loads = persisted.value("loads", nlohmann::json::array());
        if (!check("palette_face_load_persisted", loads.size() == 1 &&
                   loads[0].value("type", std::string()) == "force" &&
                   loads[0].value("faces", nlohmann::json::array()).size() == 1 &&
                   loads[0]["faces"][0].value("_pick", nlohmann::json()) ==
                       nlohmann::json({{"body", I.face.body_id}, {"topo", I.face.topo_id}}), persisted)) return;
        I.before = st.simManager->activeStudyDefinition();
        st.simManager->handleCommand(st, "sim_parameters");
        I.capture = I.dir + "/01_face_and_palette.png";
        siGo(60); break;
    }
    case 7:
        if (siWriteText(ui::SimManager::parameterEditorView(I.edited).dump(2))) siNext();
        break;
    case 8:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Apply invalid native mesh order");
        siNext(); break;
    case 9:
        if (!siSeek("/mesh_order must be 1 or 2.", p)) return;
        if (!check("invalid_edit_reports_native_schema_error",
                   shotItem("/mesh_order must be 1 or 2.") != nullptr, ui::audit::observedItems())) return;
        if (!check("invalid_edit_is_atomic", st.simManager->activeStudyDefinition() == I.before,
                   st.simManager->activeStudyDefinition())) return;
        I.edited = I.before;
        I.edited["mesh_order"] = 1;
        I.edited["mesh_fineness"] = 0.25;
        I.edited["loads"][0]["vec"] = {0.0, 0.0, -321.0};
        I.edited["metadata"]["siminteractive"] = {{"owner", "agent"}, {"note", "retained-not-executed"}};
        siNext(); break;
    case 10:
        if (siWriteText(ui::SimManager::parameterEditorView(I.edited).dump(2))) siNext();
        break;
    case 11:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Apply exact native parameters with retained agent metadata");
        siNext(); break;
    case 12: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        if (!check("valid_edit_native_store", persisted.value("mesh_order", 0) == 1 &&
                   std::abs(persisted.value("mesh_fineness", 0.0) - 0.25) < 1e-6 &&
                   persisted["loads"][0]["vec"] == I.edited["loads"][0]["vec"] &&
                   persisted.value("metadata", nlohmann::json()) == I.edited["metadata"] &&
                   persisted["fixtures"] == I.before["fixtures"] &&
                   persisted["id"] == I.before["id"] && persisted["type"] == I.before["type"], persisted)) return;
        I.edited = persisted;
        I.capture = I.dir + "/02_native_parameters.png";
        std::string err;
        if (!st.docMirror.save(st.worker, I.dir + "/interactive.step+", err)) { siFail("save", err); return; }
        nlohmann::json reopened;
        if (!st.worker.call("doc_open", {{"path", I.dir + "/interactive.step+"}}, reopened, err) ||
            reopened.contains("code")) { siFail("reopen", err.empty() ? reopened.dump() : err); return; }
        const std::string did = reopened.value("doc_id", std::string());
        nlohmann::json roundtrip, closed;
        const bool read = siStudy(st, did, I.study, roundtrip);
        st.worker.call("doc_close", {{"doc_id", did}}, closed, err);
        if (!read) return;
        if (!check("save_reopen_native_values", roundtrip["fixtures"] == persisted["fixtures"] &&
                   roundtrip["loads"] == persisted["loads"] && roundtrip["metadata"] == persisted["metadata"] &&
                   roundtrip["mesh_order"] == persisted["mesh_order"], roundtrip)) return;
        I.fidelityBase = persisted;
        I.edited = persisted;
        I.edited["metadata"]["siminteractive"]["note"] = "dirty-draft-must-survive";
        siGo(100); break;
    }
    case 60:
        if (siFocusDraft()) siNext();
        break;
    case 61: {
        nlohmann::json draft;
        if (!siReadDraft(draft)) return;
        if (!check("pristine_draft_reload_after_gesture",
                   draft == ui::SimManager::parameterEditorView(I.before),
                   {{"rendered_draft", draft}, {"native_setup", I.before}})) return;
        I.edited = I.before;
        I.edited["mesh_order"] = 0;
        siGo(7); break;
    }
    case 100:
        if (siWriteText(ui::SimManager::parameterEditorView(I.edited).dump(2))) siNext();
        break;
    case 101:
        if (!siSeek("Pressure", p)) return;
        siGesture(p, I.facePoint, true, "Add a distinct pressure boundary while a genuinely edited native draft is open");
        siNext(); break;
    case 102: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        if (!check("dirty_conflict_has_actual_native_edit",
                   persisted["loads"].size() == I.fidelityBase["loads"].size() + 1, persisted)) return;
        I.before = st.simManager->activeStudyDefinition();
        if (siFocusDraft()) siNext();
        break;
    }
    case 103: {
        nlohmann::json draft;
        if (!siReadDraft(draft)) return;
        if (!check("dirty_draft_preserved_after_gesture",
                   draft == ui::SimManager::parameterEditorView(I.edited),
                   {{"rendered_draft", draft}, {"native_setup", I.before}})) return;
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Refuse Apply of stale dirty draft without overwriting real gesture");
        siNext(); break;
    }
    case 104: {
        const char* conflict = "Setup changed while this draft was open. Revert to reload current parameters.";
        if (!siSeek(conflict, p)) return;
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        if (!check("dirty_draft_stale_apply_refused", shotItem(conflict) != nullptr &&
                   st.simManager->activeStudyDefinition() == I.before &&
                   persisted["loads"] == I.before["loads"],
                   {{"native_setup", persisted}, {"rendered_items", ui::audit::observedItems()}})) return;
        if (siFocusDraft()) siNext();
        break;
    }
    case 105: {
        nlohmann::json draft;
        if (!siReadDraft(draft)) return;
        if (!check("dirty_draft_preserved_after_refusal",
                   draft == ui::SimManager::parameterEditorView(I.edited), draft)) return;
        if (!siSeek("Revert", p)) return;
        siGesture(p, p, false, "Explicitly reload current native setup after stale draft refusal");
        siNext(); break;
    }
    case 106:
        if (siFocusDraft()) siNext();
        break;
    case 107: {
        nlohmann::json draft;
        if (!siReadDraft(draft)) return;
        if (!check("dirty_draft_revert_reloads_current",
                   draft == ui::SimManager::parameterEditorView(I.before), draft)) return;
        I.fidelityCase = 0;
        siGo(120); break;
    }
    case 120: {
        if (I.fidelityCase == 1) {
            st.simManager->handleCommand(st, "sim_new_fatigue");
            I.study = st.simManager->activeStudyId();
        }
        I.before = st.simManager->activeStudyDefinition();
        I.edited = I.before;
        if (I.fidelityCase == 0) I.edited["material"] = "alu_6061";
        else {
            const char* tokens[] = {"reversed", "zero_based", "ratio", "ratio"};
            I.edited["fatigue"] = {{"load_type", tokens[I.fidelityCase - 1]}};
            if (I.fidelityCase >= 3)
                I.edited["fatigue"]["load_ratio"] = I.fidelityCase == 3 ? 0.2 : -0.4;
        }
        st.simManager->handleCommand(st, "sim_parameters");
        siNext(); break;
    }
    case 121:
        if (siWriteText(ui::SimManager::parameterEditorView(I.edited).dump(2))) siNext();
        break;
    case 122:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, I.fidelityCase == 0 ? "Apply library material string through native editor" :
                                                     "Apply native fatigue spectrum token without overriding cycle factors");
        siNext(); break;
    case 123: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        if (I.fidelityCase == 0) {
            if (!check("library_material_string_updates_document_and_setup",
                       st.docMirror.tree().value("material", nlohmann::json::object()).value("key", std::string()) == "alu_6061" &&
                       persisted.value("material", nlohmann::json()) == "alu_6061" &&
                       st.simManager->activeStudyDefinition().value("material", nlohmann::json()) == "alu_6061",
                       {{"document_material", st.docMirror.tree().value("material", nlohmann::json())},
                        {"native_setup", persisted}})) return;
        } else {
            const auto spectrum = persisted.value("fatigue", nlohmann::json::object());
            if (!check("fatigue_native_token_accepted_without_cycle_override", spectrum == I.edited["fatigue"] &&
                       !spectrum.contains("lmin") && !spectrum.contains("lmax"), persisted)) return;
            std::string err;
            const std::string path = I.dir + "/fatigue_" + std::to_string(I.fidelityCase) + ".step+";
            if (!st.docMirror.save(st.worker, path, err)) { siFail("fatigue_save", err); return; }
            nlohmann::json opened, roundtrip, closed;
            if (!st.worker.call("doc_open", {{"path", path}}, opened, err) || opened.contains("code")) {
                siFail("fatigue_reopen", err.empty() ? opened.dump() : err); return;
            }
            const std::string did = opened.value("doc_id", std::string());
            const bool read = siStudy(st, did, I.study, roundtrip);
            st.worker.call("doc_close", {{"doc_id", did}}, closed, err);
            if (!read || !check("fatigue_native_token_reopened_without_cycle_override",
                               roundtrip.value("fatigue", nlohmann::json()) == spectrum, roundtrip)) return;
        }
        if (++I.fidelityCase < 5) siGo(120);
        else {
            st.simManager->handleCommand(st, "sim_new_static");
            I.study = st.simManager->activeStudyId();
            I.before = st.simManager->activeStudyDefinition();
            I.edited = I.before;
            I.edited.erase("order");
            I.edited["mesh_order"] = 1;
            std::string error;
            if (!st.simManager->configureActiveStudy(st, I.edited, error)) { siFail("alias_baseline", error); return; }
            I.before = st.simManager->activeStudyDefinition();
            I.edited = I.before;
            I.edited["mesh_order"] = 2;
            I.edited["order"] = 1; // independently added alias conflicts with edited canonical key
            st.simManager->handleCommand(st, "sim_parameters");
            siGo(125);
        }
        break;
    }
    case 125:
        if (siWriteText(ui::SimManager::parameterEditorView(I.edited).dump(2))) siNext();
        break;
    case 126:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Refuse independently conflicting native mesh aliases");
        siNext(); break;
    case 127: {
        const char* prefix = "/mesh_order and /order conflict; give both the same element order.";
        const auto* status = siStatusPrefix(prefix, false);
        if (!status) return;
        const auto label = status->value("label", std::string());
        if (!siSeek(label.c_str(), p)) return;
        if (!check("mesh_alias_conflict_reports_actual_error", siStatusPrefix(prefix) != nullptr &&
                   st.simManager->activeStudyDefinition() == I.before, ui::audit::observedItems())) return;
        I.fidelityCase = 0;
        siGo(140); break;
    }
    case 140: {
        // Eight additional real FEA solves: sampled modal reference, prose/object/SID modal
        // reruns, prose/object/terminal-compound static reruns and nondefault-ambient thermal.
        // Query fixture authoring is canonical; every changed definition still uses UI Apply/Run.
        const bool modal = I.fidelityCase <= 2 || I.fidelityCase == 5, thermal = I.fidelityCase == 7;
        if (I.fidelityCase == 0) {
            nlohmann::json queried, sampled;
            std::string error;
            if (!st.worker.call("doc_query", {{"doc_id", I.doc},
                       {"query", {{"of", "face"}, {"side", "left"}}}}, queried, error) ||
                queried.contains("code") || queried.value("matches", nlohmann::json::array()).size() != 1) {
                siFail("modal_reference_face_query", error.empty() ? queried.dump() : error); return;
            }
            if (!st.worker.call("doc_face_points", {{"doc_id", I.doc}, {"selector", queried["matches"][0]}},
                                sampled, error) || sampled.contains("code") ||
                sampled.value("points", nlohmann::json::array()).empty()) {
                siFail("modal_reference_sampling", error.empty() ? sampled.dump() : error); return;
            }
            I.terminalFixture = sampled;
            I.report["modal_reference_boundary"] = {{"query_matches", queried}, {"sampled_faceset", sampled}};
            I.fixtureSid = queried["matches"][0].value("sid", std::string());
            if (I.fixtureSid.empty()) { siFail("modal_reference_stable_sid", queried.dump()); return; }
            if (!st.worker.call("doc_query", {{"doc_id", I.doc},
                    {"query", {{"of", "face"}, {"side", "right"}}}}, queried, error) ||
                queried.contains("code") || queried.value("matches", nlohmann::json::array()).size() != 1 ||
                !st.worker.call("doc_face_points", {{"doc_id", I.doc}, {"selector", queried["matches"][0]}},
                                sampled, error) || sampled.contains("code") ||
                sampled.value("points", nlohmann::json::array()).empty()) {
                siFail("compound_reference_load_sampling", error.empty() ? queried.dump() : error); return;
            }
            I.terminalLoad = sampled;
            I.report["static_reference_load_boundary"] = {{"query_matches", queried}, {"sampled_faceset", sampled}};
        }
        st.simManager->handleCommand(st, thermal ? "sim_new_thermal_fea" :
                                            modal ? "sim_new_modal" : "sim_new_static");
        I.study = st.simManager->activeStudyId();
        I.edited = st.simManager->activeStudyDefinition();
        I.edited["mesh_order"] = modal ? 2 : 1;
        I.edited["order"] = modal ? 2 : 1;
        I.edited["fineness"] = 0.2;
        I.edited["learned_advisor"] = false;
        I.edited["material"] = thermal ?
            nlohmann::json{{"k_w_mk", 160.0}, {"cp_j_kgk", 900.0}, {"density_g_cm3", 2.7}} :
            nlohmann::json{{"E_mpa", 210000.0}, {"nu", 0.3}, {"density_g_cm3", 7.85}, {"yield_mpa", 250.0}};
        nlohmann::json fixture;
        if (I.fidelityCase == 0) fixture = I.terminalFixture;
        else if (I.fidelityCase == 5) fixture = {{"face", I.fixtureSid}};
        else if (I.fidelityCase == 6) fixture = {{"facesets", {I.terminalFixture, I.terminalFixture}}};
        else if (I.fidelityCase == 2 || I.fidelityCase == 4)
            fixture = {{"face", {{"of", "face"}, {"side", "left"}}}};
        else fixture = {{"face", "left"}};
        if (thermal) fixture["temp_c"] = 20.0;
        I.edited["fixtures"] = nlohmann::json::array({fixture});
        I.edited["loads"] = nlohmann::json::array();
        if (!modal) {
            const nlohmann::json face = I.fidelityCase == 4 ?
                nlohmann::json{{"of", "face"}, {"side", "right"}} : nlohmann::json("right");
            I.edited["loads"].push_back(thermal ?
                nlohmann::json{{"type", "convection"}, {"face", face}, {"h", 10.0}, {"ambient_c", 50.0}} :
                nlohmann::json{{"type", "force"}, {"face", face}, {"force", {0.0, 0.0, -100.0}}});
            if (I.fidelityCase == 6)
                I.edited["loads"] = nlohmann::json::array({{{"type", "force"},
                    {"faces", nlohmann::json::array({{{"facesets", {I.terminalLoad, I.terminalLoad}}}})},
                    {"force", {0.0, 0.0, -100.0}}}});
        }
        if (thermal) I.edited.erase("transient");
        else I.edited["n_modes"] = 3;
        std::string error;
        if (!st.simManager->configureActiveStudy(st, I.edited, error)) {
            siFail("native_boundary_fixture", error); return;
        }
        if (I.fidelityCase == 0) {
            const auto before = st.simManager->activeStudyDefinition();
            auto blocked = before;
            blocked["mesh_order"] = 1; blocked["order"] = 1;
            st.rpc.call("doc_tree", {{"doc_id", I.doc}}, [](const core::RpcResult& r) {
                g_si.report["document_read_probe_completed"] = r.ok;
            });
            const bool refused = !st.simManager->configureActiveStudy(st, blocked, error);
            nlohmann::json persisted;
            if (!siStudy(st, I.doc, I.study, persisted)) return;
            if (!check("document_request_blocks_setup_edit_atomically",
                       refused && st.simManager->activeStudyDefinition() == before &&
                       persisted.value("mesh_order", 0) == before.value("mesh_order", 0) &&
                       persisted.value("order", 0) == before.value("order", 0), persisted)) return;
        }
        I.edited = st.simManager->activeStudyDefinition();
        // Deliberately change just one of an already-retained pair. Apply must synchronize
        // the other name, and the real solver's mesh_quality.order must agree, not just JSON.
        if (!thermal) I.edited[I.fidelityCase == 1 || I.fidelityCase == 3 ? "order" : "mesh_order"] = modal ? 1 : 2;
        else I.edited["loads"][0]["ambient_c"] = 75.0;
        st.simManager->handleCommand(st, "sim_parameters");
        siNext(); break;
    }
    case 141:
        if (siWriteText(I.edited.dump(2))) siNext();
        break;
    case 142: {
        if (!siSeek("Apply parameters", p)) return;
        nlohmann::json draft;
        if (!siReadDraft(draft)) return;
        if (!check("query_rerun_rendered_draft_matches_authored", draft == I.edited,
                   {{"case", I.fidelityCase}, {"rendered_draft", draft}, {"authored", I.edited}})) return;
        siGesture(p, p, false, "Apply canonical query boundaries and native mesh/ambient aliases");
        I.metadataDuringApply = I.fidelityCase == 0;
        siNext(); break;
    }
    case 143: {
        const auto definition = st.simManager->activeStudyDefinition();
        const bool applied = definition["fixtures"] == I.edited["fixtures"] &&
            definition["loads"] == I.edited["loads"] &&
            (I.fidelityCase == 7 ||
             (definition.value("mesh_order", 0) == (I.fidelityCase <= 2 || I.fidelityCase == 5 ? 1 : 2) &&
              definition.value("order", 0) == (I.fidelityCase <= 2 || I.fidelityCase == 5 ? 1 : 2)));
        if (I.fidelityCase == 0 &&
            !check("global_metadata_poll_does_not_cancel_native_apply",
                   I.metadataQueued && applied, definition)) return;
        if (!check("query_rerun_native_editor_applied", applied,
                   {{"case", I.fidelityCase}, {"definition", definition}, {"authored", I.edited},
                    {"observed_items", ui::audit::observedItems()}})) return;
        const char* run = I.fidelityCase <= 2 || I.fidelityCase == 5 ? "Run Modal" :
                          I.fidelityCase == 7 ? "Run Thermal FEA" : "Run Static";
        if (!siSeek(run, p)) return;
        siGesture(p, p, false, "Run actual native solver from observed GUI footer with authored boundaries");
        siNext(); break;
    }
    case 144: {
        if (st.simManager->runUi().running || !st.simClient) return;
        // Structural results are projected into Study fields rather than advResult. Observe
        // the shared native completion verbatim, but only once THIS study has its own result.
        if (st.simManager->activeStudyDefinition().value("result_summary", std::string()).empty()) return;
        nlohmann::json result;
        {
            auto& job = st.simClient->job();
            std::lock_guard<std::mutex> lock(job.mtx);
            const char* expected = I.fidelityCase <= 2 || I.fidelityCase == 5 ? "sim_run_modal" :
                                   I.fidelityCase == 7 ? "sim_run_thermal" : "sim_run_static";
            if (job.ownerDocId != I.doc || job.method != expected) return;
            if (!job.ok) { siFail("fidelity_solver_refused", job.error); return; }
            result = job.result;
        }
        if (!result.is_object() || result.empty()) return;
        if (!check("fidelity_rerun_native_result_ok", result.value("ok", false) &&
                   result.value("mesh_quality", nlohmann::json::object()).value("n_elems", 0) > 0, result)) return;
        if (I.fidelityCase <= 2 || I.fidelityCase == 5) {
            const auto freqs = result.value("freqs_hz", nlohmann::json::array());
            bool finite = freqs.size() >= 3;
            for (const auto& f : freqs) finite &= f.is_number() && std::isfinite(f.get<double>()) && f.get<double>() > 1.0;
            if (!check("modal_fixture_resolves_positive_native_modes", finite, result)) return;
            if (I.fidelityCase == 0) I.modalReference = result;
            else {
                const auto reference = I.modalReference.value("freqs_hz", nlohmann::json::array());
                bool equal = reference.size() == freqs.size();
                if (equal) for (size_t k = 0; k < freqs.size(); ++k)
                    equal &= std::abs(freqs[k].get<double>() - reference[k].get<double>()) <=
                             std::max(1e-5, std::abs(reference[k].get<double>()) * 1e-6);
                if (!check(I.fidelityCase == 1 ? "modal_free_text_query_matches_sampled_boundary_solver" :
                           I.fidelityCase == 5 ? "modal_stable_sid_query_matches_sampled_boundary_solver" :
                                                "modal_structured_query_matches_sampled_boundary_solver",
                           equal, {{"reference_native_result", I.modalReference}, {"query_native_result", result},
                                   {"sampled_reference_boundary", I.terminalFixture}})) return;
            }
        } else if (I.fidelityCase < 7) {
            const auto reactions = result.value("reactions", nlohmann::json::object());
            const auto applied = reactions.value("applied_force_n", nlohmann::json::array());
            const auto reaction = reactions.value("reaction_force_n", nlohmann::json::array());
            const double displacement = result.value("summary", nlohmann::json::object()).value("max_disp_mm", 0.0);
            const bool balanced = applied.size() == 3 && reaction.size() == 3 &&
                std::abs(applied[0].get<double>()) < 1e-6 && std::abs(applied[1].get<double>()) < 1e-6 &&
                std::abs(applied[2].get<double>() + 100.0) < 1e-5 &&
                std::abs(reaction[2].get<double>() - 100.0) < 0.1 &&
                reactions.value("equilibrium_error_pct", 100.0) < 0.1 &&
                std::isfinite(displacement) && displacement > 0.0;
            if (!check(I.fidelityCase == 3 ? "static_free_text_fixture_load_queries_execute" :
                       I.fidelityCase == 6 ? "static_terminal_compound_fixture_load_executes_one_resultant" :
                                            "static_structured_fixture_load_queries_execute", balanced, result)) return;
            if (I.fidelityCase == 4) {
                I.staticReference = result;
                I.staticSetup = st.simManager->activeStudyDefinition();
            }
            if (I.fidelityCase == 6) {
                const double reference = I.staticReference["summary"].value("max_disp_mm", 0.0);
                if (!check("static_compound_duplicate_leaves_match_native_reference_solution",
                           reference > 0.0 && std::abs(displacement - reference) < std::max(1e-6, reference * 1e-5),
                           {{"reference_native_result", I.staticReference}, {"compound_native_result", result}})) return;
            }
        } else {
            bool ambientResolved = false;
            for (const auto& boundary : result.value("flux_summary", nlohmann::json::array()))
                ambientResolved |= boundary.value("type", std::string()) == "convection" &&
                    boundary.value("n_nodes", 0) > 0 &&
                    std::abs(boundary.value("t_amb_c", 0.0) - 75.0) < 1e-8 &&
                    std::abs(boundary.value("h_w_m2k", 0.0) - 10.0) < 1e-8;
            const auto summary = result.value("summary", nlohmann::json::object());
            // A nonzero film/fixture temperature difference is required by the
            // native steady solver. Check the exact insulated 1-D slab solution,
            // which also detects loss of the authored conductivity or ambient.
            const double expectedMax = (160.0 / 0.06 * 20.0 + 10.0 * 75.0) /
                                       (160.0 / 0.06 + 10.0);
            if (!check("thermal_ambient_alias_native_boundary_and_slab_solution", ambientResolved &&
                       !result.value("transient", false) &&
                       std::abs(summary.value("max_temp_c", 0.0) - expectedMax) < 1e-3 &&
                       std::abs(summary.value("min_temp_c", 0.0) - 20.0) < 1e-3, result)) return;
        }
        if (I.fidelityCase < 7 &&
            !check("edited_mesh_aliases_match_actual_native_element_order",
                   result["mesh_quality"].value("order", 0) == (I.fidelityCase <= 2 || I.fidelityCase == 5 ? 1 : 2),
                   {{"native_setup", st.simManager->activeStudyDefinition()}, {"native_result", result}})) return;
        I.report["native_fidelity_runs"].push_back({{"case", I.fidelityCase},
            {"definition", st.simManager->activeStudyDefinition()}, {"native_result", result}});
        I.capture = I.dir + "/fidelity_" + std::to_string(I.fidelityCase) + "_native_result.png";
        if (++I.fidelityCase < 8) {
            I.afterCapture = [](AppState&) { siGo(140); };
        } else {
            I.afterCapture = [](AppState&) { g_si.fidelityCase = 0; siGo(150); };
        }
        break;
    }
    case 150:
        st.simManager->handleCommand(st, "sim_new_static");
        I.study = st.simManager->activeStudyId();
        I.edited = st.simManager->activeStudyDefinition();
        I.edited["material"] = I.staticSetup["material"];
        I.edited["mesh_order"] = 2;
        I.edited["order"] = 2;
        I.edited["fineness"] = 0.2;
        I.edited["fixtures"] = nlohmann::json::array({I.terminalFixture});
        I.edited["loads"] = nlohmann::json::array({{{"type", "force"},
            {"faces", nlohmann::json::array({I.terminalLoad})}, {"force", {0.0, 0.0, -100.0}}}});
        if (I.fidelityCase == 0)
            I.edited["fixtures"] = nlohmann::json::array({{{"facesets", nlohmann::json::array()}}});
        else if (I.fidelityCase == 1)
            I.edited["fixtures"] = nlohmann::json::array({{{"facesets",
                nlohmann::json::array({{{"facesets", nlohmann::json::array({I.terminalFixture})}}})}}});
        else
            I.edited["loads"][0]["faces"] = nlohmann::json::array({{{"facesets", {I.terminalLoad}}, {"tol_mm", 1.0}}});
        st.simManager->handleCommand(st, "sim_parameters");
        siNext(); break;
    case 151:
        if (siWriteText(I.edited.dump(2))) siNext();
        break;
    case 152:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Apply malformed compound fixture/load to inspect real disabled Run readiness");
        siNext(); break;
    case 153: {
        const auto definition = st.simManager->activeStudyDefinition();
        if (!check("malformed_compound_native_editor_definition_applied",
                   definition["fixtures"] == I.edited["fixtures"] && definition["loads"] == I.edited["loads"],
                   definition)) return;
        nlohmann::json disabled;
        for (const auto& item : ui::audit::observedItems())
            if (shotLabel(item, "Run Static") && item.value("visible", false) && !item.value("enabled", true))
                disabled = item;
        if (!check("malformed_compound_real_rendered_run_disabled", disabled.is_object() &&
                   !disabled.empty() && !st.simManager->runUi().running,
                   {{"case", I.fidelityCase}, {"rendered_run_control", disabled},
                    {"rendered_items", ui::audit::observedItems()}, {"native_setup", definition}})) return;
        I.capture = I.dir + "/malformed_compound_" + std::to_string(I.fidelityCase) + "_run_disabled.png";
        if (++I.fidelityCase < 3) I.afterCapture = [](AppState&) { siGo(150); };
        else I.afterCapture = [](AppState& state) {
            state.simManager->handleCommand(state, "sim_hyd_new");
            state.ribbonTab = ui::kRibbonTabHydraulics;
            siGo(13);
        };
        break;
    }
    case 13: {
        auto& hp = st.simManager->hydraulic();
        if (!hp.circuit().is_object() || hp.circuit().value("components", nlohmann::json::array()).is_null()) return;
        if (!hp.lastError().empty()) { siFail("hydraulic_new", hp.lastError()); return; }
        if (!check("hydraulic_canvas_gestures_start_blank",
                   hp.circuit().value("components", nlohmann::json::array()).empty() &&
                   hp.circuit().value("hoses", nlohmann::json::array()).empty(), hp.circuit())) return;
        st.simManager->handleCommand(st, "sim_hyd_add");
        hp.setCentralView(ui::HydraulicPanel::CentralView::Schematic);
        siNext(); break;
    }
    case 14: {
        if (!siSeek("###hyd_canvas_palette_pump", p, "hyd_canvas_palette")) {
            if (pipingNow() - I.stepStarted > 10.0)
                siFail("hydraulic_canvas_pump_toolbar_missing", ui::audit::observedItems().dump());
            return;
        }
        bool palette = false;
        for (const auto& item : ui::audit::observedItems())
            if (item.value("native_label", std::string()) == "###hyd_canvas_palette_pump" &&
                item.value("visible", false) && item.value("enabled", true)) {
                p = shotPoint(item); palette = true; break;
            }
        if (!check("hydraulic_canvas_pump_toolbar_source", palette, ui::audit::observedItems())) return;
        const auto pos = st.simManager->hydraulic().schematicScreenPosition(-80.0, 0.0);
        siGesture(p, ImVec2(pos.first, pos.second), true, "Drag Pump from the in-canvas ISO toolbar");
        siNext(); break;
    }
    case 15: {
        const auto& components = st.simManager->hydraulic().circuit().value("components", nlohmann::json::array());
        if (!check("hydraulic_palette_placement", components.size() == 1 &&
                   components[0].value("type", std::string()) == "pump" &&
                   placedAt(components[0], -80.0, 0.0), components)) return;
        I.pump = components[0].value("id", std::string());
        if (!siSeek("###hyd_canvas_palette_reservoir", p, "hyd_canvas_palette")) {
            if (pipingNow() - I.stepStarted > 10.0)
                siFail("hydraulic_canvas_reservoir_toolbar_missing", ui::audit::observedItems().dump());
            return;
        }
        bool palette = false;
        for (const auto& item : ui::audit::observedItems())
            if (item.value("native_label", std::string()) == "###hyd_canvas_palette_reservoir" &&
                item.value("visible", false) && item.value("enabled", true)) {
                p = shotPoint(item); palette = true; break;
            }
        if (!check("hydraulic_canvas_reservoir_toolbar_source", palette, ui::audit::observedItems())) return;
        // The schematic fits the first component; keep the second drop inside that visible view.
        const auto pos = st.simManager->hydraulic().schematicScreenPosition(-20.0, 0.0);
        siGesture(p, ImVec2(pos.first, pos.second), true, "Drag Reservoir from the in-canvas ISO toolbar");
        siNext(); break;
    }
    case 16: {
        auto& hp = st.simManager->hydraulic();
        const auto components = hp.circuit().value("components", nlohmann::json::array());
        bool positioned = false;
        for (const auto& c : components)
            if (c.value("type", std::string()) == "reservoir") {
                I.tank = c.value("id", std::string());
                positioned = placedAt(c, -20.0, 0.0);
            }
        if (!check("hydraulic_second_placement", components.size() == 2 && positioned, components)) return;
        I.report["hydraulic_before_click_connection"] = hp.circuit();
        const auto pos = hp.portScreenPosition(I.pump, "T");
        siGesture(ImVec2(pos.first, pos.second), ImVec2(pos.first, pos.second), false, "Pick pump T port");
        siNext(); break;
    }
    case 17: {
        const auto& hp = st.simManager->hydraulic();
        if (!check("hydraulic_first_port_click_stages_without_hose",
                   hp.selectedComponentId() == I.pump &&
                   hp.circuit() == I.report["hydraulic_before_click_connection"],
                   {{"selected_component", hp.selectedComponentId()}, {"circuit", hp.circuit()}})) return;
        const auto pos = st.simManager->hydraulic().portScreenPosition(I.tank, "S");
        siGesture(ImVec2(pos.first, pos.second), ImVec2(pos.first, pos.second), false, "Pick reservoir suction port");
        siNext(); break;
    }
    case 18: {
        const auto hoses = st.simManager->hydraulic().circuit().value("hoses", nlohmann::json::array());
        const auto expected = nlohmann::json::array({
            {{"component_id", I.pump}, {"port", "T"}},
            {{"component_id", I.tank}, {"port", "S"}}});
        if (!check("hydraulic_second_port_click_connects", hoses.size() == 1 &&
                   hoses[0].value("connections", nlohmann::json::array()) == expected, hoses)) return;
        siNext(); break;
    }
    case 19: {
        auto& hp = st.simManager->hydraulic();
        const auto hoses = hp.circuit().value("hoses", nlohmann::json::array());
        const auto expectedEnds = nlohmann::json::array({
            {{"component_id", I.pump}, {"port", "T"}},
            {{"component_id", I.tank}, {"port", "S"}}});
        if (!check("hydraulic_port_connection", hoses.size() == 1 &&
                   hoses[0].value("connections", nlohmann::json::array()) == expectedEnds, hoses)) return;
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        if (!check("hydraulic_native_store", stored.at("params").at("circuit") == hp.circuit(), stored)) return;
        I.before = hp.circuit();
        nlohmann::json cad;
        std::string error;
        if (!st.worker.call("doc_tree", {{"doc_id", I.doc}}, cad, error, 15000) ||
            cad.contains("code") || !cad.contains("tree") || !cad["tree"].is_object() ||
            !cad.contains("revision") || !cad["revision"].is_number_integer()) {
            siFail("hydraulic_cad_history_baseline_read", error.empty() ? cad.dump() : error); return;
        }
        auto geometry = cad["tree"];
        geometry.erase("studies");
        I.report["hydraulic_keyboard_cad_baseline"] = {
            {"scope", "CAD geometry/mirror history must remain unchanged; each native hydraulic study save intentionally advances the document revision once."},
            {"doc_id", I.doc}, {"native_revision", cad["revision"]},
            {"mirror_revision", st.docMirror.revision()}, {"geometry", std::move(geometry)},
            {"mirror_features", st.docMirror.tree().value("features", nlohmann::json::array())}};
        I.report["hydraulic_connected_before_delete"] = I.before;
        const auto centre = hp.schematicScreenPosition(-80.0, 0.0);
        p = ImVec2(centre.first, centre.second);
        siGesture(p, p, false, "Select canvas pump before pressing Delete");
        siGo(300); break;
    }
    case 300: {
        auto& hp = st.simManager->hydraulic();
        if (!check("hydraulic_canvas_pump_selected_for_delete",
                   hp.selectedComponentId() == I.pump && hp.canUndo(),
                   {{"selected_component", hp.selectedComponentId()}, {"pump", I.pump},
                    {"can_undo", hp.canUndo()}})) return;
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        io.AddKeyEvent(ImGuiKey_Delete, true);
        I.report["actions"].push_back({{"action", "Delete the selected pump using the canvas keyboard"},
                                       {"key", "Delete"}, {"pointer", {I.source.x, I.source.y}}});
        siNext(); break;
    }
    case 301: {
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        if (!ImGui::IsKeyDown(ImGuiKey_Delete)) return;
        io.AddKeyEvent(ImGuiKey_Delete, false);
        siNext(); break;
    }
    case 302: {
        if (ImGui::IsKeyDown(ImGuiKey_Delete)) return;
        auto& hp = st.simManager->hydraulic();
        const auto& circuit = hp.circuit();
        const auto components = circuit.value("components", nlohmann::json::array());
        const auto hoses = circuit.value("hoses", nlohmann::json::array());
        if (!check("hydraulic_canvas_delete_removes_component_and_dangling_hose",
                   components.size() == 1 && components[0].value("id", std::string()) == I.tank &&
                   hoses.empty(), circuit)) return;
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        if (!check("hydraulic_canvas_delete_persisted", stored.at("params").at("circuit") == circuit, stored)) return;
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        io.AddKeyEvent(ImGuiMod_Ctrl, true);
        io.AddKeyEvent(ImGuiKey_Z, true);
        I.report["actions"].push_back({{"action", "Undo canvas Delete using Ctrl+Z"},
                                       {"key", "Ctrl+Z"}, {"pointer", {I.source.x, I.source.y}}});
        siNext(); break;
    }
    case 303: {
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        if (!io.KeyCtrl || !ImGui::IsKeyDown(ImGuiKey_Z)) return;
        io.AddKeyEvent(ImGuiKey_Z, false);
        io.AddKeyEvent(ImGuiMod_Ctrl, false);
        siNext(); break;
    }
    case 304: {
        if (ImGui::GetIO().KeyCtrl || ImGui::IsKeyDown(ImGuiKey_Z)) return;
        auto& hp = st.simManager->hydraulic();
        if (!check("hydraulic_canvas_ctrl_z_restores_exact_circuit", hp.circuit() == I.before,
                   {{"before_delete", I.before}, {"restored", hp.circuit()}})) return;
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        if (!check("hydraulic_canvas_ctrl_z_restoration_persisted",
                   stored.at("params").at("circuit") == I.before, stored)) return;
        nlohmann::json cad;
        std::string error;
        if (!st.worker.call("doc_tree", {{"doc_id", I.doc}}, cad, error, 15000) ||
            cad.contains("code") || !cad.contains("tree") || !cad["tree"].is_object()) {
            siFail("hydraulic_cad_history_after_undo_read", error.empty() ? cad.dump() : error); return;
        }
        auto geometry = cad["tree"];
        geometry.erase("studies");
        const auto& baseline = I.report["hydraulic_keyboard_cad_baseline"];
        const auto mirrorFeatures = st.docMirror.tree().value("features", nlohmann::json::array());
        if (!check("hydraulic_canvas_ctrl_z_preserves_cad_document_history",
                   st.docMirror.docId() == I.doc &&
                   st.docMirror.revision() == baseline["mirror_revision"] &&
                   mirrorFeatures == baseline["mirror_features"] && geometry == baseline["geometry"],
                   {{"baseline", baseline}, {"mirror_revision_after", st.docMirror.revision()},
                    {"native_revision_after", cad.value("revision", -1)},
                    {"mirror_features_after", mirrorFeatures}, {"geometry_after", geometry}})) return;
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        io.AddKeyEvent(ImGuiMod_Ctrl, true);
        io.AddKeyEvent(ImGuiKey_Z, true);
        I.report["actions"].push_back({{"action", "Undo the two-click connection using Ctrl+Z before drag-connect"},
                                       {"key", "Ctrl+Z"}, {"pointer", {I.source.x, I.source.y}}});
        siNext(); break;
    }
    case 305: {
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(I.source.x, I.source.y);
        if (!io.KeyCtrl || !ImGui::IsKeyDown(ImGuiKey_Z)) return;
        io.AddKeyEvent(ImGuiKey_Z, false);
        io.AddKeyEvent(ImGuiMod_Ctrl, false);
        siNext(); break;
    }
    case 306: {
        if (ImGui::GetIO().KeyCtrl || ImGui::IsKeyDown(ImGuiKey_Z)) return;
        auto& hp = st.simManager->hydraulic();
        if (!check("hydraulic_canvas_ctrl_z_removes_click_connection",
                   hp.circuit() == I.report["hydraulic_before_click_connection"],
                   {{"before_click_connection", I.report["hydraulic_before_click_connection"]},
                    {"after_undo", hp.circuit()}})) return;
        const auto a = hp.portScreenPosition(I.pump, "T");
        const auto b = hp.portScreenPosition(I.tank, "S");
        if (!check("hydraulic_canvas_drag_connection_port_positions",
                   std::isfinite(a.first) && std::isfinite(a.second) &&
                   std::isfinite(b.first) && std::isfinite(b.second) &&
                   (a.first != 0.0f || a.second != 0.0f) && (b.first != 0.0f || b.second != 0.0f) &&
                   std::hypot(a.first - b.first, a.second - b.second) > ImGui::GetIO().MouseDragThreshold,
                   {{"pump_T", {a.first, a.second}}, {"reservoir_S", {b.first, b.second}}})) return;
        I.source = ImVec2(a.first, a.second);
        I.target = ImVec2(b.first, b.second);
        I.phase = 0;
        I.report["actions"].push_back({
            {"action", "Drag directly from pump suction port to reservoir suction port"},
            {"input", "held_pointer_drag_without_imgui_payload"},
            {"source", {I.source.x, I.source.y}}, {"target", {I.target.x, I.target.y}}});
        ImGui::GetIO().AddMousePosEvent(I.source.x, I.source.y);
        siGo(308); break;
    }
    case 308: {
        auto& io = ImGui::GetIO();
        const auto at = [&](ImVec2 point) {
            return std::fabs(io.MousePos.x - point.x) <= 2.0f &&
                   std::fabs(io.MousePos.y - point.y) <= 2.0f;
        };
        const ImVec2 midpoint((I.source.x + I.target.x) * 0.5f,
                              (I.source.y + I.target.y) * 0.5f);
        // Port wiring tracks a held pointer, not an ImGui drag/drop payload.
        // Consume each input frame and hold over the target before releasing.
        if (I.phase == 0) {
            if (!at(I.source)) {
                io.AddMousePosEvent(I.source.x, I.source.y);
                return;
            }
            io.AddMouseButtonEvent(0, true);
            I.phase = 1;
        } else if (I.phase == 1) {
            if (!io.MouseDown[0]) return;
            auto& hp = st.simManager->hydraulic();
            if (!check("hydraulic_canvas_drag_press_stages_without_hose",
                       hp.selectedComponentId() == I.pump &&
                       hp.circuit() == I.report["hydraulic_before_click_connection"],
                       {{"selected_component", hp.selectedComponentId()}, {"circuit", hp.circuit()}})) return;
            io.AddMousePosEvent(midpoint.x, midpoint.y);
            I.phase = 2;
        } else if (I.phase == 2) {
            if (!io.MouseDown[0] || !at(midpoint)) return;
            io.AddMousePosEvent(I.target.x, I.target.y);
            I.phase = 3;
        } else if (I.phase == 3) {
            if (!io.MouseDown[0] || !at(I.target)) return;
            const auto& circuit = st.simManager->hydraulic().circuit();
            const ImVec2 delta = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
            if (!check("hydraulic_canvas_drag_held_over_target_before_release",
                       ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
                       circuit == I.report["hydraulic_before_click_connection"],
                       {{"drag_delta", {delta.x, delta.y}}, {"circuit", circuit}})) return;
            I.phase = 4;
        } else if (I.phase == 4) {
            io.AddMouseButtonEvent(0, false);
            I.phase = 5;
        } else {
            if (io.MouseDown[0]) return;
            siGo(307);
        }
        break;
    }
    case 307: {
        auto& hp = st.simManager->hydraulic();
        const auto hoses = hp.circuit().value("hoses", nlohmann::json::array());
        const auto expectedEnds = nlohmann::json::array({
            {{"component_id", I.pump}, {"port", "T"}},
            {{"component_id", I.tank}, {"port", "S"}}});
        if (!check("hydraulic_canvas_drag_release_connects_ports",
                   hoses.size() == 1 && hoses[0].value("connections", nlohmann::json::array()) == expectedEnds &&
                   hp.circuit().value("components", nlohmann::json::array()) == I.before["components"],
                   hp.circuit())) return;
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        if (!check("hydraulic_canvas_drag_connection_persisted",
                   stored.at("params").at("circuit") == hp.circuit(), stored)) return;
        I.hose = hoses[0].value("id", std::string());
        I.report["hydraulic_drag_created_hose"] = hoses[0];
        if (!check("hydraulic_drag_created_hose_identity",
                   !I.hose.empty() && !hoses[0].value("net_name", std::string()).empty(),
                   hoses[0])) return;
        I.fidelityCase = 0;
        I.capture = I.dir + "/03_hydraulic_gestures.png";
        siGo(200); break;
    }
    case 200:
        if (!st.simManager->hydraulic().setComponentParams(st, I.pump,
                {{"pump_type", "gear"}, {"displacement_cc_rev", 16.0}, {"speed_rpm", 1450.0},
                 {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}})) {
            siFail("hydraulic_pump_fixture", st.simManager->hydraulic().lastError()); return;
        }
        siGo(197); break;
    case 197:
        if (!st.simManager->hydraulic().setComponentParams(st, I.tank, {{"volume_l", 60.0}})) {
            siFail("hydraulic_reservoir_fixture", st.simManager->hydraulic().lastError()); return;
        }
        siNext(); break;
    case 198:
        if (!st.simManager->hydraulic().addHose(st, "Native return",
                {{I.pump, "P"}, {I.tank, "T"}}, 1000.0, 12.7, 40.0)) {
            siFail("hydraulic_return_fixture", st.simManager->hydraulic().lastError()); return;
        }
        siNext(); break;
    case 199: {
        auto& hp = st.simManager->hydraulic();
        auto definition = hp.simulationDefinition();
        definition["duration_s"] = 0.02;
        definition["output_dt_s"] = 0.01;
        definition["max_step_s"] = 0.01;
        definition["fluid"] = {{"density_kgm3", 870.0}, {"bulk_modulus_bar", 15000.0},
                                {"viscosity_cst", 32.0}, {"air_fraction", 0.002},
                                {"native_note", "retained-fluid-metadata"}};
        std::string error;
        // Author the runnable fixture canonically; bad edits and Runs use real GUI controls.
        if (!hp.configureSimulation(st, definition, error)) { siFail("hydraulic_sim_fixture", error); return; }
        hp.openSimulationSetup();
        siGo(201); break;
    }
    case 201: {
        auto& hp = st.simManager->hydraulic();
        if (!siExpand("Advanced simulation parameters", "Expand actual hydraulic simulation JSON")) return;
        if (!siNativeEditor("Apply JSON")) return;
        const char* keys[] = {"density_kgm3", "density_kgm3", "density_kgm3", "density_kgm3",
                              "bulk_modulus_bar", "viscosity_cst", "air_fraction"};
        const nlohmann::json bad[] = {"bad", true, 0.0, -1.0, "bad", false, 0.2};
        I.before = hp.simulationDefinition();
        I.edited = I.before;
        I.edited["fluid"][keys[I.fidelityCase]] = bad[I.fidelityCase];
        siNext(); break;
    }
    case 202:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 203:
        if (!siSeek("Apply JSON", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Reject malformed modeled hydraulic fluid through real JSON Apply");
        siNext(); break;
    case 204: {
        auto& hp = st.simManager->hydraulic();
        const char* keys[] = {"density_kgm3", "density_kgm3", "density_kgm3", "density_kgm3",
                              "bulk_modulus_bar", "viscosity_cst", "air_fraction"};
        const std::string error = std::string("fluid.") + keys[I.fidelityCase] +
            (I.fidelityCase == 6 ? " must be a finite number in [0, 0.2) or null." :
                                  " must be a finite positive number or null.");
        if (!siSeek(error.c_str(), p)) return;
        if (!check("hydraulic_modeled_fluid_invalid_apply_atomic_with_error",
                   hp.lastError() == error && hp.simulationDefinition() == I.before,
                   {{"error", hp.lastError()}, {"native_setup", hp.simulationDefinition()},
                    {"rendered_items", ui::audit::observedItems()}})) return;
        if (siFocusDraft("##t", I.editorWindow.c_str())) siNext();
        break;
    }
    case 205: {
        nlohmann::json draft;
        if (!siReadDraft(draft, "##t", I.editorWindow.c_str())) return;
        if (!check("hydraulic_invalid_fluid_draft_preserved", draft == I.edited, draft)) return;
        if (++I.fidelityCase < 7) siGo(201);
        else {
            st.simManager->handleCommand(st, "sim_hyd_hose");
            siGo(210);
        }
        break;
    }
    case 210:
    case 217: {
        if (!siExpand("Hoses", I.step == 210 ? "Open the circuit's hose list" :
                                               "Open hoses to restore physical geometry")) return;
        const auto* list = shotItem("Hoses", false);
        if (!list) return;
        const auto& windowName = (*list)["window"].get_ref<const std::string&>();
        const auto* window = ImGui::FindWindowByName(windowName.c_str());
        if (!window) return;
        // drawCircuitForm scopes each kit row by the actual hose ID, then "hose".
        // Its drawn title is not the observed native label of the invisible hit item.
        const ImGuiID hoseScope = ImHashStr(I.hose.c_str(), 0, window->ID);
        const ImGuiID rowScope = ImHashStr("hose", 0, hoseScope);
        const ImGuiID hitId = ImHashStr("##kit_rowhit", 0, rowScope);
        const nlohmann::json* target = nullptr;
        for (const auto& item : ui::audit::observedItems())
            if (item.value("window", std::string()) == windowName &&
                item.value("native_label", std::string()) == "##kit_rowhit" &&
                item.value("id", ImGuiID(0)) == hitId && item.value("enabled", true)) {
                target = &item; break;
            }
        if (!target || !target->value("visible", false)) {
            shotScrollTo("##kit_rowhit", windowName.c_str(), true);
            return;
        }
        I.report["actions"].push_back({{"action", "Locate exact drag-created hose list row"},
                                       {"hose", I.report["hydraulic_drag_created_hose"]},
                                       {"item", *target}});
        p = shotPoint(*target);
        siGesture(p, p, false, I.step == 210 ?
            "Select the gesture-created hose for its native geometry editor" :
            "Select the same hose to restore positive handoff geometry");
        siNext(); break;
    }
    case 211: {
        if (!siExpand("Advanced hose parameters", "Expand original gesture-created hose JSON")) return;
        if (!siNativeEditor("Apply JSON")) return;
        for (const auto& hose : st.simManager->hydraulic().circuit()["hoses"])
            if (hose.value("id", std::string()) == I.hose) I.edited = hose;
        I.edited["length_mm"] = 0.0;
        siNext(); break;
    }
    case 212:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 213:
        if (!siSeek("Apply JSON", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Apply exact zero native hose length");
        siNext(); break;
    case 214: {
        auto& hp = st.simManager->hydraulic();
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        bool zero = false;
        for (const auto& hose : hp.circuit()["hoses"])
            zero |= hose.value("id", std::string()) == I.hose && hose.value("length_mm", -1.0) == 0.0;
        if (!check("hydraulic_zero_hose_length_applied_and_persisted", zero &&
                   stored.at("params").at("circuit") == hp.circuit(), stored)) return;
        hp.openSimulationSetup();
        siNext(); break;
    }
    case 215:
        if (!siFooterAction("Run simulation", p)) return;
        siGesture(p, p, false, "Run real native hydraulic simulation with zero-volume suction hose");
        siNext(); break;
    case 216: {
        auto& hp = st.simManager->hydraulic();
        const auto& result = hp.simulateResult();
        if (!result.is_object() || result.empty()) {
            if (!hp.lastError().empty()) siFail("hydraulic_zero_length_simulation", hp.lastError());
            return;
        }
        const auto& samples = result.at("time_s");
        const auto& series = result.at("components").at(I.pump).at("series");
        const auto& flow = series.at("flow_lpm");
        const auto& pressure = series.at("pressure_bar");
        bool actualFlow = samples.size() >= 3 && flow.size() == samples.size() && pressure.size() == samples.size();
        // Both pump ports return to the reservoir: zero differential pressure means zero leakage.
        const double expectedFlow = 16.0 * 1450.0 / 1000.0;
        for (size_t sample = 0; actualFlow && sample < flow.size(); ++sample)
            actualFlow &= flow[sample].is_number() && pressure[sample].is_number() &&
                          std::isfinite(flow[sample].get<double>()) &&
                          std::abs(flow[sample].get<double>() - expectedFlow) < 1e-3 &&
                          std::abs(pressure[sample].get<double>()) < 1e-6;
        if (!check("hydraulic_zero_length_native_solver_flow_invariant", actualFlow, result)) return;
        I.capture = I.dir + "/hydraulic_zero_length_native_result.png";
        I.afterCapture = [](AppState& state) { state.simManager->handleCommand(state, "sim_hyd_hose"); };
        siNext(); break;
    }
    case 218:
        if (!siExpand("Advanced hose parameters", "Reopen exact hose JSON")) return;
        if (!siNativeEditor("Apply JSON")) return;
        I.edited["length_mm"] = 1000.0;
        siNext(); break;
    case 219:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 220:
        if (!siSeek("Apply JSON", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Apply positive-length handoff geometry without changing endpoints");
        siNext(); break;
    case 221:
        st.simManager->handleCommand(st, "sim_hyd_piping");
        I.edited = st.simManager->hydraulic().handoffDefinition();
        I.edited["supply_pressure_bar"] = 160.0;
        I.edited["flow_lpm"] = 20.0;
        I.edited["load_pressure_bar"] = 0.0;
        I.edited["tolerance_pct"] = 0.0;
        I.edited["native_note"] = {{"authored", true}};
        siNext(); break;
    case 222:
        if (!siExpand("Advanced piping handoff parameters", "Expand exact native handoff parameters")) return;
        if (!siNativeEditor("Apply JSON")) return;
        siNext(); break;
    case 223:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 224:
        if (!siSeek("Apply JSON", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Apply explicit zero load-pressure presence and zero verification tolerance");
        siNext(); break;
    case 225: {
        auto& hp = st.simManager->hydraulic();
        nlohmann::json stored;
        if (!siStudy(st, I.doc, hp.studyId(), stored)) return;
        if (!check("hydraulic_handoff_explicit_zeros_native_persistence", hp.handoffDefinition() == I.edited &&
                   stored.value("params", nlohmann::json::object()).value("piping_handoff", nlohmann::json()) == I.edited,
                   stored)) return;
        if (!siSeek("Generate network", p)) return;
        siGesture(p, p, false, "Run native hydraulic-to-piping handoff with explicit zero load and tolerance");
        siNext(); break;
    }
    case 226: {
        auto& hp = st.simManager->hydraulic();
        const auto result = hp.handoffResult();
        if (!result.is_object() || result.empty()) {
            if (!hp.lastError().empty()) siFail("hydraulic_zero_handoff", hp.lastError());
            return;
        }
        const auto margins = result.value("margin", nlohmann::json::object()).value("per_line", nlohmann::json::object());
        bool exactZero = !margins.empty();
        for (const auto& margin : margins) {
            exactZero &= margin.value("load_pressure_bar", -1.0) == 0.0 &&
                margin.contains("pump_margin_estimate_bar") && margin["pump_margin_estimate_bar"].is_number() &&
                margin.contains("pump_margin_solved_bar") && margin["pump_margin_solved_bar"].is_number();
            if (margin.contains("line_loss_solved_bar") && margin["line_loss_solved_bar"].is_number() &&
                margin.contains("pump_margin_solved_bar") && margin["pump_margin_solved_bar"].is_number())
                exactZero &= std::abs(margin["pump_margin_solved_bar"].get<double>() +
                                      margin["line_loss_solved_bar"].get<double>() - 160.0) < 1e-3;
        }
        const auto checks = result.value("verify", nlohmann::json::object()).value("checks", nlohmann::json::array());
        bool zeroTolerance = !checks.empty();
        for (const auto& row : checks) zeroTolerance &= row.value("tolerance_pct", -1.0) == 0.0;
        if (!check("hydraulic_handoff_zero_load_margin_and_exact_tolerance_executed",
                   result.value("stage", std::string()) == "solved" && exactZero && zeroTolerance, result)) return;
        I.capture = I.dir + "/hydraulic_zero_handoff_native_result.png";
        siNext(); break;
    }
    case 227:
        if (!siSeek("Open in Piping", p)) return;
        siGesture(p, p, false, "Open actual generated solved piping network");
        siNext(); break;
    case 228:
        if (st.simManager->piping().networkStudyId().empty()) return;
        I.pipeBase = st.simManager->piping().setupDefinition(ui::PipingPanel::Form::Solve);
        I.fidelityCase = 0;
        st.ribbonTab = ui::kRibbonTabPiping;
        siGo(250); break;
    case 250:
        st.simManager->handleCommand(st, "sim_piping_solve");
        if (!siPipingEditor()) return;
        I.before = st.simManager->piping().setupDefinition(ui::PipingPanel::Form::Solve);
        I.edited = I.pipeBase;
        I.edited["network_type"] = I.fidelityCase == 2 ? "gas" : "liquid";
        I.edited["fluid"] = I.fidelityCase == 0 ? nlohmann::json{{"name", "water"}, {"T_C", 80.0}} :
            I.fidelityCase == 1 ? nlohmann::json{{"name", "water"}, {"T", 293.15}, {"T_C", 80.0}} :
                                 nlohmann::json{{"name", "ch4"}, {"T", 293.15}, {"T_C", 80.0}};
        if (I.fidelityCase == 2) {
            I.edited["boundary"] = nlohmann::json::array();
            for (const auto& run : I.edited["piping"]["runs"]) {
                const auto id = run.value("id", std::string());
                I.edited["boundary"].push_back({{"run", id}, {"end", "start"}, {"pressure", 400.0}});
                I.edited["boundary"].push_back({{"run", id}, {"end", "end"}, {"mass_demand", 1.0}});
            }
        }
        siNext(); break;
    case 251:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 252:
        if (!siSeek("Apply parameters", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Apply exact native piping fluid aliases and Celsius/Kelvin precedence");
        siNext(); break;
    case 253: {
        auto& pp = st.simManager->piping();
        nlohmann::json stored;
        if (!siStudy(st, I.doc, pp.networkStudyId(), stored)) return;
        if (!check("piping_native_fluid_definition_persisted_exactly",
                   pp.setupDefinition(ui::PipingPanel::Form::Solve)["fluid"] == I.edited["fluid"] &&
                   stored.value("params", nlohmann::json::object()).value("fluid", nlohmann::json()) == I.edited["fluid"],
                   stored)) return;
        if (!siSeek("Solve network", p)) return;
        siGesture(p, p, false, "Run actual piping network solver with exact fluid temperature semantics");
        siNext(); break;
    }
    case 254: {
        auto& pp = st.simManager->piping();
        const auto result = pp.solveResult();
        if (!result.is_object() || result.empty()) {
            if (!pp.lastError().empty()) siFail("piping_fluid_solver_refused", pp.lastError());
            return;
        }
        const auto links = result.value("links", nlohmann::json::array());
        const auto fluid = result.value("fluid", nlohmann::json::object());
        bool native = result.value("converged", false) && !links.empty();
        if (I.fidelityCase < 2) {
            const double rho = I.fidelityCase == 0 ? 971.79 : 998.21;
            const double mu = I.fidelityCase == 0 ? 0.0003540 : 0.0010016;
            native &= std::abs(fluid.value("rho", 0.0) - rho) < 1e-5 &&
                      std::abs(fluid.value("mu", 0.0) - mu) < 1e-9;
            for (const auto& link : links) {
                const double velocity = std::abs(link.value("velocity_ms", 0.0));
                const double diameter = link.value("id_mm", 0.0) * 0.001;
                const double re = link.value("reynolds", 0.0);
                const double expected = rho * velocity * diameter / mu;
                native &= std::isfinite(re) && expected > 0.0 && std::abs(re - expected) < expected * 0.01;
            }
            if (!check(I.fidelityCase == 0 ? "piping_water_celsius_native_properties_and_reynolds" :
                                            "piping_kelvin_over_celsius_native_properties_and_reynolds", native, result)) return;
            if (I.fidelityCase == 0) I.fidelityBase = result;
            else {
                bool changed = true;
                for (const auto& link : links) {
                    bool matched = false;
                    for (const auto& hot : I.fidelityBase["links"])
                        if (hot["id"] == link["id"]) {
                            matched = true;
                            const double ratio = hot.value("reynolds", 0.0) / link.value("reynolds", 1.0);
                            const double expected = (971.79 / 0.0003540) / (998.21 / 0.0010016);
                            changed &= std::abs(ratio - expected) < expected * 0.01;
                        }
                    changed &= matched;
                }
                if (!check("piping_temperature_alias_changes_actual_hydraulics",
                           changed, {{"hot_native_result", I.fidelityBase}, {"kelvin_native_result", result}})) return;
            }
        } else {
            native &= fluid.value("name", std::string()) == "methane" &&
                      std::abs(fluid.value("T_K", 0.0) - 293.15) < 1e-6 &&
                      fluid.value("rho_std_kgm3", 0.0) > 0.0 && fluid.value("mu", 0.0) > 0.0;
            for (const auto& link : links)
                native &= std::abs(std::abs(link.value("mass_flow_kgh", 0.0)) - 1.0) < 1e-4 &&
                          link.value("reynolds", 0.0) > 0.0;
            if (!check("piping_ch4_alias_native_gas_temperature_and_massflow", native, result)) return;
        }
        I.report["piping_fidelity_runs"].push_back({{"fluid_definition", I.edited["fluid"]}, {"native_result", result}});
        I.capture = I.dir + "/piping_fluid_" + std::to_string(I.fidelityCase) + "_native_result.png";
        if (++I.fidelityCase < 3) I.afterCapture = [](AppState&) { siGo(250); };
        else I.afterCapture = [](AppState& state) {
            state.simManager->handleCommand(state, "sim_piping_solve");
            siGo(261);
        };
        break;
    }
    case 261:
        if (!siSeek("Conditions", p)) return;
        siGesture(p, p, false, "Expose actual piping boundary assignments and run-end sources");
        siNext(); break;
    case 262: {
        std::string source;
        for (const auto& item : ui::audit::observedItems()) {
            const auto label = item.value("label", std::string());
            if (label.find(" end###run_port") != std::string::npos) { source = label; break; }
        }
        if (source.empty()) { shotScrollTo("Drag a run end"); return; }
        I.before = st.simManager->piping().setupDefinition(ui::PipingPanel::Form::Solve);
        if (!siDragItems(source.c_str(), "Drop run end here", "Drag actual native pipe port across an agent setup refresh")) return;
        siNext(); break;
    }
    case 263:
        // Held-drag processing observes the immutable payload and performs the native same-ID
        // external edit/refresh before delivery. It never constructs a test payload.
        siNext(); break;
    case 264: {
        auto& pp = st.simManager->piping();
        const auto current = pp.setupDefinition(ui::PipingPanel::Form::Solve);
        if (!check("piping_stale_generation_real_drag_delivery_refused",
                   !I.dragOrigin.empty() && current["boundary"] == I.before["boundary"] &&
                   current["design"] != I.before["design"] &&
                   !pp.acceptsSetupDrag(st, ui::PipingPanel::Form::Solve, I.dragOrigin) &&
                   pp.acceptsSetupDrag(st, ui::PipingPanel::Form::Solve,
                                       pp.setupDragOrigin(st, ui::PipingPanel::Form::Solve)),
                   {{"actual_drag_origin", I.dragOrigin}, {"before_native_setup", I.before},
                    {"after_native_refresh_and_delivery", current}})) return;
        st.simManager->handleCommand(st, "sim_piping_flow");
        I.fidelityCase = 0;
        siGo(270); break;
    }
    case 270: {
        if (!siPipingEditor()) return;
        auto& pp = st.simManager->piping();
        I.before = pp.setupDefinition(ui::PipingPanel::Form::Flow);
        I.edited = I.before;
        const auto& run = pp.network()["runs"][0];
        I.edited["run_id"] = run["id"];
        I.edited["inlet"] = {{"point", run["path"][0]}, {"normal", {-1.0, 0.0, 0.0}}};
        I.edited["outlet"] = {{"point", run["path"].back()}, {"normal", {1.0, 0.0, 0.0}}};
        I.edited["fluid"] = "water";
        I.edited["mean_velocity_ms"] = I.fidelityCase == 0 ? nlohmann::json() : nlohmann::json(1.0);
        I.edited["flow_m3s"] = nullptr;
        I.edited.erase("density_kgm3");
        I.edited.erase("viscosity_pas");
        if (I.fidelityCase == 1) {
            I.edited["density_kgm3"] = nullptr; I.edited["viscosity_pas"] = nullptr;
        } else if (I.fidelityCase == 2) I.edited["density_kgm3"] = 997.0;
        else if (I.fidelityCase == 3) {
            I.edited["density_kgm3"] = 997.0; I.edited["viscosity_pas"] = 0.0008;
        } else if (I.fidelityCase == 4) I.edited["density_kgm3"] = nullptr;
        else if (I.fidelityCase == 5) {
            I.edited["density_kgm3"] = nullptr; I.edited["viscosity_pas"] = 0.0008;
        }
        siNext(); break;
    }
    case 271:
        if (siWriteText(I.edited.dump(2), "##t", I.editorWindow.c_str())) siNext();
        break;
    case 272:
        if (!siSeek("Apply parameters", p, I.editorWindow.c_str())) return;
        siGesture(p, p, false, "Apply real native CFD draft to exercise null rate and paired custom fluid semantics");
        siNext(); break;
    case 273: {
        auto& pp = st.simManager->piping();
        const bool accepted = I.fidelityCase == 3 || I.fidelityCase == 4;
        const char* error = I.fidelityCase == 0 ? "CFD needs exactly one non-null finite positive velocity or flow." :
                                                "Custom CFD density and viscosity must both be finite positive numbers.";
        if (!accepted && !siSeek(error, p)) return;
        const auto current = pp.setupDefinition(ui::PipingPanel::Form::Flow);
        if (!check(accepted ? "piping_cfd_paired_custom_or_lone_null_applied" :
                             "piping_cfd_null_rate_or_unpaired_custom_refused_atomically",
                   accepted ? current == I.edited : current == I.before,
                   {{"case", I.fidelityCase}, {"draft", I.edited}, {"native_setup", current},
                    {"rendered_items", ui::audit::observedItems()}})) return;
        if (accepted) {
            nlohmann::json listed, persisted;
            std::string error;
            if (!st.worker.call("sim_study_list", {{"doc_id", I.doc}}, listed, error)) {
                siFail("piping_cfd_native_store", error); return;
            }
            for (const auto& record : listed.value("studies", nlohmann::json::array())) {
                const auto parameters = record.value("params", nlohmann::json::object());
                if (record.value("type", std::string()) == "pipe_flow" &&
                    parameters.value("network_id", std::string()) == pp.networkStudyId() &&
                    parameters.value("run_id", std::string()) == I.edited.value("run_id", std::string()))
                    persisted = parameters;
            }
            bool exact = !persisted.empty() && persisted.value("fluid", std::string()) == "water" &&
                persisted.value("mean_velocity_ms", 0.0) == 1.0 &&
                persisted.contains("flow_m3s") && persisted["flow_m3s"].is_null();
            for (const char* key : {"density_kgm3", "viscosity_pas"}) {
                exact &= persisted.contains(key) == I.edited.contains(key);
                if (I.edited.contains(key) && persisted.contains(key)) exact &= persisted[key] == I.edited[key];
            }
            if (!check("piping_cfd_nullable_rate_and_custom_pair_native_persistence", exact, persisted)) return;
        }
        if (siFocusDraft("##t", I.editorWindow.c_str())) siNext();
        break;
    }
    case 274: {
        nlohmann::json draft;
        if (!siReadDraft(draft, "##t", I.editorWindow.c_str())) return;
        if (!check("piping_cfd_native_editor_retains_exact_draft", draft == I.edited, draft)) return;
        if (++I.fidelityCase < 6) siGo(270);
        else siGo(20);
        break;
    }
    case 20:
        st.simManager->handleCommand(st, "sim_piping_route");
        st.ribbonTab = ui::kRibbonTabPiping;
        siNext(); break;
    case 21:
        if (!siSeek("Start from face", q) || !siFacePoint(st)) return;
        siGesture(I.facePoint, q, true, "Drag real CAD face to piping route start");
        siNext(); break;
    case 22: {
        auto& pipe = st.simManager->piping();
        const auto endpoints = pipe.routeEndpoints();
        if (!check("piping_face_endpoint", endpoints["start"].value("face", nlohmann::json()).value("_pick", nlohmann::json()) ==
                   nlohmann::json({{"body", I.face.body_id}, {"topo", I.face.topo_id}}), endpoints)) return;
        nlohmann::json stored;
        if (!siStudy(st, I.doc, pipe.networkStudyId(), stored)) return;
        if (!check("piping_endpoint_native_store", stored["params"].value("route_inputs", nlohmann::json()) ==
                   endpoints, stored)) return;
        I.capture = I.dir + "/04_piping_face.png";
        I.afterCapture = [](AppState& state) { state.simManager->handleCommand(state, "sim_piping_vessel"); };
        siNext(); break;
    }
    case 23:
        if (!siDragItems("Vertical receiver", "Apply preset here", "Copy vertical receiver preset to vessel setup")) return;
        siNext(); break;
    case 24: {
        const auto parameters = st.simManager->piping().setupDefinition(ui::PipingPanel::Form::Vessel);
        if (!check("vessel_preset_native_values", parameters.value("orientation", std::string()) == "vertical" &&
                   parameters.value("inner_diameter_mm", 0.0) == 1000.0 &&
                   parameters.value("tan_tan_length_mm", 0.0) == 2000.0 &&
                   parameters.value("fill_fraction", -1.0) == 0.0, parameters)) return;
        nlohmann::json studies, persisted;
        std::string error;
        if (!st.worker.call("sim_study_list", {{"doc_id", I.doc}}, studies, error, 15000) ||
            !studies.is_object() || studies.contains("code")) { siFail("vessel_native_store", error.empty() ? studies.dump() : error); return; }
        for (const auto& study : studies.value("studies", nlohmann::json::array()))
            if (study.value("type", std::string()) == "vessel") persisted = study.value("params", nlohmann::json::object());
        if (!check("vessel_preset_persisted", persisted == parameters, persisted)) return;
        I.capture = I.dir + "/05_vessel_preset.png";
        I.afterCapture = [&I](AppState& state) {
            state.simManager->handleCommand(state, "sim_firmware_setup");
            I.before = state.simManager->activeFirmwareScenario();
        };
        siNext(); break;
    }
    case 25:
        if (!siDragItems("GPIO edges", "Drop stimulus here to add track", "Drop GPIO stimulus into native firmware scenario")) return;
        siNext(); break;
    case 26: {
        const auto scenario = st.simManager->activeFirmwareScenario();
        const size_t before = I.before.is_object() ? I.before.value("tracks", nlohmann::json::array()).size() : 0;
        if (!check("firmware_track_drop", scenario.is_object() &&
                   scenario.value("tracks", nlohmann::json::array()).size() == before + 1 &&
                   scenario["tracks"].back().value("kind", std::string()) == "gpio_edges", scenario)) return;
        I.capture = I.dir + "/06_firmware_stimulus.png";
        I.report["not_run"].push_back({{"name", "firmware_emulator_execution"},
            {"reason", "This isolated scenario authors setup tracks, not firmware source or an ELF; no emulator execution is claimed."}});
        I.afterCapture = [&I](AppState& st) {
        nlohmann::json schematic;
        std::string err;
        if (!st.worker.call("ecad_design", {{"request", "RC low-pass filter with 1000 Hz cutoff"},
                    {"name", "Interactive RC circuit"}}, schematic, err) ||
            schematic.contains("code") || !schematic.value("ok", false)) {
            siFail("circuit_recipe", err.empty() ? schematic.dump() : err); return;
        }
        st.addEcadTab("sch", schematic.value("doc_id", std::string()), schematic.value("revision", 0),
                      "Interactive RC circuit");
        st.switchToTab(I.partTab);
        };
        siNext(); break;
    }
    case 27:
        st.simManager->handleCommand(st, "sim_circuit_ac");
        siNext(); break;
    case 28:
        if (!siDragItems("V(OUT)", "Copy-drop a native probe here", "Copy native RC output-voltage probe into circuit setup")) return;
        siNext(); break;
    case 29: {
        const auto parameters = st.simManager->electronicsSetup();
        const auto probes = parameters["study"].value("probes", nlohmann::json::array());
        if (!check("circuit_probe_copy", std::count(probes.begin(), probes.end(), nlohmann::json("V(OUT)")) == 1,
                   parameters)) return;
        I.before = parameters;
        if (!siDragItems("V(OUT)", "Copy-drop a native probe here", "Repeat circuit probe copy without duplicate")) return;
        siNext(); break;
    }
    case 30:
        if (!check("circuit_probe_duplicate_safe", st.simManager->electronicsSetup() == I.before,
                   st.simManager->electronicsSetup())) return;
        I.capture = I.dir + "/07_native_circuit_probe.png";
        I.afterCapture = [&I](AppState& st) {
        st.simManager->handleCommand(st, "sim_new_thermal_fea");
        I.study = st.simManager->activeStudyId();
        I.edited = st.simManager->activeStudyDefinition();
        I.edited["material"] = {{"k_w_mk", 160.0}, {"cp_j_kgk", 900.0}, {"density_g_cm3", 2.7}};
        I.edited["transient"] = {{"t_end_s", 0.2}, {"dt_s", 0.01}, {"t_init_c", 20.0},
                                 {"frame_times_s", {0.0, 0.1, 0.2}}, {"capacitance", "lumped"}};
        I.edited["fixtures"] = nlohmann::json::array();
        I.edited["loads"] = nlohmann::json::array({{{"faces", nlohmann::json::array({I.faceSelector})},
            {"type", "flux"}, {"magnitude", 10000.0}, {"schedule", {{"duty", 0.25}, {"period_s", 0.1}}}}});
        I.edited["fineness"] = 0.2;
        I.edited["mesh_order"] = 1;
        st.simManager->handleCommand(st, "sim_parameters");
        };
        siNext(); break;
    case 31:
        if (siWriteText(I.edited.dump(2))) siNext();
        break;
    case 32:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Apply pulsed thermal schedule and exact native frame times");
        siNext(); break;
    case 33: {
        const auto definition = st.simManager->activeStudyDefinition();
        if (!check("thermal_native_editor_applied", definition.value("transient", nlohmann::json()) ==
                   I.edited["transient"] && definition["loads"][0].value("schedule", nlohmann::json()) ==
                   I.edited["loads"][0]["schedule"], definition)) return;
        if (!siSeek("Run Thermal FEA", p)) return;
        siGesture(p, p, false, "Run real transient conduction using pulsed native schedule");
        siNext(); break;
    }
    case 34:
    case 35:
        siNext(); break;  // native asynchronous export/preparation may still be launching
    case 36: {
        if (st.simManager->runUi().running) return;
        const auto result = st.simManager->activeStudyResult();
        if (!result.is_object() || !result.value("transient", false)) return;
        bool middle = false, end = false, scheduled = false;
        for (const auto& frame : result.value("frames", nlohmann::json::array())) {
            const double t = frame.value("t_s", -1.0);
            middle |= std::abs(t - 0.1) < 1e-6;
            end |= std::abs(t - 0.2) < 1e-6;
        }
        for (const auto& flux : result.value("flux_summary", nlohmann::json::array()))
            scheduled |= flux.value("scheduled", false) && flux.value("q_w_m2", 0.0) == 10000.0;
        if (!check("thermal_native_schedule_and_frames_executed", middle && end && scheduled &&
                   result.value("n_steps", 0) >= 20, result)) return;
        I.pulsedFinalC = result["summary"].value("final_max_temp_c", 20.0);
        I.edited = st.simManager->activeStudyDefinition();
        I.edited["loads"][0]["schedule"]["duty"] = 1.0;
        I.afterCapture = [](AppState& state) { state.simManager->handleCommand(state, "sim_parameters"); };
        I.capture = I.dir + "/08_pulsed_thermal_result.png";
        siNext(); break;
    }
    case 37:
        if (siWriteText(I.edited.dump(2))) siNext();
        break;
    case 38:
        if (!siSeek("Apply parameters", p)) return;
        siGesture(p, p, false, "Change native thermal duty to continuous heating");
        siNext(); break;
    case 39:
        if (!check("thermal_edit_invalidates_result", st.simManager->activeStudyResult().empty(),
                   st.simManager->activeStudyResult())) return;
        if (!siSeek("Run Thermal FEA", p)) return;
        siGesture(p, p, false, "Run continuous heating with otherwise identical native inputs");
        siNext(); break;
    case 40: {
        if (st.simManager->runUi().running) return;
        const auto result = st.simManager->activeStudyResult();
        if (!result.is_object() || !result.value("transient", false)) return;
        const double continuous = result["summary"].value("final_max_temp_c", 20.0);
        if (!check("thermal_duty_changes_solver_output", continuous > I.pulsedFinalC + 1e-4,
                   {{"pulsed_final_max_temp_c", I.pulsedFinalC},
                    {"continuous_final_max_temp_c", continuous}, {"native_result", result}})) return;
        I.capture = I.dir + "/09_continuous_thermal_result.png";
        I.afterCapture = [&I](AppState& st) {
        st.simManager->handleCommand(st, "sim_new_flow");
        st.simManager->configureActiveFlow(st, 0, 7.0f, 3.0f, 0);
        st.simManager->handleCommand(st, "sim_flow_bc");
        I.before = st.simManager->activeStudyDefinition();
        };
        siNext(); break;
    }
    case 41:
        if (!siDragItems("-Y", "Drop direction preset", "Drop incoming-flow direction while retaining exact angles")) return;
        siNext(); break;
    case 42: {
        const auto definition = st.simManager->activeStudyDefinition();
        if (!check("flow_direction_native_store", definition["flow"].value("dir", -1) == 3 &&
                   definition["flow"]["aoa"] == I.before["flow"]["aoa"] &&
                   definition["flow"]["sideslip"] == I.before["flow"]["sideslip"], definition)) return;
        I.capture = I.dir + "/10_flow_direction.png";
        I.afterCapture = [](AppState& st) {
        st.simManager->handleCommand(st, "sim_new_topology");
        st.simManager->handleCommand(st, "sim_fixture");
        };
        siNext(); break;
    }
    case 43:
        if (st.simManager->wantsViewportFacePick(st)) {
            if (!siSeek("Picking\xe2\x80\xa6", p)) return;
            siGesture(p, p, false, "Disarm topology click-picking before drag");
            return;
        }
        if (!siSeek("Drop a face here", q) || !siFacePoint(st)) return;
        siGesture(I.facePoint, q, true, "Drag real domain face to topology native fixture");
        siNext(); break;
    case 44: {
        const auto definition = st.simManager->activeTopologyStudy();
        const auto fixtures = definition.value("fixtures", nlohmann::json::array());
        bool picked = false;
        for (const auto& fixture : fixtures)
            for (const auto& face : fixture.value("faces", nlohmann::json::array()))
                picked |= face.value("_pick", nlohmann::json()) ==
                          nlohmann::json({{"body", I.face.body_id}, {"topo", I.face.topo_id}});
        if (!check("topology_face_native_fixture", picked, definition)) return;
        I.capture = I.dir + "/11_topology_face.png";
        I.afterCapture = [](AppState& st) {
        st.simManager->handleCommand(st, "sim_em_sweep");
        st.simManager->handleCommand(st, "sim_em_boundary");
        };
        siNext(); break;
    }
    case 45:
        if (!siExpand("Standard waveguide palette", "Expand actual waveguide setup palette")) return;
        siNext(); break;
    case 46:
        if (!siDragItems("WR-75", "Apply waveguide preset", "Copy WR-75 waveguide preset into EM setup")) return;
        siNext(); break;
    case 47: {
        const auto parameters = st.simManager->activeEmParameters();
        if (!check("em_waveguide_native_parameters", parameters.value("standard", std::string()) == "WR-75",
                   parameters)) return;
        I.capture = I.dir + "/12_em_waveguide.png";
        I.afterCapture = [&I](AppState& st) {
        SimAuditShot assembly;
        assembly.dir = I.dir;
        const std::string why = saBuildMotionAssembly(st, assembly);
        if (!why.empty()) { siFail("motion_assembly", why); return; }
        I.doc = assembly.asmDoc;
        std::string ground;
        for (const auto& component : st.docMirror.tree().value("components", nlohmann::json::array()))
            if (component.value("name", std::string()) == "Ground") ground = component.value("id", std::string());
        const auto mates = st.docMirror.tree().value("mates", nlohmann::json::array());
        if (ground.empty() || mates.empty()) { siFail("motion_mates", st.docMirror.tree().dump()); return; }
        I.mateA = mates[0].value("id", std::string());
        const auto offset = nlohmann::json::array({1, 0, 0, 0, 0, 1, 0, 40, 0, 0, 1, 70, 0, 0, 0, 1});
        auto inserted = st.docMirror.applyOps(st.worker, nlohmann::json::array({{
            {"op", "asm_insert_component"}, {"source", {{"path", I.dir + "/simaudit_motion_arm.step+"}}},
            {"name", "Second arm"}, {"fixed", false}, {"transform", offset}}}));
        const auto arm = inserted.id_map.value("$0", std::string());
        if (!inserted.ok || arm.empty()) { siFail("motion_second_arm", inserted.error_message); return; }
        const auto refs = nlohmann::json::array({
            {{"component", ground}, {"selector", {{"kind", "face"}, {"centroid", {0.0, 0.0, 5.0}}}}},
            {{"component", arm}, {"selector", {{"kind", "face"}, {"centroid", {0.0, 0.0, 25.0}}}}}});
        auto added = st.docMirror.applyOps(st.worker, nlohmann::json::array({{
            {"op", "asm_add_mate"}, {"mate", {{"type", "distance"}, {"value", {{"value", 70.0}, {"expr", nullptr}}},
                {"align", "aligned"}, {"suppressed", false}, {"refs", refs}}}}}));
        I.mateB = added.id_map.value("$0", std::string());
        if (!added.ok || I.mateB.empty()) { siFail("motion_second_mate", added.error_message); return; }
        if (!added.aicm_path.empty()) { std::string err; st.loadDocMesh(added.aicm_path, err); }
        };
        siNext(); break;
    }
    case 48: {
        st.simManager->handleCommand(st, "sim_new_motion");
        I.study = st.simManager->activeStudyId();
        auto definition = st.simManager->activeStudyDefinition();
        definition["motion"]["drivers"] = nlohmann::json::array({
            {{"mate_id", I.mateA}, {"start", 60.0}, {"stop", 100.0}},
            {{"mate_id", I.mateB}, {"start", 70.0}, {"stop", 85.0}}});
        std::string error;
        if (!st.simManager->configureActiveStudy(st, definition, error)) { siFail("motion_setup", error); return; }
        I.before = st.simManager->activeStudyDefinition();
        st.simManager->handleCommand(st, "sim_motion_setup");
        siNext(); break;
    }
    case 49:
        if (!siDragItems("Move Driver 2", "Move Driver 1", "Reorder second real assembly driver before first")) return;
        siNext(); break;
    case 50: {
        nlohmann::json persisted;
        if (!siStudy(st, I.doc, I.study, persisted)) return;
        const auto drivers = persisted["motion"].value("drivers", nlohmann::json::array());
        const auto before = I.before["motion"]["drivers"];
        if (!check("motion_reorder_preserves_native_ranges", drivers.size() == 2 &&
                   drivers[0] == before[1] && drivers[1] == before[0], persisted)) return;
        I.capture = I.dir + "/13_motion_driver_reorder.png";
        I.report["ok"] = true;
        I.report["seconds"] = pipingNow() - I.started;
        I.done = true;
        std::printf("siminteractive: PASS checks=%zu report=%s/report.json\n", I.report["checks"].size(), I.dir.c_str());
        std::fflush(stdout);
        break;
    }
    }
}

bool simInteractiveEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& done) {
    auto& I = g_si;
    if (!I.want) return false;
    ++st.frame;
    if (!I.capture.empty()) {
        writeBackbufferPng(I.capture.c_str(), dw, dh, I.done ? screenshotPath : nullptr);
        I.report["captures"].push_back(I.capture);
        I.capture.clear();
    }
    std::ofstream out(I.dir + "/report.json", std::ios::trunc);
    out << I.report.dump(2) << '\n';
    out.close();
    glfwSwapBuffers(st.win);
    done = I.done;
    if (done && I.failed) { std::fflush(nullptr); std::_Exit(EXIT_FAILURE); }
    return true;
}


// Empty-document drawing regression: all geometric edits begin as real viewport
// pointer/keyboard events; only the custom-K editor uses its canonical submitEdit API.
struct PipeDrawShot {
    bool init = false, want = false, done = false, failed = false;
    bool qualityCombo = false;
    int step = 0, settle = 0;
    double started = 0, stepStart = 0, cfdStart = 0, applyStart = 0, cfdDoneAt = 0;
    std::string dir, capture, queued, elbow;
    nlohmann::json report;
    SceneShotInput input;
    int sweepFrames = 0;
    float sweepZoom = 0, sweepRadius = 0;
    bool sweepStable = true;
    std::vector<std::string> planeRunIds;
    std::size_t planeElbows = 0;
};
PipeDrawShot g_pd;

void pipeDrawFail(const char* code, const std::string& detail) {
    auto& P = g_pd;
    P.done = P.failed = true; P.queued.clear(); P.report["ok"] = false;
    P.report["failure"] = {{"code", code}, {"detail", detail}, {"step", P.step}};
    P.report["items_at_failure"] = ui::audit::observedItems();
    P.capture = P.dir + "/pipedraw_FAILED.png";
    ImGui::GetIO().AddMouseButtonEvent(0, false);
    ImGui::GetIO().AddKeyEvent(ImGuiMod_Shift, false);
    std::printf("pipedraw: FAILED [%s] %s\n", code, detail.c_str());
    std::fflush(stdout);
}

void pipeDrawTick(AppState& st) {
    auto& P = g_pd;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "pipedraw") == 0;
        if (P.want) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            P.dir = std::string(repo ? repo : ".") + "/scratch/pipedraw_shots";
            std::error_code ec; std::filesystem::create_directories(P.dir, ec);
            P.started = P.stepStart = pipingNow();
            P.step = -2;  // exercise large-document -> empty-document framing in one GUI session
            P.report = {{"scenario", "pipedraw"}, {"ok", false},
                {"checks", nlohmann::json::array()}, {"skipped", nlohmann::json::array()},
                {"captures", nlohmann::json::array()}, {"input", nlohmann::json::array()}};
        }
    }
    if (!P.want || P.done) return;
    ui::audit::observedItems();
    if (pipingNow() - P.started > 600 || pipingNow() - P.stepStart > (P.step == 27 ? 210 : 180)) {
        pipeDrawFail("timeout", "scenario/action exceeded its wall-clock deadline"); return;
    }
    if (!st.workerStarted || st.frame < 5 || !P.capture.empty()) return;
    if (P.settle > 0) { --P.settle; return; }
    if (!P.queued.empty()) { P.capture = P.queued; P.queued.clear(); return; }
    auto next = [&](int step) { P.step = step; P.stepStart = pipingNow(); if (P.queued.empty()) P.settle = 3; };
    auto shoot = [&](const char* name) { P.queued = P.dir + "/" + name; P.settle = 16; };
    auto check = [&](const char* name, bool ok, const nlohmann::json& evidence) {
        P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
        if (!ok) pipeDrawFail(name, evidence.dump());
        return ok;
    };
    auto click = [&](const std::array<float, 3>& world) {
        ImVec2 p;
        if (!sceneShotPoint(st, world.data(), p)) {
            pipeDrawFail("projection", "chosen world point is outside the clear viewport"); return false;
        }
        if (!P.input.click(p)) return false;
        P.report["input"].push_back({{"kind", "viewport_click"}, {"world", world}, {"screen", {p.x, p.y}}});
        return true;
    };
    auto button = [&](const char* label) {
        if (P.input.phase > 0) return P.input.click(P.input.point);
        const auto* item = shotItem(label);
        if (!item) { shotScrollTo(label); return false; }
        return P.input.click(shotPoint(*item));
    };
    auto fieldButton = [&](const char* group, const char* field) {
        if (P.input.phase > 0) return P.input.click(P.input.point);
        const nlohmann::json* target = nullptr;
        for (const auto& item : ui::audit::observedItems()) {
            if (!item.value("enabled", true) || item.value("native_label", std::string()) != "##combo") continue;
            const auto name = item.value("window", std::string());
            if (name.find("SimulationManager") == std::string::npos) continue;
            const auto* window = ImGui::FindWindowByName(name.c_str());
            if (!window) continue;
            // kit::FieldGroup -> beginField(id) -> comboCore("##combo").
            // Match that exact scoped native ID, never another card's hidden combo.
            const auto groupId = ImHashStr(group, 0, window->ID);
            const auto fieldId = ImHashStr(field, 0, groupId);
            if (item.value("id", ImGuiID(0)) == ImHashStr("##combo", 0, fieldId)) {
                target = &item; break;
            }
        }
        if (!target) {
            const std::string seek = std::string("pipedraw_field_") + group + "_" + field;
            shotScrollTo(seek.c_str());
            return false;
        }
        if (!target->value("visible", false)) {
            shotScrollTo("##combo", (*target)["window"].get_ref<const std::string&>().c_str(), true);
            return false;
        }
        return P.input.click(shotPoint(*target));
    };
    auto* sm = st.simManager;
    if (P.step > 1 && !sm) { pipeDrawFail("manager", "Simulation manager missing"); return; }
    auto ready = [&]() {
        return sm && !st.rpc.busy() && !sm->piping().busy() && !sm->piping().editPending();
    };
    auto fittings = [&](const char* type) {
        auto out = nlohmann::json::array();
        for (const auto& f : sm->piping().network().value("fittings", nlohmann::json::array()))
            if (f.value("type", std::string()) == type) out.push_back(f);
        return out;
    };
    const std::array<float, 3> a{-300,-150,0}, b{300,-150,0}, c{300,250,0},
        d{300,250,300}, tee{0,-150,0}, end{0,-450,0};
    switch (P.step) {
    case -2: {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { pipeDrawFail("large_part", err); return; }
        st.clearViewportForNewDoc();
        st.addDocTab(false);
        auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
        auto result = st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Large framing regression fixture"},
                {"params", {{"length", dim(4000)}, {"width", dim(1650)}, {"height", dim(3063)}}},
                {"centered", true}}}}
        }));
        if (!result.ok || result.aicm_path.empty() || !st.loadDocMesh(result.aicm_path, err)) {
            pipeDrawFail("large_part_mesh", result.error_message.empty() ? err : result.error_message);
            return;
        }
        next(-1);
        P.settle = 12;
        break;
    }
    case -1:
        if (!check("large_document_framed", st.renderer->effectiveSceneRadius() > 1000,
                   {{"radius_mm", st.renderer->effectiveSceneRadius()}})) return;
        next(0);
        break;
    case 0: {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { pipeDrawFail("new_part", err); return; }
        st.clearViewportForNewDoc();
        st.addDocTab(false);
        st.planesUserToggled = st.axesUserToggled = st.originUserToggled = true;
        for (auto& p : st.showPlane) p = false;
        for (const char* id : {"axis.x", "axis.y", "axis.z", "point.origin"}) st.hiddenFeatures.insert(id);
        st.yaw = 35; st.pitch = 35; st.zoom = 0.05f;
        st.pan[0] = 0; st.pan[1] = -50; st.pan[2] = 100;
        st.renderer->setOrthographic(true);
        next(1); return;
    }
    case 1:
        if (!sm || st.rpc.busy()) return;
        st.activeWorkspace = AppState::Workspace::Simulate;
        st.leftPaneTab = AppState::LP_Simulation; st.showSimulationTools = st.showRibbon = true;
        sm->handleCommand(st, "sim_piping_new");
        next(2); return;
    case 2:
        if (!ready()) return;
        if (!check("fresh_empty_document", !st.hasDocMesh &&
            sm->piping().network().value("runs", nlohmann::json::array()).empty(), {{"has_doc_mesh", st.hasDocMesh}})) return;
        if (!check("empty_document_drops_large_radius", st.renderer->effectiveSceneRadius() < 100,
                   {{"radius_mm", st.renderer->effectiveSceneRadius()}})) return;
        sm->handleCommand(st, "sim_piping_draw");
        sm->piping().drawSettings().plane = 0;
        sm->piping().drawSettings().autoPlane = false;
        sm->piping().drawSettings().gridMm = 50;
        if (!check("draw_command_armed", sm->piping().tool() == ui::PipingPanel::Tool::DrawRun, {})) return;
        shoot("pipedraw_00_empty.png"); next(3); return;
    case 3: if (click(a)) next(50); return;
    case 4: if (click(b)) next(5); return;
    case 5: if (click(c)) next(6); return;
    case 6: if (P.input.key(ImGuiKey_Enter)) next(7); return;
    case 7: {
        if (!ready()) return;
        const auto elbows = fittings("elbow");
        if (!check("l_run_and_elbow", sm->piping().network().value("runs", nlohmann::json::array()).size() == 1 &&
            elbows.size() == 1, sm->piping().network())) return;
        P.elbow = elbows[0].value("id", std::string());
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        shoot("pipedraw_01_l_run.png"); next(8); return;
    }
    case 8:
        sm->handleCommand(st, "sim_piping_draw");
        // Finish returns to Auto; this existing one-Tab riser begins explicitly on XY.
        sm->piping().drawSettings().plane = 0;
        sm->piping().drawSettings().autoPlane = false;
        if (click(c)) next(9); return;
    case 9:
        if (P.input.key(ImGuiKey_Tab)) next(10); return;
    case 10:
        if (!check("tab_switched_plane", sm->piping().drawSettings().plane == 1 &&
            !sm->piping().drawSettings().autoPlane, {})) return;
        if (click(d)) next(11); return;
    case 11: if (P.input.key(ImGuiKey_Enter)) next(12); return;
    case 12: {
        if (!ready()) return;
        const auto runs = sm->piping().network().value("runs", nlohmann::json::array());
        bool vertical = false;
        for (const auto& run : runs) {
            const auto path = run.value("path", nlohmann::json::array());
            if (path.size() != 2) continue;
            vertical |= std::fabs(path[0][0].get<double>() - 300) < 0.1 &&
                std::fabs(path[1][0].get<double>() - 300) < 0.1 &&
                std::fabs(path[0][1].get<double>() - 250) < 0.1 &&
                std::fabs(path[1][1].get<double>() - 250) < 0.1 &&
                std::fabs(path[0][2].get<double>()) < 0.1 &&
                std::fabs(path[1][2].get<double>() - 300) < 0.1;
        }
        if (!check("vertical_leg", runs.size() == 2 && vertical, sm->piping().network())) return;
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        shoot("pipedraw_02_riser.png"); next(13); return;
    }
    case 13:
        sm->handleCommand(st, "sim_piping_draw");
        if (P.input.key(ImGuiKey_Tab)) next(14); return;
    case 14: if (P.input.key(ImGuiKey_Tab)) next(15); return;
    case 15:
        // The new fourth Tab state is Auto, not XY. Choose XY for this branch explicitly.
        sm->piping().drawSettings().plane = 0;
        sm->piping().drawSettings().autoPlane = false;
        if (click(tee)) next(16); return;
    case 16: if (click(end)) next(17); return;
    case 17: if (P.input.key(ImGuiKey_Enter)) next(18); return;
    case 18:
        if (!ready()) return;
        if (!check("branch_tee_split", fittings("tee").size() == 1 &&
            sm->piping().network().value("runs", nlohmann::json::array()).size() == 4, sm->piping().network())) return;
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        shoot("pipedraw_03_branch.png"); next(19); return;
    case 19:
        sm->handleCommand(st, "sim_piping_fitting");
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        sm->piping().drawSettings().insertType = "valve";
        sm->piping().drawSettings().insertParams = {{"valve_type", "gate"}};
        next(195); return;
    case 195: {
        ImVec2 p;
        if (P.input.phase == 0 && !siFooterAction("Place fitting", p)) {
            shotScrollTo("Place fitting"); return;
        }
        if (P.input.click(P.input.phase == 0 ? p : P.input.point)) next(196);
        return;
    }
    case 196:
        if (!check("place_fitting_footer_armed", sm->piping().tool() == ui::PipingPanel::Tool::InsertFitting, {})) return;
        if (click({-150,-150,0})) next(20); return;
    case 20: {
        if (!ready()) return;
        const auto valves = fittings("valve");
        if (!check("gate_valve_attached", valves.size() == 1 && !valves[0].value("run_id", std::string()).empty() &&
            valves[0].value("params", nlohmann::json::object()).value("valve_type", std::string()) == "gate", valves)) return;
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        shoot("pipedraw_04_valve.png"); next(21); return;
    }
    case 21: {
        if (!ready()) return;
        nlohmann::json op = {{"op", "add_fitting"}, {"type", "custom"}, {"at", {150,-150,0}},
            {"params", {{"label", "Demo constant loss"}, {"k_model", {{"kind", "constant"}, {"K", 2.5}}}}}};
        if (!sm->piping().submitEdit(st, nlohmann::json::array({op}), "Custom fitting added")) {
            pipeDrawFail("custom_submit", sm->piping().lastError()); return;
        }
        next(22); return;
    }
    case 22: {
        if (!ready()) return;
        const auto custom = fittings("custom");
        if (!check("custom_constant_k", custom.size() == 1 &&
            custom[0].value("params", nlohmann::json::object()).value("k_model", nlohmann::json::object()) ==
                nlohmann::json{{"kind", "constant"}, {"K", 2.5}}, custom)) return;
        shoot("pipedraw_05_custom.png"); next(290); return;
    }
    // A second connected polyline changes planes without Enter/finish between points.
    // Keep it after the original run-count checks; its demand is at the new far endpoint.
    case 290:
        if (!ready()) return;
        P.planeRunIds.clear();
        for (const auto& run : sm->piping().network()["runs"])
            P.planeRunIds.push_back(run.value("id", std::string()));
        P.planeElbows = fittings("elbow").size();
        sm->handleCommand(st, "sim_piping_draw");
        sm->piping().drawSettings().plane = 0;
        sm->piping().drawSettings().autoPlane = false;
        sm->piping().drawSettings().planeOffsetMm = 300;
        next(291); return;
    case 291: if (click(d)) next(292); return;
    case 292:
        if (P.input.key(ImGuiKey_Tab)) {
            if (sm->piping().drawSettings().plane == 1 && !sm->piping().drawSettings().autoPlane)
                next(293);
        }
        return;
    case 293: if (click({300,250,500})) next(294); return;
    case 294:
        sm->piping().drawSettings().plane = 0;
        sm->piping().drawSettings().autoPlane = false;
        next(295); return;
    case 295: if (click({100,250,500})) next(296); return;
    case 296: if (P.input.key(ImGuiKey_Enter)) next(297); return;
    case 297: {
        if (!ready()) return;
        nlohmann::json committed = nlohmann::json::object();
        for (const auto& run : sm->piping().network()["runs"])
            if (std::find(P.planeRunIds.begin(), P.planeRunIds.end(), run.value("id", std::string())) == P.planeRunIds.end())
                committed = run;
        const auto path = committed.value("path", nlohmann::json::array());
        bool changedPlane = path.size() == 3;
        const double expected[3][3] = {{300,250,300}, {300,250,500}, {100,250,500}};
        if (changedPlane) for (int i = 0; i < 3; ++i) for (int k = 0; k < 3; ++k)
            changedPlane &= std::fabs(path[i][k].get<double>() - expected[i][k]) < 0.1;
        const auto elbows = fittings("elbow").size();
        if (!check("plane_switch_mid_run", changedPlane &&
            sm->piping().network()["runs"].size() == P.planeRunIds.size() + 1 &&
            elbows == P.planeElbows + 1,
            {{"run", committed}, {"elbows_before", P.planeElbows}, {"elbows_after", elbows},
             {"gesture", "XY start, Tab to XZ, vertical point, XY through last point, horizontal point, Enter"}})) return;
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        sm->piping().drawSettings().planeOffsetMm = 0;
        shoot("pipedraw_05b_plane_switch.png"); next(23); return;
    }
    case 23: {
        if (!ready()) return;
        nlohmann::json probe; std::string error;
        const bool called = st.worker.call("piping_fitting_k_cfd_status", {{"job_id", "pipedraw-availability-probe"}}, probe, error, 15000);
        if (!called || probe.value("reason", std::string()) != "unknown_job") {
            P.report["skipped"].push_back({{"step", "Quick CFD-K"}, {"status", "SKIP"},
                {"note", "Python CFD-K worker unavailable"}, {"response", probe}, {"error", error}});
            next(30); return;
        }
        // The plane-switch run leaves the Draw form up; the K editor lives on Fittings.
        sm->piping().setTool(st, ui::PipingPanel::Tool::None);
        sm->handleCommand(st, "sim_piping_fitting");
        sm->piping().selectFitting(st, P.elbow);
        next(24); return;
    }
    case 24:
        if (fieldButton("loss_fields", "source")) next(25); return;
    case 25:
        // A combo click that landed on a pre-reflow rect leaves no popup: reopen it.
        if (P.input.phase == 0 && !shotItem("From CFD", false) && pipingNow() - P.stepStart > 2.0) {
            next(24); return;
        }
        if (button("From CFD")) next(26); return;
    case 26:
        // fieldEnum uses segments when both quality options fit, otherwise ##combo.
        if (P.input.phase == 0) P.qualityCombo = shotItem("Quick", false) == nullptr;
        if (P.qualityCombo) {
            if (fieldButton("fitting_cfd_fields", "quality")) next(260);
        } else if (button("Quick")) next(261);
        return;
    case 260:
        if (P.input.phase == 0 && !shotItem("Quick", false) && pipingNow() - P.stepStart > 2.0) {
            next(26); return;
        }
        if (button("Quick")) next(261); return;
    case 261:
        if (button("Compute K from CFD###compute_k")) { P.cfdStart = pipingNow(); next(27); }
        return;
    case 27: {
        if (pipingNow() - P.cfdStart > 180) {
            pipeDrawFail("cfd_timeout", sm->piping().fittingCfdState().dump()); return;
        }
        const auto cfd = sm->piping().fittingCfdState();
        const bool cfdDone = cfd.value("state", std::string()) == "done" && cfd.value("has_result", false);
        if (cfdDone && P.cfdDoneAt <= 0.0) P.cfdDoneAt = pipingNow();
        // The completion status line reflows the footer; let the layout settle before reading its rect.
        if (P.input.phase > 0 || (cfdDone && pipingNow() - P.cfdDoneAt > 0.5)) {
            ImVec2 p;
            if (P.input.phase == 0 && !siFooterAction("Apply CFD K to fitting", p)) return;
            if (P.input.click(P.input.phase == 0 ? p : P.input.point)) next(28);
            return;
        }
        // Rendered status text is also captured by the test-engine hooks.
        for (const auto& item : ui::audit::observedItems()) {
            if (!item.value("text_only", false)) continue;
            const auto label = item.value("label", std::string());
            if (label.find("engine_unavailable") != std::string::npos ||
                label.find("worker_unavailable") != std::string::npos ||
                label.find("No module named") != std::string::npos) {
                P.report["skipped"].push_back({{"step", "Quick CFD-K"}, {"status", "SKIP"}, {"note", label}});
                next(30); return;
            }
        }
        {
            const std::string state = cfd.value("state", std::string());
            if (state == "failed" || state == "refused") {
                pipeDrawFail("cfd_failed", cfd.dump()); return;
            }
        }
        return;
    }
    case 28: {
        if (!ready()) return;
        if (P.applyStart <= 0.0) P.applyStart = pipingNow();
        nlohmann::json elbow = nlohmann::json::object();
        for (const auto& f : fittings("elbow")) if (f.value("id", std::string()) == P.elbow) elbow = f;
        const bool applied = elbow.value("params", nlohmann::json::object())
            .value("k_model", nlohmann::json::object()).value("kind", std::string()) == "table";
        // The footer click is consumed on the next UI frame; give the edit time to land.
        if (!applied && pipingNow() - P.applyStart < 15.0) return;
        if (!check("cfd_table_applied", applied, {{"elbow", elbow}, {"cfd", sm->piping().fittingCfdState()},
                                                   {"error", sm->piping().lastError()}})) return;
        shoot("pipedraw_06_cfd_k.png"); next(30); return;
    }
    case 30: {
        if (!ready()) return;
        sm->handleCommand(st, "sim_piping_solve");
        auto setup = sm->piping().setupDefinition(ui::PipingPanel::Form::Solve);
        setup["boundary"] = nlohmann::json::array({{{"at", a}, {"pressure", 300.0}},
            {{"at", {100,250,500}}, {"demand_lps", 0.5}}, {{"at", end}, {"demand_lps", 0.3}}});
        setup["network_type"] = "liquid"; setup["fluid"] = {{"name", "water"}, {"T_C", 20}};
        std::string error;
        if (!sm->piping().configureSetup(st, ui::PipingPanel::Form::Solve, setup, error)) {
            pipeDrawFail("solve_setup", error); return;
        }
        next(31); return;
    }
    case 31:
        if (!ready()) return;
        if (!sm->piping().runSolve(st)) { pipeDrawFail("solve_start", sm->piping().lastError()); return; }
        next(32); return;
    case 32: {
        if (!ready()) return;
        const auto result = sm->piping().solveResult();
        if (!result.is_object()) { pipeDrawFail("solve_result", sm->piping().lastError()); return; }
        if (!check("solve_converged", result.value("converged", false), result)) return;
        double maxResidual = 0, supply = 0, demand = 0;
        for (const auto& node : result.value("nodes", nlohmann::json::array())) {
            double net = 0;
            const auto id = node.value("id", std::string());
            for (const auto& link : result.value("links", nlohmann::json::array())) {
                const double q = link.value("flow_m3h", 0.0);
                if (link.value("from", std::string()) == id) net -= q;
                if (link.value("to", std::string()) == id) net += q;
            }
            if (node.value("fixed", false)) supply -= net;
            else {
                const double sink = node.value("demand_m3h", 0.0);
                demand += sink; maxResidual = std::max(maxResidual, std::fabs(net - sink));
            }
        }
        if (!check("mass_balance", demand > 0 && maxResidual < 0.002 && std::fabs(supply-demand) < 0.002,
            {{"supply_m3h", supply}, {"demand_m3h", demand}, {"max_node_residual_m3h", maxResidual}})) return;
        next(33); return;
    }
    case 33: if (button("Pressure")) next(34); return;
    case 34:
        if (!check("pressure_colour", sm->piping().colorBy() == 1, {})) return;
        shoot("pipedraw_07_pressure.png"); next(35); return;
    case 35: {
        if (!ready()) return;
        nlohmann::json list, found = nlohmann::json::object(); std::string error;
        if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, list, error, 15000)) {
            pipeDrawFail("study_list", error); return;
        }
        for (const auto& s : list.value("studies", nlohmann::json::array()))
            if (s.value("id", std::string()) == sm->piping().networkStudyId()) found = s;
        const auto persisted = found.value("params", nlohmann::json::object()).value("piping", nlohmann::json::object());
        if (!check("network_fittings_persisted", found.value("type", std::string()) == "piping_network" &&
            persisted.value("runs", nlohmann::json::array()) == sm->piping().network()["runs"] &&
            persisted.value("fittings", nlohmann::json::array()) == sm->piping().network()["fittings"] &&
            found.value("results", nlohmann::json::object()).contains("solve"), found)) return;
        P.report["ok"] = true; P.report["seconds"] = pipingNow() - P.started;
        P.done = true; P.capture = P.dir + "/pipedraw_08_final.png"; return;
    }
    // With exactly one draft point, exercise the full clear viewport rather than a
    // small cursor jiggle. Only pointer-position events are sent during this sweep.
    case 50:
        P.sweepZoom = st.zoom;
        P.sweepRadius = st.renderer->effectiveSceneRadius();
        P.sweepFrames = 0; P.sweepStable = true;
        P.report["zoom_sweep"] = nlohmann::json::array();
        next(51); return;
    case 51: {
        ImVec2 lo, hi;
        if (!ui::viewportClearRect(lo, hi)) { pipeDrawFail("zoom_sweep_viewport", "clear viewport unavailable"); return; }
        const float zoom = st.zoom, radius = st.renderer->effectiveSceneRadius();
        P.sweepStable &= std::fabs(zoom - P.sweepZoom) <= 1e-6f * std::max(std::fabs(P.sweepZoom), 1e-6f) &&
            std::fabs(radius - P.sweepRadius) <= 1e-6f * std::max(std::fabs(P.sweepRadius), 1e-6f);
        const float fractions[6][2] = {{0.1f,0.15f}, {0.9f,0.15f}, {0.1f,0.85f},
                                      {0.9f,0.85f}, {0.5f,0.25f}, {0.5f,0.75f}};
        const int position = P.sweepFrames / 2;
        const ImVec2 p(lo.x + (hi.x-lo.x)*fractions[position][0],
                       lo.y + (hi.y-lo.y)*fractions[position][1]);
        ImGui::GetIO().AddMousePosEvent(p.x, p.y);
        P.report["zoom_sweep"].push_back({{"frame", st.frame}, {"screen", {p.x,p.y}},
            {"zoom", zoom}, {"scene_radius", radius}});
        if (++P.sweepFrames == 12) next(52);
        return;
    }
    case 52: {
        const float zoom = st.zoom, radius = st.renderer->effectiveSceneRadius();
        const bool stable = P.sweepStable &&
            std::fabs(zoom - P.sweepZoom) <= 1e-6f * std::max(std::fabs(P.sweepZoom), 1e-6f) &&
            std::fabs(radius - P.sweepRadius) <= 1e-6f * std::max(std::fabs(P.sweepRadius), 1e-6f);
        if (!check("zoom_stable_while_drawing", stable && P.sweepFrames >= 12,
            {{"pointer_positions", 6}, {"sweep_frames", P.sweepFrames}, {"relative_tolerance", 1e-6},
             {"zoom_before", P.sweepZoom}, {"zoom_after", zoom},
             {"scene_radius_before", P.sweepRadius}, {"scene_radius_after", radius}})) return;
        shoot("pipedraw_00b_zoom_stable.png"); next(4); return;
    }
    default: return;
    }
}

bool pipeDrawEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& done) {
    auto& P = g_pd;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        if (!P.failed && P.capture.find("01_l_run") != std::string::npos) {
            auto pixels = sceneShotPixels(st, {{-200,-150,0}, {100,-150,0}, {300,50,0}});
            pixels["name"] = "pipe_visible_without_document_mesh";
            pixels["has_doc_mesh"] = st.hasDocMesh;
            pixels["ok"] = pixels.value("ok", false) && !st.hasDocMesh;
            P.report["checks"].push_back(pixels);
            if (!pixels.value("ok", false)) pipeDrawFail("pipe_invisible", pixels.dump());
        }
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        P.report["captures"].push_back(P.capture); P.capture.clear();
    }
    std::ofstream out(P.dir + "/pipedraw_report.json", std::ios::trunc);
    out << P.report.dump(2) << '\n'; out.close();
    glfwSwapBuffers(st.win);
    done = P.done;
    if (!out || (done && (P.failed || !P.report.value("ok", false)))) {
        std::fflush(nullptr); std::_Exit(EXIT_FAILURE);
    }
    return true;
}
struct CopilotUiShot {
    bool init = false, want = false, done = false, failed = false, workspace = false;
    int step = 0, settle = 0, auditStage = 0;
    double started = 0;
    std::string dir, capture, auditName, originalConv;
    SceneShotInput input;
    nlohmann::json report = {{"checks", nlohmann::json::array()}, {"audits", nlohmann::json::array()}};
} g_cpui;

constexpr const char* kCpWall = "Vertical wall (L-bracket, camera hangs underneath with access to the USB connector)";
constexpr const char* kCpEave = "Under an eave/ceiling (mounted upside down with the servos protected from direct rain)";
constexpr const char* kCpFlat = "Top of a flat surface (railing, ledge, shelf, or a narrow outdoor mounting plate)";
constexpr const char* kCpSeal = "Seal exposed joints against wind-driven rain while keeping the enclosure human-editable";
constexpr const char* kCpDrain = "Leave a drainage route below the servos without creating a pocket that traps water";

bool cpuiCheck(const char* name, bool ok, const nlohmann::json& evidence) {
    auto& P = g_cpui;
    P.report["checks"].push_back({{"name", name}, {"ok", ok}, {"evidence", evidence}});
    std::printf("copilot_ui: %s %s\n", ok ? "PASS" : "FAIL", name);
    std::fflush(stdout);
    if (!ok) { P.failed = P.done = true; P.capture = P.dir + "/FAILED.png"; }
    return ok;
}

bool cpuiRelated(const nlohmann::json& finding) {
    const auto name = finding.value("window", std::string());
    return name.find("Copilot") != std::string::npos || name.find("##ai_ws") != std::string::npos ||
           name.find("##ask") != std::string::npos;
}

void cpuiAudit(const char* name) {
    g_cpui.auditName = name;
    g_cpui.auditStage = 1;
    g_cpui.settle = 6;
    ImGui::GetIO().AddMousePosEvent(-FLT_MAX, -FLT_MAX);
}

void cpuiScrollTo(const char* label) {
    if (shotItem(label, false)) { shotScrollTo(label, nullptr, true); return; }
    // Offscreen auto-height cards may not submit their inputs. Scan the actual viewport with
    // hover + wheel, reversing only at a real boundary until the target is observable.
    static int frames = 0;
    static std::string target;
    static float direction = -1.0f;
    if (target != label) { target = label; direction = -1.0f; frames = 0; }
    for (const auto& w : ui::audit::observedWindows()) {
        const auto name = w.value("window", std::string());
        if ((name.find("/cp_chat_") == std::string::npos &&
             name.find("/##ai_ws_chatlog_") == std::string::npos) ||
            !w.value("wheel_enabled", false)) continue;
        const auto& p = w["wheel_point"];
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(p[0].get<float>(), p[1].get<float>());
        if (w.value("scroll_y", 0.0f) <= 0.5f) direction = -1.0f;
        else if (w.value("scroll_y", 0.0f) >= w.value("scroll_max_y", 0.0f) - 0.5f)
            direction = 1.0f;
        if (frames++ % 4 == 1) io.AddMouseWheelEvent(0.0f, direction);
        return;
    }
}

bool cpuiClick(const char* label, bool continuation = false) {
    auto& P = g_cpui;
    if (P.input.phase > 0) return P.input.click(P.input.point);
    const auto* item = shotItem(label, true);
    if (!item) { cpuiScrollTo(label); return false; }
    const auto& r = (*item)["full_rect"];
    const auto& clip = (*item)["clip"];
    if (r[1].get<float>() < clip[1].get<float>() - 1.0f ||
        r[3].get<float>() > clip[3].get<float>() + 1.0f) {
        cpuiScrollTo(label); return false;
    }
    auto point = shotPoint(*item);
    if (continuation) {
        if (!cpuiCheck("choice_label_wraps_without_clipping",
                r[3].get<float>() - r[1].get<float>() > ImGui::GetTextLineHeight() * 1.5f &&
                r[2].get<float>() <= clip[2].get<float>() + 1.0f, *item)) return false;
        point.y = r[3].get<float>() - ImGui::GetTextLineHeight() * 0.5f;
        point.x = r[0].get<float>() + ui::metrics::px(35.0f);
    }
    return P.input.click(point);
}

void copilotUiTick(AppState& st) {
    auto& P = g_cpui;
    if (!P.init) {
        P.init = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        P.want = st.shot && mode && std::strcmp(mode, "copilot_ui") == 0;
        if (!P.want) return;
        P.workspace = std::getenv("CHUDWARE_COPILOT_UI_WORKSPACE") != nullptr;
        const char* dir = std::getenv("CHUDWARE_COPILOT_UI_OUT");
        P.dir = dir ? dir : "scratch/copilot_ui_shots";
        std::filesystem::create_directories(P.dir);
        P.started = pipingNow();
    }
    if (!P.want || P.done) return;
    ui::audit::observedItems();
    if (pipingNow() - P.started > 180) {
        cpuiCheck("timeout", false, {{"step", P.step}, {"items", ui::audit::observedItems()},
                                     {"windows", ui::audit::observedWindows()}}); return;
    }
    if (!st.workerStarted || st.frame < 5 || !P.capture.empty()) return;
    st.activeWorkspace = P.workspace ? AppState::Workspace::AI : AppState::Workspace::Model;
    st.showAIPanel = true;
    if (P.settle > 0) { --P.settle; return; }
    if (P.auditStage == 1) {
        ui::audit::AuditOptions opt; opt.regions = ui::audit::RegionAll;
        if (!ui::audit::begin(P.auditName, opt)) return;
        P.auditStage = 2; return;
    }
    if (P.auditStage == 2) {
        if (ui::audit::active()) return;
        const auto result = ui::audit::lastResult();
        P.report["audits"].push_back(result);
        nlohmann::json related = nlohmann::json::array();
        for (const char* kind : {"conflicts", "overflows"})
            for (const auto& f : result[kind]) if (cpuiRelated(f)) related.push_back(f);
        if (!cpuiCheck(P.auditName.c_str(), related.empty(), related)) return;
        P.capture = P.dir + "/" + P.auditName + ".png";
        P.auditStage = 0; return;
    }
    ImGuiIO& io = ImGui::GetIO();
    auto next = [&](int step, int settle = 4) { P.step = step; P.settle = settle; };
    static AskBatch empty;
    auto& batch = st.chat.empty() || st.chat[0].asks.empty() ? empty : st.chat[0].asks[0];
    switch (P.step) {
    case 0: {
        glfwSetWindowSize(st.win, static_cast<int>(ui::metrics::px(1280.0f)),
                          static_cast<int>(ui::metrics::px(1100.0f)));
        st.chat.clear();
        st.designStarted = true;
        ChatMsg m;
        m.ts = 1.7e9;
        m.asks.push_back(askBatchFromEvent({{"batch_id", "copilot-responsive"},
            {"agent_id", "Mechanical design and manufacturing review team"},
            {"questions", nlohmann::json::array({
                {{"id", "mount"}, {"kind", "choice"}, {"required", true},
                 {"prompt", "Outdoor + command strips + small/stealthy changes the base. What surface does it stick to?"},
                 {"suggested", "eave"}, {"options", {{{"value", "wall"}, {"label", kCpWall}},
                    {{"value", "eave"}, {"label", kCpEave}}, {{"value", "flat"}, {"label", kCpFlat}}}}},
                {{"id", "weather"}, {"kind", "choice"}, {"multi", true},
                 {"prompt", "Rain exposure and service access?"}, {"options", {
                    {{"value", "seal"}, {"label", kCpSeal}}, {{"value", "drain"}, {"label", kCpDrain}},
                    {{"value", "service-front"}, {"label", "Service access"}},
                    {{"value", "service-rear"}, {"label", "Service access"}}}}},
                {{"id", "offset"}, {"kind", "dimension"}, {"family", "length"},
                 {"prompt", "How much clearance should remain between the roof and the servo horn?"}},
                {{"id", "references"}, {"kind", "file_request"}, {"multiple", true},
                 {"prompt", "Attach mounting reference images or documents."}}
            })}}));
        auto extra = askBatchFromEvent({{"batch_id", "copilot-geometry"}, {"questions", {
            {{"id", "entities"}, {"kind", "geometry_select"}, {"multi", true},
             {"prompt", "Which mounting faces should remain editable?"}, {"targets", {"face", "edge"}}},
            {{"id", "approval"}, {"kind", "approval"},
             {"prompt", "Add a drainage route and retain the original fastener pattern?"},
             {"proposal", "The mounting interface stays unchanged while the drainage slot opens below the servos."}}
        }}});
        extra.questions[0].picks = {
            {"face", "face-one", "", "Outer mounting face with a deliberately long engineering name", nullptr},
            {"edge", "edge-two", "", "Reference edge near the roof and mounting screw clearance", nullptr}};
        m.asks.push_back(std::move(extra));
        st.chat.push_back(std::move(m));
        for (int i = 0; i < 12; ++i) {
            ChatMsg tail; tail.ts = 1.7e9;
            tail.text = "Later transcript entry " + std::to_string(i) +
                ": the mounting interface remains editable and the selected dimensions stay recorded.";
            st.chat.push_back(std::move(tail));
        }
        ui::composerSetText(st, "Review the proposed mounting layout.");
        next(1, 12); break;
    }
    case 1:
        if (cpuiClick(kCpWall, true)) next(2); break;
    case 2:
        if (!cpuiCheck("continuation_line_selects_correct_raw_answer",
                       buildAskAnswers(batch).value("mount", nlohmann::json()) == "wall",
                       buildAskAnswers(batch))) return;
        cpuiAudit("01_wrapped_choices"); next(3); break;
    case 3:
        if (cpuiClick("##other")) next(4); break;
    case 4:
        io.AddInputCharactersUTF8("Adhesive bracket on a railing"); next(5); break;
    case 5:
        if (!cpuiCheck("typing_other_selects_custom_answer",
                askAnswerValue(batch.questions[0]) == "Adhesive bracket on a railing",
                buildAskAnswers(batch))) return;
        cpuiAudit("02_other_input"); next(6); break;
    case 6:
        if (cpuiClick(kCpSeal, true)) next(7); break;
    case 7:
        if (cpuiClick(kCpDrain, true)) next(8); break;
    case 8:
        if (!cpuiCheck("multiselect_retains_distinct_raw_values",
                askAnswerValue(batch.questions[1]) == nlohmann::json::array({"seal", "drain"}),
                buildAskAnswers(batch))) return;
        cpuiAudit("03_multiselect"); next(9); break;
    case 9:
        if (cpuiClick("##dim")) next(10); break;
    case 10:
        io.AddInputCharactersUTF8("12.5"); next(11); break;
    case 11:
        if (cpuiClick("##unit")) next(12); break;
    case 12:
        if (cpuiClick("cm")) next(13); break;
    case 13:
        if (!cpuiCheck("dimension_keeps_typed_value_and_selected_unit",
                askAnswerValue(batch.questions[2]) == nlohmann::json({{"value", 12.5}, {"unit", "cm"}}),
                buildAskAnswers(batch))) return;
        cpuiAudit("04_dimension"); next(14); break;
    case 14:
        if (cpuiClick("##paths")) next(15); break;
    case 15:
        io.AddInputCharactersUTF8("reference/front.png;reference/side.png"); next(16); break;
    case 16:
        if (!cpuiCheck("file_input_keeps_multiple_paths",
                askAnswerValue(batch.questions[3]) == nlohmann::json({{"paths", {"reference/front.png", "reference/side.png"}}}),
                buildAskAnswers(batch))) return;
        cpuiAudit("05_file_request"); next(17); break;
    case 17:
        if (!shotItem("Clear")) { cpuiScrollTo("Clear"); return; }
        cpuiAudit("06_geometry_with_picks"); next(170); break;
    case 170:
        if (cpuiClick("Clear")) next(18); break;
    case 18:
        if (!cpuiCheck("geometry_clear_removes_selected_entities",
                st.chat[0].asks[1].questions[0].picks.empty(), nlohmann::json::object())) return;
        cpuiAudit("06_geometry"); next(19); break;
    case 19:
        if (cpuiClick("Decline")) next(20); break;
    case 20:
        if (!cpuiCheck("approval_decline_is_false",
                askAnswerValue(st.chat[0].asks[1].questions[1]) == false,
                buildAskAnswers(st.chat[0].asks[1]))) return;
        next(200); break;
    case 200:
        if (cpuiClick("Approve")) next(21); break;
    case 21:
        if (!cpuiCheck("approval_can_change_to_true",
                askAnswerValue(st.chat[0].asks[1].questions[1]) == true,
                buildAskAnswers(st.chat[0].asks[1]))) return;
        cpuiAudit("07_approval"); next(22); break;
    case 22: {
        if (P.input.phase > 0) {
            if (P.input.click(P.input.point)) next(23, 8);
            break;
        }
        const auto* latest = shotItem("Jump to latest");
        if (!latest) { cpuiScrollTo(kCpWall); return; }
        const auto& r = (*latest)["full_rect"];
        bool clear = true;
        for (const auto& item : ui::audit::observedItems()) {
            if (!item.value("visible", false) || item.value("window", "").find("##ask") == std::string::npos) continue;
            const auto& q = item["rect"];
            clear &= q[2].get<float>() <= r[0].get<float>() || q[0].get<float>() >= r[2].get<float>() ||
                     q[3].get<float>() <= r[1].get<float>() || q[1].get<float>() >= r[3].get<float>();
        }
        if (!cpuiCheck("latest_footer_never_overlaps_question_controls", clear, *latest)) return;
        if (cpuiClick("Jump to latest")) next(23, 8);
        break;
    }
    case 23: {
        const char* region = P.workspace ? "aiws.chatlog" : "copilot.panel";
        if (!cpuiCheck("latest_click_reaches_tail_and_hides_action",
                       ui::chatLogAtBottom(region) && !shotItem("Jump to latest"),
                       {{"at_bottom", ui::chatLogAtBottom(region)}})) return;
        for (auto& b : st.chat[0].asks)
            applyAskAnswerEvent(b, {{"source", "user"}, {"answers", buildAskAnswers(b)}});
        next(24); break;
    }
    case 24: {
        const auto& windows = ui::audit::observedWindows();
        const auto summary = std::find_if(windows.begin(), windows.end(), [](const auto& w) {
            const auto& r = w["rect"];
            return w.value("window", "").find("##ask_done") != std::string::npos &&
                   r[3].template get<float>() - r[1].template get<float>() > ImGui::GetTextLineHeight() * 4.0f;
        });
        if (summary == windows.end()) { cpuiScrollTo("##ask_done"); return; }
        cpuiAudit("08_answered_summary"); next(25); break;
    }
    case 25:
        glfwSetWindowSize(st.win, static_cast<int>(ui::metrics::px(1280.0f)),
                          static_cast<int>(ui::metrics::px(580.0f)));
        next(26, 14); break;
    case 26: {
        const nlohmann::json* prompt = nullptr;
        for (const auto& item : ui::audit::observedItems())
            if (item.value("label", "").find("##aiprompt") == 0 && item.value("visible", false)) { prompt = &item; break; }
        if (!cpuiCheck("short_window_keeps_composer_inside_clip", prompt &&
                (*prompt)["full_rect"][3].get<float>() <= (*prompt)["clip"][3].get<float>() + 1.0f,
                prompt ? *prompt : ui::audit::observedItems())) return;
        cpuiAudit("09_short_window"); next(27); break;
    }
    case 27:
        if (P.workspace) { next(46); break; }
        cpuiAudit("10_compact_header");
        P.originalConv = st.activeConvId();
        next(28); break;
    case 28:
        if (cpuiClick("##hdr_tasks")) next(29); break;
    case 29:
        if (!cpuiCheck("header_tasks_opens_its_view", st.showTaskList &&
                !st.showSessionsPicker && !st.showArtifacts, nlohmann::json::object())) return;
        next(30); break;
    case 30:
        if (cpuiClick("##hdr_chat")) next(31); break;
    case 31:
        if (cpuiClick("##hdr_arts")) next(32); break;
    case 32:
        if (!cpuiCheck("header_artifacts_opens_its_view", st.showArtifacts &&
                !st.showTaskList && !st.showSessionsPicker, nlohmann::json::object())) return;
        next(33); break;
    case 33:
        if (cpuiClick("##hdr_chat")) next(34); break;
    case 34:
        if (cpuiClick("##hdr_sess")) next(35); break;
    case 35:
        if (!cpuiCheck("header_sessions_opens_its_view", st.showSessionsPicker &&
                !st.showTaskList && !st.showArtifacts, nlohmann::json::object())) return;
        next(36); break;
    case 36:
        if (cpuiClick("##hdr_chat")) next(37); break;
    case 37:
        if (cpuiClick("##hdr_gear")) next(38); break;
    case 38:
        if (!cpuiCheck("header_settings_opens_its_view", st.showAISettingsOverlay &&
                !st.showSessionsPicker && !st.showArtifacts, nlohmann::json::object())) return;
        next(39); break;
    case 39:
        if (cpuiClick("##hdr_chat")) next(40); break;
    case 40:
        if (cpuiClick("##cp_collapse")) next(41); break;
    case 41:
        if (!cpuiCheck("header_collapse_remains_reachable", st.settings.copilotCollapsed,
                        nlohmann::json::object())) return;
        cpuiAudit("11_collapsed_header"); next(42); break;
    case 42:
        if (cpuiClick("##cp_collapse")) next(43); break;
    case 43:
        if (!cpuiCheck("header_expand_restores_panel", !st.settings.copilotCollapsed,
                        nlohmann::json::object())) return;
        next(44); break;
    case 44:
        if (cpuiClick("##hdr_new")) next(45); break;
    case 45:
        if (!cpuiCheck("header_new_chat_starts_new_session", st.activeConvId() != P.originalConv &&
                st.chat.empty() && ui::composerText(st).empty() && st.pendingInlineImagePaths.empty(),
                {{"previous", P.originalConv}, {"active", st.activeConvId()}})) return;
        next(46); break;
    case 46:
        P.report["ok"] = true; P.done = true; P.capture = P.dir + "/final.png"; break;
    }
}

bool copilotUiEndFrame(AppState& st, int dw, int dh, const char* screenshotPath, bool& done) {
    auto& P = g_cpui;
    if (!P.want) return false;
    ++st.frame;
    if (!P.capture.empty()) {
        writeBackbufferPng(P.capture.c_str(), dw, dh, P.done ? screenshotPath : nullptr);
        P.report["captures"].push_back(P.capture); P.capture.clear();
    }
    std::ofstream out(P.dir + "/report.json", std::ios::trunc); out << P.report.dump(2) << '\n';
    glfwSwapBuffers(st.win);
    done = P.done;
    if (!out || (done && P.failed)) { std::fflush(nullptr); std::_Exit(EXIT_FAILURE); }
    return true;
}
}  // namespace

namespace ui {

void ScenarioRunner::tick(AppState& st) {
    // CHUDWARE_IDAUDIT=1: whole-run ImGui ID-conflict detection under any CHUDWARE_SHOT scenario.
    ui::audit::idAuditAutoStart();

    // ---------- manufacturing operator scenario (CHUDWARE_SHOT=manufacture) ----------
    // A hardware-free mirror fixture exercises the explicit Offline banner,
    // capability truth, stale/last-good telemetry, Stage-2 verdicts and a named
    // waiver. Reassert the workspace because startup document restoration may
    // otherwise pull a non-model shot back to Model.
    static bool manufactureInit = false;
    static bool manufactureShot = false;
    if (!manufactureInit) {
        manufactureInit = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        manufactureShot = st.shot && mode && std::strcmp(mode, "manufacture") == 0;
        if (manufactureShot) seedManufactureScenario(st);
    }
    if (manufactureShot) {
        st.showRibbon = true;
        st.ribbonTab = AppState::RT_Manufacture;
        st.activeWorkspace = AppState::Workspace::Manufacture;
    }

    // ---------- C17 M1 integration scenario (CHUDWARE_SHOT=m1) ----------
    // Frame-driven state machine: each step issues scripted ops through the REAL worker
    // (DocMirror), loads the rebuilt mesh, and requests a screenshot into scratch/m1_shots/.
    // Steps: New Part -> Box 60x40x10 -> Fillet r=4 (2 picked vertical edges) -> Shell t=2
    // (remove top face) -> edit Fillet r=8 (cached rebuild assertion) -> save + reopen.
    // The capture itself happens in endFrame() when m1Capture is non-empty.
    if (st.wantM1 && st.workerStarted && !st.m1ShotDone) {
        // Resolve repo-relative scratch dir for screenshots.
        if (!m1DirReady) {
            m1DirReady = true;
            const char* repo = std::getenv("CHUDWARE_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            m1SavePath = base + "/scratch/m1_shots";
            // W1-B (item 12): create the output dir robustly. The old code left a dead `mk`
            // variable and never actually created the dir, so captures silently failed when it
            // was missing; an earlier ad-hoc string join also produced the malformed stray dir
            // `CUsers...scratch` in the repo root. Use std::filesystem on the joined path.
            std::error_code ec; std::filesystem::create_directories(m1SavePath, ec);
        }
        auto loadMesh = [&](const core::OpsResponse& r) {
            std::string error;
            if (!r.ok || r.aicm_path.empty() || !st.loadDocMesh(r.aicm_path, error)) {
                std::fprintf(stderr, "m1: mesh load failed: %s %s\n",
                             r.error_message.c_str(), error.c_str());
                std::fflush(nullptr);
                std::_Exit(EXIT_FAILURE);
            }
        };
        auto statusMs = [](const core::OpsResponse& r, const std::string& fid) -> double {
            if (!r.feature_status.is_array()) return -1.0;
            for (const auto& s : r.feature_status)
                if (s.is_object() && s.value("id", std::string()) == fid)
                    return s.value("ms", -1.0);
            return -1.0;
        };
        // Find vertical edges (axis ~ +Z) by resolving each edge topo id.
        auto verticalEdges = [&](int wantN) {
            std::vector<nlohmann::json> out;
            for (int tid = 1; tid <= 12 && (int)out.size() < wantN; ++tid) {
                std::string serr;
                std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, serr);
                if (s.empty()) continue;
                try {
                    nlohmann::json o = nlohmann::json::parse(s);
                    if (o.contains("axis") && o["axis"].is_array() && o["axis"].size() == 3 &&
                        std::fabs(o["axis"][2].get<double>()) > 0.9)
                        out.push_back(o);
                } catch (...) {}
            }
            return out;
        };
        // Find the top face (max centroid z) by resolving face topo ids.
        auto topFace = [&]() -> nlohmann::json {
            nlohmann::json best; double bestZ = -1e30;
            for (int tid = 1; tid <= 40; ++tid) {
                std::string serr;
                std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, serr);
                if (s.empty()) break;
                try {
                    nlohmann::json o = nlohmann::json::parse(s);
                    if (!o.contains("centroid")) continue;
                    double z = o["centroid"][2].get<double>();
                    if (z > bestZ) { bestZ = z; best = o; }
                } catch (...) {}
            }
            return best;
        };
        // Settle gate: after a step runs its ops we wait a few frames so the viewport + tree
        // repaint before capturing. m1Settle counts those frames.
        const int kSettle = 4;
        if (m1Settle > 0) { --m1Settle; }
        else if (m1Capture.empty()) {  // only advance when no capture is pending
            std::string err;
            switch (m1Step) {
                case 0: {  // New Part
                    if (st.docMirror.newPart(st.worker, err)) {
                        std::printf("m1: new_part doc_id=%s rev=%d\n",
                                    st.docMirror.docId().c_str(), st.docMirror.revision());
                    } else std::printf("m1: new_part FAILED: %s\n", err.c_str());
                    std::fflush(stdout);
                    m1Capture = m1SavePath + "/m1_01_newpart.png"; m1Settle = kSettle;
                    break;
                }
                case 1: {  // Box 60x40x10
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},{"params",{
                            {"length",{{"value",60.0},{"expr",nullptr}}},
                            {"width", {{"value",40.0},{"expr",nullptr}}},
                            {"height",{{"value",10.0},{"expr",nullptr}}},
                            {"centered",true}}}}}});
                    auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                    std::string boxId = r.id_map.is_object() && r.id_map.contains("$0")
                                      ? r.id_map["$0"].get<std::string>() : "f1";
                    std::printf("m1: box ok=%d rev=%d id=%s ms=%.2f\n",
                                (int)r.ok, r.revision, boxId.c_str(), statusMs(r, boxId));
                    std::fflush(stdout);
                    // C32-A material proof: CHUDWARE_MATERIAL=<plastic|aluminum|steel|brass|dark>
                    // applies the matching RealView preset to body b1 so a headless shot can be
                    // judged for metallic realism (clear env reflection + tinted fresnel).
                    if (const char* mat = std::getenv("CHUDWARE_MATERIAL")) {
                        struct P { const char* k; float m, rough, rgb[3]; };
                        static const P kP[] = {
                            {"plastic",  0.00f, 0.50f, {0.71f, 0.76f, 0.82f}},
                            {"aluminum", 0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},
                            {"steel",    0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},
                            {"brass",    0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},
                            {"dark",     0.90f, 0.45f, {0.16f, 0.17f, 0.19f}},
                        };
                        for (const auto& p : kP) {
                            if (std::strcmp(mat, p.k) == 0) {
                                nlohmann::json mop = nlohmann::json::array();
                                mop.push_back({{"op","set_appearance"},{"id","b1"},
                                    {"color",{p.rgb[0],p.rgb[1],p.rgb[2]}},
                                    {"metallic",p.m},{"roughness",p.rough}});
                                auto rm = st.docMirror.applyOps(st.worker, mop); loadMesh(rm);
                                std::printf("m1: material '%s' applied ok=%d rev=%d\n",
                                            p.k, (int)rm.ok, rm.revision);
                                std::fflush(stdout);
                                break;
                            }
                        }
                    }
                    st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                    m1Capture = m1SavePath + "/m1_02_box.png"; m1Settle = kSettle;
                    break;
                }
                case 2: {  // Fillet r=4 on two picked vertical edges
                    auto edges = verticalEdges(2);
                    std::printf("m1: picked %zu vertical edges for fillet\n", edges.size());
                    nlohmann::json arr = nlohmann::json::array();
                    for (auto& e : edges) arr.push_back(e);
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","add_feature"},{"feature",{
                        {"type","fillet"},{"name","Fillet1"},
                        {"params",{{"radius",{{"value",4.0},{"expr",nullptr}}}}},
                        {"refs",{{"edges",arr}}}}}});
                    auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                    m1FilletId = r.id_map.is_object() && r.id_map.contains("$0")
                               ? r.id_map["$0"].get<std::string>() : "f2";
                    std::printf("m1: fillet ok=%d rev=%d id=%s ms=%.2f\n",
                                (int)r.ok, r.revision, m1FilletId.c_str(),
                                statusMs(r, m1FilletId));
                    std::fflush(stdout);
                    m1Capture = m1SavePath + "/m1_03_fillet.png"; m1Settle = kSettle;
                    break;
                }
                case 3: {  // Shell t=2 remove top face
                    nlohmann::json top = topFace();
                    nlohmann::json remove = nlohmann::json::array();
                    if (top.is_object()) remove.push_back(top);
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","add_feature"},{"feature",{
                        {"type","shell"},{"name","Shell1"},
                        {"params",{{"thickness",{{"value",2.0},{"expr",nullptr}}}}},
                        {"refs",{{"remove",remove}}}}}});
                    auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                    std::string shellId = r.id_map.is_object() && r.id_map.contains("$0")
                                        ? r.id_map["$0"].get<std::string>() : "f3";
                    std::printf("m1: shell ok=%d rev=%d id=%s ms=%.2f\n",
                                (int)r.ok, r.revision, shellId.c_str(), statusMs(r, shellId));
                    // Record pre-edit timings for the cached-rebuild assertion.
                    m1FilletMsBeforeEdit = statusMs(r, m1FilletId);
                    m1BoxMsBeforeEdit = statusMs(r, "f1");
                    std::fflush(stdout);
                    m1Capture = m1SavePath + "/m1_04_shell.png"; m1Settle = kSettle;
                    break;
                }
                case 4: {  // Edit Fillet r=8 — cached rebuild: box (f1) cached, fillet+shell recompile
                    // Drive this through the PropertyManager edit path so the form flow is
                    // exercised, then commit via edit_feature.
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","edit_feature"},{"id",m1FilletId},
                        {"params",{{"radius",{{"value",8.0},{"expr",nullptr}}}}},
                        {"refs",nlohmann::json::object()}});
                    auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                    double boxAfter = statusMs(r, "f1");
                    double filletAfter = statusMs(r, m1FilletId);
                    std::printf("m1: edit_fillet ok=%d rev=%d fillet_ms=%.2f\n",
                                (int)r.ok, r.revision, filletAfter);
                    std::printf("m1: feature timings box %.2f -> %.2f ms; fillet %.2f -> %.2f ms\n",
                                m1BoxMsBeforeEdit, boxAfter, m1FilletMsBeforeEdit, filletAfter);
                    // Open the PM on the fillet so the screenshot shows the populated edit form.
                    st.propertyManager->openForEdit(st, m1FilletId);
                    std::fflush(stdout);
                    m1Capture = m1SavePath + "/m1_05_edit.png"; m1Settle = kSettle;
                    break;
                }
                case 5: {  // Save .step+ then reopen; assert tree intact (3 features)
                    st.propertyManager->closeIfActive();  // tree returns for the reopen screenshot
                    // Native round-trip: must be the .step+ container extension. doc_open only
                    // treats a path as native (feature tree preserved) when the extension is
                    // exactly ".step+"; a bare ".step" routes to the neutral-import lane, which
                    // drops the parametric tree (rev=0, features=0). Matches the imgui scenario.
                    std::string sp = m1SavePath + "/part.step+";
                    std::string serr;
                    bool saved = st.docMirror.save(st.worker, sp, serr);
                    std::printf("m1: save ok=%d path=%s err=%s\n", (int)saved, sp.c_str(),
                                serr.c_str());
                    bool opened = st.docMirror.open(st.worker, sp, serr);
                    int nfeat = 0;
                    const auto& tr = st.docMirror.tree();
                    if (tr.is_object() && tr.contains("features") && tr["features"].is_array())
                        nfeat = (int)tr["features"].size();
                    std::printf("m1: reopen ok=%d rev=%d features=%d %s\n",
                                (int)opened, st.docMirror.revision(), nfeat,
                                nfeat == 3 ? "(tree intact)" : "(TREE MISMATCH)");
                    std::fflush(stdout);
                    // Adopt the saved mesh, as the normal open path does; never force a rebuild.
                    st.addDocTab(false);
                    std::string meshError;
                    if (!saved || !opened || nfeat != 3 ||
                        !st.docMirror.lastOpenedWarm() || !st.docMirror.lastOpenedCacheHit() ||
                        !st.loadDocMesh(st.docMirror.lastOpenedAicm(), meshError)) {
                        std::fprintf(stderr, "m1: cached reopen failed: %s %s\n",
                                     serr.c_str(), meshError.c_str());
                        std::fflush(nullptr);
                        std::_Exit(EXIT_FAILURE);
                    }
                    st.docTabs[st.activeTab].path = sp;
                    st.docTabs[st.activeTab].dirty = false;
                    st.applyDocDrivenVisibility(st.docMirror.tree());
                    const int openedTab = st.activeTab;
                    const auto openedDoc = st.docMirror.docId();
                    const auto tabCount = st.docTabs.size();
                    const auto absolutePath =
                        std::filesystem::absolute(std::filesystem::u8path(sp)).generic_u8string();
                    if (!ui::featureTreeOpenPath(st, absolutePath) || st.activeTab != openedTab ||
                        st.docMirror.docId() != openedDoc || st.docTabs.size() != tabCount) {
                        std::fprintf(stderr, "m1: canonical reopen did not reuse the open document\n");
                        std::fflush(nullptr);
                        std::_Exit(EXIT_FAILURE);
                    }
                    std::printf("m1: cached mesh restored; canonical reopen reused the active document\n");
                    m1Capture = m1SavePath + "/m1_06_reopen.png"; m1Settle = kSettle;
                    break;
                }
                default:
                    st.m1ShotDone = true;  // scenario complete; loop teardown handles exit
                    std::printf("m1: scenario complete\n");
                    std::fflush(stdout);
                    break;
            }
            if (m1Step <= 5) ++m1Step;
        }
    }

    // ---------- C18 sketch scenario (CHUDWARE_SHOT=sketch) ----------
    // new part -> sketch on plane.top -> corner rect (ops + h/v + fix + 2 dims) -> sketch_enter
    // -> screenshot (entities + status) -> drag a corner + commit -> set width 80->100 -> shot
    // -> exit -> extrude the region depth 10 -> shot of the solid. Per-step captures use the
    // same backbuffer-write mechanism as m1 (sketchCapture).
    // Hold the scenario for a few frames so the dockspace builds + docks the panels before the
    // first (step-0) capture; otherwise the first screenshot lands before the layout exists.
    if (st.wantSketch && st.workerStarted && !st.sketchShotDone && st.frame >= 5) {
        auto* sk = st.sketchMode;
        if (!skDirReady) {
            skDirReady = true;
            const char* repo = std::getenv("CHUDWARE_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            skSavePath = base + "/scratch/sketch_shots";
            std::error_code ec; std::filesystem::create_directories(skSavePath, ec);  // W1-B (item 12)
        }
        auto applyOne = [&](const nlohmann::json& op) -> core::OpsResponse {
            return st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        };
        const int kSettle = 4;
        if (skSettle > 0) { --skSettle; }
        else if (sketchCapture.empty()) {
            std::string err;
            switch (skStep) {
                case 0: {  // new part + sketch + rect (entities/relations/dims) + enter
                    if (!st.docMirror.newPart(st.worker, err)) {
                        std::printf("sketch: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                        st.sketchShotDone = true; break;
                    }
                    auto r = applyOne({{"op","add_feature"},{"feature",{
                        {"type","sketch"},{"name","Sketch1"},
                        {"refs",{{"plane","plane.top"}}},
                        {"params",{{"entities",nlohmann::json::array()},
                                   {"constraints",nlohmann::json::array()}}}}}});
                    skFid = (r.id_map.is_object() && r.id_map.contains("$0")) ? r.id_map["$0"].get<std::string>() : "f1";
                    // four rect lines, chaining coincident corners via real ids
                    auto addLine = [&](double x1,double y1,double x2,double y2,bool horiz,const std::string& prev)->std::string{
                        nlohmann::json cs = nlohmann::json::array();
                        cs.push_back({{"type",horiz?"horizontal":"vertical"},{"refs",{"$self"}}});
                        if (!prev.empty()) cs.push_back({{"type","coincident"},{"refs",{prev+".p2","$self.p1"}}});
                        auto rr = applyOne({{"op","sketch_add_entity"},{"sketch",skFid},
                            {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}},{"constraints",cs}});
                        return (rr.id_map.is_object() && rr.id_map.contains("$0")) ? rr.id_map["$0"].get<std::string>() : "";
                    };
                    skL0 = addLine(0,0,80,0,true,"");
                    skL1 = addLine(80,0,80,40,false,skL0);
                    skL2 = addLine(80,40,0,40,true,skL1);
                    skL3 = addLine(0,40,0,0,false,skL2);
                    applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                        {"constraint",{{"type","coincident"},{"refs",{skL3+".p2",skL0+".p1"}}}}});
                    applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                        {"constraint",{{"type","fix"},{"refs",{skL0+".p1"}}}}});
                    applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                        {"constraint",{{"type","distance"},{"refs",{skL0+".p1",skL0+".p2"}},
                                       {"value",{{"value",80.0},{"expr",nullptr}}}}}});
                    auto rH = applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                        {"constraint",{{"type","distance"},{"refs",{skL1+".p1",skL1+".p2"}},
                                       {"value",{{"value",40.0},{"expr",nullptr}}}}}});
                    skWidthCid = (rH.id_map.is_object() && rH.id_map.contains("$0")) ? "" : "";
                    // find the width constraint id by scanning the sketch params (it's the
                    // distance on skL0). We re-read the tree to locate it.
                    {
                        const auto& tr = st.docMirror.tree();
                        if (tr.contains("features"))
                          for (auto& f : tr["features"])
                            if (f.value("id",std::string())==skFid && f.contains("params"))
                              for (auto& c : f["params"].value("constraints",nlohmann::json::array()))
                                if (c.value("type",std::string())=="distance" && c.contains("refs") &&
                                    c["refs"].size()==2 && c["refs"][0].get<std::string>()==skL0+".p1")
                                  skWidthCid = c.value("id",std::string());
                    }
                    if (sk) sk->enterForFeature(st, skFid);
                    st.zoom = 1.0f;
                    std::printf("sketch: new sketch %s rect built (width_cid=%s) entered; status=%s\n",
                                skFid.c_str(), skWidthCid.c_str(), sk ? sk->statusLine().c_str() : "?");
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "/sketch_01_enter.png"; skSettle = kSettle;
                    break;
                }
                case 1: {  // drag a corner + commit, then set width 80 -> 100
                    nlohmann::json dres;
                    nlohmann::json dragP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid},
                        {"handle",skL1+".p2"},{"xy",nlohmann::json::array({85.0,45.0})}};
                    st.worker.call("sketch_drag", dragP, dres, err);
                    nlohmann::json cres;
                    nlohmann::json cP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid}};
                    st.worker.call("sketch_drag_commit", cP, cres, err);
                    // resync mirror (commit bumped the revision) then set the width dim
                    st.docMirror.resync(st.worker);
                    if (!skWidthCid.empty())
                        st.docMirror.applyOps(st.worker, nlohmann::json::array({
                            {{"op","sketch_set_dim"},{"sketch",skFid},{"id",skWidthCid},{"value",100.0}}}));
                    nlohmann::json sres;
                    st.worker.call("sketch_solve", {{"doc_id",st.docMirror.docId()},{"sketch",skFid}}, sres, err);
                    if (sk) sk->enterForFeature(st, skFid);  // refresh overlay entities/status
                    std::printf("sketch: drag+commit, width 80->100, re-solved; status=%s dof=%d\n",
                                sk ? sk->statusLine().c_str() : "?", sres.value("dof",-1));
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "/sketch_02_dim.png"; skSettle = kSettle;
                    break;
                }
                case 2: {  // exit + extrude region depth 10
                    if (sk) sk->exitSketch(st, /*keep=*/true);
                    st.docMirror.resync(st.worker);
                    nlohmann::json region = {{"kind","region"},{"sketch",skFid},
                        {"centroid",nlohmann::json::array({50.0,20.0})},{"hint_index",0}};
                    auto r = applyOne({{"op","add_feature"},{"feature",{
                        {"type","extrude"},{"name","Boss-Extrude1"},
                        {"params",{{"depth",{{"value",10.0},{"expr",nullptr}}},{"end","blind"},{"mode","boss"}}},
                        {"refs",{{"profile",nlohmann::json::array({region})}}}}}});
                    if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    bool exok = false;
                    if (r.feature_status.is_array() && !r.feature_status.empty())
                        exok = r.feature_status.back().value("ok",false);
                    st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                    // T-WIN1: reset residual pan before the headless capture. Entering the sketch
                    // animates a Normal-To that leaves st.pan offset, so zoom=1 alone frames the
                    // scene radius but off-CENTRE -> the solid falls outside the shot. ZoomFit fixes
                    // this via kZeroPan; do the same directly here. Capture-only, platform-agnostic.
                    st.pan[0] = st.pan[1] = st.pan[2] = 0.0f;
                    std::printf("sketch: exit + extrude region depth 10 ok=%d\n", (int)exok);
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "/sketch_03_solid.png"; skSettle = kSettle;
                    break;
                }
                case 3: {  // mid-height sketch (z=5) + model refs resolved from the solid
                    auto r = applyOne({{"op","add_feature"},{"feature",{
                        {"type","sketch"},{"name","RelSketch"},
                        {"refs",{{"plane","plane.top"}}},
                        {"params",{{"offset",{{"value",5.0},{"expr",nullptr}}},
                                   {"entities",{
                                       {{"id","e1"},{"type","line"},{"p1",{10.0,46.0}},{"p2",{60.0,47.0}}},
                                       {{"id","e2"},{"type","point"},{"p",{92.0,35.0}}},
                                       {{"id","e3"},{"type","point"},{"p",{95.0,5.0}}}}},
                                   {"constraints",nlohmann::json::array()},
                                   {"auto_define",false}}}}}});
                    skRelFid = (r.id_map.is_object() && r.id_map.contains("$0")) ? r.id_map["$0"].get<std::string>() : "";
                    skRelLine = "e1"; skRelPierce = "e2"; skRelVertex = "e3";
                    // Classify the solid's topology (x 0..100, y 0..40, z 0..10) from resolved picks.
                    auto centroidOf = [](const nlohmann::json& o, double c[3]) {
                        if (!o.contains("centroid") || !o["centroid"].is_array() || o["centroid"].size() < 3) return false;
                        for (int k = 0; k < 3; ++k) c[k] = o["centroid"][k].get<double>();
                        return true;
                    };
                    for (uint32_t tid = 1; tid <= 24; ++tid) {
                        std::string se;
                        const std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, se);
                        auto o = nlohmann::json::parse(s, nullptr, false);
                        double c[3];
                        if (!o.is_object() || !centroidOf(o, c)) continue;
                        if (std::abs(c[2] - 10.0) < 1e-3 && std::abs(c[1] - 40.0) < 1e-3) { skTopEdgeSel = s; skTopEdgeTid = (int)tid; }
                        if (std::abs(c[2] - 5.0) < 1e-3 && std::abs(c[0] - 100.0) < 1e-3 && std::abs(c[1] - 40.0) < 1e-3) skVertEdgeSel = s;
                    }
                    for (uint32_t tid = 1; tid <= 16; ++tid) {
                        std::string se;
                        const std::string s = st.docMirror.resolvePick(st.worker, 1, "vertex", tid, se);
                        auto o = nlohmann::json::parse(s, nullptr, false);
                        double c[3];
                        if (!o.is_object() || !centroidOf(o, c)) continue;
                        if (std::abs(c[0] - 100.0) < 1e-3 && std::abs(c[1]) < 1e-3 && std::abs(c[2] - 10.0) < 1e-3) skVertexSel = s;
                    }
                    for (uint32_t tid = 1; tid <= 8; ++tid) {
                        std::string se;
                        const std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                        auto o = nlohmann::json::parse(s, nullptr, false);
                        double c[3];
                        if (o.is_object() && centroidOf(o, c) && std::abs(c[2] - 10.0) < 1e-3) { skTopFaceSel = s; skTopFaceTid = (int)tid; }
                    }
                    if (sk && !skRelFid.empty()) sk->enterForFeature(st, skRelFid);
                    // Headless runs have no pointer: park it inside the viewport so the Add
                    // Relation popup (placed at the mouse) opens on screen in the next step.
                    if (ImVec2 lo, hi; ui::viewportClearRect(lo, hi))
                        ImGui::GetIO().AddMousePosEvent(lo.x + (hi.x - lo.x) * 0.35f,
                                                        lo.y + (hi.y - lo.y) * 0.30f);
                    std::printf("sketch: relation sketch %s; refs top_edge=%d vertical_edge=%d vertex=%d top_face=%d\n",
                                skRelFid.c_str(), (int)!skTopEdgeSel.empty(), (int)!skVertEdgeSel.empty(),
                                (int)!skVertexSel.empty(), (int)!skTopFaceSel.empty());
                    std::fflush(stdout);
                    skSettle = 30;   // let Normal-To finish before the popup capture
                    break;
                }
                case 4: {  // Add Relation popup over a sketch line + a selected model edge
                    st.selection.clear();
                    if (!skTopEdgeSel.empty()) {
                        core::SelItem it;
                        it.hit.body_id = 1; it.hit.kind = render::PickKind::Edge;
                        it.hit.topo_id = (uint32_t)skTopEdgeTid;
                        it.selector = skTopEdgeSel; it.pending_resolve = false;
                        st.selection.mutableItems().push_back(it);
                    }
                    if (sk) { sk->selectEntities({skRelLine}); sk->onRibbonCommand(st, "sk_relations"); }
                    std::printf("sketch: Add Relation popup opened for %s + model edge\n", skRelLine.c_str());
                    std::fflush(stdout);
                    skSettle = 3;   // auto-resized ImGui windows stay hidden on their first frame
                    break;
                }
                case 5: {  // capture the popup once it has a size
                    sketchCapture = skSavePath + "/sketch_04_relpopup.png"; skSettle = kSettle;
                    break;
                }
                case 6: {  // the popup's batches: collinear to the edge, pierce, vertex coincident
                    st.selection.clear();
                    ImGui::GetIO().AddKeyEvent(ImGuiKey_Escape, true);   // dismiss the popup
                    auto batch = [&](const nlohmann::json& ref, const nlohmann::json& con) {
                        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                            {{"op","sketch_convert"},{"sketch",skRelFid},{"refs",nlohmann::json::array({ref})},
                             {"construction",true},{"fix",true},{"associative",true}},
                            {{"op","sketch_add_constraint"},{"sketch",skRelFid},{"constraint",con}}}));
                        if (!r.ok) std::printf("sketch: relation batch FAILED: %s\n", r.error_message.c_str());
                        return r.ok;
                    };
                    const auto topEdge = nlohmann::json::parse(skTopEdgeSel, nullptr, false);
                    const auto vertEdge = nlohmann::json::parse(skVertEdgeSel, nullptr, false);
                    const auto vertex = nlohmann::json::parse(skVertexSel, nullptr, false);
                    const bool okCol = batch({{"kind","edge"},{"selector",topEdge}},
                        {{"type","collinear"},{"refs",{skRelLine,"$0"}}});
                    const bool okPie = batch({{"kind","edge"},{"selector",vertEdge},{"mode","pierce"},{"near",{92.0,35.0}}},
                        {{"type","coincident"},{"refs",{skRelPierce + ".p","$0"}}});
                    const bool okVtx = batch({{"kind","vertex"},{"selector",vertex}},
                        {{"type","coincident"},{"refs",{skRelVertex + ".p","$0"}}});
                    nlohmann::json sres;
                    st.worker.call("sketch_solve", {{"doc_id",st.docMirror.docId()},{"sketch",skRelFid}}, sres, err);
                    auto ent = [&](const std::string& id) {
                        if (sres.contains("entities") && sres["entities"].is_array())
                            for (const auto& e : sres["entities"]) if (e.value("id",std::string()) == id) return e;
                        return nlohmann::json();
                    };
                    const auto L = ent(skRelLine), P = ent(skRelPierce), Q = ent(skRelVertex);
                    std::printf("sketch: model relations collinear=%d pierce=%d vertex=%d -> line y=(%.3f,%.3f) pierce=(%.3f,%.3f) vertex=(%.3f,%.3f) dof=%d\n",
                                (int)okCol, (int)okPie, (int)okVtx,
                                L.is_object() ? L["p1"][1].get<double>() : -1.0, L.is_object() ? L["p2"][1].get<double>() : -1.0,
                                P.is_object() ? P["p"][0].get<double>() : -1.0, P.is_object() ? P["p"][1].get<double>() : -1.0,
                                Q.is_object() ? Q["p"][0].get<double>() : -1.0, Q.is_object() ? Q["p"][1].get<double>() : -1.0,
                                sres.value("dof", -1));
                    std::fflush(stdout);
                    if (sk) sk->enterForFeature(st, skRelFid);
                    skSettle = 30;
                    break;
                }
                case 7: {  // Fully Define + Display/Delete Relations panel
                    ImGui::GetIO().AddKeyEvent(ImGuiKey_Escape, false);
                    if (sk) {
                        sk->clearEntitySelection();
                        sk->onRibbonCommand(st, "sk_fully_define");
                        sk->onRibbonCommand(st, "sk_display_relations");
                    }
                    std::printf("sketch: fully define -> %s\n", sk ? sk->statusLine().c_str() : "?");
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "/sketch_05_relations.png"; skSettle = kSettle;
                    break;
                }
                case 8: {  // Convert Entities of the solid's top face (one batched op), timed
                    if (sk) sk->onRibbonCommand(st, "sk_display_relations");
                    st.selection.clear();
                    if (!skTopFaceSel.empty()) {
                        core::SelItem it;
                        it.hit.body_id = 1; it.hit.kind = render::PickKind::Face;
                        it.hit.topo_id = (uint32_t)skTopFaceTid;
                        it.selector = skTopFaceSel; it.pending_resolve = false;
                        st.selection.mutableItems().push_back(it);
                    }
                    const auto t0 = std::chrono::steady_clock::now();
                    if (sk) sk->onRibbonCommand(st, "sk_convert_ent");
                    const double ms = std::chrono::duration<double, std::milli>(
                        std::chrono::steady_clock::now() - t0).count();
                    std::printf("sketch: convert top face %.1f ms -> %s\n", ms,
                                sk ? sk->statusLine().c_str() : "?");
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "/sketch_06_convert.png"; skSettle = kSettle;
                    break;
                }
                default:
                    if (sk) sk->exitSketch(st, /*keep=*/true);
                    st.sketchShotDone = true;
                    std::printf("sketch: scenario complete\n"); std::fflush(stdout);
                    break;
            }
            if (skStep <= 8) ++skStep;
        }
    }

    // ---------- 3D sketch scenario (CHUDWARE_SHOT=sketch3d) ----------
    // new part -> add a sketch3d feature (Z-axis line chained into a 3-pt arc; fix + along_axis
    // + coincident constraints) -> enter the 3D sketcher (overlay + toolbar + working-plane grid)
    // -> screenshot -> constrained drag of the arc end via sketch3d_drag + edit_feature commit
    // -> screenshot. Same per-step backbuffer capture mechanism as the 2D sketch scenario.
    if (st.wantSketch3d && st.workerStarted && !st.sketch3dShotDone && st.frame >= 5) {
        if (!s3DirReady) {
            s3DirReady = true;
            const char* repo = std::getenv("CHUDWARE_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            s3SavePath = base + "/scratch/sketch3d_shots";
            std::error_code ec; std::filesystem::create_directories(s3SavePath, ec);
        }
        const int kSettle = 4;
        if (s3Settle > 0) { --s3Settle; }
        else if (s3Capture.empty()) {
            std::string err;
            switch (s3Step) {
                case 0: {  // new part + sketch3d (line + 3-pt arc, constrained) + enter
                    if (!st.docMirror.newPart(st.worker, err)) {
                        std::printf("sketch3d: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                        st.sketch3dShotDone = true; break;
                    }
                    nlohmann::json s3op = nlohmann::json::parse(R"JSON(
                        {"op":"add_feature","feature":{"type":"sketch3d","name":"3DSketch1",
                         "params":{
                           "entities":[
                             {"id":"e1","type":"line","construction":false,
                              "p1":[0,0,0],"p2":[0,0,60]},
                             {"id":"e2","type":"arc","construction":false,
                              "p1":[0,0,60],"pm":[2.93,0,67.07],"p2":[10,0,70]}],
                           "constraints":[
                             {"id":"c1","type":"fix","refs":["e1.p1"]},
                             {"id":"c2","type":"along_axis","refs":["e1"],"axis":"z"},
                             {"id":"c3","type":"coincident","refs":["e1.p2","e2.p1"]}]}}}
                    )JSON");
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({s3op}));
                    s3Fid = (r.ok && r.id_map.is_object() && r.id_map.contains("$0"))
                                ? r.id_map["$0"].get<std::string>() : std::string("f1");
                    bool entered = st.sketch3dMode && st.sketch3dMode->enterForFeature(st, s3Fid);
                    st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                    st.pan[0] = st.pan[1] = st.pan[2] = 0.0f;
                    std::printf("sketch3d: feature %s (line+arc) entered=%d ok=%d status=%s\n",
                                s3Fid.c_str(), (int)entered, (int)r.ok,
                                st.sketch3dMode ? st.sketch3dMode->statusLine().c_str() : "?");
                    std::fflush(stdout);
                    s3Capture = s3SavePath + "/sketch3d_01_enter.png"; s3Settle = kSettle;
                    break;
                }
                case 1: {  // constrained drag of the arc end (transient) + edit_feature commit
                    nlohmann::json dres;
                    nlohmann::json dragP = {{"doc_id",st.docMirror.docId()},{"sketch",s3Fid},
                        {"handle","e2.p2"},{"xyz",nlohmann::json::array({18.0,6.0,72.0})}};
                    bool dok = st.worker.call("sketch3d_drag", dragP, dres, err) &&
                               dres.value("ok", false);
                    if (dok && dres.contains("entities")) {
                        st.docMirror.applyOps(st.worker, nlohmann::json::array({
                            {{"op","edit_feature"},{"id",s3Fid},
                             {"params",{{"entities",dres["entities"]}}}}}));
                    }
                    // re-enter refreshes the overlay from the committed doc
                    if (st.sketch3dMode) st.sketch3dMode->enterForFeature(st, s3Fid);
                    std::printf("sketch3d: drag e2.p2 ok=%d converged=%d; committed; status=%s\n",
                                (int)dok, (int)dres.value("converged", false),
                                st.sketch3dMode ? st.sketch3dMode->statusLine().c_str() : "?");
                    std::fflush(stdout);
                    s3Capture = s3SavePath + "/sketch3d_02_drag.png"; s3Settle = kSettle;
                    break;
                }
                default:
                    if (st.sketch3dMode) st.sketch3dMode->exitSketch(st, /*keep=*/true);
                    st.sketch3dShotDone = true;
                    std::printf("sketch3d: scenario complete\n"); std::fflush(stdout);
                    break;
            }
            if (s3Step <= 1) ++s3Step;
        }
    }

    // ---------- C20b assembly scenario (CHUDWARE_SHOT=asm) ----------
    // Mirrors engine/doc/test_worker_asm.py through the C++ DocMirror/AsmPanels stack:
    //   author + save base.step+ (40x40x10 plate, 8mm hole) and peg.step+ (r=3.9, h=20),
    //   new assembly, insert both, concentric (hole<->peg) + coincident ANTI (plate top<->peg
    //   bottom) -> peg seats at (0,0,15); screenshot the seated assembly with the Mates folder
    //   visible; explode factor 1.0 -> screenshot; interference window open -> screenshot.
    // Per-step captures reuse the m1/sketch backbuffer-write mechanism (asmCapture).
    if (st.wantAsm && st.workerStarted && !st.asmShotDone && st.frame >= 5) {
        auto* ap = st.asmPanels;
        if (!asmDirReady) {
            asmDirReady = true;
            const char* repo = std::getenv("CHUDWARE_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            asmSavePath = base + "/scratch/asm_shots";
            asmBasePath = asmSavePath + "/base.step+";
            asmPegPath  = asmSavePath + "/peg.step+";
            std::error_code ec; std::filesystem::create_directories(asmSavePath, ec);  // W1-B (item 12)
        }
        // Helper: author a part via raw worker calls (doc_new part -> apply_ops -> save -> close).
        auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
        auto makePart = [&](const std::string& name, const nlohmann::json& ops,
                            const std::string& path) -> bool {
            nlohmann::json res; std::string err;
            if (!st.worker.call("doc_new", {{"kind", "part"}, {"name", name}}, res, err)) return false;
            std::string did = res.value("doc_id", std::string());
            int rev = res.value("revision", 0);
            nlohmann::json ap2;
            if (!st.worker.call("doc_apply_ops",
                    {{"doc_id", did}, {"base_revision", rev}, {"ops", ops}}, ap2, err)) return false;
            nlohmann::json sv;
            if (!st.worker.call("doc_save", {{"doc_id", did}, {"path", path}}, sv, err)) return false;
            nlohmann::json cl;
            st.worker.call("doc_close", {{"doc_id", did}}, cl, err);
            return true;
        };
        const int kSettle = 4;
        if (asmSettle > 0) { --asmSettle; }
        else if (asmCapture.empty()) switch (asmStep) {  // only advance when no capture pending
            case 0: {  // author the two parts
                nlohmann::json baseOps = nlohmann::json::array();
                baseOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Plate"},{"suppressed",false},
                    {"params",{{"length",dim(40)},{"width",dim(40)},{"height",dim(10)},
                               {"centered",true}}},{"refs",nlohmann::json::object()}}}});
                baseOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","cylinder"},{"name","Hole"},{"suppressed",false},
                    {"params",{{"radius",dim(4)},{"height",dim(12)},{"centered",true},
                               {"mode","cut"}}},{"refs",nlohmann::json::object()}}}});
                nlohmann::json pegOps = nlohmann::json::array();
                pegOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","cylinder"},{"name","Peg"},{"suppressed",false},
                    {"params",{{"radius",dim(3.9)},{"height",dim(20)},{"centered",true}}},
                    {"refs",nlohmann::json::object()}}}});
                bool okB = makePart("base", baseOps, asmBasePath);
                bool okP = makePart("peg", pegOps, asmPegPath);
                std::printf("asm: parts authored base=%d peg=%d\n", (int)okB, (int)okP);
                std::fflush(stdout);
                asmSettle = 2; ++asmStep; break;
            }
            case 1: {  // new assembly + insert both (peg offset away from the hole)
                std::string err;
                if (!st.docMirror.newAssembly(st.worker, err)) {
                    std::printf("asm: new_assembly FAILED: %s\n", err.c_str()); std::fflush(stdout);
                    st.asmShotDone = true; break;
                }
                asmDoc = st.docMirror.docId();
                std::string aicm;
                ap->insertComponent(st, asmBasePath, nullptr, aicm);
                nlohmann::json off = nlohmann::json::array({1,0,0,30, 0,1,0,25, 0,0,1,18, 0,0,0,1});
                ap->insertComponent(st, asmPegPath, off, aicm);
                // identify the peg component id (k2 = second component)
                const auto& tr = st.docMirror.tree();
                if (tr.contains("components") && tr["components"].is_array() &&
                    tr["components"].size() >= 2)
                    asmK2 = tr["components"][1].value("id", std::string());
                std::printf("asm: assembly %s inserted base+peg (peg=%s)\n",
                            asmDoc.c_str(), asmK2.c_str());
                std::fflush(stdout);
                st.yaw = 28; st.pitch = 22; st.zoom = 1.0f;
                asmSettle = kSettle; ++asmStep; break;
            }
            case 2: {  // concentric + coincident(anti) mates -> peg seats at z=15
                // Resolve the mate selectors via the asm pick path (component-body ordinals).
                auto resolveAsm = [&](int bodyId, const char* kind, int topo,
                                      std::string& comp) -> nlohmann::json {
                    std::string err;
                    std::string s = st.docMirror.resolvePickAsm(st.worker, bodyId, kind, topo, comp, err);
                    if (s.empty()) return nlohmann::json();
                    try { return nlohmann::json::parse(s); } catch (...) { return nlohmann::json(); }
                };
                // Scan base (body 1) faces for the hole cylinder + the top plane (max z plane).
                std::string cBase, cPeg;
                nlohmann::json holeCyl, topPlate;
                for (int t = 1; t <= 30; ++t) {
                    std::string comp;
                    nlohmann::json s = resolveAsm(1, "face", t, comp);
                    if (!s.is_object()) break;
                    cBase = comp;
                    std::string surf = s.value("surf", std::string());
                    if (surf == "cylinder" && holeCyl.is_null()) holeCyl = s;
                    if (surf == "plane") {
                        double z = s.contains("centroid") ? s["centroid"][2].get<double>() : -1e9;
                        double bz = topPlate.is_object() && topPlate.contains("centroid")
                                    ? topPlate["centroid"][2].get<double>() : -1e9;
                        if (z > bz) topPlate = s;
                    }
                }
                // Scan peg (body 2) faces for its cylinder + the bottom plane (min z plane).
                nlohmann::json pegCyl, botPeg;
                for (int t = 1; t <= 30; ++t) {
                    std::string comp;
                    nlohmann::json s = resolveAsm(2, "face", t, comp);
                    if (!s.is_object()) break;
                    cPeg = comp;
                    std::string surf = s.value("surf", std::string());
                    if (surf == "cylinder" && pegCyl.is_null()) pegCyl = s;
                    if (surf == "plane") {
                        double z = s.contains("centroid") ? s["centroid"][2].get<double>() : 1e9;
                        double bz = botPeg.is_object() && botPeg.contains("centroid")
                                    ? botPeg["centroid"][2].get<double>() : 1e9;
                        if (z < bz) botPeg = s;
                    }
                }
                std::string aicm;
                if (holeCyl.is_object() && pegCyl.is_object()) {
                    nlohmann::json refs = nlohmann::json::array();
                    refs.push_back({{"component", cBase}, {"selector", holeCyl}});
                    refs.push_back({{"component", cPeg},  {"selector", pegCyl}});
                    ap->addMate(st, "concentric", "aligned", refs, aicm);
                }
                if (topPlate.is_object() && botPeg.is_object()) {
                    nlohmann::json refs = nlohmann::json::array();
                    refs.push_back({{"component", cBase}, {"selector", topPlate}});
                    refs.push_back({{"component", cPeg},  {"selector", botPeg}});
                    ap->addMate(st, "coincident", "anti", refs, aicm);
                }
                // Read the solved peg transform z from the tree.
                asmSolvedZ = -999.0;
                const auto& tr = st.docMirror.tree();
                if (tr.contains("components"))
                    for (const auto& c : tr["components"])
                        if (c.value("id", std::string()) == asmK2 &&
                            c.contains("transform") && c["transform"].is_array() &&
                            c["transform"].size() == 16)
                            asmSolvedZ = c["transform"][11].get<double>();
                // Open the Mate form so the screenshot shows the assembly Mate PM; the Mates
                // folder itself lives in the FeatureManager tree (visible alongside). Close the
                // auto-opened Insert window so the tree's Mates folder is unobscured.
                ap->closeInsert();
                ap->openMateForm();
                std::printf("asm: mates added; solved peg z=%.3f (expect 15)\n", asmSolvedZ);
                std::fflush(stdout);
                if (std::fabs(asmSolvedZ - 15.0) < 0.5)
                    std::printf("asm: PASS |z-15|<0.5\n");
                else
                    std::printf("asm: FAIL z=%.3f not within 0.5 of 15\n", asmSolvedZ);
                std::fflush(stdout);
                asmCapture = asmSavePath + "/asm_01_seated.png"; asmSettle = kSettle; ++asmStep; break;
            }
            case 3: {  // explode factor 1.0
                ap->closeMateForm();
                // Define an explode step for the peg (lift it +Z 40mm) so the slider has
                // something to act on, then activate the exploded view at factor 1.0.
                {
                    nlohmann::json step = {{"component", asmK2},
                        {"vector", nlohmann::json::array({0, 0, 1})}, {"distance", 40.0}};
                    nlohmann::json steps = nlohmann::json::array(); steps.push_back(step);
                    st.docMirror.applyOps(st.worker, nlohmann::json::array({
                        {{"op","asm_set_explode"},{"steps",steps}}}));
                }
                ap->handleCommand(st, "asm_explode");  // opens the Exploded View slider window
                ap->setExplode(st, true, 1.0f);
                std::printf("asm: explode factor=1.0 (peg +40mm)\n"); std::fflush(stdout);
                asmCapture = asmSavePath + "/asm_02_explode.png"; asmSettle = kSettle; ++asmStep; break;
            }
            case 4: {  // interference window
                ap->setExplode(st, false, 0.0f);  // collapse before checking real fit
                ap->closeExplode();               // dismiss the explode window so it isn't on top
                ap->openInterference(st);
                std::printf("asm: interference window open\n"); std::fflush(stdout);
                asmCapture = asmSavePath + "/asm_03_interference.png"; asmSettle = kSettle; ++asmStep; break;
            }
            default:
                st.asmShotDone = true;
                std::printf("asm: scenario complete (solved z=%.3f)\n", asmSolvedZ);
                std::fflush(stdout);
                break;
        }
    }

    // ---------- C19 FEA scenario (CHUDWARE_SHOT=fea) ----------
    // new part -> box 60x40x10 -> static study: fix the -X end face, the default 100 N -Z force on
    // the +X end, steel 1018, mesh density 0.2 -> run -> von Mises colormap. Same frame gate and
    // idle waits as simaudit's static states (newStudy / run refuse while an engine RPC is in
    // flight, which is how the old frame-0 sequence silently ended with no study). Prints
    // "fea: PASS ..." or "fea: FAILED [code] ..." and exits non-zero on failure.
    if (st.wantFea && st.workerStarted && !st.feaShotDone && st.frame >= 5) {
        StudyShot& F = g_feaShot;
        constexpr float kDensity = 0.2f;
        constexpr double kReadyTimeoutS = 30.0, kRunTimeoutS = 30.0, kResultTimeoutS = 90.0;
        if (st.simManager) st.simManager->tick(st);  // pump so completion lands while we wait
        if (feaSettle > 0) { --feaSettle; }
        else switch (feaStep) {
            case 0: {  // New part + box
                F.t0 = pipingNow();
                int rev = 0;
                const std::string why = studyShotBox(st, F, "Box1", 60.0, 40.0, 10.0, rev);
                if (!why.empty()) { studyShotFail(F, "fea", "box", why); feaStep = 99; break; }
                std::printf("fea: box 60x40x10 mm doc=%s rev=%d\n", F.docId.c_str(), rev);
                std::fflush(stdout);
                st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                F.stepStart = pipingNow();
                feaSettle = 3; ++feaStep; break;
            }
            case 1: {  // Static study + resolved fixture/load faces (-X end / +X end)
                if (!studyShotReady(st, F, "fea", kReadyTimeoutS)) {
                    if (F.failed) feaStep = 99;
                    break;
                }
                ui::SimManager& sm = *st.simManager;
                st.activeWorkspace = AppState::Workspace::Simulate;
                st.leftPaneTab = AppState::LP_Simulation;
                st.showRibbon = true;
                st.showSimulationTools = true;
                st.ribbonTab = kSaTabStatic;
                const int before = sm.studyCount();
                sm.handleCommand(st, "sim_new_static");
                if (sm.studyCount() != before + 1) {
                    studyShotFail(F, "fea", "study_not_created",
                                  "sim_new_static added no study (status: " + st.status + ")");
                    feaStep = 99; break;
                }
                sm.configureActive(st, kDensity, "steel_1018");
                // -X / +X end faces by centroid, injected exactly as a viewport click resolves them.
                struct Face { uint32_t tid = 0; nlohmann::json sel; double cx = 0.0; };
                Face lo, hi;
                lo.cx = 1e30; hi.cx = -1e30;
                for (uint32_t tid = 1; tid <= 12; ++tid) {
                    std::string se;
                    const std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                    if (s.empty()) continue;
                    nlohmann::json o = nlohmann::json::parse(s, nullptr, false);
                    if (!o.is_object() || !o.contains("centroid") || !o["centroid"].is_array() ||
                        o["centroid"].empty())
                        continue;
                    const double cx = o["centroid"][0].get<double>();
                    if (cx < lo.cx) lo = {tid, o, cx};
                    if (cx > hi.cx) hi = {tid, o, cx};
                }
                if (lo.tid == 0 || hi.tid == 0 || lo.tid == hi.tid) {
                    studyShotFail(F, "fea", "faces", "the box end faces did not resolve");
                    feaStep = 99; break;
                }
                auto pick = [&](const Face& f, const char* cmd) {
                    st.selection.clear();
                    core::SelItem it;
                    it.hit.body_id = 1;
                    it.hit.kind = render::PickKind::Face;
                    it.hit.topo_id = f.tid;
                    it.selector = f.sel.dump();
                    it.pending_resolve = false;
                    st.selection.mutableItems().push_back(it);
                    sm.handleCommand(st, cmd);
                    st.ribbonTab = kSaTabStatic;
                };
                pick(lo, "sim_fixture");
                pick(hi, "sim_load_force");
                st.selection.clear();
                std::printf("fea: static study, steel 1018, density %.1f, fixture x=%.1f, force x=%.1f\n",
                            kDensity, lo.cx, hi.cx);
                std::fflush(stdout);
                F.stepStart = pipingNow();
                F.lastTry = 0.0;
                feaSettle = 2; ++feaStep; break;
            }
            case 2: {  // Run (retried while the engine finishes the setup saves)
                if (st.docMirror.docId() != F.docId || !st.simManager) {
                    studyShotFail(F, "fea", "doc_switched", "the scenario's part is no longer active");
                    feaStep = 99; break;
                }
                ui::SimManager& sm = *st.simManager;
                if (!studyShotRun(st, F, "fea", kRunTimeoutS,
                                  [&] { return sm.runUi().running || st.colormapOn; })) {
                    if (F.failed) feaStep = 99;
                    break;
                }
                std::printf("fea: run started (%.1f s after start)\n", pipingNow() - F.t0);
                std::fflush(stdout);
                F.stepStart = pipingNow();
                F.idleSince = 0.0;
                ++feaStep; break;
            }
            case 3: {  // Wait for the von Mises colormap (the result lands on the Results node)
                const double now = pipingNow();
                if (st.colormapOn) {
                    std::printf("fea: PASS colormap=1 legend=%d solve=%.1f s total=%.1f s\n",
                                (int)st.legendOn, now - F.stepStart, now - F.t0);
                    std::fflush(stdout);
                    feaSettle = 8;  // let the legend + overlay settle before the capture
                    feaStep = 99; break;
                }
                const bool running = st.simManager && st.simManager->runUi().running;
                if (running) {
                    F.idleSince = 0.0;
                } else if (F.idleSince == 0.0) {
                    F.idleSince = now;  // finished: open Results like "View results" does
                    if (st.simManager) st.simManager->handleCommand(st, "sim_results");
                } else if (now - F.idleSince > 10.0) {
                    studyShotFail(F, "fea", "no_result",
                                  "the run ended without a result colormap (status: " + st.status + ")");
                    feaStep = 99; break;
                }
                if (now - F.stepStart > kResultTimeoutS) {
                    studyShotFail(F, "fea", "timeout",
                                  "no result colormap within " + std::to_string((int)kResultTimeoutS) +
                                      " s (status: " + st.status + ")");
                    feaStep = 99;
                }
                break;
            }
            default:
                st.feaShotDone = true;
                std::printf("fea: scenario complete (%s)\n", F.failed ? "FAILED" : "PASS");
                std::fflush(stdout);
                break;
        }
    }

    // ---------- T-FIN24 disclosure-matrix scenario (CHUDWARE_SHOT=simdisc_mech|simdisc_ecad) ----------
    // Proves the Simulate tab's progressive disclosure: mech-only -> no electronics study
    // kinds in the "+ New Study" picker; with a PCB tab open -> electronics simulation +
    // firmware emulation rows appear. The picker is held open for the capture via the
    // SimManager's scripted-shot hook.
    if (st.wantSimDisc && st.workerStarted && !st.simDiscShotDone) {
        if (st.simManager) st.simManager->tick(st);
        if (sdSettle > 0) { --sdSettle; }
        else switch (sdStep) {
            case 0: {  // (ecad variant) open a PCB tab first so the disclosure scan sees it
                if (st.simDiscKind == 1) {
                    nlohmann::json res; std::string err;
                    if (st.worker.call("ecad_new", {{"kind", "pcb"}}, res, err, 20000) &&
                        !res.contains("code")) {
                        st.addEcadTab("pcb", res.value("doc_id", std::string()),
                                      res.value("revision", 0), "PCB1");
                        std::printf("simdisc: PCB tab opened (%s)\n",
                                    res.value("doc_id", std::string()).c_str());
                    } else {
                        std::printf("simdisc: ecad_new failed: %s\n", err.c_str());
                    }
                    std::fflush(stdout);
                }
                sdSettle = 3; ++sdStep; break;
            }
            case 1: {  // new part + box so the mechanical kinds are unlocked
                std::string err;
                st.docMirror.newPart(st.worker, err);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",{{"value",60.0},{"expr",nullptr}}},
                        {"width", {{"value",40.0},{"expr",nullptr}}},
                        {"height",{{"value",10.0},{"expr",nullptr}}}},
                    },{"centered",true}}}});
                auto r = st.docMirror.applyOps(st.worker, ops);
                if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                std::printf("simdisc: part+box ok=%d\n", (int)r.ok);
                std::fflush(stdout);
                sdSettle = 6; ++sdStep; break;
            }
            case 2: {  // Simulate workspace + hold the study picker open for the capture
                st.activeWorkspace = AppState::Workspace::Simulate;
                st.leftPaneTab = AppState::LP_Simulation;
                if (st.simManager) st.simManager->shotForcePicker = true;
                sdSettle = 12; ++sdStep; break;
            }
            default:
                st.simDiscShotDone = true;
                std::printf("simdisc: scenario complete (kind=%s)\n",
                            st.simDiscKind == 1 ? "ecad" : "mech");
                std::fflush(stdout);
                break;
        }
    }

    // ---------- CAM native-viewport scenario (CHUDWARE_SHOT=cam) ----------
    // new part -> box 80x60x15 + 4 corner through-holes -> run the copilot CAM fast-path
    // (agent_run options.cam) -> adopt the built job exactly as the app drain does -> load
    // toolpaths + framed material removal at mid-cycle. The capture exercises the real
    // Manufacture workspace: native viewport geometry/HUD plus the integrated left controls.
    if (st.wantCam && st.workerStarted && !st.camShotDone && st.frame >= 5) {
        std::string err;
        st.docMirror.newPart(st.worker, err);
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op","add_feature"},{"feature",{
            {"type","box"},{"name","Plate"},{"params",{
                {"length",{{"value",80.0},{"expr",nullptr}}},
                {"width", {{"value",60.0},{"expr",nullptr}}},
                {"height",{{"value",15.0},{"expr",nullptr}}}}},{"centered",true}}}});
        nlohmann::json hole;
        hole["op"] = "add_feature";
        hole["feature"]["type"] = "hole";
        hole["feature"]["name"] = "Bores";
        hole["feature"]["params"]["style"] = "simple";
        hole["feature"]["params"]["standard"] = "custom";
        hole["feature"]["params"]["diameter"] = {{"value", 5.0}, {"expr", nullptr}};
        hole["feature"]["params"]["end"] = "through_all";
        hole["feature"]["params"]["positions"] = {{-30, -20}, {30, -20}, {30, 20}, {-30, 20}};
        hole["feature"]["refs"]["face"] = {{"of", "face"}, {"side", "top"}};
        ops.push_back(hole);
        auto r = st.docMirror.applyOps(st.worker, ops);
        std::printf("cam-shot: part ok=%d rev=%d aicm=%d\n", (int)r.ok, r.revision,
                    (int)!r.aicm_path.empty());
        std::fflush(stdout);
        if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        // Drive the same core recipe the copilot CAM fast-path calls (cam_program_from_part);
        // cam_* routes straight to the C++ core, so no agent provider is needed.
        nlohmann::json res; std::string e2;
        bool ok = st.worker.call("cam_program",
                       {{"doc_id", st.docMirror.docId()}, {"post", "fanuc"}}, res, e2);
        std::string camId = (ok && res.is_object()) ? res.value("cam_doc_id", std::string()) : std::string();
        std::printf("cam-shot: ok=%d err=%s engaged=%s cam_doc=%s ops=%d lines=%d\n",
                    (int)ok, e2.c_str(),
                    (res.is_object() ? res.value("engaged", std::string()) : std::string("?")).c_str(),
                    camId.c_str(),
                    (res.is_object() ? (int)res.value("op_count", 0) : -1),
                    (res.is_object() ? (int)res.value("line_count", 0) : -1));
        std::fflush(stdout);
        ui::camLoadJob(st, camId);                 // adopt like the GL-thread drain
        // Wave 3: drive a framed sim + adopt toolpaths synchronously so the headless capture
        // exercises the playhead path (frames -> remnant mesh -> HUD) without waiting on AsyncRpc.
        if (!camId.empty()) {
            nlohmann::json simRes; std::string e3;
            if (st.worker.call("cam_simulate",
                    {{"doc_id", camId}, {"include_field", true},
                     {"options", {{"frames", 8}}}}, simRes, e3))
                ui::camAdoptSimResult(simRes);
            nlohmann::json genRes; std::string e4;
            if (st.worker.call("cam_generate", {{"doc_id", camId}}, genRes, e4))
                ui::camAdoptToolpaths(genRes);
            ui::camDebugSetPlayhead(0.5f);
            std::printf("cam-shot: playhead sim=%d totalT=%.1fs\n", (int)!simRes.is_null(),
                        ui::camPlayTotalTime());
            std::fflush(stdout);
        }
        // Manufacture has no specialized preview/editor window: keep the native 3D viewport as
        // the full central body while the pinned Job Manager / NC Output surfaces own controls.
        st.showRibbon = true; st.ribbonTab = AppState::RT_Manufacture;
        st.activeWorkspace = AppState::Workspace::Manufacture;   // viewport playhead + HUD gate
        st.wantFocusViewport = 0;                  // let the CAM tab stay forward
        st.camShotDone = true;
    }

    // new part -> fuselage-ish bar 80x20x20 -> Flow study (water, 10 deg AoA, coarse) -> run ->
    // wait for the numeric result (and a contour if the selected solver provides one) -> capture.
    // The old frame-0 sequence never got a study (newStudy / run refuse while an engine RPC is in
    // flight), so it "timed out" without solving; the waits below mirror simaudit's. Solve cost:
    // the coarse preset is 48 cells along the longest domain axis (core/sim/flow_run.cpp); this
    // bar at 10 deg AoA spans ~412 x 198 x 185 mm, i.e. a 48 x 24 x 22 lattice (~25k cells) x 1500
    // steps ~= 3.8e7 site updates, a few seconds of D3Q19 even single-threaded. The 120 s cap is
    // headroom for export prep on a loaded machine, not solve time. Prints "flow: PASS ..." or
    // "flow: FAILED [code] ..." and exits non-zero on failure.
    if (st.wantFlow && st.workerStarted && !st.flowShotDone && st.frame >= 5) {
        StudyShot& F = g_flowShot;
        constexpr double kReadyTimeoutS = 30.0, kRunTimeoutS = 30.0, kResultTimeoutS = 120.0;
        if (st.simManager) st.simManager->tick(st);  // pump so completion lands while we wait
        if (flowSettle > 0) { --flowSettle; }
        else switch (flowStep) {
            case 0: {  // New part + a slender bar (stand-in fuselage/hull)
                F.t0 = pipingNow();
                int rev = 0;
                const std::string why = studyShotBox(st, F, "Hull", 80.0, 20.0, 20.0, rev);
                if (!why.empty()) { studyShotFail(F, "flow", "bar", why); flowStep = 99; break; }
                std::printf("flow: bar 80x20x20 mm doc=%s rev=%d\n", F.docId.c_str(), rev);
                std::fflush(stdout);
                st.yaw = 40; st.pitch = 22; st.zoom = 1.0f;
                F.stepStart = pipingNow();
                flowSettle = 3; ++flowStep; break;
            }
            case 1: {  // Flow study: water, 10 deg AoA, coarse preset
                if (!studyShotReady(st, F, "flow", kReadyTimeoutS)) {
                    if (F.failed) flowStep = 99;
                    break;
                }
                ui::SimManager& sm = *st.simManager;
                st.activeWorkspace = AppState::Workspace::Simulate;
                st.leftPaneTab = AppState::LP_Simulation;
                st.showRibbon = true;
                st.showSimulationTools = true;
                st.ribbonTab = kSaTabFlow;
                const int before = sm.studyCount();
                sm.handleCommand(st, "sim_new_flow");
                st.ribbonTab = kSaTabFlow;
                if (sm.studyCount() != before + 1) {
                    studyShotFail(F, "flow", "study_not_created",
                                  "sim_new_flow added no study (status: " + st.status + ")");
                    flowStep = 99; break;
                }
                sm.configureActiveFlow(st, /*fluid=water*/1, /*aoa*/10.0f, /*sideslip*/0.0f,
                                       /*preset=coarse*/0);
                std::printf("flow: study configured (water, AoA 10, coarse)\n");
                std::fflush(stdout);
                F.stepStart = pipingNow();
                F.lastTry = 0.0;
                flowSettle = 2; ++flowStep; break;
            }
            case 2: {  // Run (retried while the engine finishes the setup save)
                if (st.docMirror.docId() != F.docId || !st.simManager) {
                    studyShotFail(F, "flow", "doc_switched", "the scenario's part is no longer active");
                    flowStep = 99; break;
                }
                ui::SimManager& sm = *st.simManager;
                if (!studyShotRun(st, F, "flow", kRunTimeoutS,
                                  [&] { return sm.runUi().running || sm.flowHasResult(); })) {
                    if (F.failed) flowStep = 99;
                    break;
                }
                std::printf("flow: run started (%.1f s after start)\n", pipingNow() - F.t0);
                std::fflush(stdout);
                F.stepStart = pipingNow();
                F.idleSince = 0.0;
                ++flowStep; break;
            }
            case 3: {  // Wait for the numeric result (the LBM route may carry no contour overlay)
                const double now = pipingNow();
                ui::SimManager* sm = st.simManager;
                if (sm && sm->flowHasResult()) {
                    std::printf("flow: PASS numeric=1 colormap=%d legend=%d solve=%.1f s total=%.1f s\n",
                                (int)st.colormapOn, (int)st.legendOn, now - F.stepStart, now - F.t0);
                    std::fflush(stdout);
                    flowSettle = 8;  // let the Results pane settle before the capture
                    flowStep = 99; break;
                }
                if (sm && sm->runUi().running) {
                    F.idleSince = 0.0;
                } else if (F.idleSince == 0.0) {
                    F.idleSince = now;
                } else if (now - F.idleSince > 10.0) {
                    studyShotFail(F, "flow", "no_result",
                                  "the run ended without a result (status: " + st.status + ")");
                    flowStep = 99; break;
                }
                if (now - F.stepStart > kResultTimeoutS) {
                    studyShotFail(F, "flow", "timeout",
                                  "numeric result did not arrive within " +
                                      std::to_string((int)kResultTimeoutS) + " s (status: " + st.status + ")");
                    flowStep = 99;
                }
                break;
            }
            default:
                st.flowShotDone = true;
                std::printf("flow: scenario complete (%s)\n", F.failed ? "FAILED" : "PASS");
                std::fflush(stdout);
                break;
        }
    }

    // ---------- C19 evalmass scenario (CHUDWARE_SHOT=evalmass) ----------
    if (st.wantEvalMass && st.workerStarted && !st.evalMassShotDone) {
        if (evalSettle > 0) { --evalSettle; }
        else switch (evalStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",{{"value",50.0},{"expr",nullptr}}},
                        {"width", {{"value",30.0},{"expr",nullptr}}},
                        {"height",{{"value",20.0},{"expr",nullptr}}}},
                    },{"centered",true}}}});
                auto r = st.docMirror.applyOps(st.worker, ops);
                if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                std::printf("evalmass: box ok=%d\n", (int)r.ok); std::fflush(stdout);
                evalSettle = 3; ++evalStep; break;
            }
            case 1: {
                st.evaluateDialogs->handleCommand(st, "eval_mass_props");
                std::printf("evalmass: mass properties window opened\n"); std::fflush(stdout);
                // T-G14: the eval runs async on the dispatch thread; give it frames to drain so the
                // result (mass/volume/CoM/inertia rows) populates before capture. Without this the
                // window is captured while still "Computing" (or 1 frame after), so it never shows
                // the auto-size-to-content behaviour the T-G14 fix restores.
                evalSettle = 40; ++evalStep; break;
            }
            default:
                st.evalMassShotDone = true;
                std::printf("evalmass: scenario complete\n"); std::fflush(stdout);
                break;
        }
    }


    // ---------- multi-document scenario (CHUDWARE_SHOT=multidoc) ----------
    runMultidoc(st);

    // ---------- tab-switch perf scenario (CHUDWARE_SHOT=tabperf) ----------
    runTabPerf(st);

    // ---------- W17 engineering-drawing scenario (CHUDWARE_SHOT=drawing) ----------
    runDrawing(st);

    // ---------- appearance scenario (CHUDWARE_SHOT=appearance) ----------
    runAppearance(st);

    // ---------- first-run onboarding scenario (CHUDWARE_SHOT=onboarding) ----------
    runOnboarding(st);

    // ---------- T-G15 icon pipeline (CHUDWARE_SHOT=iconpipe) ----------
    runIconPipe(st);

    // ---------- RE-28 3DGS splat scenario (CHUDWARE_SHOT=reveng_splat) ----------
    runRevengSplat(st);

    // ---------- workspaces scenario (CHUDWARE_SHOT=workspaces) ----------
    runWorkspaces(st);

    // ---------- motion scenario (CHUDWARE_SHOT=motion) ----------
    runMotion(st);

    // ---------- topology optimization scenario (CHUDWARE_SHOT=topology) ----------
    runTopology(st);

    // ---------- piping / industrial scenario (CHUDWARE_SHOT=piping) ----------
    pipingShotTick(st);
    pipeDrawTick(st);

    // ---------- hydraulic circuit scenario (CHUDWARE_SHOT=hydraulics) ----------
    hydraulicsShotTick(st);

    // ---------- saved-demo review (CHUDWARE_SHOT=demoreview) ----------
    demoReviewTick(st);

    // ---------- CAD-file open smoke (CHUDWARE_SHOT=dwgopen) ----------
    dwgOpenTick(st);

    // ---------- root-scoped chat sessions (CHUDWARE_SHOT=projectchat) ----------
    projectChatTick(st);

    // ---------- mid-run message delivery captions (CHUDWARE_SHOT=steerstatus) ----------
    steerStatusTick(st);
    chatInlineTick(st);
    copilotUiTick(st);
#ifndef __EMSCRIPTEN__
    delayedAccountTick(st);
#endif

    // ---------- live document editing lock / keep-exit / takeover (CHUDWARE_SHOT=agentlock) ----------
    agentLockTick(st);
    featureStateTick(st);

    // ---------- simulation UI audit (CHUDWARE_SHOT=simaudit) ----------
    simAuditTick(st);

    // ---------- real interactive simulation setup (CHUDWARE_SHOT=siminteractive) ----------
    simInteractiveTick(st);
}

// First-run onboarding visual proof. Each step is forced through the public scenario seam, so a
// developer's existing onboarding.completed_version can never turn this into a false-positive
// capture of the normal workspace.
void ScenarioRunner::runOnboarding(AppState& st) {
    if (!obInit) {
        obInit = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        obWant = st.shot && mode && std::strcmp(mode, "onboarding") == 0;
    }
    if (!obWant || obDone || st.frame < 3) return;
    if (!obDirReady) {
        obDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        const std::string base = repo ? repo : ".";
        obSavePath = base + "/scratch/onboarding_shots";
        std::error_code ec;
        std::filesystem::create_directories(obSavePath, ec);
    }
    if (!obCapture.empty()) return;
    if (obSettle > 0) {
        // Reassert while settling. The first popup submission can coincide with startup popup
        // bookkeeping; repeatedly forcing the same step guarantees capture never races that frame.
        forceOnboardingForScenario(std::clamp((obPhase - 1) / 2, 0, 3));
        --obSettle;
        return;
    }

    static const char* kNames[] = {
        "onboarding_01_account.png",
        "onboarding_02_power.png",
        "onboarding_03_test.png",
        "onboarding_04_workspace.png",
    };
    const int step = obPhase / 2;
    if (step < 4) {
        if ((obPhase % 2) == 0) {
            forceOnboardingForScenario(step);
            obSettle = 8;
        } else {
            obCapture = obSavePath + "/" + kNames[step];
        }
        ++obPhase;
        return;
    }

    if (const char* verify = std::getenv("CHUDWARE_ONBOARDING_VERIFY_MERGE");
        verify && verify[0] && verify[0] != '0') {
        const bool ok = persistOnboardingCompletion();
        std::printf("onboarding: merge persistence %s via %s\n",
                    ok ? "PASS" : "FAILED", configPath().c_str());
        std::fflush(stdout);
    }
    obDone = true;
    std::printf("onboarding: scenario complete\n");
    std::fflush(stdout);
}

// T-G15 icon pipeline (CHUDWARE_SHOT=iconpipe). Recipe-driven clean-viewport icon generator: reads a
// JSON recipe list from CHUDWARE_ICON_RECIPES { out_dir?, px?, recipes:[{name,yaw,pitch,ops:[...]}] }
// and, one recipe per pass, builds the demo part (newPart + applyOps — the SAME engine contract the
// other scenarios use) then renders it through the renderer's transparent thumbnail path (no chrome,
// no gizmo, no datum planes, alpha=0 background) at the recipe camera, writing out_dir/<name>.png.
// The capture is a thumbnail-FBO readback (NOT the backbuffer), so it is independent of window size
// and free of UI. Doubles as a smoke test that each tool's op actually runs on the engine.
void ScenarioRunner::runIconPipe(AppState& st) {
    if (!ipInit) {
        ipInit = true;
        const char* sm = std::getenv("CHUDWARE_SHOT");
        ipWant = st.wantIconPipe && sm && std::strcmp(sm, "iconpipe") == 0;
        if (ipWant) {
            const char* repo = std::getenv("CHUDWARE_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            ipOutDir = base + "/scratch/icon_render";
            const char* rp = std::getenv("CHUDWARE_ICON_RECIPES");
            if (rp) {
                try {
                    std::ifstream f(rp);
                    nlohmann::json j; f >> j;
                    if (j.contains("out_dir") && j["out_dir"].is_string())
                        ipOutDir = j["out_dir"].get<std::string>();
                    if (j.contains("px") && j["px"].is_number())
                        ipPx = j["px"].get<int>();
                    if (j.contains("recipes") && j["recipes"].is_array())
                        for (auto& r : j["recipes"]) ipRecipes.push_back(r);
                } catch (const std::exception& e) {
                    std::printf("iconpipe: FAILED to parse recipes '%s': %s\n", rp, e.what());
                    std::fflush(stdout);
                }
            } else {
                std::printf("iconpipe: CHUDWARE_ICON_RECIPES not set — nothing to render\n");
                std::fflush(stdout);
            }
            std::error_code ec; std::filesystem::create_directories(ipOutDir, ec);
            std::printf("iconpipe: %zu recipes px=%d out=%s\n", ipRecipes.size(), ipPx,
                        ipOutDir.c_str());
            std::fflush(stdout);
        }
    }
    if (!(ipWant && st.workerStarted && !st.iconPipeShotDone)) return;

    // Drain EVERY recipe in this single tick. Everything on the recipe path is synchronous on this
    // thread (applyOps -> in-process libchudware; loadDocMesh -> AICM parse + setMeshes GL upload;
    // renderThumbnail -> immediate FBO draw; readThumbnailRGBA -> glReadPixels), so no settle
    // frames or per-frame stepping are needed. This matters headless: full GUI frames are
    // pathologically slow on an unattended desktop (the compositor can starve a hidden/occluded
    // window's presents for ~a minute per swap), so one-tick draining is the difference between
    // seconds and half an hour. The other scenarios step per-frame because they capture the
    // BACKBUFFER (needs presented frames); this one never does.
    for (ipIndex = 0; ipIndex < (int)ipRecipes.size(); ++ipIndex) {
        const nlohmann::json& r = ipRecipes[ipIndex];
        const std::string name = r.value("name", std::string("icon_") + std::to_string(ipIndex));

        // Build the demo part for this recipe from its ordered "steps". Each step is either
        // a raw op (applied verbatim) or a {feature, selrefs} pair whose edge/face references are
        // resolved at runtime from the built geometry (dress-up ops like fillet/chamfer/shell need
        // real topo picks — a static recipe can't hardcode them). Reuses the m1/multidoc resolvePick
        // selector patterns so the recipe table stays pure data (owned by scripts/icon_pipeline.py).
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) {
            std::printf("iconpipe[%s]: new_part FAILED: %s\n", name.c_str(), err.c_str());
            std::fflush(stdout);
            continue;  // skip this recipe, keep going
        }
        // Resolve a named selector to a JSON array of pick objects (edges/faces).
        auto resolveSel = [&](const std::string& sel, int n) -> nlohmann::json {
            nlohmann::json out = nlohmann::json::array();
            if (sel == "vertical_edges" || sel == "all_edges") {
                for (int tid = 1; tid <= 12 && (int)out.size() < n; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, se);
                    if (s.empty()) continue;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        if (sel == "all_edges") { out.push_back(o); continue; }
                        if (o.contains("axis") && o["axis"].is_array() && o["axis"].size() == 3 &&
                            std::fabs(o["axis"][2].get<double>()) > 0.9)
                            out.push_back(o);
                    } catch (...) {}
                }
            } else if (sel == "top_face") {  // face with the highest centroid Z (m1's topFace)
                nlohmann::json best; double bestZ = -1e30;
                for (int tid = 1; tid <= 40; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                    if (s.empty()) break;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        if (!o.contains("centroid")) continue;
                        double z = o["centroid"][2].get<double>();
                        if (z > bestZ) { bestZ = z; best = o; }
                    } catch (...) {}
                }
                if (best.is_object()) out.push_back(best);
            }
            return out;
        };
        const nlohmann::json steps = r.value("steps", nlohmann::json::array());
        bool any = false;
        for (const auto& step : steps) {
            nlohmann::json feat = step.value("feature", nlohmann::json::object());
            nlohmann::json ops = nlohmann::json::array();
            if (feat.is_object() && !feat.empty()) {
                if (step.contains("selrefs") && step["selrefs"].is_object()) {
                    nlohmann::json refs = feat.value("refs", nlohmann::json::object());
                    for (auto it = step["selrefs"].begin(); it != step["selrefs"].end(); ++it) {
                        std::string sel = it.value().value("select", std::string());
                        int nn = it.value().value("n", 64);
                        refs[it.key()] = resolveSel(sel, nn);
                    }
                    feat["refs"] = refs;
                }
                ops.push_back({{"op", "add_feature"}, {"feature", feat}});
            } else if (step.contains("op")) {
                ops.push_back(step);
            } else {
                continue;
            }
            auto resp = st.docMirror.applyOps(st.worker, ops);
            any = true;
            if (resp.ok && !resp.aicm_path.empty()) { std::string le; st.loadDocMesh(resp.aicm_path, le); }
            std::printf("iconpipe[%s]: step '%s' ok=%d rev=%d\n", name.c_str(),
                        feat.value("type", step.value("op", std::string("?"))).c_str(),
                        (int)resp.ok, resp.revision);
            std::fflush(stdout);
        }
        if (!any) {
            std::printf("iconpipe[%s]: no build steps\n", name.c_str()); std::fflush(stdout);
            continue;
        }

        // Render the transparent thumbnail and write the PNG (same tick — see drain note above).
        const float yaw   = r.value("yaw", 35.0f);
        const float pitch = r.value("pitch", 28.0f);
        if (st.renderer) {
            st.renderer->renderThumbnail(yaw, pitch, ipPx, /*transparentBg=*/true);
            std::vector<unsigned char> rgba = st.renderer->readThumbnailRGBA(ipPx);
            // Opacity sanity: count non-transparent pixels so an empty render shows in the log.
            size_t opaque = 0; for (size_t i = 3; i < rgba.size(); i += 4) if (rgba[i] > 8) ++opaque;
            std::string out = ipOutDir + "/" + name + ".png";
            stbi_write_png(out.c_str(), ipPx, ipPx, 4, rgba.data(), ipPx * 4);
            std::printf("iconpipe[%s]: wrote %s (%dx%d, %zu/%d opaque px)\n", name.c_str(),
                        out.c_str(), ipPx, ipPx, opaque, ipPx * ipPx);
            std::fflush(stdout);
        }
    }

    st.iconPipeShotDone = true;  // frees the frame loop to exit (see app.cpp shotFrame gate)
    std::printf("iconpipe: complete — %d icon(s) written to %s\n", ipIndex, ipOutDir.c_str());
    std::fflush(stdout);
}

// Out-of-line ctor + dtor (rsRenderer is a unique_ptr to render::SplatRenderer, complete only here).
ScenarioRunner::ScenarioRunner() = default;
ScenarioRunner::~ScenarioRunner() = default;

// Shot-only native GL proof: a real PLY override or a valid parser fixture, two
// camera directions, and an isolated rotated anisotropic covariance probe.
// The private FBO avoids the frame loop's subsequent backbuffer clear.
void ScenarioRunner::runRevengSplat(AppState& st) {
    if (!rsInit) {
        rsInit = true;
        const char* sm = std::getenv("CHUDWARE_SHOT");
        rsWant = st.shot && sm && std::strcmp(sm, "reveng_splat") == 0;
    }
    if (!(rsWant && st.workerStarted && !rsDone && st.frame >= 5)) return;

    const char* repo = std::getenv("CHUDWARE_REPO");
    std::string base = repo ? std::string(repo) : std::string(".");
    rsSavePath = base + "/scratch/reveng_splat_shots";
    std::error_code ec;
    std::filesystem::create_directories(rsSavePath, ec);
    nlohmann::json report = {{"ok", false}, {"views", nlohmann::json::array()},
                             {"gl_errors", nlohmann::json::array()}};
    auto glErrors = [&](const char* phase) {
        for (int i = 0; i < 32; ++i) {
            GLenum error = glGetError();
            if (error == GL_NO_ERROR) break;
            report["gl_errors"].push_back({{"phase", phase}, {"code", error}});
        }
    };
    glErrors("entry");
    rsRenderer = std::make_unique<render::SplatRenderer>();
    std::string err;
    const char* overridePath = std::getenv("CHUDWARE_SPLAT_SMOKE_PATH");
    const bool realInput = overridePath && overridePath[0];
    const std::string plyPath = realInput ? std::string(overridePath)
                                        : base + "/host/tests/fixtures/splat_tiny.ply";
    report["source"] = plyPath;
    rsLoaded = rsRenderer->load(plyPath, err);
    Vec3 minimum, maximum;
    const bool bounded = rsLoaded && rsRenderer->bounds(minimum, maximum);
    report["loaded"] = rsLoaded;
    report["count"] = rsRenderer->count();
    report["load_error"] = err;
    report["bounds"] = {minimum.x, minimum.y, minimum.z, maximum.x, maximum.y, maximum.z};
    bool passed = !ec && bounded;
    if (ec) report["directory_error"] = ec.message();

    constexpr int W = 640, H = 480;
    GLint oldDrawFbo = 0, oldReadFbo = 0, oldViewport[4], oldScissor[4], oldPack = 4;
    GLfloat oldClearColor[4];
    GLboolean oldDepthMask = GL_TRUE, oldColorMask[4];
    const GLboolean oldScissorEnabled = glIsEnabled(GL_SCISSOR_TEST);
    const GLboolean oldDepthEnabled = glIsEnabled(GL_DEPTH_TEST);
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &oldDrawFbo);
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &oldReadFbo);
    glGetIntegerv(GL_VIEWPORT, oldViewport);
    glGetIntegerv(GL_SCISSOR_BOX, oldScissor);
    glGetIntegerv(GL_PACK_ALIGNMENT, &oldPack);
    glGetFloatv(GL_COLOR_CLEAR_VALUE, oldClearColor);
    glGetBooleanv(GL_DEPTH_WRITEMASK, &oldDepthMask);
    glGetBooleanv(GL_COLOR_WRITEMASK, oldColorMask);
    GLuint fbo = 0, colorTex = 0, depthRbo = 0;
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glGenTextures(1, &colorTex);
    glBindTexture(GL_TEXTURE_2D, colorTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex, 0);
    glGenRenderbuffers(1, &depthRbo);
    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, W, H);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, depthRbo);
    const GLenum fbStatus = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    report["fbo_status"] = fbStatus;
    passed = passed && fbStatus == GL_FRAMEBUFFER_COMPLETE;
    glErrors("fbo");

    auto camera = [&](Vec3 center, float radius, Vec3 direction) {
        Renderer::CameraXform cam;
        cam.center = center;
        cam.radius = radius;
        cam.dist = radius * 4.0f;
        cam.dir = normalize(direction);
        cam.eye = center + cam.dir * cam.dist;
        cam.right = normalize(cross(Vec3{0, 1, 0}, cam.dir));
        cam.up = cross(cam.dir, cam.right);
        cam.view = lookAt(cam.eye, center, cam.up);
        const float fov = Renderer::kFovDeg * 3.14159265f / 180.0f;
        cam.proj = perspective(fov, float(W) / H, radius * 0.01f, radius * 12.0f);
        cam.mvp = mul(cam.proj, cam.view);
        cam.halfHeight = cam.dist * std::tan(fov * 0.5f);
        cam.orthographic = false;
        cam.ssWidth = cam.vpWidth = W;
        cam.ssHeight = cam.vpHeight = H;
        return cam;
    };
    std::vector<unsigned char> rgba(W * H * 4), flip(W * H * 4);
    auto renderView = [&](render::SplatRenderer& renderer, const Renderer::CameraXform& cam,
                          const char* name, bool probe, bool expectEmpty = false) {
        glViewport(0, 0, W, H);
        glDisable(GL_SCISSOR_TEST);
        glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
        glDepthMask(GL_TRUE);
        const GLfloat clearDepth = 1.0f;
        glClearBufferfv(GL_DEPTH, 0, &clearDepth);
        // RGB zero is intentional: neither opaque clear alpha nor stale depth
        // can count as rendered coverage.
        glClearColor(0, 0, 0, 0);
        glClear(GL_COLOR_BUFFER_BIT);
        renderer.draw(cam);
        glReadBuffer(GL_COLOR_ATTACHMENT0);
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
        glErrors(name);
        size_t covered = 0, colored = 0, colorBins = 0;
        bool bins[4096] = {};
        double weight = 0, mx = 0, my = 0, xx = 0, xy = 0, yy = 0;
        for (int y = 0; y < H; ++y) {
            for (int x = 0; x < W; ++x) {
                const unsigned char* p = &rgba[(size_t(y) * W + x) * 4];
                const int high = std::max({int(p[0]), int(p[1]), int(p[2])});
                const int low = std::min({int(p[0]), int(p[1]), int(p[2])});
                if (high <= 8) continue;
                ++covered;
                if (high - low > 8) ++colored;
                const int bin = (p[0] >> 4) * 256 + (p[1] >> 4) * 16 + (p[2] >> 4);
                if (!bins[bin]) { bins[bin] = true; ++colorBins; }
                const double w = double(p[0]) + p[1] + p[2];
                weight += w;
                mx += w * x; my += w * y;
                xx += w * x * x; xy += w * x * y; yy += w * y * y;
            }
        }
        double ratio = 0, correlation = 0;
        if (weight > 0) {
            mx /= weight; my /= weight;
            xx = std::max(0.0, xx / weight - mx * mx);
            xy = xy / weight - mx * my;
            yy = std::max(0.0, yy / weight - my * my);
            const double discriminant = std::sqrt((xx - yy) * (xx - yy) + 4 * xy * xy);
            ratio = std::sqrt((xx + yy + discriminant) /
                              std::max(1e-9, xx + yy - discriminant));
            correlation = xy / std::sqrt(std::max(1e-9, xx * yy));
        }
        bool viewOk = expectEmpty ? covered == 0 : covered >= 16 && colorBins >= 3;
        if (probe) viewOk = viewOk && colored >= 32 && ratio > 2.0 &&
                            std::abs(correlation) > 0.35;
        for (int y = 0; y < H; ++y)
            std::memcpy(&flip[size_t(H - 1 - y) * W * 4],
                        &rgba[size_t(y) * W * 4], size_t(W) * 4);
        const std::string path = rsSavePath + "/" + name + ".png";
        const bool pngOk = stbi_write_png(path.c_str(), W, H, 4, flip.data(), W * 4) != 0;
        report["views"].push_back({{"name", name}, {"ok", viewOk && pngOk},
            {"rgb_coverage", covered}, {"colored_pixels", colored}, {"rgb_bins", colorBins},
            {"axis_ratio", ratio}, {"xy_correlation", correlation}, {"png", path},
            {"png_written", pngOk}, {"eye", {cam.eye.x, cam.eye.y, cam.eye.z}}});
        passed = passed && viewOk && pngOk;
    };
    if (fbStatus == GL_FRAMEBUFFER_COMPLETE && bounded) {
        const Vec3 center = (minimum + maximum) * 0.5f;
        const float radius = std::max(0.001f, length(maximum - minimum) * 0.5f);
        renderView(*rsRenderer, camera(center, radius, {0.5f, 0.3f, 1.0f}),
                   "reveng_splat_01", false);
        renderView(*rsRenderer, camera(center, radius, {-1.0f, 0.2f, 0.4f}),
                   "reveng_splat_02", false);

        render::SplatRenderer probeRenderer;
        render::SplatCloud probeCloud;
        render::Gaussian probe;
        probe.scale = {0.7f, 0.1f, 0.05f};
        probe.rot[0] = 0.9659258f; probe.rot[3] = 0.2588190f;
        probe.color[0] = 0.9f; probe.color[1] = 0.2f; probe.color[2] = 0.05f;
        probe.opacity = 0.95f;
        probeCloud.gaussians.push_back(probe);
        probeRenderer.setCloud(std::move(probeCloud));
        renderView(probeRenderer, camera({}, 2.0f, {0, 0, 1}),
                   "reveng_splat_anisotropy", true);

        // A far-offscreen depth-elongated Gaussian must not project across the
        // image through an unbounded perspective Jacobian.
        render::SplatCloud offscreenCloud;
        render::Gaussian offscreen;
        offscreen.pos = {80.0f, 0.0f, 0.0f};
        offscreen.scale = {0.01f, 0.1f, 8.0f};
        offscreen.rot[0] = 1.0f;
        offscreen.color[0] = 0.1f; offscreen.color[1] = 0.9f; offscreen.color[2] = 0.1f;
        offscreen.opacity = 0.95f;
        offscreenCloud.gaussians.push_back(offscreen);
        probeRenderer.setCloud(std::move(offscreenCloud));
        renderView(probeRenderer, camera({}, 2.0f, {0, 0, 1}),
                   "reveng_splat_offscreen", false, true);
    }

    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, oldDrawFbo);
    glBindFramebuffer(GL_READ_FRAMEBUFFER, oldReadFbo);
    glDeleteFramebuffers(1, &fbo);
    glDeleteTextures(1, &colorTex);
    glDeleteRenderbuffers(1, &depthRbo);
    glViewport(oldViewport[0], oldViewport[1], oldViewport[2], oldViewport[3]);
    glScissor(oldScissor[0], oldScissor[1], oldScissor[2], oldScissor[3]);
    if (oldScissorEnabled) glEnable(GL_SCISSOR_TEST); else glDisable(GL_SCISSOR_TEST);
    if (oldDepthEnabled) glEnable(GL_DEPTH_TEST); else glDisable(GL_DEPTH_TEST);
    glDepthMask(oldDepthMask);
    glColorMask(oldColorMask[0], oldColorMask[1], oldColorMask[2], oldColorMask[3]);
    glClearColor(oldClearColor[0], oldClearColor[1], oldClearColor[2], oldClearColor[3]);
    glPixelStorei(GL_PACK_ALIGNMENT, oldPack);
    glErrors("restore");
    passed = passed && report["gl_errors"].empty();
    report["ok"] = passed;
    const std::string diagnostics = report.dump(2);
    std::ofstream out(rsSavePath + "/diagnostics.json", std::ios::trunc);
    out << diagnostics << '\n';
    out.close();
    const bool diagnosticsWritten = bool(out);
    std::printf("reveng_splat: %s\n", diagnostics.c_str());
    std::fflush(stdout);
    rsCapture = rsSavePath + "/reveng_splat_01.png";
    rsDone = true;
    // A shot is an executable smoke, not a screenshot that silently succeeds
    // after a loader/shader/FBO/readback failure.
    if (!passed || !diagnosticsWritten) {
        std::fflush(nullptr);
        std::_Exit(EXIT_FAILURE);
    }
}

// Appearance scenario (CHUDWARE_SHOT=appearance). Same frame-driven step machine + kSettle
// settling + per-step backbuffer capture (apCapture) as evalmass/m1. Flow: new part + box
// (a modeled part so the Appearance tab's live glass preview frosts real geometry) -> open the
// Settings modal jumped to the Appearance tab (index 0) -> settle -> capture. Unlike the older
// scenarios this one has no AppState want-flag: the runner reads CHUDWARE_SHOT itself (gated on
// st.shot so a stray env var in an interactive run can't hijack the frame loop).
void ScenarioRunner::runAppearance(AppState& st) {
    if (!apInit) {
        apInit = true;
        const char* sm = std::getenv("CHUDWARE_SHOT");
        apWant = st.shot && sm && std::strcmp(sm, "appearance") == 0;
    }
    if (!(apWant && st.workerStarted && !apDone && st.frame >= 5)) return;
    if (!apDirReady) {
        apDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        apSavePath = base + "/scratch/appearance_shots";
        std::error_code ec; std::filesystem::create_directories(apSavePath, ec);
    }
    const int kSettle = 4;
    if (apSettle > 0) { --apSettle; return; }
    if (!apCapture.empty()) return;  // only advance when no capture is pending
    std::string err;
    switch (apStep) {
        case 0: {  // modeled part: box 60x40x10 (mirrors evalmass's single-feature build)
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("appearance: new_part FAILED: %s\n", err.c_str());
                std::fflush(stdout);
                apDone = true; break;
            }
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Box1"},{"params",{
                    {"length",{{"value",60.0},{"expr",nullptr}}},
                    {"width", {{"value",40.0},{"expr",nullptr}}},
                    {"height",{{"value",10.0},{"expr",nullptr}}}},
                },{"centered",true}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
            std::printf("appearance: box ok=%d rev=%d\n", (int)r.ok, r.revision);
            std::fflush(stdout);
            apSettle = kSettle; ++apStep; break;
        }
        case 1: {  // open Settings, jumped to the Appearance tab (index 0) by default.
            // CHUDWARE_SHOT_SETTINGS_TAB overrides the target index so this scenario doubles as
            // the capture path for any Settings tab (including the retired indices, which the
            // modal folds onto AI + the sub-tab that absorbed them).
            const char* tabEnv = std::getenv("CHUDWARE_SHOT_SETTINGS_TAB");
            int tab = 0;
            if (tabEnv && tabEnv[0]) {
                char* end = nullptr;
                long v = std::strtol(tabEnv, &end, 10);
                if (end != tabEnv && v >= 0 && v < 32) tab = (int)v;
            }
            st.openSettings = true;
            st.openSettingsTab = tab;
            std::printf("appearance: settings modal opened on tab %d\n", tab);
            std::fflush(stdout);
            apSettle = kSettle * 2; ++apStep; break;  // let the modal + glass preview settle
        }
        case 2: {  // capture the modal over the modeled part
            apCapture = apSavePath + "/appearance_01_settings.png";
            apSettle = kSettle; ++apStep; break;
        }
        default:
            apDone = true;
            std::printf("appearance: scenario complete\n");
            std::fflush(stdout);
            break;
    }
}

// Multi-document mid-edit verification (CHUDWARE_SHOT=multidoc). Same frame-driven step machine +
// kSettle settling + per-step backbuffer capture (mdCapture) as m1. The flow:
//   A: new Part A -> box + fillet (m1's ops) -> record A's tab index + doc id.
//   B (mid-edit on A): seed a viewport selection + hide a body, then open a feature-edit PM form
//      (openForCommand "fillet") WITHOUT committing. Capture multidoc_01_A_editing.png.
//   C: new Part B (parks A's session, re-seats subsystems to B) -> assert B's PM is FRESH (inactive)
//      -> box on B -> measure on B (eval_measure). Capture multidoc_02_B.png. Record B's index/id.
//   D: switch back to A -> assert A's PM form active, mirror re-pointed to A, hidden-body mask +
//      selection preserved. Capture multidoc_03_A_restored.png.
//   E: switch to B -> assert B's PM inactive AND B's measure window is B's (independent). Capture
//      multidoc_04_B_independent.png. Finish (endFrame Break).
void ScenarioRunner::runMultidoc(AppState& st) {
    if (!(st.wantMultidoc && st.workerStarted && !st.multidocShotDone)) return;
    if (!mdDirReady) {
        mdDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        mdSavePath = base + "/scratch/multidoc_shots";
        std::error_code ec; std::filesystem::create_directories(mdSavePath, ec);
    }
    auto loadMesh = [&](const core::OpsResponse& r) {
        if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    };
    // Build the box + fillet ops verbatim from m1 (so we reuse the engine contract, not invent one).
    auto buildBox = [&]() -> core::OpsResponse {
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op","add_feature"},{"feature",{
            {"type","box"},{"name","Box1"},{"params",{
                {"length",{{"value",60.0},{"expr",nullptr}}},
                {"width", {{"value",40.0},{"expr",nullptr}}},
                {"height",{{"value",10.0},{"expr",nullptr}}},
                {"centered",true}}}}}});
        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
        return r;
    };
    // Find vertical edges (axis ~ +Z) for the fillet — copied from m1's verticalEdges helper.
    auto verticalEdges = [&](int wantN) {
        std::vector<nlohmann::json> out;
        for (int tid = 1; tid <= 12 && (int)out.size() < wantN; ++tid) {
            std::string serr;
            std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, serr);
            if (s.empty()) continue;
            try {
                nlohmann::json o = nlohmann::json::parse(s);
                if (o.contains("axis") && o["axis"].is_array() && o["axis"].size() == 3 &&
                    std::fabs(o["axis"][2].get<double>()) > 0.9)
                    out.push_back(o);
            } catch (...) {}
        }
        return out;
    };
    auto buildFillet = [&]() -> core::OpsResponse {
        auto edges = verticalEdges(2);
        nlohmann::json arr = nlohmann::json::array();
        for (auto& e : edges) arr.push_back(e);
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op","add_feature"},{"feature",{
            {"type","fillet"},{"name","Fillet1"},
            {"params",{{"radius",{{"value",4.0},{"expr",nullptr}}}}},
            {"refs",{{"edges",arr}}}}}});
        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
        return r;
    };
    const int kSettle = 4;
    if (mdSettle > 0) { --mdSettle; return; }
    if (!mdCapture.empty()) return;  // only advance when no capture is pending
    std::string err;
    switch (mdStep) {
        case 0: {  // Part A: new part + box + fillet
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("multidoc: new_part A FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.multidocShotDone = true; break;
            }
            st.addDocTab(false);
            buildBox();
            buildFillet();
            mdIdxA = st.activeTab;
            mdDocA = st.docMirror.docId();
            st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
            std::printf("multidoc: Part A built doc=%s tab=%d\n", mdDocA.c_str(), mdIdxA);
            std::fflush(stdout);
            ++mdStep; mdSettle = kSettle; break;
        }
        case 1: {  // Put A mid-edit: seed selection + hide a body, then open a PM form (no commit)
            // Seed a viewport selection (face 1 of body 1) so the restore can be asserted later.
            render::PickHit hit;
            hit.body_id = 1; hit.kind = render::PickKind::Face; hit.topo_id = 1;
            st.selection.selectOne(hit);
            // Hide body slot 0 (the box body) — the hidden-body mask is parked/restored by the
            // DocSession swap, so it doubles as a model-state preservation probe.
            if (!st.bodyVisible.empty()) { st.bodyVisible[0] = 0; st.applyBodyVisibility(); }
            // Open a feature-edit PM form WITHOUT committing (leave active_ true).
            st.propertyManager->openForCommand(st, "fillet");
            std::printf("multidoc: A mid-edit pm_active=%d sel=%zu body0_hidden=%d\n",
                        (int)st.propertyManager->active(), st.selection.size(),
                        (int)(!st.bodyVisible.empty() && st.bodyVisible[0] == 0));
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_01_A_editing.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 2: {  // Part B: new part (parks A's session) -> assert B's PM is fresh -> box -> measure
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("multidoc: new_part B FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.multidocShotDone = true; break;
            }
            st.addDocTab(false);   // parks A's session, re-seats subsystems at B's fresh instances
            // B's PropertyManager is a fresh per-document instance — it must NOT be active.
            mdPassBFresh = !st.propertyManager->active();
            std::printf("multidoc: B fresh PM active=%d -> %s\n",
                        (int)st.propertyManager->active(), mdPassBFresh ? "PASS" : "FAIL");
            std::fflush(stdout);
            buildBox();
            mdIdxB = st.activeTab;
            mdDocB = st.docMirror.docId();
            // Measure on B (eval_measure with no selection still opens the Measure window on B's
            // per-document EvaluateDialogs — that's what we assert is independent of A later).
            st.evaluateDialogs->handleCommand(st, "eval_measure");
            mdPassBMeasure = (st.evaluateDialogs->openWindowKind() == "measure");
            std::printf("multidoc: B built doc=%s tab=%d measure_window='%s'\n",
                        mdDocB.c_str(), mdIdxB, st.evaluateDialogs->openWindowKind().c_str());
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_02_B.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 3: {  // Switch back to A -> assert PM form + selection + hidden-body mask all preserved
            st.switchToTab(mdIdxA);
            mdPassPm   = st.propertyManager->active();           // A's PM form survived
            mdPassDoc  = (st.docMirror.docId() == mdDocA);       // mirror re-pointed at A
            mdPassMask = (!st.bodyVisible.empty() && st.bodyVisible[0] == 0);  // hidden body restored
            mdPassSel  = (st.selection.size() == 1);             // viewport selection restored
            std::printf("multidoc: back-to-A pm_active=%d -> %s\n",
                        (int)mdPassPm, mdPassPm ? "PASS" : "FAIL");
            std::printf("multidoc: back-to-A mirror_doc=%s expect=%s -> %s\n",
                        st.docMirror.docId().c_str(), mdDocA.c_str(),
                        mdPassDoc ? "PASS" : "FAIL");
            std::printf("multidoc: back-to-A body0_hidden=%d -> %s\n",
                        (int)mdPassMask, mdPassMask ? "PASS" : "FAIL");
            std::printf("multidoc: back-to-A selection=%zu -> %s\n",
                        st.selection.size(), mdPassSel ? "PASS" : "FAIL");
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_03_A_restored.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 4: {  // Switch to B -> assert B's PM independent (inactive) + B's measure window intact
            st.switchToTab(mdIdxB);
            mdPassBIndep = !st.propertyManager->active();        // B never had a form -> still none
            bool bMeasureNow = (st.evaluateDialogs->openWindowKind() == "measure");
            // B keeps ITS measure window; A's evaluate dialogs are a different instance (so the
            // measure window is genuinely B's, not leaked from A which never measured).
            std::printf("multidoc: at-B pm_active=%d -> %s\n",
                        (int)st.propertyManager->active(), mdPassBIndep ? "PASS" : "FAIL");
            std::printf("multidoc: at-B measure_window='%s' -> %s\n",
                        st.evaluateDialogs->openWindowKind().c_str(),
                        bMeasureNow ? "PASS" : "FAIL");
            std::fflush(stdout);
            mdPassBMeasure = mdPassBMeasure && bMeasureNow;
            mdCapture = mdSavePath + "/multidoc_04_B_independent.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 5: {  // Part C: new part (scales the DocSession to a third doc) + box + a DIFFERENT mid-edit
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("multidoc: new_part C FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.multidocShotDone = true; break;
            }
            st.addDocTab(false);   // parks B's session, re-seats subsystems at C's fresh instances
            // C's PropertyManager is a fresh per-document instance — it must NOT be active.
            mdPassCFresh = !st.propertyManager->active();
            std::printf("multidoc: C fresh PM active=%d -> %s\n",
                        (int)st.propertyManager->active(), mdPassCFresh ? "PASS" : "FAIL");
            std::fflush(stdout);
            buildBox();
            mdIdxC = st.activeTab;
            mdDocC = st.docMirror.docId();
            // Give C a mid-edit that is DISTINGUISHABLE from A's (A = fillet form, 1 selection, body0
            // hidden). C opens a CHAMFER form and seeds NO viewport selection + hides NOTHING, so the
            // form type AND the selection/mask differ from A. This is what proves a switch restores the
            // RIGHT doc's edit state rather than leaking A's.
            st.selection.clear();
            st.propertyManager->openForCommand(st, "chamfer");
            std::printf("multidoc: C built doc=%s tab=%d pm_active=%d form='%s' sel=%zu\n",
                        mdDocC.c_str(), mdIdxC, (int)st.propertyManager->active(),
                        st.propertyManager->activeFormType().c_str(), st.selection.size());
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_05_C_editing.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 6: {  // A<->C hop 1: switch to A — A's FILLET form + selection + hidden mask still intact
            st.switchToTab(mdIdxA);
            bool pm   = st.propertyManager->active();
            bool doc  = (st.docMirror.docId() == mdDocA);
            bool form = (st.propertyManager->activeFormType() == "fillet");  // A's form, not C's chamfer
            bool mask = (!st.bodyVisible.empty() && st.bodyVisible[0] == 0);
            bool sel  = (st.selection.size() == 1);
            mdPassChop[0] = pm && doc && form && mask && sel;
            std::printf("multidoc: A<->C hop 1 pm=%d doc=%s form='%s' mask=%d sel=%zu rev=%d -> %s\n",
                        (int)pm, st.docMirror.docId().c_str(),
                        st.propertyManager->activeFormType().c_str(),
                        (int)mask, st.selection.size(), st.docMirror.revision(),
                        mdPassChop[0] ? "PASS" : "FAIL");
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_07_A_after_C.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 7: {  // A<->C hop 2: switch to C — C's CHAMFER form + C's empty selection (distinct from A)
            st.switchToTab(mdIdxC);
            bool pm   = st.propertyManager->active();
            bool doc  = (st.docMirror.docId() == mdDocC);
            bool form = (st.propertyManager->activeFormType() == "chamfer");  // C's form, not A's fillet
            bool sel  = (st.selection.size() == 0);  // C seeded no selection; A had 1 -> distinct
            mdPassChop[1] = pm && doc && form && sel;
            std::printf("multidoc: A<->C hop 2 pm=%d doc=%s form='%s' sel=%zu rev=%d -> %s\n",
                        (int)pm, st.docMirror.docId().c_str(),
                        st.propertyManager->activeFormType().c_str(),
                        st.selection.size(), st.docMirror.revision(),
                        mdPassChop[1] ? "PASS" : "FAIL");
            std::fflush(stdout);
            mdCapture = mdSavePath + "/multidoc_06_C_restored.png"; ++mdStep; mdSettle = kSettle;
            break;
        }
        case 8: {  // A<->C hop 3: switch back to A — fillet/selection/mask still intact (cached path)
            st.switchToTab(mdIdxA);
            bool pm   = st.propertyManager->active();
            bool doc  = (st.docMirror.docId() == mdDocA);
            bool form = (st.propertyManager->activeFormType() == "fillet");
            bool mask = (!st.bodyVisible.empty() && st.bodyVisible[0] == 0);
            bool sel  = (st.selection.size() == 1);
            mdPassChop[2] = pm && doc && form && mask && sel;
            std::printf("multidoc: A<->C hop 3 pm=%d doc=%s form='%s' mask=%d sel=%zu rev=%d -> %s\n",
                        (int)pm, st.docMirror.docId().c_str(),
                        st.propertyManager->activeFormType().c_str(),
                        (int)mask, st.selection.size(), st.docMirror.revision(),
                        mdPassChop[2] ? "PASS" : "FAIL");
            std::fflush(stdout);
            ++mdStep; mdSettle = kSettle;
            break;
        }
        case 9: {  // A<->C hop 4: switch to C — chamfer/empty-selection still intact (independence holds)
            st.switchToTab(mdIdxC);
            bool pm   = st.propertyManager->active();
            bool doc  = (st.docMirror.docId() == mdDocC);
            bool form = (st.propertyManager->activeFormType() == "chamfer");
            bool sel  = (st.selection.size() == 0);
            mdPassChop[3] = pm && doc && form && sel;
            std::printf("multidoc: A<->C hop 4 pm=%d doc=%s form='%s' sel=%zu rev=%d -> %s\n",
                        (int)pm, st.docMirror.docId().c_str(),
                        st.propertyManager->activeFormType().c_str(),
                        st.selection.size(), st.docMirror.revision(),
                        mdPassChop[3] ? "PASS" : "FAIL");
            std::fflush(stdout);
            ++mdStep; mdSettle = kSettle;
            break;
        }
        default: {
            bool allChop = mdPassChop[0] && mdPassChop[1] && mdPassChop[2] && mdPassChop[3];
            bool allPass = mdPassPm && mdPassDoc && mdPassMask && mdPassSel &&
                           mdPassBFresh && mdPassBIndep && mdPassBMeasure &&
                           mdPassCFresh && allChop;
            if (allPass) {
                std::printf("multidoc: ALL PASS\n");
            } else {
                std::printf("multidoc: FAIL");
                if (!mdPassPm)       std::printf(" A-pm-preserved");
                if (!mdPassDoc)      std::printf(" A-mirror-repointed");
                if (!mdPassMask)     std::printf(" A-hidden-mask");
                if (!mdPassSel)      std::printf(" A-selection");
                if (!mdPassBFresh)   std::printf(" B-pm-fresh");
                if (!mdPassBIndep)   std::printf(" B-pm-independent");
                if (!mdPassBMeasure) std::printf(" B-measure-independent");
                if (!mdPassCFresh)   std::printf(" C-pm-fresh");
                for (int k = 0; k < 4; ++k)
                    if (!mdPassChop[k]) std::printf(" A<->C-hop%d", k + 1);
                std::printf("\n");
            }
            std::fflush(stdout);
            st.multidocShotDone = true;
            break;
        }
    }
}

// Tab-switch latency harness (CHUDWARE_SHOT=tabperf). Builds two real box parts, then hammers
// A<->B via the same switchToTab path the doc-tab strip uses. logTabPerf in core/doc_tabs.cpp
// prints "[perf] tab_switch ..." to stderr for each switch; collect those lines after the run.
// Intentionally measurement-only — no UI assertions, no per-step screenshots.
void ScenarioRunner::runTabPerf(AppState& st) {
    if (!(st.wantTabPerf && st.workerStarted && !st.tabPerfShotDone)) return;
    auto loadMesh = [&](const core::OpsResponse& r) {
        if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    };
    auto buildBox = [&](double L, double W, double H) -> core::OpsResponse {
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op","add_feature"},{"feature",{
            {"type","box"},{"name","Box1"},{"params",{
                {"length",{{"value",L},{"expr",nullptr}}},
                {"width", {{"value",W},{"expr",nullptr}}},
                {"height",{{"value",H},{"expr",nullptr}}},
                {"centered",true}}}}}});
        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
        return r;
    };
    // One settle frame between switches so the GPU-cache park/bind path settles and any
    // deferred mesh drain from the previous switch has a chance to finish.
    const int kSettle = 2;
    if (tpSettle > 0) { --tpSettle; return; }
    std::string err;
    switch (tpStep) {
        case 0: {  // Part A
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("tabperf: new_part A FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.tabPerfShotDone = true; break;
            }
            st.addDocTab(false);
            auto r = buildBox(60.0, 40.0, 10.0);
            tpIdxA = st.activeTab;
            std::printf("tabperf: Part A doc=%s tab=%d ok=%d\n",
                        st.docMirror.docId().c_str(), tpIdxA, (int)r.ok);
            std::fflush(stdout);
            ++tpStep; tpSettle = kSettle; break;
        }
        case 1: {  // Part B (parks A's session + GPU cache)
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("tabperf: new_part B FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.tabPerfShotDone = true; break;
            }
            st.addDocTab(false);
            auto r = buildBox(80.0, 50.0, 20.0);
            tpIdxB = st.activeTab;
            std::printf("tabperf: Part B doc=%s tab=%d ok=%d (A tab=%d)\n",
                        st.docMirror.docId().c_str(), tpIdxB, (int)r.ok, tpIdxA);
            std::fflush(stdout);
            // Prime: switch A then B once so both sessions hold a GPU cache before the timed loop.
            // These two switches are still timed (logTabPerf fires) and count toward the total.
            ++tpStep; tpSettle = kSettle; break;
        }
        case 2: {
            // Alternate A <-> B until we have kTabPerfSwitches timed switches.
            if (tpSwitches >= kTabPerfSwitches) {
                std::printf("tabperf: done switches=%d\n", tpSwitches);
                std::fflush(stdout);
                st.tabPerfShotDone = true;
                break;
            }
            const int target = (tpSwitches % 2 == 0) ? tpIdxA : tpIdxB;
            st.switchToTab(target);
            ++tpSwitches;
            if (tpSwitches == 1 || tpSwitches == kTabPerfSwitches || (tpSwitches % 6) == 0) {
                std::printf("tabperf: switch %d/%d -> tab %d doc=%s\n",
                            tpSwitches, kTabPerfSwitches, target, st.docMirror.docId().c_str());
                std::fflush(stdout);
            }
            tpSettle = kSettle;
            break;
        }
        default:
            st.tabPerfShotDone = true;
            break;
    }
}

// Engineering-drawing scenario (CHUDWARE_SHOT=drawing). Build a plate with a Ø12 bore and a 4-hole
// pattern, Make Drawing (one drawing_make: ISO frame + title block, third-angle layout at one
// standard scale, auto-dimensions, centre marks/lines), fill the title block, tolerance a dim,
// add a section, export PDF/DXF/SVG, then re-arrange first-angle. Self-checking: every check
// prints PASS/CHECK. Captures drawing_01_third_angle.png and drawing_02_first_angle.png.
void ScenarioRunner::runDrawing(AppState& st) {
    if (!(st.wantDrawing && st.workerStarted && !st.drawingShotDone && st.frame >= 5)) return;
    if (!dwgDirReady) {
        dwgDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        dwgSavePath = base + "/scratch/drawing_shots";
        std::error_code ec; std::filesystem::create_directories(dwgSavePath, ec);
    }
    auto vid_of = [](const nlohmann::json& v) -> std::string {
        std::string id = v.value("view_id", std::string());
        return id.empty() ? v.value("id", std::string()) : id;
    };
    const int kSettle = 5;
    if (dwgSettle > 0) { --dwgSettle; return; }
    if (!dwgCapture.empty()) return;
    std::string err;
    switch (dwgStep) {
        case 0: {  // new part + plate (80x50x12) with a central through hole (-> a circle in top view)
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("drawing: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                st.drawingShotDone = true; break;
            }
            st.addDocTab(false);
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Plate"},{"params",{
                    {"length",{{"value",80.0},{"expr",nullptr}}},
                    {"width", {{"value",50.0},{"expr",nullptr}}},
                    {"height",{{"value",12.0},{"expr",nullptr}}},
                    {"centered",true}}}}}});
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","cylinder"},{"name","Bore"},{"params",{
                    {"radius",{{"value",6.0},{"expr",nullptr}}},
                    {"height",{{"value",16.0},{"expr",nullptr}}},
                    {"centered",true},{"mode","cut"}}}}}});
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","hole"},{"name","Mounting Holes"},{"params",{
                    {"style","simple"},{"standard","custom"},
                    {"diameter",{{"value",6.6},{"expr",nullptr}}},{"end","through_all"},
                    {"positions", nlohmann::json::array({nlohmann::json::array({30.0, 17.0}),
                                                         nlohmann::json::array({-30.0, 17.0}),
                                                         nlohmann::json::array({30.0, -17.0}),
                                                         nlohmann::json::array({-30.0, -17.0})})}}},
                {"refs",{{"face",{{"of","face"},{"side","top"}}}}}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            std::printf("drawing: plate+hole ok=%d rev=%d\n", (int)r.ok, r.revision);
            std::fflush(stdout);
            dwgSettle = kSettle; ++dwgStep; break;
        }
        case 1: {  // Make Drawing -> standard views; ribbon swaps to the drawing tabs
            st.makeDrawing();
            std::printf("drawing: makeDrawing activeTab=%d isDrawing=%d\n",
                        st.activeTab, (int)st.activeTabIsDrawing());
            std::fflush(stdout);
            dwgSettle = kSettle; ++dwgStep; break;
        }
        case 2: {  // Detailing on top of the auto-dimensioned sheet + section + exports
            if (!st.activeTabIsDrawing()) {
                std::printf("drawing: NO drawing tab after makeDrawing CHECK\n"); std::fflush(stdout);
                st.drawingShotDone = true; break;
            }
            AppState::DocTab& tab = st.docTabs[st.activeTab];
            auto sheet0 = [&]() -> nlohmann::json {
                if (tab.drawingDoc.contains("sheets") && tab.drawingDoc["sheets"].is_array() &&
                    !tab.drawingDoc["sheets"].empty())
                    return tab.drawingDoc["sheets"][0];
                return nlohmann::json::object();
            };
            {
                const nlohmann::json sh = sheet0();
                int views = 0, anns = 0;
                for (const auto& v : sh.value("views", nlohmann::json::array())) {
                    if (v.value("type", std::string()) == "table_overlay") continue;
                    ++views;
                    anns += (int)v.value("annotations", nlohmann::json::array()).size();
                }
                const bool framed = sh.contains("frame") && sh["frame"].is_object() &&
                                    !sh["frame"].value("texts", nlohmann::json::array()).empty();
                const std::string proj =
                    sh.value("title_block", nlohmann::json::object()).value("projection", std::string());
                std::printf("drawing: make_drawing views=%d annotations=%d frame=%d projection=%s %s\n",
                            views, anns, (int)framed, proj.c_str(),
                            (views >= 4 && anns > 0 && framed && proj == "third") ? "PASS" : "CHECK");
            }
            nlohmann::json r3;
            const bool tb_ok = st.worker.call(
                "drawing_set_titleblock",
                {{"doc_id", tab.drawingDocId}, {"sheet_id", "s1"},
                 {"fields", {{"title", "MOUNTING PLATE"}, {"part_number", "CW-PLT-0042"},
                             {"drawn_by", "H. Hunter"}, {"checked_by", "QA"},
                             {"material", "AL 6061-T6"}, {"finish", "ANODISE CLEAR"},
                             {"general_tolerance", "ISO 2768-m"}, {"company", "CHUDWARE"},
                             {"rev", "A"}}}},
                r3, err);
            st.refreshDrawingTab(st.activeTab);
            // Make the first linear dim a critical +/-0.1 tolerance.
            std::string tvid, tann, top_vid;
            nlohmann::json top_bbox;
            for (const auto& v : sheet0().value("views", nlohmann::json::array())) {
                if (v.value("orientation", std::string()) == "top") {
                    top_vid = vid_of(v);
                    top_bbox = v.value("bbox", nlohmann::json());
                }
                for (const auto& a : v.value("annotations", nlohmann::json::array()))
                    if (tann.empty() && a.value("type", std::string()) == "linear_dim") {
                        tvid = vid_of(v); tann = a.value("id", std::string());
                    }
            }
            bool tol_ok = false;
            if (!tann.empty()) {
                nlohmann::json r4;
                tol_ok = st.worker.call("drawing_set_annotation",
                               {{"doc_id", tab.drawingDocId}, {"view_id", tvid}, {"ann_id", tann},
                                {"tol_type", "symmetric"}, {"tol_upper", 0.1}, {"precision", 2},
                                {"critical", true}}, r4, err);
            }
            // Section A-A through the bore (horizontal cut on the top view), then re-fit.
            bool sec_ok = false;
            if (!top_vid.empty() && top_bbox.is_array() && top_bbox.size() == 4) {
                const double x0 = top_bbox[0].get<double>(), x1 = top_bbox[2].get<double>();
                const double cy = (top_bbox[1].get<double>() + top_bbox[3].get<double>()) * 0.5;
                nlohmann::json r5;
                sec_ok = st.worker.call(
                    "drawing_add_section",
                    {{"doc_id", tab.drawingDocId}, {"parent_view_id", top_vid},
                     {"cut_line", nlohmann::json::array({nlohmann::json::array({x0 - 6.0, cy}),
                                                         nlohmann::json::array({x1 + 6.0, cy})})},
                     {"label", "A"}, {"sheet_id", "s1"}},
                    r5, err);
                nlohmann::json r6;
                st.worker.call("drawing_arrange", {{"doc_id", tab.drawingDocId}, {"sheet_id", "s1"}},
                               r6, err);
            }
            st.refreshDrawingTab(st.activeTab);
            tab.drawNeedFit = true;
            std::printf("drawing: titleblock=%d critical_dim=%s section=%d %s\n", (int)tb_ok,
                        tann.empty() ? "none" : tann.c_str(), (int)sec_ok,
                        (tb_ok && tol_ok && sec_ok) ? "PASS" : "CHECK");
            // Deliverables: vector PDF + DXF + SVG of the same sheet.
            for (const char* fmt : {"pdf", "dxf", "svg"}) {
                const std::string out = dwgSavePath + "/mounting_plate." + fmt;
                nlohmann::json rx;
                const bool ok = st.worker.call("export_drawing",
                                               {{"doc_id", tab.drawingDocId}, {"path", out},
                                                {"format", fmt}}, rx, err);
                std::error_code ec;
                const auto bytes = std::filesystem::file_size(out, ec);
                std::printf("drawing: export %s -> %s bytes=%llu %s\n", fmt, out.c_str(),
                            ec ? 0ull : (unsigned long long)bytes,
                            (ok && !ec && bytes > 200) ? "PASS" : "CHECK");
            }
            // In-app copilot trigger: a chat "make an engineering drawing of this part" is
            // intercepted by the agent fast-path (no provider needed) and builds a drawing.
            {
                nlohmann::json r7;
                bool ok7 = st.worker.call(
                    "agent_run_v2",
                    {{"request", "make an engineering drawing of this part"},
                     {"doc_id", tab.sourceDocId}},
                    r7, err);
                bool made = ok7 && r7.is_object() && r7.value("created_drawing", false);
                std::printf("drawing: agent fast-path created_drawing=%d %s\n", (int)made,
                            made ? "PASS" : "CHECK");
            }
            std::fflush(stdout);
            dwgCapture = dwgSavePath + "/drawing_01_third_angle.png"; dwgSettle = kSettle; ++dwgStep;
            break;
        }
        case 3: {  // Re-lay first-angle: top view drops below the front, right view moves left
            if (!st.activeTabIsDrawing()) { st.drawingShotDone = true; break; }
            AppState::DocTab& tab = st.docTabs[st.activeTab];
            nlohmann::json r;
            const bool ok = st.worker.call("drawing_arrange",
                                           {{"doc_id", tab.drawingDocId}, {"sheet_id", "s1"},
                                            {"projection", "first"}},
                                           r, err);
            st.refreshDrawingTab(st.activeTab);
            tab.drawNeedFit = true;
            double front_y = 0, top_y = 0;
            if (ok && r.contains("drawing"))
                for (const auto& v : r["drawing"]["sheets"][0].value("views", nlohmann::json::array())) {
                    const auto& bb = v.value("bbox", nlohmann::json());
                    if (!bb.is_array() || bb.size() != 4) continue;
                    const double cy = (bb[1].get<double>() + bb[3].get<double>()) * 0.5;
                    if (v.value("orientation", std::string()) == "front") front_y = cy;
                    if (v.value("orientation", std::string()) == "top") top_y = cy;
                }
            std::printf("drawing: first-angle arrange ok=%d top_below_front=%d %s\n", (int)ok,
                        (int)(top_y < front_y), (ok && top_y < front_y) ? "PASS" : "CHECK");
            std::fflush(stdout);
            dwgCapture = dwgSavePath + "/drawing_02_first_angle.png"; dwgSettle = kSettle; ++dwgStep;
            break;
        }
        default:
            st.drawingShotDone = true;
            std::printf("drawing: scenario complete\n"); std::fflush(stdout);
            break;
    }
}

ScenarioRunner::FrameAction ScenarioRunner::endFrame(AppState& st, int dw, int dh,
                                                     const char* screenshotPath) {
    // C17 M1 scenario: per-step captures. When the step machine set m1Capture, write the
    // current backbuffer to it (this frame already drew the post-op state) and clear it.
    // The scenario drives its own lifetime; we exit when m1ShotDone is latched AND no capture
    // is pending. (Counts as the "m1" regression-gate screenshot via m1_06_reopen.png copied
    // to the requested path by the gate runner; here we also satisfy the single screenshotPath
    // by writing the last step to it.)
    if (st.wantM1) {
        ++st.frame;
        if (!m1Capture.empty()) {
            // Mirror the final step to the caller-requested screenshotPath.
            const char* mirror = m1Capture.find("m1_06_reopen") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(m1Capture.c_str(), dw, dh, mirror);
            std::printf("m1 screenshot -> %s (%dx%d)\n", m1Capture.c_str(), dw, dh);
            std::fflush(stdout);
            m1Capture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.m1ShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // C18 sketch scenario: identical per-step capture mechanism.
    if (st.wantSketch) {
        ++st.frame;
        if (!sketchCapture.empty()) {
            const char* mirror = sketchCapture.find("sketch_03_solid") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(sketchCapture.c_str(), dw, dh, mirror);
            std::printf("sketch screenshot -> %s (%dx%d)\n", sketchCapture.c_str(), dw, dh);
            std::fflush(stdout);
            sketchCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.sketchShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // 3D sketch scenario: identical per-step capture mechanism.
    if (st.wantSketch3d) {
        ++st.frame;
        if (!s3Capture.empty()) {
            const char* mirror = s3Capture.find("sketch3d_02_drag") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(s3Capture.c_str(), dw, dh, mirror);
            std::printf("sketch3d screenshot -> %s (%dx%d)\n", s3Capture.c_str(), dw, dh);
            std::fflush(stdout);
            s3Capture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.sketch3dShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // C20b assembly scenario: identical per-step capture mechanism.
    // C39: a capture waits for async worker calls (eval_interference) to be drained —
    // busy() stays true until the result was applied, so the frame shows the table.
    if (st.wantAsm) {
        ++st.frame;
        if (!asmCapture.empty() && !st.rpc.busy()) {
            const char* mirror = asmCapture.find("asm_01_seated") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(asmCapture.c_str(), dw, dh, mirror);
            std::printf("asm screenshot -> %s (%dx%d)\n", asmCapture.c_str(), dw, dh);
            std::fflush(stdout);
            asmCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.asmShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // C19 fea/evalmass shots: drive the async scenario across frames; capture once it's done.
    if (st.wantFea) {
        ++st.frame;
        if (st.feaShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            std::printf("fea screenshot -> %s (%dx%d)\n",
                        screenshotPath ? screenshotPath : "(none)", dw, dh);
            std::fflush(stdout);
            studyShotExit(g_feaShot);  // a failed scenario exits non-zero after its capture
            glfwSwapBuffers(st.win);
            return FrameAction::Break;
        }
        glfwSwapBuffers(st.win);
        return FrameAction::Continue;
    }
    if (st.wantFlow) {
        ++st.frame;
        if (st.flowShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            std::printf("flow screenshot -> %s (%dx%d)\n",
                        screenshotPath ? screenshotPath : "(none)", dw, dh);
            std::fflush(stdout);
            studyShotExit(g_flowShot);
            glfwSwapBuffers(st.win);
            return FrameAction::Break;
        }
        glfwSwapBuffers(st.win);
        return FrameAction::Continue;
    }

    // W17 drawing scenario: per-step capture (one shot), wait for the sync RPCs to settle.
    if (st.wantDrawing) {
        ++st.frame;
        if (!dwgCapture.empty() && !st.rpc.busy()) {
            std::error_code ec; std::filesystem::create_directories(dwgSavePath, ec);
            writeBackbufferPng(dwgCapture.c_str(), dw, dh, screenshotPath);
            std::printf("drawing screenshot -> %s (%dx%d)\n", dwgCapture.c_str(), dw, dh);
            std::fflush(stdout);
            dwgCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.drawingShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (st.wantEvalMass) {
        ++st.frame;
        // C39: also wait for the async eval_mass to drain so the window shows values.
        if (st.evalMassShotDone && st.frame > 6 && !st.rpc.busy()) {
            writeBackbufferPng(screenshotPath, dw, dh);
            std::printf("evalmass screenshot -> %s (%dx%d)\n",
                        screenshotPath ? screenshotPath : "(none)", dw, dh);
            std::fflush(stdout);
            glfwSwapBuffers(st.win);
            return FrameAction::Break;
        }
        glfwSwapBuffers(st.win);
        return FrameAction::Continue;
    }


    // appearance scenario: single capture (mirrored to the caller's --ui-screenshot target),
    // waiting for any async worker call to drain so the modal + preview are settled.
    if (apWant) {
        ++st.frame;
        if (!apCapture.empty() && !st.rpc.busy()) {
            std::error_code ec; std::filesystem::create_directories(apSavePath, ec);
            writeBackbufferPng(apCapture.c_str(), dw, dh, screenshotPath);
            std::printf("appearance screenshot -> %s (%dx%d)\n", apCapture.c_str(), dw, dh);
            std::fflush(stdout);
            apCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return apDone ? FrameAction::Break : FrameAction::Continue;
    }

    // onboarding scenario: one capture per wizard step. The final workspace step is mirrored to
    // --ui-screenshot; all four remain in scratch/onboarding_shots for visual inspection.
    if (obWant) {
        ++st.frame;
        if (!obCapture.empty()) {
            const char* mirror =
                obCapture.find("onboarding_04_workspace") != std::string::npos
                    ? screenshotPath
                    : nullptr;
            writeBackbufferPng(obCapture.c_str(), dw, dh, mirror);
            std::printf("onboarding screenshot -> %s (%dx%d)\n",
                        obCapture.c_str(), dw, dh);
            std::fflush(stdout);
            obCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return obDone ? FrameAction::Break : FrameAction::Continue;
    }

    // RE-28 reveng_splat scenario: the PNG is rendered + written in tick() (own-FBO drain, like
    // iconpipe), so endFrame only needs to break the loop once the drain has completed. No
    // backbuffer capture here — the splat frame lives in a private FBO, never the default one.
    if (rsWant) {
        ++st.frame;
        return rsDone ? FrameAction::Break : FrameAction::Continue;
    }

    // multidoc scenario: identical per-step capture mechanism. Wait for any async worker call
    // (eval_measure on B) to drain so the captured frame shows the settled state.
    if (st.wantMultidoc) {
        ++st.frame;
        if (!mdCapture.empty() && !st.rpc.busy()) {
            // Mirror the LAST per-step capture to the caller's --ui-screenshot target. The final
            // capture chronologically is multidoc_06_C_restored.png (step 7, the C end of the
            // round-trip); steps 8/9 assert without capturing.
            const char* mirror = mdCapture.find("multidoc_06_C_restored") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(mdCapture.c_str(), dw, dh, mirror);
            std::printf("multidoc screenshot -> %s (%dx%d)\n", mdCapture.c_str(), dw, dh);
            std::fflush(stdout);
            mdCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.multidocShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // tabperf scenario: no screenshots — just keep the frame loop alive until the switch
    // hammer finishes, then break so the process exits and stderr can be harvested.
    if (st.wantTabPerf) {
        ++st.frame;
        glfwSwapBuffers(st.win);
        return st.tabPerfShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // workspaces scenario: one capture per workspace tab, last capture mirrored to screenshotPath.
    if (st.wantWorkspace) {
        ++st.frame;
        if (!wsCapture.empty()) {
            // Mirror the last workspace capture (AI workspace, step 8) to screenshotPath.
            const char* mirror = wsCapture.find("08_ai") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(wsCapture.c_str(), dw, dh, mirror);
            std::printf("workspaces: screenshot -> %s (%dx%d)\n", wsCapture.c_str(), dw, dh);
            std::fflush(stdout);
            wsCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.workspaceShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // motion scenario: three per-step captures (t0 setup, t1 frame0, t2 midpoint).
    if (st.wantMotion) {
        ++st.frame;
        if (!mtCapture.empty()) {
            const char* mirror = mtDone ? screenshotPath : nullptr;
            writeBackbufferPng(mtCapture.c_str(), dw, dh, mirror);
            std::printf("motion: screenshot -> %s (%dx%d)\n", mtCapture.c_str(), dw, dh);
            std::fflush(stdout);
            mtCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.motionShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    // topology scenario: per-step captures (the final capture is mirrored to --ui-screenshot).
    // A failure latches topoFailed: its capture is written, then the report, then the process
    // exits non-zero so a harness can never read the screenshots as a pass.
    if (topoWant) {
        ++st.frame;
        if (!topoCapture.empty() && (!st.rpc.busy() || ++topoCaptureWait > 600)) {
            std::error_code ec; std::filesystem::create_directories(topoSavePath, ec);
            writeBackbufferPng(topoCapture.c_str(), dw, dh, topoDone ? screenshotPath : nullptr);
            std::printf("topology: screenshot -> %s (%dx%d)\n", topoCapture.c_str(), dw, dh);
            std::fflush(stdout);
            topoReport["captures"].push_back(topoCapture);
            topoCapture.clear();
            topoCaptureWait = 0;
        }
        glfwSwapBuffers(st.win);
        if (topoDone && topoCapture.empty()) {
            topoWriteReport();
            if (topoFailed) {
                std::fflush(nullptr);
                std::_Exit(EXIT_FAILURE);
            }
            return FrameAction::Break;
        }
        return FrameAction::Continue;
    }

    // piping scenario: per-step captures; the final one is mirrored to --ui-screenshot.
    bool pipeDrawBreak = false;
    if (pipeDrawEndFrame(st, dw, dh, screenshotPath, pipeDrawBreak))
        return pipeDrawBreak ? FrameAction::Break : FrameAction::Continue;

    bool pipingBreak = false;
    if (pipingShotEndFrame(st, dw, dh, screenshotPath, pipingBreak))
        return pipingBreak ? FrameAction::Break : FrameAction::Continue;

    // hydraulics scenario: same per-step capture + report contract.
    bool hydBreak = false;
    if (hydraulicsShotEndFrame(st, dw, dh, screenshotPath, hydBreak))
        return hydBreak ? FrameAction::Break : FrameAction::Continue;

    // demoreview: saved-study captures + report; exits non-zero on failure.
    bool demoBreak = false;
    if (demoReviewEndFrame(st, dw, dh, screenshotPath, demoBreak))
        return demoBreak ? FrameAction::Break : FrameAction::Continue;

    // dwgopen: CAD-file open captures + report; exits non-zero on failure.
    bool dwgBreak = false;
    if (dwgOpenEndFrame(st, dw, dh, screenshotPath, dwgBreak))
        return dwgBreak ? FrameAction::Break : FrameAction::Continue;

    // projectchat: root/session reconciliation checks + report; exits non-zero on failure.
    bool projectChatBreak = false;
    if (projectChatEndFrame(st, dw, dh, screenshotPath, projectChatBreak))
        return projectChatBreak ? FrameAction::Break : FrameAction::Continue;

    // steerstatus: delivery-caption checks + report; exits non-zero on failure.
    bool steerBreak = false;
    if (steerStatusEndFrame(st, dw, dh, screenshotPath, steerBreak))
        return steerBreak ? FrameAction::Break : FrameAction::Continue;

    bool chatInlineBreak = false;
    if (chatInlineEndFrame(st, dw, dh, screenshotPath, chatInlineBreak))
        return chatInlineBreak ? FrameAction::Break : FrameAction::Continue;

    bool copilotUiBreak = false;
    if (copilotUiEndFrame(st, dw, dh, screenshotPath, copilotUiBreak))
        return copilotUiBreak ? FrameAction::Break : FrameAction::Continue;

    bool agentLockBreak = false;
    if (agentLockEndFrame(st, dw, dh, screenshotPath, agentLockBreak))
        return agentLockBreak ? FrameAction::Break : FrameAction::Continue;

    bool featureStateBreak = false;
    if (featureStateEndFrame(st, dw, dh, screenshotPath, featureStateBreak))
        return featureStateBreak ? FrameAction::Break : FrameAction::Continue;
#ifndef __EMSCRIPTEN__
    bool delayedAccountBreak = false;
    if (delayedAccountEndFrame(st, dw, dh, screenshotPath, delayedAccountBreak))
        return delayedAccountBreak ? FrameAction::Break : FrameAction::Continue;
#endif

    // simaudit: per-state captures + report; exits non-zero on any ID conflict.
    bool saBreak = false;
    if (simAuditEndFrame(st, dw, dh, screenshotPath, saBreak))
        return saBreak ? FrameAction::Break : FrameAction::Continue;

    bool interactiveBreak = false;
    if (simInteractiveEndFrame(st, dw, dh, screenshotPath, interactiveBreak))
        return interactiveBreak ? FrameAction::Break : FrameAction::Continue;

    return FrameAction::None;
}

// ---------- workspaces scenario (CHUDWARE_SHOT=workspaces) ----------
// Cycles all 8 workspace tabs (Model, Drawing, Simulate, Electronics, Manufacture, PLM,
// PartsLibrary, AI), letting the frame settle for kSettle frames after each switch, then
// capturing one PNG per workspace. Validates that the workspace routing switch produces a
// distinct non-blank frame for each tab.
void ScenarioRunner::runWorkspaces(AppState& st) {
    if (!st.wantWorkspace || st.workspaceShotDone) return;
    if (!st.workerStarted || st.frame < 5) return;

    if (!wsDirReady) {
        wsDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base  = repo ? std::string(repo) : std::string(".");
        wsSavePath = base + "/scratch/workspace_shots";
        std::error_code ec;
        std::filesystem::create_directories(wsSavePath, ec);
    }

    const int kSettle = 6;  // frames to wait after switching workspace before capturing

    // Drain any pending capture first.
    if (!wsCapture.empty()) return;

    if (wsSettle > 0) { --wsSettle; return; }

    // Each step: switch to a workspace, settle, capture.
    // Steps 0-7: switch + settle schedule; steps 8-15: capture; step 16: done.
    // Simpler: even steps switch+settle, odd steps capture (interleaved via wsSettle).
    using WS = AppState::Workspace;
    struct WSEntry { WS ws; const char* name; };
    static constexpr WSEntry kWS[] = {
        { WS::Model,        "01_model"        },
        { WS::Drawing,      "02_drawing"      },
        { WS::Simulate,     "03_simulate"     },
        { WS::Electronics,  "04_electronics"  },
        { WS::Manufacture,  "05_manufacture"  },
        { WS::PLM,          "06_plm"          },
        { WS::PartsLibrary, "07_parts_library"},
        { WS::AI,           "08_ai"           },
    };
    constexpr int kCount = (int)(sizeof(kWS) / sizeof(kWS[0]));

    if (wsStep < kCount) {
        // Switch workspace and schedule a settle + capture.
        st.activeWorkspace = kWS[wsStep].ws;
        std::printf("workspaces: switching to %s (step %d)\n", kWS[wsStep].name, wsStep);
        std::fflush(stdout);
        // Manufacture now shares the normal native viewport and integrated pinned column,
        // so the workspace sweep exercises the real layout without a shot-only CAM window.
        wsCapture = wsSavePath + "/workspace_" + kWS[wsStep].name + ".png";
        wsSettle  = kSettle;
        ++wsStep;
    } else {
        // All workspaces captured — restore Model and finish.
        st.activeWorkspace = WS::Model;
        st.workspaceShotDone = true;
        std::printf("workspaces: scenario complete (%d captures)\n", kCount);
        std::fflush(stdout);
    }
}

// ---------- motion scenario (CHUDWARE_SHOT=motion) ----------
// Kinematic motion study end-to-end. Steps:
//   0: Build two parts (base box + arm box), author them as AICM files.
//   1: new_assembly, insert both components.
//   2: Add a plane-distance mate between two faces (the motion driver mate).
//   3: Wait for engine readiness, create and configure a Motion study in Simulate.
//      Capture motion_t0.png (the setup/tree view before solve).
//   4: Dispatch the async motion study once; 5: wait for its completed trajectory.
//      Capture motion_t1.png (frame 0, bodies at start pose).
//   6: Advance to mid-frame via applyMotionFramePublic, capture motion_t2.png.
//      Bodies MUST be at a different position from t1 to prove live playback.
//
// Requires a real assembly with a drivable mate. Setup/solve failures stop the scenario
// explicitly; a captured setup image is not evidence of a completed motion run.
void ScenarioRunner::runMotion(AppState& st) {
    if (!st.wantMotion || st.motionShotDone) return;
    if (!st.workerStarted || st.frame < 5) return;

    if (!mtDirReady) {
        mtDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        mtSavePath = base + "/scratch/motion_shots";
        std::error_code ec;
        std::filesystem::create_directories(mtSavePath, ec);
        mtBasePath = base + "/scratch/motion_shots/motion_base.step+";
        mtArmPath  = base + "/scratch/motion_shots/motion_arm.step+";
    }

    if (!mtCapture.empty()) return;   // wait for endFrame to consume the capture
    if (mtSettle > 0) { --mtSettle; return; }

    const int kSettle = 6;

    // Helper: author a part via raw worker calls (same pattern as the asm scenario).
    // doc_new -> doc_apply_ops -> doc_save (step+) -> doc_close. The saved path is then
    // handed to insertComponent by the assembly step.
    auto makePart = [&](const char* name, const nlohmann::json& ops,
                        const std::string& outPath) -> bool {
        nlohmann::json res; std::string err;
        if (!st.worker.call("doc_new", {{"kind","part"},{"name",name}}, res, err)) {
            std::printf("motion: doc_new(%s) FAILED: %s\n", name, err.c_str()); std::fflush(stdout);
            return false;
        }
        std::string did = res.value("doc_id", std::string());
        int rev = res.value("revision", 0);
        nlohmann::json ap2;
        if (!st.worker.call("doc_apply_ops",
                {{"doc_id",did},{"base_revision",rev},{"ops",ops}}, ap2, err)) {
            std::printf("motion: doc_apply_ops(%s) FAILED: %s\n", name, err.c_str()); std::fflush(stdout);
            return false;
        }
        nlohmann::json sv;
        if (!st.worker.call("doc_save", {{"doc_id",did},{"path",outPath}}, sv, err)) {
            std::printf("motion: doc_save(%s) FAILED: %s\n", name, err.c_str()); std::fflush(stdout);
            return false;
        }
        nlohmann::json cl;
        st.worker.call("doc_close", {{"doc_id",did}}, cl, err);
        return true;
    };


    switch (mtStep) {
        case 0: {
            // Author base box (60x40x10mm) and arm box (10x10x50mm) parts.
            std::printf("motion: authoring base + arm parts\n"); std::fflush(stdout);
            auto dim = [](double v) { return nlohmann::json{{"value",v},{"expr",nullptr}}; };
            nlohmann::json baseOps = nlohmann::json::array();
            baseOps.push_back({{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Base"},{"suppressed",false},
                {"params",{{"length",dim(60)},{"width",dim(40)},{"height",dim(10)},
                           {"centered",true}}},{"refs",nlohmann::json::object()}}}});
            nlohmann::json armOps = nlohmann::json::array();
            armOps.push_back({{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Arm"},{"suppressed",false},
                {"params",{{"length",dim(10)},{"width",dim(10)},{"height",dim(50)},
                           {"centered",true}}},{"refs",nlohmann::json::object()}}}});
            bool okB = makePart("base", baseOps, mtBasePath);
            bool okA = makePart("arm",  armOps,  mtArmPath);
            std::printf("motion: parts authored base=%d arm=%d\n", (int)okB, (int)okA);
            std::fflush(stdout);
            if (!okB || !okA) { st.motionShotDone = true; break; }  // can't proceed without parts
            mtSettle = 2; ++mtStep; break;
        }
        case 1: {
            // Insert both components in one revision-safe operation. The base is grounded;
            // the arm starts 60 mm above it and remains free for the dimensional mate.
            std::string err;
            if (!st.docMirror.newAssembly(st.worker, err)) {
                std::printf("motion: new_assembly FAILED: %s\n", err.c_str());
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            mtAsmDoc = st.docMirror.docId();
            nlohmann::json off = nlohmann::json::array(
                {1,0,0,0, 0,1,0,0, 0,0,1,60, 0,0,0,1});
            nlohmann::json ops = nlohmann::json::array({
                {{"op","asm_insert_component"},{"source",{{"path",mtBasePath}}},
                 {"name","Ground"},{"fixed",true}},
                {{"op","asm_insert_component"},{"source",{{"path",mtArmPath}}},
                 {"name","Arm"},{"fixed",false},{"transform",off}},
            });
            auto r = st.docMirror.applyOps(st.worker, ops);
            mtK1 = r.id_map.value("$0", std::string());
            mtK2 = r.id_map.value("$1", std::string());
            if (!r.ok || mtK1.empty() || mtK2.empty()) {
                std::printf("motion: component insert FAILED: %s\n",
                            r.error_message.c_str());
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            if (!r.aicm_path.empty()) {
                std::string le;
                st.loadDocMesh(r.aicm_path, le);
            }
            std::printf("motion: assembly %s, base=%s arm=%s\n",
                        mtAsmDoc.c_str(), mtK1.c_str(), mtK2.c_str());
            std::fflush(stdout);
            st.yaw = 25; st.pitch = 18; st.zoom = 1.0f;
            mtSettle = kSettle; ++mtStep; break;
        }
        case 2: {
            // A resolved plane-distance mate is a proven drivable DOF. Selectors are
            // expressed in each centered component's local geometry coordinates.
            nlohmann::json baseFace = {
                {"kind","face"},{"centroid",nlohmann::json::array({0.0,0.0,5.0})}};
            nlohmann::json armFace = {
                {"kind","face"},{"centroid",nlohmann::json::array({0.0,0.0,25.0})}};
            nlohmann::json refs = nlohmann::json::array({
                {{"component",mtK1},{"selector",baseFace}},
                {{"component",mtK2},{"selector",armFace}},
            });
            nlohmann::json ops = nlohmann::json::array({{
                {"op","asm_add_mate"},
                {"mate",{{"type","distance"},
                         {"value",{{"value",60.0},{"expr",nullptr}}},
                         {"align","aligned"},{"suppressed",false},{"refs",refs}}}
            }});
            auto r = st.docMirror.applyOps(st.worker, ops);
            const std::string mateId = r.id_map.value("$0", std::string());
            if (!r.ok || mateId.empty()) {
                std::printf("motion: distance mate FAILED: %s\n",
                            r.error_message.c_str());
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            if (!r.aicm_path.empty()) {
                std::string le;
                st.loadDocMesh(r.aicm_path, le);
            }
            std::printf("motion: distance mate %s added base=%s arm=%s\n",
                        mateId.c_str(), mtK1.c_str(), mtK2.c_str());
            std::fflush(stdout);
            mtSettle = kSettle; ++mtStep; break;
        }
        case 3: {
            // Daemon bootstrap/snapshot RPCs can still be active after the viewport has
            // settled. newStudy rejects a busy engine; a frame delay is not readiness.
            if (st.rpc.busy()) return;
            if (!st.simManager) {
                std::printf("motion: FAILED — no Simulation Manager for the assembly\n");
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab     = AppState::LP_Simulation;
            const int studiesBefore = st.simManager->studyCount();
            st.simManager->handleCommand(st, "sim_new_motion");
            if (st.simManager->studyCount() != studiesBefore + 1) {
                std::printf("motion: FAILED — Motion study was not created: %s\n",
                            st.status.c_str());
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            st.simManager->configureActiveMotion(st, 60.0f, 100.0f, 24);
            std::printf("motion: Motion study created\n"); std::fflush(stdout);
            mtCapture = mtSavePath + "/motion_t0.png";
            mtSettle  = kSettle; ++mtStep; break;
        }
        case 4: {
            // Dispatch once; the following step waits for the async result.
            if (st.rpc.busy()) return;
            if (st.simManager)
                st.simManager->handleCommand(st, "sim_run");
            ++mtStep;
            break;
        }
        case 5: {
            if (st.rpc.busy()) return;
            mtRunOk = st.simManager && st.simManager->motionHasResult();
            std::printf("motion: sim_run complete, hasResult=%d, frames=%d\n",
                        (int)mtRunOk, mtRunOk ? st.simManager->motionFrameCount() : 0);
            std::fflush(stdout);
            if (!mtRunOk) {
                std::printf("motion: FAILED — no completed trajectory: %s\n", st.status.c_str());
                std::fflush(stdout);
                st.motionShotDone = true;
                break;
            }
            // Capture the completed trajectory's start pose.
            mtCapture = mtSavePath + "/motion_t1.png";
            mtSettle  = kSettle; ++mtStep; break;
        }
        case 6: {
            // Advance to mid-frame and capture. Bodies must differ from t1 to prove live playback.
            if (mtRunOk && st.simManager) {
                const int nFrames = st.simManager->motionFrameCount();
                const int mid     = nFrames / 2;
                std::printf("motion: advancing to mid-frame %d/%d\n", mid, nFrames);
                std::fflush(stdout);
                st.simManager->applyMotionFramePublic(st, mid);
                mtSettle = kSettle;
            } else {
                std::printf("motion: PARTIAL — solve did not produce frames; t2 same as t1\n");
                std::fflush(stdout);
            }
            mtCapture = mtSavePath + "/motion_t2.png";
            mtDone = true;
            ++mtStep; break;
        }
        default:
            st.motionShotDone = true;
            std::printf("motion: scenario complete (runOk=%d)\n", (int)mtRunOk);
            std::fflush(stdout);
            break;
    }
}

// ---------- topology optimization scenario (CHUDWARE_SHOT=topology) ----------
namespace {

constexpr double kTopoNaN = std::numeric_limits<double>::quiet_NaN();

double topoNow() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

// A positive number from the environment (seconds), else the fallback.
double topoEnvSeconds(const char* name, double fallback) {
    const char* v = std::getenv(name);
    if (!v || !*v) return fallback;
    char* end = nullptr;
    const double d = std::strtod(v, &end);
    return (end != v && std::isfinite(d) && d > 0.0) ? d : fallback;
}

// A numeric field, NaN when absent or not a number (never a silent default).
double topoNum(const nlohmann::json& o, const char* key) {
    if (!o.is_object()) return kTopoNaN;
    auto it = o.find(key);
    if (it == o.end() || !it->is_number()) return kTopoNaN;
    return it->get<double>();
}

std::string topoText(const nlohmann::json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return std::string();
    return v.dump();
}

const char* topoOutcomeName(ui::topology::Outcome o) {
    using O = ui::topology::Outcome;
    switch (o) {
        case O::Failed:         return "failed";
        case O::Cancelled:      return "cancelled";
        case O::Converged:      return "converged";
        case O::IterationLimit: return "iteration_limit";
        case O::Infeasible:     return "infeasible";
        case O::NotConverged:   return "not_converged";
        case O::Inconsistent:   return "inconsistent";
    }
    return "unknown";
}

}  // namespace

void ScenarioRunner::topoFail(const std::string& code, const std::string& detail) {
    std::printf("topology: FAILED [%s] %s\n", code.c_str(), detail.c_str());
    std::fflush(stdout);
    topoReport["ok"] = false;
    topoReport["failure"] = {{"code", code}, {"detail", detail}, {"step", topoStep}};
    topoFailed = true;
    topoDone = true;
    topoShotQueued.clear();
    topoCapture = topoSavePath + "/topology_FAILED.png";
}

void ScenarioRunner::topoWriteReport() {
    if (topoFailed) topoReport["ok"] = false;
    std::error_code ec;
    std::filesystem::create_directories(topoSavePath, ec);
    const std::string path = topoSavePath + "/topology_report.json";
    std::ofstream out(path, std::ios::trunc);
    out << topoReport.dump(2) << '\n';
    out.close();
    if (!out) {
        std::printf("topology: FAILED [report_write] could not write %s\n", path.c_str());
        topoFailed = true;
    } else {
        std::printf("topology: report -> %s ok=%d\n", path.c_str(),
                    (int)topoReport.value("ok", false));
    }
    std::fflush(stdout);
}

// Checks one retained run record honestly: the computation finished, the response is finite at
// the final design, the flags agree with each other and the final volume honours the budget it
// claims to satisfy. Non-convergence (iteration limit) is reported, not hidden and not failed.
bool ScenarioRunner::topoVerifyRun(const nlohmann::json& run, const std::string& tag,
                                   std::string& why) {
    namespace topo = ui::topology;
    if (!run.is_object() || !run.contains("result") || !run["result"].is_object()) {
        why = "the study holds no run record with a result";
        return false;
    }
    const nlohmann::json& r = run["result"];
    const topo::OutcomeSummary o = topo::classifyResult(r);
    const nlohmann::json sum = r.value("summary", nlohmann::json::object());
    const nlohmann::json settings = run.value("settings", nlohmann::json::object());
    const double objB = topoNum(sum, "objective_baseline");
    double objF = topoNum(sum, "objective_final");
    if (!std::isfinite(objF)) objF = topoNum(sum, "objective");
    const double vf = topoNum(sum, "volume_fraction_final");
    double target = topoNum(sum, "volume_fraction_target");
    if (!std::isfinite(target)) target = topoNum(settings, "volume_fraction");
    const double iters = topoNum(sum, "iterations");
    const double massF = topoNum(sum, "mass_final_g");

    nlohmann::json entry = {
        {"tag", tag},
        {"run_id", run.value("run_id", std::string())},
        {"method", r.value("method", std::string())},
        {"physics", r.value("physics", std::string())},
        {"formulation", r.value("formulation", std::string())},
        {"ok", o.computationOk},
        {"converged", o.converged},
        {"feasibility_reported", o.feasibilityReported},
        {"feasible", o.feasible},
        {"termination", o.termination},
        {"outcome", topoOutcomeName(o.outcome)},
        {"headline", o.headline},
        {"detail", o.detail},
        {"objective", sum.value("objective", nlohmann::json())},
        {"objective_units", sum.value("objective_units", nlohmann::json())},
        {"volume_phase", sum.value("volume_phase", nlohmann::json())},
        {"convergence_criterion", sum.value("convergence_criterion", nlohmann::json())},
    };
    auto putNum = [&](const char* k, double v) {
        entry[k] = std::isfinite(v) ? nlohmann::json(v) : nlohmann::json();
    };
    putNum("objective_baseline", objB);
    putNum("objective_final", objF);
    putNum("volume_fraction_final", vf);
    putNum("volume_fraction_target", target);
    putNum("iterations", iters);
    putNum("mass_final_g", massF);
    putNum("max_constraint_violation", o.maxViolation);
    if (r.contains("artifacts") && r["artifacts"].is_object()) {
        nlohmann::json keys = nlohmann::json::array();
        for (auto it = r["artifacts"].begin(); it != r["artifacts"].end(); ++it)
            keys.push_back(it.key());
        entry["artifacts"] = keys;
    }
    topoReport["runs"].push_back(entry);
    std::printf("topology: run[%s] %s method=%s ok=%d converged=%d feasible=%s termination=%s "
                "objective %g -> %g, volume %g (target %g), iterations %g — %s\n",
                tag.c_str(), entry["run_id"].get<std::string>().c_str(),
                entry["method"].get<std::string>().c_str(), (int)o.computationOk,
                (int)o.converged,
                o.feasibilityReported ? (o.feasible ? "yes" : "no") : "not-reported",
                o.termination.c_str(), objB, objF, vf, target, iters, o.headline.c_str());
    std::fflush(stdout);

    if (!o.computationOk) {
        std::string err = topoText(r.value("error", nlohmann::json()));
        if (err.empty()) err = topoText(r.value("termination_detail", nlohmann::json()));
        why = "the solver reported ok=false: " + (err.empty() ? o.detail : err);
        return false;
    }
    if (o.outcome == topo::Outcome::Cancelled) { why = "the run was cancelled"; return false; }
    if (o.outcome == topo::Outcome::Inconsistent) {
        why = "the result contradicts itself: " + o.detail;
        return false;
    }
    if (!std::isfinite(objB) || !std::isfinite(objF)) {
        why = "objective baseline/final is not finite";
        return false;
    }
    if (!std::isfinite(vf) || vf < 0.0 || vf > 1.0) {
        why = "final volume fraction is missing or outside [0,1]";
        return false;
    }
    if (!std::isfinite(iters) || iters < 1.0) {
        why = "the result reports no optimization iteration";
        return false;
    }
    // History: one iteration axis, every series the same length, finite samples only.
    const nlohmann::json hist = r.value("history", nlohmann::json::object());
    if (!hist.contains("iteration") || !hist["iteration"].is_array() ||
        hist["iteration"].empty()) {
        why = "the result carries no iteration history";
        return false;
    }
    const size_t nHist = hist["iteration"].size();
    for (auto it = hist.begin(); it != hist.end(); ++it) {
        if (!it->is_array()) continue;
        if (it->size() != nHist) {
            why = "history series '" + it.key() + "' length differs from the iteration axis";
            return false;
        }
        // The iteration axis and the objective trace must be finite everywhere; other series may
        // carry null for "not evaluated at this iterate" (JSON has no NaN), never a non-number.
        const bool strict = it.key() == "iteration" || it.key() == "objective";
        for (const auto& v : *it) {
            if (v.is_null() && !strict) continue;
            if (!v.is_number() || !std::isfinite(v.get<double>())) {
                why = "history series '" + it.key() + "' holds a non-finite sample";
                return false;
            }
        }
    }
    if (r.contains("constraints") && r["constraints"].is_array()) {
        for (const auto& c : r["constraints"]) {
            for (const char* k : {"value", "limit", "violation"}) {
                const double v = topoNum(c, k);
                if (!std::isfinite(v)) {
                    why = "constraint '" + c.value("name", std::string("?")) + "' has non-finite " + k;
                    return false;
                }
            }
        }
    }
    if (o.feasibilityReported && !o.feasible) {
        why = "the final design violates its constraints (max violation " +
              std::to_string(o.maxViolation) + ")";
        return false;
    }
    // A feasible claim must agree with the measured final volume of the budgeted phase.
    if (o.feasible && std::isfinite(target) && vf > target * 1.05 + 1e-3) {
        why = "feasible is claimed but the final volume fraction " + std::to_string(vf) +
              " exceeds the target " + std::to_string(target);
        return false;
    }
    if (!o.converged)
        std::printf("topology: run[%s] NOTE not converged (%s): %s\n", tag.c_str(),
                    o.termination.c_str(), o.detail.c_str());
    std::fflush(stdout);
    return true;
}

// Steps (each capture is preceded by a settle so panels lay out before the readback):
//   0 design domain: new part, 120x40x40 mm box, resolve its six faces
//   1 Simulate workspace + Topology ribbon tab; 2 wait for the native capability matrix,
//     choose SIMP + a second structural method, capture the ribbon and the "+ New Study" picker
//   3 sim_new_topology, picked fixture (-X) / force (+X) / preserve (bottom) faces
//   4 configure: 500 N -Z force, 4 mm preserve band, exclude keep-out box, PLA, 5 mm tet4
//     mesh, SIMP compliance at 35 % solid, FDM 0.4 preset and bottom bed face; capture Regions
//   5..7 Boundaries / Manufacturing / Method captures
//   8..9 Run SIMP, wait, verify, capture the result
//   10..12 switch to the second method (selectCombination), run, verify, capture the result
//   13 compare the two retained runs, capture
//   14 save .step+, reopen a copy through the engine, verify the retained runs + artifacts
//   15 thermal study: picked fixture + real heat loads, capture the boundary editor (no run)
//   16 flow study: inlet/outlet + picked wall, capture the boundary editor (no run)
//   17..18 reselect the structural study, comparison view, final capture
void ScenarioRunner::runTopology(AppState& st) {
    namespace topo = ui::topology;
    if (!topoInit) {
        topoInit = true;
        const char* mode = std::getenv("CHUDWARE_SHOT");
        topoWant = st.shot && mode && std::strcmp(mode, "topology") == 0;
        if (topoWant) {
            topoReport = {{"scenario", "topology"}, {"ok", false},
                          {"captures", nlohmann::json::array()},
                          {"runs", nlohmann::json::array()}};
        }
    }
    if (!topoWant || topoDone) return;
    if (!st.workerStarted || st.frame < 5) return;
    if (!topoDirReady) {
        topoDirReady = true;
        const char* repo = std::getenv("CHUDWARE_REPO");
        const std::string base = repo ? std::string(repo) : std::string(".");
        topoSavePath = base + "/scratch/topology_shots";
        std::error_code ec;
        std::filesystem::create_directories(topoSavePath, ec);
    }
    if (!topoCapture.empty()) return;  // endFrame consumes it
    if (topoSettle > 0) { --topoSettle; return; }
    if (!topoShotQueued.empty()) {
        topoCapture = topoShotQueued;
        topoShotQueued.clear();
        return;
    }

    const int kSettle = 8;
    const double kCapsTimeoutS = topoEnvSeconds("CHUDWARE_TOPOLOGY_CAPS_TIMEOUT_S", 90.0);
    const double kRunTimeoutS  = topoEnvSeconds("CHUDWARE_TOPOLOGY_RUN_TIMEOUT_S", 1800.0);
    auto shoot = [&](const std::string& name, int settle) {
        topoShotQueued = topoSavePath + "/" + name;
        topoSettle = settle;
    };
    // Face selector of a resolved role, with the viewport identity the wash highlight uses.
    auto faceSel = [&](const char* role) {
        nlohmann::json sel = topoFaces[role]["sel"];
        sel["_pick"] = {{"body", 1}, {"topo", topoFaces[role]["tid"]}};
        return sel;
    };
    // Human route: the face lands in st.selection exactly as a viewport click resolves it.
    auto pickFace = [&](const char* role) {
        st.selection.clear();
        core::SelItem it;
        it.hit.body_id = 1;
        it.hit.kind = render::PickKind::Face;
        it.hit.topo_id = topoFaces[role]["tid"].get<uint32_t>();
        it.selector = topoFaces[role]["sel"].dump();
        it.pending_resolve = false;
        st.selection.mutableItems().push_back(it);
    };
    auto lastEntryHasFaces = [](const nlohmann::json& arr) {
        return arr.is_array() && !arr.empty() && arr.back().is_object() &&
               arr.back().contains("faces") && arr.back()["faces"].is_array() &&
               !arr.back()["faces"].empty();
    };
    // validateTopology over the selected study; blocking issues fail the scenario by name.
    auto validateActive = [&](const char* code) -> bool {
        const nlohmann::json study = st.simManager->activeTopologyStudy();
        const auto issues = topo::validateTopology(
            study.value("topology", nlohmann::json::object()), st.simManager->topologyCapabilities(),
            study);
        std::string blocking;
        for (const auto& is : issues) {
            const bool block = is.severity == topo::Severity::Blocking;
            std::printf("topology: %s issue [%s] %s\n", block ? "BLOCKING" : "warning",
                        is.key.c_str(), is.message.c_str());
            if (block) blocking += (blocking.empty() ? "" : "; ") + is.message;
        }
        std::fflush(stdout);
        if (!blocking.empty()) { topoFail(code, blocking); return false; }
        return true;
    };
    // Steps 9/12: wait for the run sim_run started; verify it and capture the result.
    auto waitRun = [&](const std::string& tag, const std::string& shotName) {
        SimManager* sm = st.simManager;
        const double elapsed = topoNow() - topoWaitStart;
        if (sm->topologyRunActive()) {
            topoIdleFrames = 0;
            if (elapsed > kRunTimeoutS) {
                topoFail("run_" + tag + "_timeout",
                         "no result after " + std::to_string((int)elapsed) + " s: " + st.status);
                return;
            }
            if (!topoProgressShot && elapsed > 3.0) {  // Run/Cancel/progress panel mid-solve
                topoProgressShot = true;
                topoCapture = topoSavePath + "/topology_run_" + tag + "_progress.png";
            }
            return;
        }
        if (sm->topologyRunCount() <= topoRunsBefore) {
            // Neither running nor finished: give the launch a moment, then it was refused.
            if (st.rpc.busy() || ++topoIdleFrames < 30) return;
            topoFail("run_" + tag + "_not_started", "sim_run appended no run: " + st.status);
            return;
        }
        const nlohmann::json run = sm->activeTopologyResult();
        static double dfamWaitStart = 0.0;
        if (dfamWaitStart == 0.0) {
            std::string why;
            if (!topoVerifyRun(run, tag, why)) {
                topoFail("run_" + tag + "_invalid", why);
                return;
            }
        }
        const auto result = run.value("result", nlohmann::json::object());
        const auto manufacturing = result.value("manufacturing", nlohmann::json::object());
        const auto dfam = manufacturing.value("dfam", nlohmann::json::object());
        const auto artifacts = result.value("artifacts", nlohmann::json::object());
        if (!manufacturing.value("geometry_valid", false)) {
            // No validated geometry: no surface to print or check, so DFAM is never queued.
            // The SIMP run is the scenario's reference print run and must validate.
            if (tag == "simp") {
                topoFail("run_simp_geometry", "the SIMP reconstruction was not validated: " +
                                                  manufacturing.dump());
                return;
            }
            if (dfam.value("checked", false) || artifacts.contains("surface_print_stl")) {
                topoFail("run_" + tag + "_unvalidated_print",
                         "an unvalidated reconstruction produced print evidence: " +
                             manufacturing.dump());
                return;
            }
            topoReport["print_checks"][tag] = {{"geometry_valid", false},
                                               {"surface_print_stl", false},
                                               {"dfam", dfam}};
            std::printf("topology: run[%s] geometry not validated: no print surface, DFAM not run\n",
                        tag.c_str());
            topoRunIds.push_back(run.value("run_id", std::string()));
            std::printf("topology: run[%s] verified in %.1f s\n", tag.c_str(), elapsed);
            std::fflush(stdout);
            sm->handleCommand(st, "sim_results");
            shoot(shotName, kSettle * 2);
            ++topoStep;
            return;
        }
        if (!dfam.value("checked", false)) {
            const std::string error = dfam.value("error", std::string());
            if (!error.empty()) {
                topoFail("run_" + tag + "_dfam_unavailable", "DFAM check could not run: " + error);
                return;
            }
            if (dfamWaitStart == 0.0) dfamWaitStart = topoNow();
            if (topoNow() - dfamWaitStart > 125.0)
                topoFail("run_" + tag + "_dfam_timeout",
                         "DFAM was not checked: " + manufacturing.dump());
            return;
        }
        dfamWaitStart = 0.0;
        if (!artifacts.contains("surface_print_stl")) {
            topoFail("run_" + tag + "_print_surface", "surface_print_stl was not produced");
            return;
        }
        bool hasOverhang = false;
        const auto arrays = artifacts.value("overlay_aicm", nlohmann::json::object())
                                     .value("arrays", nlohmann::json::array());
        for (const auto &field : arrays)
            hasOverhang |= field.value("name", std::string()) == "overhang";
        if (!hasOverhang) {
            topoFail("run_" + tag + "_overhang", "the viewport overlay does not offer overhang");
            return;
        }
        topoReport["print_checks"][tag] = {
            {"surface_print_stl", true}, {"overhang", true}, {"dfam", dfam}};
        std::printf("topology: run[%s] surface_print_stl=present overlay.overhang=offered "
                    "manufacturing.dfam.checked=true passed=%d\n",
                    tag.c_str(), (int)dfam.value("passed", false));
        topoRunIds.push_back(run.value("run_id", std::string()));
        std::printf("topology: run[%s] verified in %.1f s\n", tag.c_str(), elapsed);
        std::fflush(stdout);
        sm->handleCommand(st, "sim_results");
        shoot(shotName, kSettle * 2);
        ++topoStep;
    };
    auto launchRun = [&](const char* tag) {
        if (st.rpc.busy()) return;  // the setup edit is still persisting
        topoRunsBefore = st.simManager->topologyRunCount();
        st.simManager->handleCommand(st, "sim_run");
        topoWaitStart = topoNow();
        topoIdleFrames = 0;
        topoProgressShot = false;
        std::printf("topology: run[%s] launched (%d retained runs before)\n", tag, topoRunsBefore);
        std::fflush(stdout);
        ++topoStep;
    };

    if (topoStep > 0 && !st.simManager) {
        topoFail("no_sim_manager", "the active document has no Simulation manager");
        return;
    }

    // The SIMP result is shown coloured by the unsupported-overhang scalar (the run's auto-show
    // picks the active field); the BESO run gets the previous field back.
    static std::string topoFieldBeforePrint;
    switch (topoStep) {
        case 0: {  // design domain
            std::string err;
            if (!st.docMirror.newPart(st.worker, err)) { topoFail("new_part", err); return; }
            auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "DesignDomain"},
                {"params", {{"length", dim(120.0)}, {"width", dim(40.0)}, {"height", dim(40.0)}}},
                {"centered", true}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (!r.ok) { topoFail("design_domain", r.error_message); return; }
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            // Resolve the six box faces and classify them by centroid.
            // Minimised keys per role: cx, -cx, cz, -cz (minX, maxX, minZ, maxZ).
            double ext[4] = {1e30, 1e30, 1e30, 1e30};
            const char* roles[4] = {"min_x", "max_x", "min_z", "max_z"};
            int faces = 0;
            topoFaces = nlohmann::json::object();
            for (uint32_t tid = 1; tid <= 24; ++tid) {
                std::string se;
                const std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                if (s.empty()) continue;
                nlohmann::json o;
                try { o = nlohmann::json::parse(s); } catch (...) { continue; }
                if (!o.contains("centroid") || !o["centroid"].is_array() || o["centroid"].size() != 3)
                    continue;
                ++faces;
                const double cx = o["centroid"][0].get<double>();
                const double cz = o["centroid"][2].get<double>();
                const double key[4] = {cx, -cx, cz, -cz};
                for (int k = 0; k < 4; ++k)
                    if (key[k] < ext[k]) {
                        ext[k] = key[k];
                        topoFaces[roles[k]] = {{"tid", tid}, {"sel", o}};
                    }
            }
            for (const char* role : roles)
                if (!topoFaces.contains(role)) {
                    topoFail("design_domain_faces",
                             "resolved " + std::to_string(faces) + " faces; no '" + role + "' face");
                    return;
                }
            std::printf("topology: design domain 120x40x40 mm rev=%d, %d faces (fixture tid %u, "
                        "load tid %u, preserve tid %u)\n", r.revision, faces,
                        topoFaces["min_x"]["tid"].get<uint32_t>(),
                        topoFaces["max_x"]["tid"].get<uint32_t>(),
                        topoFaces["min_z"]["tid"].get<uint32_t>());
            std::fflush(stdout);
            st.yaw = 35; st.pitch = 24; st.zoom = 1.0f;
            topoSettle = 3; ++topoStep;
            return;
        }
        case 1: {  // Simulate workspace, Topology ribbon tab; the capability fetch starts here
            if (!st.simManager) { topoFail("no_sim_manager", "no Simulation manager for the part"); return; }
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab = AppState::LP_Simulation;
            st.showRibbon = true;
            st.showSimulationTools = true;
            st.ribbonTab = AppState::RT_Topology;
            topoWaitStart = topoNow();
            ++topoStep;
            return;
        }
        case 2: {  // the native capability matrix decides the methods; nothing is assumed
            SimManager* sm = st.simManager;
            const topo::Capabilities& caps = sm->topologyCapabilities();
            if (caps.state == topo::Capabilities::State::Loading) {
                if (topoNow() - topoWaitStart > kCapsTimeoutS)
                    topoFail("capabilities_timeout", "topology_capabilities did not answer within " +
                                                         std::to_string((int)kCapsTimeoutS) + " s");
                return;
            }
            if (caps.state == topo::Capabilities::State::Unavailable) {
                topoFail("capabilities_unavailable", caps.error);
                return;
            }
            nlohmann::json matrix = nlohmann::json::array();
            for (const auto& c : caps.combinations)
                if (c.supported) matrix.push_back(c.method + "/" + c.physics);
            topoReport["capabilities_supported"] = matrix;
            std::printf("topology: capabilities ready, supported pairs: %s\n", matrix.dump().c_str());
            std::string reason;
            if (!caps.supports("simp", "structural", &reason)) {
                topoFail("structural_simp_unsupported", reason);
                return;
            }
            if (!caps.find("simp", "structural")->offersObjective("compliance")) {
                topoFail("structural_compliance_unsupported",
                         "SIMP/structural does not offer the compliance objective");
                return;
            }
            topoMethodA = "simp";
            topoMethodB.clear();
            for (const char* m : {"beso", "level_set", "ramp"})
                if (caps.supports(m, "structural")) { topoMethodB = m; break; }
            if (topoMethodB.empty()) {
                topoFail("no_second_structural_method",
                         "no BESO / level-set / RAMP structural combination is supported");
                return;
            }
            std::printf("topology: methods %s then %s\n",
                        topo::methodLabel(topoMethodA, &caps).c_str(),
                        topo::methodLabel(topoMethodB, &caps).c_str());
            std::fflush(stdout);
            st.ribbonTab = AppState::RT_Topology;
            sm->shotForcePicker = true;  // the "+ New Study" picker with its Topology entry
            shoot("topology_01_ribbon_picker.png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 3: {  // new study from the ribbon command + picked fixture / load / preserve faces
            SimManager* sm = st.simManager;
            sm->shotForcePicker = false;
            if (st.rpc.busy()) return;
            const int before = sm->studyCount();
            sm->handleCommand(st, "sim_new_topology");
            if (sm->studyCount() != before + 1 || !sm->activeTopologyStudy().is_object()) {
                topoFail("new_study", "sim_new_topology created no topology study: " + st.status);
                return;
            }
            topoStudyId = sm->activeStudyId();
            st.ribbonTab = AppState::RT_Topology;
            pickFace("min_x");
            sm->handleCommand(st, "sim_fixture");
            pickFace("max_x");
            sm->handleCommand(st, "sim_load_force");
            pickFace("min_z");
            sm->handleCommand(st, "sim_topology_preserve");  // opens the Regions form
            st.selection.clear();
            std::printf("topology: study %s created; fixture/load/preserve faces picked\n",
                        topoStudyId.c_str());
            std::fflush(stdout);
            topoSettle = 4; ++topoStep;
            return;
        }
        case 4: {  // physical setup through the shared study contract
            SimManager* sm = st.simManager;
            if (st.rpc.busy()) return;
            const topo::Capabilities& caps = sm->topologyCapabilities();
            const nlohmann::json study = sm->activeTopologyStudy();
            if (!study.is_object()) { topoFail("study_not_selected", "topology study lost"); return; }
            nlohmann::json loads = study.value("loads", nlohmann::json::array());
            nlohmann::json preserve = study.value("preserve", nlohmann::json::array());
            if (!lastEntryHasFaces(study.value("fixtures", nlohmann::json::array())) ||
                !lastEntryHasFaces(loads) || !lastEntryHasFaces(preserve)) {
                topoFail("pick_capture", "picked faces did not land in fixtures/loads/preserve: " +
                                             study.dump());
                return;
            }
            loads.back()["type"] = "force";
            loads.back()["vec"] = {0.0, 0.0, -500.0};       // N, tip load on the +X end
            preserve.back()["thickness_mm"] = 4.0;           // mounting flange stays solid
            // Keep-out above the middle of the span (cable / neighbour clearance), placed from
            // the resolved face centroids so it is right whatever the part origin: x 35..65 mm
            // from the fixed end, full width, from 26 mm above the bottom through the top.
            auto cen = [&](const char* role, int axis) {
                return topoFaces[role]["sel"]["centroid"][axis].get<double>();
            };
            const double x0 = cen("min_x", 0), yc = cen("min_x", 1);
            const double z0 = cen("min_z", 2), z1 = cen("max_z", 2);
            const nlohmann::json exclude = nlohmann::json::array({{
                {"label", "Clearance keep-out"},
                {"box_min_mm", {x0 + 35.0, yc - 21.0, z0 + 26.0}},
                {"box_max_mm", {x0 + 65.0, yc + 21.0, z1 + 1.0}}}});
            nlohmann::json t = study.value("topology", nlohmann::json::object());
            const topo::SwitchResult sw = topo::selectCombination(t, caps, topoMethodA, "structural");
            if (!sw.applied) { topoFail("method_select", sw.refusal); return; }
            for (const auto& k : sw.removedKeys) t[k] = nullptr;  // erase on merge, never send
            const topo::Combination* comb = caps.find(topoMethodA, "structural");
            t["objective"] = "compliance";
            // Solid budget over the whole domain: the BC/preserve layers (~17 %) are fixed solid
            // and the keep-out (~9 %) fixed void, so 35 % leaves real material to distribute.
            t["volume_fraction"] = 0.35;
            auto setIf = [&](const char* k, const nlohmann::json& v) {
                if (comb->offersKey(k)) t[k] = v;
            };
            setIf("max_iterations", 80);
            setIf("change_tolerance", 0.01);
            setIf("filter_radius_mm", 10.0);   // two 5 mm elements
            setIf("penalty", 3.0);
            setIf("preserve_thickness_mm", 4.0);
            t["build_direction"] = nullptr;  // bed face is the direction source
            const nlohmann::json patch = {
                {"name", "Bracket topology"},
                {"material", "pla"},
                {"mesh_order", 1},
                {"mesh_target_mm", 5.0},
                {"loads", loads},
                {"preserve", preserve},
                {"exclude", exclude},
                {"build_plate", {{"faces", nlohmann::json::array({faceSel("min_z")})},
                                   {"label", "Printer bed"}}},
                {"topology", t}};
            if (!sm->configureActiveTopology(st, patch)) {
                topoFail("configure", "configureActiveTopology refused the structural setup");
                return;
            }
            if (!sm->applyActiveTopologyPrintProcess(st, "fdm_0.4")) {
                topoFail("print_process", "the FDM 0.4 preset could not be applied");
                return;
            }
            const auto printed = sm->activeTopologyStudy();
            if (printed.value("print_process", std::string()) != "fdm_0.4" ||
                printed.value("build_plate", nlohmann::json::object())
                       .value("faces", nlohmann::json::array()).empty()) {
                topoFail("print_setup", "the preset or picked min_z bed face was not retained");
                return;
            }
            if (!validateActive("setup_blocked")) return;
            topoReport["structural_setup"] = sm->activeTopologyStudy();
            std::printf("topology: structural setup configured (SIMP, compliance, 35%% solid, "
                        "PLA, FDM 0.4, min_z bed face, 5 mm tet4)\n");
            std::fflush(stdout);
            shoot("topology_02_regions.png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 5:
            st.simManager->handleCommand(st, "sim_topology_boundaries");
            shoot("topology_03_boundaries.png", kSettle * 2);
            ++topoStep;
            return;
        case 6:
            st.simManager->handleCommand(st, "sim_topology_manufacturing");
            shoot("topology_04_manufacturing.png", kSettle * 2);
            ++topoStep;
            return;
        case 7:
            st.simManager->handleCommand(st, "sim_topology_setup");
            shoot("topology_05_setup_" + topoMethodA + ".png", kSettle * 2);
            ++topoStep;
            return;
        case 8:
            if (st.activeScalarField != "overhang") {
                topoFieldBeforePrint = st.activeScalarField;
                st.activeScalarField = "overhang";
            }
            launchRun(topoMethodA.c_str());
            return;
        case 9:
            waitRun(topoMethodA, "topology_06_result_" + topoMethodA + ".png");
            return;
        case 10: {  // second method on the same setup; unsupported keys erased, never sent
            SimManager* sm = st.simManager;
            if (st.rpc.busy()) return;
            // First pass: the SIMP result (overhang field) in a tall window so the results
            // panel shows Manufacturing (DFAM) and the print-surface export rows; second pass
            // restores the window and the previous field.
            static int printShot = 0;
            if (printShot == 0) {
                glfwSetWindowSize(st.win, 1340, 2400);
                sm->handleCommand(st, "sim_results");
                shoot("topology_06b_print_" + topoMethodA + ".png", kSettle * 3);
                printShot = 1;
                return;
            }
            if (printShot == 1) {
                glfwSetWindowSize(st.win, 1340, 838);
                st.activeScalarField = topoFieldBeforePrint;
                printShot = 2;
            }
            const topo::Capabilities& caps = sm->topologyCapabilities();
            nlohmann::json t = sm->activeTopologyStudy().value("topology", nlohmann::json::object());
            const topo::SwitchResult sw = topo::selectCombination(t, caps, topoMethodB, "structural");
            if (!sw.applied) { topoFail("method_switch", sw.refusal); return; }
            const topo::Combination* comb = caps.find(topoMethodB, "structural");
            if (topoMethodB == "beso" && comb->offersKey("beso_evolution_rate"))
                t["beso_evolution_rate"] = 0.04;  // reaches 35 % well inside 80 iterations
            for (const auto& k : sw.removedKeys) t[k] = nullptr;
            std::string removed;
            for (const auto& k : sw.removedKeys) removed += (removed.empty() ? "" : ",") + k;
            if (!sm->configureActiveTopology(st, {{"topology", t}})) {
                topoFail("configure_variant", "configureActiveTopology refused " + topoMethodB);
                return;
            }
            if (!validateActive("variant_blocked")) return;
            std::printf("topology: switched to %s (objective %s -> %s, removed keys [%s])\n",
                        topoMethodB.c_str(), sw.objectiveBefore.c_str(), sw.objectiveAfter.c_str(),
                        removed.c_str());
            std::fflush(stdout);
            sm->handleCommand(st, "sim_topology_setup");
            shoot("topology_07_setup_" + topoMethodB + ".png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 11:
            launchRun(topoMethodB.c_str());
            return;
        case 12:
            waitRun(topoMethodB, "topology_08_result_" + topoMethodB + ".png");
            return;
        case 13: {  // retained-run comparison from the study itself
            SimManager* sm = st.simManager;
            const nlohmann::json runs =
                sm->activeTopologyStudy().value("runs", nlohmann::json::array());
            if (!runs.is_array() || runs.size() < 2) {
                topoFail("compare_runs", "the study retains " +
                                             std::to_string(runs.is_array() ? runs.size() : 0) +
                                             " run(s); two are required");
                return;
            }
            const auto rows = topo::compareRuns(runs[runs.size() - 2], runs[runs.size() - 1]);
            nlohmann::json out = nlohmann::json::array();
            int comparable = 0;
            for (const auto& d : rows) {
                const bool finite = d.comparable && std::isfinite(d.baseline) &&
                                    std::isfinite(d.selected);
                if (finite) ++comparable;
                out.push_back({{"key", d.key}, {"label", d.label}, {"units", d.units},
                               {"baseline", std::isfinite(d.baseline) ? nlohmann::json(d.baseline)
                                                                      : nlohmann::json()},
                               {"selected", std::isfinite(d.selected) ? nlohmann::json(d.selected)
                                                                      : nlohmann::json()},
                               {"comparable", d.comparable}, {"note", d.note}});
                std::printf("topology: compare %-28s %g -> %g %s%s%s\n", d.label.c_str(),
                            d.baseline, d.selected, d.units.c_str(), d.note.empty() ? "" : " — ",
                            d.note.c_str());
            }
            std::fflush(stdout);
            topoReport["compare"] = out;
            if (comparable == 0) {
                topoFail("compare_empty", "no finite comparable metric between the two runs");
                return;
            }
            sm->handleCommand(st, "sim_topology_compare");
            shoot("topology_09_compare.png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 14: {  // save, reopen an independent copy through the engine, check retention
            SimManager* sm = st.simManager;
            if (st.rpc.busy() || sm->topologyRunActive()) return;  // the run record is persisting
            const std::string savePath = topoSavePath + "/topology_bracket.step+";
            const std::string copyPath = topoSavePath + "/topology_bracket_reopen.step+";
            std::string err;
            if (!st.docMirror.save(st.worker, savePath, err)) { topoFail("save", err); return; }
            std::error_code ec;
            std::filesystem::copy_file(savePath, copyPath,
                                       std::filesystem::copy_options::overwrite_existing, ec);
            if (ec) { topoFail("save_copy", ec.message()); return; }
            nlohmann::json opened;
            if (!st.worker.call("doc_open", {{"path", copyPath}}, opened, err) ||
                !opened.is_object() || opened.contains("code")) {
                topoFail("reopen", err.empty() ? opened.dump() : err);
                return;
            }
            const std::string did = opened.value("doc_id", std::string());
            if (did.empty() || did == st.docMirror.docId()) {
                topoFail("reopen", "doc_open of the saved copy returned no independent document");
                return;
            }
            nlohmann::json list;
            const bool listed = st.worker.call("sim_study_list", {{"doc_id", did}}, list, err, 15000) &&
                                list.is_object() && !list.contains("code");
            nlohmann::json reopenedTree;
            const bool treeRead = st.worker.call("doc_tree", {{"doc_id", did}}, reopenedTree, err);
            nlohmann::json closed;
            std::string cerr;
            st.worker.call("doc_close", {{"doc_id", did}}, closed, cerr);
            if (!listed) { topoFail("reopen_studies", err.empty() ? list.dump() : err); return; }
            if (!treeRead || !reopenedTree.contains("tree")) {
                topoFail("reopen_source", err.empty() ? reopenedTree.dump() : err);
                return;
            }
            const std::string reopenedKey = topo::geometryKey(reopenedTree["tree"]);
            nlohmann::json found;
            for (const auto& s : list.value("studies", nlohmann::json::array()))
                if (s.is_object() && s.value("id", std::string()) == topoStudyId) found = s;
            if (!found.is_object() || found.value("type", std::string()) != "topology") {
                topoFail("reopen_study_missing", "study " + topoStudyId +
                                                     " is not a topology study in the reopened file");
                return;
            }
            const nlohmann::json runs = found.value("runs", nlohmann::json::array());
            for (const auto& id : topoRunIds) {
                bool present = false;
                for (const auto& r : runs)
                    if (r.is_object() && r.value("run_id", std::string()) == id.get<std::string>())
                        present = true;
                if (!present) {
                    topoFail("reopen_run_missing", "run " + id.get<std::string>() +
                                                       " was not retained by save/reopen");
                    return;
                }
            }
            int artifacts = 0;
            nlohmann::json missing = nlohmann::json::array();
            for (const auto& r : runs) {
                if (r.value("source", nlohmann::json::object())
                        .value("geometry_key", std::string()) != reopenedKey) {
                    topoFail("reopen_stale", "unchanged reopened geometry invalidated a retained run");
                    return;
                }
                const nlohmann::json arts = r.is_object() && r.contains("result")
                    ? r["result"].value("artifacts", nlohmann::json::object())
                    : nlohmann::json::object();
                if (!arts.is_object()) continue;
                for (auto it = arts.begin(); it != arts.end(); ++it) {
                    const std::string p = it->is_object() ? it->value("path", std::string())
                                                          : std::string();
                    if (p.empty()) continue;
                    ++artifacts;
                    std::error_code fe;
                    if (!std::filesystem::exists(p, fe)) missing.push_back(p);
                }
            }
            topoReport["reopen"] = {{"saved", savePath}, {"reopened_copy", copyPath},
                                    {"runs_retained", runs.size()}, {"artifacts", artifacts},
                                    {"artifacts_missing", missing},
                                    {"reopened_doc", did}, {"live_doc", st.docMirror.docId()},
                                    {"source_current", true}};
            std::printf("topology: save/reopen kept %zu run(s), %d artifact file(s), %zu missing\n",
                        runs.size(), artifacts, missing.size());
            std::fflush(stdout);
            if (!missing.empty()) {
                topoFail("artifacts_missing", "retained artifact files are gone: " + missing.dump());
                return;
            }
            topoSettle = 2; ++topoStep;
            return;
        }
        case 15: {  // thermal boundary editor on a real conduction setup (inspected, not run)
            SimManager* sm = st.simManager;
            if (st.rpc.busy()) return;
            const topo::Capabilities& caps = sm->topologyCapabilities();
            nlohmann::json cfg = topo::newTopologyConfig(caps, "thermal");
            if (cfg.empty()) { topoFail("thermal_unsupported", "no method supports thermal"); return; }
            const int before = sm->studyCount();
            sm->handleCommand(st, "sim_new_topology");
            if (sm->studyCount() != before + 1) {
                topoFail("thermal_study", "sim_new_topology created no study: " + st.status);
                return;
            }
            cfg["volume_fraction"] = 0.4;
            const nlohmann::json aluminum = {
                {"name", "Aluminum 6061-T6"}, {"E_mpa", 68900.0}, {"nu", 0.33},
                {"density_g_cm3", 2.70}, {"k_w_mk", 167.0}, {"alpha_per_k", 2.36e-5},
                {"cp_j_kgk", 896.0}};
            if (!sm->configureActiveTopology(st, {{"name", "Heat spreader topology"},
                                                  {"material", aluminum}, {"mesh_order", 1},
                                                  {"mesh_target_mm", 5.0}, {"topology", cfg}})) {
                topoFail("thermal_configure", "configureActiveTopology refused the thermal setup");
                return;
            }
            pickFace("min_x");
            sm->handleCommand(st, "sim_fixture");  // thermal physics: a temperature fixture
            st.selection.clear();
            nlohmann::json thermal =
                sm->activeTopologyStudy().value("thermal", nlohmann::json::object());
            nlohmann::json fixtures = thermal.value("fixtures", nlohmann::json::array());
            if (!lastEntryHasFaces(fixtures)) {
                topoFail("thermal_pick", "the picked face did not land in thermal.fixtures");
                return;
            }
            fixtures.back()["temperature_c"] = 25.0;
            const nlohmann::json loads = nlohmann::json::array({
                {{"faces", nlohmann::json::array({faceSel("max_x")})}, {"type", "power"},
                 {"power_w", 20.0}},
                {{"faces", nlohmann::json::array({faceSel("max_z")})}, {"type", "convection"},
                 {"h_w_m2k", 15.0},
                 {"ambient_c", 25.0}}});
            if (!sm->configureActiveTopology(
                    st, {{"thermal", {{"reference_temperature_c", 25.0},
                                      {"fixtures", fixtures}, {"loads", loads}}}})) {
                topoFail("thermal_configure", "configureActiveTopology refused the thermal BCs");
                return;
            }
            if (!validateActive("thermal_blocked")) return;
            topoReport["thermal_setup"] = sm->activeTopologyStudy();
            sm->handleCommand(st, "sim_topology_boundaries");
            std::printf("topology: thermal study: 25 C fixture (-X), 20 W (+X), h=15 convection (top)\n");
            std::fflush(stdout);
            shoot("topology_10_thermal_boundaries.png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 16: {  // flow boundary editor on a real low-Re channel setup (inspected, not run)
            SimManager* sm = st.simManager;
            if (st.rpc.busy()) return;
            const topo::Capabilities& caps = sm->topologyCapabilities();
            nlohmann::json cfg = topo::newTopologyConfig(caps, "flow");
            if (cfg.empty()) { topoFail("flow_unsupported", "no method supports flow"); return; }
            const int before = sm->studyCount();
            sm->handleCommand(st, "sim_new_topology");
            if (sm->studyCount() != before + 1) {
                topoFail("flow_study", "sim_new_topology created no study: " + st.status);
                return;
            }
            cfg["volume_fraction"] = 0.4;  // flow budgets the FLUID phase
            const nlohmann::json flow = {
                {"viscosity_pa_s", 1.0e-3}, {"density_kg_m3", 998.0},
                {"inlets", nlohmann::json::array({{{"faces", nlohmann::json::array({faceSel("min_x")})},
                                                   {"velocity_m_s", {0.001, 0.0, 0.0}}}})},
                {"outlets", nlohmann::json::array({{{"faces", nlohmann::json::array({faceSel("max_x")})},
                                                    {"pressure_pa", 0.0}}})},
                {"fluid_k_w_mk", 0.6}, {"fluid_cp_j_kgk", 4182.0}};
            if (!sm->configureActiveTopology(st, {{"name", "Coolant channel topology"},
                                                  {"mesh_order", 1}, {"mesh_target_mm", 5.0},
                                                  {"topology", cfg}, {"flow", flow}})) {
                topoFail("flow_configure", "configureActiveTopology refused the flow setup");
                return;
            }
            pickFace("max_z");
            sm->handleCommand(st, "sim_fixture");  // flow physics: a no-slip wall
            st.selection.clear();
            const nlohmann::json walls = sm->activeTopologyStudy()
                                             .value("flow", nlohmann::json::object())
                                             .value("walls", nlohmann::json::array());
            if (!lastEntryHasFaces(walls)) {
                topoFail("flow_pick", "the picked face did not land in flow.walls");
                return;
            }
            if (!validateActive("flow_blocked")) return;
            topoReport["flow_setup"] = sm->activeTopologyStudy();
            sm->handleCommand(st, "sim_topology_boundaries");
            std::printf("topology: flow study: 1 mm/s inlet (-X), 0 Pa outlet (+X), wall (top)\n");
            std::fflush(stdout);
            shoot("topology_11_flow_boundaries.png", kSettle * 2);
            ++topoStep;
            return;
        }
        case 17: {  // back to the optimized bracket's comparison
            SimManager* sm = st.simManager;
            if (st.rpc.busy()) return;
            if (!sm->selectStudyById(st, topoStudyId)) {
                topoFail("reselect", "could not reselect study " + topoStudyId);
                return;
            }
            st.ribbonTab = AppState::RT_Topology;
            sm->handleCommand(st, "sim_topology_compare");
            topoSettle = kSettle * 2;
            ++topoStep;
            return;
        }
        default:
            topoReport["ok"] = true;
            topoDone = true;
            topoCapture = topoSavePath + "/topology_12_final.png";
            std::printf("topology: scenario complete (%s vs %s, %zu retained runs verified)\n",
                        topoMethodA.c_str(), topoMethodB.c_str(), topoRunIds.size());
            std::fflush(stdout);
            return;
    }
}

}  // namespace ui
