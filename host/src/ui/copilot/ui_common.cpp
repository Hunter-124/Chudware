// Copilot shared toolkit (T-RF6 split of legacy_chat.cpp): the shared small-UI helpers
// (primaryButton/sectionHeader/dimText/toggleChip and the code/diff/prose/fit draw helpers
// declared in core/app_state.h), the Liquid Glass v2 bubble toolkit (chatSurf/GlassBubble/
// semanticOnGlass — host contract H5, GlassGroup::Copilot), the chat copy/paste affordances,
// and small model-label/token helpers. Included by every other copilot TU via
// copilot_internal.h.

#include "copilot_internal.h"
#include "ui/tesseract.h"  // ui::drawTesseractSpinner — the brand mark doubles as the busy art

// ============================ shared small-UI helpers ============================
// These three are declared in core/app_state.h and called from the copilot module, the settings
// modal and the workspaces, so they are app-wide API with a copilot-local home. The BODIES now
// forward to ui::kit (same style as drawStatusGlyph below) — one accent-button, one section
// caption and one filter chip for the whole product. Deliberately NO pushDensity here: an
// app-wide entry point must inherit the density its host pushed, not impose one.
bool primaryButton(const char* label, const ImVec2& size) {
    return ui::kit::primaryButton(label, /*enabled=*/true, /*disabledReason=*/nullptr, size);
}
void sectionHeader(ImFont* f, const char* text) {
    // The font stays a caller decision (the About popup hands its h1); the kit sizes the row from
    // whatever font is current. `first`: this header never carried a leading gap, callers space it.
    if (f) ImGui::PushFont(f);
    ui::kit::sectionHeader(text, /*icon=*/nullptr, /*trailing=*/nullptr, /*count=*/-1,
                           /*first=*/true);
    if (f) ImGui::PopFont();
}
void dimText(const char* t) { ui::kit::dimText(t); }

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active) {
    // The callers own the flag and flip it themselves on a true return (several write through a
    // settings store rather than a bool), so the kit gets a scratch copy of the current state.
    bool on = active;
    return ui::kit::toggleChip(label, &on);
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


// Dim, wrapped prose (reasoning / notes). growToFit=false (default): fixed-height scrolling box
// at maxH, as always. growToFit=true: a LIVE streaming thinking/thought box — auto-sizes to the
// wrapped text height as it arrives (SetNextWindowSizeConstraints + AutoResizeY) instead of
// scrolling inside a small fixed box, so the card visibly fills out mid-stream; once content
// would exceed maxH (the generous cap, e.g. copilotProseStreamCapH()) it clamps there and falls
// back to the same scrolling presentation as the static case.
//
// At that clamp the box STOPS growing but the text keeps arriving, so a scroll-at-zero box
// freezes on the model's FIRST paragraph and hides everything it is currently thinking —
// exactly the moment the box is worth watching. While growToFit is on, the body therefore
// sticks to the bottom. The stick is released the moment the user scrolls up (detected by
// comparing this frame's scroll against LAST frame's maximum, since a pinned box always sits
// exactly at the previous maximum) and re-armed when they scroll back down, so reading back
// through the reasoning mid-stream is never yanked away.
void drawProseBox(const char* id, const std::string& text, float maxH, bool growToFit) {
    const auto& p = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(p.proseBg[0], p.proseBg[1], p.proseBg[2], p.proseBg[3]));
    ImGuiChildFlags flags = ImGuiChildFlags_Border;
    ImVec2 size(0, maxH);
    if (growToFit) {
        flags |= ImGuiChildFlags_AutoResizeY;
        size.y = 0;   // ignored when AutoResizeY is set; height comes from the constraint below
        ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0), ImVec2(FLT_MAX, maxH));
    }
    ImGui::BeginChild(id, size, flags);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(p.proseText[0], p.proseText[1], p.proseText[2], 1));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
    if (growToFit) {
        // Per-child persistent state (the child window's own storage), so several streaming
        // boxes on screen at once each track their own reader independently.
        ImGuiStorage* stg = ImGui::GetStateStorage();
        const ImGuiID kPrevMax = ImGui::GetID("##prose_prevmax");
        const float prevMax = stg->GetFloat(kPrevMax, 0.0f);
        const float maxY = ImGui::GetScrollMaxY();
        // Pinned == sitting at the maximum the content had when this frame's scroll value was
        // last settable. 1px of slack absorbs the sub-pixel line-height rounding.
        if (maxY > 0.0f && ImGui::GetScrollY() >= prevMax - 1.0f)
            ImGui::SetScrollY(maxY);
        stg->SetFloat(kPrevMax, maxY);
    }
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

// Per-message mode accent (CopilotMode 0..2). ONE source for both the border and the wash, so
// the two can never drift: Agent=accent, Plan=amber, Ask=neutral.
static const float* modeRole(int mode) {
    const auto& pal = ui::palette;
    switch (mode) {
        case (int)CopilotMode::Plan: return pal.statusWarn;
        case (int)CopilotMode::Ask:  return pal.textDim;
        default:                     return pal.accent;
    }
}

// mode outside 0..kCopilotModeCount-1 → 0 (no border).
ImU32 modeBorderColor(int mode) {
    if (mode < 0 || mode >= kCopilotModeCount) return 0;
    static const float kAlpha[kCopilotModeCount] = {0.70f, 0.75f, 0.63f};
    const float* r = modeRole(mode);
    return ImGui::ColorConvertFloat4ToU32(ImVec4(r[0], r[1], r[2], kAlpha[mode]));
}

// RGB (0..1) counterpart of modeBorderColor. Used by the mode-selector popup + trigger chip for
// coloredRowWash gradients (every row gets its mode tint, not only the active one).
void modeAccentRgb(int mode, float out[3]) {
    const float* r = modeRole(mode);
    out[0] = r[0];
    out[1] = r[1];
    out[2] = r[2];
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

// The status-mark radius as a fraction of its glyph CELL. ui::kit::statusGlyph takes the radius
// and reconstructs the cell from this same ratio (kit.cpp kGlyphRadiusFrac), so a caller that
// reserves `box` gets art identical to every other status mark in the app.
static constexpr float kStatusGlyphRadiusFrac = 0.30f;

ui::kit::Status chatRowStatus(const std::string& status) {
    if (status == "running")                    return ui::kit::Status::Running;
    if (status == "pass" || status == "done")   return ui::kit::Status::Done;
    if (status == "retry")                      return ui::kit::Status::Retry;
    if (status == "fail" || status == "error")  return ui::kit::Status::Failed;
    // "skipped" = the step deliberately did NOT run (harness intervention, e.g. the tool-loop
    // guard refusing a repeated call). Informational, never alarming.
    if (status == "skipped")                    return ui::kit::Status::Skipped;
    return ui::kit::Status::Pending;
}

// Draw a run-status glyph (the turning brand mark while running / check / cross / retry ring /
// "skipped" dash / pending hollow dot) centered in a `box`-wide cell at the cursor, then advance
// past it. Shared by the plan checklist, the action cards and the timeline spine.
//
// This WAS the canonical implementation; ui::kit::statusGlyph is the port of it, and the app now
// has exactly one status painter. The wrapper survives because ~seven call sites spell the status
// as the harness's raw string and pass a cell size — translating that at every site would buy
// nothing. chatSurf() is the ink source: the copilot's cached adaptive surface, which the kit's
// own ink-derived surface cannot see from inside a PushStyleColor'd card.
void drawStatusGlyph(const std::string& status, float box) {
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ui::kit::statusGlyph(ImGui::GetWindowDrawList(),
                         ImVec2(p0.x + box * 0.5f, p0.y + box * 0.5f),
                         box * kStatusGlyphRadiusFrac, chatRowStatus(status), chatSurf());
    ImGui::Dummy(ImVec2(box, box));
}

// The "AI is working" busy affordance for the chat column: the brand mark turning in place, so
// the composer and the copilot header show the same object moving the same way while a run is
// live. Sized to `box` (logical height of the glyph cell); the caller places it with
// Dummy/SetCursor. Busy IS the running status mark — same art, same legibility ladder, same
// frame pump (kit::statusGlyph declares itself busy through ui::anim, which is what keeps the
// mark turning at 60 fps instead of the idle loop's 20 Hz).
void drawBusyMark(float box) {
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ui::kit::statusGlyph(ImGui::GetWindowDrawList(),
                         ImVec2(p0.x + box * 0.5f, p0.y + box * 0.5f),
                         box * kStatusGlyphRadiusFrac, ui::kit::Status::Running, chatSurf());
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

    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]));
    ImGui::PushStyleColor(ImGuiCol_Border,
        ImVec4(pal.glassBorder[0], pal.glassBorder[1], pal.glassBorder[2], pal.glassBorder[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 6.0f * sc);
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, 1.0f);
    // Zero padding both axes: any nonzero value is dead space the highlight can never paint
    // over, which is the white strip reported. Text keeps its inset via FramePadding
    // (Selectable renders the label at bb.Min + FramePadding). Corner safety comes from the
    // per-row highlight rounding below, not from padding.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0.0f, 0.0f));
    if (!ImGui::BeginPopup(popupId)) {
        ImGui::PopStyleVar(4);
        ImGui::PopStyleColor(2);
        return;
    }
    const std::string& all = copyAll.empty() ? copyText : copyAll;
    // The highlight is drawn MANUALLY (draw list), not via Selectable's own RenderFrame:
    //  - Selectable can only paint inside its own bounding box, and sizing that box off
    //    GetContentRegionAvail()/GetWindowContentRegionMax() collapses on the popup's
    //    auto-size frame (those report ~0 while ImGui is still measuring), which shrank the
    //    whole menu to a sliver.
    //  - Instead the row is sized off the LABEL width (never zero, stable across frames),
    //    Selectable's own highlight is made transparent, and the hover fill is drawn from
    //    the window's inner left edge to its inner right edge — a true edge-to-edge bleed.
    //  - First/last rows get top/bottom corner rounding matching PopupRounding so the fill
    //    follows the card's rounded corners instead of poking square corners past them.
    auto copyRow = [&](const char* label, bool enabled, const std::string& text,
                       ImDrawFlags cornerRound = ImDrawFlags_RoundCornersNone) {
        ImGui::BeginDisabled(!enabled);
        ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_HeaderActive, ImVec4(0, 0, 0, 0));
        const ImGuiStyle& st = ImGui::GetStyle();
        const float border = st.WindowBorderSize;
        const float winW = ImGui::GetWindowWidth();
        const float textW = ImGui::CalcTextSize(label).x + 2.0f * st.FramePadding.x;
        // On the auto-size frame winW is tiny — fall back to the label width so the popup
        // measures itself from the widest row; from the next frame on, winW - 2*border is
        // the full interior and the fill bleeds edge to edge.
        const float rowW = ImMax(winW - 2.0f * border, textW) - 8.0f * sc;
        // Indent the LABEL only — the hover fill is drawn from the window's inner edge
        // regardless of cursor x, so this nudges the text right without shrinking the fill.
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + 8.0f * sc);
        const ImVec2 pos = ImGui::GetCursorScreenPos();
        const bool clicked = ImGui::Selectable(label, false, ImGuiSelectableFlags_None,
                                               ImVec2(rowW, 0.0f));
        const bool hovered = ImGui::IsItemHovered();
        ImGui::PopStyleColor(3);
        ImGui::EndDisabled();
        if (hovered && enabled) {
            const ImVec2 winPos = ImGui::GetWindowPos();
            const float rowH = ImGui::GetItemRectSize().y;
            ImGui::GetWindowDrawList()->AddRectFilled(
                ImVec2(winPos.x + border, pos.y),
                ImVec2(winPos.x + winW - border, pos.y + rowH),
                ImGui::GetColorU32(ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.30f)),
                6.0f * sc, cornerRound);
        }
        if (clicked) {  // BeginDisabled already keeps a disabled row from ever registering one
            chatCopyToClipboard(text);
            ImGui::CloseCurrentPopup();
        }
    };
    if (!selText.empty()) {
        copyRow("Copy", true, selText, ImDrawFlags_RoundCornersTop);
        ImGui::Separator();
        copyRow("Copy text", !copyText.empty(), copyText);
        copyRow("Copy all", !all.empty(), all, ImDrawFlags_RoundCornersBottom);
    } else {
        copyRow("Copy text", !copyText.empty(), copyText, ImDrawFlags_RoundCornersTop);
        copyRow("Copy all", !all.empty(), all, ImDrawFlags_RoundCornersBottom);
    }
    ImGui::EndPopup();
    ImGui::PopStyleVar(4);
    ImGui::PopStyleColor(2);
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
// The active model's total context window for the meter, resolved in strict precedence —
// first hit wins:
//   1. settings.contextWindow — the user's explicit override in AI settings. Beats everything;
//      a local endpoint no catalog knows about is exactly why the override exists.
//   2. Cached model catalog metadata for the selected (provider, model): the real window the
//      provider reports, so a known model shows its capacity instead of a guess.
//   3. ui::defaultContextWindowForModel() with that catalog row folded into a ModelInfo probe.
//      Its substring guess table is the last resort when the catalog has no usable row.
// This is no longer a plain mirror of core default_context_window: the most authoritative value
// of all is st.lastContextWindow (what the server reported for the model that ACTUALLY ran),
// which outranks every branch here. The composer's context meter applies it — this function only
// ever sees Settings, i.e. what the picker currently selects.
int effectiveContextWindow(const Settings& s) {
    if (s.contextWindow > 0) return s.contextWindow;
    const std::string model   = s.model[0] ? s.model : "";
    const std::string baseUrl = s.baseUrl;
    // bridgeModelMetaForCad() is a linear scan of the bridge catalog and this is called from
    // per-frame draw code, so memoise the resolved window on (provider, model, base_url) plus the
    // catalog's identity — refreshBridgeModelCatalog() swaps in a whole new vector, so its data
    // pointer/size moving is what un-sticks a miss recorded while the cache was still cold.
    struct Memo {
        bool                   valid = false;
        int                    provider = -1;
        std::string            model, baseUrl;
        const BridgeModelMeta* catBegin = nullptr;
        size_t                 catSize = 0;
        int                    window = 0;
    };
    static Memo memo;
    const std::vector<BridgeModelMeta>& cat = bridgeModelCatalog();
    if (!memo.valid || memo.provider != s.provider || memo.model != model ||
        memo.baseUrl != baseUrl || memo.catBegin != cat.data() || memo.catSize != cat.size()) {
        int              win = 0;
        ModelInfo        probe;
        const ModelInfo* probeP = nullptr;
        if (const BridgeModelMeta* bm = bridgeModelMetaForCad(s.provider, model, s.baseUrl)) {
            if (bm->contextWindow > 0) win = bm->contextWindow;
            probe  = modelInfoFromProbe(s.provider, model, bm->name, bm->contextWindow,
                                        bm->maxTokens, bm->vision, bm->tools,
                                        bm->reasoningEfforts, "catalog");
            probeP = &probe;
        }
        // The probe carries that same catalog window, so this only differs from `win` when the
        // row exists but reports no window (0) — then the id-pattern table answers.
        if (win <= 0) win = defaultContextWindowForModel(s.provider, model, probeP);
        memo.valid    = true;
        memo.provider = s.provider;
        memo.model    = model;
        memo.baseUrl  = baseUrl;
        memo.catBegin = cat.data();
        memo.catSize  = cat.size();
        memo.window   = win;
    }
    return memo.window;
}

}  // namespace ui
