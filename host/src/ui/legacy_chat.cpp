// AI-CAD legacy Design panel (extracted from gui_app.cpp).
//
// Also home to the shared small-UI helpers (primaryButton/sectionHeader/dimText/toggleChip and
// the code/diff/prose/fit/plan/parts draw helpers) declared in core/app_state.h — they live here
// because the Design panel is their primary user, and are shared with the other panels via the
// header.
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <string>

#include "imgui.h"

#include "legacy_chat.h"
#include "metrics.h"          // ui::metrics::px/U/asmHeaderIconGap
#include "activity_panel.h"  // C22b: for casting st.activityPanel
#include "console_panel.h"   // drawConsoleBody for the consolidated AI panel's Console tab

#include "../core/app_state.h"
#include "../core/jobs.h"

#include "../worker_client.h"

// ============================ shared small-UI helpers ============================
bool primaryButton(const char* label, const ImVec2& size) {
    const auto& p = ui::palette;
    // Primary button always uses the accent color regardless of theme.
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(p.accent[0], p.accent[1], p.accent[2], 1));
    // Hovered: lighten for dark theme, darken for light (just brighten accent channel).
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(p.accent[0]*1.15f > 1.f ? 1.f : p.accent[0]*1.15f,
               p.accent[1]*1.08f > 1.f ? 1.f : p.accent[1]*1.08f,
               p.accent[2]*1.08f > 1.f ? 1.f : p.accent[2]*1.08f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
        ImVec4(p.accentDim[0], p.accentDim[1], p.accentDim[2], 1));
    // Text on the accent button: dark for light-themed accent, dark for teal accent.
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.04f, 0.10f, 0.10f, 1));
    bool r = ImGui::Button(label, size);
    ImGui::PopStyleColor(4);
    return r;
}
void sectionHeader(ImFont* f, const char* text) {
    if (f) ImGui::PushFont(f);
    ImGui::TextUnformatted(text);
    if (f) ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
}
void dimText(const char* t) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                                ui::palette.textDim[2], 1));
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active) {
    if (active) ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1],
               ui::palette.chipBg[2], ui::palette.chipBg[3]));
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
    const auto& p = ui::palette;
    int lines = 1;
    for (char c : diff) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(p.codeBg[0], p.codeBg[1], p.codeBg[2], p.codeBg[3]));
    ImGui::BeginChild(id, ImVec2(0, h), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_HorizontalScrollbar);
    size_t start = 0;
    while (start <= diff.size()) {
        size_t nl = diff.find('\n', start);
        std::string ln = diff.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
        ImVec4 col(p.diffDefault[0], p.diffDefault[1], p.diffDefault[2], 1);
        if (ln.rfind("+++", 0) == 0 || ln.rfind("---", 0) == 0)
            col = ImVec4(p.diffHdr[0], p.diffHdr[1], p.diffHdr[2], 1);
        else if (ln.rfind("@@", 0) == 0)
            col = ImVec4(p.diffHunk[0], p.diffHunk[1], p.diffHunk[2], 1);
        else if (!ln.empty() && ln[0] == '+')
            col = ImVec4(p.diffAdd[0], p.diffAdd[1], p.diffAdd[2], 1);
        else if (!ln.empty() && ln[0] == '-')
            col = ImVec4(p.diffDel[0], p.diffDel[1], p.diffDel[2], 1);
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
    const auto& p = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(p.proseBg[0], p.proseBg[1], p.proseBg[2], p.proseBg[3]));
    ImGui::BeginChild(id, ImVec2(0, maxH), ImGuiChildFlags_Border);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(p.proseText[0], p.proseText[1], p.proseText[2], 1));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed) {
    if (!fit.present) return;
    const auto& p = ui::palette;
    const ImVec4 good(p.fitOk[0], p.fitOk[1], p.fitOk[2], 1);
    const ImVec4 bad(p.fitBad[0], p.fitBad[1], p.fitBad[2], 1);
    const ImVec4 dim(p.fitDim[0], p.fitDim[1], p.fitDim[2], 1);
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
    const auto& pal = ui::palette;
    const ImVec4 accent(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.planCardBg[0], pal.planCardBg[1], pal.planCardBg[2], pal.planCardBg[3]));
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    // Accent bar: use the same accent color with alpha from planAccentBar role.
    dl->AddRectFilled(w0, ImVec2(w0.x + 3.0f, w0.y + ImGui::GetWindowHeight()),
                      ui::Palette::toU32(pal.accent) & 0x00FFFFFFu |
                      ((uint32_t)(unsigned char)pal.planAccentBar << 24));
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
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        ImGui::ColorButton("##pdot", partColor((int)i),
                           ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                           ImVec2(10 * ui::uiScale, 10 * ui::uiScale));
        ImGui::SameLine(0, 7 * ui::uiScale);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(p.name.c_str());
        ImGui::PopFont();
        if (!p.purpose.empty()) dimText(p.purpose.c_str());
        if (!p.dims.empty()) {
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.planDims[0], pal.planDims[1], pal.planDims[2], 1));
            ImGui::TextWrapped("%s  (mm)", p.dims.c_str());
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.planFeature[0], pal.planFeature[1], pal.planFeature[2], 1));
        for (const auto& f : p.features) {
            ImGui::Bullet();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", f.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
    if (!plan.fit.empty()) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        dimText(("Fit: " + plan.fit).c_str());
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts) {
    if (parts.empty()) return;
    const auto& p = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(p.partsDim[0], p.partsDim[1], p.partsDim[2], 1));
    ImGui::TextUnformatted("Parts:");
    ImGui::PopStyleColor();
    for (size_t i = 0; i < parts.size(); ++i) {
        ImGui::SameLine();
        std::string lbl = parts[i].name + (i + 1 < parts.size() ? "," : "");
        ImGui::TextColored(partColor((int)i), "%s", lbl.c_str());
    }
}

namespace ui {

// ============================ W3-H: unified AI Copilot pane ============================
// One pane, top-down: header -> input -> mode pills -> conversation (with inline live run
// status + embedded Activity tree) -> collapsing Code / Console footers. Replaces the old
// Design/Edit/Chat tab triad. All backend launch calls are preserved verbatim.

// One pass of the agent loop, rendered as a compact expandable card. Reuses the shared
// code/diff/prose/fit/parts renderers (drawCodeBox/drawDiffBox/...). Refactored out of the
// old conversation loop unchanged in behavior.
static void drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.stepCardBg[0], pal.stepCardBg[1], pal.stepCardBg[2], pal.stepCardBg[3]));
    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    // Build issues are amber, not red: a failed pass is normal mid-loop — the error goes
    // straight back to the model, which usually fixes it.
    ImVec4 badge = s.ok ? (s.done ? accentCol
                               : ImVec4(pal.iterBuilt[0], pal.iterBuilt[1], pal.iterBuilt[2], 1))
                        : ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1);
    const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
    ImGui::PopFont();
    if (!s.dims.empty()) {
        ImGui::SameLine();
        ImGui::PushFont(fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
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
        if (!s.triedThumb) { s.thumb = loadTexture(s.contactSheet); s.triedThumb = true; }
        if (s.thumb.id) {
            float tw = logW < 220.0f ? logW : 220.0f;
            float th = tw * (float)s.thumb.h / (float)s.thumb.w;
            ImGui::Image((ImTextureID)(intptr_t)s.thumb.id, ImVec2(tw, th));
        }
    }

    if (!s.notes.empty()) dimText(s.notes.c_str());
    if (!s.ok && !s.error.empty()) {
        // Calm one-line cause; the raw traceback stays behind a toggle so a self-correcting
        // pass never reads like a crash.
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1));
        ImGui::TextWrapped("%s", errSummary(s.error).c_str());
        ImGui::PopStyleColor();
        dimText("The CAD code failed this pass; the error was fed back to the AI to correct.");
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
    if (s.showThinking && !s.thinking.empty()) drawProseBox("##think", s.thinking, 150.0f);
    if (s.showCode && !s.code.empty()) drawCodeBox("##code_v", s.code, fonts.mono, 260.0f);
    if (s.showDiff && !s.diff.empty()) drawDiffBox("##diff_v", s.diff, fonts.mono, 220.0f);
    if (s.showError && !s.error.empty()) drawCodeBox("##err_v", s.error, fonts.mono, 200.0f);
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// One assistant turn: name + plan card + summary + iteration cards. While running, an inline
// live status line (spinner + phase) with a thin progress accent bar, plus a collapsible
// "Activity" section embedding the orchestrator run tree for this run.
// C45: local models reply in markdown but ImGui renders plain text — the raw ** and ###
// marks read as noise. Strip the common inline marks (bold/italic asterisks, heading hashes,
// backticks) for DISPLAY only; the stored message keeps the original text.
static std::string stripMdMarks(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        char c = in[i];
        if (c == '*' || c == '`') continue;                       // **bold* / *it* / `code`
        if (c == '#' && (i == 0 || in[i - 1] == '\n')) {          // heading hashes at line start
            while (i < in.size() && in[i] == '#') ++i;
            if (i < in.size() && in[i] == ' ') ++i;
            --i;
            continue;
        }
        out.push_back(c);
    }
    return out;
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);

    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(ImVec4(pal.assistantName[0], pal.assistantName[1],
                             pal.assistantName[2], 1), "Assistant");
    ImGui::PopFont();
    if (m.plan.present) drawPlanCard(m.plan, fonts);
    if (!m.text.empty()) {
        if (m.errKind == ERR_SYSTEM)
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.errSystem[0], pal.errSystem[1], pal.errSystem[2], 1));
        else if (m.errKind == ERR_CAD)
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.errCad[0], pal.errCad[1], pal.errCad[2], 1));
        ImGui::TextWrapped("%s", stripMdMarks(m.text).c_str());
        if (m.errKind != ERR_NONE) ImGui::PopStyleColor();
    }

    // iteration trace — one expandable card per pass of the agent loop
    for (size_t si = 0; si < m.steps.size(); ++si) {
        ImGui::PushID((int)si);
        drawAgentStep(m.steps[si], fonts, logW);
        ImGui::PopID();
    }

    if (m.running) {
        // Inline live status: a simple braille spinner + the current phase, then a thin
        // accent progress bar so the pane reads as "working" at a glance.
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int sp = (int)(ImGui::GetTime() * 8.0) & 3;
        ImGui::TextColored(accentCol, "%s", kSpin[sp]);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
        ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..." : m.liveStatus.c_str());
        ImGui::PopStyleColor();
        // thin animated accent progress bar
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, ui::Palette::toU32(pal.accent));
        ImGui::PushStyleColor(ImGuiCol_FrameBg, ui::Palette::toU32(pal.accentSoft));
        float frac = 0.15f + 0.35f * (float)(0.5 + 0.5 * std::sin(ImGui::GetTime() * 2.2));
        ImGui::ProgressBar(frac, ImVec2(-1, 3.0f * ui::uiScale), "");
        ImGui::PopStyleColor(2);

        // For an orchestrator (v2) run, embed the Activity tree inline (collapsed by default).
        // There is no per-run filter API on ActivityPanel, so we host its full body (which
        // already shows the live run first) inside a bounded scroll region, plus a link to the
        // standalone Activity panel for the full view.
        if (st.agentV2Running && st.activityPanel) {
            if (ImGui::CollapsingHeader("Activity")) {
                ImGui::BeginChild("##inlineact", ImVec2(0, 220.0f * ui::uiScale),
                                  ImGuiChildFlags_Border);
                st.activityPanel->drawBody(st);
                ImGui::EndChild();
                if (ImGui::SmallButton("Open Activity panel")) st.showActivityPanel = true;
            }
        }
    }
}

// ---- header: small logo glyph + "AI-CAD Copilot" + status dot (idle/running/error) ----
static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    if (st.logoIcon.id) {
        float h = ImGui::GetTextLineHeight();
        ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(h, h));
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
    }
    ImGui::PushFont(st.fonts.h2);
    ImGui::TextUnformatted("AI-CAD Copilot");
    ImGui::PopFont();
    // status dot: error (red) > running (accent) > idle (dim accent)
    const float* dot = pal.statusOk;
    const char* tip = "Idle";
    if (st.statusKind == 2) { dot = pal.statusErr; tip = "Error"; }
    else if (busy || st.agentV2Running) { dot = pal.accent; tip = "Running"; }
    else { dot = pal.accentMid; tip = "Idle"; }
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - ImGui::GetTextLineHeight());
    ImGui::TextColored(ImVec4(dot[0], dot[1], dot[2], dot[3]), "%s",
                       "\xe2\x97\x8f" /* ● */);
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
}

// ---- mode pill row: [Design new] [Edit part] [Ask] + model/provider label. Wraps to a
//      second row when the pane is too narrow to fit on one line. ----
static void drawModeRow(AppState& st) {
    const auto& pal = ui::palette;
    bool hasDoc = st.docMirror.hasDoc();
    const float sc = ui::uiScale;
    const char* kModes[] = {"Design new", "Edit part", "Ask"};
    const char* kModeTips[] = {
        "Design new — describe a new part and the AI models it from scratch.",
        "Edit part — the orchestrator (v2) edits the open part via validated document ops.",
        "Ask — ask questions about the current part; nothing is modified.",
    };
    float avail = ImGui::GetContentRegionAvail().x;
    const ImGuiStyle& style = ImGui::GetStyle();
    const ImVec2 compactPad(8.0f * sc, 4.0f * sc);

    // Lay out the three pills; wrap onto the next line when the running width would overflow.
    float x = 0.0f;
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, compactPad);
    for (int i = 0; i < 3; ++i) {
        bool active = (st.aiInteraction == i);
        bool disabled = (i == 1 && !hasDoc);   // Edit requires a doc
        float w = ImGui::CalcTextSize(kModes[i]).x + compactPad.x * 2.0f;
        if (i > 0) {
            if (x + style.ItemSpacing.x + w <= avail) { ImGui::SameLine(); x += style.ItemSpacing.x; }
            else { x = 0.0f; }  // wrap
        }
        x += w;
        if (disabled) ImGui::BeginDisabled();
        if (active) ImGui::PushStyleColor(ImGuiCol_Button, ui::Palette::toU32(pal.accent));
        if (ImGui::Button(kModes[i])) st.aiInteraction = i;
        if (active) ImGui::PopStyleColor();
        if (disabled) ImGui::EndDisabled();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", kModeTips[i]);
    }
    ImGui::PopStyleVar();
    if (st.aiInteraction == 1 && !hasDoc) st.aiInteraction = 0;   // fall back when no doc
    st.agentV2 = (st.aiInteraction == 1);                         // Edit == orchestrator v2

    // Model/provider label — clicking opens Settings (do not duplicate settings UI here).
    const char* provNames[] = {"Anthropic", "OpenAI", "Local", "llama.cpp"};
    const char* prov = provNames[(st.settings.provider >= 0 && st.settings.provider < 4)
                                     ? st.settings.provider : 2];
    char label[160];
    std::snprintf(label, sizeof(label), "%s / %s", prov,
                  st.settings.model[0] ? st.settings.model : "default");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, compactPad);
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
    bool supervised = st.settings.planMode;
    const char* toggleLbl = supervised ? "Supervised" : "Autopilot";
    float toggleW = ImGui::CalcTextSize(toggleLbl).x + compactPad.x * 2.0f;
    float modelW = ImGui::GetContentRegionAvail().x;
    bool sameLineToggle = modelW > toggleW + style.ItemSpacing.x + 96.0f * sc;
    if (sameLineToggle) modelW -= toggleW + style.ItemSpacing.x;
    if (ImGui::Button(label, ImVec2(modelW, 0))) st.openSettings = true;
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Model & provider - click to open Settings.\n%s", label);
    if (sameLineToggle) ImGui::SameLine();
    if (supervised) ImGui::PushStyleColor(ImGuiCol_Button, ui::Palette::toU32(pal.accentMid));
    if (ImGui::Button(toggleLbl, ImVec2(sameLineToggle ? toggleW : -1.0f, 0)))
        st.settings.planMode = !st.settings.planMode;
    if (supervised) ImGui::PopStyleColor();
    ImGui::PopStyleVar();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", supervised
            ? "Supervised: the AI plans first and shows the plan before building. Click for Autopilot."
            : "Autopilot: the AI builds without stopping to confirm. Click for Supervised.");
}

// Launch the active turn for `text` according to the current mode (Design/Edit/Ask). Pushes
// the user + live-assistant ChatMsgs. Centralizes the three old launch paths so the top input
// and the per-turn follow-up input share one code path.
static void submitTurn(AppState& st, const std::string& text) {
    if (text.empty()) return;
    { ChatMsg u; u.fromUser = true; u.text = text; st.chat.push_back(u); }
    { ChatMsg a; a.running = true; st.chat.push_back(a); }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)

    bool editMode = (st.aiInteraction == 1);
    bool askMode  = (st.aiInteraction == 2);
    if (editMode && st.docMirror.hasDoc()) {
        // Edit part -> orchestrator v2 (validated document ops).
        st.addLog("[..] editing: " + text);
        st.status = "Editing...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        if (st.activityPanel)
            st.activityPanel->clearLive();
        launchAgentRunV2(st.worker, st.job, text, st.docMirror.docId());
    } else if (askMode) {
        // Ask -> Wave D: question answering about the open part via agent_ask_v2. The worker
        // answers from the doc tree without mutating anything (an empty doc_id is handled
        // there — the assistant falls back to general CAD usage). NOT a job: the call runs on
        // the AsyncRpc dispatch thread and the callback fills the chat on the GL thread.
        if (st.agentV2Running) {
            // A design/edit run is in flight — refuse politely instead of queueing behind it.
            if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                st.chat.back().running = false;
                st.chat.back().text = "A design/edit run is still in progress - "
                                      "ask me again when it finishes.";
            }
            return;
        }
        st.addLog("[..] ask: " + text);
        st.status = "Answering...";  st.statusKind = 0;
        nlohmann::json params = {
            {"doc_id", st.docMirror.hasDoc() ? st.docMirror.docId() : std::string()},
            {"question", text}};
        st.rpc.call("agent_ask_v2", params, [&st](const core::RpcResult& r) {
            // Find the last running assistant message (the one this turn pushed).
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok)
                err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));
            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) {
                    msg->errKind = ERR_SYSTEM;
                    msg->text = "I couldn't answer that: " + err;
                } else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no answer)";
                }
            }
            if (!err.empty()) {
                st.addLog("[err] ask: " + err);
                st.status = "Ask failed.";  st.statusKind = 2;
            } else {
                st.status = "Ready.";  st.statusKind = 0;
            }
        }, 300000);
    } else {
        // Design new -> C40: the v2 orchestrator on a FRESH part document, so "Design with AI"
        // produces a real parametric feature tree. (The legacy build123d loop rendered a dead
        // mesh disconnected from the document — you couldn't edit, dimension, or save what the
        // AI built.) The current doc is replaced only when it already has features.
        std::string err;
        bool docOk = st.docMirror.hasDoc();
        if (docOk) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
        } else {
            docOk = st.docMirror.newPart(st.worker, err);
        }
        if (!docOk) {
            st.addLog("[err] design: could not create a part document: " + err);
            st.status = "Worker not ready";  st.statusKind = 2;
            if (!st.chat.empty() && st.chat.back().running) {
                st.chat.back().running = false;
                st.chat.back().errKind = 2;
                st.chat.back().text = "I couldn't open a new part document (worker not ready). "
                                      "Check the Console and try again.";
            }
            return;
        }
        st.clearViewportForNewDoc();
        st.addLog("[..] designing (v2): " + text);
        st.status = "Designing...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        if (st.activityPanel)
            st.activityPanel->clearLive();
        launchAgentRunV2(st.worker, st.job, text, st.docMirror.docId());
    }
}

static bool hasNonSpace(const char* text) {
    if (!text) return false;
    for (const unsigned char* p = (const unsigned char*)text; *p; ++p)
        if (!std::isspace(*p)) return true;
    return false;
}

static void copyToBuffer(std::vector<char>& buf, const char* text) {
    if (buf.empty()) return;
    std::memset(buf.data(), 0, buf.size());
    if (text) std::strncpy(buf.data(), text, buf.size() - 1);
}

static std::string rpcErrorText(const core::RpcResult& r) {
    if (!r.ok) return r.err.empty() ? "worker call failed" : r.err;
    if (r.result.is_object() && r.result.contains("code"))
        return r.result.value("message", std::string("worker error"));
    return std::string();
}

static void submitHelpQuestion(AppState& st, const std::string& question) {
    if (!hasNonSpace(question.c_str())) return;
    if (!st.workerStarted) {
        st.helpError = "Runtime is not ready yet.";
        st.helpStatus = "Runtime not ready.";
        return;
    }
    if (st.helpBusy) return;
    st.helpBusy = true;
    st.helpLastQuestion = question;
    st.helpAnswer.clear();
    st.helpError.clear();
    st.helpStatus = "Asking...";
    st.addLog("[..] help: " + question);
    nlohmann::json params = {
        {"doc_id", st.docMirror.hasDoc() ? st.docMirror.docId() : std::string()},
        {"question", question}};
    st.rpc.call("agent_ask_v2", params, [&st](const core::RpcResult& r) {
        st.helpBusy = false;
        std::string err = rpcErrorText(r);
        if (!err.empty()) {
            st.helpError = err;
            st.helpAnswer.clear();
            st.helpStatus = "Ask failed.";
            st.status = "Help failed.";
            st.statusKind = 2;
            st.addLog("[err] help: " + err);
            return;
        }
        st.helpAnswer = r.result.value("answer", std::string());
        if (st.helpAnswer.empty()) st.helpAnswer = "(no answer)";
        st.helpError.clear();
        st.helpStatus = "Ready.";
        st.status = "Ready.";
        st.statusKind = 0;
    }, 300000);
}

static void drawHelpTab(AppState& st, bool canRun) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    bool workerReady = st.workerStarted && canRun && !st.agentV2Running;

    ImGui::Dummy(ImVec2(0, 2));
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("AI Help");
    ImGui::PopFont();
    dimText("Ask about the current part, feature tree, dimensions, or CAD workflow. Help mode is read-only.");

    ImGui::Dummy(ImVec2(0, 4));
    const char* examples[] = {
        "What features are in this part?",
        "Why is my sketch under defined?",
        "How should I change this without breaking downstream features?",
    };
    float avail = ImGui::GetContentRegionAvail().x;
    const ImGuiStyle& style = ImGui::GetStyle();
    float rowW = 0.0f;
    for (int i = 0; i < 3; ++i) {
        float w = ImGui::CalcTextSize(examples[i]).x + style.FramePadding.x * 2.0f;
        if (i > 0) {
            if (rowW + style.ItemSpacing.x + w <= avail) {
                ImGui::SameLine();
                rowW += style.ItemSpacing.x;
            } else {
                rowW = 0.0f;
            }
        }
        rowW += w;
        if (ImGui::SmallButton(examples[i])) copyToBuffer(st.helpQuestion, examples[i]);
    }

    if (!st.selectedFeatureId.empty()) {
        ImGui::Dummy(ImVec2(0, 3));
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::Text("Selected feature: %s", st.selectedFeatureId.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton("Ask about selection")) {
            std::string q = "Explain feature " + st.selectedFeatureId +
                            " and suggest safe edits I can make to it.";
            copyToBuffer(st.helpQuestion, q.c_str());
        }
    }

    ImGui::Dummy(ImVec2(0, 6));
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextMultiline("##help_question", st.helpQuestion.data(), st.helpQuestion.size(),
                              ImVec2(-1, ImGui::GetTextLineHeight() * 3.0f));
    if (st.helpQuestion[0] == 0) {
        ImVec2 mn = ImGui::GetItemRectMin();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(mn.x + ImGui::GetStyle().FramePadding.x * 2,
                   mn.y + ImGui::GetStyle().FramePadding.y),
            ui::Palette::toU32(pal.textDisabled), "Ask for help with this model...");
    }
    bool ctrlEnter = ImGui::IsItemFocused() && ImGui::GetIO().KeyCtrl &&
                     ImGui::IsKeyPressed(ImGuiKey_Enter, false);
    bool hasQuestion = hasNonSpace(st.helpQuestion.data());
    bool canAsk = workerReady && hasQuestion && !st.helpBusy;
    if (!canAsk) ImGui::BeginDisabled();
    bool ask = primaryButton(st.helpBusy ? "Asking..." : "Ask Help", ImVec2(-1, 32.0f * sc));
    if (!canAsk) ImGui::EndDisabled();
    if ((ask || ctrlEnter) && canAsk) submitHelpQuestion(st, std::string(st.helpQuestion.data()));
    if (ImGui::IsItemHovered() && !workerReady)
        ImGui::SetTooltip("Runtime/worker must be ready and no design/edit run can be active.");

    ImGui::Dummy(ImVec2(0, 8));
    if (st.helpBusy) {
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int sp = (int)(ImGui::GetTime() * 8.0) & 3;
        ImGui::TextColored(ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1),
                           "%s Asking the AI helper...", kSpin[sp]);
    } else if (!st.helpStatus.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted(st.helpStatus.c_str());
        ImGui::PopStyleColor();
    }

    if (!st.helpError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.errSystem[0], pal.errSystem[1], pal.errSystem[2], 1));
        ImGui::TextWrapped("I couldn't answer that: %s", st.helpError.c_str());
        ImGui::PopStyleColor();
    }
    if (!st.helpLastQuestion.empty() || !st.helpAnswer.empty()) {
        ImGui::SeparatorText("Answer");
        if (!st.helpLastQuestion.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextWrapped("Q: %s", st.helpLastQuestion.c_str());
            ImGui::PopStyleColor();
        }
        if (!st.helpAnswer.empty()) drawProseBox("##help_answer", st.helpAnswer, 260.0f * sc);
    }
}

// ---- runtime-not-ready banner (kept from the old Design body) ----
static void drawRuntimeBanner(AppState& st) {
    if (st.workerStarted) return;
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.setupWarnBg[0], pal.setupWarnBg[1], pal.setupWarnBg[2], pal.setupWarnBg[3]));
    ImGui::BeginChild("setup", ImVec2(0, 0), ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    if (st.setupRunning.load()) {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Setting up runtime...");
        dimText("First run installs an isolated Python + build123d (~once).");
    } else {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Runtime not ready");
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
        if (ImGui::Button("Try setup now")) st.startSetup(false);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
}

// ---- unified AI Copilot pane ----
void drawAIPanel(AppState& st, bool busy, bool canRun) {
    if (!st.showAIPanel) return;
    if (!ImGui::Begin("AI")) { ImGui::End(); return; }
    ui::Fonts& fonts = st.fonts;
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);

    drawCopilotHeader(st, busy);
    ImGui::Separator();
    drawRuntimeBanner(st);

    if (ImGui::BeginTabBar("##ai_tabs")) {
    if (ImGui::BeginTabItem("Design")) {

    // ---- INPUT AT TOP — always visible, multiline; Ctrl+Enter sends, Enter = newline ----
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextMultiline("##aiprompt", st.prompt.data(), st.prompt.size(),
                              ImVec2(-1, ImGui::GetTextLineHeight() * 2.4f));
    // Ctrl+Enter sends (only while the input is focused); plain Enter inserts a newline.
    bool ctrlEnter = ImGui::IsItemFocused() && ImGui::GetIO().KeyCtrl &&
                     ImGui::IsKeyPressed(ImGuiKey_Enter, false);
    // Placeholder hint when empty (InputTextMultiline has no hint variant).
    if (st.prompt[0] == 0) {
        ImVec2 mn = ImGui::GetItemRectMin();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(mn.x + ImGui::GetStyle().FramePadding.x * 2,
                   mn.y + ImGui::GetStyle().FramePadding.y),
            ui::Palette::toU32(pal.textDisabled), "Describe a part or a change\xE2\x80\xA6");
    }
    // mode pills + model label (wrapping row)
    drawModeRow(st);
    // send button
    bool canStart = canRun && st.prompt[0];
    if (!canStart) ImGui::BeginDisabled();
    const char* sendLabel = st.aiInteraction == 0 ? "Design with AI"
                          : st.aiInteraction == 1 ? "Edit with AI" : "Send";
    bool goSend = primaryButton(sendLabel, ImVec2(-1, 32 * ui::uiScale));
    if (!canStart) ImGui::EndDisabled();
    if ((goSend || ctrlEnter) && canStart) {
        submitTurn(st, std::string(st.prompt.data()));
        st.prompt[0] = 0;
    }
    if (ImGui::IsItemHovered() && !canStart && !canRun)
        ImGui::SetTooltip("Runtime/worker not ready yet.");

    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
    ImGui::Separator();

    // ---- CONVERSATION — newest at bottom, auto-scroll. Empty-state suggestion chips. ----
    // "Empty" = no user turn yet (the startup welcome assistant message may be present).
    bool conversationEmpty = true;
    for (const ChatMsg& m : st.chat) if (m.fromUser) { conversationEmpty = false; break; }
    if (conversationEmpty) {
        ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
        dimText("Try:");
        static const char* kExamples[] = {
            "GoPro tripod adapter",
            "Raspberry Pi 5 vented case",
            "an M3 hex standoff, 25mm tall",
        };
        for (int ei = 0; ei < 3; ++ei) {
            ImGui::PushID(ei);
            if (ImGui::Button(kExamples[ei], ImVec2(-1, 0)))
                std::strncpy(st.prompt.data(), kExamples[ei], st.prompt.size() - 1);
            ImGui::PopID();
        }
    }

    // conversation log fills the remaining height above the collapsing footers.
    // C40: reserve space for OPEN footers too (one-frame-lagged open state) — before, an
    // opened Console/code section landed entirely below the window with no scroll path.
    static bool s_codeOpen = false, s_consoleOpen = false;
    float footerReserve = ImGui::GetFrameHeightWithSpacing() * 2.0f + 8.0f;
    if (s_codeOpen)    footerReserve += (140.0f + 30.0f) * ui::uiScale + 24.0f;
    if (s_consoleOpen) footerReserve += 160.0f * ui::uiScale + 12.0f;
    float chatH = ImGui::GetContentRegionAvail().y - footerReserve;
    if (chatH < 120.0f) chatH = 120.0f;
    ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_Border);
    const float logW = ImGui::GetContentRegionAvail().x;
    for (size_t mi = 0; mi < st.chat.size(); ++mi) {
        ChatMsg& m = st.chat[mi];
        ImGui::PushID((int)mi);
        if (m.fromUser) {
            // user turn: a soft accent-tinted bubble
            ImGui::PushStyleColor(ImGuiCol_ChildBg,
                ImVec4(pal.userBubbleBg[0], pal.userBubbleBg[1],
                       pal.userBubbleBg[2], pal.userBubbleBg[3]));
            ImGui::PushStyleColor(ImGuiCol_Border,
                ImVec4(pal.userBubbleBorder[0], pal.userBubbleBorder[1],
                       pal.userBubbleBorder[2], pal.userBubbleBorder[3]));
            ImGui::BeginChild("##umsg", ImVec2(0, 0),
                              ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
            ImGui::PushFont(fonts.medium);
            ImGui::TextColored(accentCol, "You");
            ImGui::PopFont();
            ImGui::TextWrapped("%s", m.text.c_str());
            ImGui::EndChild();
            ImGui::PopStyleColor(2);
            ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
            ImGui::PopID();
            continue;
        }
        drawAssistantTurn(st, m, fonts, logW);
        ImGui::Dummy(ImVec2(0, 8 * ui::uiScale));
        ImGui::PopID();
    }
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 6) ImGui::SetScrollHereY(1.0f);
    ImGui::EndChild();

    // ---- footer: New part + collapsing Code / Console (kept reachable) ----
    if (busy) ImGui::BeginDisabled();
    if (ImGui::SmallButton("+ New part")) {
        freeChatTextures(st.chat);
        st.chat.clear();
        st.designStarted = false;
        st.modelInfo.clear();
        st.livePhase.clear();
        st.asmParts.clear();
        st.asmFit = FitReport{};
        st.asmStl.clear();
        st.selectedPart = -1;
        st.prompt[0] = 0;
        st.status = st.workerStarted ? "Ready." : st.status;
        st.statusKind = 0;
    }
    if (busy) ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::SmallButton("Activity panel")) st.showActivityPanel = true;

    // build123d code editor — folded away by default (was always-visible before).
    s_codeOpen = ImGui::CollapsingHeader("build123d code");
    if (s_codeOpen) {
        ImGui::PushFont(fonts.mono);
        ImGui::InputTextMultiline("##code", st.code.data(), st.code.size(),
                                  ImVec2(-1, 140 * ui::uiScale));
        ImGui::PopFont();
        if (!canRun) ImGui::BeginDisabled();
        if (ImGui::Button("Build code", ImVec2(-1, 30 * ui::uiScale))) {
            st.addLog("[..] building code");
            st.status = "Building...";  st.statusKind = 0;
            st.buildErrSummary.clear();
            st.buildErrFull.clear();
            launchJob(st.worker, st.job, JOB_BUILD, std::string(st.code.data()), false, false);
        }
        if (!canRun) ImGui::EndDisabled();
        if (!st.buildErrSummary.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1));
            ImGui::TextWrapped("Build issue - %s", st.buildErrSummary.c_str());
            ImGui::PopStyleColor();
            if (toggleChip(st.showBuildErr ? "Hide details" : "Details", st.showBuildErr))
                st.showBuildErr = !st.showBuildErr;
            if (st.showBuildErr && !st.buildErrFull.empty())
                drawCodeBox("##builderr", st.buildErrFull, fonts.mono, 180.0f);
        }
    }
    s_consoleOpen = ImGui::CollapsingHeader("Console");
    if (s_consoleOpen) {
        ImGui::BeginChild("##consolebody", ImVec2(0, 160 * ui::uiScale), ImGuiChildFlags_Border);
        drawConsoleBody(st);
        ImGui::EndChild();
    }

    ImGui::EndTabItem();
    }
    ImGuiTabItemFlags helpFlags = st.focusHelpTab ? ImGuiTabItemFlags_SetSelected
                                                   : ImGuiTabItemFlags_None;
    if (ImGui::BeginTabItem("Help", nullptr, helpFlags)) {
        drawHelpTab(st, canRun);
        ImGui::EndTabItem();
    }
    st.focusHelpTab = false;
    ImGui::EndTabBar();
    }

    ImGui::End();
}

}  // namespace ui
