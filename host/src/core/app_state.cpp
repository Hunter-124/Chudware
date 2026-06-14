// AI-CAD shared application state + data model — implementations (extracted from gui_app.cpp).
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <set>
#include <thread>

#include "app_state.h"

#include "../bootstrap.h"
#include "../mesh.h"
#include "../renderer.h"

#include "../render/aicm_loader.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "../ui/feature_tree.h"  // W1-B: ui::featureTreeResetTransient (clear tree statics on doc change)

const char* kStarterCode =
    "# build123d is imported. Assign the final solid to `result`.\n"
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    fillet(p.edges().filter_by(Axis.Z), radius=5)\n"
    "    with Locations((-18, 0, 6), (18, 0, 6)):\n"
    "        CounterBoreHole(2.6, 5, 3, depth=12)\n"
    "result = p.part\n";

// ---------------- settings (AI provider) ----------------
std::string appdataDir() {
    const char* a = std::getenv("APPDATA");
    return std::string(a ? a : ".");
}
std::string configPath() { return appdataDir() + "\\AI-CAD\\config.json"; }

void loadSettings(Settings& s) {
    std::ifstream f(configPath());
    if (!f) return;
    try {
        nlohmann::json j;
        f >> j;
        std::string prov = j.value("provider", "anthropic");
        auto oai = j.value("openai", nlohmann::json::object());
        std::string base = oai.value("base_url", "");
        bool hasBase = !base.empty();
        s.provider = (prov == "anthropic") ? 0 : (hasBase ? 2 : 1);
        std::strncpy(s.model, j.value("model", "").c_str(), sizeof(s.model) - 1);
        std::strncpy(s.apiKey, j.value("api_key", "").c_str(), sizeof(s.apiKey) - 1);
        if (hasBase) {
            std::strncpy(s.baseUrl, base.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = oai.value("vision", false);
        }
        s.offlineMode = j.value("offline_mode", false);
        s.planMode = j.value("plan_mode", true);
        s.maxIters = j.value("max_iters", 0);
        s.systemPrompt = j.value("system_prompt", "");
        s.theme = j.value("theme", "dark");
        s.units = (j.value("units", std::string("mmgs")) == "ips") ? Units::IPS : Units::MMGS;
    } catch (...) {
    }
}

void saveSettings(const Settings& s) {
    nlohmann::json j;
    if (s.provider == 0) {
        j["provider"] = "anthropic";
        j["model"] = s.model[0] ? s.model : "claude-opus-4-8";
        j["api_key"] = s.apiKey;
    } else if (s.provider == 1) {
        j["provider"] = "openai";
        j["model"] = s.model[0] ? s.model : "gpt-4o";
        j["api_key"] = s.apiKey;
    } else {
        j["provider"] = "openai";
        j["model"] = s.model;
        j["api_key"] = s.apiKey[0] ? s.apiKey : "lm-studio";
        j["openai"] = {{"base_url", s.baseUrl}, {"vision", s.vision}};
    }
    j["offline_mode"] = s.offlineMode;
    j["plan_mode"] = s.planMode;
    if (s.maxIters > 0) j["max_iters"] = s.maxIters;
    if (!s.systemPrompt.empty()) j["system_prompt"] = s.systemPrompt;
    j["theme"] = s.theme;
    j["units"] = (s.units == Units::IPS) ? "ips" : "mmgs";
    try {
        std::filesystem::create_directories(appdataDir() + "\\AI-CAD");
        std::ofstream(configPath()) << j.dump(2);
    } catch (...) {
    }
}

// ---------------- textures ----------------
Tex loadTexture(const std::string& path) {
    Tex t;
    int n;
    unsigned char* d = stbi_load(path.c_str(), &t.w, &t.h, &n, 4);
    if (!d) return t;
    glGenTextures(1, &t.id);
    glBindTexture(GL_TEXTURE_2D, t.id);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, t.w, t.h, 0, GL_RGBA, GL_UNSIGNED_BYTE, d);
    stbi_image_free(d);
    return t;
}
void setWindowIcon(GLFWwindow* win, const std::string& path) {
    int w, h, n;
    unsigned char* d = stbi_load(path.c_str(), &w, &h, &n, 4);
    if (!d) return;
    GLFWimage img{w, h, d};
    glfwSetWindowIcon(win, 1, &img);
    stbi_image_free(d);
}

// The human-readable cause of a Python traceback.
std::string errSummary(const std::string& e) {
    std::string lastExc, firstLine;
    size_t start = 0;
    while (start <= e.size()) {
        size_t nl = e.find('\n', start);
        std::string ln = e.substr(start, (nl == std::string::npos ? e.size() : nl) - start);
        while (!ln.empty() && (ln.back() == '\r' || ln.back() == ' ')) ln.pop_back();
        if (firstLine.empty() && !ln.empty()) firstLine = ln;
        if (!ln.empty() && !std::isspace((unsigned char)ln[0]) &&
            ln.rfind("HINT", 0) != 0 && ln.rfind("Traceback", 0) != 0 &&
            (ln.find("Error") != std::string::npos || ln.find("Exception") != std::string::npos))
            lastExc = ln;
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    std::string s = !lastExc.empty() ? lastExc : firstLine;
    if (s.size() > 180) s = s.substr(0, 180) + "...";
    return s;
}

std::vector<PartInfo> parseParts(const nlohmann::json& arr) {
    std::vector<PartInfo> out;
    if (!arr.is_array()) return out;
    for (auto& p : arr) {
        if (!p.is_object()) continue;
        PartInfo pi;
        pi.name = jstr(p, "name");
        pi.stlPath = jstr(p, "stl_path");
        pi.volume = jnum(p, "volume");
        auto bb = p.value("bbox", nlohmann::json::object());
        if (bb.is_object() && bb.contains("size") && bb["size"].is_array() && bb["size"].size() == 3) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "%.1f x %.1f x %.1f mm", bb["size"][0].get<double>(),
                          bb["size"][1].get<double>(), bb["size"][2].get<double>());
            pi.dims = buf;
        }
        out.push_back(std::move(pi));
    }
    return out;
}

FitReport parseFit(const nlohmann::json& f) {
    FitReport r;
    if (!f.is_object()) return r;
    r.present = true;
    r.ok = f.value("ok", true);
    r.summary = jstr(f, "summary");
    auto pairs = [](const nlohmann::json& arr, const char* valKey) {
        std::vector<FitPair> v;
        if (arr.is_array())
            for (auto& e : arr) {
                if (!e.is_object()) continue;
                FitPair fp;
                fp.a = jstr(e, "a");
                fp.b = jstr(e, "b");
                fp.value = jnum(e, valKey);
                v.push_back(std::move(fp));
            }
        return v;
    };
    if (f.contains("interferences")) r.interferences = pairs(f["interferences"], "volume");
    if (f.contains("clearances")) r.clearances = pairs(f["clearances"], "distance");
    return r;
}

PlanInfo parsePlan(const nlohmann::json& p) {
    PlanInfo pi;
    if (!p.is_object()) return pi;
    pi.present = true;
    pi.name = jstr(p, "name");
    pi.summary = jstr(p, "summary");
    pi.fit = jstr(p, "fit");
    if (auto it = p.find("parts"); it != p.end() && it->is_array())
        for (auto& q : *it) {
            if (!q.is_object()) continue;
            PlanPart pp;
            pp.name = jstr(q, "name");
            pp.purpose = jstr(q, "purpose");
            pp.dims = jstr(q, "dimensions");
            if (auto f = q.find("features"); f != q.end() && f->is_array())
                for (auto& s : *f)
                    if (s.is_string()) pp.features.push_back(s.get<std::string>());
            pi.parts.push_back(std::move(pp));
        }
    return pi;
}

void freeChatTextures(std::vector<ChatMsg>& chat) {
    for (auto& m : chat)
        for (auto& s : m.steps)
            if (s.thumb.id) { glDeleteTextures(1, &s.thumb.id); s.thumb.id = 0; }
}

// ---------------- AppState methods (formerly run_gui lambdas) ----------------
void AppState::addLog(const std::string& s) {
    log.push_back(s);
    if (log.size() > 500) log.erase(log.begin());
    // C40: tee to a file when AI_CAD_LOG_FILE is set — the in-app console is hard to reach
    // when diagnosing interaction bugs (and stdout is lost for a windowed exe).
    static FILE* tee = [] {
        const char* p = std::getenv("AI_CAD_LOG_FILE");
        return p && *p ? std::fopen(p, "a") : nullptr;
    }();
    if (tee) { std::fputs(s.c_str(), tee); std::fputc('\n', tee); std::fflush(tee); }
}

// W1-B: post a transient toast. born is filled by drawToasts on the first frame it's seen (we can't
// reach ImGui's clock from here cheaply), so we leave born=0 and let drawToasts stamp it.
// C39: thread-safe — the worker errorSink calls this from background threads.
void AppState::toast(ui::ToastLevel level, std::string msg) {
    ui::Toast t;
    t.level = level;
    t.msg = std::move(msg);
    t.born = 0.0;  // 0 = unstamped; drawToasts stamps it with ImGui::GetTime() on first sight
    t.ttl = ui::toastTtl(level);
    std::lock_guard<std::mutex> lk(toastsMtx);
    toasts.push_back(std::move(t));
    // Cap the backing queue so an error storm can't grow it without bound (drawToasts shows <= 5).
    if (toasts.size() > 32) toasts.erase(toasts.begin(), toasts.begin() + (toasts.size() - 32));
}

// W1-B (item 4): shared undo/redo. Both the tree toolbar and the global keys call these.
core::OpsResponse AppState::docUndo() {
    core::OpsResponse r = docMirror.undo(worker);
    if (r.ok) {
        addLog("[doc] undo  rev=" + std::to_string(r.revision));
        if (!r.aicm_path.empty()) { std::string le; loadDocMesh(r.aicm_path, le); }
        // C38 (T1): the owner map was just rebuilt; re-derive the feature highlight so the tinted
        // faces track the retagged topology instead of pointing at stale face ids.
        ui::refreshFeatureHighlight(*this);
        toast(ui::ToastLevel::Info, "Undo: ok");
    } else {
        // doc_undo with an empty stack reports a benign error code; treat it as "nothing to undo".
        bool nothing = r.error_code == "nothing_to_undo" || r.error_code == "no_history" ||
                       r.error_code == "empty_stack";
        addLog("[doc] undo: " + (r.error_message.empty() ? r.error_code : r.error_message));
        toast(nothing ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
              nothing ? "Undo: nothing to undo"
                      : ("Undo failed: " + (r.error_message.empty() ? r.error_code : r.error_message)));
    }
    return r;
}

core::OpsResponse AppState::docRedo() {
    core::OpsResponse r = docMirror.redo(worker);
    if (r.ok) {
        addLog("[doc] redo  rev=" + std::to_string(r.revision));
        if (!r.aicm_path.empty()) { std::string le; loadDocMesh(r.aicm_path, le); }
        // C38 (T1): re-derive the feature highlight after the rebuild (see docUndo).
        ui::refreshFeatureHighlight(*this);
        toast(ui::ToastLevel::Info, "Redo: ok");
    } else {
        bool nothing = r.error_code == "nothing_to_redo" || r.error_code == "no_history" ||
                       r.error_code == "empty_stack";
        addLog("[doc] redo: " + (r.error_message.empty() ? r.error_code : r.error_message));
        toast(nothing ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
              nothing ? "Redo: nothing to redo"
                      : ("Redo failed: " + (r.error_message.empty() ? r.error_code : r.error_message)));
    }
    return r;
}

// Provision (force=false) or repair (force=true) the runtime on a background thread.
void AppState::startSetup(bool force) {
    if (setupRunning.exchange(true)) return;
    if (force) {
        // A repair rebuilds the venv from scratch (rmtree + recreate). The running worker
        // holds venv\Scripts\python.exe open, which on Windows blocks deletion and makes
        // provisioning fail (the rc=1 "repair failed" case). Stop it first; a successful
        // setup restarts it via tryStartWorker().
        worker.stop();
        workerStarted = false;
    }
    setupDone = false;
    status = force ? "Repairing runtime..." : "Setting up runtime...";
    statusKind = 0;
    addLog(force ? "[..] repairing runtime..."
                 : "[..] provisioning runtime (first run can take a minute)...");
    std::thread([this, force]() {
        std::string lg;
        bool ok = force ? repairRuntime(lg) : ensureRuntime(lg);
        std::lock_guard<std::mutex> lk(setupMtx);
        setupMsg = lg;
        setupOk = ok;
        setupDone = true;
        setupRunning = false;
    }).detach();
}

// Start the kernel worker; if it can't start, self-heal once instead of dead-ending.
void AppState::tryStartWorker() {
    workerStarted = worker.start(werr);
    if (workerStarted) {
        status = "Ready.";
        statusKind = 0;
        addLog("[ok] kernel worker ready");
        return;
    }
    addLog("[err] worker: " + werr);
    if (!autoSetupTried && !setupRunning.load()) {
        autoSetupTried = true;
        addLog("[..] attempting automatic runtime setup...");
        startSetup(runtimeReady());  // force a rebuild if a broken venv is present
    } else {
        status = "Runtime needs setup";
        statusKind = 2;
    }
}

void AppState::showPartMesh(int sel) {
    std::string e;
    if (sel >= 0 && sel < (int)asmParts.size()) {
        // isolate one body, tinted with its assembly color
        Mesh m;
        if (!loadBinarySTL(asmParts[sel].stlPath, m, e)) {
            addLog("[err] part mesh: " + e);
            return;
        }
        Renderer::PartMesh pm;
        pm.mesh = std::move(m);
        ImVec4 c = partColor(sel);
        pm.color[0] = c.x; pm.color[1] = c.y; pm.color[2] = c.z;
        renderer->setMeshes({pm});
        return;
    }
    // Whole model. Assemblies whose parts have their own STLs render each body in its
    // palette color (matching the contact sheet); otherwise fall back to the combined
    // mesh in the neutral material.
    bool distinct = false;
    for (auto& p : asmParts)
        if (!p.stlPath.empty() && p.stlPath != asmStl) { distinct = true; break; }
    if (asmParts.size() > 1 && distinct) {
        std::vector<Renderer::PartMesh> pms;
        for (size_t i = 0; i < asmParts.size(); ++i) {
            Mesh m;
            if (!loadBinarySTL(asmParts[i].stlPath, m, e)) continue;
            Renderer::PartMesh pm;
            pm.mesh = std::move(m);
            ImVec4 c = partColor((int)i);
            pm.color[0] = c.x; pm.color[1] = c.y; pm.color[2] = c.z;
            pms.push_back(std::move(pm));
        }
        if (!pms.empty()) { renderer->setMeshes(pms); return; }
    }
    if (asmStl.empty()) return;
    Mesh m;
    if (loadBinarySTL(asmStl, m, e)) renderer->setMesh(m);
    else addLog("[err] part mesh: " + e);
}

// Convert one AICM body's tessellation to a shaded Mesh (for the studio pass + camera bounds).
// Faithfully reuses the AICM positions/normals/indices and the per-EDGE polylines as feature
// edges, so the shaded view matches exactly what the id pass picks.
static Mesh meshFromBody(const render::BodyMesh& b) {
    Mesh m;
    m.positions = b.pos;
    m.normals = b.nrm;
    m.indices = b.idx;
    // feature edges: emit every segment of every edge polyline as a line-list pair
    for (const auto& es : b.edges) {
        for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
            const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
            const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
            m.edgeLines.insert(m.edgeLines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
        }
    }
    // bounds from positions
    Vec3 mn(1e30f, 1e30f, 1e30f), mx(-1e30f, -1e30f, -1e30f);
    for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
        float x = b.pos[i], y = b.pos[i + 1], z = b.pos[i + 2];
        if (x < mn.x) mn.x = x; if (y < mn.y) mn.y = y; if (z < mn.z) mn.z = z;
        if (x > mx.x) mx.x = x; if (y > mx.y) mx.y = y; if (z > mx.z) mx.z = z;
    }
    m.bbMin = mn; m.bbMax = mx; m.center = (mn + mx) * 0.5f;
    m.radius = std::max(0.001f, length(mx - mn) * 0.5f);
    return m;
}

bool AppState::loadDocMesh(const std::string& aicmPath, std::string& err) {
    if (!renderer || !idPass || !overlays) { err = "render objects not initialized"; return false; }
    render::DocMesh doc;
    if (!render::loadAicmFile(aicmPath, doc, err)) return false;
    if (doc.bodies.empty()) {
        // C48: a rebuild can legitimately produce ZERO bodies — every solid feature rolled
        // back (rollback bar), suppressed, or deleted, or a sketch-only document. That is an
        // empty MODEL, not a load error. Clear the shaded view + pick caches so the viewport
        // tracks the document; before, the stale previous mesh kept rendering and the
        // rollback bar looked like it did nothing.
        if (renderer) renderer->setMeshes(std::vector<Renderer::PartMesh>{});
        bodyVisible.clear();
        docBodyIds.clear();
        applyBodyVisibility();
        faceNormalCache.clear();
        edgeCentroidCache.clear();
        bodyBBoxCache.clear();
        faceBBoxCache.clear();
        selection.clear();
        hover = render::PickHit{};
        pickStatus.clear();
        docMeshHasScalar = false;
        docMeshHasDisp = false;
        hasDocMesh = false;   // the empty-doc fallback (datum planes + camera) takes over
        return true;
    }

    // Shaded view: render each body with its AICM color (camera bounds come from here).
    // C19: attach any SCLR / DISP overlay arrays so the renderer can colormap + deform.
    std::vector<Renderer::PartMesh> pms;
    std::vector<uint32_t> newBodyIds;  // AICM body id per renderer slot (parallels pms)
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        newBodyIds.push_back(b.body_id);
        Renderer::PartMesh pm;
        pm.mesh = meshFromBody(b);
        pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
        pm.metallic = b.metallic; pm.roughness = b.roughness;  // C32-A RealView material
        // First scalar field for this body (von Mises / |u| / thickness / …).
        for (const auto& sc : doc.scalars) {
            if (sc.body_id == b.body_id && sc.values.size() == b.n_verts) {
                pm.scalar = sc.values;
                pm.scalarMin = sc.vmin;
                pm.scalarMax = sc.vmax;
                break;
            }
        }
        // First displacement field for this body (FEA deformed shape).
        for (const auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vec.size() == (size_t)b.n_verts * 3) {
                pm.disp = df.vec;
                pm.dispScaleHint = df.scale_hint;
                break;
            }
        }
        pms.push_back(std::move(pm));
    }
    // C48: unconditional — pms can be empty when every body has no triangles; keeping the
    // previous meshes would desync the viewport from the document (same stale-mesh bug as
    // the zero-bodies case above).
    renderer->setMeshes(pms);

    // C19: surface overlay presence for the simulation panel's result controls.
    docMeshHasScalar = false;
    docMeshHasDisp = false;
    for (const auto& pm : pms) {
        if (!pm.scalar.empty()) docMeshHasScalar = true;
        if (!pm.disp.empty()) { docMeshHasDisp = true; dispScaleHint = pm.dispScaleHint; }
    }

    if (!idPass->setDoc(doc, err)) return false;
    if (!overlays->setDoc(doc, err)) return false;

    // Solid Bodies visibility: a rebuild with the SAME body-id list keeps the user's hidden set
    // (hide-by-ordinal is stable while the body set is unchanged); a different list — a feature
    // added/removed a body, so ordinals realigned — resets everything to visible. (Documented
    // limitation: hides are by ordinal, not by persistent body identity.)
    if (docBodyIds != newBodyIds || bodyVisible.size() != newBodyIds.size())
        bodyVisible.assign(newBodyIds.size(), 1);
    docBodyIds = std::move(newBodyIds);
    applyBodyVisibility();

    // C26-C: build per-face normal + centroid cache while the DocMesh is still in memory.
    // For each face, accumulate area-weighted triangle normals and centroids.
    faceNormalCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.pos.empty() || b.idx.empty() || b.faces.empty()) continue;
        for (const auto& fr : b.faces) {
            float nx = 0, ny = 0, nz = 0;  // accumulated area-weighted normal
            float cx = 0, cy = 0, cz = 0;  // accumulated area-weighted centroid
            float totalArea = 0;
            for (uint32_t t = 0; t < fr.tri_count; ++t) {
                size_t base = (size_t)(fr.first_tri + t) * 3;
                if (base + 2 >= b.idx.size()) break;
                uint32_t i0 = b.idx[base], i1 = b.idx[base + 1], i2 = b.idx[base + 2];
                // vertex positions
                const float* p0 = &b.pos[(size_t)i0 * 3];
                const float* p1 = &b.pos[(size_t)i1 * 3];
                const float* p2 = &b.pos[(size_t)i2 * 3];
                // edge vectors
                float e1x = p1[0]-p0[0], e1y = p1[1]-p0[1], e1z = p1[2]-p0[2];
                float e2x = p2[0]-p0[0], e2y = p2[1]-p0[1], e2z = p2[2]-p0[2];
                // cross product (area-weighted normal, magnitude = 2*area)
                float crx = e1y*e2z - e1z*e2y;
                float cry = e1z*e2x - e1x*e2z;
                float crz = e1x*e2y - e1y*e2x;
                float area = std::sqrt(crx*crx + cry*cry + crz*crz);
                nx += crx; ny += cry; nz += crz;
                float triCx = (p0[0]+p1[0]+p2[0]) * (1.0f/3.0f);
                float triCy = (p0[1]+p1[1]+p2[1]) * (1.0f/3.0f);
                float triCz = (p0[2]+p1[2]+p2[2]) * (1.0f/3.0f);
                cx += triCx * area; cy += triCy * area; cz += triCz * area;
                totalArea += area;
            }
            // normalize normal; normalize centroid by total area
            float len = std::sqrt(nx*nx + ny*ny + nz*nz);
            if (len < 1e-12f) continue;
            nx /= len; ny /= len; nz /= len;
            if (totalArea > 1e-12f) { cx /= totalArea; cy /= totalArea; cz /= totalArea; }
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)fr.face_id;
            faceNormalCache[key] = {nx, ny, nz, cx, cy, cz};
        }
    }

    // C27-A: per-edge centroid cache (mean of each edge's polyline points). Mirrors the face-cache
    // keying so callout anchors can resolve PickKind::Edge topo ids -> a world point.
    edgeCentroidCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.edges.empty() || b.edge_pts.empty()) continue;
        for (const auto& es : b.edges) {
            if (es.pt_count == 0) continue;
            double cx = 0, cy = 0, cz = 0;
            uint32_t n = 0;
            for (uint32_t p = 0; p < es.pt_count; ++p) {
                size_t base = (size_t)(es.first_pt + p) * 3;
                if (base + 2 >= b.edge_pts.size()) break;
                cx += b.edge_pts[base]; cy += b.edge_pts[base + 1]; cz += b.edge_pts[base + 2];
                ++n;
            }
            if (n == 0) continue;
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)es.edge_id;
            edgeCentroidCache[key] = {(float)(cx / n), (float)(cy / n), (float)(cz / n)};
        }
    }

    // W1-B (item 8): per-body and per-face axis-aligned bounding-box caches for Zoom-to-Selection.
    // Built from the same in-memory DocMesh positions; consumed by a later wave's zoom-to-fit.
    bodyBBoxCache.clear();
    faceBBoxCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        // Body bbox: sweep all positions.
        float bmn[3] = {1e30f, 1e30f, 1e30f}, bmx[3] = {-1e30f, -1e30f, -1e30f};
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            for (int c = 0; c < 3; ++c) {
                float v = b.pos[i + c];
                if (v < bmn[c]) bmn[c] = v;
                if (v > bmx[c]) bmx[c] = v;
            }
        }
        bodyBBoxCache[b.body_id] = {bmn[0], bmn[1], bmn[2], bmx[0], bmx[1], bmx[2]};
        // Per-face bbox: sweep the vertices referenced by each face's triangle range.
        if (b.idx.empty() || b.faces.empty()) continue;
        for (const auto& fr : b.faces) {
            if (fr.tri_count == 0) continue;
            float fmn[3] = {1e30f, 1e30f, 1e30f}, fmx[3] = {-1e30f, -1e30f, -1e30f};
            bool any = false;
            for (uint32_t t = 0; t < fr.tri_count; ++t) {
                size_t base = (size_t)(fr.first_tri + t) * 3;
                if (base + 2 >= b.idx.size()) break;
                for (int k = 0; k < 3; ++k) {
                    uint32_t vi = b.idx[base + k];
                    const float* p = &b.pos[(size_t)vi * 3];
                    for (int c = 0; c < 3; ++c) {
                        if (p[c] < fmn[c]) fmn[c] = p[c];
                        if (p[c] > fmx[c]) fmx[c] = p[c];
                    }
                    any = true;
                }
            }
            if (!any) continue;
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)fr.face_id;
            faceBBoxCache[key] = {fmn[0], fmn[1], fmn[2], fmx[0], fmx[1], fmx[2]};
        }
    }

    selection.clear();
    hover = render::PickHit{};
    pickStatus.clear();
    hasDocMesh = true;
    return true;
}

void AppState::clearViewportForNewDoc() {
    if (renderer) renderer->setMeshes(std::vector<Renderer::PartMesh>{});
    hasDocMesh = false;
    // Solid Bodies visibility: a fresh doc starts with no bodies and nothing hidden (stale hides
    // must never leak into the next document).
    bodyVisible.clear();
    docBodyIds.clear();
    applyBodyVisibility();
    asmParts.clear();
    selectedPart = -1;
    selection.clear();
    modelInfo.clear();
    faceNormalCache.clear();
    edgeCentroidCache.clear();
    bodyBBoxCache.clear();
    faceBBoxCache.clear();
    // W1-B (item 5): drop the tree's selection + in-progress rename/pending-op transients so a fresh
    // doc starts clean (the selection state now lives here; the rename buffer in feature_tree.cpp is
    // cleared via the exposed reset hook).
    selectedFeatureId.clear();
    treeScrollToSelection = false;
    ui::featureTreeResetTransient();
    // Wave-2 shared UI state: a fresh doc cancels any plane-pick step, tree-driven face highlight,
    // and pending zoom-to-bbox request.
    sketchPlanePickActive = false;
    featureHighlightFaces.clear();
    zoomToBBoxRequested = false;
}

// Solid Bodies hide/show: push the host-side visibility mask to the renderer (per-slot draw
// skip) and to the id pass (hidden-body pick skip), keeping both perfectly in sync. Safe to call
// with no doc loaded (both sinks tolerate empty state).
void AppState::applyBodyVisibility() {
    if (renderer) {
        size_t n = std::min(bodyVisible.size(), renderer->bodyCount());
        for (size_t i = 0; i < n; ++i) renderer->setBodyVisible(i, bodyVisible[i] != 0);
    }
    if (idPass) {
        std::set<uint32_t> hidden;
        size_t n = std::min(bodyVisible.size(), docBodyIds.size());
        for (size_t i = 0; i < n; ++i)
            if (!bodyVisible[i]) hidden.insert(docBodyIds[i]);
        idPass->setHiddenBodies(hidden);
    }
}

std::string AppState::pickLabel(const render::PickHit& h) const {
    if (!h.valid()) return std::string();
    const char* kind = "Item";
    switch (h.kind) {
        case render::PickKind::Face: kind = "Face"; break;
        case render::PickKind::Edge: kind = "Edge"; break;
        case render::PickKind::Vertex: kind = "Vertex"; break;
        default: break;
    }
    return std::string(kind) + " " + std::to_string(h.topo_id) + " of Body" +
           std::to_string(h.body_id);
}
