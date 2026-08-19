// Integrated Manufacture/CAM workspace controls and native viewport overlay state.
#include "imgui.h"

#include "cam_panel.h"
#include "cam_mirror.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <string>
#include <vector>
#include <utility>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#include "../../core/app_state.h"
#include "../../ui_theme.h"
#include "../../mesh.h"
#include "../feature_tree.h"
#include "../metrics.h"

namespace ui {

namespace {

struct CamUI {
    CamMirror mirror;
    std::string docId;
    std::string sourceDocId;           // pinned Part doc used by every later extraction
    std::string sourceModel;
    nlohmann::json lastExtract;
    bool backplot = true;
    bool showStock = false;
    int selSetup = 0;
    int selOp = -1;
    int selTool = -1;
    int selKind = 0;                   // 1=job 2=job setup 3=stock 4=setup 5=op 6=tool 7=model

    std::vector<std::string> dialects;
    bool dialectsInflight = false;
    double dialectsNextTry = 0.0;
    int postIdx = -1;
    bool flavorPopup = false;

    int rpcInflight = 0;
    int rpcGen = 0;                    // drops callbacks after a job switch
    int generatedRev = -1;
    int genWarnings = 0;
    int pipeStage = 0;                 // 1/2/3 full pipeline; 4 sim-after-generate; 5 post-after

    std::string gcode;
    std::string gcodePath;
    nlohmann::json sim;
    nlohmann::json audit;
    bool auditInflight = false;
    std::string status;

    std::vector<nlohmann::json> simFrames;
    std::vector<double> simFrameT;
    bool framesClamped = false;
    double playT = 0.0;
    bool playing = false;
    float playSpeed = 1.0f;
    int curFrame = -1;
    Mesh frameMesh;
    bool frameMeshValid = false;
    int simGen = 0;

    bool optGouge = true;
    bool optRapid = true;
    bool optHolder = false;
    float optRes = 0.5f;

    char opName[64] = "";
    float fStepover = 0.0f, fStepdown = 0.0f, fStockLeave = 0.0f;
    float fFeed = 0.0f, fRpm = 0.0f;
    float hTop = 0.0f, hBottom = 0.0f, hClear = 0.0f, hRetract = 0.0f;
    std::string loadedSetupId;
    std::string loadedOpId;
    int loadedOpRevision = -1;
};
CamUI g;

bool camBusy() { return g.rpcInflight > 0 || g.auditInflight; }
void camBeginRpc() { ++g.rpcInflight; }
void camEndRpc() { if (g.rpcInflight > 0) --g.rpcInflight; }

void doSimulate(AppState& st);
void doPost(AppState& st);

void resetOpBuffers() {
    g.loadedSetupId.clear();
    g.loadedOpId.clear();
    g.loadedOpRevision = -1;
}

void clearSimulationAndOutput() {
    g.gcode.clear();
    g.gcodePath.clear();
    g.sim = nlohmann::json();
    g.audit = nlohmann::json();
    ++g.simGen;
    g.simFrames.clear();
    g.simFrameT.clear();
    g.framesClamped = false;
    g.playT = 0.0;
    g.playing = false;
    g.curFrame = -1;
    g.frameMeshValid = false;
}

void clearDownstreamArtifacts() {
    g.generatedRev = -1;
    g.genWarnings = 0;
    g.mirror.clearToolpaths();
    clearSimulationAndOutput();
}

void syncPostIndex() {
    g.postIdx = -1;
    for (int i = 0; i < (int)g.dialects.size(); ++i) {
        if (g.dialects[i] == g.mirror.post()) {
            g.postIdx = i;
            break;
        }
    }
}

// Resolve the pinned source Part. The first extraction pins the active Part; tab changes cannot
// silently redirect later operation geometry to another document.
bool activePartDoc(AppState& st, std::string& docId, std::string& title) {
    if (!g.sourceDocId.empty()) {
        for (const auto& tab : st.docTabs) {
            if (tab.kind == AppState::DocTab::TabKind::Part && tab.docId == g.sourceDocId) {
                docId = tab.docId;
                title = tab.title.empty() ? tab.docId : tab.title;
                return true;
            }
        }
        g.status = "source part is no longer open";
        return false;
    }
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) {
        g.status = "no active part";
        return false;
    }
    const auto& tab = st.docTabs[st.activeTab];
    if (tab.kind != AppState::DocTab::TabKind::Part || tab.docId.empty()) {
        g.status = "active tab is not a Part document";
        return false;
    }
    g.sourceDocId = tab.docId;
    g.sourceModel = tab.title.empty() ? tab.docId : tab.title;
    docId = g.sourceDocId;
    title = g.sourceModel;
    return true;
}

// ---- RPC helpers ----
// Fast tree mutations (cam_new / cam_apply_ops / cam_tree) stay sync — they're cheap. Long
// work (extract/generate/simulate/post) goes through st.rpc.call (see refreshDialects).
bool camCall(AppState& st, const char* method, const nlohmann::json& params, nlohmann::json& out) {
    std::string err;
    bool ok = st.worker.call(method, params, out, err);
    if (!ok) {
        g.status = std::string(method) + ": " + (err.empty() ? std::string("failed") : err);
    }
    return ok;
}

// Async and cooldown-guarded: no worker call is issued from a per-frame draw path.
void refreshDialects(AppState& st) {
    if (!g.dialects.empty() || g.dialectsInflight) return;
    if (ImGui::GetTime() < g.dialectsNextTry) return;
    g.dialectsInflight = true;
    st.rpc.call("cam_post_dialects", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.dialectsInflight = false;
        if (r.ok && r.result.contains("dialects") && r.result["dialects"].is_array()) {
            for (const auto& d : r.result["dialects"]) {
                const std::string id = d.value("id", std::string());
                if (!id.empty()) g.dialects.push_back(id);
            }
            syncPostIndex();
        }
        if (g.dialects.empty()) g.dialectsNextTry = ImGui::GetTime() + 10.0;
    });
}

void loadTree(const nlohmann::json& r) {
    clearDownstreamArtifacts();
    g.mirror.loadTree(r);
    syncPostIndex();
    resetOpBuffers();
}

bool ensureJob(AppState& st) {
    if (!g.docId.empty()) return true;
    nlohmann::json r;
    if (!camCall(st, "cam_new", {{"name", "CAM Job"}, {"post", "grbl"}}, r)) return false;
    g.docId = r.value("doc_id", std::string());
    loadTree(r);
    return !g.docId.empty();
}

nlohmann::json applyOps(AppState& st, const nlohmann::json& ops) {
    nlohmann::json r;
    if (g.docId.empty()) {
        g.status = "no CAM job";
        return r;
    }
    if (camBusy()) {
        g.status = "CAM busy — mutation not applied";
        return r;
    }
    const int baseRevision = g.mirror.revision();
    if (baseRevision < 0) {
        g.status = "CAM tree has no revision";
        return r;
    }
    if (camCall(st, "cam_apply_ops",
                {{"doc_id", g.docId}, {"base_revision", baseRevision}, {"ops", ops}}, r))
        loadTree(r);
    return r;
}

// Server-side export+extract (cam_extract_from_doc). Never ships a client temp path — web and
// desktop share one path. `done` runs on the GL thread inside rpc.drain(); may be empty on fail.
using ExtractDone = std::function<void(const nlohmann::json& out)>;
void extractFromDoc(AppState& st, const std::string& kind, double z, bool useZ, ExtractDone done) {
    std::string partId, title;
    if (!activePartDoc(st, partId, title)) {
        if (done) done(nlohmann::json::object());
        return;
    }
    g.sourceModel = title;
    nlohmann::json params = {{"doc_id", partId}, {"kind", kind}, {"deflection", 0.1}};
    if (useZ) params["z"] = z;
    camBeginRpc();
    g.status = "extracting " + kind + "...";
    const int gen = g.rpcGen;
    st.rpc.call("cam_extract_from_doc", std::move(params),
                [done = std::move(done), gen](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen) return;  // job was replaced while we were out
        nlohmann::json out;
        if (!r.ok) {
            g.status = std::string("cam_extract_from_doc: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
        } else if (r.result.is_object() && r.result.contains("error") &&
                   !r.result["error"].is_null()) {
            g.status = r.result.value("error", std::string("extract failed"));
        } else if (r.result.is_object()) {
            out = r.result;
        } else {
            g.status = "cam_extract_from_doc: bad response";
        }
        if (done) done(out);
    });
}

const char* setupId() {
    if (g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size())
        return g.mirror.setups()[g.selSetup].id.c_str();
    return "";
}

std::string currentDialect() {
    if (g.postIdx >= 0 && g.postIdx < (int)g.dialects.size()) return g.dialects[g.postIdx];
    if (!g.mirror.post().empty()) return g.mirror.post();
    return "grbl";
}

// Write g.gcode to a desktop temp .nc (no-op on web — use downloadGcode instead).
void writeGcodeFile(const std::string& dialect) {
#ifndef __EMSCRIPTEN__
    std::filesystem::path p =
        std::filesystem::temp_directory_path() / ("chudware_cam." + dialect + ".nc");
    std::ofstream f(p);
    if (f) {
        f << g.gcode;
        g.gcodePath = p.string();
    }
#else
    (void)dialect;
    g.gcodePath.clear();
#endif
}

// Browser Blob download of in-memory gcode (web) / ensure temp file exists (desktop).
void downloadGcode() {
    if (g.gcode.empty()) {
        g.status = "no G-code — Post first";
        return;
    }
    const std::string dialect = currentDialect();
    const std::string fname = "chudware_cam." + dialect + ".nc";
#ifdef __EMSCRIPTEN__
    EM_ASM({
        var text = UTF8ToString($0);
        var name = UTF8ToString($1);
        var blob = new Blob([text], {type: 'text/plain'});
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        setTimeout(function() {
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }, 0);
    }, g.gcode.c_str(), fname.c_str());
    g.status = "downloaded " + fname;
#else
    if (g.gcodePath.empty()) writeGcodeFile(dialect);
    g.status = g.gcodePath.empty() ? ("G-code ready (" + fname + ")")
                                   : ("saved " + g.gcodePath);
#endif
}

// Apply heights.bottom from an extract bbox when present.
void applyExtractHeights(const nlohmann::json& ex, nlohmann::json& heights) {
    if (!ex.contains("bbox")) return;
    auto& mn = ex["bbox"]["min"];
    if (mn.is_array() && mn.size() >= 3) heights["bottom"] = mn[2].get<double>();
}

// Add an operation of `type`, pulling geometry via server-side extract (async).
void addOperation(AppState& st, const std::string& type, int toolNumber) {
    if (!ensureJob(st)) return;
    if (g.mirror.setups().empty()) { g.status = "add a setup first"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    const std::string sid = setupId();

    const bool is3d = type.rfind("parallel", 0) == 0 || type.rfind("contour3", 0) == 0 ||
                      type.rfind("scallop", 0) == 0 || type.rfind("pencil", 0) == 0 ||
                      type.rfind("pocket3", 0) == 0 || type.rfind("adaptive_clear", 0) == 0 ||
                      type.rfind("radial", 0) == 0 || type.rfind("spiral", 0) == 0;
    const bool isTurn = type.rfind("turn", 0) == 0;
    const bool isDrill = (type == "drill" || type == "thread_mill");
    const bool isCut = (type == "cut2d");
    const bool isFace = (type == "face");

    // Geometry kind for this op. Face uses stock top automatically (no extract for geom).
    std::string kind = "section";
    double z = 0.0;
    bool useZ = true;
    if (is3d) {
        kind = "mesh"; useZ = false;
    } else if (isTurn) {
        kind = "profile"; useZ = false;
    } else if (isDrill) {
        kind = "holes"; useZ = false;
    } else if (isFace) {
        kind = "stock"; useZ = false;  // only need bbox for cut depth
    }

    AppState* stp = &st;
    auto finish = [stp, type, toolNumber, sid, is3d, isTurn, isDrill, isCut, isFace](
                      const nlohmann::json& ex) {
        if (!ex.empty()) g.lastExtract = ex;

        nlohmann::json geom = nlohmann::json::object();
        nlohmann::json heights = {{"top", 0}, {"bottom", -5}, {"clearance", 15}, {"retract", 5}};
        if (isFace) heights["bottom"] = -1.0;

        if (is3d) {
            if (ex.contains("mesh")) geom["mesh"] = ex["mesh"];
        } else if (isTurn) {
            if (ex.contains("profile")) geom["profile"] = ex["profile"];
        } else if (isDrill) {
            if (ex.contains("points")) geom["points"] = ex["points"];
        } else if (!isFace) {
            if (ex.contains("regions")) {
                if (isCut) {
                    nlohmann::json contours = nlohmann::json::array();
                    for (auto& reg : ex["regions"])
                        if (reg.contains("outer")) contours.push_back(reg["outer"]);
                    geom["contours"] = contours;
                } else {
                    geom["regions"] = ex["regions"];
                }
            }
        }
        applyExtractHeights(ex.empty() ? g.lastExtract : ex, heights);

        nlohmann::json op = {{"type", type}, {"tool_number", toolNumber}, {"heights", heights}};
        if (!geom.empty()) op["geometry"] = geom;
        nlohmann::json r = applyOps(*stp, nlohmann::json::array({nlohmann::json{
            {"op", "add_operation"}, {"setup_id", sid}, {"operation", op}}}));
        if (r.contains("revision")) {
            g.status = "added " + type;
            if (!g.mirror.setups().empty()) {
                // selSetup may have shifted; clamp then select last op on that setup.
                if (g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size())
                    g.selSetup = (int)g.mirror.setups().size() - 1;
                g.selOp = (int)g.mirror.setups()[g.selSetup].ops.size() - 1;
                resetOpBuffers();
            }
        }
    };

    // Reuse a prior full extract when the op only needs stock bbox (face) and we already have it.
    if (isFace && !g.lastExtract.empty()) {
        finish(g.lastExtract);
        return;
    }
    extractFromDoc(st, kind, z, useZ, finish);
}

void doGenerate(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    camBeginRpc();
    g.status = "generating...";
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    AppState* stp = &st;
    st.rpc.call("cam_generate", {{"doc_id", docId}},
                [gen, docId, stp](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.pipeStage = 0;
            g.status = std::string("cam_generate: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        clearSimulationAndOutput();
        g.mirror.loadToolpaths(r.result);
        const int n = (r.result.contains("toolpaths") && r.result["toolpaths"].is_array())
                          ? (int)r.result["toolpaths"].size() : 0;
        g.generatedRev = g.mirror.revision();
        g.genWarnings = (r.result.contains("warnings") && r.result["warnings"].is_array())
                            ? (int)r.result["warnings"].size() : 0;
        g.status = "generated " + std::to_string(n) + " toolpath(s)";
        if (g.genWarnings > 0)
            g.status += " (" + std::to_string(g.genWarnings) + " warning)";
        if (g.pipeStage == 1) {
            g.pipeStage = 2;
            doSimulate(*stp);
        } else if (g.pipeStage == 4) {
            g.pipeStage = 0;
            doSimulate(*stp);
        } else if (g.pipeStage == 5) {
            g.pipeStage = 0;
            doPost(*stp);
        }
    });
}

// Wave 4: chained Generate -> Simulate -> Post pipeline. Status reports the failing stage.
void doPipeline(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    g.pipeStage = 1;
    doGenerate(st);
}

void doSimulate(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    if (g.generatedRev != g.mirror.revision()) {
        g.pipeStage = 4;
        doGenerate(st);
        return;
    }
    camBeginRpc();
    g.status = "simulating...";
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    AppState* stp = &st;
    st.rpc.call("cam_simulate",
                {{"doc_id", docId}, {"include_field", true},
                 {"options", {{"resolution", (double)g.optRes},
                              {"check_gouge", g.optGouge},
                              {"check_rapid", g.optRapid},
                              {"check_holder", g.optHolder},
                              {"frames", 16}}}},
                [gen, docId, stp](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.pipeStage = 0;
            g.status = std::string("cam_simulate: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        camAdoptSimResult(r.result);
        g.status = "simulated";
        if (g.pipeStage == 2) {
            g.pipeStage = 3;
            doPost(*stp);
        }
    });
}

void doPost(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    if (g.generatedRev != g.mirror.revision()) {
        g.pipeStage = 5;
        doGenerate(st);
        return;
    }
    const std::string dialect = currentDialect();
    camBeginRpc();
    g.status = "posting " + dialect + "...";
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    st.rpc.call("cam_post", {{"doc_id", docId}, {"dialect", dialect}},
                [gen, docId, dialect](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.pipeStage = 0;
            g.status = std::string("cam_post: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        g.audit = nlohmann::json();
        g.gcode = r.result.value("gcode", std::string());
        writeGcodeFile(dialect);
        if (g.pipeStage == 3) {
            g.pipeStage = 0;
            g.status = "pipeline done: generated + simulated + posted " + dialect;
        } else {
            g.status = "posted " + dialect + " (" +
                       std::to_string(r.result.value("line_count", 0)) + " lines)";
        }
#ifdef __EMSCRIPTEN__
        if (!g.gcode.empty()) downloadGcode();
#endif
    });
}

// Wave 4b: feeds autofill — CuttingDB lookup via the core cam_feeds RPC, filling the op
// parameter form buffers. On error the buffers stay untouched and the status shows why.
void doFeedsAutofill(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size() ||
        g.selOp < 0 || g.selOp >= (int)g.mirror.setups()[g.selSetup].ops.size()) {
        g.status = "select an operation first";
        return;
    }
    if (camBusy()) { g.status = "CAM busy"; return; }
    const auto& setup = g.mirror.setups()[g.selSetup];
    const auto& op = setup.ops[g.selOp];
    const std::string setupIdentity = setup.id;
    const std::string opIdentity = op.id;
    const std::string material = setup.stock.value("material", std::string("aluminum"));
    camBeginRpc();
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    st.rpc.call("cam_feeds",
                {{"doc_id", docId}, {"setup_id", setupIdentity},
                 {"tool_number", op.tool_number}, {"material", material}},
                [gen, docId, setupIdentity, opIdentity, material](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.status = std::string("cam_feeds: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        if (g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size()) return;
        const auto& selectedSetup = g.mirror.setups()[g.selSetup];
        if (selectedSetup.id != setupIdentity || g.selOp < 0 ||
            g.selOp >= (int)selectedSetup.ops.size() ||
            selectedSetup.ops[g.selOp].id != opIdentity) return;
        auto num = [&](const char* key, float& dst) {
            if (r.result.contains(key) && r.result[key].is_number())
                dst = r.result[key].get<float>();
        };
        num("rpm", g.fRpm);
        num("feed", g.fFeed);
        num("stepover", g.fStepover);
        num("stepdown", g.fStepdown);
        g.status = "feeds filled from tables (" + material + ")";
    });
}

// Deterministic G-code audit via the core cam_audit RPC.
void doAudit(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (camBusy()) { g.status = "CAM busy"; return; }
    g.auditInflight = true;
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    const std::string dialect = currentDialect();
    g.status = "auditing...";
    st.rpc.call("cam_audit", {{"doc_id", docId}, {"dialect", dialect}},
                [gen, docId](const core::RpcResult& r) {
        g.auditInflight = false;
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.audit = nlohmann::json();
            g.status = std::string("cam_audit: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        g.audit = r.result;
        g.status = g.audit.value("ok", false) ? "audit ok" : "audit FAILED";
    });
}


const nlohmann::json* simResultForSetup(const std::string& setupIdentity) {
    if (!g.sim.contains("results") || !g.sim["results"].is_array()) return nullptr;
    for (const auto& result : g.sim["results"])
        if (result.value("setup_id", std::string()) == setupIdentity) return &result;
    return nullptr;
}

void drawSelectedSimEvidence() {
    if (g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size()) {
        ImGui::TextDisabled("Select a setup to see verification evidence.");
        return;
    }
    const nlohmann::json* result = simResultForSetup(g.mirror.setups()[g.selSetup].id);
    if (!result) {
        ImGui::TextDisabled("Not verified — use Simulate on the ribbon.");
        return;
    }
    const int gouges = result->value("gouges", 0);
    const int rapids = result->value("rapid_collisions", 0);
    const int holders = result->value("holder_collisions", 0);
    const bool clean = gouges == 0 && rapids == 0 && holders == 0;
    const float* color = clean ? ui::palette.statusOk : ui::palette.statusWarn;
    ImGui::TextColored(ImVec4(color[0], color[1], color[2], color[3]),
                       clean ? "Verified clean" : "Verification findings");
    const double cycle = result->value("cycle_time_s", 0.0);
    ImGui::TextDisabled("%.0f%% removed  |  %dm %02ds",
                        100.0 * result->value("machined_fraction", 0.0),
                        (int)(cycle / 60.0), (int)cycle % 60);
    if (!clean)
        ImGui::TextDisabled("gouge %d  rapid %d  holder %d", gouges, rapids, holders);
}

}  // namespace

// ---- ribbon command handler ----
void camHandleCommand(AppState& st, const std::string& cmd) {
    refreshDialects(st);
    if (cmd == "cam_show3d") {          // ribbon Output toggle: viewport backplot overlay
        g.backplot = !g.backplot;
        g.status = g.backplot ? "toolpaths shown in 3D" : "toolpaths hidden";
        return;
    }
    if (cmd == "cam_flavor") {
        g.flavorPopup = true;
        return;
    }
    if (cmd == "cam_set_stock") {
        // Re-extract the current part's stock box and apply it to the selected setup.
        if (!ensureJob(st)) return;
        if (g.mirror.setups().empty()) { g.status = "add a setup first"; return; }
        if (camBusy()) { g.status = "CAM busy"; return; }
        const std::string sid = setupId();
        // st is process-lifetime AppState; safe to capture by pointer for the drain callback.
        AppState* stp = &st;
        extractFromDoc(st, "stock", 0, false, [sid, stp](const nlohmann::json& s) {
            if (!s.contains("stock")) {
                if (g.status.find("extract") == std::string::npos)
                    g.status = "stock extraction failed";
                return;
            }
            g.lastExtract = s;
            nlohmann::json box = s["stock"]["box"];
            box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
            applyOps(*stp, nlohmann::json::array({nlohmann::json{
                {"op", "set_stock"}, {"setup_id", sid}, {"stock", box}}}));
            g.status = "stock set from part";
        });
        return;
    }
    if (cmd == "cam_new_job") {
        ++g.rpcGen;
        g.docId.clear();
        g.sourceDocId.clear();
        g.sourceModel.clear();
        g.lastExtract = nlohmann::json();
        g.mirror.clear();
        clearDownstreamArtifacts();
        g.selTool = -1;
        g.pipeStage = 0;
        g.selSetup = 0;
        g.selOp = -1;
        g.selKind = 0;
        resetOpBuffers();
        if (ensureJob(st)) g.status = "new CAM job";
        return;
    }
    if (cmd == "cam_extract") {
        if (!ensureJob(st)) return;
        if (camBusy()) { g.status = "CAM busy"; return; }
        AppState* stp = &st;
        extractFromDoc(st, "all", 0, false, [stp](const nlohmann::json& ex) {
            if (ex.empty()) return;
            g.lastExtract = ex;
            // seed a setup from the extracted stock if none exists
            if (g.mirror.setups().empty() && ex.contains("stock")) {
                nlohmann::json box = ex["stock"]["box"];
                box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
                applyOps(*stp, nlohmann::json::array({nlohmann::json{
                    {"op", "add_setup"},
                    {"setup", {{"name", "Setup1"},
                               {"machine", {{"kind", "mill3"}}},
                               {"stock", box}}}}}));
            }
            int nh = ex.contains("holes") ? (int)ex["holes"].size() : 0;
            g.status = "extracted stock + " + std::to_string(nh) + " hole(s)";
        });
        return;
    }
    if (cmd == "cam_add_setup") {
        if (!ensureJob(st)) return;
        const nlohmann::json r = applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_setup"},
            {"setup", {{"name", "Setup" + std::to_string(g.mirror.setups().size() + 1)},
                       {"machine", {{"kind", "mill3"}}},
                       {"stock", {{"type", "box_offset"}, {"off_ztop", 1.0},
                                  {"material", "aluminum"}}}}}}}));
        if (r.contains("revision"))
            g.selSetup = std::max(0, (int)g.mirror.setups().size() - 1);
        return;
    }
    if (cmd == "cam_add_tool") {
        if (!ensureJob(st)) return;
        const int n = (int)g.mirror.tools().size() + 1;
        const nlohmann::json r = applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_tool"},
            {"tool", {{"number", n}, {"type", "endmill_flat"}, {"diameter", 6},
                      {"flutes", 3}, {"name", "6mm flat endmill"}}}}}));
        if (r.contains("revision")) g.status = "added tool T" + std::to_string(n);
        return;
    }
    if (cmd == "cam_generate") { doGenerate(st); return; }
    if (cmd == "cam_simulate") { doSimulate(st); return; }
    if (cmd == "cam_post")     { doPost(st); return; }
    // operation commands: cam_op_<type>
    static const std::pair<const char*, const char*> kOps[] = {
        {"cam_op_face", "face"}, {"cam_op_contour", "contour2d"}, {"cam_op_pocket", "pocket2d"},
        {"cam_op_adaptive", "adaptive2d"}, {"cam_op_slot", "slot"}, {"cam_op_chamfer", "chamfer2d"},
        {"cam_op_engrave", "engrave"}, {"cam_op_drill", "drill"}, {"cam_op_thread", "thread_mill"},
        {"cam_op_parallel3d", "parallel3d"}, {"cam_op_waterline3d", "contour3d"},
        {"cam_op_scallop3d", "scallop3d"}, {"cam_op_pocket3d", "pocket3d"},
        {"cam_op_turn_rough", "turn_rough"}, {"cam_op_turn_finish", "turn_finish"},
        {"cam_op_turn_face", "turn_face"}, {"cam_op_turn_part", "turn_part"},
        {"cam_op_laser", "cut2d"}, {"cam_op_probe", "probe"},
    };
    for (auto& kv : kOps)
        if (cmd == kv.first) {
            int tool = g.mirror.tools().empty() ? 1 : g.mirror.tools().front().number;
            addOperation(st, kv.second, tool);
            return;
        }
}

const CamBackplot* camActiveBackplot() {
    if (!g.backplot || g.mirror.backplot().empty()) return nullptr;
    return &g.mirror.backplot();
}

// Adopt a cam_simulate response: parse the selected setup's frames into the playhead state.
// Called by doSimulate's async callback and by headless scenarios (sync worker.call path).
void camAdoptSimResult(const nlohmann::json& result) {
    g.sim = result;
    ++g.simGen;
    g.simFrames.clear();
    g.simFrameT.clear();
    g.framesClamped = false;
    g.playT = 0.0;
    g.playing = false;
    g.curFrame = -1;
    g.frameMeshValid = false;
    if (!result.contains("results") || !result["results"].is_array()) return;
    const nlohmann::json* pick = nullptr;
    const auto& results = result["results"];
    if (g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size()) {
        const std::string& sid = g.mirror.setups()[g.selSetup].id;
        for (const auto& entry : results)
            if (entry.value("setup_id", std::string()) == sid) { pick = &entry; break; }
    } else if (!results.empty()) {
        pick = &results[0];
    }
    if (!pick || !pick->contains("frames") || !(*pick)["frames"].is_array()) return;
    for (const auto& frame : (*pick)["frames"]) {
        if (!frame.contains("field") || !frame.contains("t_s") ||
            !frame["t_s"].is_number()) continue;
        g.simFrames.push_back(frame["field"]);
        g.simFrameT.push_back(frame["t_s"].get<double>());
    }
    g.framesClamped = pick->value("frames_clamped", false);
}

void camAdoptToolpaths(const nlohmann::json& result) {
    clearSimulationAndOutput();
    g.mirror.loadToolpaths(result);
    g.generatedRev = g.mirror.revision();
    g.genWarnings = (result.contains("warnings") && result["warnings"].is_array())
                        ? (int)result["warnings"].size() : 0;
}

// ---- viewport playhead (wave 3) ----
bool camSimHudActive() {
    return g.backplot && !g.mirror.backplot().empty();
}

// Frame index for the current playT (lower_bound over frame times).
int camCurrentFrameIndex() {
    if (g.simFrameT.empty()) return -1;
    auto it = std::lower_bound(g.simFrameT.begin(), g.simFrameT.end(), g.playT);
    if (it == g.simFrameT.end()) return (int)g.simFrameT.size() - 1;
    return (int)(it - g.simFrameT.begin());
}

// Rebuild the remnant mesh for frame `idx` if needed; returns true when a mesh is ready.
int camSimGen() { return g.simGen; }
bool camFrameMeshFor(int idx) {
    if (idx < 0 || idx >= (int)g.simFrames.size()) return false;
    if (idx == g.curFrame && g.frameMeshValid) return true;
    if (!camFieldToMesh(g.simFrames[idx], g.frameMesh)) {
        g.status = "sim frame mesh: malformed field (kept previous)";
        return g.frameMeshValid;
    }
    g.curFrame = idx;
    g.frameMeshValid = true;
    return true;
}
const Mesh& camFrameMesh() { return g.frameMesh; }

// Tool pose at global time t. Explicit starts keep interleaved move classes on one timeline.
bool camToolPoseAt(double t, float outPos[3]) {
    const CamBackplot& bp = g.mirror.backplot();
    double bestPastEnd = -1.0;
    double earliestFuture = std::numeric_limits<double>::infinity();
    float past[3] = {0, 0, 0};
    float future[3] = {0, 0, 0};
    bool havePast = false, haveFuture = false;
    auto walk = [&](const std::vector<float>& segs, const std::vector<float>& starts,
                    const std::vector<float>& ends) {
        for (size_t i = 0; i + 5 < segs.size(); i += 6) {
            const size_t segment = i / 6;
            if (segment >= starts.size() || segment >= ends.size()) continue;
            const double tStart = starts[segment];
            const double tEnd = ends[segment];
            if (t >= tStart && t <= tEnd) {
                const double f = (tEnd > tStart)
                                     ? std::max(0.0, std::min(1.0, (t - tStart) / (tEnd - tStart)))
                                     : 1.0;
                outPos[0] = segs[i]   + (segs[i+3] - segs[i])   * (float)f;
                outPos[1] = segs[i+1] + (segs[i+4] - segs[i+1]) * (float)f;
                outPos[2] = segs[i+2] + (segs[i+5] - segs[i+2]) * (float)f;
                return true;
            }
            if (tEnd <= t && tEnd > bestPastEnd) {
                bestPastEnd = tEnd;
                past[0] = segs[i+3]; past[1] = segs[i+4]; past[2] = segs[i+5];
                havePast = true;
            }
            if (tStart > t && tStart < earliestFuture) {
                earliestFuture = tStart;
                future[0] = segs[i]; future[1] = segs[i+1]; future[2] = segs[i+2];
                haveFuture = true;
            }
        }
        return false;
    };
    if (walk(bp.rapid, bp.rapidT0, bp.rapidT) ||
        walk(bp.cut, bp.cutT0, bp.cutT) ||
        walk(bp.lead, bp.leadT0, bp.leadT) ||
        walk(bp.plunge, bp.plungeT0, bp.plungeT) ||
        walk(bp.arc, bp.arcT0, bp.arcT)) return true;
    const float* hold = havePast ? past : (haveFuture ? future : nullptr);
    if (!hold) return false;
    outPos[0] = hold[0]; outPos[1] = hold[1]; outPos[2] = hold[2];
    return true;
}

double camPlayTotalTime() {
    return g.mirror.backplot().totalTime;
}
double camPlayT() { return g.playT; }

bool camStockBoxVisible() {
    return g.showStock && g.selSetup >= 0 &&
           g.selSetup < (int)g.mirror.setups().size() &&
           !g.mirror.setups()[g.selSetup].stockSegments.empty();
}

const std::vector<float>* camStockBoxSegments() {
    if (!camStockBoxVisible()) return nullptr;
    return &g.mirror.setups()[g.selSetup].stockSegments;
}

// ---- bottom-anchored viewport HUD rect (see cam_panel.h) ----------------------------------------
// Mirrors ui::publishOverlayColumnHeight / ui::overlayColumnHeight: camDrawViewportHud (below)
// publishes once per frame, the orientation-triad block in viewport_panel.cpp reads. Frame-stamped
// so leaving Manufacture invalidates the rect instead of deflecting the triad forever.
static ViewportHudRect g_vpHudRect;
static int             g_vpHudRectFrame = -1;

void publishViewportHudRect(float minX, float minY, float maxX, float maxY) {
    g_vpHudRect.minX = minX; g_vpHudRect.minY = minY;
    g_vpHudRect.maxX = maxX; g_vpHudRect.maxY = maxY;
    g_vpHudRect.valid = (maxX > minX && maxY > minY);
    g_vpHudRectFrame = ImGui::GetFrameCount();
}
ViewportHudRect viewportHudRect() {
    if (g_vpHudRectFrame < ImGui::GetFrameCount() - 1) { ViewportHudRect r; return r; }
    return g_vpHudRect;
}

// Viewport-local telemetry and transport. Geometry is rendered exclusively by the native overlay.
void camDrawViewportHud(AppState& /*st*/, const ImVec2& vpMin, const ImVec2& vpSize) {
    if (g.docId.empty()) return;
    const double total = g.mirror.backplot().totalTime;
    if (g.playing && total > 0.0) {
        g.playT += ImGui::GetIO().DeltaTime * g.playSpeed;
        if (g.playT >= total) {
            g.playT = total;
            g.playing = false;
        }
    }

    const ImGuiStyle& style = ImGui::GetStyle();
    const float gutter = ui::metrics::camGutter();
    const float width = std::max(ui::metrics::camFieldW() * 2.0f,
                                 std::min(vpSize.x - gutter * 2.0f,
                                          ui::metrics::camFieldW() * 4.0f));
    const float height = ImGui::GetFrameHeight() * 3.0f +
                         style.ItemSpacing.y * 2.0f + style.WindowPadding.y * 2.0f;
    ImGui::SetNextWindowPos(
        ImVec2(vpMin.x + (vpSize.x - width) * 0.5f,
               vpMin.y + vpSize.y - height - ui::metrics::camSectionGap()));
    ImGui::SetNextWindowSize(ImVec2(width, height));
    float bg[4], border[4];
    ui::Palette::withAlpha(ui::palette.statusBg, bg, 0.88f);
    ui::Palette::withAlpha(ui::palette.border, border, 0.9f);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(bg[0], bg[1], bg[2], bg[3]));
    ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(border[0], border[1], border[2], border[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, ui::metrics::camRowRound());
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, ui::metrics::stroke(1.0f));
    const ImGuiWindowFlags flags =
        ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
        ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
        ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoNav;
    if (ImGui::Begin("##cam_viewport_hud", nullptr, flags)) {
        // Publish the card's real rect (post-layout, not the requested size) so the viewport's
        // bottom-left HUD family — orientation triad, model-dimensions pill — can dodge it.
        const ImVec2 hudPos = ImGui::GetWindowPos(), hudSize = ImGui::GetWindowSize();
        publishViewportHudRect(hudPos.x, hudPos.y, hudPos.x + hudSize.x, hudPos.y + hudSize.y);
        const std::string job = g.mirror.name().empty() ? "CAM Job" : g.mirror.name();
        ImGui::TextUnformatted(job.c_str());
        ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
        ImGui::TextDisabled("rev %d  |  %s", g.mirror.revision(), currentDialect().c_str());
        const nlohmann::json* evidence =
            (g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size())
                ? simResultForSetup(g.mirror.setups()[g.selSetup].id) : nullptr;
        ImGui::SameLine(0.0f, ui::metrics::camSectionGap());
        if (!evidence) {
            ImGui::TextColored(ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1],
                                      ui::palette.statusWarn[2], ui::palette.statusWarn[3]),
                               "UNVERIFIED");
        } else {
            const bool clean = evidence->value("gouges", 0) == 0 &&
                               evidence->value("rapid_collisions", 0) == 0 &&
                               evidence->value("holder_collisions", 0) == 0;
            const float* color = clean ? ui::palette.statusOk : ui::palette.statusErr;
            ImGui::TextColored(ImVec4(color[0], color[1], color[2], color[3]),
                               clean ? "VERIFIED" : "FINDINGS");
        }

        auto legend = [](const char* label, const float color[4]) {
            ImGui::TextColored(ImVec4(color[0], color[1], color[2], color[3]), "%s", label);
            ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
        };
        legend("RAPID", ui::palette.camRapid);
        legend("LEAD", ui::palette.camLead);
        legend("PLUNGE", ui::palette.camPlunge);
        legend("CUT", ui::palette.camCut);
        legend("ARC", ui::palette.camArc);
        ImGui::Checkbox("Path##cam_hud_path", &g.backplot);
        ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
        ImGui::Checkbox("Stock##cam_hud_stock", &g.showStock);

        const float speeds[] = {0.25f, 0.5f, 1.0f, 2.0f, 4.0f};
        const char* labels[] = {"0.25x", "0.5x", "1x", "2x", "4x"};
        int speedIndex = 2;
        for (int i = 0; i < IM_ARRAYSIZE(speeds); ++i)
            if (std::fabs(speeds[i] - g.playSpeed) < 0.01f) speedIndex = i;
        if (total <= 0.0) ImGui::BeginDisabled();
        if (ImGui::Button(g.playing ? "Pause" : "Play",
                          ImVec2(0.0f, ui::metrics::camBtnH())))
            g.playing = !g.playing;
        ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
        float playhead = (float)g.playT;
        const float speedWidth = ui::metrics::camFieldW() * 0.55f;
        const float telemetryWidth = ImGui::CalcTextSize("00:00 / 00:00  100%").x;
        ImGui::SetNextItemWidth(-(speedWidth + telemetryWidth +
                                  style.ItemSpacing.x * 3.0f));
        if (ImGui::SliderFloat("##cam_viewport_time", &playhead, 0.0f, (float)total, ""))
            g.playT = playhead;
        ImGui::SameLine();
        ImGui::SetNextItemWidth(speedWidth);
        if (ImGui::BeginCombo("##cam_viewport_speed", labels[speedIndex])) {
            for (int i = 0; i < IM_ARRAYSIZE(speeds); ++i)
                if (ImGui::Selectable(labels[i], i == speedIndex)) g.playSpeed = speeds[i];
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        const int percent = total > 0.0 ? (int)std::round(100.0 * g.playT / total) : 0;
        ImGui::TextDisabled("%d:%02d / %d:%02d  %d%%",
                            (int)(g.playT / 60.0), (int)g.playT % 60,
                            (int)(total / 60.0), (int)total % 60, percent);
        if (total <= 0.0) ImGui::EndDisabled();
    }
    ImGui::End();
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
}

// Shot-only: park the playhead at a fraction of total time for a headless capture.
void camDebugSetPlayhead(float frac) {
    double total = g.mirror.backplot().totalTime;
    if (frac < 0) frac = 0; if (frac > 1) frac = 1;
    g.playT = total * frac;
}


// Adopt a core-created CAM job without retaining source or derived state from the old job.
void camLoadJob(AppState& st, const std::string& docId) {
    if (docId.empty()) return;
    ++g.rpcGen;
    g.docId = docId;
    g.sourceDocId.clear();
    g.sourceModel.clear();
    g.lastExtract = nlohmann::json();
    g.mirror.clear();
    clearDownstreamArtifacts();
    g.selSetup = 0;
    g.selOp = -1;
    g.selTool = -1;
    g.selKind = 0;
    g.pipeStage = 0;
    resetOpBuffers();

    nlohmann::json tree;
    if (!camCall(st, "cam_tree", {{"doc_id", docId}}, tree)) return;
    loadTree(tree);
    g.status = "loaded CAM job from copilot";
    if (tree.contains("gcode") && tree["gcode"].is_string()) {
        g.gcode = tree["gcode"].get<std::string>();
        if (!g.gcode.empty()) writeGcodeFile(currentDialect());
    }

    AppState* stp = &st;
    camBeginRpc();
    const int gen = g.rpcGen;
    st.rpc.call("cam_generate", {{"doc_id", docId}},
                [gen, docId, stp](const core::RpcResult& generated) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!generated.ok) {
            g.status = std::string("cam_generate: ") +
                       (generated.err.empty() ? std::string("failed") : generated.err);
            return;
        }
        g.mirror.loadToolpaths(generated.result);
        g.generatedRev = g.mirror.revision();
        g.genWarnings =
            (generated.result.contains("warnings") && generated.result["warnings"].is_array())
                ? (int)generated.result["warnings"].size() : 0;
        if (g.gcode.empty() && generated.result.contains("gcode") &&
            generated.result["gcode"].is_string()) {
            g.gcode = generated.result["gcode"].get<std::string>();
            if (!g.gcode.empty()) writeGcodeFile(currentDialect());
        }
        if (!g.gcode.empty()) return;
        const std::string dialect = currentDialect();
        camBeginRpc();
        stp->rpc.call("cam_post", {{"doc_id", docId}, {"dialect", dialect}},
                      [gen, docId, dialect](const core::RpcResult& posted) {
            camEndRpc();
            if (gen != g.rpcGen || g.docId != docId || !posted.ok) return;
            g.gcode = posted.result.value("gcode", std::string());
            if (!g.gcode.empty()) writeGcodeFile(dialect);
        });
    });
}

void drawCamOutputBody(AppState& st) {
    refreshDialects(st);
    const bool busy = camBusy();
    ImGui::TextDisabled("Post flavor");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##cam_output_dialect", currentDialect().c_str())) {
        if (g.dialects.empty())
            ImGui::TextDisabled(g.dialectsInflight ? "loading..." : "unavailable");
        for (int i = 0; i < (int)g.dialects.size(); ++i) {
            const bool selected = g.dialects[i] == currentDialect();
            if (ImGui::Selectable(g.dialects[i].c_str(), selected) && !selected) {
                const nlohmann::json r = applyOps(
                    st, nlohmann::json::array({nlohmann::json{
                            {"op", "set_post"}, {"post", g.dialects[i]}}}));
                if (r.contains("revision")) g.status = "post flavor: " + g.dialects[i];
            }
        }
        ImGui::EndCombo();
    }

    const ImVec2 buttonSize(0.0f, ui::metrics::camBtnH());
    if (busy || g.docId.empty()) ImGui::BeginDisabled();
    if (ImGui::Button("Post", buttonSize)) doPost(st);
    if (busy || g.docId.empty()) ImGui::EndDisabled();
    ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
    if (g.gcode.empty()) ImGui::BeginDisabled();
    if (ImGui::Button("Download", buttonSize)) downloadGcode();
    if (g.gcode.empty()) ImGui::EndDisabled();
    ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
    if (busy || g.gcode.empty()) ImGui::BeginDisabled();
    if (ImGui::Button("Audit", buttonSize)) doAudit(st);
    if (busy || g.gcode.empty()) ImGui::EndDisabled();

    if (!g.status.empty()) {
        ImGui::PushTextWrapPos();
        ImGui::TextDisabled("%s", g.status.c_str());
        ImGui::PopTextWrapPos();
    }
    if (!g.audit.empty()) {
        const bool ok = g.audit.value("ok", false);
        const float* color = ok ? ui::palette.statusOk : ui::palette.statusErr;
        ImGui::TextColored(ImVec4(color[0], color[1], color[2], color[3]),
                           ok ? "Audit passed" : "Audit findings");
        if (g.audit.contains("stats") && g.audit["stats"].is_object()) {
            const auto& stats = g.audit["stats"];
            ImGui::TextDisabled("%d lines  |  %d tools  |  %.0fs",
                                stats.value("lines", 0), stats.value("tool_changes", 0),
                                stats.value("est_time_s", 0.0));
        }
        auto findings = [](const char* key, const float colorRole[4]) {
            if (!g.audit.contains(key) || !g.audit[key].is_array()) return;
            ImGui::PushStyleColor(
                ImGuiCol_Text,
                ImVec4(colorRole[0], colorRole[1], colorRole[2], colorRole[3]));
            for (const auto& finding : g.audit[key])
                if (finding.is_string())
                    ImGui::BulletText("%s", finding.get_ref<const std::string&>().c_str());
            ImGui::PopStyleColor();
        };
        findings("errors", ui::palette.statusErr);
        findings("warnings", ui::palette.statusWarn);
    }

    ImGui::Separator();
    ImGui::BeginChild("##cam_output_gcode", ImVec2(0.0f, 0.0f), false,
                      ImGuiWindowFlags_NoScrollbar);
    if (!g.gcode.empty()) {
        ImGui::TextUnformatted(g.gcode.data(), g.gcode.data() + g.gcode.size());
    } else {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::camSectionGap()));
        ImGui::PushTextWrapPos();
        if (g.docId.empty())
            ImGui::TextDisabled("No CAM job. Create one from the Manufacture ribbon.");
        else if (g.mirror.setups().empty())
            ImGui::TextDisabled("No setup. Add a setup and operation before posting.");
        else
            ImGui::TextDisabled("No NC output. Generate toolpaths, then Post.");
        ImGui::PopTextWrapPos();
    }
    const ImVec2 childMin = ImGui::GetWindowPos();
    const ImVec2 childSize = ImGui::GetWindowSize();
    const auto& surface = ui::adaptivePanel(
        ui::panelId("cam_output_gcode"),
        ImRect(childMin, ImVec2(childMin.x + childSize.x, childMin.y + childSize.y)),
        ui::GlassGroup::FeatureManager);
    ui::drawWindowPillScrollbarV(surface);
    ImGui::EndChild();
}

// Manufacture workspace tree plus its selected-operation inspector and simulation evidence.
namespace {

// The FeatureManager tree-icon logical size (feature_tree.h fmTreeIcon default). Row glyphs this
// file paints by hand must use the SAME logical size so they line up with the atlas icons.
constexpr float kTreeIconDp = 16.0f;


// A tree row's sub-caption — the job row's "extracted stock + 0 hole(s)" line. Dim and wrapped,
// separated from the row by exactly one camCaptionGap (replacing, not stacking on, ItemSpacing.y)
// and left-aligned to the row's LABEL text at `labelX`, never to the pill's left edge.
void camRowCaption(float labelX, const std::string& text, bool busy) {
    if (text.empty() && !busy) return;
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() - ImGui::GetStyle().ItemSpacing.y +
                         ui::metrics::camCaptionGap());
    ImGui::SetCursorPosX(labelX);
    const char* t = text.empty() ? "working..." : text.c_str();
    if (busy) {
        busyLabel(t, ImGui::GetColorU32(ImGuiCol_TextDisabled));
    } else {
        ImGui::PushTextWrapPos(0.0f);   // wrap at the content region's right edge
        ImGui::TextDisabled("%s", t);
        ImGui::PopTextWrapPos();
    }
}
void loadSelectedOperationBuffers(const CamSetupRow& setup, const CamOpRow& op) {
    if (g.loadedSetupId == setup.id && g.loadedOpId == op.id &&
        g.loadedOpRevision == g.mirror.revision()) return;
    std::snprintf(g.opName, sizeof(g.opName), "%s", op.name.c_str());
    g.fStepover = (float)op.stepover;
    g.fStepdown = (float)op.stepdown;
    g.fStockLeave = (float)op.stock_leave;
    g.fFeed = (float)op.feed;
    g.fRpm = (float)op.rpm;
    g.hTop = (float)op.top;
    g.hBottom = (float)op.bottom;
    g.hClear = (float)op.clearance;
    g.hRetract = (float)op.retract;
    g.loadedSetupId = setup.id;
    g.loadedOpId = op.id;
    g.loadedOpRevision = g.mirror.revision();
}

void drawSelectedOperationInspector(AppState& st) {
    if (g.selKind != 5 || g.selSetup < 0 ||
        g.selSetup >= (int)g.mirror.setups().size()) return;
    const auto& setup = g.mirror.setups()[g.selSetup];
    if (g.selOp < 0 || g.selOp >= (int)setup.ops.size()) return;
    const auto& op = setup.ops[g.selOp];
    loadSelectedOperationBuffers(setup, op);
    const std::string setupIdentity = setup.id;
    const std::string opIdentity = op.id;

    fmSectionDivider("Operation inspector");
    ImGui::Text("%s  [T%d]", op.name.empty() ? op.type.c_str() : op.name.c_str(),
                op.tool_number);
    ImGui::TextDisabled("setup %s  |  op %s", setupIdentity.c_str(), opIdentity.c_str());
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputText("##cam_op_name", g.opName, sizeof(g.opName));

    if (ImGui::BeginTable("##cam_op_fields", 2, ImGuiTableFlags_SizingStretchProp)) {
        auto field = [](const char* label, const char* id, float& value, const char* format) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::AlignTextToFramePadding();
            ImGui::TextDisabled("%s", label);
            ImGui::TableNextColumn();
            ImGui::SetNextItemWidth(-FLT_MIN);
            ImGui::InputFloat(id, &value, 0.0f, 0.0f, format);
        };
        field("Feed", "##feed", g.fFeed, "%.0f");
        field("RPM", "##rpm", g.fRpm, "%.0f");
        field("Stepover", "##stepover", g.fStepover, "%.3f");
        field("Stepdown", "##stepdown", g.fStepdown, "%.3f");
        field("Stock leave", "##stock_leave", g.fStockLeave, "%.3f");
        field("Top", "##top", g.hTop, "%.3f");
        field("Bottom", "##bottom", g.hBottom, "%.3f");
        field("Clearance", "##clearance", g.hClear, "%.3f");
        field("Retract", "##retract", g.hRetract, "%.3f");
        ImGui::EndTable();
    }

    const float gap = ui::metrics::camBtnGap();
    const float buttonWidth = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
    const bool busy = camBusy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Autofill", ImVec2(buttonWidth, ui::metrics::camBtnH())))
        doFeedsAutofill(st);
    ImGui::SameLine(0.0f, gap);
    if (ImGui::Button("Apply", ImVec2(buttonWidth, ui::metrics::camBtnH()))) {
        const nlohmann::json update = {
            {"name", std::string(g.opName)},
            {"feed", g.fFeed},
            {"rpm", g.fRpm},
            {"stepover", g.fStepover},
            {"stepdown", g.fStepdown},
            {"stock_to_leave", g.fStockLeave},
            {"heights", {{"top", g.hTop}, {"bottom", g.hBottom},
                         {"clearance", g.hClear}, {"retract", g.hRetract}}}};
        const nlohmann::json result = applyOps(
            st, nlohmann::json::array({nlohmann::json{
                    {"op", "update_operation"}, {"op_id", opIdentity},
                    {"operation", update}}}));
        if (result.contains("revision")) {
            for (int si = 0; si < (int)g.mirror.setups().size(); ++si) {
                if (g.mirror.setups()[si].id != setupIdentity) continue;
                g.selSetup = si;
                for (int oi = 0; oi < (int)g.mirror.setups()[si].ops.size(); ++oi)
                    if (g.mirror.setups()[si].ops[oi].id == opIdentity) {
                        g.selOp = oi;
                        break;
                    }
                break;
            }
            g.status = "operation updated — regenerate required";
        }
    }
    if (busy) ImGui::EndDisabled();
}

void drawSimulationOptions() {
    fmSectionDivider("Simulation");
    ImGui::Checkbox("Gouge", &g.optGouge);
    ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
    ImGui::Checkbox("Rapid", &g.optRapid);
    ImGui::SameLine(0.0f, ui::metrics::camBtnGap());
    ImGui::Checkbox("Holder", &g.optHolder);
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderFloat("##cam_sim_resolution", &g.optRes, 0.2f, 2.0f,
                       "Resolution %.1f mm");
    drawSelectedSimEvidence();
}

}  // namespace

void drawCamTreeBody(AppState& st) {
    refreshDialects(st);   // async + guarded; feeds the flavor popup + Job Setup row
    fmRowStripeReset();

    // Compact vertical rhythm (match FeatureManager): tighten ItemSpacing.y only so the job tree
    // (setups/operations/tools) doesn't read "double spaced". RAII so it unwinds on early returns.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::camRowGap()));
    struct SpacingPop { ~SpacingPop() { ImGui::PopStyleVar(); } } spacingPop;

    // Ribbon "Flavor" dropdown: opened here (this body draws every frame in Manufacture) at the
    // mouse position, which is still on the ribbon button the frame after the click.
    if (g.flavorPopup) {
        ImGui::OpenPopup("##cam_flavor_popup");
        ImGui::SetNextWindowPos(ImGui::GetMousePos(), ImGuiCond_Appearing);
        g.flavorPopup = false;
    }
    if (ImGui::BeginPopup("##cam_flavor_popup")) {
        ImGui::TextDisabled("G-code flavor");
        ImGui::Separator();
        if (g.dialects.empty())
            ImGui::TextDisabled(g.dialectsInflight ? "loading..." : "unavailable");
        for (int i = 0; i < (int)g.dialects.size(); ++i) {
            const bool selected = g.dialects[i] == currentDialect();
            if (ImGui::Selectable(g.dialects[i].c_str(), selected) && !selected) {
                const nlohmann::json r = applyOps(
                    st, nlohmann::json::array({nlohmann::json{
                            {"op", "set_post"}, {"post", g.dialects[i]}}}));
                if (r.contains("revision")) g.status = "post flavor: " + g.dialects[i];
            }
        }
        ImGui::EndPopup();
    }

    if (g.docId.empty()) {
        ImGui::Spacing();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextDisabled("No CAM job.\n\nUse New Job or Extract Part on the Manufacture "
                            "ribbon to start one from the active part.");
        ImGui::PopTextWrapPos();
        if (!g.status.empty()) { ImGui::Spacing(); ImGui::TextDisabled("%s", g.status.c_str()); }
        return;
    }

    const auto& setups = g.mirror.setups();
    if (g.selSetup >= (int)setups.size()) g.selSetup = setups.empty() ? 0 : (int)setups.size() - 1;
    const bool generated = (g.generatedRev >= 0 && g.generatedRev == g.mirror.revision());

    // Deferred mutations: context-menu actions apply ops AFTER the tree walk (applyOps reloads
    // the mirror, which would invalidate the rows we're iterating).
    nlohmann::json pendingOps = nlohmann::json::array();

    // Draw one leaf row's selectable+label against the same frame-height rect as the pill/icon.
    // Draw one leaf row's selectable+label against the SAME frame-height rect the pill/icon use.
    // The Selectable is sized to GetFrameHeight() (matching fmRowPill's rowMax) and its text is
    // vertically centered; it is NOT given `selected` — selection is painted by fmRowPill alone
    // (a `selected` Selectable would paint ImGui's default header rect at the text-height bb, on
    // top of the full-height glass card, which is the offset/glitching band the user reported).
    auto leafSelectable = [&](const std::string& label) -> bool {
        const float rowH = ImGui::GetFrameHeight();
        ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign, ImVec2(0.0f, 0.5f));
        bool clicked = ImGui::Selectable(label.c_str(), false, 0, ImVec2(0.0f, rowH));
        ImGui::PopStyleVar();
        return clicked;
    };
    // The FM row contract (feature_tree.cpp treeIcon + fmRowPill): a 6dp FramePadding.x inset, a
    // 16dp glyph, then a 4dp gap before the label. Captured here so the "(nothing yet)" notes and
    // the sub-captions line up with the rows' LABEL column instead of the pill's left edge.
    const float kIconGap    = ui::metrics::U(1);
    const float kLabelInset = ui::metrics::U(1.5f) + ui::metrics::px(kTreeIconDp) + kIconGap;
    auto leafRow = [&](const char* id, const char* icon, const char* fb,
                       const std::string& label, bool selected) -> bool {
        ImGui::PushID(id);
        FmRowRect rr = fmRowPill(selected);
        fmTreeIcon(icon, fb, kTreeIconDp);
        ImGui::SameLine(0, kIconGap);
        bool clicked = leafSelectable(label);
        fmRowPillEnd(rr, selected);
        ImGui::PopID();
        return clicked;
    };
    // A dim "(nothing here yet — do X)" note under a branch. Indented to the label column and
    // WRAPPED, not pilled: the pinned column is narrow and a frame-height pill would clip the
    // actionable half of the hint (which is the half that gets the user to G-code).
    auto emptyNote = [&](const char* text) {
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + kLabelInset);
        ImGui::PushTextWrapPos(0.0f);   // wrap at the content region's right edge
        ImGui::TextDisabled("%s", text);
        ImGui::PopTextWrapPos();
    };

    // ---- Job root ----
    // ImGuiTreeNodeFlags_FramePadding is REQUIRED inside an fmRowPill: without it a TreeNodeEx is
    // TEXT height drawn at the row TOP, so the label sat ~4dp above the vertically-centered icon
    // inside the frame-height pill — the mis-proportioned "CAM Job (rev N)" row. With it the node
    // IS pill height and ImGui centers the label on the pushed 4dp FramePadding.y, exactly like
    // featureRow() in feature_tree.cpp. SpanFullWidth matches the FM rows' hit box too.
    const bool jobSel = (g.selKind == 1);
    bool jobOpen = false;
    float jobLabelX = 0.0f;
    {
        FmRowRect rr = fmRowPill(jobSel);
        fmTreeIcon("new_doc", "J", kTreeIconDp);
        ImGui::SameLine(0, kIconGap);
        // ImGui reserves FontSize + 2*FramePadding.x for the collapse arrow ahead of the label.
        jobLabelX = ImGui::GetCursorPosX() + ImGui::GetFontSize() +
                    ImGui::GetStyle().FramePadding.x * 2.0f;
        std::string jobLbl = (g.mirror.name().empty() ? std::string("CAM Job") : g.mirror.name())
                             + "  (rev " + std::to_string(g.mirror.revision()) + ")";
        jobOpen = ImGui::TreeNodeEx("##cam_job",
            ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_OpenOnArrow |
            ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding,
            "%s", jobLbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) g.selKind = 1;
        fmRowPillEnd(rr, jobSel);
        if (!jobOpen) {
            camRowCaption(jobLabelX, g.status, camBusy());
            return;
        }
    }

    // ---- Job Setup (machine family + post flavor) ----
    {
        std::string flavor = (g.postIdx >= 0 && g.postIdx < (int)g.dialects.size())
                                 ? g.dialects[g.postIdx] : g.mirror.post();
        if (flavor.empty()) flavor = "grbl";
        if (leafRow("cam_jobsetup", "settings", "S", "Job Setup — post: " + flavor,
                    g.selKind == 2))
            g.selKind = 2;
    }

    // ---- Stock Setup (selected setup's stock summary) ----
    {
        std::string lbl = "Stock Setup";
        if (!setups.empty() && setups[g.selSetup].stock.is_object() &&
            !setups[g.selSetup].stock.empty()) {
            const auto& sk = setups[g.selSetup].stock;
            lbl += " — " + sk.value("type", std::string("box"));
            const std::string mat = sk.value("material", std::string());
            if (!mat.empty()) lbl += ", " + mat;
        } else {
            lbl += " — (none)";
        }
        // Inlined (not leafRow) so the icon slot draws a clean stock-chudware glyph instead of the
        // "extrude" atlas name falling through to the bare "K" text fallback. The chudware replicates
        // treeIcon()'s geometry exactly (reserve a frame-height Dummy, paint centered in it) so it
        // shares the size/baseline of the gear/csys glyphs on the neighbouring rows.
        const bool stockSel = (g.selKind == 3);
        ImGui::PushID("cam_stock");
        FmRowRect rr = fmRowPill(stockSel);
        {
            const float gpx    = ui::metrics::px(kTreeIconDp);
            const float insetX = ImGui::GetStyle().FramePadding.x;
            const float rowH   = ImGui::GetFrameHeight();
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            ImGui::Dummy(ImVec2(insetX + gpx, rowH));
            ImDrawList* dl = ImGui::GetWindowDrawList();
            // Match the secondary-ink weight of the atlas row glyphs without reaching into
            // feature_tree.cpp's (file-static) fmSurf(): the current Text colour at ~0.85 alpha.
            ImVec4 tc = ImGui::GetStyleColorVec4(ImGuiCol_Text); tc.w *= 0.85f;
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(tc);
            const float th = ui::metrics::stroke(1.2f);
            // chudware box: a front face + a back face pushed up-right, joined at the 4 corners.
            const float x0 = p0.x + insetX, y0 = p0.y + (rowH - gpx) * 0.5f;
            const float d  = gpx * 0.28f;                // isometric depth offset
            const float fw = gpx - d, fh = gpx - d;      // front-face extent (leaves room for depth)
            ImVec2 fTL(x0, y0 + d),        fBR(x0 + fw, y0 + d + fh);
            ImVec2 fTR(fBR.x, fTL.y),      fBL(fTL.x, fBR.y);
            ImVec2 bTL(fTL.x + d, fTL.y - d), bTR(fTR.x + d, fTR.y - d), bBR(fBR.x + d, fBR.y - d);
            dl->AddRect(fTL, fBR, ink, 0.0f, 0, th);                 // front face
            dl->AddLine(fTL, bTL, ink, th);                          // top-left edge
            dl->AddLine(fTR, bTR, ink, th);                          // top-right edge
            dl->AddLine(fBR, bBR, ink, th);                          // bottom-right edge
            dl->AddLine(bTL, bTR, ink, th);                          // back top edge
            dl->AddLine(bTR, bBR, ink, th);                          // back right edge
        }
        ImGui::SameLine(0, kIconGap);
        if (leafSelectable(lbl)) g.selKind = 3;
        fmRowPillEnd(rr, stockSel);
        ImGui::PopID();
    }

    // ---- Machining Setups (WCS/machine) + their operations -------------------------------------
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        const bool setupSel = (g.selKind == 4 && g.selSetup == si);
        ImGui::PushID(si);
        FmRowRect rr = fmRowPill(setupSel);
        fmTreeIcon("ref_csys", "W", kTreeIconDp);
        ImGui::SameLine(0, kIconGap);
        std::string lbl = s.name + "  —  " + (s.machine.empty() ? "mill" : s.machine);
        const bool open = ImGui::TreeNodeEx("##cam_setup",
            (si == g.selSetup ? ImGuiTreeNodeFlags_DefaultOpen : 0) |
            ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanFullWidth |
            ImGuiTreeNodeFlags_FramePadding,
            "%s", lbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
            g.selKind = 4; g.selSetup = si; g.selOp = -1;
        }
        fmRowPillEnd(rr, setupSel);
        if (open) {
            for (int oi = 0; oi < (int)s.ops.size(); ++oi) {
                const auto& o = s.ops[oi];
                const bool opSel = (g.selKind == 5 && g.selSetup == si && g.selOp == oi);
                std::string ol = (o.name.empty() ? o.type : o.name)
                                 + "  [T" + std::to_string(o.tool_number) + "]";
                if (o.suppressed)      ol += "  — off";
                else if (generated)    ol += (g.genWarnings > 0) ? "  — generated (!)"
                                                                 : "  — generated";
                else                   ol += "  — needs generate";
                // Wave 4c: status badge — stale when the tree moved past the last generate,
                // warn count from that generate, clean after a 0-gouge/0-rapid sim.
                const char* badge = nullptr;
                const float* badgeCol = nullptr;
                if (!o.suppressed) {
                    if (g.generatedRev >= 0 && g.generatedRev != g.mirror.revision()) {
                        badge = "stale"; badgeCol = ui::palette.statusWarn;
                    } else if (g.genWarnings > 0 && generated) {
                        static thread_local char warnBuf[24];
                        std::snprintf(warnBuf, sizeof(warnBuf), "warn %d", g.genWarnings);
                        badge = warnBuf; badgeCol = ui::palette.statusWarn;
                    } else if (const nlohmann::json* sr = simResultForSetup(s.id)) {
                        if (sr->value("gouges", 0) == 0 &&
                            sr->value("rapid_collisions", 0) == 0 &&
                            sr->value("holder_collisions", 0) == 0) {
                            badge = "clean"; badgeCol = ui::palette.statusOk;
                        }
                    }
                }
                ImGui::PushID(oi);
                FmRowRect orr = fmRowPill(opSel);
                fmTreeIcon(("cam_" + o.type).c_str(), "o", kTreeIconDp,
                           o.suppressed ? 0.45f : 1.0f);
                ImGui::SameLine(0, kIconGap);
                if (leafSelectable(ol)) {
                    g.selKind = 5; g.selSetup = si; g.selOp = oi; resetOpBuffers();
                }
                if (badge) {
                    // Selectable leaves the line's text baseline at 0 — without this the badge
                    // rides the top of the pill while the label is centered.
                    ImGui::SameLine(0, ui::metrics::camBtnGap());
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextColored(ImVec4(badgeCol[0], badgeCol[1], badgeCol[2], badgeCol[3]), "%s", badge);
                }
                if (ImGui::BeginPopupContextItem("##cam_op_ctx")) {
                    if (ImGui::MenuItem(o.suppressed ? "Unsuppress" : "Suppress"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "update_operation"}, {"op_id", o.id},
                            {"operation", {{"suppressed", !o.suppressed}}}});
                    if (ImGui::MenuItem("Delete"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "remove_operation"}, {"op_id", o.id}});
                    ImGui::EndPopup();
                }
                fmRowPillEnd(orr, opSel);
                ImGui::PopID();
            }
            if (s.ops.empty()) emptyNote("(no operations — add from the ribbon)");
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    if (setups.empty()) emptyNote("(no machining setup — Add Setup on the ribbon)");

    // ---- Tools (job tool library) ----
    fmSectionDivider("Tools");
    const auto& tools = g.mirror.tools();
    for (int ti = 0; ti < (int)tools.size(); ++ti) {
        const auto& t = tools[ti];
        char lbl[128];
        std::snprintf(lbl, sizeof(lbl), "T%d  %s  \xC3\x98%.2f mm",
                      t.number, (t.name.empty() ? t.type.c_str() : t.name.c_str()), t.diameter);
        char id[24];
        std::snprintf(id, sizeof(id), "cam_tool_%d", ti);
        if (leafRow(id, "curve_helix", "T", lbl, g.selKind == 6 && g.selTool == ti)) {
            g.selKind = 6; g.selTool = ti;
        }
    }
    if (tools.empty()) emptyNote("(no tools — Add Tool on the ribbon)");

    // ---- Models (the part docs the toolpaths are computed from) ----
    fmSectionDivider("Models");
    if (!g.sourceModel.empty()) {
        if (leafRow("cam_model", "part", "M", g.sourceModel, g.selKind == 7)) g.selKind = 7;
    } else {
        emptyNote("(none — Extract Part pulls the active part)");
    }

    if (jobOpen) ImGui::TreePop();   // balance the ##cam_job TreeNodeEx (the footer sits outside)
    drawSelectedOperationInspector(st);
    drawSimulationOptions();

    // ---- footer: one full-width call to action, then the status line ----
    if (!g.docId.empty() && !g.mirror.setups().empty()) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::camSectionGap()));
        const bool busy = camBusy();
        if (busy) ImGui::BeginDisabled();
        if (ImGui::Button("Generate G-code", ImVec2(-FLT_MIN, ui::metrics::camBtnH())))
            doPipeline(st);
        if (busy) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Run the whole pipeline: generate toolpaths, simulate, then post");
    }
    camRowCaption(ImGui::GetCursorPosX(), g.status, camBusy());

    if (!pendingOps.empty()) applyOps(st, pendingOps);   // after the walk — mirror reload safe
}

}  // namespace ui
