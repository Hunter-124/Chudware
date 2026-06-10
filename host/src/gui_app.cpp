// AI-CAD desktop GUI — branded, themed, docked, with AI chat + provider settings.
//
// Panels: Design (logo + prompt + code), Chat (converse with the assistant), Viewport
// (live model), Console (log). Menu bar exposes Settings (AI provider) and runtime repair.
// The runtime is provisioned in-app with progress if missing. Worker calls run on a
// background thread; STL upload + UI happen on the main GL thread.
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"
#include "stb_image.h"
#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "ui_theme.h"
#include "win_chrome.h"
#include "worker_client.h"

namespace {

const char* kStarterCode =
    "# build123d is imported. Assign the final solid to `result`.\n"
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    fillet(p.edges().filter_by(Axis.Z), radius=5)\n"
    "    with Locations((-18, 0, 6), (18, 0, 6)):\n"
    "        CounterBoreHole(2.6, 5, 3, depth=12)\n"
    "result = p.part\n";

enum JobKind { JOB_BUILD, JOB_DESIGN, JOB_CHAT };

// ---------------- settings (AI provider) -> %APPDATA%/AI-CAD/config.json ----------------
struct Settings {
    int provider = 2;  // 0 = Anthropic, 1 = OpenAI, 2 = Local (OpenAI-compatible).
                       // Default to a local LM Studio so the app is testable out of the box.
    char model[128] = "gemma-4-e4b";
    char apiKey[256] = "";
    char baseUrl[256] = "http://localhost:1234/v1";
    bool vision = true;
    bool planMode = true;        // draft a build plan (parts/dimensions/features) first
    int maxIters = 0;            // refine-pass cap per turn; 0 = automatic
    std::string systemPrompt;    // system prompt override; empty = built-in
};

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
struct Tex {
    unsigned int id = 0;
    int w = 0, h = 0;
};
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

// ---------------- multi-part / assembly model ----------------
// Distinct per-part colors — MUST match engine/render._PART_COLORS so the swatch in the GUI
// matches the body's color in the rendered contact sheet.
inline ImVec4 partColor(int i) {
    static const ImVec4 kColors[] = {
        {0.298f, 0.447f, 0.690f, 1}, {0.867f, 0.518f, 0.322f, 1}, {0.333f, 0.659f, 0.408f, 1},
        {0.769f, 0.306f, 0.322f, 1}, {0.506f, 0.447f, 0.702f, 1}, {0.576f, 0.471f, 0.376f, 1},
        {0.855f, 0.545f, 0.765f, 1}, {0.498f, 0.498f, 0.498f, 1}, {0.800f, 0.725f, 0.455f, 1},
        {0.392f, 0.710f, 0.804f, 1},
    };
    return kColors[((i % 10) + 10) % 10];
}

struct PartInfo {
    std::string name, stlPath, dims;
    double volume = 0;
};
struct FitPair {
    std::string a, b;
    double value = 0;  // overlap mm^3 (interference) or gap mm (clearance)
};
struct FitReport {
    bool present = false;
    bool ok = true;  // true = no interference
    std::string summary;
    std::vector<FitPair> interferences, clearances;
};

// ---------------- worker job ----------------
struct AgentEvent;  // defined later (richer chat model)

struct JobState {
    std::atomic<bool> running{false};
    std::atomic<bool> ready{false};
    std::mutex mtx;
    int kind = JOB_BUILD;
    std::string stl, info, error;
    bool errorIsCad = false;          // error came from CAD code that didn't build (expected,
                                      // the loop self-corrects) — NOT an app/runtime failure
    bool isAssembly = false;          // final result was a multi-part assembly
    std::vector<PartInfo> parts;      // per-part name/stl/dims (assemblies)
    FitReport fit;                    // final fit report (assemblies)
    bool converged = true;            // design loop ended by matching the request (not the cap)
    std::string stopReason;           // confirmed | stabilized | failed | no_code | max_iters
    std::mutex evMtx;
    std::vector<AgentEvent> events;  // streamed agent progress, drained on the main thread
};

// safe JSON accessors: tolerate missing keys AND explicit null (Python None) values.
inline std::string jstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
inline bool jbool(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return it != o.end() && it->is_boolean() && it->get<bool>();
}
inline int jint(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<int>() : 0;
}
inline double jnum(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : 0.0;
}

// The human-readable cause of a Python traceback: its last "SomethingError: ..." line
// (skipping the frames and any appended kernel HINT). Falls back to the first line.
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

// Parse the kernel's per-part list ([{name, stl_path, volume, bbox:{size:[x,y,z]}}, ...]).
inline std::vector<PartInfo> parseParts(const nlohmann::json& arr) {
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

// Parse the kernel's fit report ({ok, summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}).
inline FitReport parseFit(const nlohmann::json& f) {
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

// ---------------- build plan (plan mode) ----------------
// The agent's pre-code plan: each part with concrete dimensions and a feature list.
// The Python side normalizes dimensions to a display string and features to strings.
struct PlanPart {
    std::string name, purpose, dims;
    std::vector<std::string> features;
};
struct PlanInfo {
    bool present = false;
    std::string name, summary, fit;
    std::vector<PlanPart> parts;
};

inline PlanInfo parsePlan(const nlohmann::json& p) {
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

void launchJob(WorkerClient& w, JobState& job, int kind, std::string payload, bool reset,
               bool plan);

// ---------------- small ui helpers ----------------
// A caption button (minimize / maximize-restore / close) drawn with a crisp vector glyph
// and a full-height hover fill — close hovers red, the others a soft white wash.
enum WinBtn { BTN_MIN, BTN_MAX, BTN_CLOSE };
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    const ImVec2 size(h * 1.35f, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered) {
        ImU32 bg = kind == BTN_CLOSE ? IM_COL32(232, 17, 35, 255) : IM_COL32(255, 255, 255, 26);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? IM_COL32(255, 255, 255, 255)
                                                    : IM_COL32(226, 232, 240, 235);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;  // glyph half-size
    const float th = 1.4f;
    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {  // restore: two stacked squares
            dl->AddRect(ImVec2(c.x - s + 2, c.y - s), ImVec2(c.x + s, c.y + s - 2), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2), ImVec2(c.x + s - 2, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

bool primaryButton(const char* label, const ImVec2& size) {
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.176f, 0.831f, 0.749f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.31f, 0.90f, 0.82f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(0.10f, 0.69f, 0.61f, 1));
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.04f, 0.10f, 0.10f, 1));
    bool r = ImGui::Button(label, size);
    ImGui::PopStyleColor(4);
    return r;
}
void sectionHeader(ImFont* f, const char* text) {
    if (f) ImGui::PushFont(f);
    ImGui::TextUnformatted(text);
    if (f) ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2));
}
void dimText(const char* t) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                                ui::palette.textDim[2], 1));
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);
    ImGuiID main = root, left, bottom;
    left = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left, 0.31f, nullptr, &main);
    bottom = ImGui::DockBuilderSplitNode(main, ImGuiDir_Down, 0.24f, nullptr, &main);
    ImGui::DockBuilderDockWindow("Design", left);
    ImGui::DockBuilderDockWindow("Viewport", main);
    ImGui::DockBuilderDockWindow("Console", bottom);
    ImGui::DockBuilderFinish(root);
}

// One pass of the agent loop, shown as an expandable card in the conversation.
struct AgentStep {
    int index = 0;
    bool ok = false, done = false;
    std::string notes, thinking, code, diff, dims, error, contactSheet, stdoutText;
    bool isAssembly = false;       // this pass built a multi-part assembly
    std::vector<PartInfo> parts;   // per-part list (assemblies)
    FitReport fit;                 // interference/clearance report (assemblies)
    Tex thumb;                 // lazily loaded from contactSheet on the GL thread
    bool triedThumb = false;
    bool showThinking = false, showCode = false, showDiff = false, showError = false;
};

enum ErrKind { ERR_NONE = 0, ERR_CAD = 1, ERR_SYSTEM = 2 };

struct ChatMsg {
    bool fromUser = false;
    std::string text;              // user prompt, or the assistant's final summary
    bool running = false;          // assistant turn still streaming
    int errKind = ERR_NONE;        // ERR_CAD = generated code didn't build (calm, amber);
                                   // ERR_SYSTEM = app/runtime/provider failure (red)
    std::string liveStatus;        // transient "Iteration N: building..." while running
    PlanInfo plan;                 // the build plan drafted before the code (plan mode)
    std::vector<AgentStep> steps;  // assistant turn: the iteration trace
};

// A progress event streamed from the worker (parsed off the worker thread, applied on the
// main/GL thread so texture uploads stay on the GL context). kind: "status" | "iteration".
struct AgentEvent {
    std::string kind;
    int index = 0;
    bool ok = false, done = false;
    std::string phase, text, notes, thinking, code, diff, dims, error, contactSheet, stdoutText;
    std::string stlPath;  // this iteration's built mesh — drives the live viewport preview
    bool isAssembly = false;
    std::vector<PartInfo> parts;
    FitReport fit;
    PlanInfo plan;        // kind == "plan": the drafted build plan
};

void freeChatTextures(std::vector<ChatMsg>& chat) {
    for (auto& m : chat)
        for (auto& s : m.steps)
            if (s.thumb.id) { glDeleteTextures(1, &s.thumb.id); s.thumb.id = 0; }
}

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active) {
    if (active) ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.176f, 0.831f, 0.749f, 0.30f));
    bool clicked = ImGui::SmallButton(label);
    if (active) ImGui::PopStyleColor();
    return clicked;
}

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH) {
    int lines = 1;
    for (char c : code) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    if (h < lh + 10.0f) h = lh + 10.0f;
    ImGui::InputTextMultiline(id, (char*)code.c_str(), code.size() + 1, ImVec2(-1, h),
                              ImGuiInputTextFlags_ReadOnly);
    if (mono) ImGui::PopFont();
}

// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH) {
    int lines = 1;
    for (char c : diff) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.055f, 0.065f, 0.085f, 1));
    ImGui::BeginChild(id, ImVec2(0, h), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_HorizontalScrollbar);
    size_t start = 0;
    while (start <= diff.size()) {
        size_t nl = diff.find('\n', start);
        std::string ln = diff.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
        ImVec4 col(0.78f, 0.82f, 0.88f, 1);
        if (ln.rfind("+++", 0) == 0 || ln.rfind("---", 0) == 0) col = ImVec4(0.60f, 0.66f, 0.74f, 1);
        else if (ln.rfind("@@", 0) == 0) col = ImVec4(0.45f, 0.74f, 0.96f, 1);
        else if (!ln.empty() && ln[0] == '+') col = ImVec4(0.46f, 0.86f, 0.55f, 1);
        else if (!ln.empty() && ln[0] == '-') col = ImVec4(0.96f, 0.50f, 0.50f, 1);
        ImGui::TextColored(col, "%s", ln.empty() ? " " : ln.c_str());
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    if (mono) ImGui::PopFont();
}

// Dim, wrapped prose (reasoning / notes) in a scrolling box.
void drawProseBox(const char* id, const std::string& text, float maxH) {
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.085f, 0.10f, 0.13f, 1));
    ImGui::BeginChild(id, ImVec2(0, maxH), ImGuiChildFlags_Border);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.70f, 0.74f, 0.80f, 1));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed) {
    if (!fit.present) return;
    const ImVec4 good(0.46f, 0.86f, 0.55f, 1), bad(0.96f, 0.55f, 0.5f, 1),
        dim(0.62f, 0.66f, 0.72f, 1);
    ImGui::TextColored(fit.ok ? good : bad, "%s  %s", fit.ok ? "[fits]" : "[clash]",
                       fit.summary.empty() ? (fit.ok ? "No interference" : "Interference")
                                           : fit.summary.c_str());
    if (!detailed) return;
    for (const auto& f : fit.interferences) {
        ImGui::PushStyleColor(ImGuiCol_Text, bad);
        ImGui::BulletText("interference  %s <-> %s : %.1f mm\xc2\xb3", f.a.c_str(),
                          f.b.c_str(), f.value);
        ImGui::PopStyleColor();
    }
    for (const auto& f : fit.clearances) {
        ImGui::PushStyleColor(ImGuiCol_Text, dim);
        ImGui::BulletText("clearance  %s <-> %s : %.2f mm", f.a.c_str(), f.b.c_str(),
                          f.value);
        ImGui::PopStyleColor();
    }
}

// The build-plan card: an accent-edged panel listing each planned part with its purpose,
// committed dimensions (mono) and feature bullets — the agent's blueprint, shown before
// any code runs so the user can see (and steer) what is about to be built.
void drawPlanCard(const PlanInfo& plan, const ui::Fonts& fonts) {
    const ImVec4 accent(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.071f, 0.102f, 0.110f, 1));
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    dl->AddRectFilled(w0, ImVec2(w0.x + 3.0f, w0.y + ImGui::GetWindowHeight()),
                      IM_COL32(45, 212, 191, 150));
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accent, "Build plan");
    if (!plan.name.empty()) {
        ImGui::SameLine();
        ImGui::TextUnformatted(plan.name.c_str());
    }
    ImGui::PopFont();
    if (!plan.summary.empty()) dimText(plan.summary.c_str());
    for (size_t i = 0; i < plan.parts.size(); ++i) {
        const PlanPart& p = plan.parts[i];
        ImGui::PushID((int)i);
        ImGui::Dummy(ImVec2(0, 2));
        ImGui::ColorButton("##pdot", partColor((int)i),
                           ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                           ImVec2(10, 10));
        ImGui::SameLine(0, 7);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(p.name.c_str());
        ImGui::PopFont();
        if (!p.purpose.empty()) dimText(p.purpose.c_str());
        if (!p.dims.empty()) {
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.64f, 0.72f, 0.78f, 1));
            ImGui::TextWrapped("%s  (mm)", p.dims.c_str());
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.60f, 0.64f, 0.70f, 1));
        for (const auto& f : p.features) {
            ImGui::Bullet();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", f.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
    if (!plan.fit.empty()) {
        ImGui::Dummy(ImVec2(0, 2));
        dimText(("Fit: " + plan.fit).c_str());
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts) {
    if (parts.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
    ImGui::TextUnformatted("Parts:");
    ImGui::PopStyleColor();
    for (size_t i = 0; i < parts.size(); ++i) {
        ImGui::SameLine();
        std::string lbl = parts[i].name + (i + 1 < parts.size() ? "," : "");
        ImGui::TextColored(partColor((int)i), "%s", lbl.c_str());
    }
}

// Spawn a worker job on a background thread. Design/chat jobs stream live iteration events
// into job.events (drained on the main thread); `reset` starts a fresh agent session and
// `plan` lets the agent draft a build plan before writing code (plan mode).
void launchJob(WorkerClient& w, JobState& job, int kind, std::string payload, bool reset,
               bool plan) {
    if (job.running.exchange(true)) return;
    job.ready = false;
    job.kind = kind;
    {
        std::lock_guard<std::mutex> lk(job.mtx);
        job.error.clear(); job.stl.clear(); job.info.clear();
        job.errorIsCad = false;
        job.isAssembly = false; job.parts.clear(); job.fit = FitReport{};
        job.converged = true; job.stopReason.clear();
    }
    { std::lock_guard<std::mutex> lk(job.evMtx); job.events.clear(); }
    std::thread([&w, &job, kind, payload, reset, plan]() {
        nlohmann::json result;
        std::string err;
        bool design = (kind != JOB_BUILD);

        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            e.kind = jstr(d, "kind");
            e.index = jint(d, "index");
            e.ok = jbool(d, "ok");
            e.done = jbool(d, "done");
            e.phase = jstr(d, "phase");
            e.text = jstr(d, "text");
            e.notes = jstr(d, "notes");
            e.thinking = jstr(d, "thinking");
            e.code = jstr(d, "code");
            e.diff = jstr(d, "diff");
            e.dims = jstr(d, "dims");
            e.error = jstr(d, "error");
            e.contactSheet = jstr(d, "contact_sheet");
            e.stlPath = jstr(d, "stl_path");
            e.stdoutText = jstr(d, "stdout");
            e.isAssembly = jbool(d, "is_assembly");
            if (auto it = d.find("parts"); it != d.end()) e.parts = parseParts(*it);
            if (auto it = d.find("fit"); it != d.end()) e.fit = parseFit(*it);
            if (auto it = d.find("plan"); it != d.end()) e.plan = parsePlan(*it);
            std::lock_guard<std::mutex> lk(job.evMtx);
            job.events.push_back(std::move(e));
        };

        bool ok = design
            ? w.call("agent_design",
                     {{"request", payload}, {"name", "gui_design"},
                      {"session_id", "gui_design"}, {"reset", reset}, {"plan", plan}},
                     result, err, 900000, onEvent)
            : w.call("run_model", {{"code", payload}, {"name", "gui_build"}},
                     result, err, 120000);
        std::lock_guard<std::mutex> lk(job.mtx);
        if (!ok) {
            job.error = err;            // transport / worker / provider failure -> system error
            job.errorIsCad = false;
        } else {
            nlohmann::json mr = design
                ? (result.contains("result") && result["result"].is_object()
                       ? result["result"] : nlohmann::json::object())
                : result;
            if (!mr.value("ok", false)) {
                // The worker ran fine but the CAD script didn't build — a modeling
                // problem (model- or user-authored code), not an app failure.
                job.error = mr.value("error", "The CAD code didn't produce a solid.");
                job.errorIsCad = true;
            } else {
                job.stl = mr.value("stl_path", "");
                char buf[256] = "";
                auto bb = mr.value("bbox", nlohmann::json::object());
                if (bb.contains("size")) {
                    auto sz = bb["size"];
                    std::snprintf(buf, sizeof(buf), "%.1f x %.1f x %.1f mm  |  %.0f mm\xc2\xb3",
                                  sz[0].get<double>(), sz[1].get<double>(), sz[2].get<double>(),
                                  mr.value("volume", 0.0));
                }
                job.info = buf;
                if (design) {
                    job.info += "  |  " + std::to_string(result.value("iterations", 0)) + " iters";
                    job.converged = result.value("converged", true);
                    job.stopReason = result.value("stop_reason", "");
                }
                // Multi-part assembly: capture per-part list + fit report for the parts
                // panel and the fit readout (single parts leave these empty).
                job.isAssembly = mr.value("is_assembly", false);
                if (job.isAssembly) {
                    job.parts = parseParts(mr.value("parts", nlohmann::json::array()));
                    job.fit = parseFit(mr.value("fit", nlohmann::json::object()));
                }
            }
        }
        job.ready = true;
        job.running = false;
    }).detach();
}

}  // namespace

int run_gui(const char* screenshotPath) {
    const bool shot = screenshotPath != nullptr;
    if (!glfwInit()) return 1;
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    if (!shot) glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
    GLFWwindow* win = glfwCreateWindow(shot ? 1340 : 1440, shot ? 838 : 900, "AI-CAD", nullptr, nullptr);
    if (!win) { glfwTerminate(); return 1; }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) return 1;

    const std::string assets = exeDir() + "\\assets";
    setWindowIcon(win, assets + "\\logo_window.png");

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    static std::string iniPath = appdataDir() + "\\AI-CAD\\imgui.ini";
    io.IniFilename = shot ? nullptr : iniPath.c_str();

    float xs = 1.0f, ys = 1.0f;
    glfwGetWindowContentScale(win, &xs, &ys);
    float scale = shot ? 1.0f : xs;
    ui::applyTheme(scale);
    ui::Fonts fonts = ui::loadFonts(assets, scale);
    ImGui_ImplGlfw_InitForOpenGL(win, true);
    ImGui_ImplOpenGL3_Init("#version 330");

    // Borderless window: strip the OS title bar/frame and drive our own (drag/resize/snap
    // stay native). Must be installed after the ImGui GLFW backend so our WndProc is on top.
    static chrome::HitLayout hitLayout;
    hitLayout.resizeBorder = (int)(6 * scale);
    chrome::install(win, &hitLayout);

    Tex wordmark = loadTexture(assets + "\\logo_wordmark.png");
    Tex logoIcon = loadTexture(assets + "\\logo_icon.png");

    std::string rerr;
    Renderer renderer;
    renderer.init(960, 720, rerr);
    renderer.setBackground(0.17f, 0.19f, 0.23f, 0.085f, 0.095f, 0.115f);

    Settings settings;
    loadSettings(settings);
    // editor buffer for the system-prompt override (Settings > Agent)
    std::vector<char> spBuf(16384, 0);
    std::strncpy(spBuf.data(), settings.systemPrompt.c_str(), spBuf.size() - 1);

    std::vector<std::string> log;
    auto addLog = [&](const std::string& s) {
        log.push_back(s);
        if (log.size() > 500) log.erase(log.begin());
    };

    // ---------- runtime + worker ----------
    WorkerClient worker;
    std::string werr;
    bool workerStarted = false;
    bool rtReady = runtimeReady();
    std::atomic<bool> setupRunning{false}, setupDone{false}, setupOk{false};
    std::mutex setupMtx;
    std::string setupMsg;
    std::string status;
    int statusKind = 0;  // 0 = ok (teal), 1 = build issue (amber), 2 = system error (red)
    bool autoSetupTried = false;   // guard so startup auto-heal runs once, not in a loop
    bool importRepairTried = false; // guard so per-job ModuleNotFoundError repair runs once

    // Provision (force=false) or repair (force=true) the runtime on a background thread.
    auto startSetup = [&](bool force) {
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
        std::thread([&, force]() {
            std::string lg;
            bool ok = force ? repairRuntime(lg) : ensureRuntime(lg);
            std::lock_guard<std::mutex> lk(setupMtx);
            setupMsg = lg;
            setupOk = ok;
            setupDone = true;
            setupRunning = false;
        }).detach();
    };

    // Start the kernel worker; if it can't start, self-heal once instead of dead-ending.
    auto tryStartWorker = [&]() {
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
    };

    if (rtReady) {
        tryStartWorker();
    } else {
        status = "Runtime not installed";
        statusKind = 2;
        addLog("[!] runtime not found at %APPDATA%\\AI-CAD\\runtime");
        autoSetupTried = true;
        startSetup(false);  // self-provision from the payload next to the exe / source tree
    }

    // welcome model
    {
        Mesh m;
        std::string e;
        if (loadBinarySTL(appdataDir() + "\\AI-CAD\\cache\\mounting_plate\\mounting_plate.stl", m, e))
            renderer.setMesh(m);
    }

    JobState job;
    std::vector<char> code(64 * 1024, 0);
    std::strncpy(code.data(), kStarterCode, code.size() - 1);
    std::vector<char> prompt(2048, 0);
    std::strncpy(prompt.data(), "a knurled hex standoff, 22mm tall, M3 clearance bore", prompt.size() - 1);
    std::vector<ChatMsg> chat;
    {
        ChatMsg w;
        w.text = "Hi! Describe a part and I'll model it in build123d, render it, and check the "
                 "result - then keep refining it with you. Set your AI provider in Settings first.";
        chat.push_back(w);
    }
    // current assembly (populated when a build/design returns multiple parts)
    std::vector<PartInfo> asmParts;
    FitReport asmFit;
    std::string asmStl;               // combined assembly STL (shown when selectedPart == -1)
    int selectedPart = -1;            // -1 = whole assembly; >=0 = index into asmParts
    auto showPartMesh = [&](int sel) {
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
            renderer.setMeshes({pm});
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
            if (!pms.empty()) { renderer.setMeshes(pms); return; }
        }
        if (asmStl.empty()) return;
        Mesh m;
        if (loadBinarySTL(asmStl, m, e)) renderer.setMesh(m);
        else addLog("[err] part mesh: " + e);
    };

    const char* shotMode = std::getenv("AI_CAD_SHOT");  // screenshot-only: settings|chat|assembly|plan
    bool wantSettings = shot && shotMode && std::strcmp(shotMode, "settings") == 0;
    bool wantChat = shot && shotMode && std::strcmp(shotMode, "chat") == 0;
    bool wantAssembly = shot && shotMode && std::strcmp(shotMode, "assembly") == 0;
    bool wantPlan = shot && shotMode && std::strcmp(shotMode, "plan") == 0;
    if (wantPlan) {
        // The moment plan mode shines: the plan card is drafted and visible while the
        // first code pass is still being written.
        ChatMsg u;
        u.fromUser = true;
        u.text = "an M3 hex standoff, 30mm tall";
        chat.push_back(u);
        ChatMsg a;
        a.running = true;
        a.liveStatus = "Build plan ready - writing the CAD code...";
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        PlanPart pp;
        pp.name = "standoff";
        pp.purpose = "spaces a PCB off its mounting plate";
        pp.dims = "across_flats 10, height 30, bore_radius 1.7";
        pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                       "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
        a.plan.parts.push_back(pp);
        chat.push_back(a);
    }
    if (wantAssembly) {
        // Demo a 3-part fit-test: base plate, lid above it, peg through both (clash on the
        // peg). Populates the viewport parts panel + a chat card so --ui-screenshot shows the
        // assembly UI. A real color-coded render is shown if its cache PNG exists.
        const std::string asmDir = appdataDir() + "\\AI-CAD\\cache\\screenshot_asm\\";
        asmStl = asmDir + "screenshot_asm_assembly.stl";
        std::string sheet = asmDir + "screenshot_asm_grid.png";
        // Per-part STLs (generated once for the demo) so the viewport shows the bodies
        // color-coded; missing files just fall back to the combined mesh.
        asmParts = {{"base", asmDir + "screenshot_asm_base.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"lid", asmDir + "screenshot_asm_lid.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"peg", asmDir + "screenshot_asm_peg.stl", "6.0 x 6.0 x 30.0 mm", 848.2}};
        asmFit.present = true;
        asmFit.ok = false;
        asmFit.summary = "2 interfering pair(s); largest base<->peg = 226.2 mm^3";
        asmFit.interferences = {{"base", "peg", 226.2}, {"lid", "peg", 226.2}};
        asmFit.clearances = {{"base", "lid", 10.0}};
        showPartMesh(-1);  // show the combined assembly mesh if its STL was pre-generated
        ChatMsg u; u.fromUser = true;
        u.text = "a base plate, a lid 10mm above it, and a peg through both";
        chat.push_back(u);
        ChatMsg a;
        a.text = "Built a 3-part assembly. The peg passes through both plates - tell me if that "
                 "press-fit is intended, or I'll trim it to clear.";
        AgentStep s;
        s.index = 0; s.ok = true; s.done = false; s.isAssembly = true;
        s.dims = "40.0 x 30.0 x 38.0 mm";
        s.parts = asmParts;
        s.fit = asmFit;
        s.contactSheet = sheet;   // color-coded render (loaded if present)
        s.notes = "base and lid sit 10mm apart; the peg interferes with both - intended as a "
                  "press-fit, or should I add radial clearance?";
        a.steps.push_back(s);
        chat.push_back(a);
    }
    if (wantChat) {
        ChatMsg u; u.fromUser = true; u.text = "make it 30mm tall with a chamfered top edge";
        chat.push_back(u);
        ChatMsg a;
        a.text = "Raised the standoff to 30mm and chamfered the top rim. Ask for another tweak "
                 "and I'll revise this same part.";
        // The plan-mode card the agent drafts before writing code.
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        {
            PlanPart pp;
            pp.name = "standoff";
            pp.purpose = "spaces a PCB off its mounting plate";
            pp.dims = "across_flats 10, height 30, bore_radius 1.7";
            pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                           "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
            a.plan.parts.push_back(pp);
        }
        // A self-corrected build issue first, so the calm amber styling is visible.
        AgentStep f;
        f.index = 0; f.ok = false; f.done = false;
        f.error = "Traceback (most recent call last):\n"
                  "  File \"engine\\kernel.py\", line 361, in _exec_model\n"
                  "    exec(compile(code, \"<model>\", \"exec\"), g)\n"
                  "  File \"<model>\", line 9, in <module>\n"
                  "TypeError: chamfer() got an unexpected keyword argument 'radius'";
        a.steps.push_back(f);
        AgentStep s;
        s.index = 1; s.ok = true; s.done = true;
        s.dims = "22.0 x 19.1 x 30.0 mm  |  8243 mm^3";
        s.notes = "The previous part was 22mm tall. I raised total_h to 30 and added a 1mm "
                  "chamfer on the top edge ring; the bore and across-flats are unchanged.";
        s.thinking = "Looking at the prior render the proportions were right but short. I only "
                     "need to change the height variable and add a chamfer on the top face's "
                     "perimeter edges - everything else should stay parametric.";
        s.code = "af = 11.0\ntotal_h = 30.0   # was 22\nbore_r = 1.7\n"
                 "hex_r = af / (3 ** 0.5)\nface = RegularPolygon(radius=hex_r, side_count=6)\n"
                 "result = extrude(face, amount=total_h)\n"
                 "result -= Cylinder(radius=bore_r, height=total_h)\n"
                 "top = result.edges().filter_by(Plane.XY).group_by(Axis.Z)[-1]\n"
                 "result = chamfer(top, length=1.0)";
        s.diff = "--- previous\n+++ current\n@@ -1,4 +1,6 @@\n af = 11.0\n-total_h = 22.0\n"
                 "+total_h = 30.0   # was 22\n bore_r = 1.7\n+top = result.edges()."
                 "filter_by(Plane.XY).group_by(Axis.Z)[-1]\n+result = chamfer(top, length=1.0)";
        a.steps.push_back(s);
        chat.push_back(a);
    }
    std::vector<char> chatInput(2048, 0);
    std::string modelInfo;
    std::string livePhase;        // what the agent is doing right now (viewport overlay)
    std::string buildErrSummary;  // last "Build code" failure, shown under the editor
    std::string buildErrFull;     // its full traceback, behind a Details toggle
    bool showBuildErr = false;
    float yaw = 35, pitch = 28, zoom = 1.0f;
    bool layoutInit = false, resetLayout = false, openSettings = false;
    bool showDesign = true, showViewport = true, showConsole = true;
    bool showAbout = false, designStarted = false;
    int frame = 0;

    while (!glfwWindowShouldClose(win)) {
        glfwPollEvents();

        if (setupDone.exchange(false)) {
            bool ok;
            std::string msg;
            {
                std::lock_guard<std::mutex> lk(setupMtx);
                ok = setupOk;
                msg = setupMsg;
            }
            if (ok) {
                addLog("[ok] " + msg);
                tryStartWorker();
            } else {
                addLog("[err] " + msg);
                status = "Setup needed";
                statusKind = 2;
            }
        }

        // Drain streamed agent progress into the active (running) assistant message so the
        // conversation updates live, iteration by iteration (texture loads stay on this thread).
        {
            std::vector<AgentEvent> evs;
            {
                std::lock_guard<std::mutex> lk(job.evMtx);
                if (!job.events.empty()) evs.swap(job.events);
            }
            if (!evs.empty()) {
                ChatMsg* active = nullptr;
                for (auto it = chat.rbegin(); it != chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                for (auto& e : evs) {
                    if (e.kind == "status") {
                        livePhase = e.text;
                        if (active) active->liveStatus = e.text;
                    } else if (e.kind == "plan") {
                        if (active) active->plan = e.plan;
                        livePhase = "Build plan ready - writing the CAD code...";
                    } else if (e.kind == "iteration") {
                        if (active) {
                            AgentStep* st = nullptr;
                            for (auto& s : active->steps)
                                if (s.index == e.index) { st = &s; break; }
                            if (!st) { active->steps.emplace_back(); st = &active->steps.back(); }
                            st->index = e.index; st->ok = e.ok; st->done = e.done;
                            st->notes = e.notes; st->thinking = e.thinking; st->code = e.code;
                            st->diff = e.diff; st->dims = e.dims; st->error = e.error;
                            st->contactSheet = e.contactSheet; st->stdoutText = e.stdoutText;
                            st->isAssembly = e.isAssembly; st->parts = e.parts; st->fit = e.fit;
                            active->liveStatus.clear();
                        }
                        // Live preview: show each successful build in the viewport the moment
                        // it lands, color-coded per part for assemblies (we're on the GL thread).
                        if (e.ok && !e.stlPath.empty()) {
                            asmStl = e.stlPath;
                            asmParts = e.isAssembly ? e.parts : std::vector<PartInfo>{};
                            asmFit = e.isAssembly ? e.fit : FitReport{};
                            selectedPart = -1;
                            showPartMesh(-1);
                            if (!e.dims.empty()) modelInfo = e.dims;
                            livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " built - evaluating the render...";
                        } else if (!e.ok) {
                            livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " - build issue, revising the code...";
                        }
                    }
                }
            }
        }

        if (job.ready.exchange(false)) {
            std::lock_guard<std::mutex> lk(job.mtx);
            livePhase.clear();
            std::string assistant;
            int assistantErr = ERR_NONE;
            if (!job.error.empty() && job.errorIsCad) {
                // The CAD script didn't build. That's normal territory for an AI design loop
                // (it usually self-corrects mid-run) — report it calmly, details on demand,
                // and don't dress a modeling problem up as an app crash.
                std::string cause = errSummary(job.error);
                status = "Build didn't converge";
                statusKind = 1;
                addLog("[cad] " + cause);
                assistantErr = ERR_CAD;
                if (job.kind == JOB_BUILD) {
                    buildErrSummary = cause;
                    buildErrFull = job.error;
                } else {
                    assistant = "I couldn't get this design to build cleanly - the CAD code kept "
                                "failing.\nLast error: " + cause + "\nThis is a modeling problem, "
                                "not an app problem. Try rephrasing or simplifying the request "
                                "(each iteration's Error details has the full trace).";
                }
            } else if (!job.error.empty()) {
                status = "System error";
                statusKind = 2;
                std::string e = job.error.substr(0, 300);
                addLog("[err] " + e);
                assistantErr = ERR_SYSTEM;
                // A missing Python module means the venv is incomplete — auto-repair once.
                bool missingModule = job.error.find("ModuleNotFoundError") != std::string::npos ||
                                     job.error.find("No module named") != std::string::npos;
                if (missingModule && !importRepairTried && !setupRunning.load()) {
                    importRepairTried = true;
                    addLog("[..] runtime module missing — auto-repairing (use AI-CAD > Repair runtime if this persists)...");
                    startSetup(true);
                    assistant = "A required module is missing from the runtime — repairing now (may take a minute). Try again once the console shows setup complete.";
                } else {
                    assistant = "Something went wrong on the app side (your design is fine):\n" + e;
                    // Point at Settings only for credential-shaped failures; other errors
                    // (connection refused, model missing) carry their own guidance.
                    std::string low = e;
                    for (auto& c : low) c = (char)std::tolower((unsigned char)c);
                    static const char* kAuthMarks[] = {
                        "401", "403", "unauthorized", "forbidden", "api key", "api_key",
                        "apikey", "authentication", "invalid key", "credential"};
                    for (const char* mark : kAuthMarks) {
                        if (low.find(mark) != std::string::npos) {
                            assistant += "\n(This looks like an authentication problem - "
                                         "set your provider/API key in Settings.)";
                            break;
                        }
                    }
                }
            } else if (!job.stl.empty()) {
                Mesh m;
                std::string e;
                if (loadBinarySTL(job.stl, m, e)) {
                    renderer.setMesh(m);
                    status = "Done";
                    statusKind = 0;
                    modelInfo = job.info;
                    buildErrSummary.clear();
                    addLog("[ok] built  " + job.info);
                    // Refresh the parts panel for the new model (cleared for single parts so
                    // the panel disappears), then re-load assemblies color-coded per part.
                    asmStl = job.stl;
                    asmParts = job.parts;
                    asmFit = job.fit;
                    selectedPart = -1;
                    if (job.isAssembly) showPartMesh(-1);
                    if (job.isAssembly && job.fit.present) {
                        assistant = "Done - " + job.info + ".\n" +
                                    (job.fit.ok ? "Fit OK - " : "Fit issue - ") + job.fit.summary +
                                    "\nSee the Viewport (parts list + fit readout). Ask for a tweak and I'll revise.";
                        addLog((job.fit.ok ? "[ok] fit: " : "[err] fit: ") + job.fit.summary);
                    } else {
                        assistant = "Done - " + job.info + ".\nSee the Viewport. Ask for a tweak and I'll revise.";
                    }
                    if ((job.kind == JOB_DESIGN || job.kind == JOB_CHAT) && !job.converged)
                        assistant += "\n(Stopped at the iteration cap - say \"keep going\" "
                                     "and I'll keep refining this same design.)";
                } else {
                    status = "STL load failed";
                    statusKind = 2;
                    addLog("[err] " + e);
                    assistant = "Built it, but couldn't load the mesh: " + e;
                    assistantErr = ERR_SYSTEM;
                }
            }
            if (job.kind == JOB_DESIGN || job.kind == JOB_CHAT) {
                ChatMsg* active = nullptr;
                for (auto it = chat.rbegin(); it != chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                if (active) {
                    active->running = false;
                    active->liveStatus.clear();
                    active->errKind = assistantErr;
                    if (!assistant.empty()) active->text = assistant;
                } else if (!assistant.empty()) {
                    ChatMsg m; m.text = assistant; m.errKind = assistantErr;
                    chat.push_back(m);
                }
            }
        }

        bool busy = job.running.load();

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::NewFrame();

        if (shot && frame == 1) {
            if (wantSettings) openSettings = true;
            if (wantChat || wantAssembly || wantPlan)
                designStarted = true;  // show the conversation takeover
        }

        // ---------- custom title bar (our own; the OS title bar is stripped) ----------
        ImGuiViewport* vp = ImGui::GetMainViewport();
        const bool maximized = glfwGetWindowAttrib(win, GLFW_MAXIMIZED) != 0;

        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(14.0f, 12.0f));
        const float barH = ImGui::GetFrameHeight();
        hitLayout.titleBarHeight = (int)barH;
        ImGui::PushStyleColor(ImGuiCol_MenuBarBg, ImVec4(0.110f, 0.125f, 0.157f, 1.0f));
        if (ImGui::BeginMainMenuBar()) {
            // brand mark on the left (draggable caption around it)
            const float iconH = barH * 0.52f;
            if (logoIcon.id) {
                ImGui::SetCursorPosX(12.0f);
                ImGui::SetCursorPosY((barH - iconH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)logoIcon.id, ImVec2(iconH, iconH));
                ImGui::SameLine(0, 9);
            } else {
                ImGui::SetCursorPosX(14.0f);
            }
            if (wordmark.id) {
                const float wmH = barH * 0.40f;
                ImGui::SetCursorPosY((barH - wmH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)wordmark.id,
                             ImVec2(wmH * (float)wordmark.w / wordmark.h, wmH));
                ImGui::SameLine(0, 18);
            }

            // menus — record their horizontal span so the native hit-test keeps them clickable
            hitLayout.menuLeft = (int)ImGui::GetCursorScreenPos().x;
            if (ImGui::BeginMenu("AI-CAD")) {
                if (ImGui::MenuItem("Settings...")) openSettings = true;
                if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                                    !setupRunning.load() && !job.running.load()))
                    startSetup(true);
                if (ImGui::MenuItem("Reset layout")) resetLayout = true;
                ImGui::Separator();
                if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("View")) {
                ImGui::MenuItem("Design / Chat", nullptr, &showDesign);
                ImGui::MenuItem("Viewport", nullptr, &showViewport);
                ImGui::MenuItem("Console", nullptr, &showConsole);
                ImGui::Separator();
                if (ImGui::MenuItem("Reset layout")) resetLayout = true;
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("Help")) {
                if (ImGui::MenuItem("About AI-CAD")) showAbout = true;
                ImGui::EndMenu();
            }
            hitLayout.menuRight = (int)ImGui::GetItemRectMax().x;

            // window controls, right-aligned (minimize / maximize-restore / close).
            // Position each one absolutely so they share one baseline (the menu bar's
            // horizontal flow otherwise drifts arbitrary widgets vertically).
            const float bw = barH * 1.35f;
            const float bx = ImGui::GetWindowWidth() - bw * 3.0f;
            hitLayout.controlsLeft = (int)bx;
            hitLayout.controlsRight = (int)(bx + bw * 3.0f);
            ImGui::SetCursorPos(ImVec2(bx, 0.0f));
            if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
            ImGui::SetCursorPos(ImVec2(bx + bw, 0.0f));
            if (windowButton("##max", BTN_MAX, barH, maximized)) {
                if (maximized) glfwRestoreWindow(win);
                else glfwMaximizeWindow(win);
            }
            ImGui::SetCursorPos(ImVec2(bx + bw * 2.0f, 0.0f));
            if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);
            ImGui::EndMainMenuBar();
        }
        ImGui::PopStyleColor();
        ImGui::PopStyleVar();  // FramePadding

        // ---------- dockspace host (between the title bar and the status bar) ----------
        const float statusH = 30.0f * scale;
        ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + barH));
        ImGui::SetNextWindowSize(ImVec2(vp->Size.x, vp->Size.y - barH - statusH));
        ImGui::SetNextWindowViewport(vp->ID);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
        ImGuiWindowFlags hostFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
            ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
            ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
            ImGuiWindowFlags_NoDocking;
        ImGui::Begin("##DockHost", nullptr, hostFlags);
        ImGui::PopStyleVar(3);
        ImGuiID dockId = ImGui::GetID("AICADDock");
        if ((!layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) || resetLayout) {
            buildDefaultLayout(dockId, ImVec2(vp->Size.x, vp->Size.y - barH - statusH));
            layoutInit = true;
            resetLayout = false;
        }
        ImGui::DockSpace(dockId, ImVec2(0, 0), ImGuiDockNodeFlags_None);
        ImGui::End();

        // ---------- status bar (state + live agent phase left, provider/model right) ----------
        {
            ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + vp->Size.y - statusH));
            ImGui::SetNextWindowSize(ImVec2(vp->Size.x, statusH));
            ImGui::SetNextWindowViewport(vp->ID);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14, 5));
            ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0.075f, 0.086f, 0.108f, 1));
            ImGui::Begin("##StatusBar", nullptr, hostFlags);
            ImVec4 sc = statusKind == 2 ? ImVec4(0.96f, 0.55f, 0.42f, 1)
                      : statusKind == 1 ? ImVec4(0.95f, 0.76f, 0.42f, 1)
                      : ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                               ui::palette.accent[2], 1);
            ImVec2 dp0 = ImGui::GetCursorScreenPos();
            float dr = ImGui::GetFontSize() * 0.24f;
            if (busy)  // pulse while a job runs
                sc.w = 0.45f + 0.55f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
            ImGui::GetWindowDrawList()->AddCircleFilled(
                ImVec2(dp0.x + dr + 1, dp0.y + ImGui::GetTextLineHeight() * 0.55f), dr,
                ImGui::ColorConvertFloat4ToU32(sc));
            sc.w = 1.0f;
            ImGui::Dummy(ImVec2(dr * 2 + 8, 0));
            ImGui::SameLine();
            ImGui::TextColored(sc, "%s", status.c_str());
            if (busy && !livePhase.empty()) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.55f, 0.60f, 0.67f, 1));
                ImGui::Text("|  %s", livePhase.c_str());
                ImGui::PopStyleColor();
            }
            // provider / model summary, right-aligned; click to open Settings
            const char* provName = settings.provider == 0 ? "Anthropic"
                                 : settings.provider == 1 ? "OpenAI" : "Local";
            std::string ptxt = std::string(provName) + "  /  " +
                               (settings.model[0] ? settings.model : "default") +
                               (settings.planMode ? "   [plan mode]" : "");
            float ptw = ImGui::CalcTextSize(ptxt.c_str()).x;
            ImGui::SameLine(ImGui::GetWindowWidth() - ptw - 16);
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.55f, 0.60f, 0.67f, 1));
            ImGui::TextUnformatted(ptxt.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered()) {
                ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                ImGui::SetTooltip("AI provider & agent settings");
            }
            if (ImGui::IsItemClicked()) openSettings = true;
            ImGui::End();
            ImGui::PopStyleColor();
            ImGui::PopStyleVar(3);
        }

        bool canRun = workerStarted && !busy;

        // ---------- Design ----------
        if (showDesign) {
        if (ImGui::Begin("Design"))
        {
            float panelW = ImGui::GetContentRegionAvail().x;
            if (wordmark.id) {
                float w = panelW < 230 ? panelW : 230.0f;
                ImGui::Image((ImTextureID)(intptr_t)wordmark.id, ImVec2(w, w * (float)wordmark.h / wordmark.w));
            } else {
                sectionHeader(fonts.h1, "AI-CAD");
            }
            dimText("Parametric CAD, driven by AI");

            if (!workerStarted) {
                ImGui::Dummy(ImVec2(0, 6));
                ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.20f, 0.13f, 0.10f, 1));
                ImGui::BeginChild("setup", ImVec2(0, 132), ImGuiChildFlags_Border);
                if (setupRunning.load()) {
                    ImGui::TextColored(ImVec4(0.98f, 0.78f, 0.45f, 1), "Setting up runtime...");
                    dimText("First run installs an isolated Python + build123d (~once).");
                } else {
                    ImGui::TextColored(ImVec4(0.98f, 0.7f, 0.5f, 1), "Runtime not ready");
                    dimText("Run the packaged build (dist) or install.ps1 so the engine can "
                            "self-install, then use Repair runtime.");
                    if (ImGui::Button("Try setup now")) startSetup(false);
                }
                ImGui::EndChild();
                ImGui::PopStyleColor();
            }

            ImGui::Dummy(ImVec2(0, 6));
            ImGui::Separator();
            ImGui::Dummy(ImVec2(0, 6));

            if (!designStarted) {
                // ----- prompt mode: describe a part -----
                sectionHeader(fonts.h2, "Describe a part");
                ImGui::SetNextItemWidth(-1);
                bool promptEnter = ImGui::InputTextMultiline(
                    "##prompt", prompt.data(), prompt.size(),
                    ImVec2(-1, ImGui::GetTextLineHeight() * 2.6f),
                    ImGuiInputTextFlags_EnterReturnsTrue |
                        ImGuiInputTextFlags_CtrlEnterForNewLine);
                dimText("Enter starts the design - Ctrl+Enter for a new line.");
                ImGui::Dummy(ImVec2(0, 4));
                dimText("Or try one of these:");
                static const char* kExamples[] = {
                    "a GoPro tripod mount adapter",
                    "a vented Raspberry Pi 5 case with a snap-on lid",
                    "an M3 hex standoff, 25mm tall",
                };
                for (int ei = 0; ei < 3; ++ei) {
                    if (ImGui::SmallButton(kExamples[ei]))
                        std::strncpy(prompt.data(), kExamples[ei], prompt.size() - 1);
                }
                ImGui::Dummy(ImVec2(0, 2));
                bool canStart = canRun && prompt[0];
                if (!canStart) ImGui::BeginDisabled();
                bool goDesign = primaryButton("Design with AI", ImVec2(-1, 38));
                if (!canStart) ImGui::EndDisabled();
                if ((goDesign || promptEnter) && canStart) {
                    std::string p = prompt.data();
                    designStarted = true;        // conversation takes over this section
                    { ChatMsg u; u.fromUser = true; u.text = p; chat.push_back(u); }
                    { ChatMsg a; a.running = true; chat.push_back(a); }  // live assistant turn
                    addLog("[..] designing: " + p);
                    status = "Designing...";
                    statusKind = 0;
                    launchJob(worker, job, JOB_DESIGN, p, /*reset=*/true,
                              settings.planMode);  // new part = fresh session
                }
            } else {
                // ----- conversation takeover (bounded so the code/build section below stays
                //       visible — the chat never covers it) -----
                sectionHeader(fonts.h2, "Conversation");
                ImGui::SameLine();
                const float nb = 108.0f;
                ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - nb);
                if (busy) ImGui::BeginDisabled();
                if (ImGui::Button("+ New part", ImVec2(nb, 0))) {
                    designStarted = false;       // back to "Design with AI"
                    freeChatTextures(chat);
                    chat.clear();
                    { ChatMsg w; w.text = "Describe a part and I'll model it in build123d, "
                                          "render it, and refine it with you."; chat.push_back(w); }
                    modelInfo.clear();
                    livePhase.clear();
                    asmParts.clear();
                    asmFit = FitReport{};
                    asmStl.clear();
                    selectedPart = -1;
                    chatInput[0] = 0;
                    status = workerStarted ? "Ready." : status;
                    statusKind = 0;
                }
                if (busy) ImGui::EndDisabled();

                const float inputH = ImGui::GetFrameHeightWithSpacing() + 8;
                const float buildReserve = 200.0f;  // keep room for the code/build section below
                float chatH = ImGui::GetContentRegionAvail().y - inputH - buildReserve;
                if (chatH < 130.0f) chatH = 130.0f;
                ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_Border);
                const ImVec4 accentCol(ui::palette.accent[0], ui::palette.accent[1],
                                       ui::palette.accent[2], 1);
                const float logW = ImGui::GetContentRegionAvail().x;
                for (size_t mi = 0; mi < chat.size(); ++mi) {
                    ChatMsg& m = chat[mi];
                    ImGui::PushID((int)mi);
                    if (m.fromUser) {
                        // user turn: a soft accent-tinted bubble
                        ImGui::PushStyleColor(ImGuiCol_ChildBg,
                                              ImVec4(0.176f, 0.831f, 0.749f, 0.07f));
                        ImGui::PushStyleColor(ImGuiCol_Border,
                                              ImVec4(0.176f, 0.831f, 0.749f, 0.22f));
                        ImGui::BeginChild("##umsg", ImVec2(0, 0),
                                          ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
                        ImGui::PushFont(fonts.medium);
                        ImGui::TextColored(accentCol, "You");
                        ImGui::PopFont();
                        ImGui::TextWrapped("%s", m.text.c_str());
                        ImGui::EndChild();
                        ImGui::PopStyleColor(2);
                        ImGui::Dummy(ImVec2(0, 6));
                        ImGui::PopID();
                        continue;
                    }
                    ImGui::PushFont(fonts.medium);
                    ImGui::TextColored(ImVec4(0.78f, 0.82f, 0.90f, 1), "Assistant");
                    ImGui::PopFont();
                    if (m.plan.present) drawPlanCard(m.plan, fonts);
                    if (!m.text.empty()) {
                        if (m.errKind == ERR_SYSTEM)
                            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.96f, 0.6f, 0.5f, 1));
                        else if (m.errKind == ERR_CAD)
                            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.93f, 0.80f, 0.56f, 1));
                        ImGui::TextWrapped("%s", m.text.c_str());
                        if (m.errKind != ERR_NONE) ImGui::PopStyleColor();
                    }

                    // iteration trace — one expandable card per pass of the agent loop
                    for (size_t si = 0; si < m.steps.size(); ++si) {
                        AgentStep& s = m.steps[si];
                        ImGui::PushID((int)si);
                        ImGui::Dummy(ImVec2(0, 2));
                        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.082f, 0.094f, 0.118f, 1));
                        ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                                          ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
                        // Build issues are amber, not red: a failed pass is normal mid-loop —
                        // the error goes straight back to the model, which usually fixes it.
                        ImVec4 badge = s.ok ? (s.done ? accentCol : ImVec4(0.60f, 0.78f, 0.95f, 1))
                                            : ImVec4(0.95f, 0.76f, 0.42f, 1);
                        const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
                        ImGui::PushFont(fonts.medium);
                        ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
                        ImGui::PopFont();
                        if (!s.dims.empty()) {
                            ImGui::SameLine();
                            ImGui::PushFont(fonts.mono);
                            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
                            ImGui::Text("%s", s.dims.c_str());
                            ImGui::PopStyleColor();
                            ImGui::PopFont();
                        }

                        // assembly: colored parts line + interference/clearance readout
                        if (s.isAssembly) {
                            drawPartsLine(s.parts);
                            drawFitReadout(s.fit, /*detailed=*/true);
                        }

                        // render thumbnail (lazy GL upload — we're on the GL thread here)
                        if (!s.contactSheet.empty()) {
                            if (!s.triedThumb) {
                                s.thumb = loadTexture(s.contactSheet);
                                s.triedThumb = true;
                            }
                            if (s.thumb.id) {
                                float tw = logW < 220.0f ? logW : 220.0f;
                                float th = tw * (float)s.thumb.h / (float)s.thumb.w;
                                ImGui::Image((ImTextureID)(intptr_t)s.thumb.id, ImVec2(tw, th));
                            }
                        }

                        if (!s.notes.empty()) dimText(s.notes.c_str());
                        if (!s.ok && !s.error.empty()) {
                            // Calm one-line cause; the raw traceback stays behind a toggle so
                            // a self-correcting pass never reads like a crash.
                            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.76f, 0.42f, 1));
                            ImGui::TextWrapped("%s", errSummary(s.error).c_str());
                            ImGui::PopStyleColor();
                            dimText("The CAD code failed this pass; the error was fed back to "
                                    "the AI to correct.");
                        }

                        // expanders: the model's reasoning, the full code, and what changed
                        bool first = true;
                        if (!s.thinking.empty()) {
                            if (toggleChip("Thoughts", s.showThinking)) s.showThinking = !s.showThinking;
                            first = false;
                        }
                        if (!s.code.empty()) {
                            if (!first) ImGui::SameLine();
                            if (toggleChip("Code", s.showCode)) s.showCode = !s.showCode;
                            first = false;
                        }
                        if (!s.diff.empty()) {
                            if (!first) ImGui::SameLine();
                            if (toggleChip("Diff", s.showDiff)) s.showDiff = !s.showDiff;
                            first = false;
                        }
                        if (!s.ok && !s.error.empty()) {
                            if (!first) ImGui::SameLine();
                            if (toggleChip("Error details", s.showError)) s.showError = !s.showError;
                            first = false;
                        }
                        if (s.showThinking && !s.thinking.empty())
                            drawProseBox("##think", s.thinking, 150.0f);
                        if (s.showCode && !s.code.empty())
                            drawCodeBox("##code_v", s.code, fonts.mono, 260.0f);
                        if (s.showDiff && !s.diff.empty())
                            drawDiffBox("##diff_v", s.diff, fonts.mono, 220.0f);
                        if (s.showError && !s.error.empty())
                            drawCodeBox("##err_v", s.error, fonts.mono, 200.0f);
                        ImGui::EndChild();
                        ImGui::PopStyleColor();
                        ImGui::PopID();
                    }

                    if (m.running) {
                        ImGui::Dummy(ImVec2(0, 2));
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
                        ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..."
                                                                      : m.liveStatus.c_str());
                        ImGui::PopStyleColor();
                    }
                    ImGui::Dummy(ImVec2(0, 8));
                    ImGui::PopID();
                }
                if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 6) ImGui::SetScrollHereY(1.0f);
                ImGui::EndChild();

                bool send = false;
                ImGui::SetNextItemWidth(-72);
                if (ImGui::InputText("##chatin", chatInput.data(), chatInput.size(),
                                     ImGuiInputTextFlags_EnterReturnsTrue)) {
                    send = true;
                    ImGui::SetKeyboardFocusHere(-1);  // keep typing without re-clicking
                }
                ImGui::SameLine();
                if (!canRun) ImGui::BeginDisabled();
                if (ImGui::Button("Send", ImVec2(-1, 0))) send = true;
                if (!canRun) ImGui::EndDisabled();
                if (send && canRun && chatInput[0]) {
                    std::string msg = chatInput.data();
                    { ChatMsg u; u.fromUser = true; u.text = msg; chat.push_back(u); }
                    { ChatMsg a; a.running = true; chat.push_back(a); }  // live assistant turn
                    addLog("[..] chat: " + msg);
                    status = "Refining...";
                    statusKind = 0;
                    launchJob(worker, job, JOB_CHAT, msg, /*reset=*/false,
                              settings.planMode);  // continue the session
                    chatInput[0] = 0;
                }
            }

            // ----- build123d code: ALWAYS visible below (in both modes) -----
            ImGui::Dummy(ImVec2(0, 8));
            ImGui::Separator();
            ImGui::Dummy(ImVec2(0, 6));
            sectionHeader(fonts.h2, "build123d code");
            ImGui::PushFont(fonts.mono);
            float codeH = ImGui::GetContentRegionAvail().y - 46;
            if (codeH < 110) codeH = 110;
            ImGui::InputTextMultiline("##code", code.data(), code.size(), ImVec2(-1, codeH));
            ImGui::PopFont();
            if (!canRun) ImGui::BeginDisabled();
            if (ImGui::Button("Build code", ImVec2(-1, 34))) {
                addLog("[..] building code");
                status = "Building...";
                statusKind = 0;
                buildErrSummary.clear();
                buildErrFull.clear();
                launchJob(worker, job, JOB_BUILD, std::string(code.data()), false, false);
            }
            if (!canRun) ImGui::EndDisabled();
            if (!buildErrSummary.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.76f, 0.42f, 1));
                ImGui::TextWrapped("Build issue - %s", buildErrSummary.c_str());
                ImGui::PopStyleColor();
                if (toggleChip(showBuildErr ? "Hide details" : "Details", showBuildErr))
                    showBuildErr = !showBuildErr;
                if (showBuildErr && !buildErrFull.empty())
                    drawCodeBox("##builderr", buildErrFull, fonts.mono, 180.0f);
            }
        }
        ImGui::End();
        }

        // ---------- Viewport ----------
        if (showViewport) {
        if (ImGui::Begin("Viewport"))
        {
            struct V { const char* l; float y, p; };
            const V views[] = {{"Iso", 35, 28}, {"Front", 0, 0}, {"Right", 90, 0}, {"Top", 0, 89}};
            for (int i = 0; i < 4; ++i) {
                if (i) ImGui::SameLine();
                if (ImGui::Button(views[i].l)) { yaw = views[i].y; pitch = views[i].p; }
            }
            ImGui::SameLine();
            if (ImGui::Button("Reset")) { yaw = 35; pitch = 28; zoom = 1.0f; }

            // ---- assembly fit readout + parts list (multi-part builds only) ----
            if (!asmParts.empty()) {
                ImGui::Separator();
                drawFitReadout(asmFit, /*detailed=*/true);   // always visible (key readout)
                // parts selector — scrolls if there are many bodies
                float ph = ImGui::GetContentRegionAvail().y * 0.30f;
                ph = ph > 150 ? 150 : (ph < 78 ? 78 : ph);
                ImGui::BeginChild("partspanel", ImVec2(0, ph), ImGuiChildFlags_Border);
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
                ImGui::TextUnformatted("Parts  (click to isolate in the viewport)");
                ImGui::PopStyleColor();
                if (ImGui::RadioButton("Assembly (all parts)", selectedPart == -1)) {
                    selectedPart = -1;
                    showPartMesh(-1);
                }
                for (int pi2 = 0; pi2 < (int)asmParts.size(); ++pi2) {
                    ImGui::ColorButton(("##sw" + std::to_string(pi2)).c_str(), partColor(pi2),
                                       ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                       ImVec2(12, 12));
                    ImGui::SameLine(0, 6);
                    std::string lbl = asmParts[pi2].name + "##p" + std::to_string(pi2);
                    if (ImGui::RadioButton(lbl.c_str(), selectedPart == pi2)) {
                        selectedPart = pi2;
                        showPartMesh(pi2);
                    }
                    if (!asmParts[pi2].dims.empty()) {
                        ImGui::SameLine();
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.50f, 0.54f, 0.60f, 1));
                        ImGui::Text("  %s", asmParts[pi2].dims.c_str());
                        ImGui::PopStyleColor();
                    }
                }
                ImGui::EndChild();
            }

            ImVec2 avail = ImGui::GetContentRegionAvail();
            int vw = (int)avail.x > 64 ? (int)avail.x : 64;
            int vh = (int)avail.y > 64 ? (int)avail.y : 64;
            renderer.resize(vw, vh);
            renderer.renderOrbit(yaw, pitch, zoom);
            ImGui::Image((ImTextureID)(intptr_t)renderer.colorTexture(), ImVec2((float)vw, (float)vh),
                         ImVec2(0, 1), ImVec2(1, 0));
            ImVec2 vr0 = ImGui::GetItemRectMin(), vr1 = ImGui::GetItemRectMax();
            if (ImGui::IsItemHovered()) {
                if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    yaw += io.MouseDelta.x * 0.4f;
                    pitch += io.MouseDelta.y * 0.4f;
                    pitch = pitch > 89 ? 89 : (pitch < -89 ? -89 : pitch);
                }
                if (io.MouseWheel != 0) {
                    zoom *= (1.0f + io.MouseWheel * 0.1f);
                    zoom = zoom < 0.1f ? 0.1f : (zoom > 10 ? 10 : zoom);
                }
            }

            // ---- overlays on the 3D view ----
            ImDrawList* vdl = ImGui::GetWindowDrawList();
            if (busy) {
                // live progress pill: pulsing dot + the agent's current phase
                std::string lt = livePhase.empty() ? std::string("Working...") : livePhase;
                ImVec2 ts = ImGui::CalcTextSize(lt.c_str());
                float maxW = (vr1.x - vr0.x) - 58.0f;
                ImVec2 p0(vr0.x + 12, vr0.y + 12);
                ImVec2 p1(p0.x + (ts.x > maxW ? maxW : ts.x) + 36.0f, p0.y + ts.y + 12.0f);
                vdl->AddRectFilled(p0, p1, IM_COL32(10, 14, 19, 215), (p1.y - p0.y) * 0.5f);
                vdl->AddRect(p0, p1, IM_COL32(45, 212, 191, 70), (p1.y - p0.y) * 0.5f);
                float pulse = 0.35f + 0.65f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
                vdl->AddCircleFilled(ImVec2(p0.x + 14, (p0.y + p1.y) * 0.5f), 4.0f,
                                     ImGui::ColorConvertFloat4ToU32(
                                         ImVec4(0.176f, 0.831f, 0.749f, pulse)));
                vdl->PushClipRect(p0, ImVec2(p1.x - 8, p1.y), true);
                vdl->AddText(ImVec2(p0.x + 24, p0.y + 6), IM_COL32(226, 232, 240, 240), lt.c_str());
                vdl->PopClipRect();
            }
            if (!modelInfo.empty() && renderer.hasMesh()) {
                // dimensions readout, bottom-left
                ImFont* mf = fonts.mono ? fonts.mono : ImGui::GetFont();
                float fs = mf->FontSize * 0.92f;
                ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, modelInfo.c_str());
                ImVec2 p0(vr0.x + 12, vr1.y - ts.y - 22.0f);
                ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
                vdl->AddRectFilled(p0, p1, IM_COL32(10, 14, 19, 185), 6.0f);
                vdl->AddText(mf, fs, ImVec2(p0.x + 10, p0.y + 5),
                             IM_COL32(176, 186, 198, 235), modelInfo.c_str());
            }
        }
        ImGui::End();
        }

        // ---------- Console ----------
        if (showConsole) {
        if (ImGui::Begin("Console"))
        {
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.07f, 0.08f, 0.10f, 1));
            ImGui::BeginChild("##log", ImVec2(0, 0), ImGuiChildFlags_None);
            for (auto& line : log) {
                ImVec4 col(0.78f, 0.82f, 0.88f, 1);
                if (line.rfind("[err]", 0) == 0 || line.rfind("[!]", 0) == 0)
                    col = ImVec4(0.96f, 0.55f, 0.5f, 1);
                else if (line.rfind("[cad]", 0) == 0)
                    col = ImVec4(0.95f, 0.76f, 0.42f, 1);  // build issue, not an app error
                else if (line.rfind("[ok]", 0) == 0)
                    col = ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1);
                else if (line.rfind("[..]", 0) == 0)
                    col = ImVec4(0.62f, 0.66f, 0.72f, 1);
                ImGui::TextColored(col, "%s", line.c_str());
            }
            if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4) ImGui::SetScrollHereY(1.0f);
            ImGui::EndChild();
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::End();
        }

        // ---------- Settings modal ----------
        if (openSettings) {
            ImGui::OpenPopup("Settings");
            openSettings = false;
        }
        ImGui::SetNextWindowSize(ImVec2(580, 640), ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(), ImGuiCond_Appearing,
                                ImVec2(0.5f, 0.5f));
        if (ImGui::BeginPopupModal("Settings", nullptr, ImGuiWindowFlags_NoResize)) {
            // tab content scrolls in its own region; Save/Cancel stay pinned at the bottom
            ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
            ImGui::BeginChild("##settabsarea",
                              ImVec2(0, ImGui::GetContentRegionAvail().y - 52));
            if (ImGui::BeginTabBar("##settabs")) {
                if (ImGui::BeginTabItem("AI provider")) {
                    ImGui::Dummy(ImVec2(0, 2));
                    dimText("Used by 'Design with AI' and chat. Stored in "
                            "%APPDATA%\\AI-CAD\\config.json.");
                    ImGui::Dummy(ImVec2(0, 4));
                    const char* provs[] = {"Anthropic (Claude)", "OpenAI",
                                           "Local (OpenAI-compatible)"};
                    ImGui::SetNextItemWidth(-1);
                    ImGui::Combo("##prov", &settings.provider, provs, 3);
                    ImGui::Dummy(ImVec2(0, 4));
                    ImGui::TextUnformatted("Model");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##model", settings.model, sizeof(settings.model));
                    if (settings.provider != 2) {
                        ImGui::TextUnformatted("API key");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##key", settings.apiKey, sizeof(settings.apiKey),
                                         ImGuiInputTextFlags_Password);
                    } else {
                        ImGui::TextUnformatted("Base URL");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##base", settings.baseUrl, sizeof(settings.baseUrl));
                        ImGui::TextUnformatted("API key (optional)");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##lkey", settings.apiKey, sizeof(settings.apiKey),
                                         ImGuiInputTextFlags_Password);
                        ImGui::Checkbox("Model supports vision (send screenshots)",
                                        &settings.vision);
                    }
                    ImGui::EndTabItem();
                }
                if (ImGui::BeginTabItem("Agent", nullptr,
                                        wantSettings ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2));
                    ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                    dimText("The agent first drafts a build plan - the parts, concrete "
                            "dimensions and features - shows it in the conversation, then "
                            "writes the CAD code against it.");
                    ImGui::Dummy(ImVec2(0, 4));
                    ImGui::TextUnformatted("Refine-pass cap per request");
                    ImGui::SetNextItemWidth(140);
                    ImGui::InputInt("##miters", &settings.maxIters);
                    if (settings.maxIters < 0) settings.maxIters = 0;
                    if (settings.maxIters > 64) settings.maxIters = 64;
                    ImGui::SameLine();
                    dimText("0 = automatic (stops once the render matches)");
                    ImGui::Dummy(ImVec2(0, 6));
                    ImGui::Separator();
                    ImGui::Dummy(ImVec2(0, 4));
                    ImGui::TextUnformatted("System prompt override");
                    dimText("Leave empty to use the built-in prompt (recommended). A custom "
                            "prompt fully replaces it - keep the `result`/`parts` contract "
                            "and the python+json two-block reply format or the agent breaks.");
                    ImGui::PushFont(fonts.mono);
                    ImGui::InputTextMultiline("##sysprompt", spBuf.data(), spBuf.size(),
                                              ImVec2(-1, ImGui::GetTextLineHeight() * 10));
                    ImGui::PopFont();
                    bool canFetch = workerStarted && !busy;
                    if (!canFetch) ImGui::BeginDisabled();
                    if (ImGui::Button("Load built-in prompt")) {
                        nlohmann::json pr;
                        std::string perr;
                        if (worker.call("get_prompts", nlohmann::json::object(), pr, perr,
                                        20000)) {
                            std::fill(spBuf.begin(), spBuf.end(), 0);
                            std::strncpy(spBuf.data(), pr.value("system_prompt", "").c_str(),
                                         spBuf.size() - 1);
                        } else {
                            addLog("[err] get_prompts: " + perr);
                        }
                    }
                    if (!canFetch) ImGui::EndDisabled();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Copy the built-in prompt into the editor as a "
                                          "starting point");
                    ImGui::SameLine();
                    if (ImGui::Button("Clear (use built-in)"))
                        std::fill(spBuf.begin(), spBuf.end(), 0);
                    ImGui::EndTabItem();
                }
                ImGui::EndTabBar();
            }
            ImGui::EndChild();
            ImGui::PopStyleColor();
            if (primaryButton("Save", ImVec2(120, 34))) {
                settings.systemPrompt = spBuf.data();
                saveSettings(settings);
                addLog("[ok] saved settings");
                ImGui::CloseCurrentPopup();
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(100, 34))) {
                std::fill(spBuf.begin(), spBuf.end(), 0);  // discard edits
                std::strncpy(spBuf.data(), settings.systemPrompt.c_str(), spBuf.size() - 1);
                ImGui::CloseCurrentPopup();
            }
            ImGui::EndPopup();
        }

        // ---------- About modal ----------
        if (showAbout) {
            ImGui::OpenPopup("About AI-CAD");
            showAbout = false;
        }
        ImGui::SetNextWindowSize(ImVec2(380, 0), ImGuiCond_Appearing);
        if (ImGui::BeginPopupModal("About AI-CAD", nullptr, ImGuiWindowFlags_NoResize)) {
            if (wordmark.id) {
                ImGui::Image((ImTextureID)(intptr_t)wordmark.id,
                             ImVec2(190, 190 * (float)wordmark.h / wordmark.w));
            } else {
                sectionHeader(fonts.h1, "AI-CAD");
            }
            dimText("Parametric CAD, driven by AI.");
            ImGui::Dummy(ImVec2(0, 4));
            ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
            ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
            ImGui::Dummy(ImVec2(0, 8));
            if (primaryButton("Close", ImVec2(110, 32))) ImGui::CloseCurrentPopup();
            ImGui::EndPopup();
        }

        ImGui::Render();
        int dw, dh;
        glfwGetFramebufferSize(win, &dw, &dh);
        glViewport(0, 0, dw, dh);
        glClearColor(0.06f, 0.07f, 0.08f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());

        if (shot && ++frame >= 6) {
            std::vector<unsigned char> px(dw * dh * 4);
            glReadBuffer(GL_BACK);
            glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
            std::vector<unsigned char> flip(dw * dh * 4);
            for (int y = 0; y < dh; ++y)
                std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
            stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
            std::printf("ui screenshot -> %s (%dx%d)\n", screenshotPath, dw, dh);
            break;
        }
        glfwSwapBuffers(win);
    }

    freeChatTextures(chat);  // release iteration thumbnails while the GL context is still live
    worker.stop();
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
