// AI-CAD shared application state + data model (extracted from gui_app.cpp).
//
// Holds the structs the worker/agent loop produces (Settings, JobState, ChatMsg, AgentStep,
// AgentEvent, PlanInfo, PartInfo, FitReport, ...) and the AppState aggregate that bundles every
// run_gui() local so the panel modules can share it. Behavior-preserving split — names and
// semantics are unchanged from the monolith.
#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../ui_theme.h"
#include "../win_chrome.h"
#include "../worker_client.h"

struct GLFWwindow;
class Renderer;

extern const char* kStarterCode;

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

std::string appdataDir();
std::string configPath();
void loadSettings(Settings& s);
void saveSettings(const Settings& s);

// ---------------- textures ----------------
struct Tex {
    unsigned int id = 0;
    int w = 0, h = 0;
};
Tex loadTexture(const std::string& path);
void setWindowIcon(GLFWwindow* win, const std::string& path);

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
std::string errSummary(const std::string& e);

// Parse the kernel's per-part list ([{name, stl_path, volume, bbox:{size:[x,y,z]}}, ...]).
std::vector<PartInfo> parseParts(const nlohmann::json& arr);

// Parse the kernel's fit report ({ok, summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}).
FitReport parseFit(const nlohmann::json& f);

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

PlanInfo parsePlan(const nlohmann::json& p);

// ---------------- small ui helpers ----------------
// A caption button (minimize / maximize-restore / close) drawn with a crisp vector glyph
// and a full-height hover fill — close hovers red, the others a soft white wash.
enum WinBtn { BTN_MIN, BTN_MAX, BTN_CLOSE };
bool windowButton(const char* id, WinBtn kind, float h, bool maximized);

bool primaryButton(const char* label, const ImVec2& size);
void sectionHeader(ImFont* f, const char* text);
void dimText(const char* t);

void buildDefaultLayout(ImGuiID root, const ImVec2& size);

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

void freeChatTextures(std::vector<ChatMsg>& chat);

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active);

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH);

// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH);

// Dim, wrapped prose (reasoning / notes) in a scrolling box.
void drawProseBox(const char* id, const std::string& text, float maxH);

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed);

// The build-plan card: an accent-edged panel listing each planned part.
void drawPlanCard(const PlanInfo& plan, const ui::Fonts& fonts);

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts);

// ---------------- app-state aggregate ----------------
// Every long-lived run_gui() local, bundled so the title-bar / panels / modals (each in its own
// translation unit) can share one mutable state object. Field declaration order and defaults match
// the monolith's local declarations exactly. The four run_gui lambdas (addLog / startSetup /
// tryStartWorker / showPartMesh) become methods carrying the same captured state.
struct AppState {
    // ---- window / render context (initialized in run_gui before the loop) ----
    GLFWwindow* win = nullptr;
    bool shot = false;
    float scale = 1.0f;
    std::string assets;
    ui::Fonts fonts;
    Tex wordmark, logoIcon;
    Renderer* renderer = nullptr;     // owned by run_gui; AppState only borrows it
    chrome::HitLayout* hitLayout = nullptr;

    // ---- settings + system-prompt editor buffer ----
    Settings settings;
    std::vector<char> spBuf{std::vector<char>(16384, 0)};

    // ---- console log ----
    std::vector<std::string> log;

    // ---- runtime + worker ----
    WorkerClient worker;
    std::string werr;
    bool workerStarted = false;
    bool rtReady = false;
    std::atomic<bool> setupRunning{false}, setupDone{false}, setupOk{false};
    std::mutex setupMtx;
    std::string setupMsg;
    std::string status;
    int statusKind = 0;  // 0 = ok (teal), 1 = build issue (amber), 2 = system error (red)
    bool autoSetupTried = false;    // guard so startup auto-heal runs once, not in a loop
    bool importRepairTried = false; // guard so per-job ModuleNotFoundError repair runs once

    // ---- job + chat model ----
    JobState job;
    std::vector<char> code{std::vector<char>(64 * 1024, 0)};
    std::vector<char> prompt{std::vector<char>(2048, 0)};
    std::vector<ChatMsg> chat;

    // ---- current assembly (populated when a build/design returns multiple parts) ----
    std::vector<PartInfo> asmParts;
    FitReport asmFit;
    std::string asmStl;               // combined assembly STL (shown when selectedPart == -1)
    int selectedPart = -1;            // -1 = whole assembly; >=0 = index into asmParts

    std::vector<char> chatInput{std::vector<char>(2048, 0)};
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

    // ---- screenshot-mode flags (set from AI_CAD_SHOT in run_gui) ----
    bool wantSettings = false, wantChat = false, wantAssembly = false, wantPlan = false;

    // ---- the run_gui lambdas, now methods ----
    void addLog(const std::string& s);
    void startSetup(bool force);
    void tryStartWorker();
    void showPartMesh(int sel);
};
