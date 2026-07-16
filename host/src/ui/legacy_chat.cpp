// AI-CAD Copilot pane.
//
// A PINNED, full-height, rounded liquid-glass column overlaid on the viewport's right edge with
// ~12dp margins (geometry from ui::viewportBackdrop(); invisible left-edge drag zone to resize;
// the docked task-pane layout is gone — this is the only layout). Top-down: a slim header (outline
// cube + status + Sessions + settings gear), the conversation log with a per-turn transparent activity
// timeline (what the AI is doing / thinking, expandable), and a bottom composer (compact Design/
// Edit/Ask dropdown + context meter; transparent prompt with in-box send; graduated-width segmented
// model/thinking/paperclip/cube/New toolbar — model biggest, icons smallest).
// Header gear swaps the chat area for a compact AI-settings overlay (horizontal top tabs for
// Agents/Prompts/Skills/Permissions/Providers). The full form lives in the Settings modal AI tab.
// Bubbles/cards (user prompt, prose, thinking, action, plan, error) and the composer are painted
// as translucent adaptive Liquid-Glass bubbles over the live viewport backdrop (host contract
// H5, GlassGroup::Copilot; see chatSurf()/GlassBubble/semanticOnGlass below) so every ink tone
// stays readable over ANY model color.
//
// Also home to the shared small-UI helpers (primaryButton/sectionHeader/dimText/toggleChip and
// the code/diff/prose/fit/plan/parts draw helpers) declared in core/app_state.h — they live here
// because the Copilot pane is their primary user, and are shared with the other panels via the
// header.
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <string>
#include <map>
#include <vector>
#include <unordered_map>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect — Liquid Glass v2 host port (contract H5: glass bubbles)

#include "legacy_chat.h"
#include "layout.h"          // ui::overlayBand / takeOverlayColumnReset — pinned-column geometry
#include "panel_collapse.h"  // ui::PanelCollapseAnim — shared vertical-collapse animation (T-G21)
#include "doc_tabs.h"        // ui::docTabStripHeight — top inset so the column clears the tab band
#include "model_registry.h"  // ui::ModelInfo / capability flags / provider mapping
#include "metrics.h"          // ui::metrics::px/U/asmHeaderIconGap
#include "activity_panel.h"  // C22b: ActivityPanel still fed for the AI_CAD_SHOT=activity scenario
#include "console_panel.h"   // drawConsoleBody (shared console helper)
#include "file_dialog.h"     // openFileDialog — composer paperclip attachments (M23)
#include "icons.h"           // getIcon / IconUV — composer settings glyph
#include "widgets/interwebz.h"

#include "../core/app_state.h"
#include "../core/cpp_core.h"  // detect the in-process C++ core (libaicad) backend
#include "../core/sim_client.h"  // chat-triggered OpenFOAM CFD on the SimClient channel
#include "../core/jobs.h"
#include "../ui_theme.h"

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

// ---- auto-growing, wrapping chat input -------------------------------------------------------
// ImGui's InputTextMultiline does NOT word-wrap (it renders with wrap_width=0, so long lines
// extend past the box and get clipped — "run off the screen"). So we maintain the wrapping
// ourselves: rewrapInPlace toggles spaces<->newlines so every visual line fits within wrapW.
// It is SAME-LENGTH (only swaps ' ' <-> '\n'), so cursor indices in the active widget stay
// valid. The box then grows by counting the newlines. Enter submits; Shift+Enter inserts a
// soft break that the next rewrap reflows (a prompt rarely needs hard line breaks).
static bool rewrapInPlace(char* buf, int len, float wrapW) {
    if (len <= 0 || wrapW <= 1.0f) return false;
    bool changed = false;
    // 1) flatten every existing newline back to a space — an idempotent source to re-wrap from
    //    (so widening the box reflows the text instead of leaving stale breaks).
    for (int i = 0; i < len; ++i)
        if (buf[i] == '\n') { buf[i] = ' '; changed = true; }
    // 2) greedy word-wrap: break at the last space that keeps the running line within wrapW.
    int lineStart = 0, lastSpace = -1;
    float lineW = 0.0f;
    for (int i = 0; i < len; ++i) {
        float cw = ImGui::CalcTextSize(buf + i, buf + i + 1).x;
        if (lineW + cw > wrapW && lastSpace >= lineStart) {
            buf[lastSpace] = '\n';
            changed = true;
            lineStart = lastSpace + 1;
            lineW = ImGui::CalcTextSize(buf + lineStart, buf + i).x;
            lastSpace = -1;
        }
        lineW += cw;
        if (buf[i] == ' ') lastSpace = i;
    }
    return changed;
}
namespace {
struct ChatInputCb { float wrapW = 0.0f; };
// Panel-local transparency toggle: show/hide the AI's reasoning ("Thinking") blocks. Session-only
// (DESIGN §5 "Show thinking" switch, default on); not persisted and deliberately NOT in AppState.
bool gShowThinking = true;
int chatInputEditCallback(ImGuiInputTextCallbackData* data) {
    if (data->EventFlag == ImGuiInputTextFlags_CallbackEdit) {
        auto* cb = static_cast<ChatInputCb*>(data->UserData);
        if (rewrapInPlace(data->Buf, data->BufTextLen, cb->wrapW))
            data->BufDirty = true;   // same length, so BufTextLen/CursorPos are unchanged
    }
    return 0;
}
}  // namespace
// rightReserve narrows the widget (and its wrap width) so composer overlays anchored at the
// input's right edge — the in-box send button — never sit over the text.
static bool drawChatInput(const char* id, std::vector<char>& buf, const char* placeholder,
                          int maxLines, float rightReserve = 0.0f) {
    if (buf.empty()) return false;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lineH = ImGui::GetTextLineHeight();
    float wrapW = ImGui::GetContentRegionAvail().x - rightReserve -
                  style.FramePadding.x * 2.0f - style.ScrollbarSize;
    if (wrapW < 1.0f) wrapW = 1.0f;

    // When the widget is NOT active it is safe to rewrap the backing buffer directly — this
    // covers programmatically-filled text (example chips) and panel resize. While active, the
    // edit callback rewraps ImGui's internal copy instead (external edits would be ignored).
    // We can't read ImGui's active id (internal API), so track per-id active state across frames.
    static std::unordered_map<ImGuiID, bool> s_activeLast;
    ImGuiID wid = ImGui::GetID(id);
    if (!s_activeLast[wid])
        rewrapInPlace(buf.data(), (int)std::strlen(buf.data()), wrapW);

    int rows = 1;
    for (const char* p = buf.data(); *p; ++p) if (*p == '\n') ++rows;
    if (rows > maxLines) rows = maxLines;
    const float h = lineH * (float)rows + style.FramePadding.y * 2.0f;

    ChatInputCb cb;
    cb.wrapW = wrapW;
    ImGui::InputTextMultiline(id, buf.data(), buf.size(),
        ImVec2(rightReserve > 0.0f ? -rightReserve : -1.0f, h),
        ImGuiInputTextFlags_CallbackEdit, chatInputEditCallback, &cb);
    s_activeLast[wid] = ImGui::IsItemActive();
    // Enter sends, Shift+Enter does not. Detect the KEY (not a trailing '\n') so a newline the
    // rewrap just inserted can never be mistaken for a submit.
    bool submit = ImGui::IsItemFocused() && !ImGui::GetIO().KeyShift &&
                  ImGui::IsKeyPressed(ImGuiKey_Enter, false);
    if (buf[0] == 0 && placeholder && placeholder[0]) {
        ImVec2 mn = ImGui::GetItemRectMin();
        // Honor a pushed TextDisabled (the glass composer pushes surf.textTertiary) so the hint
        // adapts to the backdrop instead of always using the theme's disabled grey.
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(mn.x + style.FramePadding.x * 2.0f, mn.y + style.FramePadding.y),
            ImGui::GetColorU32(ImGuiCol_TextDisabled), placeholder);
    }
    return submit;
}

// Forward decls — drawCodeBox/drawDiffBox sit above the chat-copy helper block but need
// the shared "Copied" toast + context menu (every copy path must light the same indicator).
static void chatNotifyCopied();
static void chatCopyContext(const ImRect& rect, const char* popupId,
                            const std::string& copyText,
                            const std::string& copyAll = {},
                            const std::string& selText = {});

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

// ---- Liquid Glass v2 host port (contract H5): glass surface + bubble helpers ----------------
// ONE adaptivePanel id shared by the whole Copilot pane — header, composer, and every card below
// fetch this SAME cached surface via ui::panelId("host_agent_glass"); ui::adaptivePanel is
// idempotent per frame (it re-samples only on the first call each frame, from the full pane rect
// painted in drawAIPanel), so every later fetch below is free regardless of which window/child is
// current when it's called. Tagged GlassGroup::Copilot so the user's per-group appearance knobs
// (tint / opacity / rounding / contrast boost) apply to every copilot surface.
static const ui::AdaptiveSurface& chatSurf() {
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
static ImVec4 semanticOnGlass(const float role[4], const ui::AdaptiveSurface& surf) {
    return semanticOnTone(role, surf.darkText);
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

// Draw a run-status glyph (spinning comet arc / check / cross / retry ring / pending hollow dot)
// centered in a `box`-wide cell at the cursor, then advance past it. Shared by the plan checklist
// and the action cards so every surface shows the same animated status art.
static void drawStatusGlyph(const std::string& status, float box) {
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
    ImU32 col = ImGui::ColorConvertFloat4ToU32(
        running || pass ? semanticOnGlass(pal.accent, surf)
        : retry         ? semanticOnGlass(pal.statusWarn, surf)
        : fail          ? semanticOnGlass(pal.statusErr, surf)
                        : surf.textTertiary);
    const float th = ui::metrics::stroke(1.6f);
    if (running) {                              // spinning comet arc
        float t = (float)ImGui::GetTime();
        const int seg = 14;
        for (int s = 0; s < seg; ++s) {
            float f = (float)s / seg, aa = t * 6.0f + f * 6.2831853f;
            float al = 0.12f + 0.88f * f;
            ImU32 cc = (col & 0x00FFFFFFu) | ((ImU32)(al * 255) << 24);
            dl->AddCircleFilled(ImVec2(c.x + std::cos(aa) * r, c.y + std::sin(aa) * r), th, cc);
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
    } else {
        dl->AddCircle(c, r * 0.8f, col, 12, th);  // pending hollow dot
    }
    ImGui::Dummy(ImVec2(box, box));
}

// A "bubble" = an auto-height child painted over an explicit glassPanel fill, ported from
// ui/imgui/src/ui/agent_panel.cpp's GlassBubble. The child's true height is only known after
// EndChild(), so the PARENT draw list is channel-split: content goes to channel 1, and
// endGlassBubble() paints the correctly-sized fill into channel 0 (behind) once the real rect is
// known — measure-then-fill, no frame lag. Child-window content lives in the child's own draw
// list and is unaffected by the parent split.
struct GlassBubble {
    ImVec2         origin;
    float          width;
    ui::GlassLevel level;
    float          rounding;   // LOGICAL dp — ui::glassPanel scales by uiScale internally
};

static GlassBubble beginGlassBubble(float width, ui::GlassLevel level, float rounding) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->ChannelsSplit(2);
    dl->ChannelsSetCurrent(1);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    return GlassBubble{p0, width, level, rounding};
}

static ImRect endGlassBubble(const GlassBubble& b) {
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
static void chatDrawCopyFlash() {
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
static std::string chatInputSelection(ImGuiID id) {
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
static void chatCopyContext(const ImRect& rect, const char* popupId,
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

    if (!ImGui::BeginPopup(popupId)) return;
    const std::string& all = copyAll.empty() ? copyText : copyAll;
    if (!selText.empty()) {
        if (ImGui::MenuItem("Copy"))
            chatCopyToClipboard(selText);
        ImGui::Separator();
    }
    if (ImGui::MenuItem("Copy text", nullptr, false, !copyText.empty()))
        chatCopyToClipboard(copyText);
    if (ImGui::MenuItem("Copy all", nullptr, false, !all.empty()))
        chatCopyToClipboard(all);
    ImGui::EndPopup();
}



// Read-only, soft-wrapped, selectable multiline body. Returns the InputText item id so the
// outer bubble can offer a selection-aware context menu. Ctrl+C (native) triggers the toast.
static ImGuiID chatDrawSelectableText(const char* id, const std::string& plain,
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
static void chatHoverCopyGlyph(const ImRect& rect, const std::string& text) {
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

// Compose a flat copy string for an activity / tool card.
static std::string chatActivityCopyText(const ActivityItem& a) {
    std::string out;
    if (!a.title.empty()) out = a.title;
    if (!a.reason.empty()) {
        if (!out.empty()) out += "\n";
        out += a.reason;
    }
    if (!a.detail.empty() && a.detail != a.reason) {
        if (!out.empty()) out += "\n";
        out += a.detail;
    }
    if (out.empty() && !a.stage.empty()) out = a.stage;
    return out;
}



// The build-plan card: a glass panel listing each planned part with its purpose, committed
// dimensions (mono) and feature bullets — plus a live status tick per part (spins while the
// matching orchestrator stage runs, checks when it passes), so it doubles as the todo checklist.
void drawPlanCard(const PlanInfo& plan, const std::vector<ActivityItem>& activity,
                  const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ImVec4 accent = semanticOnGlass(pal.accent, chatSurf());
    auto lower = [](std::string s) {
        for (char& c : s) c = (char)std::tolower((unsigned char)c);
        return s;
    };
    // Match a part to an activity stage by name (substring of title or stage key) for its tick.
    auto partStatus = [&](const std::string& name) -> std::string {
        std::string ln = lower(name);
        if (ln.empty()) return "";
        for (const auto& a : activity)
            if (lower(a.title).find(ln) != std::string::npos ||
                lower(a.stage).find(ln) != std::string::npos)
                return a.status;
        return "";
    };
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    // Left accent bar: the accent color at the planAccentBar alpha.
    dl->AddRectFilled(w0, ImVec2(w0.x + 3.0f, w0.y + ImGui::GetWindowHeight()),
                      (ui::Palette::toU32(pal.accent) & 0x00FFFFFFu) |
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
        drawStatusGlyph(partStatus(p.name), ImGui::GetTextLineHeight());
        ImGui::SameLine(0, 7 * ui::uiScale);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(p.name.c_str());
        ImGui::PopFont();
        if (!p.purpose.empty()) dimText(p.purpose.c_str());
        if (!p.dims.empty()) {
            // Adaptive secondary ink, not the theme's planDims grey — the theme tone is picked
            // for the theme panel bg and washes out over the live frost.
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
            ImGui::TextWrapped("%s  (mm)", p.dims.c_str());
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
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
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImRect planRect = endGlassBubble(gb);
    // Flat copy string of the whole plan checklist.
    {
        std::string copyTxt = "Build plan";
        if (!plan.name.empty()) { copyTxt += ": "; copyTxt += plan.name; }
        if (!plan.summary.empty()) { copyTxt += "\n"; copyTxt += plan.summary; }
        for (const auto& p : plan.parts) {
            copyTxt += "\n- ";
            copyTxt += p.name;
            if (!p.purpose.empty()) { copyTxt += " — "; copyTxt += p.purpose; }
            if (!p.dims.empty()) { copyTxt += "  ["; copyTxt += p.dims; copyTxt += " mm]"; }
            for (const auto& f : p.features) {
                copyTxt += "\n  • ";
                copyTxt += f;
            }
        }
        if (!plan.fit.empty()) { copyTxt += "\nFit: "; copyTxt += plan.fit; }
        chatCopyContext(planRect, "##plan_copy", copyTxt);
    }
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

// ============================ transparent activity timeline ============================
// Map a raw orchestrator stage key to a plain-language heading the user can follow.
static std::string friendlyStageTitle(const std::string& stage) {
    if (stage == "intake")  return "Understanding the request";
    if (stage == "plan")    return "Planning the build";
    if (stage == "model")   return "Building the model";
    if (stage == "edit")    return "Editing the model";
    if (stage == "review")  return "Reviewing the result";
    if (stage == "fix")     return "Fixing issues";
    if (stage == "memory")  return "Saving notes";
    std::string t = stage;            // Title-case fallback for any unknown stage
    if (!t.empty()) t[0] = (char)std::toupper((unsigned char)t[0]);
    return t.empty() ? std::string("Working") : t;
}

// ============================ Corporate Roles (aicad.corporate/1) ============================
// Additive kind="corporate" events (corp_event ∈ org_built|hire|team_lead|meeting|steering|
// route_rejected|wind_down). Stored as ActivityItem rows with rowKind 4–8 so we never touch
// integrator-owned app_state.h. Org chart updates in place; meetings/steering are compact cards;
// route rejections are muted one-liners. Missing fields degrade silently.
//
// rowKind 4 = org chart (stage "corp:org", detail = JSON snapshot)
// rowKind 5 = meeting digest (stage "corp:meet:<n>", detail = JSON digest)
// rowKind 6 = steering note (stage "corp:steer:<n>", reason = text)
// rowKind 7 = route rejected (stage "corp:rej:<n>", reason = muted line)
// rowKind 8 = wind-down summary (stage "corp:wind", reason = final line)

static const nlohmann::json& corpPayloadObj(const nlohmann::json& data) {
    static const nlohmann::json kEmpty = nlohmann::json::object();
    if (data.is_object() && data.contains("payload") && data["payload"].is_object())
        return data["payload"];
    // Tolerate flat envelopes (fields at top level) — additive streams may omit nesting.
    return data.is_object() ? data : kEmpty;
}

static std::string corpDeptLabel(const nlohmann::json& data, const nlohmann::json& payload) {
    if (data.is_object() && data.contains("dept")) {
        const auto& d = data["dept"];
        if (d.is_string()) return d.get<std::string>();
        if (d.is_object()) {
            std::string n = jstr(d, "name");
            if (n.empty()) n = jstr(d, "id");
            return n;
        }
    }
    std::string n = jstr(payload, "name");
    if (n.empty()) n = jstr(payload, "dept_id");
    if (n.empty()) n = jstr(payload, "to_dept");
    if (n.empty()) n = jstr(payload, "digest_dept");
    return n;
}

static std::string corpAgentId(const nlohmann::json& data, const nlohmann::json& payload) {
    std::string a = jstr(data, "agent");
    if (a.empty()) a = jstr(payload, "agent");
    if (a.empty()) a = jstr(payload, "agent_id");
    if (a.empty()) a = jstr(payload, "worker_id");
    if (a.empty()) a = jstr(payload, "from");
    return a;
}

// Short seat id for display (drop run-id suffixes when long).
static std::string corpShortId(const std::string& id) {
    if (id.size() <= 28) return id;
    return id.substr(0, 24) + "\xe2\x80\xa6";
}

static int corpWorkerCount(const nlohmann::json& dept) {
    if (!dept.is_object()) return 0;
    if (dept.contains("workers") && dept["workers"].is_array())
        return (int)dept["workers"].size();
    if (dept.contains("worker_count") && dept["worker_count"].is_number_integer())
        return dept["worker_count"].get<int>();
    if (dept.contains("workers") && dept["workers"].is_number_integer())
        return dept["workers"].get<int>();
    return 0;
}

static std::string corpDeptHead(const nlohmann::json& dept) {
    std::string h = jstr(dept, "head");
    if (h.empty()) h = jstr(dept, "head_id");
    return h;
}

static std::string corpDeptLead(const nlohmann::json& dept) {
    std::string l = jstr(dept, "lead");
    if (l.empty()) l = jstr(dept, "team_lead");
    if (l.empty()) l = jstr(dept, "team_lead_id");
    return l;
}

static std::string corpDeptNameOf(const nlohmann::json& dept) {
    std::string n = jstr(dept, "name");
    if (n.empty()) n = jstr(dept, "id");
    if (n.empty()) n = "department";
    return n;
}

// Normalize any payload departments array into a compact chart JSON for the org card.
static nlohmann::json corpNormalizeChart(const nlohmann::json& payload) {
    nlohmann::json chart = nlohmann::json::object();
    std::string dir = jstr(payload, "director_id");
    if (dir.empty()) dir = jstr(payload, "director");
    chart["director"] = dir;
    const nlohmann::json* src = nullptr;
    if (payload.contains("departments") && payload["departments"].is_array())
        src = &payload["departments"];
    else if (payload.contains("chart") && payload["chart"].is_object()) {
        const auto& c = payload["chart"];
        if (dir.empty()) {
            dir = jstr(c, "director_id");
            if (dir.empty()) dir = jstr(c, "director");
            chart["director"] = dir;
        }
        if (c.contains("departments") && c["departments"].is_array())
            src = &c["departments"];
    }
    nlohmann::json depts = nlohmann::json::array();
    if (src) {
        for (const auto& d : *src) {
            if (!d.is_object()) continue;
            nlohmann::json row = nlohmann::json::object();
            row["id"] = jstr(d, "id");
            row["name"] = corpDeptNameOf(d);
            row["head"] = corpDeptHead(d);
            row["lead"] = corpDeptLead(d);
            int wc = corpWorkerCount(d);
            row["workers"] = wc;
            // Keep worker ids when present so hire can de-dupe.
            if (d.contains("workers") && d["workers"].is_array())
                row["worker_ids"] = d["workers"];
            depts.push_back(std::move(row));
        }
    }
    chart["departments"] = std::move(depts);
    return chart;
}

static std::string corpOrgReason(const nlohmann::json& chart) {
    int nDepts = 0, nWorkers = 0;
    if (chart.contains("departments") && chart["departments"].is_array()) {
        nDepts = (int)chart["departments"].size();
        for (const auto& d : chart["departments"])
            nWorkers += corpWorkerCount(d);
    }
    std::string dir = jstr(chart, "director");
    char buf[160];
    if (!dir.empty())
        std::snprintf(buf, sizeof(buf), "Director \xc2\xb7 %d dept%s \xc2\xb7 %d worker%s",
                      nDepts, nDepts == 1 ? "" : "s", nWorkers, nWorkers == 1 ? "" : "s");
    else
        std::snprintf(buf, sizeof(buf), "%d dept%s \xc2\xb7 %d worker%s",
                      nDepts, nDepts == 1 ? "" : "s", nWorkers, nWorkers == 1 ? "" : "s");
    return std::string(buf);
}

static ActivityItem* corpFindOrg(ChatMsg& m) {
    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
        if (it->rowKind == 4 && it->stage == "corp:org") return &*it;
    return nullptr;
}

static ActivityItem& corpEnsureOrg(ChatMsg& m) {
    if (ActivityItem* a = corpFindOrg(m)) return *a;
    ActivityItem ni;
    ni.rowKind = 4;
    ni.stage = "corp:org";
    ni.title = "Corporate org";
    ni.status = "done";
    m.activity.push_back(std::move(ni));
    return m.activity.back();
}

static nlohmann::json corpParseChart(const ActivityItem& a) {
    if (a.detail.empty()) return nlohmann::json::object({{"departments", nlohmann::json::array()}});
    try {
        auto j = nlohmann::json::parse(a.detail);
        if (j.is_object()) return j;
    } catch (...) {}
    return nlohmann::json::object({{"departments", nlohmann::json::array()}});
}

static void corpApplyOrgBuilt(ChatMsg& m, const nlohmann::json& payload) {
    ActivityItem& a = corpEnsureOrg(m);
    nlohmann::json chart = corpNormalizeChart(payload);
    a.detail = chart.dump();
    a.reason = corpOrgReason(chart);
    a.title = "Corporate org";
    a.status = "done";
}

// Patch the in-place org card on hire / team_lead when a full departments[] is absent.
static void corpPatchOrgSeat(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload,
                             bool asTeamLead) {
    // Prefer full chart replacement when the backend includes departments.
    if ((payload.contains("departments") && payload["departments"].is_array()) ||
        (payload.contains("chart") && payload["chart"].is_object())) {
        corpApplyOrgBuilt(m, payload);
        return;
    }
    ActivityItem& a = corpEnsureOrg(m);
    nlohmann::json chart = corpParseChart(a);
    if (!chart.contains("departments") || !chart["departments"].is_array())
        chart["departments"] = nlohmann::json::array();

    std::string did = jstr(payload, "dept_id");
    std::string dname = corpDeptLabel(data, payload);
    if (did.empty() && !dname.empty()) did = dname;
    std::string agent = corpAgentId(data, payload);
    std::string role = jstr(payload, "role");
    if (agent.empty() && !role.empty()) agent = role;

    nlohmann::json* row = nullptr;
    for (auto& d : chart["departments"]) {
        if (!d.is_object()) continue;
        if ((!did.empty() && (jstr(d, "id") == did || jstr(d, "name") == did)) ||
            (!dname.empty() && (jstr(d, "name") == dname || jstr(d, "id") == dname))) {
            row = &d;
            break;
        }
    }
    if (!row) {
        nlohmann::json nd = nlohmann::json::object();
        nd["id"] = did;
        nd["name"] = dname.empty() ? (did.empty() ? "department" : did) : dname;
        nd["head"] = jstr(payload, "parent_id");
        nd["lead"] = "";
        nd["workers"] = 0;
        nd["worker_ids"] = nlohmann::json::array();
        chart["departments"].push_back(std::move(nd));
        row = &chart["departments"].back();
    }

    if (!agent.empty()) {
        if (!row->contains("worker_ids") || !(*row)["worker_ids"].is_array())
            (*row)["worker_ids"] = nlohmann::json::array();
        bool found = false;
        for (const auto& w : (*row)["worker_ids"])
            if (w.is_string() && w.get<std::string>() == agent) { found = true; break; }
        if (!found) {
            (*row)["worker_ids"].push_back(agent);
            (*row)["workers"] = (int)(*row)["worker_ids"].size();
        } else {
            (*row)["workers"] = (int)(*row)["worker_ids"].size();
        }
        if (asTeamLead || jstr(payload, "rank") == "team_lead")
            (*row)["lead"] = agent;
    } else if (asTeamLead) {
        std::string lead = jstr(payload, "worker_id");
        if (lead.empty()) lead = jstr(payload, "team_lead_id");
        if (!lead.empty()) (*row)["lead"] = lead;
    }

    a.detail = chart.dump();
    a.reason = corpOrgReason(chart);
    a.status = "done";
}

static int corpListCount(const nlohmann::json& o, const char* key) {
    if (!o.is_object() || !o.contains(key)) return 0;
    const auto& v = o[key];
    if (v.is_array()) return (int)v.size();
    if (v.is_number_integer()) return v.get<int>();
    if (v.is_number_float()) return (int)v.get<double>();
    return 0;
}

static std::string corpJoinRisks(const nlohmann::json& payload) {
    if (!payload.contains("risks")) return {};
    const auto& r = payload["risks"];
    if (r.is_string()) return r.get<std::string>();
    if (!r.is_array()) return {};
    std::string out;
    for (const auto& item : r) {
        std::string s;
        if (item.is_string()) s = item.get<std::string>();
        if (s.empty()) continue;
        if (!out.empty()) out += "; ";
        out += s;
        if (out.size() > 160) { out.resize(157); out += "\xe2\x80\xa6"; break; }
    }
    return out;
}

static void corpApplyMeeting(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 5;
    ni.stage = "corp:meet:" + std::to_string(m.activity.size());
    std::string dept = corpDeptLabel(data, payload);
    if (dept.empty()) dept = jstr(payload, "dept_id");
    ni.title = dept.empty() ? "Meeting" : ("Meeting \xc2\xb7 " + dept);

    int done = corpListCount(payload, "done");
    int prog = corpListCount(payload, "in_progress");
    if (prog == 0) prog = corpListCount(payload, "in-progress");
    int blocked = corpListCount(payload, "blocked");
    // Prefer explicit counts when backend sends them alongside lists.
    if (payload.contains("done_count") && payload["done_count"].is_number_integer())
        done = payload["done_count"].get<int>();
    if (payload.contains("in_progress_count") && payload["in_progress_count"].is_number_integer())
        prog = payload["in_progress_count"].get<int>();
    if (payload.contains("blocked_count") && payload["blocked_count"].is_number_integer())
        blocked = payload["blocked_count"].get<int>();

    char line[192];
    std::snprintf(line, sizeof(line), "done %d \xc2\xb7 in progress %d \xc2\xb7 blocked %d",
                  done, prog, blocked);
    ni.reason = line;
    std::string risks = corpJoinRisks(payload);
    if (!risks.empty()) {
        ni.reason += "\nRisks: " + risks;
    }
    // Pack counts for the card renderer (no app_state fields).
    ni.tokensIn = done;
    ni.tokensOut = prog;
    ni.opsCount = blocked;
    ni.status = blocked > 0 ? "warn" : "done";
    ni.expanded = false;

    nlohmann::json dig = nlohmann::json::object();
    dig["dept"] = dept;
    dig["done"] = done;
    dig["in_progress"] = prog;
    dig["blocked"] = blocked;
    dig["risks"] = risks;
    dig["trigger"] = jstr(payload, "trigger");
    dig["worker_completions"] = jint(payload, "worker_completions");
    // Preserve list bodies for expand.
    if (payload.contains("done") && payload["done"].is_array()) dig["done_items"] = payload["done"];
    if (payload.contains("in_progress") && payload["in_progress"].is_array())
        dig["in_progress_items"] = payload["in_progress"];
    if (payload.contains("blocked") && payload["blocked"].is_array())
        dig["blocked_items"] = payload["blocked"];
    ni.detail = dig.dump();
    m.activity.push_back(std::move(ni));
}

static void corpApplySteering(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 6;
    ni.stage = "corp:steer:" + std::to_string(m.activity.size());
    std::string dept = corpDeptLabel(data, payload);
    ni.title = dept.empty() ? "Director steering" : ("Director steering \xc2\xb7 " + dept);
    std::string text = jstr(payload, "text");
    if (text.empty()) text = jstr(payload, "steering");
    if (text.empty()) text = jstr(payload, "note");
    if (text.empty() && payload.contains("notes") && payload["notes"].is_array()) {
        for (const auto& n : payload["notes"]) {
            if (!n.is_string()) continue;
            if (!text.empty()) text += " ";
            text += n.get<std::string>();
        }
    }
    ni.reason = text;
    ni.detail = text;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void corpApplyRouteRejected(ChatMsg& m, const nlohmann::json& data,
                                   const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 7;
    ni.stage = "corp:rej:" + std::to_string(m.activity.size());
    std::string from = jstr(payload, "from");
    if (from.empty()) from = corpAgentId(data, payload);
    std::string to = jstr(payload, "to");
    if (to.empty()) to = jstr(payload, "channel");
    std::string reason = jstr(payload, "reason");
    // Prefer a backend-provided line; else compose the design-doc muted form.
    std::string line = jstr(payload, "line");
    if (line.empty()) line = jstr(payload, "message");
    if (line.empty()) {
        if (!from.empty() && (to.find("director") != std::string::npos ||
                              reason.find("director") != std::string::npos ||
                              reason.find("heads") != std::string::npos ||
                              to.empty())) {
            line = "worker " + corpShortId(from) +
                   " \xe2\x86\x92 director blocked \xe2\x80\x94 routed to head";
        } else if (!from.empty()) {
            line = corpShortId(from) + " route blocked";
            if (!to.empty()) line += " \xe2\x86\x92 " + to;
            if (!reason.empty()) line += " \xe2\x80\x94 " + reason;
        } else {
            line = reason.empty() ? "route rejected" : reason;
        }
    }
    ni.reason = line;
    ni.title = line;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void corpApplyWindDown(ChatMsg& m, const nlohmann::json& data,
                              const nlohmann::json& payload) {
    (void)data;
    ActivityItem ni;
    ni.rowKind = 8;
    ni.stage = "corp:wind";
    ni.title = "Org wind-down";
    std::string summary = jstr(payload, "summary");
    if (summary.empty()) summary = jstr(payload, "text");
    if (summary.empty()) summary = jstr(payload, "message");
    int done = 0;
    if (payload.contains("departments_done")) {
        if (payload["departments_done"].is_array())
            done = (int)payload["departments_done"].size();
        else if (payload["departments_done"].is_number_integer())
            done = payload["departments_done"].get<int>();
    }
    if (summary.empty()) {
        if (done > 0) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "Corporate org finished \xc2\xb7 %d department%s",
                          done, done == 1 ? "" : "s");
            summary = buf;
        } else {
            summary = "Corporate org finished";
        }
    }
    // Also refresh the org card summary line if present.
    if (ActivityItem* org = corpFindOrg(m)) {
        if (!summary.empty()) org->reason = summary;
        org->status = "done";
    }
    ni.reason = summary;
    ni.detail = summary;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void feedCorporateEvent(ChatMsg& m, const nlohmann::json& data) {
    const nlohmann::json& payload = corpPayloadObj(data);
    std::string ce = jstr(data, "corp_event");
    if (ce.empty()) ce = jstr(data, "event");
    if (ce.empty()) ce = jstr(payload, "corp_event");
    if (ce.empty()) return;  // unknown / incomplete — silent no-op

    if (ce == "org_built") {
        corpApplyOrgBuilt(m, payload);
    } else if (ce == "hire") {
        corpPatchOrgSeat(m, data, payload, /*asTeamLead=*/false);
    } else if (ce == "team_lead") {
        corpPatchOrgSeat(m, data, payload, /*asTeamLead=*/true);
    } else if (ce == "meeting") {
        corpApplyMeeting(m, data, payload);
    } else if (ce == "steering") {
        corpApplySteering(m, data, payload);
    } else if (ce == "route_rejected") {
        corpApplyRouteRejected(m, data, payload);
    } else if (ce == "wind_down") {
        corpApplyWindDown(m, data, payload);
    }
    // Unknown corp_event values: ignore (forward-compatible).
}

void feedTurnActivity(ChatMsg& m, const std::string& kind, const nlohmann::json& data) {
    // Inside a mission, sub-run stage keys (intake/model/...) repeat per task — namespace them by
    // the current task index so task 2's "model" row doesn't reopen task 1's. Outside a mission
    // (curMissionTask < 0) the key is the bare stage, preserving the single-run behavior exactly.
    auto nsKey = [&](const std::string& s) {
        return m.curMissionTask >= 0 ? (std::to_string(m.curMissionTask) + ":" + s) : s;
    };
    auto findStage = [&](const std::string& s) -> ActivityItem* {
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->stage == s) return &*it;
        return nullptr;
    };
    if (kind == "mission_started") {
        m.curMissionTask = -1;  // task rows arrive via mission_task_started
    } else if (kind == "mission_task_started") {
        int idx = jint(data, "index");
        m.curMissionTask = idx;
        std::string key = "task" + std::to_string(idx);
        ActivityItem* a = findStage(key);
        if (!a) {
            ActivityItem ni;
            ni.stage = key;
            ni.title = jstr(data, "title");
            if (ni.title.empty()) ni.title = jstr(data, "capability");
            ni.missionHeader = true;
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
    } else if (kind == "mission_task_result") {
        ActivityItem* a = findStage("task" + std::to_string(jint(data, "index")));
        if (a) {
            std::string s = jstr(data, "status");
            a->status = (s == "done") ? "done" : (s.empty() ? "done" : s);
            std::string summary = jstr(data, "summary");
            if (!summary.empty()) a->reason = summary;
        }
    } else if (kind == "mission_finished") {
        m.curMissionTask = -1;
        for (auto& a : m.activity)
            if (a.status == "running" || a.status.empty()) { a.status = "done"; a.streaming = false; }
    } else if (kind == "stage_started") {
        std::string s = nsKey(jstr(data, "stage"));
        ActivityItem* a = findStage(s);
        if (!a) {
            ActivityItem ni;
            ni.stage = s;
            ni.title = friendlyStageTitle(jstr(data, "stage"));
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
    } else if (kind == "agent_output") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            std::string raw = jstr(data, "raw");
            if (!raw.empty()) a->detail = raw;     // keep the latest attempt's thoughts/output
            a->durationMs = jint(data, "duration_ms");
            a->opsCount   = jint(data, "ops_count");
            if (data.contains("tokens") && data["tokens"].is_object()) {
                a->tokensIn  = jint(data["tokens"], "in");
                a->tokensOut = jint(data["tokens"], "out");
            }
        }
    } else if (kind == "stage_result") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            std::string v = jstr(data, "verdict");
            a->status = v.empty() ? "done" : v;
            a->reason = jstr(data, "reason");
        }
    } else if (kind == "run_finished") {
        // A sub-run finished (one mission task) OR a single-run completed. Close only the rows
        // for the CURRENT task; mission_finished does the final sweep. Outside a mission this
        // closes everything, as before.
        for (auto& a : m.activity)
            if ((a.status == "running" || a.status.empty()) && !a.missionHeader) {
                a.status = "done";
                a.streaming = false;  // finalize any still-open thought box (M13)
            }
        // Capture additive early-stop fields (ErrorDetail / contracts): stop_reason +
        // error_detail. Prefer error_detail over legacy `error`. Stashed as a meta activity
        // row so the error card can render "stopped: <reason> — <detail>" without touching
        // ChatMsg (app_state.h is integrator-owned).
        {
            std::string sr = jstr(data, "stop_reason");
            std::string ed = jstr(data, "error_detail");
            if (ed.empty()) ed = jstr(data, "error");  // soft alias
            if (!sr.empty() || !ed.empty()) {
                ActivityItem* meta = nullptr;
                for (auto& a : m.activity)
                    if (a.stage == "__stop_meta__") { meta = &a; break; }
                if (!meta) {
                    ActivityItem ni;
                    ni.stage = "__stop_meta__";
                    ni.rowKind = 0;
                    ni.status = "done";
                    ni.missionHeader = false;
                    m.activity.push_back(std::move(ni));
                    meta = &m.activity.back();
                }
                if (!sr.empty()) meta->title = sr;
                if (!ed.empty()) meta->detail = ed;
                if (!ed.empty()) meta->reason = ed;
            }
            // Prefer a model id carried on the terminal event over the stamp at run start.
            {
                std::string mid = jstr(data, "model");
                if (mid.empty()) mid = jstr(data, "model_id");
                if (!mid.empty()) m.model = std::move(mid);
            }
        }
    } else if (kind == "status") {
        // Live status/phase envelope: phase=done|error may carry the same additive fields.
        const std::string phase = jstr(data, "phase");
        if (phase == "done" || phase == "error" || phase == "failed" || phase == "cancelled") {
            std::string sr = jstr(data, "stop_reason");
            std::string ed = jstr(data, "error_detail");
            if (ed.empty()) ed = jstr(data, "error");
            if (!sr.empty() || !ed.empty()) {
                ActivityItem* meta = nullptr;
                for (auto& a : m.activity)
                    if (a.stage == "__stop_meta__") { meta = &a; break; }
                if (!meta) {
                    ActivityItem ni;
                    ni.stage = "__stop_meta__";
                    ni.rowKind = 0;
                    ni.status = "done";
                    m.activity.push_back(std::move(ni));
                    meta = &m.activity.back();
                }
                if (!sr.empty()) meta->title = sr;
                if (!ed.empty()) { meta->detail = ed; meta->reason = ed; }
            }
        }
    }
    // ---- M12-M14: typed transparency rows from the aicad.agent_events/2 stream ----
    // These arrive through the SAME app.cpp drain as the stage events above (kind already has the
    // "orch_" prefix stripped; `data` is the parsed /2 record with envelope + payload fields at
    // top level). Each event appends a typed row at its ARRIVAL POSITION in the running turn's
    // timeline, interleaving naturally with the stage rows. Span-id keys ("ctx:"/"thought:"/
    // "tool:" + id) can never collide with the orchestrator stage keys.
    else if (kind == "agent.context_injected") {                     // M12 — context chip
        ActivityItem ni;
        ni.rowKind = 1;
        ni.stage   = "ctx:" + jstr(data, "id");
        ni.title   = jstr(data, "label");
        if (ni.title.empty()) ni.title = "context";
        ni.reason    = jstr(data, "source");
        ni.ctxTokens = jint(data, "token_est");
        ni.status    = "done";
        m.activity.push_back(std::move(ni));
    } else if (kind == "agent.thought.delta" || kind == "agent.thought") {  // M13 — thought box
        // Deltas sharing a parent span coalesce into ONE growing block, finalized by the matching
        // completed agent.thought (contract §3.2). The provider seam is non-streaming today, so
        // in practice whole agent.thought blocks land per-block on arrival; the delta path is
        // coded so live token streaming lights up the moment a streaming provider ships.
        std::string pid = jstr(data, "parent_id");
        if (pid.empty()) pid = jstr(data, "node_id");
        const std::string key = "thought:" + pid;
        ActivityItem* a = nullptr;
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->rowKind == 2 && it->stage == key && it->streaming) { a = &*it; break; }
        if (!a) {
            ActivityItem ni;
            ni.rowKind = 2;
            ni.stage   = key;
            ni.title   = "Thoughts";
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        if (kind == "agent.thought.delta") {
            a->detail += jstr(data, "text");
            a->streaming = true;
            a->status    = "running";
        } else {
            // Completed block: authoritative text replaces any delta accumulation (same content,
            // possibly truncated per contract), then the box finalizes + auto-collapses.
            std::string txt = jstr(data, "text");
            if (!txt.empty() || a->detail.empty()) a->detail = txt;
            a->streaming = false;
            a->status    = "done";
        }
    } else if (kind == "agent.tool_call") {                          // M14 — tool call row
        ActivityItem ni;
        ni.rowKind = 3;
        ni.stage   = "tool:" + jstr(data, "id");
        ni.title   = jstr(data, "tool_name");
        if (ni.title.empty()) ni.title = "tool";
        ni.reason     = jstr(data, "target");   // human "what it acts on" (may be "")
        ni.durationMs = jint(data, "latency_ms");
        const bool ok = !data.is_object() || data.value("ok", true);
        ni.status     = ok ? "done" : "fail";
        m.activity.push_back(std::move(ni));
    } else if (kind == "provider_call") {
        // In-depth provider HTTP call log (InAppDiag / core agent). One row per attempt.
        // rowKind 9; fields packed into existing ActivityItem slots so we never touch
        // integrator-owned app_state.h:
        //   title      = "provider · model" (display)
        //   reason     = single-line summary (status + latency + endpoint snippet)
        //   detail     = full copyable line (+ error_excerpt when present)
        //   durationMs = latency_ms
        //   tokensIn   = http_status (0 = transport fail)
        //   opsCount   = attempt (1-based)
        //   status     = "done" | "fail" from ok / http_status
        ActivityItem ni;
        ni.rowKind = 9;
        const std::string prov = jstr(data, "provider");
        const std::string model = jstr(data, "model");
        const std::string endpoint = jstr(data, "endpoint");
        const std::string reqId = jstr(data, "request_id");
        const std::string errEx = jstr(data, "error_excerpt");
        int httpStatus = jint(data, "http_status");
        // Tolerate float-ish JSON numbers (some emitters use double).
        if (httpStatus == 0 && data.contains("http_status") && data["http_status"].is_number())
            httpStatus = (int)data["http_status"].get<double>();
        int latency = jint(data, "latency_ms");
        if (latency == 0 && data.contains("latency_ms") && data["latency_ms"].is_number())
            latency = (int)data["latency_ms"].get<double>();
        int attempt = jint(data, "attempt");
        if (attempt == 0 && data.contains("attempt") && data["attempt"].is_number())
            attempt = (int)data["attempt"].get<double>();
        if (attempt <= 0) attempt = 1;
        bool ok = true;
        if (data.contains("ok") && data["ok"].is_boolean())
            ok = data["ok"].get<bool>();
        else if (httpStatus != 0)
            ok = (httpStatus >= 200 && httpStatus < 300);
        else if (!errEx.empty())
            ok = false;

        std::string head;
        if (!prov.empty() && !model.empty()) head = prov + " \xc2\xb7 " + model;
        else if (!prov.empty()) head = prov;
        else if (!model.empty()) head = model;
        else head = "provider";
        ni.title = head;

        char statusBuf[32];
        if (httpStatus > 0)
            std::snprintf(statusBuf, sizeof(statusBuf), "HTTP %d", httpStatus);
        else
            std::snprintf(statusBuf, sizeof(statusBuf), "transport");

        std::string summary = statusBuf;
        if (latency > 0) {
            char lb[32];
            std::snprintf(lb, sizeof(lb), " %dms", latency);
            summary += lb;
        }
        if (attempt > 1) {
            char ab[24];
            std::snprintf(ab, sizeof(ab), " try#%d", attempt);
            summary += ab;
        }
        if (!endpoint.empty()) {
            summary += " ";
            // Keep endpoint short so the one-liner fits the chat column.
            if (endpoint.size() > 48) summary += endpoint.substr(0, 45) + "...";
            else summary += endpoint;
        }
        ni.reason = summary;

        // Full line for copy / expanded body.
        std::string full = head + "  " + summary;
        if (!reqId.empty()) { full += "  id="; full += reqId; }
        if (!errEx.empty()) { full += "\n  "; full += errEx; }
        ni.detail = full;

        ni.durationMs = latency;
        ni.tokensIn = httpStatus;
        ni.opsCount = attempt;
        ni.status = ok ? "done" : "fail";
        ni.stage = "prov:" + std::to_string(m.activity.size());
        m.activity.push_back(std::move(ni));
    } else if (kind == "corporate") {                               // Corporate Roles live UI
        // Additive aicad.corporate/1 stream: corp_event + optional dept/agent/payload.
        // Same drain as stage/tool events (app.cpp strips orch_ prefix).
        feedCorporateEvent(m, data);
    }
}

namespace ui {

// Forward decls for the conversation helpers (defined further down) so the header/composer can
// drive them.
static void ensureConversations(AppState& st);
static void newConversation(AppState& st);
static void switchConversation(AppState& st, int idx);
static void closeConversation(AppState& st, int idx);
static void submitTurn(AppState& st, const std::string& text);
static bool aiTurnActive(AppState& st);
static bool activeConversationRunning(AppState& st);

// ============================ W3-H: unified AI Copilot pane ============================
// One pane, top-down: header -> input -> mode pills -> conversation (with inline live run
// status + embedded Activity tree) -> collapsing Code / Console footers. Replaces the old
// Design/Edit/Chat tab triad. All backend launch calls are preserved verbatim.

// One pass of the agent loop, rendered as a compact expandable card. Reuses the shared
// code/diff/prose/fit/parts renderers (drawCodeBox/drawDiffBox/...). Refactored out of the
// old conversation loop unchanged in behavior.
static void drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& stepSurf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, stepSurf);
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    // Build issues are amber, not red: a failed pass is normal mid-loop — the error goes
    // straight back to the model, which usually fixes it.
    ImVec4 badge = s.ok ? (s.done ? accentCol : semanticOnGlass(pal.iterBuilt, stepSurf))
                        : semanticOnGlass(pal.statusWarn, stepSurf);
    const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
    ImGui::PopFont();
    if (!s.dims.empty()) {
        ImGui::SameLine();
        ImGui::PushFont(fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text, stepSurf.textSecondary);
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
            float maxTw = ui::metrics::copilotThumbW();
            float tw = logW < maxTw ? logW : maxTw;
            float th = tw * (float)s.thumb.h / (float)s.thumb.w;
            ImGui::Image((ImTextureID)(intptr_t)s.thumb.id, ImVec2(tw, th));
        }
    }

    if (!s.notes.empty()) dimText(s.notes.c_str());
    if (!s.ok && !s.error.empty()) {
        // Calm one-line cause; the raw traceback stays behind a toggle so a self-correcting
        // pass never reads like a crash.
        ImGui::PushStyleColor(ImGuiCol_Text, semanticOnGlass(pal.statusWarn, stepSurf));
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
    if (s.showThinking && !s.thinking.empty())
        drawProseBox("##think", s.thinking, ui::metrics::copilotProseMaxH());
    if (s.showCode && !s.code.empty())
        drawCodeBox("##code_v", s.code, fonts.mono, ui::metrics::px(260.0f));
    if (s.showDiff && !s.diff.empty())
        drawDiffBox("##diff_v", s.diff, fonts.mono, ui::metrics::copilotDiffMaxH());
    if (s.showError && !s.error.empty())
        drawCodeBox("##err_v", s.error, fonts.mono, ui::metrics::px(200.0f));
    ImGui::PopStyleColor();
    ImGui::EndChild();
    endGlassBubble(gb);
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

// ---- O1: model picker → RPC payload threading --------------------------------------------------
// Canonical provider NAME for the core's resolve_provider(name, cfg, env) from the GUI enum.
// PROV_LOCAL / PROV_CLOUDCOMPAT resolve as "openai" descriptors — the base_url (persisted to
// config.json by saveSettings immediately before every send) discriminates local vs cloud.
static const char* providerWireName(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:
        case ui::PROV_LOCAL:
        case ui::PROV_CLOUDCOMPAT: return "openai";
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";  // saveSettings rewrites to xai-sub when no key
        case ui::PROV_CURSOR:      return "cursor";
    }
    return "";
}

// The explicit provider/model/effort override the GUI now attaches to EVERY agent-turn RPC
// (agent_run_v2 / agent_ask_v2 / agent_clarify_v1 / agent_mission_v1). Without it the core
// resolves from ambient config.json + process env, so an AI_CAD_PROVIDER env var (or a config
// edited behind the GUI's back) silently beat the picker (tracker O1). saveSettings still runs
// before each send so descriptor details (base_url / api_key / model_path) match the picker;
// these fields pin name + model + effort explicitly on top of that.
static nlohmann::json agentProviderOverride(const Settings& s) {
    nlohmann::json j = nlohmann::json::object();
    const char* name = providerWireName(s.provider);
    if (name && name[0]) j["provider"] = name;
    // llamacpp's Settings::model holds the gguf FILE PATH (the wire model stays "local-gguf");
    // don't clobber the descriptor's model with a filesystem path.
    if (s.provider != ui::PROV_LLAMACPP && s.model[0]) j["model"] = s.model;
    if (s.reasoningEffort[0]) j["reasoning_effort"] = s.reasoningEffort;
    return j;
}

// ---- M23: composer file attachments -------------------------------------------------------------
static std::string attachmentBaseName(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

// Read a reasonably-sized TEXT file for inline injection into the request. Returns false for
// binary or large files — those ride as path-only attachments (no fabricated previews).
static bool readAttachmentText(const std::string& path, std::string& out) {
    constexpr uintmax_t kMaxInline = 64 * 1024;   // inline cap: keep the prompt sane
    std::error_code ec;
    const uintmax_t sz = std::filesystem::file_size(path, ec);
    if (ec || sz == 0 || sz > kMaxInline) return false;
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    std::string data = ss.str();
    size_t ctrl = 0;
    for (unsigned char c : data) {
        if (c == 0) return false;                    // NUL => binary
        if (c < 9 || (c > 13 && c < 32)) ++ctrl;     // odd control chars
    }
    if (ctrl * 10 > data.size()) return false;       // >10% control bytes => binary
    out = std::move(data);
    return true;
}

// Consume st.pendingAttachments into (a) an augmented request string — the text the model
// actually receives: small text files inlined verbatim, everything else as an explicit path —
// and (b) the structured "attachments" array carried in the RPC payload. HONESTY: the core's
// agent handlers currently pass only the request TEXT to the model, so the inline injection here
// is what actually delivers attachment content today; the structured field rides along (clearly
// named) for handler-side consumption later.
static nlohmann::json consumeAttachments(AppState& st, std::string& requestText) {
    nlohmann::json atts = nlohmann::json::array();
    if (st.pendingAttachments.empty()) return atts;
    for (const std::string& path : st.pendingAttachments) {
        nlohmann::json a = {{"path", path}, {"name", attachmentBaseName(path)}};
        std::string content;
        if (readAttachmentText(path, content)) {
            a["inline"] = true;
            requestText += "\n\n[Attached file: " + attachmentBaseName(path) + " (" + path +
                           ")]\n```\n" + content;
            if (content.empty() || content.back() != '\n') requestText += "\n";
            requestText += "```";
        } else {
            a["inline"] = false;
            requestText += "\n\n[Attached file (path only; content not inlined): " + path + "]";
        }
        atts.push_back(std::move(a));
    }
    st.pendingAttachments.clear();
    return atts;
}

// Wave4: serialize st.pendingContextRefs into the turn payload's "context_refs" array AND
// inline a "Context references:" block into the request text (C++ agent_run only sees request
// text — same honesty pattern as consumeAttachments). Then clear chips + disarm the cube.
static nlohmann::json consumeContextRefs(AppState& st, std::string& requestText) {
    nlohmann::json refs = nlohmann::json::array();
    if (st.pendingContextRefs.empty()) {
        st.contextPickActive = false;
        return refs;
    }
    requestText += "\n\nContext references:";
    for (const auto& r : st.pendingContextRefs) {
        nlohmann::json o = {
            {"kind", r.kind},
            {"id", r.id},
            {"doc_id", r.docId},
            {"label", r.label},
        };
        refs.push_back(std::move(o));
        // One line the model can resolve/measure/target: kind + label + ids.
        requestText += "\n- " + (r.kind.empty() ? std::string("ref") : r.kind) + ": ";
        requestText += r.label.empty() ? (r.id.empty() ? "(unnamed)" : r.id) : r.label;
        requestText += " (id: " + r.id;
        if (!r.docId.empty()) requestText += ", doc: " + r.docId;
        requestText += ")";
    }
    st.pendingContextRefs.clear();
    st.contextPickActive = false;
    return refs;
}

// Create a fresh part document if needed and launch the v2 orchestrator for `request`. The
// current doc is replaced only when it already has features (so an empty new part is reused).
// Centralized so both the direct Design path and the post-clarify "Build it" path share it.
// `extra` is merged on top of the provider override (attachments / context_refs / …).
static void launchDesignRun(AppState& st, const std::string& request,
                            nlohmann::json extra = nlohmann::json::object()) {
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
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a new part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    st.clearViewportForNewDoc();
    // O1: pin the picker's provider/model/effort explicitly in the RPC payload (beats an
    // ambient AI_CAD_PROVIDER env override in the core's resolve_provider). Merge call-site
    // extras (attachments / context_refs) on top.
    nlohmann::json payload = agentProviderOverride(st.settings);
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it)
            payload[it.key()] = it.value();
    }
    // Cursor has no C++ chat completions transport (Connect-RPC lives only in the Python
    // CursorSubProvider). Route Design to the Python worker's legacy agent_design loop so the
    // run actually completes; every other provider keeps agent_run_v2.
    const bool cursorDesign =
        (st.settings.provider == ui::PROV_CURSOR) ||
        (payload.value("provider", std::string()) == "cursor");
    if (cursorDesign) {
        st.addLog("[..] designing (cursor/python agent_design): " + request);
        st.status = "Designing...";  st.statusKind = 0;
        // Legacy path: build123d + STL, not the C++ doc-ops orchestrator — do NOT arm the
        // agentV2 drain (doc_activate / assembly-tab adopt). Chat still updates via job.events.
        st.agentV2Running = false;
        st.agentV2 = false;
        if (st.activityPanel)
            st.activityPanel->clearLive();
        launchAgentDesign(st.worker, st.job, request, st.docMirror.docId(), payload);
        return;
    }
    st.addLog("[..] designing (v2): " + request);
    st.status = "Designing...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    if (st.activityPanel)
        st.activityPanel->clearLive();
    launchAgentRunV2(st.worker, st.job, request, st.docMirror.docId(),
                     st.ribbonTab == AppState::RT_Manufacture, payload);
}

// Launch the MISSION orchestrator for a multi-capability request (e.g. "model X, then run CFD and
// prove it's good"). Mirrors launchDesignRun's doc-prep + mutex + activity reset, but routes to
// agent_mission_v1 so the run decomposes into a task chain (design/assembly -> sim -> verdict).
// Shares the same fresh-part policy: a new build starts on a clean part doc; an existing model is
// kept (a mission that only analyzes the open part should not wipe it).
// `extra` is merged on top of the provider override (attachments / context_refs / …).
static void launchMissionRunChat(AppState& st, const std::string& request, bool needFreshDoc,
                                 nlohmann::json extra = nlohmann::json::object()) {
    std::string err;
    bool docOk = st.docMirror.hasDoc();
    if (needFreshDoc) {
        if (docOk) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
        } else {
            docOk = st.docMirror.newPart(st.worker, err);
        }
    }
    if (!docOk) {
        st.addLog("[err] mission: could not create a part document: " + err);
        st.status = "Worker not ready";  st.statusKind = 2;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    if (needFreshDoc) st.clearViewportForNewDoc();
    st.addLog("[..] mission: " + request);
    st.status = "Working on it...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    if (st.activityPanel)
        st.activityPanel->clearLive();
    // O1: pin the picker's provider/model/effort explicitly in the RPC payload.
    nlohmann::json payload = agentProviderOverride(st.settings);
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it)
            payload[it.key()] = it.value();
    }
    launchMissionRun(st.worker, st.job, request, st.docMirror.docId(), payload);
}

// The inline clarify card: build-critical questions (interaction contract kinds).
// Supports text/dimension (editable field), choice single (radio) + multi (checkbox),
// approval (Y/N), and geometry_select (arm context-cube pick → chips). "Build it" folds
// answers into the request; "Skip" uses suggested defaults.
static void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, surf);
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
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
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
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
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted("Assuming:");
        ImGui::PopStyleColor();
        for (const auto& a : m.clarify.assumptions) {
            ImGui::Bullet();
            ImGui::SameLine();
            dimText(a.c_str());
        }
    }
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
    bool build = primaryButton("Build it", ImVec2(-1, ui::metrics::copilotBtnH()));
    bool skip = ImGui::Button("Skip - use defaults", ImVec2(-1, 0));
    ImGui::PopStyleColor(5);   // Text + FrameBg/Hovered/Active + TextDisabled
    ImGui::EndChild();
    endGlassBubble(gb);
    if (build || skip) {
        // Fold each answer (or its suggested default on Skip) into the request.
        std::string aug = m.clarify.request + "\n\nClarified dimensions:";
        for (auto& q : m.clarify.questions) {
            std::string val;
            if (skip) {
                val = q.suggested;
                if (val.empty() && !q.selectedValues.empty()) {
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
        launchDesignRun(st, aug);
    }
}

// --- model-routing + context-meter helpers (transparency UI) -----------------------------------
// "openai/gpt-oss-120b:free" -> "gpt-oss-120b" for compact display.
static std::string shortModelName(const std::string& id) {
    std::string s = id;
    size_t slash = s.rfind('/'); if (slash != std::string::npos) s = s.substr(slash + 1);
    size_t colon = s.find(':');  if (colon != std::string::npos) s = s.substr(0, colon);
    return s;
}
static std::string fmtTokens(int n) {
    char b[32];
    if (n >= 1000000) std::snprintf(b, sizeof(b), "%.1fM", n / 1000000.0);
    else if (n >= 1000) std::snprintf(b, sizeof(b), "%dk", (n + 500) / 1000);
    else std::snprintf(b, sizeof(b), "%d", n);
    return b;
}
// The active model's total context window (mirrors core default_context_window for the meter).
static int effectiveContextWindow(const Settings& s) {
    if (s.contextWindow > 0) return s.contextWindow;
    return ui::defaultContextWindowForModel(s.provider, s.model[0] ? s.model : "");
}
// ============================ typed-block agent timeline ============================
// The orchestrator event stream (feedTurnActivity -> ChatMsg::activity) is rendered as the
// DESIGN.md §5 typed blocks: dimmed, collapsible "thinking" blocks for reasoning-only stages and
// glass "action cards" (icon + human label + animated status + folded result + expandable detail)
// for tool / build stages. Engine RPCs + the event plumbing are unchanged — this is layout only.

// Atlas icon for a stage key (string-keyed: a missing name just falls back to no glyph).
static const char* stageIconName(const std::string& stage, bool missionHeader) {
    if (missionHeader) return "list";
    std::string s = stage;
    size_t colon = s.find(':');           // strip the "<taskIdx>:" mission namespace prefix
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "intake") return "search";
    if (s == "plan")   return "list";
    if (s == "model")  return "cpu";
    if (s == "edit")   return "edit";
    if (s == "review") return "eye";
    if (s == "fix")    return "wrench";
    if (s == "memory") return "bookmark";
    return "cpu";
}

// Classify a row: reasoning-only stages (no CAD ops, with captured detail) render as a thinking
// block; build/tool stages (model/edit/review/fix/memory) and mission headers are action cards.
static bool isThinkingItem(const ActivityItem& a) {
    if (a.missionHeader) return false;
    if (a.stage == "__stop_meta__") return false;  // hidden stop_reason/error_detail carrier
    std::string s = a.stage;
    size_t colon = s.find(':');
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "model" || s == "edit" || s == "review" || s == "fix" || s == "memory") return false;
    return a.opsCount == 0 && !a.detail.empty();
}

// A collapsible, dimmed reasoning block. While it is the frontier of work it stays open so the
// thoughts stream; once a later stage reaches a verdict (`laterDone`) it auto-collapses unless the
// user expanded it (a.expanded, set by clicking). `showThinking` off -> a single dim one-liner.
static void drawThinkingBlock(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                              bool laterDone) {
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, 2 * sc));

    if (!showThinking) {
        ImGui::PushFont(fonts.body);
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted(a.title.empty() ? "Thinking" : a.title.c_str());
        ImGui::PopStyleColor();
        ImGui::PopFont();
        return;
    }

    const bool open = a.expanded || !laterDone;   // forced open while it is the active frontier
    const bool hasDetail = !a.detail.empty();
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##thinkblk", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushFont(fonts.body);
    ImGui::TextColored(surf.textSecondary, "%s", a.title.empty() ? "Thinking" : a.title.c_str());
    ImGui::PopFont();
    if (hasDetail) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !open;
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : "(show thinking)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !open;
    }
    if (open && hasDetail)
        drawProseBox("##thinkdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());

    ImGui::EndChild();
    ImRect thinkRect = endGlassBubble(gb);
    const std::string thinkCopy = chatActivityCopyText(a);
    chatCopyContext(thinkRect, "##think_copy", thinkCopy);

}

// A glass action card for a tool call / orchestrator build stage: status glyph + tool icon + human
// label + duration/ops meta, the verdict folded in as a result line, an expandable drawer (a
// legacy AgentStep's thinking/code/diff/thumbnail when attached, else the raw detail), and a red
// border when the stage failed. `step` is non-null only for v1 runs that carry an AgentStep.
static void drawActionCard(ActivityItem& a, AgentStep* step, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool running = a.status == "running";
    const bool fail    = a.status == "fail";

    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##actcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);   // base ink (dimText derives from it)

    // ---- header row: status glyph + tool icon + label + meta ----
    drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon(stageIconName(a.stage, a.missionHeader), 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z,
                   running ? 1.0f : 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImVec4 titleCol = running ? semanticOnGlass(pal.accent, surf)
                    : fail    ? semanticOnGlass(pal.statusErr, surf)
                              : surf.textPrimary;
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(titleCol, "%s", a.title.c_str());
    ImGui::PopFont();

    const bool hasDrawer = (step && (!step->thinking.empty() || !step->code.empty() ||
                                     !step->diff.empty() || !step->contactSheet.empty())) ||
                           !a.detail.empty();
    if (hasDrawer) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.expanded ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
    }
    if (a.durationMs > 0 || a.opsCount > 0) {
        ImGui::SameLine(0, 8 * sc);
        char meta[96];
        if (a.opsCount > 0 && a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "%d ops \xc2\xb7 %.1fs", a.opsCount, a.durationMs / 1000.0);
        else if (a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
        else
            std::snprintf(meta, sizeof(meta), "%d ops", a.opsCount);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }

    // ---- result / notes line (folded into the card) ----
    if (!a.reason.empty()) dimText(a.reason.c_str());
    else if (step && !step->notes.empty()) dimText(step->notes.c_str());

    // ---- expandable drawer ----
    if (a.expanded && hasDrawer) {
        if (step) {
            if (!step->thinking.empty())
                drawProseBox("##actthink", step->thinking, ui::metrics::copilotProseMaxH());
            if (!step->code.empty())
                drawCodeBox("##actcode", step->code, fonts.mono, ui::metrics::px(260.0f));
            if (!step->diff.empty())
                drawDiffBox("##actdiff", step->diff, fonts.mono, ui::metrics::copilotDiffMaxH());
            if (!step->contactSheet.empty()) {
                if (!step->triedThumb) { step->thumb = loadTexture(step->contactSheet); step->triedThumb = true; }
                if (step->thumb.id) {
                    float maxTw = ui::metrics::copilotThumbW();
                    float tw = logW < maxTw ? logW : maxTw;
                    float th = tw * (float)step->thumb.h / (float)step->thumb.w;
                    ImGui::Image((ImTextureID)(intptr_t)step->thumb.id, ImVec2(tw, th));
                }
            }
        } else if (!a.detail.empty()) {
            drawProseBox("##actdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
        }
    }

    ImGui::PopStyleColor();   // base ink
    ImGui::EndChild();
    ImRect cardRect = endGlassBubble(gb);

    // ---- error frame: a red border around the whole card ----
    if (fail) {
        ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                            8.0f * sc, 0, ui::metrics::stroke(1.5f));
    }
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##act_copy", copyTxt);

    }
}

// ============================ M12-M14: inline transparency rows ============================
// Typed rows fed from the aicad.agent_events/2 stream (feedTurnActivity), rendered inline in the
// same timeline as the orchestrator stage rows — subdued and informational, visually distinct
// from the user/assistant bubbles. Nothing is fabricated: no events => none of these render.

// M12 — a compact, dim one-line chip for a context injection: source + label + ~token count.
static void drawContextChip(const ActivityItem& a, const ui::Fonts& fonts) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, 1 * sc));
    ui::IconUV iv = ui::getIcon("bookmark", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.85f;
        ImVec4 tint(surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.9f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 5 * sc);
    }
    std::string line = "Context";
    if (!a.reason.empty()) line += " \xc2\xb7 " + a.reason;          // source (ledger/memory/...)
    line += ": " + a.title;                                          // human label
    if (a.ctxTokens > 0) line += "  (~" + std::to_string(a.ctxTokens) + " tok)";
    ImGui::PushFont(fonts.body);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextWrapped("%s", line.c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    {
        const ImRect chipRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        chatCopyContext(chipRect, "##ctx_copy", line);

    }
}

// M13 — the model's thoughts in a collapsible textbox. While deltas stream (a.streaming) the box
// stays open so tokens appear live; a completed block auto-collapses to a one-line preview unless
// the user pinned it open. `showThinking` off -> a single dim one-liner (matches drawThinkingBlock).
static void drawThoughtBox(ActivityItem& a, const ui::Fonts& fonts, bool showThinking) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, 2 * sc));
    if (!showThinking) {
        ImGui::PushFont(fonts.body);
        ImGui::TextColored(surf.textSecondary, "Thoughts");
        ImGui::PopFont();
        return;
    }
    const bool open = a.expanded || a.streaming;
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##thoughtbox", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushFont(fonts.body);
    ImGui::TextColored(surf.textSecondary, "%s", a.streaming ? "Thinking\xe2\x80\xa6" : "Thoughts");
    ImGui::PopFont();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) a.expanded = !open;
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted(open ? "(hide)" : "(show)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) a.expanded = !open;
    if (open) {
        if (!a.detail.empty())
            drawProseBox("##thoughttxt", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
    } else if (!a.detail.empty()) {
        // Collapsed: a dim one-line preview (first line, ellipsized).
        std::string prev = a.detail.substr(0, a.detail.find('\n'));
        if (prev.size() > 110) { prev.resize(110); prev += "\xe2\x80\xa6"; }
        else if (prev.size() < a.detail.size()) prev += " \xe2\x80\xa6";
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(stripMdMarks(prev).c_str());
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImRect thoughtRect = endGlassBubble(gb);
    const std::string thoughtCopy = chatActivityCopyText(a);
    chatCopyContext(thoughtRect, "##thought_copy", thoughtCopy);

}

// M14 — one inline row per tool call: ok/fail glyph + tool icon + tool name + latency, with the
// human-readable target ("what it acts on") as a dim wrapped line beneath. Every tool the AI
// touches shows in the chat; a long detail body (future args capture) expands on click.
static void drawToolCallRow(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool fail = a.status == "fail";
    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::BeginChild("##toolrow", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();
    drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon("wrench", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(fail ? semanticOnGlass(pal.statusErr, surf) : surf.textPrimary,
                       "%s", a.title.c_str());
    ImGui::PopFont();
    if (!a.detail.empty()) {   // future args/detail capture — expandable drawer
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.expanded ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
    }
    if (fail || a.durationMs > 0) {
        ImGui::SameLine(0, 8 * sc);
        char meta[64];
        if (fail && a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "failed \xc2\xb7 %.1fs", a.durationMs / 1000.0);
        else if (fail)
            std::snprintf(meta, sizeof(meta), "failed");
        else
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
        ImGui::PushStyleColor(ImGuiCol_Text, fail ? semanticOnGlass(pal.statusErr, surf)
                                                  : surf.textSecondary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }
    if (!a.reason.empty()) dimText(a.reason.c_str());   // the human target — "what it acts on"
    if (a.expanded && !a.detail.empty())
        drawProseBox("##tooldetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
    ImGui::EndChild();
    ImRect rowRect = endGlassBubble(gb);
    if (fail)
        ImGui::GetWindowDrawList()->AddRect(rowRect.Min, rowRect.Max,
                                            ui::Palette::toU32(pal.statusErr),
                                            8.0f * sc, 0, ui::metrics::stroke(1.5f));
    {
        const std::string toolCopy = chatActivityCopyText(a);
        chatCopyContext(rowRect, "##tool_copy", toolCopy);

    }
}

// ============================ Corporate Roles timeline cards ============================
// Compact glass cards for org chart / meetings / steering; muted one-liners for route rejections
// and wind-down. Colors only via ui::palette + semanticOnGlass. Right-click Copy via chatCopyContext.


static void drawCorpOrgCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
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

static void drawCorpMeetingCard(ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const bool blocked = a.opsCount > 0 || a.status == "warn";

    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
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
    ImRect cardRect = endGlassBubble(gb);
    if (blocked) {
        ImGui::GetWindowDrawList()->AddRect(
            cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusWarn),
            8.0f * sc, 0, ui::metrics::stroke(1.5f));
    }
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##corp_meet_copy", copyTxt);

    }
}

static void drawCorpSteeringCard(const ActivityItem& a, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
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
    ImRect cardRect = endGlassBubble(gb);
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##corp_steer_copy", copyTxt);

    }
}

// Muted single-line activity (route rejection / wind-down summary).
static void drawCorpMutedLine(const ActivityItem& a, const ui::Fonts& fonts) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, 1 * sc));
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
static void drawAgentTimeline(ChatMsg& m, const ui::Fonts& fonts, float logW, bool showThinking) {
    if (m.activity.empty()) return;
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        // Hidden meta row carrying stop_reason / error_detail — never drawn as a card.
        // Provider-call rows (rowKind 9) aggregate into the collapsible Provider log.
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        ImGui::PushID((int)i);
        if (a.rowKind == 1) {
            drawContextChip(a, fonts);
        } else if (a.rowKind == 2) {
            drawThoughtBox(a, fonts, showThinking);
        } else if (a.rowKind == 3) {
            drawToolCallRow(a, fonts);
        } else if (a.rowKind == 4) {
            drawCorpOrgCard(a, fonts);
        } else if (a.rowKind == 5) {
            drawCorpMeetingCard(a, fonts);
        } else if (a.rowKind == 6) {
            drawCorpSteeringCard(a, fonts);
        } else if (a.rowKind == 7 || a.rowKind == 8) {
            drawCorpMutedLine(a, fonts);
        } else if (isThinkingItem(a)) {
            bool laterDone = false;
            for (size_t j = i + 1; j < m.activity.size(); ++j) {
                if (m.activity[j].rowKind != 0) continue;   // transparency rows don't close stages
                if (m.activity[j].stage == "__stop_meta__") continue;
                const std::string& s = m.activity[j].status;
                if (s == "pass" || s == "done" || s == "fail" || s == "retry") { laterDone = true; break; }
            }
            drawThinkingBlock(a, fonts, showThinking, laterDone);
        } else {
            drawActionCard(a, nullptr, fonts, logW);
        }
        ImGui::PopID();
    }
}

// Forward: user bubble + rewind/edit actions (defined after AI-VC helpers).
static void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                          float panelW, int msgIdx);

// ============================ AI-VC (agent version control) ============================
// Silent, deterministic checkpoints of AI-authored work. Worker RPCs (AiVcCore / CheckpointsCore):
//   plm_timeline {doc_id?} -> {ok, checkpoints:[{id, turn, when|time, label, features|features_count,
//                                                 kind?, msg_idx?, call?:{turn,i}}]}
//   plm_rollback {doc_id?, to: id|"turn:N"|"-N"|"msg:<idx>"|"call:<turn>:<i>"} ->
//     {ok, ..., chat_truncate_to?}
// Degrade: when the worker reports unknown_method the affordances hide permanently this session.
// When v2 fields (kind/msg_idx/call) are absent, UI stays at turn-level Before/After only.

namespace {

struct AiVcCheckpoint {
    std::string id;
    int         turn = -1;     // agent turn index, or -1 if unset
    std::string when;          // ISO timestamp
    std::string label;         // tool/op-batch summary
    int         features = 0;  // feature count at checkpoint
    // v2 prompt/tool granularity (absent => -1 / empty → turn-level UI only)
    std::string kind;          // "prompt" | "tool" | ""
    int         msgIdx = -1;   // chat message ordinal for kind=prompt
    int         callTurn = -1; // call.turn for kind=tool
    int         callI = -1;    // call.i for kind=tool
};

struct AiVcUi {
    bool available = true;     // false after unknown-method (hide all AI-VC chrome)
    bool loading   = false;
    bool busy      = false;
    std::string err;
    std::vector<AiVcCheckpoint> checkpoints;
    double      lastFetch = 0.0;
    std::string lastDocId;
    // v2 capability flags — latched from the last successful timeline payload.
    bool hasMsgGranularity  = false;  // any row carried msg_idx
    bool hasCallGranularity = false;  // any row carried call:{turn,i}
    // Inline confirm (click again) for per-turn / per-msg / per-call restore — never a modal.
    int    confirmMsgIdx = -1;  // chat index pending confirm, -1 = none
    int    confirmKind   = 0;   // 1 = before, 2 = after, 3 = user-msg rewind
    int    confirmCallI  = -1;  // tool-call index within turn when confirmKind is call-scoped
    double confirmUntil  = 0.0; // auto-clear after a few seconds
};
static AiVcUi s_aiVc;

// Staged "Edit message" — rollback is deferred until the user actually sends. Esc restores
// the pre-edit chat tail + composer without any plm_rollback.
struct EditStage {
    bool active = false;
    int  msgIdx = -1;
    std::string savedPrompt;                 // composer contents before edit
    std::vector<ChatMsg> chatTail;           // messages [msgIdx..) parked for Esc restore
};
static EditStage s_edit;

static std::string aiVcActiveDocId(AppState& st) {
    if (st.docMirror.hasDoc()) return st.docMirror.docId();
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        return st.docTabs[st.activeTab].docId;
    return {};
}

// Did this finished assistant turn mutate geometry? Used to gate the restore affordance.
static bool chatMsgMutatedDoc(const ChatMsg& m) {
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
        // Free textures on the tail before erase (GL-safe via freeChatTextures).
        std::vector<ChatMsg> tail(st.chat.begin() + truncateTo, st.chat.end());
        freeChatTextures(tail);
        st.chat.erase(st.chat.begin() + truncateTo, st.chat.end());
        // Invalidate height caches for remaining messages (layout may shift).
        for (auto& m : st.chat) m.uiCachedH = -1.0f;
    }
}

static void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast) {
    if (!s_aiVc.available || s_aiVc.busy || to.empty()) return;
    s_aiVc.busy = true;
    s_aiVc.confirmMsgIdx = -1;
    s_aiVc.confirmKind = 0;
    s_aiVc.confirmCallI = -1;
    nlohmann::json p = nlohmann::json::object();
    const std::string docId = aiVcActiveDocId(st);
    if (!docId.empty()) p["doc_id"] = docId;
    p["to"] = to;
    st.rpc.call("plm_rollback", p, [&st, okToast](const core::RpcResult& r) {
        s_aiVc.busy = false;
        std::string hint;
        if (!aiVcOk(r, hint)) {
            if (s_aiVc.available) st.toast(ui::ToastLevel::Error, hint);
            return;
        }
        aiVcRefreshMirror(st, r.result);
        aiVcApplyChatTruncate(st, r.result);
        st.toast(ui::ToastLevel::Success,
                 okToast.empty() ? std::string("Restored checkpoint") : okToast);
        s_aiVc.lastFetch = 0.0;  // force timeline refresh next open
    });
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
    ImGui::Dummy(ImVec2(0, 4.0f * sc));

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
    const float padX = 8.0f * sc;
    const float padY = 6.0f * sc;
    const float rowH = ImGui::GetTextLineHeight() * 2.0f + padY * 2.0f + 2.0f * sc;
    const float restW = ImGui::CalcTextSize("Restore").x + ImGui::GetStyle().FramePadding.x * 2.0f;

    ImGui::BeginChild("##ai_vc_list", ImVec2(ui::metrics::px(320), ui::metrics::px(280)),
                      ImGuiChildFlags_None, ImGuiWindowFlags_AlwaysVerticalScrollbar);
    for (int i = 0; i < (int)s_aiVc.checkpoints.size(); ++i) {
        const AiVcCheckpoint& cp = s_aiVc.checkpoints[(size_t)i];
        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        ImGui::InvisibleButton("##cprow", ImVec2(std::max(1.0f, rowW), rowH));
        const bool hov = ImGui::IsItemHovered();
        ui::glassRowBackground(dl, rowMin, rowMax, 8.0f * sc, (i % 2) == 1, false,
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
                aiVcRollback(st, cp.id, std::string("Restored: ") + title);
                ImGui::CloseCurrentPopup();
            }
            if (s_aiVc.busy) ImGui::EndDisabled();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Restore the part to this checkpoint.");
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + 3.0f * sc));
        ImGui::PopID();
    }
    ImGui::EndChild();
}

// Draw a double-overlapping left-triangle glyph (◁◁) into `dl` inside `bb`.
// Used for rewind / Before — never text (font may lack the glyph).
static void drawDoubleRewindGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx,
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
static void drawPencilGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx, ImU32 col) {
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

// Shared rectangular less-rounded header button style for Sessions / Checkpoints / gear-as-box.
// Returns true on click. `active` highlights like an open overlay.
static bool drawHeaderTextButton(const char* id, const char* label, bool active, float minW = 0.0f) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec4 txt = surf.textPrimary;
    const float round = ui::metrics::rounding(4.0f);  // less-rounded rectangular box
    ImGui::PushID(id);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, round);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(8.0f * sc, 2.0f * sc));
    ImGui::PushStyleColor(ImGuiCol_Button, active
        ? ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1], ui::palette.chipBg[2],
                 ui::palette.chipBg[3])
        : ImVec4(txt.x, txt.y, txt.z, 0.06f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(txt.x, txt.y, txt.z, 0.14f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(txt.x, txt.y, txt.z, 0.22f));
    ImGui::PushStyleColor(ImGuiCol_Text, active ? surf.textPrimary : surf.textSecondary);
    const float tw = ImGui::CalcTextSize(label).x + ImGui::GetStyle().FramePadding.x * 2.0f;
    const float bw = std::max(minW, tw);
    const bool clicked = ImGui::Button(label, ImVec2(bw, 0));
    ImGui::GetWindowDrawList()->AddRect(
        ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
        ImGui::ColorConvertFloat4ToU32(ImVec4(txt.x, txt.y, txt.z, active ? 0.45f : 0.25f)),
        round, 0, ui::metrics::stroke(1.0f));
    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(2);
    ImGui::PopID();
    return clicked;
}

// Cancel staged edit: restore chat tail + composer, NO plm_rollback.
static void cancelEditStage(AppState& st) {
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
static void beginEditStage(AppState& st, int msgIdx) {
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
static void commitEditStageOnSend(AppState& st) {
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

// Hover actions on a mutating assistant turn: restore before / after this step.
// Glyph language (◁◁) for Before; text "After" kept. Inline confirm = click again.
// When call granularity is present, Before uses call:<turn>:<i> for the first tool of the turn
// if known; otherwise degrades to turn-level "turn:N-1" / "turn:N".
static void drawAiVcTurnRestore(AppState& st, int msgIdx, int turnNum) {
    if (!s_aiVc.available || turnNum <= 0 || s_aiVc.busy) return;
    const float sc = ui::uiScale;
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
        s_aiVc.confirmCallI = -1;
    }
    const bool pending = (s_aiVc.confirmMsgIdx == msgIdx);

    // Latch hover for a short window so the user can move from the "Assistant"
    // label onto the Before/After buttons without them vanishing mid-flight.
    static int    s_hovMsg = -1;
    static double s_hovUntil = 0.0;
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenOverlapped)) {
        s_hovMsg = msgIdx;
        s_hovUntil = now + 0.6;
    }
    const bool latched = (s_hovMsg == msgIdx && now <= s_hovUntil);
    const bool show = pending || latched;
    if (!show) return;

    auto glyphBtn = [&](int kind, const char* tip) {
        ImGui::SameLine(0, 6.0f * sc);
        const bool isPend = pending && s_aiVc.confirmKind == kind;
        const float sz = ImGui::GetFrameHeight();
        ImGui::PushID(kind + 400);
        if (isPend) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.55f));
        }
        const bool clicked = ImGui::InvisibleButton("##rw", ImVec2(sz * 1.15f, sz));
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
        drawDoubleRewindGlyph(dl, bmn, bmx, col);
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
                aiVcRollback(st, to,
                             kind == 1 ? "Restored to before this step"
                                       : "Restored to after this step");
            }
        }
        if (ImGui::IsItemHovered()) {
            s_hovMsg = msgIdx;
            s_hovUntil = now + 0.6;
            ImGui::SetTooltip("%s", isPend ? "Click again to confirm." : tip);
        }
        ImGui::PopID();
    };

    auto textBtn = [&](int kind, const char* idle, const char* tip) {
        ImGui::SameLine(0, 4.0f * sc);
        const bool isPend = pending && s_aiVc.confirmKind == kind;
        if (isPend) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.55f));
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
        }
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4.0f * sc, 1.0f * sc));
        if (ImGui::SmallButton(isPend ? "Confirm?" : idle)) {
            if (!isPend) {
                s_aiVc.confirmMsgIdx = msgIdx;
                s_aiVc.confirmKind = kind;
                s_aiVc.confirmUntil = now + 4.0;
            } else {
                aiVcRollback(st, "turn:" + std::to_string(turnNum),
                             "Restored to after this step");
            }
        }
        ImGui::PopStyleVar();
        if (isPend) ImGui::PopStyleColor(2);
        if (ImGui::IsItemHovered()) {
            s_hovMsg = msgIdx;
            s_hovUntil = now + 0.6;
            ImGui::SetTooltip("%s", isPend ? "Click again to confirm." : tip);
        }
    };

    glyphBtn(1, "Restore the part to the state before this step.");
    textBtn(2, "After", "Restore the part to the state after this step.");
}

}  // namespace

// One user turn: right-aligned bubble; "You" label + rewind/edit sit on ONE row just under
// the bubble (label left, buttons right — mirrors the assistant-row footer style).
static void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                          float panelW, int msgIdx) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ImVec4 accentCol = semanticOnGlass(pal.accent, chatSurf());
    // Right-align: indent ~22% of the panel so the bubble sits against the right edge.
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + panelW * 0.22f);
    const float bw = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble(bw, ui::GlassLevel::Regular, 8.0f);
    ImGui::BeginChild("##umsg", ImVec2(bw, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    // Body only — the "You" author label lives in the footer row under the bubble.
    // Same selectable read-only InputText treatment as assistant prose (soft-wrap +
    // click-drag + Ctrl+C toast + right-click Copy).
    const std::string userBody = stripMdMarks(m.text);
    const ImGuiID textId = chatDrawSelectableText("##umsg_sel", userBody, chatSurf().textPrimary);
    ImGui::EndChild();
    ImRect bubble = endGlassBubble(gb);
    ImGui::GetWindowDrawList()->AddRect(bubble.Min, bubble.Max,
        ImGui::ColorConvertFloat4ToU32(ImVec4(accentCol.x, accentCol.y, accentCol.z, 0.55f)),
        8.0f * ui::uiScale, 0, ui::metrics::stroke(1.0f));
    const std::string sel = chatInputSelection(textId);
    chatCopyContext(bubble, "##umsg_copy", userBody, userBody, sel);
    chatHoverCopyGlyph(bubble, userBody);


    // Footer row: "You" (left) + ◁◁ rewind / pencil edit (right). Tight under the bubble.
    {
        const double now = ImGui::GetTime();
        if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
            s_aiVc.confirmMsgIdx = -1;
            s_aiVc.confirmKind = 0;
        }
        const bool pending = (s_aiVc.confirmMsgIdx == msgIdx && s_aiVc.confirmKind == 3);
        const bool showBtns = s_aiVc.available && msgIdx >= 0 && !s_aiVc.busy;

        const float btnSz = ImGui::GetFrameHeight() * 0.78f;
        const float gap = 4.0f * sc;
        const float totalBtnW = showBtns ? (btnSz + gap + btnSz) : 0.0f;

        // Nearly touch the bubble bottom (~2-3px * uiScale) — no default ItemSpacing gap.
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
        // Pin the footer flush under the bubble in screen space so scroll/layout can't open a gap.
        ImGui::SetCursorScreenPos(ImVec2(bubble.Min.x, bubble.Max.y + 2.5f * sc));

        const float winX = ImGui::GetWindowPos().x - ImGui::GetCurrentWindow()->Scroll.x;
        // Capture the start of the row so we can place buttons on the same line, right-aligned.
        const float rowStartY = ImGui::GetCursorPosY();
        const float rowStartX = ImGui::GetCursorPosX();

        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(accentCol, "You");
        ImGui::PopFont();

        if (showBtns) {
            // Right-align buttons under the bubble's right edge, same baseline as the label.
            const float rowX = bubble.Max.x - totalBtnW - winX;
            ImGui::SetCursorPos(ImVec2(std::max(rowStartX + ImGui::CalcTextSize("You").x + gap,
                                               rowX),
                                      rowStartY + (ImGui::GetTextLineHeight() - btnSz) * 0.5f));

            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ui::AdaptiveSurface& surf = chatSurf();

            // Rewind glyph button
            {
                if (pending) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f));
                }
                const bool clicked = ImGui::InvisibleButton("##umsg_rw", ImVec2(btnSz, btnSz));
                const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
                if (pending || ImGui::IsItemHovered())
                    dl->AddRectFilled(bmn, bmx,
                        ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                              surf.textPrimary.z, pending ? 0.18f : 0.10f)),
                        ui::metrics::rounding(4.0f));
                drawDoubleRewindGlyph(dl, bmn, bmx,
                    ImGui::ColorConvertFloat4ToU32(pending ? surf.textPrimary : surf.textSecondary));
                if (pending) ImGui::PopStyleColor();
                if (clicked) {
                    if (!pending) {
                        s_aiVc.confirmMsgIdx = msgIdx;
                        s_aiVc.confirmKind = 3;
                        s_aiVc.confirmUntil = now + 4.0;
                    } else {
                        aiVcRollback(st, "msg:" + std::to_string(msgIdx),
                                     "Rewound to before this message");
                    }
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(pending ? "Click again to confirm rewind."
                                              : "Rewind — restore the part to before this message.");
            }

            // Pencil edit — atlas icon (msg_edit) with filled-glyph fallback.
            ImGui::SameLine(0, gap);
            {
                const bool clicked = ImGui::InvisibleButton("##umsg_edit", ImVec2(btnSz, btnSz));
                const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
                if (ImGui::IsItemHovered())
                    dl->AddRectFilled(bmn, bmx,
                        ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                              surf.textPrimary.z, 0.10f)),
                        ui::metrics::rounding(4.0f));
                const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
                // Prefer dedicated msg_edit atlas tile (integrator regenerates make_icons);
                // until then, use the filled pencil glyph (never a bare slash).
                IconUV editIcon = ui::getIcon("msg_edit", 20);
                if (editIcon.valid) {
                    const float pad = btnSz * 0.18f;
                    dl->AddImage(editIcon.texId,
                                 ImVec2(bmn.x + pad, bmn.y + pad),
                                 ImVec2(bmx.x - pad, bmx.y - pad),
                                 editIcon.uv0, editIcon.uv1,
                                 ink);
                } else {
                    drawPencilGlyph(dl, bmn, bmx, ink);
                }
                if (clicked) beginEditStage(st, msgIdx);
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Edit this message — Esc cancels without changing the part.");
            }
        }
        ImGui::PopStyleVar();  // ItemSpacing.y = 0 for the tight footer
    }

    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
}


// Collapsible "Provider log" — aggregates rowKind=9 provider_call events into a compact
// monospace-ish list (one line per HTTP attempt). Always expanded when the turn failed;
// collapsed behind a small toggle on success. Right-click copies the full log.
static void drawProviderLog(ChatMsg& m, const ui::Fonts& fonts) {
    std::vector<ActivityItem*> calls;
    calls.reserve(8);
    bool anyFail = false;
    for (auto& a : m.activity) {
        if (a.rowKind != 9) continue;
        calls.push_back(&a);
        if (a.status == "fail" || a.status == "error") anyFail = true;
    }
    if (calls.empty()) return;

    const bool turnFailed = (m.errKind != ERR_NONE) || anyFail;
    // Persist toggle on the first call row; force-open on failure so xai/claude debug is visible.
    bool& open = calls.front()->expanded;
    if (turnFailed) open = true;

    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;

    ImGui::Dummy(ImVec2(0, 3 * sc));
    {
        char hdr[64];
        std::snprintf(hdr, sizeof(hdr), "Provider log (%d)", (int)calls.size());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::PushFont(fonts.medium ? fonts.medium : fonts.body);
        const char* chev = open ? "\xe2\x96\xbc " : "\xe2\x96\xb6 ";  // ▼ / ▶
        ImGui::Text("%s%s", chev, hdr);
        ImGui::PopFont();
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked() && !turnFailed) open = !open;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(turnFailed
                                  ? "Provider HTTP attempts for this run (expanded on failure)."
                                  : "Toggle provider HTTP call log.");
    }
    if (!open) return;

    // Build full copy text once.
    std::string allCopy;
    allCopy.reserve(calls.size() * 96);
    for (const ActivityItem* a : calls) {
        if (!allCopy.empty()) allCopy += "\n";
        allCopy += !a->detail.empty() ? a->detail
                 : (!a->title.empty() ? a->title + "  " + a->reason : a->reason);
    }

    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 6.0f);
    ImGui::BeginChild("##provlog", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    if (fonts.mono) ImGui::PushFont(fonts.mono);
    for (size_t i = 0; i < calls.size(); ++i) {
        const ActivityItem* a = calls[i];
        const bool fail = (a->status == "fail" || a->status == "error");
        // One compact line: "provider · model  HTTP n  Nms  endpoint…"
        std::string line = a->title;
        if (!a->reason.empty()) {
            if (!line.empty()) line += "  ";
            line += a->reason;
        }
        if (line.empty()) line = a->detail.empty() ? "(call)" : a->detail;
        // Collapse multi-line detail into the primary line for the list; error_excerpt stays
        // on a second muted line under fails.
        size_t nl = line.find('\n');
        std::string rest;
        if (nl != std::string::npos) {
            rest = line.substr(nl + 1);
            line.resize(nl);
        }
        ImVec4 col = fail ? semanticOnGlass(pal.statusErr, surf) : surf.textTertiary;
        // Soft red tint even when statusErr is strong — keep it muted for density.
        if (fail) col.w = std::min(col.w, 0.95f);
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextWrapped("%s", line.c_str());
        ImGui::PopStyleColor();
        if (!rest.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                fail ? semanticOnGlass(pal.statusErr, surf) : surf.textTertiary);
            ImGui::TextWrapped("  %s", rest.c_str());
            ImGui::PopStyleColor();
        }
        (void)i;
    }
    if (fonts.mono) ImGui::PopFont();
    ImGui::EndChild();
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##provlog_copy", allCopy);
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              int msgIdx = -1, int turnNum = 0) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& turnSurf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, turnSurf);
    const float sc = ui::uiScale;

    // Author label tight above the turn content (mirrors the You-row tight gap). Prefer the
    // model stamped on the turn at run start; only fall back to the live picker for an
    // in-flight turn that somehow has no stamp. Old sessions with empty model → "Assistant".
    {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
        std::string label = "Assistant";
        const char* mid = nullptr;
        if (!m.model.empty()) mid = m.model.c_str();
        else if (m.running && st.settings.model[0]) mid = st.settings.model;
        if (mid && mid[0]) {
            label += " : ";
            label += shortModelName(mid);
        }
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(turnSurf.textSecondary, "%s", label.c_str());
        ImGui::PopFont();
        // Subtle restore affordance on mutating turns (hover / inline confirm — no modal).
        if (msgIdx >= 0 && turnNum > 0 && chatMsgMutatedDoc(m))
            drawAiVcTurnRestore(st, msgIdx, turnNum);
        ImGui::Dummy(ImVec2(0, 2.5f * sc));
        ImGui::PopStyleVar();
    }

    // (1) Plan / todo block — a live checklist that ticks off as stages complete (DESIGN §5).
    if (m.plan.present) drawPlanCard(m.plan, m.activity, fonts);

    // (2) Agent timeline — typed blocks: collapsible thinking + glass action cards.
    drawAgentTimeline(m, fonts, logW, gShowThinking);

    // (3) Streaming assistant text — a Thin glass "prose" card (Liquid Glass v2 §6, host
    // contract H5) keeps body copy legible over the live viewport blur. Selectable for
    // click-drag + Ctrl+C; right-click copies selection or full text.
    if (!m.text.empty() && m.errKind == ERR_NONE) {
        const std::string prose = stripMdMarks(m.text);
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          4.0f);
        ImGui::BeginChild("##prose", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        const ImGuiID textId = chatDrawSelectableText("##prose_sel", prose, chatSurf().textPrimary);
        ImGui::EndChild();
        ImRect proseRect = endGlassBubble(gb);
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
        chatHoverCopyGlyph(proseRect, prose);
        ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
    }


    // (4) Pre-flight clarify gate: the model's build-critical questions, awaiting the user.
    if (m.clarify.active) drawClarifyCard(st, m, fonts);

    // (5) Legacy v1 AgentStep cards — kept for backward compat (glass-restyled in drawAgentStep).
    for (size_t si = 0; si < m.steps.size(); ++si) {
        ImGui::PushID((int)si);
        drawAgentStep(m.steps[si], fonts, logW);
        ImGui::PopID();
    }

    // (6) Live spinner + model-transparency line + thin progress bar (while running).
    if (m.running) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int sp = (int)(ImGui::GetTime() * 8.0) & 3;
        ImGui::TextColored(accentCol, "%s", kSpin[sp]);
        ImGui::SameLine();
        const ui::AdaptiveSurface& surf = chatSurf();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..." : m.liveStatus.c_str());
        ImGui::PopStyleColor();
        // Which model(s) are driving this turn — transparency about what's actually running.
        {
            std::string line = "Command model: " + shortModelName(st.settings.model);
            if (st.settings.expVisionReview && st.settings.expVisionModel[0])
                line += "   \xc2\xb7   Vision: " + shortModelName(st.settings.expVisionModel);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("%s", line.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, ui::Palette::toU32(pal.accent));
        ImGui::PushStyleColor(ImGuiCol_FrameBg, ui::Palette::toU32(pal.accentSoft));
        float frac = 0.15f + 0.35f * (float)(0.5 + 0.5 * std::sin(ImGui::GetTime() * 2.2));
        ImGui::ProgressBar(frac, ImVec2(-1, 3.0f * ui::uiScale), "");
        ImGui::PopStyleColor(2);
    } else {
        // (7) Usage footer — tokens + wall-time for the finished turn, dim (DESIGN §5).
        int tin = 0, tout = 0, tms = 0;
        for (const auto& a : m.activity) {
            if (a.rowKind != 0) continue;   // tool / ctx / provider rows aren't stage wall-time
            tin += a.tokensIn; tout += a.tokensOut; tms += a.durationMs;
        }
        if (tin > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textTertiary);
            ImGui::Text("%d tokens in / %d out \xc2\xb7 %.1f s", tin, tout, tms / 1000.0);
            ImGui::PopStyleColor();
        }
    }

    // (8) Error block — a distinct red-bordered glass card carrying the real reason (DESIGN §5).
    // Prefer full multi-line detail from stop_reason / error_detail (stashed on a hidden
    // __stop_meta__ activity row by feedTurnActivity). When detail is absent, keep m.text.
    if (m.errKind != ERR_NONE) {
        std::string stopReason, errorDetail;
        for (const auto& a : m.activity) {
            if (a.stage != "__stop_meta__") continue;
            stopReason = a.title;
            errorDetail = !a.detail.empty() ? a.detail : a.reason;
            break;
        }
        // Nothing to show if the turn has no prose AND no stop fields.
        if (!(m.text.empty() && stopReason.empty() && errorDetail.empty())) {
        // Also surface any failed activity reason strings (phase errors) that aren't already
        // folded into the meta row — keeps "budget exhausted (1)" from being the only line.
        std::string phaseErr;
        for (const auto& a : m.activity) {
            if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
            if (a.status != "fail" && a.status != "error") continue;
            std::string bit;
            if (!a.reason.empty()) bit = a.reason;
            else if (!a.detail.empty()) bit = a.detail;
            else if (!a.title.empty()) bit = a.title;
            if (bit.empty()) continue;
            if (errorDetail.find(bit) != std::string::npos) continue;
            if (m.text.find(bit) != std::string::npos) continue;
            if (!phaseErr.empty()) phaseErr += "\n";
            phaseErr += bit;
        }

        // Prefer full detail as the primary body when present; otherwise keep m.text.
        // Headline is always "stopped: <reason>" when stop_reason is known; full error_detail
        // is the selectable body. Generic "I stopped before fully finishing (unknown reason)"
        // only survives when both stop fields are truly absent.
        std::string body;
        if (!stopReason.empty() || !errorDetail.empty()) {
            body = "stopped: ";
            body += stopReason.empty() ? "unknown" : stopReason;
            if (!errorDetail.empty()) {
                body += "\n";
                body += errorDetail;
            }
            // Keep original prose when it adds guidance beyond the terse stop fields.
            const std::string plain = stripMdMarks(m.text);
            if (!plain.empty() && plain != errorDetail &&
                (errorDetail.empty() || plain.find(errorDetail) == std::string::npos) &&
                body.find(plain) == std::string::npos) {
                // Drop the old parenthetical-only early-stop sentence if detail supersedes it.
                const char* kFallback = "I stopped before fully finishing";
                if (plain.find(kFallback) == std::string::npos) {
                    body += "\n\n";
                    body += plain;
                } else {
                    const size_t dot = plain.find(". ");
                    if (dot != std::string::npos && dot + 2 < plain.size()) {
                        body += "\n\n";
                        body += plain.substr(dot + 2);
                    }
                }
            }
        } else {
            body = stripMdMarks(m.text);
        }
        if (!phaseErr.empty()) {
            body += "\n\n";
            body += phaseErr;
        }

        if (!body.empty()) {
            ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
            GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                              8.0f);
            ImGui::BeginChild("##errcard", ImVec2(0, 0),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
            // Error tone retuned for the frost so the reason stays readable over any model.
            const float* ec = (m.errKind == ERR_SYSTEM) ? pal.errSystem : pal.errCad;
            const ImVec4 errCol = semanticOnGlass(ec, chatSurf());
            const ImGuiID textId = chatDrawSelectableText("##err_sel", body, errCol);
            ImGui::EndChild();
            ImRect cardRect = endGlassBubble(gb);
            ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                                8.0f * ui::uiScale, 0, ui::metrics::stroke(1.5f));
            const std::string sel = chatInputSelection(textId);
            chatCopyContext(cardRect, "##err_copy", body, body, sel);
            chatHoverCopyGlyph(cardRect, body);
        }
        } // has something to show
    }

    // (9) Provider HTTP call log — always expanded on fail; collapsed toggle on success.
    if (!m.running)
        drawProviderLog(m, fonts);

}

// ---- header: outline cube + "Billet" wordmark + Sessions + status dot ----
// Copilot vertical-collapse state (file scope so the header's collapse chevron and drawAIPanel's
// height animation share it). Mirrors the FeatureManager collapse (T-G21: rolls UP to this header
// strip instead of shrinking to a pill in both axes). Seeded from st.settings.copilotCollapsed on
// drawAIPanel's first call (see s_cpInited there) so a collapsed column stays collapsed across
// restarts.
static bool s_cpCollapsed = false;
// In-column AI-settings overlay (header gear). Mutually exclusive with showSessionsPicker —
// same swap pattern: hide chat log, fill the body with the compact settings form
// (AppState::showAISettingsOverlay).

// Compact AI-settings popover body (provider + model + effort + run toggles + "All settings...");
// defined below with the picker helpers, hosted by the header gear's popup.
static void drawAISettingsBody(AppState& st, bool busy);

static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lh = ImGui::GetTextLineHeight();

    // One shared row height — the wordmark (heading font) is the tallest element; every fixed-size
    // control centers on it, so the row stays aligned at any DPI without per-item pixel nudges.
    ImFont* wordF = st.fonts.h2 ? st.fonts.h2 : st.fonts.medium;
    float wordLh = lh;
    if (wordF) { ImGui::PushFont(wordF); wordLh = ImGui::GetTextLineHeight(); ImGui::PopFont(); }
    const float rowH   = std::max(wordLh, lh);
    const float rowTop = ImGui::GetCursorPosY();
    auto centerY = [&](float h) { ImGui::SetCursorPosY(rowTop + (rowH - h) * 0.5f); };

    // Collapse chevron LEADS the header row (in-flow, so it never overlaps the cube). "»" tucks
    // the column's body away, rolling the column up to just this header row (T-G21); the same
    // chevron toggles it back open.
    {
        const float cb = lh;
        centerY(cb);
        const ImVec2 c0 = ImGui::GetCursorScreenPos();
        const bool clk = ImGui::InvisibleButton("##cp_collapse", ImVec2(cb, cb));
        const bool hov = ImGui::IsItemHovered();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        ui::glassRowBackground(wdl, c0, ImVec2(c0.x + cb, c0.y + cb), 6.0f, false, false,
                               hov ? 0.42f : 0.20f);
        const ImU32 cc = ImGui::ColorConvertFloat4ToU32(hov ? chatSurf().textPrimary
                                                             : chatSurf().textSecondary);
        const ImVec2 m(c0.x + cb * 0.5f, c0.y + cb * 0.5f);
        const float a = 3.0f * sc;
        for (int i = 0; i < 2; ++i) {   // "»"
            const float ox = (i == 0 ? -2.2f : 2.2f) * sc;
            wdl->AddLine(ImVec2(m.x + ox - a * 0.5f, m.y - a), ImVec2(m.x + ox + a * 0.5f, m.y), cc, ui::metrics::stroke(1.6f));
            wdl->AddLine(ImVec2(m.x + ox + a * 0.5f, m.y), ImVec2(m.x + ox - a * 0.5f, m.y + a), cc, ui::metrics::stroke(1.6f));
        }
        if (hov) ImGui::SetTooltip(s_cpCollapsed ? "Expand" : "Collapse");
        if (clk) {
            s_cpCollapsed = !s_cpCollapsed;
            st.settings.copilotCollapsed = s_cpCollapsed;
            saveSettings(st.settings);
        }
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
    }

    // Outline Billet cube (same glyph as the composer context picker) — theme-tinted line art
    // over the adaptive header ink, not the multi-color app logo (reads better on glass).
    {
        centerY(lh);
        IconUV cube = ui::getIcon("context_cube", 20);
        if (cube.valid) {
            const ImVec4 tint = chatSurf().textPrimary;
            ImGui::Image(cube.texId, ImVec2(lh, lh), cube.uv0, cube.uv1, tint);
        }
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
    }
    // Right-side control cluster: [Sessions|< Chat] [Settings] [status],
    // hard-anchored to the column's right edge so every control is always on-screen (T-G17).
    // Sessions and Settings are mutually exclusive in-column overlays over the chat log.
    // Sessions / Settings share drawHeaderTextButton (rectangular less-rounded boxes).
    // Checkpoints header button removed — AI-VC timeline stays available via per-message rewind.
    const float gearW = ImGui::CalcTextSize("Settings").x + 16.0f * sc;  // text box like Sessions
    const bool sessionsOpen = st.showSessionsPicker;
    const bool settingsOpen = st.showAISettingsOverlay;
    const char* sessLabel = sessionsOpen ? "< Chat" : "Sessions";
    // Widths for anchoring: text buttons stretch to fit label (+ padding from helper).
    const float sessW = std::max(ImGui::CalcTextSize("Sessions").x,
                                 ImGui::CalcTextSize("< Chat").x) + 16.0f * sc;
    const float dotW  = lh;
    const float rightInset = ui::metrics::U(1);
    const float cluster = sessW + style.ItemSpacing.x + gearW +
                          style.ItemSpacing.x + dotW;
    const float anchorX = ImGui::GetContentRegionMax().x - cluster - rightInset;

    // Wordmark: "Billet" in the heading font, "Copilot" as a baseline-aligned secondary-ink
    // suffix. Degrades whole-word (drop the suffix, then the wordmark) — never a mid-word clip.
    {
        const float titleAvail = anchorX - ImGui::GetCursorPosX() - style.ItemSpacing.x;
        float wmW = 0.0f;
        if (wordF) ImGui::PushFont(wordF);
        wmW = ImGui::CalcTextSize("Billet").x;
        if (wordF) ImGui::PopFont();
        const float sufGap = ui::metrics::U(1.5f);
        const float sufW   = sufGap + ImGui::CalcTextSize("Copilot").x;
        if (titleAvail >= wmW) {
            centerY(wordLh);
            if (wordF) ImGui::PushFont(wordF);
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
            ImGui::TextUnformatted("Billet");
            ImGui::PopStyleColor();
            if (wordF) ImGui::PopFont();
            if (titleAvail >= wmW + sufW) {
                ImGui::SameLine(0, sufGap);
                const float dy = (wordF && wordF != ImGui::GetFont())
                                     ? wordF->Ascent - ImGui::GetFont()->Ascent : 0.0f;
                ImGui::SetCursorPosY(rowTop + (rowH - wordLh) * 0.5f + dy);
                ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
                ImGui::TextUnformatted("Copilot");
                ImGui::PopStyleColor();
            }
        }
        ImGui::SameLine();
    }

    if (anchorX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(anchorX);
    centerY(lh);
    if (busy) ImGui::BeginDisabled();
    if (drawHeaderTextButton("##hdr_sess", sessLabel, sessionsOpen, sessW)) {
        st.showSessionsPicker = !st.showSessionsPicker;
        if (st.showSessionsPicker) st.showAISettingsOverlay = false;
    }
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(sessionsOpen ? "Back to the chat."
                                       : "Sessions — switch between chats.");

    ImGui::SameLine();
    centerY(lh);
    // Settings as the same stretched rectangular text box as Sessions (owner ask:
    // identical style, horizontally sized to the label — no icon-only square).
    if (drawHeaderTextButton("##hdr_gear", "Settings", settingsOpen, gearW)) {
        st.showAISettingsOverlay = !st.showAISettingsOverlay;
        if (st.showAISettingsOverlay) st.showSessionsPicker = false;
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(settingsOpen ? "Back to the chat."
                                       : "AI settings — provider, model, privacy.");

    ImGui::SameLine();
    centerY(lh);
    const float* dot = pal.accentMid;  const char* tip = "Idle";
    if (st.statusKind == 2) { dot = pal.statusErr; tip = "Error"; }
    else if (busy || st.agentV2Running || activeConversationRunning(st)) {
        dot = pal.accent; tip = "Running";
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 cp = ImGui::GetCursorScreenPos();
    ImVec4 dotCol = semanticOnGlass(dot, chatSurf());
    dotCol.w = dot[3];
    dl->AddCircleFilled(ImVec2(cp.x + lh * 0.5f, cp.y + lh * 0.5f), lh * 0.30f,
                        ImGui::ColorConvertFloat4ToU32(dotCol));
    ImGui::Dummy(ImVec2(lh, lh));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
    // Close the row at its full shared height so the divider below sits under the tallest element.
    ImGui::SetCursorPosY(std::max(ImGui::GetCursorPosY(), rowTop + rowH));
}

// Sessions list — replaces the chat log while open so users can jump between sessions easily.
static void drawSessionsPanel(AppState& st, bool busy) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ensureConversations(st);

    // New-session button spans the full tab width. Sessions <-> Chat toggle lives in the header.
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("+ New session", ImVec2(-1, 0))) {
        newConversation(st);
        st.prompt[0] = 0;
        st.showAISettingsOverlay = false;
    }
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a fresh conversation.");

    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextWrapped("Switch sessions — running ones keep working in the background.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));

    const ImGuiStyle& style = ImGui::GetStyle();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = 10.0f * sc;
    const float padY = 8.0f * sc;
    const float rowGap = 4.0f * sc;
    const float fh = ImGui::GetFontSize();
    const float titleH = fh;
    const float subH  = fh * 0.92f;
    const float rowH  = padY * 2.0f + titleH + 2.0f * sc + subH;
    const float trashSz = ImGui::GetFrameHeight();

    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        Conversation& c = st.conversations[i];
        std::string title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        bool running = false;
        for (auto it = msgs.rbegin(); it != msgs.rend(); ++it)
            if (!it->fromUser && it->running) { running = true; break; }
        if (i == st.activeConv && aiTurnActive(st)) running = true;
        bool active = (i == st.activeConv);

        char sub[96];
        const int nMsg = (int)msgs.size();
        if (nMsg == 0)
            std::snprintf(sub, sizeof(sub), "Empty session");
        else if (nMsg == 1)
            std::snprintf(sub, sizeof(sub), "1 message");
        else
            std::snprintf(sub, sizeof(sub), "%d messages", nMsg);

        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        bool picked = ImGui::InvisibleButton("##pickconv",
                                            ImVec2(std::max(1.0f, rowW - trashSz - padX), rowH));
        bool rowHov = ImGui::IsItemHovered();
        if (picked && !busy) {
            switchConversation(st, i);
            st.showAISettingsOverlay = false;
        }

        float wash = active ? 0.55f : (rowHov ? 0.42f : 0.28f);
        ui::glassRowBackground(dl, rowMin, rowMax, 8.0f * sc, (i % 2) == 1, active, wash);

        const float titleY = rowMin.y + padY;
        ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
        dl->AddText(ImVec2(rowMin.x + padX, titleY), ink, title.c_str());
        if (running) {
            const float tw = ImGui::CalcTextSize(title.c_str()).x;
            const char* badge = "running";
            const ImVec2 bsz = ImGui::CalcTextSize(badge);
            const float bx0 = rowMin.x + padX + tw + 8.0f * sc;
            const float by0 = titleY + (titleH - bsz.y) * 0.5f - 1.0f * sc;
            const float bx1 = bx0 + bsz.x + 8.0f * sc;
            const float by1 = by0 + bsz.y + 2.0f * sc;
            ImVec4 acc = semanticOnGlass(ui::palette.accent, surf);
            acc.w = 0.22f;
            dl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx1, by1),
                              ImGui::ColorConvertFloat4ToU32(acc), 4.0f * sc);
            ImVec4 accInk = semanticOnGlass(ui::palette.accent, surf);
            accInk.w = 1.0f;
            dl->AddText(ImVec2(bx0 + 4.0f * sc, by0 + 1.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(accInk), badge);
        }

        dl->AddText(ImVec2(rowMin.x + padX, titleY + titleH + 2.0f * sc),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub);

        bool doDel = false;
        if (rowHov || active) {
            const float img  = trashSz - 8.0f * sc;
            const float btnW = img + style.FramePadding.x * 2.0f;
            const float btnH = img + style.FramePadding.y * 2.0f;
            ImGui::SetCursorScreenPos(
                ImVec2(rowMax.x - btnW - 4.0f * sc, rowMin.y + (rowH - btnH) * 0.5f));
            IconUV trash = ui::getIcon("delete", 20);
            bool del = trash.valid
                ? ImGui::ImageButton("##delconv", trash.texId, ImVec2(img, img), trash.uv0, trash.uv1)
                : ImGui::SmallButton("x");
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Delete this session");
            doDel = del && !busy;
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
        ImGui::PopID();
        if (doDel) { closeConversation(st, i); break; }
    }
}


// ============================ conversation tabs + persistence ============================
// Each conversation is its own tab. The ACTIVE conversation's messages live in st.chat; the
// rest park in st.conversations[i].msgs. Persisted per document to chat_history.json.

static std::string chatHistoryPath() { return appdataAppDir() + "/chat_history.json"; }

// A stable key for the active document: the saved file path if any (survives restarts), else
// the live doc id, else "scratch" (an unsaved, never-touched session).
static std::string convDocKey(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        const auto& t = st.docTabs[st.activeTab];
        if (!t.path.empty()) return "file:" + t.path;
        if (!t.docId.empty()) return "doc:" + t.docId;
    }
    std::string d = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    return d.empty() ? std::string("scratch") : ("doc:" + d);
}

static std::string newConvId(AppState& st) { return "c" + std::to_string(++st.convCounter); }

// Guarantee at least one (active) conversation exists, adopting whatever is already in st.chat.
static void ensureConversations(AppState& st) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) return;
    st.conversations.clear();
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat 1";
    st.conversations.push_back(std::move(c));
    st.activeConv = 0;  // st.chat remains the active buffer
}

void persistChatHistory(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    if (st.chatDocKey.empty()) return;
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(chatHistoryPath());
        if (in) { try { in >> root; } catch (...) { root = nlohmann::json::object(); } }
        if (!root.is_object()) root = nlohmann::json::object();
    }
    nlohmann::json convs = nlohmann::json::array();
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : st.conversations[i].msgs;
        nlohmann::json jm = nlohmann::json::array();
        for (const auto& m : msgs) {
            if (m.text.empty()) continue;  // skip running/placeholder turns with no content yet
            nlohmann::json entry = {{"u", m.fromUser}, {"t", m.text}, {"e", m.errKind}};
            if (!m.fromUser && !m.model.empty()) entry["m"] = m.model;
            jm.push_back(std::move(entry));
        }
        convs.push_back({{"id", st.conversations[i].id},
                         {"title", st.conversations[i].title},
                         {"msgs", jm}});
    }
    root[st.chatDocKey] = {{"active", st.activeConv}, {"conversations", convs}};
    try {
        std::filesystem::create_directories(appdataAppDir());
        std::ofstream out(chatHistoryPath());
        if (out) out << root.dump(1);
    } catch (...) {
    }
}

void pollChatOpenFoam(AppState& st) {
    if (!st.chatCfdPending || !st.simClient) return;
    core::SimJobResult& job = st.simClient->job();
    if (!job.ready.load()) return;                 // OpenFOAM still meshing/solving
    if (job.ownerDocId != "__chat_cfd__") return;  // not the chat's job (a study's result)
    st.chatCfdPending = false;
    job.ready = false;                             // consume (SimManager ignored it — wrong owner)

    ChatMsg* msg = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) { msg = &*it; break; }

    std::string answer, err;
    if (!job.ok) {
        err = job.error.empty() ? "OpenFOAM run failed" : job.error;
    } else {
        const double cd = job.result.value("cd", 0.0), cl = job.result.value("cl", 0.0);
        const double drag = job.result.value("drag_n", 0.0), lift = job.result.value("lift_n", 0.0);
        const double re = job.result.value("reynolds", 0.0);
        char b[512];
        std::snprintf(b, sizeof(b),
            "Accurate OpenFOAM RANS (k-omega SST) at %.1f m/s, %.1f deg AoA (Re %.0f): drag %.3f N, "
            "lift %.3f N, Cd %.3f, Cl %.3f. Validated solver — trust these as absolute coefficients.",
            st.chatCfdVel, st.chatCfdAoa, re, drag, lift, cd, cl);
        answer = b;
    }
    if (msg) {
        msg->running = false; msg->liveStatus.clear();
        if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
        else msg->text = answer;
    }
    st.status = err.empty() ? "Ready." : "CFD failed.";
    st.statusKind = err.empty() ? 0 : 2;
}

// Replace conversations/chat with the saved set for `key` (or a fresh empty chat if none).
static void loadChatHistoryForKey(AppState& st, const std::string& key) {
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(chatHistoryPath());
        if (in) { try { in >> root; } catch (...) {} }
    }
    bool found = root.is_object() && root.contains(key) && root[key].is_object();
    if (found) {
        freeChatTextures(st.chat);
        st.chat.clear();
        st.conversations.clear();
        const nlohmann::json& e = root[key];
        for (const auto& jc : e.value("conversations", nlohmann::json::array())) {
            Conversation c;
            c.id = jc.value("id", newConvId(st));
            c.title = jc.value("title", std::string("Chat"));
            for (const auto& m : jc.value("msgs", nlohmann::json::array())) {
                ChatMsg cm;
                cm.fromUser = m.value("u", false);
                cm.text = m.value("t", std::string());
                cm.errKind = m.value("e", 0);
                if (!cm.fromUser) cm.model = m.value("m", std::string());
                cm.running = false;
                c.msgs.push_back(std::move(cm));
            }
            st.conversations.push_back(std::move(c));
        }
        if (st.conversations.empty()) { st.activeConv = -1; ensureConversations(st); return; }
        int act = e.value("active", 0);
        if (act < 0 || act >= (int)st.conversations.size()) act = 0;
        st.activeConv = act;
        st.chat = std::move(st.conversations[act].msgs);
        st.lastContextTokens = st.conversations[act].lastContextTokens;
        st.queuedPrompts = std::move(st.conversations[act].queuedPrompts);
        st.conversations[act].msgs.clear();
        st.conversations[act].queuedPrompts.clear();
    } else if (st.conversations.empty() && st.activeConv < 0) {
        ensureConversations(st);  // first init: adopt current st.chat (welcome) as conversation 1
    } else {
        freeChatTextures(st.chat);  // switching to a different doc with no history: fresh chat
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
        st.conversations.clear();
        st.activeConv = -1;
        ensureConversations(st);
    }
}

// Detect a document change and swap the chat history accordingly (called each frame).
static void syncChatToDoc(AppState& st) {
    std::string key = convDocKey(st);
    if (key == st.chatDocKey) { ensureConversations(st); return; }
    if (!st.chatDocKey.empty()) persistChatHistory(st);  // save the outgoing doc's conversations
    loadChatHistoryForKey(st, key);
    st.chatDocKey = key;
}

static void parkActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    c.msgs = std::move(st.chat);
    c.lastContextTokens = st.lastContextTokens;
    c.queuedPrompts = std::move(st.queuedPrompts);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
}

static void restoreActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    st.chat = std::move(c.msgs);
    st.lastContextTokens = c.lastContextTokens;
    st.queuedPrompts = std::move(c.queuedPrompts);
    c.msgs.clear();
    c.queuedPrompts.clear();
}

static bool conversationRunning(const Conversation& c) {
    for (auto it = c.msgs.rbegin(); it != c.msgs.rend(); ++it)
        if (!it->fromUser && it->running) return true;
    return false;
}

static bool activeConversationRunning(AppState& st) {
    if (aiTurnActive(st)) return true;
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
        return conversationRunning(st.conversations[st.activeConv]);
    return false;
}

static void newConversation(AppState& st) {
    ensureConversations(st);
    parkActiveConversation(st);
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat " + std::to_string((int)st.conversations.size() + 1);
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;
    st.showSessionsPicker = false;
    persistChatHistory(st);
}

static void switchConversation(AppState& st, int idx) {
    if (idx == st.activeConv || idx < 0 || idx >= (int)st.conversations.size()) return;
    parkActiveConversation(st);
    st.activeConv = idx;
    restoreActiveConversation(st);
    st.showSessionsPicker = false;
    persistChatHistory(st);
}

static void closeConversation(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return;
    bool closingActive = (idx == st.activeConv);
    if (closingActive) {
        freeChatTextures(st.chat);
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
    } else {
        freeChatTextures(st.conversations[idx].msgs);
    }
    st.conversations.erase(st.conversations.begin() + idx);
    if (st.conversations.empty()) {
        st.activeConv = -1;
        ensureConversations(st);
        restoreActiveConversation(st);
        persistChatHistory(st);
        return;
    }
    if (closingActive) {
        int na = std::min(idx, (int)st.conversations.size() - 1);
        st.activeConv = na;
        restoreActiveConversation(st);
    } else if (idx < st.activeConv) {
        st.activeConv--;
    }
    persistChatHistory(st);
}

// (Conversations are switched from the Sessions picker in the Copilot header.)

// Probe the local OpenAI-compatible endpoint for its loaded models + reachability, so the
// picker can hide local models that are down. Throttled to ~5s; force bypasses the throttle.
void refreshModelAvailability(AppState& st, bool force) {
    // Discover models on the LOCAL endpoint regardless of which provider is currently active, so
    // local models always show up in the picker WITHOUT the user first switching the Provider
    // dropdown to "Local". Previously this early-returned whenever a cloud provider (OpenRouter /
    // DashScope / ...) was active — st.settings.baseUrl is then a remote URL — which is exactly why
    // local models were invisible until the user "poked around in settings" and selected Local.
    auto isLoopback = [](const char* u) {
        std::string s = u ? u : "";
        return s.find("localhost") != std::string::npos ||
               s.find("127.0.0.1") != std::string::npos ||
               s.find("0.0.0.0") != std::string::npos || s.find("[::1]") != std::string::npos;
    };
    // When the active base_url IS loopback, probe it (honors a custom local port). Otherwise a cloud
    // provider is active, so its base_url isn't the local one — fall back to the default LM Studio
    // endpoint so local discovery still happens. (A non-default local port is only auto-found while
    // the Local provider is the active one; the default covers the common case.)
    bool activeLoopback = isLoopback(st.settings.baseUrl);
    std::string localUrl = activeLoopback ? std::string(st.settings.baseUrl)
                                          : std::string("http://localhost:1234/v1");
    std::string localKey = activeLoopback ? std::string(st.settings.apiKey) : std::string();
    static double lastProbe = -1e9;
    // Failure backoff (freeze fix): when the local endpoint is DOWN, each probe burns ~5s in a
    // curl connect-fail INSIDE the worker, holding the WorkerClient mutex. At the old flat 5s
    // cadence the worker was busy-failing near-100% of the time, so any SYNC worker.call from
    // the GL thread (CAM/ECAD panels on workspace switch) blocked behind it — the app appeared
    // hard-frozen and had to be force-killed. Back off to 60s after a failed probe (force — the
    // user clicking refresh in the picker — still bypasses); a success restores the 5s cadence.
    static bool lastProbeFailed = false;
    const double cooldown = lastProbeFailed ? 60.0 : 5.0;
    double now = ImGui::GetTime();
    if (!force && (st.modelProbeInFlight.load() || (now - lastProbe) < cooldown)) return;
    lastProbe = now;
    st.modelProbeInFlight.store(true);
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", localUrl},
                        {"api_key", localKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            pm.error = r.result.value("error", std::string());
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            // LM Studio reports which models are actually loaded (vs merely downloaded); when
            // present, the picker shows only those. Absent => every listed model is available.
            pm.loadedKnown = r.result.value("loaded_known", false);
            if (r.result.contains("loaded") && r.result["loaded"].is_array())
                for (const auto& m : r.result["loaded"])
                    if (m.is_string()) pm.loaded.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        lastProbeFailed = !pm.ok;   // drives the 60s down-endpoint backoff above
        st.providerModels[ui::PROV_LOCAL] = pm;
        st.providerModels[ui::PROV_LLAMACPP] = pm;  // same local endpoint family
        st.modelProbeInFlight.store(false);
    }, 12000);
}

// A row in the OpenRouter model list (curated preset or live-discovered free model).
struct OpenRouterModel {
    std::string id;
    std::string label;
    bool vision = false;
    bool free = false;
};
std::vector<OpenRouterModel> openRouterModels(AppState& st);  // fwd (defined just below)

// Probe OpenRouter's public /models catalog and cache the list of currently-FREE model ids (the
// endpoint exposes per-model pricing). Stored in providerModels[PROV_CLOUDCOMPAT] so the OpenRouter
// category can list every free model live instead of from a stale hardcoded set. Listing needs no
// key (the catalog is public); running a free model still needs the OpenRouter key. Throttled — the
// catalog changes slowly.
void refreshOpenRouterModels(AppState& st, bool force) {
    static double last = -1e9;
    double now = ImGui::GetTime();
    if (!force && (now - last) < 60.0) return;
    last = now;
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", "https://openrouter.ai/api/v1"},
                        {"api_key", st.settings.cloudKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            if (r.result.contains("free") && r.result["free"].is_array())
                for (const auto& m : r.result["free"])
                    if (m.is_string()) pm.free.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        st.providerModels[ui::PROV_CLOUDCOMPAT] = pm;
    }, 12000);
}

// The OpenRouter model list shown in the picker/manage UI: the curated presets PLUS every live
// free model the catalog probe found (deduped). `free`/`vision` flags drive the row status.
std::vector<OpenRouterModel> openRouterModels(AppState& st) {
    refreshOpenRouterModels(st, false);
    std::vector<OpenRouterModel> out;
    std::set<std::string> seen;
    for (const auto& p : ui::cloudPresets()) {
        if (std::strcmp(ui::cloudServiceKey(p.baseUrl), "openrouter") != 0) continue;
        out.push_back({p.id, p.label, p.vision,
                       p.id.find(":free") != std::string::npos});
        seen.insert(p.id);
    }
    for (const auto& id : st.providerModels[ui::PROV_CLOUDCOMPAT].free)
        if (seen.insert(id).second) out.push_back({id, id, false, true});
    return out;
}

// The active provider's API-key buffer (cloud providers keep separate keys; local reuses the
// optional apiKey buffer).
static char* providerKeyBuf(Settings& s, int provider) {
    switch (provider) {
        case ui::PROV_GEMINI: return s.geminiKey;
        case ui::PROV_ANTHROPIC: return s.anthropicKey;
        case ui::PROV_OPENAI: return s.openaiKey;
        case ui::PROV_CLOUDCOMPAT: return s.cloudKey;  // DashScope / DeepSeek / OpenAI-compatible
        case ui::PROV_XAI: return s.xaiKey;
        default: return s.apiKey;  // local OpenAI-compatible optional key
    }
}

// Availability helpers (defined with the model-dropdown section below; forward-declared so the
// "Manage models" panel inside drawAiSettingsSection can call them).
static bool providerUsable(AppState& st, int provider);
static bool localModelLoaded(const ProviderModels& pm, const std::string& id);

// Shared AI settings, split by section so Settings can glass-group them.
// ProviderModel = provider + model + auth/status; Generation = effort/tokens/window;
// Privacy = offline + thoughts; ManageModels = chat-picker visibility.
bool drawAiSettingsSection(AppState& st, bool busy, AiSettingsSection section) {
    (void)busy;
    Settings& s = st.settings;
    const float S = ui::uiScale;
    bool changed = false;
    refreshModelAvailability(st, false);

    auto drawProviderModel = [&]() {
    // ---- provider ----
    ImGui::TextUnformatted("Provider");
    ImGui::SetNextItemWidth(-1);
    const char* provs[ui::PROV_COUNT];
    for (int i = 0; i < ui::PROV_COUNT; ++i) provs[i] = ui::providerLabel(i);
    int prevProvider = s.provider;
    if (ImGui::Combo("##provider", &s.provider, provs, ui::PROV_COUNT) &&
        s.provider != prevProvider) {
        changed = true;
        // Set sensible per-provider defaults on switch.
        if (s.provider == ui::PROV_GEMINI && (!s.model[0] || std::strstr(s.model, "gpt") ||
                                              std::strstr(s.model, "claude")))
            std::strncpy(s.model, "gemini-2.5-pro", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_ANTHROPIC)
            std::strncpy(s.model, "claude-opus-4-8", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_OPENAI)
            std::strncpy(s.model, "gpt-5.6-sol", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_LOCAL) {
            std::strncpy(s.baseUrl, "http://localhost:1234/v1", sizeof(s.baseUrl) - 1);
            if (std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
                std::strstr(s.model, "claude"))
                std::strncpy(s.model, "gemma-4-e4b", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_LLAMACPP) {
            std::strncpy(s.baseUrl, "http://127.0.0.1:1234/v1", sizeof(s.baseUrl) - 1);
            if (!std::strstr(s.model, ".gguf"))
                std::strncpy(s.model, "%APPDATA%\\AI-CAD\\models\\model.gguf",
                             sizeof(s.model) - 1);
            s.vision = false;
        } else if (s.provider == ui::PROV_CLOUDCOMPAT) {
            // OpenRouter is the paid-model hub going forward — default the cloud slot to it.
            const auto& cp = ui::cloudPresets();
            if (!cp.empty()) {
                std::strncpy(s.model, cp[0].id.c_str(), sizeof(s.model) - 1);
                std::strncpy(s.baseUrl, cp[0].baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = cp[0].vision;
            }
        } else if (s.provider == ui::PROV_XAI) {
            std::strncpy(s.model, "grok-4", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_CURSOR) {
            std::strncpy(s.model, "composer-2.5", sizeof(s.model) - 1);
            s.vision = true;
        }
    }

    ImGui::Dummy(ImVec2(0, 4 * S));
    const bool cloud = ui::providerIsCloud(s.provider);

    // ---- model ----
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible API: OpenRouter (paid-model hub) + Alibaba DashScope + DeepSeek.
        // A preset bundles the service base URL + model id; credentials edited below.
        ImGui::TextUnformatted("Service / model preset");
        dimText("OpenRouter (one key, many models — paid hub), Alibaba Cloud (DashScope) Qwen, "
                "and the official DeepSeek API. CLOUD providers — Offline (ITAR) mode refuses them.");
        const auto& cps = ui::cloudPresets();
        std::vector<const char*> plabels;
        plabels.push_back("(choose a preset\xe2\x80\xa6)");
        for (const auto& p : cps) plabels.push_back(p.label.c_str());
        int psel = 0;
        for (int i = 0; i < (int)cps.size(); ++i)
            if (s.model[0] && cps[i].id == s.model) { psel = i + 1; break; }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##cloudpreset", &psel, plabels.data(), (int)plabels.size()) &&
            psel > 0 && psel - 1 < (int)cps.size()) {
            const auto& p = cps[psel - 1];
            std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
            std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = p.vision;
            changed = true;
        }
        if (psel > 0 && psel - 1 < (int)cps.size() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", cps[psel - 1].note.c_str());
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("Model id");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudmodel", s.model, sizeof(s.model))) changed = true;
    } else if (cloud) {
        // Known-good model list for this provider, plus the current value if it's custom.
        std::vector<const char*> ids, labels;
        for (const auto& m : ui::builtinModels())
            if (m.provider == s.provider) { ids.push_back(m.id.c_str()); labels.push_back(m.label.c_str()); }
        int cur = -1;
        for (size_t i = 0; i < ids.size(); ++i)
            if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
        std::string customLabel;
        if (cur < 0) {  // present whatever is configured as a trailing custom entry
            customLabel = std::string(s.model[0] ? s.model : "(choose a model)") + "  (custom)";
            labels.push_back(customLabel.c_str());
            cur = (int)labels.size() - 1;
        }
        ImGui::TextUnformatted("Model");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##modelpick", &cur, labels.data(), (int)labels.size())) {
            if (cur >= 0 && cur < (int)ids.size()) {
                std::strncpy(s.model, ids[cur], sizeof(s.model) - 1);
                changed = true;
            }
        }
    } else {
        // Local: list discovered models when the endpoint is up; else a free-text field.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        bool useGguf = (s.provider == ui::PROV_LLAMACPP);
        // Experimental: one-click presets for the research-backed open-weight local models
        // (Qwen2.5-Coder / Qwen2.5-VL / DeepSeek-R1). Fills the model id + vision flag + a default
        // base URL; the weights must already be loaded in the local server.
        if (!useGguf) {
            ImGui::TextUnformatted("Quick preset");
            dimText("Open-weight, self-hostable models recommended for the copilot (ITAR-safe). "
                    "Fills the model id, vision flag, and a default base URL. The weights must "
                    "already be loaded in your local server.");
            const auto& presets = ui::localPresets();
            std::vector<const char*> plabels;
            plabels.push_back("(choose a preset…)");
            for (const auto& p : presets) plabels.push_back(p.label.c_str());
            int psel = 0;
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localpreset", &psel, plabels.data(), (int)plabels.size()) &&
                psel > 0 && psel - 1 < (int)presets.size()) {
                const auto& p = presets[psel - 1];
                std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
                if (!p.baseUrl.empty())
                    std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = p.vision;
                changed = true;
                refreshModelAvailability(st, true);  // re-probe for the (possibly new) endpoint
            }
            if (psel > 0 && psel - 1 < (int)presets.size() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", presets[psel - 1].note.c_str());
            ImGui::Dummy(ImVec2(0, 4 * S));
        }
        ImGui::TextUnformatted(useGguf ? "GGUF model path" : "Model");
        if (!useGguf && pm.ok && !pm.models.empty()) {
            std::vector<const char*> ids;
            for (const auto& id : pm.models) ids.push_back(id.c_str());
            int cur = -1;
            for (size_t i = 0; i < ids.size(); ++i)
                if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
            std::string customLabel;
            if (cur < 0) { customLabel = std::string(s.model[0] ? s.model : "(select)") + "  (custom)";
                           ids.push_back(customLabel.c_str()); cur = (int)ids.size() - 1; }
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localmodel", &cur, ids.data(), (int)ids.size())) {
                if (cur >= 0 && (size_t)cur < pm.models.size()) {
                    std::strncpy(s.model, pm.models[cur].c_str(), sizeof(s.model) - 1);
                    changed = true;
                }
            }
        } else {
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localmodeltext", s.model, sizeof(s.model))) changed = true;
        }
    }

    // ---- credentials / connection ----
    ImGui::Dummy(ImVec2(0, 6 * S));
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible: needs BOTH an endpoint (set by the preset, editable) and a key.
        // OpenRouter is first-class — key status names the active service.
        const char* svc = ui::cloudServiceKey(s.baseUrl);
        const bool isOR = (std::strcmp(svc, "openrouter") == 0);
        ImGui::TextUnformatted(isOR ? "OpenRouter endpoint" : "Endpoint (base URL)");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudbase", s.baseUrl, sizeof(s.baseUrl))) changed = true;
        ImGui::TextUnformatted(isOR ? "OpenRouter API key" : "API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        if (isOR)
            ImGui::TextUnformatted(haveKey ? "OpenRouter key set — models available (blocked in Offline mode)"
                                           : "no OpenRouter key — add one at openrouter.ai/keys");
        else
            ImGui::TextUnformatted(haveKey ? "key set — cloud API available (blocked in Offline mode)"
                                           : "no API key — add your OpenRouter / DashScope / DeepSeek key");
        ImGui::PopStyleColor();
    } else if (s.provider == ui::PROV_XAI) {
        // xAI: SuperGrok / X Premium+ device-code OAuth AND/OR classic API key (console.x.ai).
        // Sign-in uses subscription_login{provider:"xai",device:true}; GUI polls subscription_status.
        static bool s_xaiLoginBusy = false;
        static bool s_xaiConnected = false;
        static bool s_xaiBrowserOpened = false;
        static std::string s_xaiEmail;
        static std::string s_xaiError;
        static std::string s_xaiUserCode;
        static std::string s_xaiVerifyUrl;
        static std::string s_xaiStage;
        static double s_xaiLastPoll = -1e9;

        auto pollXaiStatus = [&](bool force) {
            double now = ImGui::GetTime();
            if (!force && (now - s_xaiLastPoll) < 1.5) return;
            s_xaiLastPoll = now;
            st.rpc.call("subscription_status", nlohmann::json::object(),
                        [](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object()) return;
                auto providers = r.result.value("providers", nlohmann::json::object());
                auto x = providers.value("xai", nlohmann::json::object());
                s_xaiConnected = x.value("connected", false);
                s_xaiEmail = x.value("email", std::string());
                s_xaiLoginBusy = x.value("login_in_progress", false);
                s_xaiBrowserOpened = x.value("browser_opened", false);
                s_xaiUserCode = x.value("user_code", std::string());
                s_xaiVerifyUrl = x.value("verification_url", std::string());
                if (s_xaiVerifyUrl.empty())
                    s_xaiVerifyUrl = x.value("verification_uri", std::string());
                s_xaiStage = x.value("login_stage", std::string());
                if (auto err = x.value("last_error", std::string()); !err.empty())
                    s_xaiError = err;
                else if (s_xaiConnected)
                    s_xaiError.clear();
            });
        };
        pollXaiStatus(false);

        dimText("SuperGrok / X Premium+ subscription (device-code OAuth) or API key from console.x.ai.");
        if (s_xaiLoginBusy) {
            // Opening browser… then show the device code + fallback link.
            if (s_xaiUserCode.empty() && s_xaiVerifyUrl.empty()) {
                ImGui::TextUnformatted("Opening browser\xe2\x80\xa6");
            } else {
                if (s_xaiBrowserOpened)
                    ImGui::TextUnformatted("Browser opened \xe2\x80\x94 approve access, then wait\xe2\x80\xa6");
                else
                    ImGui::TextUnformatted("Could not auto-open browser \xe2\x80\x94 open the link below:");
                if (!s_xaiUserCode.empty()) {
                    ImGui::Text("Code: %s", s_xaiUserCode.c_str());
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Click 'Copy code' to copy this to the clipboard.");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Copy code##xai"))
                        ImGui::SetClipboardText(s_xaiUserCode.c_str());
                }
                const std::string fallbackUrl =
                    s_xaiVerifyUrl.empty() ? "https://accounts.x.ai/activate" : s_xaiVerifyUrl;
                ImGui::TextWrapped("%s", fallbackUrl.c_str());
                if (ImGui::SmallButton("Copy URL##xai"))
                    ImGui::SetClipboardText(fallbackUrl.c_str());
                ImGui::SameLine();
                if (ImGui::SmallButton("Open in browser##xai")) {
                    // Best-effort local open when the worker process is headless.
#if defined(_WIN32)
                    std::system(("start \"\" \"" + fallbackUrl + "\"").c_str());
#elif defined(__APPLE__)
                    std::system(("open '" + fallbackUrl + "'").c_str());
#else
                    std::system(("xdg-open '" + fallbackUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                }
            }
            pollXaiStatus(true);
        } else if (s_xaiConnected) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
            if (!s_xaiEmail.empty())
                ImGui::Text("Connected as %s", s_xaiEmail.c_str());
            else
                ImGui::TextUnformatted("Connected via SuperGrok / X Premium+");
            ImGui::PopStyleColor();
            if (ImGui::SmallButton("Sign out##xai")) {
                st.rpc.call("subscription_logout", nlohmann::json{{"provider", "xai"}},
                            [](const core::RpcResult&) {});
                s_xaiConnected = false;
                s_xaiEmail.clear();
                s_xaiError.clear();
                s_xaiUserCode.clear();
                s_xaiVerifyUrl.clear();
                s_xaiStage.clear();
                s_xaiBrowserOpened = false;
                s_xaiLastPoll = -1e9;
            }
        } else {
            if (ImGui::Button("Sign in with xAI", ImVec2(-1, 0))) {
                s_xaiLoginBusy = true;
                s_xaiError.clear();
                s_xaiUserCode.clear();
                s_xaiVerifyUrl.clear();
                s_xaiStage = "start";
                s_xaiBrowserOpened = false;
                st.rpc.call("subscription_login",
                            nlohmann::json{{"provider", "xai"}, {"device", true}},
                            [](const core::RpcResult& r) {
                    if (!r.ok) {
                        s_xaiLoginBusy = false;
                        s_xaiError = r.err.empty() ? "login failed" : r.err;
                    } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                        s_xaiLoginBusy = false;
                        auto reason = r.result.value("reason", std::string());
                        auto hint = r.result.value("hint", std::string());
                        s_xaiError = reason.empty() ? "login failed" : reason;
                        if (!hint.empty()) s_xaiError += " — " + hint;
                    }
                    s_xaiLastPoll = -1e9;
                });
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Opens the accounts.x.ai device-code flow in your browser.\n"
                    "Approve on phone/browser; status flips to Connected when done.");
        }
        if (!s_xaiError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_xaiError.c_str());
            ImGui::PopStyleColor();
            // Honest Heavy-gating surface: if the reason mentions API-enabled / Heavy, keep the
            // console.x.ai hint visible (subscription_login already includes it).
        }

        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("API key (console.x.ai — optional if signed in)");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##xaikey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text,
            (haveKey || s_xaiConnected) ? ui::Palette::toU32(ui::palette.statusOk)
                                        : ui::Palette::toU32(ui::palette.statusWarn));
        if (haveKey)
            ImGui::TextUnformatted("API key set — uses provider 'xai' (blocked in Offline mode)");
        else if (s_xaiConnected)
            ImGui::TextUnformatted("OAuth connected — uses provider 'xai-sub' (blocked in Offline mode)");
        else
            ImGui::TextUnformatted("Sign in with SuperGrok or paste an API key from console.x.ai");
        ImGui::PopStyleColor();
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (cloud) {
        ImGui::TextUnformatted("API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextUnformatted(haveKey ? "key set — model available"
                                       : "no API key — add one to use this provider");
        ImGui::PopStyleColor();
    } else {
        ImGui::TextUnformatted("Base URL");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##baseurl", s.baseUrl, sizeof(s.baseUrl))) {
            changed = true;
            refreshModelAvailability(st, true);  // re-probe the new endpoint
        }
        if (s.provider == ui::PROV_LOCAL) {
            ImGui::TextUnformatted("API key (optional)");
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localkey", s.apiKey, sizeof(s.apiKey),
                                 ImGuiInputTextFlags_Password)) changed = true;
        }
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        // Local availability status.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        const char* msg; ImU32 col;
        if (!pm.checked) { msg = "checking endpoint…"; col = ui::Palette::toU32(ui::palette.textDim); }
        else if (pm.ok) { msg = "endpoint reachable"; col = ui::Palette::toU32(ui::palette.statusOk); }
        else { msg = "endpoint unreachable — start your local model server"; col = ui::Palette::toU32(ui::palette.statusErr); }
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }

    };

    auto drawGeneration = [&]() {
    // ---- Generation: reasoning effort + token budgets ----
    // Labels share the row with their widgets so scroll never orphans an InputInt.
    {
        const float labelW = 180.0f * S;
        const float fieldW = 160.0f * S;
        auto labeledField = [&](const char* label) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(label);
            ImGui::SameLine(labelW);
            ImGui::SetNextItemWidth(fieldW);
        };

        const char* effLabels[8] = {};
        const char* effValues[8] = {};
        int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
        if (nEff > 0) {
            labeledField("Reasoning effort");
            int effIdx = 0;
            for (int i = 0; i < nEff; ++i)
                if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
            if (ImGui::Combo("##effort", &effIdx, effLabels, nEff)) {
                std::strncpy(s.reasoningEffort, effValues[effIdx], sizeof(s.reasoningEffort) - 1);
                changed = true;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("How much hidden reasoning the model spends before answering.\n"
                                  "Higher = better planning on hard parts, slower + more tokens.");
            ImGui::Dummy(ImVec2(0, 4 * S));
        }

        // Max response tokens (0 = auto). Keep label on the same row as the InputInt so a scroll
        // boundary can never leave a bare "0" field without its caption (Settings AI tab bug).
        labeledField("Max response tokens");
        int mt = s.maxTokens;  // 0 = auto
        if (ImGui::InputInt("##maxtokens", &mt, 1024, 4096)) {
            if (mt < 0) mt = 0;
            if (mt > 0 && mt < 512) mt = 512;
            if (mt > 200000) mt = 200000;
            s.maxTokens = mt;
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Output token cap per model call. 0 = automatic.\n"
                              "Raise this if the agent reports \"budget exhausted\" or \"no JSON ops\"\n"
                              "— a reasoning model spent its budget thinking before emitting the build.");
        ImGui::SameLine(0.0f, 8.0f * S);
        ImGui::AlignTextToFramePadding();
        if (mt == 0) dimText("0 = automatic");

        ImGui::Dummy(ImVec2(0, 4 * S));
        labeledField("Model context window");
        int cw = s.contextWindow;  // 0 = auto
        if (ImGui::InputInt("##ctxwindow", &cw, 1024, 8192)) {
            if (cw < 0) cw = 0;
            if (cw > 0 && cw < 2048) cw = 2048;
            if (cw > 2000000) cw = 2000000;
            s.contextWindow = cw;
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("The model's total token context window. 0 = automatic\n"
                              "(cloud large; local 8192). Set your LOCAL server's real window so the\n"
                              "agent trims the prompt to fit instead of overrunning it and breaking the run.");
        ImGui::SameLine(0.0f, 8.0f * S);
        ImGui::AlignTextToFramePadding();
        if (cw == 0) dimText("0 = automatic");
    }

    };

    auto drawManageModels = [&]() {
        dimText("Uncheck a model to hide it from the composer's model dropdown. Only available "
                "models (keyed cloud / loaded local) appear there; this just trims that list.");
        ImGui::Dummy(ImVec2(0, 2 * S));
        const auto& pal2 = ui::palette;
        int rowuid = 0;
        // One toggle row: [x] Label .................... status
        auto visRow = [&](const std::string& id, const std::string& label,
                          const char* status, ImU32 statusCol) {
            ImGui::PushID(rowuid++);
            bool show = (s.hiddenModels.count(id) == 0);
            if (ImGui::Checkbox("##vis", &show)) {
                if (show) s.hiddenModels.erase(id);
                else      s.hiddenModels.insert(id);
                changed = true;
            }
            ImGui::SameLine();
            ImGui::TextUnformatted(label.c_str());
            if (status && status[0]) {
                float tw = ImGui::CalcTextSize(status).x;
                ImGui::SameLine(ImGui::GetContentRegionMax().x - tw);
                ImGui::PushStyleColor(ImGuiCol_Text, statusCol);
                ImGui::TextUnformatted(status);
                ImGui::PopStyleColor();
            }
            ImGui::PopID();
        };
        const ImU32 okCol = ui::Palette::toU32(pal2.statusOk);
        const ImU32 dimCol = ui::Palette::toU32(pal2.textDim);
        const ImU32 warnCol = ui::Palette::toU32(pal2.statusWarn);
        dimText("Each provider is a category: the checkbox bulk-toggles the whole provider on/off; "
                "expand it to hide individual models. Off = hidden from the composer's model picker.");
        ImGui::Dummy(ImVec2(0, 2 * S));

        // One collapsible category header: [x] bulk-on/off checkbox + an expander showing the model
        // count. Returns whether the body (the model rows) should be drawn.
        auto catHeader = [&](const std::string& key, int n) -> bool {
            bool enabled = st.settings.disabledProviders.count(key) == 0;
            ImGui::PushID(("cat_" + key).c_str());
            if (ImGui::Checkbox("##en", &enabled)) {
                if (enabled) st.settings.disabledProviders.erase(key);
                else         st.settings.disabledProviders.insert(key);
                changed = true;
            }
            ImGui::PopID();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(enabled ? "Turn this whole provider OFF (hide all its models)"
                                          : "Turn this provider back ON");
            ImGui::SameLine();
            char hdr[128];
            std::snprintf(hdr, sizeof(hdr), "%s  (%d)%s###hdr_%s", ui::categoryLabel(key), n,
                          enabled ? "" : "   - off", key.c_str());
            return ImGui::CollapsingHeader(hdr);
        };
        auto catEnabled = [&](const std::string& key) {
            return st.settings.disabledProviders.count(key) == 0;
        };

        // Built-in cloud providers (Gemini / Anthropic / OpenAI / xAI / Cursor).
        for (int pv : {ui::PROV_GEMINI, ui::PROV_ANTHROPIC, ui::PROV_OPENAI, ui::PROV_XAI,
                       ui::PROV_CURSOR}) {
            std::string key = ui::providerCatKey(pv);
            int n = 0;
            for (const auto& m : ui::builtinModels()) if (m.provider == pv) ++n;
            if (catHeader(key, n)) {
                bool en = catEnabled(key);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                bool keyed = providerUsable(st, pv);
                for (const auto& m : ui::builtinModels()) {
                    if (m.provider != pv) continue;
                    visRow(m.id, m.label, keyed ? "available" : "needs key", keyed ? okCol : warnCol);
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Cloud services as their own categories. OpenRouter is discovered LIVE so its category
        // lists every currently-free model (plus curated paid presets); Alibaba/DeepSeek use presets.
        for (const char* svc : {"openrouter", "alibaba", "deepseek"}) {
            std::vector<OpenRouterModel> rows;
            if (std::strcmp(svc, "openrouter") == 0) {
                rows = openRouterModels(st);
            } else {
                for (const auto& p : ui::cloudPresets())
                    if (std::strcmp(ui::cloudServiceKey(p.baseUrl), svc) == 0)
                        rows.push_back({p.id, p.label, p.vision, false});
            }
            if (rows.empty()) continue;
            if (catHeader(svc, (int)rows.size())) {
                bool en = catEnabled(svc);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (std::strcmp(svc, "openrouter") == 0)
                    dimText("Free models discovered live from OpenRouter (cost $0; still need a key "
                            "to run). Paid presets are listed too.");
                for (const auto& r : rows)
                    visRow(r.id, r.label, r.free ? "free" : (r.vision ? "vision" : "paid"),
                           r.free ? okCol : dimCol);
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Local discovered models (deduped across the local provider pair).
        {
            const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
            std::vector<std::string> ids;
            std::set<std::string> seen;
            for (const auto& id : lpm.models) if (seen.insert(id).second) ids.push_back(id);
            if (catHeader("local", (int)ids.size())) {
                bool en = catEnabled("local");
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (ids.empty()) {
                    dimText("No local models discovered - start your local model server.");
                } else {
                    for (const auto& id : ids) {
                        bool loaded = localModelLoaded(lpm, id);
                        visRow(id, id,
                               lpm.loadedKnown ? (loaded ? "loaded" : "not loaded") : "available",
                               loaded ? okCol : dimCol);
                    }
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
    };

    auto drawPrivacy = [&]() {
    // ---- privacy / offline ----
    // Keep labels short enough for narrow columns / high uiScale (CalcTextSize fit).
    if (ImGui::Checkbox("Offline mode (local only)", &s.offlineMode))
        changed = true;
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Refuses any cloud provider (Gemini, Anthropic, OpenAI, xAI, OpenRouter).\n"
                          "Only a local loopback endpoint is allowed. Enforced before every run.");
    // Session-local (not persisted): matches the Copilot overlay Permissions tab.
    ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show the AI's reasoning steps inline in the conversation.");
    dimText("Telemetry is not collected \xe2\x80\x94 Billet does not phone home.");

    };

    switch (section) {
    case AiSettingsSection::ProviderModel:
        drawProviderModel();
        break;
    case AiSettingsSection::Generation:
        drawGeneration();
        break;
    case AiSettingsSection::Privacy:
        drawPrivacy();
        break;
    case AiSettingsSection::ManageModels:
        drawManageModels();
        break;
    case AiSettingsSection::All:
        drawProviderModel();
        ImGui::Dummy(ImVec2(0, 8 * S));
        ImGui::SeparatorText("Generation");
        drawGeneration();
        ImGui::Dummy(ImVec2(0, 6 * S));
        if (ImGui::CollapsingHeader("Manage models (chat picker)"))
            drawManageModels();
        ImGui::Dummy(ImVec2(0, 6 * S));
        ImGui::SeparatorText("Privacy");
        drawPrivacy();
        break;
    }
    return changed;
}

// Full form: Provider + Generation + (Manage models unless slim) + Privacy.
bool drawProviderModelPicker(AppState& st, bool busy, bool slim) {
    if (slim) {
        // Compact: provider/model/auth + generation + privacy (no manage-models dump).
        bool changed = false;
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::ProviderModel);
        ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
        ImGui::SeparatorText("Generation");
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::Generation);
        ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
        ImGui::SeparatorText("Privacy");
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::Privacy);
        return changed;
    }
    return drawAiSettingsSection(st, busy, AiSettingsSection::All);
}

// AI-settings OVERLAY body — fills the Copilot chat area when the header gear is
// toggled (same in-column swap as Sessions). Wrapping top tabs + Interwebz glass
// groups for Agents / Prompts / Skills / Permissions / Providers.
// Esc / gear / "< Chat" style back returns to the conversation.
namespace {
// Forward decl — definition lives with the subscription usage strip below.
static void drawCopilotUsageBox(AppState& st);

// ---- Overlay roles (ai_roles_get/set + model_rankings_get for Auto badge) ----
struct OvRoleAssign {
    bool isAuto = true;
    std::string provider;
    std::string model;
    // Per-role reasoning effort: "auto" | "low" | "medium" | "high" (default auto).
    // Same ai_roles.<role>.thinking key the Settings modal Model-roles table writes.
    std::string thinking = "auto";
};
struct OvRolePick {
    bool isAuto = false;
    int provider = ui::PROV_LOCAL;
    std::string model;
    std::string label;
};
struct OvRolesUi {
    std::map<std::string, OvRoleAssign> roles;
    bool loaded = false;
    bool rpcMissing = false;
    bool pollInFlight = false;
    bool saveInFlight = false;
    double lastPoll = -1e9;
    std::string lastError;
};
static OvRolesUi s_ovRoles;

static const char* kOvRoleKeys[] = {
    "advisor", "task", "planning", "default", "plm", "redaction"};
static const char* kOvRoleLabels[] = {
    "Advisor", "Task", "Planning", "Default", "PLM", "Redaction"};
static constexpr int kOvRoleCount = 6;

static const char* ovRoleProviderWire(int prov) {
    switch (prov) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_LOCAL:       return "local";
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_CLOUDCOMPAT: return "openrouter";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CURSOR:      return "cursor";
        default: return "local";
    }
}
static int ovRoleProviderFromWire(const std::string& w) {
    if (w == "anthropic") return ui::PROV_ANTHROPIC;
    if (w == "openai") return ui::PROV_OPENAI;
    if (w == "local") return ui::PROV_LOCAL;
    if (w == "llamacpp") return ui::PROV_LLAMACPP;
    if (w == "gemini") return ui::PROV_GEMINI;
    if (w == "openrouter" || w == "cloud" || w == "cloudcompat") return ui::PROV_CLOUDCOMPAT;
    if (w == "xai" || w == "xai-sub") return ui::PROV_XAI;
    if (w == "cursor") return ui::PROV_CURSOR;
    return -1;
}
static bool ovProviderIsLoopback(int prov) {
    return prov == ui::PROV_LOCAL || prov == ui::PROV_LLAMACPP;
}
static void ovEnsureDefaultRoles() {
    for (int i = 0; i < kOvRoleCount; ++i) {
        if (s_ovRoles.roles.find(kOvRoleKeys[i]) == s_ovRoles.roles.end()) {
            OvRoleAssign a;
            a.isAuto = true;
            a.thinking = "auto";
            s_ovRoles.roles[kOvRoleKeys[i]] = a;
        }
    }
}
static std::vector<OvRolePick> ovBuildRolePicks(AppState& st, bool localOnly) {
    std::vector<OvRolePick> out;
    out.push_back(OvRolePick{true, 0, "", "Auto"});
    auto add = [&](int prov, const std::string& model, const std::string& label) {
        if (localOnly && !ovProviderIsLoopback(prov)) return;
        OvRolePick e;
        e.isAuto = false;
        e.provider = prov;
        e.model = model;
        e.label = label;
        out.push_back(std::move(e));
    };
    for (const auto& m : ui::builtinModels()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s \xc2\xb7 %s",
                      ui::providerLabel(m.provider), m.label.c_str());
        add(m.provider, m.id, buf);
    }
    for (const auto& p : ui::cloudPresets()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "OpenRouter / Cloud \xc2\xb7 %s", p.label.c_str());
        add(ui::PROV_CLOUDCOMPAT, p.id, buf);
    }
    for (const auto& p : ui::localPresets()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "Local \xc2\xb7 %s", p.label.c_str());
        add(ui::PROV_LOCAL, p.id, buf);
    }
    const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
    if (lpm.ok) {
        for (const auto& id : lpm.models) {
            char buf[256];
            std::snprintf(buf, sizeof(buf), "Local \xc2\xb7 %s", id.c_str());
            add(ui::PROV_LOCAL, id, buf);
        }
    }
    const ProviderModels& lcpp = st.providerModels[ui::PROV_LLAMACPP];
    if (lcpp.ok) {
        for (const auto& id : lcpp.models) {
            char buf[256];
            std::snprintf(buf, sizeof(buf), "llama.cpp \xc2\xb7 %s", id.c_str());
            add(ui::PROV_LLAMACPP, id, buf);
        }
    }
    return out;
}
static void ovPollRoles(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_ovRoles.loaded && (now - s_ovRoles.lastPoll) < 5.0) return;
    if (s_ovRoles.pollInFlight || s_ovRoles.saveInFlight) return;
    s_ovRoles.lastPoll = now;
    s_ovRoles.pollInFlight = true;
    st.rpc.call("ai_roles_get", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovRoles.pollInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            s_ovRoles.rpcMissing = true;
            ovEnsureDefaultRoles();
            s_ovRoles.loaded = true;
            return;
        }
        s_ovRoles.rpcMissing = false;
        auto roles = r.result.value("roles", nlohmann::json::object());
        if (!roles.is_object()) roles = nlohmann::json::object();
        s_ovRoles.roles.clear();
        for (int i = 0; i < kOvRoleCount; ++i) {
            OvRoleAssign a;
            a.isAuto = true;
            a.thinking = "auto";
            auto it = roles.find(kOvRoleKeys[i]);
            if (it != roles.end()) {
                if (it->is_string()) {
                    std::string v = it->get<std::string>();
                    a.isAuto = (v == "auto" || v.empty());
                } else if (it->is_object()) {
                    a.provider = it->value("provider", std::string());
                    a.model = it->value("model", std::string());
                    const std::string mode = it->value("mode", std::string());
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    // thinking: auto|low|medium|high (default auto).
                    std::string th = it->value("thinking", std::string("auto"));
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
            }
            s_ovRoles.roles[kOvRoleKeys[i]] = std::move(a);
        }
        s_ovRoles.loaded = true;
    });
}
static void ovSetRole(AppState& st, const char* roleKey, const OvRoleAssign& a) {
    if (s_ovRoles.rpcMissing) return;
    s_ovRoles.roles[roleKey] = a;
    nlohmann::json p;
    nlohmann::json roles = nlohmann::json::object();
    // Always object form so thinking rides alongside model auto/fixed
    // (mirrors settings_modal.cpp setRoleAssign → ai_roles_set).
    nlohmann::json entry = nlohmann::json::object();
    if (a.isAuto) {
        entry["mode"] = "auto";
    } else {
        entry["provider"] = a.provider;
        entry["model"] = a.model;
    }
    std::string th = a.thinking.empty() ? "auto" : a.thinking;
    if (th != "low" && th != "medium" && th != "high" && th != "auto") th = "auto";
    entry["thinking"] = th;
    roles[roleKey] = std::move(entry);
    p["roles"] = roles;
    s_ovRoles.saveInFlight = true;
    st.rpc.call("ai_roles_set", p, [](const core::RpcResult& r) {
        s_ovRoles.saveInFlight = false;
        if (!r.ok) {
            s_ovRoles.lastError = r.err.empty() ? "ai_roles_set failed" : r.err;
            return;
        }
        if (r.result.is_object() && r.result.contains("roles") && r.result["roles"].is_object()) {
            const auto& roles = r.result["roles"];
            for (int i = 0; i < kOvRoleCount; ++i) {
                auto it = roles.find(kOvRoleKeys[i]);
                if (it == roles.end()) continue;
                OvRoleAssign a;
                a.thinking = "auto";
                if (it->is_string()) {
                    a.isAuto = (it->get<std::string>() == "auto");
                } else if (it->is_object()) {
                    a.provider = it->value("provider", std::string());
                    a.model = it->value("model", std::string());
                    const std::string mode = it->value("mode", std::string());
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    std::string th = it->value("thinking", std::string("auto"));
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
                s_ovRoles.roles[kOvRoleKeys[i]] = a;
            }
        }
        s_ovRoles.lastPoll = ImGui::GetTime();
    });
}

// ---- Skills (skills_list / skills_get / skills_save / skills_delete) ----
struct OvSkillRow {
    std::string name;
    std::string description;
    std::string source;  // builtin | user | project
    std::string inject;
};
struct OvSkillsUi {
    std::vector<OvSkillRow> skills;
    bool loaded = false;
    bool rpcMissing = false;
    bool listInFlight = false;
    bool bodyInFlight = false;
    bool saveInFlight = false;
    double lastList = -1e9;
    std::string lastError;
    std::string selected;
    std::string bodyName;          // name the editor buffer belongs to
    std::vector<char> body{std::vector<char>(65536, 0)};
    bool bodyDirty = false;
    bool creating = false;
    char newName[96] = {};
};
static OvSkillsUi s_ovSkills;

static const char* kSkillCreatorTemplate =
    "---\n"
    "name: my-skill\n"
    "description: One line describing what this skill teaches.\n"
    "inject: manual\n"
    "triggers:\n"
    "  keywords: []\n"
    "  tools: []\n"
    "  phases: []\n"
    "---\n"
    "\n"
    "# My Skill\n"
    "\n"
    "Teach a tool sequence. Always verify-before-done.\n"
    "\n"
    "## Steps\n"
    "\n"
    "1. ...\n"
    "2. ...\n"
    "\n"
    "## Checklist\n"
    "\n"
    "- [ ] name + description valid\n"
    "- [ ] verify-before-done called out\n";

static bool ovSkillsAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (r.result.is_object()) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason", std::string());
            if (reason == "unknown_method" || reason == "method_not_found")
                return true;
        }
    }
    return false;
}
static void ovPollSkills(AppState& st, bool force) {
    if (s_ovSkills.rpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && s_ovSkills.loaded && (now - s_ovSkills.lastList) < 4.0) return;
    if (s_ovSkills.listInFlight) return;
    s_ovSkills.lastList = now;
    s_ovSkills.listInFlight = true;
    st.rpc.call("skills_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovSkills.listInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            s_ovSkills.loaded = true;
            s_ovSkills.skills.clear();
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            s_ovSkills.lastError = r.err.empty() ? "skills_list failed" : r.err;
            s_ovSkills.loaded = true;
            return;
        }
        s_ovSkills.lastError.clear();
        s_ovSkills.skills.clear();
        auto arr = r.result.value("skills", nlohmann::json::array());
        if (arr.is_array()) {
            for (const auto& e : arr) {
                if (!e.is_object()) continue;
                OvSkillRow row;
                row.name = e.value("name", std::string());
                row.description = e.value("description", std::string());
                row.source = e.value("source", std::string("builtin"));
                row.inject = e.value("inject", std::string("manual"));
                if (!row.name.empty()) s_ovSkills.skills.push_back(std::move(row));
            }
        }
        std::sort(s_ovSkills.skills.begin(), s_ovSkills.skills.end(),
                  [](const OvSkillRow& a, const OvSkillRow& b) {
                      return a.name < b.name;
                  });
        s_ovSkills.loaded = true;
    });
}
static void ovLoadSkillBody(AppState& st, const std::string& name) {
    if (name.empty() || s_ovSkills.bodyInFlight) return;
    s_ovSkills.bodyInFlight = true;
    s_ovSkills.bodyName = name;
    s_ovSkills.bodyDirty = false;
    std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
    st.rpc.call("skills_get", nlohmann::json{{"name", name}},
                [name](const core::RpcResult& r) {
        s_ovSkills.bodyInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_get failed"))
                : r.err;
            return;
        }
        if (s_ovSkills.selected != name) return;  // selection moved on
        std::string body = r.result.value("body", std::string());
        std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
        if (body.size() >= s_ovSkills.body.size())
            body.resize(s_ovSkills.body.size() - 1);
        std::memcpy(s_ovSkills.body.data(), body.data(), body.size());
        s_ovSkills.bodyName = name;
        s_ovSkills.bodyDirty = false;
        s_ovSkills.lastError.clear();
    });
}
static void ovSaveSkill(AppState& st) {
    if (s_ovSkills.selected.empty() || s_ovSkills.saveInFlight) return;
    s_ovSkills.saveInFlight = true;
    nlohmann::json p;
    p["name"] = s_ovSkills.selected;
    p["body"] = std::string(s_ovSkills.body.data());
    st.rpc.call("skills_save", p, [](const core::RpcResult& r) {
        s_ovSkills.saveInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_save failed"))
                : r.err;
            return;
        }
        s_ovSkills.bodyDirty = false;
        s_ovSkills.lastError.clear();
        s_ovSkills.loaded = false;  // refresh list (source may flip to user)
        s_ovSkills.lastList = -1e9;
    });
}
static void ovDeleteSkill(AppState& st, const std::string& name) {
    if (name.empty()) return;
    st.rpc.call("skills_delete", nlohmann::json{{"name", name}},
                [name](const core::RpcResult& r) {
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_delete failed"))
                : r.err;
            return;
        }
        if (s_ovSkills.selected == name) {
            s_ovSkills.selected.clear();
            s_ovSkills.bodyName.clear();
            std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
            s_ovSkills.bodyDirty = false;
        }
        s_ovSkills.loaded = false;
        s_ovSkills.lastList = -1e9;
    });
}

// Apply provider switch defaults (shared with the old single-picker overlay).
static void ovApplyProviderDefaults(Settings& s) {
    if (s.provider == ui::PROV_GEMINI && (!s.model[0] || std::strstr(s.model, "gpt") ||
                                          std::strstr(s.model, "claude")))
        std::strncpy(s.model, "gemini-2.5-pro", sizeof(s.model) - 1);
    else if (s.provider == ui::PROV_ANTHROPIC)
        std::strncpy(s.model, "claude-opus-4-8", sizeof(s.model) - 1);
    else if (s.provider == ui::PROV_OPENAI)
        std::strncpy(s.model, "gpt-5.6-sol", sizeof(s.model) - 1);
    else if (s.provider == ui::PROV_LOCAL) {
        std::strncpy(s.baseUrl, "http://localhost:1234/v1", sizeof(s.baseUrl) - 1);
        if (std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
            std::strstr(s.model, "claude"))
            std::strncpy(s.model, "gemma-4-e4b", sizeof(s.model) - 1);
        s.vision = true;
    } else if (s.provider == ui::PROV_LLAMACPP) {
        std::strncpy(s.baseUrl, "http://127.0.0.1:1234/v1", sizeof(s.baseUrl) - 1);
        if (!std::strstr(s.model, ".gguf"))
            std::strncpy(s.model, "%APPDATA%\\AI-CAD\\models\\model.gguf", sizeof(s.model) - 1);
        s.vision = false;
    } else if (s.provider == ui::PROV_CLOUDCOMPAT) {
        const auto& cp = ui::cloudPresets();
        if (!cp.empty()) {
            std::strncpy(s.model, cp[0].id.c_str(), sizeof(s.model) - 1);
            std::strncpy(s.baseUrl, cp[0].baseUrl.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = cp[0].vision;
        }
    } else if (s.provider == ui::PROV_XAI) {
        std::strncpy(s.model, "grok-4.5", sizeof(s.model) - 1);
        s.vision = true;
    } else if (s.provider == ui::PROV_CURSOR) {
        std::strncpy(s.model, "composer-2.5", sizeof(s.model) - 1);
        s.vision = true;
    }
}

static void drawOvCorporateRolesBox(float S);

static void drawOvAgentsTab(AppState& st, float S, bool& changed) {
    Settings& s = st.settings;
    ovPollRoles(st, /*force=*/false);
    ovEnsureDefaultRoles();
    refreshModelAvailability(st, false);

    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - 8.0f * S);
    ui::InterwebzTwoColumnBegin(12.0f);
    if (ui::InterwebzGroupBegin("##ov_roles", "Role models", ImVec2(0, colH))) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
        ImGui::TextWrapped("Per-role model + thinking. Auto model from rankings; "
                           "Thinking Auto = classifier/dispatcher.");
        ImGui::PopStyleColor();
        if (s_ovRoles.rpcMissing) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
            ImGui::TextWrapped("ai_roles_* unavailable \xe2\x80\x94 defaults (read-only).");
            ImGui::PopStyleColor();
        }
        if (!s_ovRoles.lastError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_ovRoles.lastError.c_str());
            ImGui::PopStyleColor();
        }

        const auto allPicks = ovBuildRolePicks(st, /*localOnly=*/false);
        const auto localPicks = ovBuildRolePicks(st, /*localOnly=*/true);
        // Stable C-string pointers for InterwebzCombo (labels live in picks vectors).
        std::vector<const char*> allLabels, localLabels;
        allLabels.reserve(allPicks.size());
        localLabels.reserve(localPicks.size());
        for (const auto& p : allPicks) allLabels.push_back(p.label.c_str());
        for (const auto& p : localPicks) localLabels.push_back(p.label.c_str());

        for (int i = 0; i < kOvRoleCount; ++i) {
            const char* key = kOvRoleKeys[i];
            const bool isRedaction = (std::strcmp(key, "redaction") == 0);
            OvRoleAssign& a = s_ovRoles.roles[key];
            const auto& picks = isRedaction ? localPicks : allPicks;
            const auto& labels = isRedaction ? localLabels : allLabels;

            ImGui::PushID(key);
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(kOvRoleLabels[i]);
            if (a.isAuto) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
                ImGui::TextUnformatted("auto");
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Auto-picked from model rankings.");
            }
            if (isRedaction) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
                ImGui::TextUnformatted("[local]");
                ImGui::PopStyleColor();
            }

            int sel = 0;
            if (!a.isAuto) {
                const int wantProv = ovRoleProviderFromWire(a.provider);
                for (int pi = 1; pi < (int)picks.size(); ++pi) {
                    if (picks[pi].model == a.model &&
                        (wantProv < 0 || picks[pi].provider == wantProv ||
                         ovRoleProviderWire(picks[pi].provider) == a.provider)) {
                        sel = pi;
                        break;
                    }
                }
            }
            const bool ro = s_ovRoles.rpcMissing || s_ovRoles.saveInFlight;
            if (ro) ImGui::BeginDisabled();
            int comboSel = sel;
            if (ui::InterwebzCombo("##role_model", &comboSel, labels.data(), (int)labels.size()) &&
                comboSel >= 0 && comboSel < (int)picks.size()) {
                OvRoleAssign next = a;  // preserve thinking
                if (picks[comboSel].isAuto) {
                    next.isAuto = true;
                    next.provider.clear();
                    next.model.clear();
                } else {
                    next.isAuto = false;
                    next.provider = ovRoleProviderWire(picks[comboSel].provider);
                    next.model = picks[comboSel].model;
                }
                if (isRedaction && !next.isAuto &&
                    !ovProviderIsLoopback(picks[comboSel].provider)) {
                    s_ovRoles.lastError = "Redaction role refuses non-loopback providers.";
                } else {
                    ovSetRole(st, key, next);
                }
            }
            // Thinking effort: Auto / Low / Medium / High (same ai_roles.<role>.thinking).
            static const char* kThinkLabels[] = {"Auto", "Low", "Medium", "High"};
            static const char* kThinkValues[] = {"auto", "low", "medium", "high"};
            int thinkSel = 0;
            for (int ti = 0; ti < 4; ++ti) {
                if (a.thinking == kThinkValues[ti]) { thinkSel = ti; break; }
            }
            ImGui::SetNextItemWidth(ImMax(72.0f * S, ImGui::GetContentRegionAvail().x * 0.42f));
            if (ui::InterwebzCombo("Thinking", &thinkSel, kThinkLabels, 4) &&
                thinkSel >= 0 && thinkSel < 4) {
                OvRoleAssign next = a;
                next.thinking = kThinkValues[thinkSel];
                ovSetRole(st, key, next);
            }
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                if (std::strcmp(key, "default") == 0) {
                    ImGui::SetTooltip(
                        "Auto: chat classifier assigns effort per prompt.\n"
                        "Low/Medium/High: force that level for Default-role calls.");
                } else {
                    ImGui::SetTooltip(
                        "Auto: dispatcher assigns effort by task complexity.\n"
                        "Low/Medium/High: force that level for this role's calls.");
                }
            }
            if (ro) ImGui::EndDisabled();
            ImGui::PopID();
            ImGui::Dummy(ImVec2(0, 1.0f * S));
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##ov_default_prov", "Default provider", ImVec2(0, colH))) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
        ImGui::TextWrapped("Composer default. Role rows above override per sub-agent.");
        ImGui::PopStyleColor();

        const char* provs[ui::PROV_COUNT];
        for (int i = 0; i < ui::PROV_COUNT; ++i) provs[i] = ui::providerLabel(i);
        int prevProvider = s.provider;
        if (ui::InterwebzCombo("Provider", &s.provider, provs, ui::PROV_COUNT) &&
            s.provider != prevProvider) {
            changed = true;
            ovApplyProviderDefaults(s);
        }

        // Model picker for the default provider (kept reachable).
        const bool cloud = ui::providerIsCloud(s.provider);
        if (s.provider == ui::PROV_CLOUDCOMPAT) {
            const auto& cps = ui::cloudPresets();
            std::vector<const char*> plabels;
            plabels.push_back("(choose a preset\xe2\x80\xa6)");
            for (const auto& p : cps) plabels.push_back(p.label.c_str());
            int psel = 0;
            for (int i = 0; i < (int)cps.size(); ++i)
                if (s.model[0] && cps[i].id == s.model) { psel = i + 1; break; }
            if (ui::InterwebzCombo("Preset", &psel, plabels.data(), (int)plabels.size()) &&
                psel > 0 && psel - 1 < (int)cps.size()) {
                const auto& p = cps[psel - 1];
                std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
                std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = p.vision;
                changed = true;
            }
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputTextWithHint("##ov_cloudmodel", "model id", s.model, sizeof(s.model)))
                changed = true;
        } else if (cloud) {
            std::vector<const char*> ids, labels;
            for (const auto& m : ui::builtinModels())
                if (m.provider == s.provider) {
                    ids.push_back(m.id.c_str());
                    labels.push_back(m.label.c_str());
                }
            int cur = -1;
            for (size_t i = 0; i < ids.size(); ++i)
                if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
            std::string customLabel;
            if (cur < 0) {
                customLabel = std::string(s.model[0] ? s.model : "(choose a model)") + "  (custom)";
                labels.push_back(customLabel.c_str());
                cur = (int)labels.size() - 1;
            }
            if (ui::InterwebzCombo("Model", &cur, labels.data(), (int)labels.size())) {
                if (cur >= 0 && cur < (int)ids.size()) {
                    std::strncpy(s.model, ids[cur], sizeof(s.model) - 1);
                    changed = true;
                }
            }
        } else {
            const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                              ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
            if (s.provider != ui::PROV_LLAMACPP && pm.ok && !pm.models.empty()) {
                std::vector<const char*> ids;
                for (const auto& id : pm.models) ids.push_back(id.c_str());
                int cur = -1;
                for (size_t i = 0; i < ids.size(); ++i)
                    if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
                std::string customLabel;
                if (cur < 0) {
                    customLabel = std::string(s.model[0] ? s.model : "(select)") + "  (custom)";
                    ids.push_back(customLabel.c_str());
                    cur = (int)ids.size() - 1;
                }
                if (ui::InterwebzCombo("Model", &cur, ids.data(), (int)ids.size())) {
                    if (cur >= 0 && (size_t)cur < pm.models.size()) {
                        std::strncpy(s.model, pm.models[cur].c_str(), sizeof(s.model) - 1);
                        changed = true;
                    }
                }
            } else {
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##ov_localmodel", s.model, sizeof(s.model))) changed = true;
            }
        }

        {
            const char* effLabels[8] = {};
            const char* effValues[8] = {};
            int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
            if (nEff > 0) {
                int effIdx = 0;
                for (int i = 0; i < nEff; ++i)
                    if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
                if (ui::InterwebzCombo("Reasoning", &effIdx, effLabels, nEff)) {
                    std::strncpy(s.reasoningEffort, effValues[effIdx],
                                 sizeof(s.reasoningEffort) - 1);
                    changed = true;
                }
            }
        }

        // Advanced: token/window minutiae (auto) collapsed.
        if (ImGui::CollapsingHeader("Advanced")) {
            int mt = s.maxTokens;
            if (ui::InterwebzSliderInt("Max tokens", &mt, 0, 128000, "%d")) {
                if (mt < 0) mt = 0;
                if (mt > 0 && mt < 512) mt = 512;
                s.maxTokens = mt;
                changed = true;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("0 = automatic output token cap.");
            int cw = s.contextWindow;
            if (ui::InterwebzSliderInt("Context window", &cw, 0, 1000000, "%d")) {
                if (cw < 0) cw = 0;
                if (cw > 0 && cw < 2048) cw = 2048;
                s.contextWindow = cw;
                changed = true;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("0 = automatic context window.");
            if (s.provider == ui::PROV_CLOUDCOMPAT || s.provider == ui::PROV_LOCAL ||
                s.provider == ui::PROV_LLAMACPP) {
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("Base URL##ov_base", s.baseUrl, sizeof(s.baseUrl))) {
                    changed = true;
                    if (s.provider != ui::PROV_CLOUDCOMPAT) refreshModelAvailability(st, true);
                }
            }
            if (cloud || s.provider == ui::PROV_LOCAL) {
                char* key = providerKeyBuf(s, s.provider);
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText(s.provider == ui::PROV_LOCAL ? "API key (optional)##ovk"
                                                                 : "API key##ovk",
                                     key, 256, ImGuiInputTextFlags_Password))
                    changed = true;
            }
        }

        if (ImGui::Button("All settings\xe2\x80\xa6", ImVec2(-1, 0))) {
            st.openSettings = true;
            st.openSettingsTab = 2;
            st.showAISettingsOverlay = false;
        }
        if (ImGui::Button("Repair / install runtime", ImVec2(-1, 0)) &&
            !st.setupRunning.load() && !st.job.running.load()) {
            st.startSetup(true);
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();

    // Corporate Roles — opt-in org structure for copilot subagents (below Role models).
    // Persists agent.corporate_mode + agent.corporate via the same config.json path as
    // Permissions' agent.max_subagent_depth (load-modify-write, no RPC).
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1.0f)));
    drawOvCorporateRolesBox(S);
}

// ---- Corporate Roles (agent.corporate_mode + agent.corporate) -----------------
struct OvCorporateCfg {
    bool mode = false;
    int maxDepartments = 3;
    bool teamLeads = true;
    int meetingCadence = 5;
    bool directorReview = true;
};

static void ovClampCorporate(OvCorporateCfg& c) {
    if (c.maxDepartments < 1) c.maxDepartments = 1;
    if (c.maxDepartments > 6) c.maxDepartments = 6;
    if (c.meetingCadence < 1) c.meetingCadence = 1;
    if (c.meetingCadence > 20) c.meetingCadence = 20;
}

static OvCorporateCfg ovLoadCorporate() {
    // agent.corporate_mode (bool, default false) + agent.corporate object:
    // max_departments (1-6, def 3), team_leads (bool, def true),
    // meeting_cadence (1-20 completions, def 5), director_review (bool, def true).
    OvCorporateCfg c;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object()) {
                    if (agent.contains("corporate_mode") && agent["corporate_mode"].is_boolean())
                        c.mode = agent["corporate_mode"].get<bool>();
                    auto corp = agent.value("corporate", nlohmann::json::object());
                    if (corp.is_object()) {
                        if (corp.contains("max_departments") &&
                            corp["max_departments"].is_number_integer())
                            c.maxDepartments = corp["max_departments"].get<int>();
                        if (corp.contains("team_leads") && corp["team_leads"].is_boolean())
                            c.teamLeads = corp["team_leads"].get<bool>();
                        if (corp.contains("meeting_cadence") &&
                            corp["meeting_cadence"].is_number_integer())
                            c.meetingCadence = corp["meeting_cadence"].get<int>();
                        if (corp.contains("director_review") &&
                            corp["director_review"].is_boolean())
                            c.directorReview = corp["director_review"].get<bool>();
                    }
                }
            }
        }
    } catch (...) {}
    ovClampCorporate(c);
    return c;
}

static void ovSaveCorporate(const OvCorporateCfg& cfg) {
    OvCorporateCfg c = cfg;
    ovClampCorporate(c);
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["corporate_mode"] = c.mode;
        nlohmann::json corp = nlohmann::json::object();
        if (j["agent"].contains("corporate") && j["agent"]["corporate"].is_object())
            corp = j["agent"]["corporate"];
        corp["max_departments"] = c.maxDepartments;
        corp["team_leads"] = c.teamLeads;
        corp["meeting_cadence"] = c.meetingCadence;
        corp["director_review"] = c.directorReview;
        j["agent"]["corporate"] = std::move(corp);
        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::filesystem::rename(tmp, path);
    } catch (...) {}
}

static void drawOvCorporateRolesBox(float S) {
    static bool s_loaded = false;
    static OvCorporateCfg s_corp;
    if (!s_loaded) {
        s_corp = ovLoadCorporate();
        s_loaded = true;
    }

    // Content-height glass box (AutoResizeY so the box hugs content — no empty
    // glass / nested scrollbar growth). Title row matches InterwebzGroupBegin
    // and carries the on/off toggle bound to agent.corporate_mode.
    {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                   ui::palette.accent[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Corporate Roles");
        ImGui::PopStyleColor();
        {
            const ImVec2 p0 = ImGui::GetItemRectMin();
            const ImVec2 p1 = ImGui::GetItemRectMax();
            ImGui::GetWindowDrawList()->AddLine(
                ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
                ImGui::GetColorU32(ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                                          ui::palette.accent[2], 0.60f)),
                1.0f * S);
        }
        ImGui::SameLine();
        bool mode = s_corp.mode;
        if (ui::InterwebzCheckbox("On", &mode)) {
            s_corp.mode = mode;
            ovSaveCorporate(s_corp);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Opt-in company structure for copilot subagents.\n"
                "Persists as agent.corporate_mode (default off).");
        ImGui::Dummy(ImVec2(0, 4.0f * S));
    }

    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14.0f * S, 12.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8.0f * S, 8.0f * S));
    if (ImGui::BeginChild("##ov_corporate", ImVec2(0, 0),
                          ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY,
                          ImGuiWindowFlags_NoScrollbar)) {
        if (s_corp.mode) {
            ImGui::SetNextItemWidth(ImMax(120.0f * S, ImGui::GetContentRegionAvail().x * 0.85f));
            int depts = s_corp.maxDepartments;
            if (ui::InterwebzSliderInt("Max departments", &depts, 1, 6, "%d")) {
                s_corp.maxDepartments = depts;
                ovClampCorporate(s_corp);
                ovSaveCorporate(s_corp);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "How many departments the director may open (1-6).\n"
                    "Persists as agent.corporate.max_departments (default 3).");

            bool leads = s_corp.teamLeads;
            if (ui::InterwebzCheckbox("Team leads", &leads)) {
                s_corp.teamLeads = leads;
                ovSaveCorporate(s_corp);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Department heads may appoint a team lead for intra-dept sync.\n"
                    "Persists as agent.corporate.team_leads (default on).");

            ImGui::SetNextItemWidth(ImMax(120.0f * S, ImGui::GetContentRegionAvail().x * 0.85f));
            int cadence = s_corp.meetingCadence;
            if (ui::InterwebzSliderInt("Meeting cadence", &cadence, 1, 20, "%d")) {
                s_corp.meetingCadence = cadence;
                ovClampCorporate(s_corp);
                ovSaveCorporate(s_corp);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Worker task-completions between head status meetings (1-20).\n"
                    "Persists as agent.corporate.meeting_cadence (default 5).");

            bool review = s_corp.directorReview;
            if (ui::InterwebzCheckbox("Director review", &review)) {
                s_corp.directorReview = review;
                ovSaveCorporate(s_corp);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Director reviews department alignment after head meetings.\n"
                    "Persists as agent.corporate.director_review (default on).");
        }

        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                   ui::palette.textDim[2], 1));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextUnformatted(
            "Departments with heads that hire their own worker subagents; heads meet to "
            "sync status; a director oversees alignment. Workers never talk to the "
            "director directly.");
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImGui::PopStyleVar(2);
    ui::popGlassChildStyle();
}

static void drawOvPromptsTab(AppState& st, float S, bool& changed) {
    Settings& s = st.settings;
    // Keep spBuf in sync when empty buffer should reflect persisted prompt.
    if (st.spBuf[0] == 0 && !s.systemPrompt.empty())
        std::strncpy(st.spBuf.data(), s.systemPrompt.c_str(), st.spBuf.size() - 1);

    const float colH = ImMax(200.0f * S, ImGui::GetContentRegionAvail().y - 8.0f * S);
    ui::InterwebzTwoColumnBegin(12.0f);
    if (ui::InterwebzGroupBegin("##ov_prompt_ed", "System prompt override", ImVec2(0, colH))) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
        ImGui::TextWrapped("Empty = built-in. Save writes to config and the next agent turn.");
        ImGui::PopStyleColor();
        const float edH = ImMax(120.0f * S, ImGui::GetContentRegionAvail().y - 48.0f * S);
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        if (ImGui::InputTextMultiline("##ov_sysprompt", st.spBuf.data(), st.spBuf.size(),
                                      ImVec2(-1, edH))) {
            // live buffer; persisted on Save
        }
        if (st.fonts.mono) ImGui::PopFont();
        if (ImGui::Button("Save override", ImVec2(-1, 0))) {
            s.systemPrompt = st.spBuf.data();
            changed = true;
        }
        if (ImGui::Button("Clear (use built-in)", ImVec2(-1, 0))) {
            std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
            s.systemPrompt.clear();
            changed = true;
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##ov_prompt_info", "About", ImVec2(0, colH))) {
        ui::InterwebzKeyValueRow("Status",
            st.spBuf[0] ? "custom override" : "built-in default");
        ui::InterwebzKeyValueRow("Persisted",
            s.systemPrompt.empty() ? "no" : "yes");
        ImGui::Dummy(ImVec2(0, 6.0f * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
        ImGui::TextWrapped("Role-specific prompts live in Settings \xe2\x86\x92 Prompts. "
                           "This overlay edits the global system override only.");
        ImGui::PopStyleColor();
        if (ImGui::Button("Open Prompts tab\xe2\x80\xa6", ImVec2(-1, 0))) {
            st.openSettings = true;
            st.openSettingsTab = 4;
            st.showAISettingsOverlay = false;
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

static void drawOvSkillsTab(AppState& st, float S) {
    ovPollSkills(st, /*force=*/false);

    if (s_ovSkills.rpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("skills_* RPC not available \xe2\x80\x94 skill editor hidden.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_ovSkills.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_ovSkills.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const float colH = ImMax(220.0f * S, ImGui::GetContentRegionAvail().y - 8.0f * S);
    ui::InterwebzTwoColumnBegin(12.0f);
    if (ui::InterwebzGroupBegin("##ov_skill_list", "Catalog", ImVec2(0, colH))) {
        if (ImGui::Button("New skill", ImVec2(-1, 0))) {
            s_ovSkills.creating = true;
            s_ovSkills.selected.clear();
            std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
            std::strncpy(s_ovSkills.body.data(), kSkillCreatorTemplate,
                         s_ovSkills.body.size() - 1);
            s_ovSkills.bodyName.clear();
            s_ovSkills.bodyDirty = true;
            std::memset(s_ovSkills.newName, 0, sizeof(s_ovSkills.newName));
            std::strncpy(s_ovSkills.newName, "my-skill", sizeof(s_ovSkills.newName) - 1);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Create a user skill from the skill-creator template.");
        ImGui::Dummy(ImVec2(0, 4.0f * S));

        if (!s_ovSkills.loaded && s_ovSkills.listInFlight) {
            ImGui::TextUnformatted("Loading skills\xe2\x80\xa6");
        } else if (s_ovSkills.skills.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("No skills discovered.");
            ImGui::PopStyleColor();
        } else {
            for (size_t i = 0; i < s_ovSkills.skills.size(); ++i) {
                const auto& sk = s_ovSkills.skills[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovSkills.creating && s_ovSkills.selected == sk.name);
                if (ImGui::Selectable("##skrow", sel, 0, ImVec2(0, 0))) {
                    s_ovSkills.creating = false;
                    s_ovSkills.selected = sk.name;
                    ovLoadSkillBody(st, sk.name);
                }
                // Paint name + source chip on the same row.
                {
                    ImVec2 rmin = ImGui::GetItemRectMin();
                    ImVec2 rmax = ImGui::GetItemRectMax();
                    // Expand row height for two lines: name + description.
                    (void)rmin; (void)rmax;
                }
                ImGui::SameLine(0, 0);
                ImGui::BeginGroup();
                ImGui::TextUnformatted(sk.name.c_str());
                ImGui::SameLine();
                {
                    const char* chip = sk.source.c_str();
                    ImU32 chipCol = ui::Palette::toU32(ui::palette.textDim);
                    if (sk.source == "user")
                        chipCol = ui::Palette::toU32(ui::palette.accent);
                    else if (sk.source == "project")
                        chipCol = ui::Palette::toU32(ui::palette.statusOk);
                    ImGui::PushStyleColor(ImGuiCol_Text, chipCol);
                    ImGui::Text("[%s]", chip);
                    ImGui::PopStyleColor();
                }
                if (!sk.description.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("%s", sk.description.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::EndGroup();
                // Make the group also selectable.
                if (ImGui::IsItemClicked()) {
                    s_ovSkills.creating = false;
                    s_ovSkills.selected = sk.name;
                    ovLoadSkillBody(st, sk.name);
                }
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 2.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##ov_skill_ed", "Editor", ImVec2(0, colH))) {
        if (s_ovSkills.creating) {
            ImGui::TextUnformatted("Name");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##ov_newskill", s_ovSkills.newName, sizeof(s_ovSkills.newName));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Saved under the user skills dir via skills_save.");
            ImGui::PopStyleColor();
        } else if (s_ovSkills.selected.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Select a skill to edit, or New skill.");
            ImGui::PopStyleColor();
        } else {
            ui::InterwebzKeyValueRow("Skill", s_ovSkills.selected.c_str());
            // Source chip for selected.
            const char* src = "builtin";
            for (const auto& sk : s_ovSkills.skills)
                if (sk.name == s_ovSkills.selected) { src = sk.source.c_str(); break; }
            ui::InterwebzKeyValueRow("Source", src);
        }

        if (s_ovSkills.creating || !s_ovSkills.selected.empty()) {
            if (s_ovSkills.bodyInFlight && !s_ovSkills.creating) {
                ImGui::TextUnformatted("Loading body\xe2\x80\xa6");
            } else {
                const float edH = ImMax(100.0f * S,
                    ImGui::GetContentRegionAvail().y - 56.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##ov_skillbody", s_ovSkills.body.data(),
                                              s_ovSkills.body.size(), ImVec2(-1, edH)))
                    s_ovSkills.bodyDirty = true;
                if (st.fonts.mono) ImGui::PopFont();
            }

            const bool busySave = s_ovSkills.saveInFlight;
            if (busySave) ImGui::BeginDisabled();
            if (ImGui::Button(s_ovSkills.creating ? "Create##sksave" : "Save##sksave",
                              ImVec2(-1, 0))) {
                if (s_ovSkills.creating) {
                    // Normalize name from field; rewrite frontmatter name if needed.
                    std::string name = s_ovSkills.newName;
                    // strip spaces
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) { return std::isspace(c); }), name.end());
                    if (name.empty()) name = "my-skill";
                    s_ovSkills.selected = name;
                    s_ovSkills.creating = false;
                    ovSaveSkill(st);
                } else {
                    ovSaveSkill(st);
                }
            }
            if (busySave) ImGui::EndDisabled();

            // Delete only for user skills.
            if (!s_ovSkills.creating && !s_ovSkills.selected.empty()) {
                bool isUser = false;
                for (const auto& sk : s_ovSkills.skills)
                    if (sk.name == s_ovSkills.selected && sk.source == "user") {
                        isUser = true; break;
                    }
                if (isUser) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                               ui::palette.statusErr[2], 0.55f));
                    if (ImGui::Button("Delete user skill", ImVec2(-1, 0)))
                        ovDeleteSkill(st, s_ovSkills.selected);
                    ImGui::PopStyleColor();
                }
            }
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

static int ovLoadMaxSubagentDepth() {
    // agent.max_subagent_depth — nesting levels of subagent-spawning-subagent
    // (not concurrency). Root chat = depth 0. Default 2, GUI clamp 0-4.
    // Enforced by AgentCoord (delegation.py / mission_dispatch.py).
    int depth = 2;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains("max_subagent_depth") &&
                    agent["max_subagent_depth"].is_number_integer()) {
                    depth = agent["max_subagent_depth"].get<int>();
                }
            }
        }
    } catch (...) {}
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    return depth;
}
static void ovSaveMaxSubagentDepth(int depth) {
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["max_subagent_depth"] = depth;
        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::filesystem::rename(tmp, path);
    } catch (...) {}
}

static void drawOvPermissionsTab(AppState& st, float S, bool& changed) {
    Settings& s = st.settings;
    // Run permissions first, then compact Privacy. Single column so narrow
    // copilot width never clips InterwebzCheckbox labels.
    const float gapY = ui::metrics::U(1.0f);

    if (ui::InterwebzGroupBegin("##ov_run", "Run permissions", ImVec2(0, 0))) {
        if (ui::InterwebzCheckbox("Supervised (plan + clarify)", &s.planMode))
            changed = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Draft a build plan and ask clarify questions before modeling.");
        if (ui::InterwebzCheckbox("Vision (send screenshots)", &s.vision))
            changed = true;
        int iters = s.maxIters;
        ImGui::SetNextItemWidth(ImMax(120.0f * S, ImGui::GetContentRegionAvail().x * 0.85f));
        if (ui::InterwebzSliderInt("Refine-pass cap", &iters, 0, 32, "%d")) {
            if (iters < 0) iters = 0;
            s.maxIters = iters;
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("0 = automatic refine-pass budget.");

        // Nesting depth of subagent-spawning-subagent (not concurrency).
        static int s_maxDepth = -1;
        if (s_maxDepth < 0) s_maxDepth = ovLoadMaxSubagentDepth();
        int depth = s_maxDepth;
        ImGui::SetNextItemWidth(ImMax(120.0f * S, ImGui::GetContentRegionAvail().x * 0.85f));
        if (ui::InterwebzSliderInt("Max subagent nesting", &depth, 0, 4, "%d")) {
            if (depth < 0) depth = 0;
            if (depth > 4) depth = 4;
            s_maxDepth = depth;
            ovSaveMaxSubagentDepth(depth);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "How deep a subagent may spawn further subagents (0-4).\n"
                "Root chat is depth 0. Not a concurrency limit.\n"
                "Persists as agent.max_subagent_depth (default 2).");

        ui::InterwebzKeyValueRow("Mode", s.planMode ? "Supervised" : "Autopilot");
        ui::InterwebzKeyValueRow("Offline", s.offlineMode ? "on" : "off");
    }
    ui::InterwebzGroupEnd();
    ImGui::Dummy(ImVec2(0, gapY));

    // Privacy is content-height only — no fixed tall box wasting empty glass.
    if (ui::InterwebzGroupBegin("##ov_privacy", "Privacy", ImVec2(0, 0))) {
        if (ui::InterwebzCheckbox("Offline mode (local only)", &s.offlineMode))
            changed = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Refuse cloud providers. Only a local loopback endpoint is allowed.");
        ui::InterwebzCheckbox("Show assistant thoughts", &gShowThinking);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Show the AI's reasoning steps inline.");
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextUnformatted("Telemetry is not collected \xe2\x80\x94 Billet does not phone home.");
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    ui::InterwebzGroupEnd();
}

// ---- Providers tab (merged Accounts: sign-in state, accounts, usage) ----
struct OvSubUsageWindow {
    std::string window;
    std::string label;
    float usedFraction = 0.f;
    bool hasFraction = false;
    std::string resetsAt;
    std::string status;
};
struct OvSubAccountRow {
    std::string provider;
    std::string accountId;
    std::string label;
    std::string displayName;
    std::string plan;
    bool active = false;
    bool connected = false;
    bool hasUsage = false;
    std::vector<OvSubUsageWindow> usage;
};
struct OvSubLoginProgress {
    bool inProgress = false;
    bool browserOpened = false;
    std::string userCode;
    std::string verifyUrl;
    std::string stage;
    std::string lastError;
};
struct OvSubAccountsUi {
    std::vector<OvSubAccountRow> rows;
    std::map<std::string, OvSubLoginProgress> login;
    double lastAccountsPoll = -1e9;
    double lastUsagePoll = -1e9;
    bool accountsPollInFlight = false;
    bool usagePollInFlight = false;
    bool usageRpcMissing = false;
    int  lastDrawnFrame = -100;
    std::string lastError;
};
static OvSubAccountsUi s_ovSub;

static const char* kOvSubProviders[] = {"xai", "claude", "codex", "cursor"};
static const char* kOvSubProviderLabels[] = {
    "xAI Grok", "Claude", "ChatGPT / Codex", "Cursor"};
static constexpr double kOvUsagePollSec = 5.0 * 60.0;
static constexpr double kOvAccountsPollSec = 1.5;

static std::string ovOptStr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
}
static void ovParseUsageWindows(const nlohmann::json& src, OvSubAccountRow& row) {
    row.usage.clear();
    row.hasUsage = false;
    auto it = src.find("usage");
    if (it == src.end() || !it->is_array()) return;
    row.hasUsage = true;
    for (const auto& u : *it) {
        if (!u.is_object()) continue;
        OvSubUsageWindow w;
        w.window = ovOptStr(u, "window");
        w.label  = ovOptStr(u, "label");
        if (w.label.empty()) w.label = w.window.empty() ? "Usage" : w.window;
        if (u.contains("used_fraction") && u["used_fraction"].is_number()) {
            w.usedFraction = (float)std::clamp(u["used_fraction"].get<double>(), 0.0, 1.0);
            w.hasFraction = true;
        }
        w.resetsAt = ovOptStr(u, "resets_at");
        w.status   = ovOptStr(u, "status");
        if (w.status.empty()) w.status = "unknown";
        row.usage.push_back(std::move(w));
    }
}
static void ovParseAccountIdentity(const nlohmann::json& a, OvSubAccountRow& row) {
    row.displayName = ovOptStr(a, "display_name");
    row.plan        = ovOptStr(a, "plan");
    row.label = ovOptStr(a, "email");
    if (row.label.empty()) row.label = ovOptStr(a, "label");
    if (!row.label.empty() && row.label == row.accountId) row.label.clear();
    if (!row.displayName.empty() && row.displayName == row.accountId) row.displayName.clear();
    ovParseUsageWindows(a, row);
}
static std::string ovShortAccountId(const std::string& id) {
    if (id.empty()) return "(account)";
    if (id.size() <= 10) return id;
    return id.substr(0, 8) + "\xe2\x80\xa6";
}
static std::string ovAccountPrimaryLabel(const OvSubAccountRow& row) {
    if (!row.displayName.empty()) return row.displayName;
    if (!row.label.empty()) return row.label;
    return ovShortAccountId(row.accountId);
}
static void ovMergeUsageRows(const nlohmann::json& rows) {
    if (!rows.is_array()) return;
    for (const auto& r : rows) {
        if (!r.is_object()) continue;
        const std::string prov = ovOptStr(r, "provider");
        const std::string aid  = ovOptStr(r, "account_id");
        for (auto& row : s_ovSub.rows) {
            if (row.provider != prov) continue;
            if (!aid.empty() && row.accountId != aid) continue;
            if (r.contains("display_name") && r["display_name"].is_string()) {
                const std::string dn = r["display_name"].get<std::string>();
                if (!dn.empty() && dn != row.accountId) row.displayName = dn;
            }
            if (r.contains("email") && r["email"].is_string()) {
                const std::string em = r["email"].get<std::string>();
                if (!em.empty() && em != row.accountId) row.label = em;
            } else if (r.contains("label") && r["label"].is_string()) {
                const std::string lb = r["label"].get<std::string>();
                if (!lb.empty() && lb != row.accountId && row.label.empty())
                    row.label = lb;
            }
            if (r.contains("plan") && r["plan"].is_string()) {
                const std::string pl = r["plan"].get<std::string>();
                if (!pl.empty()) row.plan = pl;
            }
            ovParseUsageWindows(r, row);
            if (aid.empty()) break;
        }
    }
}
static const float* ovUsageStatusColor(const std::string& status, float frac) {
    if (status == "blocked") return ui::palette.statusErr;
    if (status == "warn")    return ui::palette.statusWarn;
    if (status == "ok")      return ui::palette.statusOk;
    if (frac >= 0.95f) return ui::palette.statusErr;
    if (frac >= 0.75f) return ui::palette.statusWarn;
    return ui::palette.statusOk;
}
static void ovDrawCompactUsage(const OvSubUsageWindow& w, float sc) {
    if (!w.hasFraction) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::Text("%s \xe2\x80\x94 %s", w.label.c_str(),
                    w.status == "needs_login" ? "sign in" :
                    w.status == "blocked" ? "verify" : "n/a");
        ImGui::PopStyleColor();
        return;
    }
    const auto& pal = ui::palette;
    const float* role = ovUsageStatusColor(w.status, w.usedFraction);
    char pctBuf[32];
    std::snprintf(pctBuf, sizeof(pctBuf), "%s %.0f%%", w.label.c_str(), w.usedFraction * 100.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(role[0], role[1], role[2], 1.0f));
    ImGui::TextUnformatted(pctBuf);
    ImGui::PopStyleColor();
    const float barH = 4.0f * sc;
    const float barW = ImGui::GetContentRegionAvail().x;
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p, ImVec2(p.x + barW, p.y + barH),
                      ui::Palette::toU32(pal.accentSoft), 2.0f * sc);
    if (w.usedFraction > 0.f) {
        dl->AddRectFilled(p, ImVec2(p.x + barW * w.usedFraction, p.y + barH),
                          ImGui::ColorConvertFloat4ToU32(
                              ImVec4(role[0], role[1], role[2], 0.95f)),
                          2.0f * sc);
    }
    ImGui::Dummy(ImVec2(barW, barH + 2.0f * sc));
}
static void ovPollSubUsage(AppState& st, bool force) {
    if (s_ovSub.usageRpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_ovSub.lastUsagePoll) < kOvUsagePollSec) return;
    if (s_ovSub.usagePollInFlight) return;
    s_ovSub.lastUsagePoll = now;
    s_ovSub.usagePollInFlight = true;
    st.rpc.call("subscription_usage", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovSub.usagePollInFlight = false;
        if (!r.ok) {
            const std::string& err = r.err;
            if (err.find("unknown_method") != std::string::npos ||
                err.find("unknown method") != std::string::npos ||
                err.find("not found") != std::string::npos)
                s_ovSub.usageRpcMissing = true;
            return;
        }
        if (!r.result.is_object() || r.result.value("ok", true) == false) return;
        ovMergeUsageRows(r.result.value("rows", nlohmann::json::array()));
    });
}
static void ovPollSubAccounts(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && (now - s_ovSub.lastAccountsPoll) < kOvAccountsPollSec) return;
    if (s_ovSub.accountsPollInFlight) return;
    s_ovSub.lastAccountsPoll = now;
    s_ovSub.accountsPollInFlight = true;
    st.rpc.call("subscription_accounts", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovSub.accountsPollInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            if (!r.ok && s_ovSub.lastError.empty())
                s_ovSub.lastError = r.err.empty() ? "subscription_accounts unavailable"
                                                  : r.err;
            return;
        }
        if (r.result.value("ok", true) == false) {
            auto reason = ovOptStr(r.result, "reason");
            s_ovSub.lastError = reason.empty() ? "accounts failed" : reason;
            return;
        }
        s_ovSub.lastError.clear();
        std::map<std::string, OvSubAccountRow> prevByKey;
        for (const auto& prev : s_ovSub.rows)
            prevByKey[prev.provider + "\n" + prev.accountId] = prev;
        s_ovSub.rows.clear();
        auto arr = r.result.value("accounts", nlohmann::json::array());
        if (!arr.is_array()) return;
        for (const auto& a : arr) {
            if (!a.is_object()) continue;
            OvSubAccountRow row;
            row.provider  = ovOptStr(a, "provider");
            row.accountId = ovOptStr(a, "account_id");
            row.active    = a.value("active", false);
            row.connected = a.value("connected", true);
            ovParseAccountIdentity(a, row);
            if (!row.hasUsage) {
                auto it = prevByKey.find(row.provider + "\n" + row.accountId);
                if (it != prevByKey.end() && it->second.hasUsage) {
                    row.hasUsage = true;
                    row.usage = it->second.usage;
                    if (row.displayName.empty()) row.displayName = it->second.displayName;
                    if (row.plan.empty())        row.plan = it->second.plan;
                }
            }
            s_ovSub.rows.push_back(std::move(row));
        }
    });
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        if (!r.ok || !r.result.is_object()) return;
        auto providers = r.result.value("providers", nlohmann::json::object());
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            OvSubLoginProgress& p = s_ovSub.login[it.key()];
            const auto& x = it.value();
            const bool wasInProgress = p.inProgress;
            p.inProgress = x.value("login_in_progress", false);
            p.browserOpened = x.value("browser_opened", false);
            p.userCode = ovOptStr(x, "user_code");
            p.verifyUrl = ovOptStr(x, "verification_url");
            if (p.verifyUrl.empty())
                p.verifyUrl = ovOptStr(x, "verification_uri");
            p.stage = ovOptStr(x, "login_stage");
            p.lastError = ovOptStr(x, "last_error");
            // Also fold connected into empty-account providers via login map stage.
            if (x.value("connected", false) && p.stage.empty())
                p.stage = "connected";
            if (wasInProgress && !p.inProgress) {
                s_ovSub.lastAccountsPoll = -1e9;
                s_ovSub.lastUsagePoll = -1e9;
            }
        }
    });
}

static void drawOvProvidersTab(AppState& st, float S) {
    const int frame = ImGui::GetFrameCount();
    const bool justOpened = (frame - s_ovSub.lastDrawnFrame) > 1;
    s_ovSub.lastDrawnFrame = frame;
    ovPollSubAccounts(st, false);
    ovPollSubUsage(st, justOpened);

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 1));
    ImGui::TextWrapped("Sign-in state, accounts, and usage per provider.");
    ImGui::PopStyleColor();

    if (!s_ovSub.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", s_ovSub.lastError.c_str());
        ImGui::PopStyleColor();
    }

    // Autoscale: only scroll when account lists get long.
    const float listH = ImMax(120.0f * S, ImGui::GetContentRegionAvail().y - 4.0f * S);
    ImGui::BeginChild("##ov_prov_list", ImVec2(0, listH), ImGuiChildFlags_None);

    for (int pi = 0; pi < 4; ++pi) {
        const char* prov = kOvSubProviders[pi];
        const char* plabel = kOvSubProviderLabels[pi];
        ImGui::PushID(prov);

        // Provider header + sign-in state chip.
        bool anyAcct = false;
        bool anyConnected = false;
        for (const auto& row : s_ovSub.rows) {
            if (row.provider != prov) continue;
            anyAcct = true;
            if (row.connected) anyConnected = true;
        }
        OvSubLoginProgress& prog = s_ovSub.login[prov];
        if (!anyConnected && prog.stage == "connected") anyConnected = true;

        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
        ImGui::TextUnformatted(plabel);
        ImGui::PopStyleColor();
        ImGui::SameLine();
        {
            const char* state = prog.inProgress ? "signing in\xe2\x80\xa6"
                              : anyAcct ? (anyConnected ? "signed in" : "disconnected")
                              : "not signed in";
            const float* scc = prog.inProgress ? ui::palette.statusWarn
                             : anyAcct && anyConnected ? ui::palette.statusOk
                             : ui::palette.textDim;
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(scc[0], scc[1], scc[2], 1));
            ImGui::TextUnformatted(state);
            ImGui::PopStyleColor();
        }

        // Compact account rows.
        for (size_t i = 0; i < s_ovSub.rows.size(); ++i) {
            OvSubAccountRow& row = s_ovSub.rows[i];
            if (row.provider != prov) continue;
            ImGui::PushID((int)i);

            bool active = row.active;
            if (ImGui::RadioButton("##act", active)) {
                if (!active) {
                    nlohmann::json p;
                    p["provider"] = prov;
                    p["account_id"] = row.accountId;
                    st.rpc.call("subscription_switch", p, [](const core::RpcResult& r) {
                        const bool rejected = r.result.is_object() &&
                                              r.result.value("ok", true) == false;
                        if (!r.ok || rejected) {
                            auto reason = rejected ? ovOptStr(r.result, "reason") : r.err;
                            s_ovSub.lastError =
                                reason.empty() ? "account switch failed" : reason;
                        }
                        s_ovSub.lastAccountsPoll = -1e9;
                        s_ovSub.lastUsagePoll = -1e9;
                    });
                    for (auto& r2 : s_ovSub.rows)
                        if (r2.provider == prov) r2.active = false;
                    s_ovSub.rows[i].active = true;
                }
            }
            ImGui::SameLine(0, 6.0f * S);

            const char* soLabel = "Out";
            const float soW = ImGui::CalcTextSize(soLabel).x
                + ImGui::GetStyle().FramePadding.x * 2.0f;
            const float gap = 6.0f * S;
            std::string primary = ovAccountPrimaryLabel(row);
            if (!row.plan.empty()) primary += " \xc2\xb7 " + row.plan;
            if (!row.connected) primary += " (off)";

            const float labelMaxW = std::max(48.0f * S,
                ImGui::GetContentRegionAvail().x - soW - gap);
            ImGui::AlignTextToFramePadding();
            {
                const ImVec2 ts = ImGui::CalcTextSize(primary.c_str());
                if (ts.x > labelMaxW) {
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    const ImVec2 p0 = ImGui::GetCursorScreenPos();
                    const char* ell = "\xe2\x80\xa6";
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    const float clipW = std::max(ellW + 4.0f * S, labelMaxW);
                    dl->PushClipRect(p0,
                        ImVec2(p0.x + clipW - ellW, p0.y + ts.y + 2.0f * S), true);
                    dl->AddText(p0, ImGui::GetColorU32(ImGuiCol_Text), primary.c_str());
                    dl->PopClipRect();
                    dl->AddText(ImVec2(p0.x + clipW - ellW, p0.y),
                                ImGui::GetColorU32(ImGuiCol_Text), ell);
                    ImGui::Dummy(ImVec2(labelMaxW,
                                        std::max(ts.y, ImGui::GetTextLineHeight())));
                } else {
                    ImGui::TextUnformatted(primary.c_str());
                }
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) &&
                    !row.accountId.empty())
                    ImGui::SetTooltip("%s", row.accountId.c_str());
            }

            ImGui::SameLine(0, 0.0f);
            {
                const float avail = ImGui::GetContentRegionAvail().x;
                if (avail > soW + 2.0f * S)
                    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + avail - soW);
            }
            if (ImGui::SmallButton(soLabel)) {
                nlohmann::json p;
                p["provider"] = prov;
                p["account_id"] = row.accountId;
                st.rpc.call("subscription_logout", p, [](const core::RpcResult& r) {
                    const bool rejected = r.result.is_object() &&
                                          r.result.value("ok", true) == false;
                    if (!r.ok || rejected) {
                        auto reason = rejected ? ovOptStr(r.result, "reason") : r.err;
                        s_ovSub.lastError =
                            reason.empty() ? "sign out failed" : reason;
                    }
                    s_ovSub.lastAccountsPoll = -1e9;
                    s_ovSub.lastUsagePoll = -1e9;
                });
                s_ovSub.rows.erase(s_ovSub.rows.begin() + (long)i);
                ImGui::PopID();
                break;
            }

            // Compact usage under the identity row.
            ImGui::Indent(ImGui::GetFrameHeight() + gap);
            if (row.hasUsage && !row.usage.empty()) {
                for (size_t wi = 0; wi < row.usage.size(); ++wi) {
                    ImGui::PushID((int)wi);
                    ovDrawCompactUsage(row.usage[wi], S);
                    ImGui::PopID();
                }
            }
            ImGui::Unindent(ImGui::GetFrameHeight() + gap);
            ImGui::PopID();
        }

        if (!anyAcct && !prog.inProgress) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No accounts");
            ImGui::PopStyleColor();
        }

        // Login progress / Add account.
        if (prog.inProgress) {
            if (prog.userCode.empty() && prog.verifyUrl.empty()) {
                ImGui::TextUnformatted("Opening browser\xe2\x80\xa6");
            } else {
                if (prog.browserOpened)
                    ImGui::TextUnformatted("Browser open \xe2\x80\x94 approve access\xe2\x80\xa6");
                else
                    ImGui::TextUnformatted("Open the link to approve:");
                if (!prog.userCode.empty()) {
                    ImGui::Text("Code: %s", prog.userCode.c_str());
                    ImGui::SameLine();
                    char copyId[64];
                    std::snprintf(copyId, sizeof(copyId), "Copy##%s", prov);
                    if (ImGui::SmallButton(copyId))
                        ImGui::SetClipboardText(prog.userCode.c_str());
                }
                const std::string fallbackUrl =
                    prog.verifyUrl.empty()
                        ? (std::string(prov) == "xai" ? "https://accounts.x.ai/activate"
                                                      : prog.verifyUrl)
                        : prog.verifyUrl;
                if (!fallbackUrl.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                    ImGui::TextWrapped("%s", fallbackUrl.c_str());
                    ImGui::PopStyleColor();
                    char openId[64];
                    std::snprintf(openId, sizeof(openId), "Open##%s", prov);
                    if (ImGui::SmallButton(openId)) {
#if defined(_WIN32)
                        std::system(("start \"\" \"" + fallbackUrl + "\"").c_str());
#elif defined(__APPLE__)
                        std::system(("open '" + fallbackUrl + "'").c_str());
#else
                        std::system(("xdg-open '" + fallbackUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                    }
                }
            }
            ovPollSubAccounts(st, true);
        } else {
            char addId[64];
            std::snprintf(addId, sizeof(addId),
                          anyAcct ? "Add account##%s" : "Sign in##%s", prov);
            if (ImGui::SmallButton(addId)) {
                prog.inProgress = true;
                prog.lastError.clear();
                prog.userCode.clear();
                prog.verifyUrl.clear();
                prog.browserOpened = false;
                prog.stage = "start";
                nlohmann::json p;
                p["provider"] = prov;
                if (std::string(prov) == "xai")
                    p["device"] = true;
                st.rpc.call("subscription_login", p, [prov](const core::RpcResult& r) {
                    OvSubLoginProgress& pr = s_ovSub.login[prov];
                    if (!r.ok) {
                        pr.inProgress = false;
                        pr.lastError = r.err.empty() ? "login failed" : r.err;
                    } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                        pr.inProgress = false;
                        auto reason = ovOptStr(r.result, "reason");
                        auto hint = ovOptStr(r.result, "hint");
                        pr.lastError = reason.empty() ? "login failed" : reason;
                        if (!hint.empty()) pr.lastError += " \xe2\x80\x94 " + hint;
                    }
                    s_ovSub.lastAccountsPoll = -1e9;
                    s_ovSub.lastUsagePoll = -1e9;
                });
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Add a %s subscription account (browser OAuth).", plabel);
        }
        if (!prog.lastError.empty() && !prog.inProgress) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", prog.lastError.c_str());
            ImGui::PopStyleColor();
        }

        ImGui::Dummy(ImVec2(0, 4.0f * S));
        ImGui::PopID();
    }

    ImGui::EndChild();
}


}  // namespace

static void drawAISettingsBody(AppState& st, bool busy) {
    Settings& s = st.settings;
    const float S = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    bool changed = false;
    (void)busy;

    // Esc returns to chat (same as gear toggle / Sessions "< Chat").
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false) && !ImGui::IsAnyItemActive()) {
        st.showAISettingsOverlay = false;
        return;
    }

    // Tight chrome spacing so the narrow copilot column packs options without
    // large empty gaps between header / tabs / content.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ui::metrics::U(1.0f), ui::metrics::U(0.75f)));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(ui::metrics::U(1.5f), ui::metrics::U(0.75f)));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("AI Settings");
    ImGui::PopFont();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextWrapped("Agents, prompts, skills, permissions, providers.");
    ImGui::PopStyleColor();
    // Clear vertical separation so the tab strip never overlaps the title block.
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1.0f)));

    // Wrapping top tabs — all five must stay visible in the narrow copilot column
    // (no horizontal scrollbar that clips Permissions/Providers).
    static int s_aiSettingsRail = 0;
    static const char* kRailLabels[] = {
        "Agents", "Prompts", "Skills", "Permissions", "Providers",
    };
    const int nTabs = 5;
    const float availW = ImGui::GetContentRegionAvail().x;
    const float gapX = ui::metrics::U(0.5f);
    const float gapY = ui::metrics::U(0.5f);
    // Compact pad so more labels fit per row; still readable at any uiScale.
    const float tabPadX = ui::metrics::U(1.5f);
    const float tabH = ImGui::GetFrameHeight();

    float naturalW[5];
    for (int i = 0; i < nTabs; ++i)
        naturalW[i] = ImGui::CalcTextSize(kRailLabels[i]).x + tabPadX * 2.0f;

    // Greedy wrap into rows, then even-fit each row to the column width so the
    // strip always shows every tab (typically 3+2 in a narrow copilot pane).
    int rowStart[5];
    int rowCount[5];
    int nRows = 0;
    {
        int i = 0;
        while (i < nTabs && nRows < nTabs) {
            rowStart[nRows] = i;
            float used = 0.0f;
            int count = 0;
            while (i < nTabs) {
                const float need = naturalW[i] + (count ? gapX : 0.0f);
                if (count > 0 && used + need > availW + 0.5f) break;
                used += need;
                ++count;
                ++i;
            }
            // Always place at least one tab per row (pathological narrow width).
            if (count == 0) {
                count = 1;
                ++i;
            }
            rowCount[nRows] = count;
            ++nRows;
        }
    }

    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gapX, gapY));
    for (int r = 0; r < nRows; ++r) {
        const int start = rowStart[r];
        const int count = rowCount[r];
        float naturalSum = 0.0f;
        for (int k = 0; k < count; ++k) naturalSum += naturalW[start + k];
        const float gaps = gapX * (float)ImMax(0, count - 1);
        const float extra = ImMax(0.0f, availW - naturalSum - gaps);
        // Distribute leftover width evenly so a partial last row still fills.
        const float grow = (count > 0) ? extra / (float)count : 0.0f;

        for (int k = 0; k < count; ++k) {
            const int i = start + k;
            if (k) ImGui::SameLine(0, gapX);
            const bool sel = (s_aiSettingsRail == i);
            const float tw = naturalW[i] + grow;
            ImGui::PushStyleColor(ImGuiCol_Button, sel
                ? ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1],
                         ui::palette.chipBg[2], ui::palette.chipBg[3])
                : ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.06f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.12f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.18f));
            ImGui::PushStyleColor(ImGuiCol_Text, sel ? surf.textPrimary : surf.textSecondary);
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                ImVec2(tabPadX, (tabH - ImGui::GetFontSize()) * 0.5f));
            if (ImGui::Button(kRailLabels[i], ImVec2(tw, tabH)))
                s_aiSettingsRail = i;
            ImGui::PopStyleVar();
            if (sel) {
                ImGui::GetWindowDrawList()->AddRect(
                    ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                    ImGui::ColorConvertFloat4ToU32(semanticOnGlass(ui::palette.accent, surf)),
                    ui::metrics::rounding(6.0f), 0, ui::metrics::stroke(1.0f));
            }
            ImGui::PopStyleColor(4);
        }
    }
    ImGui::PopStyleVar(2);

    // Content starts immediately under the tab strip — small gap only.
    ImGui::Dummy(ImVec2(0, ui::metrics::U(0.75f)));

    ImGui::BeginChild("##ai_ov_body", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ui::metrics::U(1.0f), ui::metrics::U(1.0f)));
    switch (s_aiSettingsRail) {
    case 0: drawOvAgentsTab(st, S, changed); break;
    case 1: drawOvPromptsTab(st, S, changed); break;
    case 2: drawOvSkillsTab(st, S); break;
    case 3: drawOvPermissionsTab(st, S, changed); break;
    case 4: drawOvProvidersTab(st, S); break;
    default: break;
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();

    if (changed) saveSettings(s);

    ImGui::PopStyleColor();  // surf.textPrimary
    ImGui::PopStyleVar(2);    // ItemSpacing + FramePadding
}

// ============================ searchable model dropdown ============================
// A short provider tag shown as the dim sublabel next to each model (like the reference's
// "Copilot").
static const char* providerShort(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC: return "Claude";
        case ui::PROV_OPENAI:    return "OpenAI";
        case ui::PROV_LOCAL:     return "Local";
        case ui::PROV_LLAMACPP:  return "llama.cpp";
        case ui::PROV_GEMINI:    return "Gemini";
        case ui::PROV_XAI:       return "xAI";
        case ui::PROV_CLOUDCOMPAT: return "Cloud";
        case ui::PROV_CURSOR:    return "Cursor";
    }
    return "";
}

// A discovered local model is selectable only when the endpoint can actually serve it: when LM
// Studio reports loaded state we require the model to be loaded; otherwise (servers without that
// endpoint, e.g. vLLM/llama.cpp, which only list what they serve) every listed id is available.
static bool localModelLoaded(const ProviderModels& pm, const std::string& id) {
    if (!pm.loadedKnown) return true;
    return std::find(pm.loaded.begin(), pm.loaded.end(), id) != pm.loaded.end();
}

// Has the user hidden this model from the chat picker (Settings → AI → Manage models)?
static bool modelHidden(AppState& st, const std::string& id) {
    return st.settings.hiddenModels.count(id) != 0;
}

// File-static cache of subscription_status "connected" flags so the model picker can show
// signed-in providers (xAI / Claude / Cursor / Codex) without requiring an API key.
namespace {
struct SubConnectedCache {
    bool xai = false;
    bool claude = false;
    bool cursor = false;
    bool codex = false;
    double lastPoll = -1e9;
    bool inFlight = false;
};
static SubConnectedCache s_subConn;

static void pollSubConnected(AppState& st) {
    const double now = ImGui::GetTime();
    if (s_subConn.inFlight || (now - s_subConn.lastPoll) < 2.0) return;
    s_subConn.lastPoll = now;
    s_subConn.inFlight = true;
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_subConn.inFlight = false;
        if (!r.ok || !r.result.is_object()) return;
        auto providers = r.result.value("providers", nlohmann::json::object());
        auto connected = [](const nlohmann::json& providers, const char* key) {
            auto p = providers.value(key, nlohmann::json::object());
            return p.value("connected", false);
        };
        s_subConn.xai    = connected(providers, "xai");
        s_subConn.claude = connected(providers, "claude");
        s_subConn.cursor = connected(providers, "cursor");
        s_subConn.codex  = connected(providers, "codex");
    });
}
}  // namespace

// Does this provider have credentials / a reachable endpoint / a signed-in subscription,
// so its models can be selected?
static bool providerUsable(AppState& st, int provider) {
    pollSubConnected(st);
    switch (provider) {
        case ui::PROV_GEMINI:      return st.settings.geminiKey[0] != 0;
        case ui::PROV_ANTHROPIC:   return st.settings.anthropicKey[0] != 0 || s_subConn.claude;
        case ui::PROV_OPENAI:      return st.settings.openaiKey[0] != 0 || s_subConn.codex;
        case ui::PROV_CLOUDCOMPAT: return st.settings.cloudKey[0] != 0;
        case ui::PROV_XAI:         return st.settings.xaiKey[0] != 0 || s_subConn.xai;
        case ui::PROV_LOCAL:       return st.providerModels[ui::PROV_LOCAL].ok;
        case ui::PROV_LLAMACPP:    return st.providerModels[ui::PROV_LLAMACPP].ok;
        case ui::PROV_CURSOR:      return s_subConn.cursor;
    }
    return false;
}

// One model row: checkmark gutter (drawn — no check glyph in the font) + label + dim provider
// sublabel; an accent "Add key" when the provider isn't usable. Returns true when picked.
// `uid` makes the ImGui id unique per row — model ids can repeat across providers/endpoints
// (the same local endpoint backs both PROV_LOCAL and PROV_LLAMACPP), and two rows sharing an
// id string would trip ImGui's "conflicting ID" assert.
static bool modelRow(AppState& st, int provider, const std::string& id, const std::string& label,
                     bool usable, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight();
    const float fh = ImGui::GetFontSize();
    const float gut = fh + 8 * sc;
    bool current = (st.settings.provider == provider &&
                    std::strcmp(st.settings.model, id.c_str()) == 0);
    ImGui::PushID(uid);
    bool clicked = ImGui::Selectable("##mrow", current,
                                     usable ? 0 : ImGuiSelectableFlags_Disabled, ImVec2(0, rowH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    float midY = (mn.y + mx.y) * 0.5f;
    if (current) {
        float r = fh * 0.30f; ImVec2 c(mn.x + 5 * sc + r, midY);
        ImU32 col = ui::Palette::toU32(pal.accent); float th = ui::metrics::stroke(1.6f);
        dl->AddLine(ImVec2(c.x - r * 0.55f, c.y + r * 0.05f),
                    ImVec2(c.x - r * 0.10f, c.y + r * 0.50f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.10f, c.y + r * 0.50f),
                    ImVec2(c.x + r * 0.60f, c.y - r * 0.50f), col, th);
    }
    dl->AddText(ImVec2(mn.x + gut, midY - fh * 0.5f),
                usable ? ui::Palette::toU32(pal.text) : ui::Palette::toU32(pal.textDisabled),
                label.c_str());
    float lw = ImGui::CalcTextSize(label.c_str()).x;
    dl->AddText(ImVec2(mn.x + gut + lw + 8 * sc, midY - fh * 0.5f),
                ui::Palette::toU32(pal.textDim), providerShort(provider));
    if (!usable) {
        const char* hint = "Add key";
        float hw = ImGui::CalcTextSize(hint).x;
        dl->AddText(ImVec2(mx.x - hw - 8 * sc, midY - fh * 0.5f),
                    ui::Palette::toU32(pal.accent), hint);
    }
    ImGui::PopID();
    return clicked && usable;
}

// The model dropdown body (hosted in a popup): a search box with a compact glass gear on the
// same row (opens Settings), the usable models with a checkmark on the current one, and a muted
// hint for unkeyed providers. Picking a model sets provider+model and closes the popup.
static void drawModelDropdown(AppState& st, bool busy) {
    (void)busy;
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    static char s_modelSearch[64] = "";
    refreshModelAvailability(st, false);

    if (ImGui::IsWindowAppearing()) { s_modelSearch[0] = 0; ImGui::SetKeyboardFocusHere(); }

    // Search + glass gear on one row: gear is smaller and sits to the right of a compressed field.
    const float gearSz = ImGui::GetFrameHeight() * 0.82f;
    const float gearGap = 4.0f * sc;
    const float searchW = std::max(40.0f * sc,
                                   ImGui::GetContentRegionAvail().x - gearSz - gearGap);
    ImGui::SetNextItemWidth(searchW);
    ImGui::InputTextWithHint("##modelsearch", "Search models", s_modelSearch, sizeof(s_modelSearch));
    ImGui::SameLine(0.0f, gearGap);
    {
        IconUV gear = ui::getIcon("settings", 20);
        const ImVec2 keep = ImGui::GetCursorScreenPos();
        // Align gear vertically with the search field.
        const float fieldH = ImGui::GetFrameHeight();
        if (gearSz < fieldH)
            ImGui::SetCursorScreenPos(ImVec2(keep.x, keep.y + (fieldH - gearSz) * 0.5f));
        const bool openSet = ImGui::InvisibleButton("##mdlgear", ImVec2(gearSz, gearSz));
        const bool gHov = ImGui::IsItemHovered();
        ImDrawList* gdl = ImGui::GetWindowDrawList();
        const ImVec2 gmn = ImGui::GetItemRectMin(), gmx = ImGui::GetItemRectMax();
        // Compact glass pill (theme helper — not a raw button chrome).
        ui::glassRowBackground(gdl, gmn, gmx, ui::metrics::rounding(4.0f),
                               /*stripe=*/false, /*selected=*/gHov, /*washAlpha=*/0.38f);
        const ImU32 ink = ui::Palette::toU32(gHov ? pal.text : pal.textDim);
        if (gear.valid) {
            const float isz = gearSz * 0.58f;
            const ImVec2 gc((gmn.x + gmx.x) * 0.5f, (gmn.y + gmx.y) * 0.5f);
            gdl->AddImage(gear.texId,
                          ImVec2(gc.x - isz * 0.5f, gc.y - isz * 0.5f),
                          ImVec2(gc.x + isz * 0.5f, gc.y + isz * 0.5f),
                          gear.uv0, gear.uv1, ink);
        } else {
            // Fallback: simple gear-ish cog via text when the atlas cell is missing.
            const char* gl = "\xE2\x9A\x99";  // ⚙
            const ImVec2 ts = ImGui::CalcTextSize(gl);
            gdl->AddText(ImVec2((gmn.x + gmx.x - ts.x) * 0.5f, (gmn.y + gmx.y - ts.y) * 0.5f),
                         ink, gl);
        }
        if (gHov) ImGui::SetTooltip("AI settings");
        if (openSet) { st.openSettings = true; st.openSettingsTab = 2; ImGui::CloseCurrentPopup(); }
    }
    ImGui::Separator();

    auto lc = [](std::string s) {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        return s;
    };
    std::string q = lc(s_modelSearch);
    auto matchq = [&](const std::string& a, const std::string& b) {
        return q.empty() || lc(a).find(q) != std::string::npos || lc(b).find(q) != std::string::npos;
    };

    bool picked = false;
    int uid = 0;       // monotonic per-row id seed — keeps ImGui ids unique across sections
    int shown = 0;     // visible rows after availability + search filtering
    ImGui::BeginChild("##modellist", ImVec2(0, 240 * sc), ImGuiChildFlags_None);

    auto catOff = [&](const std::string& key) {
        return st.settings.disabledProviders.count(key) != 0;
    };

    // Usable built-in cloud models (provider has a key), minus any the user hid or whose provider
    // category is bulk-disabled.
    for (const auto& m : ui::builtinModels()) {
        if (catOff(ui::providerCatKey(m.provider))) continue;
        if (!providerUsable(st, m.provider) || !matchq(m.label, providerShort(m.provider))) continue;
        if (modelHidden(st, m.id)) continue;
        ++shown;
        if (modelRow(st, m.provider, m.id, m.label, true, uid++)) {
            st.settings.provider = m.provider;
            std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
            picked = true;
        }
    }
    // Cloud OpenAI-compatible service presets (OpenRouter / Alibaba / DeepSeek). Shown when a cloud
    // key is configured and the service category isn't bulk-disabled. Picking one points the active
    // Cloud API provider at that service's endpoint + model.
    if (st.settings.cloudKey[0]) {
        // OpenRouter: the live list (all free models + curated paid), minus any the user hid.
        if (!catOff("openrouter")) {
            for (const auto& r : openRouterModels(st)) {
                if (modelHidden(st, r.id) || !matchq(r.label, "openrouter free")) continue;
                ++shown;
                if (modelRow(st, ui::PROV_CLOUDCOMPAT, r.id, r.label, true, uid++)) {
                    st.settings.provider = ui::PROV_CLOUDCOMPAT;
                    std::strncpy(st.settings.model, r.id.c_str(), sizeof(st.settings.model) - 1);
                    std::strncpy(st.settings.baseUrl, "https://openrouter.ai/api/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                    st.settings.vision = r.vision;
                    picked = true;
                }
            }
        }
        // Alibaba / DeepSeek curated presets.
        for (const auto& p : ui::cloudPresets()) {
            std::string key = ui::cloudServiceKey(p.baseUrl);
            if (key == "openrouter") continue;  // handled above (live)
            if (catOff(key)) continue;
            if (modelHidden(st, p.id) || !matchq(p.label, key)) continue;
            ++shown;
            if (modelRow(st, ui::PROV_CLOUDCOMPAT, p.id, p.label, true, uid++)) {
                st.settings.provider = ui::PROV_CLOUDCOMPAT;
                std::strncpy(st.settings.model, p.id.c_str(), sizeof(st.settings.model) - 1);
                std::strncpy(st.settings.baseUrl, p.baseUrl.c_str(), sizeof(st.settings.baseUrl) - 1);
                st.settings.vision = p.vision;
                picked = true;
            }
        }
    }
    // Live-discovered local models (when a local endpoint is up). The probe assigns the same
    // model list to PROV_LOCAL and PROV_LLAMACPP (one endpoint family), so dedup by id to avoid
    // listing each model twice. Skip models that are downloaded but not loaded, and any the user
    // hid.
    std::set<std::string> seenLocal;
    for (int lp : {ui::PROV_LOCAL, ui::PROV_LLAMACPP}) {
        if (catOff("local")) break;
        const ProviderModels& pm = st.providerModels[lp];
        if (!pm.ok) continue;
        for (const auto& id : pm.models) {
            if (!seenLocal.insert(id).second) continue;     // already shown for the sibling provider
            if (!localModelLoaded(pm, id)) continue;        // downloaded but not loaded into memory
            if (modelHidden(st, id)) continue;
            if (!matchq(id, providerShort(lp))) continue;
            ++shown;
            if (modelRow(st, lp, id, id, true, uid++)) {
                st.settings.provider = lp;
                std::strncpy(st.settings.model, id.c_str(), sizeof(st.settings.model) - 1);
                // This pick may be switching AWAY from a cloud provider whose base_url is remote.
                // Point base_url back at the local endpoint so the run hits LM Studio, not the cloud
                // (a local model id sent to the cloud 403s). Preserve an already-loopback base_url so
                // a custom local port survives.
                std::string bu = st.settings.baseUrl ? st.settings.baseUrl : "";
                bool loop = bu.find("localhost") != std::string::npos ||
                            bu.find("127.0.0.1") != std::string::npos ||
                            bu.find("0.0.0.0") != std::string::npos ||
                            bu.find("[::1]") != std::string::npos;
                if (!loop)
                    std::strncpy(st.settings.baseUrl,
                                 lp == ui::PROV_LLAMACPP ? "http://127.0.0.1:1234/v1"
                                                         : "http://localhost:1234/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                picked = true;
            }
        }
    }
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped(q.empty()
            ? "No models available — sign in to a subscription (xAI / Claude / Cursor), "
              "add an API key, or start a local model server (gear below)."
            : "No models match your search.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();

    // Unavailable models (no key / not signed in / endpoint down) are hidden from the list above;
    // surface a single muted hint pointing at Settings so they're still discoverable.
    bool anyUnavailable = false;
    for (const auto& m : ui::builtinModels())
        if (!providerUsable(st, m.provider)) { anyUnavailable = true; break; }
    if (anyUnavailable) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted(
            "Other providers need a sign-in or API key \xE2\x80\x94 add one in Settings.");
        ImGui::PopStyleColor();
    }

    if (picked) {
        // CRITICAL: persist the pick to config.json NOW. The in-app run (agent_run_v2) resolves its
        // provider from config.json in the C++ core — NOT from in-memory st.settings — so a pick that
        // only updates st.settings would leave the run using the STALE persisted provider. That's the
        // bug where selecting a local model (gemma) still routed to the previously-saved cloud
        // endpoint and 403'd. Saving here keeps config.json == what the picker shows before any run.
        saveSettings(st.settings);
        ImGui::CloseCurrentPopup();
    }
}

// Launch the active turn for `text` according to the current mode (Design/Edit/Ask). Pushes
// the user + live-assistant ChatMsgs. Centralizes the three old launch paths so the top input
// and the per-turn follow-up input share one code path.
static void submitTurn(AppState& st, const std::string& text) {
    if (text.empty()) return;
    // Persist the active provider/model to config.json BEFORE launching any run. The C++ core
    // resolves the provider from config.json (not in-memory st.settings), so this guarantees the
    // run uses exactly what the picker shows — closing the window where a freshly-picked local
    // model still ran against a stale, previously-saved cloud endpoint (the 403 "model_failed").
    // (O1 additionally pins provider/model/effort EXPLICITLY in each RPC payload below, so an
    // AI_CAD_PROVIDER env var can no longer silently override the picker.)
    saveSettings(st.settings);
    // M23: fold pending attachments into THIS turn. `sendText` (what the model receives) gets
    // inlined text-file content / explicit paths appended; the chat shows a compact marker.
    // Wave4: context_refs ride as structured payload AND are inlined into sendText (C++ agent_run
    // only reads request text — same honesty pattern as attachments).
    std::string sendText = text;
    const nlohmann::json attachments = consumeAttachments(st, sendText);
    const nlohmann::json contextRefs = consumeContextRefs(st, sendText);
    const nlohmann::json provOverride = agentProviderOverride(st.settings);
    // Shared extras (attachments + context_refs) for every agent RPC path below.
    nlohmann::json turnExtras = nlohmann::json::object();
    if (!attachments.empty()) turnExtras["attachments"] = attachments;
    if (!contextRefs.empty()) turnExtras["context_refs"] = contextRefs;
    {
        ChatMsg u; u.fromUser = true; u.text = text;
        if (!attachments.empty()) {
            u.text += "\n[attached:";
            for (const auto& a : attachments) u.text += " " + a.value("name", std::string());
            u.text += "]";
        }
        if (!contextRefs.empty()) {
            u.text += "\n[context:";
            for (const auto& r : contextRefs) {
                std::string lbl = r.value("label", std::string());
                if (lbl.empty()) lbl = r.value("kind", std::string("ref"));
                u.text += " " + lbl;
            }
            u.text += "]";
        }
        st.chat.push_back(u);
    }
    { ChatMsg a; a.running = true; if (st.settings.model[0]) a.model = st.settings.model; st.chat.push_back(a); }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)

    // Name the conversation tab after its first prompt; persist so the turn survives a restart.
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) {
        std::string& title = st.conversations[st.activeConv].title;
        bool isDefault = title.empty() || (title.rfind("Chat ", 0) == 0);
        if (isDefault) title = text.size() > 28 ? (text.substr(0, 27) + "...") : text;
    }
    ui::persistChatHistory(st);

    bool editMode = (st.aiInteraction == 1);
    bool askMode  = (st.aiInteraction == 2);

    std::string lo = text;
    std::transform(lo.begin(), lo.end(), lo.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    auto has = [&](const char* k) { return lo.find(k) != std::string::npos; };

    // MISSION intent: a request that asks to BUILD something AND then analyze/validate it
    // ("model an airplane, then run CFD and prove it's good"; "design a bracket and FEA it").
    // The old routing was mutually exclusive — a "...run a CFD..." phrase greedily hijacked the
    // whole message into the sim tool, which then refused on the empty doc. Route the combined
    // case to the mission orchestrator, which decomposes it into design -> analyze -> verdict.
    // Conservative on purpose: a miss just falls through to plain design (no regression).
    auto looksLikeMission = [&]() -> bool {
        if (editMode || askMode || st.agentV2Running) return false;
        bool buildVerb = has("make ") || has("model ") || has("build ") || has("design ") ||
                         has("create ") || has("generate ") || has("give me");
        bool analyzeVerb = has("cfd") || has("wind tunnel") || has("windtunnel") ||
                           has("aerodynam") || has("simulat") || has("flow sim") || has("fea") ||
                           has("stress") || has("downforce") ||
                           ((has("drag") || has("lift")) && (has("air") || has("speed") ||
                            has("wind") || has("m/s") || has("mph"))) ||
                           has("prove") || has("validate") || has("verify it") ||
                           has("is it a good") || has("good design") || has("analyze");
        return buildVerb && analyzeVerb;
    };
    if (looksLikeMission()) {
        launchMissionRunChat(st, sendText, /*needFreshDoc=*/true, turnExtras);
        return;
    }

    // CFD/sim intent: a flow-sim request on an open part routes to the simulation tool in ANY mode
    // (the AI runs a wind-tunnel CFD and reports drag/lift). Mirrors Ask — async, fills the chat,
    // never mutates geometry. Works fully offline (the core parses the flow condition itself).
    // This is the SIM-ONLY quick path (no build verb) — a combined build+analyze request was
    // already handled by looksLikeMission above.
    auto looksLikeSim = [&]() -> bool {
        if (!st.docMirror.hasDoc() || st.agentV2Running) return false;
        if (has("cfd") || has("wind tunnel") || has("windtunnel") || has("aerodynam") ||
            has("drag") || has("downforce"))
            return true;
        return (has("simulate") || has("simulation") || has("flow") || has("lift")) &&
               (has("air") || has("m/s") || has("mph") || has("km/h") || has("speed") ||
                has("velocity") || has("wind"));
    };
    if (looksLikeSim()) {
        st.addLog("[..] cfd: " + text);
        st.status = "Running CFD...";  st.statusKind = 0;
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"request", text}};
        // Accurate (OpenFOAM RANS) opt-in: when the user asks for an accurate/validated run AND the
        // worker reports OpenFOAM available, request solver=openfoam. The core exports an STL and
        // hands back {needs_openfoam, stl_path, velocity_ms, aoa_deg, fluid}; the app loop fulfils
        // it on the dedicated (non-blocking) SimClient channel — see pollChatOpenFoam in app.cpp.
        // (sim_run_flow must NOT go through st.rpc: WorkerClient::call serialises on callMtx_, so a
        // minutes-long solve there would freeze every other RPC.)
        const bool wantAccurate = has("accurate") || has("openfoam") || has("open foam") ||
                                  has("precise") || has("high-fidelity") || has("high fidelity") ||
                                  has("rans") || has("validated");
        bool ofAvail = false;
        if (wantAccurate && st.simClient) {
            nlohmann::json caps = st.simClient->caps();
            if (caps.is_object()) ofAvail = caps.value("openfoam", false);
        }
        if (wantAccurate && ofAvail) params["solver"] = "openfoam";
        if (wantAccurate && !ofAvail)
            st.addLog("[cfd] accurate requested but OpenFOAM not provisioned — using LBM estimate");

        st.rpc.call("agent_simulate_v1", params, [&st](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok) err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));

            // Accurate hand-off: the core exported an STL; launch OpenFOAM on the SimClient channel
            // (non-blocking) and let the app loop post the result when the study finishes.
            if (err.empty() && r.result.value("needs_openfoam", false) && st.simClient) {
                const bool water = (r.result.value("fluid", std::string("air")) == "water");
                nlohmann::json fparams = {
                    {"stl_path", r.result.value("stl_path", std::string())},
                    {"direction", {1.0, 0.0, 0.0}},
                    {"speed_mps", r.result.value("velocity_ms", 10.0)},
                    {"aoa_deg", r.result.value("aoa_deg", 0.0)},
                    {"sideslip_deg", 0.0}, {"preset", "coarse"}, {"ref_area_mm2", 0.0},
                    {"fluid", {{"name", water ? "water" : "air"},
                               {"density_kgm3", water ? 1000.0 : 1.225},
                               {"kinematic_visc_m2s", water ? 1.0e-6 : 1.5e-5}}},
                    {"solver", "openfoam"},
                };
                std::string note;
                // Sentinel owner so SimManager::tick (which matches the real doc id) ignores this
                // job — the chat's own poll consumes it instead. No study/overlay side effects.
                if (st.simClient->launch("sim_run_flow", fparams, note, "__chat_cfd__")) {
                    st.chatCfdVel = r.result.value("velocity_ms", 10.0);
                    st.chatCfdAoa = r.result.value("aoa_deg", 0.0);
                    st.chatCfdPending = true;   // app loop polls SimClient and posts the result
                    if (msg) msg->liveStatus = "OpenFOAM RANS (a few minutes)...";
                    st.status = "Running OpenFOAM...";  st.statusKind = 0;
                    return;
                }
                err = note.empty() ? "could not launch OpenFOAM" : note;  // e.g. a study is running
            }

            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
                else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no result)";
                }
            }
            st.status = err.empty() ? "Ready." : "CFD failed.";
            st.statusKind = err.empty() ? 0 : 2;
        }, 600000);
        return;
    }

    if (editMode && st.docMirror.hasDoc()) {
        // Edit part -> orchestrator v2 (validated document ops).
        st.addLog("[..] editing: " + text);
        st.status = "Editing...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        if (st.activityPanel)
            st.activityPanel->clearLive();
        nlohmann::json extra = provOverride;                       // O1: pin the picker
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            extra[it.key()] = it.value();                          // M23 + Wave4
        launchAgentRunV2(st.worker, st.job, sendText, st.docMirror.docId(),
                         st.ribbonTab == AppState::RT_Manufacture, extra);
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
            {"question", sendText}};
        for (auto it = provOverride.begin(); it != provOverride.end(); ++it)
            params[it.key()] = it.value();                              // O1: pin the picker
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            params[it.key()] = it.value();                              // M23 + Wave4
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
        // Design new -> pre-flight CLARIFY gate, THEN the v2 orchestrator on a FRESH part
        // document. The clarify call (agent_clarify_v1) runs async via AsyncRpc; its callback
        // (GL thread) either launches the run directly (request already specific) or shows the
        // inline clarify card with the build-critical questions. Any failure falls straight
        // through to the build — clarify never blocks. See launchDesignRun / drawClarifyCard.
        st.designStarted = true;
        // Autopilot (planMode off) skips the clarify gate and builds straight away.
        if (!st.settings.planMode) {
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { it->liveStatus = "Building..."; break; }
            launchDesignRun(st, sendText, turnExtras);
            return;
        }
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running) {
                it->clarify.awaiting = true;
                it->clarify.request = sendText;   // M23: the eventual build carries attachments
                it->liveStatus = "Working out what I need to build this...";
                break;
            }
        st.addLog("[..] clarify: " + text);
        st.status = "Thinking...";  st.statusKind = 0;
        const std::string clarifyDocId = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
        nlohmann::json params = {{"request", sendText}, {"doc_id", clarifyDocId}};
        for (auto it = provOverride.begin(); it != provOverride.end(); ++it)
            params[it.key()] = it.value();                              // O1: pin the picker
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            params[it.key()] = it.value();                              // M23 + Wave4
        st.rpc.call("agent_clarify_v1", params,
                    [&st, text = sendText, clarifyDocId, turnExtras](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->clarify.awaiting) { msg = &*it; break; }
            if (!msg) return;  // turn was cleared (New part) — nothing to do
            // Multi-document: the user switched to a different document while clarify was in flight.
            // launchDesignRun below would build against the NOW-active doc — the wrong one. Stop the
            // turn's spinner and let the user re-issue from the doc they meant, rather than corrupt it.
            if (st.docMirror.docId() != clarifyDocId) {
                msg->clarify.awaiting = false;
                msg->running = false;
                msg->liveStatus.clear();
                st.addLog("[..] clarify result for " + clarifyDocId + " dropped — active doc changed");
                return;
            }
            msg->clarify.awaiting = false;
            // Parse questions; on ANY error/empty -> build straight away (never block).
            // Prefer the additive aicad.interaction/1 mirror when present (choice /
            // geometry_select / multi); fall back to legacy {key,ask,unit,suggested}.
            bool ready = true;
            std::vector<ClarifyQ> qs;
            std::vector<std::string> assumptions;
            if (r.ok && r.result.is_object() && !r.result.contains("code")) {
                ready = r.result.value("ready", true);
                auto seedAnswer = [](ClarifyQ& cq) {
                    std::strncpy(cq.answer.data(), cq.suggested.c_str(),
                                 cq.answer.size() - 1);
                    cq.answer[cq.answer.size() - 1] = '\0';
                };
                auto parseInteractionQ = [&](const nlohmann::json& q) -> bool {
                    if (!q.is_object()) return false;
                    ClarifyQ cq;
                    cq.key = q.value("id", q.value("key", std::string()));
                    cq.ask = q.value("prompt", q.value("ask", std::string()));
                    cq.kind = q.value("kind", std::string("text"));
                    cq.multi = q.value("multi", q.value("multiple", false));
                    if (cq.ask.empty()) return false;
                    if (q.contains("suggested")) {
                        const auto& s = q["suggested"];
                        if (s.is_string()) cq.suggested = s.get<std::string>();
                        else if (s.is_boolean())
                            cq.suggested = s.get<bool>() ? "true" : "false";
                        else if (s.is_number())
                            cq.suggested = std::to_string(s.get<double>());
                        else if (s.is_array()) {
                            for (const auto& v : s) {
                                if (v.is_string())
                                    cq.selectedValues.push_back(v.get<std::string>());
                            }
                            std::string joined;
                            for (size_t j = 0; j < cq.selectedValues.size(); ++j) {
                                if (j) joined += ", ";
                                joined += cq.selectedValues[j];
                            }
                            cq.suggested = joined;
                        } else if (s.is_object()) {
                            if (s.contains("value") && s.contains("unit")) {
                                cq.suggested = std::to_string(s.value("value", 0.0));
                                cq.unit = s.value("unit", std::string());
                            } else if (s.contains("id")) {
                                cq.suggested = s.value("id", std::string());
                                cq.selectedKind = s.value("kind", std::string());
                                if (!cq.suggested.empty())
                                    cq.selectedValues.push_back(cq.suggested);
                            }
                        }
                    }
                    if (q.contains("options") && q["options"].is_array()) {
                        for (const auto& o : q["options"]) {
                            if (!o.is_object()) continue;
                            std::string val = o.value("value", std::string());
                            std::string lab = o.value("label", val);
                            if (!val.empty()) cq.options.emplace_back(val, lab);
                        }
                    }
                    if (q.contains("targets") && q["targets"].is_array()) {
                        for (const auto& t : q["targets"])
                            if (t.is_string()) cq.targets.push_back(t.get<std::string>());
                    }
                    if (cq.kind == "dimension" && cq.unit.empty() &&
                        q.contains("suggested") && q["suggested"].is_object())
                        cq.unit = q["suggested"].value("unit", std::string());
                    seedAnswer(cq);
                    if (cq.kind == "choice" && !cq.multi && !cq.suggested.empty())
                        cq.selectedValues = {cq.suggested};
                    qs.push_back(std::move(cq));
                    return true;
                };

                bool usedInteraction = false;
                if (r.result.contains("interaction") &&
                    r.result["interaction"].is_object() &&
                    r.result["interaction"].contains("questions") &&
                    r.result["interaction"]["questions"].is_array() &&
                    !r.result["interaction"]["questions"].empty()) {
                    usedInteraction = true;
                    for (const auto& q : r.result["interaction"]["questions"])
                        parseInteractionQ(q);
                }
                if (!usedInteraction && r.result.contains("questions") &&
                    r.result["questions"].is_array()) {
                    for (const auto& q : r.result["questions"]) {
                        if (!q.is_object()) continue;
                        if (q.contains("kind") || q.contains("options") ||
                            q.contains("targets")) {
                            parseInteractionQ(q);
                            continue;
                        }
                        ClarifyQ cq;
                        cq.key = q.value("key", std::string());
                        cq.ask = q.value("ask", std::string());
                        cq.unit = q.value("unit", std::string());
                        cq.suggested = q.value("suggested", std::string());
                        cq.kind = cq.unit.empty() ? "text" : "dimension";
                        if (cq.ask.empty()) continue;
                        seedAnswer(cq);
                        qs.push_back(std::move(cq));
                    }
                }
                if (r.result.contains("assumptions") && r.result["assumptions"].is_array())
                    for (const auto& a : r.result["assumptions"])
                        if (a.is_string()) assumptions.push_back(a.get<std::string>());
            }
            if (ready || qs.empty()) {
                msg->liveStatus.clear();
                launchDesignRun(st, text, turnExtras);  // keeps msg->running + Wave4 extras
                return;
            }
            // Hold for the user: show the clarify card (msg stops "running" until Build it).
            msg->running = false;
            msg->liveStatus.clear();
            msg->clarify.active = true;
            msg->clarify.request = text;
            msg->clarify.questions = std::move(qs);
            msg->clarify.assumptions = std::move(assumptions);
            st.status = "Waiting on a few details...";  st.statusKind = 0;
            st.addLog("[..] clarify: " + std::to_string(msg->clarify.questions.size()) +
                      " question(s)");
        }, 120000);
    }
}

// ---- runtime-not-ready banner (kept from the old Design body) ----
static void drawRuntimeBanner(AppState& st) {
    if (st.workerStarted) return;
    // The in-process C++ core (libaicad) is a complete backend on its own (doc/agent/sim route
    // through it), so don't nag about the Python runtime when it's loaded.
    if (core::cpp_core().available()) return;
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.setupWarnBg[0], pal.setupWarnBg[1], pal.setupWarnBg[2], pal.setupWarnBg[3]));
    ImGui::BeginChild("setup", ImVec2(0, 0), ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    if (st.setupRunning.load()) {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Setting up runtime...");
        dimText("First run installs the local CAD engine runtime (~once).");
    } else {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Runtime not ready");
#ifdef _WIN32
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
#else
        dimText("Place libaicad.so next to the AI-CAD executable, or set AI_CAD_LIBAICAD.");
#endif
        if (ImGui::Button("Try setup now")) st.startSetup(false);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
}

// Build a compact "Provider · model" label for the composer's model chip.
static void composerModelLabel(const Settings& s, char* out, size_t n) {
    // Keep in lockstep with ui::ProviderIndex (Anthropic..Cursor).
    const char* provNames[] = {
        "Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API", "xAI", "Cursor"
    };
    static_assert(sizeof(provNames) / sizeof(provNames[0]) == ui::PROV_COUNT,
                  "composerModelLabel provNames must match PROV_COUNT");
    const char* prov = provNames[(s.provider >= 0 && s.provider < ui::PROV_COUNT)
                                     ? s.provider : ui::PROV_GEMINI];
    std::snprintf(out, n, "%s \xc2\xb7 %s", prov, s.model[0] ? s.model : "default");
}

// Context-window meter for the composer prelude row: a small fill-bar + "N/M" readout (compact
// "ctx N/M" with no bar when the pane is too narrow). Drawn at the current cursor; advances it.
// Moved here from the footer chips, where it was pinned to the pane bottom against the status bar.
static void drawContextMeter(AppState& st, float roomForMeter) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const int   win  = effectiveContextWindow(st.settings);
    const int   used = st.lastContextTokens;
    const float frac = win > 0 ? std::min(1.0f, (float)used / (float)win) : 0.0f;
    const float barW = 40 * sc, gap = 5 * sc;
    const std::string full = fmtTokens(used) + "/" + fmtTokens(win);
    const std::string lean = "ctx " + full;       // standalone (no bar) keeps a "ctx" tag so it reads
    const float fh = ImGui::GetFrameHeight();
    // Bar + readout only when there is room for both; uses the SAME roomForMeter the caller passed to
    // measureContextMeter() so the width it reserved and the layout drawn here agree exactly.
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    const bool  showBar = roomForMeter >= barW + gap + fullTw;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (showBar) {
        ImVec2 p = ImGui::GetCursorScreenPos();
        float by0 = p.y + fh * 0.5f - 3 * sc, by1 = p.y + fh * 0.5f + 3 * sc;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImU32 fg = frac < 0.5f ? ui::Palette::toU32(pal.statusOk)
                 : frac < 0.85f ? ui::Palette::toU32(pal.statusWarn)
                                : ui::Palette::toU32(pal.statusErr);
        dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW, by1),
                          ui::Palette::toU32(pal.accentSoft), 2 * sc);
        if (frac > 0)
            dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW * frac, by1), fg, 2 * sc);
        dl->AddRect(ImVec2(p.x, by0), ImVec2(p.x + barW, by1),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), 2 * sc);
        ImGui::Dummy(ImVec2(barW, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showBar ? full.c_str() : lean.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Context window usage — the last model call's input tokens vs the "
                          "active model's window (%d / %d).\nSet the window in AI settings if "
                          "your local model differs.", used, win);
}

// Width drawContextMeter() will occupy, so the caller can right-align it on its line above the
// composer. Mirrors the showBar decision in drawContextMeter() so reserved width == drawn width.
static float measureContextMeter(AppState& st, float roomForMeter) {
    const float sc = ui::uiScale;
    const float barW = 40 * sc, gap = 5 * sc;
    const std::string full = fmtTokens(st.lastContextTokens) + "/" +
                             fmtTokens(effectiveContextWindow(st.settings));
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    if (roomForMeter >= barW + gap + fullTw) return barW + gap + fullTw;
    return ImGui::CalcTextSize(("ctx " + full).c_str()).x;
}

// Prelude row above the prompt: Design/Edit/Ask dropdown (left) + context meter (right, same row).
// Permissions live only in the Settings overlay (not the composer).
static void drawComposerPrelude(AppState& st, bool /*busy*/) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool hasDoc = st.docMirror.hasDoc();
    const ui::AdaptiveSurface& surf = chatSurf();

    // Compact frame height matching the toolbar chips (model / thinking / New).
    // Arrow button uses ImGuiCol_Button — keep it identical to FrameBg so the caret
    // well is not shaded differently from the combo body.
    const ImVec4 frameBg(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.08f);
    const ImVec4 frameHov(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.12f);
    const ImVec4 frameAct(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.16f);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8.0f * sc, 3.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
    ImGui::PushStyleColor(ImGuiCol_Button, frameBg);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, frameHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, frameAct);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, frameBg);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, frameHov);
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, frameAct);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]));

    // Design / Edit / Ask — combo (single active mode). Writes st.aiInteraction.
    const char* modes[] = {"Design", "Edit", "Ask"};
    const char* modeTips[] = {"Design — model a new part from a description.",
                              "Edit — change the open part.",
                              "Ask — questions about the part; nothing changes."};
    int mode = (st.aiInteraction >= 0 && st.aiInteraction <= 2) ? st.aiInteraction : 0;
    // Width from longest label so the box doesn't clip text at any scale.
    float modeW = 0.0f;
    for (const char* m : modes)
        modeW = ImMax(modeW, ImGui::CalcTextSize(m).x);
    modeW += ImGui::GetFrameHeight() + 12.0f * sc;  // caret + compact padding
    ImGui::SetNextItemWidth(modeW);
    if (ImGui::BeginCombo("##aimode", modes[mode], ImGuiComboFlags_HeightSmall)) {
        for (int i = 0; i < 3; ++i) {
            const bool needDoc = (i == 1 && !hasDoc);
            if (needDoc) ImGui::BeginDisabled();
            const bool sel = (mode == i);
            if (ImGui::Selectable(modes[i], sel) && !needDoc) {
                st.aiInteraction = i;
            }
            if (sel) ImGui::SetItemDefaultFocus();
            if (needDoc) ImGui::EndDisabled();
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("%s", needDoc ? "Edit — open a part first." : modeTips[i]);
        }
        ImGui::EndCombo();
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", modeTips[mode]);

    // Context meter on the SAME row, right-aligned.
    ImGui::SameLine();
    const float room = ImGui::GetContentRegionAvail().x;
    const float w    = measureContextMeter(st, room);
    const float x    = ImGui::GetContentRegionMax().x - w;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    drawContextMeter(st, room);

    ImGui::PopStyleColor(8);
    ImGui::PopStyleVar(2);
    // Tight gap to the prompt — prelude sits closer to the input box.
}

// ============================ Subscription usage box (copilot) ============================
// Compact account/plan + per-window usage bars for the ACTIVE provider. Polls
// subscription_usage every 5 min; file-static cache (Main: do not touch app_state.h).
// Degrade: hide permanently on unknown_method; xAI shows plan chip + dim "no usage API".

namespace {

struct UsageWindow {
    std::string window;
    std::string label;
    float usedFraction = -1.0f;  // <0 = unknown
    std::string status;          // ok|warn|blocked|unknown
};

struct UsageCache {
    bool available = true;   // false after unknown_method
    bool loading = false;
    bool hasRow = false;     // true when we have at least identity/plan for active provider
    double lastFetch = -1e9;
    std::string lastProvider;  // friendly id last requested
    std::string displayName;
    std::string plan;
    std::string accountId;
    std::string dashboardUrl;
    std::string source;        // api|jwt_inference|unavailable|...
    std::vector<UsageWindow> windows;
};
static UsageCache s_usage;

// Map settings.provider int → subscription_usage friendly id (codex|claude|cursor|xai).
// Non-subscription providers return empty → box hidden.
static std::string usageFriendlyProvider(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC: return "claude";
        case ui::PROV_OPENAI:    return "codex";  // ChatGPT/Codex sub
        case ui::PROV_XAI:       return "xai";
        case ui::PROV_CURSOR:    return "cursor";
        default: return {};
    }
}

static bool usageIsAbsent(const core::RpcResult& r) {
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
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented")
            return true;
    }
    return false;
}

static void usageFetch(AppState& st, const std::string& provider, bool force) {
    if (!s_usage.available || s_usage.loading || provider.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && provider == s_usage.lastProvider && (now - s_usage.lastFetch) < 300.0)
        return;
    s_usage.loading = true;
    s_usage.lastFetch = now;
    s_usage.lastProvider = provider;
    nlohmann::json p = nlohmann::json::object();
    p["provider"] = provider;
    st.rpc.call("subscription_usage", p, [provider](const core::RpcResult& r) {
        s_usage.loading = false;
        if (usageIsAbsent(r)) {
            s_usage.available = false;
            s_usage.hasRow = false;
            s_usage.windows.clear();
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            // Soft fail — keep prior cache, don't hide forever.
            return;
        }
        s_usage.displayName.clear();
        s_usage.plan.clear();
        s_usage.accountId.clear();
        s_usage.dashboardUrl.clear();
        s_usage.source.clear();
        s_usage.windows.clear();
        s_usage.hasRow = false;
        auto rowsIt = r.result.find("rows");
        if (rowsIt == r.result.end() || !rowsIt->is_array()) return;
        for (const auto& row : *rowsIt) {
            if (!row.is_object()) continue;
            const std::string rp = row.value("provider", std::string());
            if (!rp.empty() && rp != provider && rp != provider + "-sub") continue;
            s_usage.hasRow = true;
            s_usage.displayName = row.value("display_name", row.value("email", std::string()));
            s_usage.plan = row.value("plan", std::string());
            s_usage.accountId = row.value("account_id", std::string());
            s_usage.dashboardUrl = row.value("dashboard_url", std::string());
            s_usage.source = row.value("source", std::string());
            if (row.contains("usage") && row["usage"].is_array()) {
                for (const auto& u : row["usage"]) {
                    if (!u.is_object()) continue;
                    UsageWindow w;
                    w.window = u.value("window", std::string());
                    w.label = u.value("label", w.window);
                    if (u.contains("used_fraction") && u["used_fraction"].is_number())
                        w.usedFraction = (float)u["used_fraction"].get<double>();
                    w.status = u.value("status", std::string("unknown"));
                    s_usage.windows.push_back(std::move(w));
                }
            }
            break;  // first matching active row
        }
    });
}

// Compact usage strip above the model picker. Hidden when RPC absent or provider has no sub.
static void drawCopilotUsageBox(AppState& st) {
    if (!s_usage.available) return;
    const std::string prov = usageFriendlyProvider(st.settings.provider);
    if (prov.empty()) return;
    usageFetch(st, prov, /*force=*/false);
    // Nothing to show until first successful row (or xAI plan-only degrade).
    if (!s_usage.hasRow && !s_usage.loading) {
        // Still show a one-shot probe result for xAI once fetch returns empty row with no plan —
        // only paint when we have something.
        return;
    }
    if (!s_usage.hasRow) return;

    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;

    ImGui::Dummy(ImVec2(0, 2.0f * sc));
    ImGui::BeginGroup();
    // Line 1: display_name · plan chip
    {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (!s_usage.displayName.empty()) {
            ImGui::TextUnformatted(s_usage.displayName.c_str());
            if (!s_usage.plan.empty()) ImGui::SameLine(0, 6.0f * sc);
        }
        ImGui::PopStyleColor();
        if (!s_usage.plan.empty()) {
            const ImVec2 p0 = ImGui::GetCursorScreenPos();
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextUnformatted(s_usage.plan.c_str());
            ImGui::PopStyleColor();
            const ImVec2 p1 = ImGui::GetItemRectMax();
            ImGui::GetWindowDrawList()->AddRect(
                ImVec2(p0.x - 3.0f * sc, p0.y - 1.0f * sc),
                ImVec2(p1.x + 3.0f * sc, p1.y + 1.0f * sc),
                ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                      surf.textPrimary.z, 0.30f)),
                ui::metrics::rounding(4.0f), 0, ui::metrics::stroke(1.0f));
        }
    }
    // Line 2: usage bars, OR a one-shot toast when usage API is absent (no in-composer spam).
    if (s_usage.windows.empty()) {
        static std::string s_usageToastKey;
        std::string key = prov + "|" + s_usage.source + "|" + s_usage.plan + "|" + s_usage.accountId;
        if (s_usageToastKey != key) {
            s_usageToastKey = key;
            if (prov == "xai") {
                st.toast(ui::ToastLevel::Warn,
                         "no usage API — check grok.com Settings \xe2\x86\x92 Usage");
            } else if (s_usage.source == "unavailable" || s_usage.source.empty()) {
                st.toast(ui::ToastLevel::Warn, "usage unavailable");
            }
            // "no usage windows" with a real source is quiet (no toast spam).
        }
    } else {
        const float barH = 4.0f * sc;
        const float barW = std::max(40.0f * sc, ImGui::GetContentRegionAvail().x * 0.45f);
        for (const auto& w : s_usage.windows) {
            const char* lab = w.label.empty() ? w.window.c_str() : w.label.c_str();
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted(lab);
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 6.0f * sc);
            const float frac = w.usedFraction < 0.0f ? 0.0f
                               : (w.usedFraction > 1.0f ? 1.0f : w.usedFraction);
            ImVec4 fill = ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f);
            if (w.status == "warn")
                fill = semanticOnGlass(pal.errCad, surf);
            else if (w.status == "blocked")
                fill = semanticOnGlass(pal.statusErr, surf);
            ImGui::PushStyleColor(ImGuiCol_PlotHistogram, fill);
            ImGui::PushStyleColor(ImGuiCol_FrameBg,
                ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.10f));
            char overlay[16] = "";
            if (w.usedFraction >= 0.0f)
                std::snprintf(overlay, sizeof(overlay), "%.0f%%", frac * 100.0f);
            ImGui::ProgressBar(w.usedFraction < 0.0f ? 0.0f : frac, ImVec2(barW, barH),
                               w.usedFraction < 0.0f ? "" : overlay);
            ImGui::PopStyleColor(2);
        }
    }
    ImGui::EndGroup();
    ImGui::Dummy(ImVec2(0, 2.0f * sc));
}

}  // namespace
// Toolbar row below the prompt: [model | thinking | paperclip | context-cube | New]
// Graduated widths with overlapping rounded corners (connected segmented look).
// Model picker is the largest share; paperclip/cube are icon-only minimums.
// The send/stop control lives inside the input box (drawComposerSendStop).
static void drawComposerToolbar(AppState& st, bool busy, bool embedded = false) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();

    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    ImDrawList* bgDl = ImGui::GetWindowDrawList();
    if (embedded) {
        // Hairline divider from the surface's own ink so it reads on any backdrop.
        bgDl->AddLine(ImVec2(rowMin.x + 8.0f * sc, rowMin.y),
                      ImVec2(rowMin.x + availW - 8.0f * sc, rowMin.y),
                      ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textTertiary.x,
                          surf.textTertiary.y, surf.textTertiary.z, 0.40f)),
                      ui::metrics::stroke(1.0f));
    } else {
        const float rowH = ImGui::GetFrameHeight() + 4 * sc;
        ui::glassPanel(bgDl, ImRect(rowMin, ImVec2(rowMin.x + availW, rowMin.y + rowH)),
                       ui::GlassLevel::Thin, 4.0f, surf);
    }

    // Compact chip height matches the Design combo above.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * sc, 3 * sc));
    // Negative spacing so adjacent rounded chips overlap into a segmented strip.
    const float overlap = -2.0f * sc;
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(overlap, ImGui::GetStyle().ItemSpacing.y));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
    // Shared translucent chip fill (opaque fills fought the adaptive text).
    const ImVec4 chipBg(0, 0, 0, 0);
    const ImVec4 chipHov(
        pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.55f);
    const ImVec4 chipAct(
        pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.60f);
    ImGui::PushStyleColor(ImGuiCol_Button, chipBg);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, chipHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, chipAct);

    // Thinking effort — ALWAYS present. Show just the level (no "Thinking: " prefix)
    // so the model label has room to extend left.
    Settings& s = st.settings;
    const char* effLabels[8] = {};
    const char* effValues[8] = {};
    int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
    if (nEff <= 0) {
        static const char* kFbLabels[] = {"Low", "Medium", "High"};
        static const char* kFbValues[] = {"low", "medium", "high"};
        for (int i = 0; i < 3; ++i) { effLabels[i] = kFbLabels[i]; effValues[i] = kFbValues[i]; }
        nEff = 3;
    }
    int effIdx = -1;
    for (int i = 0; i < nEff; ++i)
        if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
    const char* effBtn = effIdx >= 0 ? effLabels[effIdx] : "Default";

    // Graduated segment widths (left→right: model, thinking, paperclip, cube, New).
    // Icon chips stay near-square; New < thinking < model share the remaining row.
    const int nSeg = 5;
    const float segH = ImGui::GetFrameHeight();
    const float iconSz = segH;
    const float join = -overlap;  // positive join thickness per boundary
    const float totalW = availW + join * (float)(nSeg - 1);
    const float iconW = segH;     // minimal — just an icon
    const float restW = std::max(0.0f, totalW - iconW * 2.0f);
    // Relative shares of the non-icon remainder.
    const float kNew = 1.25f, kThink = 1.75f, kModel = 3.40f;
    const float kSum = kNew + kThink + kModel;
    const float newW   = restW * (kNew   / kSum);
    const float thinkW = restW * (kThink / kSum);
    const float modelW = restW * (kModel / kSum);

    char modelLabel[160];
    composerModelLabel(st.settings, modelLabel, sizeof(modelLabel));
    {
        // Ellipsize model text to fit inside its (largest) segment. No caret/arrow.
        const float maxModelW = modelW - ImGui::GetStyle().FramePadding.x * 2.0f;
        if (maxModelW > 0.0f && ImGui::CalcTextSize(modelLabel).x > maxModelW) {
            const float dotsW = ImGui::CalcTextSize("...").x;
            size_t len = std::strlen(modelLabel);
            while (len > 1 &&
                   ImGui::CalcTextSize(modelLabel, modelLabel + len).x + dotsW > maxModelW) {
                --len;
                while (len > 1 && (modelLabel[len] & 0xC0) == 0x80) --len;  // keep UTF-8 whole
            }
            std::snprintf(modelLabel + len, sizeof(modelLabel) - len, "...");
        }
    }

    // ---- 1/5 model (biggest, no caret) ----
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (ImGui::Button(modelLabel, ImVec2(modelW, segH))) ImGui::OpenPopup("##modeldropdown");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Model — click to search and switch.");
    // Capture the model chip rect so the drop-up popup can anchor fixed above it.
    const ImVec2 modelBtnMin = ImGui::GetItemRectMin();
    const ImVec2 modelBtnMax = ImGui::GetItemRectMax();

    // ---- 2/5 thinking effort (level only; bigger than New) ----
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (ImGui::Button(effBtn, ImVec2(thinkW, segH))) ImGui::OpenPopup("##cmpeffort");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Thinking effort for the selected model.");
    if (ImGui::BeginPopup("##cmpeffort")) {
        for (int i = 0; i < nEff; ++i) {
            if (ImGui::Selectable(effLabels[i], effIdx == i)) {
                std::strncpy(s.reasoningEffort, effValues[i], sizeof(s.reasoningEffort) - 1);
                saveSettings(st.settings);   // the core reads config.json — persist immediately
            }
        }
        ImGui::EndPopup();
    }

    // ---- 3/5 paperclip (icon-only, smallest) ----
    ImGui::SameLine();
    {
        if (ImGui::Button("##attach", ImVec2(iconW, segH))) {
            if (std::getenv("AI_CAD_NO_FILE_DIALOGS")) {
                st.toast(ui::ToastLevel::Info,
                         "File dialogs are disabled (AI_CAD_NO_FILE_DIALOGS).");
            } else {
                std::string p = openFileDialog("All files (*.*)\0*.*\0");
                if (!p.empty() &&
                    std::find(st.pendingAttachments.begin(), st.pendingAttachments.end(),
                              p) == st.pendingAttachments.end())
                    st.pendingAttachments.push_back(p);
            }
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Attach a file — it rides your next message (small text "
                              "files are inlined; other files are passed by path).");
        {
            ImDrawList* adl = ImGui::GetWindowDrawList();
            const ImVec2 amn = ImGui::GetItemRectMin(), amx = ImGui::GetItemRectMax();
            const ImVec2 ac((amn.x + amx.x) * 0.5f, (amn.y + amx.y) * 0.5f);
            const float isz = iconSz * 0.55f;
            IconUV clip = ui::getIcon("paperclip", 20);
            if (clip.valid) {
                const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
                adl->AddImage(clip.texId,
                              ImVec2(ac.x - isz * 0.5f, ac.y - isz * 0.5f),
                              ImVec2(ac.x + isz * 0.5f, ac.y + isz * 0.5f),
                              clip.uv0, clip.uv1, ink);
            } else {
                // Fallback vector paperclip if the atlas cell isn't registered yet.
                const float ph = iconSz * 0.24f;
                const float pr = iconSz * 0.13f;
                const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
                const float th = ui::metrics::stroke(1.4f);
                constexpr float kPi = 3.14159265f;
                adl->PathClear();
                adl->PathLineTo(ImVec2(ac.x + pr, ac.y - ph));
                adl->PathLineTo(ImVec2(ac.x + pr, ac.y + ph * 0.55f));
                adl->PathArcTo(ImVec2(ac.x, ac.y + ph * 0.55f), pr, 0.0f, kPi);
                adl->PathLineTo(ImVec2(ac.x - pr, ac.y - ph * 0.55f));
                adl->PathArcTo(ImVec2(ac.x, ac.y - ph * 0.55f), pr, kPi, 2.0f * kPi);
                adl->PathStroke(ink, 0, th);
                adl->AddLine(ImVec2(ac.x, ac.y - ph * 0.45f), ImVec2(ac.x, ac.y + ph * 0.45f),
                             ink, th);
            }
        }
    }

    // ---- 4/5 context-cube (icon-only, smallest) ----
    ImGui::SameLine();
    {
        const bool armed = st.contextPickActive;
        // Armed state: soft accent fill (selection-well language).
        if (armed) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.28f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.40f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.50f));
        }
        if (ImGui::Button("##ctxcube", ImVec2(iconW, segH))) {
            st.contextPickActive = !st.contextPickActive;
        }
        if (armed) ImGui::PopStyleColor(3);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(armed
                ? "Finish picking (Esc) — chips ride your next message."
                : "Add model context — pick parts, faces, edges, sketches or features.");
        // Draw cube glyph (atlas) or a simple wireframe cube fallback.
        {
            ImDrawList* cdl = ImGui::GetWindowDrawList();
            const ImVec2 cmn = ImGui::GetItemRectMin(), cmx = ImGui::GetItemRectMax();
            const ImVec2 cc((cmn.x + cmx.x) * 0.5f, (cmn.y + cmx.y) * 0.5f);
            const float isz = iconSz * 0.55f;
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
            IconUV cube = ui::getIcon("context_cube", 20);
            if (cube.valid) {
                cdl->AddImage(cube.texId,
                              ImVec2(cc.x - isz * 0.5f, cc.y - isz * 0.5f),
                              ImVec2(cc.x + isz * 0.5f, cc.y + isz * 0.5f),
                              cube.uv0, cube.uv1, ink);
            } else {
                // Wireframe cube fallback (isometric-ish).
                const float s = isz * 0.42f;
                const float d = s * 0.45f;
                const ImVec2 f0(cc.x - s, cc.y - s * 0.25f);
                const ImVec2 f1(cc.x + s * 0.35f, cc.y - s * 0.25f);
                const ImVec2 f2(cc.x + s * 0.35f, cc.y + s * 0.75f);
                const ImVec2 f3(cc.x - s, cc.y + s * 0.75f);
                const ImVec2 b0(f0.x + d, f0.y - d);
                const ImVec2 b1(f1.x + d, f1.y - d);
                const ImVec2 b2(f2.x + d, f2.y - d);
                const float th = ui::metrics::stroke(1.3f);
                cdl->AddLine(f0, f1, ink, th); cdl->AddLine(f1, f2, ink, th);
                cdl->AddLine(f2, f3, ink, th); cdl->AddLine(f3, f0, ink, th);
                cdl->AddLine(b0, b1, ink, th); cdl->AddLine(b1, b2, ink, th);
                cdl->AddLine(f0, b0, ink, th); cdl->AddLine(f1, b1, ink, th);
                cdl->AddLine(f2, b2, ink, th);
            }
            if (armed) {
                cdl->AddRect(cmn, cmx,
                             ImGui::ColorConvertFloat4ToU32(
                                 ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f)),
                             ui::metrics::rounding(4.0f), 0, ui::metrics::stroke(1.5f));
            }
        }
    }

    // ---- 5/5 New session (slightly bigger than icon chips) ----
    ImGui::SameLine();
    {
        if (busy) ImGui::BeginDisabled();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        if (ImGui::Button("New", ImVec2(newW, segH))) {
            newConversation(st);
            st.prompt[0] = 0;
            st.pendingAttachments.clear();
            st.pendingContextRefs.clear();
            st.contextPickActive = false;
            st.showAISettingsOverlay = false;
        }
        ImGui::PopStyleColor();
        if (busy) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Start a fresh conversation.");
    }

    ImGui::PopStyleColor(3);   // button base / hovered / active
    ImGui::PopStyleVar(3);     // FramePadding + ItemSpacing + FrameRounding

    // Fixed drop-UP model picker: anchor the popup's BOTTOM-LEFT just above the model chip so
    // options propagate upward (composer sits at the bottom). ImGuiCond_Always + NoMove keep it
    // fixed to the chip rather than jumping with default below-item placement.
    {
        const float popW = std::max(180.0f * sc,
                                    std::min(300.0f * sc, ImGui::GetWindowWidth() - 16.0f * sc));
        const float gap = 4.0f * sc;
        ImGui::SetNextWindowSizeConstraints(ImVec2(popW, 0), ImVec2(popW, FLT_MAX));
        ImGui::SetNextWindowPos(ImVec2(modelBtnMin.x, modelBtnMin.y - gap),
                               ImGuiCond_Always, ImVec2(0.0f, 1.0f));
        (void)modelBtnMax;
    }
    if (ImGui::BeginPopup("##modeldropdown",
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_AlwaysAutoResize)) {
        drawModelDropdown(st, busy);
        ImGui::EndPopup();
    }
}

// Is the assistant currently working a turn? Covers every run path: job-based design/edit, the
// async ask/sim calls (which set the back message running without a job), and any streaming
// message. Drives the composer's send<->stop swap and the message queue.
static bool aiTurnActive(AppState& st) {
    if (st.job.running.load() || st.agentV2Running) return true;
    if (!st.chat.empty()) {
        const ChatMsg& b = st.chat.back();
        if (!b.fromUser && b.running) return true;
    }
    return false;
}

// Stop the in-flight run. The worker RPC can't be interrupted mid-flight (one blocking call per
// WorkerClient, serialized), so this is a SOFT stop: finalize the active message as stopped and,
// for a job-based run, flag the drain to discard the result when the background thread lands. The
// model may finish its current step in the background; we just stop listening.
static void requestStopRun(AppState& st) {
    // Only a job-based run produces a result the drain must discard. Async ask/sim runs land via
    // their own callbacks, which look for a *running* message — clearing it below makes them no-op.
    if (st.job.running.load() || st.job.ready.load()) st.stopRequested = true;
    st.agentV2Running = false;
    st.livePhase.clear();
    st.agentV2Status.clear();
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) {
            it->running = false;
            it->liveStatus.clear();
            it->errKind = ERR_CAD;  // a user stop is not an app error
            if (it->text.empty())
                it->text = "Stopped. The model may finish its current step in the background.";
            else
                it->text += "\n\n(Stopped by you.)";
            break;
        }
    st.status = st.job.running.load() ? "Stopping..." : "Stopped.";
    st.statusKind = 1;
    st.addLog("[..] stop requested by user");
}

// The send/stop control, drawn INSIDE the message input box at its bottom-right (flagship
// composer pattern — the input reserves right padding so text never runs under it). Idle: a
// rounded accent button with an enter/return-key glyph (Enter submits). During a turn the same
// button becomes the stop control (square glyph, statusErr fill).
static void drawComposerSendStop(AppState& st, const ImVec2& inMin, const ImVec2& inMax,
                                 float rightEdgeX, bool canRun, bool canStart, bool active,
                                 bool& submit, bool& stopReq) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float sz = ImGui::GetFrameHeight();
    const float inset = 6.0f * sc;   // clear of the composer's rounded border
    // Sit fully inside the input: keep a bottom inset so the button never intersects the
    // toolbar's New chip below the box. Short (one-row) inputs still center vertically.
    const float bottomClear = 6.0f * sc;
    float y0 = inMax.y - sz - bottomClear;
    if (y0 < inMin.y + inset) y0 = (inMin.y + inMax.y - sz) * 0.5f;
    const ImVec2 bMin(rightEdgeX - inset - sz, y0);
    const ImVec2 bMax(bMin.x + sz, bMin.y + sz);

    const ImVec2 keep = ImGui::GetCursorPos();
    ImGui::SetCursorScreenPos(bMin);
    const bool clicked = ImGui::InvisibleButton("##cp_sendstop", ImVec2(sz, sz));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float round = ui::metrics::rounding(7.0f);
    const ImVec2 c((bMin.x + bMax.x) * 0.5f, (bMin.y + bMax.y) * 0.5f);

    if (active) {
        // Stop: statusErr fill with derived hover weight (no color literals).
        ImVec4 fill(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1);
        if (hov) fill = ImVec4(std::min(fill.x * 1.12f, 1.0f), std::min(fill.y * 1.12f, 1.0f),
                               std::min(fill.z * 1.12f, 1.0f), 1);
        dl->AddRectFilled(bMin, bMax, ImGui::ColorConvertFloat4ToU32(fill), round);
        ImVec4 stopInk = ui::maxContrastTextOn(fill);
        stopInk.w = 0.92f;
        const float h = sz * 0.18f;
        dl->AddRectFilled(ImVec2(c.x - h, c.y - h), ImVec2(c.x + h, c.y + h),
                          ImGui::ColorConvertFloat4ToU32(stopInk), 1.5f * sc);
        if (hov)
            ImGui::SetTooltip(st.stopRequested ? "Stopping\xE2\x80\xA6 (finishing current step)"
                                               : "Stop the current run");
        if (clicked) stopReq = true;
    } else {
        const ImVec4 accC(pal.accent[0], pal.accent[1], pal.accent[2], 1);
        const ImVec4 fill = canStart
            ? (hov ? ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1) : accC)
            : ImVec4(pal.button[0], pal.button[1], pal.button[2], 1);
        dl->AddRectFilled(bMin, bMax, ImGui::ColorConvertFloat4ToU32(fill), round);
        // Enter/return-key glyph: stem down from the top-right into a horizontal run ending in a
        // left-pointing arrowhead. Ink pairs with the button's own fill (same rule as before).
        const ImU32 ink = canStart
            ? ImGui::ColorConvertFloat4ToU32(ui::maxContrastTextOn(accC))
            : ui::Palette::toU32(pal.textDim);
        const float g  = sz * 0.24f;
        const float by = c.y + g * 0.55f;              // the horizontal run's y
        const float th = ui::metrics::stroke(1.6f);
        dl->PathClear();
        dl->PathLineTo(ImVec2(c.x + g, c.y - g));      // stem top (right side)
        dl->PathLineTo(ImVec2(c.x + g, by));           // down
        dl->PathLineTo(ImVec2(c.x - g, by));           // across to the left
        dl->PathStroke(ink, 0, th);
        const float a = g * 0.62f;                     // arrowhead, pointing LEFT
        dl->AddLine(ImVec2(c.x - g, by), ImVec2(c.x - g + a, by - a), ink, th);
        dl->AddLine(ImVec2(c.x - g, by), ImVec2(c.x - g + a, by + a), ink, th);
        if (hov)
            ImGui::SetTooltip(canStart ? "Send  (Enter)"
                              : canRun ? "Type a message first."
                                       : "Runtime/worker not ready yet.");
        if (clicked && canStart) submit = true;
    }
    ImGui::SetCursorPos(keep);
}

// ---- the composer: a rounded input box (in-box send) + inline toolbar (model, thinking, +) ----
// Submits on Enter or the in-box send button. Returns nothing; runs the turn directly.
static void drawComposer(AppState& st, bool busy, bool canRun) {
    const bool hasDoc = st.docMirror.hasDoc();

    // Keep the orchestrator flag synced to the mode (Edit == validated document ops on the doc).
    if (st.aiInteraction == 1 && !hasDoc) st.aiInteraction = 0;
    st.agentV2 = (st.aiInteraction == 1);

    // Esc: staged edit restore, else disarm context pick (Wave4).
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        if (s_edit.active) {
            cancelEditStage(st);
        } else if (st.contextPickActive) {
            st.contextPickActive = false;
        }
    }

    // Drain the message queue: once the assistant is free, auto-send the next queued message
    // (Claude-Code-style). Done before computing `active` so the new turn shows as running now.
    if (!aiTurnActive(st) && !st.queuedPrompts.empty() && canRun) {
        std::string next = st.queuedPrompts.front();
        st.queuedPrompts.erase(st.queuedPrompts.begin());
        // Queued sends never commit a staged edit (stage should already be cleared).
        submitTurn(st, next);
    }

    const bool active = aiTurnActive(st);
    bool canStart = canRun && st.prompt[0] && !active;  // start now only when idle
    bool submit   = false;   // send / queue requested this frame
    bool stopReq  = false;   // stop requested this frame

    // ---- queued messages (shown above the composer, each removable) ----
    if (!st.queuedPrompts.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        for (size_t qi = 0; qi < st.queuedPrompts.size(); ++qi) {
            ImGui::PushID((int)qi + 9000);
            float xBtn = ImGui::GetFrameHeight();
            std::string& q = st.queuedPrompts[qi];
            std::string label = "Queued: " + (q.size() > 48 ? q.substr(0, 47) + "\xE2\x80\xA6" : q);
            ImGui::TextUnformatted(label.c_str());
            ImGui::SameLine(ImGui::GetContentRegionMax().x - xBtn);
            if (ImGui::SmallButton("x")) {
                st.queuedPrompts.erase(st.queuedPrompts.begin() + qi);
                ImGui::PopID();
                break;
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove from queue");
            ImGui::PopID();
        }
        ImGui::PopStyleColor();
    }

    // ---- chips above the input: file attachments (M23) + context refs (Wave4) ----
    if (!st.pendingAttachments.empty() || !st.pendingContextRefs.empty()) {
        const ui::AdaptiveSurface& asurf = chatSurf();
        const float sc = ui::uiScale;
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
        ImGui::PushStyleColor(ImGuiCol_Text, asurf.textPrimary);
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.08f));
        const float maxX = ImGui::GetContentRegionMax().x;
        bool anyDrawn = false;

        auto drawChipBorder = [&]() {
            ImGui::GetWindowDrawList()->AddRect(
                ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                ImGui::ColorConvertFloat4ToU32(ImVec4(asurf.textPrimary.x, asurf.textPrimary.y,
                                                      asurf.textPrimary.z, 0.35f)),
                ui::metrics::rounding(6.0f), 0, ui::metrics::stroke(1.0f));
        };
        auto placeChip = [&](float chipW) {
            if (anyDrawn) {
                ImGui::SameLine(0, 4 * sc);
                if (ImGui::GetCursorPosX() + chipW > maxX) ImGui::NewLine();
            }
            anyDrawn = true;
        };

        for (size_t ai = 0; ai < st.pendingAttachments.size(); ++ai) {
            ImGui::PushID((int)ai + 12000);
            std::string name = attachmentBaseName(st.pendingAttachments[ai]);
            if (name.size() > 30) name = name.substr(0, 29) + "\xE2\x80\xA6";
            const std::string lbl = name + "  \xC3\x97";   // "name  ×"
            const float chipW = ImGui::CalcTextSize(lbl.c_str()).x + 16 * sc;
            placeChip(chipW);
            const bool removed = ImGui::SmallButton(lbl.c_str());
            drawChipBorder();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s\nAttached — rides your next message. Click to remove.",
                                  st.pendingAttachments[ai].c_str());
            if (removed) {
                st.pendingAttachments.erase(st.pendingAttachments.begin() + ai);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }

        // Context-ref chips: kind letter + label + ×
        for (size_t ci = 0; ci < st.pendingContextRefs.size(); ++ci) {
            ImGui::PushID((int)ci + 14000);
            const auto& ref = st.pendingContextRefs[ci];
            char kindLetter = '?';
            if (!ref.kind.empty()) {
                if (ref.kind == "sketch") kindLetter = 'S';
                else if (ref.kind == "feature") kindLetter = 'F';
                else if (ref.kind == "component") kindLetter = 'C';
                else if (ref.kind == "body") kindLetter = 'B';
                else if (ref.kind == "face") kindLetter = 'A';
                else if (ref.kind == "edge") kindLetter = 'E';
                else kindLetter = (char)std::toupper((unsigned char)ref.kind[0]);
            }
            std::string name = ref.label.empty() ? (ref.kind.empty() ? "ref" : ref.kind) : ref.label;
            if (name.size() > 28) name = name.substr(0, 27) + "\xE2\x80\xA6";
            const std::string lbl = std::string(1, kindLetter) + "  " + name + "  \xC3\x97";
            const float chipW = ImGui::CalcTextSize(lbl.c_str()).x + 16 * sc;
            placeChip(chipW);
            const bool removed = ImGui::SmallButton(lbl.c_str());
            drawChipBorder();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s · %s\nContext reference — rides your next message. Click to remove.",
                                  ref.kind.c_str(),
                                  ref.label.empty() ? ref.id.c_str() : ref.label.c_str());
            if (removed) {
                st.pendingContextRefs.erase(st.pendingContextRefs.begin() + ci);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }

        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar();
    }

    // Armed pick hint (selection-well language) above the prelude.
    if (st.contextPickActive) {
        const ui::AdaptiveSurface& asurf = chatSurf();
        const float sc = ui::uiScale;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(asurf.textSecondary.x, asurf.textSecondary.y, asurf.textSecondary.z, 0.95f));
        ImGui::TextWrapped(
            "Pick parts, faces, edges, sketches or features to add to context \xE2\x80\x94 Esc to finish");
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * sc));
    }

    // ---- composer: prelude row -> transparent input -> toolbar row ----
    drawComposerPrelude(st, busy);

    const char* placeholder = st.aiInteraction == 2 ? "Ask about this part\xE2\x80\xA6"
                            : st.aiInteraction == 1 ? "Describe a change to this part\xE2\x80\xA6"
                                                    : "Describe what to build\xE2\x80\xA6";

    ImDrawList* composerDl = ImGui::GetWindowDrawList();
    ImDrawListSplitter composerSplit;
    const ImVec2 composerMin = ImGui::GetCursorScreenPos();
    const float composerW = ImGui::GetContentRegionAvail().x;
    composerSplit.Split(composerDl, 2);
    composerSplit.SetCurrentChannel(composerDl, 1);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushStyleColor(ImGuiCol_Border, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0.0f);
    // Minimal top pad so the Design dropdown + context meter sit close to the input.
    ImGui::Dummy(ImVec2(0, ui::metrics::U(0.25f)));
    // Reserve the in-box send button's footprint on the input's right so text never runs under it.
    const float sendSz    = ImGui::GetFrameHeight();
    const float sendInset = 6.0f * ui::uiScale;
    bool submitInput = drawChatInput("##aiprompt", st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines(),
                                     sendSz + sendInset * 2.0f);
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);
    // Send/stop rides INSIDE the input box, anchored bottom-right of the composer glass.
    drawComposerSendStop(st, ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                         composerMin.x + composerW, canRun, canStart, active, submit, stopReq);

    // No usage/provider id strip above the toolbar — model lives in the toolbar chip.
    drawComposerToolbar(st, busy, true);
    ImGui::PopStyleVar();

    const ImVec2 composerMax(composerMin.x + composerW, ImGui::GetItemRectMax().y);
    composerSplit.SetCurrentChannel(composerDl, 0);
    ui::glassPanel(composerDl, ImRect(composerMin, composerMax), ui::GlassLevel::Thin,
                  8.0f /*logical dp — glassPanel scales internally*/, surf);
    composerSplit.Merge(composerDl);

    // ---- stop / submit / queue ----
    if (stopReq) requestStopRun(st);
    if (submit || submitInput) {
        if (active) {
            // A turn is running — queue the message instead of starting concurrently. It auto-
            // sends when the assistant frees up (drain at the top of drawComposer).
            if (st.prompt[0]) {
                st.queuedPrompts.emplace_back(st.prompt.data());
                st.prompt[0] = 0;
            }
        } else if (canStart) {
            // Stage the rollback only on send (Esc cancelled without this).
            if (s_edit.active) commitEditStageOnSend(st);
            submitTurn(st, std::string(st.prompt.data()));
            st.prompt[0] = 0;
        }
    }
}

// ---- M19: endless scroll-back via render-windowing --------------------------------------------
// The full history is already in memory (persistChatHistory reload), so "lazy loading" here means
// lazily RENDERING: a message far outside the scroll viewport re-emits as a fixed-height spacer
// (the height measured the last time it was really drawn at this width) instead of running its
// full draw path. Per-frame cost is O(visible) real draws + O(total) trivial spacer loop — no
// crash and no unbounded frame cost on large histories. Interactive/animated messages (the
// running turn, an open clarify card) always render so nothing live is ever frozen out. Cached
// heights invalidate on width change (resize) and refresh every frame the message is visible
// (expand/collapse toggles re-measure naturally). Shared by the docked Copilot column and the AI
// workspace chat column.
static void drawChatLogWindowed(AppState& st) {
    ui::Fonts& fonts = st.fonts;
    const float logW    = ImGui::GetContentRegionAvail().x;
    const float viewTop = ImGui::GetScrollY();
    const float viewBot = viewTop + ImGui::GetWindowSize().y;
    const float slack   = ImGui::GetWindowSize().y;   // one extra viewport of buffer each side
    // 1-based index of finished assistant turns that mutated the doc — matches AI-VC turn labels.
    int mutTurn = 0;
    for (size_t mi = 0; mi < st.chat.size(); ++mi) {
        ChatMsg& m = st.chat[mi];
        ImGui::PushID((int)mi);
        const float y = ImGui::GetCursorPosY();
        const bool cacheValid = m.uiCachedH > 0.0f && std::fabs(m.uiCachedW - logW) < 0.5f;
        const bool interactive = m.running || m.clarify.active || m.clarify.awaiting ||
                                 (s_aiVc.confirmMsgIdx == (int)mi) ||
                                 (s_edit.active && s_edit.msgIdx == (int)mi);
        // Pre-count mutating assistant turns even when the row is off-screen (stable turn nums).
        int thisTurn = 0;
        if (!m.fromUser && chatMsgMutatedDoc(m)) {
            ++mutTurn;
            thisTurn = mutTurn;
        }
        if (cacheValid && !interactive &&
            (y + m.uiCachedH < viewTop - slack || y > viewBot + slack)) {
            ImGui::Dummy(ImVec2(logW, m.uiCachedH));   // same-height spacer, zero draw cost
            ImGui::PopID();
            continue;
        }
        ImGui::BeginGroup();
        if (m.fromUser) {
            drawUserBlock(st, m, fonts, logW, (int)mi);
        } else {
            drawAssistantTurn(st, m, fonts, logW, (int)mi, thisTurn);
            ImGui::Dummy(ImVec2(0, 8 * ui::uiScale));
        }
        ImGui::EndGroup();
        m.uiCachedH = ImGui::GetItemRectSize().y;
        m.uiCachedW = logW;
        ImGui::PopID();
    }
    chatDrawCopyFlash();
}


// ---- unified AI Copilot pane: header -> conversation -> bottom composer ----
// The pane is a PINNED, full-height, rounded glass column overlaid on the viewport's right edge
// (~12dp margins inside the area between ribbon and status bar) — the ONLY layout; the docked
// task-pane path is gone. Geometry comes from ui::overlayBand() (published per-frame by app.cpp;
// same source as the FeatureManager column), so the column tracks ribbon/status-bar/window
// resizes with no extra wiring. Width is user-resizable via an INVISIBLE hit-zone on the
// column's left edge (no visible splitter). Until the first backdrop is published (early frames)
// the window falls back to its default placement so nothing breaks headless.
void drawAIPanel(AppState& st, bool busy, bool canRun) {
    if (!st.showAIPanel) return;

    // Keep the local-endpoint model list fresh whenever the copilot panel exists — BEFORE the
    // visibility gate below, so discovery runs even when the pane is occluded. Without this, a
    // model loaded in LM Studio isn't discovered until the user opens the picker ("pokes around
    // in settings"). The probe is throttled to ~5s and no-ops for a non-loopback (cloud)
    // base_url, so this is cheap.
    refreshModelAvailability(st, false);

    static float s_paneW = 0.0f;                    // column width (px), session-persistent
    // s_cpCollapsed is file-scoped (shared with drawCopilotHeader's collapse chevron). Seed it
    // once from the persisted setting (T-G21: a collapsed Copilot column stays collapsed across
    // restarts) — can't do this at s_cpCollapsed's own file-scope initializer since that runs
    // before loadSettings, so seed lazily on this function's first call instead.
    static bool s_cpInited = false;
    if (!s_cpInited) { s_cpCollapsed = st.settings.copilotCollapsed; s_cpInited = true; }
    static ui::PanelCollapseAnim s_cpAnim;
    // Anchor to the overlay band app.cpp publishes each frame (ribbon bottom -> status-bar top),
    // inset by the doc-tab strip on top — the SAME geometry source as the FeatureManager column in
    // layout.cpp drawLeftPane, so the two pinned columns always share top/bottom edges. Falls back
    // to the main viewport's work rect for the pre-publish first frame.
    ui::OverlayBand band = ui::overlayBand();
    if (!band.valid) {
        const ImGuiViewport* mvp = ImGui::GetMainViewport();
        band.minX = mvp->WorkPos.x;
        band.minY = mvp->WorkPos.y;
        band.maxX = mvp->WorkPos.x + mvp->WorkSize.x;
        band.maxY = mvp->WorkPos.y + mvp->WorkSize.y;
        band.valid = band.maxX > band.minX && band.maxY > band.minY;
    }
    const bool overlay = band.valid;
    // ONE bubble radius shared by the window clip and the drawn glass (same rule as drawLeftPane):
    // the resolved Copilot rounding knob wins when set, else the 12dp bubble default.
    const ui::GlassStyleResolved& rsCp = ui::glassStyleResolved(ui::GlassGroup::Copilot);
    const float bubbleDp = (rsCp.rounding >= 0.f) ? rsCp.rounding : 12.0f;
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoBackground;
    if (overlay) {
        const float margin = ui::metrics::px(12.0f);
        // The doc-tab strip now floats over the viewport (fix 2), not above these columns, so the
        // Copilot column starts flush at the top of the viewport band — same rule as drawLeftPane,
        // keeping the two pinned columns sharing their top edge.
        const float colY = band.minY + 4.0f * ui::uiScale;   // hug the ribbon (moved up per design)
        const float colH = std::max(120.0f * ui::uiScale, band.maxY - margin - colY);
        // "Reset layout" (slot 1 = Copilot column) restores the default width + expands it.
        if (ui::takeOverlayColumnReset(1)) {
            s_paneW = 0.0f;
            s_cpCollapsed = false;
            st.settings.copilotCollapsed = false;
            saveSettings(st.settings);
        }
        if (s_paneW <= 0.0f) s_paneW = ui::metrics::dockRightTarget();
        if (std::getenv("AI_CAD_COPILOT_NARROW"))       // T-G17 shot: capture the narrowest column
            s_paneW = ui::metrics::dockRightMin();
        // T-G21 shot: mirrors AI_CAD_FM_COLLAPSED — --ui-screenshot always resets the overlay
        // columns for determinism (see the reset branch above), which clobbers the persisted
        // collapse setting before it can render; force it back for the collapsed capture.
        if (std::getenv("AI_CAD_COPILOT_COLLAPSED")) {
            s_cpCollapsed = true;
            s_cpAnim.snap(true, colH);  // snap immediately — headless captures must not depend on frame count
        }
        const float maxW = std::max((band.maxX - band.minX) * 0.5f,
                                    ui::metrics::dockRightMin());
        s_paneW = std::min(std::max(s_paneW, ui::metrics::dockRightMin()), maxW);

        // T-G21: vertical collapse — the column rolls UP to its header row (drawCopilotHeader)
        // instead of shrinking to a pill in both axes (mirrors the FeatureManager column; see
        // ui::PanelCollapseAnim). Width is unaffected by collapse.
        const float winH = s_cpAnim.tick(s_cpCollapsed, colH);
        ImGui::SetNextWindowPos(ImVec2(band.maxX - margin - s_paneW, colY), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(s_paneW, winH), ImGuiCond_Always);
        flags |= ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoScrollbar |
                 ImGuiWindowFlags_NoScrollWithMouse;
    }
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * ui::uiScale);
    const bool cpOpen = ImGui::Begin("Copilot###AI", nullptr, flags);
    ImGui::PopStyleVar();
    if (!cpOpen) { ImGui::End(); return; }
    ui::Fonts& fonts = st.fonts;

    // Liquid-Glass v2 host port (contract H5): ONE fully transparent glass surface for the whole
    // Copilot column, painted once here at panel top (id "host_agent_glass" via ui::panelId —
    // window-independent, so chatSurf() below returns the SAME cached surface for the header,
    // composer, and every card this frame; ui::adaptivePanel is idempotent per frame). Tagged
    // GlassGroup::Copilot in chatSurf(), so the user's Appearance knobs (tint/level/rounding/
    // opacity) ride in automatically. Falls back to the palette's glassBackground when no
    // viewport backdrop has been published yet.
    {
        ImVec2 wmin = ImGui::GetWindowPos();
        // The WHOLE-column glass slab is intentionally NOT painted anymore: the Copilot bounding box
        // is hidden (fully transparent) so the 3D viewport shows straight through with no misaligned
        // frosted refraction. Only the individual chat cards / header / composer / text carry their
        // own glass pills. chatSurf() is still resolved (idempotent per frame) so those elements
        // fetch the SAME contrast-matched text color as before.
        // Let the viewport HUD (result legend) dodge the pinned column (layout.h, slot 1).
        if (overlay) ui::publishOverlayColumnEdge(1, wmin.x);
    }

    // (Collapse chevron now leads the Copilot header row — see drawCopilotHeader — so it can't
    // overlap the cube the way a top-left corner chip did.)

    // Invisible left-edge resize hit-zone (design rule: no visible splitter bars). Dragging the
    // strip left widens the column; clamped at the top of the next frame.
    if (overlay) {
        const ImVec2 keep = ImGui::GetCursorPos();
        ImGui::SetCursorScreenPos(ImGui::GetWindowPos());
        ImGui::InvisibleButton("##copilot_resize",
                               ImVec2(ui::metrics::px(6.0f), ImGui::GetWindowSize().y));
        if (ImGui::IsItemHovered() || ImGui::IsItemActive())
            ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeEW);
        if (ImGui::IsItemActive()) s_paneW -= ImGui::GetIO().MouseDelta.x;
        ImGui::SetCursorPos(keep);
    }

    // focusSettingsTab opens the Settings modal AI tab (full form + leaderboard).
    // The in-column gear uses showAISettingsOverlay instead.
    if (st.focusSettingsTab) {
        st.openSettings     = true;
        st.openSettingsTab  = 2;
        st.focusSettingsTab = false;
    }

    // Keep the conversation set bound to the active document (swaps history on doc change). Must
    // run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
    // adopted rather than cleared.
    syncChatToDoc(st);

    // T-G17 screenshot hook: AI_CAD_COPILOT_VIEW forces a Copilot sub-view (and tops up a couple of
    // demo sessions) so the sessions/settings layouts can be captured headless in both themes. Runs
    // AFTER syncChatToDoc (which rebinds st.conversations to the doc) and re-tops-up each frame so
    // the demo rows survive to the capture frame. Screenshot-only — gated on the env var, no effect
    // in a normal run.
    if (const char* cpView = std::getenv("AI_CAD_COPILOT_VIEW")) {
        ensureConversations(st);
        st.conversations[0].title = "Knurled hex standoff";
        while (st.conversations.size() < 3) {
            Conversation cx; cx.id = newConvId(st);
            cx.title = (st.conversations.size() == 1) ? "Bracket + FEA study"
                                                      : "Gearbox housing revision";
            st.conversations.push_back(std::move(cx));
        }
        if (std::strcmp(cpView, "sessions") == 0) {
            st.showSessionsPicker = true;
            st.showAISettingsOverlay = false;
        } else if (std::strcmp(cpView, "settings") == 0) {
            st.showAISettingsOverlay = true;
            st.showSessionsPicker = false;
        }
    }

    ImGui::Dummy(ImVec2(0, ui::metrics::U(1.5f)));
    drawCopilotHeader(st, busy);
    ImGui::Separator();

    // T-G21: the title row + separator above IS the header strip a vertical collapse rolls up to
    // — measure it (cursor position right after, symmetric-padded) so PanelCollapseAnim knows the
    // exact collapsed target, same one-frame-lag trick as s_composerH below. Everything from the
    // runtime banner down is gated on bodyVisible() so a collapsed column costs nothing per frame.
    s_cpAnim.measureHeader();
    if (s_cpAnim.bodyVisible()) {
        drawRuntimeBanner(st);

        // Settings / Sessions overlays replace the chat log (and hide the composer) while open.
        const bool overlayOpen = st.showSessionsPicker || st.showAISettingsOverlay;
        static float s_composerH = 0.0f;
        const float bottomClearance = ui::metrics::U(5);
        float reserve = 0.0f;
        if (!overlayOpen) {
            reserve = (s_composerH > 0 ? s_composerH
                                       : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                      ui::metrics::U(2) + bottomClearance;
        } else {
            reserve = bottomClearance;
        }
        float chatH = ImGui::GetContentRegionAvail().y - reserve;
        if (chatH < ui::metrics::px(120.0f)) chatH = ui::metrics::px(120.0f);

        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_None);
        if (st.showAISettingsOverlay) {
            drawAISettingsBody(st, busy);
        } else if (st.showSessionsPicker) {
            drawSessionsPanel(st, busy);
        } else {
            drawChatLogWindowed(st);   // M19: render-windowed endless scroll-back
            if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 12)
                ImGui::SetScrollHereY(1.0f);
        }
        const bool  logScrolled = !overlayOpen && ImGui::GetScrollY() > 0.5f;
        const ImVec2 logP0 = ImGui::GetWindowPos();
        const float  logVisW = ImGui::GetWindowSize().x;
        ImGui::EndChild();

        // Top fade mask over the conversation scroll viewport.
        if (logScrolled) {
            const ImVec2 keep = ImGui::GetCursorPos();
            const float fadeH = ImGui::GetTextLineHeight() + ui::metrics::px(6.0f);
            ImGui::SetCursorScreenPos(logP0);
            ImGui::BeginChild("##chatlog_fade", ImVec2(logVisW, fadeH), ImGuiChildFlags_None,
                              ImGuiWindowFlags_NoInputs | ImGuiWindowFlags_NoScrollbar |
                              ImGuiWindowFlags_NoScrollWithMouse);
            const ui::AdaptiveSurface& fsurf = chatSurf();
            const ImU32 top = ImGui::ColorConvertFloat4ToU32(
                ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.92f));
            const ImU32 clear = ImGui::ColorConvertFloat4ToU32(
                ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.0f));
            ImGui::GetWindowDrawList()->AddRectFilledMultiColor(
                logP0, ImVec2(logP0.x + logVisW, logP0.y + fadeH), top, top, clear, clear);
            ImGui::EndChild();
            ImGui::SetCursorPos(keep);
        }
        ImGui::PopStyleColor();

        // Composer is hidden under Sessions / Settings overlays (same pattern as before for
        // sessions was "chat log only" — keep the composer under chat only).
        if (!overlayOpen) {
            ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
            ImGui::BeginGroup();
            drawComposer(st, busy, canRun);
            ImGui::EndGroup();
            s_composerH = ImGui::GetItemRectSize().y + ui::metrics::U(1);
            ImGui::Dummy(ImVec2(0, bottomClearance));
        } else {
            ImGui::Dummy(ImVec2(0, bottomClearance));
        }
    }  // s_cpAnim.bodyVisible()

    ImGui::End();
}

// ============================ AI workspace hub reuse hooks ============================
// Thin exported wrappers over the statics above (see legacy_chat.h). They let the full-page
// AI workspace (host/src/ui/ai/ai_workspace.cpp) render the SAME conversation state, session
// list and composer/send path as the docked Copilot column — single implementation, two hosts.

void chatWorkspaceSync(AppState& st) {
    syncChatToDoc(st);
    ensureConversations(st);
}

bool chatTurnActive(AppState& st) { return aiTurnActive(st); }

void chatDrawLog(AppState& st) {
    drawChatLogWindowed(st);   // M19: render-windowed endless scroll-back (shared with drawAIPanel)
}

void chatDrawComposer(AppState& st, bool busy, bool canRun) { drawComposer(st, busy, canRun); }

void chatDrawSessionsList(AppState& st, bool busy) { drawSessionsPanel(st, busy); }

}  // namespace ui
