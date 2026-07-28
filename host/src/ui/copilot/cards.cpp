// Copilot cards (T-RF6 split of legacy_chat.cpp): the Plan/Objectives section (tiles + todos
// tree + subagents, plus the drawPlanCard/drawPlanSection/drawPartsLine adapters declared in
// core/app_state.h), the clarify + plan-approve cards, the corporate-org cards
// (aicad.corporate/1), the exec-mode cards (swarm waves / eng-firm bids+award+team+comms /
// advisor / sub-run), and the AI-VC (agent version control) checkpoint timeline + turn
// restore/edit-stage UI. Event folding for these cards lives in feed.cpp; the per-turn
// timeline that hosts them lives in activity_timeline.cpp.

#include "copilot_internal.h"

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

// (The old omp-style text glyphs here — ○ ✓ → — are gone: the UI font lacks those codepoints
// and rendered them as literal "?". Rows use the vector drawStatusGlyph instead.)

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

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
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
            // Same vector status glyph the action cards / tiles use — the old omp-style text
            // glyphs (○ ✓ →) are missing from the UI font and rendered as literal "?".
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

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
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
            } else if (a.rowKind >= 4 && a.rowKind <= 8) {
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
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
        GlassBubble gb0 = beginGlassBubble(ImGui::GetContentRegionAvail().x,
                                           ui::GlassLevel::Thin, 10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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

    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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

// Back-compat: legacy PlanInfo card → normalize to PlanState and draw the section.
void drawPlanCard(const PlanInfo& plan, const std::vector<ActivityItem>& activity,
                  const ui::Fonts& fonts) {
    if (!plan.present) return;
    PlanState ps = planStateFromPlanInfo(plan);
    // Enrich part statuses from activity (same heuristic as the old card).
    for (auto& it : ps.items) {
        if (it.status.empty() || it.status == "pending")
            it.status = planItemStatusFromActivity(it, activity);
    }
    drawPlanSection(ps, activity, fonts);
}


// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts) {
    if (parts.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
    ImGui::TextUnformatted("Parts:");
    ImGui::PopStyleColor();
    for (size_t i = 0; i < parts.size(); ++i) {
        ImGui::SameLine();
        std::string lbl = parts[i].name + (i + 1 < parts.size() ? "," : "");
        ImGui::TextColored(partColor((int)i), "%s", lbl.c_str());
    }
}


namespace ui {

void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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

    // Drain context-cube picks into an armed geometry_select question.
    if (m.clarify.geometryPickQ >= 0 &&
        m.clarify.geometryPickQ < (int)m.clarify.questions.size() &&
        !st.pendingContextRefs.empty()) {
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
    ImGui::PopStyleVar();   // ##clarify WindowPadding
    endGlassBubble(gb);
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
        launchDesignRun(st, aug, buildExtra);
    }
}

// User-facing plan approve gate — ONLY manual Plan mode (plan_only / awaiting_plan_approval)
// or advisor escalate (needs_user_decision). Agent-decided plans never reach this card.
// Question: "need edits, or start execution?" Edit plan prefills the composer; Execute-as
// buttons stamp approved_plan + execute_as. Recommended button matches planExecutionStyle.
void drawPlanApproveCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    if (!m.planApprovePending || m.running) return;
    if (m.errKind != ERR_NONE) {
        m.planApprovePending = false;
        return;
    }
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);

    // Normalize recommended style from plan_ready / task_route.
    std::string style = m.planExecutionStyle;
    for (char& c : style) c = (char)std::tolower((unsigned char)c);
    if (style == "solo" || style == "standard" || style == "edit-style") style = "edit";
    if (style == "complex" || style == "multi" || style == "multi-part" || style == "swarm-style")
        style = "swarm";
    if (style == "engfirm" || style == "firm" || style == "engineering_firm") style = "eng_firm";
    if (style != "edit" && style != "agent" && style != "swarm" && style != "eng_firm")
        style = style.empty() ? "agent" : style;

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##plan_approve", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "%s",
                       m.planApproveReason.empty()
                           ? "Plan ready — need edits, or start execution?"
                           : "Plan needs your decision");
    ImGui::PopFont();

    // Planned-for style chip.
    {
        const char* styleLabel =
            (style == "edit") ? "edit" :
            (style == "swarm") ? "swarm" :
            (style == "eng_firm") ? "eng. firm" : "agent";
        char planned[96];
        std::snprintf(planned, sizeof(planned), "Planned for %s execution", styleLabel);
        dimText(planned);
    }
    if (!m.planApproveReason.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", m.planApproveReason.c_str());
        ImGui::PopStyleColor();
    } else {
        dimText("Review the plan above. Edit it, or execute with the recommended style "
                "(other styles still available).");
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

    // Button order: recommended first as primary when matching; others secondary.
    auto styleBtn = [&](const char* label, const char* key, bool asPrimary) -> bool {
        if (asPrimary) return primaryButton(label, ImVec2(-1, ui::metrics::copilotBtnH()));
        return ImGui::Button(label, ImVec2(-1, 0));
    };

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    // Edit plan — focuses composer with plan text for revision (resend as plan_only).
    bool editPlan = ImGui::Button("Edit plan", ImVec2(-1, 0));

    // Execute buttons: highlight recommended.
    const bool recAgent   = (style == "agent" || style == "edit");
    const bool recSwarm   = (style == "swarm");
    const bool recEngFirm = (style == "eng_firm");

    // Recommended first when not agent, so primary lands on the match.
    bool asAgent = false, asSwarm = false, asEngFirm = false;
    if (recEngFirm) {
        asEngFirm = styleBtn("Execute as Eng. Firm  (Recommended)", "eng_firm", true);
        asSwarm   = styleBtn("Execute as Swarm", "swarm", false);
        asAgent   = styleBtn("Execute as Agent", "agent", false);
    } else if (recSwarm) {
        asSwarm   = styleBtn("Execute as Swarm  (Recommended)", "swarm", true);
        asAgent   = styleBtn("Execute as Agent", "agent", false);
        asEngFirm = styleBtn("Execute as Eng. Firm", "eng_firm", false);
    } else {
        // edit-style and agent both map to Execute as Agent (edit-style is single-agent execute).
        const char* agentLbl = (style == "edit")
            ? "Execute as Agent  (Recommended \xc2\xb7 edit-style)"
            : "Execute as Agent  (Recommended)";
        asAgent   = styleBtn(agentLbl, "agent", true);
        asSwarm   = styleBtn("Execute as Swarm", "swarm", false);
        asEngFirm = styleBtn("Execute as Eng. Firm", "eng_firm", false);
    }
    bool cancel = ImGui::Button("Cancel", ImVec2(-1, 0));
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##plan_approve WindowPadding
    endGlassBubble(gb);

    if (cancel) {
        m.planApprovePending = false;
        m.planApproveRequest.clear();
        m.planApproveExtras = nlohmann::json::object();
        m.planExecutionStyle.clear();
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

    if (!asAgent && !asSwarm && !asEngFirm) return;

    const std::string approved = planText();
    const std::string req = m.planApproveRequest.empty() ? approved : m.planApproveRequest;
    nlohmann::json extras = m.planApproveExtras.is_object() ? m.planApproveExtras
                                                            : nlohmann::json::object();
    nlohmann::json opts = nlohmann::json::object();
    opts["advisor"] = true;
    opts["approved_plan"] = approved;
    opts["corporate_mode"] = st.settings.corporateMode;
    if (asEngFirm) {
        opts["mode"] = "eng_firm";
        opts["execute_as"] = "eng_firm";
        opts["org"] = "eng_firm";
        opts["corporate_force"] = true;
        opts["budget_usd"] = st.settings.companyBudgetUsd;
    } else if (asSwarm) {
        opts["mode"] = "swarm";
        opts["execute_as"] = "swarm";
        opts["corporate_force"] = true;
    } else {
        // edit-style and agent both resume as single-agent execute.
        opts["mode"] = (style == "edit") ? "edit" : "agent";
        opts["execute_as"] = "agent";
    }
    extras["options"] = std::move(opts);
    nlohmann::json payload = agentProviderOverride(st.settings);
    for (auto it = extras.begin(); it != extras.end(); ++it)
        payload[it.key()] = it.value();

    m.planApprovePending = false;
    m.planExecutionStyle.clear();
    m.planApproveReason.clear();
    m.running = true;
    if (st.settings.model[0]) m.model = st.settings.model;
    if (asEngFirm) m.liveStatus = "Executing plan (eng. firm)...";
    else if (asSwarm) m.liveStatus = "Executing plan (swarm)...";
    else m.liveStatus = "Executing plan (agent)...";
    st.status = m.liveStatus; st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;

    if (asSwarm || asEngFirm) {
        launchMissionRunChat(st, req, /*needFreshDoc=*/true, payload);
    } else {
        launchDesignRun(st, req, payload);
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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

// Muted single-line activity (route rejection / wind-down summary).
void drawCorpMutedLine(const ActivityItem& a, const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
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


// The agent timeline: dispatch each activity row to a thinking block or an action card, computing
// the thinking auto-collapse (a thinking row collapses once a later stage reaches a verdict).
// Typed /2 transparency rows (M12-M14) dispatch by rowKind first, at their arrival position.
// Routing / classification card — distinct glass card at the top of the activity timeline.
// rowKind 10; title=task_class, reason=reason, detail=JSON of optional picked_model/etc.
void drawClassificationCard(ActivityItem& a, const ui::Fonts& fonts,
                                   bool turnRunning) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##classcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("milestone_flag", 20);
    if (!iv.valid) iv = ui::getIcon("bookmark", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.95f;
        ImVec4 tint = semanticOnGlass(pal.accent, surf);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(semanticOnGlass(pal.accent, surf), "Routing");
    ImGui::PopFont();
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(a.title.empty() ? "task" : a.title.c_str());
    ImGui::PopStyleColor();

    std::string model, provider, scaffold, tier, structure, domain;
    std::vector<std::string> alts;
    if (!a.detail.empty()) {
        try {
            auto j = nlohmann::json::parse(a.detail);
            if (j.is_object()) {
                if (j.contains("picked_model") && j["picked_model"].is_string())
                    model = j["picked_model"].get<std::string>();
                if (j.contains("picked_provider") && j["picked_provider"].is_string())
                    provider = j["picked_provider"].get<std::string>();
                if (j.contains("scaffold_level")) {
                    if (j["scaffold_level"].is_string())
                        scaffold = j["scaffold_level"].get<std::string>();
                    else scaffold = j["scaffold_level"].dump();
                } else if (j.contains("complexity")) {
                    if (j["complexity"].is_string())
                        scaffold = j["complexity"].get<std::string>();
                    else scaffold = j["complexity"].dump();
                }
                if (j.contains("tier")) {
                    if (j["tier"].is_string()) tier = j["tier"].get<std::string>();
                    else tier = j["tier"].dump();
                }
                if (j.contains("structure") && j["structure"].is_string())
                    structure = j["structure"].get<std::string>();
                if (j.contains("domain") && j["domain"].is_string())
                    domain = j["domain"].get<std::string>();
                auto pullAlts = [&](const char* key) {
                    if (!j.contains(key) || !j[key].is_array()) return;
                    for (const auto& it : j[key]) {
                        if (it.is_string()) alts.push_back(it.get<std::string>());
                        else if (it.is_object()) {
                            std::string tc;
                            if (it.contains("task_class") && it["task_class"].is_string())
                                tc = it["task_class"].get<std::string>();
                            else if (it.contains("class") && it["class"].is_string())
                                tc = it["class"].get<std::string>();
                            else if (it.contains("name") && it["name"].is_string())
                                tc = it["name"].get<std::string>();
                            if (!tc.empty()) alts.push_back(std::move(tc));
                        }
                    }
                };
                pullAlts("alternatives");
                pullAlts("alt_classes");
                pullAlts("candidates");
            }
        } catch (...) {}
    }
    static const char* kFallbackClasses[] = {
        "simple_part", "multi_feature_part", "assembly", "enclosure",
        "cam_program", "modification", "analysis", "unknown"
    };
    if (alts.empty()) {
        for (const char* c : kFallbackClasses) alts.push_back(c);
    }

    if (!model.empty() || !provider.empty()) {
        std::string line;
        if (!provider.empty() && !model.empty()) line = provider + " \xc2\xb7 " + shortModelName(model);
        else if (!model.empty()) line = shortModelName(model);
        else line = provider;
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", line.c_str());
        ImGui::PopStyleColor();
    }
    if (!scaffold.empty() || !tier.empty() || !structure.empty()) {
        std::string meta;
        if (!scaffold.empty()) meta = "scaffold " + scaffold;
        if (!structure.empty()) {
            if (!meta.empty()) meta += " \xc2\xb7 ";
            meta += structure;
        }
        if (!tier.empty()) {
            if (!meta.empty()) meta += " \xc2\xb7 ";
            meta += "tier " + tier;
        }
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(meta.c_str());
        ImGui::PopStyleColor();
    }
    if (!domain.empty() && domain != a.title) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(("domain " + domain).c_str());
        ImGui::PopStyleColor();
    }
    if (!a.reason.empty()) dimText(a.reason.c_str());

    // Alts toggle on its OWN row (never SameLine after a wrapped reason — that
    // jammed "(hide alts)" into the reason line and broke the card layout).
    const bool forceOpen = turnRunning && !a.userToggled;
    if (forceOpen) a.expanded = true;
    const bool open = a.expanded || forceOpen;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted(open ? "(hide alts)" : "(show alts)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
    if (open && !alts.empty()) {
        const std::string chosen = a.title;
        for (const auto& alt : alts) {
            const bool isChosen = (!chosen.empty() && alt == chosen);
            // Text bullet prefix (not ImGui::Bullet+SameLine — the glyph overlapped
            // the label inside the glass child, clipping into the first character).
            const std::string line = "\xE2\x80\xA2  " + alt;  // "•  "
            if (isChosen) {
                ImGui::PushStyleColor(ImGuiCol_Text, semanticOnGlass(pal.accent, surf));
                ImGui::Text("%s  \xe2\x9c\x93", line.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
            }
        }
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##classcard WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##class_copy", copyTxt);
    }
}

// Delegation card — transparency view of subagent dispatches (rowKind 19).
// title="N subagents"; detail JSON {"delegates":[{role,brief,spawn_kind}]} upserted by
// feedTurnActivity as agent.spawn events land. Same glass style as the Routing card.
void drawDelegationCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##firmbids", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("bookmark", 20);
    if (!iv.valid) iv = ui::getIcon("team", 20);
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
                    double usd = 0.0;
                    long tokens = 0;
                    if (b.contains("usd") && b["usd"].is_number()) usd = b["usd"].get<double>();
                    if (b.contains("tokens") && b["tokens"].is_number_integer())
                        tokens = b["tokens"].get<long>();
                    else if (b.contains("tokens") && b["tokens"].is_number())
                        tokens = (long)b["tokens"].get<double>();
                    bool inScope = true, within = true;
                    if (b.contains("in_scope") && b["in_scope"].is_boolean())
                        inScope = b["in_scope"].get<bool>();
                    if (b.contains("within_budget") && b["within_budget"].is_boolean())
                        within = b["within_budget"].get<bool>();
                    char line[256];
                    if (tokens > 0 && usd > 0.0)
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 %ld tok  \xc2\xb7 $%.2f",
                                      firm.c_str(), tokens, usd);
                    else if (usd > 0.0)
                        std::snprintf(line, sizeof(line), "  %s  \xc2\xb7 $%.2f", firm.c_str(), usd);
                    else
                        std::snprintf(line, sizeof(line), "  %s", firm.c_str());
                    ImVec4 col = surf.textPrimary;
                    if (!inScope || !within) col = surf.textTertiary;
                    ImGui::PushStyleColor(ImGuiCol_Text, col);
                    ImGui::TextUnformatted(line);
                    ImGui::PopStyleColor();
                    if (!inScope || !within) {
                        ImGui::SameLine(0, 6 * sc);
                        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                        ImGui::TextUnformatted(!inScope ? "out of scope" : "over budget");
                        ImGui::PopStyleColor();
                    }
                }
            }
            // Awarded firm chip when packed on the same event.
            std::string awarded = jstr(j, "awarded_firm");
            if (!awarded.empty()) {
                double ausd = 0.0;
                if (j.contains("awarded_usd") && j["awarded_usd"].is_number())
                    ausd = j["awarded_usd"].get<double>();
                char aw[160];
                if (ausd > 0.0)
                    std::snprintf(aw, sizeof(aw), "Awarded %s  \xc2\xb7 $%.2f", awarded.c_str(), ausd);
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
                if (b.contains("usd") && b["usd"].is_number()) {
                    char u[32];
                    std::snprintf(u, sizeof(u), " $%.2f", b["usd"].get<double>());
                    copyTxt += u;
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
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
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##advisor", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ui::IconUV iv = ui::getIcon("poly_star", 20);
    if (!iv.valid) iv = ui::getIcon("bookmark", 20);
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

void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##subrun", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    ImVec4 statusCol = surf.textSecondary;
    if (a.status == "running") statusCol = semanticOnGlass(pal.accent, surf);
    else if (a.status == "done" || a.status == "pass")
        statusCol = semanticOnGlass(pal.statusOk, surf);
    else if (a.status == "fail" || a.status == "error")
        statusCol = semanticOnGlass(pal.statusErr, surf);

    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(statusCol, "%s", a.title.empty() ? "Sub-run" : a.title.c_str());
    ImGui::PopFont();
    if (!a.reason.empty()) {
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
    for (const auto& s : m.steps)
        if (s.ok || s.done) return true;
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

static void aiVcRefreshMirror(AppState& st, const nlohmann::json& res) {
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
    const std::string docId = st.docMirror.docId();
    st.docMirror.resync(st.worker);
    nlohmann::json p;
    p["doc_id"] = docId;
    st.rpc.call("doc_activate", p, [&st](const core::RpcResult& r) {
        if (!r.ok || !r.result.is_object()) return;
        std::string aicm = r.result.value("aicm_path", std::string());
        if (aicm.empty() && r.result.contains("tess") && r.result["tess"].is_object())
            aicm = r.result["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) st.loadDocMeshAsync(aicm);
        st.docMirror.resync(st.worker);
    });
}

static void aiVcApplyChatTruncate(AppState& st, const nlohmann::json& res) {
    // Prefer explicit chat_truncate_to (msg index); fall back to msg:N targets only.
    int truncateTo = -1;
    if (res.is_object() && res.contains("chat_truncate_to") &&
        !res["chat_truncate_to"].is_null()) {
        if (res["chat_truncate_to"].is_number_integer())
            truncateTo = res["chat_truncate_to"].get<int>();
        else if (res["chat_truncate_to"].is_number())
            truncateTo = (int)res["chat_truncate_to"].get<double>();
    }
    if (truncateTo < 0) return;
    if (truncateTo > (int)st.chat.size()) truncateTo = (int)st.chat.size();
    if (truncateTo < (int)st.chat.size()) {
        // ARCHIVE, never destroy. The discarded tail moves into a branch on this session
        // so a revert aimed at the wrong message can be undone — reverting must not be the
        // operation that loses work. Textures are GPU handles owned by the live view, so
        // they are released here; the branch keeps the message data only.
        if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) {
            ChatBranch br;
            br.id = "b" + std::to_string(
                st.conversations[st.activeConv].branches.size() + 1);
            br.when = (double)std::time(nullptr);
            br.fromMsgIdx = truncateTo;
            br.label = "reverted to message " + std::to_string(truncateTo) + " — " +
                       std::to_string((int)st.chat.size() - truncateTo) + " message(s) parked";
            br.msgs.assign(st.chat.begin() + truncateTo, st.chat.end());
            for (auto& m : br.msgs) {
                m.uiCachedH = -1.0f;   // heights are view state, not content
            }
            st.conversations[st.activeConv].branches.push_back(std::move(br));
        }
        // Free textures on the tail before erase (GL-safe via freeChatTextures).
        std::vector<ChatMsg> tail(st.chat.begin() + truncateTo, st.chat.end());
        freeChatTextures(tail);
        st.chat.erase(st.chat.begin() + truncateTo, st.chat.end());
        // Invalidate height caches for remaining messages (layout may shift).
        for (auto& m : st.chat) m.uiCachedH = -1.0f;
        markChatHistoryDirty();  // the branch must reach disk, not just RAM
    }
}

void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast) {
    if (!s_aiVc.available || s_aiVc.busy || to.empty()) return;
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
    st.rpc.call("plm_rollback", p, [&st, okToast, msgTruncateTo](const core::RpcResult& r) {
        s_aiVc.busy = false;
        std::string hint;
        if (!aiVcOk(r, hint)) {
            // Chat-only rewind: the backend has no geometry checkpoint for this message
            // (or no document is open), so plm_rollback refuses — but the user asked to
            // rewind the CHAT to this point, which is meaningful on its own. Truncate
            // locally and say plainly that no geometry was rolled back.
            if (s_aiVc.available && msgTruncateTo >= 0 && aiVcIsNoCheckpoint(r)) {
                nlohmann::json synth = {{"chat_truncate_to", msgTruncateTo}};
                aiVcApplyChatTruncate(st, synth);
                st.toast(ui::ToastLevel::Info,
                         "No geometry checkpoint at that point — rewound the chat only; "
                         "the part was left unchanged.");
                s_aiVc.lastFetch = 0.0;
                return;
            }
            if (s_aiVc.available) st.toast(ui::ToastLevel::Error, hint);
            return;
        }
        aiVcRefreshMirror(st, r.result);
        // Prefer backend chat_truncate_to; if absent and this was a msg:N rewind, synthesize.
        bool hadTruncate = r.result.is_object() && r.result.contains("chat_truncate_to") &&
                           !r.result["chat_truncate_to"].is_null();
        if (hadTruncate) {
            aiVcApplyChatTruncate(st, r.result);
        } else if (msgTruncateTo >= 0) {
            nlohmann::json synth = {{"chat_truncate_to", msgTruncateTo}};
            aiVcApplyChatTruncate(st, synth);
        } else {
            aiVcApplyChatTruncate(st, r.result);
        }
        st.toast(ui::ToastLevel::Success,
                 okToast.empty() ? std::string("Restored checkpoint") : okToast);
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
    if (!localtime_r(&t, &tmv)) return {};
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
    p["chat_size"] = (int)st.chat.size();
    st.rpc.call("plm_rollback_check", p, [&st, to](const core::RpcResult& r) {
        auto& c = s_aiVc.confirm;
        c.busy = false;
        if (c.to != to) return;   // user armed a different target meanwhile
        // Chat-only rewind: no geometry checkpoint at that point. Still confirm,
        // with wording that never claims geometry was restored.
        if (aiVcIsNoCheckpoint(r) && to.rfind("msg:", 0) == 0) {
            c.ok = true;
            c.notFound = true;
            // Chat-only rewind discards the tail after the target message.
            try { c.discardedMessages =
                    std::max(0, (int)st.chat.size() - std::stoi(to.substr(4))); }
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
            // Accept both field names from Main vs WorkingPheasant payloads.
            cp.when = c.value("when", c.value("time", std::string()));
            cp.label = c.value("label", std::string());
            if (c.contains("features") && c["features"].is_number())
                cp.features = c["features"].get<int>();
            else if (c.contains("features_count") && c["features_count"].is_number())
                cp.features = c["features_count"].get<int>();
            // v2: kind / msg_idx / call:{turn,i}
            cp.kind = c.value("kind", std::string());
            cp.sessionId = c.value("session_id", std::string());
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

// Shared rectangular less-rounded header button (optional leading icon).
// Returns true on click. `active` highlights like an open overlay.
bool drawHeaderTextButton(const char* id, const char* label, bool active,
                                 float minW, const char* iconName) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 txt = surf.textPrimary;
    const float round = ui::metrics::rounding(4.0f);  // less-rounded rectangular box
    const float iconSz = ImGui::GetTextLineHeight() * 0.92f;
    const float iconGap = 4.0f * sc;
    IconUV icon = (iconName && iconName[0]) ? ui::getIcon(iconName, 20) : IconUV{};
    const bool haveIcon = icon.valid;

    ImGui::PushID(id);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, round);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(haveIcon ? 6.0f * sc : 8.0f * sc, 2.0f * sc));
    ImGui::PushStyleColor(ImGuiCol_Button, active
        ? ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1], ui::palette.chipBg[2],
                 ui::palette.chipBg[3])
        : ImVec4(txt.x, txt.y, txt.z, 0.06f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(txt.x, txt.y, txt.z, 0.14f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(txt.x, txt.y, txt.z, 0.22f));
    ImGui::PushStyleColor(ImGuiCol_Text, active ? surf.textPrimary : surf.textSecondary);

    const float padX = ImGui::GetStyle().FramePadding.x;
    const float tw = ImGui::CalcTextSize(label).x;
    const float contentW = (haveIcon ? iconSz + iconGap : 0.0f) + tw;
    const float bw = std::max(minW, contentW + padX * 2.0f);
    const float bh = ImGui::GetFrameHeight();

    // Invisible button for hit + style colors; draw icon+label ourselves so the icon tints correctly.
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton("##hit", ImVec2(bw, bh));
    const bool hov = ImGui::IsItemHovered();
    const bool held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec4 bg = active
        ? ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1], ui::palette.chipBg[2],
                 ui::palette.chipBg[3])
        : ImVec4(txt.x, txt.y, txt.z, held ? 0.22f : (hov ? 0.14f : 0.06f));
    dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh),
                      ImGui::ColorConvertFloat4ToU32(bg), round);
    dl->AddRect(p0, ImVec2(p0.x + bw, p0.y + bh),
                ImGui::ColorConvertFloat4ToU32(
                    ImVec4(txt.x, txt.y, txt.z, active ? 0.45f : 0.25f)),
                round, 0, ui::metrics::stroke(1.0f));

    float x = p0.x + padX;
    const float midY = p0.y + bh * 0.5f;
    if (haveIcon) {
        const ImVec4 tint = active ? surf.textPrimary
                                   : ImVec4(surf.textSecondary.x, surf.textSecondary.y,
                                            surf.textSecondary.z, 0.95f);
        dl->AddImage(icon.texId,
                     ImVec2(x, midY - iconSz * 0.5f),
                     ImVec2(x + iconSz, midY + iconSz * 0.5f),
                     icon.uv0, icon.uv1, ImGui::ColorConvertFloat4ToU32(tint));
        x += iconSz + iconGap;
    }
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
        active ? surf.textPrimary : surf.textSecondary);
    const ImVec2 ts = ImGui::CalcTextSize(label);
    dl->AddText(ImVec2(x, midY - ts.y * 0.5f), ink, label);

    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(2);
    ImGui::PopID();
    return clicked;
}

// Cancel staged edit: restore chat tail + composer, NO plm_rollback.
void cancelEditStage(AppState& st) {
    if (!s_edit.active) return;
    // Drop any messages that replaced the parked tail (UI-only truncation during edit).
    if (s_edit.msgIdx >= 0 && s_edit.msgIdx <= (int)st.chat.size()) {
        if (s_edit.msgIdx < (int)st.chat.size()) {
            std::vector<ChatMsg> drop(st.chat.begin() + s_edit.msgIdx, st.chat.end());
            freeChatTextures(drop);
            st.chat.erase(st.chat.begin() + s_edit.msgIdx, st.chat.end());
        }
        for (auto& m : s_edit.chatTail) st.chat.push_back(std::move(m));
    }
    // Restore composer contents.
    if (st.prompt.size() < s_edit.savedPrompt.size() + 1)
        st.prompt.resize(s_edit.savedPrompt.size() + 64, 0);
    std::memset(st.prompt.data(), 0, st.prompt.size());
    if (!s_edit.savedPrompt.empty()) {
        const size_t n = std::min(s_edit.savedPrompt.size(), st.prompt.size() - 1);
        std::memcpy(st.prompt.data(), s_edit.savedPrompt.data(), n);
        st.prompt[n] = 0;
    }
    s_edit = EditStage{};
}

// Begin staged edit of user message at msgIdx: park tail, prefill composer, no rollback yet.
void beginEditStage(AppState& st, int msgIdx) {
    if (msgIdx < 0 || msgIdx >= (int)st.chat.size()) return;
    if (!st.chat[msgIdx].fromUser) return;
    if (s_edit.active) cancelEditStage(st);
    s_edit.active = true;
    s_edit.msgIdx = msgIdx;
    // Snapshot composer.
    s_edit.savedPrompt = st.prompt.data() ? std::string(st.prompt.data()) : std::string();
    // Park [msgIdx..) and drop from live chat (UI-only).
    s_edit.chatTail.assign(st.chat.begin() + msgIdx, st.chat.end());
    st.chat.erase(st.chat.begin() + msgIdx, st.chat.end());
    for (auto& m : st.chat) m.uiCachedH = -1.0f;
    // Prefill composer with the edited message text.
    const std::string& t = s_edit.chatTail.empty() ? std::string() : s_edit.chatTail.front().text;
    if (st.prompt.size() < t.size() + 1) st.prompt.resize(t.size() + 256, 0);
    std::memset(st.prompt.data(), 0, st.prompt.size());
    if (!t.empty()) {
        const size_t n = std::min(t.size(), st.prompt.size() - 1);
        std::memcpy(st.prompt.data(), t.data(), n);
        st.prompt[n] = 0;
    }
}

// Commit staged edit on send: fire msg:N rollback, discard parked tail, clear stage.
void commitEditStageOnSend(AppState& st) {
    if (!s_edit.active) return;
    const int msgIdx = s_edit.msgIdx;
    // Discard parked tail (textures free) — the new send rebuilds the conversation.
    freeChatTextures(s_edit.chatTail);
    s_edit.chatTail.clear();
    s_edit.active = false;
    s_edit.msgIdx = -1;
    s_edit.savedPrompt.clear();
    if (msgIdx >= 0 && s_aiVc.available)
        aiVcRollback(st, "msg:" + std::to_string(msgIdx), "Rewound to edit");
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
