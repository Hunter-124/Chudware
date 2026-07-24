// Reverse Engineering workspace — full-viewport shell for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline (contracts/reveng.md).
//
// RE-40 overhaul. Layout, top to bottom / left to right:
//   left   : session rail — rich session cards (thumbnail, status chip, honesty tag, stage
//            progress), a substring filter, and the New-session card.
//   center : source-lane SegmentedControl, then THE STAGE STEPPER (kit::StepTrack) pinned at
//            the TOP where a wizard's progress indicator belongs, then the active stage body.
//   right  : properties column with three tabs — Details / Capabilities / Provenance.
// Both side columns collapse to a header strip + chevron (T-G21 affordance, see below).
//
// Why the stepper moved: it used to live at the BOTTOM of the workspace, below the stage body,
// so the one control that tells you where you are in a six-step pipeline was the last thing you
// read and could not be used for navigation. It is now the second thing you read, it is
// clickable, its per-step colour comes from the mirrored session.stages badges, and steps that
// are not reachable yet are dimmed and say what unblocks them.
//
// HARD RULES (shared with every other ui/reveng TU — see reveng_widgets.h):
//   • Sizes come from ui::metrics::re*(); never a fresh pixel literal.
//   • Colours come from ui::palette / the kit; never a hardcoded ImVec4/ImU32.
//   • Every async action shows Busy / Ok / Warn / Error; never a silent no-op.
//   • Every stage that produced an artifact SHOWS it (HeroPreview / ThumbTile / Viewport3DPreview).
//
// GL-thread only. Every service round-trip is ASYNC via st.rpc.call — a SYNC worker call in a
// draw path is a known freeze bug.
#include "reveng_workspace.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "core/engine_events.h"  // core::watchRevEngReconstruct — live Reconstruct progress feed
#include "ui_theme.h"
#include "../metrics.h"     // ui::metrics::re*() — the ONLY source of sizes in this TU

#include "reveng_internal.h"
#include "reveng_widgets.h"  // RE-40 shared visual kit (cards / chips / previews / steps)
#include "img_tex.h"         // rail card thumbnails (LRU-cached, cache-owned ids)
#include "dim_review.h"      // RE-22: drawReviewStage
// RE-ANCHOR(RE-19): capture suite — webcam + gallery wired into the Source stage
#include "webcam_capture.h"
#include "image_gallery.h"
#include "sources_panel.h"   // RE-30: drawSourcesPanel
#include "stage_panels.h"    // Scale / Model / Deliver stage panels

namespace ui {
namespace reveng {

namespace M = ui::metrics;   // every size in this TU comes from here — no pixel literals

namespace {

// ---- shell-local state ---------------------------------------------------------
// Panel-local state lives in this TU, not in reveng_internal.h (which is the CROSS-panel
// contract). Everything here is derived/presentational: caches, animation, wall-clock.
struct ShellState {
    // Column roll animation (T-G21). Negative = uninitialised, snaps on the first frame.
    float railW = -1.0f;
    float propW = -1.0f;

    // Rail card thumbnails. `path` is the session's first still, resolved once from the session
    // directory; the texture itself is owned by the img_tex LRU cache (never freed here).
    struct RailThumb {
        std::string sessionId;
        std::string path;          // "" once scanned and nothing was found
        int         forImages = -1;  // session.images at scan time — rescan when it changes
    };
    std::vector<RailThumb> thumbs;

    // Reconstruct wall-clock. reconStartT < 0 => no run in flight.
    double reconStartT   = -1.0;
    double reconElapsed  = 0.0;
    // Sparse/Dense/Mesh sub-step the run was last on, so a failure lands on the right step
    // after recon.active has already gone false.
    int    lastSubStep   = 0;

    // Webcam device lifetime. drawRevEngWorkspace stops being called the instant the user
    // leaves the RE tab, so we detect the gap on re-entry and tear the camera down then.
    int  lastDrawFrame = -1000;
    bool webcamLive    = false;
};

ShellState& SH() {
    static ShellState s;
    return s;
}

// Exponential approach used for the column roll — same 120 ms feel as ui::PanelCollapseAnim.
float approachPx(float& cur, float target) {
    if (cur < 0.0f) { cur = target; return cur; }
    const float a = 1.0f - std::exp(-ImGui::GetIO().DeltaTime / 0.12f);
    cur += (target - cur) * a;
    if (std::fabs(cur - target) < 0.5f) cur = target;
    return cur;
}

// ---- stage vocabulary ----------------------------------------------------------

const char* stageName(Stage s) {
    switch (s) {
        case Stage::Source:      return "Source";
        case Stage::Scale:       return "Scale";
        case Stage::Reconstruct: return "Reconstruct";
        case Stage::Model:       return "Model";
        case Stage::Review:      return "Review";
        case Stage::Deliver:     return "Deliver";
    }
    return "?";
}

// The manifest key for a stage (contracts/reveng.md §2 session.stages). Source is "capture".
const char* stageManifestKey(Stage s) {
    switch (s) {
        case Stage::Source:      return "capture";
        case Stage::Scale:       return "scale";
        case Stage::Reconstruct: return "reconstruct";
        case Stage::Model:       return "model";
        case Stage::Review:      return "review";
        case Stage::Deliver:     return "deliver";
    }
    return "";
}

const char* stageBlurb(Stage stg) {
    switch (stg) {
        case Stage::Source:
            return "Choose a source lane: scan photos/video/webcam, ingest a datasheet, look up a "
                   "part number on the web, or seed from the parts library.";
        case Stage::Scale:
            return "Set real-world scale from an ArUco mat, two picked points, or a known "
                   "dimension. Without scale the model has shape but no size.";
        case Stage::Reconstruct:
            return "Sparse -> dense -> mesh (or a 3D Gaussian splat) reconstruction of the "
                   "captured views into geometry you can measure.";
        case Stage::Model:
            return "Fit primitives (cylinders, holes, bosses, planes) and build an editable "
                   "parametric model grounded in those fits, with a mesh-body fallback.";
        case Stage::Review:
            return "Deviation heat-map (mesh vs. B-rep) plus the dimension-confirm table, to "
                   "prove the model matches the real part within tolerance.";
        case Stage::Deliver:
            return "Attach the result to the parts hub, export a bundle, or print a scale mat.";
    }
    return "";
}

// The stage whose COMPLETION unlocks `stg`. This is a dependency graph, not the visual order:
// Reconstruct depends on Source, NOT on Scale, because the ArUco scale solver needs a sparse
// model to triangulate against (reveng_set_scale returns reason:"scale_needs_sparse"). Gating
// Reconstruct behind Scale would deadlock the Scan lane.
Stage stagePrereq(Stage stg) {
    switch (stg) {
        case Stage::Source:      return Stage::Source;       // always reachable
        case Stage::Scale:       return Stage::Source;
        case Stage::Reconstruct: return Stage::Source;
        case Stage::Model:       return Stage::Reconstruct;  // doc lanes remap this to Source
        case Stage::Review:      return Stage::Model;
        case Stage::Deliver:     return Stage::Model;
    }
    return Stage::Source;
}

// What has to happen before a stage becomes reachable — shown on the locked step.
const char* stageGateReason(Stage stg, bool scanLane) {
    switch (stg) {
        case Stage::Source:      return "pick a source lane first";
        case Stage::Scale:       return "capture or import at least one image in Source first";
        case Stage::Reconstruct: return "Source needs captured images before a reconstruction can run";
        case Stage::Model:       return scanLane
                                          ? "run Reconstruct first so there is geometry to fit"
                                          : "ingest a datasheet or research a part number in Source first";
        case Stage::Review:      return "build a model in the Model stage first";
        case Stage::Deliver:     return "build a model in the Model stage first";
    }
    return "an earlier stage is not finished";
}

// Contract badge string -> kit level. Unknown non-idle badges read as in-flight, which is the
// safe reading: the backend is doing something we do not have a name for.
kit::Level badgeLevel(const std::string& b) {
    if (b == "done" || b == "complete")            return kit::Level::Ok;
    if (b == "failed" || b == "error")             return kit::Level::Error;
    if (b.empty() || b == "idle" || b == "skipped") return kit::Level::Neutral;
    return kit::Level::Busy;
}

kit::Level statusLevel(const std::string& status) {
    if (status == "done")  return kit::Level::Ok;
    if (status == "error") return kit::Level::Error;
    if (status == "capturing" || status == "reconstructing" || status == "modeling")
        return kit::Level::Busy;
    return kit::Level::Neutral;
}

// Returns the ordered stage list for the current source lane. Doc lanes (Datasheet, Web,
// FromLibrary) skip Scale + Reconstruct — there is nothing to scale or reconstruct.
std::vector<Stage> activeStagePipeline(SourceMode mode) {
    if (mode == SourceMode::Scan) {
        return { Stage::Source, Stage::Scale, Stage::Reconstruct,
                 Stage::Model, Stage::Review, Stage::Deliver };
    }
    return { Stage::Source, Stage::Model, Stage::Review, Stage::Deliver };
}

void clampStageToPipeline(RevEngState& s) {
    const std::vector<Stage> pipeline = activeStagePipeline(s.sourceMode);
    for (const Stage& stg : pipeline)
        if (stg == s.stage) return;
    s.stage = pipeline.front();
}

// How far a session got, as a 0..1 fraction of the six canonical stages. Prefers the real
// per-stage badge mirror (only available for the selected session); falls back to the summary
// status, which is a real backend field rather than a guess.
float sessionProgress(const RevEngState& s, int idx) {
    if (idx == s.selected && s.detailSessionId == s.sessions[idx].session_id) {
        int done = 0;
        for (int i = 0; i < kStageCount; ++i)
            if (badgeLevel(s.stages[i].badge) == kit::Level::Ok) ++done;
        return (float)done / (float)kStageCount;
    }
    const std::string& st = s.sessions[idx].status;
    if (st == "done")           return 1.0f;
    if (st == "review")         return 5.0f / (float)kStageCount;
    if (st == "modeling")       return 4.0f / (float)kStageCount;
    if (st == "reconstructing") return 3.0f / (float)kStageCount;
    if (st == "capturing")      return s.sessions[idx].images > 0 ? 1.0f / (float)kStageCount : 0.0f;
    return 0.0f;
}

}  // namespace

RevEngState& S() {
    static RevEngState s;
    return s;
}

// RE-21: Apply a reveng event to the singleton (GL-thread only, called from app.cpp drain).
// CONTRACT UNCHANGED — kinds, fields and semantics are exactly as before.
void feedRevEngEvent(const RevEngEvent& ev) {
    RevEngState& s = S();
    if (ev.kind == "reconstruct_progress") {
        // A progress bar that walks backwards reads as a fault. The stage map assigns each node
        // a nominal pct, so a node whose intra-node records already reported 62-64 can be
        // followed by the next node's nominal 55. Hold the high-water mark within a run; a
        // fresh run resets it via kickReconstruct, and done/error below set their own values.
        if (!s.recon.active) s.recon.pct = 0;   // first event of this run
        s.recon.active = true;
        s.recon.done   = false;
        s.recon.err.clear();
        if (ev.pct > s.recon.pct) s.recon.pct = ev.pct;
        s.recon.phase  = ev.msg;
    } else if (ev.kind == "reconstruct_done") {
        s.recon.active = false;
        s.recon.done   = true;
        s.recon.pct    = 100;
        s.recon.err.clear();
    } else if (ev.kind == "reconstruct_error") {
        s.recon.active = false;
        s.recon.err    = ev.msg.empty() ? "Unknown reconstruction error" : ev.msg;
    }
}

// RE-27: seed the canned splat fixture (for shot modes / headless verification).
void seedSplatFixture() {
    RevEngState& s = S();
    if (s.splatLoaded) return;
    const char* repo = std::getenv("CHUDWARE_REPO");
    std::string base = repo ? std::string(repo) : std::string(".");
    std::string plyPath = base + "/host/tests/fixtures/splat_tiny.ply";
    s.splatLoaded = true;
    s.splatPathTried = plyPath;
    std::string err;
    if (!s.splat.load(plyPath, err))
        std::fprintf(stderr, "reveng seedSplatFixture: %s\n", err.c_str());
}

namespace {

// ---- session paths -------------------------------------------------------------

// Resolve a session-relative artifact path against $CHUDWARE_REVENG_DIR/<sid>/.
std::string resolveSessionPath(const std::string& session_id, const std::string& rel_or_abs) {
    if (rel_or_abs.empty()) return {};
    if (rel_or_abs[0] == '/') return rel_or_abs;
#if defined(_WIN32)
    if (rel_or_abs.size() > 2 && rel_or_abs[1] == ':') return rel_or_abs;
#endif
    const char* override_dir = std::getenv("CHUDWARE_REVENG_DIR");
    std::string root;
    if (override_dir && override_dir[0]) {
        root = override_dir;
    } else {
        const char* xdg = std::getenv("XDG_DATA_HOME");
        if (xdg && xdg[0]) root = std::string(xdg) + "/Chudware/reveng";
        else {
            const char* home = std::getenv("HOME");
            root = (home && home[0] ? std::string(home) : std::string(".")) +
                   "/.local/share/Chudware/reveng";
        }
    }
    if (!root.empty() && root.back() != '/') root += '/';
    if (!session_id.empty()) {
        root += session_id;
        root += '/';
    }
    root += rel_or_abs;
    return root;
}

bool looksLikeImage(const std::string& name) {
    const std::size_t dot = name.rfind('.');
    if (dot == std::string::npos) return false;
    std::string ext = name.substr(dot + 1);
    for (char& c : ext) c = (char)std::tolower((unsigned char)c);
    return ext == "jpg" || ext == "jpeg" || ext == "png";
}

// First still in <session>/images, else <session>/frames. Non-throwing; "" when there is none.
std::string firstSessionImage(const std::string& sid) {
    namespace fs = std::filesystem;
    const char* kDirs[] = { "images", "frames" };
    for (const char* sub : kDirs) {
        std::error_code ec;
        const fs::path dir(resolveSessionPath(sid, sub));
        if (!fs::is_directory(dir, ec)) continue;
        std::string best;
        for (fs::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
            const std::string name = it->path().filename().string();
            if (!looksLikeImage(name)) continue;
            if (best.empty() || name < best) best = name;
        }
        if (!best.empty()) return (dir / best).string();
    }
    return {};
}

// Cached rail thumbnail for one session. The returned id belongs to the img_tex LRU cache —
// never delete it; it survives session switches for free.
unsigned int railThumb(const SessionSummary& ss, int* outW, int* outH) {
    ShellState& sh = SH();
    ShellState::RailThumb* slot = nullptr;
    for (ShellState::RailThumb& t : sh.thumbs)
        if (t.sessionId == ss.session_id) { slot = &t; break; }
    if (!slot) {
        sh.thumbs.push_back(ShellState::RailThumb{ ss.session_id, {}, -1 });
        slot = &sh.thumbs.back();
    }
    if (slot->forImages != ss.images) {
        slot->forImages = ss.images;
        slot->path = ss.images > 0 ? firstSessionImage(ss.session_id) : std::string();
    }
    if (slot->path.empty()) return 0;
    return ::reveng::cachedThumbTex(slot->path, (int)M::reThumbMd(), outW, outH);
}

// ---- async loaders -------------------------------------------------------------

void loadSessions(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.listPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.listedOnce && now - s.lastListT < 4.0) return;
    s.listPending = true;
    s.lastListT = now;
    st.rpc.call("list_reveng_sessions", nlohmann::json::object(), [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.listPending = false;
        st2.listedOnce = true;
        if (!r.ok) {
            st2.listErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st2.listErr = r.result.is_object()
                              ? r.result.value("reason", std::string("Reverse Eng tools unavailable"))
                              : std::string("Reverse Eng tools unavailable");
            st2.sessions.clear();
            return;
        }
        st2.listErr.clear();
        st2.sessions.clear();
        if (r.result.contains("sessions") && r.result["sessions"].is_array()) {
            for (const auto& j : r.result["sessions"]) {
                if (!j.is_object()) continue;
                SessionSummary ss;
                ss.session_id = j.value("session_id", std::string());
                ss.name = j.value("name", std::string("(unnamed)"));
                ss.status = j.value("status", std::string());
                ss.images = j.value("images", 0);
                ss.createdAt = j.value("created_at", std::string());
                ss.updatedAt = j.value("updated_at", std::string());
                if (j.contains("part_ref") && j["part_ref"].is_string())
                    ss.partRef = j["part_ref"].get<std::string>();
                if (j.contains("honesty") && j["honesty"].is_object())
                    ss.approximate = j["honesty"].value("approximate", false);
                ss.approximate = ss.approximate || j.value("approximate", false);
                st2.sessions.push_back(std::move(ss));
            }
        }
        if (st2.selected >= (int)st2.sessions.size()) st2.selected = -1;
    });
}

// Flatten a provenance record's non-structural fields into one "k=v · k=v" line.
std::string flattenProvenance(const nlohmann::json& j) {
    std::string out;
    for (auto it = j.begin(); it != j.end(); ++it) {
        const std::string& k = it.key();
        if (k == "step" || k == "ts") continue;
        std::string v;
        if (it->is_string())            v = it->get<std::string>();
        else if (it->is_boolean())      v = it->get<bool>() ? "yes" : "no";
        else if (it->is_null())         v = "\xe2\x80\x94";
        else                            v = it->dump();
        if (v.size() > 96) v = v.substr(0, 93) + "...";
        if (!out.empty()) out += "  \xc2\xb7  ";
        out += k;
        out += "=";
        out += v;
    }
    return out;
}

// Async: pull the full session manifest — artifact paths, scale, stage badges, honesty tags and
// the provenance timeline. Everything the properties column and Reconstruct stage mirror.
void loadSessionDetail(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return;
    const std::string& sid = s.sessions[s.selected].session_id;
    if (sid.empty()) return;
    if (s.detailPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.detailSessionId == sid && now - s.lastDetailT < 3.0) return;
    s.detailPending = true;
    s.lastDetailT = now;
    nlohmann::json params = nlohmann::json::object();
    params["session_id"] = sid;
    st.rpc.call("get_reveng_session", params, [sid](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.detailPending = false;
        if (!r.ok) {
            st2.detailErr = r.err.empty() ? "get_reveng_session failed" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st2.detailErr = r.result.is_object()
                ? r.result.value("reason", std::string("get_reveng_session failed"))
                : std::string("get_reveng_session failed");
            return;
        }
        st2.detailErr.clear();
        st2.detailSessionId = sid;
        st2.artifactSplatPath.clear();
        st2.artifactMeshPath.clear();
        st2.artifactSparseDir.clear();
        for (int i = 0; i < kStageCount; ++i) st2.stages[i].badge = "idle";
        st2.provenance.clear();
        st2.honestyPipeline.clear();
        st2.honestyTier.clear();
        if (!r.result.contains("session") || !r.result["session"].is_object()) return;
        const auto& sess = r.result["session"];

        // session.scale (§2) — the Scale stage + properties column read the mirror.
        if (sess.contains("scale") && sess["scale"].is_object()) {
            const auto& sj = sess["scale"];
            ScaleState& sc = st2.scale;
            sc.mirrored    = true;
            sc.curMethod   = sj.value("method", std::string());
            sc.mmPerUnit   = sj.value("mm_per_unit", 0.0);
            sc.residualPct = sj.value("residual_pct", 0.0);
            sc.confirmed   = sj.value("confirmed", false);
        }

        // session.honesty (§2) — never render photo-derived geometry as exact.
        if (sess.contains("honesty") && sess["honesty"].is_object()) {
            const auto& hj = sess["honesty"];
            st2.honestyPipeline = hj.value("pipeline", std::string());
            if (hj.contains("single_photo_tier") && hj["single_photo_tier"].is_string())
                st2.honestyTier = hj["single_photo_tier"].get<std::string>();
        }

        if (sess.contains("artifacts") && sess["artifacts"].is_object()) {
            const auto& art = sess["artifacts"];
            auto take = [&](const char* key) -> std::string {
                if (!art.contains(key) || !art[key].is_string()) return {};
                return art[key].get<std::string>();
            };
            const std::string splat  = take("splat_path");
            const std::string mesh   = take("mesh_path");
            const std::string sparse = take("sparse_dir");
            if (!splat.empty())  st2.artifactSplatPath = resolveSessionPath(sid, splat);
            if (!mesh.empty())   st2.artifactMeshPath  = resolveSessionPath(sid, mesh);
            if (!sparse.empty()) st2.artifactSparseDir = resolveSessionPath(sid, sparse);
        }

        // session.stages (§2) — the TOP stepper's per-step colour + reachability.
        if (sess.contains("stages") && sess["stages"].is_object()) {
            const auto& sj = sess["stages"];
            for (int i = 0; i < kStageCount; ++i) {
                const char* key = stageManifestKey((Stage)i);
                if (sj.contains(key) && sj[key].is_string())
                    st2.stages[i].badge = sj[key].get<std::string>();
            }
            const std::string reconBadge = sj.value("reconstruct", std::string());
            if (reconBadge == "done" || reconBadge == "complete") {
                st2.recon.done   = true;
                st2.recon.active = false;
                st2.recon.pct    = 100;
            }
        }

        // session.provenance[] (§2) — the audit trail, newest last in the manifest.
        if (sess.contains("provenance") && sess["provenance"].is_array()) {
            for (const auto& p : sess["provenance"]) {
                if (!p.is_object()) continue;
                ProvenanceRecord rec;
                rec.step   = p.value("step", std::string("(step)"));
                rec.ts     = p.value("ts", std::string());
                rec.detail = flattenProvenance(p);
                st2.provenance.push_back(std::move(rec));
            }
        }

        const std::string status = sess.value("status", std::string());
        if (status == "done" || status == "review" || status == "modeling") {
            // Artifacts may exist even if the reconstruct badge is still "idle" in old sessions.
            if (!st2.artifactSplatPath.empty() || !st2.artifactMeshPath.empty())
                st2.recon.done = true;
        }
    });
}

// Contract order for the §8.1 provisioning keys, so the capability list never reshuffles.
const char* const kProvKeys[] = { "colmap", "openmvs", "brush", "triposr", "vggt", "docling" };

// Human fix-it text for a backend the probe did not find. reveng_status() ships
// provisioning_detail{env_var,note}; this is the fallback when it does not.
const char* provFallbackHint(const std::string& key) {
    if (key == "colmap")  return "install COLMAP and set COLMAP_BIN \xe2\x80\x94 required for sparse reconstruction";
    if (key == "openmvs") return "install OpenMVS (AGPL-3.0, never bundled) and set OPENMVS_BIN \xe2\x80\x94 dense mesh";
    if (key == "brush")   return "provision the Brush binary and set BRUSH_BIN \xe2\x80\x94 3D Gaussian splatting";
    if (key == "triposr") return "provision TripoSR weights \xe2\x80\x94 single-photo tier";
    if (key == "vggt")    return "provision VGGT weights \xe2\x80\x94 feed-forward sparse alternative to COLMAP";
    if (key == "docling") return "install docling \xe2\x80\x94 datasheet ingest";
    return "not provisioned";
}

// Async: reveng_status() capability + provisioning probe. Throttled; Refresh forces it.
// This is what lets the Reconstruct stage disable Run with a real reason instead of letting the
// user click through into a backend_not_provisioned failure.
void loadCapabilities(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    CapabilityState& c = S().caps;
    if (c.pending) return;
    const double now = ImGui::GetTime();
    const double throttle = c.loaded ? 30.0 : 6.0;
    if (!force && now - c.lastT < throttle) return;
    c.pending = true;
    c.lastT = now;
    st.rpc.call("reveng_status", nlohmann::json::object(), [](const core::RpcResult& r) {
        CapabilityState& c2 = S().caps;
        c2.pending = false;
        if (!r.ok) {
            c2.err = r.err.empty() ? "reveng_status unreachable" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            c2.err = r.result.is_object()
                         ? r.result.value("reason", std::string("reveng_status failed"))
                         : std::string("reveng_status failed");
            return;
        }
        c2.err.clear();
        c2.loaded = true;
        if (r.result.contains("capabilities") && r.result["capabilities"].is_object()) {
            const auto& cj = r.result["capabilities"];
            c2.sparse      = cj.value("sparse", false);
            c2.dense       = cj.value("dense", false);
            c2.splat       = cj.value("splat", false);
            c2.singlePhoto = cj.value("single_photo", false);
            c2.webcam      = cj.value("webcam", false);
            c2.gpu         = cj.value("gpu", false);
        }
        c2.artifacts.clear();
        const nlohmann::json* prov = nullptr;
        if (r.result.contains("provisioning") && r.result["provisioning"].is_object())
            prov = &r.result["provisioning"];
        const nlohmann::json* detail = nullptr;
        if (r.result.contains("provisioning_detail") && r.result["provisioning_detail"].is_object())
            detail = &r.result["provisioning_detail"];
        auto pushArtifact = [&](const std::string& key, bool present) {
            CapabilityState::Artifact a;
            a.key = key;
            a.present = present;
            if (detail && detail->contains(key) && (*detail)[key].is_object()) {
                const auto& d = (*detail)[key];
                std::string env, note, lic;
                if (d.contains("env_var") && d["env_var"].is_string()) env = d["env_var"].get<std::string>();
                if (d.contains("note")    && d["note"].is_string())    note = d["note"].get<std::string>();
                if (d.contains("license") && d["license"].is_string()) lic  = d["license"].get<std::string>();
                if (!note.empty()) a.hint = note;
                if (!env.empty())  a.hint += (a.hint.empty() ? "" : "  ") + std::string("set ") + env;
                if (!lic.empty())  a.hint += (a.hint.empty() ? "" : "  \xc2\xb7  ") + lic;
            }
            if (a.hint.empty()) a.hint = present ? "provisioned" : provFallbackHint(key);
            c2.artifacts.push_back(std::move(a));
        };
        if (prov) {
            for (const char* k : kProvKeys)
                if (prov->contains(k)) pushArtifact(k, (*prov)[k].get<bool>());
            // Any key the backend grew that this build does not know about — still show it.
            for (auto it = prov->begin(); it != prov->end(); ++it) {
                bool known = false;
                for (const char* k : kProvKeys) known = known || it.key() == k;
                if (!known && it->is_boolean()) pushArtifact(it.key(), it->get<bool>());
            }
        }
        c2.banners.clear();
        if (r.result.contains("banners") && r.result["banners"].is_array())
            for (const auto& b : r.result["banners"])
                if (b.is_string()) c2.banners.push_back(b.get<std::string>());
    });
}

// Prefer the session splat; the canned fixture is an explicit demo fallback only.
void ensureSplatForSession(RevEngState& s) {
    std::string want;
    bool demoFallback = false;
    if (!s.artifactSplatPath.empty()) {
        want = s.artifactSplatPath;
    } else if (s.recon.done) {
        const char* repo = std::getenv("CHUDWARE_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        want = base + "/host/tests/fixtures/splat_tiny.ply";
        demoFallback = true;
    } else {
        return;
    }
    if (want.empty()) return;
    if (s.splatLoaded && s.splatPathTried == want) return;

    s.splatLoaded = true;
    s.splatPathTried = want;
    std::string err;
    if (!s.splat.load(want, err)) {
        std::fprintf(stderr, "reveng splat load failed (%s): %s\n",
                     demoFallback ? "fixture" : "session", err.c_str());
        if (!demoFallback) {
            s.splatLoaded = false;
            s.splatPathTried.clear();
        }
    }
}

}  // namespace

// ---- reconstruction kick -------------------------------------------------------

// Kick an async reveng_reconstruct for the selected session (ribbon + Run button share this).
void kickReconstruct(AppState& st) {
    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        st.toast(ui::ToastLevel::Info, "Reverse Eng: select a session first");
        return;
    }
    if (s.recon.kickPending) return;
    static const char* kQuality[] = { "draft", "normal", "high" };
    s.recon.kickPending = true;
    s.recon.kickErr.clear();
    SH().reconStartT = ImGui::GetTime();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = s.sessions[s.selected].session_id;
    args["quality"]    = kQuality[std::clamp(s.recon.quality, 0, 2)];
    st.rpc.call("reveng_reconstruct", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        s2.recon.kickPending = false;
        if (!r.ok) {
            s2.recon.kickErr = r.err.empty() ? "reveng_reconstruct failed" : r.err;
            SH().reconStartT = -1.0;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            s2.recon.kickErr = r.result.value("reason", std::string("reveng_reconstruct failed"));
            const std::string hint = r.result.value("hint", std::string());
            if (!hint.empty()) s2.recon.kickErr += " \xe2\x80\x94 " + hint;
            SH().reconStartT = -1.0;
            return;
        }
        s2.recon.kickErr.clear();
        s2.recon.runId  = r.result.value("run_id", std::string());
        s2.recon.active = true;
        s2.recon.done   = false;
        s2.recon.pct    = 2;
        s2.recon.phase  = "queued";
        s2.recon.err.clear();
        // Arm the live progress feed on the run_id the launch just returned:
        // core::engineEventsTick tails that run's EventJournal, so the bar / sub-steps / clock
        // advance during the mission instead of waiting on the 3 s stage-badge poll. This runs
        // inside st.rpc.drain(), earlier in the frame than engineEventsTick, so the first
        // journal record replaces the "queued" seed above before anything draws.
        core::watchRevEngReconstruct(s2.recon.runId);
    });
}

// ---- ribbon command routing ----------------------------------------------------
// app.cpp dispatchCmd forwards every reveng_* id here. Each command switches to the workspace,
// lands on the right stage, and — for the three Source entry points — sets a one-shot
// SourceFocus so sources_panel.cpp opens the SPECIFIC widget the button promised. Before RE-40
// all three collapsed into one branch, so "Add Photos", "Add Video" and "Webcam" were the same
// button wearing three labels.
bool handleRevEngCommand(AppState& st, const char* cmd) {
    if (!cmd) return false;
    const std::string c(cmd);
    if (c.rfind("reveng_", 0) != 0) return false;

    RevEngState& s = S();
    auto toStage = [&](Stage stg) {
        st.activeWorkspace = AppState::Workspace::ReverseEng;
        s.stage = stg;
    };
    auto toSource = [&](SourceFocus focus) {
        toStage(Stage::Source);
        s.sourceMode  = SourceMode::Scan;
        s.sourceFocus = focus;
        s.railCollapsed = false;
    };

    if (c == "reveng_new")          { toStage(Stage::Source); s.focusNewName = true;
                                      s.railCollapsed = false; return true; }
    if (c == "reveng_open")         { st.activeWorkspace = AppState::Workspace::ReverseEng;
                                      s.railCollapsed = false;
                                      loadSessions(st, /*force=*/true); return true; }
    if (c == "reveng_from_library") { st.activeWorkspace = AppState::Workspace::PartsLibrary;
                                      return true; }
    if (c == "reveng_add_photos")   { toSource(SourceFocus::Photos); return true; }
    if (c == "reveng_add_video")    { toSource(SourceFocus::Video);  return true; }
    if (c == "reveng_webcam")       { toSource(SourceFocus::Webcam); return true; }
    if (c == "reveng_datasheet")    { toStage(Stage::Source); s.sourceMode = SourceMode::Datasheet;
                                      s.sourceFocus = SourceFocus::None; return true; }
    if (c == "reveng_scale_aruco")  { toStage(Stage::Scale); s.scale.method = 0; return true; }
    if (c == "reveng_scale_points") { toStage(Stage::Scale); s.scale.method = 1; return true; }
    if (c == "reveng_scale_known")  { toStage(Stage::Scale); s.scale.method = 2; return true; }
    if (c == "reveng_reconstruct")  { toStage(Stage::Reconstruct); kickReconstruct(st); return true; }
    if (c == "reveng_splat_view")   { toStage(Stage::Reconstruct); return true; }
    if (c == "reveng_fit")          { toStage(Stage::Model); kickFitPrimitives(st); return true; }
    if (c == "reveng_ai_model")     { toStage(Stage::Model); kickBuildParametric(st, 0); return true; }
    if (c == "reveng_mesh_body")    { toStage(Stage::Model); s.model.strategy = 1;
                                      kickBuildParametric(st, 1); return true; }
    if (c == "reveng_export")       { toStage(Stage::Deliver); return true; }
    if (c == "reveng_to_parts")     { toStage(Stage::Deliver); return true; }
    if (c == "reveng_print_mat")    { toStage(Stage::Deliver); kickPrintScaleMat(st); return true; }
    return false;
}

namespace {

// ================================================================================
// Reconstruct stage
// ================================================================================

// Result of the pre-flight capability check. Nothing here guesses: an unknown capability state
// never blocks, and a known-missing backend always names the env var that fixes it.
struct ReconGate {
    bool        canRun    = false;
    bool        hardBlock = false;   // no path to a reconstruction at all -> BlockedState
    std::string reason;              // disabledReason for the Run button / BlockedState body
    std::string hint;                // "what to do" line for the BlockedState
    std::string warn;                // non-blocking degradation notice
};

ReconGate reconPreflight(const AppState& st, const RevEngState& s) {
    ReconGate g;
    if (!st.workerStarted) {
        g.hardBlock = true;
        g.reason = "The Chudware worker is not running, so no reconstruction backend can be reached.";
        g.hint   = "Start the worker (Help > Diagnostics) and reopen this tab.";
        return g;
    }
    if (s.caps.loaded && !s.caps.sparse) {
        g.hardBlock = true;
        g.reason = "No sparse-reconstruction backend is provisioned. Structure-from-motion needs "
                   "either COLMAP or the VGGT feed-forward model, and neither was found.";
        g.hint   = "Install COLMAP and set COLMAP_BIN (or provision VGGT weights), then press "
                   "Refresh on the Capabilities tab.";
        return g;
    }
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        g.reason = "Select a session in the rail first.";
        return g;
    }
    if (s.sessions[s.selected].images <= 0) {
        g.reason = "This session has no images yet. Capture or import photos in the Source stage.";
        return g;
    }
    if (s.recon.kickPending) { g.reason = "Starting the run\xe2\x80\xa6"; return g; }
    if (s.recon.active)      { g.reason = "A reconstruction is already running for this session."; return g; }

    g.canRun = true;
    if (s.caps.loaded && !s.caps.dense) {
        g.warn = "OpenMVS is not configured, so this run produces a SPARSE point cloud only \xe2\x80\x94 "
                 "no dense mesh. Install OpenMVS (AGPL-3.0, never bundled) and set OPENMVS_BIN for "
                 "a watertight mesh.";
    }
    if (!s.caps.loaded) {
        g.warn = "Backend availability has not been probed yet \xe2\x80\x94 open the Capabilities tab "
                 "to check before a long run.";
    } else if (s.scale.mirrored && !s.scale.confirmed) {
        g.warn = "Scale is not confirmed yet. That is fine here: ArUco scaling needs a sparse model, "
                 "so reconstructing first is the normal order \xe2\x80\x94 just confirm scale before "
                 "you trust any dimension.";
    }
    return g;
}

// Sparse -> Dense -> Mesh sub-steps, driven by the RevEngEvent drain (phase name first, pct as
// the fallback when the backend only reports a percentage).
void drawReconSubSteps(const RevEngState& s) {
    static const char* kSubLabels[] = { "Sparse", "Dense", "Mesh" };
    const int n = 3;
    ShellState& sh = SH();

    int active = -1;
    if (s.recon.active) {
        const std::string& p = s.recon.phase;
        if (p.find("mesh") != std::string::npos)        active = 2;
        else if (p.find("dense") != std::string::npos)  active = 1;
        else if (p.find("sparse") != std::string::npos) active = 0;
        // Stages AFTER reconstruction (the mission continues into fit/parametric/review/
        // deliver) are all "past" this three-step track. Without this they fell through to the
        // pct ladder below, and `fit` at pct 60-65 landed in the Dense bucket — a visible
        // Mesh -> Dense -> Mesh rewind.
        else if (p == "fit" || p == "parametric" || p == "review" || p == "deliver")
                                                        active = 2;
        else if (s.recon.pct >= 67)                     active = 2;
        else if (s.recon.pct >= 34)                     active = 1;
        else                                            active = 0;
        // The track never walks backwards inside one run: a stage map that reports a lower pct
        // for a later node must not un-complete a step the user already watched finish.
        if (sh.lastSubStep > active) active = sh.lastSubStep;
        sh.lastSubStep = active;
    }

    kit::Level states[3];
    for (int i = 0; i < n; ++i) {
        // A failure belongs on the step that was RUNNING when it happened, not on step 0.
        if (!s.recon.err.empty() && i == sh.lastSubStep) states[i] = kit::Level::Error;
        else if (!s.recon.err.empty() && i < sh.lastSubStep) states[i] = kit::Level::Ok;
        else if (!s.recon.err.empty())                  states[i] = kit::Level::Neutral;
        else if (s.recon.done)                          states[i] = kit::Level::Ok;
        else if (active > i)                            states[i] = kit::Level::Ok;
        else if (active == i)                           states[i] = kit::Level::Busy;
        else                                            states[i] = kit::Level::Neutral;
    }
    kit::StepTrack("##recon_substeps", kSubLabels, states, n, active,
                   /*clickable=*/false, M::reStepperH());
}

// The artifact this session currently has, and the slot that renders it.
kit::PreviewSlot reconSlot(const RevEngState& s) {
    if (s.splatLoaded && s.splat.hasSplats()) return kit::PreviewSlot::Splat;
    if (!s.artifactMeshPath.empty())          return kit::PreviewSlot::Mesh;
    return kit::PreviewSlot::SparseCloud;
}

// Inline 3D preview of whatever the reconstruction produced. The GL pass that fills this region
// runs post-ImGui from app.cpp; it reads the rect back via kit::previewSlotRect(slot).
void drawReconPreview(RevEngState& s) {
    const kit::PreviewSlot slot = reconSlot(s);
    // The canned sparse fixture deliberately does NOT count as an artifact: it is demo scaffolding,
    // and treating it as content is exactly how "it worked, trust me" sneaks back in.
    const bool haveArtifact = (slot == kit::PreviewSlot::Splat) ||
                              (slot == kit::PreviewSlot::Mesh) ||
                              !s.artifactSparseDir.empty();

    const char* emptyLabel = haveArtifact ? nullptr
                                          : "No reconstruction yet \xe2\x80\x94 run one to see the "
                                            "point cloud and camera poses here";
    const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
    kit::Viewport3DPreview("##recon_preview", box, slot, emptyLabel);

    // Always say WHAT is on screen and whether it is this session's data.
    if (slot == kit::PreviewSlot::Splat) {
        const bool isFixture = s.splatPathTried.find("splat_tiny.ply") != std::string::npos;
        char line[64];
        std::snprintf(line, sizeof(line), "%zu gaussians", s.splat.count());
        kit::ValueChip("3DGS splat", line, isFixture ? kit::Level::Warn : kit::Level::Ok,
                       isFixture ? "Demo fixture, not this session's data."
                                 : s.splatPathTried.c_str());
        if (isFixture) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            kit::HonestyTag("approximate", "Demo fixture \xe2\x80\x94 preview only, not measurable.");
        }
    } else if (slot == kit::PreviewSlot::Mesh) {
        kit::ValueChip("mesh", "ready", kit::Level::Ok, s.artifactMeshPath.c_str());
    } else if (!s.artifactSparseDir.empty()) {
        kit::ValueChip("sparse model", "ready", kit::Level::Ok, s.artifactSparseDir.c_str());
    } else if (s.recon.done) {
        kit::InlineStatus("Reconstruction reported done but returned no artifact path \xe2\x80\x94 "
                          "nothing to preview.", kit::Level::Warn);
    }
}

void drawReconProgress(const RevEngState& s) {
    ShellState& sh = SH();
    const double now = ImGui::GetTime();
    // kickReconstruct() starts the clock at the click, so queue latency is counted too. Only
    // stop it once the run is genuinely over (not while the kick RPC is still in flight).
    if (s.recon.active && sh.reconStartT < 0.0) sh.reconStartT = now;
    if (!s.recon.active && !s.recon.kickPending && sh.reconStartT >= 0.0) {
        sh.reconElapsed = now - sh.reconStartT;
        sh.reconStartT = -1.0;
    }
    const double elapsed = sh.reconStartT >= 0.0 ? (now - sh.reconStartT) : sh.reconElapsed;

    if (!s.recon.active && !s.recon.done && s.recon.err.empty()) return;

    char cap[96];
    if (elapsed > 0.0)
        std::snprintf(cap, sizeof(cap), "%d%%  \xc2\xb7  %s", s.recon.pct,
                      kit::fmtDuration(elapsed).c_str());
    else
        std::snprintf(cap, sizeof(cap), "%d%%", s.recon.pct);

    const kit::Level lvl = !s.recon.err.empty() ? kit::Level::Error
                         : s.recon.done         ? kit::Level::Ok
                                                : kit::Level::Busy;
    kit::ProgressBar((float)s.recon.pct / 100.0f, cap, lvl);
    if (!s.recon.phase.empty() && s.recon.active)
        kit::InlineStatus(s.recon.phase.c_str(), kit::Level::Busy);
    kit::InlineStatus(s.recon.err.c_str(), kit::Level::Error);
}

void drawReconRunControls(AppState& st, RevEngState& s, const ReconGate& gate) {
    static const char* kQualityLabels[] = { "Draft", "Normal", "High" };

    kit::FieldLabel("Quality");
    kit::SegmentedControl("##recon_quality", kQualityLabels, 3, &s.recon.quality, M::reFieldW());
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HelpMarker("Draft downsamples the images and skips refinement \xe2\x80\x94 minutes, good enough to "
                    "check coverage. Normal is the default. High keeps full resolution and runs "
                    "extra bundle-adjustment passes: several times slower, and the only setting "
                    "worth using for a part you intend to measure.");
    kit::GapControl();

    const char* runLabel = s.recon.kickPending ? "Starting\xe2\x80\xa6"
                         : s.recon.active      ? "Running\xe2\x80\xa6"
                                               : "Run reconstruction";
    if (kit::PrimaryButton(runLabel, gate.canRun, gate.reason.c_str(),
                           ImVec2(ImGui::GetContentRegionAvail().x, M::reBtnH()))) {
        kickReconstruct(st);
    }
    if (!gate.canRun && !gate.reason.empty())
        kit::InlineStatus(gate.reason.c_str(), kit::Level::Warn);
    kit::InlineStatus(s.recon.kickErr.c_str(), kit::Level::Error);

    if (!s.recon.runId.empty()) {
        kit::GapControl();
        kit::KeyValueRow("Run id", s.recon.runId.c_str());
    }
}

// RE-21/RE-40: Reconstruct stage — pre-flight gate, sub-steps, inline 3D preview, run controls.
// Decomposed from the old 183-line monolith into the focused helpers above.
void drawReconstructStage(AppState& st) {
    RevEngState& s = S();

    // Lazy-load the canned sparse fixture into the RePoints overlay on first entry (until a real
    // sparse cloud lands via session artifacts).
    if (!s.cannedFixtureLoaded) {
        s.cannedFixtureLoaded = true;
        std::vector<render::SparsePoint>   pts;
        std::vector<render::CameraFrustum> frusta;
        render::buildCannedSparseFixture(pts, frusta);
        s.rePoints.setCloud(pts);
        s.rePoints.setFrusta(frusta);
    }
    // GL-safe: SplatRenderer::load() is CPU-only (GL resources are created on first draw()).
    ensureSplatForSession(s);

    const ReconGate gate = reconPreflight(st, s);

    if (gate.hardBlock) {
        if (kit::BlockedState("Reconstruction unavailable", gate.reason.c_str(), gate.hint.c_str(),
                              "Re-check backends")) {
            loadCapabilities(st, /*force=*/true);
            S().propsTab = 1;
            S().propsCollapsed = false;
        }
        return;
    }

    if (kit::CardBegin("##recon_run", "Reconstruct", "mesh")) {
        const kit::Level runLvl = !s.recon.err.empty() || !s.recon.kickErr.empty()
                                      ? kit::Level::Error
                                  : s.recon.active ? kit::Level::Busy
                                  : s.recon.done   ? kit::Level::Ok
                                                   : kit::Level::Neutral;
        const char* runTxt = !s.recon.err.empty() || !s.recon.kickErr.empty() ? "error"
                           : s.recon.active ? "running"
                           : s.recon.done   ? "complete"
                                            : "idle";
        kit::CardHeaderRight(M::reBtnW());
        kit::StatusChip(runTxt, runLvl);

        drawReconSubSteps(s);
        drawReconProgress(s);
        if (!gate.warn.empty()) {
            kit::GapControl();
            kit::InlineStatus(gate.warn.c_str(), kit::Level::Warn);
        }
        kit::GapSection();
        drawReconRunControls(st, s, gate);
    }
    kit::CardEnd();

    kit::GapCards();

    if (kit::CardBegin("##recon_preview_card", "Preview", "display_shaded")) {
        drawReconPreview(s);
    }
    kit::CardEnd();
}

// ================================================================================
// Session rail
// ================================================================================

void drawSessionCard(AppState& st, RevEngState& s, int i) {
    const SessionSummary& ss = s.sessions[i];
    const bool selected = (s.selected == i);

    ImGui::PushID(i);
    char cardId[64];
    std::snprintf(cardId, sizeof(cardId), "##re_sess_%d", i);
    if (kit::CardBegin(cardId)) {
        int tw = 0, th = 0;
        const unsigned int tex = railThumb(ss, &tw, &th);

        bool pick = false;
        // No quality badge here: the documented ThumbTile badges describe an IMAGE
        // (ok/blurred/duplicate/synthetic), not a session. Session honesty is the tag below.
        if (kit::ThumbTile("##thumb", tex, M::reThumbSm(), selected, nullptr, nullptr)) {
            pick = true;
        }
        if (tex && ImGui::IsItemHovered()) {
            std::vector<std::string> meta;
            meta.push_back(ss.name);
            char imgLine[64];
            std::snprintf(imgLine, sizeof(imgLine), "%d image%s", ss.images,
                          ss.images == 1 ? "" : "s");
            meta.push_back(imgLine);
            if (!ss.updatedAt.empty()) meta.push_back("updated " + ss.updatedAt);
            kit::ThumbHoverPreview(tex, tw, th, meta);
        }

        ImGui::SameLine(0.0f, M::reCardPadX());
        ImGui::BeginGroup();
        {
            if (ImGui::Selectable(ss.name.c_str(), selected)) pick = true;
            if (ImGui::IsItemHovered() && !ss.session_id.empty())
                ImGui::SetTooltip("%s", ss.session_id.c_str());

            kit::StatusChip(ss.status.empty() ? "new" : ss.status.c_str(), statusLevel(ss.status));
            ImGui::SameLine(0.0f, M::reCtlGap());
            char imgTxt[24];
            std::snprintf(imgTxt, sizeof(imgTxt), "%d", ss.images);
            kit::ValueChip("img", imgTxt, ss.images > 0 ? kit::Level::Ok : kit::Level::Neutral,
                           "Images captured or imported into this session.");
            if (ss.approximate) {
                ImGui::SameLine(0.0f, M::reCtlGap());
                kit::HonestyTag("approximate",
                                "This session's pipeline is flagged approximate \xe2\x80\x94 reference/preview "
                                "geometry only, not a measured model.");
            }

            const float frac = sessionProgress(s, i);
            char stageTxt[32];
            std::snprintf(stageTxt, sizeof(stageTxt), "%d/%d stages",
                          (int)(frac * kStageCount + 0.5f), kStageCount);
            kit::ProgressBar(frac, stageTxt,
                             frac >= 1.0f ? kit::Level::Ok : kit::Level::Neutral);
        }
        ImGui::EndGroup();

        if (pick && s.selected != i) {
            s.selected = i;
            s.stage = Stage::Source;
        }
    }
    kit::CardEnd();
    ImGui::PopID();
    (void)st;
}

void drawNewSessionCard(AppState& st, RevEngState& s) {
    if (kit::CardBegin("##re_new_session", "New session", "folder_new")) {
        const bool canCreate = st.workerStarted && !s.newSessionPending;
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
        if (s.focusNewName) {
            ImGui::SetKeyboardFocusHere();
            s.focusNewName = false;
        }
        ImGui::InputTextWithHint("##reveng_new_name", "Part name (optional)",
                                 s.newNameBuf, sizeof(s.newNameBuf));
        kit::GapControl();
        if (kit::PrimaryButton(s.newSessionPending ? "Creating\xe2\x80\xa6" : "Create session",
                               canCreate,
                               st.workerStarted ? "A session is already being created."
                                                : "The Chudware worker is not running.",
                               ImVec2(ImGui::GetContentRegionAvail().x, M::reBtnH()))) {
            s.newSessionPending = true;
            s.newSessionErr.clear();
            nlohmann::json params = nlohmann::json::object();
            if (s.newNameBuf[0] != '\0') params["name"] = std::string(s.newNameBuf);
            st.rpc.call("reveng_start_session", params, [&st](const core::RpcResult& r) {
                RevEngState& s2 = S();
                s2.newSessionPending = false;
                if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                    s2.newSessionErr = r.ok && r.result.is_object()
                                           ? r.result.value("reason", std::string("create failed"))
                                           : (r.err.empty() ? std::string("worker unreachable") : r.err);
                    return;
                }
                s2.newSessionErr.clear();
                s2.newNameBuf[0] = '\0';
                s2.stage = Stage::Source;   // land on Source so the entry widgets show
                s2.lastListT = -100.0;      // expire the throttle -> refresh now
                st.pendingRevengSessionId = r.result.value("session_id", std::string());
            });
        }
        if (s.newSessionPending)
            kit::InlineStatus("Creating session\xe2\x80\xa6", kit::Level::Busy);
        kit::InlineStatus(s.newSessionErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();
}

bool sessionMatchesFilter(const SessionSummary& ss, const char* filter) {
    if (!filter || filter[0] == '\0') return true;
    std::string needle(filter);
    std::string hay = ss.name + " " + ss.session_id + " " + ss.status;
    for (char& c : needle) c = (char)std::tolower((unsigned char)c);
    for (char& c : hay)    c = (char)std::tolower((unsigned char)c);
    return hay.find(needle) != std::string::npos;
}

void drawSessionRailBody(AppState& st) {
    RevEngState& s = S();

    // The column header already names this column; the body opens with its controls.
    const float refreshW = M::reBtnW();
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - refreshW -
                            ImGui::GetStyle().ItemSpacing.x);
    ImGui::InputTextWithHint("##re_rail_filter", "Filter by name or id",
                             s.railFilterBuf, sizeof(s.railFilterBuf));
    ImGui::SameLine(0.0f, ImGui::GetStyle().ItemSpacing.x);
    if (kit::SecondaryButton(s.listPending ? "\xe2\x80\xa6" : "Refresh", !s.listPending,
                             "A refresh is already in flight.", ImVec2(refreshW, 0.0f)))
        loadSessions(st, /*force=*/true);

    kit::GapControl();

    drawNewSessionCard(st, s);
    kit::GapCards();

    if (!st.workerStarted) {
        kit::BlockedState("Worker not running",
                          "The session list comes from the Chudware worker, which is not up.",
                          "Start the worker, then press Refresh.");
        return;
    }
    if (!s.listErr.empty()) {
        if (kit::BlockedState("Sessions unavailable", s.listErr.c_str(),
                              "The Reverse Engineering tools may not be registered in this build.",
                              "Retry"))
            loadSessions(st, /*force=*/true);
        return;
    }
    if (s.sessions.empty()) {
        if (s.listPending && !s.listedOnce) {
            kit::InlineStatus("Loading sessions\xe2\x80\xa6", kit::Level::Busy);
            return;
        }
        if (kit::EmptyState("part", "No sessions yet",
                            "A session holds the photos, the scale, the reconstruction and the "
                            "model for ONE physical part. Create one to start.",
                            "New session")) {
            s.focusNewName = true;
        }
        return;
    }

    int shown = 0;
    ImGui::BeginChild("##re_sess_list", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    for (int i = 0; i < (int)s.sessions.size(); ++i) {
        if (!sessionMatchesFilter(s.sessions[i], s.railFilterBuf)) continue;
        ++shown;
        drawSessionCard(st, s, i);
        kit::GapCards();
    }
    if (shown == 0) {
        kit::EmptyState("filter_funnel", "No match",
                        "No session name, id or status contains that text.", nullptr, false);
    }
    ImGui::EndChild();
}

// ================================================================================
// Properties column
// ================================================================================

void drawPropsDetails(AppState& st, RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        kit::EmptyState("part", "No session selected",
                        "Pick a session in the rail to see its manifest, scale and artifacts.",
                        nullptr, false);
        return;
    }
    const SessionSummary& ss = s.sessions[s.selected];

    // Visual anchor: whichever contact-sheet image is selected (falls back to the first).
    if (kit::CardBegin("##props_hero", "Selected image", "sketch")) {
        const int idx = (s.selectedImage >= 0 && s.selectedImage < (int)s.galleryImages.size())
                            ? s.selectedImage
                            : (s.galleryImages.empty() ? -1 : 0);
        const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
        if (idx >= 0) {
            const GalleryImage& g = s.galleryImages[idx];
            char cap[128];
            std::snprintf(cap, sizeof(cap), "%d / %d  \xc2\xb7  %s", idx + 1,
                          (int)s.galleryImages.size(),
                          g.quality.empty() ? "ok" : g.quality.c_str());
            kit::HeroPreview("##props_hero_img", g.texId, g.thumbW, g.thumbH, box, cap, nullptr);
        } else {
            kit::HeroPreview("##props_hero_img", 0, 0, 0, box, nullptr,
                             "No images in this session yet");
        }
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_session", "Session", "part")) {
        kit::KeyValueRow("Name", ss.name.c_str());
        kit::KeyValueRow("Session id", ss.session_id.c_str());
        kit::KeyValueRow("Status", ss.status.empty() ? "(unknown)" : ss.status.c_str(),
                         statusLevel(ss.status));
        char imgTxt[24];
        std::snprintf(imgTxt, sizeof(imgTxt), "%d", ss.images);
        kit::KeyValueRow("Images", imgTxt,
                         ss.images > 0 ? kit::Level::Ok : kit::Level::Warn);
        if (!ss.createdAt.empty()) kit::KeyValueRow("Created", ss.createdAt.c_str());
        if (!ss.updatedAt.empty()) kit::KeyValueRow("Updated", ss.updatedAt.c_str());
        if (!ss.partRef.empty())   kit::KeyValueRow("Part ref", ss.partRef.c_str());

        kit::GapControl();
        kit::KeyValueRow("Pipeline", s.honestyPipeline.empty() ? "scan -> mesh -> AI -> parametric"
                                                               : s.honestyPipeline.c_str());
        // Tier is DERIVED, never assumed. A multi-view scan only earns "high" once its scale is
        // confirmed; without a scale the geometry has shape but no size, which is "approximate".
        if (ss.approximate) {
            kit::HonestyTag("approximate",
                            "Reference/preview geometry only \xe2\x80\x94 do not machine or mate "
                            "from these dimensions without measuring the real part.");
        } else if (!s.honestyTier.empty()) {
            kit::HonestyTag(s.honestyTier.c_str(),
                            "Single-photo reconstruction tier reported by the backend.");
        } else if (!s.scale.mirrored) {
            kit::HonestyTag("approximate",
                            "No scale has been solved yet, so nothing here is a real-world "
                            "measurement.");
        } else if (!s.scale.confirmed) {
            kit::HonestyTag("low",
                            "A scale was solved but not confirmed. Confirm it in the Scale stage "
                            "before trusting any dimension.");
        } else if (s.scale.residualPct > 1.0) {
            kit::HonestyTag("medium",
                            "Scale is confirmed but its residual is above 1%, so expect roughly "
                            "\xc2\xb1""5%.");
        } else {
            kit::HonestyTag("high",
                            "Multi-view scan with a confirmed, low-residual scale \xe2\x80\x94 "
                            "about \xc2\xb1""2%.");
        }
        kit::InlineStatus(s.detailErr.c_str(), kit::Level::Error);
        if (s.detailPending) kit::InlineStatus("Refreshing manifest\xe2\x80\xa6", kit::Level::Busy);
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_scale", "Scale", "measure")) {
        if (!s.scale.mirrored) {
            kit::InlineStatus("No scale set yet \xe2\x80\x94 the model will have shape but no size.",
                              kit::Level::Warn);
        } else {
            kit::KeyValueRow("Method", s.scale.curMethod.empty() ? "(pending)" : s.scale.curMethod.c_str());
            char mmTxt[48];
            std::snprintf(mmTxt, sizeof(mmTxt), "%.4g mm / unit", s.scale.mmPerUnit);
            kit::KeyValueRow("Scale factor", mmTxt);
            kit::KeyValueRow("Residual", kit::fmtPct(s.scale.residualPct / 100.0, 2).c_str(),
                             s.scale.residualPct <= 1.0 ? kit::Level::Ok : kit::Level::Warn);
            kit::KeyValueRow("Confirmed", s.scale.confirmed ? "yes" : "no",
                             s.scale.confirmed ? kit::Level::Ok : kit::Level::Warn);
        }
        if (kit::SecondaryButton("Open Scale stage", true, nullptr,
                                 ImVec2(ImGui::GetContentRegionAvail().x, 0.0f))) {
            s.stage = Stage::Scale;
            clampStageToPipeline(s);
        }
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_artifacts", "Artifacts", "results")) {
        auto artifactRow = [](const char* label, const std::string& path) {
            kit::StatusChip(label, path.empty() ? kit::Level::Neutral : kit::Level::Ok,
                            path.empty() ? "Not produced yet." : path.c_str());
        };
        artifactRow("sparse", s.artifactSparseDir);
        ImGui::SameLine(0.0f, M::reCtlGap());
        artifactRow("mesh", s.artifactMeshPath);
        ImGui::SameLine(0.0f, M::reCtlGap());
        artifactRow("splat", s.artifactSplatPath);
        if (s.artifactSparseDir.empty() && s.artifactMeshPath.empty() && s.artifactSplatPath.empty())
            kit::InlineStatus("Nothing reconstructed yet.", kit::Level::Neutral);
    }
    kit::CardEnd();
    (void)st;
}

void drawPropsCapabilities(AppState& st, RevEngState& s) {
    CapabilityState& c = s.caps;

    if (kit::CardBegin("##caps_head", "What this machine can do", "health_check")) {
        const float btnW = M::reBtnW();
        kit::CardHeaderRight(btnW);
        if (kit::SecondaryButton(c.pending ? "\xe2\x80\xa6" : "Refresh", !c.pending,
                                 "A probe is already in flight.", ImVec2(btnW, 0.0f)))
            loadCapabilities(st, /*force=*/true);

        if (!st.workerStarted) {
            kit::BlockedState("Worker not running",
                              "reveng_status() cannot be reached, so backend availability is unknown.",
                              "Start the Chudware worker and press Refresh.");
            kit::CardEnd();
            return;
        }
        if (!c.err.empty()) {
            if (kit::BlockedState("Capability probe failed", c.err.c_str(),
                                  "The reveng tools may not be registered in this worker build.",
                                  "Retry"))
                loadCapabilities(st, /*force=*/true);
            kit::CardEnd();
            return;
        }
        if (!c.loaded) {
            kit::InlineStatus("Probing backends\xe2\x80\xa6", kit::Level::Busy);
            kit::CardEnd();
            return;
        }

        kit::CapabilityRow("Sparse reconstruction", c.sparse,
                           c.sparse ? "COLMAP or VGGT is available."
                                    : "Install COLMAP (COLMAP_BIN) or provision VGGT weights.");
        kit::CapabilityRow("Dense mesh", c.dense,
                           c.dense ? "OpenMVS is available."
                                   : "Needs COLMAP plus OpenMVS (OPENMVS_BIN). Without it a run "
                                     "yields a sparse cloud only.");
        kit::CapabilityRow("3D Gaussian splat", c.splat,
                           c.splat ? "Brush is available."
                                   : "Provision the Brush binary (BRUSH_BIN) for splat previews.");
        kit::CapabilityRow("Single photo", c.singlePhoto,
                           c.singlePhoto ? "TripoSR / TRELLIS weights present."
                                         : "Provision TripoSR or TRELLIS weights. Single-photo output "
                                           "is approximate by definition.");
        kit::CapabilityRow("Webcam capture", c.webcam,
                           c.webcam ? "A camera device was found."
                                    : "No camera device found \xe2\x80\x94 plug one in, or import photos "
                                      "from disk instead.");
        kit::CapabilityRow("GPU", c.gpu,
                           c.gpu ? "CUDA-capable GPU detected."
                                 : "CPU only: reconstruction still works but is far slower, and "
                                   "splat training is impractical.");
    }
    kit::CardEnd();
    if (!c.loaded) return;
    kit::GapCards();

    if (kit::CardBegin("##caps_artifacts", "Backends found", "settings")) {
        if (c.artifacts.empty()) {
            kit::InlineStatus("reveng_status() returned no provisioning block.", kit::Level::Warn);
        } else {
            for (const CapabilityState::Artifact& a : c.artifacts)
                kit::CapabilityRow(a.key.c_str(), a.present, a.hint.c_str());
        }
    }
    kit::CardEnd();

    if (!c.banners.empty()) {
        kit::GapCards();
        if (kit::CardBegin("##caps_banners", "Notices", "warning_badge")) {
            for (const std::string& b : c.banners)
                kit::InlineStatus(b.c_str(), kit::Level::Warn);
        }
        kit::CardEnd();
    }
}

void drawPropsProvenance(RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        kit::EmptyState("history", "No session selected",
                        "The provenance log records every pipeline step that touched a session.",
                        nullptr, false);
        return;
    }
    if (s.provenance.empty()) {
        kit::EmptyState("history", "Nothing recorded yet",
                        "Each capture, scale solve, reconstruction and model build appends a "
                        "record here. It is what makes a photo-derived dimension defensible.",
                        nullptr, false);
        return;
    }
    if (kit::CardBegin("##props_prov", "Provenance", "history")) {
        // Newest first: the last thing that happened is what you are asking about.
        for (int i = (int)s.provenance.size() - 1; i >= 0; --i) {
            const ProvenanceRecord& p = s.provenance[i];
            ImGui::PushID(i);
            kit::KeyValueRow(p.step.c_str(), p.ts.empty() ? "" : p.ts.c_str());
            if (!p.detail.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
                ImGui::TextWrapped("%s", p.detail.c_str());
                ImGui::PopStyleColor();
            }
            if (i > 0) kit::Rule();
            ImGui::PopID();
        }
    }
    kit::CardEnd();
}

void drawPropertiesBody(AppState& st) {
    RevEngState& s = S();
    static const char* kTabs[] = { "Details", "Capabilities", "Provenance" };
    kit::SegmentedControl("##re_props_tabs", kTabs, 3, &s.propsTab,
                          ImGui::GetContentRegionAvail().x);
    kit::GapControl();

    ImGui::BeginChild("##re_props_body", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    switch (s.propsTab) {
        case 1:  drawPropsCapabilities(st, s); break;
        case 2:  drawPropsProvenance(s);       break;
        default: drawPropsDetails(st, s);      break;
    }
    ImGui::EndChild();
}

// ================================================================================
// Columns (T-G21 collapse)
// ================================================================================

// A collapsed side column rolls back to a thin full-height header strip carrying the chevron and
// a vertical caption, and the freed width goes to the stage body. This is the T-G21 affordance
// (roll to the header strip, NEVER shrink to a floating pill) rotated to the horizontal axis the
// three RE columns are laid out on — the pinned columns in layout.cpp roll UP because they stack
// vertically; these roll SIDEWAYS because they stack horizontally.
void drawCollapsedStrip(const char* id, const char* caption, bool* collapsed, float w) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadY(), M::reCardPadY()));
    ui::pushGlassChildStyle();
    ImGui::BeginChild(id, ImVec2(w, 0.0f), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::popGlassChildStyle();
    ImGui::PopStyleVar();

    const float innerW = ImGui::GetContentRegionAvail().x;
    if (ImGui::Button("\xc2\xbb", ImVec2(innerW, M::reBtnH()))) *collapsed = false;
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Expand %s", caption);

    ImGui::Dummy(ImVec2(0.0f, M::reCtlGap()));
    for (const char* p = caption; *p; ++p) {
        const char ch[2] = { *p, '\0' };
        const float tw = ImGui::CalcTextSize(ch).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (innerW - tw) * 0.5f);
        ImGui::TextDisabled("%s", ch);
    }
    ImGui::EndChild();
}

// Header row inside an expanded column: title on the left, collapse chevron on the right.
void drawColumnHeader(const char* title, bool* collapsed) {
    const float chevW  = ImGui::GetFrameHeight();
    const float startX = ImGui::GetCursorPosX();
    const float availW = ImGui::GetContentRegionAvail().x;
    ImGui::TextUnformatted(title);
    ImGui::SameLine(startX + availW - chevW);
    if (ImGui::Button("\xc2\xab", ImVec2(chevW, chevW))) *collapsed = true;
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Collapse %s", title);
    kit::Rule();
}

// ================================================================================
// Lane selector + TOP stage stepper
// ================================================================================

// RE-30: source lane. Doc lanes drop Scale + Reconstruct from the stepper entirely, which is why
// this sits ABOVE the stepper: changing the lane changes which steps exist.
bool drawSourceLane(RevEngState& s) {
    static const char* kLaneLabels[] = { "Scan", "Datasheet", "Web / Part#", "From Library" };
    static const char* kLaneTips[] = {
        "Photos / video / webcam -> Scale -> Reconstruct -> Model",
        "Local PDF or spec sheet -> Model  (no Scale / Reconstruct)",
        "Part number or URL -> Research -> Model",
        "Seeded from the Parts Library \xe2\x80\x94 auto-routed to the right lane",
    };
    int lane = (int)s.sourceMode;

    kit::FieldLabel("Source lane");
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HelpMarker(kLaneTips[std::clamp(lane, 0, 3)]);
    const bool changed = kit::SegmentedControl("##re_lane", kLaneLabels, 4, &lane,
                                               ImGui::GetContentRegionAvail().x);
    if (changed && lane != (int)s.sourceMode) {
        s.sourceMode = (SourceMode)lane;
        return true;
    }
    return false;
}

// THE stage stepper — top of the centre column, clickable, coloured from the real session.stages
// badge mirror, with unreachable steps dimmed and explained.
void drawStageStepper(AppState& st, RevEngState& s) {
    const std::vector<Stage> pipeline = activeStagePipeline(s.sourceMode);
    const int n = (int)pipeline.size();

    std::vector<const char*> labels(n);
    std::vector<kit::Level>  states(n);
    std::vector<bool>        reach(n, true);

    const bool haveSession = s.selected >= 0 && s.selected < (int)s.sessions.size();
    // Only gate navigation when we actually know the backend's stage badges for THIS session.
    // Unknown state must never lock the user out of their own wizard.
    const bool mirrored = haveSession && s.detailSessionId == s.sessions[s.selected].session_id;

    int active = 0;
    for (int i = 0; i < n; ++i) {
        labels[i] = stageName(pipeline[i]);
        const kit::Level lvl = badgeLevel(s.stages[(int)pipeline[i]].badge);
        states[i] = lvl;
        if (pipeline[i] == s.stage) active = i;
    }
    // Source counts as done the moment the session actually has images, even if the manifest
    // badge has not caught up.
    if (haveSession && s.sessions[s.selected].images > 0 && states[0] == kit::Level::Neutral)
        states[0] = kit::Level::Ok;

    // Reachability from the real dependency graph, not from list order. A stage is reachable
    // when its PREREQUISITE stage is done, when it already carries backend state of its own, or
    // when it is the stage you are standing on (never trap the user where they are).
    const bool scanLane = (s.sourceMode == SourceMode::Scan);
    if (mirrored) {
        for (int i = 0; i < n; ++i) {
            if (pipeline[i] == Stage::Source) continue;      // always reachable
            Stage prereq = stagePrereq(pipeline[i]);
            int prereqIdx = -1;
            for (int j = 0; j < n; ++j)
                if (pipeline[j] == prereq) { prereqIdx = j; break; }
            // Doc lanes have no Reconstruct step: fall back to the step before this one.
            if (prereqIdx < 0) prereqIdx = i - 1;
            const bool prereqDone = prereqIdx < 0 || states[prereqIdx] == kit::Level::Ok;
            reach[i] = prereqDone || states[i] != kit::Level::Neutral || pipeline[i] == s.stage;
        }
    }

    const ImVec2 trackMin = ImGui::GetCursorScreenPos();
    const float  trackW   = ImGui::GetContentRegionAvail().x;
    const float  trackH   = M::reStepperH();
    const int clicked = kit::StepTrack("##re_stage_steps", labels.data(), states.data(), n,
                                       active, /*clickable=*/true, trackH);

    // The kit has no per-step tooltip hook, so explain a LOCKED step from the band it occupies
    // (StepTrack divides its width evenly). Reachable steps are left to the kit.
    {
        const float stepW = trackW / (float)n;
        for (int i = 0; i < n; ++i) {
            if (reach[i]) continue;
            const ImVec2 a(trackMin.x + stepW * (float)i, trackMin.y);
            const ImVec2 b(a.x + stepW, trackMin.y + trackH);
            if (ImGui::IsMouseHoveringRect(a, b)) {
                ImGui::SetTooltip("%s is locked \xe2\x80\x94 %s", labels[i],
                                  stageGateReason(pipeline[i], scanLane));
                break;
            }
        }
    }

    if (clicked >= 0 && clicked < n) {
        if (reach[clicked]) {
            s.stage = pipeline[clicked];
        } else {
            // Transient, not sticky: a permanent warning line under the stepper would go stale
            // the moment the blocking stage finishes.
            st.toast(ui::ToastLevel::Info,
                     std::string(labels[clicked]) + " is locked \xe2\x80\x94 " +
                         stageGateReason(pipeline[clicked], scanLane));
        }
    }
}

// ================================================================================
// Stage body
// ================================================================================

void drawStageBody(AppState& st, RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        if (kit::EmptyState("part", "Point a camera at a part",
                            "A Reverse Engineering session turns ordinary photos of a real object "
                            "into a dimensionally-correct, editable CAD model \xe2\x80\x94 for printing a "
                            "replacement, modifying it, or designing something that mates with it.\n\n"
                            "Select a session in the rail, or create one to begin.",
                            "New session")) {
            s.focusNewName = true;
            s.railCollapsed = false;
        }
        return;
    }

    switch (s.stage) {
        case Stage::Source:      drawSourcesPanel(st);       break;
        case Stage::Scale:       drawScalePanel(st);         break;
        case Stage::Reconstruct: drawReconstructStage(st);   break;
        case Stage::Model:       drawModelPanel(st);         break;
        case Stage::Review:      reveng::drawReviewStage(st); break;
        case Stage::Deliver:     drawDeliverPanel(st);       break;
    }
}

// ================================================================================
// Per-frame housekeeping
// ================================================================================

// Consume a session_id seeded from outside (Parts Library "Generate 3D model" → RE-32).
void consumePendingSession(AppState& st, RevEngState& s) {
    if (st.pendingRevengSessionId.empty() || !s.listedOnce) return;
    const std::string want = st.pendingRevengSessionId;
    st.pendingRevengSessionId.clear();
    for (int i = 0; i < (int)s.sessions.size(); ++i) {
        if (s.sessions[i].session_id == want) { s.selected = i; break; }
    }
    // Not in the list yet (backend still writing it) — expire the throttle so it surfaces fast.
    if (s.selected < 0 || s.selected >= (int)s.sessions.size() ||
        s.sessions[s.selected].session_id != want) {
        s.lastListT = -100.0;
    }
}

// Everything that must be dropped when the selected session changes. Note the webcam: the device
// stays OPEN (closing it would lose the negotiated mode and the focus/exposure lock), only the
// per-session capture bookkeeping is cleared.
void resetForSessionChange(RevEngState& s) {
    galleryFreeTextures(s.galleryImages);
    s.galleryImages.clear();
    s.gallerySession = s.selected;
    s.selectedImage = -1;

    s.splatLoaded = false;
    s.splatPathTried.clear();
    s.artifactSplatPath.clear();
    s.artifactMeshPath.clear();
    s.artifactSparseDir.clear();
    s.detailSessionId.clear();
    s.lastDetailT = -100.0;
    s.detailErr.clear();
    s.provenance.clear();
    s.honestyPipeline.clear();
    s.honestyTier.clear();
    for (int i = 0; i < kStageCount; ++i) s.stages[i].badge = "idle";

    s.recon = ReconstructState{};
    s.scale   = ScaleState{};
    s.model   = ModelState{};
    s.deliver = DeliverState{};

    SH().reconStartT  = -1.0;
    SH().reconElapsed = 0.0;


    webcamCaptureResetSession();
}

}  // namespace

// Release what the workspace holds OUTSIDE the GL frame loop: the camera device and its preview
// texture. The img_tex thumbnail LRU is deliberately NOT cleared — it is shared with the gallery
// and self-evicting, so dropping it here would dangle ids other panels still hold.
// See the seam note in reveng_workspace.h: app.cpp has no "workspace closed" callback yet, so
// drawRevEngWorkspace also calls this on re-entry after a frame gap.
void revengWorkspaceTeardown() {
    webcamCaptureShutdown();
    SH().webcamLive = false;
    SH().thumbs.clear();
}

// ---- entry point ---------------------------------------------------------------

void drawRevEngWorkspace(AppState& st) {
    // MUST be first: every Viewport3DPreview this frame republishes into a cleared slot table.
    kit::resetPreviewSlots();

    ShellState& sh = SH();
    const int frame = ImGui::GetFrameCount();
    // We stopped being drawn at some point => the user left the RE tab. app.cpp has no teardown
    // hook, so reclaim the camera here, before the first redraw of the returning tab.
    if (frame - sh.lastDrawFrame > 1 && sh.webcamLive) revengWorkspaceTeardown();
    sh.lastDrawFrame = frame;

    RevEngState& s = S();

    loadSessions(st);
    loadCapabilities(st);
    consumePendingSession(st, s);

    if (s.selected != s.gallerySession) resetForSessionChange(s);
    if (s.selected >= 0 && s.selected < (int)s.sessions.size()) loadSessionDetail(st);
    clampStageToPipeline(s);

    // The webcam widget only exists on the Scan lane's Source stage; when it stops being drawn
    // the device must be released rather than left holding /dev/video.
    const bool webcamReachable = s.selected >= 0 && s.stage == Stage::Source &&
                                 s.sourceMode == SourceMode::Scan;
    if (webcamReachable) {
        sh.webcamLive = true;
    } else if (sh.webcamLive) {
        sh.webcamLive = false;
        webcamCaptureShutdown();
    }

    const float gutter  = M::reGutter();
    const float stripW  = M::reThumbSm();
    const float railTgt = s.railCollapsed  ? stripW : M::reRailW();
    const float propTgt = s.propsCollapsed ? stripW : M::rePropW();
    const float railW   = approachPx(sh.railW, railTgt);
    const float propW   = approachPx(sh.propW, propTgt);

    // Column widths. When the window is too narrow for all three, the SIDE columns give way
    // first — the stage body is the work surface and must never be the thing that vanishes.
    const float totalW = ImGui::GetContentRegionAvail().x;
    float drawRailW = railW;
    float drawPropW = propW;
    float midW = totalW - drawRailW - drawPropW - 2.0f * gutter;
    const float midMin = M::rePreviewMinW();
    if (midW < midMin) {
        const float sideTotal = drawRailW + drawPropW;
        const float k = sideTotal > 0.0f
                            ? std::clamp(1.0f - (midMin - midW) / sideTotal, 0.0f, 1.0f)
                            : 0.0f;
        drawRailW *= k;
        drawPropW *= k;
        midW = std::max(0.0f, totalW - drawRailW - drawPropW - 2.0f * gutter);
    }

    // ---- left: session rail --------------------------------------------------
    if (s.railCollapsed && drawRailW <= stripW + M::reCardGap()) {
        drawCollapsedStrip("##reveng_rail_strip", "SESSIONS", &s.railCollapsed, drawRailW);
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
        ui::pushGlassChildStyle();
        ImGui::BeginChild("##reveng_rail", ImVec2(drawRailW, 0.0f), ImGuiChildFlags_Border);
        ui::popGlassChildStyle();
        ImGui::PopStyleVar();
        drawColumnHeader("Sessions", &s.railCollapsed);
        drawSessionRailBody(st);
        ImGui::EndChild();
    }

    // ---- centre: lane + stepper + stage body ---------------------------------
    ImGui::SameLine(0.0f, gutter);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
    ui::pushGlassChildStyle();
    ImGui::BeginChild("##reveng_center", ImVec2(midW, 0.0f), ImGuiChildFlags_Border);
    ui::popGlassChildStyle();
    ImGui::PopStyleVar();
    {
        if (drawSourceLane(s)) clampStageToPipeline(s);
        kit::GapControl();
        drawStageStepper(st, s);
        kit::Rule();
        kit::GapControl();

        ImGui::BeginChild("##reveng_stage_body", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
        drawStageBody(st, s);
        ImGui::EndChild();
    }
    ImGui::EndChild();

    // ---- right: properties ---------------------------------------------------
    ImGui::SameLine(0.0f, gutter);
    if (s.propsCollapsed && drawPropW <= stripW + M::reCardGap()) {
        drawCollapsedStrip("##reveng_props_strip", "DETAILS", &s.propsCollapsed, drawPropW);
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
        ui::pushGlassChildStyle();
        ImGui::BeginChild("##reveng_props", ImVec2(drawPropW, 0.0f), ImGuiChildFlags_Border);
        ui::popGlassChildStyle();
        ImGui::PopStyleVar();
        drawColumnHeader("Properties", &s.propsCollapsed);
        drawPropertiesBody(st);
        ImGui::EndChild();
    }
}

}  // namespace reveng

// Thin forwarders in the ui:: namespace so app.cpp calls ui::drawRevEngWorkspace(st).
void drawRevEngWorkspace(AppState& st) { reveng::drawRevEngWorkspace(st); }
void revengWorkspaceTeardown()          { reveng::revengWorkspaceTeardown(); }

}  // namespace ui
