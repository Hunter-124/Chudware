// CamPanel — the CAM workspace UI (tree + toolbar + backplot + sim + post), driving the
// C++ core's cam_* RPCs. File-static singleton state; standalone dock window.
#include "imgui.h"

#include "cam_panel.h"
#include "cam_mirror.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <string>
#include <vector>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#include "../../core/app_state.h"
#include "../../ui_theme.h"    // ui::palette (opaque title-bar / border override for this window)
#include "../../mesh.h"        // Mesh — playhead remnant-stock mesh
#include "../feature_tree.h"   // fmRowPill / fmTreeIcon / fmSectionDivider — the ratified FM row
                               // styling, reused so the Manufacture tree matches the FeatureManager

namespace ui {

namespace {

struct CamUI {
    CamMirror mirror;
    std::string docId;                 // cam job doc id ("" = no job)
    bool show = false;
    int  focusFrames = 0;              // >0: keep popping the CAM tab forward (set by camLoadJob)
    bool floatForShot = false;         // shot-only: float the panel so a headless capture sees it
    bool backplot = true;              // draw toolpaths in the 3D viewport
    int selSetup = 0;
    int selOp = -1;
    nlohmann::json lastExtract;        // last cam_extract_from_doc result (geometry source)
    std::string sourceModel;           // display name of the part doc the job extracts from
    std::vector<std::string> dialects;
    bool   dialectsInflight = false;   // async cam_post_dialects in flight (never re-fire)
    double dialectsNextTry  = 0.0;     // failure cooldown: no retry before this ImGui::GetTime()
    int postIdx = 0;
    // Long cam_* RPCs (extract/generate/simulate/post) ride AsyncRpc so the GL thread never
    // blocks. rpcInflight gates the ribbon/toolbar; callbacks run inside st.rpc.drain().
    int  rpcInflight = 0;
    int  rpcGen = 0;                   // bumped to drop stale callbacks after job switch
    // Workspace-tree state (drawCamTreeBody — the Manufacture left column):
    int  generatedRev = -1;            // mirror revision at the last successful cam_generate
    int  genWarnings  = 0;             // warning count from that generate (op status)
    bool flavorPopup  = false;         // ribbon "Flavor" clicked: open the dialect popup
    bool floatUndock  = false;         // one-shot: float (undock) the detail panel on next open
    int  selKind = 0;                  // visual tree selection: 0=none 1=job 2=jobsetup 3=stock
                                       // 4=setup 5=op 6=tool 7=models
    int  selTool = -1;                 // selected Tools row (selKind==6); separate from selOp
    std::string gcode;
    std::string gcodePath;             // desktop temp .nc path (empty on web)
    nlohmann::json sim;                // last cam_simulate result
    std::string status;                // last action status / error
    // Viewport playhead state (wave 3: animated material-removal sim in the 3D view)
    std::vector<nlohmann::json> simFrames;  // aicad.cam_sim_frames/1 field dicts
    std::vector<double> simFrameT;          // frame times (s), parallel to simFrames
    bool framesClamped = false;
    double playT = 0;                  // playhead time (s)
    bool playing = false;
    float playSpeed = 1.0f;
    int curFrame = -1;                 // frame index currently uploaded as aux mesh
    Mesh frameMesh;                    // built from simFrames[curFrame]
    bool frameMeshValid = false;
    bool showStock = false;            // stock box overlay
    bool optGouge = true, optRapid = true, optHolder = false;
    float optRes = 0.5f;
    CamBackplot bpBefore, bpAfter;     // split backplot for the playhead scrub
    double splitT = -1;                // playT the split was computed for
    // Wave 4: pipeline runner (Generate -> Simulate -> Post chained over AsyncRpc).
    int pipeStage = 0;                 // 0=idle 1=generate 2=simulate 3=post
    // Wave 4: last cam_audit result for the G-code tab.
    nlohmann::json audit;
    bool auditInflight = false;
    // operation form buffers (selected op)
    char opName[64] = "";
    float fStepover = 0, fStepdown = 0, fStockLeave = 0, fFeed = 0, fRpm = 0;
    float hTop = 0, hBottom = -5, hClear = 15, hRetract = 5;
    int loadedOpFor = -1;
};
CamUI g;

bool camBusy() { return g.rpcInflight > 0; }
void camBeginRpc() { ++g.rpcInflight; }
void camEndRpc() { if (g.rpcInflight > 0) --g.rpcInflight; }

// fwd (pipeline chains: generate -> simulate -> post)
void doSimulate(AppState& st);
void doPost(AppState& st);

// Active PART tab's doc id + display title. CAM extract always targets the part, not the job.
bool activePartDoc(AppState& st, std::string& docId, std::string& title) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) {
        g.status = "no active part";
        return false;
    }
    const auto& tab = st.docTabs[st.activeTab];
    if (tab.docId.empty()) {
        g.status = "active tab is not a part";
        return false;
    }
    docId = tab.docId;
    title = tab.title.empty() ? tab.docId : tab.title;
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

// Post-dialect list, fetched ONCE via the async rpc queue. This runs on per-frame draw paths
// (drawCamManager / the Manufacture tree), so it must never issue a synchronous worker.call:
// a failing worker would then be re-hit every frame from the GL thread (that pattern hard-froze
// the app once — see commit 50a54308). Guards: in-flight flag + a 10s failure cooldown.
void refreshDialects(AppState& st) {
    if (!g.dialects.empty() || g.dialectsInflight) return;
    if (ImGui::GetTime() < g.dialectsNextTry) return;
    g.dialectsInflight = true;
    st.rpc.call("cam_post_dialects", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.dialectsInflight = false;
        if (r.ok && r.result.contains("dialects"))
            for (auto& d : r.result["dialects"]) g.dialects.push_back(d.value("id", std::string()));
        if (g.dialects.empty()) g.dialectsNextTry = ImGui::GetTime() + 10.0;  // failed: cool down
    });
}

void loadTree(const nlohmann::json& r) { g.mirror.loadTree(r); }

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
    if (camCall(st, "cam_apply_ops", {{"doc_id", g.docId}, {"base_revision", -1}, {"ops", ops}}, r))
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
        std::filesystem::temp_directory_path() / ("aicad_cam." + dialect + ".nc");
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
    const std::string fname = "aicad_cam." + dialect + ".nc";
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
                g.loadedOpFor = -1;
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
        g.mirror.loadToolpaths(r.result);
        int n = r.result.contains("toolpaths") ? (int)r.result["toolpaths"].size() : 0;
        g.generatedRev = g.mirror.revision();
        g.genWarnings = (r.result.contains("warnings") && r.result["warnings"].is_array())
                            ? (int)r.result["warnings"].size() : 0;
        g.status = "generated " + std::to_string(n) + " toolpath(s)";
        if (g.genWarnings > 0) g.status += " (" + std::to_string(g.genWarnings) + " warning)";
        if (g.pipeStage == 1) { g.pipeStage = 2; doSimulate(*stp); }
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
    if (g.docId.empty()) return;
    if (camBusy()) { g.status = "CAM busy"; return; }
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
        g.sim = r.result;
        camAdoptSimResult(r.result);
        g.status = "simulated";
        if (g.pipeStage == 2) { g.pipeStage = 3; doPost(*stp); }
    });
}

void doPost(AppState& st) {
    if (g.docId.empty()) return;
    if (camBusy()) { g.status = "CAM busy"; return; }
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
        // Web has no local path to hand the operator — offer the Blob download immediately.
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
    const auto& op = g.mirror.setups()[g.selSetup].ops[g.selOp];
    const auto& stock = g.mirror.setups()[g.selSetup].stock;
    std::string material = stock.value("material", std::string("aluminum"));
    camBeginRpc();
    const int gen = g.rpcGen;
    const std::string docId = g.docId;
    st.rpc.call("cam_feeds",
                {{"doc_id", docId},
                 {"setup_id", g.mirror.setups()[g.selSetup].id},
                 {"tool_number", op.tool_number},
                 {"material", material}},
                [gen, docId, material](const core::RpcResult& r) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!r.ok) {
            g.status = std::string("cam_feeds: ") +
                       (r.err.empty() ? std::string("failed") : r.err);
            return;
        }
        auto num = [&](const char* k, float& dst) {
            if (r.result.contains(k) && r.result[k].is_number()) dst = r.result[k].get<float>();
        };
        num("rpm", g.fRpm);
        num("feed", g.fFeed);
        num("stepover", g.fStepover);
        num("stepdown", g.fStepdown);
        g.status = "feeds filled from tables (" + material + ")";
    });
}

// Wave 4e: deterministic G-code audit via the core cam_audit RPC.
void doAudit(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    if (g.auditInflight) return;
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

// ---- 2D backplot canvas (top view) ----
void drawBackplot(AppState& /*st*/) {
    const CamBackplot& bp = g.mirror.backplot();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.y < 80) avail.y = 200;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y), IM_COL32(24, 26, 30, 255));
    if (bp.empty()) {
        dl->AddText(ImVec2(p0.x + 10, p0.y + 10), IM_COL32(160, 160, 160, 255),
                    "No toolpaths. Add operations, then Generate.");
        ImGui::Dummy(avail);
        return;
    }
    float w = bp.maxx - bp.minx, h = bp.maxy - bp.miny;
    if (w < 1e-3f) w = 1; if (h < 1e-3f) h = 1;
    float pad = 12.0f;
    float sx = (avail.x - 2 * pad) / w, sy = (avail.y - 2 * pad) / h;
    float s = std::min(sx, sy);
    float ox = p0.x + pad - bp.minx * s;
    float oy = p0.y + avail.y - pad + bp.miny * s;  // flip Y for screen
    auto line = [&](const std::vector<float>& v, ImU32 col, float th) {
        for (size_t i = 0; i + 5 < v.size(); i += 6) {
            ImVec2 a(ox + v[i] * s, oy - v[i + 1] * s);
            ImVec2 b(ox + v[i + 3] * s, oy - v[i + 4] * s);
            dl->AddLine(a, b, col, th);
        }
    };
    line(bp.rapid,  IM_COL32(200, 60, 60, 120), 1.0f);   // rapid = faint red
    line(bp.lead,   IM_COL32(80, 200, 120, 200), 1.5f);  // lead = green
    line(bp.plunge, IM_COL32(80, 180, 220, 220), 1.5f);  // plunge = cyan
    line(bp.cut,    IM_COL32(90, 150, 240, 255), 1.6f);  // cut = blue
    line(bp.arc,    IM_COL32(150, 130, 240, 255), 1.6f); // arc = violet
    char info[96];
    std::snprintf(info, sizeof(info), "XY %.1f x %.1f mm   Z %.1f..%.1f", w, h, bp.minz, bp.maxz);
    dl->AddText(ImVec2(p0.x + 8, p0.y + 8), IM_COL32(150, 160, 170, 255), info);
    ImGui::Dummy(avail);
}

void drawSimSummary() {
    if (g.sim.empty() || !g.sim.contains("results")) { ImGui::TextDisabled("Run Simulate to see material removal + time."); return; }
    for (auto& r : g.sim["results"]) {
        ImGui::Separator();
        ImGui::Text("Stock:      %.0f mm^3", r.value("stock_volume_mm3", 0.0));
        ImGui::Text("Removed:    %.0f mm^3 (%.0f%%)", r.value("removed_volume_mm3", 0.0),
                    100.0 * r.value("machined_fraction", 0.0));
        ImGui::Text("Remaining:  %.0f mm^3", r.value("remaining_volume_mm3", 0.0));
        double t = r.value("cycle_time_s", 0.0);
        ImGui::Text("Cycle time: %dm %02ds", (int)(t / 60), (int)t % 60);
        int gouges = r.value("gouges", 0), rapids = r.value("rapid_collisions", 0);
        int holders = r.value("holder_collisions", 0);
        if (gouges > 0) { ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1], ui::palette.statusWarn[2], ui::palette.statusWarn[3]));
            ImGui::Text("GOUGES: %d (max %.3f mm)", gouges, r.value("max_gouge_mm", 0.0)); ImGui::PopStyleColor(); }
        if (rapids > 0) { ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1], ui::palette.statusWarn[2], ui::palette.statusWarn[3]));
            ImGui::Text("RAPID THROUGH STOCK: %d", rapids); ImGui::PopStyleColor(); }
        if (holders > 0) { ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1], ui::palette.statusWarn[2], ui::palette.statusWarn[3]));
            ImGui::Text("HOLDER/SHANK COLLISIONS: %d", holders); ImGui::PopStyleColor(); }
        if (gouges == 0 && rapids == 0 && holders == 0) { ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusOk[0], ui::palette.statusOk[1], ui::palette.statusOk[2], ui::palette.statusOk[3]));
            ImGui::TextUnformatted("No gouges or rapid collisions."); ImGui::PopStyleColor(); }
    }
}

}  // namespace

// ---- ribbon command handler ----
void camHandleCommand(AppState& st, const std::string& cmd) {
    // In the Manufacture WORKSPACE the CAM job tree (left column) + ribbon are the primary UI:
    // commands fire silently against the tree selection and must NOT pop the floating detail
    // panel. Everywhere else (e.g. the Manufacture ribbon tab from the Model workspace) keep the
    // legacy behavior of revealing the panel.
    const bool camWs = (st.activeWorkspace == AppState::Workspace::Manufacture);
    if (!camWs) g.show = true;
    refreshDialects(st);
    if (cmd == "cam_show3d") {          // ribbon Output toggle: viewport backplot overlay
        g.backplot = !g.backplot;
        g.status = g.backplot ? "toolpaths shown in 3D" : "toolpaths hidden";
        return;
    }
    if (cmd == "cam_flavor") {          // ribbon Output dropdown: pick the post dialect
        if (camWs) g.flavorPopup = true;    // popup rendered by drawCamTreeBody (left column)
        else       g.show = true;           // elsewhere: the panel's combo has the picker
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
        ++g.rpcGen;  // drop any in-flight extract/generate callbacks for the old job
        g.docId.clear();
        g.mirror.clear();
        g.gcode.clear();
        g.gcodePath.clear();
        g.sim = nlohmann::json();
        g.lastExtract = nlohmann::json();
        g.generatedRev = -1;
        g.genWarnings = 0;
        ensureJob(st);
        g.status = "new CAM job";
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
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_setup"},
            {"setup", {{"name", "Setup" + std::to_string(g.mirror.setups().size() + 1)},
                       {"machine", {{"kind", "mill3"}}},
                       {"stock", {{"type", "box_offset"}, {"off_ztop", 1.0}, {"material", "aluminum"}}}}}}}));
        g.selSetup = (int)g.mirror.setups().size() - 1;
        return;
    }
    if (cmd == "cam_add_tool") {
        if (!ensureJob(st)) return;
        int n = (int)g.mirror.tools().size() + 1;
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_tool"}, {"tool", {{"number", n}, {"type", "endmill_flat"}, {"diameter", 6}, {"flutes", 3},
                                          {"name", "6mm flat endmill"}}}}}));
        g.status = "added tool T" + std::to_string(n);
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
    // No g.show gate anymore: in the Manufacture workspace the 3D viewport is the central body
    // and the floating panel stays closed — the ribbon "Show in 3D" toggle (g.backplot) is the
    // one switch for the overlay.
    if (!g.backplot || g.mirror.backplot().empty()) return nullptr;
    return &g.mirror.backplot();
}

// Adopt a cam_simulate response: parse the selected setup's frames into the playhead state.
// Called by doSimulate's async callback and by headless scenarios (sync worker.call path).
void camAdoptSimResult(const nlohmann::json& result) {
    g.simFrames.clear(); g.simFrameT.clear();
    g.framesClamped = false; g.playT = 0; g.playing = false;
    g.curFrame = -1; g.frameMeshValid = false; g.splitT = -1;
    if (!result.contains("results") || !result["results"].is_array()) return;
    const nlohmann::json* pick = nullptr;
    const auto& results = result["results"];
    if (g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size()) {
        const std::string& sid = g.mirror.setups()[g.selSetup].id;
        for (const auto& e : results)
            if (e.value("setup_id", std::string()) == sid) { pick = &e; break; }
    }
    if (!pick && !results.empty()) pick = &results[0];
    if (pick && pick->contains("frames") && (*pick)["frames"].is_array()) {
        for (const auto& f : (*pick)["frames"]) {
            if (!f.contains("field") || !f.contains("t_s")) continue;
            g.simFrames.push_back(f["field"]);
            g.simFrameT.push_back(f["t_s"].get<double>());
        }
        g.framesClamped = pick->value("frames_clamped", false);
    }
}

void camAdoptToolpaths(const nlohmann::json& result) {
    g.mirror.loadToolpaths(result);
    g.generatedRev = g.mirror.revision();
    g.genWarnings = (result.contains("warnings") && result["warnings"].is_array())
                        ? (int)result["warnings"].size() : 0;
}

// ---- viewport playhead (wave 3) ----
bool camSimHudActive() {
    return g.backplot && !g.mirror.backplot().empty() && !g.simFrames.empty();
}

// Split backplot at the current playT, lazily re-splitting only when playT changed.
const std::pair<const CamBackplot*, const CamBackplot*>& camBackplotSplit() {
    static std::pair<const CamBackplot*, const CamBackplot*> out;
    if (g.splitT != g.playT) {
        g.mirror.backplot().splitAt(g.playT, g.bpBefore, g.bpAfter);
        g.splitT = g.playT;
    }
    out = {&g.bpBefore, &g.bpAfter};
    return out;
}

// Frame index for the current playT (lower_bound over frame times).
int camCurrentFrameIndex() {
    if (g.simFrameT.empty()) return -1;
    auto it = std::lower_bound(g.simFrameT.begin(), g.simFrameT.end(), g.playT);
    if (it == g.simFrameT.end()) return (int)g.simFrameT.size() - 1;
    return (int)(it - g.simFrameT.begin());
}

// Rebuild the remnant mesh for frame `idx` if needed; returns true when a mesh is ready.
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

// Tool pose at time t: walk the segment time arrays (cut/lead/plunge/arc; rapids first-fit).
bool camToolPoseAt(double t, float outPos[3]) {
    const CamBackplot& bp = g.mirror.backplot();
    auto walk = [&](const std::vector<float>& segs, const std::vector<float>& times) -> bool {
        for (size_t i = 0; i + 5 < segs.size(); i += 6) {
            double tEnd = (i / 6 < times.size()) ? times[i / 6] : 0;
            double tStart = (i >= 6) ? times[i / 6 - 1] : 0.0;
            if (t <= tEnd) {
                double f = (tEnd > tStart) ? (t - tStart) / (tEnd - tStart) : 1.0;
                outPos[0] = segs[i]   + (segs[i+3] - segs[i])   * (float)f;
                outPos[1] = segs[i+1] + (segs[i+4] - segs[i+1]) * (float)f;
                outPos[2] = segs[i+2] + (segs[i+5] - segs[i+2]) * (float)f;
                return true;
            }
        }
        return false;
    };
    // prefer cutting geometry (the tool's working position)
    if (walk(bp.cut, bp.cutT) || walk(bp.arc, bp.arcT) || walk(bp.lead, bp.leadT) ||
        walk(bp.plunge, bp.plungeT) || walk(bp.rapid, bp.rapidT)) return true;
    return false;
}

double camPlayTotalTime() {
    return g.mirror.backplot().totalTime;
}
double camPlayT() { return g.playT; }

bool camStockBoxVisible() { return g.showStock && g.selSetup >= 0 &&
                                  g.selSetup < (int)g.mirror.setups().size(); }

// 12 edges of the selected setup's box stock (world mm); false when the stock isn't a box.
bool camStockBoxSegments(std::vector<float>& out) {
    if (g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size()) return false;
    const nlohmann::json& stock = g.mirror.setups()[g.selSetup].stock;
    if (!stock.contains("min") || !stock.contains("max")) return false;
    const auto& mn = stock["min"]; const auto& mx = stock["max"];
    if (!mn.is_array() || mn.size() < 3 || !mx.is_array() || mx.size() < 3) return false;
    float x0 = mn[0].get<float>(), y0 = mn[1].get<float>(), z0 = mn[2].get<float>();
    float x1 = mx[0].get<float>(), y1 = mx[1].get<float>(), z1 = mx[2].get<float>();
    const float c[8][3] = {{x0,y0,z0},{x1,y0,z0},{x1,y1,z0},{x0,y1,z0},
                           {x0,y0,z1},{x1,y0,z1},{x1,y1,z1},{x0,y1,z1}};
    const int e[12][2] = {{0,1},{1,2},{2,3},{3,0},{4,5},{5,6},{6,7},{7,4},
                          {0,4},{1,5},{2,6},{3,7}};
    out.clear();
    for (auto& edge : e) {
        out.push_back(c[edge[0]][0]); out.push_back(c[edge[0]][1]); out.push_back(c[edge[0]][2]);
        out.push_back(c[edge[1]][0]); out.push_back(c[edge[1]][1]); out.push_back(c[edge[1]][2]);
    }
    return true;
}

// Bottom-center transport bar over the viewport (drawn in screen space by viewport_panel).
void camDrawSimHud(AppState& st, const ImVec2& vpMin, const ImVec2& vpSize) {
    if (!camSimHudActive()) return;
    double total = g.mirror.backplot().totalTime;
    if (total <= 0) return;
    if (g.playing) {
        g.playT += ImGui::GetIO().DeltaTime * g.playSpeed;
        if (g.playT >= total) { g.playT = total; g.playing = false; }
    }
    const float w = 420.0f, h = 56.0f;
    ImVec2 pos(vpMin.x + (vpSize.x - w) * 0.5f, vpMin.y + vpSize.y - h - 14.0f);
    ImGui::SetNextWindowPos(pos);
    ImGui::SetNextWindowSize(ImVec2(w, h));
    ImGui::SetNextWindowBgAlpha(0.55f);
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                             ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
                             ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoNav;
    if (ImGui::Begin("##cam_sim_hud", nullptr, flags)) {
        if (ImGui::SmallButton(g.playing ? "Pause" : "Play")) g.playing = !g.playing;
        ImGui::SameLine();
        float tf = (float)g.playT;
        ImGui::SetNextItemWidth(w - 150.0f);
        if (ImGui::SliderFloat("##camt", &tf, 0.0f, (float)total, "")) g.playT = tf;
        ImGui::SameLine();
        int mm = (int)(g.playT / 60), ss = (int)g.playT % 60;
        ImGui::Text("%d:%02d", mm, ss);
        const float speeds[] = {0.25f, 0.5f, 1.0f, 2.0f, 4.0f};
        const char* labels[] = {"0.25x", "0.5x", "1x", "2x", "4x"};
        int cur = 2;
        for (int i = 0; i < 5; ++i) if (std::fabs(speeds[i] - g.playSpeed) < 0.01f) cur = i;
        ImGui::SetNextItemWidth(70.0f);
        if (ImGui::BeginCombo("##camspeed", labels[cur])) {
            for (int i = 0; i < 5; ++i)
                if (ImGui::Selectable(labels[i], i == cur)) g.playSpeed = speeds[i];
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        // machined % interpolated between frame times
        double pct = 0;
        if (!g.simFrameT.empty() && g.simFrameT.back() > 0)
            pct = 100.0 * std::min(1.0, g.playT / g.simFrameT.back());
        ImGui::Text("machined %.0f%%", pct);
        if (g.framesClamped) { ImGui::SameLine(); ImGui::TextDisabled("(!) frames clamped"); }
    }
    ImGui::End();
}

// Shot-only: park the playhead at a fraction of total time for a headless capture.
void camDebugSetPlayhead(float frac) {
    double total = g.mirror.backplot().totalTime;
    if (frac < 0) frac = 0; if (frac > 1) frac = 1;
    g.playT = total * frac;
}

void camEnsureVisible() { g.show = true; }

void camRequestFocus() {
    g.show = true;
    // Keep focusFrames non-zero every frame the Manufacture workspace is active so the CAM window
    // wins the docked-tab race against the Viewport window in the shared central dock node.
    // Set to 3 each frame so it's always non-zero (drawCamManager decrements by 1 per frame).
    g.focusFrames = 3;
}

// Shot-only hook: make the CAM panel float (undock + centre) so a headless screenshot of the
// AI_CAD_SHOT=cam scenario can actually show the populated workspace. Never called in normal use.
void camFloatForShot() { g.floatForShot = true; g.show = true; }

// Adopt an existing CAM job doc (built core-side by the copilot's CAM fast-path) into the
// panel so it shows up populated instead of empty. Loads the job tree (setups/ops/tools).
void camLoadJob(AppState& st, const std::string& docId) {
    if (docId.empty()) return;
    ++g.rpcGen;  // drop callbacks tied to a previous job
    g.docId = docId;
    g.selSetup = 0;
    g.selOp = -1;
    g.loadedOpFor = -1;
    g.gcode.clear();
    g.gcodePath.clear();
    g.sim = nlohmann::json();
    g.simFrames.clear(); g.simFrameT.clear();
    g.framesClamped = false; g.playT = 0; g.playing = false;
    g.curFrame = -1; g.frameMeshValid = false; g.splitT = -1;
    nlohmann::json r;
    if (camCall(st, "cam_tree", {{"doc_id", docId}}, r)) {
        loadTree(r);
        g.status = "loaded CAM job from copilot";
        // cam_program returns gcode in its envelope; tree usually does not. Opportunistically
        // seed if a future tree payload (or a stashed side-channel) carries it.
        if (r.contains("gcode") && r["gcode"].is_string()) {
            g.gcode = r["gcode"].get<std::string>();
            if (!g.gcode.empty()) writeGcodeFile(currentDialect());
        }
    }
    // Pull generated toolpaths async so the GL thread never blocks on cam_generate
    // (job was already generated core-side by cam_program; response is the cached paths).
    // After toolpaths land, seed g.gcode via cam_post when the load path didn't already
    // carry it (cam_program produced gcode server-side but only the doc id reaches us).
    AppState* stp = &st;
    camBeginRpc();
    const int gen = g.rpcGen;
    st.rpc.call("cam_generate", {{"doc_id", docId}},
                [gen, docId, stp](const core::RpcResult& rr) {
        camEndRpc();
        if (gen != g.rpcGen || g.docId != docId) return;
        if (!rr.ok) return;
        g.mirror.loadToolpaths(rr.result);
        g.generatedRev = g.mirror.revision();  // copilot jobs arrive pre-generated
        g.genWarnings = (rr.result.contains("warnings") && rr.result["warnings"].is_array())
                            ? (int)rr.result["warnings"].size() : 0;
        // Future-proof: if generate ever echoes gcode, seed the G-code tab.
        if (g.gcode.empty() && rr.result.contains("gcode") && rr.result["gcode"].is_string()) {
            g.gcode = rr.result["gcode"].get<std::string>();
            if (!g.gcode.empty()) writeGcodeFile(currentDialect());
        }
        // cam_program already posted; re-post cheaply (cached toolpaths) to seed g.gcode
        // so Download NC works without a manual Post click.
        if (g.gcode.empty()) {
            const std::string dialect = currentDialect();
            camBeginRpc();
            stp->rpc.call("cam_post", {{"doc_id", docId}, {"dialect", dialect}},
                          [gen, docId, dialect](const core::RpcResult& pr) {
                camEndRpc();
                if (gen != g.rpcGen || g.docId != docId) return;
                if (!pr.ok) return;
                g.gcode = pr.result.value("gcode", std::string());
                if (!g.gcode.empty()) writeGcodeFile(dialect);
            });
        }
    });
    g.show = true;
    g.focusFrames = 10;  // surface the CAM tab for a few frames even if the panel was already
                         // open in the background (out-competes a one-off viewport focus grab)
}

// ---- the panel ----
void drawCamManager(AppState& st) {
    if (!g.show) return;
    refreshDialects(st);
    // Pop the CAM tab forward the frame the workspace first becomes visible (the user switched to
    // the Manufacture ribbon), so the docked CAM editor surfaces instead of staying behind the
    // Viewport tab. Mirrors the ECAD panel. Docked into the central node by buildDefaultLayout.
    // Shot-only (set via camFloatForShot() from the AI_CAD_SHOT=cam scenario): float the panel
    // (undock + centre) so a headless capture sees the populated workspace — a central-node-docked
    // tab won't surface over the full-bleed 3D in an offscreen render. No-op in normal use.
    if (g.floatForShot) {
        ImGui::SetNextWindowDockID(0, ImGuiCond_Always);
        ImGui::SetNextWindowPos(ImVec2(330, 210), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(660, 470), ImGuiCond_Always);
    } else if (g.floatUndock) {
        // Opened as the DETAIL editor from the Manufacture workspace tree: float it (undock)
        // so it never steals the central dock node from the 3D viewport in that workspace.
        ImGui::SetNextWindowDockID(0, ImGuiCond_Always);
        ImGui::SetNextWindowPos(ImVec2(360, 220), ImGuiCond_Appearing);
        ImGui::SetNextWindowSize(ImVec2(720, 480), ImGuiCond_Appearing);
        g.floatUndock = false;
    } else {
        ImGui::SetNextWindowSize(ImVec2(720, 480), ImGuiCond_FirstUseEver);
    }
    static bool prevShown = false;
    if (g.focusFrames > 0 || (g.show && !prevShown)) ImGui::SetNextWindowFocus();  // applies to the Begin below
    prevShown = g.show;
    if (g.focusFrames > 0) g.focusFrames--;
    // The global theme makes window title bars fully transparent (a clean look for docked panels),
    // but a FLOATING CAM window then reads as see-through at the top. Give this window an opaque
    // title bar and a solid (fully opaque) border so it stands on its own whether floating or
    // docked. Pushed before Begin (window decoration colours are read there) and popped right after.
    const ui::Palette& pal = ui::palette;
    const ImVec4 titleOpaque(pal.headerBg[0], pal.headerBg[1], pal.headerBg[2], 1.0f);
    float camBorder[4]; ui::Palette::withAlpha(pal.border, camBorder, 1.0f);
    ImGui::PushStyleColor(ImGuiCol_TitleBg,       titleOpaque);
    ImGui::PushStyleColor(ImGuiCol_TitleBgActive, titleOpaque);
    ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(camBorder[0], camBorder[1], camBorder[2], camBorder[3]));
    const bool camOpen = ImGui::Begin("CAM", &g.show);
    ImGui::PopStyleColor(3);
    if (!camOpen) { ImGui::End(); return; }

    // toolbar — long RPCs (extract/generate/sim/post) disable action buttons + show spinner.
    const bool busy = camBusy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("New Job")) camHandleCommand(st, "cam_new_job");
    ImGui::SameLine(); if (ImGui::Button("Extract Part")) camHandleCommand(st, "cam_extract");
    ImGui::SameLine(); if (ImGui::Button("Add Setup")) camHandleCommand(st, "cam_add_setup");
    ImGui::SameLine(); if (ImGui::Button("Add Tool")) camHandleCommand(st, "cam_add_tool");
    ImGui::Separator();
    // operation buttons
    auto opBtn = [&](const char* label, const char* cmd) {
        if (ImGui::Button(label)) camHandleCommand(st, cmd);
        ImGui::SameLine();
    };
    opBtn("Face", "cam_op_face"); opBtn("Contour", "cam_op_contour"); opBtn("Pocket", "cam_op_pocket");
    opBtn("Adaptive", "cam_op_adaptive"); opBtn("Drill", "cam_op_drill"); opBtn("3D Parallel", "cam_op_parallel3d");
    opBtn("Waterline", "cam_op_waterline3d"); opBtn("Turn", "cam_op_turn_rough");
    if (ImGui::Button("Laser")) camHandleCommand(st, "cam_op_laser");
    ImGui::Separator();
    if (ImGui::Button("Generate")) doGenerate(st);
    ImGui::SameLine(); if (ImGui::Button("Simulate")) doSimulate(st);
    ImGui::SameLine(); if (ImGui::Button("Run: Generate > Simulate > Post")) doPipeline(st);
    ImGui::SameLine(); ImGui::Checkbox("Show in 3D", &g.backplot);
    ImGui::SameLine(); ImGui::Checkbox("Stock box", &g.showStock);
    ImGui::SameLine(); ImGui::TextDisabled("|");
    ImGui::SameLine(); ImGui::SetNextItemWidth(120);
    if (!g.dialects.empty()) {
        std::vector<const char*> items;
        for (auto& d : g.dialects) items.push_back(d.c_str());
        ImGui::Combo("##post", &g.postIdx, items.data(), (int)items.size());
        ImGui::SameLine(); if (ImGui::Button("Post")) doPost(st);
    }
    if (busy) ImGui::EndDisabled();
    if (busy) {
        ImGui::SameLine();
        ImGui::TextDisabled("|");
        ImGui::SameLine();
        // Lightweight spinner: rotating braille dots via frame time (no theme dependency).
        static const char* kSpin[] = {"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"};
        const int fi = ((int)(ImGui::GetTime() * 10.0)) % 10;
        ImGui::TextDisabled("%s working...", kSpin[fi]);
    }
    if (!g.status.empty()) { ImGui::SameLine(); ImGui::TextDisabled("%s", g.status.c_str()); }

    ImGui::Separator();
    ImGui::Columns(2, "cam_cols");
    ImGui::SetColumnWidth(0, 240);

    // ---- LEFT: tree ----
    ImGui::BeginChild("cam_tree");
    ImGui::TextDisabled("Job: %s  (rev %d)", g.mirror.name().c_str(), g.mirror.revision());
    const auto& setups = g.mirror.setups();
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        ImGui::PushID(si);
        bool open = ImGui::TreeNodeEx(s.name.c_str(), (si == g.selSetup ? ImGuiTreeNodeFlags_DefaultOpen : 0) |
                                                          ImGuiTreeNodeFlags_OpenOnArrow);
        if (ImGui::IsItemClicked()) g.selSetup = si;
        if (open) {
            ImGui::TextDisabled("machine: %s", s.machine.c_str());
            for (int oi = 0; oi < (int)s.ops.size(); ++oi) {
                const auto& o = s.ops[oi];
                std::string lbl = o.name.empty() ? o.type : o.name;
                lbl += "  [T" + std::to_string(o.tool_number) + "]";
                if (o.suppressed) lbl += " (off)";
                if (ImGui::Selectable(lbl.c_str(), si == g.selSetup && oi == g.selOp)) {
                    g.selSetup = si; g.selOp = oi; g.loadedOpFor = -1;
                }
            }
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    ImGui::Separator();
    ImGui::TextDisabled("Tools");
    for (const auto& t : g.mirror.tools())
        ImGui::BulletText("T%d %s  \xC3\x98%.2f", t.number, t.type.c_str(), t.diameter);
    ImGui::EndChild();

    // ---- RIGHT: tabs ----
    ImGui::NextColumn();
    if (ImGui::BeginTabBar("cam_tabs")) {
        if (ImGui::BeginTabItem("Toolpaths")) { drawBackplot(st); ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Simulate")) {
            if (busy) ImGui::BeginDisabled();
            if (ImGui::Button("Run Material-Removal Sim")) doSimulate(st);
            ImGui::SameLine(); ImGui::Checkbox("Gouge", &g.optGouge);
            ImGui::SameLine(); ImGui::Checkbox("Rapids", &g.optRapid);
            ImGui::SameLine(); ImGui::Checkbox("Holder", &g.optHolder);
            ImGui::SameLine(); ImGui::SetNextItemWidth(110);
            ImGui::SliderFloat("Resolution", &g.optRes, 0.2f, 2.0f, "%.1f mm");
            if (busy) ImGui::EndDisabled();
            drawSimSummary();
            ImGui::TextDisabled("Frames play back in the 3D viewport — use the transport bar.");
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Operation")) {
            // Minimal op parameter form: the four CuttingDB fields, autofilled from the
            // core cam_feeds table for the setup material + op tool, applied via update_operation.
            const bool haveOp = g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size() &&
                                g.selOp >= 0 && g.selOp < (int)g.mirror.setups()[g.selSetup].ops.size();
            if (!haveOp) {
                ImGui::TextDisabled("Select an operation in the tree to edit its parameters.");
            } else {
                const auto& op = g.mirror.setups()[g.selSetup].ops[g.selOp];
                ImGui::Text("%s  [T%d]", (op.name.empty() ? op.type.c_str() : op.name.c_str()), op.tool_number);
                if (g.loadedOpFor != g.selOp) {
                    // reset buffers on op switch; autofill (or a future read-back) populates them
                    g.loadedOpFor = g.selOp;
                }
                ImGui::SetNextItemWidth(120); ImGui::InputFloat("Stepover (mm)", &g.fStepover, 0, 0, "%.2f");
                ImGui::SetNextItemWidth(120); ImGui::InputFloat("Stepdown (mm)", &g.fStepdown, 0, 0, "%.2f");
                ImGui::SetNextItemWidth(120); ImGui::InputFloat("Feed (mm/min)", &g.fFeed, 0, 0, "%.0f");
                ImGui::SetNextItemWidth(120); ImGui::InputFloat("RPM", &g.fRpm, 0, 0, "%.0f");
                if (busy) ImGui::BeginDisabled();
                if (ImGui::Button("Autofill from tables")) doFeedsAutofill(st);
                ImGui::SameLine();
                if (ImGui::Button("Apply to operation")) {
                    nlohmann::json upd = nlohmann::json::object();
                    if (g.fStepover > 0) upd["stepover"] = g.fStepover;
                    if (g.fStepdown > 0) upd["stepdown"] = g.fStepdown;
                    if (g.fFeed > 0) upd["feed"] = g.fFeed;
                    if (g.fRpm > 0) upd["rpm"] = g.fRpm;
                    if (!upd.empty()) {
                        nlohmann::json r = applyOps(st, nlohmann::json::array({nlohmann::json{
                            {"op", "update_operation"}, {"op_id", op.id}, {"operation", upd}}}));
                        g.status = r.contains("revision") ? "operation updated" : "update failed";
                    }
                }
                if (busy) ImGui::EndDisabled();
            }
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("G-code")) {
            if (busy) ImGui::BeginDisabled();
            if (ImGui::Button("Post")) doPost(st);
            if (busy) ImGui::EndDisabled();
            ImGui::SameLine();
            const bool canDl = !g.gcode.empty();
            if (!canDl) ImGui::BeginDisabled();
            if (ImGui::Button("Download NC")) downloadGcode();
            if (!canDl) ImGui::EndDisabled();
            ImGui::SameLine();
            if (g.auditInflight) ImGui::BeginDisabled();
            if (ImGui::Button("Audit")) doAudit(st);
            if (g.auditInflight) ImGui::EndDisabled();
            if (!g.audit.empty()) {
                const bool aok = g.audit.value("ok", false);
                const float* col = aok ? ui::palette.statusOk : ui::palette.statusErr;
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(col[0], col[1], col[2], col[3]));
                ImGui::Text("Audit: %s", aok ? "ok" : "FAILED");
                ImGui::PopStyleColor();
                if (g.audit.contains("stats") && g.audit["stats"].is_object()) {
                    const auto& s = g.audit["stats"];
                    ImGui::Text("lines %d, tool changes %d, est %.0fs",
                                s.value("lines", 0), s.value("tool_changes", 0),
                                s.value("est_time_s", 0.0));
                    if (s.contains("bounds") && s["bounds"].is_object()) {
                        const auto& b = s["bounds"];
                        auto rng = [&](const char* k) {
                            if (!b.contains(k) || !b[k].is_array() || b[k].size() < 2)
                                return std::string("?");
                            char buf[48];
                            std::snprintf(buf, sizeof(buf), "[%.1f..%.1f]",
                                          b[k][0].get<double>(), b[k][1].get<double>());
                            return std::string(buf);
                        };
                        ImGui::Text("bounds X%s Y%s Z%s mm",
                                    rng("x").c_str(), rng("y").c_str(), rng("z").c_str());
                    }
                }
                if (g.audit.contains("errors") && g.audit["errors"].is_array() &&
                    !g.audit["errors"].empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1], ui::palette.statusErr[2], ui::palette.statusErr[3]));
                    for (const auto& e : g.audit["errors"]) ImGui::BulletText("%s", e.get<std::string>().c_str());
                    ImGui::PopStyleColor();
                }
                if (g.audit.contains("warnings") && g.audit["warnings"].is_array() &&
                    !g.audit["warnings"].empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1], ui::palette.statusWarn[2], ui::palette.statusWarn[3]));
                    for (const auto& w : g.audit["warnings"]) ImGui::BulletText("%s", w.get<std::string>().c_str());
                    ImGui::PopStyleColor();
                }
            }
#ifndef __EMSCRIPTEN__
            // Desktop: show the temp .nc path the operator can grab. Hidden on web (no local path).
            if (!g.gcodePath.empty()) {
                ImGui::SameLine();
                ImGui::TextDisabled("-> %s", g.gcodePath.c_str());
            }
#endif
            ImGui::Separator();
            ImGui::InputTextMultiline("##gcode", g.gcode.data(), g.gcode.size() + 1,
                                      ImVec2(-1, -1), ImGuiInputTextFlags_ReadOnly);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::Columns(1);
    ImGui::End();
}

// ---- the Manufacture workspace LEFT TREE ------------------------------------------------------
// Drawn INSIDE drawLeftPane's pinned glass column (layout.cpp routes the Manufacture workspace
// here, the same way Simulate routes to SimManager::drawTreeBody). Shows the real job structure —
// Job / Job Setup / Stock Setup / Machining Setups (ops with status) / Tools / Models — styled
// with the exported FeatureManager row helpers. Selecting a node sets the context the ribbon
// commands act on (g.selSetup / g.selOp); double-click opens the floating panel as the detail
// editor. NO RPCs on this per-frame path (refreshDialects is async + cooldown-guarded).
namespace {

void camTreeOpenDetail() { g.show = true; g.focusFrames = 3; g.floatUndock = true; }

}  // namespace

void drawCamTreeBody(AppState& st) {
    const float S = ui::uiScale;
    refreshDialects(st);   // async + guarded; feeds the flavor popup + Job Setup row
    fmRowStripeReset();

    // Compact vertical rhythm (match FeatureManager): tighten ItemSpacing.y only so the job tree
    // (setups/operations/tools) doesn't read "double spaced". RAII so it unwinds on early returns.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * S));
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
        for (int i = 0; i < (int)g.dialects.size(); ++i)
            if (ImGui::Selectable(g.dialects[i].c_str(), i == g.postIdx)) {
                g.postIdx = i;
                g.status = "post flavor: " + g.dialects[i];
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

    // Leaf row helper: FM pill + 16px icon + selectable label. Returns clicked; double-click
    // opens the detail editor.
    // Draw one leaf row's selectable+label against the SAME frame-height rect the pill/icon use.
    // The Selectable is sized to GetFrameHeight() (matching fmRowPill's rowMax) and its text is
    // vertically centered; it is NOT given `selected` — selection is painted by fmRowPill alone
    // (a `selected` Selectable would paint ImGui's default header rect at the text-height bb, on
    // top of the full-height glass card, which is the offset/glitching band the user reported).
    auto leafSelectable = [&](const std::string& label) -> bool {
        const float rowH = ImGui::GetFrameHeight();
        ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign, ImVec2(0.0f, 0.5f));
        bool clicked = ImGui::Selectable(label.c_str(), false,
                                         ImGuiSelectableFlags_AllowDoubleClick,
                                         ImVec2(0.0f, rowH));
        ImGui::PopStyleVar();
        return clicked;
    };
    auto leafRow = [&](const char* id, const char* icon, const char* fb,
                       const std::string& label, bool selected) -> bool {
        ImGui::PushID(id);
        FmRowRect rr = fmRowPill(selected);
        fmTreeIcon(icon, fb);
        ImGui::SameLine(0, 4.0f * S);
        bool clicked = leafSelectable(label);
        const bool dbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0);
        fmRowPillEnd(rr, selected);
        ImGui::PopID();
        if (dbl) camTreeOpenDetail();
        return clicked;
    };

    // ---- Job root ----
    const bool jobSel = (g.selKind == 1);
    bool jobOpen = false;
    {
        FmRowRect rr = fmRowPill(jobSel);
        fmTreeIcon("new_doc", "J");
        ImGui::SameLine(0, 4.0f * S);
        std::string jobLbl = (g.mirror.name().empty() ? std::string("CAM Job") : g.mirror.name())
                             + "  (rev " + std::to_string(g.mirror.revision()) + ")";
        jobOpen = ImGui::TreeNodeEx("##cam_job",
            ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_OpenOnArrow |
            ImGuiTreeNodeFlags_SpanAvailWidth, "%s", jobLbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) g.selKind = 1;
        if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0)) camTreeOpenDetail();
        fmRowPillEnd(rr, jobSel);
        if (!jobOpen) {
            if (!g.status.empty()) { ImGui::Spacing(); ImGui::TextDisabled("%s", g.status.c_str()); }
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
        // Inlined (not leafRow) so the icon slot draws a clean stock-billet glyph instead of the
        // "extrude" atlas name falling through to the bare "K" text fallback. The billet replicates
        // treeIcon()'s geometry exactly (reserve a frame-height Dummy, paint centered in it) so it
        // shares the size/baseline of the gear/csys glyphs on the neighbouring rows.
        const bool stockSel = (g.selKind == 3);
        ImGui::PushID("cam_stock");
        FmRowRect rr = fmRowPill(stockSel);
        {
            const float px     = 16.0f * S;
            const float insetX = ImGui::GetStyle().FramePadding.x;
            const float rowH   = ImGui::GetFrameHeight();
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            ImGui::Dummy(ImVec2(insetX + px, rowH));
            ImDrawList* dl = ImGui::GetWindowDrawList();
            // Match the secondary-ink weight of the atlas row glyphs without reaching into
            // feature_tree.cpp's (file-static) fmSurf(): the current Text colour at ~0.85 alpha.
            ImVec4 tc = ImGui::GetStyleColorVec4(ImGuiCol_Text); tc.w *= 0.85f;
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(tc);
            const float th = std::max(1.0f, 1.2f * S);
            // billet box: a front face + a back face pushed up-right, joined at the 4 corners.
            const float x0 = p0.x + insetX, y0 = p0.y + (rowH - px) * 0.5f;
            const float d  = px * 0.28f;                 // isometric depth offset
            const float fw = px - d, fh = px - d;        // front-face extent (leaves room for depth)
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
        ImGui::SameLine(0, 4.0f * S);
        if (leafSelectable(lbl)) g.selKind = 3;
        const bool stockDbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0);
        fmRowPillEnd(rr, stockSel);
        ImGui::PopID();
        if (stockDbl) camTreeOpenDetail();
    }

    // ---- Machining Setups (WCS/machine) + their operations -------------------------------------
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        const bool setupSel = (g.selKind == 4 && g.selSetup == si);
        ImGui::PushID(si);
        FmRowRect rr = fmRowPill(setupSel);
        fmTreeIcon("ref_csys", "W");
        ImGui::SameLine(0, 4.0f * S);
        std::string lbl = s.name + "  —  " + (s.machine.empty() ? "mill" : s.machine);
        const bool open = ImGui::TreeNodeEx("##cam_setup",
            (si == g.selSetup ? ImGuiTreeNodeFlags_DefaultOpen : 0) |
            ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth,
            "%s", lbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
            g.selKind = 4; g.selSetup = si; g.selOp = -1;
        }
        if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0)) camTreeOpenDetail();
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
                    } else if (!g.sim.empty() && g.sim.contains("results")) {
                        for (const auto& sr : g.sim["results"])
                            if (sr.value("gouges", 0) == 0 && sr.value("rapid_collisions", 0) == 0 &&
                                sr.value("holder_collisions", 0) == 0) {
                                badge = "clean"; badgeCol = ui::palette.statusOk;
                                break;
                            }
                    }
                }
                ImGui::PushID(oi);
                FmRowRect orr = fmRowPill(opSel);
                fmTreeIcon(("cam_" + o.type).c_str(), "o", 16.0f, o.suppressed ? 0.45f : 1.0f);
                ImGui::SameLine(0, 4.0f * S);
                if (leafSelectable(ol)) {
                    g.selKind = 5; g.selSetup = si; g.selOp = oi; g.loadedOpFor = -1;
                }
                if (badge) {
                    ImGui::SameLine(0, 6.0f * S);
                    ImGui::TextColored(ImVec4(badgeCol[0], badgeCol[1], badgeCol[2], badgeCol[3]), "%s", badge);
                }
                const bool dbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0);
                if (ImGui::BeginPopupContextItem("##cam_op_ctx")) {
                    if (ImGui::MenuItem(o.suppressed ? "Unsuppress" : "Suppress"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "update_operation"}, {"op_id", o.id},
                            {"operation", {{"suppressed", !o.suppressed}}}});
                    if (ImGui::MenuItem("Delete"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "remove_operation"}, {"op_id", o.id}});
                    if (ImGui::MenuItem("Open in editor")) camTreeOpenDetail();
                    ImGui::EndPopup();
                }
                fmRowPillEnd(orr, opSel);
                ImGui::PopID();
                if (dbl) camTreeOpenDetail();
            }
            if (s.ops.empty()) {
                FmRowRect err = fmRowPill(false);
                ImGui::TextDisabled("   (no operations — add from the ribbon)");
                fmRowPillEnd(err, false);
            }
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    if (setups.empty()) {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (no machining setup — Add Setup on the ribbon)");
        fmRowPillEnd(err, false);
    }

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
    if (tools.empty()) {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (no tools — Add Tool on the ribbon)");
        fmRowPillEnd(err, false);
    }

    // ---- Models (the part docs the toolpaths are computed from) ----
    fmSectionDivider("Models");
    if (!g.sourceModel.empty()) {
        if (leafRow("cam_model", "part", "M", g.sourceModel, g.selKind == 7)) g.selKind = 7;
    } else {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (none — Extract Part pulls the active part)");
        fmRowPillEnd(err, false);
    }

    // ---- status footer ----
    if (g.docId.empty() == false && !g.mirror.setups().empty()) {
        ImGui::Spacing();
        if (camBusy()) ImGui::BeginDisabled();
        if (ImGui::Button("Run: Generate > Simulate > Post")) doPipeline(st);
        if (camBusy()) ImGui::EndDisabled();
    }
    if (camBusy() || !g.status.empty()) {
        ImGui::Spacing();
        ImGui::Separator();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        if (camBusy()) {
            static const char* kSpin[] = {"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"};
            const int fi = ((int)(ImGui::GetTime() * 10.0)) % 10;
            ImGui::TextDisabled("%s %s", kSpin[fi],
                               g.status.empty() ? "working..." : g.status.c_str());
        } else {
            ImGui::TextDisabled("%s", g.status.c_str());
        }
        ImGui::PopTextWrapPos();
    }

    if (jobOpen) ImGui::TreePop();   // balance the ##cam_job TreeNodeEx opened above

    if (!pendingOps.empty()) applyOps(st, pendingOps);   // after the walk — mirror reload safe
}

}  // namespace ui
