// Copilot cards (T-RF6 split of legacy_chat.cpp): the Plan/Objectives section (tiles + todos
// tree + subagents, plus the drawPlanSection adapter declared in
// core/app_state.h), the clarify + plan-approve cards, the corporate-org cards
// (aicad.corporate/1), the exec-mode cards (swarm waves / eng-firm bids+award+team+comms /
// advisor / sub-run), and the AI-VC (agent version control) checkpoint timeline + turn
// restore/edit-stage UI. Event folding for these cards lives in feed.cpp; the per-turn
// timeline that hosts them lives in activity_timeline.cpp.

#include "copilot_internal.h"
#include "ui/copilot/inline_image_refs.h"
#include "ui/chudcode_session_actions.h"  // chudcode_rewind_session + per-conversation link
#include <algorithm>
#include <iterator>
#include "util/time_local.h"

// ============================ Plan / Objectives section ============================
// Multi-shape plan display (build_plan | checklist | phased_todo | objectives | dag).
// Tiles show brief titles; full purpose/dims/assignment lives in ImGui hover tooltips.
// Todos tree rows use the shared vector status glyph (drawStatusGlyph); subagents list is
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
        // Match the C++ plan_state producer (core/agent/events.cpp: id = bare part name,
        // phase omitted) so multi-writer upsert-by-id REPLACES the C++ items instead of
        // appending duplicate rows under a second phase (the "I. Tasks" + "II. Parts" bug).
        it.id = !p.name.empty() ? p.name : ("part:" + std::to_string(i));
        it.title = p.name.empty() ? ("Part " + std::to_string(i + 1)) : p.name;
        it.kind = "part";
        // Leave status empty (activity heuristic / C++ writer owns live status — a stale
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
static std::string planItemStatusFromActivity(const PlanItem& it,
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

// Old text status glyphs are absent from the UI font; rows use drawStatusGlyph vectors.

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
    std::string shown = title;
    auto textW = [&](const std::string& s) {
        return f->CalcTextSizeA(fs, FLT_MAX, 0.0f, s.c_str()).x;
    };
    if (textW(shown) > textMax) {
        static const char* kEll = "\xe2\x80\xa6";
        const float ellW = textW(kEll);
        while (!shown.empty() && textW(shown) + ellW > textMax) shown.pop_back();
        while (!shown.empty() && (shown.back() & 0xC0) == 0x80) shown.pop_back();  // UTF-8 boundary
        if (!shown.empty()) shown += kEll;
    }

    // Glyph inside the tile (restore cursor for drawStatusGlyph, then restore after).
    ImVec2 saved = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(ImVec2(p0.x + padX, p0.y + (h - glyph) * 0.5f));
    drawStatusGlyph(status, glyph);
    ImU32 tcol = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
    // Clip title to tile width.
    dl->PushClipRect(p0, ImVec2(p0.x + w - padX, p0.y + h), true);
    dl->AddText(f, fs, ImVec2(p0.x + padX + glyph + gap, p0.y + (h - tsz.y) * 0.5f),
                tcol, shown.c_str());
    dl->PopClipRect();
    ImGui::SetCursorScreenPos(saved);

    // Re-assert the FULL TILE as the last ImGui item: drawStatusGlyph leaves a tiny
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
        const std::string st = planItemStatusFromActivity(it, activity);
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
        // Phase "touched" if any item is non-pending.
        bool touched = false;
        for (size_t ii : idxs) {
            std::string st = planItemStatusFromActivity(plan.items[ii], activity);
            if (st != "pending" && !st.empty()) touched = true;
        }
        // Always expand the first phase and any touched phase; collapse pure-pending tails.
        bool expand = (pi == 0) || touched || phases.size() == 1;

        ImGui::PushID((int)pi);
        if (multi) {
            char hdr[96];
            const char* rom = planRomanNumeral((int)pi + 1);
            if (rom)
                std::snprintf(hdr, sizeof(hdr), "%s. %s", rom, ph.c_str());
            else
                std::snprintf(hdr, sizeof(hdr), "%s", ph.c_str());
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::PushFont(fonts.medium);
            if (!expand) {
                // One-line collapsible summary for untouched phases.
                // NoTreePushOnOpen: we never TreePop — avoids ID/indent stack imbalance.
                char sum[128];
                std::snprintf(sum, sizeof(sum), "%s  ·  %d task%s",
                              hdr, (int)idxs.size(), idxs.size() == 1 ? "" : "s");
                const ImGuiTreeNodeFlags flags =
                    ImGuiTreeNodeFlags_SpanAvailWidth | ImGuiTreeNodeFlags_NoTreePushOnOpen;
                if (ImGui::TreeNodeEx("##ph", flags, "%s", sum))
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
            std::string st = planItemStatusFromActivity(it, activity);
            ImGui::PushID((int)ii);
            // Indent tree rows slightly under multi-phase headers.
            if (multi) {
                ImGui::Dummy(ImVec2(8.0f * sc, 0));
                ImGui::SameLine(0, 0);
            }
            // Use the same vector status glyph as action cards and tiles.
            drawStatusGlyph(st, ImGui::GetTextLineHeight());
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
        std::string st = planItemStatusFromActivity(it, activity);
        ImGui::PushID((int)i);
        drawStatusGlyph(st, ImGui::GetTextLineHeight());
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
    struct AgRow { std::string name, status, brief; };
    std::vector<AgRow> rows;
    for (const auto& it : plan.items) {
        if (it.kind != "subagent") continue;
        AgRow r;
        r.name = it.title;
        r.status = planItemStatusFromActivity(it, activity);
        r.brief = it.desc;
        rows.push_back(std::move(r));
    }
    // Also surface mission headers + corporate worker rows when plan has no subagent items.
    if (rows.empty()) {
        for (const auto& a : activity) {
            if (a.missionHeader) {
                AgRow r;
                r.name = a.title.empty() ? a.stage : a.title;
                r.status = a.status.empty() ? "pending" : a.status;
                r.brief = a.reason.empty() ? a.detail : a.reason;
                rows.push_back(std::move(r));
            } else if (a.rowKind >= 4 && a.rowKind <= 8 && a.stage.rfind("msg:", 0) != 0) {
                AgRow r;
                r.name = a.title.empty() ? "agent" : a.title;
                r.status = a.status.empty() ? "done" : a.status;
                r.brief = a.reason.empty() ? a.detail : a.reason;
                rows.push_back(std::move(r));
            }
        }
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
        const AgRow& r = rows[i];
        ImGui::PushID((int)i);
        drawStatusGlyph(r.status, ImGui::GetTextLineHeight());
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
    for (const auto& it : plan.items) {
        if (it.kind == "subagent" || it.kind == "objective") continue;
        ++total;
        const std::string st = planItemStatusFromActivity(it, activity);
        if (st == "done" || st == "pass") ++done;
        else if (st == "running" && current.empty())
            current = it.title.empty() ? it.id : it.title;
    }
}

// One-line live summary: "3/8 done · now: extrude base" (or the tail when nothing runs).
std::string planProgressLine(const PlanState& plan,
                                    const std::vector<ActivityItem>& activity) {
    int d = 0, t = 0; std::string cur;
    planProgress(plan, activity, d, t, cur);
    std::string out;
    if (t > 0) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%d/%d", d, t);
        out = buf;
    }
    if (!cur.empty()) { if (!out.empty()) out += " \xc2\xb7 "; out += "now: "; out += cur; }
    return out;
}

void drawPlanSection(const PlanState& plan, const std::vector<ActivityItem>& activity,
                     const ui::Fonts& fonts) {
    if (!plan.present) return;
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
        const std::string prog = planProgressLine(plan, activity);
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
    {
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
        chatCopyContext(planRect, "##plan_copy", copyTxt);
    }
}


namespace ui {

// The cards that wait on the USER (clarify, plan approval, ask_user) share one language with the
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

void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);
    GlassBubble gb = beginUserActionCard();
    ImGui::BeginChild("##clarify", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    const ImVec4 fieldBg = surf.darkText ? ImVec4(1, 1, 1, 0.50f) : ImVec4(0, 0, 0, 0.38f);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, fieldBg);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
                          ImVec4(fieldBg.x, fieldBg.y, fieldBg.z, fieldBg.w + 0.10f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
                          ImVec4(fieldBg.x, fieldBg.y, fieldBg.z, fieldBg.w + 0.16f));
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "A few details to get this right");
    ImGui::PopFont();
    dimText("Accept the suggested values or change them, then build. Skip uses the defaults.");

    // Drain context-cube picks into an armed geometry_select question — once they have resolved,
    // so the answer carries the stable sid rather than the provisional pick key.
    if (m.clarify.geometryPickQ >= 0 &&
        m.clarify.geometryPickQ < (int)m.clarify.questions.size() &&
        !st.pendingContextRefs.empty() &&
        std::none_of(st.pendingContextRefs.begin(), st.pendingContextRefs.end(),
                     [](const core::ContextRef& r) { return r.resolving; })) {
        ClarifyQ& gq = m.clarify.questions[(size_t)m.clarify.geometryPickQ];
        if (gq.kind == "geometry_select") {
            auto targetOk = [&](const std::string& k) {
                if (gq.targets.empty()) return true;
                for (const auto& t : gq.targets) if (t == k) return true;
                return false;
            };
            for (const auto& r : st.pendingContextRefs) {
                if (!targetOk(r.kind)) continue;
                if (!gq.multi) gq.selectedValues.clear();
                bool already = false;
                for (const auto& s : gq.selectedValues)
                    if (s == r.id) { already = true; break; }
                if (!already) gq.selectedValues.push_back(r.id);
                gq.selectedKind = r.kind;
                // Keep answer buffer in sync for Build-it fold.
                std::string joined;
                for (size_t i = 0; i < gq.selectedValues.size(); ++i) {
                    if (i) joined += ", ";
                    joined += gq.selectedValues[i];
                }
                std::strncpy(gq.answer.data(), joined.c_str(), gq.answer.size() - 1);
                gq.answer[gq.answer.size() - 1] = '\0';
                if (!gq.multi) break;
            }
            st.pendingContextRefs.clear();
            if (!gq.multi) {
                st.contextPickActive = false;
                gq.pickArmed = false;
                m.clarify.geometryPickQ = -1;
            }
        }
    }

    for (size_t i = 0; i < m.clarify.questions.size(); ++i) {
        ClarifyQ& q = m.clarify.questions[i];
        ImGui::PushID((int)i);
        std::string lbl = q.ask;
        if (!q.unit.empty()) lbl += "  (" + q.unit + ")";
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
        ImGui::TextWrapped("%s", lbl.c_str());

        if (q.kind == "choice" && !q.options.empty()) {
            if (q.multi) {
                // Multi-select: checkbox list.
                for (size_t oi = 0; oi < q.options.size(); ++oi) {
                    ImGui::PushID((int)oi);
                    const auto& opt = q.options[oi];
                    bool on = std::find(q.selectedValues.begin(), q.selectedValues.end(),
                                        opt.first) != q.selectedValues.end();
                    std::string lab = opt.second.empty() ? opt.first : opt.second;
                    if (ImGui::Checkbox(lab.c_str(), &on)) {
                        if (on) {
                            if (std::find(q.selectedValues.begin(), q.selectedValues.end(),
                                          opt.first) == q.selectedValues.end())
                                q.selectedValues.push_back(opt.first);
                        } else {
                            q.selectedValues.erase(
                                std::remove(q.selectedValues.begin(), q.selectedValues.end(),
                                            opt.first),
                                q.selectedValues.end());
                        }
                        std::string joined;
                        for (size_t j = 0; j < q.selectedValues.size(); ++j) {
                            if (j) joined += ", ";
                            joined += q.selectedValues[j];
                        }
                        std::strncpy(q.answer.data(), joined.c_str(), q.answer.size() - 1);
                        q.answer[q.answer.size() - 1] = '\0';
                    }
                    ImGui::PopID();
                }
            } else {
                // Single-select: radio buttons.
                int cur = -1;
                for (size_t oi = 0; oi < q.options.size(); ++oi) {
                    if (std::string(q.answer.data()) == q.options[oi].first ||
                        (!q.selectedValues.empty() &&
                         q.selectedValues[0] == q.options[oi].first))
                        cur = (int)oi;
                }
                for (size_t oi = 0; oi < q.options.size(); ++oi) {
                    ImGui::PushID((int)oi);
                    const auto& opt = q.options[oi];
                    std::string lab = opt.second.empty() ? opt.first : opt.second;
                    if (ImGui::RadioButton(lab.c_str(), cur == (int)oi)) {
                        cur = (int)oi;
                        std::strncpy(q.answer.data(), opt.first.c_str(), q.answer.size() - 1);
                        q.answer[q.answer.size() - 1] = '\0';
                        q.selectedValues = {opt.first};
                    }
                    ImGui::PopID();
                }
            }
        } else if (q.kind == "approval") {
            bool yes = std::string(q.answer.data()) == "true" ||
                       std::string(q.answer.data()) == "yes" ||
                       std::string(q.answer.data()) == "1" ||
                       q.suggested == "true" || q.suggested == "yes";
            // Seed once from suggested when empty.
            if (q.answer[0] == '\0') {
                yes = (q.suggested == "true" || q.suggested == "1" || q.suggested == "yes");
                std::strncpy(q.answer.data(), yes ? "true" : "false", q.answer.size() - 1);
            }
            if (ImGui::RadioButton("Approve", yes)) {
                std::strncpy(q.answer.data(), "true", q.answer.size() - 1);
            }
            ImGui::SameLine();
            if (ImGui::RadioButton("Reject", !yes)) {
                std::strncpy(q.answer.data(), "false", q.answer.size() - 1);
            }
        } else if (q.kind == "geometry_select") {
            if (!q.selectedValues.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                for (const auto& id : q.selectedValues) {
                    ImGui::Bullet();
                    ImGui::SameLine();
                    ImGui::TextUnformatted(id.c_str());
                }
                ImGui::PopStyleColor();
            } else {
                dimText(q.suggested.empty() ? "No pick yet — click Pick on model."
                                            : q.suggested.c_str());
            }
            const bool armed = q.pickArmed && m.clarify.geometryPickQ == (int)i;
            if (ImGui::Button(armed ? "Picking… (click model)" : "Pick on model")) {
                // Disarm others.
                for (auto& oq : m.clarify.questions) oq.pickArmed = false;
                q.pickArmed = true;
                m.clarify.geometryPickQ = (int)i;
                st.contextPickActive = true;
            }
            if (armed) {
                ImGui::SameLine();
                if (ImGui::Button("Done")) {
                    q.pickArmed = false;
                    m.clarify.geometryPickQ = -1;
                    st.contextPickActive = false;
                }
            }
        } else {
            // text / dimension / unknown — free text field
            drawChatInput("##cq", q.answer,
                          q.suggested.empty() ? "value" : q.suggested.c_str(), 2);
        }
        ImGui::PopID();
    }
    if (!m.clarify.assumptions.empty()) {
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted("Assuming:");
        ImGui::PopStyleColor();
        for (const auto& a : m.clarify.assumptions) {
            ImGui::Bullet();
            ImGui::SameLine();
            dimText(a.c_str());
        }
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    bool build = primaryButton("Build it", ImVec2(-1, ui::metrics::copilotBtnH()));
    bool skip = ImGui::Button("Skip - use defaults", ImVec2(-1, 0));
    ImGui::PopStyleColor(5);   // Text + FrameBg/Hovered/Active + TextDisabled
    ImGui::EndChild();
    endUserActionCard(gb);   // pops the ##clarify WindowPadding
    if (build || skip) {
        // Fold each answer (or its suggested default on Skip) into the request.
        std::string aug = m.clarify.request;
        for (auto& q : m.clarify.questions) {
            std::string val;
            if (q.kind == "choice" && q.multi) {
                if (!q.selectedValues.empty()) {
                    for (size_t j = 0; j < q.selectedValues.size(); ++j) {
                        if (j) val += ", ";
                        val += q.selectedValues[j];
                    }
                }
            } else {
                val = std::string(q.answer.data());
                if (val.empty() && !q.selectedValues.empty()) {
                    for (size_t j = 0; j < q.selectedValues.size(); ++j) {
                        if (j) val += ", ";
                        val += q.selectedValues[j];
                    }
                }
            }
            if (val.empty()) val = q.suggested;
            std::string nm = q.key.empty() ? q.ask : q.key;
            aug += "\n- " + nm + ": " + val + (q.unit.empty() ? "" : " " + q.unit);
            if (q.kind == "geometry_select" && !q.selectedKind.empty())
                aug += " (" + q.selectedKind + ")";
        }
        for (const auto& a : m.clarify.assumptions) aug += "\n- (assumed) " + a;
        m.clarify.active = false;
        m.clarify.questions.clear();
        m.clarify.assumptions.clear();
        m.clarify.geometryPickQ = -1;
        st.contextPickActive = false;
        m.running = true;
        if (st.settings.model[0]) m.model = st.settings.model;
        m.liveStatus = "Building...";
        nlohmann::json buildExtra = nlohmann::json::object();
        buildExtra["options"] = {{"mode", "agent"}, {"advisor", true}};
        launchAgentTurn(st, aug, buildExtra);
    }
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
    opts["corporate_mode"] = st.settings.corporateMode;
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
    kit::statusGlyphInline(answered || b.status == AskStatus::Reply ? kit::Status::Done
                                                                    : kit::Status::Skipped);
    ImGui::SameLine();
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

    std::string copy = askResolvedTitle(b.status);
    const float gap = ui::metrics::U(2.0f);
    for (size_t i = 0; i < b.questions.size(); ++i) {
        const AskQuestion& q = b.questions[i];
        const std::string prompt = q.prompt.empty() ? q.id : q.prompt;
        copy += "\n- " + prompt;
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
        copy += ": " + ans;
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
        copy += "\nYour reply: " + b.reply;
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
    chatCopyContext(rect, "##ask_copy", copy);
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
    dimText("Answer here and submit, or just reply in the chat. The agent waits for you.");

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
    if (!st.workerStarted || runId.empty()) {
        b.status = AskStatus::Expired;
        b.error = "No live run is waiting for these answers.";
        m.uiCachedH = -1.0f;
        markChatHistoryDirty();
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

// ============================ Corporate Roles timeline cards ============================
// Compact glass cards for org chart / meetings / steering; muted one-liners for route rejections
// and wind-down. Colors only via ui::palette + semanticOnGlass. Right-click Copy via chatCopyContext.


void drawCorpOrgCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##corporg", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("team", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint = semanticOnGlass(pal.accent, surf);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "%s",
                       a.title.empty() ? "Corporate org" : a.title.c_str());
    ImGui::PopFont();
    if (!a.reason.empty()) {
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.reason.c_str());
        ImGui::PopStyleColor();
    }

    nlohmann::json chart = corpParseChart(a);
    std::string dir = jstr(chart, "director");
    if (!dir.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::Text("Director  %s", corpShortId(dir).c_str());
        ImGui::PopStyleColor();
    }
    if (chart.contains("departments") && chart["departments"].is_array()) {
        for (const auto& d : chart["departments"]) {
            if (!d.is_object()) continue;
            std::string name = corpDeptNameOf(d);
            std::string head = corpDeptHead(d);
            std::string lead = corpDeptLead(d);
            int wc = corpWorkerCount(d);
            if (wc == 0 && d.contains("worker_ids") && d["worker_ids"].is_array())
                wc = (int)d["worker_ids"].size();
            char line[256];
            if (!lead.empty()) {
                std::snprintf(line, sizeof(line),
                              "  %s  \xc2\xb7 head %s  \xc2\xb7 lead %s  \xc2\xb7 %d worker%s",
                              name.c_str(),
                              corpShortId(head).c_str(),
                              corpShortId(lead).c_str(),
                              wc, wc == 1 ? "" : "s");
            } else {
                std::snprintf(line, sizeof(line),
                              "  %s  \xc2\xb7 head %s  \xc2\xb7 %d worker%s",
                              name.c_str(),
                              head.empty() ? "\xe2\x80\x94" : corpShortId(head).c_str(),
                              wc, wc == 1 ? "" : "s");
            }
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextUnformatted(line);
            ImGui::PopStyleColor();
        }
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##corporg WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    {
        // Prefer human summary over the JSON detail stash.
        std::string copyTxt = a.title.empty() ? "Corporate org" : a.title;
        if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
        if (!dir.empty()) { copyTxt += "\nDirector  "; copyTxt += dir; }
        if (chart.contains("departments") && chart["departments"].is_array()) {
            for (const auto& d : chart["departments"]) {
                if (!d.is_object()) continue;
                copyTxt += "\n- ";
                copyTxt += corpDeptNameOf(d);
                std::string head = corpDeptHead(d);
                if (!head.empty()) { copyTxt += "  head="; copyTxt += head; }
                std::string lead = corpDeptLead(d);
                if (!lead.empty()) { copyTxt += "  lead="; copyTxt += lead; }
                int wc = corpWorkerCount(d);
                if (wc == 0 && d.contains("worker_ids") && d["worker_ids"].is_array())
                    wc = (int)d["worker_ids"].size();
                copyTxt += "  workers=";
                copyTxt += std::to_string(wc);
            }
        }
        chatCopyContext(cardRect, "##corp_org_copy", copyTxt);

    }
}

void drawCorpMeetingCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const bool blocked = a.opsCount > 0 || a.status == "warn";

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##corpmeet", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("milestone_flag", 20);
    if (!iv.valid) iv = ui::getIcon("team", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint = blocked ? semanticOnGlass(pal.statusWarn, surf)
                              : semanticOnGlass(pal.accent, surf);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(blocked ? semanticOnGlass(pal.statusWarn, surf) : surf.textPrimary,
                       "%s", a.title.empty() ? "Meeting" : a.title.c_str());
    ImGui::PopFont();

    // Collapsible body: counts always visible; risks + item lists when expanded.
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted(a.expanded ? "(hide)" : "(details)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) a.expanded = !a.expanded;

    // First line of reason = counts.
    std::string counts = a.reason;
    std::string risks;
    size_t nl = counts.find('\n');
    if (nl != std::string::npos) {
        risks = counts.substr(nl + 1);
        counts.resize(nl);
    }
    if (!counts.empty()) dimText(counts.c_str());
    if (!risks.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              blocked ? semanticOnGlass(pal.statusWarn, surf) : surf.textSecondary);
        ImGui::TextWrapped("%s", risks.c_str());
        ImGui::PopStyleColor();
    }

    if (a.expanded && !a.detail.empty()) {
        try {
            auto dig = nlohmann::json::parse(a.detail);
            auto listLine = [&](const char* label, const char* key) {
                if (!dig.contains(key) || !dig[key].is_array() || dig[key].empty()) return;
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextUnformatted(label);
                ImGui::PopStyleColor();
                for (const auto& it : dig[key]) {
                    if (!it.is_string()) continue;
                    ImGui::Bullet();
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                    ImGui::TextWrapped("%s", it.get<std::string>().c_str());
                    ImGui::PopStyleColor();
                }
            };
            listLine("Done", "done_items");
            listLine("In progress", "in_progress_items");
            listLine("Blocked", "blocked_items");
        } catch (...) {
            // detail may be non-JSON — ignore expand body
        }
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##corpmeet WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    if (blocked) {
        ImGui::GetWindowDrawList()->AddRect(
            cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusWarn),
            ui::metrics::rounding(8.0f), 0, ui::metrics::stroke(1.5f));
    }
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##corp_meet_copy", copyTxt);

    }
}

void drawCorpSteeringCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##corpsteer", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("poly_star", 20);
    if (!iv.valid) iv = ui::getIcon("milestone_flag", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.95f;
        ImVec4 tint = semanticOnGlass(pal.accent, surf);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "%s",
                       a.title.empty() ? "Director steering" : a.title.c_str());
    ImGui::PopFont();
    const std::string& note = !a.reason.empty() ? a.reason : a.detail;
    if (!note.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextWrapped("%s", note.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##corpsteer WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##corp_steer_copy", copyTxt);

    }
}

// Inter-agent message (rowKind 7, stage "msg:<channel>:<id>"; title = from, reason = to,
// detail = body): ONE compact line — reply hook, channel tag, "from > to", the body's first
// line — that expands to the whole body on click. Vector marks only: the font has no arrows.
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

static void drawAgentMessageLine(const ActivityItem& a, const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const size_t chEnd = a.stage.find(':', 4);
    const std::string channel = chEnd == std::string::npos ? std::string("irc")
                                                           : a.stage.substr(4, chEnd - 4);
    const bool board = channel == "board";
    const ui::kit::Tone tone = channel == "pool" ? ui::kit::Tone::Info
                             : board             ? ui::kit::Tone::Neutral
                                                 : ui::kit::Tone::Accent;
    const char* tag = channel == "pool" ? "Pool" : board ? "Board" : "IRC";
    const std::string& from = a.title;
    const std::string& to   = a.reason;
    const std::string& body = a.detail;
    const size_t nl = body.find('\n');
    const std::string first = nl == std::string::npos ? body : body.substr(0, nl);

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    ImGuiStorage* store = ImGui::GetStateStorage();
    const ImGuiID openKey = ImGui::GetID(("##msg_open" + a.stage).c_str());
    bool open = store->GetBool(openKey, false);

    ImGui::PushFont(fonts.body);
    const float lineH = ImGui::GetTextLineHeight();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float h = lineH + ui::kit::padY() * 2.0f;
    const bool clicked = ui::kit::rowBegin(a.stage.c_str(), h, open);
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 toneCol = ImGui::ColorConvertFloat4ToU32(ui::kit::toneOn(tone, surf));
    const ImU32 nameCol = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    const ImU32 bodyCol = ImGui::ColorConvertFloat4ToU32(surf.textTertiary);
    const float gap = ui::metrics::U(1.5f);
    const float cy = p0.y + h * 0.5f;
    const float right = p0.x + w - ui::kit::padX();
    float x = p0.x + ui::kit::padX();

    paintMsgHook(dl, ImVec2(x, cy - lineH * 0.5f), lineH, toneCol);
    x += lineH + gap * 0.5f;
    ImGui::PushFont(fonts.medium);
    dl->AddText(ImVec2(x, cy - ImGui::GetTextLineHeight() * 0.5f), toneCol, tag);
    x += ImGui::CalcTextSize(tag).x + gap;
    // Names share at most half the line so the body always shows.
    const float nameMax = std::max((right - x) * 0.5f, ui::metrics::U(6));
    const std::string fromS = ui::kit::elide(from.c_str(), board ? nameMax : nameMax * 0.5f);
    dl->AddText(ImVec2(x, cy - ImGui::GetTextLineHeight() * 0.5f), nameCol, fromS.c_str());
    x += ImGui::CalcTextSize(fromS.c_str()).x;
    if (!board && !to.empty()) {
        const float aw = lineH * 0.7f;
        paintMsgArrow(dl, x + gap * 0.6f, x + gap * 0.6f + aw, cy, nameCol);
        x += aw + gap * 1.2f;
        const std::string toS = ui::kit::elide(to.c_str(), nameMax * 0.5f);
        dl->AddText(ImVec2(x, cy - ImGui::GetTextLineHeight() * 0.5f), nameCol, toS.c_str());
        x += ImGui::CalcTextSize(toS.c_str()).x;
    }
    ImGui::PopFont();
    x += gap;
    std::string firstS;
    if (!open && x < right) {
        firstS = ui::kit::elide(first.c_str(), right - x);
        dl->AddText(ImVec2(x, cy - lineH * 0.5f), bodyCol, firstS.c_str());
    }
    ui::kit::rowEnd();
    ImGui::PopFont();

    if (clicked) store->SetBool(openKey, open = !open);
    if (hovered && !open && (firstS != first || nl != std::string::npos))
        ui::kit::tooltip(body.c_str());
    const std::string copy = from + (board || to.empty() ? std::string() : " -> " + to) + ": " + body;
    chatCopyContext(ImRect(p0, ImVec2(p0.x + w, p0.y + h)), "##agent_msg_copy", copy);
    if (open) {
        ImGui::PushFont(fonts.body);
        ImGui::Indent(lineH + gap * 0.5f + ui::kit::padX());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextUnformatted(body.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        ImGui::Unindent(lineH + gap * 0.5f + ui::kit::padX());
        ImGui::PopFont();
        const ImRect r(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        chatCopyContext(r, "##agent_msg_body_copy", body);
    }
}

// Muted single-line activity (route rejection / wind-down summary), or an inter-agent message.
void drawCorpMutedLine(const ActivityItem& a, const ui::Fonts& fonts) {
    if (a.rowKind == 7 && a.stage.rfind("msg:", 0) == 0) {
        drawAgentMessageLine(a, fonts);
        return;
    }
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    const std::string& line = !a.reason.empty() ? a.reason
                            : !a.title.empty()  ? a.title
                                                : a.detail;
    if (line.empty()) return;
    ImGui::PushFont(fonts.body);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextWrapped("%s", line.c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    {
        const ImRect r(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        chatCopyContext(r, "##corp_mute_copy", line);

    }
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
            auto j = nlohmann::json::parse(a.detail);
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
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##deleg_copy", copyTxt);
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
            auto j = nlohmann::json::parse(a.detail);
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
    chatCopyContext(cardRect, "##swarm_wave_copy", chatActivityCopyText(a));
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
            auto j = nlohmann::json::parse(a.detail);
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
    std::string copyTxt = a.title.empty() ? "Firm bids" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    try {
        auto j = nlohmann::json::parse(a.detail);
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
    chatCopyContext(cardRect, "##firm_bids_copy", copyTxt);
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
    chatCopyContext(cardRect, "##firm_award_copy", chatActivityCopyText(a));
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
            auto j = nlohmann::json::parse(a.detail);
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
    std::string copyTxt = a.title.empty() ? "Firm team" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    try {
        auto j = nlohmann::json::parse(a.detail);
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
    chatCopyContext(cardRect, "##firm_team_copy", copyTxt);
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
    std::string copyTxt = a.title.empty() ? "Internal comms" : a.title;
    if (!a.reason.empty()) { copyTxt += "\n"; copyTxt += a.reason; }
    if (!a.detail.empty()) { copyTxt += "\n"; copyTxt += a.detail; }
    chatCopyContext(cardRect, "##firm_comm_copy", copyTxt);
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
    std::string copyTxt = a.title.empty()
        ? (planApproval ? "Advisor-approved plan" : "Advisor")
        : a.title;
    if (!a.reason.empty()) {
        copyTxt += planApproval ? "\n" : "\nQ: ";
        copyTxt += a.reason;
    }
    if (!a.detail.empty()) { copyTxt += "\n"; copyTxt += a.detail; }
    chatCopyContext(cardRect, "##advisor_copy", copyTxt);
}

void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts, bool live) {
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

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##subrun WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##sub_run_copy", chatActivityCopyText(a));
}



// ============================ AI-VC (agent version control) ============================
// Silent, deterministic checkpoints of AI-authored work. Worker RPCs (AiVcCore / CheckpointsCore):
//   plm_timeline {doc_id?} -> {ok, checkpoints:[{id, turn, when|time, label, features|features_count,
//                                                 kind?, msg_idx?, call?:{turn,i}}]}
//   plm_rollback {doc_id?, to: id|"turn:N"|"-N"|"msg:<idx>"|"call:<turn>:<i>"} ->
//     {ok, ..., chat_truncate_to?}
//   plm_rollback_check {doc_id?, to, file_path?, chat_size?} -> non-destructive
//     preflight for the confirm dialog: {ok, discarded_checkpoints, discarded_turns,
//     discarded_messages, geometry_rollback, chat_truncate_to,
//     file:{path, provenance_known, externally_modified, recorded_mtime, current_mtime}}
// Degrade: when the worker reports unknown_method the affordances hide permanently this session.
// When v2 fields (kind/msg_idx/call) are absent, UI stays at turn-level Before/After only.


AiVcUi s_aiVc;

// Staged "Edit message" — rollback is deferred until the user actually sends. Esc restores
// the pre-edit chat tail + composer without any plm_rollback.
EditStage s_edit;

static std::string aiVcActiveDocId(AppState& st) {
    if (st.docMirror.hasDoc()) return st.docMirror.docId();
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        return st.docTabs[st.activeTab].docId;
    return {};
}

// Did this finished assistant turn mutate geometry? Used to gate the restore affordance.
bool chatMsgMutatedDoc(const ChatMsg& m) {
    if (m.fromUser || m.running) return false;
    for (const auto& a : m.activity) {
        if (a.rowKind == 3) return true;   // tool call row
        if (a.opsCount > 0) return true;
    }
    return false;
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

// True when a msg:<idx> rollback failed only because no checkpoint exists for that
// message (backend raises "no checkpoint for msg N" -> reason "not_found"), or because
// no document is open at all ("invalid_item") — in both cases a CHAT-only rewind is
// still meaningful and must proceed locally. Never true for method-absent envelopes.
static bool aiVcIsNoCheckpoint(const core::RpcResult& r) {
    if (!r.ok || !r.result.is_object()) return false;
    const std::string reason = r.result.value("reason", std::string());
    return reason == "not_found" || reason == "invalid_item";
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

// Move msgs[truncateTo..) of convId into a new ChatBranch (persisted with the session, so the
// discarded tail stays recoverable) and truncate. No-op when nothing lies past truncateTo.
// False only when the conversation no longer exists.
static bool parkConversationTail(AppState& st, const std::string& convId, int truncateTo,
                                 const std::string& label) {
    std::vector<ChatMsg>* msgs = conversationMsgs(st, convId);
    if (!msgs) return false;
    if (truncateTo < 0) truncateTo = 0;
    if (truncateTo >= (int)msgs->size()) return true;
    auto conv = std::find_if(st.conversations.begin(), st.conversations.end(),
                             [&convId](const Conversation& c) { return c.id == convId; });
    if (conv == st.conversations.end()) return false;
    ChatBranch br;
    br.id = "b" + std::to_string(conv->branches.size() + 1);
    br.when = (double)std::time(nullptr);
    br.fromMsgIdx = truncateTo;
    br.label = label;
    br.msgs.assign(std::make_move_iterator(msgs->begin() + truncateTo),
                   std::make_move_iterator(msgs->end()));
    for (auto& m : br.msgs) m.uiCachedH = -1.0f;
    conv->branches.push_back(std::move(br));
    msgs->erase(msgs->begin() + truncateTo, msgs->end());
    for (auto& m : *msgs) m.uiCachedH = -1.0f;
    markChatHistoryDirty();
    return true;
}

static bool aiVcApplyChatTruncate(AppState& st, const std::string& convId,
                                  const nlohmann::json& res) {
    // Prefer explicit chat_truncate_to (msg index); fall back to msg:N targets only.
    int truncateTo = -1;
    if (res.is_object() && res.contains("chat_truncate_to") &&
        !res["chat_truncate_to"].is_null()) {
        if (res["chat_truncate_to"].is_number_integer())
            truncateTo = res["chat_truncate_to"].get<int>();
        else if (res["chat_truncate_to"].is_number())
            truncateTo = (int)res["chat_truncate_to"].get<double>();
    }
    if (truncateTo < 0) return false;
    std::vector<ChatMsg>* msgs = conversationMsgs(st, convId);
    if (!msgs) return false;
    if (truncateTo > (int)msgs->size()) truncateTo = (int)msgs->size();
    return parkConversationTail(
        st, convId, truncateTo,
        "reverted to message " + std::to_string(truncateTo) + " \xe2\x80\x94 " +
            std::to_string((int)msgs->size() - truncateTo) + " message(s) parked");
}

void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast) {
    if (!s_aiVc.available || s_aiVc.busy || to.empty()) return;
    // A rewind truncates the chat when it lands: never start one under a live turn, whose
    // running reply the truncation would erase. New turns wait on s_aiVc.busy (composer).
    if (activeConversationRunning(st) || editStageHoldsConversation(st.activeConvId())) {
        st.toast(ui::ToastLevel::Info, editStageHoldsConversation(st.activeConvId())
                                           ? "Finish or cancel the message edit first."
                                           : kAvailableAfterRun);
        return;
    }
    s_aiVc.busy = true;
    s_aiVc.confirmMsgIdx = -1;
    s_aiVc.confirmKind = 0;
    s_aiVc.confirmCallI = -1;
    nlohmann::json p = nlohmann::json::object();
    const std::string docId = aiVcActiveDocId(st);
    if (!docId.empty()) p["doc_id"] = docId;
    p["to"] = to;
    // Capture msg:N target so we can always clear chat below on success even when the
    // backend omits chat_truncate_to (confirmed user-msg rewind must never leave a tail).
    int msgTruncateTo = -1;
    if (to.rfind("msg:", 0) == 0) {
        try { msgTruncateTo = std::stoi(to.substr(4)); } catch (...) { msgTruncateTo = -1; }
    }
    const std::string convId = st.activeConvId();
    st.rpc.call("plm_rollback", p,
                [&st, okToast, msgTruncateTo, convId, docId](const core::RpcResult& r) {
        s_aiVc.busy = false;
        std::string hint;
        if (!aiVcOk(r, hint)) {
            // Chat-only rewind: the backend has no geometry checkpoint for this message
            // (or no document is open), so plm_rollback refuses — but the user asked to
            // rewind the CHAT to this point, which is meaningful on its own. Truncate
            // locally and say plainly that no geometry was rolled back.
            if (s_aiVc.available && msgTruncateTo >= 0 && aiVcIsNoCheckpoint(r)) {
                nlohmann::json synth = {{"chat_truncate_to", msgTruncateTo}};
                const bool chatFound = aiVcApplyChatTruncate(st, convId, synth);
                st.toast(chatFound ? ui::ToastLevel::Info : ui::ToastLevel::Error,
                         chatFound ? "No geometry checkpoint at that point — rewound the "
                                     "original chat only; the part was left unchanged."
                                   : "No geometry checkpoint; original chat no longer exists.");
                s_aiVc.lastFetch = 0.0;
                return;
            }
            if (s_aiVc.available) st.toast(ui::ToastLevel::Error, hint);
            return;
        }
        aiVcRefreshMirror(st, r.result, docId);
        // Prefer backend chat_truncate_to; if absent and this was a msg:N rewind, synthesize.
        const bool hadTruncate = r.result.is_object() &&
                                 r.result.contains("chat_truncate_to") &&
                                 !r.result["chat_truncate_to"].is_null();
        bool chatFound;
        if (hadTruncate) {
            chatFound = aiVcApplyChatTruncate(st, convId, r.result);
        } else if (msgTruncateTo >= 0) {
            nlohmann::json synth = {{"chat_truncate_to", msgTruncateTo}};
            chatFound = aiVcApplyChatTruncate(st, convId, synth);
        } else {
            chatFound = conversationMsgs(st, convId) != nullptr;
        }
        if (chatFound) {
            const std::string label = okToast.empty() ? "Restored checkpoint" : okToast;
            const bool sameConversation = st.activeConvId() == convId;
            const bool sameDocument = docId.empty() || aiVcDocStillActive(st, docId);
            st.toast(ui::ToastLevel::Success,
                     sameConversation && sameDocument
                         ? label : label + " for the original conversation/document");
        } else {
            st.toast(ui::ToastLevel::Warn,
                     "Checkpoint restored; original conversation no longer exists.");
        }
        s_aiVc.lastFetch = 0.0;  // force timeline refresh next open
    });
}

// ---- pre-rollback confirmation (the warning ladder, ONE dialog) ------------

static std::string aiVcActiveDocFilePath(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        return st.docTabs[st.activeTab].path;
    return {};
}

static std::string aiVcFmtMtime(double epoch) {
    if (epoch <= 0.0) return {};
    std::time_t t = (std::time_t)epoch;
    std::tm tmv;
    if (!chudware::util::localTime(t, tmv)) return {};
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d",
                  tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
                  tmv.tm_hour, tmv.tm_min);
    return buf;
}

void aiVcRequestRollback(AppState& st, const std::string& to, const std::string& okToast) {
    if (!s_aiVc.available || s_aiVc.busy || s_aiVc.confirm.busy || to.empty()) return;
    s_aiVc.confirm = AiVcUi::Confirm{};
    s_aiVc.confirm.busy = true;
    s_aiVc.confirm.to = to;
    s_aiVc.confirm.toast = okToast;
    nlohmann::json p = nlohmann::json::object();
    const std::string docId = aiVcActiveDocId(st);
    if (!docId.empty()) p["doc_id"] = docId;
    p["to"] = to;
    const std::string fp = aiVcActiveDocFilePath(st);
    if (!fp.empty()) p["file_path"] = fp;
    const int chatSize = (int)st.chat.size();
    const std::string convId = st.activeConvId();
    p["chat_size"] = chatSize;
    st.rpc.call("plm_rollback_check", p,
                [&st, to, convId, docId, chatSize](const core::RpcResult& r) {
        auto& c = s_aiVc.confirm;
        if (c.to != to) return;   // user armed a different target meanwhile
        c.busy = false;
        if (st.activeConvId() != convId ||
            (docId.empty() ? !aiVcActiveDocId(st).empty()
                           : !aiVcDocStillActive(st, docId))) {
            c = AiVcUi::Confirm{};
            st.toast(ui::ToastLevel::Info,
                     "Restore check cancelled after switching conversation or document.");
            return;
        }
        // Chat-only rewind: no geometry checkpoint at that point. Still confirm,
        // with wording that never claims geometry was restored.
        if (aiVcIsNoCheckpoint(r) && to.rfind("msg:", 0) == 0) {
            c.ok = true;
            c.notFound = true;
            // Chat-only rewind discards the tail after the target message.
            try { c.discardedMessages =
                    std::max(0, chatSize - std::stoi(to.substr(4))); }
            catch (...) { c.discardedMessages = -1; }
            c.geometryRollback = false;
            c.open = true;
            return;
        }
        std::string hint;
        if (!aiVcOk(r, hint)) {
            if (!s_aiVc.available) return;  // RPC absent — chrome hidden this session
            c.ok = false;
            c.err = hint;
            c.open = true;
            return;
        }
        const nlohmann::json& j = r.result;
        c.ok = true;
        c.cpLabel = j.value("checkpoint_label", std::string());
        c.cpWhen = j.value("checkpoint_when", std::string());
        if (j.contains("discarded_checkpoints") && j["discarded_checkpoints"].is_number())
            c.discardedCheckpoints = j["discarded_checkpoints"].get<int>();
        if (j.contains("discarded_turns") && j["discarded_turns"].is_number())
            c.discardedTurns = j["discarded_turns"].get<int>();
        if (j.contains("discarded_messages") && j["discarded_messages"].is_number())
            c.discardedMessages = j["discarded_messages"].get<int>();
        c.geometryRollback = j.value("geometry_rollback", false);
        if (j.contains("file") && j["file"].is_object()) {
            const auto& f = j["file"];
            c.filePath = f.value("path", std::string());
            c.provenanceKnown = f.value("provenance_known", false);
            if (f.contains("externally_modified") && f["externally_modified"].is_boolean())
                c.externallyModified = f["externally_modified"].get<bool>() ? 1 : 0;
            if (f.contains("recorded_mtime") && f["recorded_mtime"].is_number())
                c.recordedMtime = f["recorded_mtime"].get<double>();
            if (f.contains("current_mtime") && f["current_mtime"].is_number())
                c.currentMtime = f["current_mtime"].get<double>();
        }
        c.open = true;
    });
}

void drawAiVcRollbackConfirm(AppState& st) {
    auto& c = s_aiVc.confirm;
    if (c.open) {
        ImGui::OpenPopup("Restore to an earlier point?###ai_vc_confirm");
        c.open = false;
    }
    if (c.busy) {
        // Lightweight in-flight note; the dialog itself appears when data lands.
        return;
    }
    ImGui::SetNextWindowSizeConstraints(ImVec2(ui::metrics::px(360.0f), 0.0f),
                                        ImVec2(ui::metrics::px(520.0f), FLT_MAX));
    if (!ImGui::BeginPopupModal("Restore to an earlier point?###ai_vc_confirm",
                                nullptr, ImGuiWindowFlags_AlwaysAutoResize))
        return;
    const ui::AdaptiveSurface& surf = chatSurf();

    if (!c.ok) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("That could not be checked: %s",
                           c.err.empty() ? "the service did not respond" : c.err.c_str());
        ImGui::PopStyleColor();
        if (ImGui::Button("Close", ImVec2(ui::metrics::px(120.0f), 0))) {
            c = AiVcUi::Confirm{};
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
        return;
    }

    // ---- target line -------------------------------------------------------
    if (!c.notFound && (!c.cpLabel.empty() || !c.cpWhen.empty())) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        std::string tgt = c.cpLabel;
        if (!c.cpWhen.empty()) {
            if (!tgt.empty()) tgt += "  \xc2\xb7  ";
            tgt += c.cpWhen;
        }
        ImGui::TextWrapped("Restore to: %s", tgt.c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
    }

    // ---- rung 1 (always): what is discarded --------------------------------
    {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        std::string line;
        if (c.discardedMessages > 0)
            line += std::to_string(c.discardedMessages) + " chat message" +
                    (c.discardedMessages == 1 ? "" : "s");
        if (c.discardedTurns > 0) {
            if (!line.empty()) line += " and ";
            line += std::to_string(c.discardedTurns) + " turn" +
                    (c.discardedTurns == 1 ? "" : "s");
        }
        if (line.empty()) line = "Nothing after this point";
        // Not "permanently discard": the tail is parked on this session as a revert
        // branch and persists across restarts, so the wording must not overstate the loss.
        ImGui::TextWrapped("This will rewind past %s. They are kept on this session as a "
                           "revert branch, not deleted.", line.c_str());
        ImGui::PopStyleColor();
    }

    // ---- chat-only case (no geometry checkpoint at that point) -------------
    if (c.notFound) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("No geometry checkpoint exists at that point \xe2\x80\x94 only the "
                           "chat will be rewound; the part stays unchanged.");
        ImGui::PopStyleColor();
    }

    // ---- rung 2: geometry rolls back ---------------------------------------
    if (c.geometryRollback) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextWrapped("The part's geometry will be rolled back to that point; every "
                           "geometry change made since will be lost.");
        ImGui::PopStyleColor();
    }

    // ---- rung 3: backing file modified OUTSIDE this session (headline) ------
    if (c.externallyModified == 1 && !c.filePath.empty()) {
        ImGui::Spacing();
        ImGui::Separator();
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.errSystem[0], ui::palette.errSystem[1], ui::palette.errSystem[2], 1.0f));
        std::string when = aiVcFmtMtime(c.currentMtime);
        std::string msg = "Warning: " + c.filePath +
                          " was modified outside this session";
        if (!when.empty()) msg += " (last change " + when + ")";
        msg += ". Restoring will overwrite those external changes.";
        ImGui::TextWrapped("%s", msg.c_str());
        ImGui::PopStyleColor();
    } else if (!c.provenanceKnown && !c.filePath.empty()) {
        // Older checkpoint: no recorded hash — never claim the file is unmodified.
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("This checkpoint predates file tracking: the provenance of %s "
                           "is unknown \xe2\x80\x94 it may have been modified outside this "
                           "session.", c.filePath.c_str());
        ImGui::PopStyleColor();
    } else if (c.externallyModified == -1 && c.provenanceKnown) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s could not be re-checked (missing or unreadable) \xe2\x80\x94 "
                           "it may have changed outside this session.",
                           c.filePath.empty() ? "The file" : c.filePath.c_str());
        ImGui::PopStyleColor();
    }

    // ---- buttons ------------------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    const char* confirmLabel = c.notFound ? "Rewind chat only" : "Restore";
    const float bw = ui::metrics::px(140.0f);
    if (s_aiVc.busy) ImGui::BeginDisabled();
    if (c.externallyModified == 1) {
        // Destructive + external changes: the confirm button wears the danger color.
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(ui::palette.errSystem[0], ui::palette.errSystem[1], ui::palette.errSystem[2], 0.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(ui::palette.errSystem[0], ui::palette.errSystem[1], ui::palette.errSystem[2], 1.0f));
    }
    if (ImGui::Button(confirmLabel, ImVec2(bw, 0))) {
        const std::string to = c.to, toast = c.toast;
        c = AiVcUi::Confirm{};
        ImGui::CloseCurrentPopup();
        aiVcRollback(st, to, toast);
    }
    if (c.externallyModified == 1) ImGui::PopStyleColor(2);
    if (s_aiVc.busy) ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(bw, 0))) {
        c = AiVcUi::Confirm{};
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
}

static void aiVcFetchTimeline(AppState& st, bool force = false) {
    if (!s_aiVc.available || s_aiVc.loading) return;
    const std::string docId = aiVcActiveDocId(st);
    const double now = ImGui::GetTime();
    if (!force && (now - s_aiVc.lastFetch) < 1.5 && s_aiVc.lastDocId == docId &&
        !s_aiVc.checkpoints.empty())
        return;
    s_aiVc.loading = true;
    s_aiVc.lastFetch = now;
    s_aiVc.lastDocId = docId;
    nlohmann::json p = nlohmann::json::object();
    if (!docId.empty()) p["doc_id"] = docId;
    st.rpc.call("plm_timeline", p, [](const core::RpcResult& r) {
        s_aiVc.loading = false;
        std::string hint;
        if (!aiVcOk(r, hint)) {
            s_aiVc.err = s_aiVc.available ? hint : std::string();
            if (!s_aiVc.available) s_aiVc.checkpoints.clear();
            return;
        }
        s_aiVc.err.clear();
        s_aiVc.checkpoints.clear();
        s_aiVc.hasMsgGranularity = false;
        s_aiVc.hasCallGranularity = false;
        auto it = r.result.find("checkpoints");
        if (it == r.result.end() || !it->is_array()) return;
        for (const auto& c : *it) {
            if (!c.is_object()) continue;
            AiVcCheckpoint cp;
            cp.id = c.value("id", std::string());
            if (c.contains("turn") && !c["turn"].is_null()) {
                if (c["turn"].is_number_integer()) cp.turn = c["turn"].get<int>();
                else if (c["turn"].is_number()) cp.turn = (int)c["turn"].get<double>();
                else if (c["turn"].is_string()) {
                    try { cp.turn = std::stoi(c["turn"].get<std::string>()); }
                    catch (...) { cp.turn = -1; }
                }
            }
            // A nullable key MUST NOT go through nlohmann's value(): it throws type_error.302
            // when the key exists and holds null, and the default argument is evaluated eagerly
            // besides. plm_timeline emits null session_id (39 of 846 rows on this machine),
            // null msg_idx and null parent, and that throw escaped into the rpc callback:
            //   [async_rpc] callback for 'plm_timeline' threw: type must be string, but is null
            // which abandoned the whole checkpoint list mid-parse, so the rollback popover
            // silently showed a truncated history. The turn/msg_idx guards below already knew
            // this; session_id, when, label and kind did not.
            auto js = [](const nlohmann::json& o, const char* k) -> std::string {
                auto it = o.find(k);
                if (it == o.end() || !it->is_string()) return std::string();
                return it->get<std::string>();
            };
            cp.when = js(c, "when");
            if (cp.when.empty()) cp.when = js(c, "time");
            cp.label = js(c, "label");
            if (c.contains("features") && c["features"].is_number())
                cp.features = c["features"].get<int>();
            else if (c.contains("features_count") && c["features_count"].is_number())
                cp.features = c["features_count"].get<int>();
            // v2: kind / msg_idx / call:{turn,i}
            cp.kind = js(c, "kind");
            cp.sessionId = js(c, "session_id");
            if (c.contains("msg_idx") && !c["msg_idx"].is_null()) {
                if (c["msg_idx"].is_number_integer()) cp.msgIdx = c["msg_idx"].get<int>();
                else if (c["msg_idx"].is_number()) cp.msgIdx = (int)c["msg_idx"].get<double>();
                if (cp.msgIdx >= 0) s_aiVc.hasMsgGranularity = true;
            }
            if (c.contains("call") && c["call"].is_object()) {
                const auto& call = c["call"];
                if (call.contains("turn") && call["turn"].is_number())
                    cp.callTurn = call["turn"].is_number_integer()
                                      ? call["turn"].get<int>()
                                      : (int)call["turn"].get<double>();
                if (call.contains("i") && call["i"].is_number())
                    cp.callI = call["i"].is_number_integer()
                                   ? call["i"].get<int>()
                                   : (int)call["i"].get<double>();
                if (cp.callTurn >= 0 && cp.callI >= 0) s_aiVc.hasCallGranularity = true;
            }
            s_aiVc.checkpoints.push_back(std::move(cp));
        }
    });
}

// Compact "14:32" or short date from ISO-ish `when`.
static std::string aiVcNiceTime(const std::string& when) {
    if (when.size() >= 16 && when[10] == 'T')  // 2026-07-14T14:32:…
        return when.substr(11, 5);
    if (when.size() >= 16) return when.substr(0, 16);
    return when;
}

// Draw the compact AI checkpoints popover body (glass rows + Restore).
static void drawAiVcTimelinePopover(AppState& st) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted("AI checkpoints");
    ImGui::PopStyleColor();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted("Restore the part to a prior AI step.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));

    if (s_aiVc.loading && s_aiVc.checkpoints.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted("Loading\xe2\x80\xa6");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_aiVc.err.empty() && s_aiVc.checkpoints.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", s_aiVc.err.c_str());
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry")) aiVcFetchTimeline(st, /*force=*/true);
        return;
    }
    if (s_aiVc.checkpoints.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("No AI checkpoints yet \xe2\x80\x94 they appear after the copilot "
                           "edits this part.");
        ImGui::PopStyleColor();
        return;
    }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = ui::metrics::cpMenuRowPadX();
    const float padY = ui::metrics::cpMenuRowPadY();
    const float rowH = ImGui::GetTextLineHeight() * 2.0f + padY * 2.0f + 2.0f * sc;
    const float restW = ImGui::CalcTextSize("Restore").x + ImGui::GetStyle().FramePadding.x * 2.0f;

    // Content-driven height, capped: the fixed 280 px box left a slab of empty glass under one
    // or two checkpoints, and AlwaysVerticalScrollbar put the last rectangular ImGui bar in a
    // pane that draws the pill scrollbar everywhere else.
    const float rowPitch = rowH + ui::metrics::cpMenuRowGap();
    const float listH =
        std::max(ui::metrics::px(96.0f),
                 std::min(ui::metrics::px(280.0f),
                          (float)s_aiVc.checkpoints.size() * rowPitch +
                              ImGui::GetStyle().WindowPadding.y * 2.0f));
    ImGui::BeginChild("##ai_vc_list", ImVec2(ui::metrics::px(320.0f), listH),
                      ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    for (int i = 0; i < (int)s_aiVc.checkpoints.size(); ++i) {
        const AiVcCheckpoint& cp = s_aiVc.checkpoints[(size_t)i];
        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        ImGui::InvisibleButton("##cprow", ImVec2(std::max(1.0f, rowW), rowH));
        const bool hov = ImGui::IsItemHovered();
        ui::glassRowBackground(dl, rowMin, rowMax, ui::metrics::cpMenuRowRound(), (i % 2) == 1, false,
                               hov ? 0.42f : 0.28f);

        char title[160];
        if (cp.kind == "prompt" && !cp.label.empty())
            std::snprintf(title, sizeof(title), "Prompt \xc2\xb7 %s", cp.label.c_str());
        else if (cp.kind == "tool" && !cp.label.empty())
            std::snprintf(title, sizeof(title), "Tool \xc2\xb7 %s", cp.label.c_str());
        else if (cp.turn >= 0 && !cp.label.empty())
            std::snprintf(title, sizeof(title), "Turn %d \xc2\xb7 %s", cp.turn, cp.label.c_str());
        else if (cp.turn >= 0)
            std::snprintf(title, sizeof(title), "Turn %d", cp.turn);
        else if (!cp.label.empty())
            std::snprintf(title, sizeof(title), "%s", cp.label.c_str());
        else
            std::snprintf(title, sizeof(title), "Checkpoint");

        char sub[96];
        {
            std::string t = aiVcNiceTime(cp.when);
            if (cp.features > 0 && !t.empty())
                std::snprintf(sub, sizeof(sub), "%s  \xc2\xb7  %d feature%s",
                              t.c_str(), cp.features, cp.features == 1 ? "" : "s");
            else if (cp.features > 0)
                std::snprintf(sub, sizeof(sub), "%d feature%s",
                              cp.features, cp.features == 1 ? "" : "s");
            else if (!t.empty())
                std::snprintf(sub, sizeof(sub), "%s", t.c_str());
            else {
                const std::string shortId =
                    cp.id.size() > 10 ? cp.id.substr(0, 10) + "\xe2\x80\xa6" : cp.id;
                std::snprintf(sub, sizeof(sub), "%s", shortId.c_str());
            }
        }

        dl->AddText(ImVec2(rowMin.x + padX, rowMin.y + padY),
                    ImGui::ColorConvertFloat4ToU32(surf.textPrimary), title);
        dl->AddText(ImVec2(rowMin.x + padX,
                           rowMin.y + padY + ImGui::GetTextLineHeight() + 2.0f * sc),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub);

        if (!cp.id.empty()) {
            ImGui::SetCursorScreenPos(
                ImVec2(rowMax.x - restW - 6.0f * sc,
                       rowMin.y + (rowH - ImGui::GetFrameHeight()) * 0.5f));
            if (s_aiVc.busy) ImGui::BeginDisabled();
            if (ImGui::SmallButton("Restore")) {
                aiVcRequestRollback(st, cp.id, std::string("Restored: ") + title);
                ImGui::CloseCurrentPopup();
            }
            if (s_aiVc.busy) ImGui::EndDisabled();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Restore the part to this checkpoint.");
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + ui::metrics::cpMenuRowGap()));
        ImGui::PopID();
    }
    // Pill scrollbar (NoScrollbar above so it replaces, not doubles, the default bar) — MUST be
    // submitted while the list child is still the current window.
    ui::drawWindowPillScrollbarV(chatSurf());
    ImGui::EndChild();
}

// Draw a double-overlapping left-triangle glyph (◁◁) into `dl` inside `bb`.
// Used for rewind / Before — never text (font may lack the glyph).
void drawDoubleRewindGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx,
                                  ImU32 col) {
    const float w = mx.x - mn.x, h = mx.y - mn.y;
    const float cx = (mn.x + mx.x) * 0.5f;
    const float cy = (mn.y + mx.y) * 0.5f;
    const float th = h * 0.34f;   // triangle half-height
    const float tw = h * 0.28f;   // triangle depth (points left)
    // Two overlapping left-pointing triangles, slightly offset.
    auto tri = [&](float ox) {
        const float tipX = cx + ox - tw;
        const float baseX = cx + ox + tw * 0.55f;
        dl->AddTriangleFilled(ImVec2(tipX, cy),
                              ImVec2(baseX, cy - th),
                              ImVec2(baseX, cy + th), col);
    };
    tri(-w * 0.14f);
    tri(+w * 0.10f);
    (void)w;
}

// Small pencil (edit) glyph — filled diagonal body + tip + eraser. Drawn when the atlas
// tile is unavailable (pre-regeneration). Prefer getIcon("msg_edit") / prompt_edit.
void drawPencilGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx, ImU32 col) {
    const float w = mx.x - mn.x, h = mx.y - mn.y;
    const float cx = (mn.x + mx.x) * 0.5f;
    const float cy = (mn.y + mx.y) * 0.5f;
    const float s = std::min(w, h) * 0.38f;
    // Diagonal body (bottom-left tip → top-right eraser), as a filled quad so it never
    // collapses to a bare slash at small sizes.
    const ImVec2 tip(cx - s * 0.78f, cy + s * 0.72f);
    const ImVec2 end(cx + s * 0.72f, cy - s * 0.78f);
    const float dx = end.x - tip.x, dy = end.y - tip.y;
    const float len = std::sqrt(dx * dx + dy * dy);
    if (len < 1.0f) return;
    const float nx = -dy / len, ny = dx / len;
    const float half = s * 0.18f;
    // Tip wedge (leading ~22% of the shaft).
    const ImVec2 tipJoin(tip.x + dx * 0.22f, tip.y + dy * 0.22f);
    dl->AddTriangleFilled(tip,
                          ImVec2(tipJoin.x + nx * half, tipJoin.y + ny * half),
                          ImVec2(tipJoin.x - nx * half, tipJoin.y - ny * half), col);
    // Body rectangle from tip-join to eraser band.
    const ImVec2 erJoin(tip.x + dx * 0.82f, tip.y + dy * 0.82f);
    const ImVec2 b0(tipJoin.x + nx * half, tipJoin.y + ny * half);
    const ImVec2 b1(erJoin.x  + nx * half, erJoin.y  + ny * half);
    const ImVec2 b2(erJoin.x  - nx * half, erJoin.y  - ny * half);
    const ImVec2 b3(tipJoin.x - nx * half, tipJoin.y - ny * half);
    dl->AddQuadFilled(b0, b1, b2, b3, col);
    // Eraser band (short thicker cross-stroke near the top end).
    const ImVec2 e0(erJoin.x + nx * half * 1.15f, erJoin.y + ny * half * 1.15f);
    const ImVec2 e1(erJoin.x - nx * half * 1.15f, erJoin.y - ny * half * 1.15f);
    const ImVec2 e2(end.x    - nx * half * 1.15f, end.y    - ny * half * 1.15f);
    const ImVec2 e3(end.x    + nx * half * 1.15f, end.y    + ny * half * 1.15f);
    dl->AddQuadFilled(e0, e3, e2, e1, col);
}

// Shared rectangular less-rounded header button (optional leading icon), submitted in TWO
// phases on purpose.
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
    h.clicked = ImGui::Selectable("##hit", false, ImGuiSelectableFlags_NoPadWithHalfSpacing,
                                 ImVec2(w, bh));
    ImGuiContext& g = *GImGui;
    IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), id, GImGui->LastItemData.StatusFlags);
    h.hovered = ImGui::IsItemHovered();
    h.held    = ImGui::IsItemActive();
    h.rect    = ImRect(p0, ImVec2(p0.x + w, p0.y + bh));
    ImGui::PopID();
    return h;
}

void headerTextButtonPaint(const HeaderBtnHit& h, const char* label, bool active,
                           const char* iconName, bool withLabel) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 txt = surf.textPrimary;
    const float round = ui::metrics::cpMenuChipRound();
    const float iconSz = ImGui::GetTextLineHeight() * 0.92f;
    const float iconGap = 4.0f * sc;
    const bool haveIcon = iconName && iconName[0];
    const float padX = 6.0f * sc;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Navigation is flat at rest: only the current view and hovered actions carry a wash.
    if (active || h.hovered || h.held) {
        const ImVec4 bg = active
            ? ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.12f)
            : ImVec4(txt.x, txt.y, txt.z, h.held ? 0.14f : 0.06f);
        dl->AddRectFilled(h.rect.Min, h.rect.Max, ImGui::ColorConvertFloat4ToU32(bg), round);
    }
    if (active) {
        const float inset = ui::metrics::px(6.0f);
        const float y = h.rect.Max.y - ui::metrics::stroke(1.0f);
        dl->AddLine(ImVec2(h.rect.Min.x + inset, y), ImVec2(h.rect.Max.x - inset, y),
                    ImGui::ColorConvertFloat4ToU32(semanticOnGlass(ui::palette.accent, surf)),
                    ui::metrics::stroke(1.5f));
    }

    // withLabel=false is the narrow-column pill: the icon centres in the box on its own and
    // the tooltip carries the name. Never squeeze the label in — draw-list text is not clipped
    // by the pill, so it would spill across its neighbours.
    const float midY = (h.rect.Min.y + h.rect.Max.y) * 0.5f;
    const float labelW = withLabel ? ImGui::CalcTextSize(label).x : 0.0f;
    const float contentW = (haveIcon ? iconSz + (withLabel ? iconGap : 0.0f) : 0.0f) + labelW;
    float x = withLabel ? (h.rect.Min.x + padX)
                        : (h.rect.Min.x + h.rect.Max.x - contentW) * 0.5f;
    if (haveIcon) {
        const ImVec4 tint = active ? surf.textPrimary
                                   : ImVec4(surf.textSecondary.x, surf.textSecondary.y,
                                            surf.textSecondary.z, 0.95f);
        ui::drawIconGlass(dl, iconName, ImVec2(x + iconSz * 0.5f, midY), iconSz,
                          ImGui::ColorConvertFloat4ToU32(tint), 0);
        x += iconSz + iconGap;
    }
    if (!withLabel) return;
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
        active ? surf.textPrimary : surf.textSecondary);
    dl->AddText(ImVec2(x, midY - ImGui::GetTextLineHeight() * 0.5f), ink, label);
}

// ---- staged edit transaction (see EditStage in copilot_internal.h) ----------------------------

static uint64_t s_editGen = 0;   // monotonically increasing; a stale RPC reply never matches

using EditPhase = EditStage::Phase;

bool editStageInFlight() {
    return s_edit.phase == EditPhase::Rewinding || s_edit.phase == EditPhase::RollingBack;
}

bool editStageHoldsConversation(const std::string& convId) {
    return s_edit.phase != EditPhase::Off && !convId.empty() && convId == s_edit.convId;
}

// The conversation's run has reached its ACTUAL terminal: no job running, no result waiting
// to drain, no stop still unwinding, and no live bubble left in its transcript.
static bool editRunSettled(AppState& st, const std::string& convId) {
    if (JobState* run = st.runIfExists(convId)) {
        if (run->running.load() || run->ready.load() || run->stopRequested.load()) return false;
    }
    const std::vector<ChatMsg>* msgs = conversationMsgs(st, convId);
    if (!msgs) return false;
    for (const ChatMsg& m : *msgs)
        if (!m.fromUser && m.running) return false;
    return true;
}

int editStageHideFrom(AppState& st) {
    if (s_edit.phase != EditPhase::Staged && !editStageInFlight()) return -1;
    if (st.activeConvId() != s_edit.convId) return -1;
    if (!editRunSettled(st, s_edit.convId)) return -1;   // a live tail is never hidden
    return s_edit.msgIdx;
}

// Back to Staged with the edited text still in the box: nothing was adopted, the transcript
// never moved, no turn was launched. Says why in user terms.
static void editStageFail(AppState& st, const std::string& why) {
    s_edit.phase = EditPhase::Staged;
    s_edit.sendRequested = false;
    s_edit.newSessionId.clear();
    s_edit.newSessionFile.clear();
    s_edit.geometryRolledBack = false;
    st.toast(ui::ToastLevel::Error,
             "Edit not applied: " + why + " The original conversation is unchanged; your "
             "edited text is still in the box (Esc cancels).");
}

bool cancelEditStage(AppState& st) {
    if (s_edit.phase == EditPhase::Off) return true;
    if (s_edit.phase != EditPhase::Staged) {
        st.toast(ui::ToastLevel::Info, "The edit is being applied \xe2\x80\x94 it can't be "
                                       "cancelled halfway.");
        return false;
    }
    const bool viewed = st.activeConvId() == s_edit.convId;
    const std::string saved = std::move(s_edit.savedPrompt);
    if (viewed) {
        st.pendingAttachments = std::move(s_edit.savedAttachments);
        st.pendingInlineImagePaths = std::move(s_edit.savedImages);
        st.pendingContextRefs = std::move(s_edit.savedContextRefs);
    }
    s_edit = EditStage{};
    if (viewed) {
        composerSetText(st, saved);
        savePromptDraft(st, /*force=*/true);
    }
    return true;
}

void editStageBeforeConversationPark(AppState& st) {
    // Only a merely staged edit of the outgoing conversation is cancelled; a requested send
    // (waiting on the run's terminal) or an in-flight/committed transaction keeps going.
    if (s_edit.phase == EditPhase::Staged && !s_edit.sendRequested &&
        s_edit.convId == st.activeConvId())
        cancelEditStage(st);
}

void beginEditStage(AppState& st, int msgIdx) {
    if (msgIdx < 0 || msgIdx >= (int)st.chat.size() || !st.chat[msgIdx].fromUser) return;
    const std::string convId = st.activeConvId();
    if (convId.empty()) return;
    if (s_edit.phase != EditPhase::Off) {
        if (s_edit.convId != convId) {
            st.toast(ui::ToastLevel::Info,
                     "Finish or cancel the message edit in your other session first.");
            return;
        }
        if (s_edit.phase != EditPhase::Staged || s_edit.sendRequested) {
            st.toast(ui::ToastLevel::Info,
                     "The edit is being applied \xe2\x80\x94 wait for it to finish.");
            return;
        }
        if (!cancelEditStage(st)) return;
    }
    const std::string target = chudcodeEditTypedText(st.chat[msgIdx].text);
    if (target.empty() && st.chat[msgIdx].imagePaths.empty()) {
        st.toast(ui::ToastLevel::Info, "This message has no text to edit.");
        return;
    }
    std::vector<std::string> earlier;
    for (int i = 0; i < msgIdx; ++i)
        if (st.chat[i].fromUser) earlier.push_back(chudcodeEditTypedText(st.chat[i].text));
    // Stop-and-edit: a live turn is stopped first; the transcript stays as it is (the stopped
    // reply keeps its rows) and the tail is hidden only once the run's actual terminal lands.
    if (aiTurnActive(st) || activeConversationRunning(st)) requestStopRun(st);
    s_edit = EditStage{};
    s_edit.phase = EditPhase::Staged;
    s_edit.convId = convId;
    s_edit.msgIdx = msgIdx;
    s_edit.targetText = target;
    s_edit.occurrence = chudcodeEditOccurrence(earlier, target);
    s_edit.savedPrompt = composerText(st);
    s_edit.savedAttachments = std::move(st.pendingAttachments);
    s_edit.savedImages = std::move(st.pendingInlineImagePaths);
    s_edit.savedContextRefs = std::move(st.pendingContextRefs);
    st.pendingAttachments.clear();
    st.pendingContextRefs = st.chat[msgIdx].contextRefs;
    st.pendingInlineImagePaths = st.chat[msgIdx].imagePaths;
    // New messages retain every numbered reference exactly where it was authored. Legacy
    // image markers are converted in stored roster order, never looked up by basename.
    std::string editable = st.chat[msgIdx].text;
    const size_t fileRoster = editable.rfind("\n[attached:");
    if (fileRoster != std::string::npos && editable.find('\n', fileRoster + 1) == std::string::npos &&
        editable.back() == ']')
        editable.erase(fileRoster);
    if (ui::copilot::inlineImageRefs(editable, st.pendingInlineImagePaths.size()).empty()) {
        size_t pos = 0, image = 0;
        while (image < st.pendingInlineImagePaths.size() &&
               (pos = editable.find("[attached image:", pos)) != std::string::npos) {
            const size_t end = editable.find(']', pos);
            if (end == std::string::npos) break;
            const std::string marker = ui::copilot::inlineImageMarker(image++);
            editable.replace(pos, end - pos + 1, marker);
            pos += marker.size();
        }
        for (; image < st.pendingInlineImagePaths.size(); ++image) {
            if (!editable.empty()) editable += "\n";
            editable += ui::copilot::inlineImageMarker(image);
        }
    }
    composerSetText(st, editable);
    savePromptDraft(st, /*force=*/true);
}

static void editStageStartRollback(AppState& st);

// Step 2: branch the runtime session to before the edited message. A conversation with no
// runtime history at all (never ran online) has nothing to branch; its next turn seeds a fresh
// session from the truncated transcript.
static void editStageStart(AppState& st) {
    std::vector<ChatMsg>* msgs = conversationMsgs(st, s_edit.convId);
    if (!msgs) { s_edit = EditStage{}; return; }
    if (s_edit.msgIdx >= (int)msgs->size() || !(*msgs)[s_edit.msgIdx].fromUser ||
        chudcodeEditTypedText((*msgs)[s_edit.msgIdx].text) != s_edit.targetText) {
        editStageFail(st, "the conversation changed while you were editing.");
        return;
    }
    if (s_aiVc.busy) {
        editStageFail(st, "a checkpoint restore is still finishing \xe2\x80\x94 try again in a "
                          "moment.");
        return;
    }
    s_edit.gen = ++s_editGen;
    s_edit.phase = EditPhase::Rewinding;
    const ChudcodeSessionLink* link = chudcodeSessionLink(s_edit.convId);
#ifdef __EMSCRIPTEN__
    const bool hasHistory = link && !link->sessionId.empty();
#else
    const bool hasHistory = link && (!link->sessionFile.empty() || link->runtimeLive);
#endif
    if (!hasHistory) {
        editStageStartRollback(st);
        return;
    }
    std::string workspace;
    for (const Conversation& c : st.conversations)
        if (c.id == s_edit.convId) { workspace = c.projectDir; break; }
    if (workspace.empty()) workspace = st.workspaceDir;
    const uint64_t gen = s_edit.gen;
    st.rpc.call(kChudcodeRewindSessionMethod,
                chudcodeRewindSessionParams(s_edit.convId, s_edit.targetText,
                                            s_edit.occurrence, link->sessionFile, workspace),
                [&st, gen](const core::RpcResult& r) { editStageOnRewindResult(st, gen, r); },
                120000);
}

// Refusal reasons from chudcode_rewind_session, in user terms (never raw runtime text).
static std::string editRewindRefusalText(const std::string& reason) {
    if (reason.find("session_busy") != std::string::npos)
        return "the agent is still finishing its last step \xe2\x80\x94 try again in a moment.";
    if (reason.find("target_not_found") != std::string::npos)
        return "the agent's session history has no matching message to rewind to.";
    if (reason.find("session_file_mismatch") != std::string::npos)
        return "the agent session changed underneath this conversation.";
    if (reason.find("session_not_live") != std::string::npos)
        return "the agent session for this conversation isn't available.";
    return "the agent session couldn't be rewound.";
}

void editStageOnRewindResult(AppState& st, uint64_t gen, const core::RpcResult& r) {
    if (gen != s_edit.gen || s_edit.phase != EditPhase::Rewinding) return;
    if (chudcodeSessionRpcAbsent(r.ok, r.err, r.result)) {
        editStageFail(st, "this agent service can't rewind a conversation yet.");
        return;
    }
    if (core::workerLaneBusy(r)) {
        editStageFail(st, "the agent service is busy with a run \xe2\x80\x94 try again when it "
                          "finishes.");
        return;
    }
    if (!r.ok) {
        editStageFail(st, userFacingRpcError(r, "the agent service did not respond."));
        return;
    }
    ChudcodeBranchResult br;
    if (!parseChudcodeBranchResult(r.result, br)) {
        editStageFail(st, editRewindRefusalText(chudcodeActionError(r.result, "")));
        return;
    }
    // The service retired the conversation's process; the parent file is untouched, so the
    // old link stays valid until commit adopts the branch.
    chudcodeSessionLinkEnsure(s_edit.convId).runtimeLive = false;
    s_edit.newSessionId = br.sessionId;
    s_edit.newSessionFile = br.sessionFile;
    editStageStartRollback(st);
}

static void editStageCommit(AppState& st);

// Step 3: geometry back to before the edited message.
static void editStageStartRollback(AppState& st) {
    s_edit.phase = EditPhase::RollingBack;
    if (!s_aiVc.available) {   // no AI checkpoints on this service: chat + runtime edit only
        editStageCommit(st);
        return;
    }
    s_aiVc.busy = true;
    nlohmann::json p = {{"to", "msg:" + std::to_string(s_edit.msgIdx)}};
    if (!s_edit.docId.empty()) p["doc_id"] = s_edit.docId;
    const uint64_t gen = s_edit.gen;
    st.rpc.call("plm_rollback", p, [&st, gen](const core::RpcResult& r) {
        s_aiVc.busy = false;
        editStageOnRollbackResult(st, gen, r);
    });
}

void editStageOnRollbackResult(AppState& st, uint64_t gen, const core::RpcResult& r) {
    if (gen != s_edit.gen || s_edit.phase != EditPhase::RollingBack) return;
    s_aiVc.busy = false;
    s_aiVc.lastFetch = 0.0;   // timeline changed (or is confirmed): refresh on next open
    if (aiVcIsAbsent(r)) {
        s_aiVc.available = false;          // checkpoints unavailable: a chat-only edit
    } else if (aiVcIsNoCheckpoint(r)) {
        // Legitimate: no geometry checkpoint at that message, the part stays as it is.
    } else {
        std::string hint;
        if (!aiVcOk(r, hint)) {
            editStageFail(st, "the part couldn't be rolled back (" + hint + ").");
            return;
        }
        s_edit.geometryRolledBack = true;
        aiVcRefreshMirror(st, r.result, s_edit.docId);
    }
    editStageCommit(st);
}

static void editStageLaunchIfViewed(AppState& st);

// Step 4: adopt the branch link, park the old tail as a ChatBranch, truncate, persist — all
// before any turn exists, so nothing async can land on a new reply afterwards.
static void editStageCommit(AppState& st) {
    std::vector<ChatMsg>* msgs = conversationMsgs(st, s_edit.convId);
    if (!msgs || s_edit.msgIdx > (int)msgs->size()) {
        s_edit = EditStage{};
        st.toast(ui::ToastLevel::Error,
                 "The edited conversation no longer exists \xe2\x80\x94 nothing was sent.");
        return;
    }
#ifdef __EMSCRIPTEN__
    const bool hasBranch = !s_edit.newSessionId.empty();
#else
    const bool hasBranch = !s_edit.newSessionFile.empty();
#endif
    if (hasBranch) {
        ChudcodeSessionLink link;
        link.sessionId = s_edit.newSessionId;
        link.sessionFile = s_edit.newSessionFile;
        // The next turn resumes the branch; no previous health verdict carries.
        chudcodeSessionLinkSet(s_edit.convId, std::move(link));
    }
    const int parked = (int)msgs->size() - s_edit.msgIdx;
    parkConversationTail(st, s_edit.convId, s_edit.msgIdx,
                         "edited message " + std::to_string(s_edit.msgIdx) + " \xe2\x80\x94 " +
                             std::to_string(parked) + " message(s) parked");
    persistChatHistory(st);
    s_edit.phase = EditPhase::AwaitSubmit;
    editStageLaunchIfViewed(st);
}

// Step 5: exactly one turn with the edited text, in its own conversation.
static void editStageLaunchIfViewed(AppState& st) {
    if (s_edit.phase != EditPhase::AwaitSubmit || st.activeConvId() != s_edit.convId) return;
    if (st.convRunning(s_edit.convId)) return;
    const std::string text = s_edit.editedText;
    const bool geometry = s_edit.geometryRolledBack;
    st.pendingAttachments = std::move(s_edit.editedAttachments);
    st.pendingInlineImagePaths = std::move(s_edit.editedImages);
    st.pendingContextRefs = std::move(s_edit.editedContextRefs);
    composerSetText(st, text);
    s_edit = EditStage{};   // release the composer before submitting
    const size_t before = st.chat.size();
    submitTurn(st, text);
    if (st.chat.size() > before) {
        composerSetText(st, std::string());
        st.toast(ui::ToastLevel::Success,
                 geometry ? "Message edited \xe2\x80\x94 the part was rolled back to before it."
                          : "Message edited \xe2\x80\x94 no geometry checkpoint there, so the "
                            "part was left unchanged.");
    } else {
        // The turn was refused (e.g. a run is still in flight): the conversation is already
        // rewound and the edited text stays in the box, so Send runs it.
        st.toast(ui::ToastLevel::Warn,
                 "The conversation was rewound, but the edited message wasn't sent \xe2\x80\x94 "
                 "press Send to run it.");
    }
}

void editStageSubmit(AppState& st, const std::string& text) {
    if (s_edit.phase != EditPhase::Staged || s_edit.sendRequested ||
        st.activeConvId() != s_edit.convId || text.empty())
        return;
    s_edit.editedText = text;
    s_edit.editedAttachments = st.pendingAttachments;
    s_edit.editedImages = st.pendingInlineImagePaths;
    s_edit.editedContextRefs = st.pendingContextRefs;
    s_edit.sendRequested = true;
    s_edit.docId = aiVcActiveDocId(st);
    if (aiTurnActive(st)) requestStopRun(st);
    if (editRunSettled(st, s_edit.convId)) editStageStart(st);
}

void editStageTick(AppState& st) {
    if (s_edit.phase == EditPhase::Off) return;
    if (!conversationMsgs(st, s_edit.convId)) {   // its conversation was deleted
        s_edit = EditStage{};
        return;
    }
    if (s_edit.phase == EditPhase::Staged && s_edit.sendRequested &&
        editRunSettled(st, s_edit.convId))
        editStageStart(st);
    else if (s_edit.phase == EditPhase::AwaitSubmit)
        editStageLaunchIfViewed(st);
}

const char* editStageStatus(AppState& st) {
    if (!editStageHoldsConversation(st.activeConvId())) return nullptr;
    switch (s_edit.phase) {
        case EditPhase::Staged:
            if (!editRunSettled(st, s_edit.convId))
                return s_edit.sendRequested
                    ? "Waiting for the run to stop, then applying the edit\xe2\x80\xa6"
                    : "Stopping the run so this message can be edited\xe2\x80\xa6 "
                      "Esc cancels the edit.";
            return "Editing an earlier message \xe2\x80\x94 Enter replaces it and everything "
                   "after it (kept as a branch); Esc cancels.";
        case EditPhase::Rewinding:   return "Applying the edit: rewinding the agent session\xe2\x80\xa6";
        case EditPhase::RollingBack: return "Applying the edit: rolling the part back\xe2\x80\xa6";
        case EditPhase::AwaitSubmit: return "Sending the edited message\xe2\x80\xa6";
        case EditPhase::Off:         break;
    }
    return nullptr;
}

// 0 = no checkpoint resolves msg:<idx> (backend would refuse -> chat-only rewind),
// 1 = a checkpoint exists at exactly this message (geometry rolls back too),
// 2 = timeline not loaded yet — kick a throttled fetch and use neutral wording.
// Mirrors AiVcStore::resolve_target: msg targets match on EXACT msg_idx, and without
// msg granularity (v1 timelines carry no msg_idx) a msg target never resolves.
int aiVcMsgCheckpointState(AppState& st, int msgIdx) {
    if (s_aiVc.checkpoints.empty()) {
        const double now = ImGui::GetTime();
        if (!s_aiVc.loading && (now - s_aiVc.lastFetch) > 3.0)
            aiVcFetchTimeline(st);   // sets loading + lastFetch; result lands async
        return (s_aiVc.lastFetch <= 0.0 || s_aiVc.loading) ? 2 : 0;
    }
    if (!s_aiVc.hasMsgGranularity) return 0;
    for (const auto& cp : s_aiVc.checkpoints)
        if (cp.msgIdx == msgIdx) return 1;
    return 0;
}

const char* aiVcRewindTip(int cpState) {
    switch (cpState) {
        case 1:  return "Rewind to before this message — rolls the part back to the "
                        "checkpoint here and truncates the chat.";
        case 2:  return "Rewind the chat to this point.";
        default: return "Rewind the chat to this point — no geometry checkpoint exists "
                        "here, so the part stays unchanged.";
    }
}

std::string aiVcRewindToast(int cpState) {
    switch (cpState) {
        case 1:  return "Rewound to before this message";
        case 2:  return "Rewound the chat";
        default: return "Chat rewound — no geometry checkpoint at this point, "
                        "so the part was left unchanged";
    }
}

// Chat-only rewind affordance for finished turns that did NOT mutate geometry (no
// turn-level Before/After target exists). Same rewind glyph + click-again inline confirm
// as the user-message meta bar; always visible (there is no label row to hover-latch on).
static void drawAiVcChatRewind(AppState& st, int msgIdx) {
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
        s_aiVc.confirmCallI = -1;
    }
    const bool pending = (s_aiVc.confirmMsgIdx == msgIdx && s_aiVc.confirmKind == 3);
    const int cpState = aiVcMsgCheckpointState(st, msgIdx);
    const float btnSz = ImGui::GetFrameHeight() * 0.78f;
    if (pending) {
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.55f));
    }
    const bool clicked = ImGui::InvisibleButton("##chat_rw", ImVec2(btnSz, btnSz));
    const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = chatSurf();
    if (pending || ImGui::IsItemHovered())
        dl->AddRectFilled(bmn, bmx,
            ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                  surf.textPrimary.z, pending ? 0.18f : 0.10f)),
            ui::metrics::rounding(4.0f));
    const float rp = btnSz * 0.18f;   // inset to match the user-bubble rewind glyph
    drawDoubleRewindGlyph(dl, ImVec2(bmn.x + rp, bmn.y + rp), ImVec2(bmx.x - rp, bmx.y - rp),
        ImGui::ColorConvertFloat4ToU32(pending ? surf.textPrimary : surf.textSecondary));
    if (pending) ImGui::PopStyleColor();
    if (clicked) {
        if (!pending) {
            s_aiVc.confirmMsgIdx = msgIdx;
            s_aiVc.confirmKind = 3;
            s_aiVc.confirmUntil = now + 4.0;
        } else {
            aiVcRequestRollback(st, "msg:" + std::to_string(msgIdx), aiVcRewindToast(cpState));
        }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", pending ? "Click again to confirm rewind."
                                        : aiVcRewindTip(cpState));
}

// Restore actions for an assistant turn. Drawn as a BOTTOM action row under the turn
// (alwaysShow=true), mirroring the user bubble's own meta bar so both sides of the chat
// offer revert in the same place — it used to sit in the header beside the model name,
// where it read as part of the author label and was hover-gated.
// Non-mutating finished turns (turnNum <= 0) get the always-visible chat-only rewind —
// revert is offered at ANY point in the chat, and the wording never implies geometry was
// restored when no checkpoint exists for that point.
// Rewind-only (◁◁): there is no "After" counterpart. "After step N" is the same state as
// "before step N+1", so the second button only ever doubled the chrome — and per-tool-call
// rewinds (drawAiVcCallRewind) now reach every intermediate point anyway. Inline confirm =
// click again. Before uses call:<turn>:0 when call granularity exists, else turn-level.
// alwaysShow=false keeps the legacy hover-latched behavior for any caller that still anchors
// the row on a preceding label item.
void drawAiVcTurnRestore(AppState& st, int msgIdx, int turnNum, bool alwaysShow) {
    if (!s_aiVc.available || s_aiVc.busy) return;
    if (turnNum <= 0) { drawAiVcChatRewind(st, msgIdx); return; }
    const float sc = ui::uiScale;
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
        s_aiVc.confirmCallI = -1;
    }
    const bool pending = (s_aiVc.confirmMsgIdx == msgIdx);

    // Hover latch only matters when the row hangs off a preceding label item. In the
    // bottom action row there is nothing to hover-latch on, so the buttons are simply
    // always visible — same as the user bubble's rewind/edit pair.
    static int    s_hovMsg = -1;
    static double s_hovUntil = 0.0;
    if (!alwaysShow) {
        // Latch hover for a short window so the user can move from the "Assistant"
        // label onto the Before/After buttons without them vanishing mid-flight.
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenOverlapped)) {
            s_hovMsg = msgIdx;
            s_hovUntil = now + 0.6;
        }
        const bool latched = (s_hovMsg == msgIdx && now <= s_hovUntil);
        if (!(pending || latched)) return;
    } else {
        // Anchor item so the first glyphBtn's SameLine has something to sit beside
        // (it also supplies the row's small left inset).
        ImGui::Dummy(ImVec2(0.0f, 0.0f));
    }

    auto glyphBtn = [&](int kind, const char* tip) {
        ImGui::SameLine(0, 6.0f * sc);
        const bool isPend = pending && s_aiVc.confirmKind == kind;
        // Same square footprint + glyph inset as the user bubble's rewind/edit buttons
        // (btnSz = 0.78 * frame height, rp = 0.18 pad) so the affordance reads identically
        // on both sides of the chat.
        const float btnSz = ImGui::GetFrameHeight() * 0.78f;
        ImGui::PushID(kind + 400);
        if (isPend) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.55f));
        }
        const bool clicked = ImGui::InvisibleButton("##rw", ImVec2(btnSz, btnSz));
        const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ui::AdaptiveSurface& surf = chatSurf();
        const ImU32 col = ImGui::ColorConvertFloat4ToU32(
            isPend ? surf.textPrimary : surf.textSecondary);
        // Soft chip bg on hover/pending.
        if (isPend || ImGui::IsItemHovered())
            dl->AddRectFilled(bmn, bmx,
                ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                      surf.textPrimary.z, isPend ? 0.18f : 0.10f)),
                ui::metrics::rounding(4.0f));
        const float rp = btnSz * 0.18f;
        drawDoubleRewindGlyph(dl, ImVec2(bmn.x + rp, bmn.y + rp),
                              ImVec2(bmx.x - rp, bmx.y - rp), col);
        if (isPend) ImGui::PopStyleColor();
        if (clicked) {
            if (!isPend) {
                s_aiVc.confirmMsgIdx = msgIdx;
                s_aiVc.confirmKind = kind;
                s_aiVc.confirmUntil = now + 4.0;
            } else {
                std::string to;
                if (kind == 1) {
                    // Prefer call-granularity "before first tool of this turn" when available.
                    if (s_aiVc.hasCallGranularity)
                        to = "call:" + std::to_string(turnNum) + ":0";
                    else if (turnNum <= 1) to = "turn:0";
                    else to = "turn:" + std::to_string(turnNum - 1);
                } else {
                    to = "turn:" + std::to_string(turnNum);
                }
                aiVcRequestRollback(st, to, "Restored to before this step");
            }
        }
        if (ImGui::IsItemHovered()) {
            s_hovMsg = msgIdx;
            s_hovUntil = now + 0.6;
            ImGui::SetTooltip("%s", isPend ? "Click again to confirm." : tip);
        }
        ImGui::PopID();
    };

    glyphBtn(1, "Restore the part to the state before this step.");
}

// The checkpoint holding the state BEFORE tool call `callI`, or "" when it cannot be
// identified beyond doubt.
//
// The target is the checkpoint's own ID, never a synthesized "call:<turn>:<i>" string:
//  - call.i IS the 0-based tool ordinal within a run (agent_turn passes
//    tool_calls_made - 1), which is exactly what the timeline counts;
//  - but call.turn is only a chat msg_idx when the turn had one and otherwise falls back
//    to tool_calls_made, so it is NOT the GUI's turn ordinal — matching it would miss;
//  - and call.i restarts at 0 every run, so on a document edited across several runs the
//    same (call.i, tool) pair recurs. Scope to ONE run via session_id.
//
// Each call writes TWO chain records — the pre-call snapshot and the post-call one — in
// chain order. We want the pre-call state, i.e. the FIRST match. Rollback resolution is an
// exact id lookup, so a row we cannot place in the newest run gets no glyph at all rather
// than a button that would restore the wrong state.
std::string aiVcCallCheckpointId(int callI, const std::string& toolName) {
    if (callI < 0 || toolName.empty()) return {};
    // Newest run present in the timeline (checkpoints arrive oldest-first).
    std::string newestSession;
    for (const auto& cp : s_aiVc.checkpoints)
        if (!cp.sessionId.empty()) newestSession = cp.sessionId;
    if (newestSession.empty()) return {};
    for (const auto& cp : s_aiVc.checkpoints)
        if (cp.sessionId == newestSession && cp.callI == callI && cp.label == toolName &&
            !cp.id.empty())
            return cp.id;   // first match in chain order == the pre-call snapshot
    return {};
}

// Per-tool-call rewind: restore the part to the state BEFORE this individual call.
// There is deliberately no "after" counterpart — "after call i" is the same state as
// "before call i+1", so one direction covers every point in the turn without doubling
// the chrome. Same glyph and click-again-confirm as every other rewind in the chat.
void drawAiVcCallRewind(AppState& st, int callI, const std::string& toolName) {
    if (!s_aiVc.available || s_aiVc.busy) return;
    const std::string cpId = aiVcCallCheckpointId(callI, toolName);
    if (cpId.empty()) return;
    const float sc = ui::uiScale;
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
        s_aiVc.confirmCallI = -1;
    }
    // Call-scoped confirms key on kind 4 + the call index so two rows in the same turn
    // never share a pending state.
    const bool pending = s_aiVc.confirmKind == 4 && s_aiVc.confirmCallI == callI;
    ImGui::SameLine(0, 8.0f * sc);
    const float sz = ImGui::GetTextLineHeight();
    ImGui::PushID(callI + 900);
    const bool clicked = ImGui::InvisibleButton("##call_rw", ImVec2(sz * 1.15f, sz));
    const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = chatSurf();
    if (pending || ImGui::IsItemHovered())
        dl->AddRectFilled(bmn, bmx,
            ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                  surf.textPrimary.z, pending ? 0.18f : 0.10f)),
            ui::metrics::rounding(4.0f));
    const float rp = sz * 0.18f;
    drawDoubleRewindGlyph(dl, ImVec2(bmn.x + rp, bmn.y + rp), ImVec2(bmx.x - rp, bmx.y - rp),
        ImGui::ColorConvertFloat4ToU32(pending ? surf.textPrimary : surf.textSecondary));
    if (clicked) {
        if (!pending) {
            s_aiVc.confirmMsgIdx = -1;
            s_aiVc.confirmKind = 4;
            s_aiVc.confirmCallI = callI;
            s_aiVc.confirmUntil = now + 4.0;
        } else {
            aiVcRequestRollback(st, cpId, "Restored to before this step");
        }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", pending
            ? "Click again to confirm."
            : "Restore the part to the state before this step.");
    ImGui::PopID();
}


}  // namespace ui
