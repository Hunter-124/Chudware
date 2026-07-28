// Copilot shared toolkit (T-RF6 split of legacy_chat.cpp): the shared small-UI helpers
// (primaryButton/sectionHeader/dimText/toggleChip and the code/diff/prose/fit draw helpers
// declared in core/app_state.h), the Liquid Glass v2 bubble toolkit (chatSurf/GlassBubble/
// semanticOnGlass — host contract H5, GlassGroup::Copilot), the chat copy/paste affordances,
// and small model-label/token helpers. Included by every other copilot TU via
// copilot_internal.h.

#include "copilot_internal.h"
#include "ui/tesseract.h"  // ui::drawTesseractSpinner — the brand mark doubles as the busy art

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
    // Text on the accent button: max-contrast ink for the accent fill (dark ink on the teal
    // accent, light ink on the light theme's deeper blue) — no hardcoded tone.
    ImGui::PushStyleColor(ImGuiCol_Text,
        ui::maxContrastTextOn(ImVec4(p.accent[0], p.accent[1], p.accent[2], 1)));
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
    // Dim = the CURRENT text tone at reduced alpha, so the helper stays readable on ANY surface:
    // theme text on opaque panels, adaptive glass ink when a bubble pushed surf.textPrimary
    // (the old fixed palette.textDim was light grey over the light frost — washed out).
    ImVec4 c = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    c.w *= 0.62f;
    ImGui::PushStyleColor(ImGuiCol_Text, c);
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

// A small pill button that highlights when its section is expanded. All tones derive from the
// CURRENT text color (+ the translucent chip accent when active) so the chip reads on opaque
// panels AND on adaptive glass bubbles alike — the old theme-button fill under adaptive
// near-black ink was black-on-black over the glass cards.
bool toggleChip(const char* label, bool active) {
    const auto& p = ui::palette;
    const ImVec4 txt = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
    ImGui::PushStyleColor(ImGuiCol_Button, active
        ? ImVec4(p.chipBg[0], p.chipBg[1], p.chipBg[2], p.chipBg[3])
        : ImVec4(txt.x, txt.y, txt.z, 0.06f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(txt.x, txt.y, txt.z, 0.14f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(txt.x, txt.y, txt.z, 0.22f));
    bool clicked = ImGui::SmallButton(label);
    ImGui::GetWindowDrawList()->AddRect(
        ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
        ImGui::ColorConvertFloat4ToU32(ImVec4(txt.x, txt.y, txt.z, active ? 0.45f : 0.25f)),
        ui::metrics::rounding(6.0f), 0, ui::metrics::stroke(1.0f));
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();
    return clicked;
}


// Panel-local transparency toggle: show/hide the AI's reasoning ("Thinking") blocks.
// Session-only (DESIGN §5 "Show thinking" switch, default on); not persisted. Shared across the
// copilot TUs (extern in copilot_internal.h); ui::assistantThoughtsVisible() is the public accessor.
bool gShowThinking = true;

namespace ui {
bool& assistantThoughtsVisible() { return gShowThinking; }
}  // namespace ui

static void chatNotifyCopied();

// Read-only, copyable monospace code view; height fits the content up to maxH. The ink is
// paired with the box's OWN fill (codeBg) rather than inherited — a caller's adaptive glass
// textPrimary (near-black over the light viewport) on the dark-theme codeBg was black-on-black.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH) {
    const auto& p = ui::palette;
    int lines = 1;
    for (char c : code) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    const float pad = 10.0f * ui::uiScale;
    float h = lh * (float)lines + pad;
    if (h > maxH) h = maxH;
    if (h < lh + pad) h = lh + pad;
    const ImVec4 bg(p.codeBg[0], p.codeBg[1], p.codeBg[2], p.codeBg[3]);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, bg);
    ImGui::PushStyleColor(ImGuiCol_Text, ui::maxContrastTextOn(bg));
    // Mutable buffer required by InputText; ReadOnly prevents edits. Cast-away-const is unsafe
    // if the widget ever wrote — keep a local copy so a const source string is never mutated.
    std::vector<char> buf(code.size() + 1);
    if (!code.empty()) std::memcpy(buf.data(), code.data(), code.size());
    buf[code.size()] = '\0';
    ImGui::InputTextMultiline(id, buf.data(), buf.size(), ImVec2(-1, h),
                              ImGuiInputTextFlags_ReadOnly | ImGuiInputTextFlags_NoUndoRedo);
    const ImGuiID itemId = ImGui::GetItemID();
    const ImRect boxRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
    // Ctrl+C toast — InputText already put the selection on the clipboard.
    if (ImGui::IsItemActive() || ImGui::IsItemFocused()) {
        const bool copyChord =
            (ImGui::GetIO().KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_C, false)) ||
            (ImGui::GetIO().KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_Insert, false));
        if (copyChord) {
            ImGuiInputTextState* st = ImGui::GetInputTextState(itemId);
            if (st && st->HasSelection())
                chatNotifyCopied();
        }
    }
    const std::string sel = [&]() -> std::string {
        ImGuiInputTextState* st = ImGui::GetInputTextState(itemId);
        if (!st || !st->HasSelection() || !st->TextA.Data) return {};
        int a = st->GetSelectionStart(), b = st->GetSelectionEnd();
        if (a > b) { int t = a; a = b; b = t; }
        if (a < 0) a = 0;
        if (b > st->TextLen) b = st->TextLen;
        if (b <= a) return {};
        return std::string(st->TextA.Data + a, st->TextA.Data + b);
    }();
    chatCopyContext(boxRect, (std::string(id) + "_copy").c_str(), code, code, sel);
    ImGui::PopStyleColor(2);
    if (mono) ImGui::PopFont();
}


// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH) {
    const auto& p = ui::palette;
    int lines = 1;
    for (char c : diff) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f * ui::uiScale;
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
    {
        const ImRect r(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        // Unique popup id per call site (id already unique: "##actdiff" / "##actcode" etc.).
        chatCopyContext(r, (std::string(id) + "_copy").c_str(), diff);
    }
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

// ---- copilot MENU chrome (Sessions / Tasks / Artifacts / Quick settings) --------------------
// The four header views live in ONE scroll region and must read as one product, so their
// chrome comes from here rather than each view inventing its own title treatment, spacer
// stack and rule. Sizes come from ui::metrics::cpMenu*; tones from chatSurf() so the menus
// stay legible over any viewport backdrop.
namespace ui {

// Title (medium font) + optional one-line subtitle + optional accessory control (drawn just
// above the rule — e.g. the Sessions "Show archived" checkbox) + a hairline rule. Emits at
// the current cursor and leaves it on the first body row.
void cpMenuHeader(const ui::Fonts& fonts, const char* title, const char* subtitle,
                  const std::function<void()>& accessory) {
    const ui::AdaptiveSurface& surf = chatSurf();
    if (fonts.medium) ImGui::PushFont(fonts.medium);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(title);
    ImGui::PopStyleColor();
    if (fonts.medium) ImGui::PopFont();
    if (subtitle && subtitle[0]) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", subtitle);
        ImGui::PopStyleColor();
    }
    if (accessory) accessory();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));
    const float w = ImGui::GetContentRegionAvail().x;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImVec4 rule = surf.textTertiary;
    rule.w *= 0.35f;
    ImGui::GetWindowDrawList()->AddLine(p, ImVec2(p.x + w, p.y),
                                        ImGui::ColorConvertFloat4ToU32(rule),
                                        ui::metrics::stroke(1.0f));
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));
}

// Sub-section label inside a menu body: a section gap above, the dim label, a hairline, the
// head gap below. `count >= 0` appends the count next to the label.
void cpMenuSection(const char* label, int count) {
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (count >= 0) ImGui::Text("%s  %d", label, count);
    else            ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    const float w = ImGui::GetContentRegionAvail().x;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImVec4 rule = surf.textTertiary;
    rule.w *= 0.30f;
    ImGui::GetWindowDrawList()->AddLine(ImVec2(p.x, p.y + ui::metrics::px(2.0f)),
                                        ImVec2(p.x + w, p.y + ui::metrics::px(2.0f)),
                                        ImGui::ColorConvertFloat4ToU32(rule),
                                        ui::metrics::stroke(1.0f));
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));
}

// Field caption above a full-width control. Always the secondary tone, always the same gap,
// so stacked combos/sliders share one rhythm.
void cpMenuLabel(const char* label) {
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
}

// Dim, wrapped helper copy under a control (one rhythm for every menu hint).
void cpMenuHint(const char* text) {
    ImVec4 c = chatSurf().textTertiary;
    c.w *= 0.9f;
    ImGui::PushStyleColor(ImGuiCol_Text, c);
    ImGui::TextWrapped("%s", text);
    ImGui::PopStyleColor();
}

}  // namespace ui

// ---- Liquid Glass v2 host port (contract H5): glass surface + bubble helpers ----------------
// ONE adaptivePanel id shared by the whole Copilot pane — header, composer, and every card below
// fetch this SAME cached surface via ui::panelId("host_agent_glass"); ui::adaptivePanel is
// idempotent per frame (it re-samples only on the first call each frame, from the full pane rect
// painted in drawAIPanel), so every later fetch below is free regardless of which window/child is
// current when it's called. Tagged GlassGroup::Copilot so the user's per-group appearance knobs
// (tint / opacity / rounding / contrast boost) apply to every copilot surface.
const ui::AdaptiveSurface& chatSurf() {
    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
    return ui::adaptivePanel(ui::panelId("host_agent_glass"), ImRect(mn, mx),
                             ui::GlassGroup::Copilot);
}

// Semantic chroma (accent / status / badge roles) retuned for the surface it is drawn on. The
// theme roles are picked for the theme's OWN panel background; on the frosted bubbles the tone
// under them is whatever the model/viewport shows. Keep the hue but move the weight toward the
// surface's text tone: bright roles darken over a bright frost, dark roles lighten over a dark
// one — so status colors stay readable over ANY model (audit: washed-out teal/amber pattern).
static ImVec4 semanticOnTone(const float role[4], bool brightSurface) {
    ImVec4 c(role[0], role[1], role[2], 1.0f);
    const float lum = 0.2126f * c.x + 0.7152f * c.y + 0.0722f * c.z;
    if (brightSurface && lum > 0.45f) {              // bright hue on a bright frost -> deepen
        c.x *= 0.52f; c.y *= 0.52f; c.z *= 0.52f;
    } else if (!brightSurface && lum < 0.35f) {      // dark hue on a dark frost -> lift
        c.x += (1.0f - c.x) * 0.55f;
        c.y += (1.0f - c.y) * 0.55f;
        c.z += (1.0f - c.z) * 0.55f;
    }
    return c;
}
ImVec4 semanticOnGlass(const float role[4], const ui::AdaptiveSurface& surf) {
    return semanticOnTone(role, surf.darkText);
}

// Per-message mode accent for chat borders (CopilotMode 0..5). mode < 0 → 0 (no border).
// Agent=blue/accent, Plan=amber, Swarm=teal/cyan, Edit=green, Ask=neutral-gray, Eng.Firm=violet.
ImU32 modeBorderColor(int mode) {
    if (mode < 0 || mode > 5) return 0;
    const auto& pal = ui::palette;
    auto withA = [](const float c[4], float a) -> ImU32 {
        return ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], a));
    };
    switch (mode) {
        case 0: return withA(pal.accent, 0.70f);                              // Agent — accent/blue
        case 1: return withA(pal.statusWarn, 0.75f);                          // Plan — amber
        case 2: return IM_COL32(40, 190, 200, 180);                           // Swarm — teal/cyan
        case 3: return withA(pal.statusOk, 0.70f);                            // Edit — green
        case 4: return IM_COL32(150, 155, 165, 160);                          // Ask — neutral gray
        case 5: return IM_COL32(150, 90, 220, 180);                           // Eng.Firm — violet
        default: return 0;
    }
}

// RGB (0..1) counterpart of modeBorderColor — same palette sources for Agent/Plan/Edit,
// same fixed hues for Swarm/Ask/Eng.Firm. Used by the mode-selector popup + trigger chip
// for coloredRowWash gradients (every row gets its mode tint, not only the active one).
void modeAccentRgb(int mode, float out[3]) {
    const auto& pal = ui::palette;
    switch (mode) {
        case 0: // Agent — accent/blue
            out[0] = pal.accent[0]; out[1] = pal.accent[1]; out[2] = pal.accent[2];
            break;
        case 1: // Plan — amber (statusWarn)
            out[0] = pal.statusWarn[0]; out[1] = pal.statusWarn[1]; out[2] = pal.statusWarn[2];
            break;
        case 2: // Swarm — teal/cyan  (matches IM_COL32(40, 190, 200, …))
            out[0] = 40.f / 255.f; out[1] = 190.f / 255.f; out[2] = 200.f / 255.f;
            break;
        case 3: // Edit — green (statusOk)
            out[0] = pal.statusOk[0]; out[1] = pal.statusOk[1]; out[2] = pal.statusOk[2];
            break;
        case 4: // Ask — neutral gray  (matches IM_COL32(150, 155, 165, …))
            out[0] = 150.f / 255.f; out[1] = 155.f / 255.f; out[2] = 165.f / 255.f;
            break;
        case 5: // Eng.Firm — violet  (matches IM_COL32(150, 90, 220, …))
            out[0] = 150.f / 255.f; out[1] = 90.f / 255.f; out[2] = 220.f / 255.f;
            break;
        default:
            out[0] = pal.accent[0]; out[1] = pal.accent[1]; out[2] = pal.accent[2];
            break;
    }
}


// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
// Tone-matches the CALLER's current ink rather than chatSurf() — this helper is shared with the
// viewport's assembly readout (viewport_panel.cpp), which must not touch the copilot's cached
// adaptive surface: dark ink in ImGuiCol_Text means a bright surface under us, and vice versa.
void drawFitReadout(const FitReport& fit, bool detailed) {
    if (!fit.present) return;
    const auto& p = ui::palette;
    const ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    const bool brightSurface =
        (0.2126f * ink.x + 0.7152f * ink.y + 0.0722f * ink.z) < 0.5f;
    const ImVec4 good = semanticOnTone(p.fitOk, brightSurface);
    const ImVec4 bad  = semanticOnTone(p.fitBad, brightSurface);
    ImVec4 dim = ink;
    dim.w *= 0.70f;
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

// Draw a run-status glyph (the turning brand mark while running / check / cross / retry ring /
// "skipped" dash / pending hollow dot) centered in a `box`-wide cell at the cursor, then advance
// past it. Shared by the plan checklist and the action cards so every surface shows the same
// animated status art.
void drawStatusGlyph(const std::string& status, float box) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 c(p0.x + box * 0.5f, p0.y + box * 0.5f);
    const float r = box * 0.30f;
    const bool running = status == "running";
    const bool pass    = status == "pass" || status == "done";
    const bool retry   = status == "retry";
    const bool fail    = status == "fail";
    // "skipped" = the step deliberately did NOT run (harness intervention, e.g. the tool-loop
    // guard refusing a repeated call). Informational, never alarming: iterBuilt blue — the same
    // palette role the "built" step badge and the AI-VC tool chip use.
    const bool skipped = status == "skipped";
    ImU32 col = ImGui::ColorConvertFloat4ToU32(
        running || pass ? semanticOnGlass(pal.accent, surf)
        : retry         ? semanticOnGlass(pal.statusWarn, surf)
        : fail          ? semanticOnGlass(pal.statusErr, surf)
        : skipped       ? semanticOnGlass(pal.iterBuilt, surf)
                        : surf.textTertiary);
    const float th = ui::metrics::stroke(1.6f);
    if (running) {
        // Busy state is the brand mark in motion, so a running step, the composer and the copilot
        // header are all visibly the same object.
        if (box >= ui::kTesseractSpinnerMinPx) {
            ui::drawTesseractSpinner(dl, c, box, ImGui::GetTime(), col);
        } else {
            // Below the mark's legibility floor a 16-vertex wireframe is a smudge, so the original
            // comet arc stays.
            float t = (float)ImGui::GetTime();
            const int seg = 14;
            for (int s = 0; s < seg; ++s) {
                float f = (float)s / seg, aa = t * 6.0f + f * 6.2831853f;
                float al = 0.12f + 0.88f * f;
                ImU32 cc = (col & 0x00FFFFFFu) | ((ImU32)(al * 255) << 24);
                dl->AddCircleFilled(ImVec2(c.x + std::cos(aa) * r, c.y + std::sin(aa) * r), th,
                                    cc);
            }
        }
    } else if (pass) {                          // check mark
        dl->AddLine(ImVec2(c.x - r * 0.55f, c.y + r * 0.05f),
                    ImVec2(c.x - r * 0.10f, c.y + r * 0.50f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.10f, c.y + r * 0.50f),
                    ImVec2(c.x + r * 0.60f, c.y - r * 0.50f), col, th);
    } else if (fail) {                          // cross
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y - r * 0.5f),
                    ImVec2(c.x + r * 0.5f, c.y + r * 0.5f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y + r * 0.5f),
                    ImVec2(c.x + r * 0.5f, c.y - r * 0.5f), col, th);
    } else if (retry) {
        dl->AddCircle(c, r, col, 12, th);
    } else if (skipped) {                       // dash: nothing ran, nothing broke
        dl->AddLine(ImVec2(c.x - r * 0.6f, c.y), ImVec2(c.x + r * 0.6f, c.y), col, th);
    } else {
        dl->AddCircle(c, r * 0.8f, col, 12, th);  // pending hollow dot
    }
    ImGui::Dummy(ImVec2(box, box));
}

// The "AI is working" busy affordance for the chat column: the brand mark turning in place, so
// the composer and the copilot header show the same object moving the same way while a run is
// live. ImDrawList-only, driven by GetTime(); colors via palette / chatSurf (theme lint).
// Sized to `box` (logical height of the glyph cell). Caller places it with Dummy/SetCursor.
void drawBusyMark(float box) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 c(p0.x + box * 0.5f, p0.y + box * 0.5f);
    // The forge's brightest element (its strike face) was plain accent — keep that tone.
    const ImU32 col = ImGui::ColorConvertFloat4ToU32(semanticOnGlass(pal.accent, surf));
    if (box >= ui::kTesseractSpinnerMinPx)
        ui::drawTesseractSpinner(dl, c, box, ImGui::GetTime(), col);
    else
        dl->AddCircleFilled(c, box * 0.25f, col);  // legibility floor: a dot still reads as busy
    ImGui::Dummy(ImVec2(box, box));
}

// A "bubble" = an auto-height child painted over an explicit glassPanel fill, ported from
// ui/imgui/src/ui/agent_panel.cpp's GlassBubble. The child's true height is only known after
// EndChild(), so the PARENT draw list is channel-split: content goes to channel 1, and
// endGlassBubble() paints the correctly-sized fill into channel 0 (behind) once the real rect is
// known — measure-then-fill, no frame lag. Child-window content lives in the child's own draw
// list and is unaffected by the parent split. (The struct itself lives in copilot_internal.h.)
GlassBubble beginGlassBubble(float width, ui::GlassLevel level, float rounding) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->ChannelsSplit(2);
    dl->ChannelsSetCurrent(1);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    return GlassBubble{p0, width, level, rounding};
}

ImRect endGlassBubble(const GlassBubble& b) {
    ImGui::PopStyleColor();
    ImVec2 p1 = ImGui::GetItemRectMax();
    if (p1.x < b.origin.x + b.width) p1.x = b.origin.x + b.width;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->ChannelsSetCurrent(0);
    ui::glassPanel(dl, ImRect(b.origin, p1), b.level, b.rounding, chatSurf());
    dl->ChannelsMerge();
    return ImRect(b.origin, p1);
}

// ---- chat copy/paste affordances ---------------------------------------------------------------
// Right-click any bubble/card → "Copy text" / "Copy all" (and "Copy" when a selection is active).
// Prefer BeginPopupContextItem on the owning item (child/bubble) so right-click isn't stolen by
// later OpenPopup calls and so the popup is submitted every frame (gating BeginPopup behind the
// click closes it instantly next frame). Selectable assistant/error bodies use a read-only
// InputTextMultiline (soft-wrapped) so click-drag + Ctrl+C works. Every copy path shares one
// "Copied" fade near the cursor.

struct ChatCopyFlash {
    double born = -1e9;
    ImVec2 pos{};
};
static ChatCopyFlash s_copyFlash;

// Shared confirmation: ~0.8s fade near the cursor (or last mouse pos).
static void chatNotifyCopied() {
    s_copyFlash.born = ImGui::GetTime();
    s_copyFlash.pos  = ImGui::GetIO().MousePos;
}

static void chatCopyToClipboard(const std::string& text) {
    if (text.empty()) return;
    ImGui::SetClipboardText(text.c_str());
    chatNotifyCopied();
}

// Draw the shared "Copied" fade (foreground — sits above the chat). Call once per frame from
// the chat log / panel path so every copy affordance lights the same indicator.
void chatDrawCopyFlash() {
    const double age = ImGui::GetTime() - s_copyFlash.born;
    if (age < 0.0 || age > 0.80) return;
    const float t = (float)(age / 0.80);          // 0..1
    const float a = (1.0f - t) * (1.0f - t);      // ease-out fade
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImVec2 p = s_copyFlash.pos;
    p.y -= 14.0f * sc * t;                        // drift up as it fades
    const char* label = "Copied";
    const ImVec2 ts = ImGui::CalcTextSize(label);
    const ImVec2 pad(6.0f * sc, 3.0f * sc);
    const ImVec2 mn(p.x + 10.0f * sc, p.y - ts.y - pad.y * 2.0f);
    const ImVec2 mx(mn.x + ts.x + pad.x * 2.0f, mn.y + ts.y + pad.y * 2.0f);
    ImDrawList* dl = ImGui::GetForegroundDrawList();
    const ImU32 bg = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.darkText ? 0.12f : 0.92f,
               surf.darkText ? 0.12f : 0.92f,
               surf.darkText ? 0.12f : 0.92f, 0.88f * a));
    const ImU32 fg = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, a));
    const ImU32 edge = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textSecondary.x, surf.textSecondary.y, surf.textSecondary.z, 0.55f * a));
    const float rnd = ui::metrics::rounding(4.0f);
    dl->AddRectFilled(mn, mx, bg, rnd);
    dl->AddRect(mn, mx, edge, rnd, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(mn.x + pad.x, mn.y + pad.y), fg, label);
}

// Soft-wrap preserving hard newlines (paragraph breaks). InputTextMultiline does not wrap, so
// we reflow each hard line into the available width the same way the composer does.
static std::string chatSoftWrap(const std::string& in, float wrapW) {
    if (in.empty() || wrapW <= 1.0f) return in;
    std::string out;
    out.reserve(in.size() + 16);
    size_t i = 0;
    while (i <= in.size()) {
        size_t nl = in.find('\n', i);
        const size_t end = (nl == std::string::npos) ? in.size() : nl;
        // Copy one hard line into a mutable buffer and rewrap it.
        std::string line = in.substr(i, end - i);
        if (!line.empty()) {
            std::vector<char> buf(line.begin(), line.end());
            buf.push_back('\0');
            rewrapInPlace(buf.data(), (int)line.size(), wrapW);
            out.append(buf.data(), line.size());
        }
        if (nl == std::string::npos) break;
        out.push_back('\n');
        i = nl + 1;
    }
    return out;
}

// Pull the active selection (UTF-8) from an InputText id, if any.
std::string chatInputSelection(ImGuiID id) {
    if (id == 0) return {};
    ImGuiInputTextState* state = ImGui::GetInputTextState(id);
    if (!state || !state->HasSelection() || !state->TextA.Data) return {};
    int a = state->GetSelectionStart();
    int b = state->GetSelectionEnd();
    if (a > b) { int t = a; a = b; b = t; }
    if (a < 0) a = 0;
    if (b > state->TextLen) b = state->TextLen;
    if (b <= a) return {};
    return std::string(state->TextA.Data + a, state->TextA.Data + b);
}

// Context menu attached to the last item AND/OR an explicit rect. Submit BeginPopup every
// frame (never gate it). Uses AllowWhenBlockedByActiveItem so a focused selectable InputText
// does not swallow the right-click that should open the menu on the bubble.
void chatCopyContext(const ImRect& rect, const char* popupId,
                            const std::string& copyText,
                            const std::string& copyAll,
                            const std::string& selText) {
    const ImGuiHoveredFlags hovFlags =
        ImGuiHoveredFlags_AllowWhenBlockedByPopup |
        ImGuiHoveredFlags_AllowWhenBlockedByActiveItem;
    const bool overItem = ImGui::IsItemHovered(hovFlags);
    const bool overRect = ImGui::IsMouseHoveringRect(rect.Min, rect.Max, /*clip=*/false);
    if ((overItem || overRect) && ImGui::IsMouseReleased(ImGuiMouseButton_Right))
        ImGui::OpenPopup(popupId);

    // Theme the menu like the app's other popups (widgets/interwebz.cpp beginComboImpl):
    // opaque popupBg, glass border, accent hover/active highlight, and — the part that
    // actually matters for the highlight filling the row — that SAME small WindowPadding +
    // ItemSpacing. The stock ImGui popup padding this menu used before (default ~8dp) left a
    // gap between the Selectable highlight and the rounded card edge wide enough to read as
    // "the highlight doesn't fill the box"; the combo's tighter padding is the established
    // convention elsewhere in the app for this exact look, so match it instead of reinventing it.
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]));
    ImGui::PushStyleColor(ImGuiCol_Border,
        ImVec4(pal.glassBorder[0], pal.glassBorder[1], pal.glassBorder[2], pal.glassBorder[3]));
    ImGui::PushStyleColor(ImGuiCol_Header,
        ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2], pal.accentSoft[3]));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered,
        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.30f));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,
        ImVec4(pal.accentMid[0], pal.accentMid[1], pal.accentMid[2], pal.accentMid[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 6.0f * sc);
    // Selectable draws its highlight with FrameRounding (default 0 = square). Match the
    // popup's rounding so the hover box doesn't read as a hard-edged rectangle inside a
    // rounded card.
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.0f * sc);
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, 1.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(6.0f * sc, 6.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(4.0f * sc, 2.0f * sc));
    if (!ImGui::BeginPopup(popupId)) {
        ImGui::PopStyleVar(5);
        ImGui::PopStyleColor(5);
        return;
    }
    const std::string& all = copyAll.empty() ? copyText : copyAll;
    // size_arg.x must be the full content width: with size_arg.x == 0 Selectable sizes the
    // item (and therefore the highlight rect) to the LABEL width only, leaving a blank gap to
    // the right of the text — exactly the look reported. GetContentRegionAvail() is identical
    // on every row (cursor x returns to the padding edge after each NewLine), so each row's
    // highlight spans the whole popup interior.
    auto copyRow = [&](const char* label, bool enabled, const std::string& text) {
        ImGui::BeginDisabled(!enabled);
        const bool clicked = ImGui::Selectable(label, false, ImGuiSelectableFlags_None,
                                               ImVec2(ImGui::GetContentRegionAvail().x, 0.0f));
        ImGui::EndDisabled();
        if (clicked) {  // BeginDisabled already keeps a disabled row from ever registering one
            chatCopyToClipboard(text);
            ImGui::CloseCurrentPopup();
        }
    };
    if (!selText.empty()) {
        copyRow("Copy", true, selText);
        ImGui::Separator();
    }
    copyRow("Copy text", !copyText.empty(), copyText);
    copyRow("Copy all", !all.empty(), all);
    ImGui::EndPopup();
    ImGui::PopStyleVar(5);
    ImGui::PopStyleColor(5);
}



// Read-only, soft-wrapped, selectable multiline body. Returns the InputText item id so the
// outer bubble can offer a selection-aware context menu. Ctrl+C (native) triggers the toast.
ImGuiID chatDrawSelectableText(const char* id, const std::string& plain,
                                      const ImVec4& col) {
    if (plain.empty()) return 0;
    const ImGuiStyle& style = ImGui::GetStyle();
    float wrapW = ImGui::GetContentRegionAvail().x - style.FramePadding.x * 2.0f;
    if (wrapW < 1.0f) wrapW = 1.0f;
    const std::string soft = chatSoftWrap(plain, wrapW);
    int lines = 1;
    for (char c : soft) if (c == '\n') ++lines;
    const float lh = ImGui::GetTextLineHeight();
    // Match TextWrapped visual density: no extra frame padding beyond a hairline.
    const float padY = 1.0f * ui::uiScale;
    float h = lh * (float)lines + padY * 2.0f;
    if (h < lh + padY * 2.0f) h = lh + padY * 2.0f;

    // Mutable buffer for InputText (contents are read-only via flags). While the widget is
    // active ImGui keeps its own TextA copy, so a stack buffer is fine frame-to-frame.
    std::vector<char> buf(soft.size() + 1);
    if (!soft.empty())
        std::memcpy(buf.data(), soft.data(), soft.size());
    buf[soft.size()] = '\0';

    ImGui::PushStyleColor(ImGuiCol_FrameBg,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,  ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Border,         ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Text,           col);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0.0f, padY));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0.0f);
    ImGui::InputTextMultiline(id, buf.data(), buf.size(), ImVec2(-1.0f, h),
                              ImGuiInputTextFlags_ReadOnly | ImGuiInputTextFlags_NoUndoRedo |
                              ImGuiInputTextFlags_NoHorizontalScroll);

    const ImGuiID itemId = ImGui::GetItemID();
    // Ctrl+C toast — InputText already put the selection on the clipboard.
    if (ImGui::IsItemActive() || ImGui::IsItemFocused()) {
        const bool copyChord =
            (ImGui::GetIO().KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_C, false)) ||
            (ImGui::GetIO().KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_Insert, false));
        if (copyChord) {
            ImGuiInputTextState* st = ImGui::GetInputTextState(itemId);
            if (st && st->HasSelection())
                chatNotifyCopied();
        }
    }
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(5);
    return itemId;
}

// Tiny top-right "copy" glyph on hover of assistant/error bubbles (cheap, no atlas dependency).
void chatHoverCopyGlyph(const ImRect& rect, const std::string& text) {
    if (text.empty()) return;
    if (!ImGui::IsMouseHoveringRect(rect.Min, rect.Max, /*clip=*/false)) return;
    const float sc = ui::uiScale;
    const float sz = 14.0f * sc;
    const float pad = 4.0f * sc;
    const ImVec2 mn(rect.Max.x - sz - pad, rect.Min.y + pad);
    const ImVec2 mx(mn.x + sz, mn.y + sz);
    // Hit target so a click copies without needing the context menu. Restore layout cursor
    // afterwards so the glyph never opens a gap under the bubble.
    const ImVec2 saved = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(mn);
    ImGui::InvisibleButton("##chat_copy_glyph", ImVec2(sz, sz));
    const bool hov = ImGui::IsItemHovered();
    const bool clk = ImGui::IsItemClicked();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = chatSurf();
    const float a = hov ? 0.95f : 0.55f;
    const ImU32 col = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textSecondary.x, surf.textSecondary.y, surf.textSecondary.z, a));
    if (hov)
        dl->AddRectFilled(mn, mx,
            ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                  surf.textPrimary.z, 0.12f)),
            ui::metrics::rounding(3.0f));
    // Two overlapping rounded rects = clipboard glyph.
    const float r = 1.6f * sc;
    const float inset = 2.6f * sc;
    dl->AddRect(ImVec2(mn.x + inset + 1.2f * sc, mn.y + inset - 0.6f * sc),
                ImVec2(mx.x - inset + 1.2f * sc, mx.y - inset - 0.6f * sc), col, r, 0,
                ui::metrics::stroke(1.1f));
    dl->AddRect(ImVec2(mn.x + inset - 1.2f * sc, mn.y + inset + 0.8f * sc),
                ImVec2(mx.x - inset - 1.2f * sc, mx.y - inset + 0.8f * sc), col, r, 0,
                ui::metrics::stroke(1.1f));
    if (hov) ImGui::SetTooltip("Copy");
    if (clk) chatCopyToClipboard(text);
    ImGui::SetCursorScreenPos(saved);
}

// Compose a flat copy string for an activity / tool card. Tool/artifact rows pack
// args/err/artifact-path segments behind internal "\x1eERR\x1e" / "\x1eART\x1e" markers
// (feedTurnActivity, activity_timeline.cpp) — split those back out via splitToolRowDetail
// so a raw control byte and the literal marker text never land on the clipboard. Without
// this, right-click "Copy text"/"Copy all" on a tool-call or "Session files" row pasted the
// packed detail verbatim (invisible \x1e bytes + "ART"/"ERR" text mixed into the paste).
std::string chatActivityCopyText(const ActivityItem& a) {
    std::string out;
    if (!a.title.empty()) out = a.title;
    if (!a.reason.empty()) {
        if (!out.empty()) out += "\n";
        out += a.reason;
    }
    if (!a.detail.empty() && a.detail != a.reason) {
        const ui::ToolRowDetail parts = ui::splitToolRowDetail(a.detail);
        if (!parts.args.empty()) {
            if (!out.empty()) out += "\n";
            out += parts.args;
        }
        if (!parts.err.empty()) {
            if (!out.empty()) out += "\n";
            out += parts.err;
        }
        if (!parts.arts.empty()) {
            // "kind\tpath" lines — copy as a clean path list, one per line.
            std::istringstream iss(parts.arts);
            std::string line;
            while (std::getline(iss, line)) {
                if (line.empty()) continue;
                size_t tab = line.find('\t');
                std::string path = (tab == std::string::npos) ? line : line.substr(tab + 1);
                if (path.empty()) continue;
                if (!out.empty()) out += "\n";
                out += path;
            }
        }
    }
    if (out.empty() && !a.stage.empty()) out = a.stage;
    return out;
}




namespace ui {

std::string shortModelName(const std::string& id) {
    std::string s = id;
    size_t slash = s.rfind('/'); if (slash != std::string::npos) s = s.substr(slash + 1);
    size_t colon = s.find(':');  if (colon != std::string::npos) s = s.substr(0, colon);
    return s;
}
std::string fmtTokens(int n) {
    char b[32];
    if (n >= 1000000) std::snprintf(b, sizeof(b), "%.1fM", n / 1000000.0);
    else if (n >= 1000) std::snprintf(b, sizeof(b), "%dk", (n + 500) / 1000);
    else std::snprintf(b, sizeof(b), "%d", n);
    return b;
}
// The active model's total context window (mirrors core default_context_window for the meter).
int effectiveContextWindow(const Settings& s) {
    if (s.contextWindow > 0) return s.contextWindow;
    return ui::defaultContextWindowForModel(s.provider, s.model[0] ? s.model : "");
}

}  // namespace ui
