// Scripted screenshot/test scenarios — implementation. Mechanically extracted from
// app.cpp's run_gui frame loop (behavior-preserving; see scenarios.h).
#include "scenarios.h"

#include "gl_compat.h"
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <vector>

#include "../core/app_state.h"
#include "../renderer.h"  // T-G15 iconpipe: transparent thumbnail render + FBO readback
#include "../render/splat_renderer.h"  // RE-28: 3DGS splat renderer
#include "cam/cam_panel.h"  // camLoadJob (CAM copilot fast-path scenario)
#include "asm_panels.h"
#include "evaluate_dialogs.h"
#include "manufacture/manufacture_panel.h"
#include "property_manager.h"
#include "sim_manager.h"
#include "sketch_mode.h"
#include "sketch3d_mode.h"

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

}  // namespace

namespace ui {

void ScenarioRunner::tick(AppState& st) {
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
            if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
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
                    std::printf("m1: cache-assert box f1 ms before=%.2f after=%.2f (cached=%s); "
                                "fillet ms before=%.2f after=%.2f (recompiled=%s)\n",
                                m1BoxMsBeforeEdit, boxAfter,
                                (boxAfter == m1BoxMsBeforeEdit ? "yes" : "NO"),
                                m1FilletMsBeforeEdit, filletAfter,
                                (filletAfter != m1FilletMsBeforeEdit ? "yes" : "no"));
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
                    // Reopen doesn't return an aicm; force a rebuild to refresh the viewport.
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
                default:
                    st.sketchShotDone = true;
                    std::printf("sketch: scenario complete\n"); std::fflush(stdout);
                    break;
            }
            if (skStep <= 2) ++skStep;
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
    // new part -> box 60x40x10 -> static study: fix the -X end face, 100 N -Z on the +X end,
    // steel (index 0), density 0.4 -> wait for completion (cap ~90 s) -> von Mises colormap.
    if (st.wantFea && st.workerStarted && !st.feaShotDone) {
        auto* sim = st.simClient;
        if (st.simManager) st.simManager->tick(st);  // pump so completion lands while we wait
        if (feaSettle > 0) { --feaSettle; }
        else switch (feaStep) {
            case 0: {  // New part + box
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
                std::printf("fea: box ok=%d rev=%d\n", (int)r.ok, r.revision);
                std::fflush(stdout);
                st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                feaSettle = 3; ++feaStep; break;
            }
            case 1: {  // Static study + resolved fixture/load faces (-X end / +X end)
                st.activeWorkspace = AppState::Workspace::Simulate;
                st.leftPaneTab = AppState::LP_Simulation;
                st.showRibbon = true;
                st.showSimulationTools = true;
                st.ribbonTab = AppState::RT_Simulation;
                if (st.simManager) {
                    st.simManager->handleCommand(st, "sim_new_static");
                    st.simManager->configureActive(st, 0.2f, "steel_1018");
                }
                auto faceAtExtremeX = [&](bool wantMax) -> nlohmann::json {
                    nlohmann::json best; double bestC = wantMax ? -1e30 : 1e30;
                    for (int tid = 1; tid <= 12; ++tid) {
                        std::string se;
                        std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                        if (s.empty()) continue;
                        try {
                            auto o = nlohmann::json::parse(s);
                            if (!o.contains("centroid")) continue;
                            double cx = o["centroid"][0].get<double>();
                            if ((wantMax && cx > bestC) || (!wantMax && cx < bestC)) { bestC = cx; best = o; }
                        } catch (...) {}
                    }
                    return best;
                };
                nlohmann::json fixFace = faceAtExtremeX(false);
                nlohmann::json loadFace = faceAtExtremeX(true);
                // Inject the resolved selectors via the selection -> well capture path.
                st.selection.clear();
                { core::SelItem it; it.selector = fixFace.dump(); it.pending_resolve = false;
                  st.selection.mutableItems().push_back(it); }
                if (st.simManager) st.simManager->handleCommand(st, "sim_fixture");
                st.selection.clear();
                { core::SelItem it; it.selector = loadFace.dump(); it.pending_resolve = false;
                  st.selection.mutableItems().push_back(it); }
                if (st.simManager) st.simManager->handleCommand(st, "sim_load_force");
                st.selection.clear();
                std::printf("fea: faces fixCx=%.1f loadCx=%.1f\n",
                            fixFace.contains("centroid") ? fixFace["centroid"][0].get<double>() : 0.0,
                            loadFace.contains("centroid") ? loadFace["centroid"][0].get<double>() : 0.0);
                std::fflush(stdout);
                feaSettle = 2; ++feaStep; break;
            }
            case 2: {  // Run
                if (st.simManager) st.simManager->handleCommand(st, "sim_run");
                std::printf("fea: study launched (density 0.4, steel, 100 N)\n");
                std::fflush(stdout);
                feaWaitFrames = 0; ++feaStep; break;
            }
            case 3: {  // Wait for completion (cap ~90 s)
                ++feaWaitFrames;
                if (st.colormapOn || feaWaitFrames > 5400) {
                    std::printf("fea: result ready colormap=%d legend=%d (%d frames)\n",
                                (int)st.colormapOn, (int)st.legendOn, feaWaitFrames);
                    std::fflush(stdout);
                    ++feaStep;
                }
                break;
            }
            default:
                st.feaShotDone = true;
                std::printf("fea: scenario complete\n");
                std::fflush(stdout);
                break;
        }
        (void)sim;
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
    if (st.wantFlow && st.workerStarted && !st.flowShotDone) {
        if (st.simManager) st.simManager->tick(st);  // pump so completion lands while we wait
        if (flowSettle > 0) { --flowSettle; }
        else switch (flowStep) {
            case 0: {  // New part + a slender bar (stand-in fuselage/hull)
                std::string err;
                st.docMirror.newPart(st.worker, err);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Hull"},{"params",{
                        {"length",{{"value",80.0},{"expr",nullptr}}},
                        {"width", {{"value",20.0},{"expr",nullptr}}},
                        {"height",{{"value",20.0},{"expr",nullptr}}}},
                    },{"centered",true}}}});
                auto r = st.docMirror.applyOps(st.worker, ops);
                if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                std::printf("flow: bar ok=%d rev=%d\n", (int)r.ok, r.revision);
                std::fflush(stdout);
                st.yaw = 40; st.pitch = 22; st.zoom = 1.0f;
                flowSettle = 3; ++flowStep; break;
            }
            case 1: {  // Flow study: water, 10 deg AoA, coarse preset
                st.activeWorkspace = AppState::Workspace::Simulate;
                st.leftPaneTab = AppState::LP_Simulation;
                st.showRibbon = true;
                st.showSimulationTools = true;
                st.ribbonTab = AppState::RT_Simulation;
                if (st.simManager) {
                    st.simManager->handleCommand(st, "sim_new_flow");
                    st.simManager->configureActiveFlow(st, /*fluid=water*/1, /*aoa*/10.0f,
                                                       /*sideslip*/0.0f, /*preset=coarse*/0);
                }
                std::printf("flow: study configured (water, AoA 10, coarse)\n");
                std::fflush(stdout);
                flowSettle = 2; ++flowStep; break;
            }
            case 2: {  // Run
                if (st.simManager) st.simManager->handleCommand(st, "sim_run");
                std::printf("flow: study launched\n");
                std::fflush(stdout);
                flowWaitFrames = 0; ++flowStep; break;
            }
            case 3: {  // Wait for completion (cap ~180 s — coarse LBM)
                ++flowWaitFrames;
                const bool numericReady =
                    st.simManager && st.simManager->flowHasResult();
                if (numericReady) {
                    std::printf("flow: result ready numeric=1 colormap=%d legend=%d (%d frames)\n",
                                (int)st.colormapOn, (int)st.legendOn, flowWaitFrames);
                    std::fflush(stdout);
                    ++flowStep;
                } else if (flowWaitFrames > 10800) {
                    std::fprintf(stderr,
                                 "flow: FAILED — numeric result did not arrive within 180 s\n");
                    st.flowShotDone = true;
                }
                break;
            }
            default:
                st.flowShotDone = true;
                std::printf("flow: scenario complete\n");
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

    // ---------- T-G15 icon pipeline (CHUDWARE_SHOT=iconpipe) ----------
    runIconPipe(st);

    // ---------- RE-28 3DGS splat scenario (CHUDWARE_SHOT=reveng_splat) ----------
    runRevengSplat(st);

    // ---------- workspaces scenario (CHUDWARE_SHOT=workspaces) ----------
    runWorkspaces(st);

    // ---------- motion scenario (CHUDWARE_SHOT=motion) ----------
    runMotion(st);
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

// RE-28 3DGS splat scenario (CHUDWARE_SHOT=reveng_splat). Self-contained: loads the canned gaussian
// .ply fixture and draws it through the real render::SplatRenderer into its OWN offscreen FBO, reads
// it back, and writes the PNG directly — the iconpipe drain pattern (see runIconPipe lines ~1040).
//
// Why an own FBO and not the backbuffer: this runner is called from tick() BEFORE ImGui::NewFrame
// and the frame's own glClear, so anything drawn to the default framebuffer here is wiped before the
// backbuffer capture in endFrame(). Rendering to a private FBO + glReadPixels sidesteps that AND the
// swap/present path entirely, so it completes headless in one synchronous tick (no settle frames).
// Purpose: prove the loader + the real GL billboard/blend draw path run without crashing headless.
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
    std::error_code ec; std::filesystem::create_directories(rsSavePath, ec);

    // Load the committed fixture (host/tests/fixtures/splat_tiny.ply, repo-relative).
    rsRenderer = std::make_unique<render::SplatRenderer>();
    std::string plyPath = base + "/host/tests/fixtures/splat_tiny.ply";
    std::string err;
    rsLoaded = rsRenderer->load(plyPath, err);
    std::printf("reveng_splat: load %s ok=%d count=%zu%s%s\n", plyPath.c_str(),
                (int)rsLoaded, rsLoaded ? rsRenderer->count() : 0,
                rsLoaded ? "" : " err=", rsLoaded ? "" : err.c_str());
    std::fflush(stdout);

    const int W = 640, H = 480;

    // Private offscreen FBO: color (RGBA8) + depth (SplatRenderer::draw enables GL_DEPTH_TEST).
    GLuint fbo = 0, colorTex = 0, depthRbo = 0;
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glGenTextures(1, &colorTex);
    glBindTexture(GL_TEXTURE_2D, colorTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex, 0);
    glGenRenderbuffers(1, &depthRbo);
    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, W, H);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, depthRbo);
    GLenum fbStatus = glCheckFramebufferStatus(GL_FRAMEBUFFER);

    std::vector<unsigned char> rgba(W * H * 4, 0);
    size_t opaque = 0;
    if (fbStatus == GL_FRAMEBUFFER_COMPLETE) {
        glViewport(0, 0, W, H);
        glClearColor(0.10f, 0.11f, 0.13f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

        // Camera framing the fixture (gaussians span z in [-5,20], centered ~7.5).
        Vec3 center{0.0f, 0.0f, 7.5f};
        Vec3 eye{22.0f, 18.0f, 38.0f};
        Vec3 up{0.0f, 0.0f, 1.0f};
        float aspect = (float)W / (float)H;
        Mat4 proj = perspective(35.0f * 3.14159265f / 180.0f, aspect, 1.0f, 500.0f);
        Mat4 view = lookAt(eye, center, up);
        Renderer::CameraXform cam;
        cam.mvp = mul(proj, view);
        cam.eye = eye; cam.center = center; cam.radius = 20.0f;
        cam.ssWidth = W; cam.ssHeight = H;
        rsRenderer->draw(cam);  // real SplatRenderer path: billboards + back-to-front alpha blend

        glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
        for (size_t i = 3; i < rgba.size(); i += 4) if (rgba[i] > 8) ++opaque;
    } else {
        std::printf("reveng_splat: FBO incomplete (0x%x) — cannot render\n", fbStatus);
        std::fflush(stdout);
    }

    // Restore the default framebuffer so the normal frame loop is unaffected.
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glDeleteFramebuffers(1, &fbo);
    glDeleteTextures(1, &colorTex);
    glDeleteRenderbuffers(1, &depthRbo);

    // glReadPixels is bottom-up; flip to top-down for the PNG.
    std::vector<unsigned char> flip(W * H * 4);
    for (int y = 0; y < H; ++y)
        std::memcpy(&flip[(size_t)(H - 1 - y) * W * 4], &rgba[(size_t)y * W * 4], (size_t)W * 4);

    rsCapture = rsSavePath + "/reveng_splat_01.png";
    stbi_write_png(rsCapture.c_str(), W, H, 4, flip.data(), W * 4);
    std::printf("reveng_splat: wrote %s (%dx%d, %zu opaque px, %zu gaussians)\n",
                rsCapture.c_str(), W, H, opaque, rsLoaded ? rsRenderer->count() : 0);
    std::fflush(stdout);

    rsDone = true;  // one-tick drain complete; frame loop exits at the generic shot gate (frame 6)
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

// W17 engineering-drawing scenario (CHUDWARE_SHOT=drawing). Build a plate with a through hole, Make
// Drawing (standard views), then exercise the new detailing engine: Model Items auto-dimensioning,
// a smart hole callout, a critical ±toleranced dimension, and the title block — with the contextual
// View Layout / Annotation ribbon visible. One capture (drawing_01_detailed.png).
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
                {"type","cylinder"},{"name","Hole"},{"params",{
                    {"radius",{{"value",6.0},{"expr",nullptr}}},
                    {"height",{{"value",16.0},{"expr",nullptr}}},
                    {"centered",true},{"mode","cut"}}}}}});
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
        case 2: {  // Detailing: model items + hole callout + critical toleranced dim + title block
            if (!st.activeTabIsDrawing()) {
                std::printf("drawing: NO drawing tab after makeDrawing\n"); std::fflush(stdout);
                st.drawingShotDone = true; break;
            }
            AppState::DocTab& tab = st.docTabs[st.activeTab];
            // Find a view with a closed projected circle (the hole in the top view).
            std::string circVid; double cx = 0, cy = 0; bool haveCirc = false;
            if (tab.drawingDoc.contains("sheets") && tab.drawingDoc["sheets"].is_array() &&
                !tab.drawingDoc["sheets"].empty()) {
                for (const auto& v : tab.drawingDoc["sheets"][0].value("views",
                                                                       nlohmann::json::array())) {
                    for (const auto& a : v.value("arcs", nlohmann::json::array()))
                        if (a.value("closed", false) && a.contains("center") &&
                            a["center"].is_array() && a["center"].size() == 2) {
                            circVid = vid_of(v);
                            cx = a["center"][0].get<double>(); cy = a["center"][1].get<double>();
                            haveCirc = true; break;
                        }
                    if (haveCirc) break;
                }
            }
            std::string mvid = circVid;
            if (mvid.empty() && tab.drawingDoc.contains("sheets")) {
                const auto& vs = tab.drawingDoc["sheets"][0].value("views", nlohmann::json::array());
                if (vs.is_array() && !vs.empty()) mvid = vid_of(vs[0]);
            }
            nlohmann::json res;
            if (!mvid.empty())
                st.worker.call("drawing_model_items",
                               {{"doc_id", tab.drawingDocId}, {"view_id", mvid}}, res, err);
            if (haveCirc) {
                nlohmann::json r2;
                st.worker.call("drawing_add_callout",
                               {{"doc_id", tab.drawingDocId}, {"view_id", circVid}, {"kind", "hole"},
                                {"a", nlohmann::json::array({cx, cy})}}, r2, err);
            }
            nlohmann::json r3;
            st.worker.call("drawing_set_titleblock",
                           {{"doc_id", tab.drawingDocId}, {"sheet_id", "s1"},
                            {"fields", {{"title", "PLATE"}, {"drawn_by", "Chudware"},
                                        {"material", "AL 6061"}, {"scale", "1:1"}, {"rev", "A"}}}},
                           r3, err);
            st.refreshDrawingTab(st.activeTab);
            // Make the first linear dim a critical +/-0.1 tolerance.
            std::string tvid, tann;
            if (tab.drawingDoc.contains("sheets"))
                for (const auto& v : tab.drawingDoc["sheets"][0].value("views",
                                                                       nlohmann::json::array())) {
                    for (const auto& a : v.value("annotations", nlohmann::json::array()))
                        if (a.value("type", std::string()) == "linear_dim") {
                            tvid = vid_of(v); tann = a.value("id", std::string()); break;
                        }
                    if (!tann.empty()) break;
                }
            if (!tann.empty()) {
                nlohmann::json r4;
                st.worker.call("drawing_set_annotation",
                               {{"doc_id", tab.drawingDocId}, {"view_id", tvid}, {"ann_id", tann},
                                {"tol_type", "symmetric"}, {"tol_upper", 0.1}, {"precision", 2},
                                {"critical", true}}, r4, err);
            }
            st.refreshDrawingTab(st.activeTab);
            tab.drawNeedFit = true;
            int nann = 0;
            if (tab.drawingDoc.contains("sheets"))
                for (const auto& v : tab.drawingDoc["sheets"][0].value("views",
                                                                       nlohmann::json::array()))
                    nann += (int)v.value("annotations", nlohmann::json::array()).size();
            std::printf("drawing: detail done circle=%d critical_dim=%s annotations=%d\n",
                        (int)haveCirc, tann.empty() ? "none" : tann.c_str(), nann);
            std::fflush(stdout);
            // Copilot one-shot path: agent_make_drawing builds a complete drawing (views +
            // auto-dims + hole callouts + title block) from the source part in a single call.
            {
                nlohmann::json r5;
                nlohmann::json spec = {
                    {"views", nlohmann::json::array({"front", "top", "right", "iso"})},
                    {"model_items", true}, {"hole_callouts", true},
                    {"title", {{"title", "COPILOT"}, {"material", "STEEL"}, {"rev", "A"}}}};
                if (st.worker.call("drawing_make",
                                   {{"source_doc_id", tab.sourceDocId}, {"spec", spec}}, r5, err) &&
                    r5.is_object() && !r5.contains("code")) {
                    int v5 = 0, a5 = 0;
                    if (r5.contains("drawing") && r5["drawing"].contains("sheets") &&
                        r5["drawing"]["sheets"].is_array() && !r5["drawing"]["sheets"].empty())
                        for (const auto& v : r5["drawing"]["sheets"][0].value("views",
                                                                        nlohmann::json::array())) {
                            ++v5; a5 += (int)v.value("annotations", nlohmann::json::array()).size();
                        }
                    std::printf("drawing: drawing_make one-shot views=%d annotations=%d %s\n",
                                v5, a5, (v5 >= 3 && a5 > 0) ? "PASS" : "CHECK");
                } else {
                    std::printf("drawing: drawing_make FAILED: %s\n", err.c_str());
                }
                std::fflush(stdout);
            }
            // In-app copilot trigger: a chat "make an engineering drawing of this part" is
            // intercepted by the agent fast-path (no provider needed) and builds a drawing.
            {
                nlohmann::json r6;
                bool ok6 = st.worker.call(
                    "agent_run_v2",
                    {{"request", "make an engineering drawing of this part"},
                     {"doc_id", tab.sourceDocId}},
                    r6, err);
                bool made = ok6 && r6.is_object() && r6.value("created_drawing", false);
                std::printf("drawing: agent fast-path created_drawing=%d doc=%s %s\n", (int)made,
                            made ? r6.value("drawing_doc_id", std::string()).c_str() : "-",
                            made ? "PASS" : "CHECK");
                std::fflush(stdout);
            }
            dwgCapture = dwgSavePath + "/drawing_01_detailed.png"; dwgSettle = kSettle; ++dwgStep;
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
//   2: Add an angle mate between two faces (the motion driver mate).
//   3: Open Simulate workspace, create Motion study, set leftPaneTab = LP_Simulation.
//      Capture motion_t0.png (the setup/tree view before solve).
//   4: Run the motion study (sim_run — synchronous). If solve fails, report and stop.
//      Capture motion_t1.png (frame 0, bodies at start pose).
//   5: Advance to mid-frame via applyMotionFramePublic, capture motion_t2.png.
//      Bodies MUST be at a different position from t1 to prove live playback.
//
// Note: the motion scenario requires a real assembly with at least one drivable mate.
// If the worker's simulate_motion RPC fails (no kinematic solver available or no mate
// found), the scenario reports "motion solve FAILED" and still writes t0 + t1 as
// partial evidence — it does NOT fake the body-position difference.
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
            // Switch to Simulate workspace, create Motion study.
            st.activeWorkspace = AppState::Workspace::Simulate;
            st.leftPaneTab     = AppState::LP_Simulation;
            if (st.simManager) {
                st.simManager->handleCommand(st, "sim_new_motion");
                st.simManager->configureActiveMotion(st, 60.0f, 100.0f, 24);
                std::printf("motion: Motion study created\n"); std::fflush(stdout);
            } else {
                std::printf("motion: WARNING no simManager\n"); std::fflush(stdout);
            }
            mtCapture = mtSavePath + "/motion_t0.png";
            mtSettle  = kSettle; ++mtStep; break;
        }
        case 4: {
            // Run the motion study synchronously.
            if (st.simManager) {
                st.simManager->handleCommand(st, "sim_run");
                mtRunOk = st.simManager->motionHasResult();
                std::printf("motion: sim_run complete, hasResult=%d, frames=%d\n",
                            (int)mtRunOk, mtRunOk ? st.simManager->motionFrameCount() : 0);
                std::fflush(stdout);
            }
            // Capture frame 0 (bodies at start pose) regardless of solve outcome.
            mtCapture = mtSavePath + "/motion_t1.png";
            mtSettle  = kSettle; ++mtStep; break;
        }
        case 5: {
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

}  // namespace ui
