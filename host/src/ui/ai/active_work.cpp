// See active_work.h. Live doc-activity grid: per-doc shaded previews rendered from the parsed
// mesh each DocSession already holds (DocSession::cachedDocMesh, prewarmed on every pushed
// agent revision by engine_events.cpp), truthful per-doc revision activity, and an Idle list.
//
// Preview pipeline (GL thread, all plain statics):
//   draw  -> resolvePreview() registers a Want {docId, source key, mesh} for each visible card
//   pump  -> renders at most ONE want per frame through render::renderDocThumbnail, or, when a
//            doc's AICM path has sat unparsed past a grace period (the app's own prewarm usually
//            lands first), starts one background loadAicmFile so the card can show the mesh or
//            the parse failure. Textures are keyed by docId and re-rendered only when the
//            source key (revision + AICM path) changes; closed docs free theirs, LRU caps the rest.
#include "active_work.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <deque>
#include <filesystem>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui_internal.h"   // ImRect

#include "core/app_state.h"
#include "render/aicm_loader.h"
#include "render/doc_thumbnail.h"
#include "ui_theme.h"      // ui::palette / uiScale / adaptivePanel / glassPanel / glassCardWash
#include "ui/anim.h"
#include "ui/metrics.h"    // cpMenu* rhythm tokens (gutter / row pad / gap / corner)
#include "ui/copilot/copilot_internal.h" // convProjectDir, project path normalization
#include "ai_prefs.h"
#include "ui/plm/graph_thumbs.h"
#include "ui/widgets/kit.h"

namespace ui {

bool agentDocInProject(const std::string& path, const std::string& originProjectDir,
                       const std::string& activeProjectDir) {
    const std::string root = convProjectKey(activeProjectDir);
    if (root.empty()) return false;
    if (path.empty()) return !originProjectDir.empty() &&
                             convProjectKey(originProjectDir) == root;
    std::filesystem::path file = lexicalPath(path);
    if (!file.is_absolute())
        file = (lexicalPath(activeProjectDir) / file).lexically_normal();
    const std::string key = comparablePath(file);
    return key == root || (key.size() > root.size() &&
        key.compare(0, root.size(), root) == 0 &&
        (root.back() == '/' || key[root.size()] == '/'));
}

namespace {

using Clock = std::chrono::steady_clock;

// glassPanel / glassCardWash take a LOGICAL dp radius (they multiply by uiScale themselves),
// so hand them the shared cpMenu corner converted back to dp — one source of truth for the
// corner without double-scaling it.
inline float rowRoundDp()  { return ui::metrics::cpMenuRowRound()  / ui::uiScale; }
inline float chipRoundDp() { return ui::metrics::cpMenuChipRound() / ui::uiScale; }

// Arc tone for a "share of a budget" ring — the SAME ladder the Copilot composer's
// context-window meter uses (ui::kUsageWarnFrac / kUsageCritFrac, the chud-ui-shared-v1
// usage thresholds) so one colour means one thing.
inline ImU32 fracArcCol(float frac) {
    return frac < kUsageWarnFrac ? Palette::toU32(palette.statusOk)
         : frac < kUsageCritFrac ? Palette::toU32(palette.statusWarn)
                                 : Palette::toU32(palette.statusErr);
}

inline ImVec4 role(const float c[4], float a) { return ImVec4(c[0], c[1], c[2], a); }

// A doc counts as in flight while it pulses, was touched within this window, or was touched
// during the project's running agent turn. Everything else agent-touched is Idle.
constexpr double kRecentS = 300.0;
// How long a doc's AICM may sit unparsed (the app's prewarm normally lands well inside this)
// before Active Work parses it itself to learn the mesh or the failure reason.
constexpr double kParseGraceS = 1.5;
constexpr size_t kMaxThumbs = 48;     // GPU preview textures kept (LRU beyond this)
constexpr size_t kMaxEdits = 256;     // per-doc revision log entries kept

// ---------------------------------------------------------------- tool-call ticker feed

// One observed agent operation (from agent.tool_call). `target` is the human-readable
// "what it acts on" string the orchestrator emits (doc id / part name / path fragment).
struct AwOp {
    std::string       tool;
    std::string       target;
    bool              ok = true;
    int               latencyMs = -1;
    long long         seq = 0;
    Clock::time_point at{};
};

struct AwStore {
    std::string      runId;
    std::string      projectDir;  // resolved from the owning conversation's run slot
    std::deque<AwOp> ops;         // current run ticker, newest at back (cap kMaxOps)
    long long        seqCtr = 0;
};
AwStore s_aw;
constexpr size_t kMaxOps = 96;   // ring cap — plenty for a run, bounded memory

// run_id -> steady time its run_started event was drained (the only start stamp that exists:
// JobState carries no start time). Bounded: cleared wholesale past a generous cap.
std::unordered_map<std::string, Clock::time_point> s_runStart;

std::string awStr(const nlohmann::json& d, const char* k) {
    auto it = d.find(k);
    return (it != d.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// Best-effort op→doc attribution: the op target names the doc by id, tree title, saved
// filename, or display title. Substring match, case-sensitive (doc ids are exact).
bool opHitsDoc(const AwOp& op, const AppState::DocTab& t) {
    if (op.target.empty()) return false;
    const std::string& s = op.target;
    if (!t.docId.empty() && s.find(t.docId) != std::string::npos) return true;
    if (!t.title.empty() && s.find(t.title) != std::string::npos) return true;
    const std::string disp = t.displayTitle();
    if (!disp.empty() && s.find(disp) != std::string::npos) return true;
    if (!t.path.empty()) {
        const size_t slash = t.path.find_last_of("/\\");
        const std::string file = (slash == std::string::npos) ? t.path : t.path.substr(slash + 1);
        if (!file.empty() && s.find(file) != std::string::npos) return true;
    }
    return false;
}

// ---------------------------------------------------------------- per-doc revision activity

// Revisions a doc advanced by, as observed from the GUI's own mirror state. A delta is the
// agent's only when engine_events stamped a new DocTab::lastAgentEditAt with it (every foreign
// doc.changed does) or the doc is an unadopted AI mirror the user cannot edit.
struct AwEdit { Clock::time_point at; int revs = 0; };
struct AwTrack {
    int                lastRev = -1;
    Clock::time_point  seenStamp{};
    Clock::time_point  lastEditAt{};
    std::deque<AwEdit> edits;
};
std::unordered_map<std::string, AwTrack> s_track;   // docId

bool isModelTab(const AppState::DocTab& t) {
    return t.kind == AppState::DocTab::TabKind::Part ||
           t.kind == AppState::DocTab::TabKind::Assembly;
}

int tabRevision(const AppState& st, int idx) {
    const AppState::DocTab& t = st.docTabs[(size_t)idx];
    if (idx == st.activeTab && !t.docId.empty() && st.docMirror.docId() == t.docId)
        return st.docMirror.revision();
    if (!t.session) return -1;
    if (t.session->cachedRevision >= 0) return t.session->cachedRevision;
    return t.session->cachedMirror.valid() ? t.session->cachedMirror.revision : -1;
}

void observeRevisions(const AppState& st) {
    const Clock::time_point now = Clock::now();
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        const AppState::DocTab& t = st.docTabs[(size_t)i];
        if (!isModelTab(t) || t.docId.empty()) continue;
        AwTrack& tr = s_track[t.docId];
        const bool stamped = t.lastAgentEditAt != Clock::time_point{} &&
                             t.lastAgentEditAt > tr.seenStamp;
        if (stamped) tr.seenStamp = t.lastAgentEditAt;
        const int rev = tabRevision(st, i);
        if (rev < 0) continue;
        if (tr.lastRev < 0 || rev < tr.lastRev) { tr.lastRev = rev; continue; }  // (re)baseline
        if (rev == tr.lastRev) continue;
        if (stamped || (t.aiHandoff && !t.userAdopted)) {
            const Clock::time_point at = stamped ? t.lastAgentEditAt : now;
            tr.edits.push_back({at, rev - tr.lastRev});
            while (tr.edits.size() > kMaxEdits) tr.edits.pop_front();
            tr.lastEditAt = std::max(tr.lastEditAt, at);
        }
        tr.lastRev = rev;
    }
}

int editsSince(const AwTrack* tr, std::optional<Clock::time_point> since) {
    if (!tr) return 0;
    int n = 0;
    for (const AwEdit& e : tr->edits)
        if (!since || e.at >= *since) n += e.revs;
    return n;
}

// The project's agent-turn window: while a turn runs, from its (earliest) start; otherwise the
// last known turn start. `known` is false when no run_started for this project was seen.
struct RunWindow {
    bool              running = false;
    bool              known = false;
    Clock::time_point since{};
};

RunWindow projectRunWindow(AppState& st, const std::string& projectKey) {
    RunWindow w;
    Clock::time_point runningMin = Clock::time_point::max(), lastStart{};
    bool haveRunning = false, haveLast = false;
    for (const auto& conv : st.conversations) {
        if (projectKey.empty() || convProjectKey(conv.projectDir) != projectKey) continue;
        const auto slot = st.runs.find(conv.id);
        if (slot == st.runs.end() || !slot->second) continue;
        const bool running = slot->second->running.load();
        std::string runId;
        {
            std::lock_guard<std::mutex> lock(slot->second->mtx);
            runId = slot->second->runId;
        }
        w.running = w.running || running;
        const auto it = s_runStart.find(runId);
        if (runId.empty() || it == s_runStart.end()) continue;
        if (running) { runningMin = std::min(runningMin, it->second); haveRunning = true; }
        if (!haveLast || it->second > lastStart) { lastStart = it->second; haveLast = true; }
    }
    if (haveRunning)      { w.known = true; w.since = runningMin; }
    else if (!w.running && haveLast) { w.known = true; w.since = lastStart; }
    return w;
}

// ---------------------------------------------------------------- live preview cache

enum class PrevState { Ready, Loading, Empty, Failed, NoGeometry, NotModel };

struct Thumb {
    unsigned int tex = 0;
    int          w = 0, h = 0;
    std::string  srcKey;          // revision + AICM path the texture shows
    uint64_t     styleHash = 0;   // viewport palette the backdrop was drawn with
    std::string  failKey, failErr;
    uint64_t     lastUsed = 0;
};
std::unordered_map<std::string, Thumb> s_thumbs;   // docId

struct Parsed {
    std::shared_ptr<const render::DocMesh> mesh;
    std::string                            err;
};
struct ParseJob {
    std::string         docId, srcKey;
    std::future<Parsed> fut;
};
std::optional<ParseJob> s_parseJob;
std::unordered_map<std::string, std::pair<std::string, Parsed>> s_parsed;   // docId -> (srcKey, result)
std::unordered_map<std::string, std::pair<std::string, Clock::time_point>> s_pendingSince;

struct Want {
    std::string                            docId, srcKey, aicm;
    std::shared_ptr<const render::DocMesh> mesh;
    int                                    w = 0, h = 0, order = 0;
};
std::vector<Want> s_wants;   // rebuilt every frame by the painters, consumed by the pump
uint64_t          s_frame = 0;

render::DocThumbStyle thumbStyle() {
    render::DocThumbStyle s;
    for (int i = 0; i < 3; ++i) {
        s.bgTop[i] = palette.viewportTop[i];
        s.bgMid[i] = palette.viewportMid[i];
        s.bgBot[i] = palette.viewportBottom[i];
        s.edge[i]  = palette.viewportEdge[i];
    }
    return s;
}

uint64_t styleHash(const render::DocThumbStyle& s) {
    uint64_t h = 1469598103934665603ull;
    auto mix = [&h](const float* v) {
        for (int i = 0; i < 3; ++i) {
            h ^= (uint64_t)(uint32_t)(v[i] * 65535.0f);
            h *= 1099511628211ull;
        }
    };
    mix(s.bgTop); mix(s.bgMid); mix(s.bgBot); mix(s.edge);
    return h;
}

std::string sourceKey(int rev, const std::string& aicm) {
    if (rev < 0 && aicm.empty()) return std::string();
    return std::to_string(rev) + '\x1f' + aicm;
}

bool sizeDrifted(const Thumb& th, int w, int h) {
    return std::abs(th.w - w) * 4 > std::max(w, 1) || std::abs(th.h - h) * 4 > std::max(h, 1);
}

struct Preview {
    PrevState    state = PrevState::NoGeometry;
    unsigned int tex = 0;
    int          w = 0, h = 0;
    bool         stale = false;     // texture shows an older revision; a newer one is loading
    bool         diskFit = false;   // saved-file worker render: letterbox, don't crop
    std::string  reason;            // Failed
};

// Pixel size a preview rect needs (framebuffer px, quantized so small layout jitter never
// re-renders).
void targetPx(ImVec2 p0, ImVec2 p1, int& w, int& h) {
    const float fb = std::max(ImGui::GetIO().DisplayFramebufferScale.x, 1.0f);
    auto q = [](float v) { return std::clamp(((int)v + 15) / 16 * 16, 64, 1024); };
    w = q((p1.x - p0.x) * fb);
    h = q((p1.y - p0.y) * fb);
}

Preview resolvePreview(AppState& st, int idx, ImVec2 p0, ImVec2 p1, int order,
                       const std::string& projectDir) {
    Preview pv;
    const AppState::DocTab& t = st.docTabs[(size_t)idx];
    if (!isModelTab(t) || t.docId.empty()) { pv.state = PrevState::NotModel; return pv; }
    if (!aiActiveWorkPreviews()) { pv.state = PrevState::NotModel; return pv; }

    int pw = 0, ph = 0;
    targetPx(p0, p1, pw, ph);
    const render::DocThumbStyle style = thumbStyle();
    const uint64_t sh = styleHash(style);

    const DocSession* s = t.session.get();
    std::shared_ptr<const render::DocMesh> mesh;
    std::string srcKey, aicm;
    if (s) {
        aicm = s->cachedAicmPath;
        srcKey = sourceKey(s->cachedRevision, aicm);
        mesh = s->cachedDocMesh;
        if (mesh && srcKey.empty()) {
            char buf[40];
            std::snprintf(buf, sizeof(buf), "mesh@%p", (const void*)mesh.get());
            srcKey = buf;
        }
    }

    Thumb* th = nullptr;
    if (auto it = s_thumbs.find(t.docId); it != s_thumbs.end()) th = &it->second;
    if (th && th->tex) {
        th->lastUsed = s_frame;
        pv.tex = th->tex;
        pv.w = th->w;
        pv.h = th->h;
    }
    const bool current = th && th->tex && !srcKey.empty() && th->srcKey == srcKey;

    if (srcKey.empty()) {
        // No revision/mesh parked. While the agent is actively writing, the first tessellation
        // is on its way -> loading. A revision pushed WITHOUT a tessellation (mates, pose
        // writers) only arrives when the tab is activated: keep the last picture drawn rather
        // than shimmer forever.
        if (t.aiPulse) {
            pv.state = PrevState::Loading;
            pv.stale = pv.tex != 0;
            return pv;
        }
        if (pv.tex) { pv.state = PrevState::Ready; return pv; }
        if (t.meshCacheInvalidated) {
            pv.state = PrevState::NoGeometry;
            pv.reason = "Open to refresh the preview";
            return pv;
        }
        if (!t.path.empty() && !projectDir.empty()) {
            const GraphGridThumb g =
                graphThumbEnsure(st, projectDir, graphThumbKey(std::string(), 0.0, t.path), t.path);
            if (g.tex) {
                pv.state = PrevState::Ready;
                pv.tex = g.tex; pv.w = g.w; pv.h = g.h;
                pv.diskFit = true;
                return pv;
            }
            if (g.pending) { pv.state = PrevState::Loading; return pv; }
        }
        pv.state = PrevState::NoGeometry;
        return pv;
    }

    // Parsed by Active Work itself (prewarm never landed): mesh or failure for this source.
    if (!mesh) {
        if (auto it = s_parsed.find(t.docId); it != s_parsed.end() && it->second.first == srcKey) {
            if (!it->second.second.err.empty()) {
                pv.state = PrevState::Failed;
                pv.reason = it->second.second.err;
                pv.stale = false;
                pv.tex = 0;
                return pv;
            }
            mesh = it->second.second.mesh;
        }
    }
    if (mesh && mesh->empty()) { pv.state = PrevState::Empty; pv.tex = 0; return pv; }
    if (th && th->failKey == srcKey && !current) {
        pv.state = PrevState::Failed;
        pv.reason = th->failErr;
        pv.tex = 0;
        return pv;
    }

    if (current) {
        pv.state = PrevState::Ready;
        if (mesh && (sizeDrifted(*th, pw, ph) || th->styleHash != sh))
            s_wants.push_back({t.docId, srcKey, aicm, mesh, pw, ph, order});
        return pv;
    }
    pv.state = PrevState::Loading;
    pv.stale = pv.tex != 0;
    if (mesh) {
        s_wants.push_back({t.docId, srcKey, aicm, mesh, pw, ph, order});
    } else if (!aicm.empty()) {
        auto& pend = s_pendingSince[t.docId];
        if (pend.first != srcKey) pend = {srcKey, Clock::now()};
        s_wants.push_back({t.docId, srcKey, aicm, nullptr, pw, ph, order});
    }
    return pv;
}

// Loading shimmer: one soft highlight band sweeping across the slot (ui::anim phase, so the
// frame loop keeps drawing only while a shimmering card is on screen).
void paintShimmer(ImDrawList* dl, ImVec2 p0, ImVec2 p1, const AdaptiveSurface& surf,
                  float strength) {
    const float w = p1.x - p0.x;
    if (w <= 0.0f) return;
    const float band = w * 0.45f;
    const float x = p0.x - band + (w + band) * ui::anim::phase(ui::anim::kBreathe * 0.6f);
    ImVec4 hi = surf.textPrimary;
    hi.w = 0.12f * strength;
    ImVec4 lo = hi;
    lo.w = 0.0f;
    const ImU32 cHi = ImGui::ColorConvertFloat4ToU32(hi);
    const ImU32 cLo = ImGui::ColorConvertFloat4ToU32(lo);
    dl->PushClipRect(p0, p1, true);
    const float mid = x + band * 0.5f;
    dl->AddRectFilledMultiColor(ImVec2(x, p0.y), ImVec2(mid, p1.y), cLo, cHi, cHi, cLo);
    dl->AddRectFilledMultiColor(ImVec2(mid, p0.y), ImVec2(x + band, p1.y), cHi, cLo, cLo, cHi);
    dl->PopClipRect();
}

const char* kindTag(const AppState::DocTab& t) {
    switch (t.kind) {
        case AppState::DocTab::TabKind::Part:      return "PART";
        case AppState::DocTab::TabKind::Assembly:  return "ASM";
        case AppState::DocTab::TabKind::Drawing:   return "DWG";
        case AppState::DocTab::TabKind::Schematic: return "SCH";
        case AppState::DocTab::TabKind::PCB:       return "PCB";
    }
    return "DOC";
}

void centeredText(ImDrawList* dl, ImVec2 p0, ImVec2 p1, float dy, const char* text, ImVec4 col) {
    const ImVec2 ts = ImGui::CalcTextSize(text);
    dl->AddText(ImVec2(p0.x + (p1.x - p0.x - ts.x) * 0.5f, p0.y + (p1.y - p0.y - ts.y) * 0.5f + dy),
                ImGui::ColorConvertFloat4ToU32(col), text);
}

void paintPreview(ImDrawList* dl, ImVec2 p0, ImVec2 p1, const Preview& pv,
                  const AppState::DocTab& t, const AdaptiveSurface& surf) {
    const float rnd = ui::metrics::cpMenuChipRound();
    if (pv.tex && !pv.diskFit) {
        // Cover-fit: crop the texture's uv to the slot aspect so a stale-size render never
        // stretches (the next render matches the slot exactly).
        const float ta = (float)std::max(pv.w, 1) / (float)std::max(pv.h, 1);
        const float ra = (p1.x - p0.x) / std::max(p1.y - p0.y, 1.0f);
        ImVec2 uv0(0, 0), uv1(1, 1);
        if (ta > ra) { const float c = 0.5f * (1.0f - ra / ta); uv0.x = c; uv1.x = 1.0f - c; }
        else         { const float c = 0.5f * (1.0f - ta / ra); uv0.y = c; uv1.y = 1.0f - c; }
        dl->AddImageRounded((ImTextureID)(intptr_t)pv.tex, p0, p1, uv0, uv1, IM_COL32_WHITE, rnd);
        if (pv.stale) paintShimmer(dl, p0, p1, surf, 0.7f);
        return;
    }
    const float acc[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
    glassCardWash(dl, p0, p1, chipRoundDp(), acc, t.aiPulse ? 0.16f : 0.09f);
    if (pv.tex) {   // saved-file worker render: letterbox inside the wash
        const float pad = ui::metrics::cpMenuRowPadY();
        const float maxW = (p1.x - p0.x) - pad * 2.0f, maxH = (p1.y - p0.y) - pad * 2.0f;
        const float sc = std::min(maxW / std::max((float)pv.w, 1.0f), maxH / std::max((float)pv.h, 1.0f));
        const float tw = pv.w * sc, thh = pv.h * sc;
        const ImVec2 ip0(p0.x + ((p1.x - p0.x) - tw) * 0.5f, p0.y + ((p1.y - p0.y) - thh) * 0.5f);
        dl->AddImage((ImTextureID)(intptr_t)pv.tex, ip0, ImVec2(ip0.x + tw, ip0.y + thh));
        return;
    }
    const float lh = ImGui::GetTextLineHeight();
    switch (pv.state) {
        case PrevState::Loading:
            paintShimmer(dl, p0, p1, surf, 1.0f);
            centeredText(dl, p0, p1, 0.0f, kindTag(t), surf.textTertiary);
            break;
        case PrevState::Empty:
            centeredText(dl, p0, p1, 0.0f, "Empty", surf.textSecondary);
            break;
        case PrevState::Failed: {
            centeredText(dl, p0, p1, -lh * 0.5f, "Preview failed", role(palette.statusErr, 1.0f));
            const float maxW = (p1.x - p0.x) - ui::metrics::cpMenuRowPadX() * 2.0f;
            const std::string why = ui::kit::elide(pv.reason.c_str(), maxW);
            centeredText(dl, p0, p1, lh * 0.6f, why.c_str(), surf.textTertiary);
            break;
        }
        case PrevState::NoGeometry:
            centeredText(dl, p0, p1, 0.0f,
                         pv.reason.empty() ? "No geometry yet" : pv.reason.c_str(),
                         surf.textTertiary);
            break;
        case PrevState::Ready:
        case PrevState::NotModel:
            centeredText(dl, p0, p1, 0.0f, kindTag(t), surf.textTertiary);
            break;
    }
}

// Domain wrapper over the shared painter: the only Active-Work-specific part left is the
// vertical push, so the state sits inside the grid area rather than at the pane's top edge.
void awEmptyState(const char* msg, const char* sub) {
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.30f));
    ui::kit::emptyState(nullptr, msg, sub);
    ui::kit::popDensity();
}

double secondsSince(Clock::time_point t, Clock::time_point now) {
    if (t == Clock::time_point{}) return std::numeric_limits<double>::infinity();
    return std::chrono::duration<double>(now - t).count();
}

std::string agoText(double secs) {
    if (!(secs < std::numeric_limits<double>::infinity())) return std::string();
    return ui::kit::fmtAgo((double)std::time(nullptr) - secs);
}

void freeThumb(Thumb& th) { render::deleteDocThumbnail(th.tex); }

// Drop cache/track state for docs no longer open, then LRU-trim the texture set.
void sweepClosed(const AppState& st) {
    std::unordered_set<std::string> open;
    for (const auto& t : st.docTabs)
        if (!t.docId.empty()) open.insert(t.docId);
    for (auto it = s_thumbs.begin(); it != s_thumbs.end();) {
        if (open.count(it->first)) { ++it; continue; }
        freeThumb(it->second);
        it = s_thumbs.erase(it);
    }
    auto prune = [&open](auto& m) {
        for (auto it = m.begin(); it != m.end();)
            it = open.count(it->first) ? std::next(it) : m.erase(it);
    };
    prune(s_track);
    prune(s_parsed);
    prune(s_pendingSince);
    while (s_thumbs.size() > kMaxThumbs) {
        auto lru = s_thumbs.begin();
        for (auto it = s_thumbs.begin(); it != s_thumbs.end(); ++it)
            if (it->second.lastUsed < lru->second.lastUsed) lru = it;
        freeThumb(lru->second);
        s_thumbs.erase(lru);
    }
}

void pumpPreviews() {
    // Collect a finished background parse.
    if (s_parseJob && s_parseJob->fut.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
        s_parsed[s_parseJob->docId] = {s_parseJob->srcKey, s_parseJob->fut.get()};
        s_parseJob.reset();
    }
    std::stable_sort(s_wants.begin(), s_wants.end(),
                     [](const Want& a, const Want& b) { return a.order < b.order; });
    std::unordered_set<std::string> seen;
    bool rendered = false;
    const Clock::time_point now = Clock::now();
    for (const Want& w : s_wants) {
        if (!seen.insert(w.docId).second) continue;
        if (w.mesh) {
            if (rendered) continue;
            Thumb& th = s_thumbs[w.docId];
            const render::DocThumbStyle style = thumbStyle();
            std::string err;
            if (render::renderDocThumbnail(*w.mesh, w.w, w.h, style, th.tex, err)) {
                th.w = w.w;
                th.h = w.h;
                th.srcKey = w.srcKey;
                th.styleHash = styleHash(style);
                th.failKey.clear();
                th.failErr.clear();
            } else {
                th.failKey = w.srcKey;
                th.failErr = err;
            }
            th.lastUsed = s_frame;
            if (auto it = s_parsed.find(w.docId); it != s_parsed.end() && it->second.first == w.srcKey)
                s_parsed.erase(it);   // texture made — the parsed copy has no further use
            rendered = true;
            continue;
        }
        if (s_parseJob || w.aicm.empty()) continue;
        const auto pend = s_pendingSince.find(w.docId);
        if (pend == s_pendingSince.end() || pend->second.first != w.srcKey ||
            secondsSince(pend->second.second, now) < kParseGraceS)
            continue;
        if (auto it = s_parsed.find(w.docId); it != s_parsed.end() && it->second.first == w.srcKey)
            continue;   // already parsed (failed, or awaiting its render)
        const std::string path = w.aicm;
        s_parseJob = ParseJob{w.docId, w.srcKey, std::async(std::launch::async, [path]() {
            Parsed p;
            auto doc = std::make_shared<render::DocMesh>();
            std::string err;
            if (render::loadAicmFile(path, *doc, err)) p.mesh = std::move(doc);
            else p.err = err.empty() ? std::string("mesh could not be read") : err;
            return p;
        })};
    }
    s_wants.clear();
}

void openDocTab(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.docTabs.size()) return;
    const AppState::DocTab::TabKind kind = st.docTabs[(size_t)idx].kind;
    st.switchToTab(idx);
    // switchToTab leaves the workspace alone when the tab is already active; the user asked to
    // SEE the doc, so land on the workspace that renders its kind.
    if (st.activeWorkspace != AppState::Workspace::AI) return;
    switch (kind) {
        case AppState::DocTab::TabKind::Drawing:
            st.activeWorkspace = AppState::Workspace::Drawing; break;
        case AppState::DocTab::TabKind::Schematic:
        case AppState::DocTab::TabKind::PCB:
            st.activeWorkspace = AppState::Workspace::Electronics; break;
        default:
            st.activeWorkspace = AppState::Workspace::Model; break;
    }
}

}  // namespace

void activeWorkFeedEvent(const std::string& kind, const nlohmann::json& data) {
    if (!data.is_object()) return;
    if (kind == "run_started" || kind == "dag.run.started") {
        s_aw.ops.clear();
        s_aw.projectDir.clear();
        s_aw.runId = awStr(data, "run_id");
        if (!s_aw.runId.empty()) {
            if (s_runStart.size() > 512) s_runStart.clear();
            s_runStart.emplace(s_aw.runId, Clock::now());
        }
        return;
    }
    if (kind == "agent.tool_call") {
        if (s_aw.runId.empty() || awStr(data, "run_id") != s_aw.runId) return;
        AwOp op;
        op.tool      = awStr(data, "tool_name");
        if (op.tool.empty()) op.tool = "tool";
        op.target    = awStr(data, "target");
        op.ok        = data.value("ok", true);
        auto lat     = data.find("latency_ms");
        op.latencyMs = (lat != data.end() && lat->is_number()) ? lat->get<int>() : -1;
        op.seq       = s_aw.seqCtr++;
        op.at        = Clock::now();
        s_aw.ops.push_back(std::move(op));
        while (s_aw.ops.size() > kMaxOps) s_aw.ops.pop_front();
    }
    // doc.* daemon events reach docTabs via engine_events.cpp — the view re-derives liveness
    // from st.docTabs every frame, so no extra feed is needed for open/close/changed.
}

void activeWorkPump(AppState& st) {
    ++s_frame;
    observeRevisions(st);
    if (s_frame % 30 == 0) sweepClosed(st);
    if (!aiActiveWorkPreviews()) { s_wants.clear(); return; }
    pumpPreviews();
    // Saved docs with no live mesh fall back to the PLM worker render of the file on disk.
    graphThumbsPump(st);
}

void activeWorkReset() {
    s_aw.ops.clear();
    s_aw.runId.clear();
    s_aw.projectDir.clear();
    s_aw.seqCtr = 0;
}

void openAgentDocTab(AppState& st, int tabIdx) { openDocTab(st, tabIdx); }

void drawAgentDocPreview(AppState& st, int tabIdx, ImVec2 p0, ImVec2 p1) {
    if (tabIdx < 0 || tabIdx >= (int)st.docTabs.size()) return;
    const ImVec2 wp = ImGui::GetWindowPos();
    const AdaptiveSurface& surf = adaptivePanel(
        panelId("ai_doc_preview_glass"),
        ImRect(wp, ImVec2(wp.x + ImGui::GetWindowSize().x, wp.y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    const Preview pv = resolvePreview(st, tabIdx, p0, p1, -1, convProjectDir(st));
    paintPreview(ImGui::GetWindowDrawList(), p0, p1, pv, st.docTabs[(size_t)tabIdx], surf);
}

void drawActiveWorkView(AppState& st) {
    const float S = ui::uiScale;
    const int   verbosity = aiUiVerbosity();
    const std::string projectDir = convProjectDir(st);
    const std::string projectKey = convProjectKey(projectDir);
    const Clock::time_point now = Clock::now();

    // One adaptive glass surface for the whole pane (idempotent per frame — same recipe as
    // chatSurf()/drawLeftPane): every card + strip fetches this cached contrast-matched tone.
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_active_work_glass"), paneRect, GlassGroup::Global);

    // ---- resolve the op feed's project (run slot -> owning conversation) ----------------
    if (!s_aw.runId.empty() && s_aw.projectDir.empty()) {
        for (const auto& conv : st.conversations) {
            const auto slot = st.runs.find(conv.id);
            if (slot == st.runs.end() || !slot->second) continue;
            std::lock_guard<std::mutex> lock(slot->second->mtx);
            if (slot->second->runId == s_aw.runId) {
                s_aw.projectDir = conv.projectDir;
                break;
            }
        }
    }
    const bool opsInProject = !s_aw.projectDir.empty() &&
                              convProjectKey(s_aw.projectDir) == projectKey;
    const RunWindow run = projectRunWindow(st, projectKey);
    const std::optional<Clock::time_point> since =
        run.known ? std::optional<Clock::time_point>(run.since) : std::nullopt;

    // ---- collect this project's agent-touched docs ---------------------------------------
    struct Card {
        int                      idx = -1;
        const AwTrack*           track = nullptr;
        std::vector<const AwOp*> ops;         // newest at back
        Clock::time_point        lastActivity{};
        int                      edits = 0;
        int                      rev = -1;
    };
    std::vector<Card> inFlight, idle;
    std::vector<const AwOp*> visibleOps;
    int visibleFailed = 0;
    {
        std::vector<Card> all;
        for (int i = 0; i < (int)st.docTabs.size(); ++i) {
            const AppState::DocTab& t = st.docTabs[(size_t)i];
            if (!agentDocInProject(t.path, t.projectDir, projectDir)) continue;
            Card c;
            c.idx = i;
            if (auto it = s_track.find(t.docId); !t.docId.empty() && it != s_track.end())
                c.track = &it->second;
            const bool touched = t.aiHandoff || t.aiPulse || t.agentDone ||
                                 t.lastAgentEditAt != Clock::time_point{} ||
                                 (c.track && !c.track->edits.empty());
            if (!touched) continue;
            c.lastActivity = t.lastAgentEditAt;
            if (c.track) c.lastActivity = std::max(c.lastActivity, c.track->lastEditAt);
            c.edits = editsSince(c.track, since);
            c.rev = isModelTab(t) ? tabRevision(st, i) : -1;
            all.push_back(std::move(c));
        }
        // Attribute only operations naming a visible project document. Unattributed events
        // have no project provenance and must never leak another run into this project.
        if (opsInProject) {
            for (const auto& op : s_aw.ops) {
                bool hit = false;
                for (auto& c : all) {
                    if (!opHitsDoc(op, st.docTabs[(size_t)c.idx])) continue;
                    c.ops.push_back(&op);
                    c.lastActivity = std::max(c.lastActivity, op.at);
                    hit = true;
                }
                if (hit) {
                    visibleOps.push_back(&op);
                    if (!op.ok) ++visibleFailed;
                }
            }
        }
        for (auto& c : all) {
            const AppState::DocTab& t = st.docTabs[(size_t)c.idx];
            const bool live = t.aiPulse || secondsSince(c.lastActivity, now) < kRecentS ||
                (run.running && run.known && c.lastActivity != Clock::time_point{} &&
                 c.lastActivity >= run.since);
            (live ? inFlight : idle).push_back(std::move(c));
        }
    }
    auto byActivity = [&st](const Card& a, const Card& b) {
        if (a.lastActivity != b.lastActivity) return a.lastActivity > b.lastActivity;
        return st.docTabs[(size_t)a.idx].displayTitle() < st.docTabs[(size_t)b.idx].displayTitle();
    };
    std::sort(inFlight.begin(), inFlight.end(), byActivity);
    std::sort(idle.begin(), idle.end(), byActivity);

    // ---- header strip ------------------------------------------------------------------
    {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::Text("%d in flight", (int)inFlight.size());
        ImGui::PopStyleColor();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (!idle.empty()) {
            ImGui::SameLine(0, ui::metrics::cpMenuSectionGap());
            ImGui::Text("%d idle", (int)idle.size());
        }
        if (run.running) {
            ImGui::SameLine(0, ui::metrics::cpMenuSectionGap());
            ImGui::TextUnformatted("agent turn running");
        }
        if (!visibleOps.empty()) {
            ImGui::SameLine(0, ui::metrics::cpMenuSectionGap());
            ImGui::Text("%d tool call%s", (int)visibleOps.size(), visibleOps.size() == 1 ? "" : "s");
        }
        ImGui::PopStyleColor();
        if (visibleFailed > 0) {
            // "How much of this run went wrong" as the app's shared circular gauge — the same
            // ring shape every compact fraction readout uses, never a bespoke mini-bar.
            ImGui::SameLine(0, ui::metrics::cpMenuSectionGap());
            const float frac = (float)visibleFailed / (float)visibleOps.size();
            ImDrawList* hdl = ImGui::GetWindowDrawList();
            const float  r   = ImGui::GetFrameHeight() * 0.34f;
            const float  lh  = ImGui::GetTextLineHeight();
            const ImVec2 rp  = ImGui::GetCursorScreenPos();
            ImVec4 track = surf.textTertiary;
            track.w = 0.35f;
            drawFractionRing(hdl, ImVec2(rp.x + r, rp.y + lh * 0.5f), r,
                             ui::metrics::stroke(2.0f), frac,
                             ImGui::ColorConvertFloat4ToU32(track), fracArcCol(frac));
            ImGui::Dummy(ImVec2(r * 2.0f, lh));
            ImGui::SameLine(0, ui::metrics::cpMenuLabelGap());
            ImGui::PushStyleColor(ImGuiCol_Text, role(palette.statusErr, 1.0f));
            ImGui::Text("%d failed", visibleFailed);
            ImGui::PopStyleColor();
        }
    }
    if (verbosity >= 2 && !visibleOps.empty()) {
        // Recent operations attributable to this project's visible documents.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        const size_t n = visibleOps.size();
        const size_t from = n > 4 ? n - 4 : 0;
        std::string line;
        for (size_t i = from; i < n; ++i) {
            const AwOp& op = *visibleOps[i];
            if (!line.empty()) line += "   ";
            line += op.tool;
            // "→" (U+2192) is outside the UI font's requested ranges and drew a literal '?'.
            if (!op.target.empty()) { line += " into "; line += op.target; }
        }
        if (line.size() > 220) line = "… " + line.substr(line.size() - 218);
        ImGui::TextWrapped("%s", line.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    if (inFlight.empty() && idle.empty()) {
        const std::string name = lexicalPath(projectDir).filename().string();
        const std::string title = "Nothing for " +
            (name.empty() ? std::string("this project") : name) + " yet";
        awEmptyState(title.c_str(), "Parts and assemblies agents edit appear here live.");
        return;
    }

    const char* sinceLabel = run.running ? " this turn" : (run.known ? " last turn" : "");
    // "rev 42 · 5 revisions this turn · 12 s ago" — only the parts that are actually known.
    auto activityLine = [&](const Card& c) {
        std::string s;
        if (c.rev >= 0) s = "rev " + std::to_string(c.rev);
        if (c.edits > 0) {
            if (!s.empty()) s += " · ";
            s += std::to_string(c.edits) + (c.edits == 1 ? " revision" : " revisions") + sinceLabel;
        }
        const std::string ago = agoText(secondsSince(c.lastActivity, now));
        if (!ago.empty()) {
            if (!s.empty()) s += " · ";
            s += "edited " + ago;
        }
        return s;
    };

    int openIdx = -1;

    // ---- in-flight grid ------------------------------------------------------------------
    if (inFlight.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted("No agent is editing a document right now.");
        ImGui::PopStyleColor();
    } else {
        const float gap      = ui::metrics::cpMenuRowGap();
        const float availW   = ImGui::GetContentRegionAvail().x;
        const float cellMin  = 250.0f * S;
        const int   cols     = std::max(1, std::min((int)inFlight.size(), (int)(availW / cellMin)));
        const float cellW    = (availW - gap * (cols - 1)) / cols;
        const float prevH    = cellW * 0.60f;
        const int   tickRows = verbosity >= 2 ? 3 : (verbosity == 1 ? 1 : 0);
        const float lineH    = ImGui::GetTextLineHeightWithSpacing();
        const float padX     = ui::metrics::cpMenuRowPadX();
        const float padY     = ui::metrics::cpMenuRowPadY();

        for (int i = 0; i < (int)inFlight.size(); ++i) {
            const Card& c = inFlight[(size_t)i];
            const AppState::DocTab& t = st.docTabs[(size_t)c.idx];
            const int rows = std::min(tickRows, (int)c.ops.size());
            const float cardH = prevH + (3.0f + (float)tickRows) * lineH + padY * 2.0f;
            if (i % cols != 0) ImGui::SameLine(0, gap);

            if (t.docId.empty()) ImGui::PushID(c.idx);
            else                 ImGui::PushID(t.docId.c_str());
            ImGui::BeginChild("##awcard", ImVec2(cellW, cardH), ImGuiChildFlags_None,
                              ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
            const ImVec2 c0 = ImGui::GetWindowPos();
            const ImVec2 c1(c0.x + cellW, c0.y + cardH);
            ImDrawList* dl = ImGui::GetWindowDrawList();

            // Whole card is the open target; hover drives the frame emphasis.
            ImGui::SetCursorScreenPos(c0);
            if (ImGui::InvisibleButton("##open", ImVec2(cellW, cardH))) openIdx = c.idx;
            const bool hovered = ImGui::IsItemHovered();
            if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            const float hov = ui::anim::toward(ImGui::GetID("##hov"), hovered ? 1.0f : 0.0f,
                                               ui::anim::kFast);

            // Glass slab + status frame.
            glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
            const bool lastFailed = !c.ops.empty() && !c.ops.back()->ok;
            ImVec4 frame = role(palette.glassBorder, 0.9f);
            float frameW = ui::metrics::stroke(1.0f);
            if (lastFailed) {
                frame = role(palette.statusErr, 0.95f);
                frameW = ui::metrics::stroke(1.6f);
            } else if (t.aiPulse) {
                const float ph = ui::anim::pulse(ui::anim::kBreathe * 0.8f);
                frame = role(palette.accent, 0.45f + 0.50f * ph);
                frameW = ui::metrics::stroke(1.4f + 0.8f * ph);
            } else if (t.agentDone) {
                frame = role(palette.iterBuilt, 0.85f);
            } else if (t.dirty) {
                frame = role(palette.statusWarn, 0.85f);
            }
            if (hov > 0.0f && !lastFailed && !t.aiPulse) {
                const ImVec4 a = role(palette.accent, 0.85f);
                frame = ImVec4(frame.x + (a.x - frame.x) * hov, frame.y + (a.y - frame.y) * hov,
                               frame.z + (a.z - frame.z) * hov, frame.w + (a.w - frame.w) * hov);
            }
            dl->AddRect(c0, c1, ImGui::ColorConvertFloat4ToU32(frame),
                        ui::metrics::cpMenuRowRound(), 0, frameW);

            // Live preview slot.
            const ImVec2 p0(c0.x + padX, c0.y + padY);
            const ImVec2 p1(c1.x - padX, c0.y + prevH - padY);
            Preview pv;
            if (ImGui::IsRectVisible(p0, p1)) {
                pv = resolvePreview(st, c.idx, p0, p1, i, projectDir);
                paintPreview(dl, p0, p1, pv, t, surf);
            }
            if (hovered) {
                if (pv.state == PrevState::Failed)
                    ImGui::SetTooltip("Open %s\nPreview failed: %s", t.displayTitle().c_str(),
                                      pv.reason.c_str());
                else
                    ImGui::SetTooltip("Open %s", t.displayTitle().c_str());
            }

            // Title + kind.
            const float textX = c0.x + padX;
            const float textW = cellW - padX * 2.0f;
            float y = c0.y + prevH + ui::metrics::stroke(2.0f);
            ImGui::SetCursorScreenPos(ImVec2(textX, y));
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            const float tagW = ImGui::CalcTextSize(kindTag(t)).x + ui::metrics::cpMenuRowGap();
            ImGui::TextUnformatted(ui::kit::elide(t.displayTitle().c_str(), textW - tagW).c_str());
            ImGui::PopStyleColor();
            ImGui::SameLine(0, ui::metrics::cpMenuRowGap());
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted(kindTag(t));
            ImGui::PopStyleColor();

            // Status line.
            y += lineH;
            ImGui::SetCursorScreenPos(ImVec2(textX, y));
            const char* status = t.aiPulse     ? "agent editing…"
                               : t.agentDone   ? "agent done"
                               : t.dirty       ? "unsaved"
                               : t.aiHandoff   ? "agent mirror"
                                               : "open";
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  t.aiPulse ? role(palette.accent, 1.0f) : surf.textSecondary);
            if (c.ops.empty()) {
                ImGui::TextUnformatted(status);
            } else {
                ImGui::Text("%s · %d tool call%s", status, (int)c.ops.size(),
                            c.ops.size() == 1 ? "" : "s");
            }
            ImGui::PopStyleColor();

            // Activity line (revision, revisions this turn, last edit).
            y += lineH;
            const std::string act = activityLine(c);
            if (!act.empty()) {
                ImGui::SetCursorScreenPos(ImVec2(textX, y));
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                ImGui::TextUnformatted(ui::kit::elide(act.c_str(), textW).c_str());
                ImGui::PopStyleColor();
            }

            // Operation ticker (what the agent is doing along the way), newest first.
            for (int r = 0; r < rows; ++r) {
                const AwOp& op = *c.ops[c.ops.size() - 1 - (size_t)r];
                y += lineH;
                const ImVec4 dot = op.ok ? role(palette.accent, 1.0f) : role(palette.statusErr, 1.0f);
                dl->AddCircleFilled(ImVec2(textX + 3 * S, y + lineH * 0.5f), 2.4f * S,
                                    ImGui::ColorConvertFloat4ToU32(dot));
                ImGui::SetCursorScreenPos(ImVec2(textX + 10 * S, y));
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                std::string line = op.tool;
                if (op.latencyMs >= 0) {
                    char buf[32];
                    std::snprintf(buf, sizeof(buf), " · %.1fs", op.latencyMs / 1000.0);
                    line += buf;
                }
                ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
            }

            ImGui::EndChild();
            ImGui::PopID();
        }
    }

    // ---- idle list -------------------------------------------------------------------------
    if (!idle.empty()) {
        static bool idleOpen = false;
        const std::string title = "Idle · " + std::to_string(idle.size());
        ui::kit::gapRow();
        if (ui::kit::cardBegin("##aw_idle", title.c_str(), nullptr, &idleOpen)) {
            const float thumbPx = ImGui::GetTextLineHeight() * 2.0f;
            for (int i = 0; i < (int)idle.size(); ++i) {
                const Card& c = idle[(size_t)i];
                const AppState::DocTab& t = st.docTabs[(size_t)c.idx];
                // Square request for the row's leading slot; renders only while listed.
                const ImVec2 cur = ImGui::GetCursorScreenPos();
                Preview pv;
                if (ImGui::IsRectVisible(cur, ImVec2(cur.x + thumbPx, cur.y + thumbPx)))
                    pv = resolvePreview(st, c.idx, cur, ImVec2(cur.x + thumbPx, cur.y + thumbPx),
                                        1000 + i, projectDir);
                const std::string act = activityLine(c);
                ui::kit::RowStyle rs;
                // kit rows draw the thumb into a square slot with full uv: only a square-ish
                // render (the one requested just above, once it lands) avoids a stretch.
                const bool squareish = std::abs(pv.w - pv.h) * 4 <= std::max(pv.w, pv.h);
                rs.thumb = (pv.tex && !pv.stale && squareish) ? (ImTextureID)(intptr_t)pv.tex : 0;
                rs.secondary = act.empty() ? nullptr : act.c_str();
                rs.meta = kindTag(t);
                rs.tooltip = "Open this document";
                ImGui::PushID(c.idx);
                if (ui::kit::row("##idle", t.displayTitle().c_str(), rs)) openIdx = c.idx;
                ImGui::PopID();
            }
        }
        ui::kit::cardEnd();
    }

    // Deferred: switchToTab mutates docTabs state the cards above still reference.
    if (openIdx >= 0) openDocTab(st, openIdx);
}

}  // namespace ui
