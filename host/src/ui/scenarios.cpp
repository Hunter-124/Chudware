// Scripted screenshot/test scenarios — implementation. Mechanically extracted from
// app.cpp's run_gui frame loop (behavior-preserving; see scenarios.h).
#include "scenarios.h"

#include <glad/gl.h>
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
#include <vector>

#include "../core/app_state.h"
#include "../core/jobs.h"  // launchAgentRunV2 (C22b activity scenario)
#include "activity_panel.h"
#include "asm_panels.h"
#include "evaluate_dialogs.h"
#include "property_manager.h"
#include "sim_manager.h"
#include "sketch_mode.h"

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
    // ---------- C17 M1 integration scenario (AI_CAD_SHOT=m1) ----------
    // Frame-driven state machine: each step issues scripted ops through the REAL worker
    // (DocMirror), loads the rebuilt mesh, and requests a screenshot into scratch/m1_shots/.
    // Steps: New Part -> Box 60x40x10 -> Fillet r=4 (2 picked vertical edges) -> Shell t=2
    // (remove top face) -> edit Fillet r=8 (cached rebuild assertion) -> save + reopen.
    // The capture itself happens in endFrame() when m1Capture is non-empty.
    if (st.wantM1 && st.workerStarted && !st.m1ShotDone) {
        // Resolve repo-relative scratch dir for screenshots.
        if (!m1DirReady) {
            m1DirReady = true;
            const char* repo = std::getenv("AI_CAD_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            m1SavePath = base + "\\scratch\\m1_shots";
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
                    m1Capture = m1SavePath + "\\m1_01_newpart.png"; m1Settle = kSettle;
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
                    // C32-A material proof: AI_CAD_MATERIAL=<plastic|aluminum|steel|brass|dark>
                    // applies the matching RealView preset to body b1 so a headless shot can be
                    // judged for metallic realism (clear env reflection + tinted fresnel).
                    if (const char* mat = std::getenv("AI_CAD_MATERIAL")) {
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
                    m1Capture = m1SavePath + "\\m1_02_box.png"; m1Settle = kSettle;
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
                    m1Capture = m1SavePath + "\\m1_03_fillet.png"; m1Settle = kSettle;
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
                    m1Capture = m1SavePath + "\\m1_04_shell.png"; m1Settle = kSettle;
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
                    m1Capture = m1SavePath + "\\m1_05_edit.png"; m1Settle = kSettle;
                    break;
                }
                case 5: {  // Save .step+ then reopen; assert tree intact (3 features)
                    st.propertyManager->closeIfActive();  // tree returns for the reopen screenshot
                    std::string sp = m1SavePath + "\\part.step";
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
                    m1Capture = m1SavePath + "\\m1_06_reopen.png"; m1Settle = kSettle;
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

    // ---------- C18 sketch scenario (AI_CAD_SHOT=sketch) ----------
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
            const char* repo = std::getenv("AI_CAD_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            skSavePath = base + "\\scratch\\sketch_shots";
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
                    sketchCapture = skSavePath + "\\sketch_01_enter.png"; skSettle = kSettle;
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
                    sketchCapture = skSavePath + "\\sketch_02_dim.png"; skSettle = kSettle;
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
                    std::printf("sketch: exit + extrude region depth 10 ok=%d\n", (int)exok);
                    std::fflush(stdout);
                    sketchCapture = skSavePath + "\\sketch_03_solid.png"; skSettle = kSettle;
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

    // ---------- C20b assembly scenario (AI_CAD_SHOT=asm) ----------
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
            const char* repo = std::getenv("AI_CAD_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            asmSavePath = base + "\\scratch\\asm_shots";
            asmBasePath = asmSavePath + "\\base.step+";
            asmPegPath  = asmSavePath + "\\peg.step+";
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
                asmCapture = asmSavePath + "\\asm_01_seated.png"; asmSettle = kSettle; ++asmStep; break;
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
                asmCapture = asmSavePath + "\\asm_02_explode.png"; asmSettle = kSettle; ++asmStep; break;
            }
            case 4: {  // interference window
                ap->setExplode(st, false, 0.0f);  // collapse before checking real fit
                ap->closeExplode();               // dismiss the explode window so it isn't on top
                ap->openInterference(st);
                std::printf("asm: interference window open\n"); std::fflush(stdout);
                asmCapture = asmSavePath + "\\asm_03_interference.png"; asmSettle = kSettle; ++asmStep; break;
            }
            default:
                st.asmShotDone = true;
                std::printf("asm: scenario complete (solved z=%.3f)\n", asmSolvedZ);
                std::fflush(stdout);
                break;
        }
    }

    // ---------- C19 FEA scenario (AI_CAD_SHOT=fea) ----------
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

    // ---------- external-flow scenario (AI_CAD_SHOT=flow) ----------
    // new part -> fuselage-ish bar 80x20x20 -> Flow study (water, 10 deg AoA, coarse) -> run ->
    // wait for the result colormap (Cp on the model + streamlines over it) -> capture.
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
                if (st.colormapOn || flowWaitFrames > 10800) {
                    std::printf("flow: result ready colormap=%d legend=%d (%d frames)\n",
                                (int)st.colormapOn, (int)st.legendOn, flowWaitFrames);
                    std::fflush(stdout);
                    ++flowStep;
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

    // ---------- C19 evalmass scenario (AI_CAD_SHOT=evalmass) ----------
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
                ++evalStep; break;
            }
            default:
                st.evalMassShotDone = true;
                std::printf("evalmass: scenario complete\n"); std::fflush(stdout);
                break;
        }
    }

    // --- C22b activity scenario (AI_CAD_SHOT=activity) ---
    if (st.wantActivity && st.workerStarted && !st.activityShotDone && st.frame >= 5) {
        if (!actDirReady) {
            actDirReady = true;
            const char* repo = std::getenv("AI_CAD_REPO");
            std::string base = repo ? std::string(repo) : std::string(".");
            actSavePath = base + "\\scratch\\activity_shots";
            std::error_code ec; std::filesystem::create_directories(actSavePath, ec);  // W1-B (item 12)
        }
        const int kSettle = 4;
        if (actSettle > 0) { --actSettle; }
        else if (actCapture.empty()) {
            std::string aerr;
            switch (actStep) {
                case 0: {  // New part
                    if (!st.docMirror.newPart(st.worker, aerr)) {
                        std::printf("activity: new_part FAILED: %s\n", aerr.c_str());
                        std::fflush(stdout); st.activityShotDone = true; break;
                    }
                    std::printf("activity: new_part doc_id=%s rev=%d\n",
                                st.docMirror.docId().c_str(), st.docMirror.revision());
                    std::fflush(stdout);
                    actSettle = kSettle; ++actStep; break;
                }
                case 1: {  // Launch agent_run_v2
                    std::string req = "a bracket with a filleted top edge";
                    std::strncpy(st.prompt.data(), req.c_str(), st.prompt.size() - 1);
                    st.agentV2 = true;
                    st.agentV2Running = true;
                    activityPanel.clearLive();
                    launchAgentRunV2(st.worker, st.job, req, st.docMirror.docId());
                    std::printf("activity: agent_run_v2 launched: %s\n", req.c_str());
                    std::fflush(stdout);
                    actWaitFrames = 0; ++actStep; break;
                }
                case 2: {  // Wait for run to finish
                    ++actWaitFrames;
                    if (!st.job.running.load() || actWaitFrames > 1800) {
                        std::printf("activity: run done (frames=%d)\n", actWaitFrames);
                        std::fflush(stdout);
                        actSettle = kSettle * 2; ++actStep;
                    }
                    break;
                }
                case 3: {  // Screenshot 1: Activity panel with run tree
                    std::printf("activity: shot1 activity panel\n"); std::fflush(stdout);
                    actCapture = actSavePath + "\\activity_01_run.png";
                    actSettle = kSettle; ++actStep; break;
                }
                case 4: {  // Open Settings > Agents tab
                    st.openSettings = true;
                    st.pendingPromptRole = "modeler";
                    actSettle = kSettle * 2; ++actStep; break;
                }
                case 5: {  // Screenshot 2: Settings Agents tab
                    std::printf("activity: shot2 agents settings tab\n"); std::fflush(stdout);
                    actCapture = actSavePath + "\\activity_02_agents.png";
                    actSettle = kSettle; ++actStep; break;
                }
                default:
                    st.activityShotDone = true;
                    std::printf("activity: scenario complete\n"); std::fflush(stdout);
                    break;
            }
        }
    }
    // --- end C22b activity scenario ---

    // ---------- multi-document scenario (AI_CAD_SHOT=multidoc) ----------
    runMultidoc(st);

    // ---------- W17 engineering-drawing scenario (AI_CAD_SHOT=drawing) ----------
    runDrawing(st);
}

// Multi-document mid-edit verification (AI_CAD_SHOT=multidoc). Same frame-driven step machine +
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
        const char* repo = std::getenv("AI_CAD_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        mdSavePath = base + "\\scratch\\multidoc_shots";
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
            mdCapture = mdSavePath + "\\multidoc_01_A_editing.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_02_B.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_03_A_restored.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_04_B_independent.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_05_C_editing.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_07_A_after_C.png"; ++mdStep; mdSettle = kSettle;
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
            mdCapture = mdSavePath + "\\multidoc_06_C_restored.png"; ++mdStep; mdSettle = kSettle;
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

// W17 engineering-drawing scenario (AI_CAD_SHOT=drawing). Build a plate with a through hole, Make
// Drawing (standard views), then exercise the new detailing engine: Model Items auto-dimensioning,
// a smart hole callout, a critical ±toleranced dimension, and the title block — with the contextual
// View Layout / Annotation ribbon visible. One capture (drawing_01_detailed.png).
void ScenarioRunner::runDrawing(AppState& st) {
    if (!(st.wantDrawing && st.workerStarted && !st.drawingShotDone && st.frame >= 5)) return;
    if (!dwgDirReady) {
        dwgDirReady = true;
        const char* repo = std::getenv("AI_CAD_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        dwgSavePath = base + "\\scratch\\drawing_shots";
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
                            {"fields", {{"title", "PLATE"}, {"drawn_by", "AI-CAD"},
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
            dwgCapture = dwgSavePath + "\\drawing_01_detailed.png"; dwgSettle = kSettle; ++dwgStep;
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

    // --- C22b activity shot capture ---
    // C39: wait for the async post-run doc_rebuild to drain before shot1 captures.
    if (st.wantActivity) {
        ++st.frame;
        if (!actCapture.empty() && !st.rpc.busy()) {
            // W1-B (item 12): the dir was created up-front via std::filesystem; ensure it again
            // here (no-op if present) instead of the old fragile std::system("mkdir ...") shell
            // call, which could mangle quotes on some paths.
            std::error_code ec; std::filesystem::create_directories(actSavePath, ec);
            // Mirror the last step to screenshotPath
            const char* mirror = actCapture.find("activity_02_agents") != std::string::npos
                               ? screenshotPath : nullptr;
            writeBackbufferPng(actCapture.c_str(), dw, dh, mirror);
            std::printf("activity: screenshot -> %s (%dx%d)\n",
                        actCapture.c_str(), dw, dh);
            std::fflush(stdout);
            actCapture.clear();
        }
        glfwSwapBuffers(st.win);
        return st.activityShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    // --- end C22b activity shot capture ---

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

    return FrameAction::None;
}

}  // namespace ui
