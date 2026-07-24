// See active_work.h. Slice 1: doc-activity grid + operation tickers + status frames.
// Iso-preview textures reuse the PLM Graph worker doc-render cache (ui/plm/graph_thumbs):
// DocTab carries no content hash, so the cache key falls back to path+mtime — still one
// cache entry per saved revision. Unsaved daemon WIP (no saved path) has nothing to key
// off and always keeps the lightweight glass placeholder.
#include "active_work.h"

#include <algorithm>
#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include "core/app_state.h"
#include "ui_theme.h"      // ui::palette / uiScale / adaptivePanel / glassPanel / glassRowBackground
#include "ui/metrics.h"    // cpMenu* rhythm tokens (gutter / row pad / gap / corner)
#include "ai_prefs.h"
#include "ui/plm/graph_thumbs.h"

namespace ui {
namespace {

// glassPanel / glassCardWash take a LOGICAL dp radius (they multiply by uiScale themselves),
// so hand them the shared cpMenu corner converted back to dp — one source of truth for the
// corner without double-scaling it.
inline float rowRoundDp()  { return ui::metrics::cpMenuRowRound()  / ui::uiScale; }
inline float chipRoundDp() { return ui::metrics::cpMenuChipRound() / ui::uiScale; }

// Arc tone for a "share of a budget" ring — the SAME ladder the Copilot composer's
// context-window meter uses (<50% ok, <85% warn, else err) so one colour means one thing.
inline ImU32 fracArcCol(float frac) {
    return frac < 0.50f ? Palette::toU32(palette.statusOk)
         : frac < 0.85f ? Palette::toU32(palette.statusWarn)
                        : Palette::toU32(palette.statusErr);
}

// One observed agent operation (from agent.tool_call). `target` is the human-readable
// "what it acts on" string the orchestrator emits (doc id / part name / path fragment).
struct AwOp {
    std::string tool;
    std::string target;
    std::string actor;
    bool        ok = true;
    int         latencyMs = -1;
    long long   seq = 0;
};

struct AwStore {
    std::string      runId;
    std::deque<AwOp> ops;        // global run ticker, newest at back (cap kMaxOps)
    long long        seqCtr = 0;
    int              failed = 0;
};
static AwStore s_aw;

constexpr size_t kMaxOps = 96;   // ring cap — plenty for a run, bounded memory

std::string awStr(const nlohmann::json& d, const char* k) {
    auto it = d.find(k);
    return (it != d.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// A doc tab participates in Active Work when an agent owns/pulses/finished it, or when an
// observed op targets it (matched below).
bool docIsAgentWip(const AppState::DocTab& t) {
    return t.aiHandoff || t.aiPulse || t.agentDone;
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

void awEmptyState(const char* msg, const char* sub) {
    const float S = ui::uiScale;
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.30f));
    const float tw = ImGui::CalcTextSize(msg).x;
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (ImGui::GetContentRegionAvail().x - tw) * 0.5f);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1], palette.textDisabled[2], 0.55f));
    ImGui::TextUnformatted(msg);
    ImGui::PopStyleColor();
    if (sub) {
        const float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             std::max(0.0f, (ImGui::GetContentRegionAvail().x - sw) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.75f));
        ImGui::TextWrapped("%s", sub);
        ImGui::PopStyleColor();
    }
    (void)S;
}

}  // namespace

void activeWorkFeedEvent(const std::string& kind, const nlohmann::json& data) {
    if (!data.is_object()) return;
    if (kind == "run_started" || kind == "dag.run.started") {
        s_aw.ops.clear();
        s_aw.failed = 0;
        s_aw.runId  = awStr(data, "run_id");
        return;
    }
    if (kind == "agent.tool_call") {
        AwOp op;
        op.tool      = awStr(data, "tool_name");
        if (op.tool.empty()) op.tool = "tool";
        op.target    = awStr(data, "target");
        op.actor     = awStr(data, "actor");
        op.ok        = data.value("ok", true);
        auto lat     = data.find("latency_ms");
        op.latencyMs = (lat != data.end() && lat->is_number()) ? lat->get<int>() : -1;
        op.seq       = s_aw.seqCtr++;
        s_aw.ops.push_back(std::move(op));
        if (!s_aw.ops.back().ok) ++s_aw.failed;
        while (s_aw.ops.size() > kMaxOps) {
            if (!s_aw.ops.front().ok) --s_aw.failed;
            s_aw.ops.pop_front();
        }
    }
    // doc.* daemon events reach docTabs via engine_events.cpp — the view re-derives liveness
    // from st.docTabs every frame, so no extra feed is needed for open/close/changed.
}

void activeWorkPump(AppState& st) {
    // Drive at most one queued worker doc_render_png job per frame for cards missing a
    // texture (see ui/plm/graph_thumbs.h) — gated on the user pref so previews can be
    // switched off entirely without touching the render queue.
    if (!aiActiveWorkPreviews()) return;
    graphThumbsPump(st);
}

void activeWorkReset() {
    s_aw.ops.clear();
    s_aw.runId.clear();
    s_aw.failed = 0;
    s_aw.seqCtr = 0;
}

void drawActiveWorkView(AppState& st) {
    const float S = ui::uiScale;
    const int   verbosity = aiUiVerbosity();
    // Iso-preview textures (THUMB SEAM below) share the project dir the PLM Graph worker
    // cache keys thumbnails under — resolved the same way graph_tab.cpp tracks G.projectDir.
    const bool        previewsOn = aiActiveWorkPreviews();
    const std::string projectDir = previewsOn ? st.plmProjectPath() : std::string();

    // One adaptive glass surface for the whole pane (idempotent per frame — same recipe as
    // chatSurf()/drawLeftPane): every card + strip fetches this cached contrast-matched tone.
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_active_work_glass"), paneRect, GlassGroup::Global);

    // ---- collect the WIP doc set (live from docTabs) + attribute ops -------------------
    struct Card {
        const AppState::DocTab* tab = nullptr;
        std::vector<const AwOp*> ops;   // newest at back
        long long lastSeq = -1;
        bool       lastFailed = false;
    };
    std::vector<Card> cards;
    for (const auto& t : st.docTabs) {
        if (!docIsAgentWip(t)) continue;
        Card c;
        c.tab = &t;
        cards.push_back(std::move(c));
    }
    // Attribute ops; a doc with ops but no live tab flag still gets a card (agent touched a
    // background doc the poller hasn't flagged yet).
    for (const auto& op : s_aw.ops) {
        bool hit = false;
        for (auto& c : cards) {
            if (!opHitsDoc(op, *c.tab)) continue;
            c.ops.push_back(&op);
            c.lastSeq = std::max(c.lastSeq, op.seq);
            hit = true;
        }
        if (!hit && op.ok) { /* unattributed (non-doc target like a sketch/param) — global ticker only */ }
    }
    // Sort: actively-pulsing first, then most recent op, then title.
    std::sort(cards.begin(), cards.end(), [](const Card& a, const Card& b) {
        if (a.tab->aiPulse != b.tab->aiPulse) return a.tab->aiPulse > b.tab->aiPulse;
        if (a.lastSeq != b.lastSeq) return a.lastSeq > b.lastSeq;
        return a.tab->displayTitle() < b.tab->displayTitle();
    });

    // ---- header strip ------------------------------------------------------------------
    {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::Text("%d doc%s in flight", (int)cards.size(), cards.size() == 1 ? "" : "s");
        ImGui::PopStyleColor();
        ImGui::SameLine(0, 12 * S);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::Text("%d ops this run", (int)s_aw.ops.size());
        ImGui::PopStyleColor();
        if (s_aw.failed > 0) {
            // "How much of this run went wrong" as the app's shared circular gauge — the same
            // ring shape every compact fraction readout uses, never a bespoke mini-bar.
            ImGui::SameLine(0, ui::metrics::cpMenuSectionGap());
            const float frac = s_aw.ops.empty()
                ? 0.0f
                : (float)s_aw.failed / (float)s_aw.ops.size();
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
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f));
            ImGui::Text("%d failed", s_aw.failed);
            ImGui::PopStyleColor();
        }
    }
    if (verbosity >= 2 && !s_aw.ops.empty()) {
        // Global run ticker: the last few operations across ALL docs, newest last.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        const size_t n = s_aw.ops.size();
        const size_t from = n > 4 ? n - 4 : 0;
        std::string line;
        for (size_t i = from; i < n; ++i) {
            const AwOp& op = s_aw.ops[i];
            if (!line.empty()) line += "   ";
            line += op.tool;
            if (!op.target.empty()) { line += " → "; line += op.target; }
        }
        if (line.size() > 220) line = "… " + line.substr(line.size() - 218);
        ImGui::TextWrapped("%s", line.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    if (cards.empty()) {
        awEmptyState("No active agent work",
                     "Parts and assemblies agents open or edit appear here live — with the "
                     "operations they make along the way. Start an Agent run to see it.");
        return;
    }

    // ---- auto-scaling card grid ---------------------------------------------------------
    const float gap     = ui::metrics::cpMenuRowGap();
    const float availW  = ImGui::GetContentRegionAvail().x;
    const float cellMin = 250.0f * S;
    const int   cols    = std::max(1, std::min((int)cards.size(), (int)(availW / cellMin)));
    const float cellW   = (availW - gap * (cols - 1)) / cols;
    const float prevH   = cellW * 0.60f;
    const int   tickRows = verbosity >= 2 ? 3 : (verbosity == 1 ? 1 : 0);
    const float lineH   = ImGui::GetTextLineHeightWithSpacing();
    const float cardH   = prevH + (2.0f + (float)tickRows) * lineH + 18.0f * S;

    const double tsec = ImGui::GetTime();
    for (int i = 0; i < (int)cards.size(); ++i) {
        const Card& c = cards[i];
        if (i % cols != 0) ImGui::SameLine(0, gap);

        ImGui::PushID(i);
        ImGui::BeginChild("##awcard", ImVec2(cellW, cardH), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + cellW, c0.y + cardH);
        ImDrawList* dl = ImGui::GetWindowDrawList();

        // Glass slab + status frame.
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
        const bool anyFail = !c.ops.empty() && !c.ops.back()->ok;
        ImVec4 frame(palette.glassBorder[0], palette.glassBorder[1], palette.glassBorder[2], 0.9f);
        float frameW = 1.0f * S;
        if (anyFail) {
            frame = ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.95f);
            frameW = 1.6f * S;
        } else if (c.tab->aiPulse) {
            const float ph = 0.5f + 0.5f * (float)std::sin(tsec * 3.2f);
            frame = ImVec4(palette.accent[0], palette.accent[1], palette.accent[2],
                           0.45f + 0.50f * ph);
            frameW = (1.4f + 0.8f * ph) * S;
        } else if (c.tab->agentDone) {
            frame = ImVec4(palette.iterBuilt[0], palette.iterBuilt[1], palette.iterBuilt[2], 0.85f);
        } else if (c.tab->dirty) {
            frame = ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 0.85f);
        }
        dl->AddRect(c0, c1, ImGui::ColorConvertFloat4ToU32(frame),
                    ui::metrics::cpMenuRowRound(), 0, frameW);

        // Preview area: worker-rendered iso thumbnail when one is cached/available (THUMB
        // SEAM), else the lightweight kind-tag placeholder. Docs with no saved path (unsaved
        // daemon WIP) never have anything to render from and always keep the placeholder.
        {
            const float padX = ui::metrics::cpMenuRowPadX();
            const float padY = ui::metrics::cpMenuRowPadY();
            const ImVec2 p0(c0.x + padX, c0.y + padY);
            const ImVec2 p1(c1.x - padX, c0.y + prevH - padY);
            GraphGridThumb gth;
            if (previewsOn && !c.tab->path.empty() && !projectDir.empty()) {
                // DocTab carries no content hash (that's a cad_graph concept) — the key falls
                // back to path+mtime, still one cache entry per saved revision.
                const std::string key = graphThumbKey(std::string(), 0.0, c.tab->path);
                gth = graphThumbEnsure(st, projectDir, key, c.tab->path);
            }
            if (gth.tex != 0) {
                const float pad  = 4.0f * S;
                const float maxW = (p1.x - p0.x) - pad * 2.0f;
                const float maxH = (p1.y - p0.y) - pad * 2.0f;
                float tw = (float)gth.w, thh = (float)gth.h;
                const float sc = std::min(maxW / std::max(tw, 1.0f), maxH / std::max(thh, 1.0f));
                tw *= sc; thh *= sc;
                const ImVec2 ip0(p0.x + ((p1.x - p0.x) - tw) * 0.5f,
                                 p0.y + ((p1.y - p0.y) - thh) * 0.5f);
                dl->AddImage((ImTextureID)(intptr_t)gth.tex, ip0, ImVec2(ip0.x + tw, ip0.y + thh));
            } else {
                // Liquid-glass gradient placeholder (accent wash, top-bright -> bottom-faint)
                // with the doc kind tag centered — reads as a preview slot, not a flat box.
                const float acc[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
                glassCardWash(dl, p0, p1, chipRoundDp(), acc, c.tab->aiPulse ? 0.16f : 0.09f);
                const char* tag = kindTag(*c.tab);
                const ImVec2 ts = ImGui::CalcTextSize(tag);
                dl->AddText(ImVec2(p0.x + (p1.x - p0.x - ts.x) * 0.5f,
                                   p0.y + (p1.y - p0.y - ts.y) * 0.5f),
                            ImGui::ColorConvertFloat4ToU32(surf.textTertiary), tag);
            }
        }

        // Title + kind.
        const float textX = c0.x + ui::metrics::cpMenuRowPadX();
        float y = c0.y + prevH + 2 * S;
        ImGui::SetCursorScreenPos(ImVec2(textX, y));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted(c.tab->displayTitle().c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine(0, ui::metrics::cpMenuRowGap());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(kindTag(*c.tab));
        ImGui::PopStyleColor();

        // Status line.
        y += lineH;
        ImGui::SetCursorScreenPos(ImVec2(textX, y));
        const char* status = c.tab->aiPulse ? "agent editing…"
                           : c.tab->agentDone ? "agent done"
                           : c.tab->dirty    ? "modified"
                                             : "open";
        ImVec4 stCol = c.tab->aiPulse
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f)
            : surf.textSecondary;
        ImGui::PushStyleColor(ImGuiCol_Text, stCol);
        ImGui::Text("%s · %d op%s", status, (int)c.ops.size(), c.ops.size() == 1 ? "" : "s");
        ImGui::PopStyleColor();

        // Operation ticker (what the agent is doing along the way), newest last.
        for (int r = 0; r < tickRows && r < (int)c.ops.size(); ++r) {
            const AwOp& op = *c.ops[c.ops.size() - 1 - r];
            y += lineH;
            ImGui::SetCursorScreenPos(ImVec2(textX, y));
            const ImVec4 dot = op.ok
                ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f)
                : ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f);
            const float cy = y + lineH * 0.5f;
            dl->AddCircleFilled(ImVec2(textX + 3 * S, cy), 2.4f * S,
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

}  // namespace ui
