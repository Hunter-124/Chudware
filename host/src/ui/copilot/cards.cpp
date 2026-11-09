// Copilot cards: the Plan/Objectives section (tiles + todos
// tree + subagents, plus the drawPlanSection adapter declared in
// core/app_state.h), the plan-approve card, the
// exec-mode cards (swarm waves / eng-firm bids+award+team+comms /
// advisor / sub-run), and the AI-VC (agent version control) checkpoint rollback. Event folding
// for these cards lives in feed.cpp; the per-turn timeline that hosts them lives in
// activity_timeline.cpp.

#include "copilot_internal.h"
#include "subrun_usage.h"
#include <algorithm>
#include <stdexcept>


// Card bodies are immutable between row revisions. Keep parsed payloads bounded and share
// them between the painter and its lazy copy action, including malformed JSON.
static const nlohmann::json& cardDetailJson(const ActivityItem& a) {
    struct Entry {
        std::string detail;
        nlohmann::json parsed;
        size_t hash = 0;
        int frame = 0;
        bool ready = false;
    };
    static std::unordered_map<ImGuiID, Entry> entries;
    const ImGuiID id = ImGui::GetID("##card_detail_json");
    const size_t hash = std::hash<std::string>{}(a.detail);
    auto it = entries.find(id);
    if (it == entries.end()) {
        while (entries.size() >= 256) {
            auto oldest = entries.begin();
            for (auto p = entries.begin(); p != entries.end(); ++p)
                if (p->second.frame < oldest->second.frame) oldest = p;
            entries.erase(oldest);
        }
        it = entries.emplace(id, Entry{}).first;
    }
    Entry& e = it->second;
    e.frame = ImGui::GetFrameCount();
    if (!e.ready || e.hash != hash || e.detail.size() != a.detail.size() || e.detail != a.detail) {
        e.detail = a.detail;
        e.hash = hash;
        e.parsed = nlohmann::json::parse(a.detail, nullptr, false);
        e.ready = true;
    }
    if (e.parsed.is_discarded())
        throw std::runtime_error("Invalid activity card JSON");
    return e.parsed;
}
// ============================ Plan / Objectives section ============================
// Multi-shape plan display (build_plan | checklist | phased_todo | objectives | dag).
// Tiles show brief titles; full purpose/dims/assignment lives in ImGui hover tooltips.
// Todos tree rows use the shared vector status glyph (drawPlanStatusGlyph); subagents list is
// omitted when empty.

PlanState parsePlanState(const nlohmann::json& p) {
    PlanState ps;
    if (!p.is_object()) return ps;
    ps.present = true;
    ps.fromEvent = true;
    ps.shape = jstr(p, "shape");
    if (ps.shape.empty()) ps.shape = "checklist";
    ps.title = jstr(p, "title");
    if (ps.title.empty()) ps.title = jstr(p, "name");
    ps.summary = jstr(p, "summary");
    if (p.contains("revision") && p["revision"].is_number())
        ps.revision = (int)p["revision"].get<double>();
    else
        ps.revision = jint(p, "revision");
    auto parseItems = [&](const nlohmann::json& arr) {
        if (!arr.is_array()) return;
        for (const auto& q : arr) {
            if (!q.is_object()) continue;
            PlanItem it;
            it.id = jstr(q, "id");
            it.title = jstr(q, "title");
            if (it.title.empty()) it.title = jstr(q, "name");
            it.desc = jstr(q, "desc");
            if (it.desc.empty()) it.desc = jstr(q, "description");
            it.status = jstr(q, "status");
            if (it.status.empty()) it.status = "pending";
            it.kind = jstr(q, "kind");
            if (it.kind.empty()) it.kind = "step";
            it.phase = jstr(q, "phase");
            it.parent_id = jstr(q, "parent_id");
            if (it.id.empty())
                it.id = it.kind + ":" + std::to_string(ps.items.size()) + ":" + it.title;
            // Optional meta: fold purpose/dims/features into tooltip desc.
            if (q.contains("meta") && q["meta"].is_object()) {
                const auto& meta = q["meta"];
                std::string extra;
                std::string purpose = jstr(meta, "purpose");
                std::string dims = jstr(meta, "dims");
                if (dims.empty()) dims = jstr(meta, "dimensions");
                if (!purpose.empty() && it.desc.find(purpose) == std::string::npos) {
                    if (!extra.empty()) extra += "\n";
                    extra += purpose;
                }
                if (!dims.empty()) {
                    if (!extra.empty()) extra += "\n";
                    extra += dims;
                    if (dims.find("mm") == std::string::npos) extra += " mm";
                }
                if (meta.contains("features") && meta["features"].is_array()) {
                    for (const auto& f : meta["features"]) {
                        if (!f.is_string()) continue;
                        if (!extra.empty()) extra += "\n• ";
                        else extra += "• ";
                        extra += f.get<std::string>();
                    }
                }
                if (!extra.empty()) {
                    if (it.desc.empty()) it.desc = extra;
                    else if (it.desc.find(extra) == std::string::npos) {
                        it.desc += "\n";
                        it.desc += extra;
                    }
                }
            }
            ps.items.push_back(std::move(it));
        }
    };
    if (auto it = p.find("items"); it != p.end()) parseItems(*it);
    if (ps.items.empty()) {
        if (auto it = p.find("tiles"); it != p.end()) parseItems(*it);
        if (auto it = p.find("todos"); it != p.end()) parseItems(*it);
    }
    if (auto it = p.find("edges"); it != p.end() && it->is_array()) {
        for (const auto& e : *it) {
            if (!e.is_object()) continue;
            PlanEdge edge;
            edge.from = jstr(e, "from");
            edge.to = jstr(e, "to");
            if (!edge.from.empty() || !edge.to.empty())
                ps.edges.push_back(std::move(edge));
        }
    }
    if (ps.title.empty()) {
        if (ps.shape == "objectives") ps.title = "Objectives";
        else if (ps.shape == "dag") ps.title = "Plan graph";
        else if (ps.shape == "build_plan") ps.title = "Build plan";
        else ps.title = "Plan";
    }
    return ps;
}

PlanState planStateFromPlanInfo(const PlanInfo& plan) {
    PlanState ps;
    if (!plan.present) return ps;
    ps.present = true;
    ps.fromEvent = false;
    ps.shape = "build_plan";
    ps.title = plan.name.empty() ? "Build plan" : plan.name;
    ps.summary = plan.summary;
    ps.revision = 0;
    for (size_t i = 0; i < plan.parts.size(); ++i) {
        const PlanPart& p = plan.parts[i];
        PlanItem it;
        // id = bare part name, phase omitted, so multi-writer upsert-by-id REPLACES the
        // plan_state items instead of appending duplicate rows under a second phase (the
        // "I. Tasks" + "II. Parts" bug).
        it.id = !p.name.empty() ? p.name : ("part:" + std::to_string(i));
        it.title = p.name.empty() ? ("Part " + std::to_string(i + 1)) : p.name;
        it.kind = "part";
        // Leave status empty (activity heuristic owns live status — a stale
        // "pending" upsert would clobber in-progress rows) and phase empty (single group).
        std::string desc;
        if (!p.purpose.empty()) desc = p.purpose;
        if (!p.dims.empty()) {
            if (!desc.empty()) desc += "\n";
            desc += p.dims;
            if (p.dims.find("mm") == std::string::npos) desc += " mm";
        }
        for (const auto& f : p.features) {
            if (!desc.empty()) desc += "\n• ";
            else desc += "• ";
            desc += f;
        }
        it.desc = std::move(desc);
        ps.items.push_back(std::move(it));
    }
    if (!plan.fit.empty()) {
        if (!ps.summary.empty()) ps.summary += "  ·  ";
        ps.summary += "Fit: " + plan.fit;
    }
    return ps;
}

// Match plan item status against activity stages by title/stage substring (legacy heuristic).
static std::string computePlanItemStatus(const PlanItem& it,
                                              const std::vector<ActivityItem>& activity) {
    if (!it.status.empty() && it.status != "pending") return it.status;
    auto lower = [](std::string s) {
        for (char& c : s) c = (char)std::tolower((unsigned char)c);
        return s;
    };
    std::string ln = lower(it.title);
    if (ln.empty()) return it.status.empty() ? "pending" : it.status;
    for (const auto& a : activity) {
        if (a.rowKind != 0) continue;
        if (lower(a.title).find(ln) != std::string::npos ||
            lower(a.stage).find(ln) != std::string::npos) {
            if (!a.status.empty()) return a.status;
        }
    }
    return it.status.empty() ? "pending" : it.status;
}

struct PlanAgentRow { std::string name, status, brief; };
struct PlanStageRow { std::string title, stage, status; };
struct PlanStatusCache {
    uint64_t revision = ~uint64_t(0);
    double stamp = -1.0;
    const PlanItem* items = nullptr;
    size_t count = 0;
    int planRevision = -1, frame = -1;
    bool running = false, agentsReady = false;
    std::vector<std::string> statuses;
    std::vector<PlanAgentRow> agents;
    size_t activityCount = 0;
    std::map<size_t, PlanStageRow> stages;
    std::map<size_t, PlanAgentRow> sourceAgents;
    std::vector<std::pair<std::string, std::string>> itemKeys;
};
static PlanStatusCache* s_planStatus = nullptr;

struct PlanStatusScope {
    PlanStatusCache* previous = s_planStatus;
    explicit PlanStatusScope(const ChatMsg& m) {
        static std::unordered_map<const ChatMsg*, PlanStatusCache> caches;
        auto it = caches.find(&m);
        if (it == caches.end()) {
            if (caches.size() >= 64) {
                auto oldest = caches.begin();
                for (auto p = caches.begin(); p != caches.end(); ++p)
                    if (p->second.frame < oldest->second.frame) oldest = p;
                caches.erase(oldest);
            }
            it = caches.emplace(&m, PlanStatusCache{}).first;
        }
        PlanStatusCache& c = it->second;
        if (c.stamp != m.ts || c.revision != m.revision || c.running != m.running ||
            c.items != m.planState.items.data() || c.count != m.planState.items.size() ||
            c.planRevision != m.planState.revision) {
            size_t from = 0;
            if (c.stamp == m.ts && m.activity.size() >= c.activityCount &&
                m.uiActivityDirtyFrom < m.activity.size())
                from = std::min(m.uiActivityDirtyFrom, c.activityCount);
            bool stagesChanged = c.stages.lower_bound(from) != c.stages.end();
            c.stages.erase(c.stages.lower_bound(from), c.stages.end());
            c.sourceAgents.erase(c.sourceAgents.lower_bound(from), c.sourceAgents.end());
            auto lower = [](std::string text) {
                for (char& ch : text) ch = (char)std::tolower((unsigned char)ch);
                return text;
            };
            for (size_t i = from; i < m.activity.size(); ++i) {
                const auto& a = m.activity[i];
                if (a.rowKind == 0) {
                    c.stages.emplace(i, PlanStageRow{lower(a.title), lower(a.stage), a.status});
                    stagesChanged = true;
                }
                if (a.missionHeader) {
                    PlanAgentRow row;
                    row.name = a.title.empty() ? a.stage : a.title;
                    row.status = a.status.empty() ? "pending" : a.status;
                    row.brief = a.reason.empty() ? a.detail : a.reason;
                    c.sourceAgents.emplace(i, std::move(row));
                }
            }
            bool itemsChanged = c.items != m.planState.items.data() ||
                c.itemKeys.size() != m.planState.items.size() || c.stamp != m.ts;
            if (!itemsChanged)
                for (size_t i = 0; i < m.planState.items.size(); ++i)
                    if (c.itemKeys[i].first != m.planState.items[i].title ||
                        c.itemKeys[i].second != m.planState.items[i].status) { itemsChanged = true; break; }
            if (itemsChanged || stagesChanged) {
                c.statuses.clear();
                c.itemKeys.clear();
                for (const auto& item : m.planState.items) {
                    c.itemKeys.emplace_back(item.title, item.status);
                    std::string status = item.status.empty() ? "pending" : item.status;
                    if (status == "pending") {
                        const std::string title = lower(item.title);
                        if (!title.empty())
                            for (const auto& source : c.stages) {
                                const auto& stage = source.second;
                                if (!stage.status.empty() && (stage.title.find(title) != std::string::npos ||
                                    stage.stage.find(title) != std::string::npos)) { status = stage.status; break; }
                            }
                    }
                    c.statuses.push_back(std::move(status));
                }
            }
            c.activityCount = m.activity.size();
            c.revision = m.revision;
            c.stamp = m.ts;
            c.items = m.planState.items.data();
            c.count = m.planState.items.size();
            c.planRevision = m.planState.revision;
            c.running = m.running;
            c.agentsReady = false;
            c.agents.clear();
        }
        c.frame = ImGui::GetFrameCount();
        s_planStatus = &c;
    }
    ~PlanStatusScope() { s_planStatus = previous; }
};

static std::string planItemStatusFromActivity(const PlanItem& item, size_t index,
                                             const std::vector<ActivityItem>& activity) {
    if (s_planStatus && index < s_planStatus->statuses.size() &&
        s_planStatus->itemKeys[index].first == item.title &&
        s_planStatus->itemKeys[index].second == item.status) {
        return s_planStatus->statuses[index];
    }
    return computePlanItemStatus(item, activity);
}

static void drawPlanStatusGlyph(const std::string& status, float size) {
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ui::kit::statusGlyph(ImGui::GetWindowDrawList(),
                         ImVec2(p0.x + size * 0.5f, p0.y + size * 0.5f),
                         size * 0.30f,
                         status == "running" && s_planStatus && !s_planStatus->running
                             ? ui::kit::Status::Done : chatRowStatus(status),
                         chatSurf());
    ImGui::Dummy(ImVec2(size, size));
}

static const char* planShapeChipLabel(const std::string& shape) {
    if (shape == "build_plan") return "build";
    if (shape == "checklist") return "checklist";
    if (shape == "phased_todo") return "todos";
    if (shape == "objectives") return "goals";
    if (shape == "dag") return "dag";
    return shape.empty() ? "plan" : shape.c_str();
}

static const char* planRomanNumeral(int n) {
    static const char* kR[] = {
        "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
        "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"
    };
    if (n >= 1 && n <= 20) return kR[n - 1];
    return nullptr;
}

// Old text status glyphs are absent from the UI font; rows use drawPlanStatusGlyph vectors.

static void planDrawShapeChip(const char* label, const ImVec4& accent) {
    if (!label || !*label) return;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImVec2 ts = ImGui::CalcTextSize(label);
    ImVec2 pad(5.0f * sc, 1.5f * sc);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + ts.x + pad.x * 2.0f, p0.y + ts.y + pad.y * 2.0f);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 bg = ImGui::ColorConvertFloat4ToU32(
        ImVec4(accent.x, accent.y, accent.z, 0.18f));
    ImU32 edge = ImGui::ColorConvertFloat4ToU32(
        ImVec4(accent.x, accent.y, accent.z, 0.45f));
    float rnd = ui::metrics::rounding(3.0f);
    dl->AddRectFilled(p0, p1, bg, rnd);
    dl->AddRect(p0, p1, edge, rnd, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + pad.x, p0.y + pad.y),
                ImGui::ColorConvertFloat4ToU32(accent), label);
    ImGui::Dummy(ImVec2(p1.x - p0.x, p1.y - p0.y));
}

// Tile width recipe shared by planDrawTile (paint) and planDrawTilesRow (layout): measured in
// the SAME medium font the tile draws with (mixing fonts here used to overlap/clip chips), and
// capped so one long title wraps to its own row instead of eating the whole strip.
static float planTileWidth(const PlanItem& it, const ui::Fonts& fonts) {
    const float sc = ui::uiScale;
    const float glyph = ImGui::GetTextLineHeight();
    const char* title = it.title.empty() ? "(untitled)" : it.title.c_str();
    ImFont* f = fonts.medium ? fonts.medium : ImGui::GetFont();
    const float tw = f->CalcTextSizeA(ImGui::GetFontSize(), FLT_MAX, 0.0f, title).x;
    float w = glyph + 4.0f * sc + tw + 12.0f * sc;
    const float avail = ImGui::GetContentRegionAvail().x;
    const float maxW = std::min(280.0f * sc, avail > 1.0f ? avail : 280.0f * sc);
    if (w > maxW) w = maxW;
    return w;
}

// One compact tile: status glyph + brief title (ellipsized); full desc on hover tooltip.
static void planDrawTile(const PlanItem& it, const std::string& status, const ui::Fonts& fonts) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const float glyph = ImGui::GetTextLineHeight();
    const char* title = it.title.empty() ? "(untitled)" : it.title.c_str();
    ImFont* f = fonts.medium ? fonts.medium : ImGui::GetFont();
    const float fs = ImGui::GetFontSize();
    ImVec2 tsz = f->CalcTextSizeA(fs, FLT_MAX, 0.0f, title);
    float padX = 6.0f * sc;
    float padY = 3.0f * sc;
    float gap = 4.0f * sc;
    const float w = planTileWidth(it, fonts);
    float h = std::max(glyph, tsz.y) + padY * 2.0f;

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##tile", ImVec2(w, h));
    const bool hov = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenBlockedByPopup);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 bg = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z,
               hov ? 0.12f : 0.06f));
    ImU32 edge = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textSecondary.x, surf.textSecondary.y, surf.textSecondary.z,
               hov ? 0.35f : 0.18f));
    float rnd = ui::metrics::rounding(4.0f);
    dl->AddRectFilled(p0, ImVec2(p0.x + w, p0.y + h), bg, rnd);
    dl->AddRect(p0, ImVec2(p0.x + w, p0.y + h), edge, rnd, 0, ui::metrics::stroke(1.0f));

    // Ellipsize the title to the tile's text lane (full title stays on the hover tooltip).
    const float textMax = w - padX * 2.0f - glyph - gap;
    const char* cut = title;
    const char* end = title + std::strlen(title);
    f->CalcTextSizeA(fs, textMax, 0.0f, title, end, &cut);
    std::string shown(title, cut);
    if (cut != end) {
        static const char* kEll = "\xe2\x80\xa6";
        const float ellW = f->CalcTextSizeA(fs, FLT_MAX, 0.0f, kEll).x;
        f->CalcTextSizeA(fs, ImMax(0.0f, textMax - ellW), 0.0f, title, end, &cut);
        shown.assign(title, cut);
        shown += kEll;
    }

    // Glyph inside the tile (restore cursor for drawPlanStatusGlyph, then restore after).
    ImVec2 saved = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(ImVec2(p0.x + padX, p0.y + (h - glyph) * 0.5f));
    drawPlanStatusGlyph(status, glyph);
    ImU32 tcol = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
    // Clip title to tile width.
    dl->PushClipRect(p0, ImVec2(p0.x + w - padX, p0.y + h), true);
    dl->AddText(f, fs, ImVec2(p0.x + padX + glyph + gap, p0.y + (h - tsz.y) * 0.5f),
                tcol, shown.c_str());
    dl->PopClipRect();
    ImGui::SetCursorScreenPos(saved);

    // Re-assert the FULL TILE as the last ImGui item: drawPlanStatusGlyph leaves a tiny
    // Dummy behind, and planDrawTilesRow's SameLine would otherwise lay the next tile
    // out relative to the glyph (tiles painted on top of each other).
    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(w, h));

    if (hov && !it.desc.empty())
        ImGui::SetTooltip("%s", it.desc.c_str());
    else if (hov && !it.title.empty())
        ImGui::SetTooltip("%s", it.title.c_str());
}

static void planDrawTilesRow(const std::vector<PlanItem>& items,
                             const std::vector<ActivityItem>& activity,
                             const ui::Fonts& fonts,
                             const char* kindFilter /* nullable: only these kinds */) {
    if (items.empty()) return;
    const float sc = ui::uiScale;
    const float gap = 4.0f * sc;
    float avail = ImGui::GetContentRegionAvail().x;
    if (avail < 40.0f * sc) avail = 40.0f * sc;
    float x = 0.0f;
    int drawn = 0;
    for (size_t i = 0; i < items.size(); ++i) {
        const PlanItem& it = items[i];
        if (kindFilter && *kindFilter) {
            if (it.kind != kindFilter) continue;
        } else {
            // Default tile strip: parts/objectives/nodes/steps/tasks — not subagents.
            if (it.kind == "subagent") continue;
        }
        const std::string st = planItemStatusFromActivity(it, i, activity);
        const float tw = planTileWidth(it, fonts);
        if (drawn > 0) {
            if (x + gap + tw > avail) {
                // wrap to next line
                x = 0.0f;
            } else {
                ImGui::SameLine(0, gap);
                x += gap;
            }
        }
        ImGui::PushID((int)i);
        planDrawTile(it, st, fonts);
        ImGui::PopID();
        x += tw;
        ++drawn;
    }
}

// Todos tree: phase headers + compact status rows. Collapses untouched phases.
static void planDrawTodosTree(const PlanState& plan,
                              const std::vector<ActivityItem>& activity,
                              const ui::Fonts& fonts) {
    // Collect phases in order of first appearance; empty phase → "Tasks".
    std::vector<std::string> phases;
    std::map<std::string, std::vector<size_t>> byPhase;
    for (size_t i = 0; i < plan.items.size(); ++i) {
        const PlanItem& it = plan.items[i];
        if (it.kind == "subagent") continue;
        if (it.kind == "objective" && plan.shape == "objectives") continue;
        std::string ph = it.phase.empty() ? "Tasks" : it.phase;
        if (byPhase.find(ph) == byPhase.end()) phases.push_back(ph);
        byPhase[ph].push_back(i);
    }
    if (phases.empty()) return;

    const ui::AdaptiveSurface& surf = chatSurf();
    const float sc = ui::uiScale;
    const bool multi = phases.size() > 1 ||
                       plan.shape == "phased_todo" ||
                       (phases.size() == 1 && phases[0] != "Tasks");

    // ui::kit::sectionGap(), not the raw cpMenu token: these three sub-sections render BOTH inside
    // the turn timeline (Compact) and in the docked Tasks view (Cozy), and a 14 px menu gap inside
    // a 2 px conversation rhythm is exactly the row-to-row unevenness metrics.h:93 forbids.
    ImGui::Dummy(ImVec2(0, ui::kit::sectionGap()));
    ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Todos");
    ImGui::PopStyleColor();
    ImGui::PopFont();

    for (size_t pi = 0; pi < phases.size(); ++pi) {
        const std::string& ph = phases[pi];
        const auto& idxs = byPhase[ph];
        // Phase "touched" if any item is non-pending; "closed" items (done or abandoned)
        // give the header's OMP-style "closed/total" progress.
        bool touched = false;
        int closed = 0;
        for (size_t ii : idxs) {
            std::string st = planItemStatusFromActivity(plan.items[ii], ii, activity);
            if (st != "pending" && !st.empty()) touched = true;
            if (st == "done" || st == "pass" || st == "abandoned" || st == "skipped") ++closed;
        }
        // Always expand the first phase and any touched phase; collapse pure-pending tails.
        bool expand = (pi == 0) || touched || phases.size() == 1;

        ImGui::PushID((int)pi);
        if (multi) {
            char hdr[224];
            const char* rom = planRomanNumeral((int)pi + 1);
            if (rom)
                std::snprintf(hdr, sizeof(hdr), "%s. %s  %d/%d", rom, ph.c_str(), closed,
                              (int)idxs.size());
            else
                std::snprintf(hdr, sizeof(hdr), "%s  %d/%d", ph.c_str(), closed, (int)idxs.size());
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::PushFont(fonts.medium);
            if (!expand) {
                // One-line collapsible summary for untouched phases (the header counts them).
                // NoTreePushOnOpen: we never TreePop — avoids ID/indent stack imbalance.
                const ImGuiTreeNodeFlags flags =
                    ImGuiTreeNodeFlags_SpanAvailWidth | ImGuiTreeNodeFlags_NoTreePushOnOpen;
                if (ImGui::TreeNodeEx("##ph", flags, "%s", hdr))
                    expand = true;
                else {
                    ImGui::PopFont();
                    ImGui::PopStyleColor();
                    ImGui::PopID();
                    continue;
                }
            } else {
                ImGui::TextUnformatted(hdr);
            }
            ImGui::PopFont();
            ImGui::PopStyleColor();
        }

        for (size_t ii : idxs) {
            const PlanItem& it = plan.items[ii];
            std::string st = planItemStatusFromActivity(it, ii, activity);
            ImGui::PushID((int)ii);
            // Indent tree rows slightly under multi-phase headers.
            if (multi) {
                ImGui::Dummy(ImVec2(8.0f * sc, 0));
                ImGui::SameLine(0, 0);
            }
            // Use the same vector status glyph as action cards and tiles.
            drawPlanStatusGlyph(st, ImGui::GetTextLineHeight());
            ImGui::SameLine(0, 6.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextWrapped("%s", it.title.empty() ? "(untitled)" : it.title.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered() && !it.desc.empty())
                ImGui::SetTooltip("%s", it.desc.c_str());
            ImGui::PopID();
        }
        ImGui::PopID();
    }
}

// Objectives rows: objective text as primary label + status badge.
static void planDrawObjectives(const PlanState& plan,
                               const std::vector<ActivityItem>& activity,
                               const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const float sc = ui::uiScale;
    int n = 0;
    for (const auto& it : plan.items)
        if (it.kind == "objective" || plan.shape == "objectives") ++n;
    if (n == 0) return;

    ImGui::Dummy(ImVec2(0, ui::kit::sectionGap()));   // density-aware — see planDrawTodosTree
    ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Objectives");
    ImGui::PopStyleColor();
    ImGui::PopFont();

    for (size_t i = 0; i < plan.items.size(); ++i) {
        const PlanItem& it = plan.items[i];
        if (plan.shape != "objectives" && it.kind != "objective") continue;
        std::string st = planItemStatusFromActivity(it, i, activity);
        ImGui::PushID((int)i);
        drawPlanStatusGlyph(st, ImGui::GetTextLineHeight());
        ImGui::SameLine(0, 6.0f * sc);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(it.title.empty() ? "(objective)" : it.title.c_str());
        ImGui::PopFont();
        if (ImGui::IsItemHovered() && !it.desc.empty())
            ImGui::SetTooltip("%s", it.desc.c_str());
        ImGui::SameLine(0, 8.0f * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(st.c_str());
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
}

// Subagents list: name + status; hover = brief. Empty → omit.
static void planDrawSubagents(const PlanState& plan,
                              const std::vector<ActivityItem>& activity,
                              const ui::Fonts& fonts) {
    (void)fonts;
    std::vector<PlanAgentRow> uncached;
    auto& rows = s_planStatus ? s_planStatus->agents : uncached;
    if (!s_planStatus || !s_planStatus->agentsReady) {
    for (size_t i = 0; i < plan.items.size(); ++i) {
        const auto& it = plan.items[i];
        if (it.kind != "subagent") continue;
        PlanAgentRow r;
        r.name = it.title;
        r.status = planItemStatusFromActivity(it, i, activity);
        r.brief = it.desc;
        rows.push_back(std::move(r));
    }
    // Also surface mission headers when plan has no subagent items.
    if (rows.empty() && s_planStatus)
        for (const auto& source : s_planStatus->sourceAgents) rows.push_back(source.second);
    if (s_planStatus) s_planStatus->agentsReady = true;
    }
    if (rows.empty()) return;

    const ui::AdaptiveSurface& surf = chatSurf();
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, ui::kit::sectionGap()));   // density-aware — see planDrawTodosTree
    ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Subagents");
    ImGui::PopStyleColor();
    ImGui::PopFont();

    for (size_t i = 0; i < rows.size(); ++i) {
        const PlanAgentRow& r = rows[i];
        ImGui::PushID((int)i);
        drawPlanStatusGlyph(r.status, ImGui::GetTextLineHeight());
        ImGui::SameLine(0, 6.0f * sc);
        ImGui::TextUnformatted(r.name.c_str());
        const bool nameHov = ImGui::IsItemHovered();
        ImGui::SameLine(0, 8.0f * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(r.status.c_str());
        ImGui::PopStyleColor();
        const bool stHov = ImGui::IsItemHovered();
        if ((nameHov || stHov) && !r.brief.empty())
            ImGui::SetTooltip("%s", r.brief.c_str());
        ImGui::PopID();
    }
}

// Live plan progress: done/total across step/task/part items + the title of the item
// currently running (for the one-line summary shown while the turn works).
static void planProgress(const PlanState& plan, const std::vector<ActivityItem>& activity,
                         int& done, int& total, std::string& current) {
    done = 0; total = 0; current.clear();
    for (size_t i = 0; i < plan.items.size(); ++i) {
        const auto& it = plan.items[i];
        if (it.kind == "subagent" || it.kind == "objective") continue;
        ++total;
        const std::string st = planItemStatusFromActivity(it, i, activity);
        if (st == "done" || st == "pass") ++done;
        else if (st == "running" && current.empty())
            current = it.title.empty() ? it.id : it.title;
    }
}

// One-line live summary: "3/8 done · now: extrude base" (or the tail when nothing runs).
std::string planProgressLine(const ChatMsg& m) {
    PlanStatusScope scope(m);
    int d = 0, t = 0; std::string cur;
    planProgress(m.planState, m.activity, d, t, cur);
    std::string out;
    if (t > 0) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%d/%d", d, t);
        out = buf;
    }
    if (!cur.empty()) { if (!out.empty()) out += " \xc2\xb7 "; out += "now: "; out += cur; }
    return out;
}

void drawPlanSection(const ChatMsg& m, const ui::Fonts& fonts) {
    const PlanState& plan = m.planState;
    const auto& activity = m.activity;
    if (!plan.present) return;
    PlanStatusScope statusScope(m);
    // Real plan = at least one non-subagent item (step/task/part/objective/node)
    // OR an explicit fromEvent plan with a non-empty summary. Spawn-only runs that
    // only carry kind=subagent rows must not draw a Plan/checklist card — the
    // Delegating card is the spawn surface.
    bool hasRealItem = false;
    for (const auto& it : plan.items) {
        if (it.kind == "step" || it.kind == "task" || it.kind == "part" ||
            it.kind == "objective" || it.kind == "node") {
            hasRealItem = true;
            break;
        }
    }
    const bool meaningfulSummary = plan.fromEvent && !plan.summary.empty();
    if (!hasRealItem && !meaningfulSummary) return;
    // Nothing to show if we have no items and no summary/title-only noise.
    if (plan.items.empty() && plan.summary.empty() && plan.title.empty()) return;

    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accent = semanticOnGlass(pal.accent, surf);
    const float sc = ui::uiScale;

    // Collapsible section — the header toggle state persists per message via ImGui storage
    // (id is stable inside the message's PushID scope). Default open.
    ImGuiStorage* hdrStore = ImGui::GetStateStorage();
    const ImGuiID hdrId = ImGui::GetID("##plansec_open");
    const bool sectionOpen = hdrStore->GetBool(hdrId, true);
    if (!sectionOpen) {
        // Collapsed: one-line card with title + live progress; click re-opens.
        // Same leading gap + padding tokens as every other row drawAgentTimeline dispatches
        // (cpChatRowGap/cpChatCardPadX/Y) — this used to run the heavier cpMenu* rhythm, which
        // is why the plan card sat at a different inset than its neighbours in the stack.
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        GlassBubble gb0 = beginGlassBubble(ImGui::GetContentRegionAvail().x,
                                           ui::GlassLevel::Thin, 10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
        ImGui::BeginChild("##plansec_min", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(accent, "%s", plan.title.empty() ? "Plan" : plan.title.c_str());
        ImGui::PopFont();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) hdrStore->SetBool(hdrId, true);
        const std::string prog = planProgressLine(m);
        if (!prog.empty()) {
            ImGui::SameLine(0, 8.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted(prog.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::SameLine(0, 8.0f * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted("(show)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) hdrStore->SetBool(hdrId, true);
        ImGui::EndChild();
        ImGui::PopStyleVar();   // ##plansec_min WindowPadding
        endGlassBubble(gb0);
        return;
    }

    // See the collapsed branch above: same shared cpChatRowGap/cpChatCardPadX/Y tokens as
    // every other drawAgentTimeline row, not the heavier cpMenu* menu rhythm.
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##plansec", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    // Left accent bar (planAccentBar alpha).
    dl->AddRectFilled(w0, ImVec2(w0.x + ui::metrics::px(3.0f), w0.y + ImGui::GetWindowHeight()),
                      (ui::Palette::toU32(pal.accent) & 0x00FFFFFFu) |
                      ((uint32_t)(unsigned char)pal.planAccentBar << 24));

    // Header: title + optional shape chip.
    ImGui::PushFont(fonts.medium);
    const char* hdr = plan.title.empty() ? "Plan" : plan.title.c_str();
    // Prefer "Plan / Objectives" chrome when title is generic.
    if (plan.title.empty() || plan.title == "Build plan" || plan.title == "Plan") {
        if (plan.shape == "objectives")
            ImGui::TextColored(accent, "Objectives");
        else if (plan.shape == "build_plan")
            ImGui::TextColored(accent, "Build plan");
        else
            ImGui::TextColored(accent, "Plan");
    } else {
        ImGui::TextColored(accent, "%s", hdr);
    }
    ImGui::PopFont();
    // (hide) toggle on the header row — collapses the whole section to the one-line summary.
    {
        ImGui::SameLine(0, 8.0f * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted("(hide)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) hdrStore->SetBool(hdrId, false);
    }
    if (!plan.shape.empty()) {
        ImGui::SameLine(0, 8.0f * sc);
        // Vertically center chip with title line.
        float y = ImGui::GetCursorPosY();
        ImGui::SetCursorPosY(y + 1.0f * sc);
        planDrawShapeChip(planShapeChipLabel(plan.shape), accent);
    }
    // Live progress ("0/3 · now: model: …") on its OWN wrapped line under the header — same
    // line as the title it overflowed and hard-clipped (the "Build (hide) 0/3 · now:…" bug).
    int progDone = 0, progTotal = 0;
    std::string progCur;
    planProgress(plan, activity, progDone, progTotal, progCur);
    std::string prog;
    if (progTotal > 0) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%d/%d", progDone, progTotal);
        prog = buf;
    }
    if (!progCur.empty()) { if (!prog.empty()) prog += " \xc2\xb7 "; prog += "now: "; prog += progCur; }
    if (!prog.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", prog.c_str());
        ImGui::PopStyleColor();
    }
    // Summary — skipped when it only restates the progress line (the solo stage checklist
    // emits summary = request text AND a "model: <request>" running item, so the card used to
    // print the same sentence twice). Titles are word-capped, so compare prefixes both ways.
    bool summaryDup = false;
    if (!plan.summary.empty() && !progCur.empty()) {
        std::string cur = progCur;
        const size_t colon = cur.find(": ");
        if (colon != std::string::npos) cur = cur.substr(colon + 2);   // drop "model:" stage prefix
        // plan_item_title word-caps the item title, so cur is a word-boundary prefix of the
        // summary (or equal); compare prefixes both ways.
        summaryDup = plan.summary.rfind(cur, 0) == 0 || cur.rfind(plan.summary, 0) == 0;
    }
    if (!plan.summary.empty() && !summaryDup) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", plan.summary.c_str());
        ImGui::PopStyleColor();
    }

    // TILES row — compact status+title chips with hover tooltips.
    if (!plan.items.empty() && plan.shape != "objectives") {
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        if (plan.shape == "dag") {
            planDrawTilesRow(plan.items, activity, fonts, "node");
            // If no kind=node, fall back to all non-subagent items.
            bool anyNode = false;
            for (const auto& it : plan.items)
                if (it.kind == "node") { anyNode = true; break; }
            if (!anyNode) planDrawTilesRow(plan.items, activity, fonts, nullptr);
            if ((int)plan.items.size() > 8 || !plan.edges.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextUnformatted("Open in AI workspace for the full graph");
                ImGui::PopStyleColor();
            }
        } else if (plan.shape == "build_plan") {
            planDrawTilesRow(plan.items, activity, fonts, "part");
            bool anyPart = false;
            for (const auto& it : plan.items)
                if (it.kind == "part") { anyPart = true; break; }
            if (!anyPart) planDrawTilesRow(plan.items, activity, fonts, nullptr);
        } else {
            // checklist / phased_todo: still show a dense tile strip of steps/tasks.
            planDrawTilesRow(plan.items, activity, fonts, nullptr);
        }
    }

    // Shape-specific subsections.
    if (plan.shape == "objectives") {
        planDrawObjectives(plan, activity, fonts);
    } else if (plan.shape == "checklist" || plan.shape == "phased_todo" ||
               plan.shape == "build_plan") {
        // Todos tree for ordered work; build_plan also benefits from a compact checklist.
        bool hasTodoish = false;
        for (const auto& it : plan.items) {
            if (it.kind == "step" || it.kind == "task" || it.kind == "part") {
                hasTodoish = true; break;
            }
        }
        if (hasTodoish || plan.shape == "checklist" || plan.shape == "phased_todo")
            planDrawTodosTree(plan, activity, fonts);
    } else if (plan.shape == "dag") {
        // Compact tiles already drawn; optional tiny todo of nodes if few.
        if ((int)plan.items.size() <= 12)
            planDrawTodosTree(plan, activity, fonts);
    }

    // Subagents subsection only when the Plan has real work items. Spawn-only
    // (subagents alone) never reaches here; when Delegating already lists them
    // and they'd be the only Plan body, omit to avoid duplicating that card.
    if (hasRealItem)
        planDrawSubagents(plan, activity, fonts);

    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##plansec WindowPadding
    ImRect planRect = endGlassBubble(gb);

    // Flat copy string for the whole section.
    chatCopyContext(planRect, "##plan_copy", [&] {
        std::string copyTxt = plan.title.empty() ? "Plan" : plan.title;
        if (!plan.shape.empty()) { copyTxt += " ["; copyTxt += plan.shape; copyTxt += "]"; }
        if (!plan.summary.empty()) { copyTxt += "\n"; copyTxt += plan.summary; }
        for (const auto& it : plan.items) {
            copyTxt += "\n- ";
            if (!it.status.empty()) { copyTxt += "["; copyTxt += it.status; copyTxt += "] "; }
            copyTxt += it.title.empty() ? it.id : it.title;
            if (!it.phase.empty()) { copyTxt += "  ("; copyTxt += it.phase; copyTxt += ")"; }
            if (!it.desc.empty()) { copyTxt += " — "; copyTxt += it.desc; }
        }
        return copyTxt;
    });
}


namespace ui {

// The cards that wait on the USER (plan approval, ask_user) share one language with the
// user's own bubble: a soft accent-tinted card with a same-hue edge and the bubble's padding —
// distinct from the glass info cards, and never a heavy frame. Begin/end pair.
static GlassBubble beginUserActionCard() {
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                      ui::metrics::cpChatBubbleRadius() * 0.8f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatBubblePadX(), ui::metrics::cpChatBubblePadY()));
    return gb;
}
static ImRect endUserActionCard(const GlassBubble& gb) {
    ImGui::PopStyleVar();   // WindowPadding pushed by beginUserActionCard
    const bool bright = chatSurf().darkText;
    return endTintBubble(gb, ui::palette.accent, bright ? 0.07f : 0.10f, bright ? 0.38f : 0.42f);
}

// User-facing plan approve gate — ONLY manual Plan mode (plan_only / awaiting_plan_approval)
// or advisor escalate (needs_user_decision). Agent-decided plans never reach this card.
// Question: "need edits, or start execution?" Edit plan prefills the composer for a revision
// pass; Execute resumes the SAME conversation as ONE agent turn carrying the approved plan.
void drawPlanApproveCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    if (!m.planApprovePending || m.running) return;
    if (m.errKind != ERR_NONE) {
        m.planApprovePending = false;
        return;
    }
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);

    GlassBubble gb = beginUserActionCard();
    ImGui::BeginChild("##plan_approve", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "%s",
                       m.planApproveReason.empty()
                           ? "Plan ready — need edits, or start execution?"
                           : "Plan needs your decision");
    ImGui::PopFont();

    if (!m.planApproveReason.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", m.planApproveReason.c_str());
        ImGui::PopStyleColor();
    } else {
        dimText("Review the plan above. Edit it, or execute it as it stands.");
    }

    auto planText = [&]() -> std::string {
        std::string out;
        if (m.planState.present) {
            out = m.planState.title.empty() ? "Plan" : m.planState.title;
            if (!m.planState.summary.empty()) { out += "\n"; out += m.planState.summary; }
            for (const auto& it : m.planState.items) {
                out += "\n- ";
                if (!it.status.empty()) { out += "["; out += it.status; out += "] "; }
                out += it.title.empty() ? it.id : it.title;
                if (!it.desc.empty()) { out += " — "; out += it.desc; }
            }
        } else if (m.plan.present) {
            out = m.plan.name.empty() ? "Plan" : m.plan.name;
            if (!m.plan.summary.empty()) { out += "\n"; out += m.plan.summary; }
            for (const auto& p : m.plan.parts) {
                out += "\n- "; out += p.name;
                if (!p.dims.empty()) { out += " ("; out += p.dims; out += ")"; }
            }
        } else if (!m.text.empty()) {
            out = m.text;
        } else {
            out = m.planApproveRequest;
        }
        return out;
    };

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    // Edit plan — focuses composer with plan text for revision (resend as plan_only).
    const bool editPlan = ImGui::Button("Edit plan", ImVec2(-1, 0));
    const bool execute  = primaryButton("Execute", ImVec2(-1, ui::metrics::copilotBtnH()));
    const bool cancel   = ImGui::Button("Cancel", ImVec2(-1, 0));
    ImGui::PopStyleColor();
    ImGui::EndChild();
    endUserActionCard(gb);   // pops the ##plan_approve WindowPadding

    if (cancel) {
        m.planApprovePending = false;
        m.planApproveRequest.clear();
        m.planApproveExtras = nlohmann::json::object();
        m.planApproveReason.clear();
        if (m.text.empty()) m.text = "Plan cancelled.";
        st.status = "Ready."; st.statusKind = 0;
        return;
    }

    if (editPlan) {
        // Prefill composer with the plan + a revision prompt; switch to Plan mode so
        // the next send is plan_only. Cursor at end for the user's edit note.
        const std::string plan = planText();
        std::string draft =
            "Revise this plan:\n\n" + plan +
            "\n\n---\nMy edits: ";
        if (st.prompt.size() < draft.size() + 64)
            st.prompt.resize(draft.size() + 256, 0);
        std::memset(st.prompt.data(), 0, st.prompt.size());
        std::memcpy(st.prompt.data(), draft.data(), draft.size());
        st.aiInteraction = (int)CopilotMode::Plan;
        m.planApprovePending = false;  // card goes away; user will re-submit
        // Keep planApproveRequest so a subsequent Plan submit still knows the origin.
        st.status = "Edit the plan in the composer, then send.";
        st.statusKind = 0;
        return;
    }

    if (!execute) return;

    const std::string approved = planText();
    const std::string req = m.planApproveRequest.empty() ? approved : m.planApproveRequest;
    nlohmann::json extras = m.planApproveExtras.is_object() ? m.planApproveExtras
                                                            : nlohmann::json::object();
    nlohmann::json opts = nlohmann::json::object();
    opts["advisor"] = true;
    opts["approved_plan"] = approved;
    // Approval always resumes as ONE agent turn: the agent decides for itself whether the
    // approved plan wants subagents, so the host offers no execution styles.
    opts["mode"] = "agent";
    extras["options"] = std::move(opts);

    m.planApprovePending = false;
    m.planApproveReason.clear();
    m.running = true;
    if (st.settings.model[0]) m.model = st.settings.model;
    m.liveStatus = "Executing plan...";
    st.status = m.liveStatus; st.statusKind = 0;
    launchAgentTurn(st, req, extras);
}

// ============================ ask_user question cards ============================
// The online agent's ask_user batch (agent.question): one glass card per batch with a typed
// input per question (contracts/interaction.md kinds), Submit/Dismiss sent over the agent control
// lane (`answer`). Resolved batches collapse to a read-only summary. The model's own run keeps
// going while the card waits; a reply typed in the composer resolves the batch worker-side.

// The run_id an answer must name: the batch's own, else the viewed conversation's live run.
static std::string askRunId(AppState& st, const AskBatch& b) {
    if (!b.runId.empty()) return b.runId;
    if (auto* run = st.runIfExists(st.activeConvId())) {
        std::lock_guard<std::mutex> lk(run->mtx);
        return run->runId;
    }
    return std::string();
}

// True while the viewed conversation's turn runs, so the control lane can answer its batch. A
// batch restored after the app closed or crashed mid-question has no run left to answer.
static bool askRunLive(AppState& st) {
    auto* run = st.runIfExists(st.activeConvId());
    return st.workerStarted && run && run->running.load();
}

// Native-dialog filter (pairs of description/pattern, NUL-separated, double-NUL terminated).
static std::string askFileFilter(const AskQuestion& q) {
    std::vector<std::string> exts = q.accept;
    if (exts.empty() && q.media == "image") exts = {"png", "jpg", "jpeg", "webp", "bmp"};
    std::string f;
    if (!exts.empty()) {
        std::string pat;
        for (std::string e : exts) {
            while (!e.empty() && (e[0] == '*' || e[0] == '.')) e.erase(0, 1);
            if (e.empty()) continue;
            if (!pat.empty()) pat += ";";
            pat += "*." + e;
        }
        if (!pat.empty()) {
            f += (q.media == "image" ? "Images (" : "Accepted files (") + pat + ")";
            f.push_back('\0');
            f += pat;
            f.push_back('\0');
        }
    }
    f += "All files (*.*)";
    f.push_back('\0');
    f += "*.*";
    f.push_back('\0');
    f.push_back('\0');
    return f;
}

// Move the composer's context-picker chips that fit this question's targets into its picks.
// Returns how many were taken (0 = none matched).
static int askTakePicks(AppState& st, AskQuestion& q) {
    int taken = 0;
    std::vector<core::ContextRef> keep;
    for (const auto& r : st.pendingContextRefs) {
        const bool fits = q.targets.empty() ||
                          std::find(q.targets.begin(), q.targets.end(), r.kind) != q.targets.end();
        if (!fits || r.id.empty() || (!q.multi && taken > 0)) {
            keep.push_back(r);
            continue;
        }
        if (!q.multi) q.picks.clear();
        const bool dup = std::any_of(q.picks.begin(), q.picks.end(), [&](const AskPick& p) {
            return p.kind == r.kind && p.id == r.id && p.docId == r.docId;
        });
        if (!dup) q.picks.push_back(AskPick{r.kind, r.id, r.docId, r.label, r.selector});
        ++taken;
    }
    if (taken > 0) {
        st.pendingContextRefs = std::move(keep);
        savePromptDraft(st, /*force=*/true);
    }
    return taken;
}

// Same sentence modulo case, surrounding/inner whitespace runs and trailing '?', '.' or ':'.
static bool askSameSentence(const std::string& a, const std::string& b) {
    auto norm = [](const std::string& s) {
        std::string out;
        out.reserve(s.size());
        bool space = false;
        for (char c : s) {
            if (std::isspace((unsigned char)c)) { space = !out.empty(); continue; }
            if (space) { out.push_back(' '); space = false; }
            out.push_back((char)std::tolower((unsigned char)c));
        }
        while (!out.empty() && (out.back() == '?' || out.back() == '.' || out.back() == ':' ||
                                out.back() == ' '))
            out.pop_back();
        return out;
    };
    return norm(a) == norm(b);
}

// One question's input widget, by kind. Unknown kinds edit as free text.
static void drawAskInput(AppState& st, AskQuestion& q, const ui::AdaptiveSurface& surf) {
    const auto& pal = ui::palette;
    const ImGuiStyle& style = ImGui::GetStyle();
    // A native selectable owns the entire wrapped row, including keyboard activation.
    // The selector is only a compact glyph; its size never dictates the row's hit area.
    auto choiceRow = [&](const char* id, const char* label, bool selected, bool multi) {
        const float width = std::max(ImGui::GetContentRegionAvail().x,
                                     ui::metrics::stroke(1.0f));
        const float padX = kit::padX();
        const float padY = kit::padY();
        const float glyph = ImGui::GetTextLineHeight() * 0.7f;
        const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
        const float textW = std::max(width - padX * 2.0f - glyph - gap,
                                     ui::metrics::stroke(1.0f));
        const ImVec2 textSize = ImGui::CalcTextSize(label, nullptr, false, textW);
        const float height = std::max(ImGui::GetFrameHeight(), textSize.y + padY * 2.0f);
        const ImVec2 origin = ImGui::GetCursorScreenPos();
        const bool clicked = ImGui::Selectable(
            id, selected, ImGuiSelectableFlags_NoPadWithHalfSpacing, ImVec2(width, height));
        ImGuiContext& g = *GImGui;
        IMGUI_TEST_ENGINE_ITEM_INFO(
            ImGui::GetItemID(), label, GImGui->LastItemData.StatusFlags |
                ImGuiItemStatusFlags_Checkable |
                (selected ? ImGuiItemStatusFlags_Checked : 0));
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 g0(origin.x + padX, origin.y + padY +
                                            (ImGui::GetTextLineHeight() - glyph) * 0.5f);
        const ImU32 ink = ImGui::GetColorU32(ImGuiCol_Text);
        const ImU32 mark = ImGui::GetColorU32(ImGuiCol_CheckMark);
        if (multi) {
            dl->AddRect(g0, ImVec2(g0.x + glyph, g0.y + glyph), ink,
                        ui::metrics::rounding(2.0f), 0, ui::metrics::stroke(1.0f));
            if (selected)
                ImGui::RenderCheckMark(dl, ImVec2(g0.x + glyph * 0.15f,
                                                 g0.y + glyph * 0.15f), mark, glyph * 0.7f);
        } else {
            const ImVec2 center(g0.x + glyph * 0.5f, g0.y + glyph * 0.5f);
            dl->AddCircle(center, glyph * 0.5f, ink, 0, ui::metrics::stroke(1.0f));
            if (selected) dl->AddCircleFilled(center, glyph * 0.28f, mark);
        }
        dl->AddText(ImGui::GetFont(), ImGui::GetFontSize(),
                    ImVec2(g0.x + glyph + gap, origin.y + padY), ink,
                    label, nullptr, textW);
        return clicked;
    };
    if (q.kind == "choice") {
        if (q.checked.size() != q.options.size()) q.checked.resize(q.options.size(), 0);
        for (size_t oi = 0; oi < q.options.size(); ++oi) {
            ImGui::PushID((int)oi);
            const AskOption& opt = q.options[oi];
            const char* lab = opt.label.empty() ? opt.value.c_str() : opt.label.c_str();
            if (choiceRow("##ask_choice", lab,
                          q.multi ? q.checked[oi] != 0 : q.choice == (int)oi, q.multi)) {
                if (q.multi) q.checked[oi] = q.checked[oi] ? 0 : 1;
                else q.choice = (int)oi;
            }
            ImGui::PopID();
        }
        // "Other": the user's own words. Typing in the field selects the row.
        if (choiceRow("##ask_other_choice", "Other",
                      q.multi ? q.otherOn : q.choice == kAskOther, q.multi)) {
            if (q.multi) q.otherOn = !q.otherOn;
            else q.choice = kAskOther;
        }
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputTextWithHint("##other", "Your own answer", q.otherBuf.data(),
                                     q.otherBuf.size())) {
            if (q.multi) q.otherOn = true;
            else q.choice = kAskOther;
        }
    } else if (q.kind == "dimension") {
        const auto& units = askDimUnits(q.family);
        const bool pickUnit = units.size() > 1;
        float unitW = ImGui::GetFrameHeight() + style.FramePadding.x * 2.0f;
        for (const auto& u : units)
            unitW = std::max(unitW, ImGui::CalcTextSize(u.c_str()).x +
                                       ImGui::GetFrameHeight() + style.FramePadding.x * 2.0f);
        const float avail = ImGui::GetContentRegionAvail().x;
        const float minValueW = ImGui::CalcTextSize("-0.000000").x +
                                style.FramePadding.x * 2.0f;
        const bool inlineUnit = pickUnit && avail >= minValueW + unitW + style.ItemSpacing.x;
        ImGui::SetNextItemWidth(inlineUnit ? avail - unitW - style.ItemSpacing.x : -FLT_MIN);
        ImGui::InputTextWithHint("##dim", "Value", q.textBuf.data(), q.textBuf.size(),
                                 ImGuiInputTextFlags_CharsScientific);
        if (pickUnit) {
            if (inlineUnit) ImGui::SameLine();
            ImGui::SetNextItemWidth(inlineUnit ? unitW : -FLT_MIN);
            const ImVec4 popBg(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]);
            ImGui::PushStyleColor(ImGuiCol_PopupBg, popBg);
            const ImGuiID unitId = ImGui::GetID("##unit");
            if (ImGui::BeginCombo("##unit", q.unit.c_str())) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::maxContrastTextOn(popBg));
                for (const auto& u : units)
                    if (ImGui::Selectable(u.c_str(), u == q.unit)) q.unit = u;
                ImGui::PopStyleColor();
                ImGui::EndCombo();
            }
            ImGui::PopStyleColor();
            ImGuiContext& g = *GImGui;
            IMGUI_TEST_ENGINE_ITEM_INFO(unitId, "##unit", 0);
        }
    } else if (q.kind == "approval") {
        // The proposal often restates the prompt ("Add vent slots?" / "Add vent slots"); a
        // second copy right under the question is noise, so it shows only when it adds words.
        if (!q.proposal.empty() && !askSameSentence(q.proposal, q.prompt)) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextWrapped("%s", q.proposal.c_str());
            ImGui::PopStyleColor();
        }
        // Keep both states native and keyboard-accessible without suggesting a default.
        if (choiceRow("##ask_approve", "Approve", q.approval == 1, false)) q.approval = 1;
        if (choiceRow("##ask_decline", "Decline", q.approval == 0, false)) q.approval = 0;
    } else if (q.kind == "file_request") {
        const char* browse = "Browse\xe2\x80\xa6";
        const float browseW = ImGui::CalcTextSize(browse).x + style.FramePadding.x * 2.0f;
        const float avail = ImGui::GetContentRegionAvail().x;
        const bool inlineBrowse = avail >= browseW + style.ItemSpacing.x +
                                              ImGui::GetFontSize() * 12.0f;
        ImGui::SetNextItemWidth(inlineBrowse ? avail - browseW - style.ItemSpacing.x : -FLT_MIN);
        ImGui::InputTextWithHint("##paths",
                                 q.multiple ? "File paths, separated by ;" : "File path",
                                 q.textBuf.data(), q.textBuf.size());
        if (inlineBrowse) ImGui::SameLine();
        if (kit::secondaryButton("Browse\xe2\x80\xa6###ask_browse", true, nullptr,
                                 inlineBrowse ? ImVec2(0, 0) : ImVec2(-1, 0))) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
                st.toast(ui::ToastLevel::Info,
                         "File dialogs are disabled (CHUDWARE_NO_FILE_DIALOGS).");
            } else {
                const std::string filter = askFileFilter(q);
                const std::string p = openFileDialog(filter.c_str());
                if (!p.empty()) {
                    const std::string cur = askBufStr(q.textBuf);
                    const bool keep = q.multiple && cur.find_first_not_of(" \t;") != std::string::npos;
                    askBufSet(q.textBuf, keep ? cur + "; " + p : p);
                }
            }
        }
    } else if (q.kind == "geometry_select") {
        if (q.picks.empty()) {
            std::string hint = "Nothing picked yet.";
            if (!q.targets.empty()) {
                hint = "Nothing picked yet \xc2\xb7 pick a ";
                for (size_t i = 0; i < q.targets.size(); ++i)
                    hint += (i ? " or " : "") + q.targets[i];
                hint += ".";
            }
            dimText(hint.c_str());
        } else {
            for (size_t pi = 0; pi < q.picks.size(); ++pi) {
                const AskPick& p = q.picks[pi];
                const std::string lab = p.label.empty() ? p.kind + " " + p.id : p.label;
                ImGui::PushID((int)pi);
                // Even one chip can exceed a narrow card; keep the full identity as prose.
                if (ImGui::CalcTextSize(lab.c_str()).x + kit::padX() * 2.0f <=
                    ImGui::GetContentRegionAvail().x) {
                    kit::chip(lab.c_str(), kit::Tone::Accent);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, semanticOnGlass(pal.accent, surf));
                    ImGui::TextWrapped("%s", lab.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::PopID();
            }
        }
        const bool resolving =
            std::any_of(st.pendingContextRefs.begin(), st.pendingContextRefs.end(),
                        [](const core::ContextRef& r) { return r.resolving; });
        const int nRefs = (int)st.pendingContextRefs.size();
        const std::string useLbl = "Use picked geometry" +
                                   (nRefs > 0 ? " (" + std::to_string(nRefs) + ")" : std::string()) +
                                   "###ask_use_picks";
        const bool compactUse = ImGui::CalcTextSize(useLbl.c_str(), nullptr, true).x +
                                    style.FramePadding.x * 2.0f > ImGui::GetContentRegionAvail().x;
        if (kit::secondaryButton(compactUse ? "Use picks###ask_use_picks" : useLbl.c_str(),
                                 nRefs > 0 && !resolving,
                                 resolving ? "Waiting for the pick to resolve\xe2\x80\xa6"
                                           : "Pick on the model first, then use it here.",
                                 ImVec2(-1, 0))) {
            if (askTakePicks(st, q) == 0)
                st.toast(ui::ToastLevel::Info,
                         "None of the picked items fits this question.");
        }
        if (kit::secondaryButton(st.contextPickActive ? "Picking\xe2\x80\xa6###ask_pick_model"
                                                      : "Pick on model###ask_pick_model",
                                 !st.contextPickActive, "Click the model, then Esc", ImVec2(-1, 0)))
            st.contextPickActive = true;
        if (!q.picks.empty()) {
            if (kit::secondaryButton("Clear###ask_clear_picks", true, nullptr,
                                     ImVec2(-1, 0))) q.picks.clear();
        }
    } else if (q.kind == "text" && q.multiline) {
        drawChatInput("##text", q.textBuf, "Your answer", 6);
    } else {
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputTextWithHint("##text", "Your answer", q.textBuf.data(), q.textBuf.size());
    }
}

static const char* askResolvedTitle(AskStatus s) {
    switch (s) {
        case AskStatus::Answered:  return "Answered";
        case AskStatus::Reply:     return "Replied in chat";
        case AskStatus::Declined:  return "Dismissed";
        case AskStatus::Cancelled: return "Cancelled";
        case AskStatus::Expired:   return "Expired \xe2\x80\x94 the run that asked has ended";
        default:                   return "Waiting for your answers";
    }
}

// A resolved batch: what was asked and what came back, read-only. Deliberately LIGHTER than
// the live form — no card, no fill, just a quiet rail and compact "question  answer" lines —
// so an answered batch reads as a record in the transcript, not a second form to fill in.
static void drawAskSummary(AskBatch& b, const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginOpenBubble(ImGui::GetContentRegionAvail().x);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(chatProseInsetX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##ask_done", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    // A record, not a form: rows ride a compact spacing rhythm.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::px(3.0f)));
    const bool answered = b.status == AskStatus::Answered ||
                          (b.status == AskStatus::Reply && !b.answers.empty());
    ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextWrapped("%s", askResolvedTitle(b.status));
    ImGui::PopStyleColor();
    ImGui::PopFont();
    if (!b.agentId.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextWrapped("%s", b.agentId.c_str());
        ImGui::PopStyleColor();
    }
    if (b.status == AskStatus::Expired && !b.error.empty() &&
        b.error != "The run that asked has ended.")
        dimText(b.error.c_str());

    const float gap = ui::metrics::U(2.0f);
    for (size_t i = 0; i < b.questions.size(); ++i) {
        const AskQuestion& q = b.questions[i];
        const std::string prompt = q.prompt.empty() ? q.id : q.prompt;
        if (!answered) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("%s", prompt.c_str());
            ImGui::PopStyleColor();
            continue;
        }
        auto it = b.answers.find(q.id);
        const std::string ans = it != b.answers.end()
                                    ? askAnswerText(q, *it)
                                    : (q.required ? "(no answer)" : "(the suggested default)");
        // One line when question and answer fit side by side; stacked otherwise.
        const float avail = ImGui::GetContentRegionAvail().x;
        const float pw = ImGui::CalcTextSize(prompt.c_str()).x;
        const float aw = ImGui::CalcTextSize(ans.c_str()).x;
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        if (pw + gap + aw <= avail) {
            ImGui::TextUnformatted(prompt.c_str());
            ImGui::PopStyleColor();
            ImGui::SameLine(0.0f, gap);
            ImGui::TextUnformatted(ans.c_str());
        } else {
            ImGui::TextWrapped("%s", prompt.c_str());
            ImGui::PopStyleColor();
            ImGui::TextWrapped("%s", ans.c_str());
        }
    }
    if (b.status == AskStatus::Reply && !b.reply.empty()) {
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        dimText(("Your reply: " + b.reply).c_str());
    }
    ImGui::PopStyleVar();   // compact ItemSpacing
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##ask_done WindowPadding
    const ImRect rect = endOpenBubble(gb);
    // The quiet rail: the timeline rows' silhouette, so the record lines up with them.
    const float railW = ui::metrics::px(2.0f);
    ImGui::GetWindowDrawList()->AddRectFilled(
        rect.Min, ImVec2(rect.Min.x + railW, rect.Max.y),
        kit::toneU32(answered ? kit::Tone::Ok : kit::Tone::Neutral, 0.55f), railW * 0.5f);
    chatCopyContext(rect, "##ask_copy", [&] {
        std::string copy = askResolvedTitle(b.status);
        for (const auto& q : b.questions) {
            copy += "\n- " + (q.prompt.empty() ? q.id : q.prompt);
            if (!answered) continue;
            const auto it = b.answers.find(q.id);
            copy += ": " + (it != b.answers.end() ? askAnswerText(q, *it)
                : (q.required ? "(no answer)" : "(the suggested default)"));
        }
        if (b.status == AskStatus::Reply && !b.reply.empty()) copy += "\nYour reply: " + b.reply;
        return copy;
    });
}

// A pending batch: the interactive form.
static void drawAskForm(AppState& st, ChatMsg& m, AskBatch& b, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);
    const bool busy = b.status == AskStatus::Submitting;
    // The one card in a turn that needs the user (see beginUserActionCard).
    GlassBubble gb = beginUserActionCard();
    ImGui::BeginChild("##ask", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    kit::pushDensity(kit::Density::Compact);
    // Fields sit on glass: derive the frame from the surface ink (the Quick-settings idiom), so
    // values stay legible over a bright or dark backdrop.
    const ImVec4 ink = surf.textPrimary;
    const ImVec4 fieldBg(ink.x, ink.y, ink.z, 0.08f);
    const ImVec4 fieldHov(ink.x, ink.y, ink.z, 0.14f);
    const ImVec4 fieldAct(ink.x, ink.y, ink.z, 0.18f);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, fieldBg);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, fieldHov);
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, fieldAct);
    ImGui::PushStyleColor(ImGuiCol_Button, fieldBg);   // combo caret well == frame
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, fieldHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, fieldAct);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());

    ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, accentCol);
    ImGui::TextWrapped("%s", b.agentId.empty() ? "Questions for you"
                                             : ("Questions from " + b.agentId).c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    const bool live = askRunLive(st);
    dimText(live ? "Answer here and submit, or just reply in the chat. The agent waits for you."
                 : "The app closed while this was open. Submit sends your answers to the agent "
                   "as your next message.");

    if (busy) ImGui::BeginDisabled();
    for (size_t i = 0; i < b.questions.size(); ++i) {
        AskQuestion& q = b.questions[i];
        ImGui::PushID((int)i);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
        std::string lbl = q.prompt.empty() ? q.id : q.prompt;
        if (q.required) lbl += "  \xc2\xb7  required";
        ImGui::TextWrapped("%s", lbl.c_str());
        if (!q.why.empty()) dimText(q.why.c_str());
        drawAskInput(st, q, surf);
        ImGui::PopID();
    }
    if (busy) ImGui::EndDisabled();
    // Persist what the user has entered: a restart brings this form back with it.
    if (!busy) {
        nlohmann::json draft = askDraft(b);
        if (draft != b.savedDraft) {
            if (!b.savedDraft.is_null()) markChatHistoryDirty();
            b.savedDraft = std::move(draft);
        }
    }

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    std::string notReady;
    const bool ready = askBatchReady(b, &notReady);
    if (busy) {
        kit::spinner("");
        ImGui::SameLine();
        ImGui::TextWrapped("Sending your answers\xe2\x80\xa6");
    }
    kit::inlineStatus(b.error.c_str(), kit::Tone::Err);
    const bool submit = kit::primaryButton(
        "Submit answers", ready && !busy,
        busy ? "Sending\xe2\x80\xa6" : (notReady.empty() ? nullptr : notReady.c_str()),
        ImVec2(-1, ui::metrics::copilotBtnH()));
    const bool dismiss = kit::secondaryButton("Dismiss", !busy, "Sending\xe2\x80\xa6",
                                              ImVec2(-1, 0));
    if (ImGui::IsItemHovered() && !busy)
        ImGui::SetTooltip("Skip these questions \xe2\x80\x94 the agent continues on its own judgement.");

    ImGui::PopStyleVar();
    ImGui::PopStyleColor(8);
    kit::popDensity();
    ImGui::EndChild();
    endUserActionCard(gb);   // pops the ##ask WindowPadding

    if (!submit && !dismiss) return;
    const std::string runId = askRunId(st, b);
    if (!live || runId.empty()) {
        // No run is waiting: the answers go to the agent as this conversation's next message.
        b.error.clear();
        if (dismiss) {
            b.status = AskStatus::Declined;
        } else {
            b.answers = buildAskAnswers(b);
            b.status = AskStatus::Answered;
            queueAskAnswersTurn(st, b);
        }
        m.uiCachedH = -1.0f;
        ++m.revision;
        chatLogMessageChanged(m);
        markChatHistoryDirty();
        s_chatHistoryLastPersist = -1e9;
        return;
    }
    nlohmann::json answers = submit ? buildAskAnswers(b) : nlohmann::json::object();
    b.status = AskStatus::Submitting;
    b.error.clear();
    b.answers = answers;
    m.uiCachedH = -1.0f;
    markChatHistoryDirty();
    agentAnswerDispatch(st, runId, b.batchId, std::move(answers), /*declined=*/dismiss);
}

std::string askMarkerStage(const std::string& batchId) { return "__ask__:" + batchId; }

static void drawAskCard(AppState& st, ChatMsg& m, AskBatch& b, size_t index,
                        const ui::Fonts& fonts) {
    if (b.batchId.empty()) ImGui::PushID((int)index);
    else ImGui::PushID(b.batchId.c_str());
    if (askStatusOpen(b.status)) drawAskForm(st, m, b, fonts);
    else drawAskSummary(b, fonts);
    ImGui::PopID();
}

void drawAskMarker(AppState& st, ChatMsg& m, const ActivityItem& marker, const ui::Fonts& fonts) {
    for (size_t bi = 0; bi < m.asks.size(); ++bi)
        if (!m.asks[bi].batchId.empty() && askMarkerStage(m.asks[bi].batchId) == marker.stage) {
            drawAskCard(st, m, m.asks[bi], bi, fonts);
            return;
        }
}

void drawAskCards(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    for (size_t bi = 0; bi < m.asks.size(); ++bi) {
        AskBatch& b = m.asks[bi];
        const bool anchored =
            !b.batchId.empty() &&
            std::any_of(m.activity.begin(), m.activity.end(), [&](const ActivityItem& a) {
                return a.rowKind == 20 && a.stage == askMarkerStage(b.batchId);
            });
        if (!anchored) drawAskCard(st, m, b, bi, fonts);
    }
}

// --- model-routing + context-meter helpers (transparency UI) -----------------------------------
// "openai/gpt-oss-120b:free" -> "gpt-oss-120b" for compact display.

// Inter-agent message (rowKind 7, stage "msg:<channel>:<id>"; title = from, reason = to,
// detail = body, status = workpool mode, eventTs = send time), drawn like OMP's IRC card: a
// header — reply hook, channel tag, "from > to", age — over the body's first non-blank lines
// on a quote gutter, "+N more lines" past three. Click expands the whole body. Vector marks
// only: the font has no arrow or box-drawing glyphs.
static void paintMsgArrow(ImDrawList* dl, float x0, float x1, float cy, ImU32 col) {
    const float th   = ui::metrics::stroke(1.2f);
    const float head = (x1 - x0) * 0.42f;
    dl->AddLine(ImVec2(x0, cy), ImVec2(x1 - head * 0.5f, cy), col, th);
    dl->AddTriangleFilled(ImVec2(x1, cy), ImVec2(x1 - head, cy - head * 0.62f),
                          ImVec2(x1 - head, cy + head * 0.62f), col);
}
static void paintMsgHook(ImDrawList* dl, ImVec2 p, float s, ImU32 col) {
    const float th = ui::metrics::stroke(1.2f);
    const ImVec2 top(p.x + s * 0.22f, p.y + s * 0.20f);
    const ImVec2 knee(p.x + s * 0.22f, p.y + s * 0.62f);
    dl->AddLine(top, knee, col, th);
    paintMsgArrow(dl, knee.x, p.x + s * 0.92f, knee.y, col);
}

void drawAgentMessageLine(const ActivityItem& a, const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const size_t chEnd = a.stage.find(':', 4);
    const std::string channel = chEnd == std::string::npos ? std::string("irc")
                                                           : a.stage.substr(4, chEnd - 4);
    const bool board = channel == "board";
    const bool pool = channel == "pool";
    const ui::kit::Tone tone = pool    ? ui::kit::Tone::Info
                             : board   ? ui::kit::Tone::Neutral
                                       : ui::kit::Tone::Accent;
    const char* tag = pool ? "Pool" : board ? "Board" : "IRC";
    // A workpool card is sent by its pool ("pool:<name>"), not by an agent.
    const std::string from = pool && a.title.rfind("pool:", 0) == 0 ? a.title.substr(5) : a.title;
    const std::string& to = a.reason;
    const std::string& body = a.detail;
    // The card body is the message's non-blank lines, trimmed, as OMP shows them.
    std::vector<std::string> lines;
    for (size_t pos = 0; pos <= body.size();) {
        size_t nl = body.find('\n', pos);
        if (nl == std::string::npos) nl = body.size();
        const size_t b = body.find_first_not_of(" \t\r", pos);
        if (b != std::string::npos && b < nl) {
            std::string line = body.substr(b, body.find_last_not_of(" \t\r", nl - 1) - b + 1);
            std::replace(line.begin(), line.end(), '\t', ' ');
            lines.push_back(std::move(line));
        }
        pos = nl + 1;
    }
    constexpr size_t kCollapsedLines = 3;

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    ImGuiStorage* store = ImGui::GetStateStorage();
    const ImGuiID openKey = ImGui::GetID(("##msg_open" + a.stage).c_str());
    bool open = store->GetBool(openKey, false);
    const size_t shown = open ? 0 : std::min(lines.size(), kCollapsedLines);
    const size_t more = open ? 0 : lines.size() - shown;

    ImGui::PushFont(fonts.body);
    const float lineH = ImGui::GetTextLineHeight();
    const float step = ImGui::GetTextLineHeightWithSpacing();
    const float padY = ui::kit::padY();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float h = padY * 2.0f + lineH + step * (float)(shown + (more ? 1 : 0));
    const bool clicked = ui::kit::rowBegin(a.stage.c_str(), h, open);
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 toneCol = ImGui::ColorConvertFloat4ToU32(ui::kit::toneOn(tone, surf));
    const ImU32 dimCol = ImGui::ColorConvertFloat4ToU32(surf.textTertiary);
    const ImU32 bodyCol = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    const float gap = ui::metrics::U(1.5f);
    const float left = p0.x + ui::kit::padX();
    const float right = p0.x + w - ui::kit::padX();
    const float hy = p0.y + padY;   // header line top
    const float cy = hy + lineH * 0.5f;
    // Gutter under the hook's stem; body text aligned with the channel tag.
    const float gutterX = left + lineH * 0.22f;
    const float textX = left + lineH + gap * 0.5f;

    // Header meta after the names: the workpool card's mode, then the message's age.
    std::string meta = pool && a.status != "done" ? a.status : std::string();
    const std::string age = chatAgeLabel(a.eventTs);
    if (!age.empty()) meta += (meta.empty() ? std::string() : std::string(" \xc2\xb7 ")) + age;
    const float metaW = meta.empty() ? 0.0f : ImGui::CalcTextSize(meta.c_str()).x + gap;

    paintMsgHook(dl, ImVec2(left, hy), lineH, toneCol);
    float x = textX;
    ImGui::PushFont(fonts.medium);
    const float nameY = cy - ImGui::GetTextLineHeight() * 0.5f;
    dl->AddText(ImVec2(x, nameY), toneCol, tag);
    x += ImGui::CalcTextSize(tag).x + gap;
    const bool arrow = !board && !to.empty();
    const float arrowW = lineH * 0.7f;
    const float arrowSpan = arrow ? arrowW + gap * 1.8f : 0.0f;
    // Both names stay whole when they fit. Task ids nest ("Parent.Child"), so a tight header
    // first keeps only the leaf names; then the longer name gives way.
    const float room = std::max(right - x - metaW - arrowSpan, ui::metrics::U(6));
    const auto textW = [](const std::string& s) { return ImGui::CalcTextSize(s.c_str()).x; };
    const auto leaf = [](const std::string& s) {
        const size_t dot = s.rfind('.');
        return dot == std::string::npos || dot + 1 >= s.size() ? s : s.substr(dot + 1);
    };
    std::string fromS = from, toS = arrow ? to : std::string();
    if (textW(fromS) + textW(toS) > room) {
        fromS = leaf(from);
        if (arrow) toS = leaf(to);
    }
    const float fromW = textW(fromS), toW = textW(toS);
    if (fromW + toW > room) {
        const std::string fromLeaf = fromS, toLeaf = toS;
        const float half = room * 0.5f;
        if (!arrow) fromS = ui::kit::elide(fromLeaf.c_str(), room);
        else if (fromW <= half) toS = ui::kit::elide(toLeaf.c_str(), room - fromW);
        else if (toW <= half) fromS = ui::kit::elide(fromLeaf.c_str(), room - toW);
        else {
            fromS = ui::kit::elide(fromLeaf.c_str(), half);
            toS = ui::kit::elide(toLeaf.c_str(), half);
        }
    }
    const bool namesShort = fromS != from || toS != (arrow ? to : std::string());
    dl->AddText(ImVec2(x, nameY), toneCol, fromS.c_str());
    x += ImGui::CalcTextSize(fromS.c_str()).x;
    if (arrow) {
        paintMsgArrow(dl, x + gap * 0.6f, x + gap * 0.6f + arrowW, cy, toneCol);
        x += arrowSpan;
        dl->AddText(ImVec2(x, nameY), toneCol, toS.c_str());
        x += ImGui::CalcTextSize(toS.c_str()).x;
    }
    ImGui::PopFont();
    if (!meta.empty()) dl->AddText(ImVec2(x + gap, cy - lineH * 0.5f), dimCol, meta.c_str());

    bool truncated = more > 0;
    for (size_t i = 0; i < shown; ++i) {
        const std::string s = ui::kit::elide(lines[i].c_str(), right - textX);
        truncated = truncated || s != lines[i];
        dl->AddText(ImVec2(textX, hy + step * (float)(i + 1)), bodyCol, s.c_str());
    }
    if (more) {
        char moreBuf[48];
        std::snprintf(moreBuf, sizeof(moreBuf), "\xe2\x80\xa6 +%zu more line%s", more,
                      more == 1 ? "" : "s");
        dl->AddText(ImVec2(textX, hy + step * (float)(shown + 1)), dimCol, moreBuf);
    }
    const float gutterW = ui::metrics::stroke(1.0f);
    if (shown)
        dl->AddLine(ImVec2(gutterX, hy + step),
                    ImVec2(gutterX, hy + step * (float)(shown + (more ? 1 : 0)) + lineH), dimCol,
                    gutterW);
    ui::kit::rowEnd();
    ImGui::PopFont();

    if (clicked) store->SetBool(openKey, open = !open);
    if (hovered) {
        std::string tip = namesShort ? from + (arrow ? " -> " + to : std::string()) : std::string();
        if (!open && truncated) tip += (tip.empty() ? std::string() : std::string("\n\n")) + body;
        const std::string when = chatTimeTooltip(a.eventTs);
        if (!when.empty()) tip += (tip.empty() ? std::string() : std::string("\n\n")) + when;
        if (!tip.empty()) ui::kit::tooltip(tip.c_str());
    }
    chatCopyContext(ImRect(p0, ImVec2(p0.x + w, p0.y + h)), "##agent_msg_copy", [&] {
        return from + (board || to.empty() ? std::string() : " -> " + to) + ": " + body;
    });
    if (open) {
        const float indent = textX - p0.x;
        ImGui::PushFont(fonts.body);
        ImGui::Indent(indent);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextUnformatted(body.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        ImGui::Unindent(indent);
        ImGui::PopFont();
        const ImRect r(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        ImGui::GetWindowDrawList()->AddLine(ImVec2(gutterX, r.Min.y), ImVec2(gutterX, r.Max.y),
                                            dimCol, gutterW);
        chatCopyContext(r, "##agent_msg_body_copy", [&] { return body; });
    }
}

// Compact duration for the usage line, like OMP's: "0.6s", "23m36s", "1h05m".
static std::string usageDuration(double seconds) {
    char buf[32];
    const long total = (long)(seconds + 0.5);
    if (total < 60) std::snprintf(buf, sizeof(buf), "%.1fs", seconds);
    else if (total < 3600) std::snprintf(buf, sizeof(buf), "%ldm%02lds", total / 60, total % 60);
    else std::snprintf(buf, sizeof(buf), "%ldh%02ldm", total / 3600, (total % 3600) / 60);
    return buf;
}

// Provider-request usage (rowKind 21; detail = {in, out, cache_read, cache_write, ttft_ms,
// duration_ms, elapsed_ms, model}, eventTs = request start): OMP's usage row as one dim line —
// clock, "+elapsed" since the prompt, tokens in (fresh + cache writes), out, prompt-cache reads,
// time to first token, output rate. A narrow column drops the clock, rate, ttft and elapsed
// (in that order) before the token figures. Hover adds the cache share and the model.
void drawUsageLine(const ActivityItem& a, const ui::Fonts& fonts) {
    int in = 0, out = 0, cacheRead = 0, cacheWrite = 0, ttftMs = 0, durMs = 0, elapsedMs = 0;
    std::string model;
    try {
        const auto& j = cardDetailJson(a);
        if (!j.is_object()) return;
        in = jint(j, "in");
        out = jint(j, "out");
        cacheRead = jint(j, "cache_read");
        cacheWrite = jint(j, "cache_write");
        ttftMs = jint(j, "ttft_ms");
        durMs = jint(j, "duration_ms");
        elapsedMs = jint(j, "elapsed_ms");
        model = jstr(j, "model");
    } catch (...) {
        return;
    }
    // keep: higher survives a narrow column longer; the token figures never drop.
    struct Part { std::string text; int keep; };
    std::vector<Part> parts;
    const std::string clock = chatClockSeconds(a.eventTs);
    if (!clock.empty()) parts.push_back({clock, 0});
    if (elapsedMs > 0) parts.push_back({"+" + usageDuration(elapsedMs / 1000.0), 3});
    parts.push_back({"in " + ui::kit::fmtCount(in + cacheWrite), 9});
    parts.push_back({"out " + ui::kit::fmtCount(out), 9});
    if (cacheRead > 0) parts.push_back({"cache " + ui::kit::fmtCount(cacheRead), 9});
    if (ttftMs > 0) parts.push_back({"ttft " + usageDuration(ttftMs / 1000.0), 2});
    if (durMs > 100 && out > 0) {
        char rate[32];
        std::snprintf(rate, sizeof(rate), "%.1f tok/s", out * 1000.0 / durMs);
        parts.push_back({rate, 1});
    }
    const auto join = [&parts] {
        std::string s;
        for (const Part& p : parts) {
            if (p.keep < 0) continue;
            if (!s.empty()) s += " \xc2\xb7 ";
            s += p.text;
        }
        return s;
    };
    const std::string line = join();

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap() * 0.5f));
    ImGui::PushFont(fonts.body);
    ImGui::Indent(chatProseInsetX());
    const float avail = ImGui::GetContentRegionAvail().x;
    std::string shownLine = line;
    while (ImGui::CalcTextSize(shownLine.c_str()).x > avail) {
        Part* drop = nullptr;
        for (Part& p : parts)
            if (p.keep >= 0 && p.keep < 9 && (!drop || p.keep < drop->keep)) drop = &p;
        if (!drop) break;
        drop->keep = -1;
        shownLine = join();
    }
    shownLine = chatEllipsize(shownLine, avail);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted(shownLine.c_str());
    ImGui::PopStyleColor();
    ImGui::Unindent(chatProseInsetX());
    ImGui::PopFont();
    if (ImGui::IsItemHovered()) {
        // Cache share of everything the request sent: what a prompt-cache miss shows up in.
        const long long prompt = (long long)in + cacheWrite + cacheRead;
        std::string tip = chatTimeTooltip(a.eventTs);
        if (prompt > 0) {
            char share[96];
            std::snprintf(share, sizeof(share), "%s%.0f%% of %s prompt tokens read from cache",
                          tip.empty() ? "" : "\n", cacheRead * 100.0 / (double)prompt,
                          ui::kit::fmtCount(prompt).c_str());
            tip += share;
        }
        if (cacheWrite > 0) tip += "\n" + ui::kit::fmtCount(cacheWrite) + " written to cache";
        if (!model.empty()) tip += "\n" + model;
        if (shownLine != line) tip = line + "\n" + tip;
        ui::kit::tooltip(tip.c_str());
    }
    chatCopyContext(ImRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()), "##usage_copy",
                    [line] { return line; });
}


// Delegation card — transparency view of subagent dispatches (rowKind 19).
// title="N subagents"; detail JSON {"delegates":[{role,brief,spawn_kind}]} upserted by
// feedTurnActivity as agent.spawn events land.
void drawDelegationCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##delegcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("team", 20);
    if (!iv.valid) iv = ui::getIcon("milestone_flag", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.95f;
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, semanticOnGlass(pal.accent, surf));
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "Delegating");
    ImGui::PopFont();
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(a.title.empty() ? "subagents" : a.title.c_str());
    ImGui::PopStyleColor();

    if (!a.detail.empty()) {
        try {
            const auto& j = cardDetailJson(a);
            if (j.is_object() && j.contains("delegates") && j["delegates"].is_array()) {
                for (const auto& d : j["delegates"]) {
                    if (!d.is_object()) continue;
                    std::string role, brief, skind;
                    if (d.contains("role") && d["role"].is_string())
                        role = d["role"].get<std::string>();
                    if (d.contains("brief") && d["brief"].is_string())
                        brief = d["brief"].get<std::string>();
                    if (d.contains("spawn_kind") && d["spawn_kind"].is_string())
                        skind = d["spawn_kind"].get<std::string>();
                    if (role.empty() && brief.empty()) continue;
                    std::string line = "\xE2\x80\xA2  " + (role.empty() ? "subagent" : role);
                    if (!skind.empty()) line += " (" + skind + ")";
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                    ImGui::TextUnformatted(line.c_str());
                    ImGui::PopStyleColor();
                    if (!brief.empty()) {
                        ImGui::Indent(12 * sc);
                        dimText(brief.c_str());
                        ImGui::Unindent(12 * sc);
                    }
                }
            }
        } catch (...) {}
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##delegcard WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    {
        chatCopyContext(cardRect, "##deleg_copy", [&] { return chatActivityCopyText(a); });
    }
}

// ============================ Exec-mode timeline cards (swarm / eng_firm / advisor) ============

// ============================ Exec-mode timeline cards (swarm / eng_firm / advisor) ============
// Formatted glass cards for contract kinds swarm_wave / firm_* / advisor / sub_run.
// detail holds structured JSON or plain thread text — NEVER dumped raw into the prose bubble.

void drawSwarmWaveCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##swarmwave", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("team", 20);
    if (!iv.valid) iv = ui::getIcon("milestone_flag", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, semanticOnGlass(pal.accent, surf));
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "%s",
                       a.title.empty() ? "Swarm wave" : a.title.c_str());
    ImGui::PopFont();

    // Task rows from packed detail.
    if (!a.detail.empty()) {
        try {
            const auto& j = cardDetailJson(a);
            if (j.is_object() && j.contains("tasks") && j["tasks"].is_array()) {
                for (const auto& t : j["tasks"]) {
                    if (!t.is_object()) continue;
                    std::string tt = jstr(t, "title");
                    std::string stt = jstr(t, "status");
                    if (tt.empty()) continue;
                    char line[256];
                    if (!stt.empty())
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %s", tt.c_str(), stt.c_str());
                    else
                        std::snprintf(line, sizeof(line), "  %s", tt.c_str());
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                    ImGui::TextUnformatted(line);
                    ImGui::PopStyleColor();
                }
            }
        } catch (...) {
            if (!a.reason.empty()) dimText(a.reason.c_str());
        }
    } else if (!a.reason.empty()) {
        dimText(a.reason.c_str());
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##swarmwave WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##swarm_wave_copy", [&] { return chatActivityCopyText(a); });
}

void drawFirmBidsCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##firmbids", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("team", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, semanticOnGlass(pal.accent, surf));
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "%s",
                       a.title.empty() ? "Firm bids" : a.title.c_str());
    ImGui::PopFont();
    if (!a.reason.empty()) {
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.reason.c_str());
        ImGui::PopStyleColor();
    }

    if (!a.detail.empty()) {
        try {
            const auto& j = cardDetailJson(a);
            if (j.is_object() && j.contains("bids") && j["bids"].is_array()) {
                for (const auto& b : j["bids"]) {
                    if (!b.is_object()) continue;
                    std::string firm = jstr(b, "firm");
                    if (firm.empty()) continue;
                    long tokens = 0;
                    if (b.contains("tokens") && b["tokens"].is_number_integer())
                        tokens = b["tokens"].get<long>();
                    else if (b.contains("tokens") && b["tokens"].is_number())
                        tokens = (long)b["tokens"].get<double>();
                    bool inScope = true;
                    if (b.contains("in_scope") && b["in_scope"].is_boolean())
                        inScope = b["in_scope"].get<bool>();
                    char line[256];
                    if (tokens > 0)
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %ld estimated tokens",
                                      firm.c_str(), tokens);
                    else
                        std::snprintf(line, sizeof(line), "  %s", firm.c_str());
                    ImVec4 col = inScope ? surf.textPrimary : surf.textTertiary;
                    ImGui::PushStyleColor(ImGuiCol_Text, col);
                    ImGui::TextUnformatted(line);
                    ImGui::PopStyleColor();
                    if (!inScope) {
                        ImGui::SameLine(0, 6 * sc);
                        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                        ImGui::TextUnformatted("out of scope");
                        ImGui::PopStyleColor();
                    }
                }
            }
            // Awarded firm chip when packed on the same event.
            std::string awarded = jstr(j, "awarded_firm");
            if (!awarded.empty()) {
                long awardedTokens = 0;
                if (j.contains("awarded_tokens") && j["awarded_tokens"].is_number_integer())
                    awardedTokens = j["awarded_tokens"].get<long>();
                else if (j.contains("awarded_tokens") && j["awarded_tokens"].is_number())
                    awardedTokens = (long)j["awarded_tokens"].get<double>();
                char aw[192];
                if (awardedTokens > 0)
                    std::snprintf(aw, sizeof(aw), "Awarded %s  \xc2\xb7 %ld estimated tokens",
                                  awarded.c_str(), awardedTokens);
                else
                    std::snprintf(aw, sizeof(aw), "Awarded %s", awarded.c_str());
                ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
                ImGui::PushStyleColor(ImGuiCol_Text, semanticOnGlass(pal.statusOk, surf));
                ImGui::TextUnformatted(aw);
                ImGui::PopStyleColor();
            }
        } catch (...) {}
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##firmbids WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    // Copy: human summary, never raw JSON.
    chatCopyContext(cardRect, "##firm_bids_copy", [&] {
    std::string copyTxt = a.title.empty() ? "Firm bids" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    try {
        const auto& j = cardDetailJson(a);
        if (j.is_object() && j.contains("bids") && j["bids"].is_array()) {
            for (const auto& b : j["bids"]) {
                if (!b.is_object()) continue;
                std::string firm = jstr(b, "firm");
                if (firm.empty()) continue;
                copyTxt += "\n- ";
                copyTxt += firm;
                if (b.contains("tokens") && b["tokens"].is_number()) {
                    char estimate[48];
                    std::snprintf(estimate, sizeof(estimate), " %ld estimated tokens",
                                  (long)b["tokens"].get<double>());
                    copyTxt += estimate;
                }
            }
        }
    } catch (...) {}
    return copyTxt;
    });
}

void drawFirmAwardCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##firmaward", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("poly_star", 20);
    if (!iv.valid) iv = ui::getIcon("milestone_flag", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.95f;
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1,
                     semanticOnGlass(pal.statusOk, surf));
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.statusOk, surf), "%s",
                       a.title.empty() ? "Firm awarded" : a.title.c_str());
    ImGui::PopFont();
    if (!a.reason.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextWrapped("%s", a.reason.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##firmaward WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##firm_award_copy", [&] { return chatActivityCopyText(a); });
}

void drawFirmTeamCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##firmteam", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("team", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, semanticOnGlass(pal.accent, surf));
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "%s",
                       a.title.empty() ? "Firm team" : a.title.c_str());
    ImGui::PopFont();
    if (!a.reason.empty()) {
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.reason.c_str());
        ImGui::PopStyleColor();
    }

    if (!a.detail.empty()) {
        try {
            const auto& j = cardDetailJson(a);
            if (j.is_object() && j.contains("members") && j["members"].is_array()) {
                for (const auto& mem : j["members"]) {
                    if (!mem.is_object()) continue;
                    std::string role = jstr(mem, "role");
                    std::string name = jstr(mem, "name");
                    std::string firm = jstr(mem, "firm");
                    bool lead = false;
                    if (mem.contains("lead") && mem["lead"].is_boolean())
                        lead = mem["lead"].get<bool>();
                    char line[256];
                    if (lead)
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %s  \xc2\xb7 lead",
                                      role.empty() ? "role" : role.c_str(),
                                      name.empty() ? (firm.empty() ? "?" : firm.c_str())
                                                   : name.c_str());
                    else if (!firm.empty() && firm != name)
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %s  \xc2\xb7 %s",
                                      role.empty() ? "role" : role.c_str(),
                                      name.empty() ? "?" : name.c_str(), firm.c_str());
                    else
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %s",
                                      role.empty() ? "role" : role.c_str(),
                                      name.empty() ? "?" : name.c_str());
                    ImGui::PushStyleColor(ImGuiCol_Text,
                                          lead ? semanticOnGlass(pal.accent, surf)
                                               : surf.textPrimary);
                    ImGui::TextUnformatted(line);
                    ImGui::PopStyleColor();
                }
            }
        } catch (...) {}
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##firmteam WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##firm_team_copy", [&] {
    std::string copyTxt = a.title.empty() ? "Firm team" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    try {
        const auto& j = cardDetailJson(a);
        if (j.is_object() && j.contains("members") && j["members"].is_array()) {
            for (const auto& mem : j["members"]) {
                if (!mem.is_object()) continue;
                copyTxt += "\n- ";
                copyTxt += jstr(mem, "role");
                std::string name = jstr(mem, "name");
                if (!name.empty()) { copyTxt += "  "; copyTxt += name; }
            }
        }
    } catch (...) {}
    return copyTxt;
    });
}

void drawFirmCommCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##firmcomm", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    // Header is the expander toggle — collapsed by default (a.expanded starts false).
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(surf.textSecondary, "%s",
                       a.title.empty() ? "Internal comms" : a.title.c_str());
    ImGui::PopFont();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) {
        a.expanded = !a.expanded;
        a.userToggled = true;
    }
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    if (!a.reason.empty()) {
        ImGui::TextUnformatted(a.reason.c_str());
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::TextUnformatted(a.expanded ? "(hide)" : "(show)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) {
        a.expanded = !a.expanded;
        a.userToggled = true;
    }

    if (a.expanded && !a.detail.empty()) {
        // Scrollable thread body — plain lines, not raw JSON.
        drawProseBox("##firmcommbody", a.detail, ui::metrics::copilotProseMaxH());
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##firmcomm WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##firm_comm_copy", [&] {
    std::string copyTxt = a.title.empty() ? "Internal comms" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    if (!a.detail.empty()) { copyTxt += "\n"; copyTxt += a.detail; }
    return copyTxt;
    });
    (void)pal;
}

void drawAdvisorCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const bool planApproval = (a.opsCount > 0);  // feed stamps opsCount=1 for plan-approval
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##advisor", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("poly_star", 20);
    if (!iv.valid) iv = ui::getIcon("milestone_flag", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.95f;
        ImVec4 tint = planApproval ? semanticOnGlass(pal.statusOk, surf)
                                   : semanticOnGlass(pal.accent, surf);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(planApproval ? semanticOnGlass(pal.statusOk, surf)
                                   : semanticOnGlass(pal.accent, surf),
                       "%s",
                       a.title.empty()
                           ? (planApproval ? "Advisor-approved plan" : "Advisor")
                           : a.title.c_str());
    ImGui::PopFont();
    // Toggle expand.
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) {
        a.expanded = !a.expanded;
        a.userToggled = true;
    }
    if (planApproval) {
        // Collapsed-by-default informational — verdict chip always visible.
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.reason.empty() ? "approved" : a.reason.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(a.expanded ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) {
            a.expanded = !a.expanded;
            a.userToggled = true;
        }
        if (a.expanded && !a.detail.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextWrapped("%s", a.detail.c_str());
            ImGui::PopStyleColor();
        }
        // No action buttons — informational only.
    } else {
        if (a.expanded) {
            if (!a.reason.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextWrapped("Q: %s", a.reason.c_str());
                ImGui::PopStyleColor();
            }
            if (!a.detail.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                ImGui::TextWrapped("%s", a.detail.c_str());
                ImGui::PopStyleColor();
            }
        } else if (!a.detail.empty()) {
            std::string prev = a.detail;
            if (prev.size() > 100) { prev.resize(97); prev += "\xe2\x80\xa6"; }
            dimText(prev.c_str());
        }
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##advisor WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##advisor_copy", [&] {
    std::string copyTxt = a.title.empty()
        ? (planApproval ? "Advisor-approved plan" : "Advisor")
        : a.title;
    if (!a.reason.empty()) {
        copyTxt += planApproval ? "\n" : "\nQ: ";
        copyTxt += a.reason;
    }
    if (!a.detail.empty()) { copyTxt += "\n"; copyTxt += a.detail; }
    return copyTxt;
    });
}

void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts, bool live,
                    const std::function<void()>& body) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##subrun", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ImVec4 statusCol = surf.textSecondary;
    if (a.status == "running") statusCol = semanticOnGlass(pal.accent, surf);
    else if (a.status == "done" || a.status == "pass")
        statusCol = semanticOnGlass(pal.statusOk, surf);
    else if (a.status == "fail" || a.status == "error")
        statusCol = semanticOnGlass(pal.statusErr, surf);
    else if (a.status == "retry")
        statusCol = semanticOnGlass(pal.statusWarn, surf);

    const std::string title = a.title.empty() ? std::string("Sub-run") : a.title;
    ImGui::PushFont(fonts.medium);
    if (live) {
        // Live work that emits nothing while it runs (a compaction summary takes minutes):
        // motion plus elapsed time is what tells it apart from a hung app.
        std::string say = title + "\xe2\x80\xa6";
        const double elapsed = a.startedTs > 0.0 ? chatNowEpoch() - a.startedTs : 0.0;
        if (elapsed >= 3.0) say += "  \xc2\xb7  " + ui::kit::fmtDuration(elapsed);
        chatShimmerText(say.c_str(), surf.textTertiary, statusCol, ui::anim::kBreathe);
    } else {
        ImGui::TextColored(statusCol, "%s", title.c_str());
    }
    ImGui::PopFont();
    if (!live && !a.reason.empty()) {
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(a.reason.c_str());
        ImGui::PopStyleColor();
    }

    // The sub-agent's own totals (OMP's AgentProgress, carried in the detail trailer): requests,
    // context against its window, spend and model. Main's context meter says nothing about these.
    const ui::copilot::SubRunUsage use = ui::copilot::subRunUsageFromDetail(a.detail);
    const std::string usageLine = ui::copilot::subRunUsageLine(
        use, use.model.empty() ? std::string() : ui::shortModelName(use.model));
    if (!usageLine.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(chatEllipsize(usageLine, ImGui::GetContentRegionAvail().x).c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            std::string tip = usageLine;
            if (use.tokens > 0)
                tip += "\nBilled volume: " + ui::copilot::subRunCount(use.tokens) +
                       " tokens (input + output + cache writes)";
            if (use.contextTokens > 0 && use.contextWindow > 0)
                tip += "\nContext: " + std::to_string(use.contextTokens) + " of " +
                       std::to_string(use.contextWindow) + " tokens";
            if (use.durationMs > 0) tip += "\nRun time: " + ui::kit::fmtDuration(use.durationMs / 1000.0);
            if (!use.model.empty()) tip += "\nModel: " + use.model;
            ImGui::SetTooltip("%s", tip.c_str());
        }
    }
    if (use.retrying && a.status == "running") {
        std::string tip = "The provider failed or rate-limited the sub-agent; it is waiting to retry.";
        if (use.retryMax > 0)
            tip += "\nAttempt " + std::to_string(use.retryAttempt) + " of " + std::to_string(use.retryMax);
        if (use.retryDelayMs > 0) tip += "\nRetrying in " + ui::kit::fmtDuration(use.retryDelayMs / 1000.0);
        if (!use.retryError.empty()) tip += "\n" + use.retryError;
        ui::kit::chip("waiting on provider retry", ui::kit::Tone::Warn, true, tip.c_str());
    }

    // What the card owns (a sub-agent's calls) hangs under the title. Its own right-click copy
    // stops above it: a nested row has a copy menu of its own.
    float bodyTopY = -1.0f;
    if (body) {
        bodyTopY = ImGui::GetCursorScreenPos().y;
        body();
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##subrun WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    if (bodyTopY >= 0.0f) cardRect.Max.y = ImMin(cardRect.Max.y, bodyTopY);
    chatCopyContext(cardRect, "##sub_run_copy", [&] { return chatActivityCopyText(a); });
}



// ============================ AI-VC (agent version control) ============================
// Silent, deterministic checkpoints of AI-authored work. The chat transcript offers no restore
// control; the AI workspace's checkpoint strip (ai/plan_board.cpp) lists the plm_timeline rows
// itself and routes its Restore button through aiVcRollback (plm_rollback by checkpoint id):
//   plm_rollback {doc_id?, to: checkpoint id} -> {ok, ...} (geometry only; the chat is untouched)
// Degrade: when the worker reports unknown_method the affordances hide permanently this session.

AiVcUi s_aiVc;

static std::string aiVcActiveDocId(AppState& st) {
    if (st.docMirror.hasDoc()) return st.docMirror.docId();
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        return st.docTabs[st.activeTab].docId;
    return {};
}

// Protocol-2 / contract envelopes that mean "this RPC isn't registered yet".
static bool aiVcIsAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented" ||
            c == "unknown" || c.find("unknown") != std::string::npos)
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented" || reason == "no_vault")
            return true;
    }
    return false;
}

static bool aiVcOk(const core::RpcResult& r, std::string& hint) {
    if (aiVcIsAbsent(r)) {
        s_aiVc.available = false;
        hint = "AI checkpoints unavailable";
        return false;
    }
    if (!r.ok) {
        hint = r.err.empty() ? "the service did not respond" : r.err;
        return false;
    }
    if (r.result.is_object() && !r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        hint = r.result.value("hint", reason.empty() ? std::string("that could not be done")
                                                     : reason);
        return false;
    }
    return true;
}

static bool aiVcDocStillActive(const AppState& st, const std::string& docId) {
    if (docId.empty() || !st.docMirror.hasDoc() ||
        st.docMirror.docId() != docId) return false;
    if (st.activeTab < 0) return true;
    return st.activeTab < (int)st.docTabs.size() &&
           st.docTabs[st.activeTab].docId == docId;
}

static void aiVcRefreshMirror(AppState& st, const nlohmann::json& res,
                              const std::string& docId) {
    if (!aiVcDocStillActive(st, docId)) return;
    // Prefer an ops-shaped envelope (revision/tree/tess); otherwise resync + rebuild mesh.
    if (res.is_object() && (res.contains("revision") || res.contains("tree") ||
                            res.contains("ops"))) {
        st.docMirror.ingestOpsResponse(res.contains("ops") && res["ops"].is_object()
                                           ? res["ops"] : res);
        std::string aicm;
        if (res.contains("aicm_path") && res["aicm_path"].is_string())
            aicm = res["aicm_path"].get<std::string>();
        if (aicm.empty() && res.contains("tess") && res["tess"].is_object())
            aicm = res["tess"].value("aicm_path", std::string());
        if (aicm.empty() && res.contains("ops") && res["ops"].is_object())
            aicm = res["ops"].value("aicm_path", std::string());
        if (!aicm.empty()) st.loadDocMeshAsync(aicm);
        return;
    }
    if (!st.docMirror.hasDoc()) return;
    const std::string activeDocId = docId;
    st.docMirror.resync(st.worker);
    nlohmann::json p;
    p["doc_id"] = docId;
    p["origin"] = "system";
    st.rpc.call("doc_activate", p, [&st, activeDocId](const core::RpcResult& r) {
        if (!aiVcDocStillActive(st, activeDocId)) return;
        if (!r.ok || !r.result.is_object()) return;
        std::string aicm = r.result.value("aicm_path", std::string());
        if (aicm.empty() && r.result.contains("tess") && r.result["tess"].is_object())
            aicm = r.result["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) st.loadDocMeshAsync(aicm);
        st.docMirror.resync(st.worker);
    });
}

// Restore the active document to checkpoint `to` (a plm_timeline id). Geometry only: the chat
// transcript is never touched. A live run blocks it, so the agent never edits under a rollback.
void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast) {
    if (!s_aiVc.available || s_aiVc.busy || to.empty()) return;
    if (activeConversationRunning(st)) {
        st.toast(ui::ToastLevel::Info, kAvailableAfterRun);
        return;
    }
    s_aiVc.busy = true;
    nlohmann::json p = nlohmann::json::object();
    const std::string docId = aiVcActiveDocId(st);
    if (!docId.empty()) p["doc_id"] = docId;
    p["to"] = to;
    st.rpc.call("plm_rollback", p, [&st, okToast, docId](const core::RpcResult& r) {
        s_aiVc.busy = false;
        std::string hint;
        if (!aiVcOk(r, hint)) {
            if (s_aiVc.available) st.toast(ui::ToastLevel::Error, hint);
            return;
        }
        aiVcRefreshMirror(st, r.result, docId);
        const std::string label = okToast.empty() ? "Restored checkpoint" : okToast;
        st.toast(ui::ToastLevel::Success,
                 docId.empty() || aiVcDocStillActive(st, docId)
                     ? label : label + " for the original document");
    });
}

// Rounded header control (optional leading icon), submitted in TWO phases on purpose.
//
// A tab strip paints "which tab is selected", and ImGui reports a click only AFTER the widget
// has been submitted — so a one-pass button paints its highlight from state the click is about
// to invalidate. On the release frame the tab you just picked drew UNSELECTED and the old tab
// drew selected, one frame before the switch: the selection visibly flicked back to Chat and
// then jumped to the tab you asked for. Submitting every hit zone first, applying the click,
// and only then painting all of them from the resolved selection removes the whole class.
HeaderBtnHit headerTextButtonHit(const char* id, float w) {
    HeaderBtnHit h;
    ImGui::PushID(id);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float bh = ImGui::GetFrameHeight();
    // InvisibleButton retains keyboard navigation without Selectable's square hover fill.
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::kit::chipRound());
    h.clicked = ImGui::InvisibleButton("##hit", ImVec2(w, bh), ImGuiButtonFlags_EnableNav);
    ImGui::PopStyleVar();
    h.id = ImGui::GetItemID();
    ImGuiContext& g = *GImGui;
    IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), id, GImGui->LastItemData.StatusFlags);
    h.hovered = ImGui::IsItemHovered();
    h.held    = ImGui::IsItemActive();
    h.rect    = ImRect(p0, ImVec2(p0.x + w, p0.y + bh));
    ImGui::PopID();
    return h;
}

void headerTextButtonPaint(const HeaderBtnHit& h, const char* label, bool active,
                           const char* iconName, bool withLabel, bool outlined,
                           bool ownActiveFill) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 txt = surf.textPrimary;
    const float round = ui::kit::chipRound();
    const float iconSz = std::min(ui::metrics::iconHeader(),
                                  h.rect.GetHeight() - ui::metrics::U(1.0f));
    const float iconGap = ui::metrics::U(1.0f);
    const bool haveIcon = iconName && iconName[0];

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float hover = ui::anim::toward(ImHashStr("##header_hover", 0, h.id),
                                         h.hovered ? 1.0f : 0.0f, ui::anim::kFast);
    // A tab strip that paints its own sliding selection (ownActiveFill=false) hands the active
    // tab's face to that indicator; this button then only carries the ink and the hover wash.
    const bool faceActive = active && ownActiveFill;
    if (faceActive || outlined || hover > 0.0f || h.held) {
        ImVec4 fill = faceActive ? semanticOnGlass(ui::palette.accent, surf) : txt;
        fill.w = faceActive ? 0.12f + 0.05f * hover
                            : (outlined ? 0.035f : 0.0f) + 0.065f * hover;
        if (h.held) fill.w += 0.06f;
        ImVec4 edge = faceActive ? semanticOnGlass(ui::palette.accent, surf) : txt;
        edge.w = faceActive ? 0.42f : (outlined ? 0.16f : 0.0f) + 0.08f * hover;
        if (!active || ownActiveFill)
            ui::controlFace(dl, h.rect.Min, h.rect.Max, ImGui::GetColorU32(fill),
                             ImGui::GetColorU32(edge), round, h.held ? 0.0f : 1.0f);
    }

    // withLabel=false is the narrow-column pill: the icon centres in the box on its own and
    // the tooltip carries the name. Never squeeze the label in — draw-list text is not clipped
    // by the pill, so it would spill across its neighbours.
    const float midY = (h.rect.Min.y + h.rect.Max.y) * 0.5f;
    const float labelW = withLabel ? ImGui::CalcTextSize(label).x : 0.0f;
    const float contentW = (haveIcon ? iconSz + (withLabel ? iconGap : 0.0f) : 0.0f) + labelW;
    float x = (h.rect.Min.x + h.rect.Max.x - contentW) * 0.5f;
    if (haveIcon) {
        ImVec4 tint = surf.textPrimary;
        if (!active && !h.hovered) tint.w *= 0.9f;
        ui::drawIconGlass(dl, iconName, ImVec2(x + iconSz * 0.5f, midY), iconSz,
                          ImGui::GetColorU32(tint), 0);
        x += iconSz + iconGap;
    }
    if (!withLabel) return;
    // Ink eases between the resting and the lit tone instead of flipping with the selection.
    const float lit = ui::anim::toward(ImHashStr("##header_ink", 0, h.id),
                                       active || h.hovered ? 1.0f : 0.0f, ui::anim::kFast);
    const ImVec4 rest = surf.textSecondary, on = surf.textPrimary;
    const ImU32 ink = ImGui::GetColorU32(ImVec4(
        rest.x + (on.x - rest.x) * lit, rest.y + (on.y - rest.y) * lit,
        rest.z + (on.z - rest.z) * lit, rest.w + (on.w - rest.w) * lit));
    dl->PushClipRect(h.rect.Min, h.rect.Max, true);
    dl->AddText(ImVec2(x, midY - ImGui::GetTextLineHeight() * 0.5f), ink, label);
    dl->PopClipRect();
}

}  // namespace ui
