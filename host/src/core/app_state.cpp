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
#include <thread>

#include "app_state.h"

#include "../bootstrap.h"
#include "../mesh.h"
#include "../renderer.h"

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
        s.planMode = j.value("plan_mode", true);
        s.maxIters = j.value("max_iters", 0);
        s.systemPrompt = j.value("system_prompt", "");
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
    j["plan_mode"] = s.planMode;
    if (s.maxIters > 0) j["max_iters"] = s.maxIters;
    if (!s.systemPrompt.empty()) j["system_prompt"] = s.systemPrompt;
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
