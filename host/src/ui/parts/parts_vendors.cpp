// Parts Library — vendors (GUI V2 phase B): the old full right pane is gone. This TU owns
//   * drawVendorStrip  — compact fixed footer of the filters pane: status dots + "N/M signed
//     in", health summary, "Connect vendor accounts", "Vendors…" (popup) and "Test vendors".
//     ALL background polling (login watch, download queue, health) lives here so it keeps
//     running whether or not the popup is open.
//   * drawVendorsPopup — floating glass window with the vendor sign-in list, API-provider
//     credential editors, download queue and health-check results table.
//   * startHealthCheck/pollHealthStatus — vendors_health_check/_status flow (single-flight,
//     2 s backoff while running).
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/icons.h"      // hasIconSdf/getIcon/drawIconGlass — the CTA pill draws its own glyph
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

// Defined in parts_browser_panel.cpp with external linkage so the vendor pair shares
// one definition each. Deliberately NOT in parts_internal.h — they are private to
// these two TUs; promote them if a third ever needs them.
//   jstr: null-tolerant string read. nlohmann's value(key, "") THROWS type_error.302
//         on a present-but-NULL key, and an exception raised inside an st.rpc drain
//         callback aborts the GUI — every string read of a worker response goes
//         through this.
//   sitePrefersDesktopLogin: the one bot-check rule (Magento invisible reCAPTCHA
//         rejects the in-panel OSR CEF); it used to be duplicated inline here.
std::string jstr(const nlohmann::json& j, const char* key, const std::string& dflt);
bool sitePrefersDesktopLogin(const std::string& site);

// ---- health check flow -------------------------------------------------------------

void startHealthCheck(AppState& st) {
    if (!S().healthSupported || S().healthStarting || S().healthRunning) return;
    S().healthStarting = true;
    st.rpc.call("vendors_health_check", {}, [&st](const core::RpcResult& r) {
        S().healthStarting = false;
        if (!r.ok) {
            if (r.err.find("unknown method") != std::string::npos) {
                S().healthSupported = false;
            } else {
                st.toast(ui::ToastLevel::Error, "Health check failed to start: " + r.err);
            }
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st.toast(ui::ToastLevel::Error, "Health check failed to start: " +
                     (r.result.is_object() ? jstr(r.result, "error", "unknown")
                                           : std::string("bad response")));
            return;
        }
        S().healthRunning  = true;
        S().lastHealthPoll = 0.0;  // poll immediately on the next frame
    });
}

void pollHealthStatus(AppState& st) {
    if (!S().healthSupported || S().healthPolling) return;
    S().healthPolling  = true;
    S().lastHealthPoll = ImGui::GetTime();
    st.rpc.call("vendors_health_status", {}, [&st](const core::RpcResult& r) {
        S().healthPolling = false;
        // EVERY exit below must leave the state machine consistent: startHealthCheck
        // and both trigger buttons gate on !healthRunning, so a branch that returns
        // with it still set wedges the widget on "Testing…" until the app restarts.
        if (!r.ok) {
            if (r.err.find("unknown method") != std::string::npos)
                S().healthSupported = false;
            else
                st.toast(ui::ToastLevel::Error, "Vendor health check failed: " + r.err);
            S().healthRunning = false;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            // Transport succeeded but the sweep answered {"ok": false, "error": …}
            // (gui_rpc's own except-branch). Same terminal state as a transport error.
            S().healthRunning = false;
            st.toast(ui::ToastLevel::Error, "Vendor health check failed: " +
                     (r.result.is_object() ? jstr(r.result, "error", "unknown")
                                           : std::string("bad response")));
            return;
        }
        S().healthRunning = r.result.value("running", false);
        S().healthChecked = true;
        std::vector<HealthRow> rows;
        if (r.result.contains("results"))
            for (auto& x : r.result["results"]) {
                HealthRow h;
                h.site      = jstr(x, "site", "");
                h.ok        = x.value("ok", false);
                h.expected  = x.value("expected", false);
                h.latencyMs = x.value("latency_ms", 0.0);
                h.error     = jstr(x, "error", "");
                rows.push_back(std::move(h));
            }
        S().healthResults = std::move(rows);
    });
}

namespace {

// palette role → ImVec4 for PushStyleColor. Every colour in this TU is a role lookup
// (scripts/gui_lint.py --rule theme bans literals); this keeps the call sites one line
// instead of the four-line ImVec4 construction that used to be copied per status word.
inline ImVec4 roleV4(const float role[4], float a = 1.0f) {
    return ImVec4(role[0], role[1], role[2], role[3] * a);
}

// One-line coloured status word. The vendor/provider/queue/health rows all print a bare
// state word; painting each by role is what makes a failed row readable at a glance
// instead of dissolving into the surrounding dim chrome.
void textRole(const float role[4], const char* s) {
    ImGui::PushStyleColor(ImGuiCol_Text, roleV4(role));
    ImGui::TextUnformatted(s);
    ImGui::PopStyleColor();
}

// borderRow paints its own label straight into the draw list with palette.text, so
// ImGui::BeginDisabled's alpha never reaches it. Wash the row that was just submitted
// with the pane colour so a gated action visibly reads as disabled, and hang the
// reason off it: a live-looking button whose click the callee silently swallows is
// exactly what made this strip feel broken. Call right AFTER ImGui::EndDisabled().
void gatedRowFeedback(bool gated, const char* why) {
    if (!gated) return;
    const float wash[4] = {palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 0.55f};
    ImGui::GetWindowDrawList()->AddRectFilled(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                                              Palette::toU32(wash), 6.0f * ui::uiScale);
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("%s", why);
}

}  // namespace

// ---- compact strip (fixed footer of the filters pane) --------------------------------

void drawVendorStrip(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    (void)paneW;

    // -- background polling (always runs while the parts workspace is visible) --------
    bool anyLoginBusy = false;
    int  signedIn     = 0;
    for (auto& v : S().vendors) {
        if (v.loginInProgress) anyLoginBusy = true;
        if (v.loggedIn) ++signedIn;
    }
    // While a headed login window is open, poll status so the dot flips green the moment
    // the user closes the window. Edge-triggered + backed off (2 s).
    if (anyLoginBusy && !S().vendorsLoading &&
        ImGui::GetTime() - S().lastVendorPoll > 2.0)
        loadVendors(st, /*force=*/true);
    // Poll the download queue while active jobs exist (backed off; async).
    if (S().queueActive && ImGui::GetTime() - S().lastQueuePoll > 2.5)
        pollQueue(st);
    // Poll health while the backend reports running (single-flight + 2 s backoff).
    // Defer during the embedded login wizard — parallel browser_fetch sweeps starve it.
    if (S().healthRunning && !browserPanelActive() &&
        ImGui::GetTime() - S().lastHealthPoll > 2.0)
        pollHealthStatus(st);

    // Liquid-glass band behind the WHOLE footer. A bare 1px border rule used to sit here and
    // the strip read as a loose stack of controls dropped under a grey line. The footer is ONE
    // section, so it gets one tinted surface in the app's shared glass language (accent hue,
    // low alpha) closed by an accent hairline as its section edge. Painted before the footer's
    // widgets so it stays behind them.
    //
    // kBandInset pulls the band 2 px inside the pane gutter so it reads as a CARD sitting in
    // the pane instead of a full-bleed slab wedged against the pane border; kCtrlInset keeps
    // the full-width controls 4 px inside that card edge — the CTA pill used to be exactly as
    // wide as the band, so its border WAS the band's border.
    const float kBandInset = 2.0f * sc;
    const float kCtrlInset = kBandInset + 4.0f * sc;
    {
        ImDrawList*  dl    = ImGui::GetWindowDrawList();
        const ImVec2 p0    = ImGui::GetCursorScreenPos();
        const ImVec2 avail = ImGui::GetContentRegionAvail();
        const float  bx0   = p0.x + kBandInset;
        const float  bx1   = p0.x + avail.x - kBandInset;
        if (avail.y > 1.0f)
            ui::glassCardWash(dl, ImVec2(bx0, p0.y), ImVec2(bx1, p0.y + avail.y), 8.0f,
                              palette.accent, 0.14f);
        float edge[4];
        Palette::withAlpha(palette.accent, edge, 0.35f);
        dl->AddLine(ImVec2(bx0, p0.y + 0.5f), ImVec2(bx1, p0.y + 0.5f),
                    Palette::toU32(edge), 1.0f);
    }
    // The footer's own vertical rhythm. Under the theme's 9 px paragraph spacing the four
    // stacked rows measured ~122 px against a ~99 px band and the bottom button hung out of
    // the card entirely. 4 px is this footer's one gap — and it is also the clearance above
    // and below the CTA pill. Popped at the very end (this function has no early returns).
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * sc));
    ImGui::Dummy(ImVec2(0.0f, 2.0f * sc));   // 2 + 4 spacing = 6 px of band-top clearance

    // -- status logo/dot row + "N/M signed in" + health summary ------------------------
    // One inset scope: the logos, the count and the health line are plain content, so they
    // sit on the pane's TEXT line with the filter labels and the section header above them.
    // The glass band above and the borderRows below stay full-bleed on the pane gutter.
    {
        ContentInset inset;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 p = ImGui::GetCursorScreenPos();
        // Real vendor favicons wherever the prefetch has landed (S().thumbs["vendorlogo:"+id],
        // filled by another TU — this file never REQUESTS one, it only draws what is resident).
        // ONE cell size drives both the fit arithmetic below and the draw, so the run stays
        // homogeneous: sizing the layout on the 7 px dot while drawing 14 px images is exactly
        // what would overflow the pane again. The moment any logo is resident the whole row
        // switches to the wide cell; vendors still without one keep their 7 px pip centred in
        // it.
        bool anyLogoCached = false;
        for (auto& lv : S().vendors) {
            auto it = S().thumbs.find("vendorlogo:" + lv.id);
            if (it != S().thumbs.end() && it->second.tex) { anyLogoCached = true; break; }
        }
        const float cell = anyLogoCached ? 14.0f * sc : 7.0f * sc;
        const float dotD = 7.0f * sc;          // the pip itself never grows with the cell
        const float gap  = 4.0f * sc;
        const float step = cell + gap;
        const float cy   = p.y + 5.0f * sc + cell * 0.5f;

        // How many cells FIT. One cell per vendor ran straight off the pane and wrapped into
        // a second ragged row that collided with the count text; the run is now clipped to
        // the space left beside the count and the remainder collapses into a dim "+N".
        const int total = (int)S().vendors.size();
        char countNum[32];
        std::snprintf(countNum, sizeof(countNum), "%d/%d", signedIn, total);
        const char* const countTail = " signed in";
        const float countW = total > 0
            ? ImGui::CalcTextSize(countNum).x + ImGui::CalcTextSize(countTail).x
            : ImGui::CalcTextSize("no vendors").x;
        // kCtrlInset is reserved on the right so the count text stops short of the band's
        // inset edge instead of running onto (or past) it.
        const float budget =
            ImGui::GetContentRegionAvail().x - countW - 8.0f * sc - kCtrlInset;
        auto fitCount = [&](float b) {
            if (b < cell) return 0;
            const int n = (int)((b + gap) / step);
            return n > total ? total : n;
        };
        int shown = fitCount(budget);
        char overflow[16];
        overflow[0] = '\0';
        if (shown < total) {
            // Second pass: the "+N" badge needs room of its own, or the badge itself
            // becomes the thing that pushes the count off the pane edge.
            std::snprintf(overflow, sizeof(overflow), "+%d", total - shown);
            shown = fitCount(budget - ImGui::CalcTextSize(overflow).x - gap);
            std::snprintf(overflow, sizeof(overflow), "+%d", total - shown);
            if (shown >= total) overflow[0] = '\0';
        }

        float x = p.x;   // left edge of the next cell
        const double now = ImGui::GetTime();
        for (int i = 0; i < shown; ++i) {
            auto& v = S().vendors[(size_t)i];
            const float* role = v.loggedIn ? palette.statusOk : palette.textDisabled;
            if (!v.loginError.empty()) role = palette.statusErr;
            auto lg = S().thumbs.find("vendorlogo:" + v.id);
            if (lg != S().thumbs.end() && lg->second.tex) {
                lg->second.lastUse = now;   // LRU: keep on-screen logos resident
                dl->AddImage((ImTextureID)(intptr_t)lg->second.tex,
                             ImVec2(x, cy - cell * 0.5f),
                             ImVec2(x + cell, cy + cell * 0.5f));
                // Status ring around the favicon, in the SAME role the pip carried, so the
                // row still reads signed-in / failed / idle at a glance once it shows brands.
                dl->AddCircle(ImVec2(x + cell * 0.5f, cy), 9.0f * sc, Palette::toU32(role),
                              0, 1.5f * sc);
            } else {
                // Soft glow ring under the filled dot: a signed-in vendor reads as LIT instead
                // of as a flat 7px pip lost in the band.
                float glow[4];
                Palette::withAlpha(role, glow, 0.28f);
                dl->AddCircle(ImVec2(x + cell * 0.5f, cy), dotD * 0.5f * 1.6f,
                              Palette::toU32(glow), 0, 1.5f * sc);
                dl->AddCircleFilled(ImVec2(x + cell * 0.5f, cy), dotD * 0.5f,
                                    Palette::toU32(role));
            }
            // Invisible hover target per cell for the tooltip.
            ImGui::SetCursorScreenPos(ImVec2(x, cy - cell * 0.5f));
            ImGui::PushID(i);
            ImGui::InvisibleButton("##dot", ImVec2(cell, cell));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s — %s", v.label.c_str(),
                                  !v.loginError.empty() ? v.loginError.c_str()
                                  : v.loggedIn          ? "signed in"
                                                        : "not signed in");
            ImGui::PopID();
            x += step;
        }
        float runEnd = shown > 0 ? x - gap : p.x;
        if (overflow[0]) {
            const ImVec2 ts = ImGui::CalcTextSize(overflow);
            dl->AddText(ImVec2(runEnd + gap, cy - ts.y * 0.5f),
                        Palette::toU32(palette.textDisabled), overflow);
            runEnd += gap + ts.x;
        }

        ImGui::SetCursorScreenPos(ImVec2(runEnd + (runEnd > p.x ? 6.0f * sc : 0.0f),
                                         p.y + 3.0f * sc));
        if (total > 0) {
            // The count is this line's real information: the numbers carry full text
            // weight and only the word after them stays dim.
            textRole(palette.text, countNum);
            ImGui::SameLine(0.0f, 0.0f);
            textRole(palette.textDim, countTail);
        } else {
            ImGui::TextDisabled("no vendors");
        }

        if (!S().healthSupported) {
            ImGui::TextDisabled("Health check unavailable (update the worker)");
        } else if (S().healthStarting || (S().healthRunning && S().healthResults.empty())) {
            spinner("Testing vendors");
        } else if (S().healthChecked && !S().healthResults.empty()) {
            int ok = 0, needLogin = 0, failing = 0;
            for (auto& h : S().healthResults) {
                if (h.ok) ++ok;
                else if (h.expected) ++needLogin;
                else ++failing;
            }
            // Coloured tokens. The whole summary used to be one dim sentence, so a failing
            // vendor read exactly like the inert labels around it.
            auto token = [&](const float* numRole, int n, const char* word, bool lead) {
                if (lead) {
                    ImGui::SameLine(0.0f, 5.0f * sc);
                    textRole(palette.textDisabled, "·");
                }
                char b[16];
                std::snprintf(b, sizeof(b), "%d", n);
                ImGui::SameLine(0.0f, 5.0f * sc);
                textRole(numRole, b);
                ImGui::SameLine(0.0f, 4.0f * sc);
                textRole(palette.textDim, word);
            };
            textRole(palette.textDim, "Health:");
            token(palette.statusOk,   ok,        "ok",           false);
            token(palette.statusWarn, needLogin, "need sign-in", true);
            if (failing > 0)
                token(palette.statusErr, failing, "failing", true);
        } else {
            ImGui::TextDisabled(" ");
        }
    }

    // -- buttons -------------------------------------------------------------------------
    // Anything the callee would refuse is drawn disabled and says why.
    const bool wizardOpen = browserPanelActive();
    // Primary CTA — accent gradient pill. borderRow paints hover/selection treatment only,
    // so this used to be a flat accentSoft rect that was pixel-identical to the two
    // secondary buttons below it and the pane had no visible primary action. Same
    // InvisibleButton click contract, same startLoginWizard(st) action, same wizard gating.
    {
        ImGui::Indent(kCtrlInset);
        ImDrawList*  dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetCursorScreenPos();
        const float  w  = ImGui::GetContentRegionAvail().x - kCtrlInset;
        const float  h  = 22.0f * sc;
        const ImVec2 c1(c0.x + w, c0.y + h);

        ImGui::BeginDisabled(wizardOpen);
        const bool clicked = ImGui::InvisibleButton("##connect_vendors", ImVec2(w, h));
        const bool hovered = ImGui::IsItemHovered();
        ImGui::EndDisabled();

        ui::glassCardWash(dl, c0, c1, 7.0f, palette.accent, hovered ? 0.42f : 0.30f);
        float brd[4];
        Palette::withAlpha(palette.accent, brd, hovered ? 0.95f : 0.70f);
        dl->AddRect(ImVec2(c0.x + 0.5f, c0.y + 0.5f), ImVec2(c1.x - 0.5f, c1.y - 0.5f),
                    Palette::toU32(brd), 7.0f * sc, 0, 1.0f * sc);

        // Icon + centred label, laid out exactly like borderRow(centerText=true).
        const char*  icon     = "browser_panel";
        const char*  label    = "Connect vendor accounts";
        const float  iconSz   = 18.0f * sc;
        const float  iconGap  = 6.0f * sc;
        const bool   haveIcon = ui::hasIconSdf(icon) || ui::getIcon(icon).valid;
        const ImVec2 ts       = ImGui::CalcTextSize(label);
        float x = c0.x + (w - (ts.x + (haveIcon ? iconSz + iconGap : 0.0f))) * 0.5f;
        if (haveIcon) {
            const ImU32 icol =
                ui::hasIconSdf(icon)
                    ? Palette::toU32(palette.text)
                    : IM_COL32(255, 255, 255, 255);  // gui-lint: allow identity tint, not a color — raster atlas glyphs keep their own pixels
            ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, c0.y + h * 0.5f), iconSz,
                              icol, 0);
            x += iconSz + iconGap;
        }
        dl->PushClipRect(c0, c1, true);
        dl->AddText(ImVec2(x, c0.y + (h - ts.y) * 0.5f), Palette::toU32(palette.text), label);
        dl->PopClipRect();

        if (clicked) startLoginWizard(st);
        ImGui::Unindent(kCtrlInset);
    }
    gatedRowFeedback(wizardOpen, "The sign-in wizard is already open.");

    ImGui::Indent(kCtrlInset);
    const float half = (ImGui::GetContentRegionAvail().x - kCtrlInset - 4.0f * sc) * 0.5f;
    ImGui::BeginDisabled(wizardOpen);
    if (borderRow("##vendors_popup", "Vendors…", nullptr, S().vendorsPopupOpen,
                  ImVec2(half, 20.0f * sc), /*centerText=*/true))
        S().vendorsPopupOpen = !S().vendorsPopupOpen;
    ImGui::EndDisabled();
    gatedRowFeedback(wizardOpen, "Vendor Accounts stays hidden while the sign-in "
                                 "wizard owns the results column.");
    ImGui::SameLine(0.0f, 4.0f * sc);
    const bool healthBusy  = S().healthStarting || S().healthRunning;
    const bool healthGated = healthBusy || wizardOpen || !S().healthSupported;
    ImGui::BeginDisabled(healthGated);
    if (borderRow("##test_vendors", healthBusy ? "Testing…" : "Test vendors", nullptr,
                  /*selected=*/false, ImVec2(half, 20.0f * sc), /*centerText=*/true))
        startHealthCheck(st);
    ImGui::EndDisabled();
    gatedRowFeedback(healthGated,
                     !S().healthSupported ? "Health check unavailable — update the worker."
                     : healthBusy         ? "A health check is already running."
                                          : "Paused while the sign-in wizard is open — its "
                                            "browser sweeps would starve the check.");
    ImGui::Unindent(kCtrlInset);
    ImGui::PopStyleVar();   // footer ItemSpacing
}

// ---- floating "Vendor Accounts" popup ------------------------------------------------

namespace {

void drawVendorSignInList(AppState& st) {
    const float sc = ui::uiScale;
    sectionHeader("Vendor Sites", /*firstInPane=*/true, "vendor_globe");

    if (S().vendorsLoading && S().vendors.empty()) {
        ContentInset inset;
        spinner("Checking");
    } else if (S().vendors.empty()) {
        ContentInset inset;
        ImGui::TextDisabled("No vendor adapters registered.");
    } else {
        // A logo lane is reserved for EVERY row as soon as any one favicon is resident
        // (S().thumbs["vendorlogo:"+id], prefetched elsewhere — this file only draws what is
        // already there). Reserving per-row instead would leave the "[+]" column ragged; with
        // nothing cached at all the list falls back to exactly its old presentation.
        const float logoSz = 18.0f * sc;
        bool anyLogo = false;
        for (auto& lv : S().vendors) {
            auto it = S().thumbs.find("vendorlogo:" + lv.id);
            if (it != S().thumbs.end() && it->second.tex) { anyLogo = true; break; }
        }
        const float logoLane = anyLogo ? logoSz + 6.0f * sc : 0.0f;
        for (auto& v : S().vendors) {
            ImGui::PushID(v.id.c_str());
            // Row rect measured exactly as borderRowSurface measures it, so a failed
            // sign-in can be washed red over the WHOLE row: a broken vendor has to be
            // visible without reading the state word at the bottom of the row.
            const ImVec2 rmn = ImGui::GetCursorScreenPos();
            const ImVec2 rmx(rmn.x + ImGui::GetContentRegionAvail().x,
                             rmn.y + ImGui::GetFrameHeight() * 2.0f);
            borderRowSurface(false, 2.0f, 7.0f);
            if (!v.loginError.empty())
                ui::glassCardWash(ImGui::GetWindowDrawList(), rmn, rmx, 7.0f,
                                  palette.statusErr, 0.18f);
            ContentInset inset;   // row content, inside the full-bleed surface above
            // Favicon lead-in, painted straight into the draw list at the row origin with the
            // row's content indented past it — so the state word on the row's SECOND line
            // clears the logo too instead of tucking under it.
            if (logoLane > 0.0f) {
                auto lg = S().thumbs.find("vendorlogo:" + v.id);
                if (lg != S().thumbs.end() && lg->second.tex) {
                    lg->second.lastUse = ImGui::GetTime();  // LRU: keep on-screen logos resident
                    const ImVec2 lp = ImGui::GetCursorScreenPos();
                    const float  ly = lp.y + (ImGui::GetTextLineHeight() - logoSz) * 0.5f;
                    ImGui::GetWindowDrawList()->AddImage(
                        (ImTextureID)(intptr_t)lg->second.tex,
                        ImVec2(lp.x, ly), ImVec2(lp.x + logoSz, ly + logoSz));
                }
                ImGui::Indent(logoLane);
            }
            ImGui::BeginGroup();

            ImVec4 dot = v.loggedIn
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.statusErr[0], palette.statusErr[1],
                         palette.statusErr[2], 0.85f);
            if (v.loginInProgress)
                dot = ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(v.loggedIn ? "[+]" : (v.loginInProgress ? "[~]" : "[ ]"));
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(v.label.c_str());
            const char* lbl = v.loginInProgress ? "..." : (v.loggedIn ? "Re-login" : "Sign in");
            // Right-aligned on the row's content edge, measured: the old fixed 80 px left
            // "Sign in" and "Re-login" ending at different x and ignored the right inset.
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                                 ImGui::CalcTextSize(lbl).x -
                                 ImGui::GetStyle().FramePadding.x * 2.0f -
                                 ui::metrics::partsPadX());
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            // Magento + invisible reCAPTCHA (Anaheim) scores the in-panel OSR CEF
            // as a bot under software GL — open real desktop Chromium for those.
            // Everyone else gets the embedded CEF wizard first. One rule, one
            // definition (parts_browser_panel.cpp).
            const bool preferDesktop = sitePrefersDesktopLogin(v.id);
            // A row already mid sign-in must LOOK unavailable: this button used to
            // stay live while startLoginWizard's active guard ate the click.
            ImGui::BeginDisabled(v.loginInProgress);
            if (ImGui::SmallButton(lbl)) {
                if (preferDesktop) {
                    startVendorLogin(st, v);
                    st.toast(ui::ToastLevel::Info,
                             "System Chrome opened (no automation). Allow cookies, sign in, "
                             "wait for My Account, then close the window.");
                } else {
                    startLoginWizard(st, v.id);
                }
            }
            ImGui::EndDisabled();
            if (v.loginInProgress && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("Sign-in already in progress");
            ImGui::PopStyleColor();

            // The state word carries the row's state: one dim colour for all four made
            // "Login failed" read exactly like "Signed in".
            if (v.loginInProgress)
                textRole(palette.statusWarn, "  Waiting for browser sign-in...");
            else if (!v.loginError.empty())
                textRole(palette.statusErr, "  Login failed (hover)");
            else
                textRole(v.loggedIn ? palette.statusOk : palette.textDisabled,
                         v.loggedIn ? "  Signed in" : "  Not signed in");
            if (!v.loginError.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", v.loginError.c_str());

            ImGui::EndGroup();
            if (logoLane > 0.0f) ImGui::Unindent(logoLane);
            ImGui::Spacing();
            ImGui::PopID();
        }
    }
}

void drawApiProviders(AppState& st) {
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("API Providers", /*firstInPane=*/false, "settings");

    if (S().apiProviders.empty()) {
        ContentInset inset;
        ImGui::TextDisabled("No API providers available.");
        return;
    }
    for (auto& a : S().apiProviders) {
        ImGui::PushID(a.id.c_str());
        borderRowSurface(false, a.editing ? (2.2f + (float)a.fields.size()) : 1.6f, 7.0f);
        ContentInset inset;   // row content, inside the full-bleed surface above
        ImGui::BeginGroup();

        ImVec4 dot = a.configured
            ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                     palette.statusOk[2],  1.0f)
            : ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                     palette.textDisabled[2], 0.9f);
        ImGui::PushStyleColor(ImGuiCol_Text, dot);
        ImGui::TextUnformatted(a.configured ? "[+]" : "[ ]");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::TextUnformatted(a.label.c_str());
        const char* albl = a.editing ? "Close" : (a.configured ? "Edit" : "Set up");
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                             ImGui::CalcTextSize(albl).x -
                             ImGui::GetStyle().FramePadding.x * 2.0f -
                             ui::metrics::partsPadX());
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.button[0], palette.button[1],
                   palette.button[2], 0.80f));
        if (ImGui::SmallButton(albl))
            a.editing = !a.editing;
        ImGui::PopStyleColor();

        textRole(a.configured ? palette.statusOk : palette.textDisabled,
                 a.configured ? "  Configured" : "  Not configured");

        if (a.editing) {
            for (size_t i = 0; i < a.fields.size() && i < 2; ++i) {
                ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x -
                                        ui::metrics::partsPadX());
                ImGui::InputTextWithHint(
                    (std::string("##cred_") + a.fields[i]).c_str(),
                    a.fields[i].c_str(), a.fieldBuf[i], sizeof(a.fieldBuf[i]),
                    ImGuiInputTextFlags_Password);
            }
            if (a.saving) {
                spinner("Saving");
            } else if (ImGui::SmallButton("Save")) {
                saveApiCreds(st, a);
            }
        }

        ImGui::EndGroup();
        ImGui::Spacing();
        ImGui::PopID();
    }
}

void drawDownloadQueue(AppState& st) {
    const float sc = ui::uiScale;
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Download Queue", /*firstInPane=*/false, "save");

    if (S().queue.empty()) {
        ContentInset inset;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No downloads pending.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
        return;
    }
    {
        ContentInset inset;
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
    }
    ImGui::Spacing();
    for (auto& q : S().queue) {
        ImGui::PushID(q.id.c_str());
        // Status-coloured wash over the row rect (measured as borderRowSurface measures
        // it): the queue is scanned, not read, so state has to be a colour first.
        const float* qt = q.status == "done"    ? palette.statusOk
                        : q.status == "running" ? palette.accent
                        : q.status == "error"   ? palette.statusErr
                                                : palette.statusWarn;
        const ImVec2 qmn = ImGui::GetCursorScreenPos();
        const ImVec2 qmx(qmn.x + ImGui::GetContentRegionAvail().x,
                         qmn.y + ImGui::GetFrameHeight() * 1.8f);
        borderRowSurface(false, 1.8f, 7.0f);
        ui::coloredRowWash(ImGui::GetWindowDrawList(), qmn, qmx, 6.0f, qt, 0.28f);
        ContentInset inset;   // row content, inside the full-bleed surface above
        ImGui::BeginGroup();
        std::string label = q.query;
        if (label.size() > 34) label = label.substr(0, 31) + "...";
        ImGui::Text("%-34.34s", label.c_str());
        ImGui::SameLine();
        ImGui::TextDisabled("%s", q.site.c_str());
        if (q.status == "done") {
            textRole(palette.statusOk, "  Done");
            if (!q.artifactPath.empty()) {
                ImGui::SameLine();
                if (q.importing) {
                    spinner("Importing");
                } else if (ImGui::SmallButton("Import into doc")) {
                    q.importing = true;
                    nlohmann::json p;
                    p["artifact_path"] = q.artifactPath;
                    st.rpc.call("parts_import_artifact", p,
                        [&st, id = q.id](const core::RpcResult& r) {
                            for (auto& j : S().queue)
                                if (j.id == id) j.importing = false;
                            bool ok = r.ok && r.result.is_object() &&
                                      r.result.value("ok", false);
                            if (ok) {
                                st.toast(ui::ToastLevel::Success,
                                    "Imported as " + jstr(r.result, "doc_id", "doc"));
                            } else {
                                std::string why = r.ok
                                    ? jstr(r.result, "reason", "import failed")
                                    : r.err;
                                st.toast(ui::ToastLevel::Error, "Import failed: " + why);
                            }
                        });
                }
            }
        } else if (q.status == "error") {
            textRole(palette.statusErr, "  Failed");
            if (!q.error.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", q.error.c_str());
        } else if (q.status == "cancelled") {
            // Cancelled is a non-success terminal state, so it takes the middle role
            // rather than dissolving into the row chrome like a caption.
            textRole(palette.statusWarn, "  Cancelled");
        } else {
            float ph = (float)(0.5 + 0.5 * sin(ImGui::GetTime() * 2.5));
            ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
                ImVec4(palette.accent[0], palette.accent[1],
                       palette.accent[2], 0.80f));
            ImGui::ProgressBar(ph, ImVec2(-1.0f, 4.0f * sc),
                               q.status == "running" ? "downloading" : "queued");
            ImGui::PopStyleColor();
        }
        ImGui::EndGroup();
        ImGui::Spacing();
        ImGui::PopID();
    }
}

void drawHealthTable(AppState& st) {
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Vendor Health", /*firstInPane=*/false, "health_check");
    const float sc = ui::uiScale;

    if (!S().healthSupported) {
        ContentInset inset;
        ImGui::TextDisabled("Health check unavailable — the worker does not support it yet.");
        return;
    }
    // The popup can no longer be on screen while the wizard owns the results column
    // (drawVendorsPopup stands down), so "busy" is the only reachable gate here.
    const bool busy = S().healthStarting || S().healthRunning;
    // Everything below is plain content (no full-bleed row surface), so one scope puts the
    // button, the states and every result row on the section's text line.
    ContentInset inset;
    ImGui::BeginDisabled(busy);
    if (ImGui::SmallButton(busy ? "Running…" : "Run health check"))
        startHealthCheck(st);
    ImGui::EndDisabled();
    if (busy && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("A health check is already running.");
    if (busy && S().healthResults.empty()) {
        spinner("Testing");
        return;
    }
    if (S().healthResults.empty()) {
        ImGui::TextDisabled("Not tested this session.");
        return;
    }
    ImGui::Spacing();
    for (auto& h : S().healthResults) {
        ImGui::PushID(h.site.c_str());
        // State reads as a coloured dot rather than a bracket glyph: green ok, amber
        // expected sign-in-required failure, red real error.
        const float* role = h.ok ? palette.statusOk
                          : h.expected ? palette.statusWarn
                                       : palette.statusErr;
        const float dotD  = 8.0f * sc;
        const float lineH = ImGui::GetTextLineHeight();
        ImDrawList* dl    = ImGui::GetWindowDrawList();
        const ImVec2 c    = ImGui::GetCursorScreenPos();
        dl->AddCircleFilled(ImVec2(c.x + dotD * 0.5f, c.y + lineH * 0.5f), dotD * 0.5f,
                            Palette::toU32(role));
        ImGui::Dummy(ImVec2(dotD, lineH));
        ImGui::SameLine(0.0f, 6.0f * sc);
        ImGui::TextUnformatted(h.site.c_str());
        char rt[32];
        if (h.ok) std::snprintf(rt, sizeof(rt), "%.0f ms", h.latencyMs);
        else      std::snprintf(rt, sizeof(rt), "%s",
                                h.expected ? "sign-in required" : "error");
        // Right-aligned on the section's content edge, measured: the old fixed 78 px left
        // "12 ms" and "sign-in required" ending at different x.
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                             ImGui::CalcTextSize(rt).x - ui::metrics::partsPadX());
        textRole(palette.textDim, rt);
        if (!h.error.empty() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", h.error.c_str());
        ImGui::PopID();
    }
}

}  // namespace

void drawVendorsPopup(AppState& st) {
    // The embedded wizard owns the results column — never float this on top of it.
    if (!S().vendorsPopupOpen || browserPanelActive()) return;
    const float sc = ui::uiScale;
    const float popW = 420.0f * sc;

    ImGui::SetNextWindowSize(ImVec2(popW, 540.0f * sc), ImGuiCond_FirstUseEver);
    ui::pushGlassWindowStyle();
    if (ImGui::Begin("Vendor Accounts##parts_vendors_popup", &S().vendorsPopupOpen,
                     ImGuiWindowFlags_NoCollapse)) {
        drawVendorSignInList(st);
        drawApiProviders(st);
        drawDownloadQueue(st);
        drawHealthTable(st);
    }
    ImGui::End();
    ui::popGlassWindowStyle();
}

}  // namespace parts
}  // namespace ui
